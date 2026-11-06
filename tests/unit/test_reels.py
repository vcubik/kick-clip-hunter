"""reels.py's pure parts: what goes up with a clip, what a storyboard must
hold, the choices made from it, and the words the page says."""

from __future__ import annotations

import json

import pytest

from kick_clip_hunter import reels
from kick_clip_hunter.reels import ChatLine

STORYBOARD = {
    "variants": [{"id": "A", "render_args": []}, {"id": "B", "render_args": []}],
    "hooks": [{"id": 1, "text": "FIRST|*HOOK*"}, {"id": 2, "text": "SECOND"}],
}


class TestCredit:
    @pytest.mark.parametrize(
        ("channel", "credit"),
        [
            ("some_channel", "kick.com/some_channel"),
            ("somebody@twitch", "twitch.tv/somebody"),
            ("handle@youtube", "youtube.com/@handle"),
            ("handle@tiktok", "tiktok.com/@handle"),
        ],
    )
    def test_a_channel_is_credited_by_its_platforms_address(self, channel, credit):
        assert reels.credit(channel) == credit


class TestContext:
    def test_the_peak_is_the_second_most_of_chat_laughed_in(self):
        lines = [
            ChatLine(3.2, "a", "xd", True),
            ChatLine(7.1, "b", "xd", True),
            ChatLine(7.9, "c", "xd", True),
            ChatLine(7.5, "d", "what", False),
        ]

        assert reels.peak_second(lines) == 7

    def test_of_a_tie_the_earliest_second_is_the_peak(self):
        assert reels.peak_second([ChatLine(9.0, "a", "xd", True), ChatLine(4.0, "b", "xd", True)]) == 4

    def test_without_anyone_laughing_there_is_no_peak(self):
        assert reels.peak_second([ChatLine(1.0, "a", "hi", False)]) is None

    def test_chat_from_before_the_file_is_left_out_and_so_is_more_than_the_limit(self, monkeypatch):
        monkeypatch.setattr(reels, "CONTEXT_CHAT_LIMIT", 2)
        lines = [ChatLine(-1.0, "early", "x", False), *(ChatLine(float(n), f"u{n}", "x", False) for n in range(3))]

        context = reels.context(
            moment_id=1,
            channel="somebody@twitch",
            title=None,
            notes=None,
            rating=None,
            clip_start=None,
            clip_end=None,
            chat=lines,
        )

        assert [line["user"] for line in context["chat"]] == ["u0", "u1"]
        assert (context["streamer"], context["platform"], context["clip"]) == ("somebody", "twitch", None)
        assert (context["title"], context["notes"]) == ("", "")
        json.dumps(context)


class TestReadingAStoryboard:
    def test_a_good_one_is_taken_as_it_is(self):
        assert reels.read_storyboard(json.dumps(STORYBOARD).encode()) == STORYBOARD

    @pytest.mark.parametrize(
        ("raw", "said"),
        [
            (b"\xff", "not JSON"),
            (b"{", "not JSON"),
            (b"[]", "not a JSON object"),
            (b'{"hooks": [{"text": "x"}]}', "no variants"),
            (b'{"variants": [{}], "hooks": [{"text": "x"}]}', "no id"),
            (b'{"variants": [{"id": "A"}], "hooks": [{"id": 1}]}', "hooks have no words"),
            (b'{"variants": [{"id": "A"}]}', "hooks have no words"),
        ],
    )
    def test_one_the_page_could_not_offer_is_refused(self, raw, said):
        with pytest.raises(reels.BadStoryboard, match=said):
            reels.read_storyboard(raw)


class TestChoosing:
    def test_a_variant_is_found_whatever_its_case(self):
        assert reels.chosen_variant(STORYBOARD, " b ") == "B"

    def test_a_variant_the_storyboard_lacks_is_refused(self):
        with pytest.raises(ValueError, match="no such variant"):
            reels.chosen_variant(STORYBOARD, "C")

    def test_a_hooks_id_stands_for_its_words(self):
        assert reels.chosen_hook(STORYBOARD, "1") == "FIRST|*HOOK*"

    def test_anything_else_is_the_words_themselves(self):
        assert reels.chosen_hook(STORYBOARD, " MY *OWN* ") == "MY *OWN*"

    def test_no_hook_is_refused(self):
        with pytest.raises(ValueError, match="Choose a hook"):
            reels.chosen_hook(STORYBOARD, "  ")

    def test_too_long_a_hook_is_refused(self):
        with pytest.raises(ValueError, match="at most"):
            reels.chosen_hook(STORYBOARD, "X" * (reels.HOOK_MOST_CHARACTERS + 1))


class TestWords:
    def test_a_hook_is_shown_without_its_marks(self):
        assert reels.hook_shown("NEARLY RAN|*HIM OVER?*") == "NEARLY RAN / HIM OVER?"

    def test_every_state_says_something_but_none(self):
        assert reels.state_words(None) == ""
        for state in (*reels.BUSY, reels.CHOOSING, reels.DONE, reels.FAILED):
            assert reels.state_words(state)

    def test_a_failure_says_why(self):
        assert reels.state_words(reels.FAILED, "GitHub refused.") == "It did not work: GitHub refused."

    def test_names(self):
        assert reels.tag(7) == "moment-7"
        assert reels.run_name("render", "moment-7") == "render moment-7"


class TestGitHub:
    """The client against the fake GitHub: what it makes of GitHub saying no,
    or not being there."""

    @pytest.fixture
    def client(self, github):
        return reels.GitHub(github.repo, github.token)

    @pytest.mark.anyio
    async def test_a_file_goes_up_and_comes_back_down(self, client, github, tmp_path):
        release = await client.ensure_release("moment-1", "Moment 1")
        (tmp_path / "clip.mp4").write_bytes(b"clip")
        await client.upload(release, "clip.mp4", path=tmp_path / "clip.mp4")
        await client.upload(await client.release("moment-1"), "clip.mp4", data=b"clip again")

        found = (await client.release("moment-1")).asset("clip.mp4")
        await client.download(found, tmp_path / "down" / "clip.mp4")

        assert (tmp_path / "down" / "clip.mp4").read_bytes() == b"clip again"
        assert (await client.ensure_release("moment-1", "Moment 1")).id == release.id

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("fragment", "call"),
        [
            ("/releases/tags/", lambda client, release, to: client.release("moment-1")),
            ("/dispatches", lambda client, release, to: client.dispatch("render", {})),
            ("/releases/assets/", lambda client, release, to: client.remove_assets(release, ["clip.mp4"])),
            ("/releases/assets/", lambda client, release, to: client.download(release.asset("clip.mp4"), to)),
        ],
    )
    async def test_github_saying_no_is_said_in_words(self, client, github, tmp_path, fragment, call):
        release = await client.ensure_release("moment-1", "Moment 1")
        await client.upload(release, "clip.mp4", data=b"clip")
        release = await client.release("moment-1")
        github.failures[fragment] = 502

        with pytest.raises(reels.GitHubTrouble, match=r"GitHub refused .*502: Something went wrong"):
            await call(client, release, tmp_path / "clip.mp4")

        assert not (tmp_path / "clip.mp4.part").exists()

    @pytest.mark.anyio
    async def test_a_release_github_will_not_make_is_said(self, client, github):
        github.failures["POST /releases"] = 403

        with pytest.raises(reels.GitHubTrouble, match="make the release"):
            await client.ensure_release("moment-1", "Moment 1")

    @pytest.mark.anyio
    async def test_github_out_of_reach_is_said(self, client, github, tmp_path):
        release = await client.ensure_release("moment-1", "Moment 1")
        await client.upload(release, "clip.mp4", data=b"clip")
        release = await client.release("moment-1")
        github.unreachable = True

        for call in (
            client.release("moment-1"),
            client.ensure_release("moment-2", "Moment 2"),
            client.remove_assets(release, ["clip.mp4"]),
            client.upload(release, "other.json", data=b"{}"),
            client.download(release.asset("clip.mp4"), tmp_path / "clip.mp4"),
            client.dispatch("render", {}),
        ):
            with pytest.raises(reels.GitHubTrouble, match="could not be reached"):
                await call
        assert await client.failed_run("render moment-1", reels_since()) is None
        assert not (tmp_path / "clip.mp4.part").exists()

    @pytest.mark.anyio
    async def test_a_failed_run_is_told_by_its_name_and_time(self, client, github):
        since = reels_since()
        github.runs = [
            {"display_title": "render moment-2", "status": "completed", "conclusion": "failure", "created_at": NOW},
            {"display_title": "render moment-1", "status": "completed", "conclusion": "cancelled", "created_at": NOW},
        ]

        assert await client.failed_run("render moment-1", since) == "cancelled"
        assert await client.failed_run("render moment-3", since) is None

    @pytest.mark.anyio
    async def test_a_run_from_before_it_was_asked_or_unreadable_says_nothing(self, client, github):
        github.runs = [
            {"display_title": "render moment-1", "status": "completed", "conclusion": "failure", "created_at": "bad"},
            {"display_title": "render moment-1", "status": "completed", "conclusion": "failure",
             "created_at": "2020-01-01T00:00:00Z"},
            {"display_title": "render moment-1", "status": "completed", "conclusion": "success", "created_at": NOW},
        ]  # fmt: skip

        assert await client.failed_run("render moment-1", reels_since()) is None
        github.failures["/actions/runs"] = 500
        assert await client.failed_run("render moment-1", reels_since()) is None


NOW = "2026-06-01T12:00:00Z"


def reels_since():
    from datetime import datetime, timezone

    return datetime(2026, 6, 1, 11, 59, tzinfo=timezone.utc)
