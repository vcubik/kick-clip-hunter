# kick-clip-hunter

[![tests](https://github.com/vcubik/kick-clip-hunter/actions/workflows/tests.yml/badge.svg)](https://github.com/vcubik/kick-clip-hunter/actions/workflows/tests.yml)

Watches a list of [Kick](https://kick.com) streamers, notices when chat reacts to something, and
cuts a clip of that moment from a rolling recording of the stream - then puts it on a small
dashboard to be reviewed and rated.

```
Kick chat ──── signed webhooks ──> detector ─────────────────> moment ─┐
               (official API)      per-channel rolling baseline        ├─> clip + context ──> dashboard
Kick stream ── HLS segments ─────> recorder ──> 10-minute buffer ──────┘   (ffmpeg)           review, rate, tag
```

## What it does

- **Detects moments from chat.** Laughs, laugh emotes and a channel's own 7TV emotes are measured
  against that channel's last five minutes, so a quiet stream and a busy one each get their own
  bar. A moment needs a crowd - enough *distinct* people, scaled to how many are chatting - and the
  things that look like a reaction but aren't (one account spamming, a poll, a raid, everyone
  dancing to a song) are filtered out. [`detector.py`](src/kick_clip_hunter/detector.py) explains
  the rules.
- **Records what it watches.** Every live channel on the watchlist is recorded into a rolling
  ten-minute buffer by downloading the stream's own HLS segments, which keeps working through ad
  breaks and other mid-stream discontinuities.
- **Cuts the clip.** When a moment fires, a clip of about 35 seconds is cut around it - longer if
  new people keep joining the reaction - into one video file that also holds the 30 seconds before
  it and the 60 seconds after, as context.
- **Lets you review.** The dashboard queues the moments that are not rated yet and opens one at a
  time: the clip, a trace of what chat did around it - which doubles as the scrubber - the chat
  itself replayed in step with the picture, and a 1-5 rating, tags and a note, all reachable from
  the keyboard. Those ratings are what detector tuning is checked against.
- **Optionally analyses clips locally.** Speech-to-text, audio events, sound events and frame
  embeddings can be switched on per step, to collect features for a future learned model. All of
  it runs on CPU, nothing leaves the machine, and nothing is loaded unless a step is switched on.

## Setup

Python 3.12 and a full `ffmpeg` build on `PATH` (e.g. `winget install Gyan.FFmpeg`). Developed and
run on Windows; the test suite also runs on Linux.

1. Install the dependencies. For the service without the optional clip analysis:
   ```
   pip install -r requirements-core.txt
   ```
   For everything (`requirements.txt`), install a CPU-only PyTorch build first, or pip pulls in the
   multi-gigabyte CUDA one:
   ```
   pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements.txt
   ```
2. Create a Kick app at [kick.com/settings/developer](https://kick.com/settings/developer)
   (needs 2FA enabled on your account) and enable webhooks on it.
3. Copy `.env.example` to `.env` and fill in `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET`.
4. Kick pushes chat as webhooks, so the receiver needs a public HTTPS URL. Expose the webhook
   route with a tunnel and set that URL + `/webhooks/kick` as the app's webhook URL.
   [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) gives a stable URL for free, so this
   only needs doing once:
   ```
   tailscale funnel --bg --set-path /webhooks/kick http://127.0.0.1:8000/webhooks/kick
   ```
5. Log in to Kick once in the browser profile the browser-driven parts use (a window opens; the
   script never sees your password):
   ```
   PYTHONPATH=src python scripts/kick_login.py
   ```

## Usage

Run the server:

```
PYTHONPATH=src python -m uvicorn kick_clip_hunter.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Open the dashboard at `http://localhost:8000/dashboard`. Channels can be added on its Channels
page, or from the command line:

```
PYTHONPATH=src python scripts/subscribe.py <channel_slug>
```

Other scripts (`PYTHONPATH=src python scripts/<name>.py`):

| Script | What it does |
|---|---|
| `refresh_emotes.py <slug>` | Re-fetch a watched channel's 7TV emotes: what the detector listens for and the pictures chat is drawn with. |
| `list_watchlist.py`, `list_moments.py` | Inspect the database from the terminal. |
| `replay_chat.py <slug>` | Replay a channel's stored chat through the detector and report how many moments would fire, optionally with detector constants overridden - the way to check a tuning change against a real stream. |
| `import_clip.py`, `import_clips_dir.py` | Import clips from elsewhere as moments, to rate them as reference data. |
| `join_context_clips.py` | One-off: join older moments' separate context clips into the clip's file. |
| `backfill_taste.py` | Run the clip analysis steps for clips that are missing them. |
| `create_clip.py <slug>` | Publish an official Kick clip of a channel's current stream. |
| `check.py` | Lint, test formatting and the test suite - what CI runs. |

## Testing

```
python -m pip install -r requirements-dev.txt
python -m pytest
```

More than 700 tests cover the pipeline from a signed webhook to a clip on the dashboard. They need
no Kick credentials, no network and none of the ML models, and CI runs them on Windows and Linux on
every push. [docs/testing.md](docs/testing.md) describes how the suite is organised, when to run
what, and the short manual checklist for the parts that only exist against the real Kick.

## How it is put together

| Module (`src/kick_clip_hunter/`) | Role |
|---|---|
| `main.py` | FastAPI app: webhook receiver, moment sessions, dashboard and its controls. |
| `dashboard_view.py`, `chat_trace.py`, `templates/`, `static/` | The dashboard's pages: what they say, how chat is drawn against a clip, their markup, stylesheet and script. |
| `detector.py` | The detection heuristic - a rolling window per channel. |
| `recorder.py`, `recording_manager.py` | Per-channel HLS recording into the rolling buffer; clip and context cutting. |
| `kick_client.py`, `seventv_client.py`, `webhook_security.py` | Kick's public API, 7TV's emote API, webhook signature verification. |
| `kick_stream.py`, `clip_creator.py` | The parts that need a browser: finding a live stream's playable URL, publishing an official clip. |
| `db.py` | SQLite schema, in-place upgrades and queries. |
| `transcriber.py`, `audio_events.py`, `sound_events.py`, `frame_encoder.py` | The optional per-clip analysis steps. |

[CLAUDE.md](CLAUDE.md) holds the detailed development notes: design decisions, what had to be
worked out about Kick's behaviour and why, and the operational quirks found along the way.

The dashboard is set in [Archivo](https://github.com/Omnibus-Type/Archivo), bundled under the SIL
Open Font License ([`OFL.txt`](src/kick_clip_hunter/static/fonts/OFL.txt)).

## Limitations

- Kick's public API covers chat events but has no stream playback or clip endpoints. Recording
  therefore uses the stream URL the site's own player requests, captured through an automated
  browser session - unofficial behaviour that can stop working whenever the site changes.
- The laugh patterns are tuned for the chat conventions of the channels this was built on (Czech
  and Slovak); the emote-based signals are language-independent.
- Detector state lives in memory, so a channel needs about five minutes of chat after a restart
  before it can fire.
- A single-user tool: one SQLite file, no authentication on the dashboard. Expose only the webhook
  route to the internet, as the tunnel command above does.

## Status

The pipeline works end to end. The detector's thresholds are still being tuned against real
streams and the ratings collected on the dashboard; a small local model trained on those ratings,
to rank the moments the heuristic finds, is the direction being explored next.
