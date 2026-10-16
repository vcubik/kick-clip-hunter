import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Where Kick sends the browser back to once the account the bot writes as
# has signed in (see kick_auth.py): this service's own page for it. The
# Kick app has to have exactly this as its redirect URL.
DEFAULT_KICK_REDIRECT_URI = "http://localhost:8000/bot/kick/signed_in"


@dataclass(frozen=True)
class Settings:
    kick_client_id: str
    kick_client_secret: str
    # A Twitch application's, for watching Twitch channels; without them no
    # Twitch channel can be (see `twitch`).
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    kick_redirect_uri: str = DEFAULT_KICK_REDIRECT_URI
    # A key of Euler Stream's, the service that signs the requests TikTok's
    # chat is read with (see tiktok_chat.py), for more of them a day than it
    # signs without one. The chat of a few channels needs none.
    euler_stream_api_key: str = ""
    # Where notifications go (see notify.py): a Telegram bot's token and the
    # chat it writes in, a Discord channel's webhook - either, both or
    # neither. And where the dashboard is reached from, for the links in
    # them: none without it.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    dashboard_url: str = ""

    @property
    def twitch(self) -> bool:
        """Whether the service is set up to watch Twitch channels."""
        return bool(self.twitch_client_id and self.twitch_client_secret)


def load_settings() -> Settings:
    client_id = os.environ["KICK_CLIENT_ID"]
    client_secret = os.environ["KICK_CLIENT_SECRET"]
    return Settings(
        kick_client_id=client_id,
        kick_client_secret=client_secret,
        twitch_client_id=os.environ.get("TWITCH_CLIENT_ID", "").strip(),
        twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET", "").strip(),
        kick_redirect_uri=os.environ.get("KICK_REDIRECT_URI", "").strip() or DEFAULT_KICK_REDIRECT_URI,
        euler_stream_api_key=os.environ.get("EULER_STREAM_API_KEY", "").strip(),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
        dashboard_url=os.environ.get("DASHBOARD_URL", "").strip(),
    )
