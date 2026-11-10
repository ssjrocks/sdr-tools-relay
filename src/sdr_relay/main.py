"""Entry point: logging, problem reports, then the window (or --headless for a service)."""

import argparse
import logging
import logging.handlers
import sys
import traceback

from sdr_relay import APP_ID, APP_NAME, __version__, appreports
from sdr_relay.updater import state_dir


def _log_exception(exc_type, exc, tb):
    # Without a hook, PyQt6 aborts the process on an exception in a slot.
    traceback.print_exception(exc_type, exc, tb)
    logging.error("uncaught exception", exc_info=(exc_type, exc, tb))


def main():
    ap = argparse.ArgumentParser(prog="sdr-tools-relay")
    ap.add_argument("--minimized", action="store_true", help="start in the tray")
    ap.add_argument("--demo", metavar="FILE.cu8", help="play a recording instead of a dongle")
    ap.add_argument("--self-test", metavar="RESULTS.json", help="check this build works, then exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    if args.self_test:
        from sdr_relay.selftest import run as self_test
        sys.exit(self_test(args.self_test))
    log_file = state_dir() / "relay.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.handlers.RotatingFileHandler(log_file, maxBytes=512 * 1024,
                                                                       backupCount=1, encoding="utf-8")])
    logging.info("%s %s starting", APP_NAME, __version__)
    sys.excepthook = _log_exception
    appreports.init(APP_ID, APP_NAME, __version__, state_dir(), log_file=log_file)
    from sdr_relay.gui import run
    sys.exit(run(start_hidden=args.minimized, demo_file=args.demo))


if __name__ == "__main__":
    main()
