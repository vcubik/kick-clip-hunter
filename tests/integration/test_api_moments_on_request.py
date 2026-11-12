"""A channel tracked for moments, through the service: its moments are found
in its chat alone - no stream of it comes in - kept with a link to them in
the stream's VOD and told of, played from the VOD on the review page, and a
moment's clip is cut from the VOD only once it is asked for. The VOD is a
fake HLS stream that kick.com's fake lists point to; the cutting is the real
ffmpeg."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import (
    dashboard_view,
    db,
    detector,
    platforms,
    recorder,
    recording_manager,
    tuning,
    twitch_stream,
    vod_footage,
    vod_relay,
)
from kick_clip_hunter.config import load_settings
from tests.support.data import add_moment, moment
from tests.support.hls import Segment, fake_segments
from tests.support.html import parse
from tests.support.kick_api import kick_video, site_video_id
from tests.support.media import decodes_cleanly, probe, require_ffmpeg
from tests.support.twitch_api import past_broadcast
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


class TestHowItsClipsAreCut:
    """A channel tracked for moments has their clips cut from its stream's
    VOD when asked for, or each by itself as soon as the VOD holds it."""

    async def cut(self, service, how: str, channel: str = CHANNEL):
        return await service.client.post(f"/channels/{channel}/moments_auto_cut?how={how}")

    async def offered(self, service, slug: str = CHANNEL):
        page = parse((await service.client.get("/dashboard/channels")).text)
        return page.one("span", aria_label=f"Clips of {slug}'s moments")

    async def test_when_asked_to_begin_with(self, service, channel):
        assert stored(CHANNEL)["moments_auto_cut"] == 0

        choice = await self.offered(service)

        lit = [button.attrs["data-value"] for button in choice.find("button", aria_pressed="true")]
        assert lit == [dashboard_view.MOMENT_CLIPS_ON_REQUEST]

    async def test_automatically_is_kept_and_said(self, service, channel):
        response = await self.cut(service, dashboard_view.MOMENT_CLIPS_AUTOMATIC)

        assert response.json() == {
            "slug": CHANNEL,
            "how": "automatic",
            "said": f"Each moment of {CHANNEL} has its clip cut from the VOD by itself, as soon as the VOD holds it.",
        }
        assert stored(CHANNEL)["moments_auto_cut"] == 1
        choice = await self.offered(service)
        assert [button.attrs["data-value"] for button in choice.find("button", aria_pressed="true")] == ["automatic"]

        await self.cut(service, dashboard_view.MOMENT_CLIPS_ON_REQUEST)
        assert stored(CHANNEL)["moments_auto_cut"] == 0

    async def test_it_is_kept_while_the_channel_is_tracked_for_something_else(self, service, channel):
        await self.cut(service, dashboard_view.MOMENT_CLIPS_AUTOMATIC)

        await track(service, "clips")
        page = parse((await service.client.get("/dashboard/channels")).text)

        assert stored(CHANNEL)["moments_auto_cut"] == 1
        # Offered only while it is tracked for moments - the page shows it
        # again once that is chosen.
        assert "hidden" in page.one("span", data_moment_clips=True).attrs

    async def test_a_platform_that_keeps_no_vod_is_not_offered_it(self, service):
        conn = db.get_connection()
        try:
            db.add_platform_streamer(conn, "youtube", "UC123", "someone@youtube")
        finally:
            conn.close()

        page = parse((await service.client.get("/dashboard/channels")).text)

        assert page.find("span", data_moment_clips=True) == []

    async def test_an_unknown_way_or_channel_is_refused(self, service, channel):
        assert (await self.cut(service, "sometimes")).status_code == 400
        assert (await self.cut(service, "automatic", "nobody")).status_code == 404


class TestWhatAChannelKeepsOfItsVods:
    """What a channel's platform lists of its VODs once a stream is over is
    kept, and the Channels page says where it keeps footage from being had
    from them then - a moment's clip cut when asked for among it."""

    SUBSCRIBERS_ONLY = (
        "Kick keeps this channel's VODs for its subscribers only once a stream is over: "
        "a moment's clip can be cut only while its stream is still on."
    )

    async def notes(self, service) -> dict[str, tuple[str, str]]:
        """What the Channels page says of each channel's VODs, by channel,
        with its words for it on hover."""
        page = parse((await service.client.get("/dashboard/channels")).text)
        return {
            row.one("button", data_remove_channel=True).attrs["data-remove-channel"]: (note.text, note.attrs["title"])
            for row in page.find("tr")
            for note in row.find("span", data_vods=True)
        }

    async def test_setting_a_channel_to_be_tracked_for_moments_keeps_what_kick_lists(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        service.api.channels[CHANNEL].videos = [kick_video(START, 3600, SOURCE, status="subscriber_only")]

        await track(service, "moments")

        assert await self.notes(service) == {CHANNEL: ("VODs for subscribers", self.SUBSCRIBERS_ONLY)}

    async def test_every_watched_channel_is_asked_about_and_again_once_what_was_seen_is_a_day_old(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        service.api.channels[CHANNEL].videos = [kick_video(START, 3600, SOURCE, status="subscriber_only")]
        service.watch("other_channel", broadcaster_user_id=USER_ID + 1)
        service.api.channels["other_channel"].videos = [kick_video(START, 3600, SOURCE)]
        now = datetime.now(timezone.utc)

        await service.main._look_at_vod_access(now)

        # Nothing where they stay public.
        assert await self.notes(service) == {CHANNEL: ("VODs for subscribers", self.SUBSCRIBERS_ONLY)}

        service.api.channels[CHANNEL].videos = [kick_video(START, 3600, SOURCE)]
        await service.main._look_at_vod_access(now + service.main.VOD_ACCESS_FRESH - timedelta(minutes=1))
        assert await self.notes(service) == {CHANNEL: ("VODs for subscribers", self.SUBSCRIBERS_ONLY)}

        await service.main._look_at_vod_access(now + service.main.VOD_ACCESS_FRESH)
        assert await self.notes(service) == {}

    async def test_a_channel_kick_does_not_answer_for_keeps_what_was_seen(self, service):
        service.watch(CHANNEL, broadcaster_user_id=USER_ID)
        service.api.channels[CHANNEL].videos = [kick_video(START, 3600, SOURCE, status="subscriber_only")]
        now = datetime.now(timezone.utc)
        await service.main._look_at_vod_access(now)
        service.api.failures["web.kick.com"] = 503

        await service.main._look_at_vod_access(now + service.main.VOD_ACCESS_FRESH)

        assert await self.notes(service) == {CHANNEL: ("VODs for subscribers", self.SUBSCRIBERS_ONLY)}


class TestAMomentCaught:
    # Kick's VOD was seen to begin five seconds before the start kick.com
    # lists for its stream.
    EARLY = timedelta(seconds=5)

    @pytest.fixture
    def vod(self, hls, channel):
        """The VOD of the channel's stream that is on, listed by kick.com:
        the stream began an hour ago, its VOD a little before that."""
        began = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
        server = hls(fake_segments(370, duration=10.0), published=370, window=370, stream_start=began - self.EARLY)
        channel.videos = [kick_video(began, 0, server.master_url, live=True)]
        return server

    @pytest.fixture
    def notified(self, service, vod, telegram, monkeypatch):
        """Notes go to Telegram, with links to the dashboard; the channel's
        stream is on, and kick.com lists its VOD."""
        monkeypatch.setenv("DASHBOARD_URL", DASHBOARD)
        monkeypatch.setattr(service.main, "settings", load_settings())
        service.main._notifier = None

        async def no_footage(*args, **kwargs):
            raise AssertionError("no clip is cut of a moment of a channel tracked for moments")

        monkeypatch.setattr(recording_manager, "create_clip_for_moment", no_footage)

    async def react(self, service) -> dict:
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=detector.laughing_needed(20))  # enough to keep it
        await service.settle()
        (found,) = service.moments()
        return dict(found)

    def link(self, channel, found: dict, began: datetime) -> str:
        """The link to the moment's VOD on kick.com, from where its clip
        would begin - before the reaction by the pre-roll and the playback
        delay - counted from `began`, and a little later, nearer to what chat
        reacted to."""
        tuned = tuning.recommended()
        first_at = datetime.fromisoformat(found["window_start"]) - timedelta(
            seconds=tuned.pre_roll_seconds + tuned.playback_delay_seconds
        )
        seconds = int((first_at - began).total_seconds()) + platforms.WATCH_LATER_SECONDS
        return f"https://kick.com/{CHANNEL}/videos/{site_video_id(channel.videos[0])}?t={seconds}"

    async def test_it_is_kept_without_a_clip_with_where_it_is_watched_in_the_vod(self, service, notified, vod, channel):
        found = await self.react(service)

        assert (found["clip_on_request"], found["clip_path"]) == (1, None)
        assert found["laughing"] is not None
        # Counted by the VOD's own clock, as the page's player of it and the
        # clip go: from when the VOD began, not from the start kick.com lists.
        assert found["watch_url"] == self.link(channel, found, vod.stream_start)
        assert recording_manager._recorders.get(CHANNEL) is None
        # Its playlist was read, none of its footage.
        assert vod.segment_requests() == []

    async def test_a_vod_whose_playlist_cannot_be_read_is_linked_to_from_the_start_kick_lists(
        self, service, notified, vod, channel
    ):
        vod.fail_next["/master.m3u8"] = [503, 503]

        found = await self.react(service)

        assert found["watch_url"] == self.link(channel, found, vod.stream_start + self.EARLY)

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

    async def test_with_its_clips_cut_automatically_one_is_cut_from_the_vod_right_away(
        self, service, notified, vod, telegram, monkeypatch
    ):
        await service.client.post(f"/channels/{CHANNEL}/moments_auto_cut?how=automatic")
        # Not waiting for the VOD to reach the end of the moment.
        monkeypatch.setattr(vod_footage, "VOD_BEHIND_SECONDS", -3600)

        found = await self.react(service)

        # Its footage fetched from the VOD, without anyone asking.
        assert vod.segment_requests() != []
        assert service.main._vod_clips[found["id"]]["state"] != dashboard_view.FOOTAGE_NONE
        # Still told of, with where it is watched.
        (message,) = telegram.sent
        assert message["links"][0] == found["watch_url"]

    async def test_switched_to_automatically_with_the_channel_tracked_for_clips_nothing_is_cut(
        self, service, notified, vod, monkeypatch
    ):
        await service.client.post(f"/channels/{CHANNEL}/moments_auto_cut?how=automatic")
        monkeypatch.setattr(vod_footage, "VOD_BEHIND_SECONDS", -3600)
        service.warm_up(CHANNEL)
        await service.crowd_laughs(CHANNEL, people=detector.laughing_needed(20))  # enough to keep it
        # Tracked for clips again while the moment is open.
        await track(service, "clips")
        await service.settle()

        assert vod.segment_requests() == []

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

    async def test_only_the_stretch_chosen_is_cut_and_only_that_much_of_the_vod_fetched(self, service, vod, asked_for):
        # A second before the clip (18-26 s) to four seconds into it.
        response = await service.client.post(f"/moments/{asked_for}/clip", json={"start": -1, "end": 4})
        assert response.status_code == 200, response.text
        await service.settle()

        row = moment(asked_for)
        assert row["clip_start"] == (START + timedelta(seconds=18)).isoformat()
        assert row["clip_duration"] == pytest.approx(4, abs=0.15)
        assert (row["context_before"], row["context_after"]) == (pytest.approx(1, abs=0.01), pytest.approx(0, abs=0.15))
        file = recorder.CLIPS_DIR / CHANNEL / f"moment_{asked_for}.mp4"
        assert probe(file)["duration"] == pytest.approx(5, abs=0.15)
        assert decodes_cleanly(file)
        # The VOD's segments 15-20 and 20-25 - not the one after them that the
        # footage the settings keep would have needed.
        assert sorted(vod.segment_requests()) == [3, 4]

    async def test_a_stretch_chosen_with_none_of_the_clip_is_the_clip_itself(self, service, vod, asked_for):
        # 28-31 s: two seconds after the clip ends.
        response = await service.client.post(f"/moments/{asked_for}/clip", json={"start": 10, "end": 13})
        assert response.status_code == 200, response.text
        await service.settle()

        row = moment(asked_for)
        assert row["clip_start"] == (START + timedelta(seconds=28)).isoformat()
        assert row["clip_duration"] == pytest.approx(3, abs=0.15)
        assert (row["context_before"], row["context_after"]) == (0, 0)
        assert await self.standing(service, asked_for) == {
            "moment_id": asked_for,
            "state": dashboard_view.FOOTAGE_DONE,
            "busy": False,
            "words": "The clip was cut from the stream's VOD.",
        }

    @pytest.mark.parametrize(
        ("asked", "said"),
        [
            ({"start": 2}, "Expected start and end of the stretch to cut, in seconds of the clip, as JSON."),
            (
                {"start": "0", "end": 5},
                "Expected start and end of the stretch to cut, in seconds of the clip, as JSON.",
            ),
            (
                {"start": True, "end": 5},
                "Expected start and end of the stretch to cut, in seconds of the clip, as JSON.",
            ),
            ([0, 5], "Expected start and end of the stretch to cut, in seconds of the clip, as JSON."),
            ({"start": 3, "end": 3.5}, "The stretch has to end at least a second after it starts."),
            ({"start": 5, "end": 2}, "The stretch has to end at least a second after it starts."),
            ({"start": 0, "end": 601}, "A stretch holds at most 10 minutes of the stream."),
            ({"start": -700, "end": -650}, "A stretch begins and ends at most 10 minutes before the clip or after it."),
            ({"start": 650, "end": 700}, "A stretch begins and ends at most 10 minutes before the clip or after it."),
        ],
    )
    async def test_a_stretch_that_cannot_be_cut_is_refused_and_nothing_is_started(
        self, service, vod, asked_for, asked, said
    ):
        response = await service.client.post(f"/moments/{asked_for}/clip", json=asked)

        assert (response.status_code, response.json()["detail"]) == (400, said)
        await service.settle()
        assert (await self.standing(service, asked_for))["state"] == dashboard_view.FOOTAGE_NONE
        assert moment(asked_for)["clip_path"] is None
        assert vod.segment_requests() == []

    async def test_what_is_not_json_is_refused(self, service, vod, asked_for):
        response = await service.client.post(
            f"/moments/{asked_for}/clip", content=b"{not json", headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 400
        assert moment(asked_for)["clip_path"] is None

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
        block = (await page_of(service, asked_for)).one("div", data_vod_clip=True)
        assert block.one("p", data_status=True).text == standing["words"]

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

    async def test_while_another_moment_is_fetched_from_a_vod_it_is_queued_and_then_cut(self, service, vod, asked_for):
        async with service.main._vod_turn(lambda: None):
            await self.ask(service, asked_for)
            await async_wait_until(
                lambda: service.main._vod_clips.get(asked_for, {}).get("state") == dashboard_view.FOOTAGE_QUEUED,
                "the clip to be queued",
            )
            state = await self.standing(service, asked_for)
            assert (state["busy"], state["words"]) == (
                True,
                "Queued: footage of another moment is being fetched from a VOD.",
            )
            assert vod.segment_requests() == []

        await service.settle()

        assert moment(asked_for)["clip_path"] == f"{CHANNEL}/moment_{asked_for}.mp4"

    async def test_moments_are_cut_from_the_vod_one_at_a_time(self, service, vod, asked_for, monkeypatch):
        other = add_moment(
            CHANNEL,
            broadcaster_user_id=USER_ID,
            detected_at=START + timedelta(seconds=34),
            reaction_seconds=4,
            clip_on_request=1,
            laughing=9,
        )
        cutting: list[int] = []
        most = 0
        cut = vod_footage.cut

        def counted(vod, first, last, output, progress):
            nonlocal most
            cutting.append(1)
            most = max(most, len(cutting))
            try:
                return cut(vod, first, last, output, progress)
            finally:
                cutting.pop()

        monkeypatch.setattr(vod_footage, "cut", counted)

        await self.ask(service, asked_for)
        await self.ask(service, other)
        await service.settle()

        assert most == 1
        assert moment(asked_for)["clip_path"] and moment(other)["clip_path"]

    async def test_queued_it_gives_up_at_shutdown(self, service, vod, asked_for, monkeypatch):
        async with service.main._vod_turn(lambda: None):
            await self.ask(service, asked_for)
            await async_wait_until(
                lambda: service.main._vod_clips.get(asked_for, {}).get("state") == dashboard_view.FOOTAGE_QUEUED,
                "the clip to be queued",
            )
            monkeypatch.setattr(service.main, "_shutdown_requested", True)

        await service.settle()

        assert asked_for not in service.main._vod_clips
        assert moment(asked_for)["clip_path"] is None
        assert vod.segment_requests() == []

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


class TestItsVodPlayedOnThePage:
    # The VOD's clock begins two seconds after the time kick.com gives the
    # stream, in five-second segments: where a player goes is counted on
    # the VOD's own clock.
    VOD_START = START + timedelta(seconds=2)

    @pytest.fixture
    def vod(self, hls, channel):
        """Five minutes of the broadcast's VOD, listed by kick.com."""
        server = hls(fake_segments(60, duration=5.0), published=60, window=60, stream_start=self.VOD_START)
        channel.videos = [kick_video(START, 3600, server.master_url)]
        return server

    @pytest.fixture
    def kept(self, channel) -> int:
        """A moment of a channel tracked for moments: chat reacted two
        minutes into the stream."""
        return add_moment(
            CHANNEL,
            broadcaster_user_id=USER_ID,
            detected_at=START + timedelta(seconds=120),
            reaction_seconds=4,
            clip_on_request=1,
            laughing=9,
        )

    async def where(self, service, moment_id: int):
        return await service.client.get(f"/moments/{moment_id}/vod")

    async def test_it_says_where_the_vod_is_and_how_far_into_it_the_clip_would_begin(self, service, vod, kept):
        response = await self.where(service, kept)

        assert response.status_code == 200, response.text
        # Framed as the channel's clips are: before the reaction by the
        # pre-roll and the playback delay.
        tuned = tuning.recommended()
        clip_from = START + timedelta(seconds=116 - tuned.pre_roll_seconds - tuned.playback_delay_seconds)
        assert response.json() == {
            "moment_id": kept,
            "source": vod.master_url,
            "lead": pytest.approx((clip_from - self.VOD_START).total_seconds(), abs=0.001),
            # Where the link to the VOD on Kick starts.
            "start": platforms.WATCH_LATER_SECONDS,
        }
        # Only the playlist was asked for, none of the footage.
        assert vod.segment_requests() == []

    async def test_a_vod_kept_for_subscribers_says_so(self, service, channel, kept):
        channel.videos = [kick_video(START, 3600, SOURCE, status="subscriber_only")]

        response = await self.where(service, kept)

        assert (response.status_code, response.json()["detail"]) == (
            409,
            "Kick keeps the VOD of that stream for the channel's subscribers only.",
        )

    async def test_a_vod_that_does_not_reach_the_moment_yet_says_so(self, service, hls, channel, kept):
        # A stream that is on, written to its VOD up to a minute in.
        server = hls(fake_segments(60, duration=5.0), published=12, window=60, stream_start=self.VOD_START)
        channel.videos = [kick_video(START, 0, server.master_url, live=True)]

        response = await self.where(service, kept)

        assert response.status_code == 409
        assert response.json()["detail"].startswith("The VOD does not reach the moment yet")

    async def test_a_vod_that_ends_before_the_moment_says_so(self, service, hls, channel, kept):
        server = hls(fake_segments(12, duration=5.0), published=12, window=12, stream_start=self.VOD_START)
        server.end_stream()
        channel.videos = [kick_video(START, 3600, server.master_url)]

        response = await self.where(service, kept)

        assert (response.status_code, response.json()["detail"]) == (409, "The VOD ends before the moment.")

    async def test_one_with_its_clip_already_has_it_played_instead(self, service, vod):
        moment_id = add_moment(CHANNEL, clip_on_request=1, laughing=9, clip_path=f"{CHANNEL}/moment_1.mp4")

        response = await self.where(service, moment_id)

        assert (response.status_code, response.json()["detail"]) == (409, "The moment has its clip already.")

    async def test_youtube_does_not_let_its_vods_be_played_here(self, service):
        moment_id = add_moment("someone@youtube", broadcaster_user_id=-7, clip_on_request=1, laughing=9)

        response = await self.where(service, moment_id)

        assert (response.status_code, response.json()["detail"]) == (
            409,
            "YouTube does not let its VODs be played on this page.",
        )

    async def test_an_unknown_moment_is_not_found(self, service):
        assert (await self.where(service, 999)).status_code == 404


class FakeStream:
    def __init__(self, master: str) -> None:
        self._master = master

    def to_manifest_url(self) -> str:
        return self._master


class TestATwitchVodPlayedOnThePage:
    """Twitch's VOD hosts let no page but Twitch's fetch them: the service
    fetches the past broadcast's playlists and footage for the page."""

    @pytest.fixture
    def vod(self, hls, twitch_api, monkeypatch):
        """A past broadcast of five minutes, listed by Helix, its playlist
        where streamlink says - with no times of its own, as Twitch's are
        once a stream is over."""
        server = hls(fake_segments(30, duration=10.0), published=30, window=30, program_date_time=False)
        server.end_stream()
        channel = twitch_api.add_channel("someone", "101")
        channel.past_broadcasts = [past_broadcast("777", "2026-01-01T12:00:00Z", "5m")]
        answers = {"https://www.twitch.tv/videos/777": {"best": FakeStream(server.master_url)}}

        class Streamlink:
            def streams(self, url: str, options=None):
                return answers.get(url, {})

        monkeypatch.setattr(twitch_stream, "Streamlink", Streamlink)
        return server

    @pytest.fixture
    def kept(self) -> int:
        return add_moment(
            "someone@twitch",
            broadcaster_user_id=-101,
            detected_at=START + timedelta(seconds=120),
            reaction_seconds=4,
            clip_on_request=1,
            laughing=9,
        )

    async def where(self, service, moment_id: int) -> dict:
        response = await service.client.get(f"/moments/{moment_id}/vod")
        assert response.status_code == 200, response.text
        return response.json()

    async def relay(self, service, address: str):
        return await service.client.get(address)

    async def test_it_says_where_the_vod_is_through_the_service(self, service, vod, kept):
        found = await self.where(service, kept)

        assert found["source"] == vod_relay.address(vod.master_url)
        tuned = tuning.recommended()
        clip_from = START + timedelta(seconds=116 - tuned.pre_roll_seconds - tuned.playback_delay_seconds)
        assert found["lead"] == pytest.approx((clip_from - START).total_seconds(), abs=0.001)
        # Played from as far into the clip as the link to the past broadcast.
        assert found["start"] == platforms.WATCH_LATER_SECONDS

    async def test_the_page_follows_the_vod_to_its_footage_through_the_service(self, service, vod, kept):
        master = await self.relay(service, (await self.where(service, kept))["source"])

        assert master.status_code == 200
        assert master.headers["content-type"] == vod_relay.PLAYLIST_TYPE
        assert master.headers["cache-control"] == "no-store"
        variants = [line for line in master.text.splitlines() if line and not line.startswith("#")]
        assert all(line.startswith(f"{vod_relay.ROUTE}?") for line in variants)

        playlist = await self.relay(service, variants[-1])
        segments = [line for line in playlist.text.splitlines() if line and not line.startswith("#")]
        assert len(segments) == 30
        assert all(line.startswith(f"{vod_relay.ROUTE}?") for line in segments)

        footage = await self.relay(service, segments[12])
        assert footage.status_code == 200
        assert footage.content == vod.timeline[12].data
        assert vod.segment_requests() == [12]

    async def test_an_address_it_did_not_hand_out_is_not_fetched(self, service, vod):
        signature = vod_relay.signature(vod.master_url)
        elsewhere = f"{vod.base_url}/segment/3.ts"

        response = await service.client.get(vod_relay.ROUTE, params={"u": elsewhere, "s": signature})

        assert response.status_code == 403
        assert vod.requests == []

    async def test_what_the_host_will_not_hand_over_is_said(self, service, vod):
        response = await self.relay(service, vod_relay.address(f"{vod.base_url}/nothing-here"))

        assert (response.status_code, response.json()["detail"]) == (502, "The VOD's host answered 404.")


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
        assert block.find("p")[0].text.startswith("No clip has been cut of this moment yet")
        assert block.one("p", data_status=True).text == ""
        watch = page.one("a", data_watch=True)
        assert (watch.attrs["href"], watch.text) == ("https://kick.com/some_channel/videos/v1?t=3565", "Watch on Kick")
        assert watch.attrs["target"] == "_blank"
        assert page.find("video", data_clip=True) == []

    async def test_the_stretch_to_cut_starts_out_as_the_settings_frame_the_clip(self, service, channel):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID, clip_on_request=1, laughing=9)
        row = moment(moment_id)
        tuned = tuning.recommended()
        clip_from, clip_to = tuned.clip_span(
            datetime.fromisoformat(row["window_start"]), datetime.fromisoformat(row["window_end"])
        )

        page = await page_of(service, moment_id)

        # In clip time, as the strip has it; shown, and filled in, by the script.
        stretch = page.one("div", data_vod_clip=True).one("span", data_stretch="vod")
        assert "hidden" in stretch.attrs
        assert float(stretch.attrs["data-clip"]) == pytest.approx((clip_to - clip_from).total_seconds(), abs=0.001)
        assert float(stretch.attrs["data-start"]) == -tuned.context_before_seconds
        assert float(stretch.attrs["data-end"]) == pytest.approx(
            (clip_to - clip_from).total_seconds() + tuned.context_after_seconds, abs=0.001
        )
        assert float(stretch.attrs["data-least"]) == service.main.VOD_CLIP_LEAST_SECONDS
        assert float(stretch.attrs["data-most"]) == service.main.VOD_CLIP_MOST_SECONDS
        # Reset goes back to it.
        assert (stretch.attrs["data-reset-start"], stretch.attrs["data-reset-end"]) == (
            stretch.attrs["data-start"],
            stretch.attrs["data-end"],
        )
        assert [field.attrs["data-stretch-type"] for field in stretch.find("input")] == ["start", "end"]
        assert [button.text for button in stretch.find("button")] == ["Start here", "End here", "Reset"]
        # Marked on the strip by the script.
        assert "hidden" in page.one("div", data_vod_stretch_mark=True).attrs

    async def test_a_kick_moment_without_its_clip_offers_to_play_it_from_the_vod(self, service, channel):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID, clip_on_request=1, laughing=9)

        page = await page_of(service, moment_id)

        # Shown once the script is there, which plays it - and the transport
        # once the VOD's player is.
        box = page.one("div", data_vod_watch=f"/moments/{moment_id}/vod")
        assert "hidden" in box.attrs
        assert box.one("button", data_vod_watch_go=True).text == "Play from the VOD"
        # What is said of it until it plays - why it can't, too - is said in
        # the box, under the button pressed.
        assert "data-status-scope" in box.attrs
        assert box.one("p", data_status=True).text == ""
        assert "hidden" in page.one("div", data_vod_transport=True).attrs
        # The strip under it goes with the VOD's player as with a clip's.
        assert page.one("div", data_strip=True).attrs["role"] == "slider"
        assert page.one("div", data_playhead=True)

    async def test_how_far_cutting_its_clip_has_got_is_said_beside_the_button(self, service, channel):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID, clip_on_request=1, laughing=9)
        service.main._vod_clips[moment_id] = {"state": dashboard_view.FOOTAGE_JOINING}

        page = await page_of(service, moment_id)

        # Not in the moment's own status line, out of sight below a player
        # of the clip's size.
        block = page.one("div", data_vod_clip=f"/moments/{moment_id}/clip")
        assert "data-status-scope" in block.attrs
        assert block.one("p", data_status=True).text == "Putting the clip together."
        assert [line.text for line in page.find("p", data_status=True)] == ["", "Putting the clip together.", ""]

    async def test_a_twitch_moment_without_its_clip_offers_to_play_it_from_the_vod_too(self, service):
        moment_id = add_moment(
            "someone@twitch",
            broadcaster_user_id=-7,
            clip_on_request=1,
            laughing=9,
            watch_url="https://www.twitch.tv/videos/1?t=0h59m25s",
        )

        page = await page_of(service, moment_id)

        box = page.one("div", data_vod_watch=f"/moments/{moment_id}/vod")
        assert box.one("button", data_vod_watch_go=True).text == "Play from the VOD"
        assert page.one("div", data_strip=True).attrs["role"] == "slider"
        assert page.one("a", data_watch=True).text == "Watch on Twitch"
        assert page.one("div", data_vod_clip=True)

    async def test_a_youtube_moment_without_its_clip_is_not_played_from_a_vod(self, service):
        moment_id = add_moment("someone@youtube", broadcaster_user_id=-7, clip_on_request=1, laughing=9)

        page = await page_of(service, moment_id)

        assert page.find(data_vod_watch=True) == [] and page.find(data_strip=True) == []
        assert page.find(class_="ch-transport") == []

    async def test_a_moment_found_in_no_vod_has_no_link_to_one(self, service, channel):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID, clip_on_request=1, laughing=9)

        page = await page_of(service, moment_id)

        assert page.find("a", data_watch=True) == []
        assert page.one("div", data_vod_clip=True)

    async def test_a_moment_of_a_channel_tracked_for_clips_has_none_of_it(self, service):
        moment_id = add_moment(CHANNEL, broadcaster_user_id=USER_ID)

        page = await page_of(service, moment_id)

        assert page.find(data_vod_clip=True) == [] and page.find("a", data_watch=True) == []
        assert page.find(data_vod_watch=True) == []
