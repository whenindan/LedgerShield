"""Thread-safe snooze log persistence backed by a JSON file."""

import datetime
import json
import logging
from pathlib import Path

from filelock import FileLock

from models.collections import SnoozeEntry

logger = logging.getLogger(__name__)

SNOOZE_FILE = Path("data_sandbox/snooze_log.json")
LOCK_FILE = Path("data_sandbox/snooze_log.json.lock")


def load_snooze_log() -> list[dict]:
    """Read the snooze log from disk under an exclusive file lock.

    Args:
        None

    Returns:
        A list of snooze entry dicts. Returns an empty list if the snooze
        file does not yet exist.

    Raises:
        json.JSONDecodeError: If the file exists but contains invalid JSON.
    """
    with FileLock(LOCK_FILE):
        if not SNOOZE_FILE.exists():
            return []
        with SNOOZE_FILE.open(encoding="utf-8") as fh:
            return json.load(fh)


def save_snooze_log(entries: list[dict]) -> None:
    """Write the snooze log to disk under an exclusive file lock.

    Args:
        entries: The full list of snooze entry dicts to persist.

    Raises:
        IOError: If the file cannot be written.
    """
    with FileLock(LOCK_FILE):
        with SNOOZE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)


def add_snooze_entry(entry: SnoozeEntry) -> None:
    """Append a new snooze entry to the persistent log.

    Loads the current log, appends the entry serialised as a dict, and
    saves the updated log back to disk atomically via the file lock.

    Args:
        entry: A validated ``SnoozeEntry`` instance to persist.

    Raises:
        IOError: If the file cannot be written.
    """
    entries = load_snooze_log()
    entries.append(entry.model_dump())
    save_snooze_log(entries)
    logger.info(
        "snooze_store: added snooze for '%s' until %s",
        entry.client_name,
        entry.snooze_until,
    )


def is_snoozed(client_name: str) -> bool:
    """Check whether a client is currently within an active snooze window.

    Finds the most recent snooze entry for ``client_name`` and returns
    ``True`` if its ``snooze_until`` date is strictly after today.

    Args:
        client_name: The client name to look up in the snooze log.

    Returns:
        ``True`` if the client has an active (non-expired) snooze entry,
        ``False`` if no entry exists or the most recent entry has expired.
    """
    entries = load_snooze_log()
    client_entries = [e for e in entries if e.get("client_name") == client_name]
    if not client_entries:
        return False

    most_recent = max(client_entries, key=lambda e: e["snooze_until"])
    snooze_until = datetime.date.fromisoformat(most_recent["snooze_until"])
    return snooze_until > datetime.date.today()
