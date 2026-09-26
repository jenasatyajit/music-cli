"""Configuration: config dir resolution, config.toml load/save, token file location.

Layout (POSIX and Windows via the platformdirs conventions):
    ~/.config/tmusic/config.toml      user settings
    ~/.config/tmusic/oauth.json       YouTube OAuth token (chmod 0600 when possible)
    ~/.config/tmusic/tmusic.log       rotating log
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, MissingMpv

APP_NAME = "tmusic"


def config_dir() -> Path:
    override = os.environ.get("TMUSIC_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / APP_NAME
    return Path.home() / ".config" / APP_NAME


def token_path() -> Path:
    return config_dir() / "oauth.json"


def browser_auth_path() -> Path:
    return config_dir() / "browser.json"


@dataclass
class Config:
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    mpv_path: str = ""  # empty = locate on PATH
    seek_seconds: int = 10
    long_seek_seconds: int = 30
    search_limit: int = 15
    extra: dict = field(default_factory=dict)


def load_config() -> Config:
    path = config_dir() / "config.toml"
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml is not valid TOML: {e}") from e
    oauth = raw.get("oauth", {})
    mpv = raw.get("mpv", {})
    player = raw.get("player", {})
    return Config(
        oauth_client_id=str(oauth.get("client_id", "")),
        oauth_client_secret=str(oauth.get("client_secret", "")),
        mpv_path=str(mpv.get("path", "")),
        seek_seconds=int(player.get("seek_seconds", 10)),
        long_seek_seconds=int(player.get("long_seek_seconds", 30)),
        search_limit=int(player.get("search_limit", 15)),
        extra=raw,
    )


def ensure_config_dir() -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    chmod_quiet(d, stat.S_IRWXU)  # 0700: a directory must be traversable


def write_config_sample() -> Path:
    """Create config.toml with comments if absent; never overwrite existing."""
    ensure_config_dir()
    path = config_dir() / "config.toml"
    if path.exists():
        return path
    path.write_text(
        "\n".join(
            [
                "# tmusic configuration",
                "",
                "[oauth]",
                "# Google Cloud OAuth client (type: 'TVs and Limited Input devices').",
                "# Required for `tmusic auth login`.",
                'client_id = ""',
                'client_secret = ""',
                "",
                "[mpv]",
                "# Absolute path to mpv (or mpv.exe) if it is not on PATH.",
                'path = ""',
                "",
                "[player]",
                "seek_seconds = 10",
                "long_seek_seconds = 30",
                "search_limit = 15",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def chmod_quiet(path: Path, mode: int | None = None) -> None:
    try:
        if mode is None:
            mode = stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR  # 0700 dir / 0600 file
        path.chmod(mode)
    except (OSError, NotImplementedError):
        pass


def write_token(token_dict: dict) -> Path:
    """Persist the OAuth token (ytmusicapi RefreshableTokenDict) as oauth.json."""
    import json

    ensure_config_dir()
    path = token_path()
    path.write_text(json.dumps(token_dict, indent=2), encoding="utf-8")
    chmod_quiet(path)
    return path


def read_token() -> dict | None:
    import json

    path = token_path()
    if not path.exists():
        return None
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ConfigError(f"oauth.json is unreadable ({e}). Re-run `tmusic auth login`.") from e
    if not isinstance(token, dict) or "access_token" not in token:
        # a failed device exchange once wrote a Google error dict here
        return None
    return token


def find_mpv(cfg: Config) -> str:
    """Resolve the mpv executable path; raises MissingMpv with a full trail.

    Windows: prefer an actual .exe — the shinchiro winget package also drops
    an extensionless-style mpv.COM launcher, which we don't want to depend on.
    """
    import shutil

    tried: list[str] = []
    if cfg.mpv_path:
        tried.append(cfg.mpv_path)
        if Path(cfg.mpv_path).is_file():
            return cfg.mpv_path
        raise MissingMpv(tried)

    if sys.platform == "win32":
        # explicit .exe first (PATH and common install locations)
        exe = shutil.which("mpv.exe")
        if exe:
            return exe
        tried.append("PATH (mpv.exe)")
        for cand in (
            r"C:\Program Files\MPV Player\mpv.exe",
            r"C:\Program Files (x86)\MPV Player\mpv.exe",
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mpv" / "mpv.exe"),
        ):
            tried.append(cand)
            if Path(cand).is_file():
                return cand
        # last resort: whatever `mpv` resolves to (may be .com / launcher)
        found = shutil.which("mpv")
        if found:
            return found
    else:
        tried.append("PATH")
        found = shutil.which("mpv")
        if found:
            return found
    raise MissingMpv(tried)