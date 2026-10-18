# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Current status

Core pipeline is working end-to-end: webhook ingestion, SQLite storage, the detection
heuristic, automated clip recording, and a web dashboard. The detector is still being
actively tuned against real streams — expect its thresholds/weights to keep changing.
The constants in the code are what is recommended; what a channel really runs on can be
set from the dashboard's Settings page (see `tuning.py` below).

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
  https://kick.com/settings/developer - and, optionally, `TWITCH_CLIENT_ID`,
  `TWITCH_CLIENT_SECRET` of a Twitch application (https://dev.twitch.tv/console, client
  type Confidential - the redirect URL its form asks for is never used). Without them no
  Twitch channel can be watched: adding one says what is missing, and a Twitch channel
  already on the watchlist is trouble on the Health page
- One-time sign-in of the Twitch account chat is read as (only with Twitch set up):
  `PYTHONPATH=src python scripts/twitch_login.py` - shows a code to confirm on
  twitch.tv/activate (Twitch's device flow) and keeps the tokens in
  `data/twitch_token.json`, where the running service picks them up. See
  `twitch_auth.py` below for why an account is needed at all. The bot writes in Twitch's
  chat as the same account, which the sign-in asks for (`user:write:chat`) - one signed in
  before the bot could write reads as it did, and has to sign in again for the bot to write
- For the bot to write in Kick's chat (see `chat_bot.py` below), an account signs in for it
  from the dashboard - "Sign one in" beside a Kick channel's bot settings, or the Health
  page's Chat bot part - through Kick's OAuth 2.1 flow with PKCE (`kick_auth.py`); the
  tokens are kept in `data/kick_bot_token.json`. The Kick app has to have the scopes
  `chat:write` and `user:read`, and as its redirect URL exactly
  `http://localhost:8000/bot/kick/signed_in` - or what `KICK_REDIRECT_URI` in `.env` says,
  whose path stays `/bot/kick/signed_in`. Kick sends the browser back there, so it has to be
  a browser on the machine the service runs on (a private window, to sign in another
  account than the one the browser is signed in to Kick as)
- YouTube needs nothing set up: a channel is looked up, and its chat read while it is
  live, through YouTube's own web API (InnerTube), as a visitor who is not signed in -
  see `youtube_client.py` below for why not the official API
- TikTok needs nothing set up either: a channel, whether it is live and where its stream
  is are asked of TikTok's own web API (`tiktok_client.py`), and the chat of a TikTok
  stream that is being recorded is read through TikTokLive and the Euler Stream sign
  server (`tiktok_chat.py`) - a few thousand signed connections a day without a key; an
  optional `EULER_STREAM_API_KEY` in `.env` raises that. TikTokLive came with the
  user's explicit consent to it and to Euler Stream (2026-10-15)
- Notifications (optional, see `notify.py` below) go where `.env` says: `TELEGRAM_BOT_TOKEN`
  (a bot made with @BotFather) and `TELEGRAM_CHAT_ID` (a chat that has written to the bot -
  `PYTHONPATH=src python scripts/telegram_chats.py` lists them, with their ids), and/or
  `DISCORD_WEBHOOK_URL` (a channel's webhook: its settings, Integrations, Webhooks). Their
  links lead to the dashboard where `DASHBOARD_URL` says where it is reached from - from a
  phone, the machine's MagicDNS name on the tailnet with the port, e.g.
  `http://<machine>.<tailnet>.ts.net:8000` (the Funnel exposes only the webhook route, so the
  dashboard is reached inside the tailnet). Without any of them nothing is sent anywhere.
  `TELEGRAM_API_URL` points the service at a Bot API server of one's own instead of
  Telegram's ([telegram-bot-api](https://github.com/tdlib/telegram-bot-api) run with
  `--local`, e.g. `http://localhost:8081`), which takes videos of up to 2000 MB instead of
  50 MB - it needs an `api_id`/`api_hash` from my.telegram.org, is built from source, and the
  bot is moved to it with `logOut` on Telegram's (see its README); left empty, Telegram's own is asked
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
  - `subscribe.py <channel>` — add a channel to the watchlist: a Kick channel's name, a link
    to a Kick, Twitch, YouTube or TikTok channel (or to a YouTube video of one),
    `name@twitch`, `handle@youtube` or `handle@tiktok` (`platforms.parse`). Fetches and stores its
    emotes - the keywords the detector listens for and the pictures the dashboard draws -
    and for a Kick channel subscribes to `chat.message.sent` on Kick's side unless a
    subscription is already there - so re-running it is safe and repairs a dropped
    subscription. A Twitch channel's chat is subscribed by the running service, and a
    YouTube channel's read by it while the channel is live; a YouTube channel has no emote
    set anywhere (7TV, BetterTTV and FrankerFaceZ know none). A TikTok channel is added
    with its tracking off for good - it is only ever recorded - and its chat read while it is
    recorded
  - `refresh_emotes.py <channel>` — re-fetch and re-classify an already-watched channel's
    emotes (keywords and pictures: 7TV's, and on Twitch BetterTTV's and FrankerFaceZ's)
    and its subscriber badges, without talking to Kick's subscription API at all
  - `twitch_login.py` — one-time sign-in of the Twitch account chat is read as, and the
    bot writes in Twitch's chat as (see above)
  - `telegram_chats.py` — the chats that wrote to the Telegram bot in `TELEGRAM_BOT_TOKEN`
    lately, with their ids: which one goes in `TELEGRAM_CHAT_ID` (see above)
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
    exactly what CI runs; the one command before opening a PR for a larger change
  - `replay_chat.py <slug> [--since ISO] [--until ISO] [--set NAME=VALUE ...]` — replays a
    channel's stored chat through the detector on the messages' own clock and reports how
    many moments would have fired and how long their clips would have been - under the
    settings saved for the channel, and optionally with detector constants overridden for
    comparison. The way to check a threshold change against a real stream before running
    it live; read-only. The Settings page's "Try on stored chat" is the same replay
    (`replay.py`) run on the values in its form.
  - `join_context_clips.py [--dry-run]` — one-off for moments from before a moment had
    one video file: joins each clip with the `_before`/`_after` context clips next to it
    into one file, in the clip's place, without re-encoding, and records where the clip
    lies in it. Until it has been run such a moment plays as its clip alone. Running it
    again does nothing more.
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
  (1-5 rate and move on, J/K move, Space plays, Del and Del again deletes the open
  moment). Under the clip is the chat trace:
  messages a second and how many of them were laughing, on the clip's own time axis, from
  30s before the clip to 60s after it. It is also the scrubber - a timeline over the
  clip's file, which opens with the footage that led up to the clip and runs on into what
  followed; times on it are clip time (minus before the clip, plus after it) - and chat is
  replayed beside it in step with the
  picture, emotes drawn as pictures (Kick's own and the channel's 7TV ones, both loaded
  straight from their CDNs) and each chatter's name in the colour it has on Kick, with
  their badges in front of it (a subscriber's the channel's own picture for it). Where a message belongs against the picture is a guess (to
  start with, the frame broadcast 3s before it arrived), so under the chat is a slider that
  corrects it for the open moment's channel (the Trim dialog starts from that). Trim, next to the clip's controls,
  opens a dialog for cutting a stretch of the footage out for an editor (see `clip_trim.py`
  below); a moment something was downloaded from there is marked in the queue, beside its
  rating (`moments.downloaded_at`). Beside the clip, on a tab next to its chat, is its transcript
  (see `subtitles.py` below): what is said in the footage, written down when "Transcribe" is
  pressed, put right there line by line - the line being said is marked as the clip plays, and a
  line's time plays the clip from there - and downloaded as subtitles (.srt) timed to the
  moment's file, or beside the cut in the Trim dialog. Delete, under the moment's tags, asks once
  more and removes the moment for good (`POST /moments/{id}/delete`): its row and every file
  beside its clip - the cut, its chat video and record, old context clips - but not while a
  stretch of it is being cut or it is being transcribed; one whose clip is still being cut has
  that clip thrown away when it arrives. `GET /dashboard/channels` has the
  watchlist, the per-clip analysis switches, and - while notifications go anywhere - a switch
  for each kind of them (clips, the clips themselves as videos, recordings, problems;
  `app_settings` like the analysis ones; all on to begin with, the videos too), or how to set
  them up where they go nowhere; shutting the service down is the button
  at the right end of every page's bar, which asks once more in a dialog. Beside each channel are its
  Tracking switch, which is about looking for clips and nothing else (a TikTok channel has none:
  "Recorded only"; `POST /channels/{slug}/tracking` refuses to switch it on), and the button that
  starts or stops a recording of its whole stream (see `stream_recording.py` below), with
  how much has been recorded so far - and its "Always record" switch, with which a
  recording is started by itself whenever the channel goes live. Remove, at the end of its
  row, opens a dialog that takes it off the watchlist (`POST /channels/{slug}/delete`): its
  emotes, badges and own settings go, its recorder is stopped, its Kick chat subscription
  removed (a failure is only logged), and chat a platform still sends of it is dropped from
  then on (`db.was_removed`; a channel that was never on the watchlist is still taken in, as
  before). Its moments, recordings and chat stay unless the dialog's box is ticked
  (`everything=1`), which removes them for good too - the chat a chunk at a time
  (`db.REMOVE_CHUNK`). Refused while its stream is being recorded or saved, and with
  `everything` while something is being made of one of its moments or recordings. Added again,
  a channel has its old id again - a Kick channel by its Kick id, any other because the id is
  remembered (`removed_channel:<platform>:<id>` in `app_settings`) and never given to a new
  channel. `GET
  /dashboard/recordings` lists those recordings,
  the newest first and 50 to a page, and plays the open one with its chat replayed beside it - fetched a
  stretch at a time as the video plays on or is moved in, since a recording runs for hours -
  and has the buttons that delete one or retry one that could not be saved. Under the video
  is the chat trace over the whole recording (see `chat_trace.py` below): hours on one
  strip, drawn in steps of several seconds, with the channel's moments and the breaks in
  the file marked on it, and a scrubber like the one under a clip. The page comes without
  it and the script fetches it (`GET /dashboard/recordings/{id}/trace`), because counting
  the chat of a whole stream takes a second or two. Download chat, beside Download, is the
  recording's chat as a video of its own as long as the recording, on the Trim dialog's
  default colour (H.264, tens of megabytes an hour): made the first time it is asked for
  (`POST /recordings/{id}/chat_video`, a tracked background task; the page polls
  `GET /dashboard/recordings/{id}/chat_video` and downloads it once it is there), kept as
  `<recording>_chat.mov` beside the file and removed with it. Trim, beside Download, opens the same
  dialog as on the review page over the recording's file (see `clip_trim.py`). Under those is
  "Moments": "Find moments" replays the recording's chat through the detector under the
  channel's settings as they are saved and lists what would have been a moment (see
  `recording_moments.py` below) - a row's time plays the recording from there, "Make a clip"
  (or "Make clips of all N") makes it a moment with its file cut out of the recording, and one
  that is a moment already links to it on the review page. On every page
  "Recording" means a stream that is being kept whole: a channel whose stream is merely
  coming in for clips is "Live" (the lamp in the bar and the watchlist's "Right now").
  `GET /dashboard/analyze` is a channel's stored chat in figures (see `chat_stats.py`
  below). Whose chat and of which period is set in the pane on the left: a tab (24 hours,
  7 days, 30 days, all), any two times, or one of the sessions the chat came in - the
  newest of which is what the page opens on. Beside it is either the period's overview
  (figures, messages over time with the laughing and the detected moments marked, the
  busiest minutes, the most active chatters, the most used emotes, what was said and who
  was addressed most) or its messages, a page at a time (numbered pages above and under them, as under the review
  queue and the list of recordings - `dashboard_view.pages`), which is where a chatter or a
  text is searched for: a chatter that is found gets figures of their own, and every
  message leads to the chat around it. The page is links and forms and needs no script.
  `GET /dashboard/settings` is where
  detection and clips are tuned: one form of settings (see `tuning.py`), for all channels
  or - chosen in the pane on the left - for one of the watchlist's, which goes by its own
  values once its "Own settings" switch is on. Under that pane its sections are tabs, each
  saying how many of its settings are set to something else; one is shown at a time beside
  them (`?section=`, which the script keeps in the address, and which choosing another
  channel keeps), but the form is saved as a whole, from the foot under every one of them.
  Each setting has a button that puts back what it would be if left alone (what is
  recommended, or for a channel what all channels have). A list - laughing emotes, emotes and
  messages to ignore, the commands - is its items, a rectangle each: one is added from the
  field beside it (Add, or Enter; checked there as the service would) and taken out by its
  cross, and what is typed but not added goes in with the save. The last tab, "Try on stored
  chat", replays a channel's newest chat under the saved settings and under the form's,
  without saving anything. A channel's page begins with what is its own whatever it goes by:
  its clips on command, and "The bot in chat" - whether the bot writes in its chat, and the
  messages that ask it for something - with a sentence on as whom the bot writes on the
  channel's platform. `GET /dashboard/health` says
  whether what the service depends on is there (see `health.py` below): when the last webhook
  arrived and when each channel's chat last spoke, what the last check of Kick's chat
  subscriptions found - with a button that checks now, and the list of the times Kick was found to
  have dropped some - as whom the bot writes in which channels' chat and whether it can (with
  the button that signs its Kick account in), where notifications go and when the last one went
  out (with a button that sends a test, `POST /notifications/test`), what each recorder is
  doing (and which channels renamed themselves on Kick and were followed), how much room the
  disk has and what takes it, and where the tunnel stands. Each part says it in a sentence, in bold when it is trouble, and
  how many of them are trouble stands beside "Health" in the bar of every page. Its script fetches
  the page again every few seconds and swaps in what each part says now. No page reloads on its own; the
  queue announces how many new moments/clips have arrived since the page was loaded
  (polled from `GET /moments/status`), and while a stream recording runs or is being saved
  the Channels and Recordings pages ask what it is doing every couple of seconds (`GET
  /recordings/status`) and their words follow - every ten seconds with nothing being recorded
  but a channel set to be recorded always, whose recording nobody on the page starts. While a
  recording's chat is being replayed for its moments, or their clips are being cut, the
  Recordings page fetches that section again every couple of seconds (`GET
  /dashboard/recordings/{id}/moments`). An empty
  queue is the normal state, so that page then says what the service is doing instead.
- Tests: `pip install -r requirements-dev.txt`, then `python -m pytest` (with the project's
  virtual environment: `.venv\Scripts\python.exe -m pytest`). See "Testing" below.

## Language convention

All repo artifacts — commit messages, code, code comments, docs, issues/PRs — must be
written in English, regardless of what language the conversation with the user is in.

## Architecture

```
Watchlist (streamers, each on its platform - see platforms.py)
    -> Kick: app access token (client credentials, NOT user OAuth - lets the app
       subscribe to any channel's chat without that streamer's consent)
       -> Kick webhook subscription (chat.message.sent) -> FastAPI webhook receiver
    -> Twitch: an account signed in for the service (user:read:chat) -> EventSub
       WebSocket the service holds open, every Twitch channel's chat subscribed on it
    -> YouTube: the site's own web API (InnerTube), no account -> the chat of the stream
       a YouTube channel is streaming, looked at every few seconds while it is live
    -> TikTok (recorded only): TikTokLive + the Euler Stream sign server -> the chat of a
       TikTok channel's room while its stream is being recorded, kept and judged by nothing
    -> each message as a ChatMessage -> _take_chats (stored a batch at a time)
    -> Detection engine (rolling window per channel; see detector.py)
    -> SQLite: streamers, chat_messages, moments, channel_keywords, channel_emotes,
       channel_badges, chat_identities, tuning, stream_recordings
    -> recording_manager ticks a ChannelRecorder per live watched channel (downloading
       the stream's own HLS segments into a rolling buffer - found by a browser on Kick,
       by streamlink on Twitch and YouTube; on TikTok one FLV download, cut into segments of
       its own as it arrives) -> a detected moment cuts a clip from that buffer, path
       stored back on the moment
    -> while a stream recording of the channel runs, the same recorder hands every
       segment on to it as well (stream_recording) -> one video file when it is over
    -> the bot (chat_bot), where a channel has it: a command in its chat starts or stops
       that recording or asks how things stand, and what it says goes back into the
       chat - on Kick through the public API as an account signed in for it, on Twitch
       through Helix as the account chat is read as
    -> notifications (notify), where .env says: a clip saved, a recording started or
       saved or not, a part of the Health page that needs a look and looks fine again -
       to a Telegram chat through the Bot API, to a Discord channel through its webhook
    -> Web dashboard (Jinja2, embeds the clip video) + CLI scripts for reviewing the
       watchlist and moments
```

Module map (`src/kick_clip_hunter/`):
- `platforms.py` — the platforms (Kick, Twitch, YouTube, TikTok) and how a channel's name says
  which one it is on. Pure. A Kick channel is known by its slug, as it always was; a
  channel anywhere else by its name there and the platform's, `login@twitch`,
  `handle@youtube`, `handle@tiktok` (the handle in small letters, without its "@") - so the
  same name on two
  platforms is two channels, and everything kept by name (chat, moments, recorders,
  folders, the chat delay) stays apart without a migration. In the database a channel is
  kept by `streamers.broadcaster_user_id`: a Kick channel's is its Kick user id, any other
  channel's a negative number given out when it is added (`db.add_platform_streamer`),
  which no Kick id can be; its id on its own platform is in `streamers.platform_user_id`
  and its platform in `streamers.platform`. `parse` is what someone typed (a link, a name)
  as a platform and a name - for a link to YouTube that names no handle (a channel's id, an
  older kind of address, a video), the link itself, for YouTube to say whose it is;
  `LiveStatus`, `StreamUrls` (with `expires_at` for addresses that run out, `flv` for a
  stream that is one FLV download rather than a playlist),
  `StreamUrlError` are the shapes in which a platform's answers come back. `RECORDED_ONLY`
  is the platforms whose channels are only ever recorded whole - TikTok's: never tracked
  for clips (`main._is_channel_tracked`, the tracking switch refused, `_take_chats` judging
  none of their chat, whatever is stored)
- `streams.py` — what is asked of a channel's platform, sent to the one its name says:
  whether it is live and since when (`live_status` - Kick's API, Helix, YouTube's own or
  TikTok's own), where its stream is (`stream_urls` - `kick_stream.py`, `twitch_stream.py`,
  `youtube_stream.py` or `tiktok_stream.py`)
- `chat_message.py` — one chat message as the service takes it in, whichever platform it
  came from (`ChatMessage`): its channel, sender, text with the platform's own emotes as
  `[emote:<id>:<name>]` tokens (Twitch's with `twitch.` before the id, YouTube's emoji
  with `youtube.` or `emoji.`), where those stand, the sender's identity, whether they
  run the channel. `kick_chat.py` makes one of Kick's webhook payload,
  `twitch_chat.chat_message` of a Twitch event, `youtube_chat.chat_message` of a YouTube
  chat item; what a chatter typed that would read as a token is kept so that it does not
  (`typed`)
- `kick_client.py` — app access token + channel lookup (by slug, or by broadcaster id -
  `get_channel_by_id`, what a renamed channel is called now; `NoSuchChannel` when Kick has
  none) + event subscription; and, with
  the user access token of the account the bot writes as, who that account is
  (`get_user`) and a message written in a channel's chat (`send_chat_message`,
  `POST /public/v1/chat`, as type "user": the bot writes in other channels' chat as an
  ordinary account - Kick's type "bot" writes only in the token's own channel)
- `kick_auth.py` — the Kick account the bot writes in chat as. Kick's chat is read with no
  account at all, but written only as one, with a user access token (scope `chat:write`;
  `user:read` says whose it is). Signed in from the dashboard with Kick's OAuth 2.1 flow
  with PKCE: `start_sign_in` is where the browser is sent (a challenge of a secret only the
  service knows, a state that the sign-in comes back with, and the redirect URL - with a
  sacrificial `redirect=127.0.0.1` before it where it is on 127.0.0.1, which Kick's page
  would otherwise rewrite), `finish_sign_in` exchanges the code Kick sends back with that
  secret for the account's tokens. Kept in `data/kick_bot_token.json` and renewed as
  `account_tokens.py` does it
- `account_tokens.py` — what is kept of an account the service acts as, on any platform
  (`AccountToken`: its tokens, who it is, what it let the service do - `scopes`, None for
  a token kept before that was): one file, written whole or not at all, and the
  `TokenKeeper` that renews its token before it runs out (one renewal at a time - a
  refresh token works once). The keeper reads the file again whenever it was written
  since it last did, so an account signed in while the service runs is the one it goes
  on with - rather than renewing the old one and writing it back over the new
- `seventv_client.py` — fetches a channel's 7TV emote set (public API), by its id on its
  own platform (7TV knows Kick and Twitch channels): each emote's name
  in that channel, its id and the shape of its picture. Stored twice over, for two uses:
  `channel_keywords` is what the detector listens for (lowercased, filtered, weighted) and
  only changes when asked (`subscribe.py`, `refresh_emotes.py`); `channel_emotes` is what
  the dashboard draws (every emote, exact case - the same word is a different picture in
  another channel) and is also refreshed in the background on every start, together with
  7TV's global emotes (the ones every channel has; a channel's own emote of the same name
  wins over them)
- `webhook_security.py` — verifies Kick's RSA-signed webhook payloads
- `twitch_client.py` — Twitch's API (Helix) with an app access token (client
  credentials, like Kick's): a channel by its name, whether it is live, its recordings;
  and, with the signed-in account's token, an EventSub subscription and a message written
  in a channel's chat (`send_chat_message`, `POST chat/messages` - which can come back
  "not sent", with why). `app_token` raises `NotSetUp` while `.env` has no Twitch
  application
- `twitch_auth.py` — the Twitch account chat is read as, and the bot writes in it as.
  Twitch lets an application read any channel's chat over EventSub only on behalf of an
  account that reads it (scope `user:read:chat`; up to 100 channels at once, the streamers
  are asked nothing), and write in it only as one that let it (`user:write:chat`, which
  the sign-in asks for too). `twitch_login.py` signs it in with the device flow;
  `TokenKeeper` (see `account_tokens.py`) gives the running service a token that does not
  run out for a while yet, renews one about to (each renewal hands out a new refresh token,
  which replaces the old - a refresh token works once and lapses after 30 days unused), and
  says why there is none when there is none. The chat reader and the bot share one keeper
  (`main._twitch_tokens`)
- `twitch_chat.py` — Twitch's chat over an EventSub WebSocket the service holds open
  (`ChatReader`, started by the lifespan while Twitch is set up): nothing has to reach the
  service from outside. Every connection is greeted with a session, and every Twitch
  channel on the watchlist is subscribed to it (the first within ten seconds, or Twitch
  closes it as unused); a channel added later is subscribed on the same session within
  `CHANNELS_SECONDS`. A connection silent past its keepalive is given up and the next
  subscribes everything again (chat sent meanwhile is gone - Twitch keeps none); a
  request to move is followed, keeping the subscriptions; a token Twitch turns down is
  renewed once; a refused or ended subscription is kept with why, and asked for again
  after `REFUSED_SECONDS`. A message of another channel's chat shown during a shared
  chat session is left out. What it saw is kept for the Health page. Chat is handed on
  a batch at a time (`BATCH_SECONDS`)
- `twitch_stream.py` — where a live Twitch stream is: streamlink is asked for the master
  playlist and nothing else (Twitch's API has no playback; streamlink asks Twitch's
  playback service the way the site's player does, over plain HTTP - no browser). Only
  H.264, which Twitch serves as MPEG-TS. Also the stream's VOD, where the channel keeps
  past broadcasts: the recording Helix lists with the live stream's id, its playlist
  from streamlink. Importing streamlink renames the standard log levels in small letters
  for the whole process; this module names them back
- `twitch_emotes.py` — a Twitch channel's emotes beyond Twitch's own: 7TV's, BetterTTV's
  and FrankerFaceZ's together (7TV's wins a name, then BetterTTV's). A BetterTTV or
  FrankerFaceZ emote is kept with its service before its id (`bttv.<id>`, `ffz.<id>`),
  which tells `dashboard_view.typed_emote_image` where its picture is
- `youtube_client.py` — YouTube, asked the way its own web pages ask (InnerTube,
  `youtube.com/youtubei/v1`, as the web client, not signed in, the cookie question
  never answered). The official Data API was set aside: its daily quota (10,000 units, a look at
  a chat 5 of them) would not see one busy chat through a day, and it has no playback at
  all. InnerTube takes no key, but is not published and changes without notice - an
  answer without what is read of it raises `YouTubeChanged` rather than being guessed at,
  and a player asking the visitor to sign in (YouTube's robot check) `Refused`. A channel
  is found from any address that leads to it (`navigation/resolve_url`, a video's
  `player` for a link to a video), its page (`browse`) says its id, title and handle
  (`find_channel`); whether it is live is where its /live address leads, and that video's
  `player` (`live_video`; one only scheduled is not live). Its chat: the chat's page
  (`/live_chat?v=`) says who to ask as and where the view with every message begins - not
  "Top chat", YouTube's pick (`open_chat`) - and each `live_chat/get_live_chat` brings
  what was written since the look before and where to look next, nowhere once it is over
  (`look_at_chat`). The web client's version a page says it is on is gone by from then on
- `youtube_chat.py` — YouTube's chat, read while a channel is live (`ChatReader`, started
  by the lifespan): a task per YouTube channel on the watchlist looks every `LOOK_SECONDS`
  whether it is live, reads its chat until the chat is over, and looks again. A chat is
  looked at again `POLL_SECONDS` after a look - the shortest after one that brought
  messages, longer and longer after ones that brought none - and the messages of a look
  are handed on as one batch. Each of them is taken to have arrived (`received_at`) when
  YouTube says it was sent (`timestampUsec`), not when the look was made - else a look's
  messages would sit against the picture all at one instant: never after the look, never
  before what was handed on already, in the order they came (`arrived`). The detector
  still counts them on the wall clock as they are handed on. The first look at a chat (what was written before the reader
  came) is left out, and so is what is written while it cannot look (a chat opened again
  after failed looks, `RETRY_SECONDS`). A chat turned off is said and tried again after
  `UNAVAILABLE_SECONDS`. YouTube's emoji stand in the text as tokens - a channel's own
  (and YouTube's) as `[emote:youtube.<picture>:<name>]` by their picture on
  yt3.ggpht.com, the ones everyone has as `[emote:emoji.<code points>:<name>]` drawn from
  Google's Noto set - so the detector weighs them by name like any platform's emotes
  ("face_with_tears_of_joy" is laughing); the sender's "@" is dropped; a Super Chat with
  words is a message, what the chat says of members and gifts is not. What it saw is kept
  for the Health page
- `youtube_stream.py` — where a live YouTube stream is: `youtube_client.live_video` says
  which video, streamlink the master playlist of its stream (nothing else is asked of it).
  YouTube's addresses run out after six hours and say when (`expires_at`)
- `streamlink_support.py` — streamlink, imported once for `twitch_stream.py` and
  `youtube_stream.py`: the service starts without it (only those streams then fail, saying
  what to install), and the standard log levels it renames in small letters for the whole
  process are named back
- `tiktok_client.py` — TikTok, asked the way its own web pages ask: `api-live/user/room`
  (by handle, no signature, no sign-in - what streamlink's own TikTok plugin asks) says the
  account (its id, its handle as TikTok writes it, the room its streams are in) and its live
  room: whether it is live (`status` 2), since when, and where its stream is in every
  quality TikTok serves - as FLV for every stream (HLS answers 403 to anyone but TikTok's
  players; DASH only for some). `room` raises ValueError for no such account
  (`statusCode` 19881007) and `TikTokChanged` for an answer without what is read of it;
  `best_stream` is the quality the streamer sends ("origin" - H.264 or HEVC, as they send
  it, mostly 1080x1920 upright) where TikTok serves it, else the best it makes of it. The
  addresses carry `expire` (weeks ahead) and `sign`
- `tiktok_stream.py` — where a live TikTok stream is, for the recorder: the FLV address of
  `tiktok_client.best_stream`, as `StreamUrls(flv=True)`
- `tiktok_chat.py` — TikTok's chat, read only while a TikTok channel's stream is being kept
  whole (`ChatReader`, started by the lifespan; its channels are `main._tiktok_channels`,
  the TikTok recordings that run, looked at every `CHANNELS_SECONDS`). TikTok refuses an
  unsigned request for a room's chat (an empty answer), so it goes through TikTokLive
  (`tiktoklive_chat`, the reader's `connect`), which has Euler Stream sign the request that
  opens the WebSocket - one signed request per connection, a free tier of a few thousand a
  day, `EULER_STREAM_API_KEY` for more (`use_sign_key`) - and does the protobuf itself.
  TikTokLive takes seconds to import, so it is imported in a worker thread the first time a
  chat is read. A recorded channel that is not live is looked at every `LOOK_SECONDS`
  (`tiktok_client.room`, which also gives the room id: TikTokLive is asked nothing else);
  a connection that ended is made again after `AGAIN_SECONDS`, one that failed or lasted
  less than `STEADY_SECONDS` after `RETRY_SECONDS`, growing; what was written before a
  connection is left out. A comment is a ChatMessage under its writer's nickname (as
  TikTok's chat shows them; the handle where there is none) with broadcaster, moderator and
  subscriber badges (`chat_identity.from_tiktok`); emoji as typed; a subscriber's picture
  emotes, gifts, likes and the rest are not taken in. TikTokLive builds on websockets'
  legacy client, whose deprecation warnings the test suite lets through (pyproject.toml)
- `flv.py` — a live FLV stream (TikTok's) read as it arrives and cut into pieces a
  recorder can keep. Pure. `FlvReader` hands back whole tags however the bytes come;
  `Cutter` makes a piece of every stretch from a keyframe to the first keyframe at least
  `PIECE_MS` later, as a small FLV file of its own with the sequence headers in front,
  timed on the stream's clock: onMetaData's `start_time` (epoch ms, TikTok's) plus the
  tag's time, or the arrival of the first frame where there is none; times are counted
  from zero again in each run. A sequence header that changes, or a time that goes back or
  jumps more than `MAX_STEP_MS`, starts a new run; the seconds after the last keyframe
  before a break or the end make no piece. ffmpeg copies each piece into MPEG-TS
  (`recorder._flv_piece_as_ts`, dropping SEI units - `SEI_UNITS`: TikTok's H.264 has one
  after every keyframe's picture, which made the MPEG-TS reader lose the time of the frame
  after every keyframe), and `TsCounters` numbers the copies' continuity counters on so
  that joined they read as one stream
- `db.py` — SQLite schema and queries; schema changes are applied as idempotent
  `ALTER TABLE`s in `get_connection()`, not a migration framework. Chat is stored a batch
  at a time (`insert_chat_messages`, `set_chat_identities`: one commit each, however many
  rows - a commit is about a tenth of a second on this host's spinning disk)
- `detector.py` — the heuristic engine; its module docstring is the source of truth for
  how detection works, don't duplicate that explanation here. Its constants are the
  recommended values; what can differ per channel is passed in as `detector.Settings`
  (`recommended()` is the constants, read when it is called)
- `tuning.py` — what can be set from the Settings page, for all channels or for one. Pure.
  `Tuning` is everything a setting can change for a channel: the detector's settings, how
  long a moment stays open, how its clip is framed. `SETTINGS` is the list on offer - what
  each is called on the page, what it may be, which fields of a `Tuning` it sets (one
  setting can set two: the lull that ends a moment is also the detector's cooldown) - and
  the rest is values on their way in and out: checked as posted (a list as the items it
  holds, never as text to split - so a message can have a comma in it - and a part of an
  emote's name as one word), read back from the
  `tuning` table (JSON by name; owner 0 for all channels, a channel's id for its own), laid
  over each other. Only what differs from what it would be anyway is stored, so an
  untouched setting keeps following the code, and a channel keeps following all channels.
  The moment session's recommended timing lives here too (`MOMENT_SESSION_*`,
  `DYNAMIC_POST_ROLL_SECONDS`), since both `main.py` and the replay need it. Nothing is
  applied to the past: the webhook resolves the channel's settings for every message, and
  an open moment keeps the ones it fired with, down to how its clip is cut. Also here:
  clips on command (`clip_commands`, `clip_command_from_anyone`) - whole chat messages
  ("!clip") that make a moment by themselves when the channel's streamer or a moderator
  posts one (`chat_identity.runs_channel`), or anyone with the switch on. The webhook
  opens the moment without the detector (`reason=command`, its figures zero), so it
  needs no chat history, only a tracked channel; the detector never sees a command, and
  a replay leaves commands out without counting them. And the bot in chat (see
  `chat_bot.py`): whether it writes in the channel's chat (`bot_writes`) and says there
  when a clip was made (`bot_tells_clips`), and the whole messages that ask it to start a
  recording, stop it, or say how things stand (`record_commands`, `stop_commands`,
  `status_commands`; `Tuning.bot_command` says which a message is) - all off, and none,
  to begin with. A message may be in one list of commands at the most (`COMMAND_LISTS`,
  refused in `check`). These seven are a channel's alone (`Setting.per_channel`,
  `PER_CHANNEL`): not on offer for all channels (posted there they are refused, and one
  stored there holds for nobody), in force for a channel whether or not its "Own
  settings" switch is on, and the first two sections of a channel's Settings page - the
  only ones that are not locked while the switch is off. The page's script sends only
  what is not locked, so saving them leaves alone the values a channel keeps for when it
  goes by its own again
- `replay.py` — a channel's stored chat fed through the detector and the moment-session
  rule on the messages' own clock, under a given `Tuning`. Behind `scripts/replay_chat.py`,
  the Settings page's trial (`POST /tuning/trial`, `POST /channels/{slug}/tuning/trial`
  - the newest 10,000 messages, in a worker thread, one trial at a time) and the search
  for moments in a recording (`recording_moments.py`). Each run is a
  channel of its own to the detector, so a replay inside the running service leaves live
  channels' state alone
- `timeutil.py` — UTC-to-local-time display helper
- `main.py` — FastAPI app: webhook receiver + the dashboard's routes + wires moment
  detection to clip extraction. Every chat message, from either platform, goes through
  `_take_chats`: dropped while watching or the channel's tracking is off (unless its
  stream is being recorded whole), stored with its sender's identity, then counted by the
  detector one by one (`_detect`), which may open a moment session. Kick's webhook hands
  it one message at a time, Twitch's reader (`_twitch_reader`, see `twitch_chat.py`),
  YouTube's (`_youtube_reader`, see `youtube_chat.py`) and TikTok's (`_tiktok_reader`, see
  `tiktok_chat.py`; only while a TikTok stream is recorded, and judged by nothing) a batch. A stream recording is started and stopped through `POST
  /channels/{slug}/recording/start` and `/stop`, made into its file by a tracked
  background task, and removed or retried through `POST /recordings/{id}/delete` and
  `/save`; what is being done with one right now is in memory, everything about one that
  is over in its row. A stretch is cut out of one through `/recordings/{id}/trim`, by the
  same background task that cuts one out of a moment's footage (`_trim_background`). `POST /channels/{slug}/always_record` sets whether a channel is
  recorded always (`streamers.always_record`); the recording itself is then started by
  `_record_stream_by_itself`, which the recording loop calls. Settings are saved through `POST /tuning` (all channels),
  `POST /channels/{slug}/tuning` (a channel's own) and `POST
  /channels/{slug}/own_tuning` (whether it goes by them); what is stored is kept in memory
  (`_tuning_values`) and dropped on every save, together with the keyword weights worked
  out from it. The Analyze page (`GET /dashboard/analyze`) is put together by `_analysis`
  on a worker thread, with a connection of its own - a pass over a long period takes
  seconds - which is handed what it needs of the service's cached state (`_ChatWords`)
  rather than reading it from there. The bot (see `chat_bot.py`): `_take_chats` picks out
  the bot's commands among the messages - a channel's own, posted by its streamer or a
  moderator (`_bot_command`) - whatever the switches say, as the buttons on the Channels
  page work whatever they say, and once the messages are stored (not if they could not
  be: the platform delivers them again) has each carried out once (`_carry_out`: a
  recording started or stopped as the buttons do it, or how things stand). The detector
  never sees one. What the bot says goes through `_tell`, which writes in a channel's
  chat in the background where the channel has the bot writing there:
  `_start_stream_recording` and `_end_stream_recording` say so whoever started or ended
  the recording, and `_create_clip_background` says a clip was saved where that is asked
  for too. The bot's Kick account signs in through `GET /bot/kick/sign_in` (to Kick's
  consent page) and `GET /bot/kick/signed_in` (where Kick sends the browser back:
  `signed_in.html` says what came of it); the sign-ins under way are in memory
  (`_kick_sign_ins`, by their state)
- `chat_bot.py` — the bot in chat. What it says, worded for a chat (`started_words`,
  `ended_words`, `status_words`, `clip_words` - in English, like the dashboard), and how it
  writes on each platform (`KickWriter`, `TwitchWriter`; nothing on YouTube, read as a
  visitor who is not signed in, nor on TikTok, read as nobody while a stream is recorded -
  its commands are carried out without an answer there). A writer
  asks with the account's token, and once more with a renewed one if the platform turns it
  down; an account that did not let it write is not asked to (`AccountToken.may`), and
  everything that can't be written is `CannotWrite`, in words for the Health page.
  `ChatBot.say` never raises and holds nothing up; the same words in the same chat are
  not written twice within `REPEAT_SECONDS` (a moderator asking twice gets one answer),
  and what became of the last message in each channel is kept for the Health page
  (`written` - a trouble of the account's own is the account keeper's to say, so it ends
  with the next sign-in). `Recent` is the command messages carried out lately, so that
  one a platform delivers twice is carried out once
- `notify.py` — notifications: what the service tells whoever runs it, in a Telegram chat
  and/or a Discord channel. What a note says, worded for a phone (`clip_note` - how long, how
  far into the stream, what chat said most or what set it off -, `recording_started_note`,
  `recording_saved_note`, `recording_failed_note`, `recording_empty_note`, `health_note`,
  `test_note`), a `Note` (a title, a few lines, the channel, a link to the dashboard by
  `DASHBOARD_URL`, whether it is trouble), and how it is sent: `Telegram` (Bot API, as the
  bot: `sendMessage` in its HTML, `sendVideo` for a clip - 50 MB at most, 2000 MB through a
  Bot API server of one's own that `TELEGRAM_API_URL` names, `api_url`), `Discord` (the
  channel's webhook, `?wait=true`: an embed - author the channel, the title its link, red
  for trouble - and a clip as `files[0]` beside `payload_json`, 20 MB at most on a server
  without boosts). What chat wrote is escaped for each (Telegram's HTML, Discord's markdown)
  and mentions nobody on Discord (`allowed_mentions`). A 429 is waited out once, up to
  `RETRY_AFTER_MAX_SECONDS`; a destination that could not be reached is tried once more
  after `RETRY_SECONDS`; a clip too big for one (`video_max_bytes`, or a 413) goes there as
  words that say so. `Notifier.notify` never raises and holds nothing up: the notes for one
  destination go one at a time in the order they came, and what became of the last is kept
  for the Health page (`sent`, its trouble until the next goes out). A Telegram bot's token
  is in every address it is asked at, so no address is ever put into words - a transport
  error is said by its type alone - and httpx's own request log stays at WARNING (main.py).
  `HealthWatch` decides what of the Health page is told of: a part that has been trouble for
  `after_seconds` on end, once, and again once it is fine; switched off, it forgets. Also the
  set-up's words for the log (`setup_trouble`), and what `scripts/telegram_chats.py` asks
  (`telegram_bot`, `telegram_chats` - getMe, getUpdates). main.py wires it: `_notes()` (from
  `.env`, built when first asked), `_notify` (in the background, where a kind's switch is on),
  `_notify_of_clip` (after a clip's file is saved, from `_create_clip_background`; with the
  clip alone - about half a minute - cut out by `recorder.clip_alone` into a temporary folder
  where "Clip videos" is on, as it is to begin with; not for clips made from a recording by
  hand), the recording notes from
  `_start_stream_recording` (who started it: `notify.DASHBOARD`, `CHAT` and by whom, `ALWAYS`),
  `_end_stream_recording` and `_save_stream_recording`, and `_keep_telling_of_health`, which
  looks at the Health page's judgement every `HEALTH_NOTE_SECONDS` (`_tell_of_health`) and
  tells of a part after `HEALTH_NOTE_AFTER_SECONDS`
- `dashboard_view.py` — what the dashboard's pages say, worked out from stored rows: how
  the queue is grouped by stream, how times, counts and detector reasons are worded.
  Pure functions, so the wording is unit-tested without rendering a page
- `chat_trace.py` — chat set against a clip: which second of the clip a message belongs
  to, the trace under the clip and the spark in a queue row as SVG paths, and what chat
  said most in a moment ("KEKW x31"). Pure, like `dashboard_view.py`. "Clip time" is
  seconds from the clip's first frame; a clip's start is stored on the stream's program
  clock (`moments.clip_start`, with `clip_duration`), and chat is taken to have seen
  each frame `CHAT_DELAY_SECONDS` later - an assumption, since the real figure is each
  viewer's player buffer, so it can be set per channel from
  the review page (`POST /channels/{slug}/chat_delay`, kept in `app_settings`). It is not
  the recorder's `PLAYBACK_DELAY_SECONDS`, which frames clips and is on the long side. It
  starts at 3s - about what Kick's player reports for its latency, with a little room -
  for a YouTube channel at 6s (`YOUTUBE_CHAT_DELAY_SECONDS`, `default_chat_delay`: its
  player runs further behind, under 10s for a stream of low latency by YouTube's account;
  not measured yet) - and can go below 0, which is where a streamer's own on-screen chat lines up. Clips cut before those
  columns existed have their length measured (ffprobe) the first time one is opened and
  are placed by estimate: centred on the window they were cut for. The same trace is
  drawn over a stream that was kept whole, or any stretch of one (`stretch_step`,
  `stretch_counts`, `stretch_trace`): on the seconds of the recording's file, and in
  steps of as many seconds as keep it to 720 of them - 1 for the few minutes the Trim
  dialog shows, 10 for two hours, 30 for six - each step drawn as the pace over it
  (messages a second, averaged), with the grid and the times under it chosen to suit the
  length. `main._recording_strip` is what feeds it: chat placed as beside the recording,
  a message from a break in the stream not counted, the channel's moments and the file's
  breaks marked. It runs in a worker thread, and what it counted over a whole recording
  is kept in memory (`_recording_counts`) for as long as nothing it was counted under
  changes
- `chat_stats.py` — a channel's stored chat in figures, behind the Analyze page. Pure,
  like `chat_trace.py`. One pass over a period's messages (`collect`) counts everything
  the page shows: who wrote, which emotes were used (as the dashboard draws them - Kick's
  own, and the channel's 7TV set as it is *now*, so an emote it has since removed is a
  word again), what was laughing (`detector.is_laughing`, made quick enough for a long
  pass by `laugh_test`), what was said over and over (`said_key`: capitals, a longer row
  of one letter, a word repeated and every length of "xDDD" make no difference) and who
  was addressed with an @. The rest turns the counts into figures, the columns of a chart
  (`Columns`: the finest step between a minute and a week that keeps a chart to 240
  columns, on round times of the local clock) and the rows of tables. A period is from a
  start up to, but not at, an end, and never reaches past now; a session is chat with no
  silence of half an hour in it (`db.get_chat_sessions`), which as a rule is one stream
- `health.py` — what the Health page says, and what it calls trouble. Pure, like
  `dashboard_view.py`: `Facts` in (what the service noticed, and the clock), each part's sentences
  and rows out, and `report` is the page. What is noticed is kept in memory where it happens -
  `main._heard` (deliveries at the webhook receiver, before anything is decided about them, so
  chat that is dropped still counts as arrived), `main._subscriptions` (the last check),
  `main._funnel` (the tunnel, looked at every five minutes and when the page is asked for),
  `recording_manager.looks()` and `recorder_runs()` - and gathered by `main._health_facts`, for
  the page and, without what takes the database or a walk over the disk to know, for the count
  in every page's bar. A check that found subscriptions missing is also kept in the database
  (`app_settings`, key `subscription_drops`, the newest 20), so that a restart does not forget
  it, and so is a Kick channel the watchlist followed to a new name (`channel_renames`, listed
  under the recorders, no trouble). Trouble is kept rare on purpose: one quiet chat among talking ones is said in its row and
  no more, silence counts only while a channel has been live for `CHAT_SILENT_SECONDS` and the
  service up to hear it, a drop that was mended is listed but is no trouble. Twitch's,
  YouTube's and TikTok's chat have a part each, shown only while the service has anything to
  do with them; TikTok's is trouble only when a recorded channel's chat has not been read for
  `TIKTOK_LOST_SECONDS`.
  The bot's part (`bot_part`, from `main._bot_facts`) is there while it writes in a
  channel's chat, an account has signed in for it on Kick, or a Kick channel is watched (it
  has the button the account signs in with); a Kick or Twitch channel it
  cannot write in - no account to write as, one that did not let it, the last message turned
  down - is trouble, a YouTube or TikTok one is not. `bot_account_words` says as whom it
  writes, here and on a channel's Settings page. The notifications' part
  (`notifications_part`, from `main._notification_facts`) is there while they go anywhere:
  where, what they tell of, where their links lead, and when the last went out to each
  destination - one that turned the last down is trouble until the next goes out.
- `tunnel.py` — where the Tailscale Funnel in front of the webhook receiver stands, as the
  machine's own Tailscale client says (`tailscale status --json`, `tailscale funnel status
  --json`): not installed, not connected, nothing forwarding `/webhooks/kick`, served inside
  the tailnet only, forwarding to a port the service does not answer on, or up. `read` is the
  pure part. It is Tailscale's account of its configuration, not a delivery from outside - a
  request to the Funnel's address from this machine would not leave the tailnet - so the proof
  that Kick gets through stays the last webhook, which the page shows beside it. A machine
  without Tailscale is not trouble: the service may be reached another way.
- `chat_identity.py` — who a chatter is in a channel: name colour and badges, which Kick
  sends with every chat message (`sender.identity`), as does Twitch (`color`, `badges`,
  put in Kick's form by `from_twitch`) and YouTube (`authorBadges` - owner, moderator,
  verified, member and for how long - by `from_youtube`; YouTube gives names no colour), and
  TikTok (whether the writer is the streamer, a moderator, a subscriber - by `from_tiktok`;
  no colour either).
  Kept once per chatter per channel
  (`chat_identities`), not per message: the webhook receiver remembers what it last stored
  and writes only when that changes, so what the dashboard shows is the latest known
  state, and chatters not seen since this was added keep the colour worked out from their
  name. Pure, and the place where everything a chatter controls is checked (the colour
  goes into a style attribute, a badge's type into a file name). A colour too dark for
  the page's grey is lightened until it can be read. The badge pictures in
  `static/badges/` are placeholders drawn for this project, one per badge type plus
  `other.svg` for types without one; a subscriber's badge is drawn as the channel's own
  picture for it where the channel has one (see `channel_badges.py`). A Twitch
  subscription above tier 1 is kept with its tier (`tier`), read from the badge's version
- `channel_badges.py` — a channel's own subscriber badges: a picture for subscribers of a
  tier from so many months on (`Picture`; Kick has one tier). Kick lists them with the
  channel on kick.com's own API (`kick_client.get_site_channel`, `/api/v2/channels/<slug>`
  - unlike the channel page, a plain HTTP client can read it), Twitch as the channel's
  `subscriber` badge set (Helix `chat/badges`, app token). Fetched with the emote pictures
  - when a channel is added (a failure leaves it without them, it is still added), on
  every start and by `refresh_emotes.py` - and kept in `channel_badges`; ones that can't
  be fetched stay as they were. `pick` is the picture a subscriber wears: the longest
  subscription they have reached, of their tier (or tier 1's). Pure; only https
  addresses on the platforms' picture hosts are kept, since they go into a `src`. A chat
  video loads them like emote pictures, but an animated one stands still there
- `clip_trim.py` — a stretch of a moment's footage cut out for an editor, from the review
  page's Trim dialog: a player of its own and a copy of the strip with a handle at either
  end, anywhere in the file (the clip to begin with). `POST /moments/{id}/trim` takes the
  stretch in clip time and writes `moment_<id>_trim.mp4` next to the clip, replacing the
  one before; `GET` says where that stands, which is what the dialog polls. This module
  is the pure part - names, limits, clip time to file time; the cut is
  `recorder.trim_clip`. Nothing is re-encoded, so the video begins on the last keyframe at
  or before the start that was set (a couple of seconds earlier at most on a real stream)
  and the dialog says by how much. With "chat" ticked the stretch's chat goes with it
  (`chat_video.py`), where the dialog's select says (`chat_where`, the script remembers the
  last choice): drawn into the cut's picture (`video`) - where its box over the dialog's
  picture was dragged to and as large as it was made by a corner, its width setting how large
  the text is and its height how much of the column shows (`chat_box`, its left, top, width
  and height as shares of the picture, `ChatBox`; the script keeps the last one; without
  one, the bottom right) - the cut is then made together with it, its picture encoded anew,
  so it begins exactly where it was set (state `burning`, with how far it has got). The
  dialog shows the chat in that box as it will be drawn: the lines beside the player are
  moved into it, dressed as in the video and scaled to the box. Or chat goes as a video of
  its own
  (`apart`), lined up with where the video really begins. The dialog's slider moves the
  lines up to 10s either way against the picture, for that one video, on top of the
  channel's chat delay - and shows them beside the picture as they will be. Chat is
  see-through unless a background colour is picked there (`chat_background`, "#rrggbb"):
  in the picture a panel behind it, as a video of its own a video to put next to the
  picture. Its animated emotes always move. The options travel together as a
  `ChatLook` (`chat_look`). A video taken without its chat can have it made afterwards ("Make its chat",
  `POST /moments/{id}/trim/chat`, `/recordings/{id}/trim/chat`, the options as above -
  drawn in, it takes the copied cut's place): for the stretch that video holds, not the
  one set in the dialog now. That needs where the video really begins in the file, which
  nothing can tell from the video afterwards, so it is kept beside it as JSON
  (`record_name`, `cut_record`), with the end that was set and whether its chat is drawn
  into it (`burned`, which makes "Make its chat" refused) - which is also how the dialog
  says which stretch the video that is ready holds. A cut from before that was kept has to
  be exported again. Making chat is given up when the service shuts down, rather than
  holding the shutdown up for minutes.
  A moment's files are downloaded through `GET /moments/{id}/trim/video` and `/chat` - and
  `/subtitles`, its transcript timed to the cut (see `subtitles.py`) - not their address under
  /clips, since that is what marks the moment as taken. What
  state a cut is in is kept in memory while one is being made and told
  from the files otherwise. Either end of the stretch can also be typed into the dialog
  as a time (the script reads it; the server is only ever given seconds). The Recordings
  page has the same dialog over a recording's file (`POST`/`GET /recordings/{id}/trim`,
  times in seconds of the file). What differs there: the strip can't hold hours at a
  width a handle could be set on, so it shows a window of a few minutes around the
  stretch, drawn by the service (the trace endpoint with `first` and `last`) and set
  anew by the script whenever the stretch reaches its edge or leaves it; the stretch
  starts out as the minute around where the recording was being watched; the cut is
  written next to the recording's file and named for the stretch it holds
  (`..._trim_1h23m45s-1h24m30s.mp4`, see `recording_video_name`), one at a time, and is
  removed with the recording; chat as a see-through video of its own can go with a stretch
  of up to `CHAT_VIDEO_MAX_SECONDS` only (drawn into the picture or on a colour, with any);
  and a cut that would leave the disk short of what a running recording needs is refused
- `chat_video.py` — chat as video for the stretch cut out with Trim, made only when asked
  for there. A headless `patchright` browser lays every chat line of the stretch out once,
  one under another in a column 480px wide, and takes pictures of it a screenful
  (`PAGE_PIXELS`) at a time without the background, as the frames come to need them
  (`drawn_column`); each frame is then put together in numpy from those pictures
  (`frame_top`, `frames`): the lines that had arrived, the newest at the bottom from the
  frame it arrives in (the column jumps up to make room, as chat does), the oldest whole
  at the top until the frame's edge would cut it, and then gone (`cut_rows`) - identical frames are the same bytes. That makes half an hour of a busy
  chat a minute or two of work instead of twice its length. Animated emotes, which would
  stand still on whichever frame was drawn, are made to move: the browser
  is handed the emote pictures by the route that serves it (`route.fetch`), and those of
  more than one frame are hidden in the column (`chatVideo.setAside`) and taken apart into
  their frames with Pillow (`Animation`: GIF, WebP, APNG and AVIF, a frame of 10 ms or
  less shown for 100 ms as browsers do), to be drawn into each frame on the one their
  animation is at - every copy of a picture on the same one, running from the video's first
  frame (`_Moving`). Raw RGBA frames go to
  ffmpeg for one of three files: a see-through video of its own (ProRes 4444,
  `..._trim_chat.mov`, large by nature - 3-5 MB per second of a busy chat), one on a colour
  (H.264 in the same `.mov`, a fraction of that), or the cut itself with chat drawn into its
  picture (`burn`, placed by `placement`: in the dialog's box, the text no smaller or larger
  than `SCALE_LEAST` and `SCALE_MOST` of the page's and at least `COLUMN_LEAST` of the column
  shown, within the picture - or, with no box, two thirds of the picture's shorter side tall
  at the bottom right - overlaid while the stretch is re-encoded from the very frame set, with
  `-t` on the output, or the last chat frame would keep the picture going). Frames are of any
  height (`Shape`), and for chat drawn larger than the page has it the browser draws at twice
  the density (`device_scale_factor`), so the text is made smaller rather than larger and
  stays sharp.
  The picture is encoded with the first of `HARDWARE_ENCODERS` that works on the machine
  (AMF on this host's RX 480: 1080p60 at roughly 1.5-2x as fast as it plays), else x264
  (`video_encoder`, found out once and kept; the test suite pins x264), at a little over
  the source's bitrate. The browser is served the page and `static/` from memory under a
  made-up address, may fetch emote pictures from their CDNs and is refused everything
  else; it never opens kick.com, which is why headless works here. `patchright` runs
  `page.evaluate` in an isolated world by default, where the page's own script is not
  visible - hence `isolated_context=False`
- `subtitles.py` — a moment's transcript, as subtitles. Pure. Made only when it is asked for
  ("Transcribe" on the Transcript tab beside the clip: `POST /moments/{id}/subtitles`, a tracked
  background task that is given up when the service shuts down; the tab is `_transcript.html`,
  also served on its own at `GET /dashboard/moments/{id}/subtitles`, which the page fetches every
  couple of seconds while one is made). The speech model hears all of the moment's file - the clip
  and the footage either side of it, since a cut can be taken from anywhere in there - word by
  word with its times (`transcriber.transcribe_words`: at the model's own beam size, not the
  analysis step's first guesses, as a person reads and corrects this one), and `lines` makes lines
  a subtitle long of the words: to the end of a sentence or to a pause, at most
  `LINE_MAX_CHARACTERS` and `LINE_MAX_SECONDS`, broken after a comma where one is far enough in,
  each on screen for at least `LINE_MIN_SECONDS` where the next leaves room. Kept on the moment as
  JSON, in clip time (`moments.subtitles` - not `moments.transcript`, the analysis step's speech of
  the clip alone in one line, for a classifier). A line is put right on the tab (`POST
  /moments/{id}/subtitles/{line}`, as the line is left) and keeps the times it was heard at; an
  emptied one is left out of the subtitles. `srt` writes SubRip for a video whose first frame is
  at a given clip time: the moment's whole file (`GET /moments/{id}/subtitles`,
  `moment_<id>.srt`), or the cut the Trim dialog made of it (`GET /moments/{id}/trim/subtitles`,
  `moment_<id>_trim.srt`, offered beside its video): from where that really begins, which is
  read from its cut record - a cut from before records were kept has none - with a line its ends
  leave less than `CUT_LINE_MIN_SECONDS` of left out. Written as UTF-8 with a byte order mark and
  Windows line ends, which is how editors on Windows read the Czech letters right
- `templates/` — the dashboard's Jinja2 templates: `base.html` (the bar across the top),
  `review.html`, `recordings.html`, `analyze.html`, `channels.html`, `settings.html`, `health.html`, and `_moment.html` - the open moment, which is also
  served on its own (`GET /dashboard/moments/{id}`) so the page can open another moment
  without reloading. `_chat_line.html` is one line of chat, shared by the review page, the
  Recordings page and `chat_video.html`, the page chat is laid out on for a chat video - and, as its
  two halves (who, and what), by the Analyze page's lists and tables;
  `_recording_chat.html` is the box of a recording's chat for one stretch of it, also
  served on its own (`GET /dashboard/recordings/{id}/chat?at=<second>`);
  `_recording_trace.html` is the chat trace over a recording or a stretch of it, only ever
  served on its own; `_recording_moments.html` is what looking for moments in a recording
  has come to, on the page and served on its own; `_trim.html` is the Trim dialog, included
  by the open moment and by the open recording; `_transcript.html` is a moment's transcript
  beside its clip, on the open moment and served on its own; `signed_in.html` is what came
  of signing the bot's Kick account in, where Kick sends the browser back to
- `static/` — `dashboard.css` (design tokens first - every colour, size and space used
  is one of them - then components, then page layout; its opening comment states the
  rules), `dashboard.js` (no dependencies, everything wired by delegation because the
  open moment's markup gets replaced; it also runs the Trim dialog, whose strip and chat
  are copied from the page's when it is opened on a moment and fetched for the window it
  shows of a recording, a moment's transcript - the tab beside the clip, asking for it and
  following it while it is made, the line being said, saving what is put right in a line - the
  Settings form, which it posts as a
  whole and then fetches again (its tabs, which only show and hide its sections, the items of
  its lists, and the section a setting the service refused is in, shown), and stream
  recordings: the buttons, the words that follow
  a recording while there is one, a recording's chat and the trace under it, and the moments
  found in a recording - which it also marks on that trace) and the bundled typeface,
  Archivo, with its licence;
  `chat_video.css`/`chat_video.js` dress the page chat is laid out on for a chat video
  and say where each line of it is
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
  A moment has one file, `moment_<id>.mp4`. The clip itself is kept short (about 35s
  unless the reaction keeps drawing in new people), and the 30s before and 60s after it
  are in the same file on either side of it, as context - so the file is cut only once
  the context after the clip has been broadcast, about a minute later than the clip
  alone could be. `extract_clip` returns the stretch of the broadcast the clip really
  holds (whole segments, so a little more than was asked for) and how much footage the
  file has before and after it; both are stored with the moment (`clip_start`,
  `clip_duration` are the clip's own; `context_before`, `context_after` the footage
  around it, NULL where the file is the clip alone). Everything in a file comes from one
  group of segments, the one the clip is best cut from, so context beyond a break in the
  stream is left out. The analysis steps are given the clip without its context
  (`clip_alone`), cut out of the file for as long as they run. `trim_clip` cuts any
  stretch of a file out as a file of its own (see `clip_trim.py`). A stream whose
  addresses run out (YouTube's, `StreamUrls.expires_at`) has them asked for anew
  `RENEW_BEFORE_SECONDS` before, and the run goes on at the new address without starting
  over - same numbering, same group unless the variant changed. A stream that is one FLV
  download (TikTok's, `StreamUrls.flv`) is read by `_follow_flv` instead of a playlist:
  cut into pieces as it arrives (`flv.py`), each copied into MPEG-TS by an ffmpeg of its
  own (`_flv_piece_as_ts`, a few hundredths of a second) and saved and handed on like any
  segment - so the buffer, clips and stream recordings need nothing of their own for it. A
  break the cutter sees, or a piece that could not be copied, starts a new group; an FLV
  address about to run out ends the run, and the next tick starts one at a new address.
- `recording_manager.py` — background loop driving one `ChannelRecorder` per watched
  channel, gated on an `is_live` check so offline channels never touch a browser. A
  channel's recorder runs for either of two reasons: the channel is tracked for clips
  (and watching is on), or its stream is being kept whole - which the switches have no
  say over. The streams kept whole right now are held here, and the loop is what notices
  that such a recording is over (no footage for `STREAM_END_GRACE_SECONDS`, the disk
  running out, footage that can no longer be written) and reports it to `main.py`. It is
  woken when a recording is asked for, so that one does not wait half a minute for the
  next round. A channel set to be recorded always is looked at like one that is being
  recorded - tracked or not, watching on or off - and reported to `main.py` when it is
  live with no recording of it running, which is when one is started. Once per stream: a
  channel whose recording ended for any reason but its stream being over (stopped by
  hand, the disk, a refusal to start) is held off until it has been seen off the air, or
  until its switch is set again (`hold_off`; in memory, so a restart forgets it).
- `stream_recording.py` — a stream kept whole: everything a channel broadcasts from when
  a recording is started (Channels page) until it is stopped or its stream is over, as
  one video file under `data/streams/<channel>/`, named for when it began. Its module
  docstring is the full story. In short: it rides on the channel's recorder, which hands
  every segment it saves to the recording's `StreamWriter` as well (`whole_stream`), so
  the stream is fetched once. Segments that follow each other are appended to one
  MPEG-TS file, a *part* (`stream_<id>_parts/part_<start ms>.ts`); a break in the stream -
  the recorder starting over, a hole, an ad, whose own segments are left out - starts the
  next part. When it is over, `save` remuxes each part to MP4 without re-encoding and
  joins several into one; parts that differ in picture or sound format are not forced
  together, and then, as with any other failure, the recording is marked failed and its
  parts stay in their folder (the Recordings page offers to try again). When each part
  began and how long it runs is stored with the recording (`stream_recordings.parts`),
  which is what puts chat against the picture (`broadcast_time`, `file_second`); what
  fell between two parts is simply not in the file. A recording is independent of
  tracking: started with tracking off it still gets its chat stored (the webhook keeps
  a recorded channel's messages and runs no detection on them), started ahead of the
  stream it waits for it, and it ends by itself once it has footage and has been given
  none for ten minutes. One started by itself, for a channel that is recorded always, is
  a recording like any other from then on. Saving takes as much room again as the
  recording, so recordings are ended - and none is started - while the disk still has
  that much plus
  `MIN_FREE_BYTES`. Nothing has to be finished for footage to be safe: after a restart a
  recording that was running goes on, into the same recording (the stretch in between is
  one more break), unless what it kept is older than the grace period - then it is saved
  as it was cut short. A shutdown from the dashboard ends recordings and waits for their
  files.
- `recording_moments.py` — moments looked for in a stream that was kept whole, once it is
  over. Pure; its module docstring is the full story. A recording has its chat stored
  whether or not the channel was tracked, so `POST /recordings/{id}/moments` replays that
  chat (`replay.load_between`, by the channel's id, from five minutes before the first
  frame to five after the last) under the channel's settings as they are saved, in a
  tracked background task, and this module places what the replay found in the
  recording's file: the clip framed as the recorder frames one (`Tuning.framing`), with
  context either side, all of it from the one part of the recording that holds most of
  the clip - nothing is cut across a break, and a moment the file has less than half the
  clip of is counted, not listed. What was found is kept in memory (`_recording_finds`:
  each moment with where its clip lies, so settings saved later change nothing of it
  until the recording is looked through again; a restart forgets it) and shown by
  `_recording_moments.html`. `POST /recordings/{id}/moments/clips` makes moments of the
  ones asked for (`moments`: their keys; left out, all): each file is cut out of the
  recording's with `recorder.trim_clip` - so it begins on a keyframe, up to a couple of
  seconds before the context asked for (`extent`) - and the moment is stored as a
  detected one is, with the replay's figures, `detected_at` the time it would have fired
  and no stream time. Which found moments are moments already is told from the channel's
  stored moments every time (`moment_of`: a reaction that overlaps), so one the service
  detected live counts as much as one made from here, and none is made twice. One replay
  runs at a time across this and the Settings trial (`_replay_running`); it takes as
  long as the chat was busy - the detector looks back five minutes for every message,
  so minutes for a long stream of a big channel (`replay.PROGRESS_EVERY`) - says how far
  it has got, and is given up when the service shuts down. Not found this way: clips on
  command, and anything the recording has no footage of
- `clip_creator.py` — alternative path: publishes an official Kick clip via the site's
  own internal API under a logged-in account (see "Clip creation")
- `transcriber.py` — local speech-to-text of a cut clip (faster-whisper, CPU) - and, word by
  word with when each was said, of a moment's whole file for its transcript (`transcribe_words`,
  see `subtitles.py`), which is not one of the analysis steps: it is made only when asked for
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
- A channel's stored 7TV keywords are weighed under its settings when they are loaded
  (`detector.keyword_weights`), but which names are listened for at all is still decided
  only when its emote set is fetched (`classify_emote_names`) - a settings change never
  adds or drops a stored name by the built-in rules.
- The lull setting cannot go as low as the detector's short window: laughs that arrive
  right after a moment fired are still in that window when a cooldown no longer than it
  runs out, and would fire a second moment by themselves (`tuning.LULL_MIN_SECONDS`).

## Testing

[docs/testing.md](docs/testing.md) is the full guide (layout, what each level covers, when to
run what, the manual live checklist). What matters when changing code:

- `python -m pytest` runs everything (about 3800 tests, a few minutes); `python
  scripts/check.py` runs what CI runs (lint, test formatting, tests with coverage). CI
  (`.github/workflows/tests.yml`) does so on every push and pull request, on Windows and
  Linux, and fails under 90 % coverage. Before opening a PR, run lint (`python -m ruff
  check`, `python -m ruff format --check tests`) and the test files for what was changed,
  and leave the whole suite to CI - except for a larger change (the recorder, the
  detector, anything spanning several modules), where `check.py` is run first.
- The suite is hermetic by construction (`tests/conftest.py`): it runs in a throwaway working
  directory with its own database per test, the disk is always taken to have plenty of
  room, video is always encoded on the processor (whatever graphics card the machine has),
  and any attempt to reach a non-loopback host, launch a browser or exit the process fails
  the test. It is safe to run while the live
  server is running from the same checkout - but don't *edit* production files in place for
  experiments while it is (a restart would pick them up); use a copy.
- A behaviour change comes with a test that would fail without it; a bug gets a failing test
  first. Fake at the boundary (`tests/support/`: `FakeKickApi` (with the account the bot
  writes as: Kick's consent page, which a test calls with the address the service sent the
  browser to, its token endpoint checking the PKCE verifier, and the chat a message is
  written in), `FakeTwitchApi` (Helix - the chat a message is written in among it -,
  Twitch's OAuth, BetterTTV, FrankerFaceZ - the `twitch_api` fixture, which also sets the
  service up for Twitch), `bot.kick_bot_signed_in` / `twitch_signed_in` (an account
  signed in for the bot, kept where the service finds it), `FakeEventSub` (Twitch's chat WebSocket, the `eventsub`
  fixture), `FakeYouTube` (InnerTube: a channel's addresses, its page, a video's player, a
  live chat's page and every look at it - the `youtube` fixture; the other platforms'
  fakes answer through the fake Kick's transport, so a test can have several),
  `FakeTikTok` (`api-live/user/room` - the `tiktok` fixture), `FakeTikTokChats` (the
  connection TikTokLive makes to a room's chat; the comments are TikTokLive's own
  CommentEvent, `tiktok.comment`), `FakeHlsServer` (MPEG-TS, or fragmented MP4 with an
  initialization segment), `FakeFlvServer` (an FLV download, made TikTok-like by
  `as_tiktok_sends_it`), `FakeTelegram` and `FakeDiscord` (where notifications go: the Bot
  API with its HTML read as Telegram reads it, and a channel's webhook - the `telegram` and
  `discord` fixtures, which put them in `.env`; a test then loads the service's settings
  again), `WebhookSigner`, `ChatSim`, the `service` fixture) rather than
  patching the service's own functions, and never `sleep` - pass the clock in or use
  `wait_until`.
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
  `chat_video.drawn_column`,
  the login script) and actually running the ML models. After touching those or the code next
  to them, go through the live checklist in docs/testing.md. A stream recording is tested
  on tiny synthetic footage; a real, hours-long one is a step of that checklist.

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
  is fetched inside the browser session that captures the live URL). A moment whose file
  would touch an ad - in the clip or in the context around it - is cut from the VOD
  instead, so its clip comes out up to ~25s longer than usual (whole VOD segments); a
  channel with VODs turned off gets the clip from the live segments, and context only as
  far as the ad.

**Twitch (`twitch_stream.py`)** - the same recorder and buffer, no browser. Twitch's API
has no playback either, so where a live stream is comes from streamlink, asked for the
master playlist only. Twitch's playlists are of the same make as Kick's (both are IVS):
2s MPEG-TS segments with their program date-time; an ad replaces the broadcast for its
length and is marked three ways - a stream source of class `twitch-stream-source` other
than "live", a date range of class `twitch-stitched-ad` spanning its segments, "Amazon"
in their titles - any of which the recorder takes for one. Upright (portrait) variants of
a dual-format stream are never picked while there is another. The VOD of the stream that
is on is fragmented MP4 - 10s fragments that play only after the playlist's
initialization segment (`EXT-X-MAP`), on the same program clock - which the recorder
keeps as `.m4s` in a group named for its initialization segment (kept beside them as
`<group>.init`, and written first when a clip is joined from them).

**YouTube (`youtube_stream.py`)** - the same recorder and buffer, no browser. YouTube's
API has no playback either: which video a channel is streaming comes from InnerTube
(`youtube_client.live_video`), and that video's master playlist from streamlink, which
asks YouTube's player API itself (as its Android client). The playlists are plainer than
IVS's: 5s MPEG-TS segments, H.264 and AAC up to 1080p, one program date-time at the top
for the first segment listed (carried forward), titles empty; the live edge is a few
seconds behind the clock. No ads are marked in them - YouTube's ads are played by the
viewer's player - and there is no VOD. Their addresses (master, variants, segments) carry
`/expire/<epoch>/`, six hours after they were handed out, and are bound to the machine's
IP; the recorder renews them ten minutes before (see `recorder.py`). Unverified on a
real stream: how a stream with its own server-side ads, if YouTube serves any, looks.

**TikTok (`tiktok_stream.py`, recording only)** - the same recorder, no browser, no
streamlink: TikTok's own `api-live/user/room` says where a live stream is, in every
quality, as FLV (seen October 2026: H.264 only as `hd` 720x1280; "origin", what the
streamer sends, H.264 or HEVC at up to 1080x1920 upright, at a few Mbps; HLS 403 from
outside TikTok's players; DASH for some streams only, audio and video apart). An FLV
download is the stream itself, so the recorder cuts it into segments of its own (see
`flv.py`): pieces from keyframe to keyframe (TikTok sends one every 2s), timed by
onMetaData's `start_time` (epoch ms when the stream began; the tags' times count from it),
copied into MPEG-TS. TikTok's H.264 carries SEI units (JSON for its own player) before the
picture of every keyframe and one after it; the one after made ffmpeg's MPEG-TS reader
lose the time of the next frame (one untimed frame every 2s, then duplicate timestamps in
the MP4), which is why SEI units are dropped in the copy. No ads, no VOD; an address works
for about two weeks (`expire`). Unverified: streams for adults or subscribers only (the
service is a visitor that is not signed in), and a stream whose encoding changes part-way
(a new run, so a break in the recording).

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

- A Twitch channel is `login@twitch` everywhere the service names it (folders, file
  names, logs, the dashboard), and kept in the database under a negative id. A Twitch
  login can be renamed: the channel's Twitch id stays the same, but its name here does
  not follow - such a channel is found live by nobody until it is added again.
- The bot writes as an ordinary account: on Kick the one signed in for it (any account -
  one kept for the bot is best, as every message shows its name; the streamer's own works
  too), on Twitch the account chat is read as. It can write only where that account may
  chat: a channel that has it banned, or is in followers-only or subscribers-only mode it
  does not meet, turns the message down, which the Health page lists against the channel.
  Its commands are whole messages from the channel's streamer or a moderator (by the
  badges the platform sends, which a chatter has no say over); the same words from anyone
  else are chat. What it writes comes back as chat like any other message and is stored
  with it. Its messages are in English, like the dashboard. A sign-in that lapsed on Kick
  (access taken back in Kick's settings, its refresh token left unused too long) stops it
  writing there until an account signs in again from the dashboard, which the Health page
  says.
- Twitch's chat is read as an account; a sign-in that lapsed (password changed, access
  taken back, 30 days without a renewal) stops Twitch's chat until `twitch_login.py` is
  run again, which the Health page says.
- A YouTube channel is `handle@youtube` (its handle in small letters; a channel without
  a usable handle is called by its id) and is asked about by its handle - so, as on
  Twitch, a renamed handle is not followed until the channel is added again. Its id
  (`UC...`) is kept as its `platform_user_id`.
- YouTube's chat is not pushed: each live YouTube channel costs a request every one to
  five seconds while it is live, and one every half minute while it is not. Everything
  YouTube is asked goes through InnerTube, which YouTube can change at any time - the
  Health page's YouTube part, and `YouTubeChanged`/`Refused` in the log, are where that
  shows. A machine YouTube takes for a robot gets `LOGIN_REQUIRED` from the player
  ("Sign in to confirm you're not a bot") and HTTP 429s.
- A TikTok channel is `handle@tiktok` (its handle in small letters) and asked about by its
  handle, so a renamed handle is not followed until the channel is added again; its
  TikTok user id is kept as its `platform_user_id`. It is never tracked for clips - added
  with tracking off, the switch refused - only recorded: by hand or with "Always record".
  Its chat is read only while a recording of it runs, a few seconds after the recording
  starts, through TikTokLive and Euler Stream; a connection Euler Stream will not sign
  (TikTokLive's `SignAPIError`, e.g. `RATE_LIMIT`) is tried again later and later, and
  shows on the Health page's TikTok part. TikTokLive and Euler Stream change with TikTok:
  when TikTok's chat stops coming in, a newer TikTokLive is the first thing to try.
  Everything else TikTok is asked goes through `api-live/user/room`, unsigned -
  `TikTokChanged` in the log is where a change of it shows.
- A TikTok recording is kept in the quality the streamer sends - HEVC as a rule, which the
  browser playing it on the Recordings page has to be able to decode (Chrome and Edge on
  Windows do with a graphics card that decodes HEVC, as this host's RX 480 does).

- Notifications are only as good as where they go. Telegram takes a video of up to 50 MB from
  a bot and Discord a file of up to 20 MB on a server without boosts (raised from 10 MB in
  August 2026), so a clip of half a minute at 1080p60 (25-35 MB; "Clip videos" is on to begin
  with) goes to Telegram and not, as a rule, to Discord - its note says so; one that runs on
  for a minute or more can be over Telegram's 50 MB too, unless `TELEGRAM_API_URL` names a
  Bot API server of one's own (2000 MB). A Telegram bot writes only in a
  chat that has written to it first (or a group it is in): "chat not found" or a 403 on the
  Health page means `TELEGRAM_CHAT_ID` is not such a chat. A Discord webhook deleted in Discord
  answers "Unknown Webhook" (404). Changes to `.env` take a restart. A part of the Health page
  is told of after it has been trouble for two minutes; one that flickers is never told of.

- A recording of a whole stream is big: at Kick's usual bitrates a few gigabytes an hour,
  and as much again for the minutes its file takes to make (the footage is copied once,
  twice if the stream broke off and its parts have to be joined). Nothing deletes
  recordings but the button on the Recordings page, and removing their channel from the
  watchlist with everything that came of watching it.
- The browser has to be able to move about in a multi-gigabyte recording, which it does
  by asking for byte ranges; Starlette's `StaticFiles` answers those (pinned by a test),
  so the file is written with its index at the end and not rewritten to move it forward.

- The database is in SQLite's default journal mode, where a statement that is still
  being read holds off every commit - and a chat message that cannot be stored within
  the five-second busy timeout fails its webhook delivery. So whatever goes through chat
  in bulk keeps no statement open while it works through the rows: it reads the chat in
  chunks (`db.CHAT_CHUNK`), each fetched whole before the next row is handed out. That is
  the Analyze page (`db.iter_channel_chat`), which also counts only a period's newest
  100,000 messages (`ANALYZE_MESSAGE_LIMIT`), saying so on the page when that left some
  out, and the trace over a recording and the replay of a recording's chat
  (`db.iter_channel_chat_between`), which go through all
  the chat of a stream of hours. Anything new that reads chat by the tens of thousands
  of rows goes the same way, and never hands on a cursor that is still open.
- A channel has two spellings of its name. The watchlist keeps the one it was added
  under, capitals and all; Kick sends chat under its own, all in small letters, and that
  is what chat rows, moments and the per-channel chat delay are stored under. Anything
  that starts from a watchlist name (a stream recording does) finds the channel's chat by
  `broadcaster_user_id`, never by comparing names (`get_channel_chat_between`,
  `get_chat_name`).
- A Kick channel can rename itself. It keeps its user id, so its chat, settings, emotes
  and switches carry on, and its chat comes under the new name at once - but Kick no
  longer finds it by the watchlist's old name, so its recorder (and every clip) would
  fail. The service follows it (`main._follow_rename`): chat arriving under a name that
  differs from the watchlist's other than in capitals (`_notice_chat_name`), or Kick
  knowing no channel by the watchlist's name when the recording loop asks
  (`recording_manager.run_forever`'s `look_failed`, at most every `RENAME_ASK_SECONDS`
  per channel), has Kick asked by id what it is called now, and `streamers.slug` set to
  that (`db.rename_streamer`; refused, with an error in the log, if another watched
  channel has that name). `recording_manager.rename` moves what it keeps by name: a
  running recorder carries on (its buffer stays in the old name's folder until the next
  restart), a stream recording that runs goes on in the folder it was started in (its row
  keeps the old name, which is what its folder is found by; after a restart it goes on
  under the channel's current name), what the loop saw is forgotten. The chat delay set
  under the old name is copied to the new one. How far into its stream a moment was is
  asked of Kick by the channel's id (`main._stream_elapsed_seconds`), whichever name its
  chat came under. Past moments, chat and finished
  recordings stay under the old name - the review page's channel filter lists both. A
  rename is logged as a warning and listed on the Health page. Twitch, YouTube and TikTok
  renames are not followed.
- Kick's webhook config UI occasionally fails to persist a URL change silently. If a
  channel is confirmed live/chatting but no webhooks are arriving, the fix is: toggle
  **Enable Webhooks** off, re-enter the URL, re-enable, save, then refresh the page to
  confirm the URL actually stuck.
- The 7TV public API (`7tv.io/v3/users/kick/{broadcaster_user_id}`) has no auth and
  returns a channel's full emote set, including very short names ("lo", "re", "xd") that
  are useless as substring keywords — `MIN_EMOTE_NAME_LENGTH` in `detector.py` filters
  these out before they're stored. The exception is a laughing emote whose whole name is
  one of `LAUGH_EMOTE_WHOLE_NAMES` ("lo"): kept, and found in text only as a word of its
  own (`detector.mentions`). Emote names are compared in small letters everywhere.
- Event subscriptions (`chat.message.sent`) have, more than once, silently gone back to
  zero on Kick's side with no error or warning — the app token still works fine, chat
  messages just stop arriving entirely for every watched channel. There's no known
  trigger; the running server re-checks every 10 minutes and re-subscribes whatever is
  missing (logged as a warning), as does a restart or re-running `subscribe.py <slug>`
  for a channel (all of them check what Kick still has first, so nothing is ever
  subscribed twice). Every check that found some missing is listed on the Health page, which
  is also where to look first when moments stop appearing: it tells a chat that is quiet from
  one that is not arriving, and its "Check now" asks `GET events/subscriptions` at once.

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
