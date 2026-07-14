import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    kick_client_id: str
    kick_client_secret: str


def load_settings() -> Settings:
    client_id = os.environ["KICK_CLIENT_ID"]
    client_secret = os.environ["KICK_CLIENT_SECRET"]
    return Settings(kick_client_id=client_id, kick_client_secret=client_secret)
