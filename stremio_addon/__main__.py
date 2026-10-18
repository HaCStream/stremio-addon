import asyncio
import signal
import logging
from contextlib import nullcontext
from .diagnostics import LoopMonitor
import uvicorn
from .core import Settings
from .app import create_app_with_runtime
from .debug import create_debug_app
from .runtime import Runtime
from .logging_setup import configure_logging


async def serve(settings):
    runtime = Runtime(settings)
    configs = [uvicorn.Config(create_app_with_runtime(runtime), host='0.0.0.0', port=settings.port,
                              workers=1, access_log=False, log_config=None)]
    if settings.debug_enabled:
        configs.append(uvicorn.Config(create_debug_app(runtime), host=settings.debug_host,
                                      port=settings.debug_port, workers=1, access_log=False,
                                      log_config=None))
    servers = [uvicorn.Server(config) for config in configs]
    for server in servers:
        server.install_signal_handlers = lambda: None
        server.capture_signals = nullcontext
    loop = asyncio.get_running_loop()
    def shutdown(sig):
        logging.getLogger(__name__).info('Shutdown requested: signal=%s', sig.name)
        for server in servers:
            server.should_exit = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown, sig)
    monitor = LoopMonitor(loop)
    monitor.start()
    logging.getLogger(__name__).info('Starting listeners: api=%s developer_ui=%s log_level=%s',
                                     settings.port, settings.debug_enabled, settings.debug_level)
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        monitor.close()

if __name__ == '__main__':
    settings = Settings.env()
    # Uvicorn's log_config=None leaves INFO messages without a stdout handler.
    # Supervisor captures stdout/stderr for the add-on Logs tab.
    configure_logging(settings.debug_level)
    asyncio.run(serve(settings))
