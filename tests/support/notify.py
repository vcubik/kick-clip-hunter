"""Stand-ins for where notifications go: Telegram's Bot API and a Discord
channel's webhook.

`FakeTelegram` answers what the service asks of Telegram as a bot - who it
is (getMe), what was written to it (getUpdates), and the messages and
videos it sends (sendMessage, sendVideo) - in the shape Telegram answers
in: `{"ok": true, "result": ...}`, or `{"ok": false, "error_code": ...,
"description": ...}`. The HTML a message is written in is read the way
Telegram reads it: a tag it does not know, or a "<" that opens none, has
the message turned down, as is a "&" that begins none of its entities.
`FakeDiscord` is a channel's webhook: an embed,
or an embed and a file (multipart, with the embed as `payload_json`), each
checked against what Discord takes.

Like the other fakes they plug in as an `httpx` transport - through the
fake Kick's (the `telegram` and `discord` fixtures) - so the real client
code runs unmodified. Either can be told to answer the next requests with
something else (`refusals`): a status and what goes with it.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from html.parser import HTMLParser
from typing import Any

import httpx

TELEGRAM_HOST = "api.telegram.org"
TELEGRAM_TOKEN = "123456789:test-bot-token"
TELEGRAM_CHAT = "4242"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/123456789012345678/test-webhook-token"


def form(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    """A multipart/form-data body: each field by its name, as the file name
    it was sent with (None for a plain field) and its bytes."""
    request.read()
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.content)
    fields: dict[str, tuple[str | None, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fields[str(name)] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return fields


# The tags Telegram's HTML has, and the entities it knows. Every "<" and
# "&" that is not one of them has to be written as an entity.
_TELEGRAM_TAG = re.compile(r"</?(b|strong|i|em|u|ins|s|strike|del|a|code|pre|tg-spoiler|blockquote)\b[^<>]*>")
_TELEGRAM_ENTITY = re.compile(r"&(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")


class _TelegramHtml(HTMLParser):
    """The links of a message and the text it shows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.append(dict(attrs).get("href") or "")

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def read_telegram_html(text: str) -> tuple[str, list[str], str | None]:
    """The text a message shows, its links, and why Telegram would not take
    it - None where it would."""
    for at, mark in enumerate(text):
        if mark == "<" and not _TELEGRAM_TAG.match(text, at):
            return "", [], f"Unsupported start tag at byte offset {at}"
        if mark == "&" and not _TELEGRAM_ENTITY.match(text, at):
            return "", [], f"Character entity expected at byte offset {at}"
    reader = _TelegramHtml()
    reader.feed(text)
    reader.close()
    return "".join(reader.text), reader.links, None


@dataclass
class FakeTelegram:
    token: str = TELEGRAM_TOKEN
    # Where it answers: Telegram's own Bot API, or a server of one's own
    # (one more host, which TELEGRAM_API_URL would name).
    hosts: set[str] = field(default_factory=lambda: {TELEGRAM_HOST})
    username: str = "clip_hunter_bot"
    # The chats the bot may write in.
    chats: set[str] = field(default_factory=lambda: {TELEGRAM_CHAT})
    # What getUpdates answers with.
    updates: list[dict] = field(default_factory=list)
    # The largest file a bot may send.
    video_max_bytes: int = 50 * 1024**2
    # What the next requests are answered with instead, in turn: a status,
    # and what goes in the answer with it.
    refusals: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    # Every message and video sent, in order: the method, the chat, the
    # text (or caption) as sent and as shown, its links, how it was to be
    # read, and the file - its name and bytes - for a video.
    sent: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in self.hosts

    def shown(self) -> list[str]:
        """What each message showed, in order."""
        return [message["shown"] for message in self.sent]

    def _no(self, status: int, description: str, **more: Any) -> httpx.Response:
        return httpx.Response(status, json={"ok": False, "error_code": status, "description": description, **more})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        _bot, _, method = request.url.path.lstrip("/").partition("/")
        if _bot != f"bot{self.token}":
            return self._no(404 if not _bot.startswith("bot") else 401, "Unauthorized")
        if self.refusals:
            status, more = self.refusals.pop(0)
            description = more.pop("description", "Too Many Requests" if status == 429 else "Bad Request")
            return self._no(status, description, **more)
        if method == "getMe":
            result = {"id": 123456789, "is_bot": True, "first_name": "Clip hunter", "username": self.username}
            return httpx.Response(200, json={"ok": True, "result": result})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        if method == "sendMessage":
            body = json.loads(request.read())
            return self._take("sendMessage", body, str(body.get("text", "")), None)
        if method == "sendVideo":
            fields = form(request)
            plain = {name: value.decode() for name, (filename, value) in fields.items() if filename is None}
            video = fields.get("video")
            if video is None or video[0] is None:
                return self._no(400, "Bad Request: there is no video in the request")
            if len(video[1]) > self.video_max_bytes:
                return self._no(413, "Request Entity Too Large")
            return self._take("sendVideo", plain, plain.get("caption", ""), (video[0], video[1]))
        return self._no(404, "Not Found")

    def _take(self, method: str, body: dict, text: str, video: tuple[str, bytes] | None) -> httpx.Response:
        chat = str(body.get("chat_id", ""))
        if chat not in self.chats:
            return self._no(400, "Bad Request: chat not found")
        if not text and method == "sendMessage":
            return self._no(400, "Bad Request: message text is empty")
        if body.get("parse_mode") == "HTML":
            shown, links, trouble = read_telegram_html(text)
            if trouble:
                return self._no(400, f"Bad Request: can't parse entities: {trouble}")
        else:
            shown, links = text, []
        limit = 1024 if method == "sendVideo" else 4096
        if len(shown) > limit:
            return self._no(400, "Bad Request: message is too long")
        self.sent.append(
            {
                "method": method,
                "chat_id": chat,
                "text": text,
                "shown": shown,
                "links": links,
                "body": body,
                "video": video,
            }
        )
        result = {"message_id": len(self.sent), "chat": {"id": int(chat)}, "text": html.unescape(shown)}
        return httpx.Response(200, json={"ok": True, "result": result})


@dataclass
class FakeDiscord:
    url: str = DISCORD_WEBHOOK_URL
    # Deleted from its channel: every request is answered "Unknown Webhook".
    gone: bool = False
    # The largest file the channel's server takes.
    file_max_bytes: int = 20 * 1024**2
    refusals: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    # Every message posted, in order: what was posted (the payload) and the
    # file that went with it, as its name and bytes.
    posted: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in ("discord.com", "discordapp.com")

    def embeds(self) -> list[dict[str, Any]]:
        """The one embed of each message, in order."""
        return [message["payload"]["embeds"][0] for message in self.posted]

    def _no(self, status: int, message: str, code: int = 0, **more: Any) -> httpx.Response:
        return httpx.Response(status, json={"message": message, "code": code, **more})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if str(request.url.copy_with(query=None)) != self.url or request.method != "POST" or self.gone:
            return self._no(404, "Unknown Webhook", 10015)
        if self.refusals:
            status, more = self.refusals.pop(0)
            return self._no(status, more.pop("message", "You are being rate limited."), **more)
        file = None
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            fields = form(request)
            payload = json.loads(fields["payload_json"][1])
            sent = [(name, value) for name, value in fields.values() if name is not None]
            if sent:
                file = sent[0]
                if len(file[1]) > self.file_max_bytes:
                    return self._no(413, "Request entity too large", 40005)
        else:
            payload = json.loads(request.read())
        embeds = payload.get("embeds") or []
        if not payload.get("content") and not embeds and file is None:
            return self._no(400, "Cannot send an empty message", 50006)
        for embed in embeds:
            if len(embed.get("title", "")) > 256 or len(embed.get("description", "")) > 4096:
                return self._no(400, "Invalid Form Body", 50035)
        self.posted.append({"payload": payload, "file": file, "wait": request.url.params.get("wait")})
        return httpx.Response(200, json={"id": str(len(self.posted)), "type": 0, "embeds": embeds})
