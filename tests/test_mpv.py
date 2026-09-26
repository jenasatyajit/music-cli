"""Tests for MPVBackend lifecycle, IPC communication, and command execution."""

from __future__ import annotations

import shutil
import pytest

from tmusic.config import Config, find_mpv
from tmusic.errors import MissingMpv
from tmusic.mpv_backend import MPVBackend


@pytest.fixture
def mpv_available():
    try:
        find_mpv(Config())
        return True
    except MissingMpv:
        return False


def test_mpv_backend_lifecycle(mpv_available):
    if not mpv_available:
        pytest.skip("mpv is not installed on this machine")

    cfg = Config()
    backend = MPVBackend(cfg)
    try:
        backend._start()
        assert backend.is_running()

        # Query IPC property
        prop = backend._get_property("idle-active")
        assert prop is not None
        assert prop.get("error") == "success"
        assert prop.get("data") is True

        # Test pause/resume control commands
        backend.resume()
        assert not backend._paused
        backend.pause()
        assert backend._paused

    finally:
        backend.shutdown()
        assert not backend.is_running()


def test_mpv_job_object_and_active_registration(mpv_available):
    if not mpv_available:
        pytest.skip("mpv is not installed on this machine")

    import sys
    from tmusic.mpv_backend import _ACTIVE_BACKENDS, _get_process_job

    cfg = Config()
    backend = MPVBackend(cfg)
    assert backend in _ACTIVE_BACKENDS

    if sys.platform == "win32":
        job = _get_process_job()
        assert job is not None

    try:
        backend._start()
        assert backend.is_running()
    finally:
        backend.shutdown()
        assert not backend.is_running()
