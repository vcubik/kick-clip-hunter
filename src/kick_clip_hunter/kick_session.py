"""Persisted browser login session for the unofficial kick.com clip endpoint.

Kick's public API (api.kick.com/public/v1) has no clip-creation or playback
support at all. The only way to trigger Kick's own clip creation is the
undocumented, browser-session-authenticated endpoint the website itself uses
(`POST kick.com/api/internal/v1/livestreams/{id}/clips`, then a `.../finalize`
call - see scripts/kick_login.py's docstring for the full contract). That
requires being logged in as a real Kick account, so we persist a browser
storage state (cookies + local storage) captured via a one-time interactive
login rather than ever handling the account's credentials ourselves.

Plain Playwright/Selenium automation gets blocked outright by kick.com's
Cloudflare protection regardless of the browser profile used - testing
showed even a real, everyday Chrome profile got blocked once driven by
vanilla Playwright, while the same site worked fine through non-CDP-based
automation. scripts/kick_login.py therefore uses `patchright` (a Playwright
fork that patches the specific CDP leaks bot-detection checks for) instead of
`playwright` - this is a deliberate choice to evade kick.com's anti-automation
measures, done with the user's explicit knowledge that it's a ToS gray area
that may stop working at any time.
"""

from pathlib import Path

SESSION_STATE_PATH = Path("data/kick_session_state.json")

# A persistent profile dir so cookies/session survive across runs - avoids
# needing to log in again every time the saved session expires.
BROWSER_PROFILE_DIR = Path("data/kick_browser_profile")


def has_saved_session() -> bool:
    return SESSION_STATE_PATH.exists()
