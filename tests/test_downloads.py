import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon import errors

from stremio_addon.downloads import Downloads, disk
from stremio_addon.telegram import CHUNK
from stremio_addon.debug import create_debug_app
from stremio_addon.core import Settings
from stremio_addon.runtime import Runtime
from test_addon import FakeTelegram, row


class Gateway:
    def __init__(self, content):
        self.content = content
        self.client = self
        self.requests = []
        self.gates = {}
        self.started = asyncio.Queue()
        self.id = '99'
        self.failures = []
        self.closed = 0
        self.playback = asyncio.Event()
        self.playback.set()

    async def wait_background(self):
        await self.playback.wait()

    async def message(self, row):
        return SimpleNamespace(document=SimpleNamespace(id=self.id, size=len(self.content)))

    def iter_download(self, message, *, offset, **kwargs):
        gateway = self
        class Iterator:
            async def __anext__(self):
                gateway.requests.append(offset)
                gateway.started.put_nowait(offset)
                if offset in gateway.gates:
                    await gateway.gates[offset].wait()
                if gateway.failures:
                    raise gateway.failures.pop(0)
                return gateway.content[offset:offset + CHUNK]

            async def close(self):
                gateway.closed += 1
        return Iterator()


def manager(tmp_path, content):
    data = tmp_path / 'data'
    data.mkdir(exist_ok=True)
    tg = Gateway(content)
    return Downloads(SimpleNamespace(cfg=SimpleNamespace(data=data), tg=tg), tmp_path / 'media')


async def finished(m, identifier):
    await asyncio.wait_for(asyncio.shield(m.tasks[identifier]), 5)
    return m.jobs[identifier]


@pytest.mark.asyncio
async def test_pause_restart_resume_is_byte_exact(tmp_path):
    content = b'a' * CHUNK + b'b' * CHUNK + b'end'
    m = manager(tmp_path, content)
    await m.start()
    gate = asyncio.Event()
    m.runtime.tg.gates[CHUNK] = gate
    identifier = await m.create(row(size=len(content), filename='../סרט.mp4'))
    assert await m.runtime.tg.started.get() == 0
    assert await m.runtime.tg.started.get() == CHUNK
    await m.action(identifier, 'pause')
    job = m.jobs[identifier]
    final, part = m.paths(job)
    assert job['status'] == 'paused'
    assert part.read_bytes() == content[:CHUNK]
    assert not final.exists()
    await m.close()

    recovered = manager(tmp_path, content)
    await recovered.start()
    assert recovered.jobs[identifier]['status'] == 'paused'
    await recovered.action(identifier, 'resume')
    assert (await finished(recovered, identifier))['status'] == 'completed'
    assert recovered.runtime.tg.requests == [CHUNK, CHUNK * 2]
    assert final.read_bytes() == content
    assert not part.exists()
    assert final.parent == recovered.root
    await recovered.close()


@pytest.mark.asyncio
async def test_concurrency_has_no_streaming_slot_limit_and_duplicate_jobs_are_reused(tmp_path):
    m = manager(tmp_path, b'video')
    m.runtime.tg.gates[0] = asyncio.Event()
    identifiers = await asyncio.gather(*(m.create(row(id=f'tg:1:{i}', size=5)) for i in range(8)))
    for _ in range(8):
        assert await asyncio.wait_for(m.runtime.tg.started.get(), 3) == 0
    assert len(m.tasks) == 8
    assert await m.create(row(id='tg:1:0', size=5)) == identifiers[0]
    m.runtime.tg.gates[0].set()
    for identifier in identifiers:
        assert (await finished(m, identifier))['status'] == 'completed'
    assert len({m.jobs[i]['name'] for i in identifiers}) == 8
    await m.close()


@pytest.mark.asyncio
async def test_stop_deletes_partial_and_manifest_entry(tmp_path):
    m = manager(tmp_path, b'v' * (CHUNK + 3))
    m.runtime.tg.gates[CHUNK] = asyncio.Event()
    identifier = await m.create(row(size=CHUNK + 3))
    await m.runtime.tg.started.get()
    await m.runtime.tg.started.get()
    final, part = m.paths(m.jobs[identifier])
    assert part.exists()
    await m.action(identifier, 'delete')
    assert not part.exists() and not final.exists()
    assert m.listing() == []
    assert json.loads(m.manifest.read_text()) == []
    assert m.runtime.tg.closed == 2
    await m.close()


@pytest.mark.asyncio
async def test_playback_blocks_new_download_requests_and_stop_still_works(tmp_path):
    m = manager(tmp_path, b'video')
    m.runtime.tg.playback.clear()
    identifier = await m.create(row(size=5))
    await asyncio.sleep(0)
    assert not m.runtime.tg.requests
    await asyncio.wait_for(m.action(identifier, 'delete'), 2)
    assert not m.jobs
    await m.close()


@pytest.mark.asyncio
async def test_partial_chunk_is_repeated_and_changed_telegram_media_rejected(tmp_path):
    content = b'a' * CHUNK + b'hello'
    m = manager(tmp_path, content)
    m.runtime.tg.playback.clear()
    identifier = await m.create(row(size=len(content)))
    await m.action(identifier, 'pause')
    final, part = m.paths(m.jobs[identifier])
    part.write_bytes(content[:CHUNK + 2])
    m.runtime.tg.playback.set()
    m.runtime.tg.id = 'edited'
    await m.action(identifier, 'resume')
    assert (await finished(m, identifier))['status'] == 'failed'
    assert not m.runtime.tg.requests
    assert not final.exists()
    m.runtime.tg.id = '99'
    await m.action(identifier, 'resume')
    assert (await finished(m, identifier))['status'] == 'completed'
    assert m.runtime.tg.requests == [CHUNK]
    assert final.read_bytes() == content
    await m.close()


@pytest.mark.asyncio
async def test_flood_wait_and_expired_reference_retry_without_skipping_bytes(tmp_path):
    m = manager(tmp_path, b'video')
    m.runtime.tg.failures = [errors.FloodWaitError(None, capture=0), errors.FileReferenceExpiredError(None)]
    identifier = await m.create(row(size=5))
    assert (await finished(m, identifier))['status'] == 'completed'
    assert m.runtime.tg.requests == [0, 0, 0]
    assert m.paths(m.jobs[identifier])[0].read_bytes() == b'video'
    assert m.runtime.tg.closed == 3
    await m.close()


@pytest.mark.asyncio
async def test_no_space_and_symlink_fail_without_overwriting_other_files(tmp_path, monkeypatch):
    m = manager(tmp_path, b'video')
    monkeypatch.setattr('stremio_addon.downloads.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
    identifier = await m.create(row(size=5))
    assert (await finished(m, identifier))['error'] == 'OSError'
    final, part = m.paths(m.jobs[identifier])
    target = tmp_path / 'unrelated'
    target.write_bytes(b'keep')
    part.unlink()
    part.symlink_to(target)
    await m.action(identifier, 'resume')
    assert (await finished(m, identifier))['status'] == 'failed'
    assert target.read_bytes() == b'keep'
    await m.action(identifier, 'delete')
    assert target.read_bytes() == b'keep'
    await m.close()


@pytest.mark.asyncio
async def test_disk_cancellation_joins_in_flight_write():
    import threading
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    def write():
        started.set()
        release.wait(3)
        done.set()
    task = asyncio.create_task(disk(write))
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.is_set()


def test_download_api_auth_source_scope_and_batch_delete(tmp_path):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    app = create_debug_app(Runtime(cfg, FakeTelegram))
    with TestClient(app) as client:
        app.state.runtime.downloads.root = tmp_path / 'media'
        headers = {'X-Debug-Key': cfg.key}
        for method, url, body in [('GET', '/api/downloads', None),
                                  ('POST', '/api/downloads', {'source_id': 'x'}),
                                  ('POST', '/api/downloads/delete', {'ids': ['x']}),
                                  ('POST', '/api/downloads/x/pause', None)]:
            assert client.request(method, url, json=body).status_code == 401
        assert client.post('/api/downloads', json={'source_id': 'unknown'}, headers=headers).status_code == 404
        response = client.post('/api/downloads', json={'source_id': row()['id']}, headers=headers)
        assert response.status_code == 202
        identifier = response.json()['id']
        assert client.get('/api/downloads', headers=headers).json()['downloads'][0]['source_id'] == row()['id']
        assert client.post(f'/api/downloads/{identifier}/bogus', headers=headers).status_code == 400
        assert client.post('/api/downloads/x/pause', headers=headers).status_code == 404
        result = client.post('/api/downloads/delete', json={'ids': [identifier, 'missing']}, headers=headers)
        assert result.json() == {'downloads': []}
        app.state.runtime.tg.channels.clear()
        assert client.post('/api/downloads', json={'source_id': row()['id']}, headers=headers).status_code == 404
