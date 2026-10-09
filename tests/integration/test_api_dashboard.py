"""The dashboard's pages, rendered from a database the test filled in: the
review page with its queue and the open moment, and the channels page.

Assertions are made on the parsed HTML (tests/support/html.py), not on
substrings, so they survive template whitespace changes and can tell text
from markup - which matters for the escaping tests: chat messages and
usernames come from arbitrary Kick users and are shown on these pages.

The exact wording of dates, counts and reasons is covered where it is made
(tests/unit/test_dashboard_view.py); here the question is whether the right
things end up on the page.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kick_clip_hunter import dashboard_view as view
from kick_clip_hunter import recorder, recording_manager
from tests.support.data import T0, add_chat, add_moment, add_streamer, minutes
from tests.support.html import Element, parse

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def today(service, monkeypatch) -> date:
    """The pages word a date by how long ago it was. Today is pinned to the
    local day of T0, so that does not depend on when the suite runs."""
    day = T0.astimezone().date()
    monkeypatch.setattr(service.main, "_today", lambda: day)
    return day


async def page_at(service, path: str, **params) -> Element:
    response = await service.client.get(path, params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return parse(response.text)


async def review(service, **params) -> Element:
    return await page_at(service, "/dashboard", **params)


async def channels(service) -> Element:
    return await page_at(service, "/dashboard/channels")


def clock(moment: datetime) -> str:
    return f"{moment.astimezone():%H:%M}"


def queue_rows(page: Element) -> list[Element]:
    return page.find("a", class_="ch-row")


def queued(page: Element) -> list[int]:
    """Ids of the moments in the queue, top to bottom."""
    return [int(row.attrs["data-moment"]) for row in queue_rows(page)]


def open_moment(page: Element) -> Element:
    return page.one("article", class_="moment")


def opened(page: Element) -> int | None:
    """Id of the moment that is open, if one is."""
    articles = page.find("article", class_="moment")
    return int(articles[0].attrs["data-moment"]) if articles else None


def tabs(page: Element) -> list[str]:
    return [tab.text for tab in page.one("aside", class_="queue").find("a", class_="ch-tab")]


def switch_states(page: Element) -> dict[str, str]:
    """Label -> On/Off for every switch on the page: the lit half is the state."""
    states = {}
    for group in page.find("span", class_="ch-switch"):
        (lit,) = [half.text for half in group.find("button") if half.attrs["aria-pressed"] == "true"]
        states[group.attrs["aria-label"]] = lit
    return states


def chat(article: Element) -> list[tuple[str, str]]:
    """(who, what they said) for every chat line shown."""
    lines = []
    for line in article.find("p", class_="ch-chat"):
        who, said = [child for child in line.children if not isinstance(child, str)]
        lines.append((who.text, said.text))
    return lines


class RunningRecorder:
    """Stands in for a recorder that is in the middle of recording."""

    is_active = True

    def stop(self) -> None:
        self.is_active = False


class TestEmptyInstallation:
    async def test_the_review_page_renders_with_nothing_in_the_database(self, service):
        page = await review(service)

        assert queue_rows(page) == [] and opened(page) is None
        assert page.one("p", class_="queue-empty").text == "No moments yet"
        assert page.one("h1").text == "No moments yet"
        assert (
            page.one("p", class_="lead").text
            == "No channels are on the watchlist yet. Add one under Channels to start."
        )
        assert tabs(page) == ["Unrated 0", "All 0", "Best 0"]

    async def test_the_channels_page_renders_with_nothing_in_the_database(self, service):
        page = await channels(service)

        assert page.find("table") == []
        assert "No channels on the watchlist yet." in page.text

    async def test_shows_the_default_switch_positions(self, service):
        assert switch_states(await review(service)) == {"Watching": "On"}
        assert switch_states(await channels(service)) == {
            "Watching": "On",
            "Transcript": "Off",
            "Audio events": "Off",
            "Sound events": "Off",
            "Frame embeddings": "Off",
        }

    async def test_nothing_is_announced_as_new(self, service):
        page = await review(service)

        news = page.one("div", data_news=True)
        assert "hidden" in news.attrs
        assert (news.attrs["data-moments"], news.attrs["data-clips"]) == ("0", "0")

    @pytest.mark.parametrize("path", ["/dashboard", "/dashboard/channels"])
    async def test_the_stylesheet_and_the_script_a_page_asks_for_are_served(self, service, path):
        page = await page_at(service, path)

        stylesheet = page.one("link", rel="stylesheet").attrs["href"]
        script = page.one("script").attrs["src"]
        assert stylesheet.startswith("/static/dashboard.css?v=") and script.startswith("/static/dashboard.js?v=")
        for address, kind in [(stylesheet, "text/css"), (script, "text/javascript")]:
            served = await service.client.get(address)
            assert served.status_code == 200
            assert served.headers["content-type"].startswith(kind)


class TestTheBarAcrossTheTop:
    @pytest.mark.parametrize(("path", "name"), [("/dashboard", "Review"), ("/dashboard/channels", "Channels")])
    async def test_marks_the_page_you_are_on(self, service, path, name):
        page = await page_at(service, path)

        links = page.one("nav", class_="bar-nav").find("a")
        assert [link.text for link in links] == ["Review", "Channels"]
        assert [link.text for link in links if link.attrs.get("aria-current") == "page"] == [name]

    async def test_says_when_nothing_is_being_recorded(self, service):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2)

        bar = (await review(service)).one("header", class_="bar")

        assert bar.one("span", class_="ch-lamp").text == "Not recording"
        assert "ch-lamp-rec" not in bar.one("span", class_="ch-lamp").classes
        assert bar.one("span", class_="bar-meta").text == "2 channels watched"

    async def test_says_how_many_channels_are_being_recorded(self, service, monkeypatch):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2)
        add_streamer("paused_channel", 3, tracking=False)
        monkeypatch.setitem(recording_manager._recorders, "channel_a", RunningRecorder())

        bar = (await review(service)).one("header", class_="bar")

        assert bar.one("span", class_="ch-lamp-rec").text == "Recording"
        assert bar.one("span", class_="bar-meta").text == "1 of 2 channels"

    async def test_the_watching_switch_reflects_what_was_set(self, service):
        await service.client.post("/settings/watching?enabled=0")

        assert switch_states(await review(service)) == {"Watching": "Off"}


class TestWatchlist:
    def rows(self, page: Element) -> list[list[str]]:
        return [[cell.text for cell in row.find("td")] for row in page.one("table").one("tbody").find("tr")]

    async def test_lists_every_watched_channel_with_what_it_is_doing(self, service, monkeypatch, today):
        add_streamer("first_channel", 111)
        add_streamer("second_channel", 222, tracking=False)
        monkeypatch.setitem(recording_manager._recorders, "first_channel", RunningRecorder())

        page = await channels(service)

        added = view.date_words(datetime.now(timezone.utc).astimezone().date(), today)
        assert self.rows(page) == [
            ["first_channel", added, "Recording", "Off On"],
            ["second_channel", added, "Not recording", "Off On"],
        ]
        states = switch_states(page)
        assert (states["Tracking first_channel"], states["Tracking second_channel"]) == ("On", "Off")

    async def test_each_tracking_switch_posts_to_its_own_channel(self, service):
        add_streamer("first_channel", 111)

        page = await channels(service)

        assert page.one("span", aria_label="Tracking first_channel").attrs["data-switch"] == (
            "/channels/first_channel/tracking"
        )

    async def test_the_analysis_switches_reflect_what_was_set(self, service):
        await service.client.post("/settings/sound_events?enabled=1")

        states = switch_states(await channels(service))

        assert states["Sound events"] == "On"
        assert states["Transcript"] == "Off"

    async def test_every_analysis_switch_says_what_it_does(self, service):
        page = await channels(service)

        captions = [caption.text for caption in page.one("div", class_="settings").find("span", class_="ch-cap")]
        assert captions == [view.ANALYSIS_CAPTIONS[name] for name, _label in service.main.ANALYSIS_SETTINGS]
        assert all(captions)


class TestQueue:
    async def test_lists_moments_newest_first(self, service):
        oldest = add_moment("channel_a", detected_at=T0)
        newest = add_moment("channel_b", detected_at=T0 + minutes(5))
        middle = add_moment("channel_a", detected_at=T0 + minutes(2))

        page = await review(service)

        assert queued(page) == [newest, middle, oldest]

    async def test_shows_what_is_not_rated_yet_unless_asked_otherwise(self, service):
        unrated = add_moment(detected_at=T0)
        good = add_moment(detected_at=T0 + minutes(1), rating=view.BEST_RATING_MIN)
        poor = add_moment(detected_at=T0 + minutes(2), rating=view.BEST_RATING_MIN - 1)
        fresh = add_moment(detected_at=T0 + minutes(3))

        assert queued(await review(service)) == [fresh, unrated]
        assert queued(await review(service, show="all")) == [fresh, poor, good, unrated]
        assert queued(await review(service, show="best")) == [good]

    async def test_every_list_carries_its_count_and_the_chosen_one_is_marked(self, service):
        add_moment(detected_at=T0)
        add_moment(detected_at=T0 + minutes(1), rating=5)
        add_moment(detected_at=T0 + minutes(2), rating=1)

        page = await review(service, show="best")

        assert tabs(page) == ["Unrated 1", "All 3", "Best 1"]
        marked = [tab.text for tab in page.find("a", class_="ch-tab") if tab.attrs.get("aria-current") == "true"]
        assert marked == ["Best 1"]

    async def test_a_list_it_does_not_know_is_the_unrated_one(self, service):
        unrated = add_moment(detected_at=T0)
        add_moment(detected_at=T0 + minutes(1), rating=3)

        assert queued(await review(service, show="everything")) == [unrated]

    async def test_a_row_says_when_what_and_how_it_was_rated(self, service):
        add_moment(detected_at=T0, stream_elapsed_seconds=3723, reason="message_rate,laugh", rating=4)

        (row,) = queue_rows(await review(service, show="all"))

        assert row.one("span", class_="ch-time").text == "1:02:03"
        assert row.one("span", class_="ch-what").text == "Laughing, no clip"
        meter = row.one("span", class_="ch-meter")
        assert meter.attrs["aria-label"] == "rated 4 of 5"
        assert [segment.classes for segment in meter.find("i")] == [{"is-lit"}] * 4 + [set()]
        assert "is-unrated" not in row.classes

    async def test_an_unrated_row_stands_out(self, service):
        add_moment()

        (row,) = queue_rows(await review(service))

        assert "is-unrated" in row.classes
        assert row.one("span", class_="ch-meter").attrs["aria-label"] == "not rated yet"
        assert row.one("span", class_="ch-meter").find("i", class_="is-lit") == []

    async def test_a_row_without_a_stream_time_shows_the_time_of_day(self, service):
        add_moment(detected_at=T0, stream_elapsed_seconds=None)

        (row,) = queue_rows(await review(service))

        assert row.one("span", class_="ch-time").text == clock(T0)

    async def test_rows_are_grouped_under_the_stream_they_came_from(self, service, today):
        # channel_a has been live for two hours at T0, channel_b for ten minutes.
        second = add_moment("channel_a", detected_at=T0, stream_elapsed_seconds=7200)
        other = add_moment("channel_b", detected_at=T0 - minutes(1), stream_elapsed_seconds=540)
        first = add_moment("channel_a", detected_at=T0 - minutes(60), stream_elapsed_seconds=3600)

        page = await review(service)

        def went_live(at: datetime) -> str:
            return f"{view.day_words(at.astimezone().date(), today)}, from {clock(at)}"

        groups = page.find("section", class_="queue-group")
        assert [(g.one("b").text, g.one("h2").one("span").text, queued(g)) for g in groups] == [
            ("channel_a", went_live(T0 - minutes(120)), [second, first]),
            ("channel_b", went_live(T0 - minutes(10)), [other]),
        ]

    async def test_the_first_moment_of_the_list_is_the_open_one(self, service):
        add_moment(detected_at=T0)
        newest = add_moment(detected_at=T0 + minutes(1))

        page = await review(service)

        assert opened(page) == newest
        assert [int(row.attrs["data-moment"]) for row in queue_rows(page) if "aria-current" in row.attrs] == [newest]

    async def test_any_moment_of_the_list_can_be_opened(self, service):
        older = add_moment(detected_at=T0)
        add_moment(detected_at=T0 + minutes(1))

        page = await review(service, moment=older)

        assert opened(page) == older
        assert [int(row.attrs["data-moment"]) for row in queue_rows(page) if "aria-current" in row.attrs] == [older]

    async def test_each_row_links_to_itself_within_the_list_it_is_in(self, service):
        moment_id = add_moment("channel_a", rating=5)

        (row,) = queue_rows(await review(service, show="best", channel="channel_a"))

        assert row.attrs["href"] == f"/dashboard?show=best&channel=channel_a&moment={moment_id}"

    async def test_a_moment_that_is_not_in_the_list_can_still_be_opened(self, service):
        # Say, the one just rated: it has left the unrated list, and
        # reloading the page must not swap it for another.
        rated = add_moment(detected_at=T0, rating=2)
        unrated = add_moment(detected_at=T0 + minutes(1))

        page = await review(service, moment=rated)

        assert queued(page) == [unrated]
        assert opened(page) == rated

    async def test_a_moment_that_does_not_exist_gives_way_to_the_first_of_the_list(self, service):
        only = add_moment()

        assert opened(await review(service, moment=999)) == only


class TestOpenMoment:
    async def test_says_which_channel_when_and_how_far_into_the_stream(self, service, today):
        add_moment("channel_a", detected_at=T0, stream_elapsed_seconds=3723)

        article = open_moment(await review(service))

        head = article.one("header", class_="moment-head")
        assert head.one("h1").text == "channel_a"
        assert head.one("p", class_="moment-when").text == view.when_words(T0.astimezone(), today)
        assert head.one("p", class_="moment-clock").text == "1:02:03 into the stream"

    async def test_leaves_the_stream_time_out_when_it_was_not_recorded(self, service):
        add_moment(stream_elapsed_seconds=None)

        article = open_moment(await review(service))

        assert article.find("p", class_="moment-clock") == []
        assert "into the stream" not in article.text

    async def test_gives_the_detectors_figures(self, service):
        add_moment(score=12.3456, reason="laugh")

        summary = open_moment(await review(service)).one("p", class_="moment-summary").text

        assert summary == (
            "Score 12.3, set off by laughing. "
            "12 messages in 10 seconds; the usual pace is 0.5 a second. "
            "8 laughs or emote names and 3 emotes among them."
        )

    async def test_shows_the_chat_around_the_moment(self, service):
        add_moment("channel_a", detected_at=T0, reaction_seconds=10)
        add_chat("channel_a", "early_bird", "long before", T0 - timedelta(seconds=60))
        add_chat("channel_a", "alice", "what was that", T0 - timedelta(seconds=8))
        add_chat("channel_a", "bob", "xDDD", T0 - timedelta(seconds=3))
        add_chat("channel_a", "latecomer", "what did I miss", T0 + timedelta(seconds=60))
        add_chat("channel_b", "elsewhere", "another channel entirely", T0 - timedelta(seconds=5))

        article = open_moment(await review(service))

        assert chat(article) == [("alice", "what was that"), ("bob", "xDDD")]

    async def test_a_chatter_keeps_their_colour(self, service):
        add_moment("channel_a", detected_at=T0)
        for second, sender in enumerate(["alice", "bob", "alice"], start=1):
            add_chat("channel_a", sender, "xD", T0 - timedelta(seconds=second))

        names = [line.one("b") for line in open_moment(await review(service)).find("p", class_="ch-chat")]

        assert [name.classes for name in names] == [{f"ch-nick-{view.nick_colour(name.text)}"} for name in names]
        assert names[0].classes == names[2].classes

    async def test_native_emotes_are_shown_by_name(self, service):
        add_moment("channel_a", detected_at=T0)
        add_chat("channel_a", "alice", "no way [emote:37226:KEKW][emote:1730752:emojiLol]", T0 - timedelta(seconds=2))

        article = open_moment(await review(service))

        assert chat(article) == [("alice", "no way KEKW emojiLol")]
        assert [emote.text for emote in article.find("span", class_="ch-emote-name")] == ["KEKW", "emojiLol"]

    async def test_says_so_when_no_chat_was_stored_for_the_window(self, service):
        add_moment("channel_a")

        article = open_moment(await review(service))

        assert chat(article) == []
        assert article.one("p", class_="chat-empty").text == "No chat was stored for this moment."

    async def test_rating_tags_and_note_are_shown_as_saved(self, service):
        add_moment(rating=4, stream_type="gaming", moment_type="fail", notes="good one, cut a bit late")

        article = open_moment(await review(service, show="all"))

        keys = article.one("span", class_="ch-keys")
        assert keys.attrs["aria-label"] == "Rating, 4 of 5"
        assert [key.text for key in keys.find("button", class_="is-lit")] == ["1", "2", "3", "4"]
        assert [key.text for key in keys.find("button", aria_pressed="true")] == ["4"]
        assert [tag.text for tag in article.find("button", class_="ch-tag", aria_pressed="true")] == ["Fail", "Gaming"]
        assert article.one("textarea").text == "good one, cut a bit late"
        assert article.attrs["data-rating"] == "4"

    async def test_an_unrated_moment_has_nothing_lit(self, service):
        add_moment()

        article = open_moment(await review(service))

        keys = article.one("span", class_="ch-keys")
        assert keys.attrs["aria-label"] == "Rating, not rated yet"
        assert [key.text for key in keys.find("button")] == ["1", "2", "3", "4", "5"]
        assert keys.find("button", class_="is-lit") == [] and keys.find("button", aria_pressed="true") == []
        assert article.find("button", class_="ch-tag", aria_pressed="true") == []
        assert article.attrs["data-rating"] == "0"

    async def test_every_tag_the_service_accepts_is_offered(self, service):
        add_moment()

        article = open_moment(await review(service))

        def offered(field: str) -> list[tuple[str, str]]:
            return [(tag.attrs["data-value"], tag.text) for tag in article.find("button", data_tag=field)]

        assert offered("moment_type") == service.main.MOMENT_TYPES
        assert offered("stream_type") == service.main.STREAM_TYPES

    async def test_analysis_results_are_shown_once_they_exist(self, service):
        add_moment(
            clip_path="channel_a/moment_1.mp4",
            transcript="tak to bylo neco",
            audio_events="cs, HAPPY, Laughter",
            sound_events="Laughter:0.80",
        )

        analysis = open_moment(await review(service)).one("dl", class_="analysis")

        assert dict(
            zip((dt.text for dt in analysis.find("dt")), (dd.text for dd in analysis.find("dd")), strict=True)
        ) == {
            "Transcript": "tak to bylo neco",
            "Audio events": "cs, HAPPY, Laughter",
            "Sound events": "Laughter:0.80",
        }

    async def test_without_any_there_is_no_analysis_to_show(self, service):
        add_moment(clip_path="channel_a/moment_1.mp4")

        assert open_moment(await review(service)).find("dl") == []


class TestOpenMomentOnItsOwn:
    """What the page fetches to open another moment without reloading."""

    async def test_is_the_same_moment_the_page_would_show(self, service):
        moment_id = add_moment("channel_a", detected_at=T0, rating=3, notes="a note")
        add_chat("channel_a", "alice", "xDDD", T0 - timedelta(seconds=2))

        alone = await page_at(service, f"/dashboard/moments/{moment_id}")
        on_the_page = open_moment(await review(service, show="all"))

        (article,) = [child for child in alone.children if not isinstance(child, str)]
        assert article.tag == "article" and article.attrs == on_the_page.attrs
        assert article.text == on_the_page.text
        assert chat(article) == [("alice", "xDDD")]

    async def test_is_nothing_but_the_moment(self, service):
        moment_id = add_moment()

        alone = await page_at(service, f"/dashboard/moments/{moment_id}")

        assert alone.find("html") == [] and alone.find("header", class_="bar") == [] and alone.find("script") == []

    async def test_an_unknown_moment_is_a_404(self, service):
        response = await service.client.get("/dashboard/moments/999")

        assert response.status_code == 404
        assert "999" in response.json()["detail"]


class TestAnalysisInProgress:
    """A missing result is only shown as "on its way" while it can be."""

    def fresh_clip(self, **columns) -> None:
        add_moment(detected_at=datetime.now(timezone.utc), clip_path="some_channel/moment_1.mp4", **columns)

    async def shown(self, service) -> dict[str, str]:
        """Step -> what the page says about it."""
        article = open_moment(await review(service))
        return {
            dt.text: dd.text
            for analysis in article.find("dl", class_="analysis")
            for dt, dd in zip(analysis.find("dt"), analysis.find("dd"), strict=True)
        }

    async def test_nothing_is_in_progress_while_analysis_is_switched_off(self, service):
        # The default - and before this was fixed, every clip said
        # "transcribing..." forever.
        self.fresh_clip()

        assert await self.shown(service) == {}

    async def test_a_fresh_clip_shows_exactly_the_steps_that_are_switched_on(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        await service.client.post("/settings/sound_events?enabled=1")
        self.fresh_clip()

        assert await self.shown(service) == {"Transcript": "In progress", "Sound events": "In progress"}

    async def test_a_step_with_no_text_to_show_is_never_listed(self, service):
        await service.client.post("/settings/frames?enabled=1")
        self.fresh_clip()

        assert await self.shown(service) == {}

    async def test_a_result_that_has_arrived_replaces_its_placeholder(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        self.fresh_clip(transcript="to je konec")

        assert await self.shown(service) == {"Transcript": "to je konec"}

    async def test_an_empty_result_means_done_not_pending(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        self.fresh_clip(transcript="")

        assert await self.shown(service) == {}

    async def test_an_old_clip_without_a_result_is_not_pending_forever(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        long_ago = datetime.now(timezone.utc) - timedelta(seconds=service.main.ANALYSIS_PENDING_SECONDS + 60)
        add_moment(detected_at=long_ago, clip_path="some_channel/moment_1.mp4")

        assert await self.shown(service) == {}

    async def test_a_moment_without_a_clip_has_nothing_to_analyse(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        add_moment(detected_at=datetime.now(timezone.utc))

        assert await self.shown(service) == {}


class TestClips:
    def clip_file(self, relative: str) -> None:
        path = recorder.CLIPS_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really video")

    def footage(self, article: Element) -> list[tuple[str, str]]:
        """(label, address) of everything the player offers to show."""
        return [(link.text, link.attrs["href"]) for link in article.find("a", data_footage=True)]

    async def test_a_moment_with_a_clip_embeds_it(self, service):
        add_moment("channel_a", clip_path="channel_a/moment_1.mp4")

        article = open_moment(await review(service))

        assert [video.attrs["src"] for video in article.find("video")] == ["/clips/channel_a/moment_1.mp4"]
        assert article.find("p", class_="no-clip") == []
        # With nothing else to show, there is nothing to choose between.
        assert self.footage(article) == []

    async def test_a_clip_gets_its_controls(self, service):
        add_moment("channel_a", clip_path="channel_a/moment_1.mp4")

        article = open_moment(await review(service))

        labels = [button.attrs["aria-label"] for button in article.one("div", class_="ch-transport").find("button")]
        assert labels == ["Play", "Playback speed", "Mute", "Full screen"]
        assert article.one("input", type="range").attrs["aria-label"] == "Position in the clip"

    async def test_a_fresh_moment_without_a_clip_says_one_is_on_its_way(self, service):
        add_moment(detected_at=datetime.now(timezone.utc))

        article = open_moment(await review(service))

        assert article.find("video") == [] and article.find("div", class_="ch-transport") == []
        assert article.one("p", class_="no-clip").text == "The clip is still being cut. That takes about a minute."

    async def test_an_older_one_says_there_is_none(self, service):
        long_ago = datetime.now(timezone.utc) - timedelta(seconds=service.main.CLIP_PENDING_SECONDS + 60)
        add_moment(detected_at=long_ago)

        article = open_moment(await review(service))

        assert article.find("video") == []
        assert article.one("p", class_="no-clip").text == "No clip was saved for this moment."

    async def test_context_footage_is_offered_only_where_the_files_exist(self, service):
        both = add_moment("channel_a", detected_at=T0, clip_path="channel_a/moment_1.mp4")
        after_only = add_moment("channel_a", detected_at=T0 + minutes(1), clip_path="channel_a/moment_2.mp4")
        self.clip_file("channel_a/moment_1_before.mp4")
        self.clip_file("channel_a/moment_1_after.mp4")
        self.clip_file("channel_a/moment_2_after.mp4")
        before = f"{recorder.CONTEXT_BEFORE_SECONDS} s before"
        after = f"{recorder.CONTEXT_AFTER_SECONDS} s after"

        assert self.footage(open_moment(await review(service, moment=both))) == [
            (before, "/clips/channel_a/moment_1_before.mp4"),
            ("Clip", "/clips/channel_a/moment_1.mp4"),
            (after, "/clips/channel_a/moment_1_after.mp4"),
        ]
        assert self.footage(open_moment(await review(service, moment=after_only))) == [
            ("Clip", "/clips/channel_a/moment_2.mp4"),
            (after, "/clips/channel_a/moment_2_after.mp4"),
        ]

    async def test_the_clip_is_what_the_player_starts_on(self, service):
        add_moment("channel_a", clip_path="channel_a/moment_1.mp4")
        self.clip_file("channel_a/moment_1_before.mp4")

        article = open_moment(await review(service))

        assert [link.text for link in article.find("a", data_footage=True, aria_current="true")] == ["Clip"]
        assert article.one("video").attrs["src"] == "/clips/channel_a/moment_1.mp4"

    async def test_a_file_name_is_made_safe_for_an_address(self, service):
        # Imported clips keep the name of the file they came from.
        add_moment("channel_a", clip_path="channel_a/manual_1_best of #3.mp4")
        self.clip_file("channel_a/manual_1_best of #3.mp4")

        address = open_moment(await review(service)).one("video").attrs["src"]

        assert address == "/clips/channel_a/manual_1_best%20of%20%233.mp4"
        assert (await service.client.get(address)).content == b"not really video"

    async def test_clip_files_are_served(self, service):
        self.clip_file("channel_a/moment_1.mp4")

        response = await service.client.get("/clips/channel_a/moment_1.mp4")

        assert response.status_code == 200
        assert response.content == b"not really video"

    async def test_a_missing_clip_is_a_404(self, service):
        response = await service.client.get("/clips/channel_a/moment_999.mp4")

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/clips/../kick_clip_hunter.db",
            "/clips/%2e%2e/kick_clip_hunter.db",
            "/clips/channel_a/../../kick_clip_hunter.db",
            "/static/../main.py",
            "/static/%2e%2e/main.py",
        ],
    )
    async def test_the_file_routes_do_not_reach_outside_their_directories(self, service, path):
        # The database sits one level above the clips it is served next to,
        # the application's code one level above its stylesheet.
        add_moment()
        self.clip_file("channel_a/moment_1.mp4")

        response = await service.client.get(path)

        assert response.status_code == 404

    async def test_the_page_knows_how_many_moments_and_clips_it_was_made_with(self, service):
        # What it later compares the service's totals against, to say what is new.
        add_moment(detected_at=T0)
        add_moment(detected_at=T0 + minutes(1), clip_path="some_channel/moment_2.mp4")
        add_moment(detected_at=T0 + minutes(2), clip_path="some_channel/moment_3.mp4")

        news = (await review(service)).one("div", data_news=True)

        assert (news.attrs["data-moments"], news.attrs["data-clips"]) == ("3", "2")
        assert news.one("a").attrs["href"] == "/dashboard"


class TestUntrustedText:
    """Anything a Kick user can type ends up on this page."""

    PAYLOAD = '<script>alert("xss")</script><img src=x onerror=alert(1)>'

    async def test_chat_messages_and_usernames_are_escaped(self, service):
        add_moment("channel_a", detected_at=T0)
        add_chat("channel_a", "<u>mallory</u>", self.PAYLOAD, T0 - timedelta(seconds=2))

        page = await review(service)

        article = open_moment(page)
        # Shown literally, as text...
        assert chat(article) == [("<u>mallory</u>", self.PAYLOAD)]
        # ...and nothing of it became markup.
        assert article.find("script") == [] and article.find("img") == [] and article.find("u") == []
        assert len(page.find("script")) == 1  # the page's own

    async def test_an_emote_name_is_escaped_too(self, service):
        add_moment("channel_a", detected_at=T0)
        add_chat("channel_a", "mallory", "[emote:1:<img src=x onerror=alert(1)>]", T0 - timedelta(seconds=2))

        article = open_moment(await review(service))

        assert article.one("span", class_="ch-emote-name").text == "<img src=x onerror=alert(1)>"
        assert article.find("img") == []

    async def test_notes_cannot_break_out_of_their_text_area(self, service):
        add_moment(notes='</textarea><script>alert("xss")</script>')

        article = open_moment(await review(service))

        assert article.one("textarea").text == '</textarea><script>alert("xss")</script>'
        assert article.find("script") == []

    async def test_a_transcript_is_escaped_too(self, service):
        add_moment(clip_path="some_channel/moment_1.mp4", transcript="<script>alert(1)</script>")

        article = open_moment(await review(service))

        assert article.one("dl", class_="analysis").one("dd").text == "<script>alert(1)</script>"
        assert article.find("script") == []

    async def test_a_channel_name_is_escaped_wherever_it_appears(self, service):
        # Kick's own names are tame, but an imported clip is filed under
        # whatever was typed on the command line.
        name = '<u>x</u>" onmouseover="alert(1)'
        moment_id = add_moment(name, clip_path="imports/clip.mp4")
        add_streamer(name, 7)

        page = await review(service, channel=name)

        assert page.find("u") == []
        assert open_moment(page).one("h1").text == name
        assert open_moment(page).attrs["data-channel"] == name
        assert page.one("section", class_="queue-group").one("b").text == name
        assert [option.text for option in page.find("option", selected=True)] == [name]
        (row,) = queue_rows(page)
        assert row.attrs["href"] == view.review_url(channel=name, moment=moment_id)
        assert "onmouseover" not in row.attrs and "onmouseover" not in open_moment(page).attrs

        watchlist = await channels(service)

        assert watchlist.find("u") == []
        assert watchlist.one("table").one("tbody").one("tr").find("td")[0].text == name


class TestPaging:
    @pytest.fixture(autouse=True)
    def five_moments_two_per_page(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "MOMENTS_PAGE_SIZE", 2)
        self.moments = [add_moment(detected_at=T0 + minutes(number)) for number in range(5)]

    def pages(self, page: Element) -> tuple[str, dict[str, str]]:
        """What the paging line says and where its links lead."""
        paging = page.one("nav", class_="queue-pages")
        return paging.one("span").text, {link.text: link.attrs["href"] for link in paging.find("a")}

    async def test_the_first_page_holds_the_newest(self, service):
        page = await review(service)

        assert queued(page) == self.moments[4:2:-1]
        assert self.pages(page) == ("1 to 2 of 5", {"Older": "/dashboard?offset=2"})

    async def test_a_middle_page_links_both_ways(self, service):
        page = await review(service, offset=2)

        assert queued(page) == self.moments[2:0:-1]
        assert self.pages(page) == ("3 to 4 of 5", {"Newer": "/dashboard", "Older": "/dashboard?offset=4"})

    async def test_the_last_page_only_links_back(self, service):
        page = await review(service, offset=4)

        assert queued(page) == self.moments[:1]
        assert self.pages(page) == ("5 to 5 of 5", {"Newer": "/dashboard?offset=2"})

    async def test_each_page_opens_its_own_first_moment(self, service):
        page = await review(service, offset=2)

        assert opened(page) == self.moments[2]
        assert queue_rows(page)[0].attrs["href"] == f"/dashboard?offset=2&moment={self.moments[2]}"

    async def test_a_negative_offset_is_the_first_page(self, service):
        page = await review(service, offset=-10)

        assert queued(page) == self.moments[4:2:-1]

    async def test_an_offset_past_the_end_is_the_last_page(self, service):
        # A list shrinks as its moments are rated; the page that was there
        # must not turn into an empty one.
        page = await review(service, offset=50)

        assert queued(page) == self.moments[:1]
        assert self.pages(page)[0] == "5 to 5 of 5"

    async def test_a_list_that_fits_one_page_has_no_paging(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "MOMENTS_PAGE_SIZE", 5)

        assert (await review(service)).find("nav", class_="queue-pages") == []


class TestChannelFilter:
    @pytest.fixture(autouse=True)
    def moments_of_two_channels(self, service):
        add_streamer("channel_a", 1)
        self.first = add_moment("channel_a", detected_at=T0)
        self.gone = add_moment("no_longer_watched", detected_at=T0 + minutes(1), rating=5)
        self.second = add_moment("channel_a", detected_at=T0 + minutes(2))

    def options(self, page: Element) -> list[Element]:
        return page.one("select", id="channel-filter").find("option")

    async def test_offers_every_channel_that_has_moments_watched_or_not(self, service):
        page = await review(service)

        assert [(option.attrs["value"], option.text) for option in self.options(page)] == [
            ("", "All channels"),
            ("channel_a", "channel_a"),
            ("no_longer_watched", "no_longer_watched"),
        ]
        assert [option.text for option in self.options(page) if "selected" in option.attrs] == ["All channels"]

    async def test_filters_the_list_and_the_counts(self, service):
        page = await review(service, channel="channel_a")

        assert queued(page) == [self.second, self.first]
        assert tabs(page) == ["Unrated 2", "All 2", "Best 0"]
        assert [option.text for option in self.options(page) if "selected" in option.attrs] == ["channel_a"]
        assert page.one("aside", class_="queue").attrs["data-channel"] == "channel_a"

    async def test_an_empty_choice_means_every_channel(self, service):
        # What the form sends for "All channels".
        page = await review(service, channel="")

        assert tabs(page) == ["Unrated 2", "All 3", "Best 1"]

    async def test_the_lists_and_the_pages_keep_the_filter(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "MOMENTS_PAGE_SIZE", 1)

        page = await review(service, channel="channel_a")

        queue = page.one("aside", class_="queue")
        assert [tab.attrs["href"] for tab in queue.find("a", class_="ch-tab")] == [
            "/dashboard?channel=channel_a",
            "/dashboard?show=all&channel=channel_a",
            "/dashboard?show=best&channel=channel_a",
        ]
        assert [link.attrs["href"] for link in page.one("nav", class_="queue-pages").find("a")] == [
            "/dashboard?channel=channel_a&offset=1"
        ]

    async def test_choosing_a_channel_keeps_the_list(self, service):
        page = await review(service, show="best")

        form = page.one("form", class_="queue-filter")
        assert [(field.attrs["name"], field.attrs["value"]) for field in form.find("input")] == [("show", "best")]

    async def test_a_channel_without_moments_shows_an_empty_list(self, service):
        page = await review(service, channel="nobody")

        assert queue_rows(page) == [] and opened(page) is None
        assert page.one("h1").text == "No moments from nobody yet"


class TestNothingToReview:
    """The normal state: moments are rare, so most visits find none waiting."""

    async def test_says_since_when_nothing_is_new(self, service):
        add_moment(detected_at=T0 - minutes(90), rating=2)
        add_moment(detected_at=T0 - minutes(30), rating=4)

        page = await review(service)

        assert queue_rows(page) == [] and opened(page) is None
        assert page.one("p", class_="queue-empty").text == "No unrated moments"
        assert (
            page.one("h1").text
            == f"Nothing new since {view.since_words((T0 - minutes(30)).astimezone(), T0.astimezone().date())}"
        )

    async def test_offers_the_rated_clips_instead(self, service):
        add_moment(rating=2)

        statement = (await review(service)).one("section", class_="statement")

        assert [(link.text, link.attrs["href"]) for link in statement.find("a")] == [
            ("Go through rated clips", "/dashboard?show=all")
        ]

    async def test_has_nothing_to_offer_when_there_are_no_clips_at_all(self, service):
        statement = (await review(service)).one("section", class_="statement")

        assert statement.find("a") == []

    async def test_says_what_the_service_is_doing_meanwhile(self, service, monkeypatch):
        add_streamer("channel_a", 1)
        add_streamer("channel_b", 2)

        async def lead() -> str:
            return (await review(service)).one("p", class_="lead").text

        assert (await lead()).startswith("None of the 2 watched channels is being recorded right now.")

        monkeypatch.setitem(recording_manager._recorders, "channel_a", RunningRecorder())
        assert (await lead()).startswith("1 of the 2 watched channels is live and being recorded.")

        await service.client.post("/settings/watching?enabled=0")
        assert await lead() == "Watching is off. Nothing new arrives until you turn it on."

    async def test_an_empty_best_list_says_what_would_be_on_it(self, service):
        add_moment(rating=1)

        page = await review(service, show="best")

        assert page.one("h1").text == f"No clips rated {view.BEST_RATING_MIN} or higher yet"
        assert page.one("p", class_="lead").text == f"A clip you rate {view.BEST_RATING_MIN} or higher is listed here."


class TestShuttingDown:
    async def test_in_normal_operation_nothing_says_so(self, service):
        for page in (await review(service), await channels(service)):
            assert page.find("div", class_="ch-banner-halt") == []
            assert page.find("button", disabled=True) == []

        button = (await channels(service)).one("button", data_shutdown_open=True)
        assert button.text == "Shut down"

    async def test_asking_to_shut_down_takes_a_second_step(self, service):
        section = (await channels(service)).one("section", data_shutdown=True)

        confirm = section.one("div", data_shutdown_confirm=True)
        assert "hidden" in confirm.attrs
        assert "Stops watching and shuts down once clips in progress are finished." in confirm.text
        assert [button.text for button in confirm.find("button")] == ["Shut down", "Cancel"]

    async def test_every_page_says_how_much_work_is_still_running(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        monkeypatch.setattr(service.main, "_moment_sessions", {"channel_a": object(), "channel_b": object()})

        for page in (await review(service), await channels(service)):
            banner = page.one("div", class_="ch-banner-halt")
            assert banner.text == f"Shutting down. {view.shutdown_words(2)}"
            assert "Waiting for 2 jobs to finish" in banner.text

    async def test_the_controls_that_no_longer_apply_are_disabled(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "_shutdown_requested", True)

        page = await channels(service)

        watching = page.one("span", aria_label="Watching")
        assert all("disabled" in half.attrs for half in watching.find("button"))
        button = page.one("button", data_shutdown_open=True)
        assert "disabled" in button.attrs and button.text == "Shutting down"
