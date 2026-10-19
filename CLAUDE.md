# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
It is loaded into every request, so it is kept to what every session needs; the detail is in
`docs/`, read when the work calls for it (see "Where the detail is").

## Project overview

kick-clip-hunter watches a list of Kick.com, Twitch and YouTube streamers and detects potentially
viral/funny moments from chat activity (message-rate spikes, emote/keyword frequency). Detected
moments are stored with a timestamp, stream-elapsed offset, and the chat snippet around
them, and a clip is automatically cut from a rolling local recording buffer. A channel's
whole stream can also be recorded on request, with its chat - and a TikTok channel is
watched for that alone: recorded, never looked for clips in (`platforms.RECORDED_ONLY`). A bot
can write in a channel's chat - when a recording starts or ends, when a clip was made - and
take commands there from its streamer and moderators (start or stop a recording, say how
things stand); it is off for every channel until that channel's settings switch it on. And
the service can tell whoever runs it what happens - a clip saved, a recording started or
saved, something on the Health page that needs a look - in a Telegram chat, a Discord channel
or both (`notify.py`), once `.env` says where. See [README.md](README.md) for the roadmap.

Status: the core pipeline works end-to-end (webhook ingestion, SQLite storage, the detection
heuristic, automated clip recording, a web dashboard). The detector is still being tuned against
real streams - expect its thresholds/weights to keep changing. The constants in the code are what
is recommended; what a channel really runs on is set from the dashboard's Settings page
(`tuning.py`).

## Where the detail is

Read these on demand, not up front:

- [docs/setup.md](docs/setup.md) - every setup step in full (`.env` keys, the Twitch/Kick/bot
  sign-ins, notifications, the Telegram local Bot API server, the Tailscale Funnel), every script
  and its arguments, logs. Read when setting something up or running/writing a script.
- [docs/architecture.md](docs/architecture.md) - the data-flow diagram and a paragraph per module
  (what it does, its key functions and constants, how it is wired into `main.py`). Read before
  changing a module you have not worked on in this session, together with its docstring.
- [docs/dashboard.md](docs/dashboard.md) - every dashboard page (review, Channels, Recordings,
  Analyze, Settings, Health): what it shows, its keyboard and buttons, the routes and polling
  behind it. Read before changing a page, a template or `dashboard.js`.
- [docs/platforms.md](docs/platforms.md) - how video is got from Kick, Twitch, YouTube and TikTok
  (playlists, ads, VODs, FLV) and the manual official-clip path, with why. Read before touching
  the recorder, a `*_stream.py` module, `kick_stream.py` or `clip_creator.py`.
- [docs/operations.md](docs/operations.md) - gotchas seen live: channel names and renames per
  platform, the bot's account limits, YouTube/TikTok fragility, notification size limits, disk
  use, SQLite locking, Kick's webhook UI and vanishing subscriptions. Read when something
  misbehaves live, or before changing code near one of them.
- [docs/testing.md](docs/testing.md) - the full testing guide: layout, the fakes in
  `tests/support/`, what each level covers, the manual live checklist.

## Setup & running (short)

- Deps: install CPU-only PyTorch first (`pip install torch torchaudio --index-url
  https://download.pytorch.org/whl/cpu` - the host's AMD RX 480 has no practical CUDA/ROCm path,
  so all ML runs on CPU and the default CUDA wheel is GBs of dead weight), then `pip install -r
  requirements.txt` (`requirements-core.txt` is the service without the ML stack).
- `ffmpeg` must be a full build on PATH (`winget install Gyan.FFmpeg`) - the one bundled with
  Playwright/patchright lacks HTTPS.
- `.env` (gitignored): `KICK_CLIENT_ID`/`KICK_CLIENT_SECRET` (a Kick app); optionally
  `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` (without them no Twitch channel can be watched),
  `KICK_REDIRECT_URI`, `EULER_STREAM_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `TELEGRAM_API_URL`, `DISCORD_WEBHOOK_URL`, `DASHBOARD_URL`. YouTube and TikTok need nothing.
  Notifications can also be set up on the Settings page (that wins over `.env`).
- One-time sign-ins: `scripts/twitch_login.py` (the Twitch account chat is read and the bot
  writes as, device flow), `scripts/kick_login.py` (a real browser window, for clip creation), the
  bot's Kick account from the dashboard (OAuth 2.1 + PKCE, redirect
  `http://localhost:8000/bot/kick/signed_in`). Details in docs/setup.md.
- Run: `PYTHONPATH=src python -m uvicorn kick_clip_hunter.main:app --host 0.0.0.0 --port 8000
  --no-access-log`. Kick pushes webhooks, so a public HTTPS tunnel is needed: Tailscale Funnel
  exposes only `/webhooks/kick` (the dashboard stays inside the tailnet); on Windows the
  Tailscale tray app must be running or every CLI command silently does nothing.
- Scripts run as `PYTHONPATH=src python scripts/<name>.py`: `subscribe.py` (add a channel; safe
  to re-run, repairs a dropped subscription), `refresh_emotes.py`, `replay_chat.py` (replay
  stored chat through the detector to check a threshold change - read-only), `check.py` (what CI
  runs), `list_watchlist.py`, `list_moments.py`, `import_clip.py`, `import_clips_dir.py`,
  `create_clip.py`, `backfill_taste.py`, `join_context_clips.py`, `telegram_chats.py`,
  `twitch_login.py`, `kick_login.py`. See docs/setup.md.
- Logs: console and `data/logs/kick_clip_hunter.log` (rotated) - check the file first after
  something went wrong unattended.
- Dashboard at `GET /dashboard` (review queue), plus Channels, Recordings, Analyze, Settings and
  Health pages - see docs/dashboard.md.

## Language convention

All repo artifacts — commit messages, code, code comments, docs, issues/PRs — must be
written in English, regardless of what language the conversation with the user is in.

## Architecture

Chat comes in per platform - Kick by signed webhook (app access token, no streamer consent
needed), Twitch over an EventSub WebSocket as a signed-in account, YouTube by polling InnerTube
as a visitor, TikTok (recorded channels only) through TikTokLive + Euler Stream - and every
message becomes a `ChatMessage` that goes through `main._take_chats`: stored a batch at a time,
then fed to the detector, which may open a moment session. `recording_manager` runs a
`ChannelRecorder` per live channel that keeps a rolling buffer of the stream's own segments; a
moment cuts its clip (with context before and after, one file) from that buffer, and a whole-stream
recording rides on the same recorder. The chat bot, notifications and the dashboard hang off
`main.py`. Full diagram and per-module detail: [docs/architecture.md](docs/architecture.md).

Module map (`src/kick_clip_hunter/`; each module's docstring is the source of truth):

- `platforms.py` — the platforms and how a channel's name says which one (`slug`, `login@twitch`,
  `handle@youtube`, `handle@tiktok`); non-Kick channels have negative `broadcaster_user_id`s
- `streams.py` — live status and stream URLs, dispatched to the channel's platform
- `chat_message.py` — `ChatMessage`, one chat message from any platform, emotes as tokens
- `kick_chat.py` — Kick's webhook payload as a `ChatMessage`
- `kick_client.py` — Kick public API: app token, channel lookup, subscriptions, the bot's chat
- `kick_auth.py` — the Kick account the bot writes as (OAuth 2.1 + PKCE from the dashboard)
- `account_tokens.py` — a stored account token and the `TokenKeeper` that renews it
- `kick_session.py`, `kick_stream.py` — the browser login's paths; capturing a Kick stream's real
  HLS URL in a headed browser
- `seventv_client.py` — a channel's 7TV emotes (detector keywords and dashboard pictures)
- `webhook_security.py` — verifies Kick's RSA-signed webhooks
- `twitch_client.py`, `twitch_auth.py`, `twitch_chat.py`, `twitch_stream.py`, `twitch_emotes.py` —
  Helix, the signed-in Twitch account, EventSub chat, the stream via streamlink, BTTV/FFZ/7TV emotes
- `youtube_client.py`, `youtube_chat.py`, `youtube_stream.py` — InnerTube (not the quota-limited
  Data API), chat polling while live, the stream via streamlink
- `streamlink_support.py` — imports streamlink once and undoes its log-level renaming
- `tiktok_client.py`, `tiktok_stream.py`, `tiktok_chat.py` — TikTok's room API, its FLV stream,
  chat via TikTokLive/Euler Stream while recorded
- `flv.py` — cuts a live FLV download into MPEG-TS-able pieces (pure)
- `config.py` — settings read from `.env`
- `watchlist.py` — adding a channel (emotes, subscription, row - in an order that leaves nothing
  half-done)
- `db.py` — SQLite schema and queries; schema changes are idempotent `ALTER TABLE`s in
  `get_connection()`, chat stored a batch per commit
- `detector.py` — the heuristic engine; its docstring is how detection works
- `tuning.py` — everything the Settings page can set, globally and per channel (pure); also clip
  commands and the bot's switches and commands
- `replay.py` — stored chat replayed through the detector (behind `replay_chat.py`, the Settings
  trial and moments in a recording)
- `main.py` — the FastAPI app: webhook receiver, chat intake, dashboard routes, background tasks
- `chat_bot.py` — what the bot says and how it writes on Kick and Twitch
- `notify.py` — notifications to Telegram/Discord, and `HealthWatch`
- `dashboard_view.py`, `chat_trace.py`, `chat_stats.py`, `health.py` — pure wording/figures for
  the dashboard, the chat trace, the Analyze page, the Health page
- `tunnel.py` — where the Tailscale Funnel stands
- `chat_identity.py` — a chatter's colour and badges per channel (where chatter input is checked)
- `channel_badges.py` — a channel's own subscriber badge pictures
- `clip_trim.py`, `chat_video.py`, `subtitles.py` — the Trim dialog's cut, chat as video (headless
  patchright + ffmpeg), a moment's transcript as subtitles
- `recorder.py` — per-channel rolling segment buffer and clip extraction (deliberately not a
  long-running `ffmpeg -c copy`; see its docstring)
- `recording_manager.py` — the loop driving the recorders, ending whole-stream recordings
- `stream_recording.py` — a whole stream kept as one file
- `recording_moments.py` — moments found afterwards in a recording's chat
- `clip_creator.py` — manual official Kick clips via the site's internal API
- `transcriber.py`, `audio_events.py`, `frame_encoder.py`, `sound_events.py`, `ml_loading.py` —
  optional per-clip local ML capture (off by default), lazily loaded under one lock
- `timeutil.py`, `win_console.py` — local-time display; turning off the console's QuickEdit mode,
  which otherwise freezes the app on a click
- `templates/`, `static/` — Jinja2 pages and the dashboard's CSS/JS (`dashboard.css` opens with
  its rules: every colour, size and space is a design token)

### Detector design principles (see `detector.py` docstring for the full picture)

- Every threshold is relative to that channel's *own* recent baseline, not a fixed
  number — a quiet stream and a busy one need different reference points.
- Signals require a distinct-sender spike, not just raw count, since Kick doesn't
  rate-limit a single account.
- Native Kick emotes and 7TV emote mentions are both classified by whether the emote
  itself signals laughing (weighted higher) vs. an unrelated reaction (weighted lower).
- Ratio-based scores are capped (`MAX_RATIO_SCORE`) — dividing by a near-zero baseline
  rate produces technically-correct but meaningless triple-digit scores otherwise.
- Moments should be *rare*. A low detection rate is success, not a bug — don't loosen
  thresholds just because a channel goes a long time without one.
- A channel's stored 7TV keywords are weighed under its settings when they are loaded
  (`detector.keyword_weights`), but which names are listened for at all is still decided
  only when its emote set is fetched (`classify_emote_names`) - a settings change never
  adds or drops a stored name by the built-in rules.
- The lull setting cannot go as low as the detector's short window: laughs that arrive
  right after a moment fired are still in that window when a cooldown no longer than it
  runs out, and would fire a second moment by themselves (`tuning.LULL_MIN_SECONDS`).

## Must-know gotchas

The rest is in [docs/operations.md](docs/operations.md).

- A channel has two spellings: the watchlist keeps the one it was added under, Kick sends chat in
  small letters. Anything starting from a watchlist name finds the channel's chat by
  `broadcaster_user_id`, never by comparing names.
- SQLite runs in its default journal mode, where an open read statement holds off every commit
  and a chat insert that waits past the busy timeout fails its webhook. Whatever reads chat in
  bulk reads it in chunks (`db.CHAT_CHUNK`) and never hands on an open cursor.
- Kick's chat subscriptions have silently dropped to zero more than once; the service re-checks
  every 10 minutes. The Health page is the first place to look when moments stop appearing.
- InnerTube (YouTube), TikTok's room API and TikTokLive/Euler Stream are unpublished or third
  party and change without notice; `YouTubeChanged`/`TikTokChanged` in the log is where it shows.

## Testing

[docs/testing.md](docs/testing.md) is the full guide. What matters when changing code:

- `python -m pytest` runs everything (about 3800 tests, a few minutes; with the project's venv:
  `.venv\Scripts\python.exe -m pytest`, after `pip install -r requirements-dev.txt`); `python
  scripts/check.py` runs what CI runs (lint, test formatting, tests with coverage). CI
  (`.github/workflows/tests.yml`) does so on every push and pull request, on Windows and
  Linux, and fails under 90 % coverage. Before opening a PR, run lint (`python -m ruff
  check`, `python -m ruff format --check tests`) and the test files for what was changed,
  and leave the whole suite to CI - except for a larger change (the recorder, the
  detector, anything spanning several modules), where `check.py` is run first.
- The suite is hermetic by construction (`tests/conftest.py`): a throwaway working directory and
  database per test, plenty of disk assumed, video always encoded on the CPU, and any attempt to
  reach a non-loopback host, launch a browser or exit the process fails the test. It is safe to
  run while the live server runs from the same checkout - but don't *edit* production files in
  place for experiments while it does (a restart would pick them up); use a copy.
- A behaviour change comes with a test that would fail without it; a bug gets a failing test
  first. Fake at the boundary with the fakes in `tests/support/` (fake Kick, Twitch, EventSub,
  YouTube, TikTok, HLS/FLV servers, Telegram, Discord, `WebhookSigner`, `ChatSim`, the `service`
  fixture - listed in docs/testing.md) rather than patching the service's own functions, and
  never `sleep` - pass the clock in or use `wait_until`.
- Detector tests are written against `detector.py`'s constants, not their current values, so
  retuning a threshold doesn't break them. If a tuning change does fail a test, it changed a
  rule, not a number - check that was intended.
- A constant patched with `monkeypatch` still reaches the service, because what is
  recommended is read when it is asked for (`tuning.recommended()`); a test about a channel's
  *own* settings saves them through the API or builds a `detector.Settings` / `tuning.tuned`.
  The `service` fixture leaves all constants alone and shortens only the two wall-clock
  limits of the moment sessions the service opens (`service.quiet_seconds`,
  `service.max_open_seconds`).
- `tests/` is kept formatter-clean (`python -m ruff format tests`); application code is only
  linted.
- Not covered, and why: the browser-driven parts (`kick_stream.py`, `clip_creator.create_clip`,
  `chat_video.drawn_column`, the login script) and actually running the ML models. After touching
  those or the code next to them, go through the live checklist in docs/testing.md. A stream
  recording is tested on tiny synthetic footage; a real, hours-long one is a step of that
  checklist.

## Keeping this file short

New knowledge goes where it belongs: a module's behaviour in its docstring, an overview paragraph
in docs/architecture.md, a page in docs/dashboard.md, a live gotcha in docs/operations.md. Here,
add at most a line to the module map or a must-know gotcha that every session needs.

## Workflow conventions

- Every change goes on its own feature branch with a PR — don't push straight to
  `master`, and don't merge without an explicit go-ahead from the user.
- Lint and the tests for what was changed pass before a PR is opened (the whole of
  `check.py` for a larger change - see "Testing"), and its CI run is green before it is
  merged.
- Squash-merge, then delete the branch (local and remote) and sync `master`.
- Don't hardcode the current watchlist's specific streamer names in PR descriptions or
  test plans — the watchlist changes constantly, so describe test coverage generically.
- No link to the Claude Code session in PR descriptions, PR comments or commit messages
  (no `Claude-Session:` trailer, no claude.ai/code/session_... URL).
