# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

kick-clip-hunter watches a list of Kick.com streamers and detects potentially viral/funny
moments from chat activity (message-rate spikes, emote/keyword frequency). Detected
moments are stored with a timestamp, stream-elapsed offset, and the chat snippet around
them; automatic clip cutting is a later phase. See [README.md](README.md) for the roadmap.

## Current status

Core pipeline is working end-to-end: webhook ingestion, SQLite storage, the detection
heuristic, and a web dashboard. The detector is still being actively tuned against real
streams — expect its thresholds/weights to keep changing.

## Setup & running

- Python deps: `pip install -r requirements.txt`
- `.env` (gitignored): `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` from a Kick app at
  https://kick.com/settings/developer
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
- Dashboard: `GET /dashboard` (auto-refreshes every 15s)
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
    -> Web dashboard (Jinja2) + CLI scripts for reviewing the watchlist and moments
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
- `main.py` — FastAPI app: webhook receiver + `/dashboard` route
- `templates/dashboard.html` — the dashboard's Jinja2 template

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

## Operational quirks

- Kick's webhook config UI occasionally fails to persist a URL change silently. If a
  channel is confirmed live/chatting but no webhooks are arriving, the fix is: toggle
  **Enable Webhooks** off, re-enter the URL, re-enable, save, then refresh the page to
  confirm the URL actually stuck.
- The 7TV public API (`7tv.io/v3/users/kick/{broadcaster_user_id}`) has no auth and
  returns a channel's full emote set, including very short names ("lo", "re", "xd") that
  are useless as substring keywords — `MIN_EMOTE_NAME_LENGTH` in `detector.py` filters
  these out before they're stored.
- There is no official Kick API for VOD access or clip creation. Any real video capture
  (later phase) would have to go through the unofficial
  `kick.com/api/v2/channels/{slug}` `playback_url` (m3u8) plus ffmpeg, kept isolated
  behind its own module since that endpoint is undocumented and may change.

## Workflow conventions

- Every change goes on its own feature branch with a PR — don't push straight to
  `master`, and don't merge without an explicit go-ahead from the user.
- Squash-merge, then delete the branch (local and remote) and sync `master`.
- Don't hardcode the current watchlist's specific streamer names in PR descriptions or
  test plans — the watchlist changes constantly, so describe test coverage generically.
