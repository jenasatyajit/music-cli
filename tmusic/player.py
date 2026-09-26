"""PlayerCore — playback state machine + the single worker thread.

Threading model (deliberate):
- The UI thread (curses) calls the public methods and reads snapshot()/
  drain_events(). It NEVER touches the YTMusic client or the mpv backend.
- One worker thread performs ALL network calls (search, stream URLs,
  library) and ALL playback transitions. mpv pause/seek are fired
  directly (fire-and-forget IPC, safe from any thread).
- This gives one writer for playback state and zero lock contention on the
  YTMusic client.

Events (drain_events): ("playing"|"paused"|"stopped"|"loading"|"error"
| "results"|"end-of-queue", payload...) — the UI translates them into
messages/view changes.
"""

from __future__ import annotations

import queue
import threading
import time

from .errors import TmusicError
from .logging import get_logger, log_exception
from .models import Playback, State, Track
from .mpv_backend import MPVBackend
from .ytm import get_stream_url, library_songs, liked_songs, quick_picks, search

log = get_logger("player")


class PlayerCore:
    def __init__(self, cfg, client) -> None:
        self.cfg = cfg
        self.client = client
        self.backend = MPVBackend(cfg)
        self.playback = Playback()
        self._cmd_q: "queue.Queue[tuple]" = queue.Queue()
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        self._load_gen = 0  # generation counter: stale loads are dropped

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, name="player-worker", daemon=True)
        self._worker.start()

    def shutdown(self) -> None:
        self._running = False
        try:
            self._cmd_q.put(("__shutdown__",))
        except Exception:
            pass
        if self._worker:
            self._worker.join(timeout=3)
        try:
            self.backend.shutdown()
        except Exception:
            log_exception(log, Exception("backend shutdown failed"), "core.shutdown")

    # ------------------------------------------------------------------ public API (UI thread)

    def snapshot(self) -> Playback:
        p = self.playback
        return Playback(
            state=p.state,
            queue=list(p.queue),
            index=p.index,
            position=p.position,
            status_message=p.status_message,
        )

    def drain_events(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except Exception:
                return out

    def play_list(self, tracks: list[Track], index: int = 0) -> None:
        if not tracks:
            return
        self._cmd_q.put(("play_list", tracks, max(0, min(index, len(tracks) - 1))))

    def next(self) -> None:
        self._cmd_q.put(("next",))

    def prev(self) -> None:
        self._cmd_q.put(("prev",))

    def toggle_pause(self) -> None:
        p = self.playback
        if p.state == State.PLAYING:
            self.backend.pause()
            p.state = State.PAUSED
            self._emit("paused")
        elif p.state == State.PAUSED:
            self.backend.resume()
            p.state = State.PLAYING
            self._emit("resumed")

    def stop(self) -> None:
        self._cmd_q.put(("stop",))

    def seek(self, delta: float) -> None:
        if self.playback.state not in (State.PLAYING, State.PAUSED):
            return
        self.backend.seek_by(delta)

    def add_to_queue(self, track: Track, after_current: bool = True) -> None:
        pb = self.playback
        if not pb.queue:
            pb.queue.append(track)
            return
        pos = pb.index + 1 if (after_current and pb.index >= 0) else len(pb.queue)
        pb.queue.insert(pos, track)
        self._emit("queue-changed")
        log.info("queued: %s (now %d)", track.label, len(pb.queue))

    def remove_from_queue(self, index: int) -> None:
        pb = self.playback
        if not (0 <= index < len(pb.queue)):
            return
        removed = pb.queue.pop(index)
        if index < pb.index:
            pb.index -= 1
        elif index == pb.index:
            # the current track was removed; mpv keeps playing it, but the
            # queue no longer references it.
            pass
        self._emit("queue-changed")
        log.info("dequeued: %s (now %d)", removed.label, len(pb.queue))

    # search / library — network, so they run on the worker
    def search(self, query: str, scope: str | None = None) -> None:
        self._cmd_q.put(("search", query, scope))

    def list_liked(self) -> None:
        self._cmd_q.put(("liked",))

    def list_library(self) -> None:
        self._cmd_q.put(("library",))

    def list_quick_picks(self) -> None:
        self._cmd_q.put(("quick_picks",))

    # ------------------------------------------------------------------ worker

    def _emit(self, kind: str, *payload) -> None:
        self._events.put((kind, *payload))

    def _loop(self) -> None:
        while self._running:
            try:
                cmd = self._cmd_q.get(timeout=0.2)
            except Exception:
                cmd = None

            # 1) handle a command
            if cmd:
                self._handle(cmd)

            # 2) watch for track end (auto-next)
            if self.playback.state in (State.PLAYING, State.PAUSED):
                for ev in self.backend.drain_events():
                    if ev.event == "end-file":
                        log.info("track ended: %s", self.playback.current)
                        self._auto_next()
                        break
                    if ev.event == "error":
                        self._set_error(f"playback error: {ev.reason}")
                    elif ev.event == "crash":
                        self._set_error("the audio engine crashed; restarting it")
                        self._restart_backend()

            # 3) position poll (2 Hz, centralised — the UI never polls mpv)
            if self.playback.state in (State.PLAYING, State.PAUSED):
                pos = self.backend.position()
                if pos is not None:
                    self.playback.position = pos

    def _handle(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "__shutdown__":
            self._running = False
            return
        if kind == "play_list":
            _, tracks, index = cmd
            self.playback.queue = list(tracks)
            self._load(index)
        elif kind == "next":
            self._load(self.playback.index + 1)
        elif kind == "prev":
            self._prev()
        elif kind == "stop":
            self._stop()
        elif kind == "search":
            self._do_search(cmd[1], cmd[2])
        elif kind == "liked":
            self._do_listing("liked_songs", "Your liked songs")
        elif kind == "library":
            self._do_listing("library_songs", "Your library")
        elif kind == "quick_picks":
            self._do_listing("quick_picks", "Quick picks")

    # ------------------------------------------------------------------ transitions

    def _load(self, index: int) -> None:
        pb = self.playback
        if not (0 <= index < len(pb.queue)):
            if pb.queue:
                self._emit("end-of-queue")
                self._stop(emit=False)
            return
        track = pb.queue[index]
        gen = self._load_gen + 1
        self._load_gen = gen
        pb.index = index
        pb.state = State.LOADING
        pb.position = 0.0
        pb.status_message = f"loading: {track.label}"
        self._emit("loading", track.label)
        log.info("loading [%d/%d]: %s", index, len(pb.queue), track.label)
        try:
            url, duration = get_stream_url(self.client, track)
            if track.duration_seconds <= 0:
                track.duration_seconds = duration
            if gen != self._load_gen:  # superseded by a newer command
                log.info("load superseded, dropping")
                return
            self.backend.play(url, seek=0.0)
            self.backend.resume()
            pb.state = State.PLAYING
            pb.status_message = ""
            self._emit("playing", track.label)
        except TmusicError as e:
            log_exception(log, e, f"load failed: {track.video_id}")
            pb.state = State.ERROR
            pb.status_message = e.description
            self._emit("error", e.description)
        except Exception as e:  # noqa: BLE001 — worker must survive
            log_exception(log, e, f"unexpected load failure: {track.video_id}")
            pb.state = State.ERROR
            pb.status_message = f"unexpected error: {e}"
            self._emit("error", pb.status_message)

    def _prev(self) -> None:
        pb = self.playback
        if pb.state in (State.PLAYING, State.PAUSED) and pb.position > 3:
            self.backend.seek(0)  # restart current
            pb.position = 0
            return
        self._load(pb.index - 1)

    def _stop(self, emit: bool = True) -> None:
        pb = self.playback
        self.backend.stop_track()
        pb.state = State.IDLE
        pb.position = 0.0
        if emit:
            self._emit("stopped")
        log.info("stopped")

    def _auto_next(self) -> None:
        pb = self.playback
        if pb.index + 1 < len(pb.queue):
            self._load(pb.index + 1)
        else:
            self._emit("end-of-queue")
            self._stop(emit=False)

    def _set_error(self, msg: str) -> None:
        self.playback.state = State.ERROR
        self.playback.status_message = msg
        self._emit("error", msg)

    def _restart_backend(self) -> None:
        try:
            self.backend.shutdown()
        except Exception:
            pass
        time.sleep(0.2)
        # next _load() will spawn a fresh mpv automatically

    # ------------------------------------------------------------------ listings

    def _do_search(self, query: str, scope: str | None) -> None:
        self.playback.status_message = f"searching: {query}"
        try:
            tracks = search(self.client, query, scope=scope)
            title = f"{'Library: ' if scope else 'Search: '}{query}"
            if tracks:
                self._emit("results", title, tracks)
            else:
                self._emit("message", f"no results for {query!r}")
        except TmusicError as e:
            log_exception(log, e, f"search failed: {query!r}")
            self._emit("error", e.description)
        self.playback.status_message = ""

    def _do_listing(self, fn_name: str, title: str) -> None:
        fn = {
            "liked_songs": lambda: liked_songs(self.client, limit=100),
            "library_songs": lambda: library_songs(self.client, limit=100),
            "quick_picks": lambda: quick_picks(self.client, limit=25),
        }[fn_name]
        self.playback.status_message = f"loading: {title}"
        try:
            tracks = fn()
            if tracks:
                self._emit("results", title, tracks)
            else:
                self._emit("message", f"{title}: empty")
        except TmusicError as e:
            log_exception(log, e, f"{title} failed")
            self._emit("error", e.description)
        self.playback.status_message = ""