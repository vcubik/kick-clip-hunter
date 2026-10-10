# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

kick-clip-hunter watches a list of Kick.com streamers and detects potentially viral/funny
moments from chat activity (message-rate spikes, emote/keyword frequency). Detected
moments are stored with a timestamp, stream-elapsed offset, and the chat snippet around
them, and a clip is automatically cut from a rolling local recording buffer. See
[README.md](README.md) for the roadmap.

## Current status

Core pipeline is working end-to-end: webhook ingestion, SQLite storage, the detection
heuristic, automated clip recording, and a web dashboard. The detector is still being
actively tuned against real streams — expect its thresholds/weights to keep changing.

## Setup & running

- Python deps: `pip install -r requirements.txt` (the whole application: it pulls in
  `requirements-core.txt`, the service without the ML stack) - but install a CPU-only
  PyTorch build first: `pip install torch torchaudio --index-url
  https://download.pytorch.org/whl/cpu`. `funasr`/`transformers` (audio
  events, frame embeddings - see below) pull in `torch` as a dependency;
  without this step first, pip grabs the default CUDA-bundled wheel, which is
  multiple GB of dead weight on this host's AMD GPU (RX 480 - no practical
  CUDA/ROCm path on Windows, so everything ML-related here runs on CPU).
- `ffmpeg` must be a real, full build on PATH (e.g. `winget install Gyan.FFmpeg`) — the
  one Playwright/patchright bundle for their own internal use lacks HTTPS support and
  can't fetch anything
- `.env` (gitignored): `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` from a Kick app at
  https://kick.com/settings/developer
- One-time browser login for clip creation: `PYTHONPATH=src python scripts/kick_login.py`
  — opens a real browser window, log in yourself, it detects success and saves the
  session to `data/kick_session_state.json` / `data/kick_browser_profile`. See
  "Clip creation" below for why this is needed at all.
- Run the server: `PYTHONPATH=src python -m uvicorn kick_clip_hunter.main:app --host 0.0.0.0 --port 8000 --no-access-log`
  (`--no-access-log` avoids interleaving uvicorn's own request log with the app's
  channel-tagged log lines)
- Local dev needs a public HTTPS tunnel, since Kick pushes webhooks rather than being
  polled. This host uses Tailscale Funnel, which gives a stable
  `https://<machine>.<tailnet>.ts.net` URL for free (no domain needed), so the Kick
  app's webhook URL only has to be set once. One-time setup:
  `tailscale funnel --bg --set-path /webhooks/kick http://127.0.0.1:8000/webhooks/kick`
  — `--bg` persists it across reboots, and `--set-path` exposes only the webhook route
  (the dashboard stays off the public internet). `tailscale funnel status` shows the URL.
  On Windows the Tailscale tray app has to be running: without it the backend sits in
  `NoState` ("Tailscale is starting") and every CLI command silently does nothing.
  A quick `cloudflared tunnel --url http://localhost:8000` also works as a fallback, but
  its URL changes every restart and has to be re-entered in the Kick app each time.
- Scripts (run with `PYTHONPATH=src python scripts/<name>.py`):
  - `subscribe.py <slug>` — add a channel to the watchlist (fetches and stores its 7TV emotes -
    the keywords the detector listens for and the pictures the dashboard draws - and subscribes to `chat.message.sent` on Kick's side unless a subscription is
    already there - so re-running it is safe and repairs a dropped subscription)
  - `refresh_emotes.py <slug>` — re-fetch and re-classify an already-watched channel's 7TV
    emotes (keywords and pictures) without talking to Kick's subscription API at all
  - `list_watchlist.py`, `list_moments.py` — inspect the DB from the CLI
  - `kick_login.py` — one-time interactive browser login for clip creation (see above)
  - `create_clip.py <slug>` — manually publish an official Kick clip for a channel's
    current livestream (see "Clip creation" below)
  - `import_clip.py <mp4-path> <channel_slug>` — import an externally-sourced clip
    (e.g. an official Kick clip downloaded outside this pipeline) as a moment, so it
    can be rated and used as training/reference data. Bypasses live chat detection
    entirely, so it's stored with zeroed-out detector signals (`reason=manual_import`)
    rather than fabricated ones - only the clip file and rating are real.
  - `import_clips_dir.py <folder> [channel_slug]` — same as `import_clip.py`, but for
    every video file in a folder at once (`channel_slug` defaults to `unknown`)
  - `check.py [pytest args]` — lint, test formatting and the test suite with coverage, i.e.
    exactly what CI runs; the one command before opening a PR
  - `replay_chat.py <slug> [--since ISO] [--until ISO] [--set NAME=VALUE ...]` — replays a
    channel's stored chat through the detector on the messages' own clock and reports how
    many moments would have fired and how long their clips would have been, optionally
    with detector constants overridden for comparison. The way to check a threshold change
    against a real stream before running it live; read-only.
  - `backfill_taste.py [--limit N]` — fills in transcript/audio-event tags/frame
    embedding for any moment with a clip but missing one or more of them (imports
    don't go through the live pipeline's background tasks, so they start out missing
    all three)
- Logs: the server logs to the console and to `data/logs/kick_clip_hunter.log` (rotated,
  10 x 10 MB) - check the file first when something went wrong while unattended. On
  startup it also turns off the console's QuickEdit mode (`win_console.py`), which
  otherwise freezes the whole app whenever someone clicks in the terminal window.
- Dashboard: `GET /dashboard` is the review page - a queue of moments (unrated, all, or
  best: rated 4 or higher) and the one that is open beside it, driven from the keyboard
  (1-5 rate and move on, J/K move, Space plays). Under the clip is the chat trace:
  messages a second and how many of them were laughing, on the clip's own time axis, from
  30s before the clip to 60s after it. It is also the scrubber - one timeline over the
  clip and its two context files - and chat is replayed beside it in step with the
  picture, emotes drawn as pictures (Kick's own and the channel's 7TV ones, both loaded
  straight from their CDNs) and each chatter's name in the colour it has on Kick, with
  their badges in front of it. Where a message belongs against the picture is a guess (to
  start with, the frame broadcast as it arrived), so under the chat are Earlier/Later
  buttons that correct it for the open moment's channel. `GET /dashboard/channels` has the
  watchlist, the per-clip analysis switches and shutdown. Neither reloads on its own; the
  queue announces how many new moments/clips have arrived since the page was loaded
  (polled from `GET /moments/status`). An empty queue is the normal state, so that page
  then says what the service is doing instead.
- Tests: `pip install -r requirements-dev.txt`, then `python -m pytest` (with the project's
  virtual environment: `.venv\Scripts\python.exe -m pytest`). See "Testing" below.

## Language convention

All repo artifacts — commit messages, code, code comments, docs, issues/PRs — must be
written in English, regardless of what language the conversation with the user is in.

## Architecture

```
Watchlist (streamers)
    -> Kick app access token (client credentials, NOT user OAuth - lets the app
       subscribe to any channel's chat without that streamer's consent)
    -> Kick webhook subscription (chat.message.sent) -> FastAPI webhook receiver
    -> Detection engine (rolling window per channel; see detector.py)
    -> SQLite: streamers, chat_messages, moments, channel_keywords, channel_emotes,
       chat_identities
    -> recording_manager ticks a ChannelRecorder per live watched channel (downloading
       the stream's own HLS segments into a rolling buffer) -> a detected moment cuts a
       clip from that buffer, path stored back on the moment
    -> Web dashboard (Jinja2, embeds the clip video) + CLI scripts for reviewing the
       watchlist and moments
```

Module map (`src/kick_clip_hunter/`):
- `kick_client.py` — app access token + channel lookup + event subscription
- `seventv_client.py` — fetches a channel's 7TV emote set (public API): each emote's name
  in that channel, its id and the shape of its picture. Stored twice over, for two uses:
  `channel_keywords` is what the detector listens for (lowercased, filtered, weighted) and
  only changes when asked (`subscribe.py`, `refresh_emotes.py`); `channel_emotes` is what
  the dashboard draws (every emote, exact case - the same word is a different picture in
  another channel) and is also refreshed in the background on every start, together with
  7TV's global emotes (the ones every channel has; a channel's own emote of the same name
  wins over them)
- `webhook_security.py` — verifies Kick's RSA-signed webhook payloads
- `db.py` — SQLite schema and queries; schema changes are applied as idempotent
  `ALTER TABLE`s in `get_connection()`, not a migration framework
- `detector.py` — the heuristic engine; its module docstring is the source of truth for
  how detection works, don't duplicate that explanation here
- `timeutil.py` — UTC-to-local-time display helper
- `main.py` — FastAPI app: webhook receiver + the dashboard's routes + wires moment
  detection to clip extraction
- `dashboard_view.py` — what the dashboard's pages say, worked out from stored rows: how
  the queue is grouped by stream, how times, counts and detector reasons are worded.
  Pure functions, so the wording is unit-tested without rendering a page
- `chat_trace.py` — chat set against a clip: which second of the clip a message belongs
  to, the trace under the clip and the spark in a queue row as SVG paths, and what chat
  said most in a moment ("KEKW x31"). Pure, like `dashboard_view.py`. "Clip time" is
  seconds from the clip's first frame; a clip's start is stored on the stream's program
  clock (`moments.clip_start`, with `clip_duration`), and chat is taken to have seen
  each frame `CHAT_DELAY_SECONDS` later - an assumption, since the real figure is the
  player's buffer plus any delay the streamer has set, so it can be set per channel from
  the review page (`POST /channels/{slug}/chat_delay`, kept in `app_settings`). It is not
  the recorder's `PLAYBACK_DELAY_SECONDS`, which frames clips and is on the long side. It
  starts at 0 - chat where it arrived, the one placement a streamer's own on-screen chat
  lets you check - and can go a little below. Clips cut before those
  columns existed have their length measured (ffprobe) the first time one is opened and
  are placed by estimate: centred on the window they were cut for
- `chat_identity.py` — who a chatter is in a channel: name colour and badges, which Kick
  sends with every chat message (`sender.identity`). Kept once per chatter per channel
  (`chat_identities`), not per message: the webhook receiver remembers what it last stored
  and writes only when that changes, so what the dashboard shows is the latest known
  state, and chatters not seen since this was added keep the colour worked out from their
  name. Pure, and the place where everything a chatter controls is checked (the colour
  goes into a style attribute, a badge's type into a file name). A colour too dark for
  the page's grey is lightened until it can be read. The badge pictures in
  `static/badges/` are placeholders drawn for this project, one per badge type plus
  `other.svg` for types without one; a channel's own subscriber badges are not fetched
- `templates/` — the dashboard's Jinja2 templates: `base.html` (the bar across the top),
  `review.html`, `channels.html`, and `_moment.html` - the open moment, which is also
  served on its own (`GET /dashboard/moments/{id}`) so the page can open another moment
  without reloading
- `static/` — `dashboard.css` (design tokens first - every colour, size and space used
  is one of them - then components, then page layout; its opening comment states the
  rules), `dashboard.js` (no dependencies, everything wired by delegation because the
  open moment's markup gets replaced) and the bundled typeface, Archivo, with its licence
- `kick_session.py` — paths for the persisted browser login (session state + profile dir)
- `kick_stream.py` — captures a live channel's real, working HLS URL (see "Clip creation")
- `recorder.py` — per-channel recording into a rolling segment buffer, plus
  `extract_clip()` to cut a clip from it. A thread per channel polls the live playlist
  (`httpx`) and saves each source MPEG-TS segment untouched, named by its
  program-date-time; ffmpeg is only used to remux the joined segments into the clip.
  It deliberately isn't a long-running `ffmpeg -c copy`: a server-side stitched ad
  changes the stream layout mid-broadcast, which left ffmpeg alive but recording
  garbage for hours. See the module docstring for the full story, including how clips
  handle a discontinuity and the `PLAYBACK_DELAY_SECONDS` clock shift.
  A moment's clip is kept short (about 35s unless the reaction keeps drawing in new
  people); the 30s before and 60s after it are saved next to it as
  `moment_<id>_before.mp4` / `_after.mp4` and played by the dashboard from the same strip
  as the clip. `extract_clip` returns the stretch of the broadcast the clip really holds
  (whole segments, so a little more than was asked for), which is stored with the moment.
- `recording_manager.py` — background loop driving one `ChannelRecorder` per watched
  channel, gated on an `is_live` check so offline channels never touch a browser
- `clip_creator.py` — alternative path: publishes an official Kick clip via the site's
  own internal API under a logged-in account (see "Clip creation")
- `transcriber.py` — local speech-to-text of a cut clip (faster-whisper, CPU)
- `audio_events.py` — local audio event/emotion tags for a cut clip (SenseVoice via
  funasr, CPU) - language/emotion/non-speech-event tags only, not a second transcript
- `frame_encoder.py` — local video-frame embeddings for a cut clip (SigLIP2 via
  transformers, CPU) - 3 uniformly-sampled frames, each kept as its own vector
  (concatenated in the BLOB) rather than averaged into one, so temporal position
  within the clip isn't lost
- `sound_events.py` — local AudioSet sound-event tags + embedding for a cut clip
  (PANNs Cnn14, CPU) - broader complement to `audio_events.py`: SenseVoice's ~8-class
  event vocabulary is a secondary feature of an ASR model and mostly comes back
  "unknown" on noisy multi-source stream audio (game sound + mic + music at once);
  PANNs' 527 AudioSet classes are purpose-built for tagging exactly that. On Windows,
  `panns_inference` downloads its label CSV and model checkpoint via a hardcoded
  `os.system('wget ...')` call that silently no-ops (no wget binary) - both are
  pre-downloaded via `urllib` before the library's own logic can run; see the module
  docstring.

- `ml_loading.py` - one lock shared by the four analysis modules above. Their libraries
  (faster-whisper, funasr, transformers/torch, panns_inference) are imported, and their
  models loaded, the first time a result is actually asked for, not at startup: importing
  them takes anywhere from ten seconds to a couple of minutes and every analysis step is
  off by default. The lock keeps those first imports/loads one at a time even though they
  now happen on worker threads.

  These four all run as fire-and-forget background tasks right after a clip is saved
  (`main.py`'s `_transcribe_clip_background` and siblings), storing their raw output on
  the `moments` row. Each has its own on/off switch on the dashboard's Channels page
  ("Per-clip analysis", persisted in `app_settings`); all four default to off since they cost CPU
  time on every clip, and `backfill_taste.py` can fill in whatever was skipped later. None of them judge or score a clip - they're pure local data
  capture for a future learned classifier (detector features + these embeddings/tags,
  no API call at inference), once enough rated moments exist to train one.

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

## Testing

[docs/testing.md](docs/testing.md) is the full guide (layout, what each level covers, when to
run what, the manual live checklist). What matters when changing code:

- `python -m pytest` runs everything (700+ tests, well under a minute); `python
  scripts/check.py` runs what CI runs (lint, test formatting, tests with coverage). CI
  (`.github/workflows/tests.yml`) does so on every push and pull request, on Windows and
  Linux, and fails under 90 % coverage. Run `check.py` before opening a PR.
- The suite is hermetic by construction (`tests/conftest.py`): it runs in a throwaway working
  directory with its own database per test, and any attempt to reach a non-loopback host,
  launch a browser or exit the process fails the test. It is safe to run while the live
  server is running from the same checkout - but don't *edit* production files in place for
  experiments while it is (a restart would pick them up); use a copy.
- A behaviour change comes with a test that would fail without it; a bug gets a failing test
  first. Fake at the boundary (`tests/support/`: `FakeKickApi`, `FakeHlsServer`,
  `WebhookSigner`, `ChatSim`, the `service` fixture) rather than patching the service's own
  functions, and never `sleep` - pass the clock in or use `wait_until`.
- Detector tests are written against `detector.py`'s constants, not their current values, so
  retuning a threshold doesn't break them. If a tuning change does fail a test, it changed a
  rule, not a number - check that was intended.
- `tests/` is kept formatter-clean (`python -m ruff format tests`); application code is only
  linted.
- Not covered, and why: the browser-driven parts (`kick_stream.py`, `clip_creator.create_clip`,
  the login script) and actually running the ML models. After touching those or the code next
  to them, go through the live checklist in docs/testing.md.

## Clip creation

Two independent, unrelated paths exist. Both required real reverse-engineering because
Kick's official public API has no clip/VOD/playback support at all (confirmed both by
testing — `stream.url`/`stream.key` on the official channels endpoint are always empty
for viewers — and by the fact that none of the app's OAuth scopes relate to media).

**1. Self-hosted recording (the automated path, `recorder.py` + `recording_manager.py`)**
- The `playback_url` embedded in a channel page's server-rendered HTML does not work if
  used directly (fails AWS IVS signature verification) — only the URL the page's own
  player actually requests, once fully loaded, is valid. Capturing that live request is
  what `kick_stream.py` does.
- kick.com's pages can't be fetched with a plain HTTP client (httpx/curl) and don't
  load reliably under plain Playwright/Selenium or in headless mode. The project drives
  a headed browser through `patchright`, so every stream-URL fetch briefly opens a real
  visible browser window; there's no way around that on this stack.
- Once you have the real URL, the recorder follows it like any HLS client (periodic
  playlist re-fetch) indefinitely — no need to refresh preemptively, only restart if it
  dies or stalls. Playlists are Twitch/IVS-style: 2-4s MPEG-TS segments, each with an
  `EXT-X-PROGRAM-DATE-TIME` and an `EXTINF` title of `live`.
- Ad breaks (seen in the Slots & Casino category: a 30s mid-roll every 30 minutes) are
  stitched into the live playlist server-side and *replace* the broadcast for their
  length - the stream's own segments for that stretch are never listed. They are marked
  by `EXT-X-DATERANGE` tags (`live-video-net-stream-source` naming the ad instead of
  `live`, plus `live-video-net-stitched-ad-break-start`/`-end`), a discontinuity on
  either side and the ad's id as the segment title. The recorder stores them as an ad
  group, logs every discontinuity and saves the raw playlist to `data/hls_debug/`.
- What was really on stream during an ad is only in the broadcast's VOD, which Kick
  writes as the stream goes along (12.5s segments, same program clock, roughly 15-25s
  behind live) and serves from `stream.kick.com` to a plain HTTP client. Its URL comes
  from `/api/v2/channels/{slug}/videos` (not reachable with a plain HTTP client, so it
  is fetched inside the browser session that captures the live URL). A clip or context clip whose window
  touches an ad is cut from the VOD instead, so it comes out up to ~25s longer than
  usual (whole VOD segments); a channel with VODs turned off still gets the ad.

**2. Manual official clips (`clip_creator.py`, `scripts/create_clip.py`)**
- Kick's own "Create Clip" button hits `POST /api/internal/v1/livestreams/{livestream_slug}/clips`
  (body `{"duration": 180}`, cuts a 180s source buffer *server-side* — not a client-side
  recording buffer, despite what some reverse-engineered docs elsewhere claim) then
  `POST .../clips/{clip_id}/finalize` (body `{"duration", "start_time" (offset into the
  180s source), "title"}`) returning the public clip. `livestream_slug` (distinct from
  the stable numeric livestream id) is embedded in the channel page's HTML and appears
  to rotate, so it's re-extracted fresh each call.
- Requires being logged in as a real Kick account (`scripts/kick_login.py` captures that
  session once) — clips get attributed to whichever account logged in there.
- Not wired into the automatic pipeline; it's a standalone manual tool.

## Operational quirks

- Kick's webhook config UI occasionally fails to persist a URL change silently. If a
  channel is confirmed live/chatting but no webhooks are arriving, the fix is: toggle
  **Enable Webhooks** off, re-enter the URL, re-enable, save, then refresh the page to
  confirm the URL actually stuck.
- The 7TV public API (`7tv.io/v3/users/kick/{broadcaster_user_id}`) has no auth and
  returns a channel's full emote set, including very short names ("lo", "re", "xd") that
  are useless as substring keywords — `MIN_EMOTE_NAME_LENGTH` in `detector.py` filters
  these out before they're stored.
- Event subscriptions (`chat.message.sent`) have, more than once, silently gone back to
  zero on Kick's side with no error or warning — the app token still works fine, chat
  messages just stop arriving entirely for every watched channel. There's no known
  trigger; the running server re-checks every 10 minutes and re-subscribes whatever is
  missing (logged as a warning), as does a restart or re-running `subscribe.py <slug>`
  for a channel (all of them check what Kick still has first, so nothing is ever
  subscribed twice). If moments stop appearing and the dashboard's
  watchlist looks right, check `GET events/subscriptions` on the official API before
  assuming the detector or webhook receiver broke.

## Workflow conventions

- Every change goes on its own feature branch with a PR — don't push straight to
  `master`, and don't merge without an explicit go-ahead from the user.
- Tests and lint pass before a PR is opened, and its CI run is green before it is merged.
- Squash-merge, then delete the branch (local and remote) and sync `master`.
- Don't hardcode the current watchlist's specific streamer names in PR descriptions or
  test plans — the watchlist changes constantly, so describe test coverage generically.
