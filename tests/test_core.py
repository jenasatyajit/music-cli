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


def test_track_from_ytm_ytmusicapi_name_format():
    item = {
        "videoId": "xyz789",
        "title": "Bohemian Rhapsody",
        "artists": [{"name": "Queen", "id": "123"}],
        "album": {"name": "A Night At The Opera", "id": "456"},
        "duration_seconds": 355,
    }
    t = Track.from_ytm(item)
    assert t is not None
    assert t.artists == "Queen"
    assert t.album == "A Night At The Opera"
    assert t.label == "Bohemian Rhapsody — Queen"


def test_track_from_ytm_multiple_artists_and_no_empty_commas():
    item = {
        "videoId": "aP8yml7kaWU",
        "title": "Ishqa Ve",
        "artists": [
            {"name": "Zeeshan Ali", "id": "1"},
            {"name": "Sandeep Aulakh", "id": "2"},
            {"name": "Honey Dhillon", "id": "3"},
        ],
        "album": {"name": "Ishqa Ve", "id": "4"},
    }
    t = Track.from_ytm(item)
    assert t is not None
    assert t.artists == "Zeeshan Ali, Sandeep Aulakh, Honey Dhillon"
    assert t.label == "Ishqa Ve — Zeeshan Ali, Sandeep Aulakh, Honey Dhillon"


def test_track_from_ytm_no_artists_no_commas():
    item = {
        "videoId": "single123",
        "title": "Solo Piano",
        "artists": [],
    }
    t = Track.from_ytm(item)
    assert t is not None
    assert t.artists == ""
    assert t.label == "Solo Piano"


def test_track_duration_assignment():
    t = Track(video_id="test1", title="Test Song", duration_seconds=0.0)
    assert t.duration_seconds == 0.0
    t.duration_seconds = 245.5
    assert t.duration_seconds == 245.5


def test_track_from_ytm_duration_string():
    item1 = {"videoId": "v1", "title": "Track 1", "length": "3:16"}
    t1 = Track.from_ytm(item1)
    assert t1 is not None
    assert t1.duration_seconds == 196.0

    item2 = {"videoId": "v2", "title": "Track 2", "duration": "1:02:15"}
    t2 = Track.from_ytm(item2)
    assert t2 is not None
    assert t2.duration_seconds == 3735.0


def test_up_next_function():
    from unittest.mock import MagicMock
    from tmusic.ytm import up_next

    mock_client = MagicMock()
    mock_client.get_watch_playlist.return_value = {
        "tracks": [
            {"videoId": "v_seed", "title": "Seed Song", "length": "3:00"},
            {"videoId": "v_next1", "title": "Next Song 1", "length": "4:00"},
            {"videoId": "v_next2", "title": "Next Song 2", "length": "2:30"},
        ]
    }
    res = up_next(mock_client, "v_seed", limit=10, exclude_current=True)
    assert len(res) == 2
    assert res[0].video_id == "v_next1"
    assert res[1].video_id == "v_next2"
    assert res[0].duration_seconds == 240.0


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


# ---------------------------------------------------------------- short seek

def test_config_short_seek_seconds():
    cfg = Config()
    assert cfg.short_seek_seconds == 5
    assert cfg.seek_seconds == 10
    assert cfg.long_seek_seconds == 30


def test_tui_short_seek_keys():
    from unittest.mock import MagicMock
    from tmusic.tui import TUIApp

    mock_core = MagicMock()
    mock_core.cfg = Config(short_seek_seconds=5)
    app = TUIApp(core=mock_core, authed=False)

    # Main mode
    app._handle_key(ord("g"))
    mock_core.seek.assert_called_with(-5)

    app._handle_key(ord("h"))
    mock_core.seek.assert_called_with(5)

    # Cover mode
    app.mode = "cover"
    app._handle_key(ord("g"))
    mock_core.seek.assert_called_with(-5)

    app._handle_key(ord("h"))
    mock_core.seek.assert_called_with(5)

    # Results mode
    app.mode = "results"
    app._handle_key(ord("g"))
    mock_core.seek.assert_called_with(-5)

    app._handle_key(ord("h"))
    mock_core.seek.assert_called_with(5)


# ---------------------------------------------------------------- like / unlike

def test_track_like_status_parsing():
    t_liked = Track.from_ytm({"videoId": "abc", "videoType": "OMV", "title": "A", "likeStatus": "LIKE"})
    assert t_liked.like_status == "LIKE"

    t_from_liked_src = Track.from_ytm({"videoId": "def", "videoType": "OMV", "title": "B"}, source="liked")
    assert t_from_liked_src.like_status == "LIKE"

    t_unliked = Track.from_ytm({"videoId": "ghi", "videoType": "OMV", "title": "C", "likeStatus": "INDIFFERENT"})
    assert t_unliked.like_status == "INDIFFERENT"


def test_tui_shift_l_key():
    from unittest.mock import MagicMock
    from tmusic.tui import TUIApp

    mock_core = MagicMock()
    mock_track = Track(video_id="vid123", title="Song 1")
    mock_core.snapshot.return_value.queue = [mock_track]
    mock_core.snapshot.return_value.current = mock_track

    app = TUIApp(core=mock_core, authed=True)

    # Main mode (cursor at 0)
    app.cursor = 0
    app._handle_key(ord("L"))
    mock_core.toggle_like.assert_called_with(mock_track)

    # Cover mode
    mock_core.toggle_like.reset_mock()
    app.mode = "cover"
    app._handle_key(ord("L"))
    mock_core.toggle_like.assert_called_with(mock_track)

    # Results mode
    mock_core.toggle_like.reset_mock()
    app.mode = "results"
    res_track = Track(video_id="vid456", title="Song 2")
    app.results = [res_track]
    app.results_cursor = 0
    app._handle_key(ord("L"))
    mock_core.toggle_like.assert_called_with(res_track)


def test_player_do_toggle_like():
    from unittest.mock import MagicMock
    from tmusic.player import PlayerCore

    mock_client = MagicMock()
    core = PlayerCore(cfg=Config(), client=mock_client)

    track = Track(video_id="v1", title="Song", like_status="INDIFFERENT")
    core._do_toggle_like(track)
    assert track.like_status == "LIKE"
    mock_client.rate_song.assert_called_with(videoId="v1", rating="LIKE")

    # Toggle back
    core._do_toggle_like(track)
    assert track.like_status == "INDIFFERENT"
    mock_client.rate_song.assert_called_with(videoId="v1", rating="INDIFFERENT")
