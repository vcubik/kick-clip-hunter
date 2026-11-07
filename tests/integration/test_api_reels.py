"""A moment's clip made into a reel, through the service (see reels.py): the
clip and what goes with it sent to a fake GitHub, the storyboard workflow
started, the storyboard and the transcript taken back and shown on the Reels
page, the transcript put right there, a variant and a hook chosen, the render
workflow started with the transcript as subtitles and the reel taken back -
and what happens when GitHub refuses, a workflow fails or nothing comes
back. What the workflows would put into the release the tests put there
themselves; the service's look at GitHub is called as its loop would."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import chat_trace, db, recorder, reels, subtitles
from kick_clip_hunter.config import load_settings
from tests.support.data import T0, add_chat, add_moment
from tests.support.github import FakeGitHub, FakeRelease
from tests.support.html import parse
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


async def ask(service, moment_id: int):
    return await service.client.post(f"/moments/{moment_id}/reel")


async def asked_for_storyboard(service, github: FakeGitHub, moment_id: int) -> None:
    response = await ask(service, moment_id)
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


async def storyboard_back(service, github: FakeGitHub, moment_id: int, **answered) -> None:
    await asked_for_storyboard(service, github, moment_id)
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

        assert response.json() == {"moment_id": moment_id, "lines": 2}
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


class TestRendering:
    async def test_the_chosen_variant_and_hook_start_the_render_and_the_reel_comes_back(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        payload = await rendered(reeling, github, moment_id, "a", "2")

        tag = reels.tag(moment_id)
        assert payload == {"tag": tag, "variant": "A", "hook": "WAS IT *GREEN?*", "subtitles": "auto"}
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


class TestRemovingTheMoment:
    async def test_its_reel_goes_with_it(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        contact = recorder.CLIPS_DIR / reel_of(moment_id)["contact_path"]

        response = await reeling.client.post(f"/moments/{moment_id}/delete")

        assert response.status_code == 200
        assert reel_of(moment_id) is None
        assert not contact.exists()
