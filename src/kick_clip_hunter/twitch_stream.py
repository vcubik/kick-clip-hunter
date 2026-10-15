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
"""

from __future__ import annotations

import asyncio
import logging

from . import twitch_client
from .config import load_settings
from .platforms import StreamUrlError, StreamUrls
from .streamlink_support import NOT_INSTALLED, Streamlink, StreamlinkError

logger = logging.getLogger("kick_clip_hunter")

CHANNEL_URL = "https://www.twitch.tv/{login}"
VIDEO_URL = "https://www.twitch.tv/videos/{video_id}"


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
