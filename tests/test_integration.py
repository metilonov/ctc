"""Real libtorrent loopback traffic; deterministic private Drive stand-in."""
import asyncio
import hashlib
import os

import pytest

import libtorrent as lt

from config import Settings
from database.store import Store
from scheduler.service import Service
from storage.workspace import Workspace
from torrent.engine import Engine, inspect


def make_torrent(root, sizes=(12*1024**2+123, 12*1024**2+321, 12*1024**2+111)):
    source = root / 'seed'
    for i, size in enumerate(sizes):
        path = source / 'legal-test' / 'nested' / f'{i}.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as f:
            if size >= 1024**3:
                # Sparse deterministic data speeds fixture creation. Actual peer
                # transfer still sends every payload byte; receiver uses real disk.
                if os.name == 'nt':
                    import ctypes
                    import msvcrt
                    returned = ctypes.c_ulong()
                    ok = ctypes.windll.kernel32.DeviceIoControl(
                        ctypes.c_void_p(msvcrt.get_osfhandle(f.fileno())),
                        0x900C4, None, 0, None, 0, ctypes.byref(returned), None)
                    if not ok:
                        raise OSError('Could not create sparse fixture')
                    # CRT _chsize_s zero-fills even sparse files; use native EOF.
                    handle = ctypes.c_void_p(msvcrt.get_osfhandle(f.fileno()))
                    if not ctypes.windll.kernel32.SetFilePointerEx(handle, ctypes.c_longlong(size), None, 0):
                        raise OSError('Could not seek sparse fixture')
                    if not ctypes.windll.kernel32.SetEndOfFile(handle):
                        raise OSError('Could not extend sparse fixture')
                    f.seek(0)
                else:
                    f.truncate(size)
                f.write(bytes([65+i])*4096)
                f.seek(size-4096)
                f.write(bytes([90-i])*4096)
                continue
            block = bytes([65+i])*1024**2
            remaining = size
            while remaining:
                chunk = block[:min(remaining, len(block))]
                f.write(chunk)
                remaining -= len(chunk)
    fs = lt.file_storage()
    lt.add_files(fs, str(source / 'legal-test'))
    piece = 4*1024**2 if max(sizes) >= 1024**3 else 256*1024
    creator = lt.create_torrent(fs, piece, flags=lt.create_torrent.v1_only)
    lt.set_piece_hashes(creator, str(source))
    data = bytes(lt.bencode(creator.generate()))
    return source, data


def seeder(source, data):
    s = lt.session({'listen_interfaces': '127.0.0.1:0', 'enable_dht': False,
                    'enable_lsd': False, 'enable_upnp': False, 'enable_natpmp': False})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(data)
    params.save_path = str(source)
    params.flags |= lt.torrent_flags.seed_mode
    params.flags &= ~(lt.torrent_flags.paused | lt.torrent_flags.auto_managed)
    h = s.add_torrent(params)
    return s, h


class PrivateDrive:
    def __init__(self, db, fail=0):
        self.db = db
        self.fail = fail
        self.files = {}
        self.calls = 0

    async def upload(self, f, path, interrupted=lambda: False, progress=lambda n, rate: None):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError('controlled Drive outage')
        assert not interrupted()
        digest = sha256(path)
        self.files[(f['job'], f['idx'])] = (path.stat().st_size, digest)
        progress(path.stat().st_size, 1024**2)
        return f"private-{f['idx']}"


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(1024**2):
            h.update(chunk)
    return h.hexdigest()


BIG = os.getenv('RUN_LARGE_TESTS') == '1'
CASES = [((12*1024**2+123, 12*1024**2+321, 12*1024**2+111), 34*1024**2, 2*1024**2)]
for size, count in [(1, 12), (2, 6), (4, 3)]:
    CASES.append(pytest.param(tuple(size*1024**3+i+1 for i in range(count)),
                             10*1024**3, 256*1024**2,
                             marks=pytest.mark.skipif(not BIG, reason='Set RUN_LARGE_TESTS=1 for GiB traffic'),
                             id=f'{size}GiB-files-total12GiB'))


@pytest.mark.parametrize('sizes,limit,reserve', CASES)
async def test_real_transfer_beyond_workspace_with_boundaries(tmp_path, sizes, limit, reserve):
    source, data = make_torrent(tmp_path, sizes)
    seed, handle = seeder(source, data)
    # Each file needs ~29 MiB including reserve. Total payload is >36 MiB;
    # with 34 MiB workspace only one fits, forcing three cleanup cycles.
    s = Settings(tmp_path / 'data', limit=limit, reserve=reserve,
                 min_free=0, slots=2, timeout=600)
    ws = Workspace(s)
    db = Store(ws.root / 'state.db')
    drive = PrivateDrive(db)
    engine = Engine(s, test_peers=[('127.0.0.1', seed.listen_port())])
    service = Service(db, ws, s, engine, drive)
    job = service.add(data)
    db.execute('UPDATE files SET selected=1')
    service.start(job)
    worker = asyncio.create_task(service.run())
    peak = 0
    try:
        async with asyncio.timeout(3600 if BIG else 100):
            while db.one('SELECT state FROM jobs WHERE id=?', (job,))['state'] != 'done':
                peak = max(peak, ws.used())
                assert peak <= s.limit
                if worker.done():
                    worker.result()
                errors = db.all("SELECT * FROM files WHERE state='error'")
                assert not errors, errors
                await asyncio.sleep(.1)
        assert len(drive.files) == len(sizes)
        assert not list(ws.slots.iterdir())
        assert db.one('SELECT SUM(reservation) n FROM files')['n'] == 0
        _, fs = inspect(data)
        for f in fs:
            expected = sha256(source / f['path'])
            assert drive.files[(job, f['idx'])] == (f['size'], expected)
        print(f'peak workspace={peak} bytes; limit={s.limit}; payload={sum(f["size"] for f in fs)}')
    finally:
        service.stopping = True
        await worker
        seed.remove_torrent(handle)
        del handle
        del seed
        # Seeder is an isolated fixture directory, outside the limited worker volume.
        # Windows may briefly retain C++ file handles; leave fixture cleanup to pytest.


class FakeEngine:
    def __init__(self, path):
        self.path = path
    async def download(self, *args):
        return self.path
    def pause_all(self):
        pass


async def test_drive_error_restart_confirmation_cleanup(tmp_path):
    _, data = make_torrent(tmp_path, (1000,))
    s = Settings(tmp_path / 'data', attempts=2)
    ws = Workspace(s)
    db = Store(ws.root / 'state.db')
    drive = PrivateDrive(db, fail=2)
    service = Service(db, ws, s, None, drive)
    job = service.add(data)
    f = db.one('SELECT * FROM files')
    slot = ws.slot(job, f['idx'])
    slot.mkdir()
    path = slot / 'payload' / f['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'a'*1000)
    service.engine = FakeEngine(path)
    db.execute('UPDATE files SET selected=1,reservation=100000')
    service.start(job)
    await service.process(db.one('SELECT * FROM files'))
    assert path.exists()
    await service.process(db.one('SELECT * FROM files'))
    assert db.one('SELECT state FROM files')['state'] == 'error'
    assert path.exists() and db.one('SELECT reservation FROM files')['reservation'] > 0
    # Reconnect/manual retry resets finite attempts, retaining payload/reservation.
    service.start(job)
    assert db.one('SELECT attempts FROM files')['attempts'] == 0
    await service.process(db.one('SELECT * FROM files'))
    assert not slot.exists()
    assert db.one('SELECT state FROM files')['state'] == 'done'
    # Crash after remote confirmation but before local deletion.
    slot.mkdir()
    (slot / 'remaining').write_bytes(b'data')
    db.update_file(job, f['idx'], state='confirmed', reservation=100000)
    db.db.close()
    new_db = Store(ws.root / 'state.db')
    recovered = Service(new_db, ws, s, None, drive)
    recovered.recover()
    assert not slot.exists()
    assert new_db.one('SELECT state,reservation FROM files') == {'state': 'done', 'reservation': 0}
