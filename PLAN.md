# tmusic — plan & status

Terminal YouTube Music player. Dev on zemon (Linux), used on a Windows laptop
(PowerShell or cmd, mpv via winget). Single user.

## Decisions (locked)

| Topic | Decision |
|---|---|
| Audio | mpv only (wasapi on Windows). No server audio, no pipe backend. |
| Auth | **Browser cookies** (`browser.json`, SAPISIDHASH) via `tmusic auth cookies`. OAuth kept as legacy — InnerTube rejects Bearer tokens on WEB_REMIX since 2025-08-29 (ytmusicapi #813). Unauthenticated = public search+play. |
| Stream source | **yt-dlp** (`bestaudio`, itag 251) as primary resolver — decrypts signatureCipher that ytmusicapi's unauth get_song leaves encrypted. ytmusicapi.get_song as fallback + duration source. |
| UI | curses TUI (full screen). `windows-curses` on Windows; Windows Terminal recommended, cmd best-effort. |
| Search/web | ytmusicapi for music. Exa is research-time only (not an app feature). |
| Code | /home/zemon/music-cli, uv-managed venv (.venv), pyproject deps (incl. yt-dlp). |

## Architecture

```
tmusic (argparse CLI)
├── TUI (curses, M2)            main thread, 10 Hz repaint
├── PlayerCore (M2)             queue, index, position; single writer
├── ytm.py                      YTMusic service: auth, search, stream URL,
│                               liked songs, watch playlist. Translates all
│                               ytmusicapi/requests exceptions → tmusic.errors
└── mpv_backend.py              MPVBackend: mpv child process + JSON IPC
                                (unix socket / named pipe), reader thread →
                                MPVEvent queue; play/pause/resume/stop/seek/
                                position/duration/wait_end
```

Config/token/logs: `~/.config/tmusic/` (Windows: `%LOCALAPPDATA%\tmusic\`).
config.toml (oauth creds, mpv path, seek steps), oauth.json (0600), tmusic.log (rotating 5x1MB).

Error policy: `errors.TmusicError` subclasses each carry a human
`user_message`; raw detail goes to the log file only. Exit codes: 0 ok, 1
user error, 130 Ctrl-C.

## Milestones

- **M0 — scaffold, config, auth, doctor** (DONE 2026-09-26)
  pyproject + uv venv (py3.12), package modules, 11 passing unit tests,
  `tmusic doctor` / `auth login|status` / `search` / `play` (headless)
  commands, full error translation.
- **M0.5 — auth + stream path fixed after real-world debugging** (DONE)
  Found: (1) config dir was chmod 0600 → 0700; (2) Google device flow
  returns pending-error dicts, not exceptions — handled; (3) OAuth Bearer
  tokens rejected by InnerTube since 2025-08-29 (#813) → switched default
  to browser-cookie auth + unauthenticated; (4) yt-dlp added as dep and
  made the stream-URL resolver (decrypts signatures). Verified: search
  20 results w/ durations, yt-dlp → signed itag 251 URL in ~2s.
- **M1 — play-through on Windows** (NEXT — only laptop-side verification left)
  User side: winget install mpv + python; copy repo to laptop; uv sync;
  `tmusic auth cookies` (paste DevTools headers); `tmusic run` and press
  Enter to play.
  Done = a song is audible on the laptop; `tmusic doctor` all green.
- **M2 — TUI** (DONE 2026-09-26 — code complete, pty-verified)
  curses full-screen: header w/ auth badge, now-playing (title/artist/
  album, animated EQ, states), seekable timeline (arrows + mouse click),
  queue list (j/k/↑↓ cursor, d delete, scroll), search prompt (/public,
  L/library), results view (Enter=play from here, s=all, a=add), liked
  (m) / library (l), help (?), status bar. Single worker thread in
  PlayerCore (all network + transitions), 2 Hz position poll, auto-next on
  mpv end-file, stale-load generation guard. Verified in pty: render,
  search flow, queue add/play, clean mpv-missing error in TUI, no stderr
  screen corruption (console logging muted in TUI).
  NOT yet verified (needs mpv+audio): real playback, seek, auto-next.
- **M3 — library UX + Windows polish**
  library/liked/playlist browsing as queue sources, play history,
  cmd.exe verification (fallback: Rich-live simple layout if flaky),
  README polish, Windows installer script (powershell one-liner).
- **M4 — stretch**
  lyrics view (ytmusicapi.get_lyrics), real FFT EQ via ffmpeg sidecar,
  radio mode (get_watch_playlist radio=True), mouse support.

## Known risks

- Google OAuth client is a one-time manual step (Google Cloud Console) —
  biggest user-side dependency.
- signatureTimestamp drifts daily → handled by auto-refresh retry.
- Bot-checks (YTMusicGatedError) → distinct user message, backoff + retry.
- Signed URLs expire ~6h → never cached across tracks/sessions.
- cmd.exe curses quirks → windows-curses package; verified in M3.

## Verified on zemon (2026-09-26)

- `uv sync` clean; pytest 11/11 pass
- `tmusic --version`, `doctor` (reports missing oauth + mpv correctly),
  `auth login` (clean MissingOauthClient guidance), `auth status`,
  `search` (clean AuthError message)
- chmod bug found & fixed (config dir must be 0700, not 0600)