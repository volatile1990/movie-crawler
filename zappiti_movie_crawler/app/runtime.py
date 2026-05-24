from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any


STOP_REQUESTED = False


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("smbclient").setLevel(logging.WARNING)
    logging.getLogger("smbprotocol").setLevel(logging.WARNING)


def request_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logging.info("Received signal %s, stopping after current step.", signum)


def stop_requested() -> bool:
    return STOP_REQUESTED


def parse_retry_after(value: str | None, default_seconds: float = 2.0) -> float:
    if not value:
        return default_seconds
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        return default_seconds


def sleep_interruptibly(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not STOP_REQUESTED and time.monotonic() < deadline:
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def sleep_before_retry(attempt: int, retry_after: str | None = None) -> None:
    if STOP_REQUESTED:
        return
    delay = parse_retry_after(retry_after, default_seconds=min(10.0, 2.0 * (attempt + 1)))
    sleep_interruptibly(int(delay) if delay.is_integer() else int(delay) + 1)
