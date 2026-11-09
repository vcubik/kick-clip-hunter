"""reels.py's pure parts: what goes up with a clip, what a storyboard must
hold, the choices made from it, and the words the page says."""

from __future__ import annotations

import json

import pytest

from kick_clip_hunter import reels, subtitles
from kick_clip_hunter.reels import ChatLine
from kick_clip_hunter.subtitles import Line

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

    def test_an_emoji_shortcut_counts_as_its_emoji(self):
        hook = "X" * (reels.HOOK_MOST_CHARACTERS - 2) + "{rofl}{SKULL}"

        assert reels.chosen_hook(STORYBOARD, hook) == hook

    def test_shortcuts_beyond_what_may_be_typed_are_refused(self):
        with pytest.raises(ValueError, match="at most"):
            reels.chosen_hook(STORYBOARD, "{x}" * (reels.HOOK_MOST_TYPED // 3 + 1))


class TestWords:
    def test_a_hook_is_shown_without_its_marks(self):
        assert reels.hook_shown("NEARLY RAN|*HIM OVER?*") == "NEARLY RAN / HIM OVER?"

    def test_a_hooks_emoji_shortcuts_are_shown_as_emoji_whatever_their_case(self):
        assert reels.hook_shown("NEARLY RAN|*HIM OVER?* {rofl}{Lebka}") == "NEARLY RAN / HIM OVER? 🤣💀"

    def test_an_unknown_shortcut_is_shown_as_typed_and_marked(self):
        assert reels.hook_pieces("WHAT {rofl} {nope}!") == [
            {"text": "WHAT 🤣 ", "unknown": False},
            {"text": "{nope}", "unknown": True},
            {"text": "!", "unknown": False},
        ]

    def test_a_hook_without_shortcuts_is_one_piece(self):
        assert reels.hook_pieces("PLAIN") == [{"text": "PLAIN", "unknown": False}]

    def test_every_emoji_is_offered_once_with_all_its_shortcuts(self):
        picks = {pick["emoji"]: pick["codes"] for pick in reels.EMOJI_PICKS}

        assert picks["😭"] == ["sob", "cry", "brek"]
        assert sorted(code for codes in picks.values() for code in codes) == sorted(reels.EMOJI_CODES)

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


class TestTheTranscript:
    def test_a_time_is_written_to_the_tenth(self):
        assert [reels.clock(seconds) for seconds in (0, 7.44, 7.46, 59.96, 725.0, -3)] == [
            "0:00.0",
            "0:07.4",
            "0:07.5",
            "1:00.0",
            "12:05.0",
            "0:00.0",
        ]

    def test_lines_are_kept_tidy_in_the_order_they_are_said(self):
        kept = reels.checked_transcript(
            [
                {"start": 3, "end": 4.25, "text": " second\n line "},
                {"start": 1.0004, "end": 2.0, "text": "A: first"},
                {"start": 2.5, "end": 2.6, "text": ""},
            ],
            length=10.0,
        )

        assert kept == [Line(1.0, 2.0, "A: first"), Line(3.0, 4.25, "second line")]

    def test_a_line_is_cut_to_what_a_subtitle_holds(self):
        (kept,) = reels.checked_transcript([{"start": 0, "end": 1, "text": "x" * 400}], length=None)

        assert len(kept.text) == subtitles.TEXT_MAX_CHARACTERS

    def test_a_line_may_run_a_little_past_the_files_measured_end(self):
        reels.checked_transcript([{"start": 9, "end": 10.0 + reels.TRANSCRIPT_SLACK_SECONDS, "text": "x"}], 10.0)

    @pytest.mark.parametrize(
        ("posted", "said"),
        [
            ("lines", "Expected the transcript's lines"),
            ([None], "Line 1: expected its start, end and words"),
            ([{"start": True, "end": 2, "text": "x"}], "Line 1: write when it starts and ends"),
            ([{"start": 1, "end": float("nan"), "text": "x"}], "Line 1: write when it starts and ends"),
            ([{"start": 2, "end": 1, "text": "x"}], "Line 1: it has to end after it starts"),
            ([{"start": 1, "end": 11, "text": "x"}], "Line 1: the clip ends at 0:10.0"),
        ],
    )
    def test_what_cannot_be_kept_is_refused_saying_which_line(self, posted, said):
        with pytest.raises(ValueError, match=said):
            reels.checked_transcript(posted, length=10.0)

    def test_too_many_lines_are_refused(self, monkeypatch):
        monkeypatch.setattr(reels, "TRANSCRIPT_MOST_LINES", 1)

        with pytest.raises(ValueError, match="at most 1 lines"):
            reels.checked_transcript([{"start": 0, "end": 1, "text": "x"}] * 2, length=None)

    def test_a_variant_has_subtitles_where_its_render_says_so(self):
        assert reels.subtitled({"render_args": ["--seg", "1-2", "--subs", "work/clip.subs.srt"]})
        assert not reels.subtitled({"render_args": ["--seg", "1-2"]})
        assert not reels.subtitled({})

    def test_a_file_is_known_again_by_its_stamp_until_it_changes(self, tmp_path):
        file = tmp_path / "moment.mp4"
        file.write_bytes(b"clip")
        first = reels.stamp(file)

        assert reels.stamp(file) == first
        file.write_bytes(b"clip and more")
        assert reels.stamp(file) != first
        assert reels.stamp(tmp_path / "gone.mp4") is None

    def test_a_stretch_of_a_file_is_another_file_by_its_stamp(self, tmp_path):
        file = tmp_path / "moment.mp4"
        file.write_bytes(b"clip")

        whole = reels.stamp(file)
        stretch = reels.stamp(file, (2.0, 9.5))

        assert len({whole, stretch, reels.stamp(file, (2.0, 9.0))}) == 3
        assert reels.stamp(file, (2.0, 9.5)) == stretch
        assert reels.stamp(tmp_path / "gone.mp4", (2.0, 9.5)) is None

    def test_lines_of_a_stretch_are_moved_to_the_file(self):
        assert reels.moved([Line(0.5, 1.25, "a")], 8.0001) == [Line(8.5, 9.25, "a")]

    def test_the_list_says_where_each_reel_stands_in_a_word(self):
        assert [reels.row_words(state) for state in (reels.STORYBOARDING, reels.CHOOSING, reels.DONE, None)] == [
            "Storyboard",
            "Choose",
            "Ready",
            "",
        ]


class TestAStretchOfTheFile:
    def test_none_asked_for_is_the_whole_file(self):
        assert reels.part_asked(None, None, 35.0) is None

    def test_a_stretch_is_kept_to_the_thousandth(self):
        assert reels.part_asked(8.00049, 20, 35.0) == (8.0, 20.0)

    def test_one_that_leaves_out_next_to_nothing_is_the_whole_file(self):
        slack = reels.PART_SLACK_SECONDS
        assert reels.part_asked(slack, 35.0 - slack, 35.0) is None
        assert reels.part_asked(slack * 2, 35.0, 35.0) == (slack * 2, 35.0)

    def test_without_the_files_length_only_its_start_is_checked(self):
        assert reels.part_asked(0, 500, None) == (0.0, 500.0)
        with pytest.raises(ValueError, match=r"within the clip's file\."):
            reels.part_asked(-1, 5, None)

    @pytest.mark.parametrize(
        ("start", "end", "said"),
        [
            (None, 5, "Expected the start and end"),
            (1, float("inf"), "Expected the start and end"),
            (False, 5, "Expected the start and end"),
            (-0.5, 5, "within the clip's file, 0:00.0 to 0:35.0"),
            (1, 35.0 + reels.TRANSCRIPT_SLACK_SECONDS + 0.1, "within the clip's file"),
            (4, 4 + reels.PART_LEAST_SECONDS / 2, "at least a second after it starts"),
        ],
    )
    def test_one_that_cannot_be_cut_is_refused(self, start, end, said):
        with pytest.raises(ValueError, match=said):
            reels.part_asked(start, end, 35.0)


class TestComments:
    def test_none_is_no_comment(self):
        assert reels.comment_asked(None) == ""
        assert reels.comment_asked(" \n ") == ""

    def test_its_rows_are_kept_without_the_space_at_their_ends(self):
        assert reels.comment_asked("  Start late.  \r\n\r\nEnd early.\rKeep the laugh. \n") == (
            "Start late.\n\nEnd early.\nKeep the laugh."
        )

    def test_one_as_long_as_allowed_goes_along(self):
        assert reels.comment_asked("x" * reels.COMMENT_MOST_CHARACTERS) == "x" * reels.COMMENT_MOST_CHARACTERS

    @pytest.mark.parametrize(
        ("value", "said"),
        [("x" * (reels.COMMENT_MOST_CHARACTERS + 1), "at most"), (["x"], "as words"), (3, "as words")],
    )
    def test_what_cannot_go_along_is_refused(self, value, said):
        with pytest.raises(ValueError, match=said):
            reels.comment_asked(value)

    def test_the_storyboards_comment_goes_up_in_its_context(self):
        context = reels.context(
            moment_id=1,
            channel="some_channel",
            title=None,
            notes=None,
            rating=None,
            clip_start=None,
            clip_end=None,
            chat=[],
            comment="Start late.",
        )

        assert context["comment"] == "Start late."


class TestFeedback:
    def feedback(self, **changed):
        return reels.feedback(
            **{
                "moment_id": 7,
                "channel": "somebody@twitch",
                "title": None,
                "storyboard": {"summary": "He falls.", "variants": [{"id": "B", "structure": "S2"}]},
                "storyboard_comment": None,
                "variant": "B",
                "hook": "FIRST|*HOOK*",
                "subtitles_asked": None,
                "render_comment": "Shorter.",
                "render_note": None,
                "rendered_at": "2026-11-07T10:00:00+00:00",
                "words": "Too long.",
                **changed,
            }
        )

    def test_it_says_what_the_reel_was_made_of_and_with(self):
        sent = self.feedback()

        assert sent["tag"] == "moment-7"
        assert (sent["streamer"], sent["platform"], sent["credit"]) == ("somebody", "twitch", "twitch.tv/somebody")
        assert (sent["summary"], sent["variant"], sent["variant_title"]) == ("He falls.", "B", "S2")
        assert (sent["storyboard_comment"], sent["render_comment"], sent["render_note"]) == ("", "Shorter.", "")
        assert (sent["subtitles"], sent["feedback"]) == (reels.SUBTITLES_AS_THE_VARIANT, "Too long.")
        json.dumps(sent)

    def test_a_variant_the_storyboard_lacks_has_no_title(self):
        assert self.feedback(variant="Z")["variant_title"] == ""
        assert self.feedback(storyboard=None)["summary"] == ""


class TestWhatTheRenderSays:
    def test_its_note_is_what_claude_made_of_the_comment(self):
        assert reels.rendered_note(json.dumps({"note": " Ended it sooner. "}).encode()) == "Ended it sooner."

    @pytest.mark.parametrize("raw", [b"not json", b"\xff", b"[1]", b'{"note": 3}', b"{}"])
    def test_anything_else_says_nothing(self, raw):
        assert reels.rendered_note(raw) == ""

    def test_a_long_note_is_cut_to_what_a_comment_may_be(self):
        note = reels.rendered_note(json.dumps({"note": "x" * (reels.COMMENT_MOST_CHARACTERS + 5)}).encode())

        assert len(note) == reels.COMMENT_MOST_CHARACTERS


class TestAnswers:
    def test_what_is_answered_goes_along_tidied_and_the_rest_is_left_out(self):
        answers = reels.answers_asked(
            {
                "cut": "raw",
                "layout": "cam",
                "kind": "",
                "subtitles": "no",
                "who": "  Pepa   from the van ",
                "peak": 12.34567,
                "keep": [{"start": 30, "end": 33, "note": ""}, {"start": 1, "end": 4.5, "note": " says  he won't "}],
                "render_now": True,
            },
            40.0,
        )

        assert answers == {
            "cut": "raw",
            "layout": "cam",
            "subtitles": "no",
            "who": "Pepa from the van",
            "peak": 12.346,
            # In the order they are in the clip.
            "keep": [{"start": 1.0, "end": 4.5, "note": "says he won't"}, {"start": 30.0, "end": 33.0, "note": ""}],
            "render_now": True,
        }

    def test_none_answered_is_nothing(self):
        assert reels.answers_asked(None, 40.0) == {}
        assert reels.answers_asked({"cut": "", "keep": [], "render_now": False}, 40.0) == {}

    def test_an_answer_that_is_none_of_the_choices_is_words_of_ones_own(self):
        answers = reels.answers_asked({"cut": "  only the  first minute ", "kind": "   "}, 40.0)

        assert answers == {"cut": "only the first minute"}

    @pytest.mark.parametrize(
        ("posted", "said"),
        [
            ([], "Expected the answers"),
            ({"cut": 5}, "Expected the answer to Cut as words"),
            ({"hook_style": "x" * (reels.ANSWER_OWN_MOST + 1)}, "of your own to Hook style is at most"),
            ({"who": 5}, "who is in the clip"),
            ({"who": "x" * (reels.ANSWER_WHO_MOST + 1)}, "at most"),
            ({"peak": "soon"}, "the joke's time"),
            ({"peak": True}, "the joke's time"),
            ({"peak": 41.0}, "not in the clip's file"),
            ({"keep": {"start": 1}}, "as a list"),
            ({"keep": [{"start": 5, "end": 5}]}, "end after it starts"),
            ({"keep": [{"start": -1, "end": 5}]}, "not in the clip's file"),
            ({"keep": [{"start": 1, "end": 2, "note": "x" * (reels.ANSWER_NOTE_MOST + 1)}]}, "at most"),
            ({"keep": [{"start": 1, "end": 2}] * (reels.ANSWER_KEEP_MOST + 1)}, "At most"),
        ],
    )
    def test_what_cannot_go_along_is_refused(self, posted, said):
        with pytest.raises(ValueError, match=said):
            reels.answers_asked(posted, 40.0)

    def test_a_file_of_unknown_length_takes_any_time_after_its_start(self):
        assert reels.answers_asked({"peak": 500}, None) == {"peak": 500.0}

    def test_they_go_up_in_the_context(self):
        made = {
            "moment_id": 1, "channel": "some_channel", "title": None, "notes": None, "rating": None,
            "clip_start": None, "clip_end": None, "chat": [],
        }  # fmt: skip

        assert reels.context(**made, answers={"cut": "tighten"})["answers"] == {"cut": "tighten"}
        assert reels.context(**made)["answers"] == {}

    def test_for_a_stretch_their_times_are_moved_and_what_is_outside_it_left_out(self):
        answers = {
            "cut": "raw",
            "peak": 5.0,
            "keep": [
                {"start": 8, "end": 12, "note": "a"},
                {"start": 18, "end": 25, "note": "b"},
                {"start": 30, "end": 35, "note": "c"},
            ],
        }

        moved = reels.answers_moved(answers, (10.0, 20.0))

        # The joke was before the stretch; the parts are cut to it.
        assert moved == {
            "cut": "raw",
            "keep": [{"start": 0.0, "end": 2.0, "note": "a"}, {"start": 8.0, "end": 10.0, "note": "b"}],
        }
        assert reels.answers_moved(answers, None) == answers


class TestRecommended:
    def test_the_storyboards_recommended_variant_and_hook(self):
        storyboard = {
            "variants": [{"id": "A"}, {"id": "B", "recommended": True}],
            "hooks": [{"id": 1, "text": "ONE"}, {"id": 2, "text": "TWO", "recommended": True}],
        }

        assert reels.recommended(storyboard) == ("B", "2")

    def test_where_it_recommends_none_the_first(self):
        assert reels.recommended(STORYBOARD) == ("A", "1")


class TestKeptLines:
    def test_kept_as_when_each_ticked_line_starts_and_ends_and_read_back(self):
        posted = [
            {"start": 3.0, "end": 4.0, "text": "two", "keep": True},
            {"start": 1.0, "end": 2.0, "text": "one", "keep": False},
            {"start": 5.0, "end": 6.0, "text": "  ", "keep": True},
            {"start": 7.0, "end": 8.0, "text": "three", "keep": "yes"},
        ]

        kept = reels.kept_asked(posted, None)

        assert kept == [(3.0, 4.0)]
        assert reels.kept_read(reels.kept_stored(kept)) == {(3.0, 4.0)}
        assert reels.kept_stored([]) is None

    @pytest.mark.parametrize("stored", [None, "", "not json", '{"a": 1}', "[[1]]", '[["x", 2]]'])
    def test_what_cannot_be_read_is_none_kept(self, stored):
        assert reels.kept_read(stored) == set()

    def test_the_kept_lines_of_a_stretch_cut_to_it_and_timed_to_it(self):
        transcript = [Line(1.0, 3.0, "into it"), Line(4.0, 5.0, "in it"), Line(6.0, 7.0, "not ticked")]
        kept = {(1.0, 3.0), (4.0, 5.0), (9.0, 9.5)}

        assert reels.kept_lines(transcript, kept, (2.0, 10.0)) == [
            {"start": 0.0, "end": 1.0, "text": "into it"},
            {"start": 2.0, "end": 3.0, "text": "in it"},
        ]
        assert reels.kept_lines(transcript, kept, (5.0, 10.0)) == []
        assert len(reels.kept_lines(transcript, kept, None)) == 2


class TestCompilations:
    def test_its_release_and_its_parts_are_named(self):
        assert reels.compilation_tag(3) == "compilation-3"
        assert [reels.part_name(number) for number in (1, 2)] == ["part-1.mp4", "part-2.mp4"]

    def test_what_goes_up_is_each_parts_context_with_the_file_it_goes_up_as(self):
        first = {"moment_id": 1, "streamer": "a", "platform": "kick", "credit": "kick.com/a"}
        second = {"moment_id": 2, "streamer": "b", "platform": "kick", "credit": "kick.com/b"}
        third = {"moment_id": 3, "streamer": "a", "platform": "kick", "credit": "kick.com/a"}

        context = reels.compilation_context(
            compilation_id=4,
            theme="roads",
            parts=[first, second, third],
            comment="best last",
            answers={"order": "mine"},
        )

        assert context == {
            "kind": "compilation",
            "compilation_id": 4,
            "theme": "roads",
            "streamer": "a",
            "platform": "kick",
            "credit": "kick.com/a",
            "credits": ["kick.com/a", "kick.com/b"],
            "parts": [
                {"file": "part-1.mp4", **first},
                {"file": "part-2.mp4", **second},
                {"file": "part-3.mp4", **third},
            ],
            "transcript_corrected": False,
            "comment": "best last",
            "answers": {"order": "mine"},
        }

    def test_its_theme_is_one_row_and_not_too_long(self):
        assert reels.theme_asked("  the   roads\nin Thailand ") == "the roads in Thailand"
        assert reels.theme_asked(None) == ""
        with pytest.raises(ValueError, match="as words"):
            reels.theme_asked(3)
        with pytest.raises(ValueError, match=f"at most {reels.THEME_MOST_CHARACTERS}"):
            reels.theme_asked("x" * (reels.THEME_MOST_CHARACTERS + 1))

    def test_its_answers_are_its_own_questions_and_none_of_a_time_in_a_file(self):
        posted = {"transition": "flash", "labels": "a few words of mine", "kind": "fail", "peak": 3, "keep": []}

        answers = reels.answers_asked(posted, None, reels.COMPILATION_ANSWER_CHOICES)

        assert answers == {"transition": "flash", "labels": "a few words of mine"}

    def test_where_the_parts_were_joined_is_read_and_kept(self):
        raw = json.dumps([{"start": 0, "end": 6.25}, {"start": 6.25, "end": 12.5}]).encode()

        spans = reels.read_joined(raw)

        assert spans == [(0.0, 6.25), (6.25, 12.5)]
        assert reels.joined_read(reels.joined_stored(spans)) == spans
        assert reels.joined_read(None) == []
        assert reels.joined_read("not json") == []

    @pytest.mark.parametrize("raw", [b"nope", b"[]", b"{}", b'[{"start": 3, "end": 2}]', b'[{"start": "0", "end": 2}]'])
    def test_a_parts_json_that_does_not_say_where_they_are_is_bad(self, raw):
        with pytest.raises(reels.BadStoryboard):
            reels.read_joined(raw)

    def test_its_state_speaks_of_clips(self):
        assert reels.state_words(reels.SENDING, several=True) == "Sending the clips to GitHub."
