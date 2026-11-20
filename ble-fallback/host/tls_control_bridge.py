"""Bridge one raw GATT byte stream through authenticated TLS into service.Control.

The device is the TLS-PSK client, as in the existing K151 Wi-Fi link. This
module is deliberately transport-only and never starts or registers BlueZ.
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class _Session:
    peer: str
    loop: asyncio.AbstractEventLoop
    uplink: Any
    downlink: Any
    server: socket.socket
    transport: socket.socket
    generation: int
    stop: threading.Event = field(default_factory=threading.Event)
    workers: list[threading.Thread] = field(default_factory=list)


class TLSControlBridge:
    """Adapt GATT asyncio queues to the existing TLS server and Control.session."""

    def __init__(self, control: Any, tls: Any, owner: Any = None, *, queue_limit: int = 16):
        if queue_limit < 1:
            raise ValueError("queue_limit must be positive")
        self.control, self.tls, self.owner = control, tls, owner
        self.queue_limit = queue_limit
        self._lock = threading.Lock()
        self._session: _Session | None = None
        self._generation = 0
        self.last_error = None
        self.last_failed = None
        self._active_diagnostic = None

    @property
    def peer(self):
        with self._lock:
            return self._session.peer if self._session else None

    @property
    def _workers(self):
        """Compatibility/read-only view used by teardown tests."""
        with self._lock:
            return list(self._session.workers) if self._session else []

    def start(self, peer: str, loop: asyncio.AbstractEventLoop,
              uplink: asyncio.Queue, downlink: asyncio.Queue) -> None:
        """Start the single TLS session for a BlueZ device identity."""
        with self._lock:
            if self._session is not None:
                if self._session.peer != peer:
                    raise PermissionError("another GATT peer owns the session")
                return
            self._generation += 1
            self._active_diagnostic = {"generation": self._generation, "first_cause": None,
                                       "stage": None, "detail": None,
                                       "tx_bytes": 0, "rx_bytes": 0}
            server, transport = socket.socketpair()
            server.settimeout(0.5)
            transport.settimeout(0.5)
            session = _Session(peer, loop, uplink, downlink, server, transport,
                               self._generation)
            self._session = session
            session.workers = [
                threading.Thread(target=self._run_control, args=(session,),
                                 name="ble-tls-control", daemon=True),
                threading.Thread(target=self._gatt_to_tls, args=(session,),
                                 name="ble-uplink", daemon=True),
                threading.Thread(target=self._tls_to_gatt, args=(session,),
                                 name="ble-downlink", daemon=True),
            ]
            for worker in session.workers:
                worker.start()

    async def feed_uplink(self, payload: bytes) -> None:
        """Accept one already validated GATT fragment payload with backpressure."""
        if not payload or len(payload) > 512:
            raise ValueError("invalid GATT payload")
        with self._lock:
            session = self._session
            if session is None or session.stop.is_set():
                raise ConnectionError("no active GATT/TLS session")
        await session.uplink.put(bytes(payload))
        if session.stop.is_set():
            raise ConnectionError("GATT/TLS session stopped")

    def _is_current(self, session: _Session) -> bool:
        with self._lock:
            return self._session is session

    @staticmethod
    def _error_label(exc: Exception) -> str:
        """Keep useful transport reasons without logging payloads or paths."""
        import errno, os
        if isinstance(exc, OSError) and isinstance(exc.errno, int) and exc.errno in errno.errorcode:
            return f"{type(exc).__name__}: {os.strerror(exc.errno)}"
        return type(exc).__name__

    def _run_control(self, session: _Session) -> None:
        stream = None
        try:
            stream = self.tls.wrap(session.server)
            cipher = stream.sock.cipher()
            if not cipher:
                raise PermissionError("TLS cipher negotiation failed")
            if self.owner is not None:
                self.owner.mark_tls_authenticated(session.peer, os.environ.get("SIMSTIM_PEER_IDENTITY", "device"), cipher[0])
            if hasattr(self.control, "set_transport"):
                self.control.set_transport("ble")
            self.control.session(stream)
        except Exception as exc:
            if not session.stop.is_set():
                self._record_failure(session, "tls_or_control", exc)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            else:
                try:
                    session.server.close()
                except OSError:
                    pass
            self._finish(session)

    @staticmethod
    def _wait_future(session: _Session, future, timeout: float = 0.25):
        while not session.stop.is_set():
            try:
                return future.result(timeout=timeout)
            except TimeoutError:
                continue
        future.cancel()
        raise ConnectionError("GATT/TLS session stopped")

    @staticmethod
    def _send_chunk(session: _Session, chunk: bytes, stall_limit: float = 15.0) -> None:
        """Drain one ciphertext chunk without replaying a prefix after backpressure."""
        import time
        pending = memoryview(chunk)
        last_progress = time.monotonic()
        while pending:
            if session.stop.is_set():
                raise ConnectionAbortedError('session retired')
            try:
                n = session.transport.send(pending)
            except (socket.timeout, BlockingIOError):
                if time.monotonic() - last_progress >= stall_limit:
                    raise TimeoutError('uplink stalled')
                continue
            if not n:
                raise BrokenPipeError('uplink closed')
            pending = pending[n:]
            last_progress = time.monotonic()

    def _record_failure(self, session: _Session, stage: str, exc: Exception | str) -> None:
        with self._lock:
            current = self._active_diagnostic
            if current is None or current["generation"] != session.generation or current["first_cause"] is not None:
                return
            current["stage"] = stage
            current["first_cause"] = self._error_label(exc) if isinstance(exc, Exception) else str(exc)
            current["detail"] = current["first_cause"]
            self.last_error = current["first_cause"]
            self.last_failed = dict(current)

    def _gatt_to_tls(self, session: _Session) -> None:
        while not session.stop.is_set():
            future = asyncio.run_coroutine_threadsafe(session.uplink.take(512), session.loop)
            try:
                chunk = self._wait_future(session, future)
                self._send_chunk(session, chunk)
                with self._lock:
                    if self._active_diagnostic and self._active_diagnostic["generation"] == session.generation:
                        self._active_diagnostic["tx_bytes"] += len(chunk)
            except Exception as exc:
                if not session.stop.is_set():
                    self._record_failure(session, "gatt_uplink", exc)
                    self._finish(session)
                return

    def _tls_to_gatt(self, session: _Session) -> None:
        while not session.stop.is_set():
            try:
                chunk = session.transport.recv(512)
            except socket.timeout:
                continue
            except OSError:
                if not session.stop.is_set():
                    self._record_failure(session, "tls_downlink", "socket_error")
                    self._finish(session)
                return
            if not chunk:
                self._record_failure(session, "tls_downlink", "peer_eof")
                self._finish(session)
                return
            with self._lock:
                if self._active_diagnostic and self._active_diagnostic["generation"] == session.generation:
                    self._active_diagnostic["rx_bytes"] += len(chunk)
            future = asyncio.run_coroutine_threadsafe(session.downlink.put(chunk), session.loop)
            try:
                self._wait_future(session, future, timeout=0.25)
            except Exception as exc:
                if not session.stop.is_set():
                    self._record_failure(session, "gatt_downlink", exc)
                    self._finish(session)
                return

    def _finish(self, session: _Session) -> None:
        """Stop only this generation; never close shared Control state."""
        session.stop.set()
        with self._lock:
            if self._session is session:
                self._session = None
                release_owner = True
            else:
                release_owner = False
        if release_owner and self.owner is not None:
            self.owner.release(session.peer)
        for sock in (session.server, session.transport):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self, peer: str | None = None, *, drain_timeout: float = 1.0,
              cause: str | None = None) -> None:
        with self._lock:
            session = self._session
            if session is None or (peer is not None and peer != session.peer):
                return
        if cause is not None:
            self._record_failure(session, "bluez_disconnect", cause)
        self._finish(session)
        deadline = __import__("time").monotonic() + max(0.0, drain_timeout)
        for worker in session.workers:
            if worker is threading.current_thread():
                continue
            worker.join(max(0.0, deadline - __import__("time").monotonic()))
