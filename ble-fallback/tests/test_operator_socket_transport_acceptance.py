"""Real operator Unix-socket acceptance across Wi-Fi TLS and BLE fragments."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
WIRELESS = ROOT / "wireless-control"
HOST = ROOT / "ble-fallback" / "host"
sys.path[:0] = [str(WIRELESS), str(HOST), str(WIRELESS / "tests")]

import service
import dual_transport_daemon
from bluez_gatt import BoundedByteQueue, OrderedFragments, encode_fragment
from tls_control_bridge import TLSControlBridge
from test_public_entrypoints import SimulatedPSKPeer

KEY = bytes(range(32))
DIGEST = "c" * 64
CUES = ("online", "no_update", "check_failed")


def operator_request(path, command, timeout=10):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    sock.sendall((json.dumps({"command": command}) + "\n").encode())
    with sock.makefile("rb") as reader:
        raw = reader.readline(4097)
    sock.close()
    if not raw.endswith(b"\n") or len(raw) > 4096:
        raise AssertionError("operator socket returned no bounded JSON line")
    return json.loads(raw)


class OperatorSocketTransportAcceptance(unittest.TestCase):
    def start_operator(self,control,path):
        stop = threading.Event()
        worker = threading.Thread(target=service.run_operator,args=(control,path),
                                  kwargs={"stop":stop},daemon=True)
        worker.start()
        def finish():
            stop.set()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "operator did not stop")
        self.addCleanup(finish)

    def exercise(self, bearer, cue):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            root = Path(temp)
            key_path = root / "device.psk"
            key_path.write_bytes(KEY)
            key_path.chmod(0o600)
            control = dual_transport_daemon.SharedControl(root / "state.json")
            control.expected = DIGEST
            listener_path = root / "operator.sock"
            self.start_operator(control,listener_path)
            until = time.monotonic() + 2
            while not listener_path.exists() and time.monotonic() < until:
                time.sleep(.005)
            self.assertTrue(listener_path.exists(), "operator Unix socket did not start")

            tls = service.OpenSSLPSK(KEY, service.IDENTITY)
            peer = SimulatedPSKPeer(KEY, service.IDENTITY)
            stop = threading.Event()
            bridge = None
            relay_threads = []
            if bearer == "wifi":
                tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                tcp_listener.bind(("127.0.0.1", 0)); tcp_listener.listen(1)
                device = socket.create_connection(tcp_listener.getsockname())
                host, _ = tcp_listener.accept()
                tcp_listener.close()
                # Exercise the production Wi-Fi session entrypoint over TCP loopback.
                session_thread = threading.Thread(target=dual_transport_daemon.wifi_session,
                                                  args=(control, tls, host), daemon=True)
                session_thread.start()
                peer.connect(device)
            else:
                loop = asyncio.new_event_loop()
                loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
                loop_thread.start()
                uplink, downlink = BoundedByteQueue(), BoundedByteQueue()
                bridge = TLSControlBridge(control, tls)
                bridge.start("synthetic-device", loop, uplink, downlink)
                device, relay = socket.socketpair()

                def relay_in():
                    parser = OrderedFragments(); seq = 0
                    while not stop.is_set():
                        try: data = relay.recv(512)
                        except OSError: return
                        if not data: return
                        frame = encode_fragment(seq, data)
                        payload = parser.decode(frame).payload
                        seq = (seq + 1) & 0xffff
                        future = asyncio.run_coroutine_threadsafe(bridge.feed_uplink(payload), loop)
                        try: future.result(timeout=2)
                        except Exception: return

                def relay_out():
                    parser = OrderedFragments(); seq = 0
                    while not stop.is_set():
                        future = asyncio.run_coroutine_threadsafe(downlink.take(512), loop)
                        try: payload = future.result(timeout=.5)
                        except TimeoutError:
                            future.cancel()
                            continue
                        except Exception: return
                        frame = encode_fragment(seq, payload)
                        data = parser.decode(frame).payload
                        seq = (seq + 1) & 0xffff
                        try: relay.sendall(data)
                        except OSError: return

                relay_threads = [threading.Thread(target=relay_in, daemon=True),
                                 threading.Thread(target=relay_out, daemon=True)]
                for worker in relay_threads: worker.start()
                peer.connect(device)

            peer.write((f"SIMSTIM_STANDALONE sha256={DIGEST} uptime_s=17 board_ready=1\n").encode())
            deadline = time.monotonic() + 3
            while not control.connected and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(control.connected, f"{bearer} authenticated Control.session did not start")
            result_box, errors = [], []
            def invoke():
                try: result_box.append(operator_request(listener_path, f"verify-cue {cue}"))
                except Exception as exc: errors.append(exc)
            caller = threading.Thread(target=invoke, daemon=True)
            caller.start()
            request = peer.read_line().decode("ascii").strip()
            fields = request.split()
            self.assertEqual(fields[:2], ["VERIFY_CUE", cue], request)
            self.assertRegex(fields[2], r"^[0-9a-f]{16}$")
            nonce = fields[2]
            if cue == "online":
                peer.write((f"SIMSTIM_SPEAKER cue=online result=played stereo_bytes=2048 status=0000 reset_off=1 verify={nonce}\n"
                            f"VERIFY_DONE {nonce} cue=online outcome=played\n").encode())
            else:
                tap_id = "1a2b3c4d"
                peer.write(f"TAP_FETCH {tap_id} VERIFY {nonce}\n".encode())
                provider = peer.read_line().decode("ascii").strip()
                expected_provider = "TAP_NONE" if cue == "no_update" else "TAP_FAIL"
                self.assertEqual(provider, f"{expected_provider} {tap_id}")
                terminal = "none" if cue == "no_update" else "failed"
                peer.write((f"SIMSTIM_SPEAKER cue={cue} result=played stereo_bytes=2048 status=0000 reset_off=1 verify={nonce} tap_id={tap_id}\n"
                            f"TAP_DONE {tap_id} {terminal}\n"
                            f"VERIFY_DONE {nonce} tap_id={tap_id} outcome={terminal}\n").encode())
            caller.join(5)
            self.assertFalse(caller.is_alive(), "operator socket request exceeded bound")
            self.assertFalse(errors, repr(errors))
            self.assertEqual(len(result_box), 1)
            response = result_box[0]
            self.assertTrue(response["ok"], response)
            result = response["result"]
            self.assertEqual(result["cue"], cue)
            self.assertEqual(result["transport"], bearer)
            self.assertEqual(result["device_elf_sha256"], DIGEST)
            self.assertIn("SIMSTIM_SPEAKER", result["playback_receipt"])
            self.assertIn("VERIFY_DONE", result["transaction_receipt"])
            if cue != "online": self.assertIn("TAP_DONE", result["tap_receipt"])
            stop.set()
            try: peer.close()
            except Exception: pass
            try: device.close()
            except Exception: pass
            if bearer == "wifi":
                try: host.close()
                except Exception: pass
                session_thread.join(2)
            else:
                try: relay.close()
                except Exception: pass
                bridge.close("synthetic-device")
                for worker in relay_threads: worker.join(1)
                async def cancel_loop_tasks():
                    current = asyncio.current_task()
                    tasks = [task for task in asyncio.all_tasks(loop)
                             if task is not current and not task.done()]
                    for task in tasks: task.cancel()
                    await asyncio.sleep(0)
                    await asyncio.gather(*tasks, return_exceptions=True)
                asyncio.run_coroutine_threadsafe(cancel_loop_tasks(), loop).result(timeout=2)
                loop.call_soon_threadsafe(loop.stop)
                loop_thread.join(1)
                loop.close()

    def test_all_verify_cues_via_operator_socket_over_wifi_and_ble(self):
        for bearer in ("wifi", "ble"):
            for cue in CUES:
                with self.subTest(bearer=bearer, cue=cue):
                    self.exercise(bearer, cue)

    def test_replaced_session_after_enqueue_is_rejected_without_device_send(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            root = Path(temp)
            control = dual_transport_daemon.SharedControl(root / "state.json")
            control.expected = DIGEST
            key_path = root / "key"
            key_path.write_bytes(KEY); key_path.chmod(0o600)
            tls = service.OpenSSLPSK(KEY, service.IDENTITY)
            peer_sock, server_sock = socket.socketpair()
            peer = SimulatedPSKPeer(KEY, service.IDENTITY)
            errors = []
            def server_session():
                stream = tls.wrap(server_sock)
                control.set_transport("wifi")
                try: control.session(stream)
                except Exception as exc: errors.append(exc)
                finally: stream.close()
            worker = threading.Thread(target=server_session, daemon=True); worker.start()
            peer.connect(peer_sock)
            peer.write((f"SIMSTIM_STANDALONE sha256={DIGEST} uptime_s=18 board_ready=1\n").encode())
            until = time.monotonic() + 3
            while not control.connected and time.monotonic() < until: time.sleep(.005)
            self.assertTrue(control.connected)
            entered, release = threading.Event(), threading.Event()
            original_get = control.pending.get
            def gated_get(*args, **kwargs):
                item = original_get(*args, **kwargs)
                entered.set()
                if not release.wait(2): raise TimeoutError("dispatch gate timeout")
                control.current = object()  # newer/replaced session
                return item
            control.pending.get = gated_get
            path = root / "operator.sock"
            self.start_operator(control,path)
            until = time.monotonic() + 2
            while not path.exists() and time.monotonic() < until: time.sleep(.005)
            output = []
            caller = threading.Thread(target=lambda: output.append(operator_request(path, "verify-cue online")), daemon=True)
            caller.start()
            self.assertTrue(entered.wait(2), "request was not enqueued")
            release.set()
            caller.join(3)
            self.assertFalse(caller.is_alive())
            self.assertEqual(len(output), 1)
            self.assertFalse(output[0]["ok"], output[0])
            self.assertIn("device command expired", output[0]["error"])
            peer_sock.settimeout(.3)
            with self.assertRaises((socket.timeout, RuntimeError)):
                peer.read_line()
            peer.close(); peer_sock.close(); server_sock.close(); worker.join(1)


if __name__ == "__main__":
    unittest.main()
