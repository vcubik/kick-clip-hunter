"""Persisted browser login session for the unofficial kick.com clip endpoint.

Kick's public API (api.kick.com/public/v1) has no clip-creation or playback
support at all. The only way to trigger Kick's own clip creation is the
undocumented, browser-session-authenticated endpoint the website itself uses
(`POST kick.com/api/internal/v1/livestreams/{id}/clips`, then a `.../finalize`
call - see scripts/kick_login.py's docstring for the full contract). That
requires being logged in as a real Kick account, so we persist a browser
storage state (cookies + local storage) captured via a one-time interactive
login rather than ever handling the account's credentials ourselves.

The session lives in a real, visible browser window driven through
`patchright` (a Playwright-compatible library): kick.com's pages only load
reliably in a headed browser, not for a plain HTTP client or a headless one.
"""

from pathlib import Path

SESSION_STATE_PATH = Path("data/kick_session_state.json")

# A persistent profile dir so cookies/session survive across runs - avoids
# needing to log in again every time the saved session expires.
BROWSER_PROFILE_DIR = Path("data/kick_browser_profile")


def has_saved_session() -> bool:
    return SESSION_STATE_PATH.exists()
