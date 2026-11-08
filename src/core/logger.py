"""Console output for application events, without third-party payload dumps."""

import logging
import sys


class ConsoleHandler(logging.StreamHandler):
    def emit(self, record):
        self.stream = sys.stderr
        super().emit(record)


def setup_logging() -> None:
    logger = logging.getLogger("recruiter")
    if not logger.handlers:
        handler = ConsoleHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s | %(message)s", datefmt="%H:%M:%S"
            )
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
