# tmusic

Terminal YouTube Music player — full-screen curses TUI, mpv audio backend.

Dev host: `zemon` (Linux). Use target: Windows laptop (PowerShell or cmd).

## Setup (Windows)

```
winget install mpv
winget install python   # 3.12+
```

Copy this folder to the laptop, then:

```
uv sync
tmusic doctor            # verify environment
tmusic auth cookies      # log in (library + liked songs) — recommended
tmusic run               # full-screen TUI
tmusic search "oasis"    # (headless alternative)
tmusic play "wonderwall" # (headless alternative)
```

## Windows terminal

Windows Terminal is the recommended shell (best key handling). Plain cmd.exe
works but resize handling may be flaky.

## Logging in (browser cookies)

1. Log in to music.youtube.com in your browser (the account you want).
2. Open DevTools (F12) → **Network** tab.
3. Press play on any song (generates traffic).
4. Find a request whose URL starts with `music.youtube.com/youtubei/v1/`
   (e.g. `browse`, `next`, `player`).
5. Right-click it → **Copy** → **Copy request headers**.
6. Run `tmusic auth cookies` in tmusic, paste, then finish the paste with
   **Ctrl-D** (Linux/macOS) or **Enter, Ctrl-Z, Enter** (Windows).

Stored at `%LOCALAPPDATA%\tmusic\browser.json` (0600).
Cookies last a few weeks; re-run `tmusic auth cookies` when `tmusic doctor`
says the account check fails.

## Commands

| command | what |
|---|---|
| `tmusic doctor` | environment check (config, auth, mpv, yt-dlp) |
| `tmusic auth cookies` | browser-cookie login (recommended) |
| `tmusic auth status` | show logged-in account / auth mode |
| `tmusic search <q> [--limit N] [--library]` | search (public or your library) |
| `tmusic play <q> [--index N]` | play the Nth result |
| `tmusic run` | full-screen TUI (M2) |

Add `-v` / `-vv` for more logging. Details always in the log file.

## Config

`%LOCALAPPDATA%\tmusic\config.toml` (created on first run):

```toml
[mpv]
# path to mpv.exe if it is not on PATH
path = ""

[player]
seek_seconds = 10
long_seek_seconds = 30
search_limit = 15
```

Logs: `%LOCALAPPDATA%\tmusic\tmusic.log` (rotating, 5×1MB).

## How it works (short)

- ytmusicapi (unauthenticated or cookie-auth) → search + metadata + durations
- yt-dlp → resolves the signed, decrypted audio stream URL (itag 251)
- mpv (JSON IPC) → decodes/plays, seek/pause/stop, end-of-track events

Pure-OAuth login (`tmusic auth login`) is kept as a legacy path but is not the
default: YouTube's InnerTube API rejects Bearer tokens on the web client
since 2025-08-29 (ytmusicapi issue #813).