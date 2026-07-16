"""Interactive one-time login to capture a Kick browser session.

Uses `patchright` (not plain `playwright`) to drive the browser: kick.com's
Cloudflare protection blocked vanilla Playwright automation outright, even
with a real, everyday Chrome profile. patchright patches the specific CDP
leaks that kind of bot detection checks for. This is a deliberate choice to
evade kick.com's anti-automation measures - see kick_session.py's docstring.

Log in yourself in the window that opens (this script never sees your
password) - it polls the page and saves automatically once it detects you're
logged in, so there's nothing to switch back to a terminal for. Cookies/
storage are saved to data/kick_session_state.json for reuse by the
clip-creation flow.

Usage: python scripts/kick_login.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from patchright.sync_api import sync_playwright

from kick_clip_hunter.kick_session import BROWSER_PROFILE_DIR, SESSION_STATE_PATH

POLL_SECONDS = 3
TIMEOUT_SECONDS = 600

if __name__ == "__main__":
    SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,
        )
        page = context.new_page()
        page.goto("https://kick.com/")

        print("A browser window has opened. Log in to Kick there yourself.")
        print("Waiting for login to complete (checking automatically)...")

        deadline = time.monotonic() + TIMEOUT_SECONDS
        logged_in = False
        while time.monotonic() < deadline:
            try:
                logged_in = page.evaluate(
                    "!document.body.innerText.includes('Log In')"
                )
            except Exception:
                logged_in = False
            if logged_in:
                break
            time.sleep(POLL_SECONDS)

        if not logged_in:
            print(f"Timed out after {TIMEOUT_SECONDS}s without detecting a login.")
            context.close()
            sys.exit(1)

        context.storage_state(path=str(SESSION_STATE_PATH))
        context.close()

    print(f"Logged in - session saved to {SESSION_STATE_PATH}")
