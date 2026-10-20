# Architecture and module map in detail

The data flow and a paragraph per module of `src/kick_clip_hunter/`. A module's own docstring is the source of truth where it goes deeper; this is the overview of how they fit together.
Moved out of CLAUDE.md so that it is read only when needed. Where it says a module is described "below" or "above", see [architecture.md](architecture.md); "Clip creation" is [platforms.md](platforms.md), "Operational quirks" [operations.md](operations.md), "Testing" [testing.md](testing.md) and the dashboard [dashboard.md](dashboard.md).

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
       stored back on the moment - once its session has closed, and only if enough
       people laughed around it (a moment that falls short is removed)
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
- `webhook_security.py` — verifies Kick's RSA-signed webhook payloads; Kick's public key is fetched once a
  run and a copy kept in `app_settings`, which stands in while Kick cannot be reached
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
  set-up's words for the log (`setup_trouble`), what the Settings page sets up (`Setup`: what
  was saved there - kept in `app_settings` as `notify_setup:<field>`, an empty value saying
  there is none - laid over `.env` by `in_force`, checked by `check_setup`; the token and the
  webhook are `SECRETS`, never put on the page), and what the page's "Find chats" and
  `scripts/telegram_chats.py` ask (`telegram_bot`, `telegram_chats` - getMe, getUpdates).
  main.py wires it: `_notes()` (from the setup in force, built when first asked and again
  once `POST /notifications/setup` saves another; `POST /notifications/telegram/chats` is
  Find chats), `_notify` (in the background, where a kind's switch is on),
  `_notify_of_clip` (after a clip's file is saved, from `_create_clip_background`; with the
  clip alone - about half a minute - cut out by `recorder.clip_alone` into a temporary folder
  where "Clip videos" is on, as it is to begin with; not for clips made from a recording by
  hand), the recording notes from
  `_start_stream_recording` (who started it: `notify.DASHBOARD`, `CHAT` and by whom, `ALWAYS`),
  `_end_stream_recording` and `_save_stream_recording`, and `_keep_telling_of_health`, which
  looks at the Health page's judgement every `HEALTH_NOTE_SECONDS` (`_tell_of_health`) and
  tells of a part after `HEALTH_NOTE_AFTER_SECONDS`, and `_keep_summing_up_days`, which sends
  the summary of the last 24 hours at `DAILY_SUMMARY_HOUR` local time (`_sum_up_day`: clips by
  channel, clips still unrated, recordings saved, chat counted a channel at a time on its
  index, the Health page and the disk - `notify.daily_note`; the day last summed up is kept in
  `app_settings` as `daily_summary_sent_for`, so a day missed while the service was down goes
  out once it runs again, and none goes out twice)
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
