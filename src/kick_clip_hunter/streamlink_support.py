"""Streamlink, which finds where Twitch's and YouTube's streams are
(twitch_stream.py, youtube_stream.py) - imported here, once for both.

A service upgraded without it still starts, and records its Kick channels:
only a stream streamlink would have found cannot be found (NOT_INSTALLED).
And importing it renames the standard log levels in small letters, for
every logger in the process: the service's log keeps them as they were.
"""

from __future__ import annotations

import logging

try:
    from streamlink import Streamlink
    from streamlink.exceptions import StreamlinkError
except ImportError:  # pragma: no cover - installed wherever the suite runs
    Streamlink = None  # type: ignore[assignment, misc]

    class StreamlinkError(Exception):  # type: ignore[no-redef]
        """Stands in for streamlink's own, which nothing can raise now."""


NOT_INSTALLED = "streamlink is not installed: pip install -r requirements-core.txt"

for _level in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG):
    logging.addLevelName(_level, logging.getLevelName(_level).upper())

__all__ = ["NOT_INSTALLED", "Streamlink", "StreamlinkError"]
