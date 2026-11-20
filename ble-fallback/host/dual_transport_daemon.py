"""Same-process SIMSTIM Wi-Fi and BlueZ BLE fallback host.

The listener is intentionally not a systemd unit: the parent owns cutover.
The existing Control, PSK, operator socket, and ELF pin are reused unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
import sys
sys.path.insert(0, str(PROJECT / "wireless-control"))
sys.path.insert(0, str(PROJECT / "tools"))
import service
import look_room
import bluez_gatt

WIFI_BIND = service.BIND
WIFI_PORT = service.PORT
ADAPTER = "/org/bluez/hci0"


class SharedControl(service.Control):
    """Existing operator protocol plus bounded local transport diagnostics."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._owner_lock = threading.Lock()
        self._owner = None
        self._transport_local = threading.local()
        self.generation = 0
        self.wifi_accept_enabled = True
        self.wifi_accept_event = threading.Event()
        self.wifi_accept_event.set()
        self._wifi_resume_timer = None
        self.diagnostics = {"ble_mtu": 23, "ble_fragment_limit": bluez_gatt.payload_limit_for_mtu(23),
                            "ble_last_error": None, "reconnect_backoff_s": 1}

    def set_transport(self, transport: str):
        if transport not in {"wifi", "ble"}:
            raise ValueError("invalid transport")
        self._transport_local.name = transport

    def session(self, stream):
        # One Control session at a time, regardless of transport. The adapter
        # sets thread-local transport immediately before the existing protocol.
        if not self._owner_lock.acquire(blocking=False):
            raise service.WirelessError("another authenticated transport owns Control")
        try:
            self.generation += 1
            self._owner = getattr(self._transport_local, "name", "wifi")
            super().session(stream)
        finally:
            self._owner = None
            self._owner_lock.release()

    def status(self):
        result = super().status()
        authenticated = self.connected
        result["transport"] = (self._owner or "disconnected") if authenticated else "disconnected"
        result["generation"] = self.generation
        result["diagnostics"] = dict(self.diagnostics)
        return result

    def _resume_wifi(self):
        self._wifi_resume_timer = None
        self.wifi_accept_enabled = True
        self.wifi_accept_event.set()

    def _submit(self, command, timeout=None):
        if command == "wifi-suspend" or command.startswith("wifi-suspend "):
            fields = command.split()
            if len(fields) > 2:
                raise service.WirelessError("usage: wifi-suspend [seconds]")
            duration = float(fields[1]) if len(fields) == 2 else None
            if duration is not None and not 0.1 <= duration <= 3600:
                raise service.WirelessError("wifi-suspend duration must be 0.1..3600 seconds")
            self.wifi_accept_enabled = False
            self.wifi_accept_event.clear()
            timer = self._wifi_resume_timer
            if timer is not None:
                timer.cancel()
            if duration is not None:
                self._wifi_resume_timer = threading.Timer(duration, self._resume_wifi)
                self._wifi_resume_timer.daemon = True
                self._wifi_resume_timer.start()
            # Retire only this process's authenticated Wi-Fi stream. No route,
            # interface, NetworkManager, or systemd network state is touched.
            if self._owner == "wifi":
                current = self.current
                if current is not None:
                    current.close()
            return {"ok": True, "wifi_accept": "suspended", "scope": "this process listener only",
                    "restore_after_s": duration}
        if command == "wifi-resume":
            self._resume_wifi()
            return {"ok": True, "wifi_accept": "enabled", "scope": "this process listener only"}
        if command == "diagnostics":
            return {"owner": self._owner, "generation": self.generation,
                    "wifi_accept": "enabled" if self.wifi_accept_enabled else "suspended",
                    "diagnostics": dict(self.diagnostics)}
        if timeout is None:
            timeout = service.OPERATION_TIMEOUTS.get(command, 35) if self._owner == "ble" else 35
        return super()._submit(command, timeout)


def make_wifi_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((WIFI_BIND, WIFI_PORT))
    listener.listen(4)
    listener.settimeout(.25)
    return listener


def accept_wifi(control: SharedControl, listener, tls):
    while True:
        if not control.wifi_accept_event.wait(timeout=.25):
            continue
        try:
            peer, _ = listener.accept()
        except socket.timeout:
            continue
        if not control.wifi_accept_enabled:
            peer.close()
            continue
        threading.Thread(target=wifi_session, args=(control, tls, peer), daemon=True,
                         name="simstim-wifi-session").start()


def wifi_session(control, tls, peer):
    stream = None
    try:
        peer.settimeout(5)
        stream = tls.wrap(peer)
        if hasattr(control, "set_transport"):
            control.set_transport("wifi")
        control.session(stream)
    except (service.WirelessError, OSError, ValueError, TimeoutError) as exc:
        print(f"wireless-control: wifi peer closed ({type(exc).__name__})", file=sys.stderr, flush=True)
    finally:
        try:
            (stream or peer).close()
        except OSError:
            pass


async def serve(adapter: str, *, key_path: Path, operator_path: Path, state_path: Path | None,
                wifi_enabled=True):
    elf_path = service.release_elf_path()
    expected = os.environ.get("SIMSTIM_ELF_SHA256", "") or None
    key = service.load_key(key_path)
    control = SharedControl(state_path)
    control.elf_path = elf_path
    control.expected = expected
    tls = service.OpenSSLPSK(key, service.IDENTITY)
    control.startup_pin = expected
    control.diagnostics["elf_pin"] = expected
    operator = threading.Thread(target=service.run_operator,
        args=(control, operator_path), daemon=True, name="simstim-operator")
    operator.start()
    listener = make_wifi_listener() if wifi_enabled else None
    workers = []
    if listener is not None:
        workers.append(threading.Thread(target=accept_wifi, args=(control, listener, tls),
                                        daemon=True, name="simstim-wifi-accept"))
        workers[-1].start()
    from dbus_next import BusType
    from dbus_next.aio import MessageBus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    objects = None
    ad_exported = False
    ad_registered = False
    app_registered = False
    disconnect_handler = None
    ad_path = "/org/portable/device_gateway/advertisement0"
    try:
        objects, owner, up_q, down_q, notify_worker = bluez_gatt.create_application(control=control, tls=tls)
        disconnect_handler = await bluez_gatt.install_disconnect_watcher(bus, owner, owner.bridge)
        advertisement = bluez_gatt.create_advertisement()
        bus.export(ad_path, advertisement)
        ad_exported = True
        await bluez_gatt.register_advertisement(bus, adapter, advertisement)
        ad_registered = True
        await bluez_gatt.register_application(bus, adapter, objects)
        app_registered = True
        notifier = asyncio.create_task(notify_worker())
        try:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                if control._owner is None:
                    if owner.bridge.last_failed is not None:
                        control.diagnostics["ble_last_failed"] = dict(owner.bridge.last_failed)
                        control.diagnostics["ble_last_error"] = owner.bridge.last_failed["first_cause"]
                    control.diagnostics["reconnect_backoff_s"] = owner.backoff_s
        finally:
            notifier.cancel()
            await asyncio.gather(notifier, return_exceptions=True)
    finally:
        bluez_gatt.remove_disconnect_watcher(bus, disconnect_handler)
        if app_registered:
            await bluez_gatt.unregister_application(bus, adapter, objects)
        elif objects:
            for path in objects:
                bus.unexport(path)
        if ad_registered:
            await bluez_gatt.unregister_advertisement(bus, adapter, ad_path)
        if ad_exported:
            bus.unexport(ad_path)
        bus.disconnect()
        if listener:
            listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", default=ADAPTER)
    parser.add_argument("--key", type=Path, default=service.KEYFILE)
    parser.add_argument("--operator-socket", type=Path, default=service.OPSOCK)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--no-wifi-listener", action="store_true")
    args = parser.parse_args()
    asyncio.run(serve(args.adapter, key_path=args.key, operator_path=args.operator_socket,
                      state_path=args.state, wifi_enabled=not args.no_wifi_listener))

if __name__ == "__main__":
    main()
