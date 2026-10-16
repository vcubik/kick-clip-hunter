"""The Analyze page: a channel's stored chat in figures - whose chat and of
which period, the overview of that period, its messages, one chatter, and
the chat around one message.

Like the other page tests these read the parsed HTML. How the figures are
worked out and worded is tested where that is done
(tests/unit/test_chat_stats.py); here it is that the page asks for the right
stretch of the right channel's chat and shows what came of it.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

import pytest

from kick_clip_hunter import chat_stats, chat_trace, db
from kick_clip_hunter import dashboard_view as view
from tests.support.data import T0, add_chat, add_moment, add_streamer, minutes
from tests.support.html import Element, parse

pytestmark = pytest.mark.anyio

CHANNEL = "some_channel"
# When the pages are asked for: the evening after the chat most tests store.
NOW = T0 + timedelta(hours=3)
KICK_KEKW = "[emote:37226:KEKW]"
# One of Kick's emotes that nobody laughs with.
KICK_HEART = "[emote:99:emojiHeart]"
# How long a session of the chat these tests store runs.
SESSION = minutes(15)


@pytest.fixture(autouse=True)
def now(service, monkeypatch) -> datetime:
    """The clock the page goes by is pinned, and with it the day its dates
    are worded from."""
    monkeypatch.setattr(service.main, "_now", lambda: NOW.astimezone())
    monkeypatch.setattr(service.main, "_today", lambda: NOW.astimezone().date())
    return NOW


@pytest.fixture
def today() -> date:
    return NOW.astimezone().date()


async def analyze(service, **params) -> Element:
    response = await service.client.get("/dashboard/analyze", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return parse(response.text)


def between(start: datetime, end: datetime) -> dict[str, str]:
    """A period by its two ends, as the page's address carries it."""
    return {"from": chat_stats.local_text(start), "to": chat_stats.local_text(end)}


#: The ten minutes most tests store their chat in.
TEN_MINUTES = between(T0, T0 + minutes(10))


def say(
    second: float, sender: str = "alice", content: str = "hello", *, channel: str = CHANNEL, owner: int = 1
) -> None:
    """A message that arrived that many seconds after T0."""
    add_chat(channel, sender, content, T0 + timedelta(seconds=second), owner)


def session(start: datetime, *, messages: int = 30, long: timedelta = SESSION, owner: int = 1) -> None:
    """Chat that came in as one session: enough of it, for long enough."""
    for number in range(messages):
        add_chat(CHANNEL, f"viewer{number % 6}", f"line {number}", start + long * number / messages, owner)


def store_emotes(broadcaster_user_id: int, emotes: dict[str, tuple[str, int, int]]) -> None:
    conn = db.get_connection()
    try:
        db.replace_channel_emotes(conn, broadcaster_user_id, emotes)
    finally:
        conn.close()


def figures(page: Element) -> dict[str, tuple[str, str]]:
    """(the figure, its caption) by what each figure is of."""
    return {
        box.one("dt").text: (box.one("dd", class_="an-value").text, box.one("dd", class_="ch-cap").text)
        for box in page.one("dl", class_="an-figures").find("div")
    }


def table(page: Element, title: str) -> list[list[str]]:
    """The body of the table under a heading, a list of cell texts a row.
    A cell that holds a picture and no words says the picture's name."""
    section = page.one("section", aria_labelledby=title)
    rows = []
    for row in section.one("tbody").find("tr"):
        cells = [child for child in row.children if not isinstance(child, str)]
        rows.append([cell.text or " ".join(image.attrs["alt"] for image in cell.find("img")) for cell in cells])
    return rows


def lines(page: Element) -> list[tuple[str, str]]:
    """(who, what) for every message listed."""
    listed = []
    for line in page.find("div", class_="an-line"):
        chat = line.one("p", class_="ch-chat")
        listed.append((chat.one("b").text, chat.one("span").text))
    return listed


def tabs(nav: Element) -> dict[str, bool]:
    return {tab.text: tab.attrs.get("aria-current") == "true" for tab in nav.find("a", class_="ch-tab")}


class TestWithNoChatStored:
    async def test_the_page_says_so_and_points_to_the_channels(self, service):
        page = await analyze(service)

        statement = page.one("section", class_="statement")
        assert statement.one("h1").text == "No chat stored yet"
        assert statement.one("a").attrs["href"] == "/dashboard/channels"
        assert "disabled" in page.one("select", name="channel").attrs
        assert page.find("dl", class_="an-figures") == []

    async def test_the_bar_marks_the_page(self, service):
        page = await analyze(service)

        current = [link for link in page.one("nav", class_="bar-nav").find("a") if "aria-current" in link.attrs]
        assert [(link.text, link.attrs["href"]) for link in current] == [("Analyze", "/dashboard/analyze")]

    async def test_a_watched_channel_without_chat_is_not_offered(self, service):
        add_streamer(CHANNEL, 1)

        page = await analyze(service)

        assert page.one("section", class_="statement").one("h1").text == "No chat stored yet"


class TestWhoseChat:
    def offered(self, page: Element) -> list[str]:
        return [option.text for option in page.one("select", name="channel").find("option")]

    def chosen(self, page: Element) -> str:
        return page.one("select", name="channel").one("option", selected=True).text

    async def test_a_channel_goes_by_its_name_on_the_watchlist(self, service):
        add_streamer("Some_Channel", 1)
        say(0)  # stored under Kick's own spelling

        page = await analyze(service)

        assert self.offered(page) == ["Some_Channel"]
        assert page.one("header", class_="an-head").one("h1").text == "Some_Channel"

    async def test_one_that_left_the_watchlist_goes_by_the_name_its_chat_came_under(self, service):
        say(0, channel="gone_channel", owner=7)

        assert self.offered(await analyze(service)) == ["gone_channel"]

    async def test_channels_are_offered_in_the_order_of_their_names(self, service):
        add_streamer("beta", 2)
        add_streamer("Alpha", 3)
        say(0, channel="gamma", owner=1)
        say(0, channel="beta", owner=2)
        say(0, channel="alpha", owner=3)

        assert self.offered(await analyze(service)) == ["Alpha", "beta", "gamma"]

    async def test_with_none_asked_for_it_is_the_one_whose_chat_is_newest(self, service):
        say(0, channel="quiet_channel", owner=1)
        say(600, channel="busy_channel", owner=2)
        say(300, channel="other_channel", owner=3)

        assert self.chosen(await analyze(service)) == "busy_channel"

    @pytest.mark.parametrize("asked", ["Some_Channel", "some_channel"])
    async def test_a_channel_is_found_by_either_spelling_of_its_name(self, service, asked):
        add_streamer("Some_Channel", 1)
        say(0)
        say(600, channel="another_channel", owner=2)

        assert self.chosen(await analyze(service, channel=asked)) == "Some_Channel"

    async def test_a_channel_without_stored_chat_is_not_found(self, service):
        say(0)

        response = await service.client.get("/dashboard/analyze", params={"channel": "nobody"})

        assert response.status_code == 404

    async def test_only_that_channels_chat_is_counted(self, service):
        for second in range(3):
            say(second)
        for second in range(5):
            say(second, sender="stranger", channel="another_channel", owner=2)

        page = await analyze(service, channel=CHANNEL, **TEN_MINUTES)

        assert figures(page)["Messages"][0] == "3"
        assert [row[1] for row in table(page, "an-chatters")] == ["alice"]


class TestThePeriod:
    def period_words(self, page: Element) -> str:
        return page.one("header", class_="an-head").one("p").text

    def sessions(self, page: Element) -> list[Element]:
        return page.one("aside", class_="queue").find("a", class_="rec-row")

    async def test_with_none_asked_for_it_is_the_newest_session(self, service, today):
        session(T0 - timedelta(days=2))
        session(T0)

        page = await analyze(service)

        newest = chat_stats.whole_minutes(T0, T0 + minutes(14.5))
        assert self.period_words(page) == chat_stats.period_words(newest, today)
        assert figures(page)["Messages"][0] == "30"
        assert [row.attrs.get("aria-current") for row in self.sessions(page)] == ["true", None]
        assert tabs(page.one("nav", aria_label="Period")) == {
            "24 hours": False,
            "7 days": False,
            "30 days": False,
            "All": False,
        }

    async def test_the_sessions_are_listed_the_newest_first(self, service, today):
        session(T0 - timedelta(days=2), messages=40, long=minutes(20))
        session(T0, messages=30)

        rows = self.sessions(await analyze(service))

        assert [row.one("span", class_="rec-when").text.split(", ")[1] for row in rows] == [
            "30 messages",
            "40 messages",
        ]
        assert rows[1].one("b").text == view.sentence_case(
            view.day_words((T0 - timedelta(days=2)).astimezone().date(), today)
        )
        older = chat_stats.whole_minutes(T0 - timedelta(days=2), T0 - timedelta(days=2) + minutes(19.5))
        assert rows[1].attrs["href"] == chat_stats.analyze_url(CHANNEL, period=older)

    async def test_a_few_stray_messages_are_not_a_session(self, service):
        session(T0 - timedelta(days=1))
        for second in range(5):
            say(second)

        page = await analyze(service)

        assert len(self.sessions(page)) == 1
        # ... and so not what the page opens on either.
        assert figures(page)["Messages"][0] == "30"

    async def test_without_a_session_it_is_everything_that_is_stored(self, service):
        say(0)
        say(-86400, sender="bob")

        page = await analyze(service)

        assert figures(page)["Messages"][0] == "2"
        assert tabs(page.one("nav", aria_label="Period"))["All"] is True
        assert page.one("aside", class_="queue").one("p", class_="queue-empty").text == "No sessions yet"

    async def test_a_preset_reaches_back_from_now(self, service, today):
        say(0)
        say(-2 * 86400, sender="bob")
        say(-20 * 86400, sender="carol")

        day = await analyze(service, period="day")
        week = await analyze(service, period="week")
        everything = await analyze(service, period="all")

        assert [figures(page)["Messages"][0] for page in (day, week, everything)] == ["1", "2", "3"]
        assert tabs(week.one("nav", aria_label="Period")) == {
            "24 hours": False,
            "7 days": True,
            "30 days": False,
            "All": False,
        }
        assert self.period_words(week) == chat_stats.period_words(
            chat_stats.Period(NOW - timedelta(days=7), NOW), today
        )

    async def test_each_preset_leads_to_itself_for_this_channel(self, service):
        say(0)

        page = await analyze(service, channel=CHANNEL)

        assert [tab.attrs["href"] for tab in page.one("nav", aria_label="Period").find("a")] == [
            f"/dashboard/analyze?channel={CHANNEL}&period={name}" for name in ("day", "week", "month", "all")
        ]

    async def test_any_two_times_are_a_period(self, service):
        for second in (-1, 0, 599, 600):
            say(second)

        page = await analyze(service, **TEN_MINUTES)

        assert figures(page)["Messages"][0] == "2"
        fields = page.one("form", class_="an-range")
        assert fields.one("input", name="from").attrs["value"] == chat_stats.local_text(T0)
        assert fields.one("input", name="to").attrs["value"] == chat_stats.local_text(T0 + minutes(10))
        assert fields.one("input", name="channel").attrs["value"] == CHANNEL

    async def test_a_period_without_chat_says_so_and_offers_all_of_it(self, service):
        say(0)

        page = await analyze(service, **between(T0 - timedelta(days=9), T0 - timedelta(days=8)))

        empty = page.one("section", aria_labelledby="an-empty")
        assert empty.one("h2").text == "No chat in this period"
        assert empty.one("a", class_="ch-btn").attrs["href"] == f"/dashboard/analyze?channel={CHANNEL}&period=all"
        assert page.find("dl", class_="an-figures") == []

    async def test_what_is_not_a_time_is_as_good_as_no_period(self, service):
        session(T0)

        page = await analyze(service, **{"from": "yesterday", "to": "soon"})

        assert figures(page)["Messages"][0] == "30"


class TestTheOverview:
    async def test_says_how_much_was_written_by_how_many(self, service):
        for second, sender in enumerate(["alice", "alice", "bob", "carol", "alice"]):
            say(second, sender)

        shown = figures(await analyze(service, **TEN_MINUTES))

        assert shown["Messages"][0] == "5"
        assert shown["Chatters"][0] == "3"
        assert shown["Wrote once"] == ("67%", "2 chatters with one message")
        assert "New chatters" not in shown

    async def test_tells_who_had_not_written_before_once_there_is_a_before(self, service):
        say(-86400, "regular")
        say(0, "regular")
        say(1, "newcomer")

        shown = figures(await analyze(service, **TEN_MINUTES))

        assert shown["New chatters"] == ("1", "50% had not written here before")

    async def test_a_channels_own_laugh_emotes_count_as_laughing(self, service):
        service.watch(CHANNEL, 1, keywords={"kekw": 1.0, "catjam": 1.0})
        for second, said in enumerate(["KEKW", "xD", "catJAM", "ahoj"]):
            say(second, content=said)

        shown = figures(await analyze(service, **TEN_MINUTES))

        assert shown["Laughing"] == ("50%", "2 messages")

    async def test_the_chart_has_a_column_for_every_minute_of_a_short_period(self, service):
        say(125)
        say(130, content="xD")

        chart = (await analyze(service, **TEN_MINUTES)).one("section", aria_labelledby="an-activity")

        columns = chart.find(class_="an-column")
        assert len(columns) == 10
        assert [column.tag for column in columns[1:4]] == ["span", "a", "span"]
        assert columns[2].attrs["title"].endswith(": 2 messages, 1 laughing")
        assert len(columns[2].find("i", class_="an-all")) == 1
        assert len(columns[2].find("i", class_="an-laugh")) == 1
        assert columns[1].find("i") == []
        assert chart.one(class_="ch-readout").text == "a column is 1 minute"

    async def test_a_minute_of_the_chart_leads_to_its_messages_in_the_order_they_came(self, service):
        say(125)

        page = await analyze(service, **TEN_MINUTES)

        minute = chat_stats.Period(T0 + minutes(2), T0 + minutes(3))
        assert page.one("a", class_="an-column").attrs["href"] == chat_stats.analyze_url(
            CHANNEL, period=minute, view=chat_stats.VIEW_MESSAGES, order=chat_stats.ORDER_OLDEST
        )

    async def test_a_column_of_a_longer_period_leads_to_that_stretch_looked_at_closer(self, service):
        say(125)

        page = await analyze(service, **between(T0 - timedelta(days=1), T0 + minutes(10)))

        (column,) = page.one("section", aria_labelledby="an-activity").find("a", class_="an-column")
        opened = await service.client.get(column.attrs["href"])
        assert "view=messages" not in column.attrs["href"]
        assert figures(parse(opened.text))["Messages"][0] == "1"

    async def test_a_moment_is_marked_on_the_chart_and_counted(self, service):
        say(5)
        add_moment(CHANNEL, detected_at=T0 + timedelta(seconds=200))
        add_moment(CHANNEL, detected_at=T0 + timedelta(seconds=210))
        add_moment(CHANNEL, detected_at=T0 + minutes(30))
        add_moment("another_channel", detected_at=T0 + timedelta(seconds=30), broadcaster_user_id=2)
        add_moment(CHANNEL, detected_at=T0 + timedelta(seconds=40), reason=view.IMPORT_REASON)

        page = await analyze(service, **TEN_MINUTES)

        columns = page.one("section", aria_labelledby="an-activity").find(class_="an-column")
        assert [number for number, column in enumerate(columns) if "is-moment" in column.classes] == [3]
        assert columns[3].attrs["title"].endswith(": 0 messages, 2 moments")
        moments = [box for box in page.one("dl", class_="an-figures").find("div") if box.one("dt").text == "Moments"]
        assert moments[0].one("dd", class_="an-value").text == "2"
        assert moments[0].one("a").attrs["href"] == view.review_url(view.SHOW_ALL, CHANNEL)

    async def test_the_most_active_chatters_lead_to_what_they_wrote(self, service):
        for second, sender in enumerate(["bob", "alice", "bob", "carol", "bob", "alice"]):
            say(second, sender)

        page = await analyze(service, **TEN_MINUTES)

        assert [row[:4] for row in table(page, "an-chatters")] == [
            ["1", "bob", "3", "50%"],
            ["2", "alice", "2", "33%"],
            ["3", "carol", "1", "17%"],
        ]
        link = page.one("section", aria_labelledby="an-chatters").find("a")[0]
        assert link.attrs["href"] == chat_stats.analyze_url(
            CHANNEL, period=chat_stats.Period(T0, T0 + minutes(10)), chatter="bob"
        )

    async def test_emotes_are_counted_and_drawn_kicks_own_and_the_channels(self, service):
        store_emotes(1, {"catJAM": ("7tv-jam", 64, 32)})
        say(0, "alice", f"{KICK_KEKW} {KICK_KEKW}")
        say(1, "bob", "catJAM")
        say(2, "bob", "catjam is not how it is spelled")

        page = await analyze(service, **TEN_MINUTES)

        assert table(page, "an-emotes") == [["KEKW", "2", "1", "Kick"], ["catJAM", "1", "1", "7TV"]]
        pictures = page.one("section", aria_labelledby="an-emotes").find("img", class_="ch-emote")
        assert [picture.attrs["src"] for picture in pictures] == [
            view.KICK_EMOTE_IMAGE.format(id="37226"),
            view.SEVENTV_EMOTE_IMAGE.format(id="7tv-jam"),
        ]
        assert figures(page)["With an emote"] == ("67%", "2 messages")
        # The emote each chatter used most is beside their name.
        assert [row[4] for row in table(page, "an-chatters")] == ["catJAM", "KEKW"]

    async def test_a_period_without_emotes_says_so(self, service):
        say(0)

        section = (await analyze(service, **TEN_MINUTES)).one("section", aria_labelledby="an-emotes")

        assert section.find("table") == []
        assert section.one("p").text == "No emote was used in this period."

    async def test_what_was_said_most_and_who_was_addressed_most(self, service):
        for second, (sender, said) in enumerate(
            [("alice", "GG"), ("bob", "gg"), ("carol", "gg @alice"), ("bob", "@Alice díky"), ("dave", "once")]
        ):
            say(second, sender, said)

        page = await analyze(service, **TEN_MINUTES)

        assert table(page, "an-said") == [["gg", "2", "2"]]
        assert table(page, "an-mentioned") == [["alice", "2", "2"]]

    async def test_nothing_said_twice_and_nobody_addressed_are_said_in_words(self, service):
        say(0)

        page = await analyze(service, **TEN_MINUTES)

        assert page.one("section", aria_labelledby="an-said").find("table") == []
        assert page.one("section", aria_labelledby="an-mentioned").find("table") == []

    async def test_the_busiest_minutes_say_what_chat_said_most_in_them(self, service):
        for second in range(4):
            say(60 + second, f"viewer{second}", "KEKW")
        say(300, "alice", "ahoj")

        page = await analyze(service, **TEN_MINUTES)

        busiest = table(page, "an-busiest")
        assert [row[1:] for row in busiest] == [["4", f"KEKW {chat_trace.TIMES_SIGN}4"], ["1", ""]]
        link = page.one("section", aria_labelledby="an-busiest").find("a")[0]
        assert link.attrs["href"] == chat_stats.analyze_url(
            CHANNEL,
            period=chat_stats.Period(T0 + minutes(1), T0 + minutes(2)),
            view=chat_stats.VIEW_MESSAGES,
            order=chat_stats.ORDER_OLDEST,
        )

    async def test_the_tables_are_longer_when_asked(self, service):
        for number in range(30):
            say(number, f"viewer{number:02d}")

        short = await analyze(service, **TEN_MINUTES)
        long = await analyze(service, top=100, **TEN_MINUTES)

        assert len(table(short, "an-chatters")) == chat_stats.TOP_ROWS
        assert len(table(long, "an-chatters")) == 30
        more = [link.text for link in short.find("a", class_="ch-btn")]
        fewer = [link.text for link in long.find("a", class_="ch-btn")]
        assert ("Show 100 of each" in more, "Show 25 of each" in more) == (True, False)
        assert ("Show 100 of each" in fewer, "Show 25 of each" in fewer) == (False, True)

    async def test_short_tables_do_not_offer_to_be_longer(self, service):
        say(0)

        page = await analyze(service, top=7, **TEN_MINUTES)

        assert [link.text for link in page.find("a", class_="ch-btn")] == []

    async def test_the_hours_of_the_day_are_charted_once_there_is_more_than_one_day(self, service):
        say(0)

        one_day = await analyze(service, **TEN_MINUTES)
        say(-86400)
        two_days = await analyze(service, **between(T0 - timedelta(days=1), T0 + minutes(10)))

        assert one_day.find("section", aria_labelledby="an-hours") == []
        assert len(two_days.one("section", aria_labelledby="an-hours").find(class_="an-column")) == 24

    async def test_only_the_newest_messages_of_a_very_long_period_are_counted(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "ANALYZE_MESSAGE_LIMIT", 5)
        say(-86400, "regular")
        for second in range(8):
            say(second, "regular" if second < 3 else "newcomer")

        page = await analyze(service, **TEN_MINUTES)

        shown = figures(page)
        assert shown["Messages"][0] == "5"
        assert page.one("div", class_="an-in").find("p", class_="section-lead")[0].text == (
            "Only the newest 5 of this period's 8 messages were counted."
        )
        # Who is new is told from all of the period, so not beside these.
        assert "New chatters" not in shown

    async def test_a_period_is_counted_off_the_loop_the_webhook_is_answered_on(self, service, monkeypatch):
        say(0)
        loop_thread = threading.current_thread()
        counted_on = []
        count = service.main._analysis

        def watched(*args):
            counted_on.append(threading.current_thread())
            return count(*args)

        monkeypatch.setattr(service.main, "_analysis", watched)

        await analyze(service, **TEN_MINUTES)

        assert len(counted_on) == 1
        assert counted_on[0] is not loop_thread


class TestTheMessagesOfAPeriod:
    async def test_they_are_listed_the_newest_first_with_when_each_arrived(self, service, today):
        say(1, "alice", "first")
        say(62, "bob", "second")

        page = await analyze(service, view="messages", **TEN_MINUTES)

        assert lines(page) == [("bob", "second"), ("alice", "first")]
        assert [when.text for when in page.find("a", class_="an-when")] == [
            chat_stats.second_words(T0 + timedelta(seconds=62), today),
            chat_stats.second_words(T0 + timedelta(seconds=1), today),
        ]
        assert page.one("h2", id="an-found").text == "2 messages"
        assert tabs(page.one("header", class_="an-head").one("nav")) == {"Overview": False, "Messages": True}

    async def test_or_in_the_order_they_came(self, service):
        say(1, "alice", "first")
        say(62, "bob", "second")

        page = await analyze(service, view="messages", order="oldest", **TEN_MINUTES)

        assert lines(page) == [("alice", "first"), ("bob", "second")]
        assert tabs(page.one("nav", aria_label="Order")) == {"Newest first": False, "Oldest first": True}

    async def test_an_order_nobody_offers_is_the_newest_first(self, service):
        say(1, "alice", "first")
        say(62, "bob", "second")

        page = await analyze(service, view="messages", order="sideways", **TEN_MINUTES)

        assert lines(page) == [("bob", "second"), ("alice", "first")]

    async def test_they_come_a_page_at_a_time(self, service):
        for number in range(130):
            say(number, content=f"line {number}")

        first = await analyze(service, view="messages", **TEN_MINUTES)
        second = await analyze(service, view="messages", offset=100, **TEN_MINUTES)

        assert [said for _who, said in lines(first)] == [f"line {number}" for number in range(129, 29, -1)]
        assert [said for _who, said in lines(second)] == [f"line {number}" for number in range(29, -1, -1)]
        for pages in first.find("nav", class_="an-pages"):
            assert pages.one("span", class_="page-words").text == "1 to 100 of 130"
            assert [link.text for link in pages.find("a", class_="ch-btn")] == ["On"]
            assert "offset=100" in pages.one("a", rel="next").attrs["href"]
        for pages in second.find("nav", class_="an-pages"):
            assert [link.text for link in pages.find("a", class_="ch-btn")] == ["Back"]
            assert pages.one("span", class_="page-words").text == "101 to 130 of 130"

    async def test_their_pages_are_above_them_and_under_them(self, service):
        for number in range(130):
            say(number, content=f"line {number}")

        page = await analyze(service, view="messages", **TEN_MINUTES)

        above, under = page.find("nav", class_="an-pages")
        assert "an-pages-top" in above.classes
        assert [link.attrs["href"] for link in above.find("a")] == [link.attrs["href"] for link in under.find("a")]
        assert [(link.text, link.attrs.get("aria-current")) for link in under.find("a", class_="page-number")] == [
            ("1", "true"),
            ("2", None),
        ]

    async def test_a_page_past_the_end_is_the_last_one(self, service):
        for number in range(130):
            say(number, content=f"line {number}")

        page = await analyze(service, view="messages", offset=5000, **TEN_MINUTES)

        assert len(lines(page)) == 30

    async def test_a_period_that_fits_one_page_has_no_pages(self, service):
        say(0)

        assert (await analyze(service, view="messages", **TEN_MINUTES)).find("nav", class_="an-pages") == []

    async def test_every_message_leads_to_the_chat_around_it_and_to_its_writer(self, service):
        say(1, "alice", "hello")
        message_id = service.rows("SELECT message_id FROM chat_messages")[0]["message_id"]
        period = chat_stats.Period(T0, T0 + minutes(10))

        line = (await analyze(service, view="messages", **TEN_MINUTES)).one("div", class_="an-line")

        assert line.one("a", class_="an-when").attrs["href"] == chat_stats.analyze_url(
            CHANNEL, period=period, around=message_id
        )
        assert line.one("p").one("a").attrs["href"] == chat_stats.analyze_url(CHANNEL, period=period, chatter="alice")

    async def test_the_search_stays_in_the_channel_and_the_period(self, service):
        say(0)

        form = (await analyze(service, chatter="ali", q="hel", **TEN_MINUTES)).one("form", role="search")

        sent = {field.attrs["name"]: field.attrs["value"] for field in form.find("input")}
        assert sent == {"channel": CHANNEL, **TEN_MINUTES, "view": "messages", "chatter": "ali", "q": "hel"}

    async def test_the_search_of_a_preset_stays_in_the_preset(self, service):
        say(0)

        form = (await analyze(service, period="week")).one("form", role="search")

        sent = {field.attrs["name"]: field.attrs["value"] for field in form.find("input")}
        assert sent == {"channel": CHANNEL, "period": "week", "view": "messages", "chatter": "", "q": ""}


class TestLookingForAChatter:
    def chat(self) -> None:
        say(-86400, "Marmot", "from before the period")
        say(1, "Marmot", "xD")
        say(2, "alice", "@marmot ahoj")
        say(3, "Marmot", f"{KICK_HEART} to je síla @alice")
        say(4, "Marmot2", "someone else")
        say(5, "Marmot", "xD")

    async def test_their_messages_are_found_however_the_name_is_typed(self, service):
        self.chat()

        page = await analyze(service, chatter="marmot", **TEN_MINUTES)

        assert [who for who, _said in lines(page)] == ["Marmot"] * 3
        assert page.one("h2", id="an-found").text == "3 messages from Marmot"
        assert page.one("h2", id="an-chatter").text == "Marmot"

    async def test_the_page_says_what_there_is_to_say_of_them(self, service, today):
        self.chat()

        page = await analyze(service, chatter="Marmot", **TEN_MINUTES)

        shown = figures(page)
        assert shown["Messages"] == ("3", "60% of the chat, number 1 of 3 chatters")
        assert shown["First seen"][0] == view.date_words((T0 - timedelta(days=1)).astimezone().date(), today)
        assert shown["Laughing"] == ("67%", "2 messages")
        assert shown["Mentioned"] == ("1", "messages of others address them")
        card = page.one("section", class_="an-card")
        told = card.one("dl", class_="an-facts")
        facts = dict(zip([name.text for name in told.find("dt")], told.find("dd"), strict=True))
        assert [image.attrs["alt"] for image in facts["Emotes used most"].find("img")] == ["emojiHeart"]
        assert facts["Says most often"].text == "xD 2"
        assert facts["Addresses most"].text == "alice 1"
        assert len(card.find(class_="an-column")) == 10

    async def test_the_only_name_that_has_the_text_in_it_is_who_was_meant(self, service):
        say(1, "Marmot", "mine")
        say(2, "alice", "not mine")

        page = await analyze(service, chatter="armo", **TEN_MINUTES)

        assert lines(page) == [("Marmot", "mine")]

    async def test_a_name_nobody_has_gets_the_names_that_have_it_in_them(self, service):
        self.chat()

        page = await analyze(service, chatter="rmo", **TEN_MINUTES)

        section = page.one("section", aria_labelledby="an-nobody")
        assert section.one("h2").text == "Nobody called rmo wrote in this period"
        offered = section.one("ul", class_="an-alike").find("li")
        assert [item.text for item in offered] == ["Marmot 3 messages", "Marmot2 1 message"]
        assert offered[0].one("a").attrs["href"] == chat_stats.analyze_url(
            CHANNEL, period=chat_stats.Period(T0, T0 + minutes(10)), chatter="Marmot"
        )
        assert lines(page) == []

    async def test_a_name_that_is_in_nobodys_gets_nothing(self, service):
        self.chat()

        section = (await analyze(service, chatter="zed", **TEN_MINUTES)).one("section", aria_labelledby="an-nobody")

        assert section.find("ul") == []

    async def test_a_text_is_found_in_anyones_messages_however_it_is_capitalised(self, service):
        say(1, "alice", "ČAU všem")
        say(2, "bob", "no čau")
        say(3, "carol", "ahoj")

        page = await analyze(service, q="Čau", **TEN_MINUTES)

        assert lines(page) == [("bob", "no čau"), ("alice", "ČAU všem")]
        assert page.one("h2", id="an-found").text == '2 messages with "Čau" in them'
        assert page.find("section", class_="an-card") == []

    async def test_a_chatter_and_a_text_narrow_the_search_together(self, service):
        say(1, "alice", "gg wp")
        say(2, "alice", "ahoj")
        say(3, "bob", "gg")

        page = await analyze(service, chatter="alice", q="gg", **TEN_MINUTES)

        assert lines(page) == [("alice", "gg wp")]
        assert page.one("h2", id="an-found").text == '1 message from alice with "gg" in it'
        # What is said of the chatter is said of all they wrote in the period.
        assert figures(page)["Messages"][0] == "2"

    async def test_the_pages_of_a_search_stay_in_the_search(self, service):
        for number in range(130):
            say(number, "alice", f"gg {number}")

        page = await analyze(service, chatter="alice", q="gg", order="oldest", **TEN_MINUTES)

        onward = page.find("nav", class_="an-pages")[-1].one("a", rel="next").attrs["href"]
        # It opens at the messages, under what is said of the chatter.
        assert (
            onward
            == chat_stats.analyze_url(
                CHANNEL,
                period=chat_stats.Period(T0, T0 + minutes(10)),
                chatter="alice",
                text="gg",
                order=chat_stats.ORDER_OLDEST,
                offset=100,
            )
            + f"#{chat_stats.MESSAGES_ANCHOR}"
        )
        assert page.find("section", id=chat_stats.MESSAGES_ANCHOR) != []


class TestTheChatAroundAMessage:
    def ids(self, service) -> list[str]:
        return [row["message_id"] for row in service.rows("SELECT message_id FROM chat_messages ORDER BY received_at")]

    async def test_it_is_shown_in_the_order_it_was_written_with_the_message_marked(self, service, today):
        for number in range(5):
            say(number, f"viewer{number}", f"line {number}")

        page = await analyze(service, around=self.ids(service)[2], **TEN_MINUTES)

        assert [said for _who, said in lines(page)] == [f"line {number}" for number in range(5)]
        marked = [line for line in page.find("div", class_="an-line") if "is-asked" in line.classes]
        assert [line.one("p").one("span").text for line in marked] == ["line 2"]
        assert marked[0].attrs["id"] == chat_stats.MESSAGE_ANCHOR
        assert page.one("h2", id="an-around").text == (
            f"Chat around {chat_stats.second_words(T0 + timedelta(seconds=2), today)}"
        )
        assert tabs(page.one("header", class_="an-head").one("nav")) == {"Overview": False, "Messages": True}

    async def test_it_reaches_beyond_the_period_that_is_open(self, service):
        say(-30, content="before the period")
        say(5, content="asked")

        page = await analyze(service, around=self.ids(service)[1], **TEN_MINUTES)

        assert [said for _who, said in lines(page)] == ["before the period", "asked"]

    async def test_more_is_offered_either_way_only_where_there_may_be_more(self, service):
        reach = chat_stats.CONTEXT_MESSAGES
        for number in range(2 * reach + 20):
            say(number, content=f"line {number}")
        ids = self.ids(service)

        def offered(page: Element) -> list[str]:
            return [link.text for link in page.one("section", aria_labelledby="an-around").find("a", class_="ch-btn")]

        middle = await analyze(service, around=ids[reach + 10], **TEN_MINUTES)
        start = await analyze(service, around=ids[3], **TEN_MINUTES)
        end = await analyze(service, around=ids[-4], **TEN_MINUTES)

        assert offered(middle) == ["Earlier", "Later"]
        assert offered(start) == ["Later"]
        assert offered(end) == ["Earlier"]
        assert len(lines(middle)) == 2 * reach + 1
        earlier = middle.one("section", aria_labelledby="an-around").find("a", class_="ch-btn")[0]
        assert f"around={ids[10]}" in earlier.attrs["href"]

    async def test_a_message_the_channel_does_not_have_is_said_to_be_missing(self, service):
        say(0)
        say(0, channel="another_channel", owner=2)
        theirs = service.rows("SELECT message_id FROM chat_messages WHERE broadcaster_user_id = 2")[0]["message_id"]

        for asked in ("no-such-message", theirs):
            page = await analyze(service, channel=CHANNEL, around=asked)

            assert page.one("h2", id="an-around").text == f"That message is not in the stored chat of {CHANNEL}"
            assert lines(page) == []


class TestWhatChatWroteIsTextAndNeverMarkup:
    PAYLOAD = '<script>alert("xss")</script><img src=x onerror=alert(1)>'

    async def test_in_the_overview(self, service):
        say(0, self.PAYLOAD, self.PAYLOAD)
        say(1, "bob", self.PAYLOAD)
        say(2, "bob", f"[emote:1:{self.PAYLOAD}] @{self.PAYLOAD}")

        response = await service.client.get("/dashboard/analyze", params=TEN_MINUTES)
        page = parse(response.text)

        assert page.find("script", src=False) == []
        assert not [image for image in page.find("img") if "onerror" in image.attrs]
        assert table(page, "an-chatters")[1][1] == self.PAYLOAD
        assert table(page, "an-said")[0][0] == self.PAYLOAD

    async def test_in_the_messages_and_the_search_for_them(self, service):
        say(0, self.PAYLOAD, self.PAYLOAD)

        response = await service.client.get(
            "/dashboard/analyze", params={"chatter": self.PAYLOAD, "q": self.PAYLOAD, **TEN_MINUTES}
        )
        page = parse(response.text)

        assert page.find("script", src=False) == []
        assert not [image for image in page.find("img") if "onerror" in image.attrs]
        assert lines(page) == [(self.PAYLOAD, self.PAYLOAD)]
        assert page.one("form", role="search").one("input", name="q").attrs["value"] == self.PAYLOAD

    async def test_in_a_name_nobody_has(self, service):
        say(0)

        response = await service.client.get("/dashboard/analyze", params={"chatter": self.PAYLOAD, **TEN_MINUTES})
        page = parse(response.text)

        assert page.find("script", src=False) == []
        assert page.one("h2", id="an-nobody").text == f"Nobody called {self.PAYLOAD} wrote in this period"
