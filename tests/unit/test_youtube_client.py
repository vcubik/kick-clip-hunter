"""What the service asks of YouTube's own web API: who a channel is, and
whether it is live (see youtube_client.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from kick_clip_hunter import youtube_client
from kick_clip_hunter.youtube_client import Channel, ChatPage, LiveChat, LiveVideo
from tests.support.youtube import PAGE_CLIENT_VERSION, VISITOR, channel_id_of, message

pytestmark = pytest.mark.anyio


class TestFindingAChannel:
    async def test_a_handle_leads_to_the_channel_of_that_handle(self, youtube):
        youtube.add_channel("Some.Channel", title="Some Channel")

        assert await youtube_client.find_channel("Some.Channel") == Channel(
            channel_id_of("Some.Channel"), "Some Channel", "Some.Channel"
        )

    async def test_capitals_make_no_difference_to_a_handle(self, youtube):
        youtube.add_channel("Some.Channel")

        channel = await youtube_client.find_channel("some.channel")

        assert channel.handle == "Some.Channel"
        assert channel.login == "some.channel"

    @pytest.mark.parametrize(
        "link",
        [
            "https://www.youtube.com/channel/{id}",
            "https://www.youtube.com/channel/{id}/live",
            "https://www.youtube.com/c/SomeChannel/videos",
            "https://www.youtube.com/watch?v=oldvideo001",
            "https://www.youtube.com/watch?v=livevideo01",
        ],
    )
    async def test_a_link_to_anything_of_a_channel_leads_to_the_channel(self, youtube, link):
        channel = youtube.add_channel("some_channel", old_addresses=["c/SomeChannel"], videos=["oldvideo001"])
        channel.go_live("livevideo01")

        found = await youtube_client.find_channel(link.format(id=channel.channel_id))

        assert (found.channel_id, found.handle) == (channel.channel_id, "some_channel")

    async def test_a_channel_is_asked_about_as_youtubes_web_client(self, youtube):
        youtube.add_channel("some_channel")

        await youtube_client.find_channel("some_channel")

        (resolved,) = youtube.calls("POST", "/youtubei/v1/navigation/resolve_url")
        assert resolved["url"] == "https://www.youtube.com/@some_channel"
        assert resolved["context"]["client"]["clientName"] == "WEB"
        assert resolved["context"]["client"]["clientVersion"] == youtube_client.CLIENT_VERSION
        (browsed,) = youtube.calls("POST", "/youtubei/v1/browse")
        assert browsed["browseId"] == channel_id_of("some_channel")

    async def test_a_handle_in_another_script_is_read_as_youtube_writes_it(self, youtube):
        youtube.add_channel("Český-Kanál")

        channel = await youtube_client.find_channel("český-kanál")

        assert (channel.handle, channel.login) == ("Český-Kanál", "český-kanál")
        assert youtube.calls("POST", "resolve_url")[0]["url"].endswith("/@%C4%8Desk%C3%BD-kan%C3%A1l")

    async def test_a_channel_without_a_handle_is_known_by_its_id(self, youtube):
        channel = youtube.add_channel(None, old_addresses=["user/OldName"])

        found = await youtube_client.find_channel("https://www.youtube.com/user/OldName")

        assert (found.handle, found.login) == (None, channel.channel_id)

    @pytest.mark.parametrize("handle", ["some/channel", "some..channel", ".channel", "channel.", "some channel"])
    async def test_a_handle_not_fit_to_name_anything_here_is_not_taken(self, youtube, handle):
        channel = youtube.add_channel(handle, old_addresses=["c/SomeChannel"])

        found = await youtube_client.find_channel("https://www.youtube.com/c/SomeChannel")

        assert found.login == channel.channel_id

    @pytest.mark.parametrize(
        "where",
        [
            "no_such_channel",
            "https://www.youtube.com/c/NoSuchChannel",
            "https://www.youtube.com/watch?v=nosuchvideo",
            f"https://www.youtube.com/channel/{channel_id_of('nobody')}",
            "https://www.youtube.com/",
        ],
    )
    async def test_what_leads_to_no_channel_is_said_to(self, youtube, where):
        youtube.add_channel("some_channel")

        with pytest.raises(ValueError, match=r"No YouTube channel|YouTube has nothing"):
            await youtube_client.find_channel(where)

    async def test_youtube_failing_is_not_taken_for_a_channel_that_is_not_there(self, youtube):
        youtube.add_channel("some_channel")
        youtube.failures["resolve_url"] = 503

        with pytest.raises(httpx.HTTPStatusError):
            await youtube_client.find_channel("some_channel")


class TestWhetherAChannelIsLive:
    async def test_a_channel_streaming_is_live_since_youtube_says(self, youtube):
        youtube.add_channel("some_channel").go_live("livevideo01", started_at="2026-03-01T20:00:00+00:00")

        assert await youtube_client.live_video("some_channel") == LiveVideo(
            "livevideo01", datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
        )

    async def test_a_channel_streaming_nothing_is_not_live(self, youtube):
        youtube.add_channel("some_channel")

        assert await youtube_client.live_video("some_channel") is None
        # Where /live leads says it all: no video's player is asked.
        assert youtube.calls("POST", "/player") == []

    async def test_a_stream_that_is_only_scheduled_is_not_live_yet(self, youtube):
        youtube.add_channel("some_channel").schedule("upcoming001")

        assert await youtube_client.live_video("some_channel") is None

    async def test_its_live_address_is_asked_where_it_leads(self, youtube):
        youtube.add_channel("some_channel").go_live("livevideo01")

        await youtube_client.live_video("some_channel")

        assert youtube.calls("POST", "resolve_url")[0]["url"] == "https://www.youtube.com/@some_channel/live"
        assert youtube.calls("POST", "/player")[0]["videoId"] == "livevideo01"

    async def test_a_channel_known_by_its_id_is_asked_about_by_it(self, youtube):
        channel = youtube.add_channel(None)
        channel.go_live("livevideo01")

        assert await youtube_client.live_video(channel.channel_id) is not None
        assert youtube.calls("POST", "resolve_url")[0]["url"] == (
            f"https://www.youtube.com/channel/{channel.channel_id}/live"
        )

    async def test_a_stream_youtube_gives_no_start_for_is_live_since_nobody_knows(self, youtube):
        youtube.add_channel("some_channel").go_live("livevideo01", started_at=None)

        assert await youtube_client.live_video("some_channel") == LiveVideo("livevideo01", None)

    async def test_a_handle_youtube_no_longer_has_is_said_to(self, youtube):
        with pytest.raises(ValueError, match="YouTube has nothing"):
            await youtube_client.live_video("gone_channel")

    async def test_youtube_asking_to_sign_in_is_said_to(self, youtube):
        youtube.add_channel("some_channel").go_live("livevideo01")
        youtube.player_refusals["livevideo01"] = {
            "status": "LOGIN_REQUIRED",
            "reason": "Sign in to confirm you're not a bot",
        }

        with pytest.raises(youtube_client.Refused, match="not a bot"):
            await youtube_client.live_video("some_channel")

    async def test_a_player_that_says_nothing_of_the_video_is_not_guessed_at(self, youtube):
        youtube.add_channel("some_channel").go_live("livevideo01")
        youtube.player_refusals["livevideo01"] = {"status": "ERROR", "reason": "Video unavailable"}

        with pytest.raises(youtube_client.YouTubeChanged, match="ERROR"):
            await youtube_client.live_video("some_channel")

    async def test_a_client_that_is_given_is_the_one_asked_with(self, youtube):
        youtube.add_channel("some_channel")

        async with youtube_client.new_client() as client:
            assert await youtube_client.live_video("some_channel", client) is None
            assert not client.is_closed


class TestReadingAChat:
    async def test_a_chat_is_read_in_the_view_with_every_message_in_it(self, youtube):
        youtube.chat("livevideo01")

        chat = await youtube_client.open_chat("livevideo01")

        assert chat.continuation == "livevideo01|live|0"
        assert youtube.calls("GET", "/live_chat") == [{"v": "livevideo01", "is_popout": "1"}]

    async def test_it_is_asked_about_as_its_page_says_to_ask(self, youtube):
        youtube.chat("livevideo01")

        chat = await youtube_client.open_chat("livevideo01")
        await youtube_client.look_at_chat(chat)

        assert chat.context["client"]["visitorData"] == VISITOR
        (_, _, asked_as), *_ = youtube.chat("livevideo01").looks
        assert asked_as["client"]["visitorData"] == VISITOR

    async def test_the_version_its_page_is_on_is_gone_by_from_then_on(self, youtube):
        youtube.chat("livevideo01")

        await youtube_client.open_chat("livevideo01")

        assert youtube_client.client_context()["client"]["clientVersion"] == PAGE_CLIENT_VERSION

    async def test_the_first_look_brings_what_was_written_before_and_where_to_look_next(self, youtube):
        youtube.chat("livevideo01").history = [message("m-1", "alice", "earlier")]

        page = await youtube_client.look_at_chat(await youtube_client.open_chat("livevideo01"))

        assert [action["addChatItemAction"]["clientId"] for action in page.actions] == ["m-1"]
        assert page.continuation == "livevideo01|live|1"

    async def test_each_look_after_brings_what_came_since(self, youtube):
        chat = youtube.chat("livevideo01")
        chat.say(message("m-2", "bob", "now"))
        opened = await youtube_client.open_chat("livevideo01")

        page = await youtube_client.look_at_chat(opened.at("livevideo01|live|1"))
        nothing = await youtube_client.look_at_chat(opened.at("livevideo01|live|2"))

        assert [action["addChatItemAction"]["clientId"] for action in page.actions] == ["m-2"]
        assert nothing == ChatPage([], "livevideo01|live|3")

    async def test_a_chat_that_is_over_says_nowhere_to_look_next(self, youtube):
        youtube.chat("livevideo01").ended = True

        page = await youtube_client.look_at_chat(await youtube_client.open_chat("livevideo01"))

        assert page.continuation is None

    async def test_an_answer_without_a_live_chat_in_it_is_a_chat_that_is_over(self):
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"responseContext": {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            page = await youtube_client.look_at_chat(LiveChat("v", "c", {}), client)

        assert page == ChatPage([], None)

    async def test_a_chat_that_is_turned_off_says_so(self, youtube):
        youtube.chat("livevideo01").unavailable = "Chat is disabled for this live stream."

        with pytest.raises(youtube_client.ChatUnavailable, match="Chat is disabled"):
            await youtube_client.open_chat("livevideo01")

    @pytest.mark.parametrize(
        ("page", "said"),
        [
            ("<html>nothing here</html>", "no chat on it"),
            (
                '<script>window["ytInitialData"] = {"contents": {"liveChatRenderer": {}}};</script>',
                "does not say where",
            ),
        ],
    )
    async def test_a_page_without_what_is_read_of_it_is_not_guessed_at(self, page, said):
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=page)

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            with pytest.raises(youtube_client.YouTubeChanged, match=said):
                await youtube_client.open_chat("livevideo01", client)

    async def test_without_a_choice_of_views_the_chat_is_read_where_the_page_says(self):
        data = {
            "contents": {
                "liveChatRenderer": {
                    "continuations": [{"timedContinuationData": {"continuation": "the-only-one", "timeoutMs": 5000}}]
                }
            }
        }

        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=f'<script>window["ytInitialData"] = {json.dumps(data)};</script>')

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            chat = await youtube_client.open_chat("livevideo01", client)

        assert chat == LiveChat("livevideo01", "the-only-one", youtube_client.client_context())


class TestAskingYouTube:
    async def test_an_answer_that_is_not_json_is_said_to_be_what_it_is(self):
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>consent</html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            with pytest.raises(youtube_client.YouTubeChanged, match="not JSON"):
                await youtube_client.ask("player", {"videoId": "x"}, client)

    async def test_an_answer_that_is_not_an_object_is_said_to_be_what_it_is(self):
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["something", "else"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            with pytest.raises(youtube_client.YouTubeChanged, match="not an object"):
                await youtube_client.ask("player", {"videoId": "x"}, client)

    async def test_an_address_that_leads_nowhere_youtube_can_say_is_not_guessed_at(self):
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"responseContext": {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            with pytest.raises(youtube_client.YouTubeChanged, match="did not say where"):
                await youtube_client.live_video("some_channel", client)

    def test_the_version_the_site_says_it_is_on_is_gone_by_from_then_on(self):
        youtube_client.remember_client_version("2.20271231.01.00")

        assert youtube_client.client_context()["client"]["clientVersion"] == "2.20271231.01.00"

    @pytest.mark.parametrize("version", [None, "", "latest", "2.2027", 2.2])
    def test_what_is_no_version_is_not_taken_for_one(self, version):
        youtube_client.remember_client_version(version)

        assert youtube_client.client_context()["client"]["clientVersion"] == youtube_client.CLIENT_VERSION

    @pytest.mark.parametrize(
        ("login", "page"),
        [
            ("some_channel", "https://www.youtube.com/@some_channel"),
            ("český", "https://www.youtube.com/@%C4%8Desk%C3%BD"),
            ("UCAAAAAAAAAAAAAAAAAAAAAA", "https://www.youtube.com/channel/UCAAAAAAAAAAAAAAAAAAAAAA"),
            ("ucaaaaaaaaaaaaaaaaaaaaaa", "https://www.youtube.com/@ucaaaaaaaaaaaaaaaaaaaaaa"),
        ],
    )
    def test_a_channels_page_is_where_the_name_it_is_known_by_says(self, login, page):
        assert youtube_client.channel_page(login) == page
