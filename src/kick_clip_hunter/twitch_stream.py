"""Finds where a live Twitch channel's stream is.

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
codecs as fragmented MP4, which the recorder does not keep.
"""

from __future__ import annotations

import logging

from streamlink import Streamlink
from streamlink.exceptions import StreamlinkError

from .platforms import StreamUrlError, StreamUrls

# Importing streamlink renames the standard log levels in small letters, for
# every logger in the process. The service's log keeps them as they were.
for _level in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG):
    logging.addLevelName(_level, logging.getLevelName(_level).upper())

CHANNEL_URL = "https://www.twitch.tv/{login}"


def get_stream_urls(login: str) -> StreamUrls:
    """The master playlist of a live channel's stream. Blocks while it is
    asked for; raises StreamUrlError if the channel is not live, or Twitch
    would not say where its stream is."""
    try:
        streams = Streamlink().streams(CHANNEL_URL.format(login=login))
    except StreamlinkError as error:
        raise StreamUrlError(f"Twitch did not say where the stream of {login!r} is: {error}") from error
    stream = streams.get("best")
    if stream is None:
        raise StreamUrlError(f"No live stream found for {login!r} on Twitch - is it live?")
    return StreamUrls(live=stream.to_manifest_url())
