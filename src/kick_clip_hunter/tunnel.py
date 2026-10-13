"""Whether Kick can reach the webhook receiver: where the tunnel in front of
it stands.

Kick pushes chat rather than being asked for it, so the service is only of
use while something carries requests from the internet to it - on this
project's host a Tailscale Funnel (see the README). When that is down
nothing fails: chat simply stops arriving, exactly as it does when a stream
ends. So the Health page asks the local Tailscale client: is Tailscale
connected, and does a Funnel forward the webhook's path to the port this
service answers on?

That is Tailscale's account of its own configuration, not a delivery made
from outside. A Funnel that is set up right can still be out of reach for a
reason only the other end sees, and a service reached some other way (a
cloudflared tunnel, a reverse proxy) is none of Tailscale's business. The
proof either way is the last webhook that did arrive, which the page shows
beside this.

`read` is the pure part - what the client's two JSON answers come to - and
`look` runs the client.
"""

import json
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

TAILSCALE = "tailscale"
# Where the Windows installer puts the client. It is on PATH there as a
# rule, but not for every way a service can be started.
TAILSCALE_ON_WINDOWS = r"C:\Program Files\Tailscale\tailscale.exe"
COMMAND_TIMEOUT_SECONDS = 5

# The path Kick posts to (main.kick_webhook).
WEBHOOK_PATH = "/webhooks/kick"

# Where the Funnel stands.
UP = "up"
# There is no Tailscale client on this machine.
NOT_INSTALLED = "not_installed"
# There is one, and it could not be asked.
NOT_ANSWERING = "not_answering"
# Tailscale is not connected. On Windows it says "NoState" for as long as
# its tray app is not running.
NOT_CONNECTED = "not_connected"
# Nothing forwards the webhook's path anywhere.
NO_ROUTE = "no_route"
# Something does, but only inside the tailnet: Funnel is not on for it.
TAILNET_ONLY = "tailnet_only"
# The Funnel forwards to a port this service does not answer on.
OTHER_PORT = "other_port"

# What Tailscale calls its backend while it is connected.
CONNECTED = "Running"


@dataclass(frozen=True)
class Funnel:
    state: str
    # The webhook's address on the internet, and what that is forwarded to -
    # where something forwards it at all.
    address: str | None = None
    target: str | None = None
    # What Tailscale had to say where it was not connected or could not be
    # asked, in its own words.
    said: str = ""
    # The warnings Tailscale has of its own (its "Health"), as it words them.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Route:
    address: str
    target: str
    port: int | None
    public: bool


def _port(target: str) -> int | None:
    """The port a forwarding target is on, or None if it can't be told."""
    try:
        parts = urlsplit(target if "://" in target else f"http://{target}")
        return parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None


def _mount(handlers: dict, path: str) -> str | None:
    """The handler a request for `path` goes to: the longest mount point
    that is the path or a folder above it, as Tailscale picks one."""
    found = [mount for mount in handlers if path == mount or path.startswith(mount.rstrip("/") + "/")]
    return max(found, key=len, default=None)


def _routes(serve: dict) -> list[_Route]:
    """Everything in Tailscale's serve configuration that forwards the
    webhook's path to an address."""
    routes = []
    public = serve.get("AllowFunnel") or {}
    for site, served in (serve.get("Web") or {}).items():
        handlers = (served or {}).get("Handlers") or {}
        mount = _mount(handlers, WEBHOOK_PATH)
        target = (handlers.get(mount) or {}).get("Proxy") if mount is not None else None
        if not target:
            continue
        host, _, site_port = site.rpartition(":")
        address = f"https://{host}{'' if site_port == '443' else f':{site_port}'}{WEBHOOK_PATH}"
        routes.append(_Route(address, target, _port(target), bool(public.get(site))))
    return routes


def read(status: dict, serve: dict, port: int | None = None) -> Funnel:
    """Where the Funnel stands, from what `tailscale status --json` and
    `tailscale funnel status --json` answered. `port` is the one this
    service answers on, where that is known; with it a Funnel that forwards
    somewhere else is told apart from one that is right."""
    warnings = tuple(str(warning) for warning in status.get("Health") or ())
    backend = str(status.get("BackendState") or "")
    if backend != CONNECTED:
        return Funnel(NOT_CONNECTED, said=backend, warnings=warnings)

    routes = _routes(serve)
    if not routes:
        return Funnel(NO_ROUTE, warnings=warnings)

    def right(route: _Route) -> bool:
        return port is None or route.port is None or route.port == port

    # The one that is nearest to working is the one worth talking about.
    route = max(routes, key=lambda route: (route.public, right(route)))
    if not route.public:
        state = TAILNET_ONLY
    else:
        state = UP if right(route) else OTHER_PORT
    return Funnel(state, address=route.address, target=route.target, warnings=warnings)


def _ask(*arguments: str) -> dict:
    """The Tailscale client's answer to a command that answers in JSON."""
    for client in (TAILSCALE, TAILSCALE_ON_WINDOWS):
        try:
            done = subprocess.run(
                [client, *arguments],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
        except FileNotFoundError:
            continue
        answer = json.loads(done.stdout)
        if not isinstance(answer, dict):
            raise ValueError(f"tailscale {' '.join(arguments)} did not answer with an object")
        return answer
    raise FileNotFoundError(TAILSCALE)


def _why(error: Exception) -> str:
    """Why the client could not be asked, in a few words."""
    if isinstance(error, subprocess.TimeoutExpired):
        return f"no answer within {COMMAND_TIMEOUT_SECONDS} seconds"
    if isinstance(error, subprocess.CalledProcessError):
        said = (error.stderr or error.stdout or "").strip().splitlines()
        return said[0] if said else f"exit code {error.returncode}"
    return type(error).__name__


def look(port: int | None = None) -> Funnel:
    """Asks the local Tailscale client where the Funnel stands. Blocks for
    as long as the client takes to answer, so it is called from a worker
    thread."""
    try:
        status = _ask("status", "--json")
        # A client that is not connected has nothing to say about what it
        # serves, and on Windows may not answer the question at all.
        serve = _ask("funnel", "status", "--json") if status.get("BackendState") == CONNECTED else {}
    except FileNotFoundError:
        return Funnel(NOT_INSTALLED)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return Funnel(NOT_ANSWERING, said=_why(error))
    return read(status, serve, port)
