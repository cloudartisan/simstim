import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from dbus_next import Variant
    from dbus_next.service import ServiceInterface
except ModuleNotFoundError as exc:
    if exc.name != 'dbus_next':
        raise
    raise unittest.SkipTest('requires dbus-next; run with the BLE service interpreter') from exc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
import bluez_gatt


class SignalBus:
    def __init__(self):
        self.signals = []

    def _interface_signal_notify(self, interface, bus_interface, member, signature, body):
        from dbus_next import Message
        message = Message.new_signal("/org/portable/device_gateway/service0/char1",
                                     bus_interface, member, signature, body)
        message._marshall()
        self.signals.append(message)


class Control:
    def __init__(self):
        self.diagnostics = {}


class TLS:
    pass


class NotifyWorkerRegression(unittest.IsolatedAsyncioTestCase):
    async def test_actual_notify_worker_emits_byte_array_across_reconnect(self):
        objects, owner, _up_q, down_q, notify_worker = bluez_gatt.create_application(
            control=Control(), tls=TLS()
        )
        up = objects["/org/portable/device_gateway/service0/char0"]
        down = objects["/org/portable/device_gateway/service0/char1"]
        bridge = owner.bridge
        bridge.start = lambda peer, *_args: setattr(bridge, "_session", SimpleNamespace(peer=peer))

        async def feed(_payload):
            return None

        bridge.feed_uplink = feed
        bus = SignalBus()
        ServiceInterface._add_bus(down, bus)
        task = asyncio.create_task(notify_worker())
        try:
            await down.StartNotify.__wrapped__(down)
            class Option:
                def __init__(self, value): self.value = value

            for index, (peer, payload) in enumerate((("central-a", b"server-flight"),
                                                      ("central-b", b"server-flight-2"))):
                if index:
                    # BlueZ disconnect invalidates the TLS peer; the first new
                    # uplink restarts the real callback's per-session sequence.
                    bridge._session = None
                    owner.device = None
                    owner.authenticated = False
                    await down.StopNotify.__wrapped__(down)
                    await down.StartNotify.__wrapped__(down)
                frame = bluez_gatt.encode_fragment(0, b"client-hello")
                options = {"device": Option(peer), "mtu": Option(247)}
                await up.WriteValue.__wrapped__(up, frame, options)
                self.assertEqual(owner.bridge.peer, peer)
                self.assertEqual(owner.bridge.control.diagnostics["ble_mtu"], 247)
                await down_q.put(payload)
                async with asyncio.timeout(1):
                    while len(bus.signals) < index + 1:
                        if task.done():
                            raise task.exception() or AssertionError("notify worker stopped")
                        await asyncio.sleep(0)
                message = bus.signals[-1]
                self.assertEqual((message.member, message.signature),
                                 ("PropertiesChanged", "sa{sv}as"))
                changed = message.body[1]
                self.assertIsInstance(changed["Value"], Variant)
                framed = changed["Value"].value
                self.assertIsInstance(framed, bytes)
                self.assertEqual(int.from_bytes(framed[:2], "big"), 0)
                self.assertEqual(framed[4:], payload)
                self.assertEqual(int.from_bytes(framed[2:4], "big"), len(payload))
                # A variant nested inside the emitted variant is invalid for
                # the D-Bus ay property and would fail real marshalling.
                self.assertNotIsInstance(changed["Value"].value, Variant)
                bridge._session = None
                owner.device = None
                owner.authenticated = False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            ServiceInterface._remove_bus(down, bus)


if __name__ == "__main__":
    unittest.main(verbosity=2)
