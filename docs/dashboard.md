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
  rating, and beside when it was in the open moment (`moments.downloaded_at`), and the queue
  can be narrowed to those moments or to the others (`?downloaded=yes|no`; the counts on its
  tabs follow) - and, beside that, to the moments whose clip is on disk or to those that are
  only a link into the VOD (`?on_disk=yes|no`, by `moments.clip_path`). After it, "Add before" and "Add after" put that much more of
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
  A moment of a channel tracked for moments has no clip until it is asked for: in the clip's
  place the page says so, with "Cut the clip" (`POST /moments/{id}/clip`, a tracked background
  task the page follows as it follows footage being added - `GET /moments/{id}/clip`, its words
  beside the button, under what the page says in the clip's place - until it opens the moment
  again with its clip). Above the button is the stretch of the stream that is cut - and all of
  the VOD that is fetched: to begin with the clip with the footage the settings keep around it,
  in clip time as the strip has it (`−0:30.0` to `+1:00.0`), marked on the strip. Either end is
  typed as the Trim dialog takes a time, or set where the VOD plays on the page ("Start here" /
  "End here", or I and O), and "Reset" puts it back; only a stretch that was changed is posted
  (`start`, `end`, at most `main.VOD_CLIP_MOST_SECONDS` long and that far from the clip at
  most). The moment's clip is then what of the clip the stretch holds, the rest of it the
  footage around - or the whole stretch, where it holds none of the clip. The clip is cut from the
  stream's VOD (`vod_footage.cut`), framed as the channel's clips are framed now, once the VOD
  reaches the end of the moment's footage - which can take a minute while the stream is on, said
  as "Waiting for the stream's VOD" - and the moment is then like any other; refused until chat
  has stopped reacting to it (whether it is kept is known a minute after it fired), and the
  moment is not removed while it goes on. Chat and its trace are there before the clip: the
  trace is drawn on the stretch the clip would hold. Where the moment's stream's VOD was found
  when it was caught, "Watch on Kick" / "Watch on Twitch", beside how far into the stream it
  was, opens it there in a new tab, from where its clip begins - on Kick 10 s later, nearer to
  what chat reacted to (`kick_stream.WATCH_LATER_SECONDS`; the clip itself is not moved), and
  counted by the VOD's own clock, as the player below goes, where its playlist could be read
  when the moment was kept (`moments.watch_url`). On Kick the VOD also plays on the page, in the clip's place: "Play
  from the VOD" - or Space, or pressing the trace, which starts it there - asks where it is
  (`GET /moments/{id}/vod`: the VOD's playlist, how far into it the clip would begin, framed as
  the trace is, by `vod_footage.seconds_in`, and where to start - where the link does), and
  hls.js plays the playlist (`static/hls/`, loaded the first time it is wanted). Its time is
  clip time, as a clip's is, so the transport, the trace and the chat go with it; it is asked
  for each time, and a VOD kept for subscribers once its stream is over, or one that does not
  reach the moment (yet), is said in the box, under the button. Both say what they say where
  they were pressed: the moment's own status line, under its rating, is out of sight below a
  player that size. Twitch lets no page but its own fetch its VODs, so a Twitch moment's VOD
  comes through the service: the playlist the page is given is an address of `GET /vod/relay`,
  which fetches it from Twitch and writes every address in it as one of its own
  (`vod_relay.py`); it starts where the clip would begin, as Twitch's link does. A YouTube
  moment has no VOD to play (`main.VOD_PLAYED_HERE`).
  Beside the clip, on a tab next to its chat, is its transcript
  (see `subtitles.py` below): what is said in the footage, written down when "Transcribe" is
  pressed, put right there line by line - the line being said is marked as the clip plays, and a
  line's time plays the clip from there - and downloaded as subtitles (.srt) timed to the
  moment's file, or beside the cut in the Trim dialog. The Trim dialog can also transcribe the cut
  that is there alone ("Transcribe the cut", `POST /moments/{id}/trim/subtitles`, followed as the
  dialog follows a cut being made), which takes seconds rather than the whole file's while: its
  lines are the moment's transcript, the tab says which stretch it was heard from and offers
  "Transcribe the rest", and a later cut or the rest is added without touching a line put right. Under the
  moment's tags, where the service is set up for reels and the moment has a clip, "Make a
  reel" - or, once one was asked for, "Open the reel" beside where it stands - leads to the
  Reels page with the moment open there (below). Delete, under the moment's tags, asks once
  more and removes the moment for good (`POST /moments/{id}/delete`): its row and every file
  beside its clip - the cut, its chat video and record, old context clips - but not while a
  stretch of it is being cut, it is being transcribed or footage is being added to it; one whose
  clip is still being cut has
  that clip thrown away when it arrives. `GET /dashboard/channels` has the
  watchlist and the per-clip analysis switches; shutting the service down is the button
  at the right end of every page's bar, which asks once more in a dialog. Beside each channel are its
  Tracking - Off, Chat, Moments or Clips, one of them lit (`POST /channels/{slug}/tracking`,
  `what` `off`, `chat`, `moments` or `clips`): Clips looks for clips while the channel is live,
  its stream coming in to cut each one from, which keeps its chat too; Moments looks for them in
  its chat alone - nothing of its stream comes in - keeps each with where it is watched in the
  stream's VOD and tells of it (the "Moments" notification), and has its clip cut from the VOD
  once it is asked for on the review page - or, with "Clips from the VOD" under the choice set
  to Automatically (`POST /channels/{slug}/moments_auto_cut`, `how` `on_request` or
  `automatic`, kept as `streamers.moments_auto_cut`; shown only while Moments is lit), by
  itself as soon as the VOD holds it. Only Kick and Twitch channels are offered it, their
  VODs staying once a stream is over (`platforms.KEEP_VODS`; the route refuses it for any other),
  and choosing it says what the platform lists of the channel's VODs that would keep a clip from
  being cut afterwards - none at all, or kept for subscribers only once a stream is over
  (`streams.vod_access` and `vod_trouble`, the response's `said`, in the row's status line).
  What the platform lists is also kept, and asked again of every Kick and Twitch channel on
  the watchlist once what was seen is a day old (`main._look_at_vod_access`,
  `streamers.vod_access`): where it keeps footage from being had from the VODs once a stream
  is over - for a moment's clip cut when asked for, or more of a moment's stream added to its
  file - the row says so under the channel's name ("VODs for subscribers", "VODs not
  public", "No VODs listed"), with the platform's words for it on hover. Chat keeps its chat
  and nothing more - stored like a tracked channel's, for the Analyze page,
  never seen by the detector, nothing recorded for it. Of a channel tracked for moments or for
  its chat its platform is still asked whether it is live, so that "Right now" can say "Live,
  chat only" (no lamp: no stream is coming in) and the Health page can tell its chat gone quiet;
  A Kick channel's row also says where its stream is followed from, for its clips and its
  recordings - "Live" (its address found in a browser window) or "From the VOD" (the VOD Kick
  writes as it goes along: no browser, no ads, footage up to a minute behind; `POST
  /channels/{slug}/stream_source`, `source` `live` or `vod`, kept as
  `streamers.stream_from_vod`; refused for any other platform, hidden while Moments is lit). A
  recorder that is running starts over on the other at its next tick.
  Off keeps nothing (a TikTok channel has none of
  them: "Recorded only", and the route refuses anything but `off` for one) - and the button that
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
  H.264, tens of megabytes an hour) - or, with "on a background" unticked, see-through, to
  lay over the recording (ProRes 4444, a few gigabytes an hour, checked against the disk
  at 5 MB a second; the choice is remembered like the colour): made the first time it is
  asked for (`POST /recordings/{id}/chat_video` with `chat_background`, `null` for
  see-through, a tracked background task; the page polls
  `GET /dashboard/recordings/{id}/chat_video` and downloads it once it is there), kept as
  `<recording>_chat.mov` beside the file, with the colour it was made on (or `null`) in
  `<recording>_chat.json`, and removed with it. Choosing otherwise than that makes it anew
  in its place. Trim, beside Download, opens the same
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
  their links), which kinds go out (clips, the clips themselves as videos, moments of channels
  tracked for moments - with a link to each in the stream's VOD - recordings, problems, a daily
  summary; all on to begin with), and a test - saved by a button of its own, not the form's
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
- Reels: `GET /dashboard/reels` (in the bar where the service is set up for reels, see
  `reels.py` below) has every reel that was asked for, the one asked for last first, with where
  it stands in a word ("Storyboard", "Choose", "Rendering", "Ready", "Failed"), and the one that
  is open beside it - `?moment=` opens a moment's, one that has none yet too, which is how the
  review page leads here. Above "Make a reel" (later "New storyboard") is the stretch of the
  moment's file that is sent - all of it to begin with, then the one sent last - typed in
  seconds of the file or set where the player under the transcript is ("Start here", "End
  here"; "Whole file" puts it back): less to transcribe and for Claude to look through where
  the rest would be thrown away. It is cut out of the file as the Trim dialog cuts one, nothing
  re-encoded, so it begins on the last keyframe at or before its start. Above the button a
  comment for the storyboard can be written - what the clip is about, where to cut, what the
  hook should play on - which Claude goes by; the one sent last is offered again. Above it,
  folded away until opened (and open once answered), are questions for the storyboard, each
  left to Claude unless chosen - or answered in words of one's own in the field beside its
  choices, which lets go of the choice: cut it or not ("Don't cut, keep it all", "Cut out dead air"),
  what the clip is, its length, the hook's style, subtitles, a teaser, a sound effect, emoji,
  who is in it, where the joke is and the parts that have to be in the reel - however far
  apart, each with a few words of what it is - set where the player under the transcript is
  ("Here") or typed, and "Render the recommended variant and hook straight away", which renders
  them, subtitles as the variant has them, as soon as the storyboard is back (`reels.ANSWER_CHOICES`,
  `reels.answers_asked`; the reel repository checks the storyboard keeps to them). "Make a reel"
  sends the stretch, its chat, the comment and the answers to GitHub (`POST /moments/{id}/reel`,
  `start`, `end`, `comment` and `answers`; a stretch that leaves out next to nothing is the
  whole file), and a storyboard comes back in a few minutes:
  what is in the clip, two or three variants (one recommended, those with subtitles saying
  so), three hooks and a sheet of frames - beside them the reel's transcript, under the
  moment's file playing in a player of its own: a line a block, when it starts and ends
  (typed as the Trim dialog takes a time, to the tenth of a second, in seconds of the file),
  what is said, a button that plays the file from the line's start to its end, one that adds a
  line after it and one that takes it out; "Add a line" adds one at the end, and what the
  speech model heard is listed under it, each line with "Add". The line being said is marked as
  the file plays, Enter goes on to the next line, and nothing is kept until "Save"
  (`POST /moments/{id}/reel/transcript`, `lines`: each with `start`, `end` and `text`; refused,
  saying which line, for one without its times or past the file's end; an empty one is left
  out, the rest kept in the order they are said), and leaving the page with something not
  saved asks first. "A: " and "B: " in front of a line colour it in the reel for who says it,
  `*a word*` makes it stand out. A variant and a hook (or words of one's own, `|` breaking the
  line and `*word*` highlighted) are pressed - an emoji shortcut in a hook (`{rofl}`, `{lebka}`,
  any case; `reels.EMOJI_CODES`, kept in step with the render script's) shown as its emoji, and
  one the render script does not know struck through, as the reel leaves it out. The words of
  one's own are shown so under the field as they are typed, and the emoji buttons under them put
  a shortcut where the cursor is; the shortcuts go up as typed, and count as their emoji against
  the hook's 80 characters. Subtitles are switched on or off (as the pressed
  variant has them to begin with), a comment for the render written if there is something to
  change in the variant (Claude reworks it by that first), and "Render"
  (`POST /moments/{id}/reel/render`, `variant`, `hook`, `subtitles` and `comment`, after saving
  what is not saved in the transcript) has the reel made with the transcript as its subtitles;
  it plays at the top once it is back, with a download, what it was made with and what Claude
  made of the comment. Under it, feedback on the reel - what works, what does not - goes with
  "Send feedback" (`POST /moments/{id}/reel/feedback`, `feedback`) to the reel repository,
  where later storyboards go by it; it is kept here even where GitHub does not take it (502),
  and then the page says so until it is sent again. A comment of up to 2000 characters goes
  along; what is typed in one and not sent stays when the page puts the reel in place anew,
  and leaving the page with one asks first. A new render or storyboard is another reel, so
  the comment for the render (after a storyboard) and the feedback start empty again. While a step goes on the page fetches the reel
  (`GET /dashboard/reels/{id}`) every five seconds and puts in place where it stands and the
  reel - and the rest only once another storyboard is back, never over a transcript being put
  right; the transcript's times are the moment's file's whatever was sent, and only its lines
  in the stretch go up as subtitles; a new storyboard of the same file and stretch keeps the
  transcript (another stretch is heard anew), the one put right by hand
  going to Claude with it, and one of a file that footage was added to since is said to be of
  the file as it was. A storyboard and a reel are also told of as notes, which lead here.
- On a narrow screen: up to 1100 px wide the panes stack and the page scrolls - the queue (or
  the list of recordings, or Analyze's channel and period) above what is open in it, the chat
  under that. A moment opened from the queue is then scrolled into view, and so is what the
  page's address asks for: a moment (`?moment=`, as a notification's link has it), a recording
  (`?recording=`), or on the Analyze page any channel or period - the script's `showOpen`. Up to
  640 px (a phone) nothing on a page is wider than the screen: the bar has the pages as tabs in
  two rows of three, the transport under a clip takes two lines (no volume slider - a phone has
  its own buttons), the rating keys run across the width, the steps between a trace's ends are
  left out, the watchlist is a block per channel with each value named in front of it (the
  cells' `data-label`), and the Trim dialog takes the whole screen. On a touch screen
  (`pointer: coarse`) the compact controls and tabs grow to 36 and 40 px, the handles and
  sliders' thumbs to 20 px, a field's words are 16 px so that a phone does not zoom in on it,
  and the keyboard's keys are not listed (`hover: none`).
