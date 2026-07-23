"""Local HTTP proxy so ffmpeg can follow a Kick HLS stream whose manifest
URLs exceed ffmpeg's own hardcoded URL-length limit.

The playback URL kick_stream.py captures (an AWS MediaTailor SSAI stream)
carries a signed JWT token that pushes both the master playlist URL and the
per-quality variant/media playlist URL past ~4096 bytes. ffmpeg's
libavformat http protocol silently truncates request URLs at that length,
corrupting the token and getting a 400 back - confirmed with
`-loglevel trace`, whose logged GET line cuts off mid-token, while curl and
httpx (no such limit) succeed on the byte-for-byte identical URL. Segment
URLs are well under 1KB, so ffmpeg fetches those directly without issue.

The fix: point ffmpeg at this local server instead of the real URL. It
fetches the real master/variant playlists itself (httpx, no length limit)
and rewrites only the variant-playlist line(s) in the master to point back
at itself; segment URLs inside the variant playlist are passed through
unchanged since ffmpeg can fetch those on its own.
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

logger = logging.getLogger("kick_clip_hunter")

FETCH_TIMEOUT_SECONDS = 10


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # ffmpeg polls this constantly - don't spam stderr

    def do_GET(self):
        if self.path == "/master.m3u8":
            self._serve(self.server.master_url, is_master=True)
            return
        if self.path.startswith("/variant/"):
            key = self.path[len("/variant/"):].removesuffix(".m3u8")
            real_url = self.server.variant_urls.get(key)
            if real_url is not None:
                self._serve(real_url, is_master=False)
                return
        self.send_error(404)

    def _serve(self, real_url: str, is_master: bool) -> None:
        try:
            response = httpx.get(real_url, timeout=FETCH_TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("HLS proxy: failed to fetch %s", real_url)
            self.send_error(502)
            return

        text = self._rewrite_variants(response.text) if is_master else response.text
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rewrite_variants(self, master_text: str) -> str:
        lines = []
        next_index = 0
        for line in master_text.splitlines():
            if line.startswith("http"):
                key = str(next_index)
                self.server.variant_urls[key] = line.strip()
                lines.append(f"/variant/{key}.m3u8")
                next_index += 1
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"


class HlsProxy:
    """Serves one master playlist (and the variant playlists it lists) on
    127.0.0.1 for the lifetime of one ffmpeg run."""

    def __init__(self, master_url: str):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.master_url = master_url
        self._server.variant_urls = {}
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/master.m3u8"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
