import hashlib

import httpx
import pytest

from database.store import Store
from drive.client import Drive


class Vault:
    def access_token(self):
        return 'test-token'


def setup(tmp_path):
    db = Store(tmp_path / 'state.db')
    f = dict(idx=0, path='root/nested/file.bin', size=1024*1024, offset=0, piece=16384)
    db.add_job('a'*12, 'test', b'x', [f])
    f = db.one('SELECT * FROM files')
    path = tmp_path / 'file.bin'
    path.write_bytes(b'z' * f['size'])
    return db, f, path


async def test_resumes_after_network_loss_and_no_duplicate(tmp_path):
    db, f, path = setup(tmp_path)
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    received, complete, drop = 0, False, True
    sessions = 0

    def handler(r):
        nonlocal received, complete, drop, sessions
        if r.url.path.endswith('/generateIds'):
            return httpx.Response(200, json={'ids': ['stable-id']})
        if r.url.path.endswith('/stable-id') and r.method == 'GET':
            return httpx.Response(200, json={'id': 'stable-id', 'size': str(f['size']),
                                  'md5Checksum': digest}) if complete else httpx.Response(404)
        if r.url.path == '/drive/v3/files' and r.method == 'GET':
            return httpx.Response(200, json={'files': [{'id': 'folder'}]})
        if r.url.path == '/upload/drive/v3/files' and r.method == 'POST':
            sessions += 1
            return httpx.Response(200, headers={'Location': 'https://www.googleapis.com/session'})
        if r.url.path == '/session':
            if r.headers['Content-Range'].startswith('bytes */'):
                return httpx.Response(308, headers={'Range': f'bytes=0-{received-1}'} if received else {})
            if drop:
                received = len(r.content)//2
                drop = False
                raise httpx.ReadError('controlled interruption')
            start = int(r.headers['Content-Range'].split()[1].split('-')[0])
            assert start == received
            received += len(r.content)
            complete = received == f['size']
            return httpx.Response(200, json={'id': 'stable-id'})
        raise AssertionError((r.method, r.url))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    drive = Drive(db, Vault(), client=client)
    with pytest.raises(httpx.ReadError):
        await drive.upload(f, path)
    f = db.one('SELECT * FROM files')
    assert f['drive_id'] == 'stable-id' and f['upload_session']
    assert await drive.upload(f, path) == 'stable-id'
    assert sessions == 1
    assert await drive.upload(db.one('SELECT * FROM files'), path) == 'stable-id'
    assert sessions == 1
    await client.aclose()


async def test_checksum_failure_keeps_file(tmp_path):
    db, f, path = setup(tmp_path)
    def handler(r):
        if r.url.path.endswith('/generateIds'):
            return httpx.Response(200, json={'ids': ['id']})
        return httpx.Response(200, json={'size': str(f['size']), 'md5Checksum': 'bad'})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    drive = Drive(db, Vault(), client=client)
    with pytest.raises(RuntimeError, match='checksum'):
        await drive.verify('id', f['size'], 'expected')
    assert path.exists()
    await client.aclose()
