"""Storage uses naive UTC; public timestamps include an explicit UTC offset."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def storage_time(value: datetime) -> datetime:
    return as_utc(value).replace(tzinfo=None)


def iso_utc(value: datetime) -> str:
    return as_utc(value).isoformat().replace("+00:00", "Z")
