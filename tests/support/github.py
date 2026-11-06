"""A stand-in for GitHub as the reels reach it (see reels.py): the releases of
one repository and their files, the repository_dispatch events that start
its workflows, and the runs of those.

It answers as GitHub's REST API does: a release by its tag (404 where there
is none), a release made, a file taken out of one; a file put into one on
uploads.github.com (refused where the release already has a file of that
name, or the body is not as long as Content-Length says); a file handed
over as a redirect to objects.githubusercontent.com, which must be reached
without the token - as GitHub's own storage would refuse it. Every call to
the API needs the token.

What the workflows would do is the test's to do: `answer_storyboard` and
`answer_reel` put into a release what the storyboard and render workflows
put there, `runs` is what the Actions runs say. Like the other fakes it plugs
in through the fake Kick's transport (the `github` fixture), so the real
client code runs unmodified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

API_HOST = "api.github.com"
UPLOADS_HOST = "uploads.github.com"
OBJECTS_HOST = "objects.githubusercontent.com"
REPO = "someone/klipy-edit"
TOKEN = "github_pat_test"


@dataclass
class FakeRelease:
    id: int
    tag: str
    name: str
    # By name: the file's id and its bytes.
    assets: dict[str, tuple[int, bytes]] = field(default_factory=dict)


@dataclass
class FakeGitHub:
    repo: str = REPO
    token: str = TOKEN
    releases: dict[str, FakeRelease] = field(default_factory=dict)
    dispatches: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # The repository's Actions runs, newest first, as GitHub lists them.
    runs: list[dict[str, Any]] = field(default_factory=list)
    # Whether the token may read the repository's Actions.
    reads_actions: bool = True
    # Status codes to answer the next call to a path containing the key with
    # ("POST /releases": only a call of that method).
    failures: dict[str, int] = field(default_factory=dict)
    # Whether GitHub cannot be reached at all.
    unreachable: bool = False
    _next_id: int = 100

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in (API_HOST, UPLOADS_HOST, OBJECTS_HOST)

    # -- what the workflows would do ------------------------------------------

    def put(self, tag: str, name: str, data: bytes) -> None:
        release = self.releases[tag]
        release.assets[name] = (self._id(), data)

    def answer_storyboard(self, tag: str, storyboard: dict[str, Any], contact: bytes | None = b"jpeg") -> None:
        self.put(tag, "storyboard.json", json.dumps(storyboard).encode())
        if contact is not None:
            self.put(tag, "contact.jpg", contact)

    def answer_reel(self, tag: str, reel: bytes) -> None:
        self.put(tag, "reel.mp4", reel)

    def asset(self, tag: str, name: str) -> bytes | None:
        release = self.releases.get(tag)
        found = release.assets.get(name) if release else None
        return found[1] if found else None

    # -- GitHub ---------------------------------------------------------------

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _release_json(self, release: FakeRelease) -> dict[str, Any]:
        return {
            "id": release.id,
            "tag_name": release.tag,
            "name": release.name,
            "upload_url": f"https://{UPLOADS_HOST}/repos/{self.repo}/releases/{release.id}/assets{{?name,label}}",
            "assets": [
                {"id": asset_id, "name": name, "size": len(data)} for name, (asset_id, data) in release.assets.items()
            ],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("GitHub is down", request=request)
        path = request.url.path
        for key, status in list(self.failures.items()):
            method, _, fragment = key.rpartition(" ")
            if fragment in path and method in ("", request.method):
                del self.failures[key]
                return httpx.Response(status, json={"message": "Something went wrong"})
        if request.url.host == OBJECTS_HOST:
            if "authorization" in request.headers:
                return httpx.Response(400, text="Only one auth mechanism allowed")
            for release in self.releases.values():
                for asset_id, data in release.assets.values():
                    if path == f"/{asset_id}":
                        return httpx.Response(200, content=data)
            return httpx.Response(404)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        repo = f"/repos/{self.repo}"
        if request.url.host == UPLOADS_HOST:
            return self._upload(request, repo)
        if request.method == "GET" and (found := re.fullmatch(f"{repo}/releases/tags/(.+)", path)):
            release = self.releases.get(found[1])
            if release is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self._release_json(release))
        if request.method == "POST" and path == f"{repo}/releases":
            body = json.loads(request.content)
            if body["tag_name"] in self.releases:
                return httpx.Response(422, json={"message": "Validation Failed"})
            release = FakeRelease(self._id(), body["tag_name"], body.get("name", ""))
            self.releases[release.tag] = release
            return httpx.Response(201, json=self._release_json(release))
        if found := re.fullmatch(f"{repo}/releases/assets/(\\d+)", path):
            return self._asset(request, int(found[1]))
        if request.method == "POST" and path == f"{repo}/dispatches":
            body = json.loads(request.content)
            self.dispatches.append((body["event_type"], body.get("client_payload", {})))
            return httpx.Response(204)
        if request.method == "GET" and path == f"{repo}/actions/runs":
            if not self.reads_actions:
                return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
            return httpx.Response(200, json={"total_count": len(self.runs), "workflow_runs": self.runs})
        return httpx.Response(404, json={"message": "Not Found"})

    def _asset(self, request: httpx.Request, asset_id: int) -> httpx.Response:
        for release in self.releases.values():
            for name, (known, _) in list(release.assets.items()):
                if known != asset_id:
                    continue
                if request.method == "DELETE":
                    del release.assets[name]
                    return httpx.Response(204)
                if request.headers.get("accept") == "application/octet-stream":
                    return httpx.Response(302, headers={"Location": f"https://{OBJECTS_HOST}/{asset_id}?sig=x"})
                return httpx.Response(200, json={"id": asset_id, "name": name})
        return httpx.Response(404, json={"message": "Not Found"})

    def _upload(self, request: httpx.Request, repo: str) -> httpx.Response:
        found = re.fullmatch(f"{repo}/releases/(\\d+)/assets", request.url.path)
        release = next((r for r in self.releases.values() if found and r.id == int(found[1])), None)
        if request.method != "POST" or release is None:
            return httpx.Response(404, json={"message": "Not Found"})
        name = request.url.params.get("name", "")
        if name in release.assets:
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
        data = request.content
        if request.headers.get("content-length") != str(len(data)):
            return httpx.Response(400, json={"message": "Content-Length does not match the body"})
        release.assets[name] = (self._id(), data)
        return httpx.Response(201, json={"id": release.assets[name][0], "name": name, "size": len(data)})
