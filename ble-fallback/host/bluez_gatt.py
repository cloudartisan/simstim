"""BlueZ GATT byte-stream peripheral (explicit opt-in; never auto-started).

No adapter power, scan, or advertisement calls are made by this module. The
operator must separately arrange a connectable peripheral and register the app.
"""
from __future__ import annotations
import asyncio
import struct
import os
import time
from dataclasses import dataclass
from tls_control_bridge import TLSControlBridge

SERVICE_UUID = "7c5a0001-82d8-4f3a-9a10-4b1510000001"
UPLINK_UUID = "7c5a0002-82d8-4f3a-9a10-4b1510000001"
DOWNLINK_UUID = "7c5a0003-82d8-4f3a-9a10-4b1510000001"
MAX_PAYLOAD = 512
MAX_QUEUED = 8192

class BusyError(RuntimeError): pass

@dataclass(frozen=True)
class Fragment:
    sequence: int
    payload: bytes

def payload_limit_for_mtu(mtu: int) -> int:
    """Return payload bytes per framed ATT notification/write."""
    if not 23 <= mtu <= 517:
        raise ValueError("invalid negotiated ATT MTU")
    return min(MAX_PAYLOAD, mtu - 3 - 4)


def encode_fragment(sequence: int, payload: bytes) -> bytes:
    if not 0 <= sequence <= 0xffff or not 0 < len(payload) <= MAX_PAYLOAD:
        raise ValueError("invalid BLE fragment")
    return struct.pack(">HH", sequence, len(payload)) + payload

class OrderedFragments:
    """Reject malformed, duplicated, skipped, replayed and reordered fragments."""
    def __init__(self): self.expected = 0
    def decode(self, raw: bytes) -> Fragment:
        if len(raw) < 5: raise ValueError("short fragment")
        seq, size = struct.unpack(">HH", raw[:4])
        if not 0 < size <= MAX_PAYLOAD or len(raw) != size + 4:
            raise ValueError("invalid fragment length")
        if seq != self.expected: raise ValueError("duplicate, skipped, or out-of-order fragment")
        self.expected = (self.expected + 1) & 0xffff
        return Fragment(seq, raw[4:])

class Owner:
    """Exclusive transport owner; controls remain blocked until TLS is authenticated."""
    def __init__(self):
        self.device = None
        self.authenticated = False
        self._lock = asyncio.Lock()
        self._retry_count = 0
        self._retry_after = 0.0
        self.backoff_s = 0
    async def acquire(self, device):
        async with self._lock:
            if self.device is not None: raise BusyError("BLE transport already owned")
            if time.monotonic() < self._retry_after:
                raise BusyError("BLE reconnect backoff active")
            self.device, self.authenticated = device, False
    def mark_tls_authenticated(self, device, identity: str, cipher: str):
        if self.device != device or identity != os.environ.get("SIMSTIM_PEER_IDENTITY", "device") or not cipher:
            raise PermissionError("TLS peer/owner authentication failed")
        self.authenticated = True
        self._retry_count = 0
        self.backoff_s = 0
    def release(self, device):
        if self.device == device:
            self.device, self.authenticated = None, False
            self._retry_count = min(self._retry_count + 1, 5)
            self.backoff_s = min(30, 2 ** (self._retry_count - 1))
            self._retry_after = time.monotonic() + self.backoff_s

class BoundedByteQueue:
    def __init__(self, cap=MAX_QUEUED): self.cap, self._data, self._condition = cap, bytearray(), asyncio.Condition()
    async def clear(self):
        async with self._condition:
            self._data.clear(); self._condition.notify_all()
    async def put(self, data: bytes):
        if len(data) > self.cap: raise ValueError("write exceeds queue budget")
        async with self._condition:
            await self._condition.wait_for(lambda: len(self._data)+len(data) <= self.cap)
            self._data.extend(data); self._condition.notify_all()
    async def take(self, limit: int) -> bytes:
        async with self._condition:
            await self._condition.wait_for(lambda: bool(self._data))
            out = bytes(self._data[:limit]); del self._data[:len(out)]; self._condition.notify_all(); return out

# BlueZ classes are built lazily: importing this module is side-effect free.
def create_application(owner=None, *, control=None, tls=None):
    """Create BlueZ objects without registering or activating them.

    Supply the existing ``service.Control`` and ``service.OpenSSLPSK`` objects
    to enable the authenticated stream bridge. Their absence makes this
    function refuse to expose a command-capable GATT application.
    """
    if control is None or tls is None:
        raise ValueError("existing service.Control and authenticated TLS policy are required")
    try:
        from dbus_next import Variant
        from dbus_next.service import ServiceInterface, method, dbus_property, PropertyAccess
    except ImportError as exc: raise RuntimeError("install dbus-next for opt-in BlueZ GATT service") from exc
    owner = owner or Owner(); bridge = TLSControlBridge(control, tls, owner)
    owner.bridge = bridge
    up_q, down_q = BoundedByteQueue(), BoundedByteQueue()
    path = "/org/portable/device_gateway"
    inbound, outbound = OrderedFragments(), OrderedFragments()
    # ATT MTU includes 3 bytes of ATT overhead and our framing adds 4 bytes.
    # BlueZ supplies the negotiated MTU in WriteValue options; default to the
    # mandatory ATT MTU (23) until the first write establishes it.
    fragment_limit = [min(MAX_PAYLOAD, 23 - 3 - 4)]

    class Characteristic(ServiceInterface):
        def __init__(self, uuid, flags, queue=None):
            super().__init__("org.bluez.GattCharacteristic1")
            self.uuid, self.flags, self.queue, self.notifying, self.value = uuid, flags, queue, False, b""
        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s": return self.uuid
        @dbus_property(access=PropertyAccess.READ)
        def Service(self) -> "o": return path+"/service0"
        @dbus_property(access=PropertyAccess.READ)
        def Flags(self) -> "as": return self.flags
        @dbus_property(access=PropertyAccess.READ)
        def Value(self) -> "ay": return self.value
        @dbus_property(access=PropertyAccess.READ)
        def Notifying(self) -> "b": return self.notifying
        @method()
        async def WriteValue(self, value: "ay", options: "a{sv}"):
            if self.queue is None: raise PermissionError("write not supported")
            devopt = options.get("device")
            if devopt is None: raise PermissionError("BlueZ did not identify peer")
            peer = devopt.value
            mtu_opt = options.get("mtu")
            mtu = int(mtu_opt.value) if mtu_opt is not None else 23
            if not 23 <= mtu <= 517:
                raise ValueError("invalid negotiated ATT MTU")
            fragment_limit[0] = payload_limit_for_mtu(mtu)
            control.diagnostics["ble_mtu"] = mtu
            control.diagnostics["ble_fragment_limit"] = fragment_limit[0]
            acquired = owner.device is None
            if acquired: await owner.acquire(peer)
            if owner.device != peer: raise BusyError("another central owns BLE")
            try:
                if bridge.peer is None:
                    # Sequence state belongs to one TLS session and may reset
                    # only after teardown. Never carry queued TLS data across
                    # authenticated sessions.
                    await up_q.clear(); await down_q.clear()
                    inbound.expected = outbound.expected = 0
                frag = inbound.decode(bytes(value))
                if len(frag.payload) > fragment_limit[0]:
                    raise ValueError("fragment exceeds negotiated MTU payload limit")
                bridge.start(peer, asyncio.get_running_loop(), up_q, down_q)
                await bridge.feed_uplink(frag.payload)
            except Exception:
                if acquired: owner.release(peer)
                raise
        @method()
        async def StartNotify(self): self.notifying = True
        @method()
        async def StopNotify(self): self.notifying = False

    class Service(ServiceInterface):
        def __init__(self): super().__init__("org.bluez.GattService1")
        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s": return SERVICE_UUID
        @dbus_property(access=PropertyAccess.READ)
        def Primary(self) -> "b": return True
        @dbus_property(access=PropertyAccess.READ)
        def Characteristics(self) -> "ao": return [path+"/service0/char0", path+"/service0/char1"]
    svc, up, down = Service(), Characteristic(UPLINK_UUID,["write","write-without-response"],up_q), Characteristic(DOWNLINK_UUID,["notify"])
    class ObjectManager(ServiceInterface):
        def __init__(self): super().__init__("org.freedesktop.DBus.ObjectManager")
        @method()
        def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
            return {
                path+"/service0": {"org.bluez.GattService1": {
                    "UUID": Variant("s", SERVICE_UUID), "Primary": Variant("b", True),
                    "Characteristics": Variant("ao", [path+"/service0/char0", path+"/service0/char1"])}},
                path+"/service0/char0": {"org.bluez.GattCharacteristic1": {
                    "UUID": Variant("s", UPLINK_UUID), "Service": Variant("o", path+"/service0"),
                    "Flags": Variant("as", ["write", "write-without-response"]),
                    "Value": Variant("ay", b""), "Notifying": Variant("b", False)}},
                path+"/service0/char1": {"org.bluez.GattCharacteristic1": {
                    "UUID": Variant("s", DOWNLINK_UUID), "Service": Variant("o", path+"/service0"),
                    "Flags": Variant("as", ["notify"]), "Value": Variant("ay", b""),
                    "Notifying": Variant("b", False)}}}
    manager = ObjectManager()
    async def notify_worker():
        while True:
            if not down.notifying or bridge.peer is None:
                await asyncio.sleep(.05); continue
            payload = await down_q.take(fragment_limit[0])
            down.value = encode_fragment(outbound.expected,payload); outbound.expected=(outbound.expected+1)&0xffff
            down.emit_properties_changed({"Value": down.value})
    objects={path:manager,path+"/service0":svc,path+"/service0/char0":up,path+"/service0/char1":down}
    return objects, owner, up_q, down_q, notify_worker

def register_application(bus, adapter_path, objects):
    """Export the object tree and ask BlueZ to register its GATT app.

    Registration is deliberately a caller-invoked operation: constructing the
    app never powers an adapter, advertises or scans. The caller must have
    selected an adapter and an authorized device session policy.
    """
    try:
        from dbus_next import Message, MessageType
    except ImportError as exc:
        raise RuntimeError("install dbus-next for opt-in BlueZ GATT registration") from exc
    app_path = "/org/portable/device_gateway"
    for path, interface in objects.items():
        bus.export(path, interface)
    msg = Message(destination="org.bluez", path=adapter_path,
                  interface="org.bluez.GattManager1", member="RegisterApplication",
                  signature="oa{sv}", body=[app_path, {}], message_type=MessageType.METHOD_CALL)
    async def finish():
        try:
            reply = await bus.call(msg)
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(reply.error_name or "BlueZ RegisterApplication failed")
        except Exception:
            for path in objects:
                bus.unexport(path)
            raise
    return finish()


def create_advertisement():
    """Build the matching LEAdvertisement1 object; do not register it."""
    try:
        from dbus_next import Variant
        from dbus_next.service import ServiceInterface, method, dbus_property, PropertyAccess
    except ImportError as exc:
        raise RuntimeError("install dbus-next for opt-in BlueZ advertisement") from exc

    class Advertisement(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.LEAdvertisement1")

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s": return "peripheral"

        @dbus_property(access=PropertyAccess.READ)
        def ServiceUUIDs(self) -> "as": return [SERVICE_UUID]

        @dbus_property(access=PropertyAccess.READ)
        def LocalName(self) -> "s": return "Device-Gateway"

        @dbus_property(access=PropertyAccess.READ)
        def Includes(self) -> "as": return ["tx-power"]

        @method()
        def Release(self): pass

    return Advertisement()


async def install_disconnect_watcher(bus, owner, bridge):
    """Watch BlueZ Device1.Connected and retire a lost peer's TLS stream."""
    from dbus_next import Message, MessageType
    match = "type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.Device1'"
    reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[match],
        message_type=MessageType.METHOD_CALL))
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(reply.error_name or "failed to subscribe to BlueZ disconnects")
    def on_message(message):
        if message.message_type != MessageType.SIGNAL or message.interface != "org.freedesktop.DBus.Properties" or message.member != "PropertiesChanged":
            return
        if len(message.body) < 2 or message.body[0] != "org.bluez.Device1":
            return
        connected = message.body[1].get("Connected")
        peer = owner.device
        if peer and message.path == peer and connected is not None and connected.value is False:
            bridge.close(peer, cause="bluez_connected_false")
            owner.release(peer)
        return
    bus.add_message_handler(on_message)
    return on_message


def remove_disconnect_watcher(bus, handler):
    if handler is not None:
        bus.remove_message_handler(handler)


async def register_advertisement(bus, adapter_path, advertisement):
    """Explicitly register an already exported LEAdvertisement1 object."""
    try:
        from dbus_next import Message, MessageType
    except ImportError as exc:
        raise RuntimeError("install dbus-next for opt-in BlueZ advertisement") from exc
    msg = Message(destination="org.bluez", path=adapter_path,
                  interface="org.bluez.LEAdvertisingManager1", member="RegisterAdvertisement",
                  signature="oa{sv}", body=["/org/portable/device_gateway/advertisement0", {}],
                  message_type=MessageType.METHOD_CALL)
    reply = await bus.call(msg)
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(reply.error_name or "BlueZ RegisterAdvertisement failed")


async def unregister_advertisement(bus, adapter_path, advertisement_path):
    """Explicitly unregister advertisement; caller controls adapter lifecycle."""
    try:
        from dbus_next import Message, MessageType
    except ImportError as exc:
        raise RuntimeError("install dbus-next for opt-in BlueZ advertisement") from exc
    msg = Message(destination="org.bluez", path=adapter_path,
                  interface="org.bluez.LEAdvertisingManager1", member="UnregisterAdvertisement",
                  signature="o", body=[advertisement_path], message_type=MessageType.METHOD_CALL)
    reply = await bus.call(msg)
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(reply.error_name or "BlueZ UnregisterAdvertisement failed")

async def unregister_application(bus, adapter_path, objects):
    """Unregister from BlueZ and unexport all paths; no adapter power changes."""
    try:
        from dbus_next import Message, MessageType
    except ImportError as exc:
        raise RuntimeError("install dbus-next for opt-in BlueZ GATT registration") from exc
    msg = Message(destination="org.bluez", path=adapter_path,
                  interface="org.bluez.GattManager1", member="UnregisterApplication",
                  signature="o", body=["/org/portable/device_gateway"],
                  message_type=MessageType.METHOD_CALL)
    reply = await bus.call(msg)
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(reply.error_name or "BlueZ UnregisterApplication failed")
    for path in objects:
        bus.unexport(path)

def main():
    raise SystemExit("No implicit startup: registration requires an explicit attended caller; no adapter, advertisement or service started here.")
if __name__ == "__main__": main()
