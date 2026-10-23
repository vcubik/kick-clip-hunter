# kick-clip-hunter

[![tests](https://github.com/vcubik/kick-clip-hunter/actions/workflows/tests.yml/badge.svg)](https://github.com/vcubik/kick-clip-hunter/actions/workflows/tests.yml)

Watches a list of [Kick](https://kick.com), [Twitch](https://www.twitch.tv) and
[YouTube](https://www.youtube.com) streamers, notices when chat reacts to something, and cuts a
clip of that moment from a rolling recording of the stream - then puts it on a small dashboard to
be reviewed and rated. Whole streams can be recorded with their chat too, those of
[TikTok](https://www.tiktok.com) streamers among them.

```
Kick chat ────── signed webhooks ──┐
                 (official API)    │
Twitch chat ──── EventSub ─────────┼─> detector ─────────────────> moment ─┐
                 WebSocket         │   per-channel rolling baseline        ├─> clip + context ──> dashboard
YouTube chat ─── read while live ──┘                                       │   (ffmpeg)           review, rate, tag
Kick, Twitch, ── HLS segments ───────> recorder ──> rolling buffer ────────┘
YouTube stream
```

## What it does

- **Detects moments from chat.** Laughs, laugh emotes and a channel's own 7TV emotes are measured
  against that channel's last five minutes, so a quiet stream and a busy one each get their own
  bar. A moment needs a crowd - enough *distinct* people, scaled to how many are chatting - and the
  things that look like a reaction but aren't (one account spamming, a poll, a raid, everyone
  dancing to a song) are filtered out. [`detector.py`](src/kick_clip_hunter/detector.py) explains
  the rules.
- **Watches Kick, Twitch and YouTube alike.** A channel of any of them is added by its name or a
  link to it, and from then on goes through the same detector, recorder and dashboard: Twitch's
  chat with its own emotes and those of 7TV, BetterTTV and FrankerFaceZ, Kick's with its own and
  7TV's, YouTube's with its emoji - read while the channel is live.
- **Records what it watches.** Every live channel on the watchlist is recorded into a rolling
  buffer a few minutes deep - as far back as the channel's clip settings need - by downloading
  the stream's own HLS segments, which keeps working through ad
  breaks and other mid-stream discontinuities. A clip whose footage an ad covered is cut from the
  stream's own VOD instead, where the channel keeps one.
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
  out - start and end set on the same timeline, or typed as times - without re-encoding. Its chat
  can go with it, moved against the picture if it does not sit right: drawn into the picture in a
  corner (the stretch is then encoded anew, on the graphics card where there is one), or as a video
  of its own - transparent to lay over it, or on a colour to put beside it. A stretch of a recorded
  stream is cut out the same way, half an hour with its chat in it as readily as a clip. A clip's
  file that ends too soon, or begins too late, gets more of the stream before or after it at the
  press of a button, from the stream's own VOD - days later too on Kick and Twitch, and on YouTube
  while the stream is still on - found in it by its pictures and joined without re-encoding.
- **Writes down what is said, as subtitles.** A clip's transcript is made on request, on the
  machine: what is said in it and in the footage around it, line by line with its times, beside the
  clip to be put right while it plays. It is downloaded as subtitles (.srt) that line up with the
  trimmed video - or with the whole file - in any editor.
- **Records a whole stream when asked.** A channel's stream can be recorded whole from the
  Channels page - tracked for clips or not, started ahead of the stream or in the middle of it -
  until it is stopped or the stream is over. It ends up as one video file, and the Recordings page
  plays it with the chat that went with it replayed beside it. Under the video is what chat did
  over the whole stream - hours on one timeline, with the moments that were found marked on it -
  which is how to find one's way about it. A channel can also be set to be recorded always: every
  stream of it is then recorded from when it goes live.
- **Records TikTok streams.** A TikTok channel is added like any other, to be recorded and for
  nothing else: no clips are looked for in it. Its stream, which TikTok serves as one FLV download
  rather than a playlist, is cut into segments of its own as it arrives and kept in the quality the
  streamer sends; its chat is read while it is recorded and replayed beside the recording like any
  other.
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
  around it. A channel can also be watched for its chat alone: every message of it is kept for
  these figures, and no clip is looked for in it nor any of its video kept.
- **Talks in chat, where it is asked to.** A channel can have the bot write in its chat - when
  a recording of its stream starts and ends, and, if wanted, when a clip of it was made - and
  take commands there from its streamer and moderators: start a recording, stop it, say how
  things stand (whether it looks for clips and how many it made of the stream, whether the
  stream is being recorded and for how long). The commands are whatever messages the channel's
  settings say, and nothing of it happens on a channel until its settings switch it on. It
  writes on Kick as an account signed in for it from the dashboard, on Twitch as the account
  chat is read as; on YouTube its commands work, but it cannot answer.
- **Says when something it depends on is gone.** Chat stops arriving the same way whether a
  stream is over, Kick has dropped its chat subscriptions or the tunnel is down. The Health page
  tells them apart: when the last webhook arrived, what the last check of the subscriptions found
  and when Kick was found to have dropped some, how Twitch's chat connection stands and as which
  account it reads, whose YouTube and TikTok chat comes in, as whom the bot writes and where it
  cannot, what each recorder is doing, how much room the disk has, where the tunnel stands. Every
  page says beside its name when one of them needs a look.
- **Tells you what happens, wherever you are.** Where the Settings page (or `.env`) names a
  Telegram bot or a Discord channel's webhook - or both - a short note goes there when a clip is saved (whose, how long, how
  far into the stream, what chat said most, and the clip itself - half a minute of it, without the
  footage around it - where it fits), when a
  recording of a whole stream starts and when it is saved or could not be, and when something on
  the Health page has needed a look for a couple of minutes, and again once it looks fine. Each
  kind is switched on the same page; the Health page says where they go and sends a test. With
  the dashboard's address on the tailnet set there, every note links to what it is about.
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
6. Optional, for Twitch channels: register an application at
   [dev.twitch.tv/console](https://dev.twitch.tv/console) (client type Confidential; the OAuth
   redirect URL the form asks for is not used - `http://localhost` will do) and put its
   `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` in `.env`. Twitch's chat is read on behalf of an
   account - any will do, one kept for this is best - which signs in once with a code shown by:
   ```
   PYTHONPATH=src python scripts/twitch_login.py
   ```
   Nothing has to reach the service from outside for Twitch: its chat comes over a connection the
   service opens itself. The same account is what the bot writes in Twitch's chat as, which the
   sign-in asks it to allow; one signed in before the bot could write has to sign in again for it to.
7. Optional, for the bot to write in Kick's chat: in the Kick app, set the redirect URL to
   `http://localhost:8000/bot/kick/signed_in` (or put another host and port in `.env` as
   `KICK_REDIRECT_URI`, keeping the path) and allow it the scopes `chat:write` (sending chat
   messages) and `user:read` (reading who the account is). Then, with the service running, sign the account the
   bot is to write as in from the dashboard - "Sign one in" beside a Kick channel's bot settings,
   or on the Health page - in a browser on the same machine (a private window, if it is signed in
   to Kick as someone else). Kick shows its own consent page; the service never sees a password.

8. Optional, for notifications: everything below can be set on the dashboard instead, with the
   service running - Settings, All channels, tab Notifications, where "Find chats" lists the chats
   that wrote to the bot - and holds from the next notification on. In `.env`: make a Telegram
   bot with [@BotFather](https://t.me/BotFather), put
   its token in `.env` as `TELEGRAM_BOT_TOKEN`, write anything to the bot, then run
   ```
   PYTHONPATH=src python scripts/telegram_chats.py
   ```
   and put the id it lists for your chat in `.env` as `TELEGRAM_CHAT_ID`. For Discord, make a
   webhook in a channel's settings (Integrations, Webhooks) and put its address in `.env` as
   `DISCORD_WEBHOOK_URL`. For the notes to link to the dashboard from a phone, put the address it is
   reached at on the tailnet in `.env` as `DASHBOARD_URL` (e.g.
   `http://<machine>.<tailnet>.ts.net:8000`). Telegram takes a video of up to 50 MB from a bot;
   with a [Bot API server of your own](https://github.com/tdlib/telegram-bot-api) running in its
   `--local` mode, put its address in `.env` as `TELEGRAM_API_URL` (e.g. `http://localhost:8081`)
   and it takes up to 2000 MB.

YouTube needs nothing set up: its channels are looked up, and their chat read, the way the site's
own pages do it, as a visitor who is not signed in.

TikTok needs nothing set up either. Its channels and streams are looked up the way the site's own
pages do it; its chat is read with [TikTokLive](https://github.com/isaackogan/TikTokLive), which
has the request that opens a chat signed by the [Euler Stream](https://www.eulerstream.com) sign
server - a few thousand a day without a key, more with an `EULER_STREAM_API_KEY` in `.env`.

## Usage

Run the server:

```
PYTHONPATH=src python -m uvicorn kick_clip_hunter.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Open the dashboard at `http://localhost:8000/dashboard`. Channels can be added on its Channels
page, or from the command line - by a Kick channel's name, a link to a Kick, Twitch, YouTube or
TikTok channel (or to a YouTube video of one), or a Twitch channel's name followed by `@twitch`, a
YouTube channel's handle followed by `@youtube` or a TikTok channel's followed by `@tiktok`:

```
PYTHONPATH=src python scripts/subscribe.py <channel>
```

Other scripts (`PYTHONPATH=src python scripts/<name>.py`):

| Script | What it does |
|---|---|
| `refresh_emotes.py <channel>` | Re-fetch a watched channel's emotes - 7TV's, and on Twitch BetterTTV's and FrankerFaceZ's: what the detector listens for and the pictures chat is drawn with. (A YouTube channel has none: its emoji come with every message.) |
| `twitch_login.py` | Sign in the Twitch account the service reads chat as, and the bot writes in it as - once, and again if the Health page says the sign-in has lapsed or the account has not let the bot write. |
| `telegram_chats.py` | List the chats that wrote to the Telegram bot notifications go through, with their ids - which one goes in `TELEGRAM_CHAT_ID`. |
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

About 3800 tests cover the pipeline from a signed webhook, or a Twitch or YouTube chat message,
to a clip on the dashboard. They need no Kick or Twitch credentials, no network and none of the ML
models, and CI runs them on Windows and Linux on every push. [docs/testing.md](docs/testing.md) describes how the suite is organised, when to run
what, and the short manual checklist for the parts that only exist against the real Kick.

## How it is put together

| Module (`src/kick_clip_hunter/`) | Role |
|---|---|
| `main.py` | FastAPI app: chat taken in from either platform, moment sessions, dashboard and its controls. |
| `platforms.py`, `streams.py`, `chat_message.py` | Which platform a channel is on and how its name says so; what is asked of a channel's platform (is it live, where is its stream, where is the VOD of a stream of it); one chat message as the service takes it in, whichever platform it came from. |
| `dashboard_view.py`, `chat_trace.py`, `templates/`, `static/` | The dashboard's pages: what they say, how chat is drawn against a clip and over a whole recording, their markup, stylesheet and script. |
| `chat_stats.py` | A channel's stored chat in figures, for the Analyze page: one pass over a period's messages, and the figures, chart columns and table rows made of it. |
| `detector.py` | The detection heuristic - a rolling window per channel. |
| `tuning.py`, `replay.py` | What of detection and of clips can be set from the dashboard, for all channels or for one; and stored chat replayed through the detector, to try settings out before they are saved. |
| `recorder.py`, `recording_manager.py` | Per-channel HLS recording into the rolling buffer; clip and context cutting; which channels are recorded right now, and why. |
| `stream_recording.py` | A stream kept whole: what is written while it is recorded, how that becomes one video file, and how a second of the file is tied back to when it was on air - which is what places chat against it. |
| `recording_moments.py` | Moments looked for in a recorded stream afterwards: which chat to replay, where in the recording's file a replayed moment's clip lies, and which of them are moments already. |
| `kick_client.py`, `kick_chat.py`, `seventv_client.py`, `webhook_security.py` | Kick's public API, its webhook's chat message, 7TV's emote API, webhook signature verification. |
| `chat_bot.py`, `kick_auth.py`, `account_tokens.py` | The bot in chat: what it says and how it writes on each platform; the Kick account it writes as, signed in with Kick's OAuth 2.1 and PKCE; the tokens of any account the service acts as, kept and renewed. |
| `twitch_client.py`, `twitch_auth.py`, `twitch_chat.py`, `twitch_stream.py`, `twitch_emotes.py` | Twitch: its API (Helix); the account chat is read as; its chat over an EventSub WebSocket; where a live stream and its VOD are (through streamlink); a channel's BetterTTV and FrankerFaceZ emotes. |
| `youtube_client.py`, `youtube_chat.py`, `youtube_stream.py`, `streamlink_support.py` | YouTube: the site's own API - who a channel is, whether it is live, its chat; the chat of a live channel, looked at every few seconds; where a live stream is (through streamlink, imported once for Twitch and YouTube). |
| `tiktok_client.py`, `tiktok_stream.py`, `tiktok_chat.py`, `flv.py` | TikTok: the site's own API - who a channel is, whether it is live, where its stream is; the chat of a channel whose stream is recorded, through TikTokLive; an FLV stream read as it arrives and cut into pieces the recorder keeps as MPEG-TS segments. |
| `kick_stream.py`, `clip_creator.py` | The parts that need a browser: finding a live stream's playable URL, publishing an official clip - and, without one, where a Kick stream's VOD is. |
| `vod_footage.py` | A moment's file made longer from its stream's VOD: the file found in the VOD by its pictures, the VOD's own footage joined without re-encoding. |
| `clip_trim.py`, `chat_video.py` | For an editor: a stretch of a moment's footage, or of a recorded stream, cut out as a video of its own, with its chat drawn into the picture or as a video of its own (transparent ProRes 4444, or H.264 on a colour). The chat is laid out once by a headless browser and every frame is put together from that layout. |
| `notify.py` | Notifications: what is told of a clip, a recording or a part of the Health page that needs a look, and how it goes to a Telegram chat (the Bot API) and a Discord channel (its webhook). |
| `health.py`, `tunnel.py` | The Health page: what the service notices of the things it depends on put into words, and where the tunnel Kick reaches it through stands. |
| `db.py` | SQLite schema, in-place upgrades and queries. |
| `subtitles.py` | A clip's transcript as subtitles: lines a subtitle long made of the words the speech model heard, put right on the review page, written out as SubRip timed to the video they go with. |
| `transcriber.py`, `audio_events.py`, `sound_events.py`, `frame_encoder.py` | The optional per-clip analysis steps - and the speech model a transcript is made with. |

[CLAUDE.md](CLAUDE.md) is the short guide for development; the detailed notes are in `docs/`:
[architecture](docs/architecture.md), [the dashboard](docs/dashboard.md), [how video is got from
each platform and why](docs/platforms.md), [the operational quirks found along the
way](docs/operations.md), [setup](docs/setup.md) and [testing](docs/testing.md).

The dashboard is set in [Archivo](https://github.com/Omnibus-Type/Archivo), bundled under the SIL
Open Font License ([`OFL.txt`](src/kick_clip_hunter/static/fonts/OFL.txt)).

## Limitations

- Kick's public API covers chat events but has no stream playback or clip endpoints. Recording
  therefore uses the stream URL the site's own player requests, captured through an automated
  browser session - unofficial behaviour that can stop working whenever the site changes.
- Twitch's API has no stream playback either: where a stream is comes from streamlink, which asks
  Twitch's playback service the way the site's own player does. When Twitch changes that,
  recording Twitch streams waits for a streamlink release that follows.
- Twitch lets an application read chat only on behalf of an account, which can read up to a hundred
  channels at once.
- YouTube is asked the way its own pages ask, through an API it does not publish: no key and no
  daily quota, but it can change without notice, and YouTube may take a machine that asks a lot for
  a robot. Its chat is looked at every few seconds, so a message arrives a second or few after it
  was written, and what is written while the service cannot look is not read. Where a stream is
  comes from streamlink, as for Twitch.
- TikTok has no API for live streams. Whether a channel is live and where its stream is come from
  an API of the site's own pages, which can change without notice; its chat can only be read
  through a request signed the way TikTok's own pages sign theirs, which is left to a third party,
  Euler Stream - which learns which streams are read, and from where. TikTok channels are
  recorded only, never looked for clips in. The service watches as a visitor who is not signed
  in, so a stream TikTok shows only to signed-in viewers (one for adults, or for subscribers) is
  likely out of its reach.
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
