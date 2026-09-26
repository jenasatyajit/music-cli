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


@dataclass
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
        artists = f" — {self.artists.strip()}" if self.artists and self.artists.strip() else ""
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

        # Parse artists across ytmusicapi search/watch/home formats
        artists_list: list[str] = []
        raw_artists = item.get("artists")
        if isinstance(raw_artists, list):
            for a in raw_artists:
                if isinstance(a, dict):
                    name = a.get("name") or a.get("text") or ""
                    if name and isinstance(name, str) and name.strip():
                        artists_list.append(name.strip())
                elif isinstance(a, str) and a.strip():
                    artists_list.append(a.strip())
        elif isinstance(raw_artists, str) and raw_artists.strip():
            artists_list.append(raw_artists.strip())

        # Fallback to author / uploader
        if not artists_list:
            author = item.get("author") or item.get("uploader")
            if isinstance(author, dict):
                aname = author.get("name") or author.get("text") or ""
                if aname and isinstance(aname, str) and aname.strip():
                    artists_list.append(aname.strip())
            elif isinstance(author, str) and author.strip():
                artists_list.append(author.strip())

        # Fallback to subtitles (often contains artist in home / search shelves)
        if not artists_list:
            subtitles = item.get("subtitles")
            if isinstance(subtitles, list):
                for s in subtitles:
                    if isinstance(s, dict):
                        sname = s.get("name") or s.get("text") or ""
                    elif isinstance(s, str):
                        sname = s
                    else:
                        sname = ""
                    sname = sname.strip()
                    if (
                        sname
                        and sname.lower() not in ("song", "video", "single", "album", "ep")
                        and not any(sname.lower().endswith(u) for u in ("views", "plays"))
                    ):
                        artists_list.append(sname)
            elif isinstance(subtitles, str) and subtitles.strip():
                sname = subtitles.strip()
                if sname.lower() not in ("song", "video", "single", "album", "ep"):
                    artists_list.append(sname)

        artists = ", ".join(artists_list)

        # Parse album
        album = ""
        raw_album = item.get("album")
        if isinstance(raw_album, dict):
            album = raw_album.get("name") or raw_album.get("text") or ""
        elif isinstance(raw_album, str):
            album = raw_album
        album = album.strip() if isinstance(album, str) else ""

        duration = item.get("duration_seconds") or item.get("lengthSeconds")
        if not duration and (item.get("length") or item.get("duration")):
            raw_len = str(item.get("length") or item.get("duration") or "").strip()
            if ":" in raw_len:
                try:
                    parts = [int(p) for p in raw_len.split(":")]
                    if len(parts) == 2:
                        duration = parts[0] * 60 + parts[1]
                    elif len(parts) == 3:
                        duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
                except ValueError:
                    duration = 0.0
            else:
                try:
                    duration = int(raw_len)
                except ValueError:
                    duration = 0.0
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

        title = item.get("title", "?")
        if isinstance(title, dict):
            title = title.get("text") or title.get("name") or "?"
        title = str(title).strip() or "?"

        return cls(
            video_id=video_id,
            title=title,
            artists=artists,
            album=album,
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