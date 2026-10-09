"""The dashboard's wording: what the pages say, worked out from stored rows.

All of it is pure, so each rule gets a test that names it - how a day, a
count or a detector reason is put into words, how the queue is grouped by
stream, what an empty list and an idle service are described as.

Times are built in the machine's own time zone and compared on its own
clock, so nothing here depends on which zone the suite runs in.
"""

from __future__ import annotations

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


class TestChat:
    def test_a_name_always_gets_the_same_colour(self):
        assert view.nick_colour("alice") == view.nick_colour("alice")

    def test_every_colour_is_one_the_stylesheet_has(self):
        names = [f"viewer{number}" for number in range(200)] + ["", "x", "ěščřžýáíé", "🤡"]

        assert {view.nick_colour(name) for name in names} == set(range(1, view.NICK_COLOURS + 1))

    def test_plain_text_is_one_part(self):
        assert view.chat_parts("what was that") == [{"text": "what was that"}]

    def test_a_native_emote_becomes_its_name(self):
        assert view.chat_parts("[emote:37226:KEKW]") == [{"emote": "KEKW"}]

    def test_text_and_emotes_keep_their_order(self):
        assert view.chat_parts("he fell [emote:1:KEKW][emote:2:emojiLol] off the chair") == [
            {"text": "he fell "},
            {"emote": "KEKW"},
            {"emote": "emojiLol"},
            {"text": " off the chair"},
        ]

    def test_something_that_only_looks_like_an_emote_stays_text(self):
        assert view.chat_parts("[emote:KEKW] [emote:12:]") == [{"text": "[emote:KEKW] [emote:12:]"}]

    def test_an_empty_message_has_no_parts(self):
        assert view.chat_parts("") == []

    def test_lines_carry_the_name_its_colour_and_the_parts(self):
        messages = [
            {"sender_username": "alice", "content": "xDDD"},
            {"sender_username": None, "content": None},
        ]

        assert view.chat_lines(messages) == [
            {"nick": "alice", "colour": view.nick_colour("alice"), "parts": [{"text": "xDDD"}]},
            {"nick": "", "colour": view.nick_colour(""), "parts": []},
        ]


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
            "rating": 4,
            "rating_words": "rated 4 of 5",
        }

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
    def test_beside_the_recording_lamp(self, recording, tracked, words):
        assert view.channel_count_words(recording, tracked) == words

    def state(self, **overrides) -> str:
        values = {"recording": 0, "tracked": 3, "watchlist": 3, "watching": True, "shutting_down": False}
        values.update(overrides)
        return view.service_words(**values)

    def test_while_recording_it_says_how_many_channels(self):
        assert self.state(recording=2).startswith("2 of the 3 watched channels are live and being recorded.")
        assert self.state(recording=1).startswith("1 of the 3 watched channels is live and being recorded.")

    def test_while_nothing_is_live_it_says_so_and_what_happens_next(self):
        words = self.state()

        assert words.startswith("None of the 3 watched channels is being recorded right now.")
        assert words.endswith("When one goes live and its chat erupts, the moment lands in the queue.")

    def test_a_single_watched_channel_is_not_counted(self):
        assert self.state(tracked=1, watchlist=1, recording=1).startswith("The watched channel is live")
        assert self.state(tracked=1, watchlist=1).startswith("The watched channel is not being recorded right now.")

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
            (1, "Waiting for 1 job to finish (clips being cut or analysed). Nothing new is being watched."),
            (2, "Waiting for 2 jobs to finish (clips being cut or analysed). Nothing new is being watched."),
        ],
    )
    def test_what_a_shutdown_is_waiting_for(self, pending, words):
        assert view.shutdown_words(pending) == words
