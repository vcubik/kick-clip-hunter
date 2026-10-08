"""The dashboard page, rendered from a database the test filled in.

Assertions are made on the parsed HTML (tests/support/html.py), not on
substrings, so they survive template whitespace changes and can tell text
from markup - which matters for the escaping tests: chat messages and
usernames come from arbitrary Kick users and are shown on this page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import recorder
from kick_clip_hunter.timeutil import to_local
from tests.support.data import T0, add_chat, add_moment, add_streamer, minutes
from tests.support.html import parse

pytestmark = pytest.mark.anyio


async def dashboard(service, **params):
    response = await service.client.get("/dashboard", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return parse(response.text)


def moment_cards(page):
    return page.find("div", class_="moment")


def toggle_states(page) -> dict[str, str]:
    """Label -> ON/OFF for the global switches at the top of the page."""
    states = {}
    for row in page.find("div", class_="toggles"):
        for button in row.find("button", class_="toggle"):
            label, _, state = button.text.rpartition(":")
            states[label.strip()] = state.strip()
    return states


class TestEmptyInstallation:
    async def test_renders_with_nothing_in_the_database(self, service):
        page = await dashboard(service)

        assert "No streamers on the watchlist yet." in page.text
        assert "No moments detected yet." in page.text
        assert moment_cards(page) == []

    async def test_shows_the_default_switch_positions(self, service):
        page = await dashboard(service)

        assert toggle_states(page) == {
            "Watching": "ON",
            "Transcript": "OFF",
            "Audio events": "OFF",
            "Sound events": "OFF",
            "Frame embeddings": "OFF",
        }

    async def test_the_refresh_button_knows_there_is_nothing_yet(self, service):
        page = await dashboard(service)

        button = page.one("button", id="refresh-btn")
        assert (button.attrs["data-moments"], button.attrs["data-clips"]) == ("0", "0")


class TestWatchlist:
    async def test_lists_every_watched_channel_with_its_tracking_state(self, service):
        add_streamer("first_channel", 111)
        add_streamer("second_channel", 222, tracking=False)

        page = await dashboard(service)

        rows = [[cell.text for cell in row.find("td")] for row in page.one("table").find("tr")][1:]
        assert [(row[0], row[1], row[3]) for row in rows] == [
            ("first_channel", "111", "ON"),
            ("second_channel", "222", "OFF"),
        ]

    async def test_switches_reflect_what_was_set(self, service):
        await service.client.post("/settings/watching?enabled=0")
        await service.client.post("/settings/sound_events?enabled=1")

        states = toggle_states(await dashboard(service))

        assert states["Watching"] == "OFF"
        assert states["Sound events"] == "ON"
        assert states["Transcript"] == "OFF"


class TestMomentList:
    async def test_shows_moments_newest_first(self, service):
        add_moment("channel_a", detected_at=T0, reason="laugh")
        add_moment("channel_b", detected_at=T0 + minutes(5), reason="emotes")
        add_moment("channel_a", detected_at=T0 + minutes(2), reason="message_rate,laugh")

        page = await dashboard(service)

        reasons = [card.one("span", class_="reason").text for card in moment_cards(page)]
        assert reasons == ["emotes", "message_rate,laugh", "laugh"]
        assert "Detected moments (3)" in page.text

    async def test_a_card_carries_the_moments_numbers(self, service):
        add_moment("channel_a", detected_at=T0, score=12.3456, stream_elapsed_seconds=3723)

        (card,) = moment_cards(await dashboard(service))

        header = card.one("div", class_="moment-header").text
        assert "channel_a" in header
        assert to_local(T0.isoformat()) in header
        assert "stream time: 1:02:03" in header
        assert "score: 12.35" in header
        assert "12 msgs in detection window (1.20/s vs baseline 0.50/s)" in header
        assert "emotes: 3" in header and "keyword hits: 8" in header

    async def test_stream_time_is_unknown_when_it_was_not_recorded(self, service):
        add_moment(stream_elapsed_seconds=None)

        (card,) = moment_cards(await dashboard(service))

        assert "stream time: unknown" in card.text

    async def test_shows_the_chat_around_the_moment(self, service):
        add_moment("channel_a", detected_at=T0, reaction_seconds=10)
        add_chat("channel_a", "early_bird", "long before", T0 - timedelta(seconds=60))
        add_chat("channel_a", "alice", "what was that", T0 - timedelta(seconds=8))
        add_chat("channel_a", "bob", "xDDD", T0 - timedelta(seconds=3))
        add_chat("channel_a", "latecomer", "what did I miss", T0 + timedelta(seconds=60))
        add_chat("channel_b", "elsewhere", "another channel entirely", T0 - timedelta(seconds=5))

        (card,) = moment_cards(await dashboard(service))

        lines = [
            line.text for line in card.one("div", class_="snippet").find("div") if line.find("span", class_="sender")
        ]
        assert lines == ["alice: what was that", "bob: xDDD"]

    async def test_says_so_when_no_chat_was_stored_for_the_window(self, service):
        add_moment("channel_a")

        (card,) = moment_cards(await dashboard(service))

        assert "No chat messages found in this window." in card.text

    async def test_rating_tags_and_notes_are_shown_as_saved(self, service):
        add_moment(rating=4, stream_type="gaming", moment_type="fail", notes="good one, cut a bit late")

        (card,) = moment_cards(await dashboard(service))

        active_rating = [b.text for b in card.find("button", class_="rating-btn") if "active" in b.classes]
        active_tags = [b.text for b in card.find("button", class_="tag-btn") if "active" in b.classes]
        assert active_rating == ["4"]
        assert active_tags == ["Gaming", "Fail"]
        assert card.one("textarea", class_="notes-input").text == "good one, cut a bit late"

    async def test_an_unrated_moment_has_no_active_rating(self, service):
        add_moment()

        (card,) = moment_cards(await dashboard(service))

        assert [b.text for b in card.find("button", class_="rating-btn")] == ["1", "2", "3", "4", "5"]
        assert not any("active" in b.classes for b in card.find("button", class_="rating-btn"))

    async def test_analysis_results_are_shown_once_they_exist(self, service):
        add_moment(
            clip_path="channel_a/moment_1.mp4",
            transcript="tak to bylo neco",
            audio_events="cs, HAPPY, Laughter",
            sound_events="Laughter:0.80",
        )

        (card,) = moment_cards(await dashboard(service))

        assert "tak to bylo neco" in card.text
        assert "cs, HAPPY, Laughter" in card.text
        assert "Laughter:0.80" in card.text


class TestAnalysisInProgress:
    """A missing result is only shown as "on its way" while it can be."""

    PLACEHOLDERS = ("transcribing…", "detecting audio events…", "tagging sound events…")

    def fresh_clip(self, **columns) -> None:
        add_moment(detected_at=datetime.now(timezone.utc), clip_path="some_channel/moment_1.mp4", **columns)

    async def shown(self, service) -> list[str]:
        (card,) = moment_cards(await dashboard(service))
        return [placeholder for placeholder in self.PLACEHOLDERS if placeholder in card.text]

    async def test_nothing_is_in_progress_while_analysis_is_switched_off(self, service):
        # The default - and before this was fixed, every clip said
        # "transcribing..." forever.
        self.fresh_clip()

        assert await self.shown(service) == []

    async def test_a_fresh_clip_shows_exactly_the_steps_that_are_switched_on(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        await service.client.post("/settings/sound_events?enabled=1")
        self.fresh_clip()

        assert await self.shown(service) == ["transcribing…", "tagging sound events…"]

    async def test_a_result_that_has_arrived_replaces_its_placeholder(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        self.fresh_clip(transcript="to je konec")

        (card,) = moment_cards(await dashboard(service))
        assert "to je konec" in card.text and "transcribing…" not in card.text

    async def test_an_empty_result_means_done_not_pending(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        self.fresh_clip(transcript="")

        assert await self.shown(service) == []

    async def test_an_old_clip_without_a_result_is_not_pending_forever(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        long_ago = datetime.now(timezone.utc) - timedelta(seconds=service.main.ANALYSIS_PENDING_SECONDS + 60)
        add_moment(detected_at=long_ago, clip_path="some_channel/moment_1.mp4")

        assert await self.shown(service) == []

    async def test_a_moment_without_a_clip_has_nothing_to_analyse(self, service):
        await service.client.post("/settings/transcript?enabled=1")
        add_moment(detected_at=datetime.now(timezone.utc))

        assert await self.shown(service) == []


class TestClips:
    def clip_file(self, relative: str) -> None:
        path = recorder.CLIPS_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really video")

    async def test_a_moment_without_a_clip_has_no_player(self, service):
        add_moment()

        (card,) = moment_cards(await dashboard(service))

        assert card.find("video") == []

    async def test_a_moment_with_a_clip_embeds_it(self, service):
        add_moment("channel_a", clip_path="channel_a/moment_1.mp4")

        (card,) = moment_cards(await dashboard(service))

        assert [video.attrs["src"] for video in card.find("video")] == ["/clips/channel_a/moment_1.mp4"]
        assert card.find("details", class_="context") == []

    async def test_context_clips_are_offered_only_where_the_files_exist(self, service):
        add_moment("channel_a", detected_at=T0, clip_path="channel_a/moment_1.mp4")
        add_moment("channel_a", detected_at=T0 + minutes(1), clip_path="channel_a/moment_2.mp4")
        self.clip_file("channel_a/moment_1_before.mp4")
        self.clip_file("channel_a/moment_1_after.mp4")
        self.clip_file("channel_a/moment_2_after.mp4")

        newer, older = moment_cards(await dashboard(service))

        assert [video.attrs["src"] for video in older.one("details", class_="context").find("video")] == [
            "/clips/channel_a/moment_1_before.mp4",
            "/clips/channel_a/moment_1_after.mp4",
        ]
        assert [video.attrs["src"] for video in newer.one("details", class_="context").find("video")] == [
            "/clips/channel_a/moment_2_after.mp4"
        ]

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
        ],
    )
    async def test_the_clip_route_does_not_reach_outside_the_clip_directory(self, service, path):
        # The database sits one level above the clips it is served next to.
        add_moment()
        self.clip_file("channel_a/moment_1.mp4")

        response = await service.client.get(path)

        assert response.status_code == 404

    async def test_the_refresh_button_counts_moments_and_clips(self, service):
        add_moment(detected_at=T0)
        add_moment(detected_at=T0 + minutes(1), clip_path="some_channel/moment_2.mp4")
        add_moment(detected_at=T0 + minutes(2), clip_path="some_channel/moment_3.mp4")

        button = (await dashboard(service)).one("button", id="refresh-btn")

        assert (button.attrs["data-moments"], button.attrs["data-clips"]) == ("3", "2")


class TestUntrustedText:
    """Anything a Kick user can type ends up on this page."""

    PAYLOAD = '<script>alert("xss")</script><img src=x onerror=alert(1)>'

    async def test_chat_messages_and_usernames_are_escaped(self, service):
        add_moment("channel_a", detected_at=T0)
        add_chat("channel_a", "<b>mallory</b>", self.PAYLOAD, T0 - timedelta(seconds=2))

        page = await dashboard(service)

        (card,) = moment_cards(page)
        # Shown literally, as text...
        assert self.PAYLOAD in card.one("div", class_="snippet").text
        assert "<b>mallory</b>:" in card.text
        # ...and nothing of it became markup.
        assert card.find("script") == [] and card.find("img") == [] and card.find("b") == []
        assert len(page.find("script")) == 1  # the page's own

    async def test_notes_cannot_break_out_of_their_text_area(self, service):
        add_moment(notes='</textarea><script>alert("xss")</script>')

        (card,) = moment_cards(await dashboard(service))

        assert card.one("textarea").text == '</textarea><script>alert("xss")</script>'
        assert card.find("script") == []

    async def test_a_transcript_is_escaped_too(self, service):
        add_moment(clip_path="some_channel/moment_1.mp4", transcript="<script>alert(1)</script>")

        (card,) = moment_cards(await dashboard(service))

        assert "<script>alert(1)</script>" in card.text
        assert card.find("script") == []


class TestPaging:
    @pytest.fixture(autouse=True)
    def five_moments_two_per_page(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "MOMENTS_PAGE_SIZE", 2)
        for number in range(5):
            add_moment(detected_at=T0 + minutes(number), reason=f"moment-{number}")

    def reasons(self, page) -> list[str]:
        return [card.one("span", class_="reason").text for card in moment_cards(page)]

    def links(self, page) -> dict[str, str]:
        return {link.text: link.attrs["href"] for link in page.find("a", class_="page-link")}

    async def test_the_first_page_holds_the_newest(self, service):
        page = await dashboard(service)

        assert self.reasons(page) == ["moment-4", "moment-3"]
        assert "Showing 1-2 of 5" in page.text
        assert self.links(page) == {"Older →": "?channel=&offset=2"}

    async def test_a_middle_page_links_both_ways(self, service):
        page = await dashboard(service, offset=2)

        assert self.reasons(page) == ["moment-2", "moment-1"]
        assert "Showing 3-4 of 5" in page.text
        assert self.links(page) == {"← Newer": "?channel=&offset=0", "Older →": "?channel=&offset=4"}

    async def test_the_last_page_only_links_back(self, service):
        page = await dashboard(service, offset=4)

        assert self.reasons(page) == ["moment-0"]
        assert "Showing 5-5 of 5" in page.text
        assert self.links(page) == {"← Newer": "?channel=&offset=2"}

    async def test_a_negative_offset_is_the_first_page(self, service):
        page = await dashboard(service, offset=-10)

        assert self.reasons(page) == ["moment-4", "moment-3"]

    async def test_an_offset_past_the_end_shows_nothing_but_still_renders(self, service):
        page = await dashboard(service, offset=50)

        assert moment_cards(page) == []


class TestChannelFilter:
    @pytest.fixture(autouse=True)
    def moments_of_two_channels(self, service):
        add_streamer("channel_a", 1)
        add_moment("channel_a", detected_at=T0, reason="a-1")
        add_moment("no_longer_watched", detected_at=T0 + minutes(1), reason="gone-1")
        add_moment("channel_a", detected_at=T0 + minutes(2), reason="a-2")

    async def test_offers_every_channel_that_has_moments_watched_or_not(self, service):
        page = await dashboard(service)

        options = [option.text for option in page.one("select", id="channel-filter").find("option")]
        assert options == ["All", "channel_a", "no_longer_watched"]

    async def test_filters_the_list_and_the_count(self, service):
        page = await dashboard(service, channel="channel_a")

        assert [card.one("span", class_="reason").text for card in moment_cards(page)] == ["a-2", "a-1"]
        assert "Detected moments (2)" in page.text
        selected = [o.text for o in page.one("select", id="channel-filter").find("option") if "selected" in o.attrs]
        assert selected == ["channel_a"]

    async def test_paging_links_keep_the_filter(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "MOMENTS_PAGE_SIZE", 1)

        page = await dashboard(service, channel="channel_a")

        assert [link.attrs["href"] for link in page.find("a", class_="page-link")] == ["?channel=channel_a&offset=1"]

    async def test_a_channel_without_moments_shows_an_empty_list(self, service):
        page = await dashboard(service, channel="nobody")

        assert moment_cards(page) == []
        assert "Detected moments (0)" in page.text


class TestShutdownBanner:
    async def test_is_absent_in_normal_operation(self, service):
        page = await dashboard(service)

        assert page.find("div", class_="shutdown-banner") == []
        assert "disabled" not in page.one("button", class_="shutdown-btn").attrs

    async def test_appears_with_the_amount_of_work_still_running(self, service, monkeypatch):
        monkeypatch.setattr(service.main, "_shutdown_requested", True)
        monkeypatch.setattr(service.main, "_moment_sessions", {"channel_a": object(), "channel_b": object()})

        page = await dashboard(service)

        assert "waiting for 2 in-flight item(s)" in page.one("div", class_="shutdown-banner").text
        assert "disabled" in page.one("button", class_="shutdown-btn").attrs
