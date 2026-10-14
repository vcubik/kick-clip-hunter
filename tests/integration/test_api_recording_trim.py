"""A stretch of a stream that was kept whole, cut out for an editor from the
Recordings page's Trim dialog - with its chat, if that was asked for: as a
video of its own to lay over it, or drawn into its picture.

The same cut as from a moment's footage (tests/integration/
test_api_dashboard.py), which is where what the two share is gone through
in full; here it is what differs for a recording: its times are seconds of
its file, its cut is named for the stretch it holds, its chat is told by
the channel's id and placed by the recording's parts, and its footage is
long enough to ask too much of - and chat drawn into the picture, which
makes most sense for a long stretch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from kick_clip_hunter import chat_trace, chat_video, clip_trim, db, recording_manager, stream_recording
from kick_clip_hunter import dashboard_view as view
from tests.support.browser import FakeBrowser
from tests.support.data import T0, add_chat, add_moment, add_stream_recording, stream_recordings
from tests.support.html import parse
from tests.support.media import decodes_cleanly, make_video, probe, require_ffmpeg, video_stream
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CHANNEL = "Some_Channel"
# The name Kick sends the channel's chat under.
CHAT_NAME = "some_channel"
DELAY = timedelta(seconds=chat_trace.CHAT_DELAY_SECONDS)
TOLERANCE = 0.35


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> Path:
    """Twenty seconds of video with a frame a cut can begin on every two."""
    require_ffmpeg()
    return make_video(tmp_path_factory.mktemp("footage") / "footage.mp4", seconds=20, keyframe_every=2.0)


@pytest.fixture
def browser(monkeypatch) -> Iterator[FakeBrowser]:
    fake = FakeBrowser()
    monkeypatch.setattr(chat_video, "drawn_column", fake.drawn_column)
    yield fake
    fake.release()


def said_at(second: float, sender: str = "alice", content: str = "hello") -> None:
    """Chat written by someone watching that second of a stream that began
    at T0."""
    add_chat(CHAT_NAME, sender, content, T0 + timedelta(seconds=second) + DELAY)


def recording_of(footage: Path, **stored) -> tuple[int, Path]:
    """A recording whose file is the footage, which began at T0, and where
    that file is."""
    recording_id = add_stream_recording(CHANNEL, **{"seconds": 20.0, **stored})
    (row,) = [row for row in stream_recordings() if row["id"] == recording_id]
    file = stream_recording.STREAMS_DIR / row["file_path"]
    file.write_bytes(footage.read_bytes())
    return recording_id, file


def cut_of(file: Path, start: float, end: float) -> Path:
    return file.with_name(clip_trim.recording_video_name(file.name, start, end))


def chat_of(video: Path) -> Path:
    return video.with_name(chat_video.video_name(video.name))


def record_of(video: Path) -> Path:
    return video.with_name(clip_trim.record_name(video.name))


async def ask(service, recording_id: int, **asked):
    return await service.client.post(f"/recordings/{recording_id}/trim", json={"start": 4, "end": 10, **asked})


async def state(service, recording_id: int) -> dict:
    return (await service.client.get(f"/recordings/{recording_id}/trim")).json()


def lines(browser: FakeBrowser) -> list[tuple[str, str]]:
    """(who, the second of the video their line arrives at) for the one page
    the browser was given."""
    (page,) = [parse(html) for html in browser.pages]
    return [(line.one("b").text, line.attrs["data-at"]) for line in page.find("p", class_="ch-chat")]


class TestTheVideo:
    async def test_asking_cuts_the_stretch_out_as_a_video_named_for_it(self, service, footage):
        recording_id, file = recording_of(footage)

        response = await ask(service, recording_id, start=4, end=10)

        assert response.status_code == 200
        assert response.json() == {
            "recording_id": recording_id,
            "state": "cutting",
            "words": view.trim_words(view.TRIM_CUTTING),
            "video_url": None,
            "chat_url": None,
            "cut": None,
            "chat_later": False,
        }
        await service.settle()

        video = cut_of(file, 4, 10)
        assert video.name.endswith("_trim_0h00m04s-0h00m10s.mp4")
        assert probe(video) == {"duration": pytest.approx(6, abs=TOLERANCE), "streams": ["audio", "video"]}
        assert decodes_cleanly(video)
        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("ready", view.trim_words(view.TRIM_READY))
        # Which stretch it holds is said as the dialog writes its times.
        assert now["cut"] == {"from": 4, "to": 10}
        assert now["video_url"].startswith(f"/streams/{CHANNEL}/{video.name}?v=") and now["chat_url"] is None
        served = await service.client.get(now["video_url"])
        assert served.status_code == 200 and served.content == video.read_bytes()
        # The recording itself is what it was.
        assert file.read_bytes() == footage.read_bytes()

    async def test_it_begins_on_a_frame_a_cut_can_begin_on_and_says_how_much_earlier(self, service, footage):
        recording_id, file = recording_of(footage)

        # The frame a cut can begin on before second 5 is at second 4.
        await ask(service, recording_id, start=5, end=9)
        await service.settle()

        assert probe(cut_of(file, 5, 9))["duration"] == pytest.approx(5, abs=TOLERANCE)
        now = await state(service, recording_id)
        assert now["words"] == view.trim_words(view.TRIM_READY, early=1.0)
        assert now["cut"] == {"from": 4, "to": 9}

    async def test_the_whole_recording_can_be_asked_for(self, service, footage):
        recording_id, file = recording_of(footage)

        await ask(service, recording_id, start=0, end=20)
        await service.settle()

        assert probe(cut_of(file, 0, 20))["duration"] == pytest.approx(20, abs=TOLERANCE)

    async def test_a_recording_is_cut_whatever_its_channel_is_doing_now(self, service, footage):
        # Another recording of the channel is running; this one is over.
        recording_id, file = recording_of(footage)
        service.watch(CHANNEL, 1)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        response = await ask(service, recording_id)
        await service.settle()

        assert response.status_code == 200 and cut_of(file, 4, 10).exists()

    async def test_a_cut_that_fails_is_said_in_words_and_can_be_tried_again(self, service, footage, caplog):
        recording_id, file = recording_of(footage)
        file.write_bytes(b"not really video")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await ask(service, recording_id)
            await service.settle()

        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("failed", view.trim_words(view.TRIM_FAILED))
        assert now["video_url"] is None and not cut_of(file, 4, 10).exists()
        assert any(f"trimming failed for stream recording {recording_id}" in r.getMessage() for r in caplog.records)

        file.write_bytes(footage.read_bytes())
        await ask(service, recording_id)
        await service.settle()

        assert (await state(service, recording_id))["state"] == "ready"


class TestOneCutAtATime:
    async def test_a_new_cut_takes_the_place_of_the_one_before_and_of_its_chat(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)
        await ask(service, recording_id, start=4, end=10, chat=True)
        await service.settle()
        first = cut_of(file, 4, 10)
        assert first.exists() and chat_of(first).exists()

        await ask(service, recording_id, start=12, end=18)
        await service.settle()

        second = cut_of(file, 12, 18)
        assert sorted(path.name for path in file.parent.iterdir()) == sorted(
            [file.name, second.name, record_of(second).name]
        )
        now = await state(service, recording_id)
        assert now["cut"]["from"] == 12
        assert second.name in now["video_url"] and now["chat_url"] is None

    async def test_asking_again_while_one_is_being_made_starts_nothing_more(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)
        browser.hold()

        for end in (10, 12, 14):
            response = await ask(service, recording_id, end=end, chat=True)
            assert response.json()["state"] in view.TRIM_BUSY
        await async_wait_until(lambda: browser.pages, "the rendering to reach the browser")
        browser.release()
        await service.settle()

        assert len(browser.pages) == 1
        # The first one asked for is the one that was made.
        assert [path.name for path in file.parent.iterdir() if path.suffix == ".mp4" and path != file] == [
            cut_of(file, 4, 10).name
        ]

    async def test_the_one_before_is_not_offered_while_its_replacement_is_made(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5)
        await ask(service, recording_id, chat=True)
        await service.settle()
        browser.hold()

        response = await ask(service, recording_id, start=12, end=18, chat=True)

        assert response.json()["video_url"] is None and response.json()["chat_url"] is None
        browser.release()
        await service.settle()

    async def test_a_cut_made_before_a_restart_is_still_offered_and_still_named(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5)
        await ask(service, recording_id, start=5, end=9, chat=True)
        await service.settle()

        service.main._recording_trims.clear()  # all that a restart forgets

        now = await state(service, recording_id)
        # ... which is how far before the start that was set it begins.
        assert (now["state"], now["cut"]["from"]) == ("ready", 4)
        assert now["video_url"] is not None and now["chat_url"] is not None

    async def test_each_recording_has_its_own(self, service, footage):
        one, one_file = recording_of(footage)
        other, other_file = recording_of(footage, started_at=T0 + timedelta(hours=3))

        await ask(service, one, start=4, end=10)
        await service.settle()

        assert (await state(service, one))["state"] == "ready"
        assert (await state(service, other))["state"] == "none"
        cut = cut_of(one_file, 4, 10)
        assert sorted(path.name for path in other_file.parent.iterdir() if "_trim_" in path.name) == sorted(
            [cut.name, record_of(cut).name]
        )


class TestItsChat:
    async def test_with_chat_the_video_is_there_first_and_its_chat_follows(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)
        browser.hold()

        await ask(service, recording_id, chat=True)
        await async_wait_until(lambda: browser.pages, "the rendering to reach the browser")

        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("chat", view.trim_words(view.TRIM_CHAT))
        assert now["video_url"] is not None and now["chat_url"] is None

        browser.release()
        await service.settle()

        now = await state(service, recording_id)
        video = cut_of(file, 4, 10)
        assert now["state"] == "ready" and now["chat_url"].startswith(f"/streams/{CHANNEL}/{chat_of(video).name}?v=")
        stream = video_stream(chat_of(video))
        assert (stream["codec_name"], stream["profile"]) == ("prores", "4444")
        # As long as the video it goes over.
        assert int(stream["nb_frames"]) == pytest.approx(probe(video)["duration"] * chat_video.FPS, abs=2)

    async def test_its_chat_is_the_chat_of_the_stretch_counted_from_where_the_video_starts(
        self, service, footage, browser
    ):
        recording_id, _file = recording_of(footage)
        said_at(2, "alice")  # before the stretch: on screen from the first frame
        said_at(4.5, "bob")
        said_at(9.5, "carol")
        said_at(11, "dave")  # after it

        await ask(service, recording_id, start=4, end=10, chat=True)
        await service.settle()

        assert lines(browser) == [("alice", "-2"), ("bob", "0.5"), ("carol", "5.5")]

    async def test_chat_is_lined_up_with_where_the_video_really_begins(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5.5, "bob")

        # Asked to start at second 5, the video begins at second 4.
        await ask(service, recording_id, start=5, end=9, chat=True)
        await service.settle()

        assert lines(browser) == [("bob", "1.5")]

    @pytest.mark.parametrize(
        ("shift", "shown"),
        [
            # Later, carol's line falls past the end of the six seconds; earlier,
            # dave's - from after the stretch - comes into them.
            (1.5, [("alice", "-0.5"), ("bob", "2")]),
            (-2, [("alice", "-4"), ("bob", "-1.5"), ("carol", "3.5"), ("dave", "5")]),
        ],
        ids=["later", "earlier"],
    )
    async def test_chat_can_be_moved_against_the_picture(self, service, footage, browser, shift, shown):
        recording_id, _file = recording_of(footage)
        said_at(2, "alice")
        said_at(4.5, "bob")
        said_at(9.5, "carol")
        said_at(11, "dave")

        await ask(service, recording_id, start=4, end=10, chat=True, chat_shift=shift)
        await service.settle()

        assert lines(browser) == shown

    async def test_chat_is_placed_by_the_parts_the_recording_is_made_of(self, service, footage, browser):
        # Ten seconds, then - a minute later - ten more: second 12 of the
        # file was on air 72 seconds after the stream began.
        recording_id, _file = recording_of(footage, parts=[(T0, 10.0), (T0 + timedelta(seconds=70), 10.0)])
        said_at(8, "alice")
        said_at(40, "bob")  # while nothing was recorded
        said_at(72, "carol")

        await ask(service, recording_id, start=6, end=14, chat=True)
        await service.settle()

        # What was said in the break shows where the picture picks up.
        assert lines(browser) == [("alice", "2"), ("bob", "4"), ("carol", "6")]

    async def test_chat_is_see_through_unless_a_colour_is_asked_for(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)

        await ask(service, recording_id, chat=True)
        await service.settle()
        clear = video_stream(chat_of(cut_of(file, 4, 10)))
        await ask(service, recording_id, chat=True, chat_background="#0A2540")
        await service.settle()
        coloured = video_stream(chat_of(cut_of(file, 4, 10)))

        assert (clear["codec_name"], clear["profile"]) == ("prores", "4444")
        # On a colour nothing is see-through, and it is a fraction of the size.
        assert (coloured["codec_name"], coloured["pix_fmt"]) == ("h264", "yuv420p")

    async def test_chat_cannot_go_with_a_stretch_that_has_none(self, service, footage, browser):
        recording_id, file = recording_of(footage)

        response = await ask(service, recording_id, chat=True)

        assert response.status_code == 409 and "No chat" in response.json()["detail"]
        assert browser.pages == [] and not cut_of(file, 4, 10).exists()

    async def test_nor_with_one_too_long_for_it(self, service, footage, browser, monkeypatch):
        monkeypatch.setattr(clip_trim, "CHAT_VIDEO_MAX_SECONDS", 5.0)
        recording_id, file = recording_of(footage)
        said_at(5)

        refused = await ask(service, recording_id, start=4, end=10, chat=True)
        without = await ask(service, recording_id, start=4, end=10)
        await service.settle()

        assert refused.status_code == 400
        assert refused.json()["detail"].startswith("Chat can go as a see-through video with a stretch of up to")
        assert "drawn into the video, or put on a background" in refused.json()["detail"]
        # The video alone can be as long as the recording.
        assert without.status_code == 200 and cut_of(file, 4, 10).exists()
        assert browser.pages == []

    @pytest.mark.parametrize(
        "asked", [{"chat_where": "right"}, {"chat_background": "#0a2540"}], ids=["in-the-video", "on-a-colour"]
    )
    async def test_drawn_into_the_video_or_on_a_colour_it_can_go_with_any(
        self, service, footage, browser, monkeypatch, asked
    ):
        monkeypatch.setattr(clip_trim, "CHAT_VIDEO_MAX_SECONDS", 5.0)
        recording_id, _file = recording_of(footage)
        said_at(5)

        response = await ask(service, recording_id, start=4, end=10, chat=True, **asked)
        await service.settle()

        assert response.status_code == 200
        assert (await state(service, recording_id))["state"] == "ready"


class TestChatInTheVideo:
    """Chat drawn into the cut's picture: the cut is made with it, encoded
    anew - which is why it begins where it was asked to."""

    async def test_it_is_made_together_with_the_cut_to_the_frame_asked_for(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)

        response = await ask(service, recording_id, start=5, end=9, chat=True, chat_where="right")

        assert response.json()["state"] == "burning"
        await service.settle()
        video = cut_of(file, 5, 9)
        # A copied cut would begin on the frame at second 4.
        assert probe(video) == {"duration": pytest.approx(4, abs=0.1), "streams": ["audio", "video"]}
        assert decodes_cleanly(video)
        assert not chat_of(video).exists()
        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("ready", view.trim_words(view.TRIM_READY, burned=True))
        assert now["cut"] == {"from": 5, "to": 9}
        assert now["chat_url"] is None and now["chat_later"] is False
        record = clip_trim.read_cut_record(record_of(video).read_text(encoding="utf-8"))
        assert record == clip_trim.Cut(5, pytest.approx(4, abs=0.1), 9, burned=True)

    async def test_its_chat_is_counted_from_where_it_was_asked_to_begin(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(3, "alice")
        said_at(6.5, "bob")

        await ask(service, recording_id, start=5, end=9, chat=True, chat_where="left", chat_shift=0.5)
        await service.settle()

        assert lines(browser) == [("alice", "-1.5"), ("bob", "2")]

    async def test_how_far_it_has_got_is_said_while_it_is_made(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5)
        browser.hold()

        await ask(service, recording_id, start=4, end=10, chat=True, chat_where="right")
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")

        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("burning", view.trim_words(view.TRIM_BURNING, done=0.0))
        assert now["video_url"] is None
        # Nothing else is started meanwhile.
        again = await ask(service, recording_id, start=2, end=8)
        assert again.json()["state"] == "burning"
        browser.release()
        await service.settle()
        assert (await state(service, recording_id))["state"] == "ready"

    async def test_it_is_given_up_when_the_service_shuts_down(self, service, footage, browser, monkeypatch, caplog):
        recording_id, file = recording_of(footage)
        said_at(5)
        browser.hold()
        await ask(service, recording_id, start=4, end=10, chat=True, chat_where="right")
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")

        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            browser.release()
            await service.settle()

        # Not minutes later, and with nothing half-made left behind.
        assert recording_id not in service.main._recording_trims
        assert sorted(path.name for path in file.parent.iterdir()) == [file.name]
        assert any("given up: shutting down" in record.getMessage() for record in caplog.records)

    async def test_one_that_cannot_be_made_says_so_and_leaves_nothing(self, service, footage, browser, caplog):
        recording_id, file = recording_of(footage)
        said_at(5)
        browser.failure = RuntimeError("the browser would not start")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await ask(service, recording_id, start=4, end=10, chat=True, chat_where="right")
            await service.settle()

        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("failed", view.trim_words(view.TRIM_FAILED))
        assert sorted(path.name for path in file.parent.iterdir()) == [file.name]
        assert any("drawing chat into the cut" in record.getMessage() for record in caplog.records)

    async def test_a_cut_taken_without_chat_can_have_it_drawn_in_later(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)
        await ask(service, recording_id, start=5, end=9)
        await service.settle()
        video = cut_of(file, 5, 9)
        copied = probe(video)["duration"]

        response = await service.client.post(f"/recordings/{recording_id}/trim/chat", json={"chat_where": "right"})

        assert response.status_code == 200 and response.json()["state"] == "burning"
        await service.settle()
        # In the cut's place, as long as it was: it begins on the frame the
        # copied cut began on, a second before the start that was set.
        assert probe(video)["duration"] == pytest.approx(copied, abs=0.15)
        assert lines(browser) == [("alice", "1")]
        now = await state(service, recording_id)
        assert (now["state"], now["chat_later"], now["cut"]) == ("ready", False, {"from": 4, "to": 9})
        assert clip_trim.read_cut_record(record_of(video).read_text(encoding="utf-8")).burned
        assert sorted(path.name for path in file.parent.iterdir()) == sorted(
            [file.name, video.name, record_of(video).name]
        )

    async def test_but_not_twice(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5)
        await ask(service, recording_id, start=5, end=9, chat=True, chat_where="right")
        await service.settle()

        again = await service.client.post(f"/recordings/{recording_id}/trim/chat", json={"chat_where": "left"})
        apart = await service.client.post(f"/recordings/{recording_id}/trim/chat", json={})

        assert again.status_code == apart.status_code == 409
        assert again.json()["detail"] == "This video has its chat in it already."
        assert len(browser.pages) == 1

    async def test_where_it_goes_must_be_one_of_the_places_there_are(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5)

        response = await ask(service, recording_id, chat=True, chat_where="top")

        assert response.status_code == 400
        assert response.json()["detail"] == "Chat_where must be one of right, left, apart."
        assert browser.pages == [] and service.main._background_tasks == set()


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "asked",
        [
            {"start": 5, "end": 5.5},
            {"start": 9, "end": 4},
            {"start": -30, "end": 2},
            {"start": 0, "end": 60},
            {"start": "0:04", "end": 10},
            {"start": None},
            {"chat_shift": clip_trim.CHAT_SHIFT_LIMIT_SECONDS + 1},
            {"chat_background": "navy"},
        ],
        ids=["too-short", "backwards", "before-footage", "past-footage", "text", "no-start", "too-far", "colour-name"],
    )
    async def test_a_stretch_that_cannot_be_cut_is_refused_in_words(self, service, footage, asked):
        recording_id, _file = recording_of(footage)

        response = await ask(service, recording_id, **asked)

        detail = response.json()["detail"]
        assert response.status_code == 400 and detail[0].isupper() and detail.endswith(".")
        assert (await state(service, recording_id))["state"] == "none"
        assert service.main._background_tasks == set()

    @pytest.mark.parametrize("body", [b"start=0&end=4", b"[0, 4]"], ids=["form", "list"])
    async def test_so_is_what_does_not_say_which_stretch(self, service, footage, body):
        recording_id, _file = recording_of(footage)

        response = await service.client.post(
            f"/recordings/{recording_id}/trim", content=body, headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 400

    async def test_there_is_nothing_to_cut_from_without_a_file(self, service, footage):
        failed = add_stream_recording(CHANNEL, state="failed")
        gone = add_stream_recording(CHANNEL, file=False, started_at=T0 + timedelta(hours=3))

        for recording_id in (failed, gone):
            response = await ask(service, recording_id)

            assert response.status_code == 409, recording_id
            assert (await state(service, recording_id))["state"] == "none"

    async def test_nor_from_a_recording_that_is_not_over(self, service):
        service.watch(CHANNEL, 1)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        assert (await ask(service, 1)).status_code == 409

    async def test_a_cut_that_would_fill_the_disk_is_not_started(self, service, footage, monkeypatch):
        # Three gigabytes of recording, of which the cut is three tenths.
        recording_id, file = recording_of(footage)
        room = recording_manager.MIN_FREE_BYTES + 0.3 * 3 * 1024**3
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: int(room) - 1)

        refused = await ask(service, recording_id, start=4, end=10)

        assert refused.status_code == 409 and "disk is too full" in refused.json()["detail"]
        assert not cut_of(file, 4, 10).exists()

        monkeypatch.setattr(stream_recording, "free_bytes", lambda: int(room) + 1)
        assert (await ask(service, recording_id, start=4, end=10)).status_code == 200
        await service.settle()

    async def test_an_unknown_recording_is_not_found(self, service):
        assert (await ask(service, 999)).status_code == 404
        assert (await service.client.get("/recordings/999/trim")).status_code == 404

    async def test_none_is_started_once_the_service_is_shutting_down(self, service, footage, process_exits):
        recording_id, file = recording_of(footage)
        await service.client.post("/shutdown")

        response = await ask(service, recording_id)

        assert response.status_code == 409
        assert not cut_of(file, 4, 10).exists()


class TestItsChatLater:
    """The chat of a stretch whose video was taken without it."""

    async def ask_chat(self, service, recording_id: int, **asked):
        return await service.client.post(f"/recordings/{recording_id}/trim/chat", json=asked)

    async def test_it_is_made_for_the_stretch_the_video_holds(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5.5, "bob")
        # Asked to start at second 5, the video begins at second 4.
        await ask(service, recording_id, start=5, end=9)
        await service.settle()
        assert (await state(service, recording_id))["chat_later"] is True

        response = await self.ask_chat(service, recording_id, chat_shift=0.5)

        assert response.status_code == 200 and response.json()["state"] == "chat"
        await service.settle()
        now = await state(service, recording_id)
        assert now["state"] == "ready" and now["chat_later"] is False
        assert now["chat_url"].startswith(f"/streams/{CHANNEL}/{chat_of(cut_of(file, 5, 9)).name}?v=")
        assert lines(browser) == [("bob", "2")]

    async def test_not_for_a_video_cut_before_where_it_begins_was_kept(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)
        await ask(service, recording_id)
        await service.settle()
        record_of(cut_of(file, 4, 10)).unlink()

        now = await state(service, recording_id)
        assert now["chat_later"] is False
        # Then only its name says which stretch it holds, to the second.
        assert (now["cut"], now["words"]) == (None, "0:00:04 to 0:00:10 is ready to download.")
        assert (await self.ask_chat(service, recording_id)).status_code == 409
        assert browser.pages == []

    async def test_nor_where_nothing_has_been_cut(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        said_at(5)

        assert (await self.ask_chat(service, recording_id)).status_code == 409
        assert browser.pages == []

    async def test_nor_for_a_stretch_without_chat(self, service, footage, browser):
        recording_id, _file = recording_of(footage)
        await ask(service, recording_id)
        await service.settle()

        response = await self.ask_chat(service, recording_id)

        assert response.status_code == 409 and "No chat" in response.json()["detail"]
        assert browser.pages == []


class TestDeletingTheRecording:
    async def test_what_was_cut_out_of_it_goes_with_it(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        other_id, other_file = recording_of(footage, started_at=T0 + timedelta(hours=3))
        said_at(5)
        await ask(service, recording_id, chat=True)
        await ask(service, other_id)
        await service.settle()
        video = cut_of(file, 4, 10)
        assert video.exists() and chat_of(video).exists()

        response = await service.client.post(f"/recordings/{recording_id}/delete")

        assert response.status_code == 200
        # Another recording's cut, next to it, is left alone.
        other_cut = cut_of(other_file, 4, 10)
        assert sorted(path.name for path in file.parent.iterdir()) == sorted(
            [other_file.name, other_cut.name, record_of(other_cut).name]
        )
        assert (await service.client.get(f"/recordings/{recording_id}/trim")).status_code == 404

    async def test_it_is_not_deleted_while_a_stretch_of_it_is_being_cut_out(self, service, footage, browser):
        recording_id, file = recording_of(footage)
        said_at(5)
        browser.hold()
        await ask(service, recording_id, chat=True)
        await async_wait_until(lambda: browser.pages, "the rendering to reach the browser")

        refused = await service.client.post(f"/recordings/{recording_id}/delete")

        assert refused.status_code == 409 and "being cut out" in refused.json()["detail"]
        assert file.exists()

        browser.release()
        await service.settle()
        assert (await service.client.post(f"/recordings/{recording_id}/delete")).status_code == 200
        assert not file.exists()

    async def test_moments_of_the_channel_are_left_alone(self, service, footage):
        recording_id, _file = recording_of(footage)
        moment_id = add_moment(CHAT_NAME, detected_at=T0 + timedelta(seconds=10))

        await service.client.post(f"/recordings/{recording_id}/delete")

        conn = db.get_connection()
        try:
            assert db.get_moment(conn, moment_id) is not None
        finally:
            conn.close()
