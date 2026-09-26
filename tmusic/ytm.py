"""YouTube Music service: auth (OAuth device flow), search, stream URLs.

All ytmusicapi exceptions are translated here into tmusic.errors types so no
upstream type ever leaks into the UI layer.

Threading: YTMusic is NOT thread-safe; every public method takes the client
and does one call. The CLI/TUI owns the client instance.
"""

from __future__ import annotations

import getpass
import sys
import time

import requests
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.exceptions import YTMusicError

from .config import browser_auth_path, config_dir, read_token, token_path, write_token
from .errors import (
    AuthError,
    MissingOauthClient,
    OauthLoginFailed,
    StreamUrlError,
    TrackNotPlayable,
    YTMGatedError,
    YTMApiError,
)
from .logging import get_logger
from .models import Track

log = get_logger("ytm")


def _translate(e: BaseException, context: str) -> YTMApiError:
    """Map ytmusicapi/requests failures to user-facing types."""
    from ytmusicapi.exceptions import YTMusicGatedError, YTMusicUserError  # 1.x names

    if isinstance(e, YTMusicError):
        msg = str(e)
        if isinstance(e, YTMusicGatedError) or "gated" in msg.lower() or "bot" in msg.lower():
            log.info("gated/bot-checked request: %s", msg)
            return YTMGatedError()  # keep the user-facing default message
        if "401" in msg or "Unauthorized" in msg:
            return AuthError(
                "YouTube Music says you're not logged in — that's needed for this.\n"
                "Run `tmusic auth cookies` and paste fresh headers."
            )
        if isinstance(e, YTMusicUserError):
            return YTMApiError(msg)
        return YTMApiError(f"YouTube Music error during {context}: {msg}")
    if isinstance(e, requests.RequestException):
        return YTMApiError(f"Network problem during {context}: {e}")
    return YTMApiError(f"Unexpected failure during {context}: {e}")


def make_client(cfg) -> YTMusic:
    """Build a YTMusic client.

    Auth resolution order (first hit wins):
      1. browser.json  — browser/cookie auth (SAPISIDHASH), the reliable path
      2. unauthenticated — public search + playback (no library)

    NOTE: pure-OAuth (oauth.json) is kept out of the default path because
    YouTube's InnerTube API rejects Bearer tokens on the WEB_REMIX client
    since 2025-08-29 (upstream ytmusicapi issue #813).
    """
    browser = browser_auth_path()
    if browser.exists():
        try:
            client = YTMusic(str(browser))
            return client
        except Exception as e:
            raise AuthError(
                "browser.json is invalid or its cookies have expired.\n"
                "Re-run `tmusic auth cookies` and paste fresh headers.\n"
                f"(detail: {e})"
            ) from e
    return YTMusic()


def is_authenticated() -> bool:
    return browser_auth_path().exists()


def oauth_login(cfg, poll_interval: float = 3.0, timeout: float = 600.0,
                interactive: bool = True) -> dict:
    """OAuth device-flow login.

    interactive=True  (tmusic auth login, local console): print URL, then
                      press Enter after authorizing (classic flow).
    interactive=False (headless/remote): print URL once, then poll Google
                      automatically until authorized — no keypress needed.
    """
    if not cfg.oauth_client_id or not cfg.oauth_client_secret:
        raise MissingOauthClient()
    creds = OAuthCredentials(
        client_id=cfg.oauth_client_id,
        client_secret=cfg.oauth_client_secret,
        session=requests.Session(),
    )
    try:
        code = creds.get_code()
    except Exception as e:
        raise OauthLoginFailed(f"Could not start Google login: {e}") from e

    url = code.get("verification_url", "")
    user_code = code.get("user_code", "")
    print("\n=== tmusic — YouTube Music login ===")
    print(f"1. Open this URL in any browser (your laptop is fine):\n\n    {url}")
    if user_code:
        print(f"\n2. If Google asks for a code, enter:  {user_code}\n")
    if interactive:
        print("3. Come back here and press Enter when you've authorized tmusic...")

    deadline = time.monotonic() + timeout
    if interactive:
        while time.monotonic() < deadline:
            input()
            token = _exchange(creds, code)
            if token is not None:
                return _finish(token)
    else:
        print("Waiting for you to authorize (polling automatically)...")
        while time.monotonic() < deadline:
            token = _exchange(creds, code, quiet=True)
            if token is not None:
                return _finish(token)
            time.sleep(poll_interval)
    raise OauthLoginFailed("Timed out waiting for authorization.")


def _exchange(creds, code: dict, quiet: bool = False) -> dict | None:
    """Exchange the device code for a token.

    Google's device flow returns plain error DICTS (not exceptions) while the
    user has not authorized yet: authorization_pending / slow_down. Only a
    dict with access_token is success; denied/expired raises.
    """
    try:
        resp = dict(creds.token_from_code(code.get("device_code", "")))
    except Exception as e:
        low = str(e).lower()
        if "expired" in low or "inactive" in low:
            raise OauthLoginFailed("The login code expired before you finished.") from e
        if not quiet:
            print(f"   Not authorized yet ({e}). Press Enter to retry...")
        return None
    if "access_token" in resp:
        return resp
    err = resp.get("error", "unknown")
    if err in ("slow_down", "authorization_pending"):
        if not quiet:
            print(f"   Waiting for Google ({err})...")
        return None
    raise OauthLoginFailed(f"Google returned {err}: {resp.get('error_description', '')}")


def _finish(token: dict) -> dict:
    write_token(token)
    print(f"Logged in. Token saved to {token_path()}")
    return token


def browser_login(cfg, headers_raw: str | None = None) -> Path:
    """Store browser/cookie auth (the reliable path for library + authed calls).

    headers_raw: multi-line 'Key: value' request headers copied from a
    logged-in music.youtube.com request (must include cookie + x-goog-authuser).
    If None, prompt interactively.
    """
    from ytmusicapi.auth.browser import setup_browser

    try:
        setup_browser(filepath=str(browser_auth_path()), headers_raw=headers_raw)
    except Exception as e:
        raise AuthError(
            f"Could not parse those headers: {e}\n"
            "You must copy the headers of a real request from a LOGGED-IN\n"
            "music.youtube.com session (it needs 'cookie' and 'x-goog-authuser')."
        ) from e
    ensure_token_perms()
    print(f"Browser auth saved to {browser_auth_path()}")
    return browser_auth_path()


def ensure_token_perms() -> None:
    from .config import chmod_quiet

    chmod_quiet(browser_auth_path())


def _call(client: YTMusic, fn_name: str, *args, **kwargs):
    """Invoke a YTMusic method, translating exceptions."""
    fn = getattr(client, fn_name)
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        raise _translate(e, fn_name) from e


def search(client: YTMusic, query: str, limit: int = 15, scope: str | None = None) -> list[Track]:
    """Search songs. scope: None (public), 'library', 'uploads'.

    ytmusicapi ignores limit for the songs filter (returns ~20+), so we
    slice client-side.
    """
    items = _call(client, "search", query, filter="songs", scope=scope, limit=max(20, limit))
    tracks = [t for t in (Track.from_ytm(it, source="search") for it in items) if t]
    if not tracks and items:
        log.warning("search %r: %d raw items, 0 playable tracks", query, len(items))
    return tracks[:limit]


def get_track(client: YTMusic, video_id: str) -> Track:
    """Metadata for one track (also validates it is playable)."""
    song = _call(client, "get_song", video_id)
    status = song.get("playabilityStatus", {})
    if status.get("status") not in (None, "OK"):
        raise TrackNotPlayable(video_id, str(status.get("reason", "playback blocked")))
    t = Track.from_ytm(song, source="single")
    if t is None:
        raise TrackNotPlayable(video_id, "no playable video id returned")
    return t


def get_stream_url(client: YTMusic, track: Track) -> tuple[str, float]:
    """Resolve a fresh signed audio stream URL for the track.

    Returns (url, duration_seconds). yt-dlp is the primary resolver because it
    decrypts YouTube's signed/ciphered stream URLs (ytmusicapi's unauth get_song
    returns them ciphered). ytmusicapi.get_song is a fast fallback that also
    gives us a clean duration.
    """
    # 1) fast metadata pass via ytmusicapi (gives duration; url may be usable
    #    when present) — never fatal.
    duration = _duration_via_ytm(client, track)

    # 2) authoritative URL + duration via yt-dlp (decrypts signatures,
    #    picks bestaudio, and reports accurate duration).
    try:
        url, dur = _stream_url_ytdlp(track)
        if url:
            return url, (dur or duration or track.duration_seconds)
    except StreamUrlError:
        raise
    except Exception as e:
        log.warning("yt-dlp resolve failed for %s: %s", track.video_id, e)

    # 3) last resort: ytmusicapi get_song adaptiveFormats url (unauth, sometimes
    #    already decrypted).
    url = _url_via_ytm(client, track)
    if url:
        return url, duration
    raise StreamUrlError(f"could not resolve a stream URL for {track.video_id!r}")


def _duration_via_ytm(client: YTMusic, track: Track) -> float:
    try:
        song = _call(client, "get_song", track.video_id)
        sd = song.get("streamingData") or {}
        raw = song.get("lengthSeconds") or (
            int(sd["approxDurationMs"]) // 1000 if sd.get("approxDurationMs") else 0
        )
        return float(raw or 0.0)
    except Exception as e:
        log.debug("duration lookup failed for %s: %s", track.video_id, e)
        return track.duration_seconds


def _stream_url_ytdlp(track: Track) -> tuple[str | None, float]:
    """Best-audio URL + duration via yt-dlp (no download). Raises on hard failure."""
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": "bestaudio/best",
        "socket_timeout": 20,
    }
    url = f"https://music.youtube.com/watch?v={track.video_id}"
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    dur = float(info.get("duration") or 0.0)
    best = info.get("url")
    if best:
        return best, dur
    # fall back to a direct audio format entry
    for f in info.get("formats", []):
        if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url"):
            return f["url"], dur
    return None, dur


def _url_via_ytm(client: YTMusic, track: Track) -> str | None:
    try:
        song = _call(client, "get_song", track.video_id)
        sd = song.get("streamingData") or {}
        return _pick_audio_url(sd)
    except Exception as e:
        log.debug("ytm url fallback failed for %s: %s", track.video_id, e)
        return None


def _pick_audio_url(sd: dict) -> str | None:
    """Highest-bitrate audio-only mp4/opus stream (mpv decodes both)."""
    formats = sd.get("adaptiveFormats") or []
    audio = []
    for f in formats:
        mime = f.get("mimeType", "")
        if not mime.startswith("audio/") or not f.get("url"):
            continue
        try:
            bitrate = int(f.get("averageBitrate") or f.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        audio.append((bitrate, f["url"]))
    if not audio:
        # fall back to progressive (audio+video) — still plays fine in mpv
        for f in sd.get("formats") or []:
            if f.get("url") and ("audio" in f.get("mimeType", "")):
                return f["url"]
        return None
    audio.sort(reverse=True)
    return audio[0][1]


def account_info(client: YTMusic) -> dict:
    return _call(client, "get_account_info")


def liked_songs(client: YTMusic, limit: int = 50) -> list[Track]:
    data = _call(client, "get_liked_songs", limit=limit)
    items = data.get("tracks", [])
    return [t for t in (Track.from_ytm(it, source="liked") for it in items) if t]


def library_songs(client: YTMusic, limit: int = 50) -> list[Track]:
    data = _call(client, "get_library_songs", limit=limit)
    items = data.get("tracks", []) if isinstance(data, dict) else data
    return [t for t in (Track.from_ytm(it, source="library") for it in items) if t]


def watch_playlist(client: YTMusic, video_id: str, limit: int = 25) -> list[Track]:
    """'Up next in context' for a track — the raw material for auto-next."""
    data = _call(client, "get_watch_playlist", videoId=video_id, limit=limit)
    items = data.get("tracks", []) if isinstance(data, dict) else []
    return [t for t in (Track.from_ytm(it, source="context") for it in items) if t]


def up_next(client: YTMusic, video_id: str, limit: int = 25, exclude_current: bool = True) -> list[Track]:
    """Fetch the 'Up Next' recommended queue for a specific track from YouTube Music."""
    tracks = watch_playlist(client, video_id=video_id, limit=limit + 1)
    if exclude_current and tracks and tracks[0].video_id == video_id:
        return tracks[1 : limit + 1]
    return tracks[:limit]


def quick_picks(client: YTMusic, limit: int = 20) -> list[Track]:
    """Fetch the user's personalized 'Quick picks' (or home recommendations)."""
    home = _call(client, "get_home", limit=8)
    for section in home:
        title = section.get("title", "").lower()
        if "quick pick" in title:
            contents = section.get("contents", [])
            tracks = [t for t in (Track.from_ytm(it, source="quick_picks") for it in contents) if t]
            if tracks:
                return tracks[:limit]

    for section in home:
        title = section.get("title", "").lower()
        if any(keyword in title for keyword in ("listen again", "mixed for you", "trending songs", "for you")):
            contents = section.get("contents", [])
            tracks = [t for t in (Track.from_ytm(it, source="home") for it in contents) if t]
            if tracks:
                return tracks[:limit]

    for section in home:
        contents = section.get("contents", [])
        tracks = [t for t in (Track.from_ytm(it, source="home") for it in contents) if t]
        if tracks:
            return tracks[:limit]

    return []


def rate_song(client: YTMusic, video_id: str, rating: str = "LIKE") -> dict | None:
    """Rate a song: 'LIKE', 'INDIFFERENT' (removes rating / unlike), or 'DISLIKE'."""
    return _call(client, "rate_song", videoId=video_id, rating=rating)
