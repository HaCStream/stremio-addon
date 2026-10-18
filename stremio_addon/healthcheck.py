"""Container health check using the same configuration source as the server."""
import sys
import time
import logging
from .logging_setup import LogFormatter
from urllib.error import URLError
from urllib.request import urlopen

from .core import Settings


def main():
    started = time.monotonic()
    try:
        port = Settings.env().port
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as response:
            if response.status != 200:
                raise RuntimeError("non-200 response")
    except (OSError, RuntimeError, URLError, ValueError) as exc:
        # Docker retains probe output; stdout also reaches the container log.
        message = 'Health probe failed: endpoint=/healthz elapsed_seconds=%.2f error=%s' % (
            time.monotonic() - started, type(exc).__name__)
        message = LogFormatter().format(logging.LogRecord(
            __name__, logging.WARNING, __file__, 0, message, (), None))
        print(message, file=sys.stderr)
        try:
            with open('/proc/1/fd/1', 'w') as output:
                output.write(message + '\n')
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
