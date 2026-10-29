from concurrent.futures import ThreadPoolExecutor

import pytest

from config.settings import GIB, Settings
from database.store import Store
from scheduler.planner import SmartStorageScheduler, batches, reservation
from storage.workspace import Workspace, safe_path


def file(size, idx=0):
    return dict(idx=idx, path=f'root/sub/{idx}.bin', size=size, offset=idx*size, piece=1024**2)


@pytest.mark.parametrize('size,count', [(GIB, 9), (2*GIB, 4), (4*GIB, 2)])
def test_realistic_capacity(tmp_path, size, count):
    s = Settings(tmp_path, slots=4)
    budget = s.limit - s.reserve
    assert budget // reservation(file(size)) == count


def test_149_gib_is_not_rejected():
    fs = [file(4*GIB, i) for i in range(37)] + [file(GIB, 37)]
    assert sum(f['size'] for f in fs) == 149*GIB
    assert batches(fs, 10*GIB - 256*1024**2) == 19


def test_atomic_reservation_and_disk_exhaustion(tmp_path):
    s = Settings(tmp_path, limit=100*1024**2, reserve=10*1024**2, slots=4, min_free=0)
    ws = Workspace(s)
    db = Store(tmp_path / 'test.db')
    db.add_job('a'*12, 'test', b'x', [file(30*1024**2, i) for i in range(5)])
    db.execute("UPDATE files SET selected=1")
    db.execute("UPDATE jobs SET state='running'")
    planner = SmartStorageScheduler(db, ws, s)
    assert planner.claim()
    assert planner.claim() is None  # two full reservations do not fit
    ws.free = lambda: 0
    assert planner.claim() is None


def test_sqlite_claims_across_connections(tmp_path):
    s = Settings(tmp_path, limit=100*1024**2, reserve=10*1024**2, slots=4, min_free=0)
    ws = Workspace(s)
    db = Store(tmp_path / 'test.db')
    db.add_job('b'*12, 'test', b'x', [file(30*1024**2, i) for i in range(3)])
    db.execute('UPDATE files SET selected=1')
    db.execute("UPDATE jobs SET state='running'")

    def claim(_):
        other = Store(tmp_path / 'test.db')
        try:
            return SmartStorageScheduler(other, ws, s).claim()
        finally:
            other.db.close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(claim, range(3)))
    assert sum(r is not None for r in results) == 1


def test_written_payload_does_not_double_charge_its_reservation(tmp_path):
    s = Settings(tmp_path, limit=100*1024**2, reserve=10*1024**2, slots=2, min_free=0)
    ws = Workspace(s)
    db = Store(tmp_path / 'state.db')
    db.add_job('f'*12, 'test', b'x', [file(20*1024**2, i) for i in range(2)])
    db.execute('UPDATE files SET selected=1')
    db.execute("UPDATE jobs SET state='running'")
    planner = SmartStorageScheduler(db, ws, s)
    first = planner.claim()
    slot = ws.slot(first['job'], first['idx'])
    slot.mkdir()
    with (slot / 'payload').open('wb') as payload:
        payload.truncate(first['size'])
    assert planner.claim() is not None


@pytest.mark.parametrize('path', ['../x', '/x', 'a/../../x', 'C:/x', 'a\\x', 'CON.txt', 'a/./b', 'a\x00b'])
def test_paths_rejected(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_nested_path_and_cleanup(tmp_path):
    assert safe_path('torrent/inner/file.bin') == 'torrent/inner/file.bin'
    ws = Workspace(Settings(tmp_path))
    slot = ws.slot('a'*12, 0)
    slot.mkdir()
    (slot / 'file').write_bytes(b'123')
    with pytest.raises(ValueError):
        ws.clean('../outside', 0)
    ws.clean('a'*12, 0)
    assert not slot.exists()
