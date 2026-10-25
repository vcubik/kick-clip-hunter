"""The chat of a whole stream that was kept whole, as a video of its own:
Download chat, beside the recording's Download on the Recordings page.

Made the first time it is asked for, on the colour picked beside it (the
one the Trim dialog starts out with unless another is) or see-through, as
long as the recording, with its lines placed as they are
beside the recording; kept next to the recording's file and removed with
it. What a chat video looks like frame by frame is chat_video.py's, gone
through in tests/integration/test_chat_video.py; here it is what this one
is made of and how it is offered.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from kick_clip_hunter import chat_trace, chat_video, clip_trim, recording_manager, stream_recording
from kick_clip_hunter import dashboard_view as view
from tests.support.browser import FakeBrowser
from tests.support.data import T0, add_chat, add_stream_recording, stream_recordings
from tests.support.html import parse
from tests.support.media import FFMPEG, require_ffmpeg, video_stream
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CHANNEL = "Some_Channel"
# The name Kick sends the channel's chat under.
CHAT_NAME = "some_channel"
DELAY = timedelta(seconds=chat_trace.CHAT_DELAY_SECONDS)
SECONDS = 4.0


@pytest.fixture
def browser(monkeypatch) -> Iterator[FakeBrowser]:
    require_ffmpeg()
    fake = FakeBrowser()
    monkeypatch.setattr(chat_video, "drawn_column", fake.drawn_column)
    yield fake
    fake.release()


def said_at(second: float, sender: str = "alice", content: str = "hello") -> None:
    """Chat written by someone watching that second of a stream that began
    at T0."""
    add_chat(CHAT_NAME, sender, content, T0 + timedelta(seconds=second) + DELAY)


def recording(**stored) -> tuple[int, Path]:
    """A recording of SECONDS that began at T0, and where its file is."""
    recording_id = add_stream_recording(CHANNEL, **{"seconds": SECONDS, **stored})
    (row,) = [row for row in stream_recordings() if row["id"] == recording_id]
    return recording_id, stream_recording.STREAMS_DIR / row["file_path"]


def chat_of(file: Path) -> Path:
    return file.with_name(chat_video.video_name(file.name))


def noted_of(file: Path) -> Path:
    """Where the colour its chat video was made on is noted."""
    return chat_of(file).with_suffix(".json")


def corner(video: Path) -> list[int]:
    """The red, green and blue of the top left pixel of a video's first
    frame - where, with no chat yet, there is only what is behind it."""
    # fmt: off
    result = subprocess.run(
        [
            FFMPEG, "-v", "error", "-i", str(video), "-frames:v", "1",
            "-vf", "crop=2:2:0:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        check=True, capture_output=True,
    )
    # fmt: on
    return list(result.stdout[:3])


def colour(written: str) -> list[int]:
    return [int(written[index : index + 2], 16) for index in (1, 3, 5)]


# Asked for with nothing said of what is behind the chat.
UNSAID = object()


async def ask(service, recording_id: int, background=UNSAID):
    """Asks for the chat video; a `background` of None asks for it
    see-through."""
    body = {} if background is UNSAID else {"json": {"chat_background": background}}
    return await service.client.post(f"/recordings/{recording_id}/chat_video", **body)


async def state(service, recording_id: int) -> dict:
    return (await service.client.get(f"/recordings/{recording_id}/chat_video")).json()


def lines(browser: FakeBrowser) -> list[tuple[str, str]]:
    """(who, the second of the video their line arrives at) for the one page
    the browser was given."""
    (page,) = [parse(html) for html in browser.pages]
    return [(line.one("b").text, line.attrs["data-at"]) for line in page.find("p", class_="ch-chat")]


async def page_of(service, recording_id: int):
    response = await service.client.get(f"/dashboard/recordings?recording={recording_id}")
    assert response.status_code == 200
    return parse(response.text)


class TestMakingIt:
    async def test_asking_makes_the_whole_recordings_chat_a_video_on_a_colour(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        browser.hold()

        response = await ask(service, recording_id)

        assert response.status_code == 200
        assert response.json() == {
            "recording_id": recording_id,
            "url": f"/recordings/{recording_id}/chat_video",
            "page_url": f"/dashboard/recordings/{recording_id}/chat_video",
            "state": "making",
            "busy": True,
            "download_url": None,
            "background": None,
            "see_through": False,
            "colour": clip_trim.DEFAULT_CHAT_BACKGROUND,
            "chat": True,
            "words": view.chat_file_words(view.CHAT_FILE_MAKING, 0.0),
        }
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")
        # Nothing is offered while it is made: not even what is half there.
        assert (await state(service, recording_id))["download_url"] is None
        browser.release()
        await service.settle()

        now = await state(service, recording_id)
        assert (now["state"], now["busy"], now["words"]) == ("ready", False, "")
        assert now["download_url"].startswith(f"/streams/{CHANNEL}/{chat_of(file).name}?v=")
        served = await service.client.get(now["download_url"])
        assert served.status_code == 200 and served.content == chat_of(file).read_bytes()
        stream = video_stream(chat_of(file))
        # On a colour, not see-through: tens of megabytes an hour, not gigabytes.
        assert stream["codec_name"] == "h264"
        assert int(stream["nb_frames"]) == pytest.approx(SECONDS * chat_video.FPS, abs=2)
        # With no colour asked for, the one the Trim dialog starts out with.
        assert corner(chat_of(file)) == pytest.approx(colour(clip_trim.DEFAULT_CHAT_BACKGROUND), abs=3)
        assert now["background"] == now["colour"] == clip_trim.DEFAULT_CHAT_BACKGROUND
        # Only it, and the colour it is on, are left beside the recording.
        assert sorted(path.name for path in file.parent.iterdir()) == sorted(
            [file.name, chat_of(file).name, noted_of(file).name]
        )

    async def test_its_lines_are_placed_as_beside_the_recording(self, service, browser):
        recording_id, _file = recording(
            parts=[(T0, 2.0), (T0 + timedelta(seconds=10), 2.0)],
        )
        said_at(-1, "alice")  # before it: not chat of the recording
        said_at(1, "bob")
        said_at(10.5, "carol")  # the second part, which is second 2.5 of the file
        said_at(20, "dave")  # after it

        await ask(service, recording_id)
        await service.settle()

        assert lines(browser) == [("bob", "1"), ("carol", "2.5")]

    async def test_asking_again_while_it_is_made_or_once_it_is_there_starts_nothing_more(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        browser.hold()
        await ask(service, recording_id)
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")

        again = await ask(service, recording_id)

        assert again.status_code == 200 and again.json()["state"] == "making"
        browser.release()
        await service.settle()
        made = chat_of(file).stat().st_mtime_ns
        assert (await ask(service, recording_id)).json()["state"] == "ready"
        await service.settle()
        assert len(browser.pages) == 1 and chat_of(file).stat().st_mtime_ns == made

    async def test_it_is_made_on_the_colour_asked_for(self, service, browser):
        recording_id, file = recording()
        said_at(1)

        await ask(service, recording_id, "#0A2540")
        await service.settle()

        assert corner(chat_of(file)) == pytest.approx([10, 37, 64], abs=3)
        now = await state(service, recording_id)
        assert (now["state"], now["background"], now["colour"]) == ("ready", "#0a2540", "#0a2540")

    async def test_asking_for_another_colour_makes_it_anew_in_its_place(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        await ask(service, recording_id, "#0a2540")
        await service.settle()
        browser.hold()

        again = await ask(service, recording_id, "#00ff00")

        # What is there is not offered while the one in its place is made.
        assert (again.json()["state"], again.json()["download_url"]) == ("making", None)
        browser.release()
        await service.settle()
        assert len(browser.pages) == 2
        assert corner(chat_of(file)) == pytest.approx([0, 255, 0], abs=6)
        now = await state(service, recording_id)
        assert (now["state"], now["background"]) == ("ready", "#00ff00")
        assert sorted(path.name for path in file.parent.iterdir()) == sorted(
            [file.name, chat_of(file).name, noted_of(file).name]
        )

    async def test_it_is_made_see_through_when_asked_for_on_no_colour(self, service, browser):
        recording_id, file = recording()
        said_at(1)

        await ask(service, recording_id, None)
        await service.settle()

        stream = video_stream(chat_of(file))
        assert (stream["codec_name"], stream["pix_fmt"]) == ("prores", "yuva444p12le")
        assert int(stream["nb_frames"]) == pytest.approx(SECONDS * chat_video.FPS, abs=2)
        now = await state(service, recording_id)
        assert (now["state"], now["background"], now["see_through"]) == ("ready", None, True)
        # The page offers the first colour, should it be wanted on one after all.
        assert now["colour"] == clip_trim.DEFAULT_CHAT_BACKGROUND
        made = chat_of(file).stat().st_mtime_ns
        assert (await ask(service, recording_id, None)).json()["state"] == "ready"
        await service.settle()
        assert len(browser.pages) == 1 and chat_of(file).stat().st_mtime_ns == made

    @pytest.mark.parametrize(("first", "then"), [(None, "#00ff00"), ("#00ff00", None)])
    async def test_one_see_through_or_on_a_colour_is_made_anew_when_asked_for_the_other_way(
        self, service, browser, first, then
    ):
        recording_id, file = recording()
        said_at(1)
        await ask(service, recording_id, first)
        await service.settle()

        assert (await ask(service, recording_id, then)).json()["state"] == "making"
        await service.settle()

        assert len(browser.pages) == 2
        assert video_stream(chat_of(file))["codec_name"] == ("prores" if then is None else "h264")
        now = await state(service, recording_id)
        assert (now["background"], now["see_through"]) == (then, then is None)

    async def test_one_made_before_its_colour_was_noted_is_taken_as_on_the_first_colour(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        await ask(service, recording_id)
        await service.settle()
        noted_of(file).unlink()
        made = chat_of(file).stat().st_mtime_ns

        now = (await ask(service, recording_id, clip_trim.DEFAULT_CHAT_BACKGROUND)).json()

        assert (now["state"], now["background"]) == ("ready", clip_trim.DEFAULT_CHAT_BACKGROUND)
        await service.settle()
        assert len(browser.pages) == 1 and chat_of(file).stat().st_mtime_ns == made

    async def test_how_far_it_has_got_is_said_while_it_is_made(self, service, browser, monkeypatch):
        recording_id, _file = recording()
        said_at(1)
        browser.hold()
        await ask(service, recording_id)
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")
        service.main._recording_chat_files[recording_id]["done"] = 0.42

        shown = parse((await service.client.get(f"/dashboard/recordings/{recording_id}/chat_video")).text)

        assert "data-busy" in shown.one(data_rec_chat_file=True).attrs
        assert shown.one(data_status=True).text == "Making the chat video: 42 %."
        assert "disabled" in shown.one("button", data_make_chat_file=True).attrs
        browser.release()
        await service.settle()

    async def test_one_that_cannot_be_made_says_so_leaves_nothing_and_can_be_asked_for_again(
        self, service, browser, caplog
    ):
        recording_id, file = recording()
        said_at(1)
        browser.failure = RuntimeError("the browser would not start")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await ask(service, recording_id)
            await service.settle()

        now = await state(service, recording_id)
        assert (now["state"], now["words"]) == ("failed", view.chat_file_words(view.CHAT_FILE_FAILED))
        assert sorted(path.name for path in file.parent.iterdir()) == [file.name]
        assert any("making the chat video of the whole" in record.getMessage() for record in caplog.records)

        browser.failure = None
        await ask(service, recording_id)
        await service.settle()
        assert (await state(service, recording_id))["state"] == "ready"

    async def test_it_is_given_up_when_the_service_shuts_down(self, service, browser, monkeypatch, caplog):
        recording_id, file = recording()
        said_at(1)
        browser.hold()
        await ask(service, recording_id)
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")

        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        with caplog.at_level(logging.INFO, logger="kick_clip_hunter"):
            browser.release()
            await service.settle()

        assert recording_id not in service.main._recording_chat_files
        assert sorted(path.name for path in file.parent.iterdir()) == [file.name]
        assert any("given up: shutting down" in record.getMessage() for record in caplog.records)


class TestWhatIsRefused:
    async def test_a_recording_without_chat_has_none_to_make(self, service, browser):
        recording_id, _file = recording()

        response = await ask(service, recording_id)

        assert response.status_code == 409 and "No chat" in response.json()["detail"]
        assert browser.pages == []

    async def test_nor_one_with_more_than_a_video_can_hold(self, service, browser, monkeypatch):
        recording_id, _file = recording()
        said_at(1, "alice")
        said_at(2, "bob")
        monkeypatch.setattr(service.main, "RECORDING_CHAT_VIDEO_LIMIT", 1)

        response = await ask(service, recording_id)

        assert response.status_code == 409 and "Trim" in response.json()["detail"]
        assert browser.pages == []

    async def test_nor_one_that_would_fill_the_disk(self, service, browser, monkeypatch):
        recording_id, _file = recording()
        said_at(1)
        room = recording_manager.MIN_FREE_BYTES + SECONDS * service.main.RECORDING_CHAT_FILE_BYTES_PER_SECOND
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: int(room) - 1)

        response = await ask(service, recording_id)

        assert response.status_code == 409 and "disk is too full" in response.json()["detail"]
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: int(room) + 1)
        assert (await ask(service, recording_id)).status_code == 200
        await service.settle()

    async def test_nor_a_see_through_one_without_the_far_larger_room_it_takes(self, service, browser, monkeypatch):
        recording_id, _file = recording()
        said_at(1)
        main = service.main
        assert main.RECORDING_CHAT_FILE_SEE_THROUGH_BYTES_PER_SECOND > main.RECORDING_CHAT_FILE_BYTES_PER_SECOND
        room = recording_manager.MIN_FREE_BYTES + SECONDS * main.RECORDING_CHAT_FILE_SEE_THROUGH_BYTES_PER_SECOND
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: int(room) - 1)

        response = await ask(service, recording_id, None)

        assert response.status_code == 409 and "disk is too full" in response.json()["detail"]
        # On a colour it fits.
        assert (await ask(service, recording_id)).status_code == 200
        await service.settle()

    @pytest.mark.parametrize("background", ["navy", "#000; background: url(//example.com)", 7])
    async def test_nor_on_what_is_not_a_colour(self, service, browser, background):
        recording_id, file = recording()
        said_at(1)

        response = await ask(service, recording_id, background)

        assert response.status_code == 400 and "chat_background" in response.json()["detail"]
        assert browser.pages == [] and not chat_of(file).exists()

    @pytest.mark.parametrize("body", ["not json", "[1, 2]"])
    async def test_nor_when_what_is_posted_is_not_an_object(self, service, browser, body):
        recording_id, _file = recording()
        said_at(1)

        response = await service.client.post(
            f"/recordings/{recording_id}/chat_video", content=body, headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 400
        assert browser.pages == []

    async def test_nor_one_without_its_file(self, service, browser):
        recording_id, _file = recording(file=False)

        assert (await ask(service, recording_id)).status_code == 409

    async def test_nor_one_that_is_not_over(self, service):
        recording_id = add_stream_recording(CHANNEL, state=stream_recording.RECORDING)

        assert (await ask(service, recording_id)).status_code == 409

    async def test_an_unknown_recording_is_not_found(self, service):
        assert (await ask(service, 999)).status_code == 404
        assert (await service.client.get("/recordings/999/chat_video")).status_code == 404
        assert (await service.client.get("/dashboard/recordings/999/chat_video")).status_code == 404


class TestOnThePage:
    async def test_it_is_beside_download_and_made_when_asked_for(self, service, browser):
        recording_id, _file = recording()
        said_at(1)

        page = await page_of(service, recording_id)

        place = page.one("div", class_="rec-file").one(data_rec_chat_file=True)
        assert place.attrs["data-url"] == f"/recordings/{recording_id}/chat_video"
        assert place.attrs["data-page"] == f"/dashboard/recordings/{recording_id}/chat_video"
        button = place.one("button", data_make_chat_file=True)
        # Asking is the script's to do.
        assert button.text == "Download chat" and "hidden" in button.attrs
        assert place.find("a") == []
        # And so is picking the colour, beside it - or none, for a
        # see-through one.
        look = place.one(data_chat_file_look=True)
        assert "hidden" in look.attrs
        on_colour = look.one("input", data_chat_file_on_colour=True)
        assert on_colour.attrs["type"] == "checkbox" and "checked" in on_colour.attrs
        picker = look.one("input", data_chat_file_colour=True)
        assert picker.attrs["type"] == "color" and "disabled" not in picker.attrs
        assert picker.attrs["value"] == clip_trim.DEFAULT_CHAT_BACKGROUND

    async def test_once_it_is_there_it_is_a_link_to_it(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        await ask(service, recording_id)
        await service.settle()

        page = await page_of(service, recording_id)

        link = page.one("div", class_="rec-file").one("a", data_chat_file_ready=True)
        assert link.text == "Download chat" and "download" in link.attrs
        assert link.attrs["href"].startswith(f"/streams/{CHANNEL}/{chat_of(file).name}?v=")

    async def test_the_colour_beside_it_is_the_one_it_was_made_on(self, service, browser):
        recording_id, _file = recording()
        said_at(1)
        await ask(service, recording_id, "#0a2540")
        await service.settle()

        place = (await page_of(service, recording_id)).one("div", class_="rec-file").one(data_rec_chat_file=True)

        # The script makes it anew if another one is picked.
        assert place.one("a", data_chat_file_ready=True).attrs["data-background"] == "#0a2540"
        assert place.one("input", data_chat_file_colour=True).attrs["value"] == "#0a2540"
        assert "checked" in place.one("input", data_chat_file_on_colour=True).attrs

    async def test_a_see_through_one_is_shown_as_on_no_background(self, service, browser):
        recording_id, _file = recording()
        said_at(1)
        await ask(service, recording_id, None)
        await service.settle()

        place = (await page_of(service, recording_id)).one("div", class_="rec-file").one(data_rec_chat_file=True)

        assert place.one("a", data_chat_file_ready=True).attrs["data-background"] == ""
        assert "checked" not in place.one("input", data_chat_file_on_colour=True).attrs
        assert "disabled" in place.one("input", data_chat_file_colour=True).attrs

    async def test_the_colour_cannot_be_changed_while_it_is_made(self, service, browser):
        recording_id, _file = recording()
        said_at(1)
        browser.hold()
        await ask(service, recording_id)
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")

        shown = parse((await service.client.get(f"/dashboard/recordings/{recording_id}/chat_video")).text)

        assert "disabled" in shown.one("input", data_chat_file_colour=True).attrs
        assert "disabled" in shown.one("input", data_chat_file_on_colour=True).attrs
        browser.release()
        await service.settle()

    async def test_a_recording_without_chat_offers_none(self, service):
        recording_id, _file = recording()

        page = await page_of(service, recording_id)

        place = page.one("div", class_="rec-file").one(data_rec_chat_file=True)
        assert place.find("button") == [] and place.find("input") == []


class TestDeletingTheRecording:
    async def test_its_chat_video_goes_with_it(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        await ask(service, recording_id)
        await service.settle()
        assert chat_of(file).exists()

        assert (await service.client.post(f"/recordings/{recording_id}/delete")).status_code == 200

        assert not file.exists() and not chat_of(file).exists() and not noted_of(file).exists()

    async def test_it_is_not_deleted_while_its_chat_video_is_made(self, service, browser):
        recording_id, file = recording()
        said_at(1)
        browser.hold()
        await ask(service, recording_id)
        await async_wait_until(lambda: browser.pages, "the chat to reach the browser")

        refused = await service.client.post(f"/recordings/{recording_id}/delete")

        assert refused.status_code == 409 and "chat video" in refused.json()["detail"]
        browser.release()
        await service.settle()
        assert (await service.client.post(f"/recordings/{recording_id}/delete")).status_code == 200
        assert not file.exists()
