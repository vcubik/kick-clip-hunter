# kick-clip-hunter

A bot that watches a list of [Kick](https://kick.com) streamers and detects potentially
viral/funny moments based on chat activity. Detected moments are first saved as a
timestamp + VOD link, with automatic clip cutting planned as a later phase.

## Project status

Early planning / M0. No code yet, just the repository scaffold.

## Architecture (plan)

```
Watchlist (streamers)
    -> Kick OAuth app + webhook subscription (chat.message.sent, livestream metadata)
    -> Webhook receiver (FastAPI)
    -> Detection engine (rolling window: messages/sec, emote/keyword frequency)
    -> DB (SQLite): stored moments (channel, timestamp, VOD link, chat snippet, score)
    -> Dashboard/CLI for managing the watchlist and reviewing captured moments
```

Phase 2 (later, optional): a worker that keeps a rolling buffer of the stream (ffmpeg +
the unofficial `kick.com/api/v2/channels/{slug}` playback URL) and cuts an actual video
clip out of it when a moment is detected.

## Stack

- Python
- FastAPI (webhook receiver, later dashboard)
- SQLite

## Roadmap

- [ ] M0 - register Kick dev app, OAuth flow, receive the first webhook
- [ ] M1 - streamer watchlist + storing incoming chat messages in the DB
- [ ] M2 - detection heuristic (messages/sec spike) + storing moments with a timestamp
- [ ] M3 - add emote/keyword detection, tune thresholds
- [ ] M4 - simple dashboard for reviewing captured moments
- [ ] M5 - (optional) automatic video clips via m3u8 capture
