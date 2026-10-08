"""Suite-wide setup. Everything in here exists to keep the tests hermetic.

The application was written to be run from the repository root against a
real `data/` directory, real Kick credentials and a real browser, and parts
of it do their setup at import time (`main.py` opens its log file and mounts
`data/clips` as soon as it is imported). None of that may leak into a test
run, so:

* the whole session runs inside a throwaway working directory, entered before
  any application module is imported (`pytest_configure`);
* every test additionally gets its own working directory and its own SQLite
  file, so relative `data/...` paths and the database never carry state from
  one test to the next;
* module-level state (detector windows, caches, feature flags, open moment
  sessions) is reset around every test;
* three things a test must never do are blocked outright and reported as a
  failure even if the code under test swallows the error: talking to
  anything but loopback, launching a browser, and exiting the process.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Shared fixtures live in tests/support/fixtures.py; importing them here is
# what registers them for every test directory (the `x as x` form marks each
# one as a deliberate re-export rather than an unused import).
from tests.support.fixtures import hls as hls
from tests.support.fixtures import kick_api as kick_api
from tests.support.fixtures import recording as recording
from tests.support.fixtures import service as service
from tests.support.fixtures import signer as signer
from tests.support.fixtures import ts_segments as ts_segments
from tests.support.media import require_ffmpeg

_INVOCATION_DIR = Path.cwd()
_sandbox: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Runs before collection, i.e. before any test module imports the app."""
    global _sandbox
    _sandbox = Path(tempfile.mkdtemp(prefix="kick-clip-hunter-tests-"))
    os.chdir(_sandbox)

    # config.py reads these when the app is imported. Set here they also win
    # over a developer's real .env, which python-dotenv never lets override
    # variables that already exist.
    os.environ["KICK_CLIENT_ID"] = "test-client-id"
    os.environ["KICK_CLIENT_SECRET"] = "test-client-secret"

    # No test may talk TLS to anything (see `no_outbound_network`), so the
    # trust store is pointed at an empty directory. Every httpx client the
    # service creates would otherwise parse the full CA bundle - about a
    # third of a second each, hundreds of times over a run - and should one
    # ever get past the socket guard, it could not verify a real host.
    no_certificates = _sandbox / "no-ca-certificates"
    no_certificates.mkdir()
    os.environ["SSL_CERT_DIR"] = str(no_certificates)
    os.environ.pop("SSL_CERT_FILE", None)

    # The database path is absolute (anchored to the repository, not the
    # working directory), so it has to be redirected explicitly. Each test
    # then gets a file of its own - see `isolated_workdir`.
    from kick_clip_hunter import db

    db.DB_PATH = _sandbox / "data" / "kick_clip_hunter.db"


def pytest_sessionfinish(session: pytest.Session) -> None:
    # Back to where pytest was started before reports are written, so
    # relative report paths (coverage.xml, junit) land where they're expected.
    os.chdir(_INVOCATION_DIR)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _sandbox is None:
        return
    # The app's rotating log file lives in the sandbox; on Windows it can't
    # be deleted while a handler still holds it open.
    sandbox = _sandbox.resolve()
    for name in (None, "uvicorn"):
        target = logging.getLogger(name)
        for handler in list(target.handlers):
            if isinstance(handler, logging.FileHandler) and sandbox in Path(handler.baseFilename).resolve().parents:
                target.removeHandler(handler)
                handler.close()
    shutil.rmtree(_sandbox, ignore_errors=True)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marks tests by the directory they live in, so `-m "not integration"`
    works without every module having to repeat the marker."""
    for item in items:
        parts = Path(str(item.fspath)).parts
        if "integration" in parts or "e2e" in parts:
            item.add_marker(pytest.mark.integration)
        if "e2e" in parts:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(autouse=True)
def ffmpeg_for_marked_tests(request: pytest.FixtureRequest) -> None:
    """A test marked `ffmpeg` is skipped on a machine without the binaries -
    or failed, where the environment says they must be there (see
    `require_ffmpeg`)."""
    if request.node.get_closest_marker("ffmpeg") is not None:
        require_ffmpeg()


@pytest.fixture
def anyio_backend() -> str:
    """Async tests run on asyncio only - it is what the service runs on."""
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private working directory and database file for every test.

    All of the app's `data/...` paths are relative, so after this they point
    inside `tmp_path`; the one absolute path, the database, is redirected.
    """
    from kick_clip_hunter import db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "kick_clip_hunter.db")

    # The service commits after every statement, and every commit is an
    # fsync plus a rollback-journal file created and deleted - on a spinning
    # disk that is ~100 ms each, and building the schema of a fresh database
    # alone takes seconds. A test database only has to survive the test, so
    # crash-safety is switched off (no fsync, journal kept in memory);
    # nothing about what SQLite stores or returns changes.
    real_connect = sqlite3.connect

    def connect_without_fsync(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA journal_mode = MEMORY")
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_without_fsync)
    return tmp_path


def _reset_module_state() -> None:
    from kick_clip_hunter import detector, kick_client, recording_manager, webhook_security

    detector._entries.clear()
    detector._last_moment_at.clear()
    kick_client._token_cache.clear()
    webhook_security._public_key_cache = None
    for recorder in recording_manager._recorders.values():
        recorder.stop()
    recording_manager._recorders.clear()

    # Only if a test has imported the app - importing it here would make
    # every run pay for FastAPI even when just the detector is under test.
    main = sys.modules.get("kick_clip_hunter.main")
    if main is not None:
        main._flags.clear()
        main._moment_sessions.clear()
        main._background_tasks.clear()
        main._channel_keywords_cache.clear()
        main._stream_start_cache.clear()
        main._shutdown_requested = False


@pytest.fixture(autouse=True)
def clean_module_state():
    """The detector, the API clients and the app keep state in module
    globals; without this, test order would decide what a test sees."""
    _reset_module_state()
    yield
    _reset_module_state()


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch: pytest.MonkeyPatch):
    """Fails any test that tries to reach a host other than loopback.

    Name resolution and plain socket connects are both guarded, which covers
    httpx in its sync and async forms. The attempt is recorded and reported at
    teardown as well, because application code that catches broad exceptions
    (startup reconciliation, background tasks) would otherwise hide it.
    """
    attempts: list[str] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(host: object, port: object = "") -> None:
        if isinstance(host, bytes):
            host = host.decode()
        if host and host not in _LOOPBACK_HOSTS:
            attempts.append(f"{host}:{port}")
            raise RuntimeError(
                f"test tried to reach {host}:{port} - the suite must stay offline; "
                "use the fakes in tests/support instead"
            )

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        check(host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    def guarded_connect(self, address, *args, **kwargs):
        if isinstance(address, tuple):
            check(*address[:2])
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if isinstance(address, tuple):
            check(*address[:2])
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield attempts
    if attempts:
        pytest.fail(f"outbound network access attempted: {', '.join(attempts)}")


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch):
    """The real stream-URL capture and clip publishing drive a visible
    browser against kick.com. Tests replace the functions that need it; this
    makes forgetting to do so a failure rather than a window popping up."""
    from kick_clip_hunter import clip_creator, kick_stream

    launches: list[str] = []

    def refuse(*args, **kwargs):
        launches.append("sync_playwright")
        raise RuntimeError("test tried to launch a browser")

    monkeypatch.setattr(kick_stream, "sync_playwright", refuse)
    monkeypatch.setattr(clip_creator, "sync_playwright", refuse)
    yield launches
    if launches:
        pytest.fail("a browser launch was attempted")


class _ExitGuard:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.expected = False


@pytest.fixture(autouse=True)
def no_process_exit(monkeypatch: pytest.MonkeyPatch):
    """The dashboard's shutdown button ends the service with `os._exit`,
    which would take the test run down with it. Calls are recorded instead,
    and fail the test unless it asked for them (see `process_exits`)."""
    guard = _ExitGuard()
    monkeypatch.setattr(os, "_exit", guard.codes.append)
    yield guard
    if guard.codes and not guard.expected:
        pytest.fail(f"os._exit({guard.codes[0]}) was called")


@pytest.fixture
def process_exits(no_process_exit: _ExitGuard) -> list[int]:
    """For tests of the shutdown path: the exit codes the app tried to end
    the process with."""
    no_process_exit.expected = True
    return no_process_exit.codes
