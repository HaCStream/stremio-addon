import asyncio
from types import SimpleNamespace

import pytest

from addon.start import configure
from stremio_addon.core import Settings, Store
from stremio_addon.telegram import Telegram


@pytest.fixture
def gateway(tmp_path):
    store = Store(tmp_path / 'db')
    tg = Telegram(Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path), store)
    tg.index = lambda channel, message: None
    yield tg
    store.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [0, 100, 101])
async def test_history_status_requires_confirmed_messages(gateway, count):
    states = []

    class Client:
        async def get_messages(self, entity, limit, offset_id):
            states.append(gateway.status['phase'])
            return [SimpleNamespace(id=i) for i in range(count, 0, -1)][:limit]

    gateway.client = Client()
    gateway.index = lambda channel, message: states.append(gateway.status['phase'])
    await gateway.scan_history(1, 1)
    assert states[0] == 'checking'  # Unknown checkpoint alone is not backlog evidence.
    assert all(state == 'indexing' for state in states[1:])
    assert gateway.store.checkpoint(1)['complete'] == int(count <= 100)
    assert gateway.pending_channels == ({1} if count == 101 else set())
    assert gateway.status['phase'] == ('indexing' if count == 101 else 'checking')


@pytest.mark.asyncio
async def test_catch_up_has_fixed_target_and_resumes_after_restart(gateway, tmp_path):
    gateway.store.save_checkpoint(dict(channel=1, newest=10, oldest=1, complete=1))
    indexed = []

    class Client:
        target = 12

        async def get_messages(self, entity, limit, offset_id):
            assert gateway.status['phase'] == 'checking'
            return [SimpleNamespace(id=self.target)]

        async def iter_messages(self, entity, min_id, max_id, reverse):
            assert gateway.status['phase'] == 'indexing'
            # A newer arrival must wait for the next cycle.
            for i in range(min_id + 1, 14):
                if i < max_id:
                    yield SimpleNamespace(id=i)

    gateway.client = Client()
    gateway.index = lambda channel, message: indexed.append(message.id)
    await gateway.scan_new_messages(1, 1)
    assert indexed == [11, 12]
    assert gateway.status['phase'] == 'checking'
    gateway.store.db.close()
    gateway.store = Store(tmp_path / 'db')
    gateway.client.target = 13
    await gateway.scan_new_messages(1, 1)
    assert indexed == [11, 12, 13]
    assert gateway.store.checkpoint(1)['newest'] == 13


@pytest.mark.asyncio
async def test_idle_cycle_uses_interval_and_reconciliation_is_not_indexing(gateway, monkeypatch):
    gateway.scan_interval_seconds = 7200
    gateway.store.save_checkpoint(dict(channel=1, newest=10, oldest=1, complete=1))

    async def discover():
        gateway.channels = {1: 1}

    class Client:
        async def get_messages(self, entity, limit, offset_id):
            assert gateway.status['phase'] == 'checking'
            return [SimpleNamespace(id=10)]

    async def reconcile():
        assert gateway.status['phase'] != 'indexing'

    async def wait_for(awaitable, timeout):
        awaitable.close()
        assert timeout == 7200
        assert gateway.status['phase'] == 'ready'
        raise asyncio.CancelledError()

    gateway.client = Client()
    gateway.discover, gateway.reconcile = discover, reconcile
    monkeypatch.setattr(asyncio, 'wait_for', wait_for)
    with pytest.raises(asyncio.CancelledError):
        await gateway.run()


@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'invalid'])
def test_scan_interval_validation(monkeypatch, value):
    for name, v in dict(ADDON_URL='http://localhost', ADDON_ACCESS_KEY='a'*32,
                        TELEGRAM_APP_API_ID='1', TELEGRAM_APP_API_HASH='hash',
                        USER_SESSION_STRING='session', AI_SEARCH_ENABLED='false',
                        SCAN_INTERVAL_SECONDS=value).items():
        monkeypatch.setenv(name, v)
    with pytest.raises(ValueError, match='SCAN_INTERVAL_SECONDS must be a positive integer'):
        Settings.env()


def test_scan_interval_ha_translation_and_env_precedence(monkeypatch):
    env = {}
    configure({'SCAN_INTERVAL_SECONDS': 600}, env)
    assert env['SCAN_INTERVAL_SECONDS'] == '600'
    configure({'SCAN_INTERVAL_SECONDS': 900}, env)
    assert env['SCAN_INTERVAL_SECONDS'] == '600'
    for name, value in dict(ADDON_URL='http://localhost', ADDON_ACCESS_KEY='a'*32,
                            TELEGRAM_APP_API_ID='1', TELEGRAM_APP_API_HASH='hash',
                            USER_SESSION_STRING='session', AI_SEARCH_ENABLED='false').items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('SCAN_INTERVAL_SECONDS', raising=False)
    assert Settings.env().scan_interval_seconds == 300
    monkeypatch.setenv('SCAN_INTERVAL_SECONDS', '600')
    assert Settings.env().scan_interval_seconds == 600


@pytest.mark.asyncio
async def test_confirmed_backlog_continues_without_waiting_for_scan_interval(gateway, monkeypatch):
    gateway.scan_interval_seconds = 7200

    async def discover():
        gateway.channels = {1: 1}

    class Client:
        async def get_messages(self, entity, limit, offset_id):
            return [SimpleNamespace(id=i) for i in range(201, 0, -1)][:limit]

    async def reconcile():
        pass

    async def wait_for(awaitable, timeout):
        awaitable.close()
        assert timeout == 2
        assert gateway.status['phase'] == 'indexing'
        assert gateway.status['pending_channels'] == 1
        raise asyncio.CancelledError()

    gateway.client = Client()
    gateway.discover, gateway.reconcile = discover, reconcile
    monkeypatch.setattr(asyncio, 'wait_for', wait_for)
    with pytest.raises(asyncio.CancelledError):
        await gateway.run()


@pytest.mark.asyncio
async def test_failed_check_does_not_report_up_to_date(gateway, monkeypatch):
    gateway.store.save_checkpoint(dict(channel=1, newest=10, oldest=1, complete=1))

    async def discover():
        gateway.channels = {1: 1}

    class Client:
        async def get_messages(self, entity, limit, offset_id):
            raise ConnectionError('private payload')

    async def sleep(seconds):
        assert gateway.status['phase'] == 'scan_incomplete'
        assert gateway.status['last_error'] == 'ConnectionError'
        raise asyncio.CancelledError()

    gateway.client = Client()
    gateway.discover = discover
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await gateway.run()
