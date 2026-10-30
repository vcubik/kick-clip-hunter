"""Finds where a live Twitch channel's stream is - and the recording of it
Twitch makes as it goes along (its VOD).

Twitch's API has no way to watch a stream: the address a player follows is
handed out by the site's own playback service, for a token the site's own
client asks for. Streamlink knows how to ask for one - and keeps up with
Twitch changing how - so it is used here for that and nothing else: it
answers with the stream's master playlist, and the recorder follows the
stream from there as it follows any (see recorder.py). No browser is
involved; the token is asked for over plain HTTP.

The playlists are of the same make as Kick's (both come from Amazon's IVS):
2s MPEG-TS segments with their program date-time, and ads stitched in that
replace the broadcast for their length. Streamlink asks only for H.264 unless
told otherwise, which is what the recorder needs: Twitch serves its newer
codecs as fragmented MP4, which the recorder does not keep from a live
stream.

What was on while an ad covered the stream is in its VOD, where the channel
keeps its past broadcasts: Helix says which of a channel's recordings is the
one of the stream that is on (by the stream's id), and streamlink where its
playlist is. Twitch's VODs are fragmented MP4 on the same program clock -
the recorder keeps their fragments for the clips that need them.

A past broadcast stays there once its stream is over - for a week to two
months, as long as Twitch keeps the channel's - and `vod_at` finds the one
of any moment by its time (Helix gives when each began and how long it
runs). Its playlist is seen to be another make then: 10s MPEG-TS segments
without a program date-time, so where footage of it is is counted from
when the broadcast began, to a few seconds (see vod_footage.py). Where
Twitch found music in it that it may not play, a segment's address says
"-muted": its sound is silence.

A person watches a past broadcast at twitch.tv/videos/<id>, from a time
the address says (`?t=1h2m3s`) - `watch_link_at` gives that of the one a
moment was in, from when the moment's clip would begin.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

import httpx

from . import twitch_client
from .config import load_settings
from .platforms import NoVod, StreamUrlError, StreamUrls, Vod
from .streamlink_support import NOT_INSTALLED, Streamlink, StreamlinkError

logger = logging.getLogger("kick_clip_hunter")

CHANNEL_URL = "https://www.twitch.tv/{login}"
VIDEO_URL = "https://www.twitch.tv/videos/{video_id}"
# How many of a channel's past broadcasts are looked through for the one a
# moment was in: as many as Helix gives at once.
ARCHIVES_LOOKED_AT = 100
# How far outside the time Helix gives a past broadcast a moment of it may
# lie: the length of one that is still on lags behind it.
VOD_SLACK_SECONDS = 120
_LENGTH = re.compile(r"(\d+)([hms])")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def _master(url: str) -> str | None:
    """The master playlist streamlink finds at a Twitch address, if any."""
    if Streamlink is None:  # pragma: no cover - see the import above
        raise StreamUrlError(NOT_INSTALLED)
    stream = Streamlink().streams(url).get("best")
    return stream.to_manifest_url() if stream is not None else None


def get_stream_urls(login: str) -> StreamUrls:
    """The master playlist of a live channel's stream, and its VOD's where
    the channel keeps one. Blocks while they are asked for; raises
    StreamUrlError if the channel is not live, or Twitch would not say where
    its stream is."""
    try:
        live = _master(CHANNEL_URL.format(login=login))
    except StreamlinkError as error:
        raise StreamUrlError(f"Twitch did not say where the stream of {login!r} is: {error}") from error
    if live is None:
        raise StreamUrlError(f"No live stream found for {login!r} on Twitch - is it live?")
    return StreamUrls(live=live, vod=_vod(login))


def _vod(login: str) -> str | None:
    """The master playlist of the VOD of a live channel's stream, or None if
    there is none to be had: the channel keeps no past broadcasts, keeps
    them for its subscribers only, or Twitch could not be asked."""
    try:
        # A thread of the recorder's own, with no event loop running in it.
        video_id = asyncio.run(_archive_of_the_stream(login))
        return _master(VIDEO_URL.format(video_id=video_id)) if video_id else None
    except Exception as error:
        logger.info("[%s] the VOD of the stream could not be found (%s)", login, type(error).__name__)
        return None


async def _archive_of_the_stream(login: str) -> str | None:
    """The id of the recording Twitch is making of the channel's stream."""
    settings = load_settings()
    token = await twitch_client.app_token(settings)
    stream = await twitch_client.get_stream(login, settings.twitch_client_id, token)
    if stream is None:
        return None
    for video in await twitch_client.get_archives(stream["user_id"], settings.twitch_client_id, token):
        if str(video.get("stream_id")) == str(stream["id"]):
            return str(video["id"])
    return None


def length_seconds(text: object) -> int:
    """How long Helix says a past broadcast is ("3h2m1s"), in seconds."""
    return sum(int(number) * _UNIT_SECONDS[unit] for number, unit in _LENGTH.findall(str(text or "")))


def _began(video: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(video.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def covering(videos: list, when: datetime, live_stream_id: str | None = None) -> dict | None:
    """Which of a channel's past broadcasts `when` was in: the one whose time
    it lies in, or failing that the nearest that begins or ends no more than
    VOD_SLACK_SECONDS from it - the one of the stream that is on (by its
    id) reaching to now. None if none does. Pure."""
    best: tuple[float, dict] | None = None
    for video in videos:
        began = _began(video) if isinstance(video, dict) else None
        if began is None:
            continue
        on = live_stream_id is not None and str(video.get("stream_id")) == str(live_stream_id)
        ended = None if on else began + timedelta(seconds=length_seconds(video.get("duration")))
        if when < began:
            outside = (began - when).total_seconds()
        elif ended is not None and when > ended:
            outside = (when - ended).total_seconds()
        else:
            outside = 0.0
        if outside <= VOD_SLACK_SECONDS and (best is None or outside < best[0]):
            best = (outside, video)
    return best[1] if best else None


async def _archive_at(login: str, when: datetime) -> dict | None:
    """The past broadcast of the channel's that `when` was in, if Helix
    lists one."""
    settings = load_settings()
    token = await twitch_client.app_token(settings)
    user = await twitch_client.get_user(login, settings.twitch_client_id, token)
    stream = await twitch_client.get_stream(login, settings.twitch_client_id, token)
    archives = await twitch_client.get_archives(user["id"], settings.twitch_client_id, token, ARCHIVES_LOOKED_AT)
    return covering(archives, when, stream["id"] if stream else None)


async def _asked_archive_at(login: str, when: datetime) -> dict:
    """_archive_at, raising NoVod in words that say why where Helix could
    not be asked, or lists none."""
    try:
        video = await _archive_at(login, when)
    except twitch_client.NotSetUp as error:
        raise NoVod(str(error)) from error
    except (httpx.HTTPError, ValueError) as error:
        raise NoVod(f"Twitch could not be asked for the channel's past broadcasts ({type(error).__name__}).") from error
    if video is None:
        raise NoVod(
            "Twitch lists no past broadcast of that stream: the channel keeps none, it was deleted, "
            "or it is older than Twitch keeps them."
        )
    return video


def watch_link(video_id: str, seconds: float) -> str:
    """Where a past broadcast is watched on twitch.tv from `seconds` into
    it, written as Twitch's own share button writes the time ("1h2m3s").
    Pure."""
    whole = max(int(seconds), 0)
    return f"{VIDEO_URL.format(video_id=video_id)}?t={whole // 3600}h{whole % 3600 // 60}m{whole % 60}s"


async def watch_link_at(login: str, when: datetime) -> str:
    """Where the channel's stream that was on at `when` is watched on
    twitch.tv from then on: its past broadcast - of a stream that is on as
    well, which Twitch plays as it goes along. Raises NoVod, saying why,
    where there is none."""
    video = await _asked_archive_at(login, when)
    began = _began(video)
    if began is None:
        raise NoVod("Twitch does not say when the past broadcast of that stream began.")
    return watch_link(str(video["id"]), (when - began).total_seconds())


async def vod_trouble(login: str) -> str | None:
    """What would keep a clip from being cut from the channel's past
    broadcasts, by its newest one as Helix lists it, in words - None where
    nothing seen does, or Twitch could not be asked."""
    try:
        settings = load_settings()
        token = await twitch_client.app_token(settings)
        user = await twitch_client.get_user(login, settings.twitch_client_id, token)
        archives = await twitch_client.get_archives(user["id"], settings.twitch_client_id, token, 1)
    except (twitch_client.NotSetUp, httpx.HTTPError, KeyError, ValueError):
        return None
    if not archives:
        return "Twitch lists no past broadcasts of this channel: if it keeps none, no moment's clip can be cut."
    if archives[0].get("viewable", "public") != "public":
        return "Twitch keeps this channel's past broadcasts from the public: no moment's clip can be cut."
    return None


def vod_at(login: str, when: datetime) -> Vod:
    """The past broadcast of the channel's stream that was on at `when` -
    one that is on, or one that is over - with when it began. Blocks while
    it is asked for; raises NoVod, saying why, where there is none to be
    had."""
    # A thread of its own, with no event loop running in it.
    video = asyncio.run(_asked_archive_at(login, when))
    if video.get("viewable", "public") != "public":
        raise NoVod("Twitch keeps the past broadcast of that stream from the public.")
    try:
        master = _master(VIDEO_URL.format(video_id=video["id"]))
    except StreamlinkError as error:
        raise NoVod(f"Twitch would not say where the past broadcast is: {error}") from error
    if master is None:
        raise NoVod("Twitch would not say where the past broadcast is.")
    return Vod(master=master, started_at=_began(video))
