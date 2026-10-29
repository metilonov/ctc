import asyncio
import hashlib

import libtorrent as lt
import pytest

from config import Settings
from database.store import Store
from scheduler.service import Service
from storage.workspace import Workspace


def metadata(size=1000):
    piece = 1024**2
    return bytes(lt.bencode({b'info': {b'name': b'controlled.bin', b'length': size,
        b'piece length': piece, b'pieces': b'x'*20*((size+piece-1)//piece)}}))


def setup(tmp_path, **kwargs):
    settings = Settings(tmp_path, **kwargs)
    ws = Workspace(settings)
    db = Store(tmp_path / 'state.db')
    return settings, ws, db, Service(db, ws, settings, None, None)


def test_add_second_job_while_payload_larger_than_control_reserve(tmp_path):
    s, ws, db, service = setup(tmp_path, reserve=1024**2)
    slot = ws.slot('a'*12, 0)
    slot.mkdir()
    with (slot / 'data').open('wb') as f:
        f.truncate(2*1024**2)
    assert ws.used() > s.reserve
    assert service.add(metadata())
    assert service.add(metadata())
    assert db.one('SELECT COUNT(*) n FROM jobs')['n'] == 2


def test_large_single_file_visible_but_start_rejected(tmp_path):
    s, ws, db, service = setup(tmp_path)
    job = service.add(metadata(20*1024**3))
    db.execute('UPDATE files SET selected=1')
    with pytest.raises(ValueError, match='резервом'):
        service.start(job)
    assert db.one('SELECT state FROM jobs')['state'] == 'selecting'
    assert not list(ws.slots.iterdir())


def test_cleanup_retries_are_bounded_and_manual_resume_resets(tmp_path, monkeypatch):
    s, ws, db, service = setup(tmp_path, attempts=2)
    job = service.add(metadata())
    db.update_file(job, 0, state='confirmed', selected=1, reservation=1000)
    calls = 0
    def fail(*args):
        nonlocal calls
        calls += 1
        raise PermissionError('controlled cleanup failure')
    monkeypatch.setattr(ws, 'clean', fail)
    for _ in range(4):
        db.update_file(job, 0, retry_at=0)
        service.cleanup_confirmed(db.one('SELECT * FROM files'))
    assert calls == 2
    f = db.one('SELECT * FROM files')
    assert f['state'] == 'confirmed' and f['reservation'] == 1000
    service.start(job)
    assert db.one('SELECT attempts FROM files')['attempts'] == 0


async def test_full_disk_restart_still_uploads_verified_payload(tmp_path):
    s, ws, db, service = setup(tmp_path, min_free=1)
    job = service.add(metadata())
    slot = ws.slot(job, 0)
    path = slot / 'payload' / 'controlled.bin'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'x'*1000)
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    db.update_file(job, 0, selected=1, state='uploading', reservation=10000, md5=digest)
    db.execute("UPDATE jobs SET state='running'")
    ws.free = lambda: 0
    class NoDownload:
        def pause_all(self):
            pass
    class Upload:
        async def upload(self, f, payload, interrupted, progress):
            assert not interrupted()
            assert payload.read_bytes() == b'x'*1000
            return 'confirmed-id'
    service.engine, service.drive = NoDownload(), Upload()
    task = asyncio.create_task(service.run())
    try:
        async with asyncio.timeout(5):
            while db.one('SELECT state FROM jobs')['state'] != 'done':
                if task.done():
                    task.result()
                await asyncio.sleep(.05)
        assert not slot.exists()
    finally:
        service.stopping = True
        await task
