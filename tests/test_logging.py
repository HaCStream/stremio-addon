import logging
import re
import subprocess
import sys

import pytest

from stremio_addon.logging_setup import LogFormatter


@pytest.mark.parametrize('level', [logging.INFO, logging.WARNING, logging.ERROR])
def test_server_display_name_preserves_severity_and_record(level):
    record = logging.LogRecord('uvicorn.error', level, __file__, 1,
                               'Server message', (), None)
    output = LogFormatter().format(record)
    assert re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ', output)
    assert f'{logging.getLevelName(level)}: uvicorn.server: Server message' in output
    assert record.name == 'uvicorn.error'


def test_application_and_server_logs_reach_stdout_once():
    result = subprocess.run([sys.executable, '-c', '''
import logging
from stremio_addon.logging_setup import configure_logging
configure_logging()
configure_logging()
logging.getLogger('stremio_addon.telegram').info('Sync started')
logging.getLogger('stremio_addon.interactions').info('Search completed')
logging.getLogger('uvicorn.error').info('Application startup complete')
logging.getLogger('telethon').debug('Hidden transport details')
'''], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert 'INFO: stremio_addon.telegram: Sync started' in lines[0]
    assert 'INFO: stremio_addon.interactions: Search completed' in lines[1]
    assert 'INFO: uvicorn.server: Application startup complete' in lines[2]
    assert result.stderr == ''


@pytest.mark.parametrize('level', ['INFO', 'DEBUG', 'WARNING'])
def test_configured_levels(level):
    result = subprocess.run([sys.executable, '-c', f"""
import logging
from stremio_addon.logging_setup import configure_logging
configure_logging('{level}')
log = logging.getLogger('addon.test')
log.debug('details')
log.info('routine')
log.warning('warning')
log.error('error')
logging.getLogger('telethon').warning('transport warning')
logging.getLogger('httpx').info('request detail')
"""], capture_output=True, text=True, check=True)
    assert ('details' in result.stdout) == (level == 'DEBUG')
    assert ('routine' in result.stdout) == (level != 'WARNING')
    assert 'WARNING: addon.test: warning' in result.stdout
    assert 'ERROR: addon.test: error' in result.stdout
    assert 'transport warning' in result.stdout
    assert ('request detail' in result.stdout) == (level == 'DEBUG')
