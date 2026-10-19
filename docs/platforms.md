# Clip creation and the platforms' streams

How video is got from each platform, and why it is done that way. Read before touching `recorder.py`, `kick_stream.py`, a `*_stream.py` module or `clip_creator.py`.
Moved out of CLAUDE.md so that it is read only when needed. Where it says a module is described "below" or "above", see [architecture.md](architecture.md); "Clip creation" is [platforms.md](platforms.md), "Operational quirks" [operations.md](operations.md), "Testing" [testing.md](testing.md) and the dashboard [dashboard.md](dashboard.md).

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
