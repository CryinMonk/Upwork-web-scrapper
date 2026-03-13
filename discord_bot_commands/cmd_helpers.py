"""
cmd_helpers.py

Shared utilities imported by all cmd_*.py modules.
"""

import logging
from database.database import log as db_log

logger = logging.getLogger("commands")


def log(level: str, message: str) -> None:
    """Write a log entry to the DB sink under the 'commands' logger."""
    db_log(level, "commands", message)