"""mpv audio backend — JSON IPC control over a child mpv process.

Design notes (M1):
- One mpv instance, one URL at a time. Loading the next track = new command.
- IPC socket: POSIX unix socket in the temp dir; Windows named pipe.
- A reader thread turns mpv's JSON events (end-file, shutdown, property
  changes) into a queue.Queue of MPVEvent for the player to consume.
- The backend is the ONLY place that knows mpv exists; the player core talks
  to the narrow Backend interface (play/pause/stop/seek/position/wait_end).

Error policy: every mpv failure is logged at debug/exception level and
surfaced as MPV* errors from errors.py. Stale sockets from crashed runs are
cleaned up on start.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

from .config import find_mpv
from .errors import MPVProcessError, MPVTimeoutError
from .logging import get_logger

log = get_logger("mpv")

IPC_TIMEOUT = 5.0  # seconds for a control command to round-trip


@dataclass
class MPVEvent:
    event: str  # "end-file", "error", "paused", "resumed", "crash", ...
    reason: str = ""
    data: dict | None = None


class MPVBackend:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.mpv_path = ""  # resolved lazily on first _start()
        self.proc: subprocess.Popen | None = None
        self.events: Queue[MPVEvent] = Queue()
        self._reader: threading.Thread | None = None
        self._ipc: str | None = None
        self._sock_path: Path | None = None
        self._paused = False
        self._lock = threading.RLock()
        self._cmd_seq = 0
        self._started_at = 0.0
        self._start_offset = 0.0  # where we seeked on load (for position math fallback)
        log.info("mpv resolved: %s", self.mpv_path)

    # ------------------------------------------------------------------ lifecycle

    def _ipc_name(self) -> tuple[str, Path | None]:
        tag = f"tmusic-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        if sys.platform == "win32":
            return f"\\\\.\\pipe\\{tag}", None
        path = Path(tempfile.gettempdir()) / f"{tag}.sock"
        return path.as_posix(), path

    def _start(self) -> None:
        if not self.mpv_path:
            # find_mpv raises MissingMpv (a TmusicError) with guidance
            self.mpv_path = find_mpv(self.cfg)
        self._stop_process(quiet=True)
        ipc, sock_path = self._ipc_name()
        self._ipc = ipc
        self._sock_path = sock_path
        if sock_path is not None:
            # remove stale socket from a previous run of the same process
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass
        cmd = [
            self.mpv_path,
            f"--input-ipc={ipc}",
            "--no-video",
            "--no-terminal",  # silence stdout noise; we use IPC
            "--really-quiet",
            "--keep-open=no",
            "--pause",  # start paused; the player calls resume() on purpose
            "--no-config",
            "--force-rgba",  # no-op without video, harmless
        ]
        log.debug("starting mpv: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc", daemon=True)
        self._reader.start()
        if not self._wait_ready():
            self._stop_process()
            raise MPVProcessError("mpv did not open its IPC socket")

    def _wait_ready(self, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            if self._ipc is not None and self._send("get_property", "idle-active", timeout=0.4):
                return True
        return False

    def shutdown(self) -> None:
        self._stop_process()

    def _stop_process(self, quiet: bool = False) -> None:
        with self._lock:
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                self._send("quit", timeout=1.5)
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if not quiet:
                    log.warning("mpv did not exit; killing")
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self._reader:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._sock_path:
            try:
                self._sock_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._sock_path = None

    # ------------------------------------------------------------------ IPC plumbing

    def _send(self, command: str, *args, timeout: float = IPC_TIMEOUT) -> dict | None:
        """Send a command; None on any transport failure (caller decides)."""
        if self.proc is None or self.proc.poll() is not None:
            return None
        self._cmd_seq += 1
        payload = json.dumps({"command": [command, *args], "request_id": self._cmd_seq})
        try:
            if sys.platform == "win32":
                f = self._win_connect(timeout)
                f.sendall(payload.encode() + b"\n")
                f.close()
            else:
                import socket
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(self._ipc)
                s.sendall(payload.encode() + b"\n")
                s.close()
        except OSError as e:
            log.debug("IPC send failed (%s): %s", command, e)
            return None
        # We do not match request_ids (mpv would reply to every command); the
        # transport is fire-and-verified-via-property where it matters.
        return {"sent": True}

    def _get_property(self, name: str, timeout: float = IPC_TIMEOUT) -> dict | None:
        """get_property with response matching. Uses a persistent connection."""
        if self.proc is None or self.proc.poll() is not None:
            return None
        self._cmd_seq += 1
        rid = self._cmd_seq
        payload = json.dumps({"command": ["get_property", name], "request_id": rid})
        try:
            if sys.platform == "win32":
                import socket  # noqa: F401
                s = self._win_connect(timeout)
            else:
                import socket
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(self._ipc)
            s.sendall(payload.encode() + b"\n")
            buf = b""
            s.settimeout(timeout)
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("request_id") == rid:
                        s.close()
                        return msg
        except (OSError, json.JSONDecodeError) as e:
            log.debug("get_property(%s) failed: %s", name, e)
            return None
        return None

    def _win_connect(self, timeout: float) -> "object":
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # placeholder; replaced below
        s.close()
        # Windows named pipe: plain file open is the reliable path.
        class _PipeFile:
            def __init__(self, path: str):
                import msvcrt  # noqa: F401
                self.f = open(path, "w+", encoding="utf-8", newline="")

            def sendall(self, b: bytes) -> None:
                self.f.write(b.decode("utf-8"))
                self.f.flush()

            def recv(self, n: int = 65536) -> bytes:
                data = self.f.readline()
                return data.encode("utf-8") if data else b""

            def settimeout(self, t: float) -> None:
                pass

            def close(self) -> None:
                self.f.close()

        return _PipeFile(self._ipc)

    def _read_loop(self) -> None:
        """Consume mpv's async events; surface interesting ones as MPVEvent."""
        import socket

        try:
            if sys.platform == "win32":
                conn = self._win_connect(timeout=5.0)
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(self._ipc)
                conn = sock
            while self.proc is not None and self.proc.poll() is None:
                line = conn.recv(65536)
                if not line:
                    break
                for raw in line.split(b"\n"):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event = msg.get("event")
                    if event == "end-file":
                        self.events.put(MPVEvent("end-file", reason=str(msg.get("reason", ""))))
                    elif event == "shutdown":
                        self.events.put(MPVEvent("shutdown"))
                    elif event == "file-error":
                        self.events.put(MPVEvent("error", reason=str(msg.get("error", "unknown"))))
                    elif event == "property-change":
                        if msg.get("name") == "pause":
                            self.events.put(MPVEvent("paused" if msg.get("data") else "resumed"))
        except OSError as e:
            log.debug("IPC reader loop ended: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            if self.proc is not None and self.proc.poll() is not None:
                rc = self.proc.returncode
                self.events.put(MPVEvent("crash", reason=f"exit code {rc}"))

    # ------------------------------------------------------------------ player-facing API

    def play(self, url: str, seek: float = 0.0) -> None:
        """Load and play a URL (starting paused; call resume())."""
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self._start_offset = max(0.0, seek)
            self._started_at = time.monotonic()
            # loadfile replaces the current one; pause is already on by default
            ok = self._send("loadfile", url, "replace")
            if ok is None:
                raise MPVProcessError("mpv IPC socket is not available")
            if seek > 0:
                self._send("seek", float(seek), "absolute")

    def resume(self) -> None:
        self._send("set_property", "pause", False)
        self._paused = False

    def pause(self) -> None:
        self._send("set_property", "pause", True)
        self._paused = True

    def stop_track(self) -> None:
        self._send("stop")

    def seek(self, seconds: float) -> None:
        self._send("seek", float(seconds), "absolute")

    def seek_by(self, delta: float) -> None:
        self._send("seek", float(delta), "relative")

    def position(self) -> float | None:
        """Current playback position in seconds, or None if unknown."""
        msg = self._get_property("time-pos")
        if msg and msg.get("error") == "success" and isinstance(msg.get("data"), (int, float)):
            return float(msg["data"])
        # fallback: monotonic clock since (un)pause is approximate; only used
        # while the socket is briefly unavailable
        if self.proc is not None and self.proc.poll() is None and not self._paused:
            return self._start_offset + (time.monotonic() - self._started_at)
        return None

    def duration(self) -> float | None:
        msg = self._get_property("duration")
        if msg and msg.get("error") == "success" and isinstance(msg.get("data"), (int, float)):
            return float(msg["data"])
        return None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_end(self, timeout: float | None = None) -> str:
        """Block until the current track ends (or an error/crash).

        Returns "end" | "error:<reason>" | "timeout" | "crash".
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                ev = self.events.get(timeout=1.0)
            except Empty:
                if not self.is_running():
                    return "crash"
                if deadline and time.monotonic() > deadline:
                    return "timeout"
                continue
            if ev.event == "end-file":
                return "end"
            if ev.event == "error":
                return f"error:{ev.reason}"
            if ev.event == "crash":
                return "crash"
            # paused/resumed/shutdown: ignore, keep waiting

    def drain_events(self) -> list[MPVEvent]:
        out: list[MPVEvent] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except Empty:
                return out