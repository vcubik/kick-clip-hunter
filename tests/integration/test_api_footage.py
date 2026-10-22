"""Adding footage to a moment's file from its stream's VOD, through the
service: the page's control, what may be asked and when, and what comes of
it - the file made longer in its place, the clip where it was in it, the
Trim dialog's cut moved with it. The VOD is a fake HLS stream that kick.com's
fake list of the channel's streams points to; the cutting is the real
ffmpeg."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kick_clip_hunter import clip_trim, dashboard_view, recorder, vod_footage
from tests.support.data import add_moment, moment
from tests.support.hls import Segment
from tests.support.html import parse
from tests.support.kick_api import kick_video
from tests.support.media import probe, require_ffmpeg

pytestmark = [pytest.mark.anyio, pytest.mark.ffmpeg]

CHANNEL = "some_channel"
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# The moment's file holds the broadcast's seconds 10 to 20: three seconds
# before the clip, the clip (13 to 17), three after it.
FIRST, CLIP, CLIP_SECONDS, LAST = 10, 13, 4.0, 20
TOLERANCE = 0.15


@pytest.fixture(autouse=True)
def _ffmpeg():
    require_ffmpeg()


@pytest.fixture
def vod(hls, ts_segments, service):
    """The broadcast's VOD in five-second segments, listed by kick.com."""
    timeline = [
        Segment(b"".join(path.read_bytes() for path in ts_segments[first : first + 5]), duration=5.0)
        for first in range(0, len(ts_segments), 5)
    ]
    server = hls(timeline, published=len(timeline), window=len(timeline), stream_start=START)
    service.api.add_channel(CHANNEL, 1).videos = [kick_video(START, 3600, server.master_url)]
    return server


def clip_file(ts_segments, relative: str = f"{CHANNEL}/moment_1.mp4") -> Path:
    path = recorder.CLIPS_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.with_suffix(".joined")
    raw.write_bytes(b"".join(segment.read_bytes() for segment in ts_segments[FIRST:LAST]))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c", "copy", str(path)], check=True)
    raw.unlink()
    return path


@pytest.fixture
def cut_moment(ts_segments) -> int:
    """A moment whose clip the service cut from the live stream: its file,
    and where the clip is on the stream's clock and in the file."""
    clip_file(ts_segments)
    return add_moment(
        CHANNEL,
        detected_at=START + timedelta(seconds=LAST + 30),
        clip_path=f"{CHANNEL}/moment_1.mp4",
        clip_start=(START + timedelta(seconds=CLIP)).isoformat(),
        clip_duration=CLIP_SECONDS,
        context_before=float(CLIP - FIRST),
        context_after=float(LAST - CLIP) - CLIP_SECONDS,
    )


async def ask(service, moment_id: int, side: str = "after", seconds: object = 3):
    return await service.client.post(f"/moments/{moment_id}/footage", json={"side": side, "seconds": seconds})


async def standing(service, moment_id: int) -> dict:
    response = await service.client.get(f"/moments/{moment_id}/footage")
    assert response.status_code == 200, response.text
    return response.json()


async def open_page(service, moment_id: int):
    response = await service.client.get(f"/dashboard/moments/{moment_id}")
    assert response.status_code == 200, response.text
    return parse(response.text)


def keyframes(path: Path) -> list[tuple[float, str]]:
    return vod_footage.keyframes(path)


class TestAddingFootage:
    async def test_footage_after_the_file_is_added_in_its_place(self, service, vod, cut_moment, ts_segments):
        old = keyframes(recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4")

        response = await ask(service, cut_moment, "after", 3)
        assert response.status_code == 200, response.text
        await service.settle()

        file = recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4"
        row = moment(cut_moment)
        assert probe(file)["duration"] == pytest.approx(13, abs=TOLERANCE)
        assert keyframes(file)[: len(old)] == old
        # The clip is where it was, in the broadcast and in its file.
        assert (row["clip_start"], row["clip_duration"], row["context_before"]) == (
            (START + timedelta(seconds=CLIP)).isoformat(),
            CLIP_SECONDS,
            pytest.approx(3, abs=0.01),
        )
        assert row["context_after"] == pytest.approx(6, abs=TOLERANCE)
        assert sorted(path.name for path in file.parent.iterdir()) == ["moment_1.mp4"]

    async def test_footage_before_moves_the_clip_on_in_its_file(self, service, vod, cut_moment):
        file = recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4"
        clip_frame = dict((time, picture) for time, picture in keyframes(file))[3.0]

        await ask(service, cut_moment, "before", 4)
        await service.settle()

        row = moment(cut_moment)
        assert row["context_before"] == pytest.approx(7, abs=0.01)
        # The clip's first picture is that much further into the file now.
        assert {picture: time for time, picture in keyframes(file)}[clip_frame] == pytest.approx(7, abs=0.01)

    async def test_it_says_what_was_added(self, service, vod, cut_moment):
        await ask(service, cut_moment, "before", 4)
        await service.settle()

        state = await standing(service, cut_moment)

        assert state == {
            "moment_id": cut_moment,
            "state": dashboard_view.FOOTAGE_DONE,
            "busy": False,
            "words": "Added 4 s before from the stream's VOD.",
        }

    async def test_the_cut_made_with_trim_is_moved_on_with_what_was_added_before(self, service, vod, cut_moment):
        video = recorder.CLIPS_DIR / CHANNEL / clip_trim.video_name("moment_1.mp4")
        video.write_bytes(b"a cut")
        record = video.with_name(clip_trim.record_name(video.name))
        record.write_text(clip_trim.cut_record(2.0, 5.0, 7.0), encoding="utf-8")

        await ask(service, cut_moment, "before", 4)
        await service.settle()

        # As far on as the clip is: what was added before the file, to the
        # frame the new file's picture begins on (the sound may begin a
        # hundredth of a second earlier).
        added = moment(cut_moment)["context_before"] - (CLIP - FIRST)
        begins, duration, end = clip_trim.read_cut_record(record.read_text(encoding="utf-8"))[:3]
        assert added == pytest.approx(4, abs=0.05)
        assert (begins, duration, end) == (pytest.approx(2 + added), 5.0, pytest.approx(7 + added))

    async def test_the_page_plays_the_new_file_and_not_a_copy_of_the_old_it_kept(self, service, vod, cut_moment):
        file = recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4"
        os.utime(file, (1_700_000_000, 1_700_000_000))
        before = (await open_page(service, cut_moment)).one("video", data_clip=True).attrs["src"]

        await ask(service, cut_moment, "after", 3)
        await service.settle()

        after = (await open_page(service, cut_moment)).one("video", data_clip=True).attrs["src"]
        assert before.split("?")[0] == after.split("?")[0]
        assert before != after

    async def test_a_stream_kick_lists_no_vod_of_says_why_and_leaves_the_moment_alone(
        self, service, vod, cut_moment, ts_segments
    ):
        service.api.channels[CHANNEL].videos = []
        content = (recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4").read_bytes()

        await ask(service, cut_moment, "after", 3)
        await service.settle()

        state = await standing(service, cut_moment)
        assert state["state"] == dashboard_view.FOOTAGE_FAILED
        assert state["words"].startswith("Kick lists no VOD of that stream")
        assert (recorder.CLIPS_DIR / CHANNEL / "moment_1.mp4").read_bytes() == content
        assert moment(cut_moment)["context_after"] == pytest.approx(3)

    async def test_asked_again_while_it_goes_on_nothing_more_is_started(self, service, vod, cut_moment):
        service.main._footage[cut_moment] = {"state": dashboard_view.FOOTAGE_FETCHING, "done": 0.5}

        response = await ask(service, cut_moment, "after", 3)

        assert response.json()["words"] == "Fetching the footage from the VOD: 50 %."
        assert response.json()["busy"] is True
        assert not service.main._background_tasks


class TestWhatCanBeAskedFor:
    @pytest.mark.parametrize(
        "body",
        [{"side": "middle", "seconds": 3}, {"side": "after"}, {"side": "after", "seconds": "3"}, [1], {"seconds": 3}],
    )
    async def test_a_side_and_a_number_of_seconds_are_wanted(self, service, cut_moment, body):
        response = await service.client.post(f"/moments/{cut_moment}/footage", json=body)

        assert response.status_code == 400
        assert response.json()["detail"] == "Expected side (before or after) and seconds as JSON."

    @pytest.mark.parametrize("seconds", [0, vod_footage.ADD_MOST_SECONDS + 1, -5])
    async def test_so_much_at_a_time(self, service, cut_moment, seconds):
        response = await ask(service, cut_moment, "before", seconds)

        assert response.status_code == 400
        assert response.json()["detail"] == (
            f"Footage is added {vod_footage.ADD_LEAST_SECONDS} to {vod_footage.ADD_MOST_SECONDS} seconds at a time."
        )

    async def test_an_unknown_moment_is_not_found(self, service):
        assert (await ask(service, 999)).status_code == 404
        assert (await service.client.get("/moments/999/footage")).status_code == 404

    async def test_a_moment_without_its_clip_has_nothing_to_add_to(self, service):
        moment_id = add_moment(CHANNEL, clip_path=f"{CHANNEL}/gone.mp4", clip_start=START.isoformat(), clip_duration=4)

        response = await ask(service, moment_id)

        assert (response.status_code, response.json()["detail"]) == (409, "The moment has no clip to add footage to.")

    async def test_an_imported_clip_cannot_be_placed_in_a_vod(self, service, ts_segments):
        clip_file(ts_segments, f"{CHANNEL}/manual_1.mp4")
        moment_id = add_moment(CHANNEL, reason=dashboard_view.IMPORT_REASON, clip_path=f"{CHANNEL}/manual_1.mp4")

        response = await ask(service, moment_id)

        assert response.status_code == 409
        assert "cut from a Kick, Twitch or YouTube stream" in response.json()["detail"]

    async def test_not_while_a_stretch_of_it_is_being_cut_out(self, service, cut_moment):
        service.main._trims[cut_moment] = {"state": dashboard_view.TRIM_CUTTING}

        response = await ask(service, cut_moment)

        assert response.status_code == 409
        assert response.json()["detail"].startswith("A stretch of this moment is being cut out")

    async def test_not_while_the_service_shuts_down(self, service, cut_moment, monkeypatch):
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        response = await ask(service, cut_moment)

        assert (response.status_code, response.json()["detail"]) == (409, "The service is shutting down.")


class TestWhileFootageIsAdded:
    """The moment's file is about to be another: nothing else is made of it,
    and it is not removed meanwhile."""

    @pytest.fixture
    def busy(self, service, cut_moment) -> int:
        service.main._footage[cut_moment] = {"state": dashboard_view.FOOTAGE_FINDING}
        return cut_moment

    async def test_no_stretch_is_cut_out_of_it(self, service, busy):
        response = await service.client.post(f"/moments/{busy}/trim", json={"start": 0, "end": 2, "chat": False})

        assert (response.status_code, response.json()["detail"]) == (409, service.main.FOOTAGE_BUSY_WORDS)

    async def test_it_is_not_transcribed(self, service, busy):
        response = await service.client.post(f"/moments/{busy}/subtitles")

        assert (response.status_code, response.json()["detail"]) == (409, service.main.FOOTAGE_BUSY_WORDS)

    async def test_it_is_not_removed(self, service, busy):
        response = await service.client.post(f"/moments/{busy}/delete")

        assert (response.status_code, response.json()["detail"]) == (409, service.main.FOOTAGE_BUSY_WORDS)

    async def test_the_page_says_so_and_offers_nothing_more_meanwhile(self, service, busy):
        page = await open_page(service, busy)

        group = page.one("span", class_="footage")
        assert "data-busy" in group.attrs
        assert all("disabled" in button.attrs for button in group.find("button"))
        # The moment's own status line, the first on it.
        status = page.find("p", class_="status-line")[0]
        assert status.text == dashboard_view.FOOTAGE_WORDS[dashboard_view.FOOTAGE_FINDING]


class TestOnThePage:
    async def test_a_cut_clip_has_the_control_with_how_much_to_add(self, service, cut_moment):
        group = (await open_page(service, cut_moment)).one("span", class_="footage")

        # Shown by the script, which is what can use it.
        assert "hidden" in group.attrs and "data-busy" not in group.attrs
        assert group.attrs["data-footage"] == f"/moments/{cut_moment}/footage"
        options = group.one("select").find("option")
        assert [(option.attrs["value"], option.text) for option in options] == [
            ("15", "15 s"),
            ("30", "30 s"),
            ("60", "1 min"),
            ("120", "2 min"),
        ]
        assert [option.attrs["value"] for option in options if "selected" in option.attrs] == ["30"]
        assert [(button.attrs["data-footage-side"], button.text) for button in group.find("button")] == [
            ("before", "Add before"),
            ("after", "Add after"),
        ]

    @pytest.mark.parametrize("channel", ["some_channel@twitch", "some_channel@youtube"])
    async def test_twitch_and_youtube_clips_have_it_too(self, service, ts_segments, channel):
        clip_file(ts_segments, "elsewhere/moment_1.mp4")
        moment_id = add_moment(
            channel, clip_path="elsewhere/moment_1.mp4", clip_start=START.isoformat(), clip_duration=CLIP_SECONDS
        )

        assert (await open_page(service, moment_id)).find("span", class_="footage")

    async def test_an_imported_clip_has_none(self, service, ts_segments):
        clip_file(ts_segments, f"{CHANNEL}/manual_1.mp4")
        moment_id = add_moment(
            CHANNEL, reason=dashboard_view.IMPORT_REASON, clip_path=f"{CHANNEL}/manual_1.mp4", clip_duration=10.0
        )

        assert (await open_page(service, moment_id)).find("span", class_="footage") == []

    async def test_a_clip_from_before_its_place_in_the_stream_was_kept_has_none(self, service, ts_segments):
        clip_file(ts_segments)
        moment_id = add_moment(CHANNEL, clip_path=f"{CHANNEL}/moment_1.mp4", clip_duration=CLIP_SECONDS)

        assert (await open_page(service, moment_id)).find("span", class_="footage") == []
