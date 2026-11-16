from dataclasses import dataclass
from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admins: frozenset[int]
    timezone_name: str
    database: str
    max_participants: int
    lesson_minutes: int
    feedback_hours: int

    @property
    def tz(self):
        return ZoneInfo(self.timezone_name)


def load_config() -> Config:
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or token == 'PUT_YOUR_TOKEN_HERE':
        raise RuntimeError('Укажите BOT_TOKEN в .env')
    return Config(
        token=token,
        admins=frozenset(int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()),
        timezone_name=os.getenv('TIMEZONE', 'Europe/Moscow'),
        database=os.getenv('DATABASE_PATH', './data/queuebot.sqlite3'),
        max_participants=int(os.getenv('MAX_PARTICIPANTS', '35')),
        lesson_minutes=100,  # Fixed lesson length; old .env values are ignored.
        feedback_hours=0,  # Legacy compatibility only; no survey is run.
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def show_time(utc_str: str, cfg: Config) -> str:
    return datetime.fromisoformat(utc_str).astimezone(cfg.tz).strftime('%d.%m.%Y %H:%M')
