# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

kick-clip-hunter watches a list of Kick.com streamers and detects potentially viral/funny
moments from chat activity (message-rate spikes, emote/keyword frequency). Detected
moments are stored as a timestamp + VOD link; automatic clip cutting is a later phase.
See [README.md](README.md) for the full roadmap.

## Current status

Early planning stage — no application code exists yet, only the repo scaffold
(`README.md`, `.gitignore`). There are no build/lint/test commands to run yet. Once
code lands, this file should be updated with the actual commands (e.g. `pytest`,
`ruff`, how to run the FastAPI app).

## Language convention

All repo artifacts — commit messages, code, code comments, docs, issues/PRs — must be
written in English, regardless of what language the conversation with the user is in.

## Planned architecture

The system is a pipeline, not a monolith — keep these stages as separate, independently
testable components rather than merging them:

```
Watchlist (streamers)
    -> Kick OAuth app + webhook subscription (chat.message.sent, livestream metadata)
    -> Webhook receiver (FastAPI)
    -> Detection engine (rolling window per channel: messages/sec, emote/keyword frequency)
    -> DB (SQLite): stored moments (channel, timestamp, VOD link, chat snippet, score)
    -> Dashboard/CLI for managing the watchlist and reviewing captured moments
```

Key constraints that shape the design:
- Kick chat is consumed via **official webhooks** (`chat.message.sent`), which Kick
  pushes to a publicly reachable HTTPS endpoint — not a client-side WebSocket
  subscription. Local development needs a tunnel (ngrok/Cloudflare Tunnel).
- There is no official Kick API for VOD access or clip creation. Any real video
  capture (Phase 2 / M5) has to go through the unofficial
  `kick.com/api/v2/channels/{slug}` `playback_url` (m3u8) plus ffmpeg, and should stay
  isolated behind its own module since that endpoint is undocumented and may change.
- Stack: Python, FastAPI, SQLite.
