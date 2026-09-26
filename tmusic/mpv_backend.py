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

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

from .config import find_mpv
from .errors import MPVProcessError, MPVTimeoutError
from .logging import get_logger

log = get_logger("mpv")

IPC_TIMEOUT = 5.0  # seconds for a control command to round-trip

# Global registry of active MPV backends so atexit / console close can clean them up
_ACTIVE_BACKENDS: weakref.WeakSet[MPVBackend] = weakref.WeakSet()
_WINDOWS_JOB_HANDLE: int | None = None
_WINDOWS_JOB_LOCK = threading.Lock()
_CONSOLE_CTRL_HANDLER_INSTALLED = False
_ORPHANS_CLEANED = False


def _create_kill_on_close_job() -> int | None:
    """Create a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    When the parent process terminates for ANY reason (terminal closed, crash,
    kill), Windows automatically closes the job handle, which triggers the Windows
    kernel to terminate all child processes (mpv.exe) instantly.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoCounters", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        res = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception as e:
        log.debug("could not create Windows Job Object: %s", e)
        return None


def _get_process_job() -> int | None:
    global _WINDOWS_JOB_HANDLE
    if sys.platform != "win32":
        return None
    with _WINDOWS_JOB_LOCK:
        if _WINDOWS_JOB_HANDLE is None:
            _WINDOWS_JOB_HANDLE = _create_kill_on_close_job()
        return _WINDOWS_JOB_HANDLE


def _cleanup_all_active_backends() -> None:
    for b in list(_ACTIVE_BACKENDS):
        try:
            b.shutdown()
        except Exception:
            pass


atexit.register(_cleanup_all_active_backends)


def _install_console_ctrl_handler() -> None:
    global _CONSOLE_CTRL_HANDLER_INSTALLED
    if sys.platform != "win32" or _CONSOLE_CTRL_HANDLER_INSTALLED:
        return
    try:
        import ctypes
        from ctypes import wintypes

        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(ctrl_type: int) -> bool:
            # 0=CTRL_C, 1=CTRL_BREAK, 2=CTRL_CLOSE, 5=CTRL_LOGOFF, 6=CTRL_SHUTDOWN
            _cleanup_all_active_backends()
            return False

        _handler._cb = HandlerRoutine(_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler._cb, True)
        _CONSOLE_CTRL_HANDLER_INSTALLED = True
    except Exception as e:
        log.debug("failed to install console ctrl handler: %s", e)


def _cleanup_stale_orphans_async() -> None:
    global _ORPHANS_CLEANED
    if sys.platform != "win32" or _ORPHANS_CLEANED:
        return
    _ORPHANS_CLEANED = True

    def _worker():
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name = 'mpv.exe'\" | Select-Object ProcessId, CommandLine | ConvertTo-Json",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
            if not res.stdout.strip():
                return
            data = json.loads(res.stdout)
            if isinstance(data, dict):
                data = [data]
            my_pid = os.getpid()
            import re
            for item in data:
                cmdline = item.get("CommandLine") or ""
                pid = item.get("ProcessId")
                if "tmusic-" in cmdline and pid:
                    m = re.search(r"tmusic-(\d+)-", cmdline)
                    if m:
                        parent_pid = int(m.group(1))
                        if parent_pid != my_pid:
                            check = subprocess.run(
                                ["tasklist", "/FI", f"PID eq {parent_pid}"],
                                capture_output=True,
                                text=True,
                            )
                            if str(parent_pid) not in check.stdout:
                                log.info("cleaning up orphaned mpv PID %s from dead parent %s", pid, parent_pid)
                                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        except Exception:
            pass

    threading.Thread(target=_worker, name="mpv-orphan-cleaner", daemon=True).start()


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
        self._stderr_drain: threading.Thread | None = None
        self._ipc: str | None = None
        self._sock_path: Path | None = None
        self._paused = False
        self._lock = threading.RLock()
        self._ipc_lock = threading.RLock()
        self._stopping = False
        self._cmd_seq = 0
        self._started_at = 0.0
        self._start_offset = 0.0  # where we seeked on load (for position math fallback)
        _ACTIVE_BACKENDS.add(self)
        _install_console_ctrl_handler()
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
            f"--input-ipc-server={ipc}",
            "--idle=yes",
            "--no-video",
            "--no-terminal",  # silence stdout noise; we use IPC
            "--really-quiet",
            "--keep-open=no",
            "--pause",  # start paused; the player calls resume() on purpose
            "--no-config",
        ]
        log.debug("starting mpv: %s", " ".join(cmd))
        self._stopping = False
        _cleanup_stale_orphans_async()
        popen_kwargs = {}
        if sys.platform != "win32":
            popen_kwargs["preexec_fn"] = os.setsid
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        job = _get_process_job()
        if job and hasattr(self.proc, "_handle"):
            try:
                import ctypes

                ctypes.windll.kernel32.AssignProcessToJobObject(job, int(self.proc._handle))
            except Exception as e:
                log.debug("failed to assign mpv to Job Object: %s", e)
        if not self._wait_ready():
            err_detail = ""
            if self.proc and self.proc.poll() is not None and self.proc.stderr:
                try:
                    err_bytes = self.proc.stderr.read()
                    if err_bytes:
                        err_detail = f": {err_bytes.decode('utf-8', errors='replace').strip()}"
                except Exception:
                    pass
            self._stop_process()
            raise MPVProcessError(f"mpv did not open its IPC socket{err_detail}")

        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc", daemon=True)
        self._reader.start()
        self._stderr_drain = threading.Thread(target=self._drain_stderr, name="mpv-stderr", daemon=True)
        self._stderr_drain.start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc and proc.stderr:
            try:
                for line in proc.stderr:
                    if line:
                        log.debug("mpv stderr: %s", line.decode("utf-8", errors="replace").strip())
            except Exception:
                pass

    def _wait_ready(self, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False
            if self._ipc is not None and self._send("get_property", "idle-active", timeout=0.4):
                return True
            time.sleep(0.05)
        return False

    def shutdown(self) -> None:
        self._stop_process()

    def _stop_process(self, quiet: bool = False) -> None:
        self._stopping = True
        with self._lock:
            proc = self.proc
            self.proc = None
        if proc and proc.poll() is None:
            try:
                self._send_raw(["quit"], timeout=1.0)
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
        if self._stderr_drain:
            self._stderr_drain.join(timeout=0.5)
            self._stderr_drain = None
        if self._sock_path:
            try:
                self._sock_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._sock_path = None
        self._ipc = None
        self._stopping = False

    # ------------------------------------------------------------------ IPC plumbing

    def _send_raw(self, cmd: list, timeout: float = IPC_TIMEOUT) -> bool:
        if not self._ipc:
            return False
        self._cmd_seq += 1
        payload = json.dumps({"command": cmd, "request_id": self._cmd_seq}) + "\n"
        with self._ipc_lock:
            try:
                if sys.platform == "win32":
                    f = self._win_connect(timeout)
                    f.sendall(payload.encode("utf-8"))
                    f.close()
                else:
                    import socket
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(timeout)
                    s.connect(self._ipc)
                    s.sendall(payload.encode("utf-8"))
                    s.close()
                return True
            except OSError as e:
                log.debug("IPC send_raw failed (%s): %s", cmd, e)
                return False

    def _send(self, command: str, *args, timeout: float = IPC_TIMEOUT) -> dict | None:
        """Send a command; None on any transport failure (caller decides)."""
        if self.proc is None or self.proc.poll() is not None:
            return None
        ok = self._send_raw([command, *args], timeout=timeout)
        return {"sent": True} if ok else None

    def _get_property(self, name: str, timeout: float = IPC_TIMEOUT) -> dict | None:
        """get_property with response matching."""
        if self.proc is None or self.proc.poll() is not None or not self._ipc:
            return None
        self._cmd_seq += 1
        rid = self._cmd_seq
        payload = json.dumps({"command": ["get_property", name], "request_id": rid}) + "\n"
        with self._ipc_lock:
            try:
                if sys.platform == "win32":
                    s = self._win_connect(timeout)
                else:
                    import socket
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(timeout)
                    s.connect(self._ipc)
                try:
                    s.sendall(payload.encode("utf-8"))
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
                                return msg
                finally:
                    s.close()
            except (OSError, json.JSONDecodeError) as e:
                log.debug("get_property(%s) failed: %s", name, e)
                return None
        return None

    def _win_connect(self, timeout: float) -> "object":
        # Windows named pipe: plain file open is the reliable path.
        class _PipeFile:
            def __init__(self, path: str):
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
                try:
                    self.f.close()
                except Exception:
                    pass

        return _PipeFile(self._ipc)

    def _read_loop(self) -> None:
        """Consume mpv's async events; surface interesting ones as MPVEvent."""
        conn = None
        deadline = time.monotonic() + 5.0
        while not self._stopping and self.proc is not None and self.proc.poll() is None and time.monotonic() < deadline:
            try:
                if sys.platform == "win32":
                    conn = self._win_connect(timeout=5.0)
                else:
                    import socket
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(1.0)
                    sock.connect(self._ipc)
                    conn = sock
                break
            except OSError:
                time.sleep(0.05)
        if conn is None:
            log.debug("IPC reader could not connect")
            return

        try:
            buf = b""
            while not self._stopping and self.proc is not None and self.proc.poll() is None:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
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
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if not self._stopping and self.proc is not None and self.proc.poll() is not None:
                rc = self.proc.returncode
                if rc != 0:
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