"""A scriptable live HLS stream on 127.0.0.1.

The recorder is an HLS client: it reads a master playlist, follows one
variant playlist as it grows, and downloads every media segment listed.
`FakeHlsServer` serves exactly that over real HTTP, from a timeline the test
controls - how many segments have been "broadcast" so far, where the stream
has a discontinuity, which requests fail - so the recorder's real networking
and threading code runs against it unmodified.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEGMENT_SECONDS = 2.0


@dataclass
class Segment:
    """One media segment of the broadcast."""

    data: bytes
    duration: float = SEGMENT_SECONDS
    title: str = "live"
    # An EXT-X-DISCONTINUITY tag precedes this segment in the playlist.
    discontinuity: bool = False
    # Extra tag lines emitted right before this segment (e.g. an ad marker).
    tags: tuple[str, ...] = ()


class FakeHlsServer:
    """Serves `/master.m3u8`, two variant playlists and `/segment/<n>.ts`.

    `published` is how many segments of `timeline` exist so far - the live
    edge. The variant playlist lists a sliding window of the most recent
    `window` of them, like a real live stream.
    """

    def __init__(
        self,
        timeline: list[Segment],
        *,
        published: int = 0,
        window: int = 6,
        first_sequence: int = 100,
        program_date_time: bool = True,
        stream_start: datetime | None = None,
    ) -> None:
        self.timeline = timeline
        self.published = published
        self.window = window
        self.first_sequence = first_sequence
        self.program_date_time = program_date_time
        self.stream_start = stream_start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.ended = False
        # Request path -> HTTP statuses to answer with first, one per request,
        # before serving it normally. "playlist" stands for the variant playlist.
        self.fail_next: dict[str, list[int]] = {}
        self.requests: list[str] = []
        self._lock = threading.Lock()

        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # keep test output quiet
                pass

            def do_GET(self):
                server._respond(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # The short poll interval is only about how fast close() returns.
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    # -- test controls ---------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def master_url(self) -> str:
        return f"{self.base_url}/master.m3u8"

    def publish(self, count: int = 1) -> None:
        """Advance the live edge by `count` segments."""
        with self._lock:
            self.published = min(len(self.timeline), self.published + count)

    def publish_all(self) -> None:
        self.publish(len(self.timeline))

    def end_stream(self) -> None:
        with self._lock:
            self.ended = True

    def segment_start(self, index: int) -> datetime:
        """Program date-time of timeline segment `index`."""
        return self.stream_start + timedelta(seconds=sum(s.duration for s in self.timeline[:index]))

    def segment_requests(self) -> list[int]:
        """Timeline indexes of the segments requested so far, in order."""
        with self._lock:
            return [int(path.split("/")[-1].removesuffix(".ts")) for path in self.requests if "/segment/" in path]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    # -- HTTP ------------------------------------------------------------

    def _respond(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path
        with self._lock:
            self.requests.append(path)
            key = "playlist" if path == "/variant/high.m3u8" else path
            forced = self.fail_next.get(key)
            status = forced.pop(0) if forced else None
        if status is not None:
            handler.send_error(status)
            return

        if path == "/master.m3u8":
            # The best variant is deliberately not listed first.
            body = (
                b"#EXTM3U\n"
                b'#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360,CODECS="avc1.4D401F,mp4a.40.2"\n'
                b"variant/low.m3u8\n"
                b'#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2"\n'
                b"variant/high.m3u8\n"
            )
        elif path == "/variant/high.m3u8":
            body = self._variant_playlist().encode()
        elif path == "/variant/low.m3u8":
            body = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        elif path.startswith("/segment/"):
            body = self.timeline[int(path.split("/")[-1].removesuffix(".ts"))].data
        else:
            handler.send_error(404)
            return

        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _variant_playlist(self) -> str:
        with self._lock:
            published, ended = self.published, self.ended
        first = max(0, published - self.window)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:6",
            f"#EXT-X-MEDIA-SEQUENCE:{self.first_sequence + first}",
            '#EXT-X-DATERANGE:ID="playlist",CLASS="timestamp",START-DATE="2026-01-01T12:00:00Z"',
        ]
        for index in range(first, published):
            segment = self.timeline[index]
            if segment.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend(segment.tags)
            if self.program_date_time:
                stamp = self.segment_start(index).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
            lines.append(f"#EXTINF:{segment.duration:.3f},{segment.title}")
            # Relative on purpose: the recorder has to resolve it itself.
            lines.append(f"../segment/{index}.ts")
        if ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


def fake_segments(count: int, **kwargs) -> list[Segment]:
    """`count` segments whose bytes just identify them - enough for anything
    that stores and joins segments without decoding them."""
    return [Segment(data=f"segment-{index:03d};".encode(), **kwargs) for index in range(count)]
