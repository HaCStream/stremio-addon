import asyncio
import time
import logging

import pytest

from stremio_addon.diagnostics import LoopMonitor


@pytest.mark.asyncio
async def test_stall_detected_while_loop_is_blocked_and_monitor_stops(caplog):
    with caplog.at_level(logging.WARNING):
        monitor = LoopMonitor(asyncio.get_running_loop(), threshold=.03, interval=.005)
        monitor.start()
        try:
            time.sleep(.12)
            assert 'Event loop stalled' in caplog.text
            await asyncio.sleep(.03)
            assert 'Event loop recovered' in caplog.text
        finally:
            monitor.close()
        assert not monitor.thread.is_alive()
