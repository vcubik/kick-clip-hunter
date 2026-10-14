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
    )
