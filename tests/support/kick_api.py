"""An in-memory stand-in for the two HTTP APIs the service talks to.

`FakeKickApi` answers the same endpoints, with the same response shapes, as
Kick's public API (token, channel lookup, event subscriptions, webhook public
key) and 7TV's emote-set lookup. It plugs in as an `httpx` transport, so the
real client code runs unmodified - request building, auth headers, status
handling and JSON parsing are all exercised; only the network is replaced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx


def seventv_emote_id(name: str) -> str:
    """The id the fake 7TV gives an emote of that name, unless the channel
    says otherwise: made from the name, so two channels' emotes of one name
    are the same emote only if nothing is set to tell them apart."""
    return "7TV" + name.encode().hex().upper()


@dataclass
class FakeChannel:
    slug: str
    broadcaster_user_id: int
    is_live: bool = False
    # ISO timestamp of when the current stream started, as Kick reports it.
    start_time: str | None = None
    # 7TV emote names connected to the channel; None = no 7TV account at all.
    seventv_emotes: list[str] | None = field(default_factory=list)
    # False = a 7TV account that has no emote set selected ("emote_set": null).
    seventv_has_emote_set: bool = True
    # Emote ids by name, for the names that should not get seventv_emote_id's.
    seventv_ids: dict[str, object] = field(default_factory=dict)
    # (width, height) of an emote's smallest picture by name; 32x32 otherwise.
    # None = 7TV lists no picture files for it.
    seventv_sizes: dict[str, tuple[int, int] | None] = field(default_factory=dict)

    def seventv_emote(self, name: str) -> dict:
        """One entry of the channel's emote set, in the shape 7TV sends."""
        emote_id = self.seventv_ids.get(name, seventv_emote_id(name))
        size = self.seventv_sizes.get(name, (32, 32))
        files = []
        if size is not None:
            files = [
                {"name": f"{scale}x.{kind}", "width": size[0] * scale, "height": size[1] * scale}
                for kind in ("avif", "webp")
                for scale in (1, 2, 3, 4)
            ]
        return {
            "id": emote_id,
            "name": name,
            "data": {"id": emote_id, "host": {"url": f"//cdn.7tv.app/emote/{emote_id}", "files": files}},
        }


@dataclass
class FakeKickApi:
    channels: dict[str, FakeChannel] = field(default_factory=dict)
    # broadcaster_user_ids that currently have a chat.message.sent subscription.
    subscribed: list[int] = field(default_factory=list)
    public_key_pem: str = ""
    token_lifetime_seconds: int = 3600
    # What the channel lookup answers for a slug that does not exist. The
    # real API responds 400; None makes it return an empty result instead.
    unknown_slug_status: int | None = 400
    # Force an HTTP status for every request whose URL contains the key.
    failures: dict[str, int] = field(default_factory=dict)
    # broadcaster_user_ids whose subscription request is refused.
    subscribe_failures: set[int] = field(default_factory=set)
    # Every request received, as (method, url path, parsed body or query).
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    tokens_issued: int = 0

    def add_channel(self, slug: str, broadcaster_user_id: int, **kwargs) -> FakeChannel:
        channel = FakeChannel(slug=slug, broadcaster_user_id=broadcaster_user_id, **kwargs)
        self.channels[slug] = channel
        return channel

    def calls(self, method: str, path_fragment: str) -> list[object]:
        """Bodies/queries of the recorded requests matching a method and path."""
        return [body for m, path, body in self.requests if m == method and path_fragment in path]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if request.method == "POST" and request.content:
            content_type = request.headers.get("content-type", "")
            body: object = (
                json.loads(request.content)
                if "json" in content_type
                else dict(httpx.QueryParams(request.content.decode()))
            )
        else:
            body = dict(request.url.params)
        self.requests.append((request.method, f"{request.url.host}{path}", body))

        for fragment, status in self.failures.items():
            if fragment in url:
                return httpx.Response(status, json={"message": "forced failure"})

        if request.url.host == "id.kick.com" and path == "/oauth/token":
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"app-token-{self.tokens_issued}",
                    "token_type": "Bearer",
                    "expires_in": self.token_lifetime_seconds,
                },
            )

        if request.url.host == "api.kick.com":
            if path == "/public/v1/public-key":
                return httpx.Response(200, json={"data": {"public_key": self.public_key_pem}})
            if not request.headers.get("authorization", "").startswith("Bearer app-token-"):
                return httpx.Response(401, json={"message": "Unauthorized"})
            if path == "/public/v1/channels":
                channel = self.channels.get(request.url.params.get("slug", ""))
                if channel is None and self.unknown_slug_status is not None:
                    return httpx.Response(self.unknown_slug_status, json={"message": "Invalid request"})
                return httpx.Response(200, json={"data": [self._channel_json(channel)] if channel else []})
            if path == "/public/v1/events/subscriptions" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"broadcaster_user_id": user_id, "event": "chat.message.sent", "method": "webhook"}
                            for user_id in self.subscribed
                        ]
                    },
                )
            if path == "/public/v1/events/subscriptions" and request.method == "POST":
                if body["broadcaster_user_id"] in self.subscribe_failures:  # type: ignore[index]
                    return httpx.Response(500, json={"message": "Internal Server Error"})
                self.subscribed.append(body["broadcaster_user_id"])  # type: ignore[index]
                return httpx.Response(200, json={"data": [{"name": "chat.message.sent", "version": 1}]})

        if request.url.host == "7tv.io" and path.startswith("/v3/users/kick/"):
            user_id = int(path.rsplit("/", 1)[1])
            channel = next((c for c in self.channels.values() if c.broadcaster_user_id == user_id), None)
            if channel is None or channel.seventv_emotes is None:
                return httpx.Response(404, json={"error": "Unknown User"})
            if not channel.seventv_has_emote_set:
                return httpx.Response(200, json={"emote_set": None})
            return httpx.Response(
                200, json={"emote_set": {"emotes": [channel.seventv_emote(n) for n in channel.seventv_emotes]}}
            )

        return httpx.Response(404, json={"message": f"fake API has no route for {request.method} {url}"})

    @staticmethod
    def _channel_json(channel: FakeChannel) -> dict:
        return {
            "slug": channel.slug,
            "broadcaster_user_id": channel.broadcaster_user_id,
            "stream": {"is_live": channel.is_live, "start_time": channel.start_time},
        }
