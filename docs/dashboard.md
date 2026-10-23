# The dashboard, page by page

What each dashboard page shows and does, and the routes behind it. Read before changing a page.
Moved out of CLAUDE.md so that it is read only when needed. Where it says a module is described "below" or "above", see [architecture.md](architecture.md); "Clip creation" is [platforms.md](platforms.md), "Operational quirks" [operations.md](operations.md), "Testing" [testing.md](testing.md) and the dashboard [dashboard.md](dashboard.md).

- Dashboard: `GET /dashboard` is the review page - a queue of moments (unrated, all, or
  best: rated 4 or higher) and the one that is open beside it, driven from the keyboard
  (1-5 rate and move on, J/K move, Space plays, Del and Del again deletes the open
  moment). Each row shows how many distinct people laughed around the moment (from 10s
  before it fired to 60s after, `moments.laughing`), and the queue can be ordered by that
  (`?order=laughing`, "Most laughing first") instead of newest first: of everything
  measured on the rated moments it told a good clip from a poor one best, far better than
  the stored score. Moments without a count (imports, or ones from before it was kept until
  `scripts/backfill_laughing.py` has been run) come last. Under the clip is the chat trace:
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
  rating (`moments.downloaded_at`). After it, "Add before" and "Add after" put that much more of
  the stream into the moment's file - 15 s to 2 min, chosen in the list in front of them, which
  the script remembers - ahead of its first frame or after its last, from the stream's own
  recording: its VOD on Kick and Twitch, days later too, and on YouTube the stream wound back
  while it is still on (see `vod_footage.py` below). That is `POST /moments/{id}/footage`
  (`side`, `seconds`), a tracked background task the page follows (`GET /moments/{id}/footage`,
  every second, its words in the moment's status line) until it opens the moment again with its
  longer file: the clip where it was in the broadcast and that much further into the file, the
  Trim dialog's cut moved with it. The player's address carries when the file was written
  (`?v=`), so a browser never plays its cached copy of the one before. Not offered for an
  imported clip, or one cut before its place on the stream's clock was kept; refused while a
  stretch of the moment is being cut or it is being transcribed, and those wait for it in turn.
  Beside the clip, on a tab next to its chat, is its transcript
  (see `subtitles.py` below): what is said in the footage, written down when "Transcribe" is
  pressed, put right there line by line - the line being said is marked as the clip plays, and a
  line's time plays the clip from there - and downloaded as subtitles (.srt) timed to the
  moment's file, or beside the cut in the Trim dialog. Delete, under the moment's tags, asks once
  more and removes the moment for good (`POST /moments/{id}/delete`): its row and every file
  beside its clip - the cut, its chat video and record, old context clips - but not while a
  stretch of it is being cut, it is being transcribed or footage is being added to it; one whose
  clip is still being cut has
  that clip thrown away when it arrives. `GET /dashboard/channels` has the
  watchlist and the per-clip analysis switches; shutting the service down is the button
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
  recording's chat as a video of its own as long as the recording, on the colour picked
  beside it (the Trim dialog's default colour to begin with, then the last one picked;
  H.264, tens of megabytes an hour): made the first time it is asked for
  (`POST /recordings/{id}/chat_video` with `chat_background`, a tracked background task;
  the page polls `GET /dashboard/recordings/{id}/chat_video` and downloads it once it is
  there), kept as `<recording>_chat.mov` beside the file, with the colour it was made on in
  `<recording>_chat.json`, and removed with it. Picking another colour than that makes it
  anew in its place. Trim, beside Download, opens the same
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
  (figures - messages and chatters set against the period before: the same stretch of time
  before, or for a session the session before - messages over time with the laughing and
  the detected moments marked, the busiest minutes, the most active chatters, the most used
  emotes, what was said and who was addressed most, chat by the badges its chatters have
  now, and the words used most, the little words of Czech, Slovak and English left out) or its messages, a page at a time (numbered pages above and under them, as under the review
  queue and the list of recordings - `dashboard_view.pages`), which is where a chatter or a
  text is searched for: a chatter that is found gets figures of their own, and every
  message leads to the chat around it. The third view, Shared audience (`view=audience`,
  which another period or channel keeps), sets the period's chatters against those of the
  other channels of the same platform in the same period (`chat_stats.Audience`): how many
  also wrote elsewhere, the channels shared with most, the chatters seen in the most other
  chats, and a map - a table of every two of those channels on chart paper, a cell the part
  of the row channel's chatters who also wrote in the column channel's chat. A name is
  taken for the same person wherever it writes, only within one platform; the accounts a
  channel ignores (Settings) are left out. The page is links and forms and needs no script.
  `GET /dashboard/settings` is where
  detection and clips are tuned: one form of settings (see `tuning.py`), for all channels
  or - chosen in the pane on the left - for one of the watchlist's, which goes by its own
  values once its "Own settings" switch is on. Under that pane its sections are tabs, each
  saying how many of its settings are set to something else; one is shown at a time beside
  them (`?section=`, which the script keeps in the address, and which choosing another
  channel keeps), but the form is saved as a whole, from the foot under every one of them.
  Each setting has a button that puts back what it would be if left alone (what is
  recommended, or for a channel what all channels have). A list - laughing emotes, emotes,
  messages and accounts to ignore, the commands - is its items, a rectangle each (the accounts
  to ignore start as the well-known chat bots, `detector.IGNORED_SENDERS`): one is added from the
  field beside it (Add, or Enter; checked there as the service would) and taken out by its
  cross, and what is typed but not added goes in with the save. The last tab, "Try on stored
  chat", replays a channel's newest chat under the saved settings and under the form's,
  without saving anything. The page for all channels has, before it, a tab for what is the
  service's: Notifications - where they go (a Telegram bot and its chat, with "Find chats" to
  pick it among those that wrote to the bot; a Discord webhook; the dashboard's address for
  their links), which kinds go out (clips, the clips themselves as videos, recordings,
  problems, a daily summary; all on to begin with), and a test - saved by a button of its own, not the form's
  (`POST /notifications/setup`), holding at once, `.env` holding for what was never set there;
  the token and the webhook are never put on the page. A channel's page begins with what is
  its own whatever it goes by:
  its clips on command; "The bot in chat" - whether the bot writes in its chat, what it tells
  of there by itself (the stream starting, recordings, a moment caught, a clip made), and the
  messages that ask it for something - with a sentence on as whom the bot writes on the
  channel's platform; and "What the bot says" - each of those messages in words of its own,
  a field each that shows the bot's own words while it is empty (a word in braces, such as
  `{stream_time}`, is filled in; the caption says which), and a message said every so many
  minutes while the stream is on. The page for all channels has the bot's two sections
  too, without the commands, after detection's; a channel's own values of them hold over
  them whatever it goes by. `GET /dashboard/health` says
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
