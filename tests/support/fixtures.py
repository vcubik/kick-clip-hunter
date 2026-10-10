"""Fixtures for tests that run several parts of the service together.

Imported into tests/conftest.py, which is what makes them available to every
test directory. Each one stands in for something the service normally reaches
outside the process for: a live HLS stream, kick.com's stream URL, Kick's and
7TV's HTTP APIs, Kick's webhook signatures.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from kick_clip_hunter import db, detector, recorder
from kick_clip_hunter.kick_stream import StreamUrls
from kick_clip_hunter.recorder import ChannelRecorder
from tests.support.hls import FakeHlsServer, Segment
from tests.support.kick_api import FakeKickApi
from tests.support.media import make_ts_segments, require_ffmpeg
from tests.support.waiting import async_wait_until
from tests.support.webhook import WebhookSigner, chat_payload


@pytest.fixture
def hls() -> Iterator[Callable[..., FakeHlsServer]]:
    """Starts fake live streams on loopback; they are shut down afterwards."""
    servers: list[FakeHlsServer] = []

    def start(timeline: list[Segment], **kwargs) -> FakeHlsServer:
        server = FakeHlsServer(timeline, **kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


class Recording:
    """A real `ChannelRecorder` pointed at a fake stream instead of kick.com."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self._recorders: list[ChannelRecorder] = []
        # Channel slugs the recorder asked a stream URL for, in order - each
        # one would have been a browser window against the real site.
        self.url_requests: list[str] = []

    def of(
        self, server: FakeHlsServer, channel: str = "some_channel", vod: FakeHlsServer | None = None
    ) -> ChannelRecorder:
        """`vod` is a second fake stream standing in for the broadcast's own
        recording; without one the channel has no VOD."""

        def stream_urls(slug: str) -> StreamUrls:
            self.url_requests.append(slug)
            return StreamUrls(live=server.master_url, vod=vod.master_url if vod else None)

        self._monkeypatch.setattr(recorder, "get_stream_urls", stream_urls)
        channel_recorder = ChannelRecorder(channel)
        self._recorders.append(channel_recorder)
        return channel_recorder

    def stop_all(self) -> None:
        for channel_recorder in self._recorders:
            channel_recorder.stop()


@pytest.fixture
def recording(hls, monkeypatch: pytest.MonkeyPatch) -> Iterator[Recording]:
    # Depends on `hls` so recorders are stopped before their servers go away.
    monkeypatch.setattr(recorder, "PLAYLIST_POLL_SECONDS", 0.01)
    rig = Recording(monkeypatch)
    yield rig
    rig.stop_all()


@pytest.fixture(scope="session")
def signer() -> WebhookSigner:
    """One RSA key pair for the session - generating one takes a moment."""
    return WebhookSigner()


@pytest.fixture
def kick_api(signer: WebhookSigner, monkeypatch: pytest.MonkeyPatch) -> FakeKickApi:
    """Routes every `httpx.AsyncClient` the service creates to a fake Kick /
    7TV API. Clients given an explicit transport (the ASGI test client) are
    left alone."""
    api = FakeKickApi(public_key_pem=signer.public_key_pem)

    class RoutedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs) -> None:
            kwargs.setdefault("transport", api.transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RoutedAsyncClient)
    return api


@pytest.fixture(scope="session")
def ts_segments(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """Forty consecutive one-second MPEG-TS segments of real video + audio,
    generated once per session."""
    require_ffmpeg()
    return make_ts_segments(tmp_path_factory.mktemp("media"), count=40, seconds_each=1.0)


# -- the running service ---------------------------------------------------


class FastSleep:
    """Stands in for the `asyncio` module inside main.py so that its waits
    (for post-roll footage to be broadcast, between session polls) run
    `speedup` times faster. Everything else is the real asyncio."""

    def __init__(self, speedup: float) -> None:
        self._speedup = speedup

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    async def sleep(self, seconds: float, result=None):
        return await asyncio.sleep(seconds / self._speedup, result)


class Service:
    """The FastAPI app with everything around it faked: requests go in through
    an in-process ASGI client, Kick's API and webhook signatures through
    `FakeKickApi` and `WebhookSigner`.

    Background work the app starts while handling a request (moment sessions,
    clip cutting) runs on the test's own event loop, so `settle()` can wait
    for all of it deterministically.
    """

    def __init__(self, main, client: httpx.AsyncClient, api: FakeKickApi, signer: WebhookSigner) -> None:
        self.main = main
        self.client = client
        self.api = api
        self.signer = signer
        self._user_ids: dict[str, int] = {}

    def watch(
        self,
        slug: str,
        broadcaster_user_id: int | None = None,
        *,
        live: bool = True,
        live_for_seconds: int = 3600,
        keywords: dict[str, float] | None = None,
        subscribed: bool = True,
    ) -> int:
        """Puts a channel on the watchlist, both locally and on the fake Kick side."""
        user_id = broadcaster_user_id or 1000 + len(self._user_ids)
        self._user_ids[slug] = user_id
        conn = db.get_connection()
        try:
            db.add_streamer(conn, user_id, slug)
            if keywords:
                db.replace_channel_keywords(conn, user_id, keywords)
        finally:
            conn.close()
        started = datetime.now(timezone.utc) - timedelta(seconds=live_for_seconds)
        self.api.add_channel(slug, user_id, is_live=live, start_time=started.isoformat() if live else None)
        if subscribed:
            self.api.subscribed.append(user_id)
        return user_id

    def user_id(self, slug: str) -> int:
        return self._user_ids.setdefault(slug, 9000 + len(self._user_ids))

    async def deliver(self, payload: dict, event_type: str = "chat.message.sent", **header_overrides: str):
        """POSTs one webhook delivery, correctly signed unless headers are overridden."""
        body, headers = self.signer.delivery(payload, event_type)
        headers.update(header_overrides)
        return await self.client.post("/webhooks/kick", content=body, headers=headers)

    async def chat(
        self, channel: str, sender: str, content: str, *, emote_positions: int = 0, identity: dict | None = None
    ):
        payload = chat_payload(channel, self.user_id(channel), sender, content, emote_positions, identity)
        return await self.deliver(payload)

    def warm_up(self, channel: str, chatters: int = 10) -> None:
        """Gives the detector five minutes of ordinary history for a channel,
        ending now on the real clock - webhook messages sent afterwards
        continue that same timeline."""
        now = time.monotonic()
        seconds = int(detector.BASELINE_WINDOW_SECONDS)
        for second in range(seconds, 0, -1):
            content = "xd" if second == seconds // 2 else f"message {second}"
            detector.record_message(
                channel,
                sender=f"viewer{second % chatters}",
                content=content,
                laugh_weight=detector.classify_message(content)[0],
                now=now - second,
            )

    async def crowd_laughs(self, channel: str, people: int = 8) -> None:
        for number in range(people):
            response = await self.chat(channel, f"fan{number}", "xDDD")
            assert response.status_code == 200, response.text

    async def clock_tick(self) -> None:
        """Waits for the detector's clock to move on. `time.monotonic()` only
        advances every ~16 ms on Windows, so two things done back to back
        can carry the same timestamp; call this when a test needs "strictly
        after" to be true on the clock too."""
        started = time.monotonic()
        while time.monotonic() == started:
            await asyncio.sleep(0.002)

    async def settle(self, timeout: float = 20.0) -> None:
        """Waits until no moment is open and no background task is running."""
        await async_wait_until(
            lambda: not self.main._moment_sessions and not self.main._background_tasks,
            "moment sessions and background tasks to finish",
            timeout=timeout,
        )

    def rows(self, query: str, *params) -> list[sqlite3.Row]:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def moments(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM moments ORDER BY id")

    def chat_log(self) -> list[tuple[str, str, str]]:
        return [
            (row["channel_slug"], row["sender_username"], row["content"])
            for row in self.rows("SELECT * FROM chat_messages ORDER BY received_at, rowid")
        ]


@pytest.fixture
async def service(kick_api: FakeKickApi, signer: WebhookSigner, monkeypatch: pytest.MonkeyPatch):
    from kick_clip_hunter import main

    # main.py's waits are real seconds in production (post-roll footage,
    # session polling); run them a thousand times faster here. The two limits
    # it measures on the wall clock are shortened to match.
    monkeypatch.setattr(main, "asyncio", FastSleep(speedup=1000))
    monkeypatch.setattr(main, "MOMENT_SESSION_QUIET_SECONDS", 0.05)
    monkeypatch.setattr(main, "MOMENT_SESSION_MAX_SECONDS", 2.0)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield Service(main, client, kick_api, signer)
        # Nothing the test started may outlive it on this event loop.
        leftovers = list(main._background_tasks)
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)
