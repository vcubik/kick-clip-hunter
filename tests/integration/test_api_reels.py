"""A moment's clip made into a reel, through the service (see reels.py): the
clip and what goes with it sent to a fake GitHub, the storyboard workflow
started, the storyboard and the transcript taken back and shown on the Reels
page, the transcript put right there, a variant and a hook chosen, the render
workflow started with the transcript as subtitles and the reel taken back,
the comments that go with each step and the feedback on the reel - and what
happens when GitHub refuses, a workflow fails or nothing comes back. What the
workflows would put into the release the tests put there themselves; the
service's look at GitHub is called as its loop would."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import pytest

from kick_clip_hunter import chat_trace, db, recorder, reels, subtitles
from kick_clip_hunter.config import load_settings
from tests.support.data import T0, add_chat, add_moment
from tests.support.github import FakeGitHub, FakeRelease
from tests.support.html import parse
from tests.support.media import make_video, probe, require_ffmpeg
from tests.support.waiting import async_wait_until

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
USER_ID = 1
DASHBOARD = "http://pc.tail1234.ts.net:8000"
CLIP_BYTES = b"the moment's file, clip and footage around it"
BEFORE, CLIP, AFTER = 5.0, 20.0, 10.0
STORYBOARD = {
    "streamer": CHANNEL,
    "summary": "He nearly runs someone over.",
    "thoughts": "The point comes late, so a teaser.",
    "variants": [
        {"id": "A", "recommended": False, "structure": "S1", "title": "Straight", "duration": 16.0,
         "why": "Plain.", "render_args": ["--seg", "1-17"]},
        {"id": "B", "recommended": True, "structure": "S2", "title": "Teaser first", "duration": 18.2,
         "why": "The point first.",
         "render_args": ["--teaser", "14-16", "--seg", "1-17", "--subs", "work/clip.subs.srt"]},
    ],
    "hooks": [
        {"id": 1, "type": "T1", "text": "NEARLY RAN|*HIM OVER?*"},
        {"id": 2, "type": "T4", "text": "WAS IT *GREEN?*"},
    ],
    "question": "Which variant and hook?",
}  # fmt: skip
# What the speech model heard, and the subtitles Claude made of it.
HEARD = [subtitles.Line(1.2, 2.5, "watch out"), subtitles.Line(3.0, 4.6, "he has a jahu now")]
PROPOSED = [subtitles.Line(1.2, 2.5, "A: Watch out!"), subtitles.Line(3.0, 4.6, "B: He has a *red* now?")]


def srt_bytes(lines) -> bytes:
    return subtitles.srt(lines, 0.0).encode(subtitles.ENCODING)


def lines_of(raw: bytes) -> list[subtitles.Line]:
    return subtitles.from_srt(raw.decode("utf-8-sig"))


@pytest.fixture
def reeling(service, github: FakeGitHub, monkeypatch):
    """The service set up to have reels made in the fake GitHub."""
    monkeypatch.setattr(service.main, "settings", load_settings())
    return service


@pytest.fixture
def told(reeling, telegram, monkeypatch):
    """...and to send notes to Telegram, with links to the dashboard."""
    monkeypatch.setenv("DASHBOARD_URL", DASHBOARD)
    monkeypatch.setattr(reeling.main, "settings", load_settings())
    reeling.main._notifier = None
    return telegram


def clip_cut_at(chat_start: datetime, seconds: float) -> dict:
    """Columns of a moment whose clip is `seconds` long and whose first
    frame chat is taken to have seen at `chat_start` (stored, as the
    recorder stores it, on the stream's clock, which chat runs behind)."""
    broadcast = chat_start - timedelta(seconds=chat_trace.CHAT_DELAY_SECONDS)
    return {"clip_start": broadcast.isoformat(), "clip_duration": seconds}


def clip_file(name: str = "moment_1.mp4"):
    return recorder.CLIPS_DIR / CHANNEL / name


def clipped_moment(name: str = "moment_1.mp4", **columns) -> int:
    """A moment whose file holds BEFORE seconds of footage, its CLIP-second
    clip and AFTER seconds more, chat taken to have seen the clip's first
    frame at T0."""
    file = clip_file(name)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(CLIP_BYTES)
    return add_moment(
        CHANNEL,
        detected_at=T0 + timedelta(seconds=12),
        clip_path=f"{CHANNEL}/{name}",
        context_before=BEFORE,
        context_after=AFTER,
        **{**clip_cut_at(T0, CLIP), **columns},
    )


def reel_of(moment_id: int):
    conn = db.get_connection()
    try:
        return db.get_reel(conn, moment_id)
    finally:
        conn.close()


def set_reel(moment_id: int, **fields) -> None:
    conn = db.get_connection()
    try:
        db.put_reel(conn, moment_id, **fields)
    finally:
        conn.close()


def transcript_of(moment_id: int) -> list[subtitles.Line] | None:
    return subtitles.read(reel_of(moment_id)["transcript"])


async def ask(service, moment_id: int, **asked):
    if asked:
        return await service.client.post(f"/moments/{moment_id}/reel", json=asked)
    return await service.client.post(f"/moments/{moment_id}/reel")


async def asked_for_storyboard(service, github: FakeGitHub, moment_id: int, **asked) -> None:
    response = await ask(service, moment_id, **asked)
    assert response.status_code == 200, response.text
    await async_wait_until(lambda: reel_of(moment_id)["state"] != reels.SENDING, "the clip to be sent")
    assert reel_of(moment_id)["state"] == reels.STORYBOARDING, reel_of(moment_id)["error"]


def answer(github: FakeGitHub, moment_id: int, storyboard=STORYBOARD, heard=HEARD, proposed=None) -> None:
    """What the storyboard workflow puts into the release."""
    tag = reels.tag(moment_id)
    github.answer_storyboard(tag, storyboard, contact=b"frames")
    if heard is not None:
        github.put(tag, reels.HEARD, srt_bytes(heard))
        github.put(tag, reels.WORDS, b"[]")
    if proposed is not None:
        github.put(tag, reels.SUBTITLES, srt_bytes(proposed))


async def storyboard_back(service, github: FakeGitHub, moment_id: int, comment: str = "", **answered) -> None:
    await asked_for_storyboard(service, github, moment_id, **({"comment": comment} if comment else {}))
    answer(github, moment_id, **answered)
    await service.main._look_at_reels()


async def render(service, moment_id: int, variant: str, hook: str, **more):
    return await service.client.post(
        f"/moments/{moment_id}/reel/render", json={"variant": variant, "hook": hook, **more}
    )


async def rendered(service, github: FakeGitHub, moment_id: int, variant: str, hook: str, **more) -> dict:
    """Asks for a render and waits for it to be dispatched: its payload."""
    asked = len(github.dispatches)
    response = await render(service, moment_id, variant, hook, **more)
    assert response.status_code == 200, response.text
    await async_wait_until(lambda: len(github.dispatches) > asked, "the render to be asked for")
    event, payload = github.dispatches[-1]
    assert event == "render"
    return payload


async def save(service, moment_id: int, lines: list[dict]):
    return await service.client.post(f"/moments/{moment_id}/reel/transcript", json={"lines": lines})


async def workspace(service, moment_id: int):
    response = await service.client.get(f"/dashboard/reels/{moment_id}")
    assert response.status_code == 200, response.text
    return parse(response.text).one(data_reel=True)


async def page(service, path: str):
    response = await service.client.get(path)
    assert response.status_code == 200, response.text
    return parse(response.text)


class TestNotSetUp:
    async def test_without_a_repository_nothing_offers_a_reel_and_none_can_be_asked_for(self, service):
        moment_id = clipped_moment()

        review = await page(service, f"/dashboard/moments/{moment_id}")
        reels_page = await page(service, f"/dashboard/reels?moment={moment_id}")
        response = await ask(service, moment_id)

        assert review.find(data_reel_link=True) == []
        assert reels_page.find(data_reel=True) == []
        assert "not set up" in reels_page.one("main").text
        assert all(link.attrs["href"] != "/dashboard/reels" for link in reels_page.one("nav").find("a"))
        assert response.status_code == 409
        assert "REELS_REPO" in response.json()["detail"]


class TestTheWayThere:
    async def test_the_review_page_leads_to_the_reels_page_where_the_reel_is_made(self, reeling, github):
        moment_id = clipped_moment()

        link = (await page(reeling, f"/dashboard/moments/{moment_id}")).one(data_reel_link=True)
        opened = (await page(reeling, link.attrs["href"])).one(data_reel=True)

        assert link.text == "Make a reel"
        assert link.attrs["href"] == f"/dashboard/reels?moment={moment_id}"
        assert opened.attrs["data-reel-id"] == str(moment_id)
        assert opened.one(data_reel_ask=True).text == "Make a reel"

    async def test_once_asked_for_the_review_page_says_where_it_stands(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        line = (await page(reeling, f"/dashboard/moments/{moment_id}")).one("p", class_="reel-link")

        assert line.one(data_reel_link=True).text == "Open the reel"
        assert "being made" in line.text

    async def test_the_reels_page_is_in_the_bar_and_lists_the_reels_the_last_asked_first(self, reeling, github):
        first = clipped_moment("moment_1.mp4", title="The first one")
        second = clipped_moment("moment_2.mp4")
        await storyboard_back(reeling, github, first)
        await asked_for_storyboard(reeling, github, second)

        listed = await page(reeling, "/dashboard/reels")

        assert any(link.attrs["href"] == "/dashboard/reels" for link in listed.one("nav").find("a"))
        rows = listed.find("a", class_="rec-row")
        assert [row.attrs["data-reel-row"] for row in rows] == [str(second), str(first)]
        assert [row.one(data_reel_row_words=True).text for row in rows] == ["Storyboard", "Choose"]
        assert "The first one" in rows[1].text
        # The last asked for is open, and its row is marked.
        assert rows[0].attrs.get("aria-current") == "true"
        assert listed.one(data_reel=True).attrs["data-reel-id"] == str(second)

    async def test_a_moment_that_is_gone_is_said_to_be(self, reeling, github):
        listed = await page(reeling, "/dashboard/reels?moment=999")

        assert "No such moment" in listed.one("main").text


class TestAskingForAStoryboard:
    async def test_the_clip_and_its_context_go_up_and_the_storyboard_workflow_starts(self, reeling, github):
        moment_id = clipped_moment(title="Nearly ran him over", notes="good one", rating=5)
        add_chat(CHANNEL, "fan", "xdddd", T0 + timedelta(seconds=3))
        add_chat(CHANNEL, "other", "xdddd", T0 + timedelta(seconds=3.5))
        add_chat(CHANNEL, "early", "what", T0 - timedelta(seconds=2))

        await asked_for_storyboard(reeling, github, moment_id)

        tag = reels.tag(moment_id)
        assert github.dispatches == [("storyboard", {"tag": tag})]
        assert github.asset(tag, "clip.mp4") == CLIP_BYTES
        context = json.loads(github.asset(tag, "context.json"))
        assert context["streamer"] == CHANNEL
        assert context["credit"] == f"kick.com/{CHANNEL}"
        assert (context["title"], context["notes"], context["rating"]) == ("Nearly ran him over", "good one", 5)
        assert context["clip"] == {"start": BEFORE, "end": BEFORE + CLIP}
        # Chat at the second of the file it belongs to: the clip begins
        # BEFORE seconds into it.
        assert [(line["t"], line["user"]) for line in context["chat"]] == [
            (BEFORE - 2, "early"),
            (BEFORE + 3, "fan"),
            (BEFORE + 3.5, "other"),
        ]
        assert context["peak"] == BEFORE + 3
        assert context["transcript_corrected"] is False
        assert context["comment"] == ""

    async def test_the_page_says_the_storyboard_is_being_made_and_offers_nothing_more_meanwhile(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        opened = await workspace(reeling, moment_id)

        assert "data-busy" in opened.attrs
        assert "being made" in opened.one(data_status=True).text
        assert "disabled" in opened.one(data_reel_ask=True).attrs
        assert opened.attrs["data-row-words"] == "Storyboard"

    async def test_asked_again_while_it_is_made_nothing_more_is_started(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        response = await ask(reeling, moment_id)

        assert response.json()["state"] == reels.STORYBOARDING
        assert len(github.dispatches) == 1

    async def test_a_moment_without_its_clip_file_has_no_reel_made(self, reeling, github):
        moment_id = clipped_moment()
        clip_file().unlink()

        response = await ask(reeling, moment_id)
        opened = await workspace(reeling, moment_id)

        assert response.status_code == 409
        assert github.releases == {}
        assert opened.find(data_reel_ask=True) == []
        assert "no clip" in opened.one(data_status=True).text

    async def test_github_refusing_the_clip_is_said(self, reeling, github):
        moment_id = clipped_moment()
        github.failures["/assets"] = 500

        await ask(reeling, moment_id)
        await async_wait_until(lambda: reel_of(moment_id)["state"] == reels.FAILED, "the reel to fail")

        assert "GitHub refused" in reel_of(moment_id)["error"]
        assert github.dispatches == []

    async def test_a_send_cut_short_by_a_restart_is_given_up(self, reeling, github):
        moment_id = clipped_moment()
        set_reel(moment_id, state=reels.SENDING, asked_at=T0.isoformat())

        await reeling.main._look_at_reels()

        assert reel_of(moment_id)["state"] == reels.FAILED
        assert "cut short" in reel_of(moment_id)["error"]


class TestTheStoryboardComingBack:
    async def test_it_is_kept_with_its_contact_sheet_and_the_page_offers_its_choices(self, reeling, github):
        moment_id = clipped_moment()

        await storyboard_back(reeling, github, moment_id)

        reel = reel_of(moment_id)
        assert reel["state"] == reels.CHOOSING
        assert json.loads(reel["storyboard"])["summary"] == STORYBOARD["summary"]
        assert (recorder.CLIPS_DIR / reel["contact_path"]).read_bytes() == b"frames"
        opened = await workspace(reeling, moment_id)
        variants = opened.find(data_reel_variant=True)
        assert [
            (v.attrs["data-reel-variant"], v.attrs["aria-pressed"], v.attrs["data-subtitled"]) for v in variants
        ] == [
            ("A", "false", "off"),
            ("B", "true", "on"),
        ]
        hooks = opened.find(data_reel_hook=True)
        assert [hook.one("b").text for hook in hooks] == ["NEARLY RAN / HIM OVER?", "WAS IT GREEN?"]
        assert hooks[0].attrs["aria-pressed"] == "true"
        assert opened.one("img", class_="reel-contact").attrs["src"].startswith(f"/clips/reels/moment_{moment_id}")
        # The recommended variant has subtitles, so they are on to begin with.
        switch = opened.one(data_reel_subtitles=True)
        assert switch.one(data_value="on").attrs["aria-pressed"] == "true"

    async def test_the_transcript_is_what_the_speech_model_heard_where_claude_proposed_no_subtitles(
        self, reeling, github
    ):
        moment_id = clipped_moment()

        await storyboard_back(reeling, github, moment_id)

        assert transcript_of(moment_id) == HEARD
        assert subtitles.read(reel_of(moment_id)["heard"]) == HEARD
        editor = (await workspace(reeling, moment_id)).one(data_reel_transcript=True)
        lines = editor.one(data_reel_lines=True).find(data_reel_line=True)
        assert [line.one(data_reel_start=True).attrs["value"] for line in lines] == ["0:01.2", "0:03.0"]
        assert [line.one(data_reel_end=True).attrs["value"] for line in lines] == ["0:02.5", "0:04.6"]
        assert [line.one(data_reel_text=True).text for line in lines] == ["watch out", "he has a jahu now"]
        assert float(editor.attrs["data-length"]) == BEFORE + CLIP + AFTER

    async def test_the_subtitles_claude_proposed_are_the_transcript_and_what_was_heard_is_beside_it(
        self, reeling, github
    ):
        moment_id = clipped_moment()

        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)

        assert transcript_of(moment_id) == PROPOSED
        heard = (await workspace(reeling, moment_id)).one("details", class_="reel-heard")
        taken = heard.find(data_reel_take=True)
        assert [(button.attrs["data-start"], button.attrs["data-text"]) for button in taken] == [
            ("1.2", "watch out"),
            ("3", "he has a jahu now"),
        ]

    async def test_its_coming_is_told_of_with_a_link_to_the_reels_page(self, reeling, github, told):
        moment_id = clipped_moment()

        await storyboard_back(reeling, github, moment_id)
        await async_wait_until(lambda: told.sent, "the note")

        shown = told.sent[0]["shown"]
        assert "Storyboard ready" in shown
        assert STORYBOARD["summary"] in shown
        assert told.sent[0]["links"] == [f"{DASHBOARD}/dashboard/reels?moment={moment_id}"]

    async def test_one_that_is_not_a_storyboard_fails_the_reel(self, reeling, github):
        moment_id = clipped_moment()

        await storyboard_back(reeling, github, moment_id, storyboard={"summary": "no variants"})

        assert reel_of(moment_id)["state"] == reels.FAILED
        assert "no variants" in reel_of(moment_id)["error"]

    async def test_a_failed_workflow_run_fails_the_reel(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)
        github.runs = [
            {
                "display_title": reels.run_name("storyboard", reels.tag(moment_id)),
                "status": "completed",
                "conclusion": "failure",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        await reeling.main._look_at_reels()

        assert reel_of(moment_id)["state"] == reels.FAILED
        assert "ended in failure" in reel_of(moment_id)["error"]

    async def test_a_run_still_going_is_waited_for(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)
        github.runs = [
            {
                "display_title": reels.run_name("storyboard", reels.tag(moment_id)),
                "status": "in_progress",
                "conclusion": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        await reeling.main._look_at_reels()

        assert reel_of(moment_id)["state"] == reels.STORYBOARDING

    async def test_without_a_word_from_github_it_is_given_up_after_its_limit(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)
        github.reads_actions = False
        long_ago = datetime.now(timezone.utc) - reels.STORYBOARD_LIMIT - timedelta(minutes=1)
        set_reel(moment_id, asked_at=long_ago.isoformat())

        await reeling.main._look_at_reels()

        assert reel_of(moment_id)["state"] == reels.FAILED
        assert "Nothing came back" in reel_of(moment_id)["error"]


class TestPuttingTheTranscriptRight:
    async def test_what_is_saved_is_kept_in_order_with_empty_lines_left_out(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await save(
            reeling,
            moment_id,
            [
                {"start": 3.0, "end": 4.6, "text": "  He has  a *red*\n now? "},
                {"start": 1.2, "end": 2.5, "text": "A: Watch out!"},
                {"start": 5.0, "end": 6.0, "text": "   "},
            ],
        )

        assert response.json() == {"moment_id": moment_id, "lines": 2, "kept": 0, "cut": 0}
        assert transcript_of(moment_id) == [
            subtitles.Line(1.2, 2.5, "A: Watch out!"),
            subtitles.Line(3.0, 4.6, "He has a *red* now?"),
        ]
        assert reel_of(moment_id)["transcript_edited"] == 1
        editor = (await workspace(reeling, moment_id)).one(data_reel_transcript=True)
        assert "Put right by hand" in editor.text
        # What was heard is kept as it was heard.
        assert subtitles.read(reel_of(moment_id)["heard"]) == HEARD

    @pytest.mark.parametrize(
        ("line", "said"),
        [
            ({"start": "0:01", "end": 2.0, "text": "x"}, "Line 2: write when it starts and ends"),
            ({"start": 2.0, "end": 2.0, "text": "x"}, "Line 2: it has to end after it starts"),
            ({"start": -1.0, "end": 2.0, "text": "x"}, "Line 2: it has to end after it starts"),
            ({"start": 30.0, "end": BEFORE + CLIP + AFTER + 1, "text": "x"}, "Line 2: the clip ends at 0:35.0"),
            ({"start": 1.0, "end": 2.0}, "Line 2: expected its start, end and words"),
        ],
    )
    async def test_a_line_that_cannot_be_kept_is_refused_with_its_number(self, reeling, github, line, said):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await save(reeling, moment_id, [{"start": 0.0, "end": 1.0, "text": "fine"}, line])

        assert response.status_code == 400
        assert said in response.json()["detail"]
        assert transcript_of(moment_id) == HEARD

    async def test_nothing_is_saved_before_there_is_a_transcript(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        response = await save(reeling, moment_id, [])

        assert response.status_code == 409
        assert "comes with its storyboard" in response.json()["detail"]

    async def test_nothing_is_saved_while_a_new_storyboard_brings_another(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await asked_for_storyboard(reeling, github, moment_id)

        response = await save(reeling, moment_id, [])
        editor = (await workspace(reeling, moment_id)).one(data_reel_transcript=True)

        assert response.status_code == 409
        assert "disabled" in editor.one(data_reel_transcript_save=True).attrs
        assert all("disabled" in field.attrs for field in editor.one(data_reel_lines=True).find(data_reel_text=True))


class TestLinesKeptInTheReel:
    async def test_ticked_lines_are_kept_offered_again_and_go_up_with_the_render(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)

        response = await save(
            reeling,
            moment_id,
            [
                {"start": 1.2, "end": 2.5, "text": "A: Watch out!", "keep": False},
                {"start": 3.0, "end": 4.6, "text": "B: He has a *red* now?", "keep": True},
                {"start": 5.0, "end": 6.0, "text": "  ", "keep": True},
            ],
        )
        lines = (await workspace(reeling, moment_id)).one(data_reel_lines=True).find(data_reel_line=True)
        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert response.json() == {"moment_id": moment_id, "lines": 2, "kept": 1, "cut": 0}
        assert ["checked" in line.one(data_reel_keep_line=True).attrs for line in lines] == [False, True]
        assert payload["keep"] == [{"start": 3.0, "end": 4.6, "text": "B: He has a *red* now?"}]

    async def test_ticks_go_with_a_transcript_a_new_storyboard_replaces(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await save(reeling, moment_id, [{"start": 1.2, "end": 2.5, "text": "watch out", "keep": True}])
        # Not put right by hand any more: the next storyboard brings its own.
        set_reel(moment_id, transcript_edited=0)

        await storyboard_back(reeling, github, moment_id)
        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert reel_of(moment_id)["kept_lines"] is None
        assert payload["keep"] == []

    async def test_only_ticked_lines_in_the_stretch_go_up_timed_to_it(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        set_reel(moment_id, part_start=2.0, part_end=12.0)
        await save(
            reeling,
            moment_id,
            [
                {"start": 0.5, "end": 1.5, "text": "before the stretch", "keep": True},
                {"start": 1.5, "end": 3.0, "text": "into the stretch", "keep": True},
                {"start": 5.0, "end": 6.0, "text": "in it", "keep": True},
            ],
        )

        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert payload["keep"] == [
            {"start": 0.0, "end": 1.0, "text": "into the stretch"},
            {"start": 3.0, "end": 4.0, "text": "in it"},
        ]


class TestLinesCutOutOfTheReel:
    async def test_marked_lines_are_kept_offered_again_and_go_up_with_the_render(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)

        response = await save(
            reeling,
            moment_id,
            [
                {"start": 1.2, "end": 2.5, "text": "A: Watch out!", "cut": True},
                {"start": 3.0, "end": 4.6, "text": "B: He has a *red* now?", "keep": True, "cut": False},
            ],
        )
        lines = (await workspace(reeling, moment_id)).one(data_reel_lines=True).find(data_reel_line=True)
        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert response.json() == {"moment_id": moment_id, "lines": 2, "kept": 1, "cut": 1}
        assert ["checked" in line.one(data_reel_cut_line=True).attrs for line in lines] == [True, False]
        assert ["checked" in line.one(data_reel_keep_line=True).attrs for line in lines] == [False, True]
        assert payload["cut"] == [{"start": 1.2, "end": 2.5, "text": "A: Watch out!"}]
        assert payload["keep"] == [{"start": 3.0, "end": 4.6, "text": "B: He has a *red* now?"}]

    async def test_a_line_both_kept_and_cut_out_is_refused_saying_which(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await save(
            reeling,
            moment_id,
            [
                {"start": 1.2, "end": 2.5, "text": "watch out"},
                {"start": 3.0, "end": 4.6, "text": "a red now?", "keep": True, "cut": True},
            ],
        )

        assert response.status_code == 400
        assert "Line 2" in response.json()["detail"]
        assert reel_of(moment_id)["cut_lines"] is None

    async def test_marks_go_with_a_transcript_a_new_storyboard_replaces(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await save(reeling, moment_id, [{"start": 1.2, "end": 2.5, "text": "watch out", "cut": True}])
        set_reel(moment_id, transcript_edited=0)

        await storyboard_back(reeling, github, moment_id)
        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert reel_of(moment_id)["cut_lines"] is None
        assert payload["cut"] == []


class TestAVariantOfOnesOwn:
    async def test_it_goes_up_on_the_recommended_variant_and_is_offered_again(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(
            reeling, github, moment_id, "", "1", variant_own="  Only the second fall,\nthe webcam alone. "
        )

        assert (payload["variant"], payload["own"]) == ("B", "Only the second fall,\nthe webcam alone.")
        reel = reel_of(moment_id)
        assert (reel["variant"], reel["variant_own"]) == ("B", "Only the second fall,\nthe webcam alone.")
        github.answer_reel(reels.tag(moment_id), b"the reel")
        await reeling.main._look_at_reels()
        opened = await workspace(reeling, moment_id)
        assert opened.one(data_reel_own_variant=True).text == "Only the second fall, the webcam alone."
        assert all(variant.attrs["aria-pressed"] == "false" for variant in opened.find(data_reel_variant=True))
        assert "Your own variant, hook: NEARLY RAN / HIM OVER?" in opened.text

    async def test_a_chosen_variant_wins_over_words_of_ones_own(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(reeling, github, moment_id, "A", "1", variant_own="Something else.")

        assert (payload["variant"], payload["own"]) == ("A", "")
        assert reel_of(moment_id)["variant_own"] is None
        opened = await workspace(reeling, moment_id)
        assert opened.one(data_reel_variant="A").attrs["aria-pressed"] == "true"

    async def test_its_feedback_says_it_was_ones_own(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await rendered(reeling, github, moment_id, "", "1", variant_own="Only the second fall.")
        github.answer_reel(reels.tag(moment_id), b"the reel")
        await reeling.main._look_at_reels()

        response = await reeling.client.post(f"/moments/{moment_id}/reel/feedback", json={"feedback": "Good."})

        assert response.status_code == 200, response.text
        sent = json.loads(github.asset(reels.tag(moment_id), reels.FEEDBACK))
        assert (sent["variant"], sent["variant_own"]) == ("B", "Only the second fall.")

    @pytest.mark.parametrize(
        ("own", "said"),
        [
            ("", "Choose a variant, or describe your own."),
            ("   ", "Choose a variant, or describe your own."),
            ("x" * (reels.COMMENT_MOST_CHARACTERS + 1), "at most"),
            (["no"], "Expected the comment as words."),
        ],
    )
    async def test_none_or_one_that_cannot_go_along_is_refused(self, reeling, github, own, said):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await render(reeling, moment_id, "", "1", variant_own=own)

        assert response.status_code == 400
        assert said in response.json()["detail"]
        assert [event for event, _ in github.dispatches] == ["storyboard"]


class TestRendering:
    async def test_the_chosen_variant_and_hook_start_the_render_and_the_reel_comes_back(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(reeling, github, moment_id, "a", "2")

        tag = reels.tag(moment_id)
        assert payload == {
            "tag": tag,
            "variant": "A",
            "hook": "WAS IT *GREEN?*",
            "subtitles": "auto",
            "comment": "",
            "own": "",
            "keep": [],
            "cut": [],
        }
        assert reel_of(moment_id)["state"] == reels.RENDERING

        github.answer_reel(tag, b"the reel")
        await reeling.main._look_at_reels()

        reel = reel_of(moment_id)
        assert reel["state"] == reels.DONE
        assert (recorder.CLIPS_DIR / reel["reel_path"]).read_bytes() == b"the reel"
        opened = await workspace(reeling, moment_id)
        assert opened.one("video", class_="reel-video").attrs["src"].startswith(f"/clips/reels/moment_{moment_id}")
        assert "Variant A, hook: WAS IT GREEN?" in opened.text
        assert opened.one(data_reel_variant="A").attrs["aria-pressed"] == "true"
        assert opened.one(data_reel_hook="2").attrs["aria-pressed"] == "true"
        assert opened.attrs["data-row-words"] == "Ready"

    async def test_the_transcript_goes_up_as_the_subtitles_it_is_rendered_with(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)
        put_right = [{"start": 1.2, "end": 2.5, "text": "A: Watch OUT!"}]
        await save(reeling, moment_id, put_right)

        payload = await rendered(reeling, github, moment_id, "B", "1", subtitles="on")

        assert payload["subtitles"] == "on"
        assert lines_of(github.asset(reels.tag(moment_id), reels.SUBTITLES)) == [
            subtitles.Line(1.2, 2.5, "A: Watch OUT!")
        ]
        assert reel_of(moment_id)["subtitles"] == "on"

    async def test_subtitles_switched_off_are_said_so_and_with_them_too_once_it_is_back(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(reeling, github, moment_id, "B", "1", subtitles="off")
        github.answer_reel(reels.tag(moment_id), b"the reel")
        await reeling.main._look_at_reels()

        assert payload["subtitles"] == "off"
        opened = await workspace(reeling, moment_id)
        assert "without subtitles" in opened.one(data_reel_region="result").text
        assert opened.one(data_reel_subtitles=True).one(data_value="off").attrs["aria-pressed"] == "true"

    async def test_an_empty_transcript_has_no_subtitles_made_of_it(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)
        await save(reeling, moment_id, [])

        refused = await render(reeling, moment_id, "B", "1", subtitles="on")
        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert refused.status_code == 400
        assert "nothing to subtitle" in refused.json()["detail"]
        assert payload["subtitles"] == "off"
        assert github.asset(reels.tag(moment_id), reels.SUBTITLES) is None

    async def test_unknown_subtitles_are_refused(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await render(reeling, moment_id, "A", "1", subtitles="maybe")

        assert response.status_code == 400

    async def test_the_reel_is_sent_with_its_note(self, reeling, github, told):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await rendered(reeling, github, moment_id, "B", "1")
        told.sent.clear()

        github.answer_reel(reels.tag(moment_id), b"the reel")
        await reeling.main._look_at_reels()
        await async_wait_until(lambda: told.sent, "the note")

        assert told.sent[0]["method"] == "sendVideo"
        assert told.sent[0]["video"][1] == b"the reel"
        assert "Variant B, hook: NEARLY RAN / HIM OVER?" in told.sent[0]["shown"]

    async def test_words_of_ones_own_are_the_hook_and_are_offered_again(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(reeling, github, moment_id, "B", "MY OWN|*HOOK*")

        assert payload["hook"] == "MY OWN|*HOOK*"
        opened = await workspace(reeling, moment_id)
        assert opened.one(data_reel_own=True).attrs["value"] == "MY OWN|*HOOK*"
        assert all(hook.attrs["aria-pressed"] == "false" for hook in opened.find(data_reel_hook=True))

    async def test_emoji_shortcuts_go_up_as_typed_and_the_page_shows_them_as_emoji(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(reeling, github, moment_id, "B", "MY *OWN* {rofl}{nope}")

        assert payload["hook"] == "MY *OWN* {rofl}{nope}"
        opened = await workspace(reeling, moment_id)
        shown = opened.one(data_reel_own_shown=True)
        assert shown.children[0] == "MY OWN 🤣"
        assert shown.one("s", class_="reel-unknown").text == "{nope}"

    async def test_the_storyboards_hooks_show_their_emoji_and_the_page_offers_them_all(self, reeling, github):
        moment_id = clipped_moment()
        hooks = [{"id": 1, "type": "T1", "text": "NEARLY RAN|*HIM OVER?* {ROFL}{huh}"}]

        await storyboard_back(reeling, github, moment_id, storyboard={**STORYBOARD, "hooks": hooks})

        opened = await workspace(reeling, moment_id)
        hook = opened.one(data_reel_hook=True).one("b")
        assert hook.children[0] == "NEARLY RAN / HIM OVER? 🤣"
        assert hook.one("s", class_="reel-unknown").text == "{huh}"
        table = opened.one(data_reel_emoji_codes=True)
        assert json.loads(table.attrs["data-reel-emoji-codes"]) == reels.EMOJI_CODES
        assert [pick.attrs["data-reel-emoji"] for pick in table.find(data_reel_emoji=True)] == [
            pick["codes"][0] for pick in reels.EMOJI_PICKS
        ]

    async def test_rendering_again_takes_the_last_reel_out_of_the_release_first(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        github.answer_reel(reels.tag(moment_id), b"an older reel")

        await rendered(reeling, github, moment_id, "B", "1")

        assert github.asset(reels.tag(moment_id), "reel.mp4") is None

    @pytest.mark.parametrize(
        ("variant", "hook", "said"),
        [
            ("Z", "1", "no such variant"),
            ("A", "", "Choose a hook"),
            ("A", "X" * (reels.HOOK_MOST_CHARACTERS + 1), "at most"),
        ],
    )
    async def test_a_choice_the_storyboard_does_not_allow_is_refused(self, reeling, github, variant, hook, said):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await render(reeling, moment_id, variant, hook)

        assert response.status_code == 400
        assert said in response.json()["detail"]
        assert len(github.dispatches) == 1

    async def test_nothing_is_rendered_before_a_storyboard_is_there(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        response = await render(reeling, moment_id, "A", "1")

        assert response.status_code == 409


class TestAReelFromBeforeTranscripts:
    """One whose storyboard came back before a reel's transcript was kept."""

    @pytest.fixture
    def old_reel(self, reeling, github):
        moment_id = clipped_moment()
        tag = reels.tag(moment_id)
        github.releases[tag] = FakeRelease(id=1, tag=tag, name="")
        github.put(tag, reels.SUBTITLES, srt_bytes(PROPOSED))
        set_reel(moment_id, state=reels.CHOOSING, asked_at=T0.isoformat(), storyboard=json.dumps(STORYBOARD))
        return moment_id

    async def test_its_page_says_a_new_storyboard_brings_one(self, reeling, github, old_reel):
        opened = await workspace(reeling, old_reel)

        assert opened.find(data_reel_transcript=True) == []
        assert "before transcripts were kept" in opened.text

    async def test_it_is_rendered_with_the_subtitles_its_release_has(self, reeling, github, old_reel):
        payload = await rendered(reeling, github, old_reel, "B", "1")

        assert payload["subtitles"] == "auto"
        assert lines_of(github.asset(reels.tag(old_reel), reels.SUBTITLES)) == PROPOSED

    async def test_a_new_storyboard_has_its_clip_heard_anew(self, reeling, github, old_reel):
        github.put(reels.tag(old_reel), "clip.mp4", CLIP_BYTES)

        await asked_for_storyboard(reeling, github, old_reel)

        assert github.asset(reels.tag(old_reel), reels.SUBTITLES) is None
        assert reel_of(old_reel)["clip_stamp"] == reels.stamp(clip_file())


class TestAskingAnew:
    async def test_the_same_file_keeps_its_transcript_and_is_not_sent_again(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)
        tag = reels.tag(moment_id)
        clip_id = github.releases[tag].assets["clip.mp4"][0]

        await asked_for_storyboard(reeling, github, moment_id)

        assert github.releases[tag].assets["clip.mp4"][0] == clip_id
        # What was heard stays for the workflow not to hear it again; the
        # subtitles Claude proposed go, for it to propose them anew.
        assert lines_of(github.asset(tag, reels.HEARD)) == HEARD
        assert github.asset(tag, reels.SUBTITLES) is None
        assert [github.asset(tag, name) for name in ("storyboard.json", "contact.jpg")] == [None, None]
        assert json.loads(github.asset(tag, "context.json"))["transcript_corrected"] is False
        assert transcript_of(moment_id) == PROPOSED

    async def test_a_transcript_put_right_goes_up_for_claude_and_stays_when_the_storyboard_is_back(
        self, reeling, github
    ):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)
        await save(reeling, moment_id, [{"start": 3.0, "end": 4.6, "text": "B: He has a red now?"}])
        put_right = transcript_of(moment_id)

        await asked_for_storyboard(reeling, github, moment_id)

        tag = reels.tag(moment_id)
        assert lines_of(github.asset(tag, reels.SUBTITLES)) == put_right
        assert json.loads(github.asset(tag, "context.json"))["transcript_corrected"] is True
        # Whatever subtitles come back, the transcript put right is kept.
        answer(github, moment_id, proposed=PROPOSED)
        await reeling.main._look_at_reels()
        assert reel_of(moment_id)["state"] == reels.CHOOSING
        assert transcript_of(moment_id) == put_right

    async def test_a_file_made_longer_is_sent_again_and_heard_anew(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, proposed=PROPOSED)
        await save(reeling, moment_id, [{"start": 3.0, "end": 4.6, "text": "put right"}])
        clip_file().write_bytes(CLIP_BYTES + b" and footage added after it")

        stale = await workspace(reeling, moment_id)
        await asked_for_storyboard(reeling, github, moment_id)

        assert "Footage was added" in stale.text
        tag = reels.tag(moment_id)
        assert github.asset(tag, "clip.mp4") == CLIP_BYTES + b" and footage added after it"
        assert [github.asset(tag, name) for name in reels.TRANSCRIPT_FILES] == [None, None, None]
        reel = reel_of(moment_id)
        assert (reel["transcript"], reel["heard"], reel["transcript_edited"]) == (None, None, 0)

    async def test_a_new_storyboard_takes_the_last_answers_away_here_and_on_github(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await rendered(reeling, github, moment_id, "B", "1")
        github.answer_reel(reels.tag(moment_id), b"the reel")
        await reeling.main._look_at_reels()
        kept = [recorder.CLIPS_DIR / reel_of(moment_id)[name] for name in ("contact_path", "reel_path")]

        await asked_for_storyboard(reeling, github, moment_id)

        tag = reels.tag(moment_id)
        assert [github.asset(tag, name) for name in ("storyboard.json", "contact.jpg", "reel.mp4")] == [None] * 3
        assert not any(path.exists() for path in kept)
        assert reel_of(moment_id)["storyboard"] is None


# A stretch of a moment's file: 8 to 20 seconds of it.
STRETCH = {"start": 8, "end": 20}


@pytest.mark.ffmpeg
class TestAStretchOfTheFile:
    """Only a stretch of the moment's file sent: here 8 to 20 seconds of it,
    which begins three seconds into the clip (BEFORE is 5) - a frame a cut
    can begin on every second."""

    @pytest.fixture
    def moment_id(self, reeling) -> int:
        require_ffmpeg()
        moment_id = clipped_moment()
        make_video(clip_file(), BEFORE + CLIP + AFTER, keyframe_every=1.0)
        return moment_id

    async def asked(self, service, moment_id: int, stretch: dict | None = None):
        return await service.client.post(f"/moments/{moment_id}/reel", json=stretch or STRETCH)

    async def sent(self, service, moment_id: int, stretch: dict | None = None) -> None:
        response = await self.asked(service, moment_id, stretch)
        assert response.status_code == 200, response.text
        await async_wait_until(lambda: reel_of(moment_id)["state"] != reels.SENDING, "the clip to be sent")
        assert reel_of(moment_id)["state"] == reels.STORYBOARDING, reel_of(moment_id)["error"]

    async def test_only_the_stretch_goes_up_with_its_chat_and_clip_timed_to_it(
        self, reeling, github, moment_id, tmp_path
    ):
        add_chat(CHANNEL, "early", "what", T0 - timedelta(seconds=2))
        add_chat(CHANNEL, "fan", "xdddd", T0 + timedelta(seconds=3))
        add_chat(CHANNEL, "other", "xdddd", T0 + timedelta(seconds=3.5))
        add_chat(CHANNEL, "late", "ok", T0 + timedelta(seconds=16))

        await self.sent(reeling, moment_id)

        reel = reel_of(moment_id)
        assert (reel["part_start"], reel["part_end"]) == (pytest.approx(8, abs=0.05), pytest.approx(20, abs=0.15))
        tag = reels.tag(moment_id)
        sent = tmp_path / "sent.mp4"
        sent.write_bytes(github.asset(tag, "clip.mp4"))
        assert probe(sent)["duration"] == pytest.approx(12, abs=0.15)
        context = json.loads(github.asset(tag, "context.json"))
        # The clip ran on past the stretch's end; its start was before it.
        assert context["clip"] == {"start": 0.0, "end": pytest.approx(12, abs=0.15)}
        # Chat at the second of the stretch it belongs to; what was said
        # before it or after it does not go.
        assert [(line["t"], line["user"]) for line in context["chat"]] == [
            (pytest.approx(0, abs=0.05), "fan"),
            (pytest.approx(0.5, abs=0.05), "other"),
        ]

    async def test_what_comes_back_is_timed_to_the_file_and_goes_up_timed_to_the_stretch(
        self, reeling, github, moment_id
    ):
        await self.sent(reeling, moment_id)
        answer(github, moment_id, proposed=PROPOSED)
        await reeling.main._look_at_reels()
        begins = reel_of(moment_id)["part_start"]

        # The page has it in seconds of the moment's file, as it plays it.
        assert transcript_of(moment_id) == reels.moved(PROPOSED, begins)
        assert subtitles.read(reel_of(moment_id)["heard"]) == reels.moved(HEARD, begins)
        # A line of the file outside the stretch is kept, but not rendered.
        put_right = [
            {"start": 1.0, "end": 2.0, "text": "before the stretch"},
            {"start": begins + 1.2, "end": begins + 2.5, "text": "A: Watch OUT!"},
        ]
        assert (await save(reeling, moment_id, put_right)).status_code == 200
        await rendered(reeling, github, moment_id, "B", "1", subtitles="on")

        assert lines_of(github.asset(reels.tag(moment_id), reels.SUBTITLES)) == [
            subtitles.Line(1.2, 2.5, "A: Watch OUT!")
        ]

    async def test_a_transcript_with_nothing_said_in_the_stretch_renders_without_subtitles(
        self, reeling, github, moment_id
    ):
        await self.sent(reeling, moment_id)
        answer(github, moment_id)
        await reeling.main._look_at_reels()
        await save(reeling, moment_id, [{"start": 1.0, "end": 2.0, "text": "before the stretch"}])

        refused = await render(reeling, moment_id, "B", "1", subtitles="on")
        payload = await rendered(reeling, github, moment_id, "B", "1")

        assert (refused.status_code, refused.json()["detail"]) == (
            400,
            "The transcript is empty: there is nothing to subtitle.",
        )
        assert payload["subtitles"] == "off"

    async def test_the_same_stretch_keeps_its_transcript_and_another_is_heard_anew(self, reeling, github, moment_id):
        await self.sent(reeling, moment_id)
        answer(github, moment_id, proposed=PROPOSED)
        await reeling.main._look_at_reels()
        kept = transcript_of(moment_id)

        await self.sent(reeling, moment_id)
        assert transcript_of(moment_id) == kept
        answer(github, moment_id)
        await reeling.main._look_at_reels()
        await self.sent(reeling, moment_id, {"start": 6, "end": 20})

        assert (reel_of(moment_id)["transcript"], reel_of(moment_id)["heard"]) == (None, None)
        assert [github.asset(reels.tag(moment_id), name) for name in reels.TRANSCRIPT_FILES] == [None, None, None]

    async def test_the_whole_file_asked_for_is_the_file_itself(self, reeling, github, moment_id):
        await self.sent(reeling, moment_id)
        answer(github, moment_id)
        await reeling.main._look_at_reels()

        await self.sent(reeling, moment_id, {"start": 0, "end": BEFORE + CLIP + AFTER})

        reel = reel_of(moment_id)
        assert (reel["part_start"], reel["part_end"]) == (None, None)
        assert github.asset(reels.tag(moment_id), "clip.mp4") == clip_file().read_bytes()
        assert not reeling.main._reel_file(moment_id, "part.mp4").exists()

    @pytest.mark.parametrize(
        ("stretch", "said"),
        [
            ({"start": 8}, "Expected the start and end of the stretch, in seconds of the clip's file."),
            ({"start": "8", "end": 20}, "Expected the start and end of the stretch, in seconds of the clip's file."),
            ({"start": -1, "end": 20}, "The stretch has to lie within the clip's file, 0:00.0 to 0:35.0."),
            ({"start": 8, "end": 40}, "The stretch has to lie within the clip's file, 0:00.0 to 0:35.0."),
            ({"start": 8, "end": 8.5}, "The stretch has to end at least a second after it starts."),
        ],
    )
    async def test_a_stretch_that_cannot_be_cut_is_refused(self, reeling, github, moment_id, stretch, said):
        response = await self.asked(reeling, moment_id, stretch)

        assert (response.status_code, response.json()["detail"]) == (400, said)
        assert reel_of(moment_id) is None
        assert github.dispatches == []

    async def test_the_page_offers_the_whole_file_and_then_the_stretch_sent(self, reeling, github, moment_id):
        offered = (await workspace(reeling, moment_id)).one("span", data_stretch="reel")
        await self.sent(reeling, moment_id)
        again = (await workspace(reeling, moment_id)).one("span", data_stretch="reel")

        # Seconds of the file; shown, and filled in, by the script.
        assert "hidden" in offered.attrs
        assert [offered.attrs[name] for name in ("data-start", "data-end", "data-clip", "data-highest")] == [
            "0",
            "35",
            "35",
            "35",
        ]
        assert (offered.attrs["data-reset-start"], offered.attrs["data-reset-end"]) == ("0", "35")
        assert [button.text for button in offered.find("button")] == ["Start here", "End here", "Whole file"]
        assert float(again.attrs["data-start"]) == pytest.approx(8, abs=0.05)
        assert float(again.attrs["data-end"]) == pytest.approx(20, abs=0.15)
        # Nothing is changed while the storyboard is made.
        assert all("disabled" in control.attrs for control in again.find("input") + again.find("button"))

    async def test_the_stretch_goes_when_the_moment_does(self, reeling, github, moment_id):
        await self.sent(reeling, moment_id)
        part = reeling.main._reel_file(moment_id, "part.mp4")
        assert part.exists()

        response = await reeling.client.post(f"/moments/{moment_id}/delete")

        assert response.status_code == 200
        assert not part.exists()


async def reel_back(service, github: FakeGitHub, moment_id: int, note: str | None = None, **asked) -> None:
    """Renders the reel with variant B and hook 1 and has it come back - with
    what Claude made of the render's comment, where `note` is given."""
    await rendered(service, github, moment_id, "B", "1", **asked)
    tag = reels.tag(moment_id)
    if note is not None:
        github.put(tag, reels.RENDERED, json.dumps({"variant": "B", "note": note}).encode())
    github.answer_reel(tag, b"the reel")
    await service.main._look_at_reels()
    assert reel_of(moment_id)["state"] == reels.DONE, reel_of(moment_id)["error"]


async def give_feedback(service, moment_id: int, words: object):
    return await service.client.post(f"/moments/{moment_id}/reel/feedback", json={"feedback": words})


class TestComments:
    async def test_a_comment_for_the_storyboard_goes_up_in_its_context_and_is_offered_again(self, reeling, github):
        moment_id = clipped_moment()

        await asked_for_storyboard(
            reeling, github, moment_id, comment="  The joke is the second fall.  \r\n\r\nKeep it short.\n "
        )

        said = "The joke is the second fall.\n\nKeep it short."
        assert json.loads(github.asset(reels.tag(moment_id), "context.json"))["comment"] == said
        assert reel_of(moment_id)["storyboard_comment"] == said
        field = (await workspace(reeling, moment_id)).one(data_reel_comment="storyboard")
        assert field.text == "The joke is the second fall. Keep it short."
        # Not to be changed while the storyboard is made.
        assert "disabled" in field.attrs

    @pytest.mark.parametrize(
        ("comment", "said"),
        [("x" * (reels.COMMENT_MOST_CHARACTERS + 1), "at most"), (5, "Expected the comment as words")],
    )
    async def test_a_comment_that_cannot_go_along_is_refused_and_nothing_is_sent(self, reeling, github, comment, said):
        moment_id = clipped_moment()

        response = await ask(reeling, moment_id, comment=comment)

        assert response.status_code == 400
        assert said in response.json()["detail"]
        assert github.dispatches == []
        assert reel_of(moment_id) is None

    async def test_a_comment_for_the_render_goes_with_it_and_what_claude_made_of_it_comes_back(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        await reel_back(
            reeling, github, moment_id, note="Ended it at 16.2 s, after the laugh.", comment=" End sooner. "
        )

        assert github.dispatches[-1][1]["comment"] == "End sooner."
        reel = reel_of(moment_id)
        assert (reel["render_comment"], reel["render_note"]) == ("End sooner.", "Ended it at 16.2 s, after the laugh.")
        opened = await workspace(reeling, moment_id)
        assert opened.one(data_reel_comment="render").text == "End sooner."
        assert "Ended it at 16.2 s, after the laugh." in opened.one(data_reel_render_note=True).text

    async def test_a_reel_whose_render_says_nothing_readable_of_it_is_taken_all_the_same(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await rendered(reeling, github, moment_id, "B", "1", comment="End sooner.")
        tag = reels.tag(moment_id)
        github.put(tag, reels.RENDERED, b"not json")
        github.answer_reel(tag, b"the reel")

        await reeling.main._look_at_reels()

        assert reel_of(moment_id)["state"] == reels.DONE
        assert reel_of(moment_id)["render_note"] is None
        assert (await workspace(reeling, moment_id)).find(data_reel_render_note=True) == []

    async def test_a_comment_for_the_render_that_is_too_long_is_refused(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await render(reeling, moment_id, "B", "1", comment="x" * (reels.COMMENT_MOST_CHARACTERS + 1))

        assert response.status_code == 400
        assert reel_of(moment_id)["state"] == reels.CHOOSING

    async def test_a_new_storyboard_starts_the_reels_comments_and_feedback_afresh(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id, comment="The first fall.")
        await reel_back(reeling, github, moment_id, note="Done.", comment="End sooner.")
        await give_feedback(reeling, moment_id, "Too long.")

        await asked_for_storyboard(reeling, github, moment_id, comment="The second fall.")

        reel = reel_of(moment_id)
        assert reel["storyboard_comment"] == "The second fall."
        assert [reel[name] for name in ("render_comment", "render_note", "feedback")] == [None, None, None]
        tag = reels.tag(moment_id)
        assert [github.asset(tag, name) for name in (reels.RENDERED, reels.FEEDBACK)] == [None, None]


class TestFeedback:
    async def test_it_goes_up_with_what_the_reel_was_made_of_and_the_feedback_workflow_starts(self, reeling, github):
        moment_id = clipped_moment(title="Nearly ran him over")
        await storyboard_back(reeling, github, moment_id, comment="The second fall.")
        await reel_back(reeling, github, moment_id, note="Ended it sooner.", comment="End sooner.", subtitles="on")
        rendered_at = reel_of(moment_id)["asked_at"]

        response = await give_feedback(reeling, moment_id, " The hook gives it away.\nGreat ending. ")

        assert response.status_code == 200, response.text
        tag = reels.tag(moment_id)
        assert github.dispatches[-1] == ("feedback", {"tag": tag})
        assert json.loads(github.asset(tag, reels.FEEDBACK)) == {
            "tag": tag,
            "moment_id": moment_id,
            "streamer": CHANNEL,
            "platform": "kick",
            "credit": f"kick.com/{CHANNEL}",
            "title": "Nearly ran him over",
            "summary": STORYBOARD["summary"],
            "storyboard_comment": "The second fall.",
            "variant": "B",
            "variant_title": "Teaser first",
            "variant_own": "",
            "hook": "NEARLY RAN|*HIM OVER?*",
            "subtitles": "on",
            "render_comment": "End sooner.",
            "render_note": "Ended it sooner.",
            "rendered_at": rendered_at,
            "feedback": "The hook gives it away.\nGreat ending.",
        }
        reel = reel_of(moment_id)
        assert (reel["feedback"], reel["feedback_sent"]) == ("The hook gives it away.\nGreat ending.", 1)
        box = (await workspace(reeling, moment_id)).one(data_reel_feedback_box=True)
        assert box.one(data_reel_comment="feedback").text == "The hook gives it away. Great ending."
        assert box.one(data_status=True).text == "Sent."
        assert box.one(data_reel_feedback=True).text == "Send again"

    async def test_it_is_offered_only_once_the_reel_is_back(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        choosing = await workspace(reeling, moment_id)
        refused = await give_feedback(reeling, moment_id, "Too long.")
        await rendered(reeling, github, moment_id, "B", "1")
        rendering = await give_feedback(reeling, moment_id, "Too long.")

        assert choosing.find(data_reel_feedback_box=True) == []
        assert (refused.status_code, rendering.status_code) == (409, 409)
        assert "no reel" in refused.json()["detail"]
        assert all(event != "feedback" for event, _ in github.dispatches)

    @pytest.mark.parametrize(("words", "said"), [("   ", "Write what you think"), (None, "Write what you think")])
    async def test_none_is_refused(self, reeling, github, words, said):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await reel_back(reeling, github, moment_id)

        response = await give_feedback(reeling, moment_id, words)

        assert response.status_code == 400
        assert said in response.json()["detail"]

    async def test_what_github_does_not_take_is_kept_said_so_and_sent_again(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await reel_back(reeling, github, moment_id)
        github.failures["POST /dispatches"] = 500

        refused = await give_feedback(reeling, moment_id, "Too long.")

        assert refused.status_code == 502
        assert "Kept here, but not sent: GitHub refused" in refused.json()["detail"]
        assert (reel_of(moment_id)["feedback"], reel_of(moment_id)["feedback_sent"]) == ("Too long.", 0)
        box = (await workspace(reeling, moment_id)).one(data_reel_feedback_box=True)
        assert "not sent to GitHub yet" in box.one(data_status=True).text

        again = await give_feedback(reeling, moment_id, "Too long.")

        assert again.status_code == 200
        assert reel_of(moment_id)["feedback_sent"] == 1

    async def test_sent_again_it_takes_the_place_of_what_was_sent(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await reel_back(reeling, github, moment_id)

        await give_feedback(reeling, moment_id, "Too long.")
        await give_feedback(reeling, moment_id, "Too long, but the hook is great.")

        tag = reels.tag(moment_id)
        assert json.loads(github.asset(tag, reels.FEEDBACK))["feedback"] == "Too long, but the hook is great."
        assert [event for event, _ in github.dispatches].count("feedback") == 2

    async def test_a_new_render_is_another_reel_whose_feedback_starts_afresh(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await reel_back(reeling, github, moment_id, note="Nothing to change.")
        await give_feedback(reeling, moment_id, "Too long.")

        await rendered(reeling, github, moment_id, "A", "2", comment="Shorter.")

        reel = reel_of(moment_id)
        assert (reel["feedback"], reel["feedback_sent"], reel["render_note"]) == (None, 0, None)
        tag = reels.tag(moment_id)
        assert [github.asset(tag, name) for name in (reels.REEL, reels.RENDERED, reels.FEEDBACK)] == [None] * 3
        assert (await workspace(reeling, moment_id)).find(data_reel_feedback_box=True) == []


class TestRemovingTheMoment:
    async def test_its_reel_goes_with_it(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        contact = recorder.CLIPS_DIR / reel_of(moment_id)["contact_path"]

        response = await reeling.client.post(f"/moments/{moment_id}/delete")

        assert response.status_code == 200
        assert reel_of(moment_id) is None
        assert not contact.exists()


class TestQuestions:
    ANSWERS: ClassVar[dict] = {
        "cut": "raw",
        "layout": "cam_game",
        "subtitles": "no",
        "who": "Pepa",
        "peak": BEFORE + 14.25,
        "keep": [{"start": BEFORE + 1, "end": BEFORE + 4.5, "note": "says he won't"}],
    }

    async def test_the_answers_go_up_in_the_context_and_are_offered_again(self, reeling, github):
        moment_id = clipped_moment()

        await asked_for_storyboard(reeling, github, moment_id, answers=self.ANSWERS)

        context = json.loads(github.asset(reels.tag(moment_id), "context.json"))
        assert context["answers"] == self.ANSWERS
        opened = await workspace(reeling, moment_id)
        box = opened.one(data_reel_questions=True)
        assert "open" in box.attrs
        pressed = {
            group.attrs["data-reel-answer"]: group.one(aria_pressed="true").attrs["data-value"]
            for group in box.find(data_reel_answer=True)
        }
        assert pressed == {
            "cut": "raw", "layout": "cam_game", "kind": "", "length": "", "hook_style": "", "subtitles": "no",
            "teaser": "", "sfx": "", "emoji": "",
        }  # fmt: skip
        assert box.one(data_reel_who=True).attrs["value"] == "Pepa"
        assert box.one(data_reel_peak=True).attrs["value"] == "0:19.2"
        part = box.one(data_reel_keeps=True).one(data_reel_keep=True)
        assert part.one(data_reel_keep_start=True).attrs["value"] == "0:06.0"
        assert part.one(data_reel_keep_end=True).attrs["value"] == "0:09.5"
        assert part.one(data_reel_keep_note=True).attrs["value"] == "says he won't"
        # Not to be sent again while the storyboard is made.
        assert "disabled" in opened.one(data_reel_ask=True).attrs

    async def test_the_full_height_reaction_layout_can_be_chosen(self, reeling, github):
        moment_id = clipped_moment()

        await asked_for_storyboard(reeling, github, moment_id, answers={"layout": "react"})

        context = json.loads(github.asset(reels.tag(moment_id), "context.json"))
        assert context["answers"] == {"layout": "react"}
        box = (await workspace(reeling, moment_id)).one(data_reel_questions=True)
        layout = box.one(data_reel_answer="layout")
        assert layout.one(aria_pressed="true").attrs["data-value"] == "react"

    async def test_unasked_the_questions_are_folded_away_and_left_to_claude(self, reeling, github):
        moment_id = clipped_moment()

        box = (await workspace(reeling, moment_id)).one(data_reel_questions=True)

        assert "open" not in box.attrs
        assert all(
            group.one(aria_pressed="true").attrs["data-value"] == "" for group in box.find(data_reel_answer=True)
        )
        assert all(field.attrs["value"] == "" for field in box.find(data_reel_answer_own=True))

    async def test_an_answer_of_ones_own_goes_up_and_is_offered_again_in_its_field(self, reeling, github):
        moment_id = clipped_moment()

        await asked_for_storyboard(reeling, github, moment_id, answers={"length": "as long as the story needs"})

        context = json.loads(github.asset(reels.tag(moment_id), "context.json"))
        assert context["answers"] == {"length": "as long as the story needs"}
        group = (await workspace(reeling, moment_id)).one(data_reel_answer="length")
        assert group.find(aria_pressed="true") == []
        assert group.one(data_reel_answer_own=True).attrs["value"] == "as long as the story needs"

    async def test_an_answer_that_cannot_go_along_is_refused_and_nothing_is_sent(self, reeling, github):
        moment_id = clipped_moment()

        response = await ask(reeling, moment_id, answers={"keep": [{"start": 3, "end": 2}]})

        assert response.status_code == 400
        assert "end after it starts" in response.json()["detail"]
        assert github.dispatches == []
        assert reel_of(moment_id) is None

    async def test_asked_to_render_straight_away_the_recommended_reel_is_rendered_once_the_storyboard_is_back(
        self, reeling, github
    ):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id, answers={"render_now": True})
        hooks = [STORYBOARD["hooks"][0], {**STORYBOARD["hooks"][1], "recommended": True}]
        answer(github, moment_id, storyboard={**STORYBOARD, "hooks": hooks})

        await reeling.main._look_at_reels()

        await async_wait_until(lambda: len(github.dispatches) == 2, "the render to be asked for")
        tag = reels.tag(moment_id)
        assert github.dispatches[-1] == (
            "render",
            {
                "tag": tag,
                "variant": "B",
                "hook": "WAS IT *GREEN?*",
                "subtitles": "auto",
                "comment": "",
                "own": "",
                "keep": [],
                "cut": [],
            },
        )
        reel = reel_of(moment_id)
        assert (reel["state"], reel["variant"], reel["hook"]) == (reels.RENDERING, "B", "WAS IT *GREEN?*")

    async def test_otherwise_the_storyboard_waits_for_a_choice(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        assert reel_of(moment_id)["state"] == reels.CHOOSING
        assert [event for event, _ in github.dispatches] == ["storyboard"]

    async def test_a_new_storyboard_goes_by_its_own_answers(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id, answers={"cut": "raw"})
        answer(github, moment_id)
        await reeling.main._look_at_reels()

        await asked_for_storyboard(reeling, github, moment_id)

        assert reel_of(moment_id)["storyboard_answers"] is None
        assert json.loads(github.asset(reels.tag(moment_id), "context.json"))["answers"] == {}
