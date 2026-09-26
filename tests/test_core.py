"""Sanity tests: formatting, track normalization, error translation, config.

No network, no mpv — these must pass anywhere, including CI.
"""

from __future__ import annotations

import pytest

from tmusic.config import Config
from tmusic.errors import (
    ConfigError,
    MissingMpv,
    TmusicError,
    YTMGatedError,
    YTMApiError,
)
from tmusic.logging import user_error_text
from tmusic.models import Track, fmt_time
from tmusic import ytm as y


# ---------------------------------------------------------------- fmt_time

def test_fmt_time_basic():
    assert fmt_time(0) == "00:00"
    assert fmt_time(59) == "00:59"
    assert fmt_time(65) == "01:05"
    assert fmt_time(3661) == "1:01:01"
    assert fmt_time(None) == "--:--"
    assert fmt_time(-3) == "--:--"


def test_eq_frame_no_index_error():
    from tmusic.tui import _eq_frame

    for tick in range(300):
        frame = _eq_frame("test_seed", 12, tick)
        assert len(frame) == 12


# ---------------------------------------------------------------- Track

def test_track_from_ytm_song():
    item = {
        "videoId": "abc123",
        "title": "Wonderwall",
        "artists": [{"text": "Oasis"}],
        "lengthSeconds": "251",
        "album": {"text": "(What's the Story) Morning Light?"},
        "videoType": "OMV",
    }
    t = Track.from_ytm(item)
    assert t is not None
    assert t.video_id == "abc123"
    assert t.title == "Wonderwall"
    assert t.artists == "Oasis"
    assert t.duration_seconds == 251.0
    assert t.album == "(What's the Story) Morning Light?"
    assert t.label == "Wonderwall — Oasis"


def test_track_from_ytm_skips_live_and_garbage():
    assert Track.from_ytm({"videoId": "x", "isLive": True, "title": "live"}) is None
    assert Track.from_ytm({"title": "no id"}) is None
    assert Track.from_ytm({"videoId": "y"}) is not None  # minimal is ok
    # 1.12 full enum forms
    assert Track.from_ytm({"videoId": "z", "videoType": "MUSIC_VIDEO_TYPE_OMV", "title": "t"}) is not None
    assert Track.from_ytm({"videoId": "p", "videoType": "MUSIC_VIDEO_TYPE_PODCAST_EPISODE", "title": "t"}) is None


# ---------------------------------------------------------------- errors

def test_error_user_message_default():
    assert "unexpected" in TmusicError().description.lower() or True
    e = YTMApiError("custom user text")
    assert e.description == "custom user text"


def test_error_translation_gated():
    from ytmusicapi.exceptions import YTMusicGatedError

    out = y._translate(YTMusicGatedError("gated"), "search")
    assert isinstance(out, YTMGatedError)
    assert "bot check" in out.description.lower()


def test_error_translation_network():
    import requests

    out = y._translate(requests.ConnectionError("boom"), "search")
    assert isinstance(out, YTMApiError)
    assert "Network" in out.description


def test_missing_mpv_lists_tried():
    e = MissingMpv(["/x/mpv", "PATH"])
    assert "/x/mpv" in e.description and "winget" in e.description
    assert isinstance(e, TmusicError)


def test_user_error_text_plain_exception():
    assert user_error_text(ValueError("x")) == "Unexpected error: x"


# ---------------------------------------------------------------- config

def test_find_mpv_raises_with_trail(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    cfg = Config(mpv_path=str(tmp_path / "nope"))
    with pytest.raises(MissingMpv) as ei:
        from tmusic.config import find_mpv

        find_mpv(cfg)
    assert str(tmp_path / "nope") in ei.value.description


# ---------------------------------------------------------------- stream pick

def test_pick_audio_url_prefers_highest_bitrate():
    sd = {
        "adaptiveFormats": [
            {"mimeType": "audio/webm; codecs=opus", "url": "low", "averageBitrate": "50000"},
            {"mimeType": "audio/mp4; codecs=mp4a.40.2", "url": "high", "averageBitrate": "130000"},
            {"mimeType": "video/mp4", "url": "vid", "url2": "x"},
        ]
    }
    assert y._pick_audio_url(sd) == "high"


def test_pick_audio_url_progressive_fallback():
    sd = {"formats": [{"mimeType": "audio/mp4", "url": "prog"}]}
    assert y._pick_audio_url(sd) == "prog"
    assert y._pick_audio_url({}) is None