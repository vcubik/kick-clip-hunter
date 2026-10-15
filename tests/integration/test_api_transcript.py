"""A moment's transcript, from the review page's request on: what is said in
its footage, written down by the speech model, shown beside the clip, put
right line by line, and taken away as subtitles timed to the moment's file
or to the cut made of it with Trim.

The speech model is a stand-in (`FakeSpeech`): the real one is a download of
gigabytes and is not run in the suite (see docs/testing.md). How its words
are taken from it is in tests/unit/test_analysis_wrappers.py, and what lines
are made of them in tests/unit/test_subtitles.py.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from kick_clip_hunter import clip_trim, subtitles
from kick_clip_hunter import dashboard_view as view
from kick_clip_hunter.subtitles import Line, Word
from tests.support.data import T0, add_moment, moment
from tests.support.html import Element, parse
from tests.support.media import make_video, require_ffmpeg
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CLIP = "channel_a/moment_1.mp4"
SOURCE = Path("data/clips/channel_a/moment_1.mp4")
CUT = Path("data/clips/channel_a/moment_1_trim.mp4")
# A moment's file, as the recorder cuts one: 30 s before the clip, the
# clip, 60 s after it.
EXTENT = {"context_before": 30.0, "clip_duration": 35.0, "context_after": 60.0}
# Two sentences said in that file: one before the clip, one in it - on the
# file's own time, as the model hears them.
HEARD = [
    [Word(28.0, 28.4, " Tak"), Word(28.5, 29.5, " co?")],
    [Word(31.0, 31.6, " Jedeme"), Word(31.7, 32.5, " dál.")],
]


class FakeSpeech:
    """Stands in for the speech model: hears what it is given to hear, on a
    worker thread as the model does, telling how far it has got - and can
    be held half-way, to see what the page says meanwhile."""

    def __init__(self) -> None:
        self.heard: list[list[Word]] = HEARD
        self.failure: Exception | None = None
        self.files: list[Path] = []
        self.halfway = threading.Event()
        self._going = threading.Event()
        self._going.set()

    def hold(self) -> None:
        self._going.clear()

    def release(self) -> None:
        self._going.set()

    def transcribe_words(self, path: Path, progress=None) -> list[list[Word]]:
        self.files.append(path)
        if progress is not None:
            progress(0.25)
        self.halfway.set()
        self._going.wait(timeout=20)
        if self.failure is not None:
            raise self.failure
        if progress is not None:
            progress(1.0)
        return self.heard


@pytest.fixture
def speech(service, monkeypatch) -> Iterator[FakeSpeech]:
    fake = FakeSpeech()
    monkeypatch.setattr(service.main.transcriber, "transcribe_words", fake.transcribe_words)
    yield fake
    fake.release()


def clip(**columns) -> int:
    """A moment with its file on disk - bytes the stand-in never looks at."""
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE.write_bytes(b"not really video")
    return add_moment("channel_a", detected_at=T0, clip_path=CLIP, **{**EXTENT, **columns})


def transcribed(*lines: Line, **columns) -> int:
    """A moment with a clip whose transcript is made already."""
    return clip(subtitles=subtitles.stored(lines), **columns)


async def tab(service, moment_id: int) -> Element:
    """The transcript beside the moment's clip, as the review page has it."""
    response = await service.client.get("/dashboard", params={"moment": moment_id})
    assert response.status_code == 200
    return parse(response.text).one("div", class_="transcript")


async def served(service, moment_id: int) -> Element:
    """The same, as the page fetches it on its own."""
    response = await service.client.get(f"/dashboard/moments/{moment_id}/subtitles")
    assert response.status_code == 200
    return parse(response.text).one("div", class_="transcript")


def lines_of(transcript: Element) -> list[tuple[str, str, str, str]]:
    """(from, to, time, text) of every line shown."""
    return [
        (
            line.attrs["data-from"],
            line.attrs["data-to"],
            line.one("button").text,
            line.one("textarea").text,
        )
        for line in transcript.find("li", class_="transcript-line")
    ]


def stored(moment_id: int) -> list[Line] | None:
    return subtitles.read(moment(moment_id)["subtitles"])


class TestBesideTheClip:
    async def test_a_clip_has_a_transcript_tab_that_offers_to_make_one(self, service):
        moment_id = clip()

        response = await service.client.get("/dashboard")
        article = parse(response.text).one("article", class_="moment")

        tabs = article.one("div", role="tablist").find("button", role="tab")
        assert [tab.text for tab in tabs] == ["Chat", "Transcript"]
        pane = article.one("div", data_side_pane="transcript")
        # Chat is shown to begin with; the transcript is a tab away.
        assert "hidden" in pane.attrs and pane.attrs["aria-labelledby"] == tabs[1].attrs["id"]
        transcript = pane.one("div", class_="transcript")
        assert transcript.attrs["data-transcript"] == f"/moments/{moment_id}/subtitles"
        assert transcript.attrs["data-page"] == f"/dashboard/moments/{moment_id}/subtitles"
        assert "data-busy" not in transcript.attrs
        assert transcript.one("p", class_="transcript-words").text == view.transcript_words(view.TRANSCRIPT_NONE)
        assert transcript.one("button", data_transcribe=True).text == "Transcribe"
        assert transcript.find("a") == []

    async def test_a_moment_without_a_clip_has_nothing_to_transcribe(self, service):
        add_moment("channel_a", detected_at=T0)

        response = await service.client.get("/dashboard")
        article = parse(response.text).one("article", class_="moment")

        assert article.find("div", role="tablist") == []
        assert article.find("div", class_="transcript") == []
        assert article.one("div", class_="chat-head").text == "Chat around the moment"

    async def test_the_tab_is_served_on_its_own(self, service):
        moment_id = transcribed(Line(1.0, 2.5, "Jedeme dál."))

        assert lines_of(await served(service, moment_id)) == lines_of(await tab(service, moment_id))
        assert (await service.client.get("/dashboard/moments/999/subtitles")).status_code == 404
        no_clip = add_moment("channel_a", detected_at=T0)
        assert (await service.client.get(f"/dashboard/moments/{no_clip}/subtitles")).status_code == 404


class TestMakingIt:
    async def test_what_is_said_in_the_whole_file_is_written_down_in_clip_time(self, service, speech):
        moment_id = clip()

        response = await service.client.post(f"/moments/{moment_id}/subtitles")

        assert response.status_code == 200
        assert response.json() == {
            "moment_id": moment_id,
            "state": view.TRANSCRIPT_MAKING,
            "words": view.transcript_words(view.TRANSCRIPT_MAKING),
        }
        await service.settle()
        # All of the file, the footage around the clip as well as the clip.
        assert speech.files == [SOURCE]
        # Heard 28 s into a file that opens 30 s before the clip.
        assert stored(moment_id) == [Line(-2.0, -0.5, "Tak co?"), Line(1.0, 2.5, "Jedeme dál.")]

    async def test_the_tab_shows_each_line_with_its_time_and_its_text_to_put_right(self, service, speech):
        moment_id = clip()
        await service.client.post(f"/moments/{moment_id}/subtitles")
        await service.settle()

        transcript = await served(service, moment_id)

        minus = view.MINUS
        assert lines_of(transcript) == [
            ("-2", "-0.5", f"{minus}0:02.0", "Tak co?"),
            ("1", "2.5", "0:01.0", "Jedeme dál."),
        ]
        fields = transcript.find("textarea")
        assert [field.attrs["data-transcript-line"] for field in fields] == ["0", "1"]
        # Spelling is checked in the language the footage was heard in.
        assert {field.attrs["lang"] for field in fields} == {"cs"}
        assert fields[0].attrs["aria-label"] == f"Line at {minus}0:02.0"
        assert transcript.find("button", data_transcribe=True) == []

    async def test_while_it_is_being_made_the_tab_says_how_far_it_has_got(self, service, speech):
        moment_id = clip()
        speech.hold()

        await service.client.post(f"/moments/{moment_id}/subtitles")
        await async_wait_until(speech.halfway.is_set, "the speech model to be half-way")

        transcript = await served(service, moment_id)
        assert "data-busy" in transcript.attrs
        assert transcript.one("p", class_="transcript-words").text == "Transcribing: 25 %."
        assert transcript.find("button", data_transcribe=True) == []

        speech.release()
        await service.settle()

        transcript = await served(service, moment_id)
        assert "data-busy" not in transcript.attrs and len(lines_of(transcript)) == 2

    async def test_asking_again_while_it_is_being_made_starts_nothing_more(self, service, speech):
        moment_id = clip()
        speech.hold()

        await service.client.post(f"/moments/{moment_id}/subtitles")
        again = await service.client.post(f"/moments/{moment_id}/subtitles")
        speech.release()
        await service.settle()

        assert again.status_code == 200 and again.json()["state"] == view.TRANSCRIPT_MAKING
        assert speech.files == [SOURCE]

    async def test_one_that_could_not_be_made_says_so_and_can_be_asked_for_again(self, service, speech, caplog):
        moment_id = clip()
        speech.failure = RuntimeError("the model file is corrupt")

        with caplog.at_level(logging.ERROR, logger="kick_clip_hunter"):
            await service.client.post(f"/moments/{moment_id}/subtitles")
            await service.settle()

        transcript = await served(service, moment_id)
        assert transcript.one("p", class_="transcript-words").text == view.transcript_words(view.TRANSCRIPT_FAILED)
        assert transcript.find("button", data_transcribe=True) != []
        assert stored(moment_id) is None
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert ["transcribing the footage of moment 1 failed" in record.getMessage() for record in errors] == [True]

        speech.failure = None
        await service.client.post(f"/moments/{moment_id}/subtitles")
        await service.settle()

        assert len(stored(moment_id)) == 2

    async def test_footage_in_which_nothing_is_said_has_a_transcript_of_no_lines(self, service, speech):
        moment_id = clip()
        speech.heard = []

        await service.client.post(f"/moments/{moment_id}/subtitles")
        await service.settle()

        assert stored(moment_id) == []
        transcript = await served(service, moment_id)
        assert transcript.one("p", class_="transcript-words").text == view.transcript_words(
            view.TRANSCRIPT_READY, said=False
        )
        assert transcript.find("button", data_transcribe=True) == [] and transcript.find("a") == []

    async def test_a_transcript_that_is_there_is_kept(self, service, speech):
        moment_id = transcribed(Line(1.0, 2.5, "put right by hand"))

        response = await service.client.post(f"/moments/{moment_id}/subtitles")

        assert response.status_code == 409
        assert response.json()["detail"] == "The moment has its transcript already."
        assert stored(moment_id) == [Line(1.0, 2.5, "put right by hand")] and speech.files == []

    async def test_there_is_none_to_make_without_a_clip(self, service, speech):
        no_clip = add_moment("channel_a", detected_at=T0)
        gone = add_moment("channel_a", detected_at=T0, clip_path="channel_a/moment_99.mp4", **EXTENT)

        for moment_id in (no_clip, gone):
            response = await service.client.post(f"/moments/{moment_id}/subtitles")
            assert response.status_code == 409
            assert response.json()["detail"] == "The moment has no clip to transcribe."
        assert (await service.client.post("/moments/999/subtitles")).status_code == 404
        assert speech.files == []

    async def test_none_is_started_while_the_service_shuts_down(self, service, speech, monkeypatch):
        moment_id = clip()
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        response = await service.client.post(f"/moments/{moment_id}/subtitles")

        assert response.status_code == 409 and speech.files == []

    async def test_one_being_made_is_given_up_when_the_service_shuts_down(self, service, speech, monkeypatch):
        moment_id = clip()
        speech.hold()
        await service.client.post(f"/moments/{moment_id}/subtitles")
        await async_wait_until(speech.halfway.is_set, "the speech model to be half-way")

        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        speech.release()
        await service.settle()

        assert stored(moment_id) is None
        # Nothing of it is left to say: it is made again when asked for.
        transcript = await served(service, moment_id)
        assert transcript.one("p", class_="transcript-words").text == view.transcript_words(view.TRANSCRIPT_NONE)


class TestPuttingItRight:
    TRANSCRIPT = (Line(-2.0, -0.5, "Tak co?"), Line(1.0, 2.5, "Jedeme dal."))

    async def correct(self, service, moment_id: int, line: int, body):
        return await service.client.post(f"/moments/{moment_id}/subtitles/{line}", json=body)

    async def test_a_line_says_what_it_was_put_right_to_and_keeps_its_times(self, service):
        moment_id = transcribed(*self.TRANSCRIPT)

        response = await self.correct(service, moment_id, 1, {"text": "  Jedeme   dál. "})

        assert response.status_code == 200
        assert response.json() == {"moment_id": moment_id, "line": 1, "text": "Jedeme dál."}
        assert stored(moment_id) == [self.TRANSCRIPT[0], Line(1.0, 2.5, "Jedeme dál.")]
        assert lines_of(await tab(service, moment_id))[1][3] == "Jedeme dál."

    async def test_a_line_can_be_two_rows_of_a_subtitle(self, service):
        moment_id = transcribed(*self.TRANSCRIPT)

        await self.correct(service, moment_id, 0, {"text": "Tak co,\n\nřekl"})

        assert stored(moment_id)[0].text == "Tak co,\nřekl"

    async def test_one_that_is_not_there_cannot_be_put_right(self, service):
        moment_id = transcribed(*self.TRANSCRIPT)

        response = await self.correct(service, moment_id, 2, {"text": "tak"})

        assert response.status_code == 404
        assert response.json()["detail"] == "The transcript has no line 2."
        assert stored(moment_id) == list(self.TRANSCRIPT)

    async def test_nor_one_of_a_transcript_not_made_yet(self, service):
        moment_id = clip()

        response = await self.correct(service, moment_id, 0, {"text": "tak"})

        assert response.status_code == 409
        assert (await self.correct(service, 999, 0, {"text": "tak"})).status_code == 404

    @pytest.mark.parametrize("body", [{}, {"text": 5}, ["tak"], {"words": "tak"}])
    async def test_what_is_not_a_lines_text_is_refused(self, service, body):
        moment_id = transcribed(*self.TRANSCRIPT)

        response = await self.correct(service, moment_id, 0, body)

        assert response.status_code == 400
        assert stored(moment_id) == list(self.TRANSCRIPT)

    async def test_what_is_not_json_at_all_is_refused(self, service):
        moment_id = transcribed(*self.TRANSCRIPT)

        response = await service.client.post(
            f"/moments/{moment_id}/subtitles/0", content=b"tak", headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Expected the line's text as JSON."

    async def test_a_line_shown_is_text_however_it_was_put_right(self, service):
        moment_id = transcribed(*self.TRANSCRIPT)

        await self.correct(service, moment_id, 0, {"text": "<script>alert(1)</script>"})

        field = (await tab(service, moment_id)).find("textarea")[0]
        assert field.children == ["<script>alert(1)</script>"]


class TestSubtitlesOfTheWholeFile:
    async def test_they_are_timed_to_the_moments_file(self, service):
        moment_id = transcribed(Line(-29.0, -27.5, "Tak co?"), Line(1.0, 2.5, "Jedeme dál."))

        response = await service.client.get(f"/moments/{moment_id}/subtitles")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-subrip"
        assert response.headers["content-disposition"] == 'attachment; filename="moment_1.srt"'
        assert response.headers["cache-control"] == "no-store"
        # The file opens 30 s before the clip.
        assert (
            response.content
            == (
                "\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\nTak co?\r\n\r\n"
                "2\r\n00:00:31,000 --> 00:00:32,500\r\nJedeme dál.\r\n\r\n"
            ).encode()
        )

    async def test_a_file_that_is_the_clip_alone_starts_with_the_clip(self, service):
        moment_id = transcribed(Line(1.0, 2.5, "Jedeme dál."), context_before=None, context_after=None)

        response = await service.client.get(f"/moments/{moment_id}/subtitles")

        assert "00:00:01,000 --> 00:00:02,500" in response.text

    async def test_a_line_left_empty_is_left_out_and_so_is_a_transcript_with_nothing_said(self, service):
        said = transcribed(Line(1.0, 2.5, ""), Line(3.0, 4.0, "Jedeme dál."))
        nothing = add_moment(
            "channel_a", detected_at=T0, clip_path=CLIP, subtitles=subtitles.stored([Line(1.0, 2.5, "")]), **EXTENT
        )

        response = await service.client.get(f"/moments/{said}/subtitles")

        assert response.text.removeprefix("\ufeff").startswith("1\r\n00:00:33,000 --> 00:00:34,000\r\nJedeme dál.")
        # Nothing to download is offered where nothing is said.
        assert (await tab(service, said)).one("a", data_transcript_download=True).attrs["href"] == (
            f"/moments/{said}/subtitles"
        )
        assert (await tab(service, nothing)).find("a") == []

    async def test_there_are_none_before_the_transcript_is_made(self, service):
        moment_id = clip()

        assert (await service.client.get(f"/moments/{moment_id}/subtitles")).status_code == 404
        assert (await service.client.get("/moments/999/subtitles")).status_code == 404

    async def test_downloading_them_is_not_taking_a_cut_away(self, service):
        moment_id = transcribed(Line(1.0, 2.5, "Jedeme dál."))

        await service.client.get(f"/moments/{moment_id}/subtitles")

        assert moment(moment_id)["downloaded_at"] is None


def cut(begins: float, duration: float, *, kept: bool = True) -> None:
    """A video cut out of the moment's file with Trim, beginning `begins`
    seconds into that file - and what is kept of it beside it, unless that
    was not kept (a cut from before it was)."""
    CUT.parent.mkdir(parents=True, exist_ok=True)
    CUT.write_bytes(b"not really video")
    if kept:
        CUT.with_name(clip_trim.record_name(CUT.name)).write_text(
            clip_trim.cut_record(begins, duration, begins + duration), encoding="utf-8"
        )


class TestSubtitlesOfACut:
    async def trim(self, service, moment_id: int) -> dict:
        return (await service.client.get(f"/moments/{moment_id}/trim")).json()

    async def test_a_cut_has_subtitles_of_its_own_timed_to_where_it_begins(self, service):
        # From 2 s before the clip (28 s into the file) for 6 s: a line said
        # long before it is not in it, one said as it ends is cut to its end.
        moment_id = transcribed(Line(-29.0, -27.5, "before"), Line(-1.0, 0.5, "in it"), Line(3.0, 5.0, "at its end"))
        cut(28.0, 6.0)

        state = await self.trim(service, moment_id)
        response = await service.client.get(state["subtitles_url"])

        assert state["subtitles_url"] == f"/moments/{moment_id}/trim/subtitles"
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-subrip"
        assert response.headers["content-disposition"] == 'attachment; filename="moment_1_trim.srt"'
        assert response.text == (
            "\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\nin it\r\n\r\n"
            "2\r\n00:00:05,000 --> 00:00:06,000\r\nat its end\r\n\r\n"
        )

    async def test_the_trim_dialog_has_a_place_for_them_beside_the_video(self, service):
        moment_id = transcribed(Line(-1.0, 0.5, "in it"))

        response = await service.client.get("/dashboard", params={"moment": moment_id})
        dialog = parse(response.text).one("dialog", class_="trim")

        # Shown by the script once the service says there are some.
        link = dialog.one("a", data_trim_subtitles=True)
        assert link.text == "Download subtitles" and "hidden" in link.attrs and "download" in link.attrs

    async def test_the_tab_offers_them_beside_those_of_the_whole_file(self, service):
        moment_id = transcribed(Line(-1.0, 0.5, "in it"))
        cut(28.0, 6.0)

        links = (await tab(service, moment_id)).find("a")

        assert [(link.text, link.attrs["href"]) for link in links] == [
            ("Whole footage", f"/moments/{moment_id}/subtitles"),
            ("The cut from Trim", f"/moments/{moment_id}/trim/subtitles"),
        ]

    async def test_downloading_them_marks_the_moment_as_taken(self, service):
        moment_id = transcribed(Line(-1.0, 0.5, "in it"))
        cut(28.0, 6.0)

        await service.client.get(f"/moments/{moment_id}/trim/subtitles")

        assert moment(moment_id)["downloaded_at"] is not None

    async def test_a_cut_in_which_nothing_is_said_has_none(self, service):
        moment_id = transcribed(Line(20.0, 21.5, "long after it"))
        cut(28.0, 6.0)

        assert (await self.trim(service, moment_id))["subtitles_url"] is None
        assert [link.text for link in (await tab(service, moment_id)).find("a")] == ["Whole footage"]

    async def test_a_cut_without_where_it_begins_has_none(self, service):
        moment_id = transcribed(Line(-1.0, 0.5, "in it"))
        cut(28.0, 6.0, kept=False)

        response = await service.client.get(f"/moments/{moment_id}/trim/subtitles")

        assert (await self.trim(service, moment_id))["subtitles_url"] is None
        assert response.status_code == 409
        assert response.json()["detail"].endswith("Export it again.")
        assert moment(moment_id)["downloaded_at"] is None

    async def test_there_are_none_without_a_cut_or_a_transcript(self, service):
        no_transcript = clip()
        cut(28.0, 6.0)
        no_cut = add_moment(
            "channel_b", detected_at=T0, clip_path="channel_b/moment_2.mp4", subtitles=subtitles.stored([]), **EXTENT
        )

        assert (await service.client.get(f"/moments/{no_transcript}/trim/subtitles")).status_code == 404
        assert (await service.client.get(f"/moments/{no_cut}/trim/subtitles")).status_code == 404
        assert (await self.trim(service, no_transcript))["subtitles_url"] is None

    async def test_a_real_cut_begins_on_a_frame_a_cut_can_begin_on_and_its_subtitles_with_it(self, service):
        require_ffmpeg()
        # Twelve seconds, the clip in the middle four, a frame a cut can
        # begin on every two: asked to start a second before the clip, the
        # cut begins two seconds before it.
        make_video(SOURCE, seconds=12, keyframe_every=2.0)
        extent = {"context_before": 4.0, "clip_duration": 4.0, "context_after": 4.0}
        moment_id = add_moment(
            "channel_a",
            detected_at=T0,
            clip_path=CLIP,
            subtitles=subtitles.stored([Line(-1.5, -0.5, "just before"), Line(0.5, 1.5, "in the clip")]),
            **extent,
        )

        await service.client.post(f"/moments/{moment_id}/trim", json={"start": -1, "end": 3})
        await service.settle()
        state = await self.trim(service, moment_id)
        response = await service.client.get(state["subtitles_url"])

        assert cues(response.text) == [
            (pytest.approx(0.5, abs=0.05), pytest.approx(1.5, abs=0.05), "just before"),
            (pytest.approx(2.5, abs=0.05), pytest.approx(3.5, abs=0.05), "in the clip"),
        ]


def cues(srt: str) -> list[tuple[float, float, str]]:
    """The subtitles in a SubRip file: when each is shown, and what it says."""

    def seconds(stamp: str) -> float:
        hours, minutes, rest = stamp.split(":")
        whole, thousandths = rest.split(",")
        return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(thousandths) / 1000

    found = []
    for block in srt.removeprefix("\ufeff").split("\r\n\r\n"):
        if block:
            _number, times, *rows = block.split("\r\n")
            start, end = times.split(" --> ")
            found.append((seconds(start), seconds(end), "\n".join(rows)))
    return found
