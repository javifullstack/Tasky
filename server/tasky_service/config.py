from dataclasses import dataclass
from zoneinfo import ZoneInfo
import os


def _system_timezone_name():
    zoneinfo_root = os.path.realpath("/usr/share/zoneinfo")
    localtime = os.path.realpath("/etc/localtime")
    prefix = zoneinfo_root + os.sep
    if localtime.startswith(prefix):
        return localtime[len(prefix):]
    return os.getenv("TZ", "UTC").lstrip(":")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    device_token: str
    todoist_token: str
    trello_key: str
    trello_token: str
    timezone: ZoneInfo
    refresh_seconds: int

    @classmethod
    def from_env(cls):
        timezone_name = os.getenv("TASKY_TIMEZONE", _system_timezone_name())
        return cls(
            host=os.getenv("TASKY_HOST", "0.0.0.0"),
            port=int(os.getenv("TASKY_PORT", "8787")),
            device_token=os.getenv("TASKY_DEVICE_TOKEN", ""),
            todoist_token=os.getenv("TODOIST_API_TOKEN", ""),
            trello_key=os.getenv("TRELLO_API_KEY", ""),
            trello_token=os.getenv("TRELLO_API_TOKEN", ""),
            timezone=ZoneInfo(timezone_name),
            refresh_seconds=max(60, int(os.getenv("TASKY_REFRESH_SECONDS", "300"))),
        )