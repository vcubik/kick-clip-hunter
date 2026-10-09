"""Shared helpers for displaying stored UTC timestamps in local time."""

from datetime import datetime


def to_local_datetime(iso_timestamp: str) -> datetime:
    """Timestamps are stored in UTC; they are shown in the system's local time zone."""
    return datetime.fromisoformat(iso_timestamp).astimezone()


def to_local(iso_timestamp: str) -> str:
    return to_local_datetime(iso_timestamp).strftime("%Y-%m-%d %H:%M:%S")
