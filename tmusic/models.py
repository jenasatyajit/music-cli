"""Domain models: Track, SearchResults, PlaybackState.

Keep these dumb dataclasses — no I/O, no ytmusicapi imports — so the TUI and
tests can build them by hand.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


def fmt_time(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass(frozen=True)
class Track:
    """A playable item. video_id is the universal YTM/YouTube identifier."""

    video_id: str
    title: str
    artists: str = ""
    album: str = ""
    duration_seconds: float = 0.0
    thumbnail_url: str = ""
    is_live: bool = False
    # context for "next in context" (watch playlist) — set by the service
    source: str = ""  # "search", "liked", "playlist:<id>", "radio"

    @property
    def label(self) -> str:
        artists = f" — {self.artists}" if self.artists else ""
        return f"{self.title}{artists}"

    @classmethod
    def from_ytm(cls, item: dict, source: str = "") -> "Track | None":
        """Normalize a ytmusicapi search/playlist/watch item; None if unplayable."""
        video_id = item.get("videoId")
        if not video_id:
            return None
        # 1.12 returns the full enum, e.g. "MUSIC_VIDEO_TYPE_OMV". Accept the
        # full and short forms; reject podcasts/episodes/albums/artists/mixes.
        vt = item.get("videoType") or ""
        vt = vt.replace("MUSIC_VIDEO_TYPE_", "")
        if vt not in ("", "OMV", "ATV", "UGC", "OFFICIAL_SOURCE_MUSIC"):
            return None
        if item.get("isLive"):
            return None  # v1: no live streams
        artists = ", ".join(
            a.get("text", "") for a in (item.get("artists") or []) if isinstance(a, dict)
        )
        duration = item.get("duration_seconds") or item.get("lengthSeconds")
        try:
            duration = int(duration) if duration else 0.0
        except (TypeError, ValueError):
            duration = 0.0
        thumb = ""
        if isinstance(item.get("thumbnails"), list) and item["thumbnails"]:
            thumb = str(item["thumbnails"][-1].get("url", ""))
        elif isinstance(item.get("thumbnail"), dict):
            thumb = str(item["thumbnail"].get("url", ""))
        if not thumb and video_id:
            thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        return cls(
            video_id=video_id,
            title=item.get("title", "?"),
            artists=artists,
            album=item.get("album", {}).get("text", "") if isinstance(item.get("album"), dict) else "",
            duration_seconds=float(duration or 0.0),
            thumbnail_url=thumb,
            source=source,
        )


class State(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    LOADING = "loading"
    ERROR = "error"


@dataclass
class Playback:
    """Current playback snapshot (read by the UI every frame)."""

    state: State = State.IDLE
    queue: list[Track] = field(default_factory=list)
    index: int = -1
    position: float = 0.0  # seconds
    status_message: str = ""

    @property
    def current(self) -> Track | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    @property
    def next_track(self) -> Track | None:
        if 0 <= self.index + 1 < len(self.queue):
            return self.queue[self.index + 1]
        return None