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

- Python deps: `pip install -r requirements.txt` - but install a CPU-only
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
  polled (e.g. `cloudflared tunnel --url http://localhost:8000`). The tunnel URL changes
  every restart — update it in the Kick app's webhook settings each time.
- Scripts (run with `PYTHONPATH=src python scripts/<name>.py`):
  - `subscribe.py <slug>` — add a channel to the watchlist (subscribes to `chat.message.sent`
    on Kick's side, fetches and stores its 7TV emote keywords)
  - `refresh_emotes.py <slug>` — re-fetch and re-classify an already-watched channel's 7TV
    emotes without touching its Kick subscription (re-running `subscribe.py` would create a
    duplicate subscription)
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
  - `backfill_taste.py [--limit N]` — fills in transcript/audio-event tags/frame
    embedding for any moment with a clip but missing one or more of them (imports
    don't go through the live pipeline's background tasks, so they start out missing
    all three)
- Dashboard: `GET /dashboard` (auto-refreshes every 15s, skipped while a clip video is
  playing so it doesn't get cut off mid-watch)
- No automated test suite yet — verification so far has been ad-hoc unit tests written
  inline during development (rolling-window detector logic, signature verification, etc.),
  not committed as a pytest suite.

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
    -> SQLite: streamers, chat_messages, moments, channel_keywords
    -> recording_manager ticks a ChannelRecorder per live watched channel (ffmpeg
       segmenting a real HLS URL into a rolling buffer) -> a detected moment cuts a
       clip from that buffer, path stored back on the moment
    -> Web dashboard (Jinja2, embeds the clip video) + CLI scripts for reviewing the
       watchlist and moments
```

Module map (`src/kick_clip_hunter/`):
- `kick_client.py` — app access token + channel lookup + event subscription
- `seventv_client.py` — fetches a channel's 7TV emote set (public API)
- `webhook_security.py` — verifies Kick's RSA-signed webhook payloads
- `db.py` — SQLite schema and queries; schema changes are applied as idempotent
  `ALTER TABLE`s in `get_connection()`, not a migration framework
- `detector.py` — the heuristic engine; its module docstring is the source of truth for
  how detection works, don't duplicate that explanation here
- `timeutil.py` — UTC-to-local-time display helper
- `main.py` — FastAPI app: webhook receiver + `/dashboard` route + wires moment
  detection to clip extraction
- `templates/dashboard.html` — the dashboard's Jinja2 template
- `kick_session.py` — paths for the persisted browser login (session state + profile dir)
- `kick_stream.py` — captures a live channel's real, working HLS URL (see "Clip creation")
- `recorder.py` — per-channel ffmpeg recording into a rolling segment buffer, plus
  `extract_clip()` to cut a clip from it
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

  These three all run as fire-and-forget background tasks right after a clip is saved
  (`main.py`'s `_transcribe_clip_background` and siblings), storing their raw output on
  the `moments` row. None of them judge or score a clip - they're pure local data
  capture for a future learned classifier (detector features + these embeddings/tags,
  no API call at inference), once enough rated moments exist to train one. See the
  moment-judge design (PR #19, not yet merged) for the fuller picture, including why the
  actual judging step calls the Claude API instead of running locally.

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
- kick.com's Cloudflare protection blocks plain HTTP clients (httpx/curl) *and* plain
  Playwright/Selenium automation outright — even headed, even with a real logged-in
  Chrome profile. Only `patchright` (a Playwright fork patching the CDP leaks bot
  detection checks for) gets through, and only in headed mode — headless still gets
  blocked even with valid cookies. This means every stream-URL fetch briefly opens a
  real visible browser window; there's no way around that on this stack.
- Once you have the real URL, ffmpeg follows it like any HLS client (periodic manifest
  re-fetch) indefinitely — no need to refresh preemptively, only restart if the process
  actually dies.
- This is a deliberate choice to evade kick.com's anti-automation measures, done with
  the user's explicit sign-off that it's a ToS gray area which may stop working at any
  time.

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
  trigger; the fix is just re-running `subscribe.py <slug>` for each watchlisted channel
  (safe — it's idempotent about the DB side, and there's nothing to "duplicate" once the
  old subscription is already gone). If moments stop appearing and the dashboard's
  watchlist looks right, check `GET events/subscriptions` on the official API before
  assuming the detector or webhook receiver broke.

## Workflow conventions

- Every change goes on its own feature branch with a PR — don't push straight to
  `master`, and don't merge without an explicit go-ahead from the user.
- Squash-merge, then delete the branch (local and remote) and sync `master`.
- Don't hardcode the current watchlist's specific streamer names in PR descriptions or
  test plans — the watchlist changes constantly, so describe test coverage generically.
