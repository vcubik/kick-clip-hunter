"""A moment's transcript as subtitles: the lines made of what the speech
model heard, how they are kept and put right, and the SubRip file an editor
takes away, timed to the video it goes with.

The model itself is not run here (see test_analysis_wrappers.py for how its
words are taken from it); the whole of it from the review page's request on
is in tests/integration/test_api_transcript.py.
"""

from __future__ import annotations

import json

import pytest

from kick_clip_hunter import subtitles
from kick_clip_hunter.subtitles import Line, Word


def spoken(text: str, start: float = 0.0, each: float = 0.3, gap: float = 0.05) -> list[Word]:
    """The words of `text` as the model hears them said one after another
    from `start` on: each `each` seconds long, `gap` seconds apart, with the
    space the model writes in front of every word."""
    words = []
    at = start
    for word in text.split():
        words.append(Word(at, at + each, f" {word}"))
        at += each + gap
    return words


def texts(lines: list[Line]) -> list[str]:
    return [line.text for line in lines]


class TestLines:
    """What the model heard, made into lines a subtitle long."""

    def test_a_short_sentence_is_one_line_from_its_first_word_to_its_last(self):
        (line,) = subtitles.lines([spoken("Tak co, jedeme dál?", start=2.0)])

        assert line == Line(2.0, 2.0 + 4 * 0.3 + 3 * 0.05, "Tak co, jedeme dál?")

    def test_its_times_are_clip_time(self):
        # The file the words were heard in opens with 30 s before the clip.
        (line,) = subtitles.lines([spoken("tak co", start=31.0)], begins=-30.0)

        assert (line.start, line.text) == (1.0, "tak co")

    def test_a_line_ends_with_its_sentence(self):
        lines = subtitles.lines([spoken("To je konec. Ne! Fakt? No…")])

        assert texts(lines) == ["To je konec.", "Ne!", "Fakt?", "No…"]

    def test_a_closing_quote_after_the_full_stop_still_ends_it(self):
        assert texts(subtitles.lines([spoken('řekl "dost." a šel')])) == ['řekl "dost."', "a šel"]

    def test_a_pause_starts_a_new_line(self):
        heard = [spoken("tak co", start=0.0) + spoken("jedeme dál", start=0.65 + subtitles.PAUSE_SECONDS)]

        assert texts(subtitles.lines(heard)) == ["tak co", "jedeme dál"]

    def test_a_shorter_pause_does_not(self):
        heard = [spoken("tak co", start=0.0) + spoken("jedeme dál", start=0.65 + subtitles.PAUSE_SECONDS - 0.1)]

        assert texts(subtitles.lines(heard)) == ["tak co jedeme dál"]

    def test_a_line_never_runs_from_one_stretch_the_model_heard_into_the_next(self):
        lines = subtitles.lines([spoken("tak co", start=0.0), spoken("jedeme dál", start=0.7)])

        assert texts(lines) == ["tak co", "jedeme dál"]

    def test_a_long_sentence_is_broken_into_lines_that_fit(self):
        sentence = "a pak jsme tam přišli a nikdo tam nebyl a tak jsme zase šli domů a bylo po všem"

        lines = subtitles.lines([spoken(sentence, each=0.2, gap=0.0)])

        assert len(lines) > 1
        assert all(len(line.text) <= subtitles.LINE_MAX_CHARACTERS for line in lines)
        assert " ".join(texts(lines)) == sentence

    def test_it_is_broken_after_a_comma_far_enough_into_it(self):
        sentence = "Když jsem tam přišel, tak tam nikdo nebyl a nevěděl jsem co"

        first, second = subtitles.lines([spoken(sentence, each=0.2, gap=0.0)])

        assert first.text == "Když jsem tam přišel,"
        assert second.text == "tak tam nikdo nebyl a nevěděl jsem co"

    def test_but_not_after_one_so_early_that_a_word_would_be_left_on_its_own(self):
        sentence = "Jo, tak když jsem tam přišel tak tam nikdo nebyl a nevěděl"

        first, _rest = subtitles.lines([spoken(sentence, each=0.2, gap=0.0)])

        assert first.text.startswith("Jo, tak")

    def test_a_word_too_long_for_what_is_left_after_the_comma_starts_a_line_of_its_own(self):
        sentence = "Tak když jsem tam přišel, tak tam byly nejneobhospodařovávatelnějšími"

        lines = subtitles.lines([spoken(sentence, each=0.2, gap=0.0)])

        assert texts(lines) == ["Tak když jsem tam přišel,", "tak tam byly", "nejneobhospodařovávatelnějšími"]

    def test_a_line_said_slowly_is_broken_after_the_most_seconds_a_line_is_shown(self):
        lines = subtitles.lines([spoken("tak a teď ještě jednou", each=1.5, gap=0.2)])

        assert len(lines) == 2
        assert all(line.end - line.start <= subtitles.LINE_MAX_SECONDS for line in lines)

    def test_a_word_said_quickly_stays_long_enough_to_be_read(self):
        (line,) = subtitles.lines([[Word(5.0, 5.2, " Jo.")]])

        assert (line.start, line.end) == (5.0, 5.0 + subtitles.LINE_MIN_SECONDS)

    def test_but_never_into_the_line_after_it(self):
        first, _second = subtitles.lines([[Word(5.0, 5.2, " Jo.")], [Word(5.6, 6.0, " Ne.")]])

        assert first.end == 5.6

    def test_lines_heard_overlapping_are_kept_apart(self):
        first, second = subtitles.lines([[Word(1.0, 3.0, " tak")], [Word(2.5, 4.0, " co")]])

        assert first.end == second.start == 2.5

    def test_lines_come_in_the_order_they_are_said(self):
        lines = subtitles.lines([spoken("druhá", start=5.0), spoken("první", start=1.0)])

        assert texts(lines) == ["první", "druhá"]

    def test_words_are_told_apart_by_the_space_in_front_of_them(self):
        # The model writes the first word of a stretch without one, and
        # punctuation it heard apart from a word with none either.
        (line,) = subtitles.lines([[Word(0.0, 0.3, "No"), Word(0.3, 0.6, " jo"), Word(0.6, 0.7, ",")]])

        assert line.text == "No jo,"

    def test_a_word_that_is_only_space_is_left_out(self):
        (line,) = subtitles.lines([[Word(0.0, 0.3, " tak"), Word(0.3, 0.4, "  "), Word(0.4, 0.7, " co")]])

        assert line.text == "tak co"

    def test_nothing_heard_is_no_lines(self):
        assert subtitles.lines([]) == []
        assert subtitles.lines([[]]) == []


class TestKeeping:
    """A transcript as it is kept on its moment."""

    TRANSCRIPT = (Line(-2.5, -0.75, "Tak co, jedeme dál?"), Line(1.0, 2.0, "Ne."))

    def test_it_reads_back_as_it_was_kept(self):
        assert subtitles.read(subtitles.stored(self.TRANSCRIPT)) == list(self.TRANSCRIPT)

    def test_czech_is_kept_as_it_is_written(self):
        assert "dál" in subtitles.stored(self.TRANSCRIPT)

    def test_times_are_kept_to_the_thousandth_of_a_second(self):
        (kept,) = json.loads(subtitles.stored([Line(1.23456, 2.34567, "tak")]))

        assert (kept["start"], kept["end"]) == (1.235, 2.346)

    def test_a_transcript_with_no_lines_is_still_one(self):
        assert subtitles.read(subtitles.stored([])) == []

    def test_none_has_been_made_where_nothing_is_kept(self):
        assert subtitles.read(None) is None

    @pytest.mark.parametrize("kept", ["", "not json", "{}", "[1, 2]", '[{"start": 1}]'])
    def test_what_is_not_one_reads_as_none_made(self, kept):
        assert subtitles.read(kept) is None


class TestCorrecting:
    TRANSCRIPT = (Line(0.0, 1.5, "tak co"), Line(2.0, 3.5, "jedeme dal"), Line(4.0, 5.0, "ne"))

    def test_a_line_says_what_it_was_corrected_to_and_keeps_its_times(self):
        corrected = subtitles.corrected(self.TRANSCRIPT, 1, "jedeme dál")

        assert corrected == [self.TRANSCRIPT[0], Line(2.0, 3.5, "jedeme dál"), self.TRANSCRIPT[2]]

    def test_what_was_typed_is_tidied_as_a_subtitle_has_to_be(self):
        # A blank row would end the subtitle there.
        corrected = subtitles.corrected(self.TRANSCRIPT, 0, "  tak   co,\n\n   řekl   \n")

        assert corrected[0].text == "tak co,\nřekl"

    def test_a_line_can_be_emptied(self):
        assert subtitles.corrected(self.TRANSCRIPT, 2, "  \n ")[2] == Line(4.0, 5.0, "")

    def test_it_holds_so_much_and_no_more(self):
        corrected = subtitles.corrected(self.TRANSCRIPT, 0, "a" * (subtitles.TEXT_MAX_CHARACTERS + 50))

        assert len(corrected[0].text) == subtitles.TEXT_MAX_CHARACTERS

    @pytest.mark.parametrize("index", [-1, 3])
    def test_a_line_that_is_not_there_cannot_be(self, index):
        with pytest.raises(IndexError):
            subtitles.corrected(self.TRANSCRIPT, index, "tak")

    def test_there_is_something_to_put_in_subtitles_while_any_line_says_something(self):
        assert subtitles.said(self.TRANSCRIPT)
        assert not subtitles.said([Line(0.0, 1.0, ""), Line(1.0, 2.0, " ")])
        assert not subtitles.said([])


class TestSubRip:
    """The subtitles of a video, as the file an editor takes them in."""

    def test_each_line_is_numbered_and_timed_with_a_blank_row_after_it(self):
        srt = subtitles.srt([Line(1.0, 2.5, "Tak co?"), Line(3.0, 4.25, "Jedeme dál.")], start=0.0)

        assert srt == (
            "1\r\n00:00:01,000 --> 00:00:02,500\r\nTak co?\r\n\r\n"
            "2\r\n00:00:03,000 --> 00:00:04,250\r\nJedeme dál.\r\n\r\n"
        )

    def test_a_line_of_two_rows_is_one_subtitle_of_two_rows(self):
        srt = subtitles.srt([Line(1.0, 2.5, "Tak co,\nřekl")], start=0.0)

        assert srt == "1\r\n00:00:01,000 --> 00:00:02,500\r\nTak co,\r\nřekl\r\n\r\n"

    def test_its_times_are_the_videos_own(self):
        # A video whose first frame is 30 s before the clip's.
        srt = subtitles.srt([Line(-29.0, -27.5, "tak")], start=-30.0)

        assert "00:00:01,000 --> 00:00:02,500" in srt

    def test_times_run_into_hours(self):
        srt = subtitles.srt([Line(3725.4321, 3726.0, "tak")], start=0.0)

        assert "01:02:05,432 --> 01:02:06,000" in srt

    def test_a_line_with_nothing_to_say_is_left_out_and_leaves_no_gap_in_the_numbers(self):
        srt = subtitles.srt([Line(0.0, 1.0, "tak"), Line(1.0, 2.0, ""), Line(2.0, 3.0, "co")], start=0.0)

        assert srt.split("\r\n")[::4][:2] == ["1", "2"]
        assert "\r\n\r\n3\r\n" not in srt

    def test_a_line_said_before_or_after_the_video_is_not_in_it(self):
        transcript = [Line(0.0, 1.0, "before"), Line(2.0, 3.0, "in it"), Line(5.0, 6.0, "after")]

        srt = subtitles.srt(transcript, start=1.5, length=3.0)

        assert "before" not in srt and "after" not in srt
        assert "00:00:00,500 --> 00:00:01,500\r\nin it" in srt

    def test_a_line_the_videos_ends_cut_into_is_cut_to_them(self):
        transcript = [Line(0.0, 2.0, "begun"), Line(9.0, 11.0, "ended")]

        srt = subtitles.srt(transcript, start=1.0, length=9.0)

        assert "00:00:00,000 --> 00:00:01,000\r\nbegun" in srt
        assert "00:00:08,000 --> 00:00:09,000\r\nended" in srt

    def test_one_of_which_too_little_is_left_in_the_video_is_left_out(self):
        transcript = [Line(0.0, 2.0, "begun"), Line(4.0, 4.3, "Jo.")]

        srt = subtitles.srt(transcript, start=2.0 - subtitles.CUT_LINE_MIN_SECONDS + 0.1, length=5.0)

        # A line that short is in the video if all of it is.
        assert "begun" not in srt and "Jo." in srt

    def test_without_a_length_the_video_runs_as_long_as_the_transcript(self):
        assert "late" in subtitles.srt([Line(5000.0, 5001.0, "late")], start=0.0)

    def test_subtitles_of_nothing_are_an_empty_file(self):
        assert subtitles.srt([], start=0.0) == ""

    def test_it_is_written_as_utf8_with_the_mark_that_says_so(self):
        written = subtitles.srt([Line(0.0, 1.0, "dál")], start=0.0).encode(subtitles.ENCODING)

        assert written.startswith(b"\xef\xbb\xbf") and "dál".encode() in written

    def test_it_is_named_after_the_video_it_goes_with(self):
        assert subtitles.name("moment_12_trim.mp4") == "moment_12_trim.srt"
