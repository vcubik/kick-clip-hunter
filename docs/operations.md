# Operational quirks

Gotchas seen while running the service: names, renames, platform behaviour, disk, the database, Kick's webhooks and subscriptions. Read when something misbehaves live, or before changing code that touches one of them.
Moved out of CLAUDE.md so that it is read only when needed. Where it says a module is described "below" or "above", see [architecture.md](architecture.md); "Clip creation" is [platforms.md](platforms.md), "Operational quirks" [operations.md](operations.md), "Testing" [testing.md](testing.md) and the dashboard [dashboard.md](dashboard.md).

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
  TikTok user id is kept as its `platform_user_id`. It is never tracked, for clips or for its
  chat alone - added with tracking off, anything else refused - only recorded: by hand or with
  "Always record".
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

- Footage added to a moment's file from its stream's VOD (the review page's "Add before" /
  "Add after") is only there while the channel keeps its VODs where anyone may watch them:
  a Kick channel that keeps them for its subscribers only, or a Twitch past broadcast that
  is gone (after a week for most channels, up to two months for some), has none, and the
  page says so. Kick lists only a channel's newest 10-30 streams. A YouTube stream can be
  wound back only while it is still on, and only where its streamer lets viewers rewind.
  Every press fetches the file's whole stretch again with what is added - for a two-minute
  file and half a minute more, 150-200 MB of Kick's 1080p60, some 15-25 s on this host - and
  the file it replaces is gone: there is no undoing it but Trim.
- The recorder's look for a Kick stream's VOD, inside the browser that finds the stream, has
  come back empty for whole streams that had one (`recording started ... (VOD not
  available)`), which left the clips through their ads without the VOD's footage. A run
  without its VOD looks again over plain HTTP every minute, and logs `the stream's VOD was
  found on a later look` when it finds it; the Health page's VOD column follows.

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
