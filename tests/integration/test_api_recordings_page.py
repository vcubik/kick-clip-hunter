"""Where stream recordings show on the dashboard: the control beside each
channel on the Channels page, and the Recordings page - its list, the
recording that is open, and the chat replayed beside it.

Like the other page tests these read the parsed HTML, and leave the exact
wording to where it is made (tests/unit/test_dashboard_view.py).
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from kick_clip_hunter import chat_trace, db, recording_manager, stream_recording
from kick_clip_hunter import dashboard_view as view
from tests.support.data import T0, add_chat, add_stream_recording, add_streamer, minutes, stream_recordings
from tests.support.html import Element, parse

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
DELAY = timedelta(seconds=chat_trace.CHAT_DELAY_SECONDS)


@pytest.fixture(autouse=True)
def today(service, monkeypatch) -> date:
    """Today is pinned to the local day of T0, as in the other page tests."""
    day = T0.astimezone().date()
    monkeypatch.setattr(service.main, "_today", lambda: day)
    return day


class RunningRecorder:
    """Stands in for a channel's recorder while the stream is coming in."""

    is_active = True
    whole_stream = None

    def stop(self) -> None:
        self.is_active = False


async def page_at(service, path: str, **params) -> Element:
    response = await service.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return parse(response.text)


async def channels(service) -> Element:
    return await page_at(service, "/dashboard/channels")


async def recordings(service, **params) -> Element:
    return await page_at(service, "/dashboard/recordings", **params)


def footage(channel: str, seconds: int = 6) -> None:
    """Gives the recording of a channel that much footage of made-up bytes."""
    kept = recording_manager.stream_recording_of(channel)
    for second in range(seconds):
        kept.writer.add(T0 + timedelta(seconds=second), 1.0, "7-0", b"x" * 1000)


def said_at(
    recording_second: float,
    channel: str = CHANNEL,
    sender: str = "alice",
    content: str = "hello",
    broadcaster_user_id: int = 1,
) -> None:
    """Chat written by someone watching that second of a recording that
    began at T0 - so arriving that much later than it was broadcast as chat
    is taken to run behind."""
    add_chat(channel, sender, content, T0 + timedelta(seconds=recording_second) + DELAY, broadcaster_user_id)


def chat_lines(box: Element) -> list[tuple[str, str, str]]:
    """(second of the file, who, what) for every line in a box of chat."""
    lines = []
    for line in box.find("p", class_="ch-chat"):
        who, what = [child for child in line.children if not isinstance(child, str)]
        lines.append((line.attrs["data-at"], who.text, what.text))
    return lines


class TestTheControlBesideAChannel:
    def control(self, page: Element, channel: str = CHANNEL) -> Element:
        return page.one("span", class_="rec-control", data_recording=channel)

    def shown(self, control: Element) -> list[str]:
        """The buttons of a control that are not hidden."""
        return [button.text for button in control.find("button") if "hidden" not in button.attrs]

    async def test_a_channel_that_is_not_being_recorded_offers_to_start(self, service):
        add_streamer(CHANNEL, 1)

        control = self.control(await channels(service))

        assert self.shown(control) == ["Start recording"]
        assert control.one("span", data_recording_words=True).text == ""
        assert control.attrs["data-recording-state"] == ""
        # Both buttons are there for the script to swap.
        assert [button.text for button in control.find("button")] == ["Start recording", "Stop recording"]

    async def test_one_that_is_says_what_the_recording_is_doing_and_offers_to_stop(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        control = self.control(await channels(service))

        assert self.shown(control) == ["Stop recording"]
        assert control.one("span", data_recording_words=True).text == "Waiting for the stream"
        assert control.attrs["data-recording-state"] == "running"

    async def test_it_says_how_much_has_been_recorded(self, service, monkeypatch):
        service.watch(CHANNEL)
        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        footage(CHANNEL, seconds=75)

        control = self.control(await channels(service))

        assert control.one("span", data_recording_words=True).text == "0:01:15 recorded, 0 MB"

    async def test_one_whose_recording_is_being_saved_says_so_and_can_be_recorded_again(self, service, monkeypatch):
        add_streamer(CHANNEL, 1)
        monkeypatch.setitem(service.main._stream_saves, 7, {"channel": CHANNEL, "seconds": 3725.0})

        control = self.control(await channels(service))

        assert control.one("span", data_recording_words=True).text == "Saving 1:02:05"
        assert control.attrs["data-recording-state"] == "saving"
        assert self.shown(control) == ["Start recording"]

    async def test_each_channel_has_its_own(self, service):
        service.watch("channel_a")
        service.watch("channel_b")
        await service.client.post("/channels/channel_b/recording/start")

        page = await channels(service)

        assert self.shown(self.control(page, "channel_a")) == ["Start recording"]
        assert self.shown(self.control(page, "channel_b")) == ["Stop recording"]
        labels = [button.attrs["aria-label"] for button in self.control(page, "channel_a").find("button")]
        assert labels == ["Start recording channel_a", "Stop recording channel_a"]

    async def test_the_page_says_what_recording_is(self, service):
        add_streamer(CHANNEL, 1)

        lead = (
            (await channels(service)).one("section", aria_labelledby="watchlist-title").one("p", class_="section-lead")
        )

        assert "Tracking looks for clips" in lead.text
        assert "whole stream and its chat, tracked or not" in lead.text
        assert "Recordings page" in lead.text


class TestWhatAChannelIsDoingRightNow:
    """ "Recording" means one thing on the dashboard: a stream that is being
    kept whole. A channel that is merely watched for clips is live."""

    def right_now(self, page: Element) -> dict[str, tuple[str, bool]]:
        """By channel: the words, and whether its lamp is lit."""
        found = {}
        for row in page.one("table").one("tbody").find("tr"):
            cells = row.find("td")
            found[cells[0].text] = (cells[2].text, bool(cells[2].find("span", class_="ch-lamp-rec")))
        return found

    async def test_a_channel_recorded_whole_says_recording_while_its_stream_comes_in(self, service, monkeypatch):
        service.watch("recorded")
        service.watch("only_watched")
        service.watch("waited_for")
        for slug in ("recorded", "only_watched"):
            monkeypatch.setitem(recording_manager._recorders, slug, RunningRecorder())
        await service.client.post("/channels/recorded/recording/start")
        await service.client.post("/channels/waited_for/recording/start")

        now = self.right_now(await channels(service))

        assert now == {
            "recorded": ("Recording", True),
            "only_watched": ("Live", True),
            "waited_for": ("Not live", False),
        }

    async def test_a_channel_recorded_with_tracking_off_is_looked_at_all_the_same(self, service):
        add_streamer(CHANNEL, 1, tracking=False)
        assert self.right_now(await channels(service)) == {CHANNEL: ("Not watched", False)}

        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        assert self.right_now(await channels(service)) == {CHANNEL: ("Not live", False)}

    async def test_the_bar_says_recording_only_when_a_stream_is_kept_whole(self, service, monkeypatch):
        service.watch(CHANNEL)
        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())

        async def lamp() -> str:
            return (await channels(service)).one("header", class_="bar").one("span", class_="ch-lamp-rec").text

        assert await lamp() == "Live"

        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        assert await lamp() == "Recording"

    async def test_the_bar_counts_a_recorded_channel_among_those_looked_at(self, service, monkeypatch):
        add_streamer("tracked", 1)
        add_streamer("recorded_only", 2, tracking=False)
        service.api.add_channel("recorded_only", 2)
        monkeypatch.setitem(recording_manager._recorders, "recorded_only", RunningRecorder())
        await service.client.post("/channels/recorded_only/recording/start")

        bar = (await channels(service)).one("header", class_="bar")

        assert bar.one("span", class_="bar-meta").text == "1 of 2 channels"

    async def test_what_is_said_about_moments_counts_only_where_they_can_come_from(self, service, monkeypatch):
        # A stream kept whole on a channel that is not tracked brings no
        # moments, however live it is.
        add_streamer("tracked", 1)
        add_streamer("recorded_only", 2, tracking=False)
        monkeypatch.setitem(recording_manager._recorders, "recorded_only", RunningRecorder())
        await service.client.post("/channels/recorded_only/recording/start")

        lead = (await page_at(service, "/dashboard")).one("p", class_="lead").text

        assert lead.startswith("The watched channel is not live right now.")


class TestTheListOfRecordings:
    def rows(self, page: Element) -> list[list[str]]:
        return [[part.text for part in row.find("span") + row.find("b")] for row in page.find("a", class_="rec-row")]

    async def test_with_none_the_page_says_how_to_make_one(self, service):
        page = await recordings(service)

        assert page.find("a", class_="rec-row") == []
        assert page.one("p", class_="queue-empty").text == "No recordings yet"
        statement = page.one("section", class_="statement")
        assert statement.one("h1").text == "No recordings yet"
        assert "Start one next to the channel on the Channels page" in statement.one("p", class_="lead").text
        assert [(link.text, link.attrs["href"]) for link in statement.find("a")] == [
            ("Go to Channels", "/dashboard/channels")
        ]

    async def test_lists_them_newest_first_with_when_and_how_long(self, service, today):
        add_stream_recording("channel_a", started_at=T0 - timedelta(days=1), seconds=9230)
        add_stream_recording("channel_b", started_at=T0 - minutes(90), seconds=600)

        page = await recordings(service)

        assert self.rows(page) == [
            ["0:10:00", view.since_words((T0 - minutes(90)).astimezone(), today), "channel_b"],
            ["2:33:50", view.since_words((T0 - timedelta(days=1)).astimezone(), today), "channel_a"],
        ]

    async def test_the_newest_is_the_open_one_unless_another_is_asked_for(self, service):
        older = add_stream_recording("channel_a", started_at=T0 - timedelta(days=1))
        newer = add_stream_recording("channel_b")

        def current(page: Element) -> list[str]:
            return [row.attrs["href"] for row in page.find("a", class_="rec-row", aria_current="true")]

        assert current(await recordings(service)) == [f"/dashboard/recordings?recording={newer}"]
        assert current(await recordings(service, recording=older)) == [f"/dashboard/recordings?recording={older}"]
        assert (await recordings(service, recording=older)).one("article", class_="recording").one("h1").text == (
            "channel_a"
        )
        # One that is not there gives way to the newest.
        assert current(await recordings(service, recording=999)) == [f"/dashboard/recordings?recording={newer}"]

    async def test_one_that_is_not_over_says_what_it_is_doing(self, service, monkeypatch):
        service.watch(CHANNEL)
        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        footage(CHANNEL, seconds=42)

        page = await recordings(service)

        (row,) = page.find("a", class_="rec-row")
        assert row.one("span", class_="rec-length").text == "0:00:42 recorded, 0 MB"
        # The script keeps these words current, by the recording.
        assert row.attrs["data-recording-row"] == "1"
        assert "data-recording-words" in row.one("span", class_="rec-length").attrs

    async def test_one_that_could_not_be_saved_says_so(self, service):
        add_stream_recording(CHANNEL, state="failed")

        (row,) = (await recordings(service)).find("a", class_="rec-row")

        assert row.one("span", class_="rec-length").text == "Not saved"
        assert "data-recording-row" not in row.attrs

    async def test_the_list_carries_the_mark_it_was_made_with(self, service):
        # What the script compares with the service's to know the list is
        # out of date.
        add_stream_recording(CHANNEL)
        service.watch("channel_b")
        await service.client.post("/channels/channel_b/recording/start")

        page = await recordings(service)

        status = (await service.client.get("/recordings/status")).json()
        assert page.one("aside", data_recordings=True).attrs["data-mark"] == status["mark"] == "2:2r"

    async def test_says_how_much_room_the_disk_has(self, service, monkeypatch):
        monkeypatch.setattr(stream_recording, "free_bytes", lambda: 225 * 1024**3)

        page = await recordings(service)

        assert page.one("p", class_="queue-foot").text == "225 GB free on the disk recordings are kept on."

    async def test_a_channel_name_is_text_and_never_markup(self, service):
        add_stream_recording("<b>bold</b>")

        page = await recordings(service)

        assert page.one("a", class_="rec-row").one("b").text == "<b>bold</b>"
        assert page.one("article", class_="recording").one("h1").text == "<b>bold</b>"


class TestARecordingThatCanBePlayed:
    async def test_shows_its_video_and_what_it_is(self, service, today):
        recording_id = add_stream_recording(CHANNEL, started_at=T0 - minutes(150), seconds=9230)
        (row,) = stream_recordings()

        article = (await recordings(service)).one("article", class_="recording")

        assert article.one("h1").text == CHANNEL
        started = (T0 - minutes(150)).astimezone()
        ended = started + timedelta(seconds=9230)
        assert article.one("p", class_="moment-when").text == view.recording_span_words(started, ended, today)
        assert article.one("span", class_="counter").text == "2:33:50"
        video = article.one("video", data_recording_clip=True)
        assert video.attrs["src"] == f"/streams/{row['file_path']}"
        assert "controls" in video.attrs
        assert article.one("p", class_="moment-summary").text == "3 GB."
        assert recording_id == row["id"]

    async def test_says_where_the_file_is_and_offers_it(self, service):
        add_stream_recording(CHANNEL)
        (row,) = stream_recordings()

        line = (await recordings(service)).one("div", class_="rec-file")

        # Where on this machine, for whoever wants the file itself.
        assert line.one("span", class_="ch-meta").text == str(
            (stream_recording.STREAMS_DIR / row["file_path"]).resolve()
        )
        download = line.one("a")
        assert (download.text, download.attrs["href"]) == ("Download", f"/streams/{row['file_path']}")
        assert "download" in download.attrs

    async def test_the_file_is_served_and_can_be_read_from_anywhere_in_it(self, service):
        # A recording is gigabytes: a player moves about in it by asking for
        # the bytes it needs, not by fetching all of it.
        add_stream_recording(CHANNEL)
        (row,) = stream_recordings()
        address = f"/streams/{row['file_path']}"

        whole = await service.client.get(address)
        middle = await service.client.get(address, headers={"Range": "bytes=4-9"})

        assert whole.status_code == 200 and whole.content == b"not really a video"
        assert middle.status_code == 206
        assert middle.content == b"really"
        assert middle.headers["content-range"] == f"bytes 4-9/{len(whole.content)}"

    async def test_says_what_is_missing_and_why_it_ended(self, service):
        add_stream_recording(
            CHANNEL,
            parts=[(T0, 1800.0), (T0 + timedelta(seconds=1830), 900.0), (T0 + timedelta(seconds=2790), 600.0)],
            ended_by="stream_ended",
        )

        summary = (await recordings(service)).one("p", class_="moment-summary").text

        assert summary == (
            "3 GB. The recording has 2 breaks, where the file is missing 1 minute 30 seconds of the stream. "
            "It ended by itself once the stream had been gone for 10 minutes."
        )

    async def test_deleting_is_asked_for_twice(self, service):
        recording_id = add_stream_recording(CHANNEL)

        control = (await recordings(service)).one("article", class_="recording").one("span", data_delete=True)

        assert "hidden" not in control.one("button", data_delete_open=True).attrs
        confirm = control.one("span", data_delete_confirm=True)
        assert "hidden" in confirm.attrs
        assert "Removes the file for good." in confirm.text
        assert confirm.one("button", data_delete_go=True).attrs["data-delete-go"] == str(recording_id)
        assert [button.text for button in confirm.find("button")] == ["Delete", "Cancel"]

    async def test_a_file_name_is_made_safe_for_an_address(self, service):
        add_stream_recording(CHANNEL, file=False, file_path="some channel/a file #1?.mp4")
        folder = stream_recording.STREAMS_DIR / "some channel"
        folder.mkdir(parents=True)
        (folder / "a file #1?.mp4".replace("?", "")).write_bytes(b"x")
        db_path = "some channel/a file #1.mp4"
        conn = db.get_connection()
        try:
            conn.execute("UPDATE stream_recordings SET file_path = ?", (db_path,))
            conn.commit()
        finally:
            conn.close()

        video = (await recordings(service)).one("video", data_recording_clip=True)

        assert video.attrs["src"] == "/streams/some%20channel/a%20file%20%231.mp4"
        assert (await service.client.get(video.attrs["src"])).content == b"x"


class TestARecordingThatCanNotBePlayed:
    async def test_one_that_is_running_says_so_and_where_to_stop_it(self, service, monkeypatch):
        service.watch(CHANNEL)
        monkeypatch.setitem(recording_manager._recorders, CHANNEL, RunningRecorder())
        await service.client.post(f"/channels/{CHANNEL}/recording/start")
        footage(CHANNEL, seconds=42)

        page = await recordings(service)

        assert page.find("video") == []
        statement = page.one("section", class_="statement")
        assert statement.one("h1").text == f"Recording {CHANNEL}"
        lead = statement.one("p", class_="lead")
        assert lead.one("span", data_recording_words=True).text == "0:00:42 recorded, 0 MB"
        assert lead.text.endswith("It can be played here once it has been stopped and saved.")
        # The script keeps the words current, by the channel.
        assert lead.attrs["data-recording"] == CHANNEL
        assert [link.attrs["href"] for link in statement.find("a")] == ["/dashboard/channels"]

    async def test_one_that_is_being_saved_says_that_it_takes_a_while(self, service, monkeypatch):
        recording_id = add_stream_recording(CHANNEL, state="saving")
        monkeypatch.setitem(service.main._stream_saves, recording_id, {"channel": CHANNEL, "seconds": 600.0})

        statement = (await recordings(service)).one("section", class_="statement")

        assert statement.one("h1").text == f"Saving the recording of {CHANNEL}"
        assert "being made into one file" in statement.one("p", class_="lead").text
        assert statement.find("button") == []

    async def test_one_that_could_not_be_saved_says_where_its_footage_is_and_offers_another_try(self, service):
        recording_id = add_stream_recording(CHANNEL, state="failed")

        statement = (await recordings(service)).one("section", class_="statement")

        assert statement.one("h1").text == "This recording could not be saved"
        folder = stream_recording.parts_folder(CHANNEL, recording_id).resolve()
        assert str(folder) in statement.one("p", class_="lead").text
        again = statement.one("button", data_save_again=True)
        assert (again.text, again.attrs["data-save-again"]) == ("Try saving again", str(recording_id))
        assert statement.one("button", data_delete_go=True).attrs["data-delete-go"] == str(recording_id)

    async def test_one_whose_file_is_gone_says_which_file_and_can_be_taken_off_the_list(self, service):
        recording_id = add_stream_recording(CHANNEL, file=False)
        (row,) = stream_recordings()

        page = await recordings(service)

        assert page.find("video") == []
        statement = page.one("section", class_="statement")
        assert statement.one("h1").text == "The file of this recording is gone"
        assert (
            str((stream_recording.STREAMS_DIR / row["file_path"]).resolve()) in statement.one("p", class_="lead").text
        )
        assert statement.one("button", data_delete_go=True).attrs["data-delete-go"] == str(recording_id)
        assert statement.find("button", data_save_again=True) == []

    async def test_a_recording_whose_parts_can_not_be_read_is_not_offered_to_play(self, service):
        recording_id = add_stream_recording(CHANNEL)
        conn = db.get_connection()
        try:
            conn.execute("UPDATE stream_recordings SET parts = 'not json' WHERE id = ?", (recording_id,))
            conn.commit()
        finally:
            conn.close()

        page = await recordings(service)

        assert page.find("video") == []


class TestChatBesideARecording:
    def box(self, page: Element) -> Element:
        return page.one("div", data_recording_chat=True)

    async def chunk(self, service, recording_id: int, at: float | str) -> Element:
        response = await service.client.get(f"/dashboard/recordings/{recording_id}/chat", params={"at": at})
        assert response.status_code == 200, response.text
        return self.box(parse(response.text))

    async def test_every_line_knows_which_second_of_the_file_it_belongs_to(self, service):
        add_stream_recording(CHANNEL, seconds=600)
        said_at(12.5, sender="alice", content="first")
        said_at(95, sender="bob", content="second")

        box = self.box(await recordings(service))

        assert chat_lines(box) == [("12.5", "alice", "first"), ("95", "bob", "second")]

    async def test_the_page_starts_with_the_first_stretch_and_says_which(self, service):
        recording_id = add_stream_recording(CHANNEL, seconds=3600)
        after = service.main.RECORDING_CHAT_AFTER_SECONDS
        said_at(after - 1, content="in the first stretch")
        said_at(after + 30, content="in the next one")

        box = self.box(await recordings(service))

        assert [what for _at, _who, what in chat_lines(box)] == ["in the first stretch"]
        assert (box.attrs["data-first"], box.attrs["data-last"]) == ("0", str(after))
        assert "data-whole" not in box.attrs
        assert box.attrs["data-url"] == f"/dashboard/recordings/{recording_id}/chat"

    async def test_another_stretch_is_fetched_around_where_the_video_is(self, service):
        recording_id = add_stream_recording(CHANNEL, seconds=3600)
        before, after = service.main.RECORDING_CHAT_BEFORE_SECONDS, service.main.RECORDING_CHAT_AFTER_SECONDS
        said_at(1000 - before - 5, content="too early")
        said_at(1000 - before + 5, content="just before")
        said_at(1000 + after - 5, content="just in")
        said_at(1000 + after + 5, content="too late")

        box = await self.chunk(service, recording_id, 1000)

        assert [what for _at, _who, what in chat_lines(box)] == ["just before", "just in"]
        assert (box.attrs["data-first"], box.attrs["data-last"]) == (str(1000 - before), str(1000 + after))

    async def test_the_last_stretch_says_that_it_is(self, service):
        recording_id = add_stream_recording(CHANNEL, seconds=600)
        said_at(590, content="at the end")

        box = await self.chunk(service, recording_id, 550)

        assert chat_lines(box) == [("590", "alice", "at the end")]
        assert box.attrs["data-last"] == "600" and "data-whole" in box.attrs

    async def test_a_short_recording_is_all_there_from_the_start(self, service):
        add_stream_recording(CHANNEL, seconds=120)

        box = self.box(await recordings(service))

        assert (box.attrs["data-first"], box.attrs["data-last"]) == ("0", "120")
        assert "data-whole" in box.attrs

    @pytest.mark.parametrize("at", ["-50", "nan", "inf", "99999"])
    async def test_a_second_that_is_not_in_the_file_is_taken_to_the_nearest_that_is(self, service, at):
        recording_id = add_stream_recording(CHANNEL, seconds=600)

        box = await self.chunk(service, recording_id, at)

        assert 0 <= float(box.attrs["data-first"]) <= float(box.attrs["data-last"]) <= 600

    async def test_chat_is_put_where_the_channel_has_it_against_the_picture(self, service):
        # The delay set for the channel on the review page holds here too.
        add_stream_recording(CHANNEL, seconds=600)
        add_chat(CHANNEL, "alice", "hello", T0 + timedelta(seconds=100))
        conn = db.get_connection()
        try:
            db.set_chat_delay(conn, CHANNEL, 8)
        finally:
            conn.close()

        page = await recordings(service)

        assert chat_lines(self.box(page)) == [("92", "alice", "hello")]
        assert page.one("div", class_="chat-foot").text == "Chat 8 s earlier, as the channel has it on the review page."

    async def test_chat_is_found_under_the_name_kick_sends_it_with(self, service):
        # The watchlist has the channel as it was typed; chat arrives under
        # Kick's own spelling of the name, which is all in small letters.
        add_stream_recording("Some_Channel", seconds=600)
        said_at(12.5, channel="some_channel", content="there all along")

        box = self.box(await recordings(service))

        assert chat_lines(box) == [("12.5", "alice", "there all along")]

    async def test_and_so_is_the_delay_the_channel_has_on_the_review_page(self, service):
        # Which is kept under the name its moments have: the chat's.
        recording_id = add_stream_recording("Some_Channel", seconds=600)
        add_chat("some_channel", "alice", "hello", T0 + timedelta(seconds=100))
        conn = db.get_connection()
        try:
            db.set_chat_delay(conn, "some_channel", 8)
        finally:
            conn.close()

        page = await recordings(service)

        assert chat_lines(self.box(page)) == [("92", "alice", "hello")]
        assert page.one("div", class_="chat-foot").text == "Chat 8 s earlier, as the channel has it on the review page."
        assert chat_lines(await self.chunk(service, recording_id, 100)) == [("92", "alice", "hello")]

    async def test_chat_from_a_break_in_the_stream_is_shown_where_the_picture_picks_up(self, service):
        # Two parts: a hundred seconds, then - a minute later - fifty more.
        add_stream_recording(CHANNEL, parts=[(T0, 100.0), (T0 + timedelta(seconds=160), 50.0)])
        said_at(40, content="in the first part")
        said_at(130, content="while nothing was recorded")
        said_at(170, content="in the second part")

        box = self.box(await recordings(service))

        assert chat_lines(box) == [
            ("40", "alice", "in the first part"),
            ("100", "alice", "while nothing was recorded"),
            ("110", "alice", "in the second part"),
        ]

    async def test_only_the_channels_own_chat_and_only_from_while_it_was_recorded(self, service):
        add_stream_recording(CHANNEL, seconds=120)
        said_at(-30, content="before it began")
        said_at(60, content="during")
        said_at(60, channel="another_channel", content="elsewhere", broadcaster_user_id=2)
        said_at(200, content="after it ended")

        box = self.box(await recordings(service))

        assert [what for _at, _who, what in chat_lines(box)] == ["during"]

    async def test_a_recording_with_no_chat_says_so(self, service):
        add_stream_recording(CHANNEL)

        box = self.box(await recordings(service))

        assert chat_lines(box) == []
        assert box.one("p", class_="chat-empty").text == "No chat was stored for this stretch."

    async def test_a_chat_too_busy_for_a_whole_stretch_is_given_as_far_as_it_goes(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "RECORDING_CHAT_LIMIT", 5)
        recording_id = add_stream_recording(CHANNEL, seconds=600)
        for second in range(10, 30):
            said_at(second, content=f"message {second}")

        box = await self.chunk(service, recording_id, 0)

        assert [at for at, _who, _what in chat_lines(box)] == ["10", "11", "12", "13", "14"]
        # The stretch ends where the lines do, so the next one is asked for
        # from there.
        assert box.attrs["data-last"] == "14" and "data-whole" not in box.attrs

    async def test_emotes_and_who_wrote_are_drawn_as_on_the_review_page(self, service):
        add_stream_recording(CHANNEL, broadcaster_user_id=41, seconds=120)
        said_at(5, sender="alice", content="KEKW [emote:37226:KEKW]", broadcaster_user_id=41)
        conn = db.get_connection()
        try:
            db.replace_channel_emotes(conn, 41, {"KEKW": ("7TVKEKW", 32, 32)})
            db.set_chat_identity(conn, 41, "alice", "#00ff00", json.dumps([{"type": "moderator", "text": "Moderator"}]))
        finally:
            conn.close()

        line = self.box(await recordings(service)).one("p", class_="ch-chat")

        assert [image.attrs["alt"] for image in line.find("img", class_="ch-emote")] == ["KEKW", "KEKW"]
        assert line.one("b").attrs["style"] == "color: #00ff00"
        assert line.one("img", class_="ch-badge").attrs["alt"] == "Moderator"

    async def test_what_chat_wrote_is_text_and_never_markup(self, service):
        add_stream_recording(CHANNEL, seconds=120)
        said_at(5, sender="<i>mallory</i>", content='<script>alert(1)</script> & "quotes"')

        box = self.box(await recordings(service))

        assert chat_lines(box) == [("5", "<i>mallory</i>", '<script>alert(1)</script> & "quotes"')]
        assert box.find("script") == [] and box.find("i") == []

    async def test_an_unknown_recording_is_a_404(self, service):
        response = await service.client.get("/dashboard/recordings/999/chat")

        assert response.status_code == 404

    async def test_a_recording_with_no_file_yet_has_no_chat_to_place(self, service):
        service.watch(CHANNEL)
        await service.client.post(f"/channels/{CHANNEL}/recording/start")

        response = await service.client.get("/dashboard/recordings/1/chat")

        assert response.status_code == 409

    async def test_a_stretch_is_nothing_but_the_box_of_chat(self, service):
        recording_id = add_stream_recording(CHANNEL)

        response = await service.client.get(f"/dashboard/recordings/{recording_id}/chat")

        page = parse(response.text)
        assert page.find("header") == [] and page.find("video") == []
        assert "chat-lines" in self.box(page).classes
