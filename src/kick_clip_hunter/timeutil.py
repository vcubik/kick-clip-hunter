"""Shared helper for displaying stored UTC timestamps in local time."""

from datetime import datetime


def to_local(iso_timestamp: str) -> str:
    """Timestamps are stored in UTC; display them in the system's local time zone."""
    return datetime.fromisoformat(iso_timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
