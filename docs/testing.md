# Testing

The project has an automated test suite covering the whole pipeline - webhook in, clip on the
dashboard out - plus a linter, a CI workflow that runs both on every push, and a short manual
checklist for the few things that only exist against the real Kick.

- [Quick start](#quick-start)
- [How the suite is organised](#how-the-suite-is-organised)
- [What is tested](#what-is-tested)
- [What keeps it hermetic](#what-keeps-it-hermetic)
- [When to run what](#when-to-run-what)
- [Continuous integration](#continuous-integration)
- [What is not automated - the live checklist](#what-is-not-automated---the-live-checklist)
- [Checking a detector change against a real stream](#checking-a-detector-change-against-a-real-stream)
- [Writing tests](#writing-tests)
- [Troubleshooting](#troubleshooting)

## Quick start

```bash
python -m pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

That is the whole suite: about 2800 tests in a few minutes. It needs no `.env`, no Kick
account, no network, and it cannot touch the real `data/` directory - see
[What keeps it hermetic](#what-keeps-it-hermetic). The commands are the same in PowerShell and in a
POSIX shell; if the project's virtual environment isn't activated, call its interpreter directly
(`.venv\Scripts\python.exe -m pytest`).

`requirements-dev.txt` installs the service's core dependencies and the test tools, but not the ML
libraries from `requirements.txt` - the suite never runs a real model. `ffmpeg` and `ffprobe` have to
be on `PATH` for the clip-cutting tests; without them those tests are skipped and say so.

Useful variations:

| To... | Run |
|---|---|
| run everything CI runs (lint, formatting, tests with coverage) | `python scripts/check.py` |
| run one file | `python -m pytest tests/unit/test_detector_firing.py` |
| run the tests matching a phrase | `python -m pytest -k "straggler"` |
| run only the fast, single-module tests | `python -m pytest tests/unit` |
| leave out everything that starts threads, servers or ffmpeg | `python -m pytest -m "not integration"` |
| stop at the first failure, with full output | `python -m pytest -x -vv` |
| see what the slowest tests are | `python -m pytest --durations=15` |
| measure coverage (fails under 90 %) | `python -m pytest --cov --cov-report=term-missing:skip-covered` |
| lint everything | `python -m ruff check .` |
| check / fix the formatting of the tests | `python -m ruff format --check tests` / `python -m ruff format tests` |

## How the suite is organised

```
tests/
  conftest.py     the safety net: sandboxing, state reset, the "must never happen" guards
  support/        fakes and helpers shared by the tests
  unit/           one module at a time
  integration/    several modules together, with real I/O on loopback
  e2e/            the whole pipeline in one test
  scenarios/      whole evenings of chat through the detector
```

| Level | What runs for real | What is replaced | Speed |
|---|---|---|---|
| `unit` | The module under test. | Its collaborators, and the clock where there is one. | milliseconds each |
| `integration` | Real threads, real HTTP on `127.0.0.1`, real SQLite files, real `ffmpeg`, the real FastAPI app through an in-process client. | The outside world: Kick's and 7TV's APIs, the live stream, the browser. | up to a second each |
| `e2e` | Everything inside the service at once: recorder, recording loop, signature check, detector, moment session, ffmpeg, database, rendered dashboard. | Only the outside world, as above. | a couple of seconds each |
| `scenarios` | The detector, fed long scripted or randomised chat. | Nothing - the detector takes its clock as an argument. | under two seconds each |

Tests under `integration/` and `e2e/` carry the `integration` marker automatically (and `e2e` the
`e2e` marker); tests that need the ffmpeg binaries carry `ffmpeg`.

The helpers in `tests/support/` are what make the higher levels possible without mocks of the
service's own code:

| Helper | What it stands in for |
|---|---|
| `chat.ChatSim` | A chat room on a simulated clock: scripted messages go through the same classification calls the webhook handler makes - under a channel's own detector settings, if it is given some. |
| `chat.StoredStream` | The same script written to the database instead, as the service leaves a stream's chat behind - for replaying it. |
| `kick_api.FakeKickApi` | Kick's public API (token, channel lookup, subscriptions, webhook public key) and 7TV's emote lookup, as an `httpx` transport - the real client code runs unmodified against it. Also the account the bot writes in Kick's chat as: Kick's consent page (`consent`, called with the address the service sent the browser to), its token endpoint checking the PKCE verifier against the challenge, who a user token is of, and the chat a message is written in. |
| `bot.kick_bot_signed_in` / `twitch_signed_in` | An account signed in for the bot, its tokens kept where the service finds them - as the dashboard's sign-in leaves Kick's, as `scripts/twitch_login.py` leaves Twitch's (or as one signed in before the bot could write). |
| `webhook.WebhookSigner` | Kick's signing key: produces deliveries signed exactly the way Kick signs them (RSA-SHA256 over id, timestamp and body). |
| `hls.FakeHlsServer` | A live HLS stream over real HTTP, driven by the test: how much has been broadcast, where it has a discontinuity or an ad, which requests fail. |
| `tiktok.FakeTikTok` | TikTok's own web API (`api-live/user/room`): a channel, whether it is live and where its stream is, as an `httpx` transport (the `tiktok` fixture). |
| `tiktok.FakeFlvServer`, `tiktok.make_flv`, `tiktok.as_tiktok_sends_it` | A stream as one FLV download over real HTTP, of real video made by ffmpeg - dressed, where a test wants it, as TikTok sends one: `start_time` in its onMetaData, its times from then, a SEI unit after every keyframe's picture. |
| `tiktok.FakeTikTokChats`, `tiktok.comment` | The connection TikTokLive makes to a room's chat, and a comment as TikTokLive hands one over (its own CommentEvent). |
| `media.make_ts_segments` | Real MPEG-TS video with sound (a test pattern and a tone), generated by ffmpeg, for the tests that cut clips. |
| `fixtures.service` | The running service: the FastAPI app behind an in-process client, with the fakes above wired in and its waits time-compressed. The two limits a moment session measures on the wall clock are shortened to match (`service.quiet_seconds`, `service.max_open_seconds`); the constants and any saved settings are left as they are. |
| `html.parse` | A small HTML tree, so dashboard assertions are about structure and can tell text from markup. |
| `tailscale.FakeTailscale` | The machine's Tailscale client, which the Health page asks about the tunnel: answers the two commands that are run, in the shapes the real client prints, or is missing, not connected, slow or failing. |
| `data.add_moment` / `add_chat` / `add_stream_recording` | Rows in the test database, written through the service's own storage code. |

## What is tested

The suite follows the pipeline. For each stage, the question it answers and where to look:

**1. Webhook intake** - `integration/test_api_webhook.py`, `unit/test_webhook_security.py`
Is a delivery only accepted when Kick signed it (wrong key, changed body, replayed under another id or
timestamp, missing header - all rejected)? Is an accepted message stored once however often it is
redelivered, classified, and handed to the detector for the right channel? Are messages dropped while
watching is off, while one channel's tracking is off, or once shutdown was requested?

**2. Storage** - `unit/test_db.py`
Does a fresh database get the full schema, and does a database left by any earlier version of the
code get upgraded in place without losing rows (including the two historical rating-column
migrations and a half-finished one)? Does each query return what the dashboard and scripts rely on -
ordering, paging, filtering, the padded chat snippet, the back-fill candidates?

**3. Detection** - `unit/test_detector_*.py`, `scenarios/test_stream_scenarios.py`
How is one message classified (laugh forms, native emotes, a channel's own 7TV emote names, dance
emotes, poll votes)? When does a reaction become a moment: enough *distinct* people, scaled to how
many are chatting, measured against that channel's own last five minutes? And what must never be a
moment - one account spamming, a poll, a copy-pasted raid, everybody dancing to a song, chat simply
getting busier, anything during warm-up or cooldown? The scenario tests run whole evenings mixing all
of those and assert exactly which events fire; a randomised test checks that every moment the
detector ever produces rests on several distinct people, whatever the thresholds are tuned to.
`unit/test_detector_settings.py` asks the same of a channel with settings of its own, one setting at
a time: a bigger share of chat, a floor or ceiling of its own, a signal switched off, other weights,
emotes it added as laughing or told to be ignored, messages to leave out - and that none of it
touches another channel.

**4. Moment session** - `unit/test_moment_session.py`, `unit/test_detector_reaction_active.py`,
`integration/test_api_moments.py`
Does a moment close once the reaction has been quiet, is it extended only by new people reacting
*after* it fired (never by the laughs that fired it, never by one straggler), and is it cut off at the
cap? Does a re-fire while a moment is open keep it alive instead of opening a duplicate?

**5. Recording** - `unit/test_recorder_playlist.py`, `unit/test_recorder_buffer.py`,
`integration/test_recorder_recording.py`, `integration/test_recording_manager.py`
Does the recorder follow a live playlist and save every segment exactly once, named by its program
time? Does it survive what broke its predecessor - a discontinuity, a stitched ad, a title change, a
gap - by starting a new group and carrying on? Are failed downloads retried, playlist errors ridden
out, a stalled or dead run restarted, the buffer pruned while recording? Is a channel only ever
ticked (which opens a browser in production) when the API says it is live and it isn't paused?
`unit/test_flv.py` and `integration/test_recorder_flv.py` ask the same of a stream that comes as one
FLV download, as TikTok's do: are its tags read whole however the bytes arrive, is it cut into
pieces from keyframe to keyframe named by when onMetaData says the stream began, does a picture
encoded otherwise or a jump in its times start a new group - and do the MPEG-TS copies of the pieces
join into one stream in which every frame keeps its time, TikTok's SEI units and all?

**6. Clip cutting** - `integration/test_clip_cutting.py`
With the real ffmpeg: is the clip the right length, playable, with video and sound? Is a clip that
straddles a break cut from its longer side instead of stitched across it? Is the footage before and
after the clip in the same file, with nothing repeated and nothing missing, and left out where it lies
beyond a break? Can the clip be cut back out of its file for the analysis steps, starting on its own
first picture? Do an older moment's separate context clips join into the file that would be cut today
(with the scripts that use this in `integration/test_clip_scripts.py`)? Is a stretch trimmed out of a
file playable, begun on the last frame a cut can begin on before the start asked for and ended where
it was asked to, with the file it came from untouched?

**7. Per-clip analysis** - `integration/test_api_moments.py`, `unit/test_analysis_wrappers.py`,
`integration/test_lazy_ml_imports.py`
Does each of the four steps run only when its switch is on, store its result, and fail without
affecting the others? Are the ML libraries really loaded on first use only - never at startup - and
each model exactly once even when first used from several threads at the same moment? Is a model's
raw output turned into what gets stored (hallucinated subtitle credits dropped, tags de-duplicated,
top labels above the confidence floor)?

**8. Dashboard and controls** - `integration/test_api_dashboard.py`, `integration/test_api_controls.py`,
`unit/test_dashboard_view.py`, `unit/test_chat_trace.py`, `unit/test_clip_trim.py`,
`unit/test_chat_video.py`, `integration/test_chat_video.py`, `integration/test_api_transcript.py`,
`unit/test_subtitles.py`, `integration/test_api_removal.py`
Do the pages render from an empty database and from a full one? Does the review queue hold the right
moments - unrated, all, best - newest first, grouped by stream, paged and filtered by channel, and is
the right one open, with its chat and its clip, started where the clip begins in its file? Is chat put on the clip's own time axis
correctly - a stored clip start shifted by the channel's chat delay, an older clip placed by its
length, a missing clip stood in for by its window - and are the trace, the queue's sparks and "what
chat said most" drawn from that as intended? Does changing a channel's chat delay move its chat, and
only its? Does an empty queue say what the service
is doing? Is everything a Kick user can type - chat, usernames, emote names - escaped? Do the file
routes refuse to serve anything outside their directories? Are dates, counts and detector reasons
worded as intended? Does deleting a moment take every file beside its clip and nothing outside the
clips folder, and wait for what is being made of it; does removing a channel stop its chat, recorder
and Kick subscription, keep or remove what came of watching it as asked, and give it its id back
when it is added again? Do rating, tags and notes validate and persist; do the switches survive a
restart; does shutdown wait for work in flight and stop recorders before exiting? Is a stretch asked
for from the Trim dialog cut out of the right part of the file and refused where it cannot be; does
its chat video hold the stretch's chat counted from where the video really begins, moved by what was
asked for; is each frame of it the column of chat as it stood then - the newest line at the bottom
from the frame it arrives in, none of the lines still to come, the oldest whole at the top or not there at all -
see-through, or on the colour asked for; do animated emotes show the frame of
their animation the video is at, every copy of one alike, moving with their line; is chat drawn
into the cut's picture in the box asked for - its text as large as the box is wide within the limits,
as much of the column as the box is tall, within the picture, twice as fine where it is larger than
the page has it - from the very frame that was set, with how far it has
got said and given up at a shutdown; does a new cut replace the one before, and is each state said
in words? Does the dialog have a field for either end of the cut to be typed into, and a choice of
where its chat goes, and a box over the picture for chat drawn into it, with what the script
keeps it to? Is a moment's transcript made of all of its file when it is asked for and only
then, on clip time, with how far it has got said meanwhile, given up at a shutdown, said in words
when it fails and kept once it is there? Are the words the speech model heard made into lines a
subtitle long - a sentence, a pause, a line that would not fit or be read in time, broken after a
comma where one is far enough into it? Is a corrected line tidied, kept with its times, and left out
of the subtitles when it is emptied? Are the subtitles (.srt) timed to the video they go with - the
moment's file, or the cut made with Trim from where it really begins, with a line it cuts too little
of left out - and written so that an editor on Windows reads their Czech?

**9. Settings** - `unit/test_tuning.py`, `integration/test_api_settings.py`, `unit/test_replay_chat.py`
Is every field of what the service works from settable by exactly one setting, and is what the code
recommends a value its setting takes? Is a posted value checked - its kind, its range, pairs that
only make sense one way round - and refused in words that name the setting? Is only what differs
stored, so that an untouched setting follows the code and a channel follows all channels? Does a
saved value reach the next chat message, the moment that fires and the way its clip is cut, while a
moment that is already open keeps the settings it fired with? Do a channel's own values hold only
while its switch is on, and never for another channel? Does a replay of stored chat fire at exactly
the instants the live detector would, under whatever settings it is given, without touching the
state of a channel that is live - and does the page's trial say what the saved settings and the
form's would each have made, saving nothing? Does the page show every setting with what it would be
if left alone, mark the ones that were set, and keep a channel's form closed until it goes by its own?

**10. Stream recordings** - `unit/test_stream_recording.py`, `integration/test_api_recordings.py`,
`integration/test_api_recordings_page.py`, `integration/test_api_recording_trim.py`, and in
`integration/test_recorder_recording.py` and `integration/test_recording_manager.py`; the trace over
a recording in `unit/test_chat_trace.py`, and how its chat is read in `unit/test_db.py`
Is every segment a recorder saves also kept while a recording runs - in order, once, ads left out,
a break in the stream a break in what is kept - and does a failure to keep it cost the buffer
nothing? Is a recorded channel's recorder kept going whatever the tracking and watching switches
say, started at once rather than at the next round, and let go when the recording is over? Does
one started ahead of its stream wait, does one whose stream has been gone for a while end by
itself, and is every recording ended while the disk still has the room saving it takes? Is a
channel that is recorded always looked at whatever the switches say, reported when it is live with
nothing keeping its stream, given a recording without anyone starting one - and left alone for the
rest of a stream its recording was stopped in, then recorded again in the next? With the
real ffmpeg: does what was kept become one playable file, its parts joined in the order they were
on air and stored with it, parts of different pictures left apart rather than forced together, and
can a save that was cut short be done again? Is a recorded channel's chat stored with tracking
off, without anyone looking for moments in it? After a restart, does a recording that was running
go on and one that is stale get saved? Does a shutdown wait for the file? Is a second of the file
tied to the right moment on air, so that chat lands on the picture - across a break too - and is
the chat page given a stretch at a time? Do the pages say what a recording is doing in every state,
serve the file in byte ranges, and remove nothing outside the recordings folder? Is what chat did
over a recording drawn in steps that fit a strip however long it ran, each step the pace over it,
with chat placed as beside the video, a break's chat left out and the break marked, the channel's
moments marked - and counted once, until chat is placed differently, its chat read a chunk at a
time, every message once, without a statement left open against the webhook's writes? Is a stretch
of a recording cut out as a video named for the stretch, one at a time, its chat told by the
channel's id and placed by the recording's parts, refused where it is too long for its chat or
would fill the disk, and removed with the recording?

**10a. Moments found in a recording** - `unit/test_recording_moments.py`,
`integration/test_api_recording_moments.py`, and in `unit/test_replay_chat.py`
Is a replayed moment's clip framed in the recording's file as the recorder would have framed it
live, with its context, kept to one part of the recording and left out - but counted - where the
file has too little of it? Does what the cut file holds add up, when the cut begins on an earlier
frame than was asked? Is the chat a recording's channel wrote found by the channel's id and
replayed under the settings saved for it, off the event loop, one replay at a time, saying how far
it has got, given up on shutdown, and nothing stored by looking? With the real ffmpeg: is a found
moment made into a moment as the service would have detected it - same figures, the time it would
have fired, its file clip and context in one - shown on the review page with its chat, and never
made twice, whether it was made from here or detected while the stream was on? Does a cut that
fails leave neither a moment nor a file behind?

**11. The Analyze page** - `unit/test_chat_stats.py`, `integration/test_api_analyze.py`, and in
`unit/test_db.py`
Is the period the one that was asked for - a preset, any two times, the newest session where
nothing was asked - and never one that reaches past now? Does one pass count a period's chat to the
same figures in whatever order the messages come: who wrote, every use of an emote under the right
one of two emotes of the same name, laughing exactly where the detector says so, the same thing
said however it was capitalised or stretched, someone addressed once a message? Do a chart's
columns begin on round times, hold every instant of the period and stay few enough to read from
two hours to several years? Is a stretch of chat read up to but not at its end, one channel's
whatever name its chat came under, narrowed by a chatter's name or a text however either is
capitalised in any alphabet - and without a statement left open against the webhook's writes? Does
chat fall into sessions where it went silent, the stray messages between them left out? Does the
page show the right channel under its watchlist name, count the period's messages and no others,
mark detected moments on the chart, and lead from a column, a busy minute, a chatter's name or a
message to the right place? Does it find a chatter however the name is typed and offer the names
that are close when nobody has it, say so when only part of a very long period was counted, do the
counting off the event loop - and is everything chat wrote text, never markup?

**12. The Health page** - `unit/test_health.py`, `unit/test_tunnel.py`,
`integration/test_api_health.py`, and in `integration/test_recording_manager.py`,
`integration/test_recorder_recording.py` and `unit/test_db.py`
Is every delivery noticed - kept or not, chat or not - and one with a signature that is not Kick's
counted as refused instead? Is what a check of the subscriptions found said as it was: all there,
some dropped and mended, everything dropped at once, one that could not be subscribed again, Kick
not answering - and is a drop still listed after a restart? Does the button check at once without
ever running beside another check? Does the recording loop keep what it last saw of each channel -
live since when, why a look failed - and forget a channel it no longer looks at; does a recorder
say since when it has been running through however many restarts, and count as footage only what
it did not have yet? Is the tunnel read right from what Tailscale prints - up, serving inside the
tailnet only, forwarding to another port, not connected, not there at all - and never waited for
by a page? Is trouble what the page says it is and nothing less rare - silence only while a channel
is live and the service has been up to hear it, a quiet chat among talking ones not at all - and is
the count beside Health in the bar of every page the one the page itself comes to?

**12a. The bot in chat** - `unit/test_chat_bot.py`, `unit/test_kick_auth.py`,
`unit/test_account_tokens.py`, `integration/test_api_bot.py`, and in `unit/test_tuning.py`,
`unit/test_health.py`, `unit/test_twitch_auth.py` and `unit/test_replay_chat.py`
Is the bot off for every channel until its own settings switch it on - writing nothing, taking no
command - and are those settings each channel's alone, never two commands in one message? Is a
command the whole message, from the channel's streamer or a moderator only, carried out whatever
the tracking and watching switches say, once however often the platform delivers it, never during
a shutdown - and kept from the detector, live and in a replay? Does it start and stop a recording
as the buttons do, and answer how things stand: clips of the stream that is on, the recording and
how long it runs? Is what it says written in the channel's chat only where the channel has it
writing there - about recordings whoever started or ended them, about a clip where that is asked
for too - the same words not twice in a row, and never past the length of a chat? Does it write on
Kick as the account signed in for it and on Twitch as the account chat is read as, renewing a token
the platform turned down once, not asking an account that did not let it write, and saying in words
what a channel or a platform refused; does it write nothing on YouTube and still carry the commands
out? Does the Kick account sign in through Kick's OAuth flow with PKCE - a challenge only the
service's secret answers, a sign-in that comes back once, in time and only if it was started here,
one turned down kept as nothing - and is a newer sign-in, Kick's or Twitch's, the account that is
gone on with? Do the Health page and a channel's Settings say as whom it writes and where it
cannot, and is a channel it cannot write in trouble until a message goes out there?

**13. Startup, watchlist and scripts** - `integration/test_startup.py`, `unit/test_api_clients.py`,
`unit/test_scripts.py`, `unit/test_replay_chat.py`
Does a restart heal the chat subscriptions Kick silently drops - re-subscribing exactly the missing
channels, and starting regardless when Kick is unreachable? Do the API clients send what Kick expects
and cache the token until just before it expires? Do the command-line scripts do what they say, and
only that (refreshing emotes must not subscribe a second time)?

**14. The whole thing** - `e2e/test_webhook_to_clip.py`, `e2e/test_stream_recording.py`
A stream is recorded from an HLS server, chat erupts through signed webhooks, and the resulting clip -
one file, with its context around it - is on the dashboard, served, playable and rateable, and an
analysis step switched on for it is handed the clip alone. Once more with an ad
break right before the reaction, and once with no recording at all. And a stream recorded whole:
asked for with tracking off, downloaded by the real recorder under the real loop, stopped, and
played back on the Recordings page with the chat that arrived meanwhile on the right second -
then once more ending by itself when the stream does.

**15. The suite itself** - `unit/test_suite_isolation.py`
Are the guarantees in the next section actually in force?

### How good is it?

- **Coverage** is about 96 % of lines and branches (`--cov` prints the table). What is left is listed
  under [What is not automated](#what-is-not-automated---the-live-checklist).
- **Mutation spot-check.** Coverage only says a line ran, not that a test would notice it being
  wrong. So thirty behaviours were broken on purpose, one at a time - the crowd-size floor removed,
  signatures always accepted, clips stitched across a break, chat rendered unescaped, the watching
  switch ignored, and so on - and the suite run against each. It caught twenty-nine straight away;
  the thirtieth exposed a test that could not fail, which was rewritten until it did.
- **Any warning is an error** (`filterwarnings = error`): a coroutine that was never awaited, a
  thread that died with an exception or a deprecation in a dependency fails the run.

## What keeps it hermetic

The service is meant to be run from the repository against a real `data/` directory, real
credentials and a real browser, and a developer may well have it running live while the tests run
next to it. `tests/conftest.py` makes that safe, and fails loudly rather than silently if a test
tries to step outside:

- **Files.** The whole session runs in a throwaway working directory, entered before the app is
  imported (it opens its log file on import). Every test then gets its own working directory and
  its own SQLite file, so nothing carries over between tests and nothing is written under the
  repository.
- **State.** The detector, the API clients and the app keep state in module globals; all of it is
  reset around every test, so tests pass in any order.
- **Network.** Resolving or connecting to anything but loopback fails the test - even if the code
  under test swallows the error. The service is given dummy credentials and an empty certificate
  store.
- **Browser and process.** Launching a browser or calling `os._exit` (which is how the dashboard's
  shutdown button ends the service) fails the test unless the test is about exactly that.
- **Time.** No test sleeps and hopes. The detector takes its clock as an argument; the app's waits
  are compressed a thousandfold; anything on another thread is awaited with a bounded poll that
  fails with a message saying what it was waiting for.
- **Disk.** Whether a stream recording may start, or has to end, depends on how full the disk is.
  The suite always takes it to have plenty of room; a test about a full disk says how little.
- **Tailscale.** The Health page asks the machine's Tailscale client where the tunnel stands. As
  far as the suite can tell there is none on the machine; a test about the tunnel says what the
  client answers.
- **Graphics card.** Chat drawn into a cut is encoded on the machine's graphics card where ffmpeg
  can use one. As far as the suite can tell there is none: it always encodes with x264, so what a
  test makes is the same on every machine; a test about finding the encoder sets that back.

## When to run what

| When | What | How long |
|---|---|---|
| While working on one module | That module's test file (`python -m pytest tests/unit/test_recorder_buffer.py`) | seconds |
| Before opening a pull request for a small change | Lint (`python -m ruff check`, `python -m ruff format --check tests`) and the test files for what was changed; CI runs the rest | seconds |
| Before opening a pull request for a larger change (the recorder, the detector, several modules at once) | Everything CI will run: `python scripts/check.py` | a few minutes |
| On every push and pull request | Automatically, in CI: lint, then the suite with coverage on Windows and on Linux | a few minutes |
| Every Monday | Automatically, in CI: the same run against whatever the unpinned dependencies have become | - |
| After changing a detector threshold or weight | The suite, then [`replay_chat.py`](#checking-a-detector-change-against-a-real-stream) on a recent stream, before and after | seconds |
| After touching the recorder, the stream-URL capture, the webhook endpoint or the tunnel - and before relying on the service for a stream that matters | The suite, then the [live checklist](#what-is-not-automated---the-live-checklist) | ten minutes, on a live channel |
| After upgrading Python, ffmpeg or a dependency | The whole suite locally (the ffmpeg tests are the ones that notice) | under a minute |
| When a bug is found | First a test that fails because of it, then the fix | - |

A change that alters behaviour comes with a test that would have failed without it; a pull request
whose CI run is red does not get merged.

## Continuous integration

`.github/workflows/tests.yml` runs on every push to `master`, on every pull request, once a week,
and on demand:

- **lint** - `ruff check .` over the whole repository and `ruff format --check tests`. (The
  application code predates the formatter and is only linted; the tests are kept formatter-clean.)
- **test** - on `windows-latest` (what the service runs on) and `ubuntu-latest`: installs ffmpeg and
  `requirements-dev.txt`, runs the whole suite with coverage, fails under 90 %, and publishes the
  coverage table to the run summary. `REQUIRE_FFMPEG=1` turns "ffmpeg missing, skipping" into a
  failure there, so the clip tests can never be silently skipped in CI.

## What is not automated - the live checklist

Some things only exist against the real Kick, Twitch, YouTube and TikTok, and faking them would
only test the fake:

| Not covered by the suite | Why |
|---|---|
| `kick_stream.get_stream_urls`, `clip_creator.create_clip`, `scripts/kick_login.py`, `scripts/create_clip.py` | They drive a visible browser against kick.com, whose pages only load in a headed browser. |
| `chat_video.drawn_column`, `static/chat_video.js` | They are a headless browser laying the chat out and taking pictures of it. Everything around it is tested: which lines go on the page, the page itself, what the browser may load, how the frames are put together from its pictures (from a made-up column), and the encoding of the frames - and their drawing into a cut - with real ffmpeg (step 9 below). |
| `chat_video.HARDWARE_ENCODERS` on a real graphics card | The suite always encodes on the processor, so that what it makes does not depend on the machine. Which encoder is used is said in the log the first time chat is drawn into a cut (step 9 below). |
| `static/dashboard.js` - the Trim dialog: its handles, its player, the chat beside it, reading a time that was typed, the window it shows of a recording | As for the rest of the script (below): what the dialog asks the server for is tested, what happens in the browser is checked by hand (steps 9 and 11 below). |
| `static/dashboard.js` - the trace under a recording: fetching it, the playhead, scrubbing | The same: the trace the service draws is tested, the script that puts it on the page is checked by hand (step 11 below). |
| `static/dashboard.js` - the moments found in a recording: asking for them, following a replay or the cutting of clips, the marks on the trace, playing from a row | The same: every request and the section the service renders are tested, the script is checked by hand (step 11 below). |
| Loading and running the four models (`_get_model` bodies, `frame_encoder.encode_clip`'s tensor maths) | Multi-gigabyte downloads; the wrappers' own logic *is* tested, with stand-in models. That the speech model still hands over words with their times, and what a real transcript looks like as subtitles in an editor, is step 15 below. |
| `static/dashboard.js` - the transcript beside a clip: its tab, asking for it and following it while it is made, marking the line being said, a line's time playing the clip, saving what is put right in a line | As for the rest of the script: the tab the service renders and every request it makes are tested, the script is checked by hand (step 15 below). |
| Kick's actual API and playlist behaviour | `FakeKickApi` and `FakeHlsServer` encode what has been observed. If Kick changes, the fakes keep passing - the checklist below is what notices. |
| Kick's actual consent page and chat API, and Twitch's chat API: the bot's sign-in on Kick, a message written in a Kick or Twitch chat | `FakeKickApi` and `FakeTwitchApi` encode what Kick and Twitch document (Kick's OAuth 2.1 with PKCE, `POST /public/v1/chat`; Helix's `POST chat/messages`); a channel's own rules of who may chat exist only there. Step 17 below is what notices a change. |
| Twitch's actual API, chat connection and playlists: Helix, the device flow of `scripts/twitch_login.py`, the EventSub WebSocket, streamlink finding a stream and its VOD | `FakeTwitchApi`, `FakeEventSub` and `FakeHlsServer` encode what Twitch documents and what was observed (a live playlist's tags, a VOD in fragmented MP4); streamlink itself is never run - a test that would reach it fails. Step 14 below is what notices a change. |
| YouTube's actual web API, chat and playlists: InnerTube's answers, a live chat's page and its looks, streamlink finding a stream | `FakeYouTube` and `FakeHlsServer` encode what was observed of them (October 2026: the shapes of `resolve_url`, `browse`, `player`, the chat page's settings and data, `get_live_chat`'s actions; a live playlist with one program date-time at the top); InnerTube is not published, so a change is only noticed live. streamlink itself is never run. Step 16 below is what notices a change. |
| TikTok's actual web API, its FLV streams and its chat: `api-live/user/room`, a live FLV download, `tiktok_chat.tiktoklive_chat` (TikTokLive connecting through the Euler Stream sign server) | `FakeTikTok` and `FakeFlvServer` encode what was observed (October 2026: the room answer's shape and its qualities, FLV with `start_time` in onMetaData and a SEI unit after every keyframe's picture); the connection to a room's chat is stood in for by `FakeTikTokChats`, while the comments it hands over are TikTokLive's own CommentEvent. Step 17 below is what notices a change. |
| `static/dashboard.js` - the keyboard flow, the clip player, opening a moment without a reload | There is no browser in the suite. The pages are tested as the HTML the server renders, and every endpoint the script calls has tests of its own; the script itself is checked by hand (steps 2 and 6 below). |
| A stream recording of real length | The suite records and saves seconds of synthetic footage. Hours of a real stream - gigabytes on disk, a file made in minutes, a player moving about in it - are step 11 below. |
| The public tunnel and the Kick-side webhook configuration | Infrastructure outside the repository. What the Health page makes of Tailscale's answers is tested; that those answers are still what Tailscale prints is step 1 below. |
| `static/dashboard.js` - the Health page: fetching it again every few seconds, the Check now button | As for the rest of the script: the page and the endpoint the button calls are tested, the script is checked by hand (step 1 below). |

Run this on a live channel after changing any of the above, or the code right next to it:

1. **Start the server.** The log shows either `chat subscriptions present for all N watched
   channels` or the channels it re-subscribed, and `tunnel: up`. Startup takes a second or two - if
   it takes much longer, an ML library is being imported at startup again. The Health page says the
   same: the subscriptions as they were found, the Funnel's address, nothing beside "Health" in the
   bar. Its times count up without the page reloading, and "Check now" brings the subscriptions'
   "checked" back to "just now".
2. **Open the dashboard.** The review page lists the existing moments and announces nothing as
   new; the Channels page shows the watchlist and the switches. Clicking a row of the queue opens
   that moment without the page reloading, and `J`/`K` move through the queue.
3. **Check chat is arriving.** With a tracked channel live, chat lines appear in the log within
   seconds. If they don't, see "Operational quirks" in `CLAUDE.md`.
4. **Check recording.** A browser window opens briefly, then the log says `recording started`.
   `data/recordings/<channel>/` fills with `.ts` segments and stays at roughly ten minutes' worth.
5. **Wait for a moment.** The log shows, in order: `MOMENT detected`, `moment N closed after Xs
   reaction`, and about a minute and a half later `clip saved for moment N: ... (35 s, with 30 s
   before it and 60 s after)` - one file, cut once the footage after the clip has been broadcast.
6. **Review it.** Within half a minute the queue announces the new moment. Opened, it has a clip
   that plays with sound and starts before the reaction - at 0:00, not in the footage the file
   opens with - and under it the trace of chat with the moment marked. Clicking on the trace moves
   the clip there, and into the footage before or after it at either end (shown as minus and plus
   times); played on, the clip runs into the footage after it, and played again from the very end
   it starts over at the clip. Chat lines appear beside the clip as it reaches them. This is the check that
   the picture and chat line up: the laughing on the trace should start a few seconds after what
   caused it on screen. If it is clearly off, move the channel's chat with the slider under the
   chat lines - the lines follow as it is dragged, the trace once it is let go of, the clip plays on, and the setting is still
   there after a reload. On a stream that shows its own chat, "Chat as it arrived" should have a
   message appear beside the clip within a few seconds of appearing in the picture. `Space` plays and
   pauses; a number key rates the moment and moves on to the next unrated one. Reload: the rating
   sticks.
7. **Try one analysis step.** Turn `Transcript` on (Channels page); the next clip gets a transcript
   (the first one takes longer while the model loads). Turn it off again.
8. **If the stream runs an ad:** the log shows `stream discontinuity` with `ad=True`, a playlist
   is saved under `data/hls_debug/`, and segments keep arriving afterwards. The recording's start
   line says whether a VOD was found; if it was, a moment right after the ad logs `footage from
   the VOD used` and nowhere in its file - not in the clip, not in the footage before it - is the ad.
9. **Trim a clip.** Press Trim next to the clip's controls. The dialog opens on the clip as it was
   cut, on a copy of the strip; dragging a handle moves that end of the stretch and the picture
   with it, as far as the footage goes and no closer to the other handle than a second. `I` and
   `O` put the start and the end where the player is, `Space` plays and stops at the end of the
   stretch, and a number key rates nothing. Type a time into "From" or "to" and press Enter - `0:12.5`,
   `-0:05` for five seconds before the clip, `+0:10` for ten after its end: the handle and the
   picture go there; something that is no time is said so and the field goes back to what it
   was. Tick the chat box: chat appears beside the picture and
   follows it, and the slider moves it earlier or later; with "on a background" ticked the chat
   beside the picture takes the colour picked. Choose "as a video of its own" and Export: the
   dialog says it is cutting, then that the video is ready while its chat is made, counting up to
   100 % within seconds, and offers both to download - and how much before the start that was set
   the video begins. The log says `moment N trimmed` and `chat video saved for moment N`. Laid
   over the video in an editor (or with ffmpeg's `overlay` filter) from its first frame, the chat
   video is as long as the video, has nothing behind the text, and its lines appear when they did
   beside the picture in the dialog, each there at once at the bottom, the ones before moving up
   for it, the oldest whole at the top until it would be cut and then gone, with emotes and badges drawn - animated emotes moving
   as they do beside the picture, in their places, with every copy of an emote alike. With "on
   a background" ticked it is the colour behind the text and a small H.264 file. Then choose "in the
   video": the chat beside the player moves into a box over the picture, at the bottom right,
   dressed as in the video. Drag it to the left edge and a corner of it to make it larger: it
   keeps within the picture, the text grows with its width and its height shows more or fewer
   lines; it will not get smaller or larger than it may. Export: the dialog counts up while the
   video is made with its chat in it, and offers one file to download, which begins on the very
   frame that was set and has the chat where the box was and as large, the text sharp - on a
   panel of the colour if one was picked. Reopened (or the page reloaded), the box is where it
   was left. The log says, the first time, which encoder chat is drawn into
   videos with - `h264_amf` on this host's graphics card - and then `moment N cut with its chat
   in it`. "Make its chat" on a video exported without chat draws it into that video, in its
   place. Closed and opened again, the dialog still has the stretch, the downloads and where chat
   went.
10. **Change a setting.** On the Settings page, pick a channel that has chat stored and switch
   its own settings on. Raise "At least this many people" and press "Try on stored chat": after a
   few seconds a table says how many moments the saved settings and the form's would each have
   made, and the second row has fewer. Save: the page says so, the setting is marked as set with a
   button that would put back what all channels have, and the log has `settings of <channel> ->
   reacting_least=...`. A value outside its range, or a floor above its ceiling, is refused in a
   sentence and what was typed stays in the form. In "More laughing emotes" type a part of an emote
   name and press Enter (or Add): it is listed under the field with a cross, which takes it out
   again; one with a space in it, too short or already there is refused in the form's status line,
   and one typed but not added is added when the form is saved. With the channel live, the next
   moments follow the new values without a restart. Put the value back and save, or switch the channel's own
   settings off, when done.
11. **Record a stream.** On the Channels page press "Start recording" beside a live channel -
   one with tracking off shows that the two are independent. Within a few seconds (a browser
   window first, if the channel was not being recorded already) the row counts up what has been
   recorded and how big it is, its lamp and the one in the bar say "Recording", and
   `data/streams/<channel>/stream_<id>_parts/` holds a growing `.ts` file. Chat of that channel
   keeps arriving in the log. Beside a channel that is not live the same button says "Waiting
   for the stream" instead. Let it run for a while - through an ad break if there is one, which
   the log shows as a discontinuity - then press "Stop recording": the row says it is saving,
   the log says `stream recording N saved`, and the parts folder is gone. The Recordings page
   lists it with its length; opened, the video plays with sound, moving far ahead in it takes a
   moment and not a download of everything in between, and the chat beside it follows the
   picture, also after such a jump - it should sit against the picture as chat does beside a
   clip of that channel. Under the video, a moment after the page, is what chat did over the
   whole recording: the times under it run to the recording's length, pointing at it says the
   time and the pace there, a press on it takes the video there and the playhead follows the
   video as it plays. Where chat went off shows as a peak, a moment found during the recording
   as a yellow mark, a break in the stream as a slit in the paper. Go to a peak and press
   Trim: the dialog opens with a minute around that place set, on a strip that shows about
   five minutes of the recording second by second. Drag a handle, type an end as `1:23:45`
   and press Enter - typed far off, the strip moves to show it - and Export: the dialog names
   the stretch that is ready, the downloaded file has the stretch in its name and plays, and
   the log says `stream recording N trimmed`. With the chat box ticked the lines beside the
   picture follow it, and its chat is made as for a clip - as a see-through video of its own
   for a stretch of up to ten minutes (a longer one is refused in a sentence that says what
   can be done instead). Set half an hour and "in the video": the dialog counts
   up for about half as long as the stretch runs on this host (the log says `cut with its chat
   in it ... encoded with h264_amf`), and the file plays from the frame that was set with chat
   where its box was throughout. Shut the
   service down from the Channels page while one is being made: it does not wait for it, and
   the log says it was given up. A second export replaces the first.
   The file is where the page says it is and plays in another player.
   Under the file's line is "Moments". Press "Find moments": the section counts through the
   recording's messages - a busy chat of hours takes minutes, and the rest of the dashboard and
   incoming chat carry on meanwhile - then lists what would have been a moment under the
   channel's saved settings, each marked along the foot of the trace. For a stream that was
   tracked while it was recorded, the list should be close to the moments the stream really
   had, and those rows say "Open in review". A row's time plays the recording from where its
   clip begins. "Make a clip" on a row that is no moment yet: within seconds the row links to
   the review page, where the moment plays like any other - clip, context either side, chat
   in step - and sits in the queue at the time it happened; the log says `moment N made from
   stream recording M`. Change a setting on the Settings page, press "Look again", and the
   list follows the new values.
   Delete it from the page when done: it asks twice, and the file and what was cut out of it
   are gone. If the service is
   restarted while a recording runs, the log says the recording `goes on after the restart`,
   and the finished file has a break where the service was away. Then switch "Always record" on
   beside a live channel that is not being recorded: within a few seconds the log says `stream
   recording N started by itself` and the row counts up, without the page being reloaded. Press
   "Stop recording": it is saved, and no new recording begins while that stream is still on.
   Leave the switch on for a channel that is offline, and check after its next stream that the
   stream is on the Recordings page from its first minute.
12. **Look at the chat in figures.** The Analyze page opens on the newest session of the channel
   that chatted last, within a second or two. Its figures agree with what the stream was like, the
   chart shows chat rising where it did and marks the minutes moments were detected in, and the
   emotes in the tables are pictures. A column of the chart opens that stretch on its own; a
   chatter's name opens what they wrote, and the time beside a message the chat around it. "All",
   on the channel with the most stored chat, answers within a few seconds - and chat lines keep
   arriving in the log while it is being counted.
13. **Shut down from the Channels page.** It asks once more before doing it; then a banner shows
   how much work is still in flight, and the process exits on its own once that is done, and not
   before. A stream recording that was running is ended and saved first.
14. **Watch a Twitch channel.** With `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` in `.env`, run
   `scripts/twitch_login.py`: it shows a code, and confirmed on twitch.tv/activate it says whose
   account signed in. Add a live Twitch channel on the Channels page by a link to it: it is listed
   as `<name>@twitch` with its Twitch id in the tooltip. Within half a minute the log says
   `twitch chat: reading N channel(s) as <account>`, its chat lines follow, and the Health page's
   Twitch part says whose chat comes in and as whom - nothing beside "Health" in the bar. No
   browser window opens for its recording: the log says `recording started`, with a VOD found if
   the channel keeps its past broadcasts. A moment of it plays like a Kick one, its emotes drawn
   as pictures - Twitch's own, and the channel's 7TV, BetterTTV and FrankerFaceZ ones - and its
   chat sits against the picture after the same calibration as in step 6 (Twitch's player runs a
   few seconds behind the broadcast, as Kick's does). If its stream runs an ad, the log shows a
   discontinuity with `ad=True`; a moment through it logs `footage from the VOD used`, the
   channel's folder under `data/recordings/` holds `.m4s` fragments and a `.init` file beside its
   `.ts` segments, and nowhere in the clip is the ad. Restart the service: the reader connects
   again on its own, and the Health page shows how long ago.
15. **Transcribe a clip.** Open a moment whose clip has someone talking in it and choose the
   Transcript tab beside the clip: it offers to transcribe it. Press Transcribe: the tab says it is
   transcribing - the first time, while the model loads, for a while - then counts up and shows
   the lines, each with its time, the one being said marked as the clip plays and kept in sight.
   The log says `transcript of moment N saved (M lines)`. Pressing a line's time plays the clip
   from there; the lines read like subtitles, none longer than a row and none on screen too long.
   Put a word right and press Enter: the next line gets the cursor, the tab says `Line saved.`,
   and the correction is still there after a reload; Shift+Enter makes a line of two rows, and a
   line emptied is left out. "Whole footage" downloads `moment_N.srt`; put next to the moment's
   file in an editor (DaVinci Resolve, Premiere, CapCut), its subtitles start with it and their
   Czech letters come out right. Trim a stretch and Export: "Download subtitles" appears beside
   the video, and `moment_N_trim.srt` lines up with that video from its first frame - closed, the
   dialog leaves "The cut from Trim" in the tab as well. The next moment opened shows the same tab.
16. **Watch a YouTube channel.** Add a live YouTube channel on the Channels page by a link to its
   page, `youtube.com/@<handle>`, and again by a link to its live stream: it is listed once, as
   `<handle>@youtube` with its channel id (`UC...`) in the tooltip. Within half a minute the log
   says `youtube chat: reading the chat of <handle>@youtube` and its chat lines follow - none
   from before that line - a second or few after they show in the chat on youtube.com (in its
   "Live chat" view, not "Top chat"); the Health page's YouTube part says its chat comes in.
   No browser window opens for its recording: the log says `recording started`, VOD not
   available. A moment of it plays like a Kick one, YouTube's emoji in its chat drawn as
   pictures - laughing ones counted as laughing - and its chat comes in a line at a time, as it
   was written, not a few lines at once. It sits against the picture after the calibration of
   step 6, which for a YouTube channel starts at 6 s rather than Kick's 3 (YouTube's player runs
   further behind the broadcast, the more so for a stream of normal latency) - note the figure
   it settles at, to correct that default by. Six hours into its stream the log says
   `the stream's address was renewed before it ran out`, with no `discontinuity` line and no
   restart around it. When the stream ends the log says `the chat of <handle>@youtube is
   over`. A channel whose streamer turned chat off is listed on the Health page with what
   YouTube says of it, and nothing is beside "Health" in the bar.
17. **Record a TikTok stream.** Add a live TikTok channel on the Channels page by a link to its
   page, `tiktok.com/@<handle>`: it is listed as `<handle>@tiktok` with its TikTok user id in the
   tooltip, "Recorded only" where its Tracking switch would be. Start recording it. No browser
   window opens: within half a minute the log says `recording started`, VOD not available, and no
   `could not be copied into MPEG-TS` follows; within a few seconds of the recording the log says
   `tiktok chat: reading the chat of <handle>@tiktok`, and the Health page's TikTok part says its
   chat comes in. Stop it after a few minutes. The Recordings page plays the recording - upright,
   in the quality the streamer sends (HEVC as a rule: the browser has to be able to play it) -
   with its chat replayed beside it and lined up with the picture after the calibration of step 6;
   ffprobe finds the file whole (`ffmpeg -v error -i <file> -f null -` says nothing). Nothing of
   its chat is a moment, and nothing is beside "Health" in the bar.
18. **Let the bot write in chat.** In the Kick app's settings, set the redirect URL to
   `http://localhost:8000/bot/kick/signed_in` and allow the scopes `chat:write` and `user:read`.
   On a Kick channel's Settings page, under "The bot in chat", follow "Sign one in" - in a private
   window if the browser is signed in to Kick as someone else - and sign in the account the bot is
   to write as on Kick's own page: the browser comes back to a page that says "Signed in on Kick as
   <account>", and the log says `bot: signed in on Kick as <account>`. Switch "Write in chat" on
   for a channel that is live, put `!record start`, `!record stop` and `!status` in the three
   fields of messages, and save. In that channel's chat, as its streamer or a moderator, write
   `!status`: within a couple of seconds the bot answers under the account's name, and the log
   says `the bot wrote in chat`. `!record start` starts a recording - the Channels page counts it
   up - and the bot says so; `!record stop` stops it and says how much of the stream it kept.
   Written from a viewer's account, the same do nothing; with "Write in chat" off, they are
   carried out without an answer. The Health page's Chat bot part lists the channel and when the
   bot last wrote there, nothing beside "Health" in the bar. With "Say when a clip is made" on,
   the channel's next clip is said in its chat about a minute and a half after the reaction, with
   how far into the stream it was. On Twitch: run `scripts/twitch_login.py` again - it asks to
   write in chat as well now - and do the same on a Twitch channel; the bot answers as the
   account chat is read as. On a YouTube channel, `!record start` starts a recording and nothing
   is written.

## Checking a detector change against a real stream

Every chat message is stored with its arrival time, and the detector takes its clock as an argument,
so a past stream can be fed through the current code exactly as it arrived:

```bash
python scripts/replay_chat.py some_channel --since 2026-10-07 --until 2026-10-08
```

```
some_channel: 18367 messages over 8.2 h of chat (2026-10-07 13:06 .. 21:17 UTC)
                     moments  per hour  not extended  median clip  longest
current settings          37       4.5            20          35s      59s
```

"Current settings" are the ones the service goes by for that channel: what was saved on the
dashboard's Settings page, and the constants for everything else. Hours are hours of chat - a hole
in the stored chat longer than the detector remembers is not counted. Add `--set NAME=VALUE` for
any constant of `detector.py` (and `--quiet` / `--max` for the moment session) to get a second row
with the change applied, whatever the Settings page has that constant at:

```bash
python scripts/replay_chat.py some_channel --since 2026-10-07 --set MIN_REACTION_UNIQUE_FRACTION=0.15
```

It reproduces *when* moments fire - the first time it was used it matched the live run 102 to 103 -
so it answers "how many moments, how long the clips". It cannot say whether the moments that remain
are the good ones; that needs ratings. It only reads the database.

The Settings page runs the same replay ("Try on stored chat") on the newest 10,000 messages of a
channel, with the values in its form against the ones that are saved - the quicker way to try a
value that is a setting. The script is for a chosen stretch of time, and for constants that are not
settings.

The test suite deliberately does not pin today's thresholds: the detector tests are written against
the detector's own constants, and the scenario tests use reactions several times larger or smaller
than whatever the thresholds currently are. Retuning a number should not break a test; changing a
*rule* should.

## Writing tests

- **Name a test after the behaviour, as a sentence.** `test_one_account_spamming_is_not_a_crowd`,
  not `test_record_message_3`. The list of test names in a file should read as a description of the
  module.
- **One behaviour per test,** laid out as set-up, action, assertions, separated by blank lines.
- **Fake at the boundary, not inside.** Replace the HTTP transport, the stream, the clock - not the
  service's own functions. If a test needs to patch three internal functions, it wants to be an
  integration test using the helpers above.
- **Never sleep.** Pass the clock in (`ChatSim`), or poll with `wait_until(condition, "what you are
  waiting for")`.
- **Refer to the detector's constants** rather than repeating their current values, so a retune
  doesn't turn into test churn.
- **Give a channel settings the way the service gets them.** Through `POST /tuning` or
  `/channels/<slug>/tuning` in an integration test, as a `detector.Settings` handed to `ChatSim` in a
  unit test. A constant changed with `monkeypatch` is still what a channel without settings runs on.
- **Use made-up channel and user names** (`some_channel`, `alice`). No real streamer's name, chat or
  footage belongs in the repository.
- **Check the test can fail.** After writing one, break the behaviour it describes and run it.
- **A bug gets a test first.** Reproduce it in a failing test, then fix it.
- **Keep `tests/` formatter-clean**: `python -m ruff format tests` before committing.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `SKIPPED ... ffmpeg/ffprobe not found on PATH` | Install a full ffmpeg build (see `CLAUDE.md`). |
| `test tried to reach api.kick.com:443 - the suite must stay offline` | The code under test called Kick without the `kick_api` (or `service`) fixture in place. |
| `a browser launch was attempted` | Something reached `get_stream_urls` or `create_clip` unreplaced; use the `recording` fixture. |
| `os._exit(0) was called` | The shutdown path ran in a test that didn't ask for `process_exits`. |
| A run hangs | After 120 seconds pytest prints every thread's stack (`faulthandler_timeout`); the test at the bottom of the main thread's stack is the one. |
| A test about "after the trigger" fails only on Windows | `time.monotonic()` ticks every ~16 ms there, so two things done back to back can share a timestamp; see `Service.clock_tick`. |
| `kick-clip-hunter-tests-*` directories pile up in the temp directory | Left by runs that were killed before they could clean up; safe to delete. |
