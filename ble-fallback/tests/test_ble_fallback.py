import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import importlib.util
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "wireless-control"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
sys.path.insert(0, str(ROOT / "wireless-control" / "tests"))
from bluez_gatt import (BusyError, MAX_PAYLOAD, OrderedFragments, Owner,
                        encode_fragment, BoundedByteQueue, payload_limit_for_mtu)
from tls_control_bridge import TLSControlBridge
import service
import ble_server
import dual_transport_daemon
from test_public_entrypoints import SimulatedPSKPeer

class BleFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_uplink_chunk_survives_slow_reader_without_replay(self):
        from types import SimpleNamespace
        a,b=socket.socketpair()
        payload=bytes(range(256))*1800
        received=bytearray()
        try:
            a.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
            a.settimeout(.03); b.settimeout(3)
            def slow_reader():
                time.sleep(.15)
                while len(received)<len(payload):
                    part=b.recv(2048)
                    if not part: break
                    received.extend(part)
            t=threading.Thread(target=slow_reader,daemon=True); t.start()
            TLSControlBridge._send_chunk(SimpleNamespace(stop=threading.Event(),transport=a),payload,2)
            t.join(3); self.assertEqual(bytes(received),payload)
        finally:
            a.close();b.close()

    def test_uplink_chunk_expires_on_continuous_stall(self):
        from types import SimpleNamespace
        class Stalled:
            def send(self,chunk): raise socket.timeout()
        with patch('time.monotonic',side_effect=[2,18]), self.assertRaises(TimeoutError):
            TLSControlBridge._send_chunk(SimpleNamespace(stop=threading.Event(),transport=Stalled()),b'x')

    def test_framing_and_strict_sequence(self):
        parser = OrderedFragments()
        p = b"TLS record bytes"
        self.assertEqual(parser.decode(encode_fragment(0,p)).payload,p)
        with self.assertRaises(ValueError): parser.decode(encode_fragment(0,p))
        with self.assertRaises(ValueError): parser.decode(encode_fragment(2,p))
    def test_rejects_bad_lengths_and_caps(self):
        parser=OrderedFragments()
        for raw in (b"", b"\0\0\0\0\0", b"\0\0\0\2x", b"\0\0\2\1"+b"x"*MAX_PAYLOAD):
            with self.assertRaises(ValueError): parser.decode(raw)
        with self.assertRaises(ValueError): encode_fragment(0,b"x"*(MAX_PAYLOAD+1))
    async def test_owner_single_session_and_auth_gate(self):
        o=Owner(); await o.acquire("peer-a")
        with self.assertRaises(BusyError): await o.acquire("peer-b")
        with self.assertRaises(PermissionError): o.mark_tls_authenticated("peer-a","wrong","cipher")
        self.assertFalse(o.authenticated)
        o.mark_tls_authenticated("peer-a","simstim-device","TLS-PSK")
        self.assertTrue(o.authenticated)
        with patch("bluez_gatt.time.monotonic", return_value=100.0):
            o.release("peer-a")
            self.assertEqual(o.backoff_s, 1)
            with self.assertRaises(BusyError): await o.acquire("peer-a")
        with patch("bluez_gatt.time.monotonic", return_value=101.0):
            await o.acquire("peer-a")
            o.release("peer-a")
            self.assertEqual(o.backoff_s, 2)
        self.assertFalse(o.authenticated); self.assertIsNone(o.device)
    async def test_owner_default_matches_existing_wifi_tls_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            owner = Owner()
            await owner.acquire("peer-default")
            owner.mark_tls_authenticated("peer-default", service.IDENTITY, "TLS-PSK")
            self.assertTrue(owner.authenticated)

    async def test_queue_bounded_and_stream_order(self):
        q=BoundedByteQueue(cap=4)
        await q.put(b"ab"); await q.put(b"cd")
        async def producer(): await q.put(b"e")
        task=asyncio.create_task(producer()); await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(await q.take(3),b"abc"); await task
        self.assertEqual(await q.take(4),b"de")
        with self.assertRaises(ValueError): await q.put(b"12345")
    async def test_tls_psk_bridge_reaches_existing_control_protocol(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            key_path = Path(d) / "device.psk"
            key_path.write_bytes(key)
            key_path.chmod(0o600)
            control = service.Control(Path(d) / "state.json")
            control.expected = None
            owner = Owner()
            await owner.acquire("synthetic-device")
            uplink, downlink = BoundedByteQueue(), BoundedByteQueue()
            bridge = TLSControlBridge(control, service.OpenSSLPSK(key, service.IDENTITY), owner)
            bridge.start("synthetic-device", asyncio.get_running_loop(), uplink, downlink)
            client_sock, relay_sock = socket.socketpair()
            peer = SimulatedPSKPeer(key, service.IDENTITY)
            done = threading.Event()

            # Capture the test loop before crossing into synthetic GATT workers.
            loop = asyncio.get_running_loop()
            def relay_in():
                parser = OrderedFragments()
                sequence = 0
                try:
                    while not done.is_set():
                        payload = relay_sock.recv(MAX_PAYLOAD)
                        if not payload:
                            return
                        frame = encode_fragment(sequence, payload)
                        payload = parser.decode(frame).payload
                        sequence = (sequence + 1) & 0xffff
                        asyncio.run_coroutine_threadsafe(bridge.feed_uplink(payload), loop).result(timeout=2)
                except Exception:
                    return

            def relay_out():
                parser = OrderedFragments()
                sequence = 0
                try:
                    while not done.is_set():
                        payload = asyncio.run_coroutine_threadsafe(downlink.take(MAX_PAYLOAD), loop).result(timeout=2)
                        frame = encode_fragment(sequence, payload)
                        payload = parser.decode(frame).payload
                        sequence = (sequence + 1) & 0xffff
                        relay_sock.sendall(payload)
                except Exception:
                    return

            def device_roundtrip():
                peer.connect(client_sock)
                banner = "SIMSTIM_STANDALONE sha256=" + "c" * 64 + " uptime_s=17 board_ready=1\n"
                peer.write(banner.encode())
                until = time.monotonic() + 3
                while not control.connected and time.monotonic() < until:
                    time.sleep(.01)
                if not control.connected:
                    raise AssertionError(f"TLS-authenticated Control.session did not become active: {bridge.last_error}")
                if not owner.authenticated:
                    raise AssertionError("GATT owner was not marked TLS-authenticated")
                result = []
                op = threading.Thread(target=lambda: result.append(control._submit("home", timeout=4)))
                op.start()
                if peer.read().decode() != "LOOK_HOME\n":
                    raise AssertionError("operator command was not forwarded")
                peer.write(b"LOOK_MOTOR result=ok action=HOME stage=home_centre reason=complete yaw_raw=458 yaw_deg=0 pitch_raw=699 pitch_deg=30\n")
                op.join(3)
                if not result or not result[0]["ok"]:
                    raise AssertionError("operator protocol did not receive successful device receipt")
                if "yaw_raw=458" not in result[0]["receipt"]:
                    raise AssertionError("operator receipt was not preserved")

            incoming = threading.Thread(target=relay_in, daemon=True)
            outgoing = threading.Thread(target=relay_out, daemon=True)
            incoming.start(); outgoing.start()
            try:
                await asyncio.to_thread(device_roundtrip)
            finally:
                done.set()
                bridge.close("synthetic-device")
                client_sock.close(); relay_sock.close()
                try: peer.close()
                except Exception: pass
                incoming.join(1); outgoing.join(1)

    def test_transport_error_keeps_safe_os_reason(self):
        self.assertEqual(TLSControlBridge._error_label(BrokenPipeError(32, "Broken pipe")),
                         "BrokenPipeError: Broken pipe")
        self.assertEqual(TLSControlBridge._error_label(RuntimeError("private payload text")),
                         "RuntimeError")

    async def test_failed_generation_survives_disconnect_and_reconnect_in_real_status(self):
        from bluez_gatt import Owner, BoundedByteQueue

        class WaitingTLS:
            def __init__(self): self.entered = threading.Event(); self.release = threading.Event()
            def wrap(self, sock):
                self.entered.set(); self.release.wait(3)
                raise OSError("test teardown")

        class PartialThenBrokenPipe:
            def __init__(self, sock): self.sock, self.calls = sock, 0
            def send(self, _data):
                self.calls += 1
                if self.calls == 1: return 7
                raise BrokenPipeError(32, "Broken pipe")
            def recv(self, size): return self.sock.recv(size)
            def shutdown(self, how): return self.sock.shutdown(how)
            def close(self): return self.sock.close()

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            control = dual_transport_daemon.SharedControl(Path(d) / "state.json")
            owner = Owner()
            control._ble_owner = owner
            tls = WaitingTLS()
            bridge = TLSControlBridge(control, tls, owner)
            owner.bridge = bridge
            loop = asyncio.get_running_loop()
            uplink, downlink = BoundedByteQueue(), BoundedByteQueue()
            await owner.acquire("old-device")
            bridge.start("old-device", loop, uplink, downlink)
            self.assertTrue(tls.entered.wait(1))
            old_session = bridge._session
            old_session.transport = PartialThenBrokenPipe(old_session.transport)
            await uplink.put(b"private-payload-must-not-escape")
            deadline = time.monotonic() + 2
            while bridge.peer is not None and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            self.assertIsNone(bridge.peer, "failed generation did not clear its session")
            failed = control.status()["diagnostics"]
            self.assertEqual(failed["ble_generation"], 1)
            self.assertEqual(failed["ble_stage"], "gatt_uplink")
            self.assertEqual(failed["ble_tx_bytes"], 7)
            self.assertEqual(failed["ble_rx_bytes"], 0)
            self.assertGreater(failed["ble_elapsed_ms"], 0)
            self.assertNotIn("private-payload", repr(failed))
            self.assertNotIn("old-device", repr(failed))

            # Attach a genuine next owner/session before reading status again.
            await asyncio.sleep(1.05)  # Owner's real one-second reconnect backoff.
            await owner.acquire("new-device")
            bridge.start("new-device", loop, uplink, downlink)
            current = control.status()["diagnostics"]
            self.assertEqual({k: current[k] for k in (
                "ble_generation", "ble_stage", "ble_tx_bytes", "ble_rx_bytes")}, {
                "ble_generation": 1, "ble_stage": "gatt_uplink",
                "ble_tx_bytes": 7, "ble_rx_bytes": 0})
            bridge.close("new-device")
            tls.release.set()

    def test_ble_acceptance_checks_staged_update_before_single_capture_attempt(self):
        import trial_ble_session
        calls = []
        phase = {"transport": "wifi"}
        pin = "a" * 64

        def request_once(_socket, command, _timeout):
            calls.append(command)
            if command == "wifi-suspend 123":
                phase["transport"] = "ble"
                return {"ok": True, "result": {"ok": True}}
            if command == "stage Regression check.":
                return {"ok": True, "result": {"ok": True, "receipt": "UPDATE_STATUS bytes=320 crc=deadbeef"}}
            if command == "frame":
                raise TimeoutError("capture reply ambiguous")
            if command == "wifi-resume":
                return {"ok": True, "result": {"ok": True}}
            return {"ok": True, "result": {
                "ok": True, "peer_identity": service.IDENTITY,
                "device_elf_sha256": pin, "transport": phase["transport"],
                "generation": 4, "uptime_s": 10}}

        with (patch.object(trial_ble_session, "wifi_suspend_budget", return_value=123),
              patch.object(trial_ble_session, "request_once", side_effect=request_once)):
            with self.assertRaisesRegex(TimeoutError, "capture reply ambiguous"):
                trial_ble_session.run(pin, ["stage Regression check.", "frame"])
        self.assertLess(calls.index("stage Regression check."), calls.index("frame"))
        self.assertEqual(calls.count("frame"), 1, "ambiguous capture must never be replayed")
        self.assertEqual(calls[-1], "wifi-resume")

    def test_ble_disconnect_keeps_generation_first_cause_and_stage(self):
        from types import SimpleNamespace
        import time
        control = SimpleNamespace(diagnostics={"ble_mtu": 185, "ble_fragment_limit": 178})
        bridge = TLSControlBridge(control, object())
        bridge._active_diagnostic = {"generation": 9, "first_cause": None,
                                     "stage": None, "detail": None,
                                     "tx_bytes": 120, "rx_bytes": 80}
        session = SimpleNamespace(generation=9, started_monotonic=time.monotonic() - 0.01)
        bridge._record_failure(session, "tls_downlink", "peer_eof")
        bridge._record_failure(session, "bluez_disconnect", "bluez_connected_false")
        self.assertEqual({k: v for k, v in bridge.last_failed.items() if k != "elapsed_ms"}, {
            "generation": 9, "first_cause": "peer_eof", "stage": "tls_downlink",
            "detail": "peer_eof", "tx_bytes": 120, "rx_bytes": 80})
        self.assertEqual(bridge.last_error, "peer_eof")
        self.assertEqual(set(bridge.safe_diagnostics()), {
            "ble_generation", "ble_stage", "ble_tx_bytes", "ble_rx_bytes",
            "ble_elapsed_ms", "ble_mtu", "ble_fragment_limit"})
        self.assertEqual({k: v for k, v in bridge.safe_diagnostics().items()
                          if k != "ble_elapsed_ms"}, {
            "ble_generation": 9, "ble_stage": "tls_downlink", "ble_tx_bytes": 120,
            "ble_rx_bytes": 80, "ble_mtu": 185, "ble_fragment_limit": 178})
        self.assertGreaterEqual(bridge.safe_diagnostics()["ble_elapsed_ms"], 0)

    async def test_real_tls_bridge_large_bidirectional_stream_at_negotiated_mtus(self):
        key = bytes(range(32))
        size = 153600
        source = bytes((i * 37 + 11) & 0xff for i in range(size))
        for mtu in (23, 185, 247, 517):
            with self.subTest(mtu=mtu), tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
                control = type("EchoControl", (), {})()
                control.current = type("UnrelatedStream", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
                unrelated_stream = control.current
                errors = []
                echo_received = threading.Event()

                def session(stream):
                    try:
                        data = bytearray()
                        while len(data) < size:
                            chunk = stream.sock.recv(min(4096, size - len(data)))
                            if not chunk:
                                raise AssertionError("TLS input ended early")
                            data.extend(chunk)
                        if bytes(data) != source:
                            raise AssertionError("TLS uplink differed from source")
                        stream.sock.sendall(data)
                        # Keep the TLS server stream alive until the client has
                        # consumed the whole echo; otherwise close may discard
                        # encrypted bytes still buffered by the transport.
                        if not echo_received.wait(10):
                            raise AssertionError("client did not confirm echoed stream")
                    except Exception as exc:
                        errors.append(exc)

                control.session = session
                tls = service.OpenSSLPSK(key, service.IDENTITY)
                limit = payload_limit_for_mtu(mtu)
                uplink, downlink = BoundedByteQueue(cap=8192), BoundedByteQueue(cap=8192)
                bridge = TLSControlBridge(control, tls, queue_limit=16)
                loop = asyncio.get_running_loop()
                bridge.start("test-peer", loop, uplink, downlink)
                client_sock, relay_sock = socket.socketpair()
                relay_sock.setblocking(False)
                peer = SimulatedPSKPeer(key, service.IDENTITY)
                out = bytearray()
                incoming_seq = outgoing_seq = 0
                parser_in = OrderedFragments()

                async def uplink_relay():
                    nonlocal incoming_seq
                    paced_fragments = 0
                    while True:
                        chunk = await loop.sock_recv(relay_sock, 512)
                        if not chunk:
                            return
                        payloads = bytearray()
                        for offset in range(0, len(chunk), limit):
                            part = parser_in.decode(encode_fragment(incoming_seq, chunk[offset:offset + limit]))
                            incoming_seq = (incoming_seq + 1) & 0xffff
                            payloads.extend(part.payload)
                            if mtu == 185:
                                paced_fragments += 1
                                await asyncio.sleep(0.002)
                                if paced_fragments % 64 == 0:
                                    await asyncio.sleep(0.15)  # link-level pause/recovery window
                        await bridge.feed_uplink(payloads)

                async def downlink_relay():
                    nonlocal outgoing_seq
                    parser = OrderedFragments()
                    paced_fragments = 0
                    while not echo_received.is_set():
                        chunk = await downlink.take(512)
                        payloads = bytearray()
                        for offset in range(0, len(chunk), limit):
                            part = parser.decode(encode_fragment(outgoing_seq, chunk[offset:offset + limit]))
                            outgoing_seq = (outgoing_seq + 1) & 0xffff
                            payloads.extend(part.payload)
                            if mtu == 185:
                                paced_fragments += 1
                                await asyncio.sleep(0.002)
                                if paced_fragments % 64 == 0:
                                    await asyncio.sleep(0.15)
                        await loop.sock_sendall(relay_sock, payloads)
                        out.extend(payloads)

                async def relay():
                    await asyncio.gather(uplink_relay(), downlink_relay())

                def client_roundtrip():
                    # SSL_set_fd inherits the descriptor's blocking mode. Never
                    # set a socket timeout (which makes it nonblocking), and
                    # never call SSL_read/SSL_write concurrently on this SSL*.
                    peer.connect(client_sock)
                    for pos in range(0, size, 4096):
                        remaining = memoryview(source[pos:pos + 4096])
                        while remaining:
                            written = peer.write(remaining.tobytes())
                            if written <= 0:
                                raise AssertionError("TLS writer made no progress")
                            remaining = remaining[written:]
                    client_sock.shutdown(socket.SHUT_WR)
                    while len(client_received) < size:
                        block = peer.read(min(4096, size - len(client_received)))
                        # The async relay accumulates the exact same stream;
                        # consume via the TLS peer to verify both directions.
                        client_received.extend(block)
                    echo_received.set()

                client_received = bytearray()
                relay_task = asyncio.create_task(relay())
                session_workers = bridge._workers
                try:
                    try:
                        # 182-byte ATT payloads with paced notifications and
                        # recurring 150 ms link pauses need a realistic window.
                        await asyncio.wait_for(asyncio.to_thread(client_roundtrip), timeout=35)
                    except asyncio.TimeoutError:
                        try:
                            client_sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        raise
                    session_workers = bridge._workers
                    bridge.close("test-peer")
                    relay_task.cancel()
                    await asyncio.gather(relay_task, return_exceptions=True)
                    self.assertFalse(errors, repr(errors))
                    self.assertEqual(bytes(client_received), source)
                    self.assertIsNone(bridge.peer)
                    self.assertFalse(unrelated_stream.closed, "bridge closed an unrelated Control stream")
                finally:
                    echo_received.set()
                    bridge.close("test-peer")
                    try:
                        client_sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    client_sock.close(); relay_sock.close()
                    try: peer.close()
                    except Exception: pass
                    if not relay_task.done():
                        relay_task.cancel()
                    await asyncio.gather(relay_task, return_exceptions=True)
                    self.assertFalse(any(t.is_alive() for t in session_workers), "bridge worker leaked")

        self.assertEqual(payload_limit_for_mtu(23), 16)
        self.assertEqual(payload_limit_for_mtu(247), 240)
        self.assertEqual(payload_limit_for_mtu(517), 510)
        for mtu in (22, 518):
            with self.assertRaises(ValueError): payload_limit_for_mtu(mtu)

    async def test_large_camera_or_speech_payload_backpressures_without_loss(self):
        # Exercise a stream much larger than both the ATT fragment and bounded
        # queues; production consumes it incrementally as BlueZ notifications.
        source = bytes((i * 37) & 0xff for i in range(256 * 1024))
        limit = payload_limit_for_mtu(185)
        queue = BoundedByteQueue(cap=4096)
        received = bytearray()
        async def producer():
            for offset in range(0, len(source), limit):
                await queue.put(source[offset:offset + limit])
        async def consumer():
            while len(received) < len(source):
                received.extend(await queue.take(limit))
        await asyncio.wait_for(asyncio.gather(producer(), consumer()), timeout=10)
        self.assertEqual(bytes(received), source)

    def test_local_wifi_accept_control_is_service_scoped(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            control = dual_transport_daemon.SharedControl(Path(d) / "state.json")
            suspended = control._submit("wifi-suspend")
            self.assertFalse(control.wifi_accept_event.is_set())
            self.assertEqual(suspended["scope"], "this process listener only")
            resumed = control._submit("wifi-resume")
            self.assertTrue(control.wifi_accept_event.is_set())
            self.assertEqual(resumed["wifi_accept"], "enabled")
            self.assertEqual(control.status()["transport"], "disconnected")

    def test_timed_wifi_suspend_closes_authenticated_wifi_stream_and_restores(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            control = dual_transport_daemon.SharedControl(Path(d) / "state.json")
            class Stream:
                closed = False
                def close(self): self.closed = True
            stream = Stream()
            control.current = stream
            control._owner = "wifi"
            control.connected = True
            control.set_transport("wifi")
            response = control._submit("wifi-suspend 0.1")
            self.assertEqual(response["restore_after_s"], 0.1)
            self.assertTrue(stream.closed)
            self.assertFalse(control.wifi_accept_event.is_set())
            self.assertTrue(control.wifi_accept_event.wait(2), "timed Wi-Fi listener restore did not fire")
            self.assertTrue(control.wifi_accept_enabled)
            self.assertEqual(response["scope"], "this process listener only")
            self.assertIsNone(control._wifi_resume_timer)

    def test_import_is_side_effect_free(self):
        import bluez_gatt
        self.assertTrue(callable(bluez_gatt.create_application))
        self.assertFalse(hasattr(bluez_gatt, "_bus"))

    def test_wifi_owner_gate_fails_closed_and_allows_only_inactive(self):
        class Result:
            def __init__(self, state): self.stdout = state + "\n"
        with patch.object(ble_server.subprocess, "run", return_value=Result("inactive")):
            self.assertFalse(ble_server.wifi_is_active("wifi.service"))
        for state in ("active", "activating", "deactivating", "reloading"):
            with self.subTest(state=state), patch.object(
                ble_server.subprocess, "run", return_value=Result(state)
            ):
                self.assertTrue(ble_server.wifi_is_active("wifi.service"))
        with patch.object(ble_server.subprocess, "run", side_effect=OSError("systemctl unavailable")):
            with self.assertRaises(OSError):
                ble_server.wifi_is_active("wifi.service")

    def test_ble_owner_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            lock = Path(d) / "fallback.lock"
            fd = ble_server.acquire_exclusive_lock(lock)
            try:
                with self.assertRaises(RuntimeError):
                    ble_server.acquire_exclusive_lock(lock)
            finally:
                os.close(fd)
            fd2 = ble_server.acquire_exclusive_lock(lock)
            os.close(fd2)

    def test_exact_elf_pin_rejects_mismatch_before_server_start(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            elf = Path(d) / "board.elf"
            elf.write_bytes(b"\x7fELFtest-image")
            import hashlib
            correct = hashlib.sha256(elf.read_bytes()).hexdigest()
            self.assertEqual(ble_server.look_room.verify_board_elf(elf, correct), correct)
            with self.assertRaises(Exception):
                ble_server.look_room.verify_board_elf(elf, "0" * 64)

    def test_control_session_enforces_configured_exact_elf_pin(self):
        class Stream:
            def readline(self, timeout=15):
                return ("SIMSTIM_STANDALONE sha256=" + "1" * 64 + " uptime_s=1 board_ready=1\n").encode()
        control = service.Control()
        control.expected = "2" * 64
        with self.assertRaisesRegex(service.WirelessError, "unexpected device ELF"):
            control.session(Stream())

    @unittest.skipUnless(importlib.util.find_spec("dbus_next"), "dbus-next installed in isolated host runtime")
    def test_gatt_and_advertisement_dbus_interfaces_match(self):
        import bluez_gatt
        class Control: pass
        class TLS: pass
        objects, _owner, _up, _down, _notify = bluez_gatt.create_application(control=Control(), tls=TLS())
        service = objects["/org/portable/device_gateway/service0"]
        uplink = objects["/org/portable/device_gateway/service0/char0"]
        downlink = objects["/org/portable/device_gateway/service0/char1"]
        self.assertEqual(service.UUID, bluez_gatt.SERVICE_UUID)
        self.assertEqual(uplink.UUID, bluez_gatt.UPLINK_UUID)
        self.assertEqual(downlink.UUID, bluez_gatt.DOWNLINK_UUID)
        self.assertEqual(uplink.flags, ["write", "write-without-response"])
        self.assertEqual(downlink.flags, ["notify"])
        advertisement = bluez_gatt.create_advertisement()
        info = advertisement.introspect()
        self.assertEqual(advertisement.name, "org.bluez.LEAdvertisement1")
        self.assertEqual({p.name for p in info.properties}, {"Type", "ServiceUUIDs", "LocalName", "Includes"})

if __name__=="__main__": unittest.main()
