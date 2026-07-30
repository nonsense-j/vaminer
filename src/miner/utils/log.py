"""Shared console and run-file logging for the Miner."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOG_LEVEL = logging.INFO
LOG_FORMAT = "[%(levelname)s] %(filename)s:%(funcName)s -> %(message)s"
_RAW_RECORD_ATTRIBUTE = "_miner_raw"
_FILE_ONLY_RECORD_ATTRIBUTE = "_miner_file_only"


class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, _FILE_ONLY_RECORD_ATTRIBUTE, False)


class _ConsoleHandler(logging.StreamHandler):
    """Marker type preventing duplicate console handlers."""


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

if not any(isinstance(handler, _ConsoleHandler) for handler in logger.handlers):
    console_handler = _ConsoleHandler(sys.stdout)
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
    input_id: str,
    trace_id: str,
    runtime: str,
) -> Iterator[Path]:
    """Attach one append-only log file for the duration of a VAS run."""
    run_dir = log_root / vas_id / input_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{trace_id}__{runtime}"
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
