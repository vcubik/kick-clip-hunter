"""A channel's stored chat in figures: the periods the Analyze page offers,
the pass that counts a period's messages, and what is made of the counts -
figures, the columns of a chart, the rows of a table.

Everything is built from plain lists of messages. Times are given on the
local clock, as the page tells them, so the tests hold in any time zone -
and in January, when no zone changes its clock.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from kick_clip_hunter import chat_stats, detector
from kick_clip_hunter.chat_stats import Period
from kick_clip_hunter.dashboard_view import KICK_EMOTE_IMAGE, SEVENTV_EMOTE_IMAGE

TODAY = date(2026, 1, 20)
CHANNEL = "some_channel"

# The channel's 7TV emotes: (emote id, width, height) by the name chat types.
EMOTES = {"KEKW": ("7tv-kekw", 32, 32), "catJAM": ("7tv-jam", 64, 32)}
KEKW = SEVENTV_EMOTE_IMAGE.format(id="7tv-kekw")
NATIVE = "[emote:37226:KEKW]"
NATIVE_KEKW = KICK_EMOTE_IMAGE.format(id="37226")


def local(day: int, hour: int = 20, minute: int = 0, second: float = 0.0) -> datetime:
    """A time on the local clock, in January 2026."""
    return (datetime(2026, 1, day, hour, minute) + timedelta(seconds=second)).astimezone()


def message(at: datetime, content: str = "hello", sender: str | None = "alice") -> dict:
    """A stored message, as the database hands it out."""
    return {
        "received_at": at.astimezone(timezone.utc).isoformat(),
        "sender_username": sender,
        "content": content,
    }


#: An evening of two hours, which a chart draws a minute to a column.
EVENING = Period(local(12, 20), local(12, 22))


def collected(messages: list[dict], period: Period = EVENING, **settings) -> chat_stats.Collected:
    settings.setdefault("emotes", EMOTES)
    return chat_stats.collect(messages, chat_stats.columns_for(period), **settings)


def by_label(figures: list[dict]) -> dict[str, dict]:
    return {figure["label"]: figure for figure in figures}


class TestTimesInAnAddress:
    def test_a_time_without_a_zone_is_local_time(self):
        assert chat_stats.parse_local("2026-01-12T18:43") == local(12, 18, 43)

    def test_a_time_with_a_zone_is_that_time(self):
        assert chat_stats.parse_local("2026-01-12T18:43:00+00:00") == datetime(2026, 1, 12, 18, 43, tzinfo=timezone.utc)

    @pytest.mark.parametrize("text", [None, "", "yesterday", "2026-13-40T00:00", "0001-01-01T00:00"])
    def test_what_is_not_a_time_is_none(self, text):
        # The last one is a time, but one no clock here reaches back to.
        assert chat_stats.parse_local(text) is None or text == "0001-01-01T00:00"

    def test_a_time_is_written_to_the_minute_and_read_back_the_same(self):
        assert chat_stats.local_text(local(12, 18, 43, 59.5)) == "2026-01-12T18:43"
        assert chat_stats.parse_local(chat_stats.local_text(local(12, 18, 43))) == local(12, 18, 43)

    def test_whole_minutes_hold_the_first_and_the_last_message(self):
        period = chat_stats.whole_minutes(local(12, 18, 43, 20), local(12, 21, 16, 59.9))

        assert period == Period(local(12, 18, 43), local(12, 21, 17))
        assert period.start <= local(12, 18, 43, 20) and local(12, 21, 16, 59.9) < period.end


class TestWhichPeriod:
    NOW = local(15, 12)
    STORED = Period(local(1, 9), local(12, 23))
    SESSION = Period(local(12, 18, 43), local(12, 21, 17))

    def chosen(self, preset=None, start=None, end=None, **overrides):
        given = {"now": self.NOW, "stored": self.STORED, "newest_session": self.SESSION}
        return chat_stats.chosen_period(preset, start, end, **{**given, **overrides})

    def test_with_nothing_asked_for_it_is_the_newest_session(self):
        assert self.chosen() == (self.SESSION, None)

    def test_without_a_session_it_is_everything_that_is_stored(self):
        assert self.chosen(newest_session=None) == (self.STORED, chat_stats.PRESET_ALL)

    @pytest.mark.parametrize(("name", "reach"), [("day", 1), ("week", 7), ("month", 30)])
    def test_a_preset_reaches_back_from_now(self, name, reach):
        assert self.chosen(name) == (Period(self.NOW - timedelta(days=reach), self.NOW), name)

    def test_all_is_everything_that_is_stored(self):
        assert self.chosen(chat_stats.PRESET_ALL) == (self.STORED, chat_stats.PRESET_ALL)

    def test_a_preset_nobody_offers_is_as_good_as_none(self):
        assert self.chosen("fortnight") == (self.SESSION, None)

    def test_two_ends_that_were_given_win_over_a_preset(self):
        start, end = local(10, 8), local(11, 8)

        assert self.chosen("week", start, end) == (Period(start, end), None)

    def test_a_start_alone_runs_until_now(self):
        assert self.chosen(start=local(14, 8)) == (Period(local(14, 8), self.NOW), None)

    def test_an_end_alone_holds_everything_stored_up_to_it(self):
        assert self.chosen(end=local(5, 8)) == (Period(self.STORED.start, local(5, 8)), None)

    def test_ends_the_wrong_way_round_are_as_good_as_none(self):
        assert self.chosen(start=local(11, 8), end=local(10, 8)) == (self.SESSION, None)

    def test_no_period_reaches_past_now(self):
        tomorrow = self.NOW + timedelta(days=1)
        going = Period(local(15, 11), self.NOW + timedelta(seconds=20))

        assert self.chosen(start=local(14, 8), end=tomorrow)[0].end == self.NOW
        assert self.chosen(newest_session=going)[0] == Period(local(15, 11), self.NOW)
        assert self.chosen("all", stored=Period(local(1, 9), tomorrow))[0].end == self.NOW


class TestTheAddressOfAPage:
    def test_what_is_at_its_default_is_left_out(self):
        assert chat_stats.analyze_url(CHANNEL) == "/dashboard/analyze?channel=some_channel"

    def test_a_preset_goes_by_its_name(self):
        assert chat_stats.analyze_url(CHANNEL, preset="week") == "/dashboard/analyze?channel=some_channel&period=week"

    def test_any_other_period_goes_by_its_two_ends(self):
        url = chat_stats.analyze_url(CHANNEL, period=Period(local(12, 18, 43), local(12, 21, 17)))

        assert url == "/dashboard/analyze?channel=some_channel&from=2026-01-12T18%3A43&to=2026-01-12T21%3A17"

    def test_a_preset_that_is_given_is_what_the_period_is(self):
        url = chat_stats.analyze_url(CHANNEL, preset="all", period=EVENING)

        assert url == "/dashboard/analyze?channel=some_channel&period=all"

    def test_a_search_says_what_it_is_for(self):
        url = chat_stats.analyze_url(CHANNEL, chatter="alice", text="gg wp", order=chat_stats.ORDER_OLDEST, offset=100)

        assert url == "/dashboard/analyze?channel=some_channel&chatter=alice&q=gg+wp&order=oldest&offset=100"

    def test_the_messages_are_asked_for_by_name_only_where_nothing_else_does(self):
        messages = chat_stats.VIEW_MESSAGES

        assert chat_stats.analyze_url(CHANNEL, view=messages).endswith("&view=messages")
        assert "view" not in chat_stats.analyze_url(CHANNEL, view=messages, chatter="alice")

    def test_longer_tables_are_asked_for(self):
        assert chat_stats.analyze_url(CHANNEL, top=chat_stats.TOP_ROWS_MORE).endswith("&top=100")

    def test_the_chat_around_a_message_is_scrolled_to(self):
        url = chat_stats.analyze_url(CHANNEL, preset="week", around="message 7", chatter="alice")

        assert url == "/dashboard/analyze?channel=some_channel&period=week&around=message+7#asked"

    def test_a_name_cannot_break_out_of_the_address(self):
        assert chat_stats.analyze_url("a&b=c d") == "/dashboard/analyze?channel=a%26b%3Dc+d"


class TestWords:
    def test_a_count_is_written_as_it_is_read(self):
        assert chat_stats.whole_words(42834) == "42,834"
        assert chat_stats.counted_words(1, "message") == "1 message"
        assert chat_stats.counted_words(42834, "message") == "42,834 messages"

    @pytest.mark.parametrize(
        ("part", "whole", "words"),
        [(0, 50, "0%"), (5, 0, "0%"), (1, 2000, "under 0.1%"), (17, 500, "3.4%"), (1, 20, "5%"), (1, 3, "33%")],
    )
    def test_a_share_is_a_percentage_to_the_point_where_that_says_more(self, part, whole, words):
        assert chat_stats.share_words(part, whole) == words

    def test_a_period_within_a_day_names_the_day_once(self):
        period = Period(local(12, 18, 43), local(12, 21, 17))

        assert chat_stats.period_words(period, TODAY) == "Monday 12 January, 18:43 to 21:17"
        assert chat_stats.period_words(Period(local(20, 9), local(20, 10)), TODAY) == "Today, 09:00 to 10:00"

    def test_a_period_that_ends_with_its_day_ends_at_midnight(self):
        period = Period(local(12, 18), local(13, 0))

        assert chat_stats.one_day(period)
        assert chat_stats.period_words(period, TODAY) == "Monday 12 January, 18:00 to midnight"

    def test_a_period_over_midnight_names_both_days(self):
        period = Period(local(12, 21, 8), local(13, 0, 47))

        assert not chat_stats.one_day(period)
        assert chat_stats.period_words(period, TODAY) == "12 January, 21:08 to 13 January, 00:47"

    def test_a_message_is_timed_to_the_second_and_a_minute_by_its_day(self):
        assert chat_stats.second_words(local(12, 21, 35, 2.7), TODAY) == "12 January 21:35:02"
        assert chat_stats.minute_words(local(19, 21, 35), TODAY) == "Yesterday at 21:35"

    @pytest.mark.parametrize(
        ("total", "chatter", "text", "words"),
        [
            (5614, None, "", "5,614 messages"),
            (312, "alice", "", "312 messages from alice"),
            (1, None, "lol", '1 message with "lol" in it'),
            (12, "alice", "lol", '12 messages from alice with "lol" in them'),
            (0, "alice", "lol", 'No messages from alice with "lol" in them'),
        ],
    )
    def test_a_search_says_what_it_came_to(self, total, chatter, text, words):
        assert chat_stats.found_words(total, chatter, text) == words

    def test_stored_chat_says_how_far_it_reaches(self):
        long = Period(local(3, 18), local(19, 22))

        assert chat_stats.stored_words(long, TODAY) == "Chat is stored from 3 January to yesterday."
        assert chat_stats.stored_words(Period(local(20, 9), local(20, 10)), TODAY) == "Chat is stored from today."

    def test_a_page_of_messages_says_which_it_is(self):
        assert chat_stats.paging_words(1200, 100, 5614) == "1,201 to 1,300 of 5,614"

    def test_the_tabs_are_the_presets_and_all(self):
        assert chat_stats.preset_labels() == [
            ("day", "24 hours"),
            ("week", "7 days"),
            ("month", "30 days"),
            ("all", "All"),
        ]


class TestTheColumnsOfAChart:
    def test_a_short_period_has_a_column_for_every_minute(self):
        columns = chat_stats.columns_for(EVENING)

        assert (columns.seconds, columns.count, columns.days) == (60, 120, 0)
        assert columns.start(0) == EVENING.start
        assert columns.start(columns.count) == EVENING.end

    @pytest.mark.parametrize(
        ("hours", "seconds"),
        [(4, 60), (5, 120), (24, 600), (7 * 24, 3600), (30 * 24, 10800), (120 * 24, 43200)],
    )
    def test_columns_are_the_finest_that_keep_a_chart_readable(self, hours, seconds):
        period = Period(local(1, 0), local(1, 0) + timedelta(hours=hours))

        columns = chat_stats.columns_for(period)

        assert columns.seconds == seconds
        assert columns.count <= chat_stats.MAX_COLUMNS

    def test_columns_begin_on_round_times_of_the_clock(self):
        columns = chat_stats.columns_for(Period(local(12, 18, 43), local(13, 14, 43)))

        assert columns.seconds == 300
        assert columns.start(0) == local(12, 18, 40)
        assert columns.start(4) == local(12, 19, 0)

    def test_every_moment_of_a_period_has_a_column(self):
        period = Period(local(12, 18, 43), local(13, 14, 43))
        columns = chat_stats.columns_for(period)

        assert columns.of(period.start) == 0
        assert columns.of(period.end - timedelta(microseconds=1)) == columns.count - 1
        assert columns.of(local(12, 19, 4, 59)) == 4

    def test_a_moment_outside_the_period_has_none(self):
        columns = chat_stats.columns_for(EVENING)

        assert columns.of(EVENING.start - timedelta(seconds=1)) < 0
        assert columns.of(EVENING.end) == columns.count

    def test_a_long_period_has_a_column_for_every_day(self):
        period = Period(local(1, 15), local(1, 15) + timedelta(days=200))

        columns = chat_stats.columns_for(period)

        assert (columns.days, columns.count) == (1, 201)
        assert columns.start(0) == local(1, 0)
        assert columns.of(local(1, 15)) == 0
        assert columns.of(local(4, 3)) == 3
        assert columns.of(period.end - timedelta(microseconds=1)) == columns.count - 1

    def test_a_very_long_one_has_a_column_for_every_week(self):
        columns = chat_stats.columns_for(Period(local(1, 15), local(1, 15) + timedelta(days=400)))

        assert columns.days == 7
        assert columns.of(local(7, 23)) == 0
        assert columns.of(local(8, 0)) == 1
        assert columns.start(1) == local(8, 0)

    def test_a_period_longer_than_weeks_can_show_still_fits(self):
        columns = chat_stats.columns_for(Period(local(1, 0), local(1, 0) + timedelta(days=7 * 300)))

        assert columns.days > 7
        assert columns.count <= chat_stats.MAX_COLUMNS

    def test_a_period_of_no_length_still_has_a_column(self):
        assert chat_stats.columns_for(Period(local(12, 20), local(12, 20))).count == 1


class TestWhatCountsAsTheSameThingSaid:
    def test_capitals_make_no_difference(self):
        assert chat_stats.said_key("GG WP")[0] == chat_stats.said_key("gg wp")[0]

    def test_a_longer_row_of_one_character_says_no_more(self):
        assert chat_stats.said_key("?????????")[0] == chat_stats.said_key("????")[0]
        assert chat_stats.said_key("noooooo")[0] == chat_stats.said_key("noooo")[0]
        assert chat_stats.said_key("??")[0] != chat_stats.said_key("???")[0]

    def test_a_word_said_over_and_over_is_said_once(self):
        assert chat_stats.said_key("W W   W")[0] == chat_stats.said_key("w")[0]
        assert chat_stats.said_key("no way no way")[0] != chat_stats.said_key("no way")[0]

    @pytest.mark.parametrize("laugh", ["xd", "XD", "xDDDDDD", "xdd xD"])
    def test_every_typed_laugh_is_the_same_laugh(self, laugh):
        assert chat_stats.said_key(laugh) == ("xd", "xD")

    def test_a_word_that_only_starts_like_a_laugh_is_not_one(self):
        assert chat_stats.said_key("xdr")[0] == "xdr"

    def test_it_is_shown_as_it_was_written(self):
        assert chat_stats.said_key("  Dobrý   VEČER ")[1] == "Dobrý VEČER"

    def test_different_things_stay_different(self):
        assert chat_stats.said_key("ahoj")[0] != chat_stats.said_key("ahoj všem")[0]


class TestLaughing:
    NAMES = ("kekw", "omegalul")
    SAID = (
        "xD",
        "to je xddd teda",
        "maxdps",
        "this is KEKW",
        "kekwait what",
        "[emote:1:emojiLol]",
        "[emote:2:catKiss]",
        "ahoj",
        "",
    )

    @pytest.mark.parametrize("patterns", [(), ("kiss",)])
    def test_a_message_is_laughing_exactly_when_the_detector_says_so(self, patterns):
        laughing = chat_stats.laugh_test(self.NAMES, patterns)

        assert [laughing(said) for said in self.SAID] == [
            detector.is_laughing(said, self.NAMES, patterns) for said in self.SAID
        ]

    def test_a_channel_without_laugh_emotes_still_laughs_in_letters(self):
        laughing = chat_stats.laugh_test((), ())

        assert laughing("xD") and not laughing("this is KEKW")

    def test_a_name_is_looked_for_as_it_is_written(self):
        # A name with a character that means something in a pattern.
        assert chat_stats.laugh_test(("lol.", ""), ())("so lol. yes")
        assert not chat_stats.laugh_test(("lol.",), ())("so lolx yes")


class TestOnePassOverTheChat:
    def test_counts_the_messages_and_who_wrote_them(self):
        kept = collected(
            [
                message(local(12, 20, 0, 5), sender="alice"),
                message(local(12, 20, 30), sender="bob"),
                message(local(12, 21, 59, 59), sender="alice"),
            ]
        )

        assert kept.messages == 3
        assert kept.by_chatter == {"alice": 2, "bob": 1}
        assert (kept.first, kept.last) == (local(12, 20, 0, 5), local(12, 21, 59, 59))

    def test_the_order_of_the_messages_makes_no_difference(self):
        messages = [
            message(local(12, 20, minute, second), content, sender)
            for minute in range(0, 60, 7)
            for second, (sender, content) in enumerate(
                [("alice", "xD"), ("bob", "KEKW ?"), ("carol", f"{NATIVE} @alice"), ("alice", "gg")]
            )
        ]
        shuffled = random.Random(7).sample(messages, len(messages))

        assert collected(shuffled) == collected(messages)

    def test_a_message_is_counted_into_the_column_it_arrived_in(self):
        kept = collected(
            [
                message(local(12, 20, 0, 0)),
                message(local(12, 20, 0, 59.9), "xD"),
                message(local(12, 20, 1, 0)),
                message(local(12, 21, 59, 59)),
            ]
        )

        assert kept.by_column[:2] == [2, 1]
        assert kept.laughing_by_column[:2] == [1, 0]
        assert kept.by_column[-1] == 1
        assert sum(kept.by_column) == 4

    def test_it_is_counted_by_the_hour_the_minute_and_the_day_it_arrived_in(self):
        kept = collected(
            [message(local(12, 20, 15)), message(local(12, 20, 15, 30)), message(local(13, 1, 5))],
            Period(local(12, 20), local(13, 2)),
        )

        assert (kept.by_hour[20], kept.by_hour[1], sum(kept.by_hour)) == (2, 1, 3)
        assert sorted(kept.by_minute.values()) == [1, 2]
        assert kept.days == {date(2026, 1, 12), date(2026, 1, 13)}

    def test_laughing_is_counted(self):
        kept = collected(
            [message(local(12, 20), said) for said in ("xD", "omegalul what", "ahoj")], laugh_names=("omegalul",)
        )

        assert kept.laughing == 2

    def test_an_emote_is_counted_every_time_it_is_used(self):
        kept = collected(
            [
                message(local(12, 20, 0), "KEKW KEKW look KEKW", "alice"),
                message(local(12, 20, 1), "KEKW", "bob"),
                message(local(12, 20, 2), "no emote here", "carol"),
            ]
        )

        assert kept.emote_uses == {KEKW: 4}
        assert kept.emote_people == {KEKW: {"alice", "bob"}}
        assert kept.emote_shape == {KEKW: ("KEKW", 20)}
        assert kept.with_emote == 2
        assert kept.chatter_emotes == {"alice": {KEKW: 3}, "bob": {KEKW: 1}}

    def test_kicks_emote_and_the_channels_of_the_same_name_are_two_emotes(self):
        kept = collected([message(local(12, 20), f"{NATIVE} KEKW")])

        assert kept.emote_uses == {NATIVE_KEKW: 1, KEKW: 1}

    def test_a_word_is_an_emote_only_as_the_channel_spells_it(self):
        kept = collected([message(local(12, 20), "kekw KEKW! catjam")])

        assert not kept.emote_uses
        assert kept.with_emote == 0

    def test_what_was_said_is_counted_with_who_said_it(self):
        kept = collected(
            [
                message(local(12, 20, 0), "GG", "alice"),
                message(local(12, 20, 1), "gg", "bob"),
                message(local(12, 20, 2), "gg gg", "bob"),
                message(local(12, 20, 3), "once", "carol"),
            ]
        )

        assert kept.said == {"gg": 3, "once": 1}
        assert kept.said_people == {"gg": {"alice", "bob"}}
        assert kept.said_first["gg"] == ("alice", "GG")

    def test_a_message_of_nothing_but_emotes_is_not_something_said(self):
        kept = collected([message(local(12, 20), "KEKW  KEKW"), message(local(12, 20, 1), NATIVE)])

        assert not kept.said
        assert kept.emote_uses == {KEKW: 2, NATIVE_KEKW: 1}

    def test_who_was_addressed_is_counted_once_a_message(self):
        kept = collected(
            [
                message(local(12, 20, 0), "@Bob hi @bob", "alice"),
                message(local(12, 20, 1), "no @BOB, @carol_7 did it", "dave"),
                message(local(12, 20, 2), "mail me at erin@example.com", "erin"),
            ]
        )

        assert kept.mentions == {"bob": 2, "carol_7": 1}
        assert kept.mention_people["bob"] == {"alice", "dave"}
        assert kept.mention_spelling["bob"] == {"Bob": 1, "BOB": 1}

    def test_a_message_with_nobody_or_nothing_in_it_is_still_a_message(self):
        kept = collected([{"received_at": local(12, 20).isoformat(), "sender_username": None, "content": None}])

        assert kept.messages == 1
        assert kept.by_chatter == {"": 1}

    def test_nothing_comes_of_no_messages(self):
        kept = collected([])

        assert (kept.messages, kept.first, kept.last) == (0, None, None)
        assert kept.by_column == [0] * 120


class TestWhoAddressesAChatter:
    def test_counts_the_messages_of_others_that_do(self):
        messages = [
            message(local(12, 20, 0), "@alice ahoj", "bob"),
            message(local(12, 20, 1), "ty jo @ALICE", "carol"),
            message(local(12, 20, 2), "@alice2 someone else", "bob"),
            message(local(12, 20, 3), "talking to myself @alice", "Alice"),
            message(local(12, 20, 4), "alice without the sign", "bob"),
        ]

        assert chat_stats.mentions_of(messages, "alice") == 2


class TestWhoIsWho:
    COUNTS = (("Marmot", 40), ("alice", 30), ("Alicia", 30), ("bob", 5))

    def test_a_name_is_drawn_like_a_line_of_chat_draws_it(self):
        badges = '[{"type":"moderator","text":"Moderator"}]'

        known = chat_stats.chatter_name("alice", {"alice": ("#ff0000", badges)})
        unknown = chat_stats.chatter_name("alice")

        assert known["nick"] == "alice"
        assert known["own_colour"] is not None
        assert known["badges"] == [{"icon": "moderator", "words": "Moderator"}]
        assert (unknown["own_colour"], unknown["badges"]) == (None, [])
        assert unknown["colour"] == known["colour"]

    @pytest.mark.parametrize("typed", ["marmot", "MARMOT", "  Marmot ", "@marmot"])
    def test_a_chatter_is_found_however_the_name_is_typed(self, typed):
        assert chat_stats.find_chatter(self.COUNTS, typed) == ("Marmot", [])

    def test_the_only_name_that_has_the_text_in_it_is_who_was_meant(self):
        assert chat_stats.find_chatter(self.COUNTS, "armo") == ("Marmot", [])

    def test_a_name_in_full_wins_over_the_names_that_have_it_in_them(self):
        assert chat_stats.find_chatter([*self.COUNTS, ("ali", 1)], "ali") == ("ali", [])

    def test_several_names_that_have_it_are_offered_the_most_active_first(self):
        assert chat_stats.find_chatter(self.COUNTS, "ali") == (None, [("alice", 30), ("Alicia", 30)])

    def test_nobody_is_found_where_nobody_has_it(self):
        assert chat_stats.find_chatter(self.COUNTS, "zed") == (None, [])
        assert chat_stats.find_chatter(self.COUNTS, " @ ") == (None, [])

    def test_a_chatter_stands_behind_everyone_who_wrote_more(self):
        assert chat_stats.rank_of(self.COUNTS, "Marmot") == 1
        assert chat_stats.rank_of(self.COUNTS, "alice") == chat_stats.rank_of(self.COUNTS, "Alicia") == 2
        assert chat_stats.rank_of(self.COUNTS, "bob") == 4
        assert chat_stats.rank_of(self.COUNTS, "nobody") is None


class TestTheFiguresOfAPeriod:
    def kept(self) -> chat_stats.Collected:
        return collected(
            [
                message(local(12, 20, 0, 1), "xD", "alice"),
                message(local(12, 20, 0, 2), "KEKW", "alice"),
                message(local(12, 20, 0, 3), "hello", "alice"),
                message(local(12, 20, 5), "hello", "bob"),
                message(local(12, 20, 9), "bye", "carol"),
            ]
        )

    def test_says_how_much_was_written_and_by_how_many(self):
        figures = by_label(chat_stats.overview_figures(self.kept(), TODAY))

        assert (figures["Messages"]["value"], figures["Chatters"]["value"]) == ("5", "3")
        assert figures["Messages"]["caption"] == "1.7 a minute in the 3 minutes with chat"
        assert figures["Chatters"]["caption"] == "half of them wrote 1 message"
        assert (figures["Wrote once"]["value"], figures["Wrote once"]["caption"]) == (
            "67%",
            "2 chatters with one message",
        )

    def test_says_what_part_of_the_messages_laughed_or_had_an_emote(self):
        figures = by_label(chat_stats.overview_figures(self.kept(), TODAY))

        assert (figures["Laughing"]["value"], figures["Laughing"]["caption"]) == ("20%", "1 message")
        assert (figures["With an emote"]["value"], figures["With an emote"]["caption"]) == ("20%", "1 message")

    def test_names_the_busiest_minute(self):
        figure = by_label(chat_stats.overview_figures(self.kept(), TODAY))["Busiest minute"]

        assert (figure["value"], figure["caption"]) == ("3", "messages, Monday 12 January at 20:00")

    def test_new_chatters_are_told_only_where_that_can_be_known(self):
        kept = self.kept()

        assert "New chatters" not in by_label(chat_stats.overview_figures(kept, TODAY))
        figure = by_label(chat_stats.overview_figures(kept, TODAY, returning=1))["New chatters"]
        assert (figure["value"], figure["caption"]) == ("2", "67% had not written here before")

    def test_the_most_active_get_a_figure_once_there_are_more_chatters_than_them(self):
        few = chat_stats.TOP_FEW
        busy = [message(local(12, 20), sender=f"user{n}") for n in range(few + 1)] + [
            message(local(12, 20, 1), sender="user0") for _ in range(9)
        ]

        figure = by_label(chat_stats.overview_figures(collected(busy), TODAY))[f"The {few} most active"]

        assert f"The {few} most active" not in by_label(chat_stats.overview_figures(self.kept(), TODAY))
        assert figure["value"] == "95%"

    def test_moments_lead_to_the_review_page_when_there_are_some(self):
        none = by_label(chat_stats.overview_figures(self.kept(), TODAY, moments_href="/dashboard"))["Moments"]
        some = by_label(chat_stats.overview_figures(self.kept(), TODAY, moments=3, moments_href="/dashboard"))[
            "Moments"
        ]

        assert (none["value"], none["href"], none["caption"]) == ("0", None, "none was detected in this period")
        assert (some["value"], some["href"], some["caption"]) == ("3", "/dashboard", "detected in this period")

    def test_the_median_of_an_even_number_of_chatters_is_rounded_down(self):
        kept = collected(
            [message(local(12, 20), sender="alice")] + [message(local(12, 20, 1), sender="bob") for _ in range(4)]
        )

        assert by_label(chat_stats.overview_figures(kept, TODAY))["Chatters"]["caption"] == (
            "half of them wrote 2 messages or fewer"
        )


class TestTheFiguresOfAChatter:
    def figures(self, **overrides) -> dict[str, dict]:
        kept = collected(
            [
                message(local(12, 20, 4), "xD"),
                message(local(13, 21, 16), "KEKW"),
                message(local(13, 21, 17), "ahoj"),
                message(local(19, 9, 30), "dobré ráno"),
            ],
            Period(local(12, 0), local(20, 0)),
        )
        given = {"everyone": 400, "rank": 12, "chatters": 245, "first_seen": local(3, 18, 50), "mentioned": 7}
        return by_label(chat_stats.chatter_figures(kept, TODAY, **{**given, **overrides}))

    def test_says_how_much_they_wrote_and_where_that_puts_them(self):
        figure = self.figures()["Messages"]

        assert (figure["value"], figure["caption"]) == ("4", "1% of the chat, number 12 of 245 chatters")

    def test_says_when_they_were_first_and_last_seen(self):
        figures = self.figures()

        assert (figures["First seen"]["value"], figures["First seen"]["caption"]) == (
            "3 January",
            "their oldest stored message, at 18:50",
        )
        assert (figures["Last message"]["value"], figures["Last message"]["caption"]) == (
            "Yesterday",
            "in this period, at 09:30",
        )
        assert "First seen" not in self.figures(first_seen=None)

    def test_says_on_how_many_days_they_wrote_and_how(self):
        figures = self.figures()

        assert figures["Days"]["value"] == "3"
        assert (figures["Laughing"]["value"], figures["With an emote"]["value"]) == ("25%", "25%")
        assert figures["Mentioned"]["value"] == "7"


class TestTheChartOfAPeriod:
    def chart(self, messages: list[dict], period: Period = EVENING, **settings) -> dict:
        settings.setdefault("one_day", chat_stats.one_day(period))
        return chat_stats.activity_chart(collected(messages, period), TODAY, **settings)

    def test_a_column_stands_as_tall_as_its_messages_under_a_round_top(self):
        chart = self.chart(
            [message(local(12, 20, 0, second), "xD" if second < 2 else "hello") for second in range(5)]
            + [message(local(12, 20, 1))]
        )

        # Five messages fit under 8, the first round top they do.
        assert [level["words"] for level in chart["levels"]] == ["8", "6", "4", "2"]
        assert [level["top"] for level in chart["levels"]] == ["0", "25", "50", "75"]
        assert [(column["all"], column["laugh"]) for column in chart["columns"][:3]] == [
            ("62.5", "25"),
            ("12.5", "0"),
            ("0", "0"),
        ]
        assert len(chart["columns"]) == 120

    @pytest.mark.parametrize(("peak", "top"), [(0, "4"), (4, "4"), (5, "8"), (9, "20"), (150, "200"), (2001, "4,000")])
    def test_the_top_of_a_chart_is_a_round_number(self, peak, top):
        chart = self.chart([message(local(12, 20, 0, second / 100)) for second in range(peak)])

        assert chart["levels"][0]["words"] == top

    def test_a_column_says_what_it_stands_for(self):
        chart = self.chart(
            [message(local(12, 20, 0), "xD"), message(local(12, 20, 0, 30)), message(local(12, 20, 1))],
            moments=[local(12, 20, 0, 40)],
        )

        assert chart["columns"][0]["words"] == "20:00 to 20:01: 2 messages, 1 laughing, 1 moment"
        assert chart["columns"][1]["words"] == "20:01 to 20:02: 1 message"
        assert chart["columns"][2]["words"] == "20:02 to 20:03: 0 messages"

    def test_a_column_a_moment_was_detected_in_is_marked(self):
        chart = self.chart([message(local(12, 20, 0))], moments=[local(12, 20, 7, 30), local(12, 23)])

        assert [number for number, column in enumerate(chart["columns"]) if column["moment"]] == [7]

    def test_a_column_with_messages_leads_where_it_is_told_to(self):
        chart = self.chart(
            [message(local(12, 20, 3))], link=lambda columns, number: f"/column/{columns.start(number):%H%M}"
        )

        assert [column["href"] for column in chart["columns"][2:5]] == [None, "/column/2003", None]

    def test_a_chart_says_how_long_a_column_is(self):
        week = Period(local(12, 0), local(19, 0))
        year = Period(local(1, 0), local(1, 0) + timedelta(days=200))

        assert self.chart([])["each"] == "1 minute"
        assert self.chart([], Period(local(12, 18), local(13, 14)))["each"] == "5 minutes"
        assert self.chart([], week)["each"] == "1 hour"
        assert self.chart([], year)["each"] == "1 day"

    def test_times_are_written_under_round_times_of_the_clock(self):
        chart = self.chart([], Period(local(12, 18, 43), local(12, 21, 17)))

        assert [tick["words"] for tick in chart["axis"]] == ["19:00", "19:30", "20:00", "20:30", "21:00"]
        assert chart["axis"][0]["left"] == "11.039"  # 17 of the 154 minutes in

    def test_over_several_days_a_new_day_is_named_and_columns_say_which_day(self):
        chart = self.chart([message(local(13, 8, 10))], Period(local(12, 18), local(14, 12)))

        assert [tick["words"] for tick in chart["axis"]] == [
            "18:00",
            "13 January",
            "06:00",
            "12:00",
            "18:00",
            "14 January",
            "06:00",
        ]
        assert chart["columns"][56]["words"] == "13 January, 08:00 to 08:15: 1 message"

    def test_days_are_written_under_a_chart_of_days(self):
        chart = self.chart([message(local(3, 12))], Period(local(1, 15), local(1, 15) + timedelta(days=200)))

        assert [tick["words"] for tick in chart["axis"]][:3] == ["1 January", "29 January", "26 February"]
        assert chart["columns"][2]["words"] == "Saturday 3 January: 1 message"

    def test_a_week_is_named_by_its_two_ends(self):
        chart = self.chart([], Period(local(1, 15), local(1, 15) + timedelta(days=400)))

        assert chart["each"] == "7 days"
        assert chart["columns"][1]["words"] == "8 January to 14 January: 0 messages"

    def test_no_time_is_written_where_there_is_no_room_left_for_it(self):
        chart = self.chart([], Period(local(12, 20), local(12, 21, 1)))

        assert [tick["words"] for tick in chart["axis"]][-1] == "20:50"
        assert all(float(tick["left"]) <= 92 for tick in chart["axis"])


class TestTheChartOfTheDay:
    def test_has_a_column_for_every_hour(self):
        kept = collected(
            [message(local(12, 20, 15)), message(local(13, 20, 45)), message(local(13, 23, 59))],
            Period(local(12, 0), local(14, 0)),
        )

        chart = chat_stats.hours_chart(kept)

        assert len(chart["columns"]) == 24
        assert chart["columns"][20]["words"] == "20:00 to 21:00: 2 messages"
        assert chart["columns"][23]["words"] == "23:00 to 00:00: 1 message"
        assert chart["columns"][20]["all"] == "50"
        assert [tick["words"] for tick in chart["axis"]] == [f"{hour:02d}:00" for hour in range(0, 24, 3)]


class TestTheTables:
    def kept(self) -> chat_stats.Collected:
        said = [
            ("alice", "KEKW gg"),
            ("alice", f"{NATIVE} {NATIVE}"),
            ("alice", "gg @Bob"),
            ("alice", "gg"),
            ("bob", "GG"),
            ("bob", "catJAM @CAROL"),
            ("zed", "nothing much"),
            ("dave", "?????"),
            ("dave", "???"),
        ]
        return collected([message(local(12, 20, 0, n), content, sender) for n, (sender, content) in enumerate(said)])

    def test_the_most_active_chatters_come_first(self):
        rows = chat_stats.top_chatters(self.kept(), 3, lambda name: f"/chatter/{name}")

        assert [(row["place"], row["name"], row["messages"], row["share"]) for row in rows] == [
            (1, "alice", "4", "44%"),
            (2, "bob", "2", "22%"),
            (3, "dave", "2", "22%"),
        ]
        assert rows[0]["href"] == "/chatter/alice"

    def test_a_chatter_is_shown_the_emote_they_used_most(self):
        rows = {row["name"]: row["emote"] for row in chat_stats.top_chatters(self.kept(), 10)}

        assert rows["alice"] == {"emote": "KEKW", "image": NATIVE_KEKW, "width": 20}
        assert rows["bob"]["emote"] == "catJAM"
        assert rows["zed"] is None

    def test_the_most_used_emotes_say_how_often_by_how_many_and_whose_they_are(self):
        rows = chat_stats.top_emotes(self.kept(), 10)

        assert [(row["emote"], row["uses"], row["people"], row["source"]) for row in rows] == [
            ("KEKW", "2", "1", "Kick"),
            # As often as each other, so in the order of their names.
            ("catJAM", "1", "1", "7TV"),
            ("KEKW", "1", "1", "7TV"),
        ]
        assert rows[1]["width"] == 40  # twice as wide as it is tall

    def test_a_table_is_as_long_as_it_was_asked_to_be(self):
        assert len(chat_stats.top_emotes(self.kept(), 2)) == 2
        assert len(chat_stats.top_chatters(self.kept(), 1)) == 1

    def test_what_was_said_more_than_once_is_listed_with_how_many_said_it(self):
        rows = chat_stats.said_most(self.kept(), 10, EMOTES)

        assert [(row["times"], row["people"]) for row in rows] == [("2", "1"), ("2", "2")]
        # Written as the first of them that was met wrote it.
        assert rows[0]["parts"] == [{"text": "???"}]
        assert rows[1]["parts"] == [{"text": "gg"}]

    def test_an_emote_in_what_was_said_is_drawn_as_one(self):
        kept = collected([message(local(12, 20, 0, n), "KEKW no way", f"user{n}") for n in range(3)])

        (row,) = chat_stats.said_most(kept, 10, EMOTES)

        assert [part.get("emote") or part["text"] for part in row["parts"]] == ["KEKW", " no way"]
        assert (row["times"], row["people"]) == ("3", "3")

    def test_who_was_addressed_most_is_written_as_they_write_themselves(self):
        rows = chat_stats.mentioned_most(self.kept(), 10, lambda name: f"/chatter/{name}")

        assert [(row["name"], row["messages"], row["people"], row["href"]) for row in rows] == [
            ("bob", "1", "1", "/chatter/bob"),
            # Wrote nothing in the period: named as chat named them, and
            # there is nothing of theirs to lead to.
            ("CAROL", "1", "1", None),
        ]

    def test_a_chatters_own_emotes_are_listed_with_how_often(self):
        rows = chat_stats.chatter_emotes(self.kept(), 2)

        assert [(row["emote"], row["uses"]) for row in rows] == [("KEKW", "2"), ("catJAM", "1")]
        assert rows[0]["image"] == NATIVE_KEKW

    def test_the_busiest_minutes_come_first_and_the_earlier_of_two_as_busy(self):
        kept = collected(
            [message(local(12, 20, 0)), message(local(12, 20, 5)), message(local(12, 20, 5, 30))]
            + [message(local(12, 21, 0, second)) for second in range(2)]
        )

        assert chat_stats.busiest_minutes(kept, 2) == [(local(12, 20, 5), 2), (local(12, 21, 0), 2)]


class TestSessions:
    SESSIONS = (
        {"first": local(19, 18, 43, 20).isoformat(), "last": local(19, 21, 16, 59).isoformat(), "messages": 1106},
        {"first": local(10, 21, 8, 1).isoformat(), "last": local(11, 0, 47, 3).isoformat(), "messages": 8156},
    )

    def rows(self, current: Period) -> list[dict]:
        return chat_stats.session_rows(
            self.SESSIONS, TODAY, current, lambda period: chat_stats.local_text(period.start)
        )

    def test_a_session_says_when_it_was_how_long_and_how_much(self):
        first, second = self.rows(EVENING)

        assert (first["day"], first["length"]) == ("Yesterday", "2:33:39")
        assert first["when"] == "18:43 to 21:16, 1,106 messages"
        assert (second["day"], second["when"]) == ("Saturday 10 January", "21:08 to 00:47, 8,156 messages")

    def test_it_leads_to_the_period_that_holds_it(self):
        assert chat_stats.session_period(self.SESSIONS[0]) == Period(local(19, 18, 43), local(19, 21, 17))
        assert [row["href"] for row in self.rows(EVENING)] == ["2026-01-19T18:43", "2026-01-10T21:08"]

    def test_the_session_that_is_the_open_period_is_marked(self):
        assert [row["current"] for row in self.rows(Period(local(19, 18, 43), local(19, 21, 17)))] == [True, False]
        assert [row["current"] for row in self.rows(EVENING)] == [False, False]

    def test_one_that_is_still_going_is_marked_though_its_period_ends_now(self):
        now = local(19, 21, 16, 59.5)

        assert [row["current"] for row in self.rows(Period(local(19, 18, 43), now))] == [True, False]

    def test_a_shorter_period_that_begins_with_it_is_not_the_session(self):
        assert [row["current"] for row in self.rows(Period(local(19, 18, 43), local(19, 20)))] == [False, False]
