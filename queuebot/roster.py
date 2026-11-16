"""Optional group-specific friendly names, case-insensitive by Telegram username.

When a Telegram user has no username, or the username is not in roster.csv,
we display their own Telegram full_name as before.
"""
import csv
from functools import lru_cache
from pathlib import Path

ROSTER_PATH = Path(__file__).with_name('roster.csv')


def normalize_username(username: str | None) -> str:
    return (username or '').strip().lstrip('@').casefold()


@lru_cache(maxsize=1)
def preferred_names() -> dict[str, str]:
    try:
        stream = ROSTER_PATH.open('r', encoding='utf-8-sig', newline='')
    except FileNotFoundError:
        return {}
    with stream:
        entries = csv.DictReader(stream)
        return {
            normalize_username(entry['username']): f"{entry['first_name'].strip()} {entry['last_name'].strip()}"
            for entry in entries if normalize_username(entry.get('username'))
        }


def display_name(username: str | None, full_name: str) -> str:
    return preferred_names().get(normalize_username(username), full_name)
