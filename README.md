# kick-clip-hunter

A bot that watches a list of [Kick](https://kick.com) streamers and detects potentially
viral/funny moments based on chat activity. Detected moments are saved as a timestamp +
stream-elapsed offset + chat snippet; automatic clip cutting is planned as a later phase.

## Project status

Core pipeline (M0-M4) is working end-to-end: webhook ingestion, storage, detection, and a
web dashboard. The detection heuristic is still being actively tuned against real streams.

## Setup

1. `pip install -r requirements.txt`
2. Create a Kick app at [kick.com/settings/developer](https://kick.com/settings/developer)
   (needs 2FA enabled on your account), enable webhooks on it.
3. Copy `.env.example` to `.env` and fill in `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET`.
4. For local development, expose your machine with a tunnel (e.g.
   [cloudflared](https://github.com/cloudflare/cloudflared)) and set that URL + `/webhooks/kick`
   as the app's webhook URL. The tunnel URL changes every restart, so this needs
   re-doing each time.

## Usage

Run the server:

```
PYTHONPATH=src python -m uvicorn kick_clip_hunter.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Add a streamer to the watchlist (subscribes to their chat on Kick's side and fetches
their 7TV emotes):

```
PYTHONPATH=src python scripts/subscribe.py <channel_slug>
```

Other scripts (`PYTHONPATH=src python scripts/<name>.py`):
- `refresh_emotes.py <channel_slug>` - re-fetch a watched channel's 7TV emotes without
  re-subscribing on Kick's side
- `list_watchlist.py` - list the current watchlist
- `list_moments.py` - list detected moments

View the dashboard at `http://localhost:8000/dashboard` (auto-refreshes every 15s).

## Architecture

```
Watchlist (streamers)
    -> Kick app access token + webhook subscription (chat.message.sent)
    -> Webhook receiver (FastAPI)
    -> Detection engine (rolling window: message rate, native/7TV emotes, laugh patterns)
    -> DB (SQLite): stored moments (channel, timestamp, stream offset, chat snippet, score)
    -> Web dashboard + CLI for managing the watchlist and reviewing captured moments
```

Phase 2 (later, optional): a worker that keeps a rolling buffer of the stream (ffmpeg +
the unofficial `kick.com/api/v2/channels/{slug}` playback URL) and cuts an actual video
clip out of it when a moment is detected.

## Stack

- Python
- FastAPI (webhook receiver + dashboard)
- SQLite

## Roadmap

- [x] M0 - register Kick dev app, OAuth flow, receive the first webhook
- [x] M1 - streamer watchlist + storing incoming chat messages in the DB
- [x] M2 - detection heuristic (messages/sec spike) + storing moments with a timestamp
- [x] M3 - add emote/keyword detection, tune thresholds
- [x] M4 - simple dashboard for reviewing captured moments
- [ ] M5 - (optional) automatic video clips via m3u8 capture
