"""A channel tracked for moments, through the service: its moments are found
in its chat alone - no stream of it comes in - kept with a link to them in
the stream's VOD and told of, and a moment's clip is cut from the VOD only
once it is asked for. The VOD is a fake HLS stream that kick.com's fake
lists point to; the cutting is the real ffmpeg."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import dashboard_view, db, detector, kick_stream, recorder, recording_manager, tuning
from kick_clip_hunter.config import load_settings
from tests.support.data import add_moment, moment
from tests.support.hls import Segment
from tests.support.html import parse
from tests.support.kick_api import kick_video, site_video_id
from tests.support.media import decodes_cleanly, probe, require_ffmpeg
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
USER_ID = 41
DASHBOARD = "http://pc.tail1234.ts.net:8000"
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SOURCE = "https://stream.kick.com/ivs/v1/1/abc/media/hls/master.m3u8"
# A clip framed small enough for forty seconds of test footage: two seconds
# either side of the reaction, three of footage around it, no delay.
ROLL, CONTEXT = 2, 3


async def track(service, what: str, channel: str = CHANNEL):
    return await service.client.post(f"/channels/{channel}/tracking?what={what}")


@pytest.fixture
def channel(service):
    """A Kick channel on the watchlist, live, tracked for moments."""
    service.watch(CHANNEL, broadcaster_user_id=USER_ID, live_for_seconds=3600)
    conn = db.get_connection()
    try:
        db.set_streamer_tracking(conn, USER_ID, True, moments_only=True)
    finally:
        conn.close()
    return service.api.channels[CHANNEL]


def stored(slug: str):
    """A channel's row on the watchlist, as it is stored."""
    conn = db.get_connection()
    try:
        return db.get_streamer_by_slug(conn, slug)
    finally:
        conn.close()


async def page_of(service, moment_id: int):
    response = await service.client.get(f"/dashboard/moments/{moment_id}")
    assert response.status_code == 200, response.text
    return parse(response.text)


class TestTrackingAChannelForMoments:
    async def test_a_kick_channel_is_tracked_for_moments_and_no_stream_of_it_comes_in(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        service.api.channels[CHANNEL].videos = [kick_video(START, 3600, SOURCE)]

        response = await track(service, "moments")

        assert response.json() == {"slug": CHANNEL, "tracking": "moments", "said": ""}
        row = stored(CHANNEL)
        assert (row["tracking_enabled"], row["chat_only"], row["moments_only"]) == (1, 0, 1)
        # Looked at - whether it is live - and never recorded for it.
        assert service.main._is_channel_tracked(CHANNEL) is False
        assert service.main._is_tracked_by_chat(CHANNEL) is True

    async def test_clips_set_it_back_to_having_its_stream_come_in(self, service, channel):
        await track(service, "clips")

        assert service.main._is_channel_tracked(CHANNEL) is True
        assert service.main._is_tracked_by_chat(CHANNEL) is False

    async def test_vods_kept_for_subscribers_once_a_stream_is_over_are_said(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        service.api.channels[CHANNEL].videos = [kick_video(START, 3600, SOURCE, status="subscriber_only")]

        response = await track(service, "moments")

        assert response.status_code == 200
        assert response.json()["said"] == (
            "Kick keeps this channel's VODs for its subscribers only once a stream is over: "
            "a moment's clip can be cut only while its stream is still on."
        )

    async def test_kick_not_answering_says_nothing_and_holds_nothing_up(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        service.api.failures["web.kick.com"] = 503

        response = await track(service, "moments")

        assert response.json() == {"slug": CHANNEL, "tracking": "moments", "said": ""}

    @pytest.mark.parametrize(("platform", "title"), [("youtube", "YouTube"), ("tiktok", "TikTok")])
    async def test_a_platform_that_keeps_no_vod_once_a_stream_is_over_cannot_be(self, service, platform, title):
        slug = f"someone@{platform}"
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, platform, "UC123", slug)
        finally:
            conn.close()

        response = await track(service, "moments", slug)

        assert response.status_code == 409
        assert title in response.json()["detail"]
        assert stored(slug)["moments_only"] == 0

    async def test_while_it_is_live_only_its_chat_comes_in(self, service, channel, monkeypatch):
        now = datetime.now(timezone.utc)
        monkeypatch.setitem(
            recording_manager._looks, CHANNEL, recording_manager.Look(at=now, live_since=now - timedelta(minutes=5))
        )

        page = parse((await service.client.get("/dashboard/channels")).text)

        right_now = page.one("td", data_now=CHANNEL)
        assert right_now.text == "Live, chat only"
        # The lamp is for a stream coming in.
        assert right_now.find("span", class_="ch-lamp") == []

    async def test_the_channels_page_offers_moments_only_where_a_vod_stays(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "youtube", "UC123", "someone@youtube")
        finally:
            conn.close()

        page = parse((await service.client.get("/dashboard/channels")).text)

        def offered(slug: str) -> list[str]:
            choice = page.one("span", aria_label=f"Tracking {slug}")
            return [button.attrs["data-value"] for button in choice.find("button")]

        assert offered(CHANNEL) == ["off", "chat", "moments", "clips"]
        assert offered("someone@youtube") == ["off", "chat", "clips"]


class TestAMomentCaught:
    @pytest.fixture
    def notified(self, service, channel, telegram, monkeypatch):
        """Notes go to Telegram, with links to the dashboard; the channel's
        stream is on, and kick.com lists its VOD."""
        monkeypatch.setenv("DASHBOARD_URL", DASHBOARD)
        monkeypatch.setattr(service.main, "settings", load_settings())
        service.main._notifier = None
        began = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
        channel.videos = [kick_video(began, 0, SOURCE, live=True)]

        async def no_footage(*args, **kwargs):
            raise AssertionError("no clip is cut of a moment of a channel tracked for moments")

        monkeypatch.setattr(recording_manager, "create_clip_for_moment", no_footage)
        return began

    async def react(self, service) -> dict:
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=detector._dynamic_min_reaction_unique(10))
        await service.settle()
        (found,) = service.moments()
        return dict(found)

    async def test_it_is_kept_without_a_clip_with_where_it_is_watched_in_the_vod(self, service, notified, channel):
        found = await self.react(service)

        assert (found["clip_on_request"], found["clip_path"]) == (1, None)
        assert found["laughing"] is not None
        # From where its clip would begin: before the reaction by the
        # pre-roll and the playback delay, into the VOD that began an hour ago
        # - and the link a little later, nearer to what chat reacted to.
        tuned = tuning.recommended()
        first_at = datetime.fromisoformat(found["window_start"]) - timedelta(
            seconds=tuned.pre_roll_seconds + tuned.playback_delay_seconds
        )
        seconds = int((first_at - notified).total_seconds()) + kick_stream.WATCH_LATER_SECONDS
        assert found["watch_url"] == f"https://kick.com/{CHANNEL}/videos/{site_video_id(channel.videos[0])}?t={seconds}"
        assert recording_manager._recorders.get(CHANNEL) is None

    async def test_it_is_told_of_with_the_vod_and_the_dashboard_to_go_to(self, service, notified, telegram):
        found = await self.react(service)

        (message,) = telegram.sent
        assert message["shown"].split("\n")[0] == f"{CHANNEL} · Moment caught"
        assert message["shown"].split("\n")[-3:] == [
            "No clip has been cut: ask for one on the dashboard.",
            "Watch on Kick",
            "Open on the dashboard",
        ]
        assert message["links"] == [found["watch_url"], f"{DASHBOARD}/dashboard?moment={found['id']}"]

    async def test_switched_off_it_is_not_told_of(self, service, notified, telegram):
        await service.client.post("/settings/notify_moments?enabled=0")

        found = await self.react(service)

        assert telegram.sent == []
        assert found["watch_url"] is not None

    async def test_one_kick_lists_no_vod_of_is_kept_and_told_of_without_the_link(
        self, service, notified, channel, telegram
    ):
        channel.videos = []

        found = await self.react(service)

        assert (found["clip_on_request"], found["watch_url"]) == (1, None)
        (message,) = telegram.sent
        assert message["links"] == [f"{DASHBOARD}/dashboard?moment={found['id']}"]


@pytest.mark.ffmpeg
class TestItsClipCutWhenAskedFor:
    @pytest.fixture(autouse=True)
    def small_clips(self, monkeypatch):
        require_ffmpeg()
        monkeypatch.setattr(recorder, "PRE_ROLL_SECONDS", ROLL)
        monkeypatch.setattr(tuning, "DYNAMIC_POST_ROLL_SECONDS", ROLL)
        monkeypatch.setattr(recorder, "PLAYBACK_DELAY_SECONDS", 0)
        monkeypatch.setattr(recorder, "CONTEXT_BEFORE_SECONDS", CONTEXT)
        monkeypatch.setattr(recorder, "CONTEXT_AFTER_SECONDS", CONTEXT)

    @pytest.fixture
    def vod(self, hls, ts_segments, channel):
        """The broadcast's VOD in five-second segments, listed by kick.com."""
        timeline = [
            Segment(b"".join(path.read_bytes() for path in ts_segments[first : first + 5]), duration=5.0)
            for first in range(0, len(ts_segments), 5)
        ]
        server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)
        channel.videos = [kick_video(START, 3600, server.master_url)]
        return server

    @pytest.fixture
    def asked_for(self, channel) -> int:
        """A moment kept on a channel tracked for moments: chat reacted 20 to
        24 seconds into the VOD."""
        return add_moment(
            CHANNEL,
            broadcaster_user_id=USER_ID,
            detected_at=START + timedelta(seconds=24),
            reaction_seconds=4,
            clip_on_request=1,
            laughing=9,
        )

    async def ask(self, service, moment_id: int):
        return await service.client.post(f"/moments/{moment_id}/clip")

    async def standing(self, service, moment_id: int) -> dict:
        response = await service.client.get(f"/moments/{moment_id}/clip")
        assert response.status_code == 200, response.text
        return response.json()

    async def test_the_clip_is_cut_from_the_vod_with_the_footage_around_it(self, service, vod, asked_for):
        response = await self.ask(service, asked_for)
        assert response.status_code == 200, response.text
        await service.settle()

        row = moment(asked_for)
        assert row["clip_path"] == f"{CHANNEL}/moment_{asked_for}.mp4"
        # The reaction, 20-24 s, two seconds either side: 18-26 s - with
        # three seconds of footage before and after it in its file.
        assert row["clip_start"] == (START + timedelta(seconds=18)).isoformat()
        assert row["clip_duration"] == pytest.approx(8, abs=0.01)
        assert (row["context_before"], row["context_after"]) == (pytest.approx(3, abs=0.01), pytest.approx(3, abs=0.15))
        file = recorder.CLIPS_DIR / CHANNEL / f"moment_{asked_for}.mp4"
        assert probe(file)["duration"] == pytest.approx(14, abs=0.15)
        assert decodes_cleanly(file)
        assert sorted(path.name for path in file.parent.iterdir()) == [file.name]
        # The VOD's segments 15-20, 20-25 and 25-30, and no more.
        assert sorted(vod.segment_requests()) == [3, 4, 5]

    async def test_it_says_it_is_done_and_the_page_plays_it(self, service, vod, asked_for):
        await self.ask(service, asked_for)
        await service.settle()

        assert await self.standing(service, asked_for) == {
            "moment_id": asked_for,
            "state": dashboard_view.FOOTAGE_DONE,
            "busy": False,
            "words": "The clip was cut from the stream's VOD.",
        }
        page = await page_of(service, asked_for)
        assert page.one("video", data_clip=True).attrs["data-lead"] == "3"
        assert page.find(data_vod_clip=True) == []

    async def test_a_stream_kick_lists_no_vod_of_says_why_and_leaves_the_moment_as_it_was(
        self, service, channel, asked_for
    ):
        channel.videos = []

        await self.ask(service, asked_for)
        await service.settle()

        standing = await self.standing(service, asked_for)
        assert (standing["state"], standing["busy"]) == (dashboard_view.FOOTAGE_FAILED, False)
        assert standing["words"].startswith("Kick lists no VOD of that stream")
        assert moment(asked_for)["clip_path"] is None
        assert (await page_of(service, asked_for)).one("p", data_status=True).text == standing["words"]

    async def test_a_vod_that_holds_only_the_footage_before_the_clip_cuts_none(
        self, service, hls, ts_segments, channel, asked_for
    ):
        # One-second segments, the first 18 of them: up to where the clip
        # would begin.
        timeline = [Segment(path.read_bytes(), duration=1.0) for path in ts_segments]
        server = hls(timeline, published=18, window=len(timeline), stream_start=START)
        channel.videos = [kick_video(START, 3600, server.master_url)]

        await self.ask(service, asked_for)
        await service.settle()

        standing = await self.standing(service, asked_for)
        assert (standing["state"], standing["words"]) == (
            dashboard_view.FOOTAGE_FAILED,
            "The VOD holds nothing of the clip itself, only of the footage around it.",
        )
        assert moment(asked_for)["clip_path"] is None
        assert list((recorder.CLIPS_DIR / CHANNEL).iterdir()) == []

    async def test_footage_that_cannot_be_put_together_fails_and_leaves_nothing(self, service, hls, channel, asked_for):
        timeline = [Segment(b"not a video at all", duration=5.0) for _ in range(8)]
        server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)
        channel.videos = [kick_video(START, 3600, server.master_url)]

        await self.ask(service, asked_for)
        await service.settle()

        assert await self.standing(service, asked_for) == {
            "moment_id": asked_for,
            "state": dashboard_view.FOOTAGE_FAILED,
            "busy": False,
            "words": "The clip could not be cut. The server log says why.",
        }
        assert moment(asked_for)["clip_path"] is None
        assert list((recorder.CLIPS_DIR / CHANNEL).iterdir()) == []

    async def test_asked_again_while_it_goes_on_nothing_more_is_started(self, service, vod, asked_for):
        service.main._vod_clips[asked_for] = {"state": dashboard_view.FOOTAGE_FETCHING, "done": 0.5}

        response = await self.ask(service, asked_for)

        assert response.json()["words"] == "Fetching the moment's footage from the VOD: 50 %."
        assert not service.main._background_tasks

    async def test_while_it_goes_on_the_moment_is_not_removed(self, service, vod, asked_for):
        service.main._vod_clips[asked_for] = {"state": dashboard_view.FOOTAGE_JOINING}

        response = await service.client.post(f"/moments/{asked_for}/delete")

        assert response.status_code == 409
        assert response.json()["detail"] == service.main.VOD_CLIP_BUSY_WORDS
        assert "data-busy" in (await page_of(service, asked_for)).one("div", data_vod_clip=True).attrs

    async def test_it_waits_for_the_vod_to_reach_the_end_of_the_moment_and_gives_up_at_shutdown(
        self, service, vod, channel, monkeypatch
    ):
        # Chat reacted just now: the VOD runs behind the stream that is on.
        moment_id = add_moment(
            CHANNEL, broadcaster_user_id=USER_ID, detected_at=datetime.now(timezone.utc), clip_on_request=1, laughing=9
        )

        await self.ask(service, moment_id)
        await async_wait_until(
            lambda: service.main._vod_clips.get(moment_id, {}).get("state") == dashboard_view.VOD_CLIP_WAITING,
            "the clip to wait for the VOD",
        )
        words = (await self.standing(service, moment_id))["words"]
        assert words.startswith("Waiting for the stream's VOD to reach the end of the moment: ")

        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        await service.settle()

        assert moment_id not in service.main._vod_clips
        assert moment(moment_id)["clip_path"] is None


class TestWhatCanBeAskedFor:
    async def test_a_moment_whose_clip_is_cut_as_it_is_found_is_not_cut_again(self, service):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID)

        response = await service.client.post(f"/moments/{moment_id}/clip")

        assert response.status_code == 409
        assert "tracked for moments" in response.json()["detail"]

    async def test_one_with_its_clip_already_is_not_cut_again(self, service):
        moment_id = add_moment(CHANNEL, clip_on_request=1, laughing=9, clip_path=f"{CHANNEL}/moment_1.mp4")

        response = await service.client.post(f"/moments/{moment_id}/clip")

        assert (response.status_code, response.json()["detail"]) == (409, "The moment has its clip already.")

    async def test_not_while_chat_is_still_reacting_to_it(self, service):
        moment_id = add_moment(CHANNEL, clip_on_request=1)

        response = await service.client.post(f"/moments/{moment_id}/clip")

        assert response.status_code == 409
        assert response.json()["detail"].startswith("Chat is still reacting to the moment")
        assert not service.main._background_tasks

    async def test_an_unknown_moment_is_not_found(self, service):
        assert (await service.client.post("/moments/999/clip")).status_code == 404
        assert (await service.client.get("/moments/999/clip")).status_code == 404


class TestTheReviewPage:
    async def test_a_moment_without_its_clip_offers_to_cut_it_and_leads_to_the_vod(self, service, channel):
        moment_id = add_moment(
            CHANNEL,
            broadcaster_user_id=USER_ID,
            clip_on_request=1,
            laughing=9,
            watch_url="https://kick.com/some_channel/videos/v1?t=3565",
        )

        page = await page_of(service, moment_id)

        block = page.one("div", data_vod_clip=f"/moments/{moment_id}/clip")
        assert "data-busy" not in block.attrs
        assert block.one("button", data_vod_clip_go=True).text == "Cut the clip"
        assert block.one("p").text.startswith("No clip has been cut of this moment yet")
        watch = page.one("a", data_watch=True)
        assert (watch.attrs["href"], watch.text) == ("https://kick.com/some_channel/videos/v1?t=3565", "Watch on Kick")
        assert watch.attrs["target"] == "_blank"
        assert page.find("video", data_clip=True) == []

    async def test_a_moment_found_in_no_vod_has_no_link_to_one(self, service, channel):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID, clip_on_request=1, laughing=9)

        page = await page_of(service, moment_id)

        assert page.find("a", data_watch=True) == []
        assert page.one("div", data_vod_clip=True)

    async def test_a_moment_of_a_channel_tracked_for_clips_has_none_of_it(self, service):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID)

        page = await page_of(service, moment_id)

        assert page.find(data_vod_clip=True) == [] and page.find("a", data_watch=True) == []
