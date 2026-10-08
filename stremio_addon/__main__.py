import asyncio
import logging
import signal
import sys
import uvicorn
from .core import Settings
from .app import create_app_with_runtime
from .debug import create_debug_app
from .runtime import Runtime


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
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [setattr(server, 'should_exit', True) for server in servers])
    await asyncio.gather(*(server.serve() for server in servers))

if __name__ == '__main__':
    settings = Settings.env()
    # Uvicorn's log_config=None leaves INFO messages without a stdout handler.
    # Supervisor captures stdout/stderr for the add-on Logs tab.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s: %(name)s: %(message)s',
                        datefmt='%Y-%m-%dT%H:%M:%S%z')
    # No request paths or Telegram payloads in application logs.
    logging.getLogger('telethon').setLevel(logging.CRITICAL)
    asyncio.run(serve(settings))
