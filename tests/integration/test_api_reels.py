"""A moment's clip made into a reel, through the service (see reels.py): the
clip and what goes with it sent to a fake GitHub, the storyboard workflow
started, the storyboard taken back and shown on the review page, a variant
and a hook chosen there, the render workflow started and the reel taken back
- and what happens when GitHub refuses, a workflow fails or nothing comes
back. What the workflows would put into the release the tests put there
themselves; the service's look at GitHub is called as its loop would."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import chat_trace, db, recorder, reels
from kick_clip_hunter.config import load_settings
from tests.support.data import T0, add_chat, add_moment
from tests.support.github import FakeGitHub
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
         "why": "The point first.", "render_args": ["--teaser", "14-16", "--seg", "1-17"]},
    ],
    "hooks": [
        {"id": 1, "type": "T1", "text": "NEARLY RAN|*HIM OVER?*"},
        {"id": 2, "type": "T4", "text": "WAS IT *GREEN?*"},
    ],
    "question": "Which variant and hook?",
}  # fmt: skip


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


def clipped_moment(**columns) -> int:
    """A moment whose file holds BEFORE seconds of footage, its CLIP-second
    clip and AFTER seconds more, chat taken to have seen the clip's first
    frame at T0."""
    path = f"{CHANNEL}/moment_1.mp4"
    file = recorder.CLIPS_DIR / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(CLIP_BYTES)
    return add_moment(
        CHANNEL,
        detected_at=T0 + timedelta(seconds=12),
        clip_path=path,
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


async def ask(service, moment_id: int):
    return await service.client.post(f"/moments/{moment_id}/reel")


async def asked_for_storyboard(service, github: FakeGitHub, moment_id: int) -> None:
    response = await ask(service, moment_id)
    assert response.status_code == 200, response.text
    await async_wait_until(lambda: reel_of(moment_id)["state"] != reels.SENDING, "the clip to be sent")
    assert reel_of(moment_id)["state"] == reels.STORYBOARDING, reel_of(moment_id)["error"]


async def storyboard_back(service, github: FakeGitHub, moment_id: int, storyboard=STORYBOARD) -> None:
    await asked_for_storyboard(service, github, moment_id)
    github.answer_storyboard(reels.tag(moment_id), storyboard, contact=b"frames")
    await service.main._look_at_reels()


async def render(service, moment_id: int, variant: str, hook: str):
    return await service.client.post(f"/moments/{moment_id}/reel/render", json={"variant": variant, "hook": hook})


async def reel_section(service, moment_id: int):
    response = await service.client.get(f"/dashboard/moments/{moment_id}/reel")
    assert response.status_code == 200, response.text
    return parse(response.text)


class TestNotSetUp:
    async def test_without_a_repository_the_page_offers_no_reel_and_none_can_be_asked_for(self, service):
        moment_id = clipped_moment()

        page = parse((await service.client.get(f"/dashboard/moments/{moment_id}")).text)
        response = await ask(service, moment_id)

        assert page.find(data_reel=True) == []
        assert response.status_code == 409
        assert "REELS_REPO" in response.json()["detail"]


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

    async def test_the_page_says_the_storyboard_is_being_made_and_offers_nothing_more_meanwhile(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        section = (await reel_section(reeling, moment_id)).one(data_reel=True)

        assert "data-busy" in section.attrs
        assert "being made" in section.one(data_status=True).text
        assert "disabled" in section.one(data_reel_ask=True).attrs

    async def test_asked_again_while_it_is_made_nothing_more_is_started(self, reeling, github):
        moment_id = clipped_moment()
        await asked_for_storyboard(reeling, github, moment_id)

        response = await ask(reeling, moment_id)

        assert response.json()["state"] == reels.STORYBOARDING
        assert len(github.dispatches) == 1

    async def test_a_moment_without_its_clip_file_has_no_reel_made(self, reeling, github):
        moment_id = clipped_moment()
        (recorder.CLIPS_DIR / f"{CHANNEL}/moment_1.mp4").unlink()

        response = await ask(reeling, moment_id)

        assert response.status_code == 409
        assert github.releases == {}

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
        section = await reel_section(reeling, moment_id)
        variants = section.find(data_reel_variant=True)
        assert [(v.attrs["data-reel-variant"], v.attrs["aria-pressed"]) for v in variants] == [
            ("A", "false"),
            ("B", "true"),
        ]
        hooks = section.find(data_reel_hook=True)
        assert [hook.one("b").text for hook in hooks] == ["NEARLY RAN / HIM OVER?", "WAS IT GREEN?"]
        assert hooks[0].attrs["aria-pressed"] == "true"
        assert section.one("img", class_="reel-contact").attrs["src"].startswith(f"/clips/reels/moment_{moment_id}")

    async def test_its_coming_is_told_of(self, reeling, github, told):
        moment_id = clipped_moment()

        await storyboard_back(reeling, github, moment_id)
        await async_wait_until(lambda: told.sent, "the note")

        shown = told.sent[0]["shown"]
        assert "Storyboard ready" in shown
        assert STORYBOARD["summary"] in shown
        assert told.sent[0]["links"] == [f"{DASHBOARD}/dashboard?moment={moment_id}"]

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


class TestRendering:
    async def test_the_chosen_variant_and_hook_start_the_render_and_the_reel_comes_back(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)

        response = await render(reeling, moment_id, "a", "2")
        await async_wait_until(lambda: len(github.dispatches) == 2, "the render to be asked for")

        assert response.status_code == 200, response.text
        tag = reels.tag(moment_id)
        assert github.dispatches[1] == ("render", {"tag": tag, "variant": "A", "hook": "WAS IT *GREEN?*"})
        assert reel_of(moment_id)["state"] == reels.RENDERING

        github.answer_reel(tag, b"the reel")
        await reeling.main._look_at_reels()

        reel = reel_of(moment_id)
        assert reel["state"] == reels.DONE
        assert (recorder.CLIPS_DIR / reel["reel_path"]).read_bytes() == b"the reel"
        section = await reel_section(reeling, moment_id)
        assert section.one("video", class_="reel-video").attrs["src"].startswith(f"/clips/reels/moment_{moment_id}")
        assert "Variant A, hook: WAS IT / GREEN?" not in section.text  # the slash is only for line breaks
        assert "Variant A, hook: WAS IT GREEN?" in section.text
        assert section.one(data_reel_variant="A").attrs["aria-pressed"] == "true"
        assert section.one(data_reel_hook="2").attrs["aria-pressed"] == "true"

    async def test_the_reel_is_sent_with_its_note(self, reeling, github, told):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await render(reeling, moment_id, "B", "1")
        await async_wait_until(lambda: len(github.dispatches) == 2, "the render to be asked for")
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

        await render(reeling, moment_id, "B", "MY OWN|*HOOK*")
        await async_wait_until(lambda: len(github.dispatches) == 2, "the render to be asked for")

        assert github.dispatches[1][1]["hook"] == "MY OWN|*HOOK*"
        section = await reel_section(reeling, moment_id)
        assert section.one(data_reel_own=True).attrs["value"] == "MY OWN|*HOOK*"
        assert all(hook.attrs["aria-pressed"] == "false" for hook in section.find(data_reel_hook=True))

    async def test_rendering_again_takes_the_last_reel_out_of_the_release_first(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        github.answer_reel(reels.tag(moment_id), b"an older reel")

        await render(reeling, moment_id, "B", "1")
        await async_wait_until(lambda: len(github.dispatches) == 2, "the render to be asked for")

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


class TestAskingAnew:
    async def test_a_new_storyboard_takes_the_last_answers_away_here_and_on_github(self, reeling, github):
        moment_id = clipped_moment()
        await storyboard_back(reeling, github, moment_id)
        await render(reeling, moment_id, "B", "1")
        await async_wait_until(lambda: len(github.dispatches) == 2, "the render to be asked for")
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
