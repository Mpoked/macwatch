"""Logování na stderr. Pod systemd s prioritami pro journalctl."""

from __future__ import annotations

import logging
import os
import sys

# Úrovně syslogu, podle prefixu <N> je journald přiřadí jako prioritu.
_SYSLOG_LEVELS = {
    logging.DEBUG: 7,
    logging.INFO: 6,
    logging.WARNING: 4,
    logging.ERROR: 3,
    logging.CRITICAL: 2,
}


class JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        priority = _SYSLOG_LEVELS.get(record.levelno, 6)
        message = super().format(record)
        # víceřádkové zprávy (traceback) dostanou prefix na každém řádku
        return "\n".join(f"<{priority}>{line}" for line in message.splitlines())


def running_under_journal(stream=None) -> bool:
    """Podle systemd.exec(5): JOURNAL_STREAM musí sedět na zařízení a inode stderr.

    Samotná existence proměnné nestačí, dědí ji i terminál spuštěný ze služby.
    """
    value = os.environ.get("JOURNAL_STREAM", "")
    if ":" not in value:
        return False
    try:
        st = os.fstat((stream or sys.stderr).fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return value == f"{st.st_dev}:{st.st_ino}"


def configure(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if running_under_journal():
        handler.setFormatter(JournalFormatter("%(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
