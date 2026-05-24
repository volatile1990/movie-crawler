from __future__ import annotations

import logging
import signal

from catalog import run_once
from options import load_options
from runtime import configure_logging, request_stop, sleep_interruptibly, stop_requested


def main() -> int:
    configure_logging()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    options = load_options()
    if options.get("run_on_start", True):
        run_once(options)

    if options.get("run_once", False):
        return 0

    while not stop_requested():
        interval_seconds = int(options["scan_interval_minutes"]) * 60
        logging.info("Next scan in %s minutes.", options["scan_interval_minutes"])
        sleep_interruptibly(interval_seconds)
        if stop_requested():
            break
        options = load_options()
        run_once(options)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        logging.exception("Crawler failed: %s", exc)
        raise SystemExit(1) from exc
