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
- **Lets you tune it.** How big a reaction has to be, what counts as one and how a clip is framed
  around it are set on the dashboard's Settings page - for all channels, and for one channel on its
  own where its chat needs something else. What is in the form can be replayed on a channel's
  stored chat before it is saved, to see how many moments it would have made.
- **Hands the footage to an editor.** From the open moment a stretch of the file can be trimmed
  out - start and end set on the same timeline, or typed as times - without re-encoding, and its
  chat rendered as a transparent video to lay over it, moved against the picture if it does not
  sit right. A stretch of a recorded stream is cut out the same way.
- **Records a whole stream when asked.** A channel's stream can be recorded whole from the
  Channels page - tracked for clips or not, started ahead of the stream or in the middle of it -
  until it is stopped or the stream is over. It ends up as one video file, and the Recordings page
  plays it with the chat that went with it replayed beside it. Under the video is what chat did
  over the whole stream - hours on one timeline, with the moments that were found marked on it -
  which is how to find one's way about it. A channel can also be set to be recorded always: every
  stream of it is then recorded from when it goes live.
- **Finds the moments of a recorded stream afterwards.** The chat stored with a recording can be
  replayed through the detector at any time, under the settings as they are saved then. What would
  have been a moment is listed under the recording and marked on its timeline, each one a click
  away in the video - so a change of settings can be judged on the picture - and any of them can
  be made into a moment like the others, its clip cut out of the recording.
- **Shows what chat did.** The Analyze page turns a channel's stored chat into figures for any
  period - one session, the last day, week or month, or everything: messages over time with the
  laughing and the detected moments marked, the busiest minutes, the most active chatters, the
  most used emotes, what was said and who was addressed most. A chatter can be looked up, with
  everything they wrote and what there is to say of them, and any message read in the chat
  around it.
- **Says when something it depends on is gone.** Chat stops arriving the same way whether a
  stream is over, Kick has dropped its chat subscriptions or the tunnel is down. The Health page
  tells them apart: when the last webhook arrived, what the last check of the subscriptions found
  and when Kick was found to have dropped some, what each recorder is doing, how much room the disk
  has, where the tunnel stands. Every page says beside its name when one of them needs a look.
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
| `replay_chat.py <slug>` | Replay a channel's stored chat through the detector and report how many moments would fire, optionally with detector constants overridden - the way to check a tuning change against a real stream. The Settings page does the same with the values in its form. |
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

About 2800 tests cover the pipeline from a signed webhook to a clip on the dashboard. They need
no Kick credentials, no network and none of the ML models, and CI runs them on Windows and Linux on
every push. [docs/testing.md](docs/testing.md) describes how the suite is organised, when to run
what, and the short manual checklist for the parts that only exist against the real Kick.

## How it is put together

| Module (`src/kick_clip_hunter/`) | Role |
|---|---|
| `main.py` | FastAPI app: webhook receiver, moment sessions, dashboard and its controls. |
| `dashboard_view.py`, `chat_trace.py`, `templates/`, `static/` | The dashboard's pages: what they say, how chat is drawn against a clip and over a whole recording, their markup, stylesheet and script. |
| `chat_stats.py` | A channel's stored chat in figures, for the Analyze page: one pass over a period's messages, and the figures, chart columns and table rows made of it. |
| `detector.py` | The detection heuristic - a rolling window per channel. |
| `tuning.py`, `replay.py` | What of detection and of clips can be set from the dashboard, for all channels or for one; and stored chat replayed through the detector, to try settings out before they are saved. |
| `recorder.py`, `recording_manager.py` | Per-channel HLS recording into the rolling buffer; clip and context cutting; which channels are recorded right now, and why. |
| `stream_recording.py` | A stream kept whole: what is written while it is recorded, how that becomes one video file, and how a second of the file is tied back to when it was on air - which is what places chat against it. |
| `recording_moments.py` | Moments looked for in a recorded stream afterwards: which chat to replay, where in the recording's file a replayed moment's clip lies, and which of them are moments already. |
| `kick_client.py`, `seventv_client.py`, `webhook_security.py` | Kick's public API, 7TV's emote API, webhook signature verification. |
| `kick_stream.py`, `clip_creator.py` | The parts that need a browser: finding a live stream's playable URL, publishing an official clip. |
| `clip_trim.py`, `chat_video.py` | For an editor: a stretch of a moment's footage, or of a recorded stream, cut out as a video of its own, and its chat rendered as a transparent video (ProRes 4444) to lay over it. |
| `health.py`, `tunnel.py` | The Health page: what the service notices of the things it depends on put into words, and where the tunnel Kick reaches it through stands. |
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
  and Slovak); the emote-based signals are language-independent, and which emotes count as
  laughing can be added to per channel.
- Detector state lives in memory, so a channel needs about five minutes of chat after a restart
  before it can fire.
- A recording of a whole stream takes a few gigabytes an hour, and as much again while its file is
  being made. An ad break that replaced the broadcast is a break in the recording: unlike a clip,
  it is not filled in from the stream's VOD.
- A single-user tool: one SQLite file, no authentication on the dashboard. Expose only the webhook
  route to the internet, as the tunnel command above does.

## Status

The pipeline works end to end. The detector's thresholds are still being tuned against real
streams and the ratings collected on the dashboard; a small local model trained on those ratings,
to rank the moments the heuristic finds, is the direction being explored next.
