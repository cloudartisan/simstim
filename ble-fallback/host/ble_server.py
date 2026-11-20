"""Explicitly started, Wi-Fi-exclusive BlueZ BLE fallback server."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "wireless-control"))
sys.path.insert(0, str(ROOT / "tools"))
import service
import look_room
import bluez_gatt

WIFI_UNIT = "simstim-wireless-control@host.service"
LOCK_PATH = Path.home() / ".local/state/simstim-wireless/ble-fallback.lock"
ELF_PATH = ROOT / "k151-standalone/build/k151_standalone.elf"


def wifi_is_active(unit: str = WIFI_UNIT) -> bool:
    """Fail closed unless systemd confirms the Wi-Fi owner is not active."""
    result = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", unit],
        check=True, capture_output=True, text=True, timeout=5,
    )
    state = result.stdout.strip()
    if state not in {"inactive", "failed", "not-found"}:
        return True
    return False


def acquire_exclusive_lock(path: Path = LOCK_PATH):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("another BLE fallback process owns the transport") from exc
    return fd


async def serve(adapter: str, *, wifi_unit: str = WIFI_UNIT):
    # Guard before acquiring resources and repeatedly while registered. systemd
    # state is also guarded by the host lock; Wi-Fi must be stopped by operator.
    lock_fd = acquire_exclusive_lock()
    objects = None
    bus = None
    adv = None
    adv_path = "/org/portable/device_gateway/advertisement0"
    try:
        if wifi_is_active(wifi_unit):
            raise RuntimeError(f"Wi-Fi owner {wifi_unit} is active; refusing BLE takeover")
        digest = look_room.verify_board_elf(ELF_PATH, os.environ.get("SIMSTIM_ELF_SHA256", ""))
        control = service.Control()
        control.expected = digest
        key = service.load_key(service.KEYFILE)
        tls = service.OpenSSLPSK(key, service.IDENTITY)
        from dbus_next.aio import MessageBus
        bus = await MessageBus().connect()
        adapter_path = adapter
        objects, owner, _up_q, _down_q, notify_worker = bluez_gatt.create_application(
            control=control, tls=tls,
        )
        from bluez_gatt import create_advertisement
        advertisement = create_advertisement()
        bus.export(adv_path, advertisement)
        adv = adv_path
        await bluez_gatt.register_advertisement(bus, adapter_path, advertisement)
        if wifi_is_active(wifi_unit):
            raise RuntimeError(f"Wi-Fi owner {wifi_unit} became active before GATT registration")
        await bluez_gatt.register_application(bus, adapter_path, objects)
        notifier = asyncio.create_task(notify_worker())
        try:
            while True:
                await asyncio.sleep(0.25)
                if wifi_is_active(wifi_unit):
                    raise RuntimeError(f"Wi-Fi owner {wifi_unit} became active; BLE stopping")
        finally:
            notifier.cancel()
            await asyncio.gather(notifier, return_exceptions=True)
    finally:
        if bus is not None:
            if objects is not None:
                try:
                    await bluez_gatt.unregister_application(bus, adapter, objects)
                except Exception:
                    for path in objects:
                        bus.unexport(path)
            if adv is not None:
                try:
                    await bluez_gatt.unregister_advertisement(bus, adapter, adv_path)
                except Exception:
                    bus.unexport(adv_path)
            bus.disconnect()
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, help="BlueZ adapter object path, e.g. /org/bluez/hci0")
    parser.add_argument("--wifi-unit", default=WIFI_UNIT)
    parser.add_argument("--check-only", action="store_true", help="validate pinned ELF, PSK and Wi-Fi ownership without D-Bus")
    args = parser.parse_args()
    if args.check_only:
        if wifi_is_active(args.wifi_unit):
            raise SystemExit(f"REFUSED: Wi-Fi owner {args.wifi_unit} is active")
        digest = look_room.verify_board_elf(ELF_PATH, os.environ.get("SIMSTIM_ELF_SHA256", ""))
        service.load_key(service.KEYFILE)
        print(f"ready: Wi-Fi inactive; ELF pinned {digest}; PSK file valid; BLE not registered")
        return
    try:
        asyncio.run(serve(args.adapter, wifi_unit=args.wifi_unit))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
