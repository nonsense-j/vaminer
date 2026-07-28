"""Shared console and run-file logging for the miner."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOG_LEVEL = logging.INFO
LOG_FORMAT = "[%(levelname)s] %(filename)s:%(funcName)s -> %(message)s"
_RAW_RECORD_ATTRIBUTE = "_miner_raw"
_FILE_ONLY_RECORD_ATTRIBUTE = "_miner_file_only"


class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, _FILE_ONLY_RECORD_ATTRIBUTE, False)


class _RunFormatter(logging.Formatter):
    """Keep Rich panels intact while formatting ordinary miner records."""

    def __init__(self) -> None:
        super().__init__(LOG_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, _RAW_RECORD_ATTRIBUTE, False):
            return record.getMessage()
        return super().format(record)


logger = logging.getLogger("MINER")
logger.setLevel(LOG_LEVEL)
logger.propagate = False

if not any(getattr(handler, "_miner_console_handler", False) for handler in logger.handlers):
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler._miner_console_handler = True  # type: ignore[attr-defined]
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    console_handler.addFilter(_ConsoleFilter())
    logger.addHandler(console_handler)


def log_renderable(rendered: str) -> None:
    """Write an already-rendered Rich panel to the active run file only."""
    logger.info(
        rendered.rstrip("\n"),
        extra={
            _RAW_RECORD_ATTRIBUTE: True,
            _FILE_ONLY_RECORD_ATTRIBUTE: True,
        },
    )


@contextmanager
def run_log_file(
    log_root: Path,
    vas_id: str,
    *,
    timestamp: datetime | None = None,
) -> Iterator[Path]:
    """Attach one append-only log file for the duration of a VAS run."""
    run_dir = log_root / vas_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started_at = timestamp or datetime.now().astimezone()
    stem = f"miner-{started_at:%Y%m%d-%H%M%S}"
    log_path = run_dir / f"{stem}.log"
    sequence = 1
    while log_path.exists():
        log_path = run_dir / f"{stem}-{sequence}.log"
        sequence += 1
    log_path.touch(exist_ok=False)

    file_handler = logging.FileHandler(
        log_path,
        mode="a",
        encoding="utf-8",
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(_RunFormatter())
    logger.addHandler(file_handler)
    try:
        yield log_path
    finally:
        logger.removeHandler(file_handler)
        file_handler.close()
