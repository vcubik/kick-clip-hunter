"""The dashboard's wording: what the pages say, worked out from stored rows.

All of it is pure, so each rule gets a test that names it - how a day, a
count or a detector reason is put into words, how the queue is grouped by
stream, what an empty list and an idle service are described as.

Times are built in the machine's own time zone and compared on its own
clock, so nothing here depends on which zone the suite runs in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from kick_clip_hunter import dashboard_view as view

TODAY = date(2026, 10, 9)  # a Friday


def local(hour: int, minute: int = 0, *, days_ago: int = 0) -> datetime:
    """A time on the local clock, `days_ago` days before TODAY."""
    day = TODAY - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, hour, minute).astimezone()


def row(**overrides) -> dict:
    """A moment as the database hands it over."""
    values = {
        "id": 1,
        "channel_slug": "some_channel",
        "detected_at": local(21, 47).isoformat(),
        "reason": "laugh",
        "score": 7.5,
        "message_count": 12,
        "baseline_message_rate": 0.5,
        "current_message_rate": 1.2,
        "emote_count": 3,
        "keyword_hits": 8,
        "stream_elapsed_seconds": 3600,
        "clip_path": "some_channel/moment_1.mp4",
        "rating": None,
        "title": None,
        "downloaded_at": None,
    }
    values.update(overrides)
    return values


class TestCountsAndNumbers:
    @pytest.mark.parametrize(("count", "words"), [(0, "0 messages"), (1, "1 message"), (12, "12 messages")])
    def test_a_count_agrees_with_its_noun(self, count, words):
        assert view.count_words(count, "message") == words

    @pytest.mark.parametrize(
        ("value", "words"), [(0.5, "0.5"), (1.25, "1.25"), (12.0, "12"), (0.071, "0.07"), (0.0, "0"), (100.0, "100")]
    )
    def test_a_measured_value_carries_no_trailing_zeros(self, value, words):
        assert view.number_words(value) == words

    def test_nothing_is_never_written_as_minus_nothing(self):
        assert view.number_words(-0.0) == "0"
        assert view.number_words(-0.001) == "0"
        assert view.number_words(-0.5) == "-0.5"

    def test_a_value_can_be_rounded_harder(self):
        assert view.number_words(12.3456, 1) == "12.3"
        assert view.number_words(19.96, 1) == "20"

    @pytest.mark.parametrize(
        ("items", "words"),
        [([], ""), (["a"], "a"), (["a", "b"], "a and b"), (["a", "b", "c"], "a, b and c")],
    )
    def test_a_list_is_joined_the_way_it_is_said(self, items, words):
        assert view.list_words(items) == words

    @pytest.mark.parametrize(("rating", "words"), [(None, "not rated yet"), (1, "rated 1 of 5"), (5, "rated 5 of 5")])
    def test_a_rating_in_words(self, rating, words):
        assert view.rating_words(rating) == words


class TestDaysAndTimes:
    def test_today_and_yesterday_are_called_that(self):
        assert view.day_words(TODAY, TODAY) == "today"
        assert view.day_words(TODAY - timedelta(days=1), TODAY) == "yesterday"

    def test_any_other_day_is_written_out_with_its_weekday(self):
        assert view.day_words(date(2026, 10, 7), TODAY) == "Wednesday 7 October"
        assert view.day_words(date(2026, 10, 10), TODAY) == "Saturday 10 October"

    def test_the_year_is_only_given_when_it_is_not_this_one(self):
        assert view.date_words(date(2026, 9, 12), TODAY) == "12 September"
        assert view.date_words(date(2025, 12, 31), TODAY) == "31 December 2025"
        assert view.day_words(date(2025, 12, 31), TODAY) == "Wednesday 31 December 2025"

    def test_a_moment_is_dated_as_a_day_and_a_time(self):
        assert view.when_words(local(21, 47), TODAY) == "Today at 21:47"
        assert view.when_words(local(9, 5, days_ago=1), TODAY) == "Yesterday at 09:05"
        assert view.when_words(local(9, 5, days_ago=2), TODAY) == "Wednesday 7 October at 09:05"

    def test_since_is_the_time_alone_for_today_and_names_the_day_otherwise(self):
        assert view.since_words(local(2, 14), TODAY) == "02:14"
        assert view.since_words(local(23, 59, days_ago=1), TODAY) == "yesterday, 23:59"
        assert view.since_words(local(18, 0, days_ago=3), TODAY) == "Tuesday 6 October, 18:00"

    @pytest.mark.parametrize(
        ("seconds", "words"),
        [(0, "0:00:00"), (59, "0:00:59"), (3723, "1:02:03"), (36000, "10:00:00"), (90000, "25:00:00")],
    )
    def test_stream_time_is_hours_minutes_and_seconds(self, seconds, words):
        assert view.stream_time_words(seconds) == words

    def test_stream_time_that_was_not_recorded_has_no_words(self):
        assert view.stream_time_words(None) is None


class TestReasons:
    @pytest.mark.parametrize("reason", sorted(view.REASON_WORDS))
    def test_every_reason_the_detector_gives_has_words_of_its_own(self, reason):
        (words,) = view.reason_words(reason)

        assert words == view.REASON_WORDS[reason]
        assert "_" not in words

    def test_what_only_added_to_a_moment_is_named_after_what_set_it_off(self):
        # The detector stores message_rate first, but it never fires alone.
        assert view.reason_words("message_rate,laugh,emotes") == ["laughing", "emotes", "a busy chat"]

    def test_it_is_left_out_where_there_is_only_room_for_the_main_reason(self):
        assert view.reason_words("message_rate,laugh", main_only=True) == ["laughing"]

    def test_but_never_at_the_price_of_saying_nothing(self):
        assert view.reason_words("message_rate", main_only=True) == ["a busy chat"]

    def test_a_reason_without_words_is_shown_as_it_was_stored(self):
        assert view.reason_words("some_new_signal") == ["some new signal"]

    def test_an_empty_reason_gives_nothing(self):
        assert view.reason_words("") == []

    def test_a_queue_row_names_the_main_reasons(self):
        assert view.queue_label(row(reason="message_rate,laugh,emote_mention")) == "Laughing, emote names"

    def test_a_queue_row_says_when_there_is_no_clip(self):
        assert view.queue_label(row(clip_path=None)) == "Laughing, no clip"

    def test_an_imported_clip_is_called_that(self):
        assert view.queue_label(row(reason=view.IMPORT_REASON)) == "Imported"

    def test_a_clip_made_on_command_is_called_that(self):
        assert view.queue_label(row(reason=view.COMMAND_REASON)) == "On command"
        assert view.queue_label(row(reason=view.COMMAND_REASON), "KEKW x3") == "KEKW x3"

    def test_a_name_the_reviewer_gave_takes_the_place_of_everything_else(self):
        named = row(title="He fell off the chair")

        assert view.queue_label(named, "KEKW \N{MULTIPLICATION SIGN}31") == "He fell off the chair"
        assert view.queue_label(row(title="Chair", clip_path=None)) == "Chair, no clip"

    def test_what_a_named_row_would_say_without_its_name_can_be_asked_for(self):
        named = row(title="He fell off the chair")

        assert (
            view.queue_label(named, "KEKW \N{MULTIPLICATION SIGN}31", named=False) == "KEKW \N{MULTIPLICATION SIGN}31"
        )
        assert view.queue_label(named, named=False) == view.queue_label(row())

    def test_what_chat_said_most_takes_the_place_of_the_reason(self):
        # Written the way chat wrote it, not put into sentence case.
        assert view.queue_label(row(), "xD \N{MULTIPLICATION SIGN}14") == "xD \N{MULTIPLICATION SIGN}14"
        assert view.queue_label(row(clip_path=None), "KEKW \N{MULTIPLICATION SIGN}31") == (
            "KEKW \N{MULTIPLICATION SIGN}31, no clip"
        )


class TestSummary:
    def test_gives_the_detectors_figures_as_sentences(self):
        assert view.summary_words(row()) == (
            "Score 7.5, set off by laughing. "
            "12 messages in 10 seconds; the usual pace is 0.5 a second. "
            "8 laughs or emote names and 3 emotes among them."
        )

    def test_names_every_reason(self):
        summary = view.summary_words(row(reason="message_rate,laugh,emotes"))

        assert summary.startswith("Score 7.5, set off by laughing, emotes and a busy chat.")

    def test_the_window_is_worked_out_from_the_count_and_the_rate(self):
        summary = view.summary_words(row(message_count=45, current_message_rate=3.0))

        assert "45 messages in 15 seconds;" in summary

    def test_singulars(self):
        summary = view.summary_words(
            row(message_count=1, current_message_rate=1.0, keyword_hits=1, emote_count=1, baseline_message_rate=0.07)
        )

        assert "1 message in 1 second; the usual pace is 0.07 a second." in summary
        assert summary.endswith("1 laugh or emote name and 1 emote among them.")

    def test_what_there_was_none_of_is_not_mentioned(self):
        assert view.summary_words(row(keyword_hits=0)).endswith("3 emotes among them.")
        assert view.summary_words(row(emote_count=0)).endswith("8 laughs or emote names among them.")
        assert view.summary_words(row(keyword_hits=0, emote_count=0)).endswith("the usual pace is 0.5 a second.")

    def test_without_a_rate_there_is_no_window_to_name(self):
        summary = view.summary_words(row(message_count=12, current_message_rate=0.0))

        assert "12 messages; the usual pace" in summary

    def test_without_a_reason_only_the_score_is_given(self):
        assert view.summary_words(row(reason="")).startswith("Score 7.5. 12 messages")

    def test_an_imported_clip_has_no_figures_to_give(self):
        imported = row(reason=view.IMPORT_REASON, score=0.0, message_count=0, current_message_rate=0.0)

        assert view.summary_words(imported) == "Imported clip. No chat was measured for it."

    def test_nor_has_a_clip_made_on_command(self):
        commanded = row(reason=view.COMMAND_REASON, score=0.0, message_count=0, current_message_rate=0.0)

        assert view.summary_words(commanded) == "Asked for in chat. The detector had no part in it."


# One channel's 7TV emotes, as stored: (emote id, width, height) by name.
EMOTES = {"KEKW": ("ID1", 32, 32), "WideHard": ("ID2", 96, 32), "re": ("ID3", 32, 32)}


class TestChat:
    def test_a_name_always_gets_the_same_colour(self):
        assert view.nick_colour("alice") == view.nick_colour("alice")

    def test_every_colour_is_one_the_stylesheet_has(self):
        names = [f"viewer{number}" for number in range(200)] + ["", "x", "ěščřžýáíé", "🤡"]

        assert {view.nick_colour(name) for name in names} == set(range(1, view.NICK_COLOURS + 1))

    def test_plain_text_is_one_part(self):
        assert view.chat_parts("what was that") == [{"text": "what was that"}]

    def test_a_native_emote_becomes_its_name_and_where_its_picture_is(self):
        assert view.chat_parts("[emote:37226:KEKW]") == [
            {"emote": "KEKW", "image": "https://files.kick.com/emotes/37226/fullsize", "width": view.EMOTE_HEIGHT}
        ]

    def test_text_and_emotes_keep_their_order(self):
        parts = view.chat_parts("he fell [emote:1:KEKW][emote:2:emojiLol] off the chair")

        assert [part.get("text") or part["emote"] for part in parts] == [
            "he fell ",
            "KEKW",
            "emojiLol",
            " off the chair",
        ]
        assert [part["image"] for part in parts if "emote" in part] == [
            view.KICK_EMOTE_IMAGE.format(id=1),
            view.KICK_EMOTE_IMAGE.format(id=2),
        ]

    def test_an_emotes_id_holds_nothing_that_could_lead_elsewhere(self):
        # The id ends up in an address the page loads a picture from.
        assert view.chat_parts("[emote:../../x:KEKW]") == [{"text": "[emote:../../x:KEKW]"}]
        assert view.chat_parts("[emote:a/b:KEKW]") == [{"text": "[emote:a/b:KEKW]"}]

    def test_a_twitch_emote_has_its_picture_from_twitch(self):
        assert view.chat_parts("[emote:twitch.emotesv2_abc123:LUL]") == [
            {
                "emote": "LUL",
                "image": "https://static-cdn.jtvnw.net/emoticons/v2/emotesv2_abc123/default/dark/2.0",
                "width": view.EMOTE_HEIGHT,
            }
        ]

    def test_an_emote_of_a_platform_without_pictures_is_its_name(self):
        assert view.chat_parts("ha [emote:elsewhere.12:KEKW]") == [{"text": "ha "}, {"text": "KEKW"}]

    @pytest.mark.parametrize(
        ("picture", "source"),
        [
            ("https://files.kick.com/emotes/37226/fullsize", "Kick"),
            ("https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/2.0", "Twitch"),
            ("https://cdn.7tv.app/emote/ID1/2x.webp", "7TV"),
        ],
    )
    def test_whose_emote_a_picture_is(self, picture, source):
        assert view.emote_source(picture) == source

    def test_something_that_only_looks_like_an_emote_stays_text(self):
        assert view.chat_parts("[emote:KEKW] [emote:12:]") == [{"text": "[emote:KEKW] [emote:12:]"}]

    def test_an_empty_message_has_no_parts(self):
        assert view.chat_parts("") == []
        assert view.chat_parts("", EMOTES) == []

    def words(self, content: str, emotes=None) -> list:
        """Text parts as strings, emotes as (name,) tuples."""
        parts = view.chat_parts(content, EMOTES if emotes is None else emotes)
        return [part["text"] if "text" in part else (part["emote"],) for part in parts]

    def test_a_channels_7tv_emote_becomes_its_picture(self):
        assert view.chat_parts("KEKW", EMOTES) == [
            {"emote": "KEKW", "image": "https://cdn.7tv.app/emote/ID1/2x.webp", "width": view.EMOTE_HEIGHT}
        ]

    def test_the_same_word_is_whatever_emote_the_channel_has_under_it(self):
        (here,) = view.chat_parts("KEKW", {"KEKW": ("HERE", 32, 32)})
        (there,) = view.chat_parts("KEKW", {"KEKW": ("THERE", 32, 32)})

        assert here["image"] == view.SEVENTV_EMOTE_IMAGE.format(id="HERE")
        assert there["image"] == view.SEVENTV_EMOTE_IMAGE.format(id="THERE")

    def test_without_the_channels_emotes_the_word_stays_a_word(self):
        assert view.chat_parts("KEKW") == [{"text": "KEKW"}]
        assert view.chat_parts("KEKW", {}) == [{"text": "KEKW"}]

    def test_7tv_emotes_are_picked_out_of_the_text_around_them(self):
        assert self.words("he fell KEKW off the chair KEKW") == ["he fell ", ("KEKW",), " off the chair ", ("KEKW",)]

    def test_emotes_in_a_row_keep_the_spaces_between_them(self):
        assert self.words("KEKW KEKW  WideHard") == [("KEKW",), " ", ("KEKW",), "  ", ("WideHard",)]

    def test_an_emote_is_a_whole_word_spelled_exactly_like_its_name(self):
        # "re" is a real emote of the channel, and a part of ordinary words.
        for content in ("kekw", "Kekw", "KEKW!", "KEKWW", "xKEKW", "treba", "re:"):
            assert self.words(content) == [content]
        assert self.words("re") == [("re",)]

    def test_native_and_7tv_emotes_keep_their_order(self):
        assert self.words("[emote:1:emojiLol] KEKW ha [emote:2:KEKW]KEKW") == [
            ("emojiLol",),
            " ",
            ("KEKW",),
            " ha ",
            ("KEKW",),
            ("KEKW",),
        ]

    def test_a_native_emotes_own_name_is_not_looked_up_as_a_7tv_one(self):
        (part,) = view.chat_parts("[emote:5:KEKW]", EMOTES)

        assert part["image"] == view.KICK_EMOTE_IMAGE.format(id=5)

    def test_an_emote_is_as_wide_as_its_shape_makes_it(self):
        def width(size: tuple[int, int]) -> int:
            (part,) = view.chat_parts("KEKW", {"KEKW": ("ID", *size)})
            return part["width"]

        assert width((32, 32)) == view.EMOTE_HEIGHT
        assert width((96, 32)) == 3 * view.EMOTE_HEIGHT
        assert width((16, 32)) == view.EMOTE_HEIGHT // 2
        # Unknown shape: square.
        assert width((0, 0)) == width((32, 0)) == width((0, 32)) == view.EMOTE_HEIGHT

    def test_a_very_wide_emote_does_not_take_over_the_line(self):
        (part,) = view.chat_parts("KEKW", {"KEKW": ("ID", 3200, 32)})
        (sliver,) = view.chat_parts("KEKW", {"KEKW": ("ID", 1, 3200)})

        assert part["width"] == view.EMOTE_MAX_WIDTH
        assert sliver["width"] == 1


class TestQueueGroups:
    def test_moments_of_one_stream_form_one_group(self):
        # A stream that went live at 20:00, with moments one and two hours in.
        rows = [
            row(id=2, detected_at=local(22, 0).isoformat(), stream_elapsed_seconds=7200),
            row(id=1, detected_at=local(21, 0).isoformat(), stream_elapsed_seconds=3600),
        ]

        (group,) = view.queue_groups(rows, TODAY)

        assert (group["channel"], group["when"]) == ("some_channel", "today, from 20:00")
        assert [item["id"] for item in group["rows"]] == [2, 1]

    def test_a_row_says_when_in_the_stream_what_and_how_it_was_rated(self):
        rows = [row(id=7, detected_at=local(21, 2).isoformat(), stream_elapsed_seconds=3723, rating=4)]

        ((item,),) = (group["rows"] for group in view.queue_groups(rows, TODAY))

        assert item == {
            "id": 7,
            "time": "1:02:03",
            "time_title": "Today at 21:02",
            "what": "Laughing",
            "unnamed": "Laughing",
            "has_clip": True,
            "downloaded": False,
            "rating": 4,
            "rating_words": "rated 4 of 5",
        }

    def test_a_row_says_what_chat_said_most_where_that_is_known(self):
        rows = [row(id=2, reason="laugh"), row(id=1, reason="emotes")]

        (group,) = view.queue_groups(rows, TODAY, {2: "KEKW \N{MULTIPLICATION SIGN}31", 1: None})

        assert [item["what"] for item in group["rows"]] == ["KEKW \N{MULTIPLICATION SIGN}31", "Emotes"]

    def test_the_time_a_stream_went_live_may_differ_by_a_little(self):
        # Worked out from two moments it never agrees to the second.
        rows = [
            row(id=2, detected_at=local(22, 0).isoformat(), stream_elapsed_seconds=7205),
            row(id=1, detected_at=local(21, 0).isoformat(), stream_elapsed_seconds=3600),
        ]

        assert len(view.queue_groups(rows, TODAY)) == 1

    def test_a_channel_that_went_live_again_starts_a_new_group(self):
        rows = [
            row(id=2, detected_at=local(22, 0).isoformat(), stream_elapsed_seconds=600),
            row(id=1, detected_at=local(15, 0).isoformat(), stream_elapsed_seconds=3600),
        ]

        groups = view.queue_groups(rows, TODAY)

        assert [group["when"] for group in groups] == ["today, from 21:50", "today, from 14:00"]

    def test_channels_live_at_the_same_time_are_kept_apart_newest_group_first(self):
        rows = [
            row(id=4, channel_slug="channel_b", detected_at=local(22, 30).isoformat(), stream_elapsed_seconds=1800),
            row(id=3, channel_slug="channel_a", detected_at=local(22, 0).isoformat(), stream_elapsed_seconds=7200),
            row(id=2, channel_slug="channel_b", detected_at=local(21, 30).isoformat(), stream_elapsed_seconds=5400),
            row(id=1, channel_slug="channel_a", detected_at=local(21, 0).isoformat(), stream_elapsed_seconds=3600),
        ]

        groups = view.queue_groups(rows, TODAY)

        assert [(group["channel"], [item["id"] for item in group["rows"]]) for group in groups] == [
            ("channel_b", [4]),
            ("channel_a", [3, 1]),
            ("channel_b", [2]),
        ]

    def test_a_stream_is_dated_by_the_day_it_went_live(self):
        # Live since 23:00 the evening before; the moment came after midnight.
        rows = [row(detected_at=local(0, 30).isoformat(), stream_elapsed_seconds=5400)]

        (group,) = view.queue_groups(rows, TODAY)

        assert group["when"] == "yesterday, from 23:00"

    def test_moments_without_a_stream_time_are_grouped_by_channel_and_day(self):
        rows = [
            row(id=3, detected_at=local(21, 0).isoformat(), stream_elapsed_seconds=None),
            row(id=2, detected_at=local(9, 0).isoformat(), stream_elapsed_seconds=None),
            row(id=1, detected_at=local(21, 0, days_ago=1).isoformat(), stream_elapsed_seconds=None),
        ]

        groups = view.queue_groups(rows, TODAY)

        assert [(group["when"], [item["id"] for item in group["rows"]]) for group in groups] == [
            ("today", [3, 2]),
            ("yesterday", [1]),
        ]

    def test_such_a_row_shows_the_time_of_day_instead(self):
        rows = [row(detected_at=local(21, 47).isoformat(), stream_elapsed_seconds=None)]

        (group,) = view.queue_groups(rows, TODAY)

        assert group["rows"][0]["time"] == "21:47"

    def test_a_moment_without_a_stream_time_does_not_join_a_known_stream(self):
        rows = [
            row(id=2, detected_at=local(22, 0).isoformat(), stream_elapsed_seconds=None),
            row(id=1, detected_at=local(21, 0).isoformat(), stream_elapsed_seconds=3600),
        ]

        assert [group["when"] for group in view.queue_groups(rows, TODAY)] == ["today", "today, from 20:00"]

    def test_no_rows_no_groups(self):
        assert view.queue_groups([], TODAY) == []


class TestReviewUrl:
    def test_the_plain_address_is_the_unrated_list(self):
        assert view.review_url() == "/dashboard"
        assert view.review_url(view.SHOW_UNRATED, None, offset=0) == "/dashboard"

    def test_only_what_differs_from_that_is_spelled_out(self):
        assert view.review_url(view.SHOW_BEST) == "/dashboard?show=best"
        assert view.review_url(view.SHOW_ALL, "channel_a", offset=50, moment=7) == (
            "/dashboard?show=all&channel=channel_a&offset=50&moment=7"
        )

    def test_a_channel_name_is_made_safe_for_an_address(self):
        assert view.review_url(channel='a&b="c"') == "/dashboard?channel=a%26b%3D%22c%22"


class TestAnEmptyList:
    def test_before_the_first_moment_there_is_nothing_yet(self):
        for show in view.SHOWS:
            assert view.empty_queue_words(show, None, None, TODAY) == {
                "queue": "No moments yet",
                "heading": "No moments yet",
            }

    def test_with_everything_rated_it_says_since_when_nothing_is_new(self):
        words = view.empty_queue_words(view.SHOW_UNRATED, None, local(2, 14), TODAY)

        assert words == {"queue": "No unrated moments", "heading": "Nothing new since 02:14"}

    def test_an_empty_best_list_explains_what_would_be_on_it(self):
        words = view.empty_queue_words(view.SHOW_BEST, None, local(2, 14), TODAY)

        assert words["heading"] == f"No clips rated {view.BEST_RATING_MIN} or higher yet"
        assert words["lead"] == f"A clip you rate {view.BEST_RATING_MIN} or higher is listed here."

    def test_a_chosen_channel_is_named(self):
        assert view.empty_queue_words(view.SHOW_ALL, "channel_a", None, TODAY)["heading"] == (
            "No moments from channel_a yet"
        )
        assert view.empty_queue_words(view.SHOW_UNRATED, "channel_a", local(21, 0, days_ago=1), TODAY)["heading"] == (
            "Nothing new from channel_a since yesterday, 21:00"
        )


class TestTheServiceInWords:
    @pytest.mark.parametrize(
        ("recording", "tracked", "words"),
        [
            (0, 0, "No channels watched"),
            (0, 1, "1 channel watched"),
            (0, 5, "5 channels watched"),
            (1, 1, "1 of 1 channel"),
            (2, 5, "2 of 5 channels"),
        ],
    )
    def test_beside_the_lamp(self, recording, tracked, words):
        assert view.channel_count_words(recording, tracked) == words

    @pytest.mark.parametrize(
        ("state", "words"),
        [
            ({"live": True}, "Live"),
            ({"live": True, "recorded": True}, "Recording"),
            ({"live": False}, "Not live"),
            # Its recording is waiting for the stream; nothing is coming in.
            ({"live": False, "recorded": True}, "Not live"),
            ({"live": False, "looked_at": False}, "Not watched"),
        ],
    )
    def test_the_lamp_itself(self, state, words):
        # "Recording" is kept for a stream that is being kept whole.
        assert view.now_words(**state) == words

    def state(self, **overrides) -> str:
        values = {"recording": 0, "tracked": 3, "watchlist": 3, "watching": True, "shutting_down": False}
        values.update(overrides)
        return view.service_words(**values)

    def test_while_channels_are_live_it_says_how_many(self):
        assert self.state(recording=2).startswith("2 of the 3 watched channels are live.")
        assert self.state(recording=1).startswith("1 of the 3 watched channels is live.")

    def test_while_nothing_is_live_it_says_so_and_what_happens_next(self):
        words = self.state()

        assert words.startswith("None of the 3 watched channels is live right now.")
        assert words.endswith("When one goes live and its chat erupts, the moment lands in the queue.")

    def test_a_single_watched_channel_is_not_counted(self):
        assert self.state(tracked=1, watchlist=1, recording=1).startswith("The watched channel is live.")
        assert self.state(tracked=1, watchlist=1).startswith("The watched channel is not live right now.")

    def test_it_never_calls_a_watched_channel_recorded(self):
        # That word is for a stream that is being kept whole.
        for recording in (0, 1, 2):
            assert "record" not in self.state(recording=recording)

    def test_an_empty_watchlist_points_to_where_to_add_a_channel(self):
        assert self.state(tracked=0, watchlist=0) == (
            "No channels are on the watchlist yet. Add one under Channels to start."
        )

    def test_watching_switched_off_explains_the_silence(self):
        assert self.state(watching=False) == "Watching is off. Nothing new arrives until you turn it on."

    def test_so_does_every_channel_being_paused(self):
        assert self.state(tracked=0) == "Tracking is off for every channel on the watchlist."

    def test_a_shutdown_comes_before_everything_else(self):
        assert self.state(recording=2, shutting_down=True) == (
            "The service is shutting down, so nothing new is being watched."
        )

    @pytest.mark.parametrize(
        ("pending", "words"),
        [
            (0, "Nothing new is being watched."),
            (
                1,
                "Waiting for 1 job to finish (clips being cut or analysed, recordings being saved). "
                "Nothing new is being watched.",
            ),
            (
                2,
                "Waiting for 2 jobs to finish (clips being cut or analysed, recordings being saved). "
                "Nothing new is being watched.",
            ),
        ],
    )
    def test_what_a_shutdown_is_waiting_for(self, pending, words):
        assert view.shutdown_words(pending) == words


class TestACutInWords:
    """What the Trim dialog says of the stretch it was asked to cut out."""

    def test_nothing_is_said_before_one_was_asked_for(self):
        assert view.trim_words(view.TRIM_NONE) == ""

    @pytest.mark.parametrize(
        "state",
        [
            view.TRIM_CUTTING,
            view.TRIM_CHAT,
            view.TRIM_BURNING,
            view.TRIM_READY,
            view.TRIM_FAILED,
            view.TRIM_CHAT_FAILED,
        ],
    )
    def test_every_other_state_is_a_sentence(self, state):
        words = view.trim_words(state)

        assert words[0].isupper() and words.endswith(".")

    def test_the_states_in_which_more_is_to_come(self):
        assert view.TRIM_BUSY == {view.TRIM_CUTTING, view.TRIM_CHAT, view.TRIM_BURNING}

    def test_chat_being_made_says_how_far_it_has_got(self):
        assert view.trim_words(view.TRIM_BURNING, done=0.427) == "Making the video with its chat in it: 42 %."
        assert view.trim_words(view.TRIM_CHAT, done=0.5) == "The video is ready. Making its chat: 50 %."

    @pytest.mark.parametrize("state", [view.TRIM_CUTTING, view.TRIM_READY, view.TRIM_FAILED])
    def test_nothing_else_says_how_far_it_has_got(self, state):
        assert view.trim_words(state, done=0.5) == view.trim_words(state)

    def test_a_cut_with_its_chat_drawn_in_says_so_once_it_is_ready(self):
        assert view.trim_words(view.TRIM_READY, burned=True) == "Ready to download, with its chat in it."
        assert view.trim_words(view.TRIM_READY, stretch=(5025, 5071), burned=True) == (
            "1:23:45 to 1:24:31 is ready to download, with its chat in it."
        )
        assert view.trim_words(view.TRIM_BURNING, burned=True) == view.trim_words(view.TRIM_BURNING)

    @pytest.mark.parametrize("state", [view.TRIM_CHAT, view.TRIM_READY, view.TRIM_CHAT_FAILED])
    def test_a_video_that_begins_before_the_start_that_was_set_says_by_how_much(self, state):
        words = view.trim_words(state, early=1.26)

        assert words.startswith(view.trim_words(state))
        assert words.endswith(" It begins 1.3 s before the start you set, on a frame a cut can begin on.")

    def test_one_that_begins_where_it_was_set_says_nothing_of_it(self):
        assert view.trim_words(view.TRIM_READY, early=0.04) == view.trim_words(view.TRIM_READY)

    @pytest.mark.parametrize("state", [view.TRIM_NONE, view.TRIM_CUTTING, view.TRIM_FAILED])
    def test_nor_does_a_state_in_which_there_is_no_video(self, state):
        assert view.trim_words(state, early=3.0) == view.trim_words(state)

    def test_a_cut_of_a_recording_that_is_ready_is_named_by_the_stretch_it_holds(self):
        # Several are cut from one stream; which one this is has to be said.
        assert view.trim_words(view.TRIM_READY, stretch=(5025, 5071)) == "1:23:45 to 1:24:31 is ready to download."

    def test_and_still_says_how_much_earlier_it_begins(self):
        words = view.trim_words(view.TRIM_READY, early=1.26, stretch=(5025, 5071))

        assert words == (
            "1:23:45 to 1:24:31 is ready to download. "
            "It begins 1.3 s before the start you set, on a frame a cut can begin on."
        )

    @pytest.mark.parametrize(
        "state", [view.TRIM_NONE, view.TRIM_CUTTING, view.TRIM_CHAT, view.TRIM_FAILED, view.TRIM_CHAT_FAILED]
    )
    def test_any_other_state_of_one_reads_as_it_does_of_a_moment(self, state):
        assert view.trim_words(state, stretch=(5025, 5071)) == view.trim_words(state)


class TestSettingsInWords:
    def test_nothing_is_said_while_every_channel_goes_by_the_settings_for_all(self):
        assert view.own_settings_words([]) is None

    def test_one_channel_with_its_own_is_named(self):
        assert view.own_settings_words(["channel_a"]) == "channel_a has settings of its own on top of these."

    def test_so_are_several(self):
        assert view.own_settings_words(["channel_a", "channel_b", "channel_c"]) == (
            "channel_a, channel_b and channel_c have settings of their own on top of these."
        )


class TestATrialInWords:
    FIRST = datetime(2026, 3, 1, 18, 2)
    LAST = datetime(2026, 3, 1, 21, 31)
    TODAY = date(2026, 3, 2)

    @dataclass
    class Came:
        """What a replay came to (see replay.Summary)."""

        moments: int = 12
        per_hour: float = 3.4286
        not_extended: int = 7
        median_clip_seconds: float | None = 35.0
        longest_clip_seconds: float | None = 58.6

    def words(self, *rows, messages: int = 8000, hours: float = 3.5) -> dict:
        return view.trial_words(messages, hours, self.FIRST, self.LAST, self.TODAY, list(rows))

    def test_says_how_much_chat_was_replayed_and_from_when(self):
        assert (
            self.words()["summary"]
            == "8000 messages over 3.5 hours of chat, from yesterday, 18:02 to yesterday, 21:31."
        )

    def test_an_hour_is_an_hour(self):
        assert "1 message over 1 hour of chat" in self.words(messages=1, hours=1.0)["summary"]

    def test_each_set_of_settings_is_a_row_of_what_it_came_to(self):
        (row,) = self.words(("As saved", self.Came()))["rows"]

        assert row == {
            "label": "As saved",
            "moments": "12",
            "per_hour": "3.4",
            "not_extended": "7",
            "median": "35 s",
            "longest": "59 s",
        }

    def test_settings_that_make_no_moment_have_no_clip_to_measure(self):
        none = self.Came(moments=0, per_hour=0.0, not_extended=0, median_clip_seconds=None, longest_clip_seconds=None)

        rows = self.words(("As saved", self.Came()), ("With these values", none))["rows"]

        assert [row["label"] for row in rows] == ["As saved", "With these values"]
        assert (rows[1]["moments"], rows[1]["per_hour"], rows[1]["median"], rows[1]["longest"]) == ("0", "0", "-", "-")


class TestMomentsFoundInARecordingInWords:
    """What the Recordings page says of the moments looked for in a
    recording's chat (see recording_moments.py)."""

    def found(self, **figures) -> str:
        figures = {"found": 3, "messages": 8000, "looked": local(21, 47), "today": TODAY, **figures}
        return view.find_words(view.FIND_FOUND, **figures)

    def test_before_anything_was_looked_for_it_says_what_looking_does(self):
        words = view.find_words(view.FIND_NONE)

        assert words.startswith("Replays the chat stored with this recording through the detector")
        assert "as they are saved" in words

    def test_while_the_chat_is_read_there_is_nothing_to_count_yet(self):
        assert view.find_words(view.FIND_LOOKING) == "Reading the recording's chat."

    def test_while_it_is_replayed_it_says_how_far_that_has_got(self):
        assert view.find_words(view.FIND_LOOKING, done=1500, of=36108) == (
            "Replaying the recording's chat: 1500 of 36108 messages. A long stream with a busy chat takes minutes."
        )

    def test_a_replay_that_failed_points_at_the_log(self):
        assert view.find_words(view.FIND_FAILED) == (
            "The recording's chat could not be replayed. The server log says why."
        )

    def test_what_was_found_is_counted_with_the_chat_it_was_found_in_and_when(self):
        assert self.found() == (
            "3 moments in the recording's chat (8000 messages), under the settings as they were saved at 21:47."
        )

    def test_one_is_one(self):
        assert self.found(found=1, messages=1).startswith("1 moment in the recording's chat (1 message), ")

    def test_looked_for_on_another_day_it_says_which(self):
        assert self.found(looked=local(21, 47, days_ago=1)).endswith("as they were saved at yesterday, 21:47.")

    @pytest.mark.parametrize(
        ("found", "already", "words"),
        [
            (3, 1, "1 of them is a moment already."),
            (3, 2, "2 of them are moments already."),
            (3, 3, "All of them are moments already."),
            (1, 1, "It is a moment already."),
        ],
    )
    def test_it_says_how_many_of_them_are_moments_already(self, found, already, words):
        assert self.found(found=found, already=already).endswith(f" {words}")

    def test_nothing_found_is_said_of_the_chat_and_the_settings(self):
        assert self.found(found=0) == (
            "Nothing in the recording's chat (8000 messages) would have been a moment "
            "under the settings as they were saved at 21:47."
        )

    def test_a_recording_without_chat_says_so(self):
        assert self.found(found=0, messages=0) == (
            "No chat was stored for this recording, so there is nothing to look for moments in."
        )

    @pytest.mark.parametrize(
        ("missing", "words"),
        [
            (1, "1 more fell where the recording has too little footage of it."),
            (4, "4 more fell where the recording has too little footage of them."),
        ],
    )
    def test_moments_the_recording_has_no_footage_of_are_counted_last(self, missing, words):
        assert self.found(missing=missing).endswith(f" {words}")
        assert self.found(found=0, missing=missing).endswith(f" {words}")

    def test_while_clips_are_cut_it_says_which_one(self):
        assert view.find_words(view.FIND_CUTTING, done=0, of=5) == "Cutting clip 1 of 5."
        assert view.find_words(view.FIND_CUTTING, done=4, of=5) == "Cutting clip 5 of 5."
        # The last one is done, and the page has not been told yet.
        assert view.find_words(view.FIND_CUTTING, done=5, of=5) == "Cutting clip 5 of 5."

    def test_once_they_are_cut_it_says_how_that_went_first(self):
        assert self.found(made=2, already=2).startswith("2 clips made, and in the review queue. 3 moments in ")
        assert self.found(made=1, already=1).startswith("1 clip made, and in the review queue. ")
        assert self.found(made=1, failed=2, already=1).startswith(
            "1 clip made, and in the review queue. 2 clips could not be cut. The server log says why. 3 moments"
        )
        assert self.found(failed=1).startswith("1 clip could not be cut. The server log says why. ")

    def test_the_states_in_which_more_is_to_come(self):
        assert view.FIND_BUSY == {view.FIND_LOOKING, view.FIND_CUTTING}

    def test_a_found_moment_is_what_set_it_off_how_strongly_and_how_long_its_clip_is(self):
        assert view.found_words("laugh", 7.25, 41.4) == "Laughing, score 7.2, a clip of 41 seconds"
        assert view.found_words("emote_mention,message_rate", 12.0, 95.0) == (
            "Emote names, score 12, a clip of 1 minute 35 seconds"
        )


class TestRecordingsInWords:
    """What the Channels and Recordings pages say of a stream recording."""

    @pytest.mark.parametrize(
        ("size", "words"),
        [
            (0, "0 MB"),
            (300_000, "0 MB"),
            (512 * 1024**2, "512 MB"),
            (999 * 1024**2, "999 MB"),
            (1000 * 1024**2, "1 GB"),
            (1024**3, "1 GB"),
            (int(4.26 * 1024**3), "4.3 GB"),
            (225 * 1024**3, "225 GB"),
        ],
    )
    def test_how_much_room_something_takes(self, size, words):
        assert view.size_words(size) == words

    @pytest.mark.parametrize(
        ("seconds", "words"),
        [
            (0, "less than a second"),
            (0.4, "less than a second"),
            (1, "1 second"),
            (38, "38 seconds"),
            (60, "1 minute"),
            (92, "1 minute 32 seconds"),
            (600, "10 minutes"),
            (3600, "1 hour"),
            (3725, "1 hour 2 minutes"),
            (7259, "2 hours"),
        ],
    )
    def test_a_length_of_time_is_written_out(self, seconds, words):
        # To the second up to an hour, to the minute past it.
        assert view.duration_words(seconds) == words

    @pytest.mark.parametrize(
        ("state", "words"),
        [
            ({"running": True}, "Waiting for the stream"),
            ({"running": True, "on_air": True}, "Starting"),
            ({"running": True, "on_air": True, "seconds": 5025.4, "size": 4_500_000_000}, "1:23:45 recorded, 4.2 GB"),
            ({"running": True, "seconds": 5025.4, "size": 4_500_000_000}, "1:23:45 recorded, the stream is off"),
            ({"running": False, "seconds": 5025.4}, "Saving 1:23:45"),
            ({"running": False}, "Saving"),
        ],
    )
    def test_what_a_recording_that_is_not_over_is_doing(self, state, words):
        assert view.recording_state(7, **state) == {"id": 7, "running": state["running"], "words": words}

    def test_a_row_of_the_list_says_what_is_being_done_or_that_nothing_came_of_it(self):
        running = view.recording_state(7, running=True)

        assert view.recording_row_words("recording", running) == "Waiting for the stream"
        assert view.recording_row_words("failed", None) == "Not saved"
        assert view.recording_row_words("ready", None) is None

    def test_stopping_one_answers_with_whether_there_is_anything_to_save(self):
        assert view.RECORDING_STOPPED_WORDS[True] == "Saving the recording. It will be on the Recordings page."
        assert view.RECORDING_STOPPED_WORDS[False] == "Nothing had been recorded yet."

    @pytest.mark.parametrize(
        ("seconds", "words"), [(3, "Chat 3 s earlier"), (0, "Chat as it arrived"), (-2, "Chat 2 s later")]
    )
    def test_where_chat_is_put_against_the_picture(self, seconds, words):
        assert view.chat_delay_words(seconds) == words

    def test_when_a_recording_was_on_air(self):
        assert view.recording_span_words(local(21, 4), local(23, 31), TODAY) == "Today, 21:04 to 23:31"
        assert view.recording_span_words(local(21, 4, days_ago=1), local(1, 12), TODAY) == "Yesterday, 21:04 to 01:12"
        assert view.recording_span_words(local(21, 4, days_ago=3), local(23, 31, days_ago=3), TODAY) == (
            "Tuesday 6 October, 21:04 to 23:31"
        )

    def test_one_that_has_not_ended_only_says_when_it_began(self):
        assert view.recording_span_words(local(21, 4), None, TODAY) == "Today, 21:04"

    def summary(self, size=None, parts=1, missing=0.0, ended_by=None) -> str:
        return view.recording_summary_words(size, parts, missing, ended_by, 600)

    def test_a_recording_that_went_as_asked_only_says_how_big_it_is(self):
        assert self.summary(size=3 * 1024**3, ended_by="stopped") == "3 GB."

    def test_one_the_stream_broke_off_in_says_how_often_and_what_is_missing(self):
        assert self.summary(parts=3, missing=92.4) == (
            "The recording has 2 breaks, where the file is missing 1 minute 32 seconds of the stream."
        )
        assert self.summary(parts=2, missing=0.2) == (
            "The recording has 1 break, where the file is missing less than a second of the stream."
        )

    def test_one_that_was_not_stopped_by_hand_says_why_it_ended(self):
        assert self.summary(ended_by="stream_ended") == (
            "It ended by itself once the stream had been gone for 10 minutes."
        )
        assert self.summary(size=1024**3, parts=2, missing=30, ended_by="disk_full") == (
            "1 GB. The recording has 1 break, where the file is missing 30 seconds of the stream. "
            "It was ended because the disk was running out of room."
        )

    def test_with_nothing_known_nothing_is_said(self):
        assert self.summary() == ""
        assert self.summary(ended_by="a reason from the future") == ""

    def test_every_way_a_recording_can_end_has_its_words(self):
        from kick_clip_hunter import stream_recording

        reasons = {
            stream_recording.STOPPED,
            stream_recording.STREAM_ENDED,
            stream_recording.DISK_FULL,
            stream_recording.WRITE_FAILED,
            stream_recording.SHUTDOWN,
            stream_recording.INTERRUPTED,
        }

        assert set(view.RECORDING_ENDED_WORDS) == reasons
        for reason in reasons - {stream_recording.STOPPED}:
            words = view.RECORDING_ENDED_WORDS[reason]
            assert words[0].isupper() and words.endswith(".")

    def test_how_much_room_the_disk_has_left(self):
        assert view.free_room_words(225 * 1024**3) == "225 GB free on the disk recordings are kept on."


class TestRecordingAlwaysInWords:
    def test_stopping_a_recording_says_what_became_of_it(self):
        assert view.recording_stopped_words(True, False) == "Saving the recording. It will be on the Recordings page."
        assert view.recording_stopped_words(False, False) == "Nothing had been recorded yet."

    def test_for_a_channel_recorded_always_it_also_says_that_this_did_not_switch_that_off(self):
        assert view.recording_stopped_words(True, True) == (
            "Saving the recording. It will be on the Recordings page. The channel's next stream is recorded again."
        )
        assert view.recording_stopped_words(False, True) == (
            "Nothing had been recorded yet. The channel's next stream is recorded again."
        )

    def test_the_switch_says_what_it_does_either_way(self):
        assert view.ALWAYS_RECORD_WORDS[True].format(channel="some_channel") == (
            "Every stream of some_channel is recorded from when it goes live. "
            "Stopping a recording ends it for that stream."
        )
        assert view.ALWAYS_RECORD_WORDS[False].format(channel="some_channel") == (
            "Streams of some_channel are recorded only when you start a recording."
        )
