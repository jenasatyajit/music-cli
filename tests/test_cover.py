"""Tests for cover art rendering, dithering, and formatting."""

from __future__ import annotations

import io
from PIL import Image

from tmusic.cover import (
    render_colored_braille,
    render_colored_halfblock,
    render_curses_braille,
    detect_terminal_protocol,
)


def _make_dummy_image_bytes(w: int = 64, h: int = 64) -> bytes:
    img = Image.new("RGB", (w, h), color=(128, 64, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_render_curses_braille():
    data = _make_dummy_image_bytes()
    for w in (16, 26, 44):
        lines = render_curses_braille(data, width=w)
        assert len(lines) > 0
        for line in lines:
            assert len(line) == w
            for ch in line:
                assert 0x2800 <= ord(ch) <= 0x28FF


def test_render_colored_braille():
    data = _make_dummy_image_bytes()
    lines = render_colored_braille(data, width=16, dual_color=True)
    assert len(lines) > 0
    # ANSI escape sequences must be present
    assert "\033[38;2;" in lines[0]


def test_render_colored_halfblock():
    data = _make_dummy_image_bytes()
    lines = render_colored_halfblock(data, width=16)
    assert len(lines) > 0
    assert "▀" in lines[0]
    assert "\033[38;2;" in lines[0]


def test_detect_terminal_protocol():
    proto = detect_terminal_protocol()
    assert proto in ("iterm2", "kitty", "ansi")
