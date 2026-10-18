"""Independent event-loop watchdog; never reads frame locals or request data."""
import logging
import sys
import threading
import time

log = logging.getLogger(__name__)


class LoopMonitor:
    def __init__(self, loop, threshold=2, interval=1):
        self.loop = loop
        self.threshold = threshold
        self.interval = interval
        self.last_tick = time.monotonic()
        self.thread_id = threading.get_ident()
        self.stopped = threading.Event()
        self.reported = False
        self.handle = None
        self.thread = threading.Thread(target=self.watch, name='loop-monitor', daemon=True)

    def tick(self):
        delay = time.monotonic() - self.last_tick
        if self.reported:
            log.warning('Event loop recovered: heartbeat_gap_seconds=%.2f', delay)
            self.reported = False
        self.last_tick = time.monotonic()
        if not self.stopped.is_set():
            self.handle = self.loop.call_later(self.interval, self.tick)

    def start(self):
        self.tick()
        self.thread.start()

    def watch(self):
        while not self.stopped.wait(self.interval):
            delay = time.monotonic() - self.last_tick
            if delay > self.threshold and not self.reported:
                self.reported = True
                # File/function/line only: source lines and locals may contain secrets.
                frame = sys._current_frames().get(self.thread_id)
                stack = []
                while frame is not None:
                    stack.append('%s:%s:%s' % (frame.f_code.co_filename,
                                               frame.f_lineno, frame.f_code.co_name))
                    frame = frame.f_back
                log.warning('Event loop stalled: heartbeat_gap_seconds=%.2f stack=%s',
                            delay, ' <- '.join(stack))

    def close(self):
        self.stopped.set()
        if self.handle:
            self.handle.cancel()
        self.thread.join(timeout=1)
