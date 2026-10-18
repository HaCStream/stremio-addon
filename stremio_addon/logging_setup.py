"""Timestamped stdout logging shared by both HTTP listeners."""
import copy
import logging
import re
import sys


class LogFormatter(logging.Formatter):
    def __init__(self):
        super().__init__('%(asctime)s %(levelname)s: %(name)s: %(message)s',
                         datefmt='%Y-%m-%dT%H:%M:%S%z')

    def format(self, record):
        # Uvicorn sends lifecycle messages and errors to the same logger.
        # Give it a neutral display name without changing the shared record.
        if record.name == 'uvicorn.error':
            record = copy.copy(record)
            record.name = 'uvicorn.server'
        # HTTPX INFO logs and exception URLs can include TMDB's v3 query key.
        return re.sub(r'(?i)(api_key=)[^&\s\"\']+', r'\1[redacted]', super().format(record))


def configure_logging(level="INFO"):
    level = str(level).upper()
    if level not in ("NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("Invalid DEBUG_LEVEL")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogFormatter())
    logging.basicConfig(level=level, handlers=[handler])
    logging.getLogger().setLevel(level)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG if level in ("NOTSET", "DEBUG") else logging.WARNING)
    logging.getLogger('telethon').setLevel(logging.DEBUG if level in ('NOTSET', 'DEBUG') else logging.WARNING)
