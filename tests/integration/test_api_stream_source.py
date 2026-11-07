"""Where a Kick channel's stream is followed from, chosen on the Channels
page: live, its address found in a browser window, or from the VOD Kick
writes as it goes along (streamers.stream_from_vod) - which the recording
loop hands its recorder."""

from __future__ import annotations

import pytest

from kick_clip_hunter import dashboard_view, db
from tests.support.html import parse

CHANNEL = "some_channel"
USER_ID = 41

pytestmark = pytest.mark.anyio


@pytest.fixture
def channel(service):
    """A Kick channel on the watchlist, tracked for clips."""
    service.watch(CHANNEL, broadcaster_user_id=USER_ID)
    return CHANNEL


def stored(slug: str):
    conn = db.get_connection()
    try:
        return db.get_streamer_by_slug(conn, slug)
    finally:
        conn.close()


async def choose(service, source: str, channel: str = CHANNEL):
    return await service.client.post(f"/channels/{channel}/stream_source?source={source}")


async def offered(service, slug: str = CHANNEL):
    page = parse((await service.client.get("/dashboard/channels")).text)
    return page.find("span", aria_label=f"Video of {slug}")


async def test_live_to_begin_with(service, channel):
    (choice,) = await offered(service)

    lit = [button.attrs["data-value"] for button in choice.find("button", aria_pressed="true")]
    assert lit == [dashboard_view.STREAM_SOURCE_LIVE]
    assert service.main._streams_from_vod(CHANNEL) is False


async def test_from_the_vod_is_kept_said_and_heeded(service, channel):
    response = await choose(service, dashboard_view.STREAM_SOURCE_VOD)

    assert response.json() == {
        "slug": CHANNEL,
        "source": "vod",
        "said": dashboard_view.STREAM_SOURCE_WORDS["vod"].format(channel=CHANNEL),
    }
    assert stored(CHANNEL)["stream_from_vod"] == 1
    assert service.main._streams_from_vod(CHANNEL) is True
    (choice,) = await offered(service)
    assert [button.attrs["data-value"] for button in choice.find("button", aria_pressed="true")] == ["vod"]

    await choose(service, dashboard_view.STREAM_SOURCE_LIVE)

    assert service.main._streams_from_vod(CHANNEL) is False


async def test_it_is_hidden_while_the_channel_is_tracked_for_moments(service, channel):
    await service.client.post(f"/channels/{CHANNEL}/tracking?what=moments")

    page = parse((await service.client.get("/dashboard/channels")).text)

    assert "hidden" in page.one("span", data_stream_source=True).attrs


async def test_a_channel_of_another_platform_is_neither_offered_it_nor_let_have_it(service):
    conn = db.get_connection()
    try:
        db.add_platform_streamer(conn, "twitch", "123", "someone@twitch")
    finally:
        conn.close()

    assert await offered(service, "someone@twitch") == []
    assert (await choose(service, "vod", "someone@twitch")).status_code == 400
    assert service.main._streams_from_vod("someone@twitch") is False


async def test_an_unknown_source_or_channel_is_refused(service, channel):
    assert (await choose(service, "browser")).status_code == 400
    assert (await choose(service, "vod", "nobody")).status_code == 404
