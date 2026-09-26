"""tmusic exception hierarchy.

Rule: every user-visible failure is a TmusicError carrying a `user_message`
that a human can act on. Internal detail (traceback, upstream payload) goes
to the log file via log.exception — it never replaces the user message.
"""

from __future__ import annotations

from typing import Any


class TmusicError(Exception):
    """Base for all expected, user-explainable failures."""

    #: Shown to the user in place of the raw exception text.
    user_message = "tmusic hit an unexpected error. Check ~/.config/tmusic/tmusic.log for details."

    def __init__(self, user_message: str | None = None, *details: Any) -> None:
        super().__init__(*details if details else (user_message or self.__class__.__name__,))
        if user_message is not None:
            self.user_message = user_message

    @property
    def description(self) -> str:
        return self.user_message


# --- config / environment ------------------------------------------------

class ConfigError(TmusicError):
    user_message = "Configuration problem. Check ~/.config/tmusic/config.toml (run `tmusic doctor` to verify setup)."


class MissingOauthClient(ConfigError):
    user_message = (
        "YouTube OAuth client credentials are not configured.\n"
        "  1. Create an OAuth client (type: TVs and Limited Input devices) in the Google Cloud Console\n"
        "  2. Put client_id / client_secret under [oauth] in ~/.config/tmusic/config.toml\n"
        "  3. Run `tmusic auth login`"
    )


class MissingMpv(ConfigError):
    def __init__(self, tried: list[str]) -> None:
        super().__init__(
            "mpv was not found. Install it with:\n"
            "    winget install mpv\n"
            "or set [mpv] path in ~/.config/tmusic/config.toml.\n"
            f"Searched: {', '.join(tried)}"
        )


# --- YouTube Music --------------------------------------------------------

class AuthError(TmusicError):
    user_message = "Not logged in to YouTube Music. Run `tmusic auth login`."


class OauthLoginFailed(TmusicError):
    user_message = "YouTube login did not complete in time. Run `tmusic auth login` again."


class YTMApiError(TmusicError):
    """Upstream ytmusicapi failure, translated to human language."""

    user_message = "YouTube Music request failed. Check your connection and try again."


class YTMGatedError(YTMApiError):
    user_message = (
        "YouTube flagged this request (bot check / region / age-gate).\n"
        "Try again in a minute; if it persists, re-run `tmusic auth login`."
    )


class TrackNotPlayable(YTMApiError):
    def __init__(self, video_id: str, reason: str) -> None:
        super().__init__(
            f"Track {video_id!r} is not playable: {reason or 'unknown reason'}. "
            "It may be blocked in your country or age-restricted."
        )


class StreamUrlError(YTMApiError):
    user_message = "Could not resolve a stream URL for this track. Try the next track."


# --- playback --------------------------------------------------------------

class MPVProcessError(TmusicError):
    user_message = "The audio engine (mpv) crashed or stopped unexpectedly. The player restarted it — if this repeats, try `winget upgrade mpv`."


class PlaybackError(TmusicError):
    user_message = "Playback failed. Check ~/.config/tmusic/tmusic.log for details."


class MPVTimeoutError(PlaybackError):
    user_message = "mpv did not respond. The player restarted the audio engine."