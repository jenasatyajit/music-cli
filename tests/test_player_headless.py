"""Headless PlayerCore smoke test: state machine + worker + events (no TUI, no mpv).

Run: .venv/bin/python tests/test_player_headless.py
"""
import time

from tmusic.config import Config
from tmusic.models import Track
from tmusic.player import PlayerCore
from tmusic import player as player_mod


class StubBackend:
    def __init__(self):
        self.events = _EmptyQueue()
        self.started = False
        self.played = []
        self.paused = False
        self.pos = 12.5

    def play(self, url, seek=0.0):
        self.started = True
        self.played.append(url)
        self.pos = 0.0

    def resume(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def stop_track(self):
        pass

    def seek(self, s):
        pass

    def seek_by(self, s):
        pass

    def position(self):
        return self.pos

    def duration(self):
        return 200.0

    def is_running(self):
        return True

    def wait_end(self, timeout=None):
        return "end"

    def drain_events(self):
        return []

    def shutdown(self):
        pass


class _EmptyQueue:
    def put(self, *a):
        pass

    def get_nowait(self):
        raise Exception("empty")


def make_track(n, vid="v%02d", dur=100.0):
    return Track(video_id=vid % n, title=f"Song {n}", artists="Test Artist", duration_seconds=dur)


def wait_for(core, pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        evs = core.drain_events()
        if pred(core.snapshot(), evs):
            return evs
        time.sleep(0.05)
    return []


def main():
    from tmusic.ytm import YTMusic
    from ytmusicapi import YTMusic as RealYTMusic

    cfg = Config()
    client = RealYTMusic()  # unauth, only used for search

    # stub the stream resolver (no network)
    player_mod.get_stream_url = lambda c, t: (f"https://example/stream/{t.video_id}", 200.0)

    core = PlayerCore(cfg, client)
    core.backend = StubBackend()
    core.start()
    failures = []

    # 1) play a list -> loading -> playing
    tracks = [make_track(i) for i in range(1, 4)]
    core.play_list(tracks, 0)
    evs = wait_for(core, lambda pb, evs: pb.state.name == "PLAYING")
    if pb := core.snapshot():
        if pb.state.name != "PLAYING" or pb.index != 0:
            failures.append(f"play_list: state={pb.state} index={pb.index}")
    if not core.backend.played or not core.backend.played[0].startswith("https://example/stream/"):
        failures.append("backend.play not called with resolved url")
    print("1) play_list -> playing:         ", "OK" if not failures else failures)

    # 2) position polling reached the UI side
    core.backend.pos = 12.5
    time.sleep(0.3)
    pb = core.snapshot()
    if pb.position != 12.5:
        failures.append(f"position poll: {pb.position}")
    print("2) position poll (12.5):         ", "OK" if pb.position == 12.5 else failures)

    # 3) pause / resume
    core.toggle_pause()
    time.sleep(0.2)
    if core.snapshot().state.name != "PAUSED":
        failures.append("pause: " + core.snapshot().state.name)
    if not core.backend.paused:
        failures.append("backend.pause not called")
    core.toggle_pause()
    time.sleep(0.2)
    if core.snapshot().state.name != "PLAYING":
        failures.append("resume: " + core.snapshot().state.name)
    print("3) pause/resume:                  ", "OK" if not any(f.startswith(("pause", "resume", "backend.pause")) for f in failures) else failures)

    # 4) next / prev
#    semantics: prev -> previous track when <=3s in; restart current when >3s in
    core.next()
    wait_for(core, lambda pb, evs: pb.index == 1)
    if core.snapshot().index != 1:
        failures.append(f"next: index={core.snapshot().index}")
    core.backend.pos = 0.0
    wait_for(core, lambda pb, evs: pb.position == 0.0)  # ensure position poll applied
    core.prev()  # at index1, pos 0.0 (<=3s) -> previous track (index 0)
    wait_for(core, lambda pb, evs: pb.index == 0)
    if core.snapshot().index != 0:
        failures.append(f"prev(track): expected 0, got {core.snapshot().index}")
    core.backend.pos = 30.0  # mid-track
    wait_for(core, lambda pb, evs: pb.position == 30.0)
    core.prev()  # >3s in -> restart current (stays index 0, seeks to 0)
    core.backend.pos = 0.0  # stub: simulate the seek(0)
    time.sleep(0.3)
    if core.snapshot().index != 0:
        failures.append(f"prev(restart): expected 0, got {core.snapshot().index}")
    if core.snapshot().position != 0.0:
        failures.append(f"prev(restart): expected pos 0, got {core.snapshot().position}")
    print("4) next/prev:                     ", "OK" if not any(f.startswith(("next", "prev")) for f in failures) else failures)

    # 5) add + remove from queue (add inserts AFTER current index 0)
    n0 = len(core.snapshot().queue)
    core.add_to_queue(make_track(99))
    if len(core.snapshot().queue) != n0 + 1:
        failures.append("add_to_queue: " + str(len(core.snapshot().queue)))
    core.remove_from_queue(1)  # the track we just inserted
    if len(core.snapshot().queue) != n0:
        failures.append("remove_from_queue: " + str(len(core.snapshot().queue)))
    print("5) add/remove queue:              ", "OK" if not any("queue" in f for f in failures) else failures)

    # 6) stop
    core.stop()
    wait_for(core, lambda pb, evs: pb.state.name == "IDLE")
    if core.snapshot().state.name != "IDLE":
        failures.append(f"stop: {core.snapshot().state.name}")
    print("6) stop -> idle:                  ", "OK" if not any(f.startswith("stop") for f in failures) else failures)

    # 7) real network search (unauth)
    core.search("oasis wonderwall")
    evs = wait_for(core, lambda pb, evs: any(e[0] == "results" or e[0] == "error" for e in evs), timeout=15)
    found = [e for e in evs if e[0] == "results"]
    if not found:
        err = [e for e in evs if e[0] == "error"]
        if not err:
            failures.append("search: no results or error event")
        else:
            failures.append(f"search error: {err[0][1]}")
    else:
        print("7) search (real network):       OK —", len(found[0][2]), "tracks")

    core.shutdown()
    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("ALL PLAYER SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()