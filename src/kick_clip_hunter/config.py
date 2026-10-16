import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    kick_client_id: str
    kick_client_secret: str
    # A Twitch application's, for watching Twitch channels; without them no
    # Twitch channel can be (see `twitch`).
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    # A key of Euler Stream's, the service that signs the requests TikTok's
    # chat is read with (see tiktok_chat.py), for more of them a day than it
    # signs without one. The chat of a few channels needs none.
    euler_stream_api_key: str = ""

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
        euler_stream_api_key=os.environ.get("EULER_STREAM_API_KEY", "").strip(),
    )
