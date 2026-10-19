import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

        async def get_messages(self, entity, limit, offset_id=0, min_id=0, max_id=0, reverse=False):
            if not reverse:
                assert gateway.status['phase'] == 'checking'
                return [SimpleNamespace(id=self.target)]
            assert gateway.status['phase'] == 'indexing'
            # A newer arrival must wait for the next cycle.
            return [SimpleNamespace(id=i) for i in range(min_id + 1, 14)
                    if i < max_id][:limit]

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


class QueueClient:
    """Independent history model with sparse IDs (including non-video messages)."""
    def __init__(self, messages):
        self.messages = sorted(messages, reverse=True)

    async def get_messages(self, entity, limit, offset_id=0, min_id=0, max_id=0, reverse=False):
        found = [i for i in self.messages if (not offset_id or i < offset_id)
                 and i > min_id and (not max_id or i < max_id)]
        return [SimpleNamespace(id=i) for i in sorted(found, reverse=not reverse)[:limit]]


def enable_queue_counts(gateway, messages):
    gateway.client = QueueClient(messages)


@pytest.mark.asyncio
async def test_queue_estimates_include_id_gaps_and_reach_zero(gateway, tmp_path):
    # 205 messages spread across 20,500 IDs; most need not contain videos.
    messages = list(range(100, 20600, 100))
    enable_queue_counts(gateway, messages)
    observed = []
    gateway.index = lambda channel, message: observed.append(gateway.queue(channel))
    assert gateway.queue(1) is None
    await gateway.scan(1, 1)
    assert observed[0] == 20500
    assert gateway.queue(1) == 10599

    # A fresh process must derive remaining history from the saved checkpoint.
    gateway.store.db.close()
    gateway.store = Store(tmp_path / 'db')
    gateway.history_remaining.clear()
    gateway.new_remaining.clear()
    assert gateway.queue(1) is None
    await gateway.scan(1, 1)
    assert gateway.queue(1) == 599
    await gateway.scan(1, 1)
    assert gateway.queue(1) == 0
    assert gateway.store.checkpoint(1)['complete']

    # Catch-up uses ID distance; deleted messages are acceptable in the estimate.
    gateway.client.messages = [40000, 30000] + gateway.client.messages
    observed.clear()
    await gateway.scan(1, 1)
    assert observed == [19500, 19500]  # Progress advances after the whole page commits.
    assert gateway.queue(1) == 0


@pytest.mark.asyncio
async def test_queue_with_empty_history_and_failed_check(gateway):
    enable_queue_counts(gateway, [])
    await gateway.scan(1, 1)
    assert gateway.queue(1) == 0
    gateway.client.get_messages = AsyncMock(side_effect=ConnectionError)
    # With a nonempty checkpoint, the next check fails rather than claiming 0.
    gateway.store.save_checkpoint(dict(channel=1, newest=10, oldest=1, complete=1))
    with pytest.raises(ConnectionError):
        await gateway.scan_new_messages(1, 1)
    assert gateway.queue(1) is None

