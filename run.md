# Running and Building `tmusic` on Windows

This guide documents how **tmusic** is built, packaged, and exposed as a system-wide command accessible from any directory in Windows.

---

## 1. Prerequisites Check

Before building or running `tmusic`, ensure the following are installed:

- **Python 3.10+**: Python 3.13 (`py` or `python.exe`)
- **MPV Player**: `mpv.exe` (installed at `C:\Program Files\MPV Player\mpv.exe` and on `PATH`)
- **Package Manager / Runner**: `uv` (`C:\Users\jenas\.local\bin\uv.exe`) or `pip` (`py -m pip`)

---

## 2. Building the Application

`tmusic` uses [pyproject.toml](file:///s:/tmp/music-cli/pyproject.toml) configured with `hatchling` as its build backend.

### Option 1: Fast Build with `uv` (Recommended)
Open PowerShell in the project directory (`s:\tmp\music-cli`) and run:
```powershell
uv build
```
*(Or call directly: `& "$HOME\.local\bin\uv.exe" build`)*

This produces:
- `dist/tmusic-0.1.0-py3-none-any.whl` (installable wheel)
- `dist/tmusic-0.1.0.tar.gz` (source distribution)

### Option 2: Build with Standard Python `build`
```powershell
py -m pip install --upgrade build; py -m build
```

---

## 3. Creating the Global Command (`tmusic`)

To invoke `tmusic` anywhere in Windows (Command Prompt, PowerShell, Windows Terminal, Warp, Cursor, etc.), the following methods are supported:

### Method A: Global Isolated CLI with `uv tool` (Active Setup)
Run this command from `s:\tmp\music-cli`:
```powershell
uv tool install --editable . --force
```
- Installs `tmusic.exe` directly into `C:\Users\jenas\.local\bin`.
- The `--editable` flag ensures any edits or updates made in `s:\tmp\music-cli` take effect immediately without having to rebuild and reinstall every time.

### Method B: System-Wide Wrapper Script (Active Setup)
A global launcher script is installed at:
`E:\app-file\tmusic.cmd`

Because `E:\app-file\` is in the system-wide Machine `PATH`, typing `tmusic` resolves instantly across every shell and terminal on Windows, even before restarting terminals or refreshing environment variables.

Contents of `E:\app-file\tmusic.cmd`:
```cmd
@echo off
if exist "%USERPROFILE%\.local\bin\tmusic.exe" (
    "%USERPROFILE%\.local\bin\tmusic.exe" %*
) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\Scripts\tmusic.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python313\Scripts\tmusic.exe" %*
) else (
    py -m tmusic.cli %*
)
```

### Method C: Installing via Python `pip`
To install the built wheel into your default Python environment:
```powershell
py -m pip install dist\tmusic-0.1.0-py3-none-any.whl --force-reinstall
```
This generates `tmusic.exe` in `C:\Users\jenas\AppData\Local\Programs\Python\Python313\Scripts\`.

---

## 4. Verifying Global Access

From any directory or drive (for example `C:\`):

```powershell
# Check binary discovery
where.exe tmusic

# Check version
tmusic --version

# Run full diagnostics
tmusic doctor
```

Expected output for `tmusic doctor`:
```text
tmusic 0.1.0 — environment check

[config] directory: C:\Users\jenas\AppData\Local\tmusic
[config] config.toml: C:\Users\jenas\AppData\Local\tmusic\config.toml (present)
[auth]   browser.json:  C:\Users\jenas\AppData\Local\tmusic\browser.json (present — logged in)
[mpv]    mpv:           C:\Program Files\MPV Player\mpv.exe
[ytdlp]  yt-dlp:        2026.08.19
[ytm]    account:       Satyajit Jena

log file: C:\Users\jenas\AppData\Local\tmusic\tmusic.log

all good
```

---

## 5. Daily Usage & Commands

| Command | Description |
|---|---|
| `tmusic run` | Launch full-screen curses TUI player |
| `tmusic search "<query>"` | Search YouTube Music songs, albums, and artists |
| `tmusic play "<query>"` | Immediately stream and play the best match |
| `tmusic picks` | Browse or play personalized Quick Picks |
| `tmusic upnext` (or `next`) | Show or play radio recommendations for the current track |
| `tmusic cover` | Display HD cover art rendered directly in the terminal |
| `tmusic auth cookies` | Authenticate with YouTube Music via browser headers |
| `tmusic auth status` | Check current authenticated account status |
| `tmusic doctor` | Verify dependencies, MPV, config, and credentials |

### TUI Player Keybindings (`tmusic run`)

| Key | Action |
|---|---|
| `Space` / `Enter` | Toggle Play / Pause |
| `g` / `h` | **Short seek 5s** (`g` backward 5s, `h` forward 5s) |
| `←` / `→` | **Standard seek 10s** (`←` backward 10s, `→` forward 10s) |
| `Shift+←` / `Shift+→` (or `PgUp` / `PgDn`) | **Long seek 30s** (`Shift+←` backward 30s, `Shift+→` forward 30s) |
| `Mouse click` | Click progress bar to jump directly to timestamp |
| `n` / `p` | Next track / Previous track (restarts track if >3s in) |
| `x` | Stop playback |
| `j` / `k` (or `↓` / `↑`) | Move cursor down / up |
| `d` | Delete selected track from queue |
| `/` | Search public catalogue |
| `L` (`Shift+L`) | **Like / Unlike song** (toggles thumbs up on current or selected track) |
| `u` | Load Up Next / radio recommendations |
| `r` | Load Quick Picks |
| `l` | Load Library |
| `m` | Load Liked songs |
| `c` | High-definition terminal cover art view |
| `?` | Open interactive help |
| `q` / `Esc` | Quit application |

---

## 6. Updating Code

When you make changes to the source code:
- If installed via `uv tool install --editable .`, your changes are already active immediately.
- To rebuild distributions after version bump:
  ```powershell
  uv build
  ```
- To update the installed package:
  ```powershell
  uv tool install --editable . --force
  ```
