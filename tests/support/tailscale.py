"""A stand-in for the machine's Tailscale client.

tunnel.py runs `tailscale ... --json` and reads what it prints.
`FakeTailscale` takes the place of the `subprocess` module inside tunnel.py
and answers the two commands it uses in the shapes the real client prints
them (copied from Tailscale 1.102), so everything but starting a process is
exercised: which commands are asked, how their answers are read, what a
client that is missing, slow or failing comes to.
"""

from __future__ import annotations

import json
import subprocess

HOST = "machine.tailnet.ts.net"
ADDRESS = f"https://{HOST}/webhooks/kick"


def serve_config(
    port: int = 8000,
    *,
    mount: str = "/webhooks/kick",
    target: str | None = None,
    funnel: bool = True,
    site: str = f"{HOST}:443",
) -> dict:
    """What `tailscale funnel status --json` prints for one path forwarded
    to a local port - by default the set-up the README describes."""
    config: dict = {
        "TCP": {site.rpartition(":")[2]: {"HTTPS": True}},
        "Web": {site: {"Handlers": {mount: {"Proxy": target or f"http://127.0.0.1:{port}{mount.rstrip('/')}"}}}},
    }
    if funnel:
        config["AllowFunnel"] = {site: True}
    return config


class FakeTailscale:
    # What tunnel.py catches, under the names it knows them by.
    SubprocessError = subprocess.SubprocessError
    CalledProcessError = subprocess.CalledProcessError
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, installed: bool = True) -> None:
        self.installed = installed
        # The one name the client can be started by; None is any.
        self.only_as: str | None = None
        # What `tailscale status --json` says of itself.
        self.backend = "Running"
        self.health: list[str] = []
        # What `tailscale funnel status --json` prints; {} is a client that
        # serves nothing.
        self.serve: dict = serve_config()
        # Set to have every command fail with this, or print this.
        self.fails: Exception | None = None
        self.prints: str | None = None
        # The arguments of every command that was run, in order.
        self.commands: list[list[str]] = []

    def run(self, command: list[str], **options) -> subprocess.CompletedProcess:
        if not self.installed or self.only_as not in (None, command[0]):
            raise FileNotFoundError(command[0])
        arguments = list(command[1:])
        self.commands.append(arguments)
        if self.fails is not None:
            raise self.fails
        if self.prints is not None:
            printed = self.prints
        elif arguments == ["status", "--json"]:
            printed = json.dumps(
                {"BackendState": self.backend, "Health": self.health, "Self": {"DNSName": f"{HOST}.", "Online": True}}
            )
        elif arguments == ["funnel", "status", "--json"]:
            printed = json.dumps(self.serve)
        else:
            raise AssertionError(f"tunnel.py ran a Tailscale command the fake does not know: {arguments}")
        return subprocess.CompletedProcess(command, 0, stdout=printed, stderr="")
