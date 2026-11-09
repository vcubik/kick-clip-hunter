"""Several moments' clips made into one reel, through the service (see
reels.py): moments added to a compilation from the review page, its clips
put in order and each cut to a stretch of its moment's file on the Reels
page, the stretches and what goes with them sent to a fake GitHub, the
storyboard - with the clip the workflow joined them into - taken back, and
the rest of the way as a moment's reel goes. What the workflows would put
into the release the tests put there themselves; the service's look at
GitHub is called as its loop would."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from kick_clip_hunter import db, recorder, reels, subtitles
from kick_clip_hunter.config import load_settings
from tests.support.data import T0, add_chat, add_moment
from tests.support.github import FakeGitHub
from tests.support.html import parse
from tests.support.media import make_video, probe, require_ffmpeg
from tests.support.waiting import async_wait_until

from .test_api_reels import STORYBOARD, clip_cut_at, srt_bytes

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
OTHER = "other_channel"
BEFORE, CLIP, AFTER = 4.0, 6.0, 4.0
LENGTH = BEFORE + CLIP + AFTER
# Where the workflow says each part lies in the clip it joined them into.
JOINED = [{"start": 0.0, "end": 6.0}, {"start": 6.0, "end": 12.0}]
OUTSIDE = "The stretch has to lie within the clip's file, 0:00.0 to 0:14.0."
HEARD = [subtitles.Line(1.0, 2.0, "watch the road"), subtitles.Line(7.0, 8.5, "not again")]


@pytest.fixture
def reeling(service, github: FakeGitHub, monkeypatch):
    """The service set up to have reels made in the fake GitHub."""
    monkeypatch.setattr(service.main, "settings", load_settings())
    return service


def clipped(name: str, channel: str = CHANNEL, *, seconds: float = 0.0, **columns) -> int:
    """A moment whose file - a real video, a frame a cut can begin on every
    second - holds BEFORE seconds of footage, its CLIP-second clip and AFTER
    seconds more."""
    require_ffmpeg()
    file = recorder.CLIPS_DIR / channel / name
    make_video(file, LENGTH, keyframe_every=1.0)
    at = T0 + timedelta(seconds=seconds)
    return add_moment(
        channel,
        detected_at=at + timedelta(seconds=8),
        clip_path=f"{channel}/{name}",
        context_before=BEFORE,
        context_after=AFTER,
        **{**clip_cut_at(at, CLIP), **columns},
    )


async def add(service, moment_id: int):
    return await service.client.post(f"/moments/{moment_id}/compilation")


async def added(service, *moment_ids: int) -> int:
    for moment_id in moment_ids:
        response = await add(service, moment_id)
        assert response.status_code == 200, response.text
    return response.json()["compilation_id"]


def compilation_reel(compilation_id: int):
    conn = db.get_connection()
    try:
        return db.get_compilation_reel(conn, compilation_id)
    finally:
        conn.close()


def parts_of(compilation_id: int) -> list[tuple[int, float | None, float | None]]:
    conn = db.get_connection()
    try:
        return [
            (row["moment_id"], row["part_start"], row["part_end"])
            for row in db.get_compilation_parts(conn, compilation_id)
        ]
    finally:
        conn.close()


async def page(service, path: str):
    response = await service.client.get(path)
    assert response.status_code == 200, response.text
    return parse(response.text)


async def workspace(service, compilation_id: int):
    return (await page(service, f"/dashboard/reels/compilations/{compilation_id}")).one(data_reel=True)


async def sent(service, compilation_id: int, **asked) -> None:
    response = await service.client.post(f"/compilations/{compilation_id}/reel", json=asked)
    assert response.status_code == 200, response.text
    await async_wait_until(lambda: compilation_reel(compilation_id)["state"] != reels.SENDING, "the clips to be sent")
    assert compilation_reel(compilation_id)["state"] == reels.STORYBOARDING, compilation_reel(compilation_id)["error"]


def answer(github: FakeGitHub, compilation_id: int, joined=JOINED, clip: bytes = b"the joined clip") -> None:
    """What the storyboard workflow puts into a compilation's release."""
    tag = reels.compilation_tag(compilation_id)
    github.put(tag, reels.CLIP, clip)
    github.put(tag, reels.JOINED, json.dumps(joined).encode())
    github.answer_storyboard(tag, STORYBOARD, contact=b"frames")
    github.put(tag, reels.HEARD, srt_bytes(HEARD))


async def storyboard_back(service, github: FakeGitHub, compilation_id: int, **asked) -> None:
    await sent(service, compilation_id, **asked)
    answer(github, compilation_id)
    await service.main._look_at_reels()


class TestAddingClips:
    async def test_the_review_page_offers_to_add_a_clip_and_then_leads_to_the_compilation(self, reeling, github):
        first = clipped("moment_1.mp4")
        second = clipped("moment_2.mp4", seconds=60)

        before = (await page(reeling, f"/dashboard/moments/{first}")).one(data_compilation_link=True)
        response = await add(reeling, first)
        after = (await page(reeling, f"/dashboard/moments/{first}")).one(data_compilation_link=True)
        other = (await page(reeling, f"/dashboard/moments/{second}")).one(data_compilation_link=True)

        assert before.one(data_compilation_add=True).attrs["data-compilation-add"] == f"/moments/{first}/compilation"
        standing = response.json()
        compilation_id = standing["compilation_id"]
        assert standing == {
            "compilation_id": compilation_id,
            "parts": 1,
            "moments": [first],
            "url": f"/dashboard/reels?compilation={compilation_id}",
            "words": "In the compilation: 1 clip.",
        }
        assert after.find(data_compilation_add=True) == []
        assert after.one("a").attrs["href"] == standing["url"]
        assert "In the compilation: 1 clip." in after.text
        # Another moment is offered the same compilation.
        assert other.one(data_compilation_add=True)
        assert "has 1 clip" in other.text

    async def test_each_clip_starts_as_the_clip_the_service_cut_in_the_order_added(self, reeling, github):
        first = clipped("moment_1.mp4")
        second = clipped("moment_2.mp4", OTHER, seconds=60)

        compilation_id = await added(reeling, second, first)

        assert parts_of(compilation_id) == [(second, BEFORE, BEFORE + CLIP), (first, BEFORE, BEFORE + CLIP)]

    async def test_a_clip_is_added_once_and_no_more_than_a_compilation_holds(self, reeling, github, monkeypatch):
        monkeypatch.setattr(reels, "COMPILATION_MOST_PARTS", 2)
        first, second, third = (clipped(f"moment_{n}.mp4", seconds=60 * n) for n in range(1, 4))
        await added(reeling, first)

        twice = await add(reeling, first)
        await added(reeling, second)
        too_many = await add(reeling, third)

        assert (twice.status_code, twice.json()["detail"]) == (409, "The compilation has this clip already.")
        assert (too_many.status_code, too_many.json()["detail"]) == (409, "A compilation holds at most 2 clips.")

    async def test_a_moment_without_its_clip_is_not_added(self, reeling, github):
        moment_id = add_moment(CHANNEL, clip_path=f"{CHANNEL}/gone.mp4")

        response = await add(reeling, moment_id)

        assert (response.status_code, response.json()["detail"]) == (409, "The moment has no clip to add.")

    async def test_once_its_reel_is_asked_for_clips_go_to_a_new_compilation(self, reeling, github):
        first, second, third = (clipped(f"moment_{n}.mp4", seconds=60 * n) for n in range(1, 4))
        compilation_id = await added(reeling, first, second)
        await sent(reeling, compilation_id)

        later = await added(reeling, third)

        assert later != compilation_id
        assert parts_of(later) == [(third, BEFORE, BEFORE + CLIP)]


class TestPuttingItTogether:
    async def test_the_reels_page_lists_it_and_shows_its_clips_with_their_stretches(self, reeling, github):
        first = clipped("moment_1.mp4", title="The first bend")
        second = clipped("moment_2.mp4", OTHER, seconds=60)
        compilation_id = await added(reeling, first, second)

        listed = await page(reeling, "/dashboard/reels")
        opened = listed.one(data_reel=True)

        row = listed.one("a", class_="rec-row", data_reel_row=f"c{compilation_id}")
        assert row.attrs["aria-current"] == "true"
        assert "2 clips" in row.text and "Not sent" in row.text
        parts = opened.find("li", data_part=True)
        assert [part.attrs["data-moment-id"] for part in parts] == [str(first), str(second)]
        assert "The first bend" in parts[0].text
        stretch = parts[0].one("span", data_stretch="part")
        assert [stretch.attrs[name] for name in ("data-start", "data-end", "data-clip")] == ["4", "10", "14"]
        assert parts[0].one("video", data_part_source=True).attrs["src"].startswith(f"/clips/{CHANNEL}/moment_1.mp4")
        assert opened.one(data_reel_ask=True).attrs["data-reel-ask"] == f"/compilations/{compilation_id}/reel"
        # None of a moment's questions about a time in a file.
        assert opened.find(data_reel_peak=True) == []
        names = [group.attrs["data-reel-answer"] for group in opened.find(data_reel_answer=True)]
        assert {"transition", "labels", "order"} <= set(names)
        assert "kind" not in names

    async def test_its_order_stretches_and_theme_are_kept(self, reeling, github):
        first = clipped("moment_1.mp4")
        second = clipped("moment_2.mp4", seconds=60)
        compilation_id = await added(reeling, first, second)

        response = await reeling.client.post(
            f"/compilations/{compilation_id}",
            json={
                "theme": "  the roads   in Thailand ",
                "parts": [{"moment_id": second}, {"moment_id": first, "start": 2.5, "end": 9}],
            },
        )

        assert response.status_code == 200, response.text
        assert parts_of(compilation_id) == [(second, None, None), (first, 2.5, 9.0)]
        opened = await workspace(reeling, compilation_id)
        assert opened.one(data_compilation_theme=True).attrs["value"] == "the roads in Thailand"
        assert opened.one("h1").text == "the roads in Thailand"

    @pytest.mark.parametrize(
        ("parts", "said"),
        [
            ("all", "Expected the clips as a list."),
            ([{"moment_id": "1"}], "Clip 1: expected the moment it is of."),
            ([{"moment_id": 999}], "Clip 1: its moment is gone, or has no clip."),
            ([{"moment_id": 1, "start": 3, "end": 30}], f"Clip 1: {OUTSIDE}"),
            ([{"moment_id": 1}, {"moment_id": 1}], "Clip 2: the compilation has it already."),
        ],
    )  # fmt: skip
    async def test_clips_that_cannot_be_kept_are_refused(self, reeling, github, parts, said):
        first = clipped("moment_1.mp4")
        compilation_id = await added(reeling, first)

        response = await reeling.client.post(f"/compilations/{compilation_id}", json={"parts": parts})

        assert (response.status_code, response.json()["detail"]) == (400, said)
        assert parts_of(compilation_id) == [(first, BEFORE, BEFORE + CLIP)]

    async def test_one_clip_is_not_a_compilation(self, reeling, github):
        compilation_id = await added(reeling, clipped("moment_1.mp4"))

        response = await reeling.client.post(f"/compilations/{compilation_id}/reel", json={})

        assert response.status_code == 409
        assert response.json()["detail"].startswith("A compilation is made of at least 2 clips.")
        assert github.dispatches == []

    async def test_deleting_it_forgets_it_and_its_files_but_not_the_moments(self, reeling, github):
        first = clipped("moment_1.mp4")
        second = clipped("moment_2.mp4", seconds=60)
        compilation_id = await added(reeling, first, second)
        await storyboard_back(reeling, github, compilation_id)
        joined = reeling.main._compilation_reel(compilation_id).file(reels.CLIP)
        assert joined.exists()

        response = await reeling.client.post(f"/compilations/{compilation_id}/delete")

        assert response.status_code == 200, response.text
        assert compilation_reel(compilation_id) is None
        assert parts_of(compilation_id) == []
        assert not joined.exists()
        assert (await reeling.client.get(f"/dashboard/moments/{first}")).status_code == 200

    async def test_a_moment_removed_is_taken_out_of_it(self, reeling, github):
        first = clipped("moment_1.mp4")
        second = clipped("moment_2.mp4", seconds=60)
        compilation_id = await added(reeling, first, second)

        response = await reeling.client.post(f"/moments/{first}/delete")

        assert response.status_code == 200, response.text
        assert parts_of(compilation_id) == [(second, BEFORE, BEFORE + CLIP)]


class TestAskingForAStoryboard:
    async def test_each_stretch_goes_up_as_a_part_with_its_own_context(self, reeling, github, tmp_path):
        first = clipped("moment_1.mp4", title="First bend", notes="a scooter")
        second = clipped("moment_2.mp4", OTHER, seconds=60, broadcaster_user_id=2)
        add_chat(CHANNEL, "fan", "KEKW", T0 + timedelta(seconds=2))
        compilation_id = await added(reeling, first, second)

        await sent(
            reeling,
            compilation_id,
            theme="Thai roads",
            parts=[{"moment_id": first, "start": 2, "end": 11}, {"moment_id": second}],
            comment="The second is the best.",
            answers={"transition": "flash", "labels": "numbers", "order": "best_last", "peak": 3},
        )

        tag = reels.compilation_tag(compilation_id)
        assert github.dispatches == [("storyboard", {"tag": tag})]
        one, two = tmp_path / "1.mp4", tmp_path / "2.mp4"
        one.write_bytes(github.asset(tag, "part-1.mp4"))
        two.write_bytes(github.asset(tag, "part-2.mp4"))
        assert probe(one)["duration"] == pytest.approx(9, abs=0.15)
        assert probe(two)["duration"] == pytest.approx(LENGTH, abs=0.15)
        context = json.loads(github.asset(tag, reels.CONTEXT))
        assert {name: context[name] for name in ("kind", "compilation_id", "theme", "comment", "answers")} == {
            "kind": "compilation",
            "compilation_id": compilation_id,
            "theme": "Thai roads",
            "comment": "The second is the best.",
            # Of a time in a file: none, whatever was posted.
            "answers": {"transition": "flash", "labels": "numbers", "order": "best_last"},
        }
        assert context["credits"] == [f"kick.com/{CHANNEL}", f"kick.com/{OTHER}"]
        first_part, second_part = context["parts"]
        assert (first_part["file"], first_part["moment_id"], first_part["title"]) == ("part-1.mp4", first, "First bend")
        assert first_part["notes"] == "a scooter"
        # Timed to the stretch, which begins two seconds into the file.
        assert first_part["clip"] == {"start": pytest.approx(2, abs=0.05), "end": pytest.approx(8, abs=0.05)}
        assert [(line["t"], line["user"]) for line in first_part["chat"]] == [(pytest.approx(4, abs=0.05), "fan")]
        assert (second_part["file"], second_part["streamer"]) == ("part-2.mp4", OTHER)
        # Cut out to be sent, and gone once they are.
        assert not reeling.main._compilation_reel(compilation_id).file("part-1.mp4").exists()
        assert parts_of(compilation_id)[0][1:] == (pytest.approx(2, abs=0.05), pytest.approx(11, abs=0.15))

    async def test_nothing_of_it_changes_while_the_storyboard_is_made(self, reeling, github):
        compilation_id = await added(reeling, clipped("moment_1.mp4"), clipped("moment_2.mp4", seconds=60))
        await sent(reeling, compilation_id)

        response = await reeling.client.post(f"/compilations/{compilation_id}", json={"theme": "other"})
        opened = await workspace(reeling, compilation_id)

        assert response.status_code == 409
        box = opened.one(data_compilation=True)
        assert all("disabled" in control.attrs for control in box.find("input") + box.find("button"))
        assert opened.attrs["data-state"] == reels.STORYBOARDING
        assert "Sending the clips" not in opened.text


class TestTheStoryboardComingBack:
    async def test_the_joined_clip_and_where_each_part_is_in_it_are_kept_and_shown(self, reeling, github):
        compilation_id = await added(reeling, clipped("moment_1.mp4"), clipped("moment_2.mp4", seconds=60))

        await storyboard_back(reeling, github, compilation_id)

        reel = compilation_reel(compilation_id)
        assert reel["state"] == reels.CHOOSING
        assert reels.joined_read(reel["joined"]) == [(0.0, 6.0), (6.0, 12.0)]
        assert reeling.main._compilation_reel(compilation_id).file(reels.CLIP).read_bytes() == b"the joined clip"
        opened = await workspace(reeling, compilation_id)
        assert (
            opened.one("video", data_reel_source=True)
            .attrs["src"]
            .startswith(f"/clips/reels/compilation_{compilation_id}_clip.mp4")
        )
        assert [button.attrs["data-reel-seek"] for button in opened.find(data_reel_seek=True)] == ["0", "6"]
        # The transcript, in seconds of the joined clip.
        lines = opened.one(data_reel_lines=True).find(data_reel_line=True)
        assert [line.one(data_reel_start=True).attrs["value"] for line in lines] == ["0:01.0", "0:07.0"]
        assert (
            opened.one(data_reel_render=True).attrs["data-reel-render"] == f"/compilations/{compilation_id}/reel/render"
        )

    async def test_without_the_joined_clip_it_fails(self, reeling, github):
        compilation_id = await added(reeling, clipped("moment_1.mp4"), clipped("moment_2.mp4", seconds=60))
        await sent(reeling, compilation_id)
        github.answer_storyboard(reels.compilation_tag(compilation_id), STORYBOARD)

        await reeling.main._look_at_reels()

        reel = compilation_reel(compilation_id)
        assert (reel["state"], reel["error"]) == (
            reels.FAILED,
            "The storyboard workflow did not join the clips into one.",
        )

    async def test_the_same_clips_keep_their_transcript_and_are_not_sent_again(self, reeling, github):
        compilation_id = await added(reeling, clipped("moment_1.mp4"), clipped("moment_2.mp4", seconds=60))
        await storyboard_back(reeling, github, compilation_id)
        tag = reels.compilation_tag(compilation_id)
        github.releases[tag].assets.pop("part-1.mp4")

        await sent(reeling, compilation_id)

        assert github.asset(tag, "part-1.mp4") is None
        assert github.asset(tag, reels.CLIP) == b"the joined clip"
        assert github.asset(tag, reels.HEARD) is not None
        assert compilation_reel(compilation_id)["heard"] is not None

    async def test_another_stretch_is_joined_and_heard_anew(self, reeling, github):
        first = clipped("moment_1.mp4")
        second = clipped("moment_2.mp4", seconds=60)
        compilation_id = await added(reeling, first, second)
        await storyboard_back(reeling, github, compilation_id)
        tag = reels.compilation_tag(compilation_id)

        await sent(reeling, compilation_id, parts=[{"moment_id": second}, {"moment_id": first}])

        assert github.asset(tag, reels.CLIP) is None
        assert github.asset(tag, reels.HEARD) is None
        assert github.asset(tag, "part-1.mp4") is not None
        reel = compilation_reel(compilation_id)
        assert (reel["heard"], reel["joined"]) == (None, None)


class TestTheRestOfTheWay:
    async def test_it_is_rendered_put_right_and_given_feedback_on_as_a_moments_reel(self, reeling, github):
        compilation_id = await added(reeling, clipped("moment_1.mp4"), clipped("moment_2.mp4", seconds=60))
        await reeling.client.post(f"/compilations/{compilation_id}", json={"theme": "Thai roads"})
        await storyboard_back(reeling, github, compilation_id)
        tag = reels.compilation_tag(compilation_id)

        saved = await reeling.client.post(
            f"/compilations/{compilation_id}/reel/transcript",
            json={"lines": [{"start": 7.0, "end": 8.5, "text": "Not again!", "keep": True}]},
        )
        response = await reeling.client.post(
            f"/compilations/{compilation_id}/reel/render", json={"variant": "B", "hook": "1", "subtitles": "on"}
        )
        await async_wait_until(lambda: len(github.dispatches) > 1, "the render to be asked for")
        github.answer_reel(tag, b"the reel")
        await reeling.main._look_at_reels()
        feedback = await reeling.client.post(
            f"/compilations/{compilation_id}/reel/feedback", json={"feedback": "Great order."}
        )

        assert saved.status_code == 200, saved.text
        assert response.status_code == 200, response.text
        event, payload = github.dispatches[1]
        assert (event, payload["tag"], payload["keep"]) == (
            "render",
            tag,
            [{"start": 7.0, "end": 8.5, "text": "Not again!"}],
        )
        assert subtitles.from_srt(github.asset(tag, reels.SUBTITLES).decode("utf-8-sig")) == [
            subtitles.Line(7.0, 8.5, "Not again!")
        ]
        reel = compilation_reel(compilation_id)
        assert reel["state"] == reels.DONE
        assert (recorder.CLIPS_DIR / reel["reel_path"]).read_bytes() == b"the reel"
        assert feedback.status_code == 200, feedback.text
        record = json.loads(github.asset(tag, reels.FEEDBACK))
        assert {name: record[name] for name in ("tag", "moment_id", "compilation_id", "title", "streamer")} == {
            "tag": tag,
            "moment_id": None,
            "compilation_id": compilation_id,
            "title": "Thai roads",
            "streamer": CHANNEL,
        }
        assert github.dispatches[-1] == ("feedback", {"tag": tag})
