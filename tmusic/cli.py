"""tmusic CLI — argparse dispatch.

Commands:
    tmusic doctor            verify environment (config, token, mpv, network)
    tmusic auth login        OAuth device flow to YouTube Music
    tmusic auth status       show logged-in account
    tmusic search <query>    list search results (headless)
    tmusic play <query>      search + play first result via mpv
    tmusic run               full curses TUI (M2)

Exit codes: 0 ok, 1 user error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .config import (
    browser_auth_path,
    config_dir,
    ensure_config_dir,
    find_mpv,
    load_config,
    write_config_sample,
)
from .errors import TmusicError
from .logging import get_logger, log_exception, setup_logging, user_error_text

log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tmusic",
        description="tmusic — terminal YouTube Music player",
    )
    p.add_argument("--version", action="version", version=f"tmusic {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("doctor", help="verify environment and configuration")

    auth = sub.add_parser("auth", help="YouTube Music authentication")
    auth_sub = auth.add_subparsers(dest="auth_command")
    auth_sub.add_parser("login", help="log in via Google (device flow) — legacy")
    auth_sub.add_parser("cookies", help="log in via browser cookies (recommended)")
    auth_sub.add_parser("status", help="show the logged-in account")

    sp = sub.add_parser("search", help="search YouTube Music")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--library", action="store_true", help="search your library instead")

    pp = sub.add_parser("play", help="search and play the first result")
    pp.add_argument("query", nargs="+")
    pp.add_argument("--index", type=int, default=1, help="which result to play (default 1)")

    sub.add_parser("run", help="launch the full-screen TUI (M2)")
    return p


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------- doctor

def cmd_doctor(cfg) -> int:
    print(f"tmusic {__version__} — environment check")
    ok = True

    print(f"\n[config] directory: {config_dir()}")
    ensure_config_dir()
    print(f"[config] config.toml: {config_dir() / 'config.toml'}", end="")
    if (config_dir() / "config.toml").exists():
        print(" (present)")
    else:
        print(" (missing — a template will be created on first use)")
        ok = False

    print(f"[auth]   browser.json:  {browser_auth_path()} "
          + ("(present — logged in)" if browser_auth_path().exists()
             else "(absent — unauthenticated, library unavailable)"))

    try:
        mpv = find_mpv(cfg)
        print(f"[mpv]    mpv:           {mpv}")
    except TmusicError as e:
        print(f"[mpv]    mpv:           MISSING — {e}")
        ok = False

    try:
        import yt_dlp
        print(f"[ytdlp]  yt-dlp:        {yt_dlp.version.__version__}")
    except ImportError:
        print("[ytdlp]  yt-dlp:        MISSING — pip install yt-dlp")
        ok = False

    if browser_auth_path().exists():
        try:
            from .ytm import account_info, make_client

            info = account_info(make_client(cfg))
            print(f"[ytm]    account:       {info.get('name') or info.get('accountName') or 'unknown'}")
        except TmusicError as e:
            print(f"[ytm]    account:       check failed: {user_error_text(e)}")
            ok = False

    print(f"\nlog file: {config_dir() / 'tmusic.log'}")
    print(f"\n{'all good' if ok else 'fix the items above, then re-run tmusic doctor'}")
    return 0 if ok else 1


# --------------------------------------------------------------------- auth

def cmd_login(cfg) -> int:
    from .ytm import oauth_login

    oauth_login(cfg)
    return 0


def cmd_cookies(cfg) -> int:
    from .ytm import browser_login

    print("Paste the request headers of any request from music.youtube.com\n"
          "(logged in), then press Enter followed by Ctrl-D (Linux/macOS)\n"
          "or Enter, Ctrl-Z, Enter (Windows) to finish.\n")
    print("Tip: Chrome/Edge DevTools → Network → pick a music.youtube.com\n"
          "request → Copy → Copy request headers.\n")
    try:
        browser_login(cfg)
    except EOFError:
        print("\n(nothing pasted)", file=sys.stderr)
        return _fail("no headers received — try again")
    return 0


def cmd_status(cfg) -> int:
    from .ytm import account_info, is_authenticated, make_client

    if is_authenticated():
        client = make_client(cfg)
        info = account_info(client)
        print(f"logged in (browser cookies) as: "
              f"{info.get('name') or info.get('accountName') or 'unknown'}")
        return 0
    print("not logged in — running unauthenticated (public search + playback).")
    print("for library/liked songs:  tmusic auth cookies")
    return 0


def _auth_dispatch(cfg, args) -> int:
    if args.auth_command == "login":
        return cmd_login(cfg)
    if args.auth_command == "cookies":
        return cmd_cookies(cfg)
    if args.auth_command == "status":
        return cmd_status(cfg)
    return _fail("say 'login', 'cookies' or 'status'")


# --------------------------------------------------------------------- search / play

def _client_or_die(cfg):
    from .ytm import make_client

    return make_client(cfg)


def cmd_search(cfg, args) -> int:
    from .models import fmt_time
    from .ytm import is_authenticated, search

    if args.library and not is_authenticated():
        return _fail("library search needs login — run `tmusic auth cookies` first.")
    client = _client_or_die(cfg)
    query = " ".join(args.query)
    tracks = search(client, query, limit=args.limit or cfg.search_limit,
                    scope="library" if args.library else None)
    if not tracks:
        print(f"no results for {query!r}")
        return 1
    for i, t in enumerate(tracks, 1):
        print(f"{i:2d}. {t.label}   [{fmt_time(t.duration_seconds)}]")
    print("\nplay one with: tmusic play " + query + " --index N")
    return 0


def cmd_play(cfg, args) -> int:
    from .mpv_backend import MPVBackend
    from .ytm import get_stream_url, search

    client = _client_or_die(cfg)
    query = " ".join(args.query)
    tracks = search(client, query, limit=max(5, args.index))
    if not tracks:
        return _fail(f"no results for {query!r}")
    if args.index < 1 or args.index > len(tracks):
        return _fail(f"--index {args.index} out of range (1..{len(tracks)})")
    track = tracks[args.index - 1]
    print(f"playing: {track.label}")
    backend = MPVBackend(cfg)
    try:
        url, duration = get_stream_url(client, track)
        if duration and not track.duration_seconds:
            track.duration_seconds = duration
        backend.play(url)
        backend.resume()
        print(f"stream: {url[:80]}...")
        print("mpv is running — press Ctrl+C here to stop.")
        try:
            pos = 0.0
            while backend.is_running():
                time.sleep(0.5)
                p = backend.position()
                if p is not None:
                    pos = p
            # loop exited because mpv died or the file ended
            evs = backend.drain_events()
            for ev in evs:
                if ev.event in ("end-file", "error"):
                    break
            print(f"\nfinished at {pos:.0f}s (mpv exited)")
        except KeyboardInterrupt:
            print("\nstopping...")
    finally:
        backend.shutdown()
    return 0


# --------------------------------------------------------------------- run (TUI)

def cmd_run(cfg, args) -> int:
    if not sys.stdout.isatty():
        return _fail("the TUI needs an interactive terminal (not a pipe).")
    from .tui import run_tui

    return run_tui(cfg)


# --------------------------------------------------------------------- dispatch

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = setup_logging(args.verbose)
    log.info("tmusic %s started, args=%s", __version__, argv)

    try:
        cfg = load_config()
        write_config_sample()
        ensure_config_dir()
    except TmusicError as e:
        return _fail(user_error_text(e))

    handlers = {
        "doctor": lambda: cmd_doctor(cfg),
        "auth": lambda: _auth_dispatch(cfg, args),
        "search": lambda: cmd_search(cfg, args),
        "play": lambda: cmd_play(cfg, args),
        "run": lambda: cmd_run(cfg, args),
        None: lambda: (parser.print_help(), 0)[1],
    }
    try:
        return handlers[args.command]()
    except TmusicError as e:
        log_exception(log, e, "command failed")
        return _fail(user_error_text(e))
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 130
    except Exception as e:  # last resort: never dump a raw traceback at the user
        log_exception(log, e, "unexpected failure")
        return _fail(
            f"unexpected error: {e}\n"
            f"details: {config_dir() / 'tmusic.log'}"
        )


if __name__ == "__main__":
    sys.exit(main())