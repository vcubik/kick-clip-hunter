# Setup and running in detail

Every setup step, every script and where the logs are. CLAUDE.md has the short version.
Moved out of CLAUDE.md so that it is read only when needed. Where it says a module is described "below" or "above", see [architecture.md](architecture.md); "Clip creation" is [platforms.md](platforms.md), "Operational quirks" [operations.md](operations.md), "Testing" [testing.md](testing.md) and the dashboard [dashboard.md](dashboard.md).

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
- Notifications (optional, see `notify.py` below) are set up on the dashboard's Settings page,
  under "All channels", tab Notifications (saved there, they hold at once, no restart) - or in
  `.env`, which holds for whatever the page never set: `TELEGRAM_BOT_TOKEN`
  (a bot made with @BotFather) and `TELEGRAM_CHAT_ID` (a chat that has written to the bot -
  the page's "Find chats", or `PYTHONPATH=src python scripts/telegram_chats.py`, lists them,
  with their ids), and/or
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
  - `backfill_laughing.py` — counts how many people laughed around each moment stored
    before the count was kept, from its stored chat (what the review queue can be ordered
    by); safe to re-run and to run while the service runs
  - `backfill_taste.py [--limit N]` — fills in transcript/audio-event tags/frame
    embedding for any moment with a clip but missing one or more of them (imports
    don't go through the live pipeline's background tasks, so they start out missing
    all three)
- Logs: the server logs to the console and to `data/logs/kick_clip_hunter.log` (rotated,
  10 x 10 MB) - check the file first when something went wrong while unattended. On
  startup it also turns off the console's QuickEdit mode (`win_console.py`), which
  otherwise freezes the whole app whenever someone clicks in the terminal window.
- Tests: `pip install -r requirements-dev.txt`, then `python -m pytest` (with the project's
  virtual environment: `.venv\Scripts\python.exe -m pytest`). See "Testing" below.
