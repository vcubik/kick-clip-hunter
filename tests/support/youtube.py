"""An in-memory stand-in for YouTube, as far as the service asks it anything.

`FakeYouTube` answers what the service asks of YouTube's own web API
(InnerTube, www.youtube.com/youtubei/v1): where an address on youtube.com
leads, what a channel's page says of it, what a video's player says of it -
in the shapes YouTube answers in, cut down to what is read of them. Like the
other fakes it plugs in as an `httpx` transport (the `youtube` fixture), so
the real client code runs unmodified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

HOSTS = frozenset({"www.youtube.com"})
API = "/youtubei/v1/"


def channel_id_of(handle: str) -> str:
    """The id the fake gives a channel of that handle, unless told
    otherwise: "UC" and 22 more characters, as YouTube's are."""
    return "UC" + hashlib.sha256(handle.lower().encode()).hexdigest()[:22]


def not_found() -> httpx.Response:
    return httpx.Response(
        404, json={"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}}
    )


@dataclass
class FakeYouTubeChannel:
    # As YouTube writes it, without the "@"; None for a channel without one.
    handle: str | None
    channel_id: str
    title: str = ""
    # Addresses of an older kind that lead to its page: "c/<name>", "user/<name>".
    old_addresses: list[str] = field(default_factory=list)
    # The video its /live address leads to - and whether that video is live
    # (otherwise it is one the channel has scheduled), since when.
    live_video: str | None = None
    is_live: bool = False
    started_at: str | None = None
    # Its videos besides that one, by id.
    videos: list[str] = field(default_factory=list)

    def go_live(self, video_id: str = "livevideo01", started_at: str | None = "2026-03-01T20:00:00+00:00") -> None:
        self.live_video, self.is_live, self.started_at = video_id, True, started_at

    def schedule(self, video_id: str = "upcoming001") -> None:
        """A stream it has scheduled, which /live leads to until it begins."""
        self.live_video, self.is_live, self.started_at = video_id, False, None

    def go_offline(self) -> None:
        if self.live_video:
            self.videos.append(self.live_video)
        self.live_video, self.is_live, self.started_at = None, False, None

    def owns_video(self, video_id: str) -> bool:
        return video_id == self.live_video or video_id in self.videos


@dataclass
class FakeYouTube:
    channels: list[FakeYouTubeChannel] = field(default_factory=list)
    # Force an HTTP status for every request whose URL contains the key.
    failures: dict[str, int] = field(default_factory=dict)
    # Every request received, as (method, path, parsed body or query).
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    # What the player says of a video instead of its details, by video id -
    # e.g. YouTube asking the visitor to sign in.
    player_refusals: dict[str, dict] = field(default_factory=dict)

    def add_channel(self, handle: str | None, channel_id: str | None = None, **kwargs) -> FakeYouTubeChannel:
        channel = FakeYouTubeChannel(
            handle=handle, channel_id=channel_id or channel_id_of(handle or "no handle"), **kwargs
        )
        self.channels.append(channel)
        return channel

    def calls(self, method: str, path_fragment: str) -> list[object]:
        """Bodies/queries of the recorded requests matching a method and path."""
        return [body for m, path, body in self.requests if m == method and path_fragment in path]

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in HOSTS

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and request.content:
            body: object = json.loads(request.content)
        else:
            body = dict(request.url.params)
        self.requests.append((request.method, path, body))
        for fragment, status in self.failures.items():
            if fragment in str(request.url):
                return httpx.Response(status, json={"error": {"code": status, "message": "forced failure"}})
        if request.method == "POST" and path.startswith(API):
            if not isinstance(body, dict) or not isinstance(body.get("context", {}).get("client"), dict):
                return httpx.Response(400, json={"error": {"code": 400, "message": "no client in the context"}})
            endpoint = path.removeprefix(API)
            if endpoint == "navigation/resolve_url":
                return self._resolve(body["url"])
            if endpoint == "browse":
                return self._browse(body["browseId"])
            if endpoint == "player":
                return self._player(body["videoId"])
        return httpx.Response(404, text=f"fake YouTube has no route for {request.method} {request.url}")

    # -- what is asked ---------------------------------------------------------

    def by_handle(self, handle: str) -> FakeYouTubeChannel | None:
        return next((c for c in self.channels if c.handle is not None and c.handle.lower() == handle.lower()), None)

    def by_id(self, channel_id: str) -> FakeYouTubeChannel | None:
        return next((c for c in self.channels if c.channel_id == channel_id), None)

    def _resolve(self, url: str) -> httpx.Response:
        parts = urlsplit(url)
        pieces = [unquote(piece) for piece in parts.path.split("/") if piece]
        if pieces[:1] == ["watch"] or pieces[:1] == ["live"]:
            video_id = parse_qs(parts.query).get("v", [""])[0] if pieces[0] == "watch" else pieces[1]
            return _endpoint({"watchEndpoint": {"videoId": video_id}})
        live = pieces[-1:] == ["live"] and len(pieces) > 1
        if live:
            pieces = pieces[:-1]
        if not pieces:
            return _endpoint({"browseEndpoint": {"browseId": "FEwhat_to_watch"}})
        channel: FakeYouTubeChannel | None
        if pieces[0].startswith("@"):
            channel = self.by_handle(pieces[0][1:])
        elif pieces[0] == "channel" and len(pieces) > 1:
            channel = self.by_id(pieces[1])
            if channel is None:
                # YouTube leads to the page of any id, there or not.
                return _endpoint({"browseEndpoint": {"browseId": pieces[1]}})
        else:
            channel = next((c for c in self.channels if "/".join(pieces[:2]) in c.old_addresses), None)
        if channel is None:
            return not_found()
        if live and channel.live_video is not None:
            return _endpoint({"watchEndpoint": {"videoId": channel.live_video}})
        return _endpoint({"browseEndpoint": {"browseId": channel.channel_id, "params": "EgRsaXZl"}})

    def _browse(self, browse_id: str) -> httpx.Response:
        channel = self.by_id(browse_id)
        if channel is None:
            return httpx.Response(200, json={"responseContext": {}, "alerts": [{"alertRenderer": {}}]})
        metadata = {
            "title": channel.title or channel.handle or "",
            "externalId": channel.channel_id,
            "channelUrl": f"https://www.youtube.com/channel/{channel.channel_id}",
        }
        if channel.handle is not None:
            metadata["vanityChannelUrl"] = f"http://www.youtube.com/@{quote(channel.handle)}"
        return httpx.Response(200, json={"responseContext": {}, "metadata": {"channelMetadataRenderer": metadata}})

    def _player(self, video_id: str) -> httpx.Response:
        if video_id in self.player_refusals:
            return httpx.Response(200, json={"playabilityStatus": self.player_refusals[video_id]})
        channel = next((c for c in self.channels if c.owns_video(video_id)), None)
        if channel is None:
            return httpx.Response(200, json={"playabilityStatus": {"status": "ERROR", "reason": "Video unavailable"}})
        streaming = video_id == channel.live_video
        details: dict = {"videoId": video_id, "channelId": channel.channel_id, "author": channel.title}
        broadcast: dict = {}
        if streaming and channel.is_live:
            details["isLive"] = True
            broadcast = {"isLiveNow": True, "startTimestamp": channel.started_at}
        elif streaming:
            details["isUpcoming"] = True
            broadcast = {"isLiveNow": False, "startTimestamp": "2099-01-01T00:00:00+00:00"}
        return httpx.Response(
            200,
            json={
                "playabilityStatus": {"status": "OK"},
                "videoDetails": details,
                "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": broadcast}},
            },
        )


def _endpoint(endpoint: dict) -> httpx.Response:
    return httpx.Response(200, json={"responseContext": {}, "endpoint": endpoint})
