"""tmusic curses TUI — full-screen player.

Layout (main view):

    ┌ tmusic v0.2 ─ YouTube Music ────────────────────── [authed]
    │
    │   ▂▄▆█▆▄▂▅▃▂   Wonderwall — Oasis
    │   (What's The Story) Morning Light?
    │
    │   00:00 ──[██████▏░░░░░░░░░░░░░░░░░░░]────────── 04:19
    │
    ├─ queue (3) ─────────────────────────────────────
    │   >  Wonderwall — Oasis               04:19
    │      Champagne Supernova — Oasis      07:31
    │      Don't Look Back — Oasis          04:42
    └─────────────────────────────────────────────────
     space=pause n=next p=prev /search l=library m=liked ?=help q=quit

Modes: MAIN (queue), RESULTS (search/library/liked list), PROMPT (inline
search input), HELP overlay. Rendering is a single draw() per frame driven
by a 50 ms curses timeout; all state comes from PlayerCore.snapshot() and
core.drain_events().
"""

from __future__ import annotations

import curses
import sys
import time

from . import __version__
from .models import Playback, State, Track, fmt_time
from .player import PlayerCore

try:  # windows-curses provides a curses shim on win32
    from curses import KEY_RESIZE
except ImportError:  # pragma: no cover
    KEY_RESIZE = 410

EQU_CHARS = "▁▂▃▄▅▆▇█"


def _eq_frame(seed: str, width: int, tick: int) -> str:
    """Deterministic pseudo-EQ: bars derived from seed + tick (cosmetic)."""
    out = []
    for i in range(width):
        h = 0
        s = f"{seed}:{i}:{tick // 3 % 97}"
        for ch in s:
            h = (h * 31 + ord(ch)) % 1000003
        v = (h % 9)
        # smooth a bit with the neighbour phase
        out.append(EQU_CHARS[v])
    return "".join(out)


def _clip(s: str, width: int) -> str:
    s = s or ""
    return s if len(s) <= width else s[: max(0, width - 1)] + "…"


def _bar(pos: float, total: float, width: int) -> str:
    if total <= 0:
        return "─" * width
    frac = max(0.0, min(1.0, pos / total))
    filled = int(frac * width)
    return "█" * filled + "─" * (width - filled)


def _safe_write(win, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            win.addnstr(y, x, text, min(len(text), w - x - 1), attr)
        except curses.error:
            pass


class TUIApp:
    def __init__(self, core: PlayerCore, authed: bool) -> None:
        self.core = core
        self.authed = authed
        self.mode = "main"  # main | results | prompt | help
        self.results: list[Track] = []
        self.results_title = ""
        self.results_cursor = 0
        self.prompt = ""
        self.prompt_scope: str | None = None
        self.scroll = 0  # queue scroll offset
        self.cursor = 0  # queue cursor (main)
        self.status = ""
        self.status_until = 0.0
        self.tick = 0
        self._last_resize = 0.0

    # ------------------------------------------------------------------ msgs

    def flash(self, msg: str, secs: float = 3.0) -> None:
        self.status = msg
        self.status_until = time.monotonic() + secs

    # ------------------------------------------------------------------ event loop

    def run(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        curses.use_default_colors()
        try:
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            self.C_CYAN, self.C_GREEN = curses.color_pair(1), curses.color_pair(2)
            self.C_YELLOW, self.C_RED = curses.color_pair(3), curses.color_pair(4)
            self.C_MAG = curses.color_pair(5)
        except curses.error:
            self.C_CYAN = self.C_GREEN = self.C_YELLOW = self.C_RED = self.C_MAG = 0

        self.core.start()
        try:
            while True:
                self.tick += 1
                self._drain_events()
                self._resize(stdscr)
                self._draw(stdscr)
                stdscr.refresh()
                key = self._read_key(stdscr)
                if key is None:
                    continue
                if self._handle_key(key) == "quit":
                    return 0
        finally:
            self.core.shutdown()

    def _read_key(self, stdscr) -> int | None:
        try:
            while True:
                ch = stdscr.getch()
                if ch == -1:
                    return None
                if ch == curses.KEY_MOUSE:
                    mx, my, _, _, _ = curses.getmouse()
                    return self._mouse_to_key(my, mx, stdscr)
                return ch
        except curses.error:
            return None

    def _mouse_to_key(self, my: int, mx: int, stdscr) -> int:
        """Timeline click → seek; otherwise ignore."""
        h, w = stdscr.getmaxyx()
        if my == 4 and self.mode == "main":
            track = self.core.snapshot().current
            if track and track.duration_seconds > 0:
                x0, x1 = 6, w - 8
                if x0 < mx < x1:
                    frac = (mx - x0) / max(1, (x1 - x0))
                    target = frac * track.duration_seconds
                    delta = target - self.core.snapshot().position
                    self.core.seek(delta)
        return -1  # consumed, no action

    def _resize(self, stdscr) -> None:
        if time.monotonic() - self._last_resize > 0.2:
            curses.resizeterm(*stdscr.getmaxyx())
            self._last_resize = time.monotonic()

    # ------------------------------------------------------------------ events

    def _drain_events(self) -> None:
        for ev in self.core.drain_events():
            kind = ev[0]
            if kind == "playing":
                self.flash(f"▶ {ev[1]}", 2.0)
                self.mode = "main"
            elif kind == "paused":
                self.flash("⏸ paused", 2.0)
            elif kind == "resumed":
                self.flash("▶ resumed", 2.0)
            elif kind == "stopped":
                self.flash("■ stopped")
            elif kind == "loading":
                self.flash(f"… loading {ev[1]}", 30.0)
            elif kind == "error":
                self.flash(f"✖ {ev[1]}", 6.0)
            elif kind == "end-of-queue":
                self.flash("end of queue")
            elif kind == "results":
                self.results_title, self.results = ev[1], ev[2]
                self.results_cursor = 0
                self.mode = "results"
            elif kind == "message":
                self.flash(ev[1], 4.0)
            elif kind == "queue-changed":
                self.flash("queue updated")

    # ------------------------------------------------------------------ keys

    def _handle_key(self, key: int) -> str:
        if self.mode == "help":
            return "quit" if key in (ord("q"), 27, 10) else None

        if self.mode == "prompt":
            return self._key_prompt(key)

        if self.mode == "results":
            return self._key_results(key)

        # main
        pb = self.core.snapshot()
        if key in (ord("q"),):
            return "quit"
        elif key == 27:  # esc
            return "quit"
        elif key in (ord(" "), 10):
            if pb.state in (State.IDLE, State.ERROR) and pb.queue:
                # nothing playing (or errored) — start the queue at the cursor
                self.core.play_list(pb.queue, self.cursor)
            else:
                self.core.toggle_pause()
        elif key in (ord("n"),):
            self.core.next()
        elif key in (ord("p"),):
            self.core.prev()
        elif key in (ord("x"),):
            self.core.stop()
        elif key in (ord("/"),):
            self.mode = "prompt"
            self.prompt = ""
            self.prompt_scope = None
        elif key in (ord("L"),):
            self.mode = "prompt"
            self.prompt = ""
            self.prompt_scope = "library"
        elif key in (ord("l"),):
            self.core.list_library()
        elif key in (ord("m"),):
            self.core.list_liked()
        elif key in (ord("?"),):
            self.mode = "help"
        elif key == curses.KEY_LEFT:
            self.core.seek(-self.core.cfg.seek_seconds)
        elif key == curses.KEY_RIGHT:
            self.core.seek(self.core.cfg.seek_seconds)
        elif key == 68:  # shift+left (windows-curses / xterm convention)
            self.core.seek(-self.core.cfg.long_seek_seconds)
        elif key == 66:  # shift+right
            self.core.seek(self.core.cfg.long_seek_seconds)
        elif key in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
            self._autoscroll()
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(pb.queue) - 1, self.cursor + 1)
            self._autoscroll()
        elif key == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - 5)
            self._autoscroll()
        elif key == curses.KEY_NPAGE:
            self.cursor = min(len(pb.queue) - 1, self.cursor + 5)
            self._autoscroll()
        elif key in (ord("d"),):
            if 0 <= self.cursor < len(pb.queue):
                self.core.remove_from_queue(self.cursor)
                self.cursor = min(self.cursor, len(self.core.snapshot().queue) - 1)
        elif key == -1:
            return None
        return None

    def _key_prompt(self, key: int) -> str | None:
        if key in (27, 9):
            self.mode = "main"
        elif key in (10, 13, curses.KEY_ENTER):
            q = self.prompt.strip()
            if q:
                self.mode = "main"
                self.core.search(q, scope=self.prompt_scope)
            else:
                self.mode = "main"
        elif key in (127, curses.KEY_BACKSPACE, 8):
            self.prompt = self.prompt[:-1]
        elif key == -1:
            pass
        elif 32 <= key < 127:
            self.prompt += chr(key)
        return None

    def _key_results(self, key: int) -> str | None:
        if key in (27, 9, ord("q")):
            self.mode = "main"
        elif key in (curses.KEY_UP, ord("k")):
            self.results_cursor = max(0, self.results_cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.results_cursor = min(len(self.results) - 1, self.results_cursor + 1)
        elif key in (10, 13, curses.KEY_ENTER):
            t = self.results[self.results_cursor]
            # play this result as a queue starting at it
            self.core.play_list(self.results[self.results_cursor:], 0)
            self.mode = "main"
        elif key == ord("a"):
            self.core.add_to_queue(self.results[self.results_cursor])
        elif key == ord("s"):
            self.core.play_list(self.results, self.results_cursor)
            self.mode = "main"
        elif key == -1:
            pass
        return None

    def _autoscroll(self) -> None:
        h, w = 24, 80
        list_h = max(3, h - 9)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + list_h:
            self.scroll = self.cursor - list_h + 1

    # ------------------------------------------------------------------ draw

    def _draw(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if self.mode == "help":
            self._draw_help(stdscr, h, w)
            return
        self._draw_header(stdscr, h, w)
        self._draw_nowplaying(stdscr, h, w)
        if self.mode == "results":
            self._draw_results(stdscr, h, w)
        else:
            self._draw_queue(stdscr, h, w)
        self._draw_status(stdscr, h, w)

    def _draw_header(self, stdscr, h: int, w: int) -> None:
        auth = f"[{ 'logged in' if self.authed else 'guest' }]"
        left = f" tmusic v{__version__} ─ YouTube Music "
        mid = max(0, w - len(left) - len(auth))
        _safe_write(stdscr, 0, 0, left + "─" * mid, curses.A_BOLD | self.C_CYAN)
        _safe_write(stdscr, 0, w - len(auth), auth, self.C_CYAN)

    def _draw_nowplaying(self, stdscr, h: int, w: int) -> None:
        pb = self.core.snapshot()
        track = pb.current
        y = 2
        if track:
            seed = track.video_id or track.title
            if pb.state == State.PLAYING:
                eq = _eq_frame(seed, 12, self.tick)
            elif pb.state == State.PAUSED:
                eq = "┈" * 12
            elif pb.state == State.LOADING:
                eq = "▓▒░▓▒░▓▒░▓"
            else:
                eq = " " * 12
            _safe_write(stdscr, y, 4, eq, self.C_MAG)
            _safe_write(stdscr, y, 18, _clip(track.label, w - 22), curses.A_BOLD)
            if track.album:
                _safe_write(stdscr, y + 1, 18, _clip(track.album, w - 22), curses.A_DIM)
        else:
            _safe_write(stdscr, y, 4, "♪ nothing playing — / to search", curses.A_DIM)

        # timeline
        ty = 4
        dur = track.duration_seconds if track else 0.0
        if dur <= 0 and pb.state == State.PLAYING and pb.position > 0:
            dur = pb.position  # avoid div-by-zero; will self-correct
        pos_txt = fmt_time(pb.position)
        dur_txt = fmt_time(dur)
        bar_w = max(10, min(w - 28, w // 2))
        if track:
            bar = _bar(pb.position, dur, bar_w) if dur > 0 else "─" * bar_w
            line = f"{pos_txt:>7} ──[{bar}]─ {dur_txt}"
            _safe_write(stdscr, ty, 0, line[: w - 1], self.C_GREEN)
        else:
            _safe_write(stdscr, ty, 0, f"{pos_txt:>7} {'─' * bar_w} {dur_txt}", curses.A_DIM)
        if pb.state == State.PAUSED and track:
            _safe_write(stdscr, ty + 1, 2, "⏸ PAUSED — space to resume", self.C_YELLOW)
        elif pb.state == State.LOADING:
            _safe_write(stdscr, ty + 1, 2, "… resolving stream", curses.A_DIM)
        elif pb.state == State.ERROR and pb.status_message:
            _safe_write(stdscr, ty + 1, 2, _clip("✖ " + pb.status_message, w - 2), self.C_RED)

    def _draw_queue(self, stdscr, h: int, w: int) -> None:
        pb = self.core.snapshot()
        qy = 7
        _safe_write(stdscr, qy, 0, f" queue ({len(pb.queue)})", curses.A_BOLD | self.C_CYAN)
        if not pb.queue:
            _safe_write(stdscr, qy + 1, 2, "empty — / search then Enter to play", curses.A_DIM)
            return
        list_h = max(3, h - qy - 3)
        if self.scroll > len(pb.queue) - 1:
            self.scroll = max(0, len(pb.queue) - 1)
        for row in range(list_h):
            idx = self.scroll + row
            if idx >= len(pb.queue):
                break
            t = pb.queue[idx]
            marker = ">" if idx == pb.index else " "
            attr = 0
            if idx == pb.index:
                attr = curses.A_BOLD | self.C_GREEN
            if idx == self.cursor and self.cursor != pb.index:
                attr = curses.A_REVERSE
            dur_t = f"{fmt_time(t.duration_seconds)}" if t.duration_seconds else ""
            _safe_write(stdscr, qy + 1 + row, 0, f" {marker}", attr)
            _safe_write(stdscr, qy + 1 + row, 3, _clip(t.label, w - 14), attr)
            _safe_write(stdscr, qy + 1 + row, w - 9, dur_t, attr)

    def _draw_results(self, stdscr, h: int, w: int) -> None:
        ry = 6
        _safe_write(stdscr, ry, 0, _clip(f" {self.results_title} ({len(self.results)})", w - 1),
                    curses.A_BOLD | self.C_CYAN)
        _safe_write(stdscr, ry, 0, " ", 0)
        list_h = max(3, h - ry - 4)
        if self.results_cursor < self.scroll:
            self.scroll = self.results_cursor
        elif self.results_cursor >= self.scroll + list_h:
            self.scroll = self.results_cursor - list_h + 1
        if not self.results:
            _safe_write(stdscr, ry + 1, 2, "(no results)", curses.A_DIM)
        for row in range(list_h):
            idx = self.scroll + row
            if idx >= len(self.results):
                break
            t = self.results[idx]
            marker = ">" if idx == self.results_cursor else " "
            attr = curses.A_REVERSE if idx == self.results_cursor else 0
            dur_t = f"{fmt_time(t.duration_seconds)}" if t.duration_seconds else ""
            _safe_write(stdscr, ry + 1 + row, 0, f" {marker}", attr)
            _safe_write(stdscr, ry + 1 + row, 3, _clip(t.label, w - 14), attr)
            _safe_write(stdscr, ry + 1 + row, w - 9, dur_t, attr)
        _safe_write(stdscr, ry + list_h + 1, 0, " Enter=play s=play all a=add to queue esc=back", curses.A_DIM)

    def _draw_status(self, stdscr, h: int, w: int) -> None:
        if self.mode == "prompt":
            label = "search library> " if self.prompt_scope == "library" else "search> "
            line = _clip(label + self.prompt + "█", w - 1)
            _safe_write(stdscr, h - 1, 0, line, curses.A_REVERSE)
            _safe_write(stdscr, h - 2, 0, _clip("(enter to search, esc to cancel)", w - 1), curses.A_DIM)
            return
        y = h - 1
        now = time.monotonic()
        status = self.status if now < self.status_until else ""
        base = " space=pause n=next p=prev ←/→=seek l=library m=liked d=del ?=help q=quit"
        if status:
            _safe_write(stdscr, y, 0, _clip(" " + status + "  ", w - len(base) - 1), self.C_YELLOW)
            _safe_write(stdscr, y, w - len(base), _clip(base, len(base)), curses.A_DIM)
        else:
            _safe_write(stdscr, y, 0, _clip(" " + base, w - 1), curses.A_DIM)

    def _draw_help(self, stdscr, h: int, w: int) -> None:
        rows = [
            "tmusic — keys",
            "",
            "  space / enter    play · pause",
            "  n · p            next · previous (p restarts track after 3s)",
            "  x                stop",
            "  ← / →            seek 10s        PgUp / PgDn  seek 30s",
            "  mouse click      seek (on the progress line)",
            "  /                search public catalogue",
            "  L                search your library",
            "  l                load your library",
            "  m                load your liked songs",
            "  j / k or ↑ / ↓   move cursor     d  delete from queue",
            "  in results: Enter play from here · s play all · a add to queue",
            "",
            "  ?                this help        q / esc  quit",
        ]
        y0 = max(0, h // 2 - len(rows) // 2)
        for i, row in enumerate(rows):
            _safe_write(stdscr, y0 + i, max(1, w // 2 - 30), row,
                        curses.A_BOLD if i == 0 else 0)
        _safe_write(stdscr, y0 + len(rows) + 1, max(1, w // 2 - 20),
                    "(any key to close)", curses.A_DIM)


def run_tui(cfg) -> int:
    from .logging import mute_console
    from .ytm import is_authenticated, make_client

    mute_console()  # curses owns the terminal; logs go to file only
    client = make_client(cfg)
    core = PlayerCore(cfg, client)
    app = TUIApp(core, authed=is_authenticated())

    def _main(stdscr) -> int:
        return app.run(stdscr)

    try:
        return curses.wrapper(_main)
    except KeyboardInterrupt:
        return 130
    except TuiFatalError as e:
        print(f"error: {e.user_message}", file=sys.stderr)
        return 1


class TuiFatalError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message