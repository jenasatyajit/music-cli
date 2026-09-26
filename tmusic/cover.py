"""Album art fetching, caching, and high-definition terminal rendering.

Supports:
- High-definition 24-bit TrueColor Braille (cell-adaptive local thresholding & dual TrueColor)
- 24-bit TrueColor Half-Block (▀) pixel art
- Atkinson-dithered Unicode Braille for curses TUI (with smart polarity detection)
- Native terminal inline image protocols (iTerm2 / Kitty) when supported
- Automatic letterbox cropping to restore square album aspect ratio
- HD Google usercontent resolution upscaling (=w800-h800)
- Disk + memory caching to prevent redundant network fetches
"""

from __future__ import annotations

import base64
import functools
import io
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Literal

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import config_dir
from .logging import get_logger

log = get_logger("cover")

# Dot weight mapping for Unicode Braille (U+2800..U+28FF)
# Grid:
# (0,0) -> 0x01   (1,0) -> 0x08
# (0,1) -> 0x02   (1,1) -> 0x10
# (0,2) -> 0x04   (1,2) -> 0x20
# (0,3) -> 0x40   (1,3) -> 0x80
BRAILLE_DOT_MAP = (
    ((0, 0), 0x01),
    ((0, 1), 0x02),
    ((0, 2), 0x04),
    ((1, 0), 0x08),
    ((1, 1), 0x10),
    ((1, 2), 0x20),
    ((0, 3), 0x40),
    ((1, 3), 0x80),
)


def cache_dir() -> Path:
    d = config_dir() / "cache" / "covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def crop_letterbox(img: Image.Image, threshold: int = 15) -> Image.Image:
    """Trim black/dark letterbox bars (common in 4:3 and 16:9 YouTube video thumbnails).

    Restores the true square aspect ratio of the underlying album cover art.
    """
    gray = img.convert("L")
    mask = gray.point(lambda p: 255 if p > threshold else 0)
    bbox = mask.getbbox()
    if bbox:
        w, h = img.size
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Only crop if the bbox preserves at least 40% of each dimension
        if bw > w * 0.4 and bh > h * 0.4:
            return img.crop(bbox)
    return img


@functools.lru_cache(maxsize=64)
def fetch_cover_bytes(url: str, video_id: str | None = None) -> bytes | None:
    """Fetch image bytes with HD upscaling, fallback resolution, and disk + memory caching."""
    if not url and not video_id:
        return None

    # Check disk cache
    if video_id:
        cached_file = cache_dir() / f"{video_id}.jpg"
        if cached_file.is_file():
            try:
                return cached_file.read_bytes()
            except OSError:
                pass

    urls_to_try: list[str] = []
    if url:
        # Upgrade Google usercontent thumbnails to 800x800 high definition
        hd_url = re.sub(r"=w\d+-h\d+.*", "=w800-h800-l90-rj", url)
        hd_url = re.sub(r"=s\d+.*", "=s800", hd_url)
        urls_to_try.append(hd_url)
        if hd_url != url:
            urls_to_try.append(url)

    if video_id:
        urls_to_try.append(f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg")
        urls_to_try.append(f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg")
        urls_to_try.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")

    for u in urls_to_try:
        try:
            resp = requests.get(u, timeout=4.0)
            if resp.status_code == 200 and resp.content:
                data = resp.content
                # Save to disk cache
                if video_id:
                    try:
                        (cache_dir() / f"{video_id}.jpg").write_bytes(data)
                    except OSError:
                        pass
                return data
        except Exception as e:
            log.debug("failed to fetch cover from %s: %s", u, e)

    return None


def detect_terminal_protocol() -> Literal["iterm2", "kitty", "ansi"]:
    """Detect if the current terminal supports native inline image display."""
    term_prog = os.environ.get("TERM_PROGRAM", "").lower()
    lc_term = os.environ.get("LC_TERMINAL", "").lower()

    if "kitty" in os.environ.get("KITTY_WINDOW_ID", "") or os.environ.get("TERM") == "xterm-kitty":
        return "kitty"
    if any(t in term_prog or t in lc_term for t in ("iterm", "wezterm", "vscode", "tabby")):
        return "iterm2"
    if "VSCODE_INJECTION" in os.environ or "VSCODE_PID" in os.environ:
        return "iterm2"

    return "ansi"


def render_iterm2(image_bytes: bytes, width: str = "44ch") -> str:
    """Render inline image using iTerm2 OSC 1337 escape sequence (VS Code, WezTerm, iTerm2)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"\033]1337;File=inline=1;width={width};preserveAspectRatio=1:{b64}\a"


def render_kitty(image_bytes: bytes) -> str:
    """Render inline image using Kitty graphics protocol (WezTerm, Kitty, Ghostty)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"\033_Gf=100,a=T,m=0;{b64}\033\\"


def enhance_image(
    img: Image.Image,
    contrast: float = 1.25,
    sharpness: float = 1.8,
) -> Image.Image:
    """Preprocess image: autocrop letterbox, unsharp mask edge enhancement, autocontrast."""
    img = crop_letterbox(img)
    # Unsharp mask emphasizes edges and fine shapes (faces, letters, instruments)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=2))
    img = ImageOps.autocontrast(img, cutoff=0.5)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if sharpness != 1.0:
        img = ImageEnhance.Sharpness(img).enhance(sharpness)
    return img


def render_colored_braille(
    image_bytes: bytes,
    width: int = 44,
    contrast: float = 1.25,
    sharpness: float = 1.8,
    dual_color: bool = True,
) -> list[str]:
    """High-definition 24-bit TrueColor Braille (2x4 micro-dots per cell).

    Uses cell-adaptive local thresholding (Chafa-style):
    - Evaluates luminance contrast within each 2x4 dot block.
    - Flat areas (sky, backdrops, solid colors) render with smooth, true-color cell fills.
    - Edges and details map to micro-dots with dedicated foreground and background TrueColors.
    - Preserves 4x the spatial resolution of Half-block without random dither noise.
    """
    try:
        img_rgb = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        log.debug("cannot decode image bytes: %s", e)
        return []

    img_rgb = enhance_image(img_rgb, contrast=contrast, sharpness=sharpness)

    img_w, img_h = img_rgb.size
    aspect = img_h / img_w

    # 2 horizontal micro-dots per cell
    pixel_w = width * 2
    # Standard terminal cells have ~2:1 height:width aspect ratio.
    # Braille cells have 4 vertical dots : 2 horizontal dots (2:1).
    # Thus, pixel_h = pixel_w * aspect yields 1:1 square geometry on screen.
    pixel_h = int(pixel_w * aspect)
    pixel_h = max(4, ((pixel_h + 3) // 4) * 4)

    img_rgb = img_rgb.resize((pixel_w, pixel_h), Image.Resampling.LANCZOS)
    rgb_pix = img_rgb.load()

    lines = []
    for y in range(0, pixel_h, 4):
        row = []
        for x in range(0, pixel_w, 2):
            pixels = []
            for (dx, dy), bit in BRAILLE_DOT_MAP:
                px = x + dx
                py = y + dy
                if px < pixel_w and py < pixel_h:
                    r, g, b = rgb_pix[px, py]
                    lum = int(0.299 * r + 0.587 * g + 0.114 * b)
                    pixels.append((lum, r, g, b, bit))

            if not pixels:
                row.append(" ")
                continue

            lums = [p[0] for p in pixels]
            min_l = min(lums)
            max_l = max(lums)
            mean_l = sum(lums) / len(lums)

            # Flat/smooth cell: minimal internal contrast
            if max_l - min_l < 14:
                avg_r = sum(p[1] for p in pixels) // len(pixels)
                avg_g = sum(p[2] for p in pixels) // len(pixels)
                avg_b = sum(p[3] for p in pixels) // len(pixels)
                if dual_color:
                    row.append(f"\033[38;2;{avg_r};{avg_g};{avg_b}m\033[48;2;{avg_r};{avg_g};{avg_b}m \033[0m")
                else:
                    if mean_l > 40:
                        row.append(f"\033[38;2;{avg_r};{avg_g};{avg_b}m⣿\033[0m")
                    else:
                        row.append(" ")
                continue

            # Detailed cell: threshold by local cell mean to extract sharp edges
            code = 0x2800
            fg_r = fg_g = fg_b = fg_n = 0
            bg_r = bg_g = bg_b = bg_n = 0

            for lum, r, g, b, bit in pixels:
                if lum >= mean_l:
                    code |= bit
                    fg_r += r
                    fg_g += g
                    fg_b += b
                    fg_n += 1
                else:
                    bg_r += r
                    bg_g += g
                    bg_b += b
                    bg_n += 1

            f_r = min(255, int((fg_r // fg_n if fg_n else 0) * 1.1))
            f_g = min(255, int((fg_g // fg_n if fg_n else 0) * 1.1))
            f_b = min(255, int((fg_b // fg_n if fg_n else 0) * 1.1))

            b_r = bg_r // bg_n if bg_n else 0
            b_g = bg_g // bg_n if bg_n else 0
            b_b = bg_b // bg_n if bg_n else 0

            if dual_color:
                if code == 0x2800:
                    row.append(f"\033[38;2;{b_r};{b_g};{b_b}m\033[48;2;{b_r};{b_g};{b_b}m \033[0m")
                elif code == 0x28FF:
                    row.append(f"\033[38;2;{f_r};{f_g};{f_b}m\033[48;2;{f_r};{f_g};{f_b}m \033[0m")
                else:
                    row.append(f"\033[38;2;{f_r};{f_g};{f_b}m\033[48;2;{b_r};{b_g};{b_b}m{chr(code)}\033[0m")
            else:
                if code == 0x2800:
                    row.append(" ")
                else:
                    row.append(f"\033[38;2;{f_r};{f_g};{f_b}m{chr(code)}\033[0m")

        lines.append("".join(row))
    return lines


def render_colored_halfblock(image_bytes: bytes, width: int = 44) -> list[str]:
    """High-definition 24-bit TrueColor Half-Block (▀) pixel art (2 square pixels per cell)."""
    try:
        img_rgb = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        log.debug("cannot decode image bytes: %s", e)
        return []

    img_rgb = enhance_image(img_rgb, contrast=1.2, sharpness=1.5)

    img_w, img_h = img_rgb.size
    aspect = img_h / img_w

    pixel_w = width
    pixel_h = max(2, int(width * aspect))
    if pixel_h % 2 != 0:
        pixel_h += 1

    img_rgb = img_rgb.resize((pixel_w, pixel_h), Image.Resampling.LANCZOS)
    rgb_pix = img_rgb.load()

    lines = []
    for y in range(0, pixel_h, 2):
        row = []
        for x in range(pixel_w):
            tr, tg, tb = rgb_pix[x, y]
            br, bg, bb = rgb_pix[x, y + 1] if (y + 1 < pixel_h) else (0, 0, 0)
            row.append(f"\033[38;2;{tr};{tg};{tb}m\033[48;2;{br};{bg};{bb}m▀\033[0m")
        lines.append("".join(row))
    return lines


def render_curses_braille(
    image_bytes: bytes,
    width: int = 20,
) -> list[str]:
    """Plain Unicode Braille lines (no ANSI escape codes) for curses TUI rendering.

    Uses Atkinson dithering + smart background polarity detection to prevent
    inverted solid-block artifacts on light album covers.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        log.debug("cannot decode image bytes: %s", e)
        return []

    img = crop_letterbox(img)

    # Sample border pixels to detect background polarity
    w, h = img.size
    border_samples: list[tuple[int, int, int]] = []
    step_x = max(1, w // 10)
    step_y = max(1, h // 10)
    for sx in range(0, w, step_x):
        border_samples.append(img.getpixel((sx, 0)))
        border_samples.append(img.getpixel((sx, h - 1)))
    for sy in range(0, h, step_y):
        border_samples.append(img.getpixel((0, sy)))
        border_samples.append(img.getpixel((w - 1, sy)))

    avg_border_lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in border_samples) / max(1, len(border_samples))

    # If the background is light, invert so foreground artwork appears as lit dots in dark terminal
    if avg_border_lum > 135:
        img = ImageOps.invert(img)

    img = ImageOps.autocontrast(img, cutoff=1.0)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=2))

    aspect = h / w
    pixel_w = width * 2
    pixel_h = int(pixel_w * aspect)
    pixel_h = max(4, ((pixel_h + 3) // 4) * 4)

    img_gray = img.convert("L").resize((pixel_w, pixel_h), Image.Resampling.LANCZOS)

    # Atkinson dithering for crisp contours
    raw = [img_gray.getpixel((px, py)) for py in range(pixel_h) for px in range(pixel_w)]
    pixels = [raw[i * pixel_w : (i + 1) * pixel_w] for i in range(pixel_h)]
    bits = [[0] * pixel_w for _ in range(pixel_h)]

    for py in range(pixel_h):
        for px in range(pixel_w):
            old = pixels[py][px]
            new = 255 if old > 115 else 0
            bits[py][px] = new
            err = (old - new) // 8
            if err != 0:
                for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                    nx, ny = px + dx, py + dy
                    if 0 <= nx < pixel_w and 0 <= ny < pixel_h:
                        pixels[ny][nx] = max(0, min(255, pixels[ny][nx] + err))

    lines = []
    for y in range(0, pixel_h, 4):
        row = []
        for x in range(0, pixel_w, 2):
            char_code = 0x2800
            for (dx, dy), bit in BRAILLE_DOT_MAP:
                px = x + dx
                py = y + dy
                if px < pixel_w and py < pixel_h and bits[py][px] > 0:
                    char_code |= bit
            row.append(chr(char_code))
        lines.append("".join(row))
    return lines


def display_cover_terminal(
    image_bytes: bytes,
    width: int = 44,
    prefer_native: bool = True,
    style: str = "braille",
) -> None:
    """Print the cover art directly to stdout using the best available method."""
    if prefer_native or style == "native":
        protocol = detect_terminal_protocol()
        if protocol == "iterm2":
            sys.stdout.write(render_iterm2(image_bytes, width=f"{width}ch") + "\n")
            sys.stdout.flush()
            return
        if protocol == "kitty":
            sys.stdout.write(render_kitty(image_bytes) + "\n")
            sys.stdout.flush()
            return

    if style == "halfblock":
        lines = render_colored_halfblock(image_bytes, width=width)
    else:
        lines = render_colored_braille(image_bytes, width=width, dual_color=True)

    for line in lines:
        print(line)
