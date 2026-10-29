import socket

import libtorrent as lt
import pytest
from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials

from config import Settings
from database.store import Store
from drive.auth import TokenVault
from scheduler.service import Service
from storage.workspace import Workspace
from torrent.engine import inspect
from torrent.inputs import PublicResolver, fetch_torrent


def test_encrypted_credentials_reconnection(tmp_path):
    key = Fernet.generate_key().decode()
    vault = TokenVault(tmp_path, key)
    first = Credentials(token='first', refresh_token='refresh', token_uri='https://oauth2.googleapis.com/token',
                        client_id='id', client_secret='secret')
    vault.save(first)
    assert b'first' not in vault.path.read_bytes()
    assert vault.load().token == 'first'
    second = Credentials(token='second', refresh_token='new-refresh',
                         token_uri='https://oauth2.googleapis.com/token', client_id='id', client_secret='secret')
    vault.save(second)
    assert TokenVault(tmp_path, key).load().token == 'second'
    with pytest.raises(InvalidToken):
        TokenVault(tmp_path, Fernet.generate_key().decode()).load()


@pytest.mark.parametrize('url', ['http://example.com/a.torrent', 'https://127.0.0.1/a',
                                 'https://user:pass@example.com/a', 'https://example.com:8443/a'])
async def test_unsafe_url_rejected_without_fetch(url):
    with pytest.raises(ValueError):
        await fetch_torrent(url)


async def test_dns_rebinding_is_blocked(monkeypatch):
    import asyncio
    async def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
    with pytest.raises(ValueError, match='non-public'):
        await PublicResolver().resolve('public-looking.example', 443)


def test_metadata_rejects_raw_traversal_before_libtorrent_sanitizes():
    data = lt.bencode({b'info': {b'name': b'root', b'piece length': 16384,
        b'pieces': b'x'*20, b'files': [{b'length': 1, b'path': [b'..', b'escape']}]}})
    with pytest.raises(ValueError):
        inspect(bytes(data))


def test_retained_payload_survives_restart_and_disk_guard_allows_upload(tmp_path):
    s = Settings(tmp_path)
    ws = Workspace(s)
    db = Store(tmp_path / 'state.db')
    job = 'c'*12
    db.add_job(job, 'test', b'x', [dict(idx=0, path='nested/file', size=3, offset=0, piece=16384)])
    db.execute("UPDATE jobs SET state='running'")
    db.update_file(job, 0, state='uploading', reservation=1000, selected=1,
                   drive_id='stable-id', upload_session='https://www.googleapis.com/session')
    slot = ws.slot(job, 0)
    slot.mkdir()
    (slot / 'payload').write_bytes(b'abc')
    db.db.close()
    reopened = Store(tmp_path / 'state.db')
    service = Service(reopened, ws, s, None, None)
    service.recover()
    f = reopened.one('SELECT * FROM files')
    assert f['state'] == 'queued' and f['reservation'] == 1000
    assert f['drive_id'] == 'stable-id' and f['upload_session']
    assert (slot / 'payload').read_bytes() == b'abc'
    service.disk_blocked = True
    assert service.interrupted(job)
    assert not service.interrupted(job, upload=True)


async def test_cancel_releases_space_after_workers_stop(tmp_path):
    s = Settings(tmp_path)
    ws = Workspace(s)
    db = Store(tmp_path / 'state.db')
    job = 'd'*12
    db.add_job(job, 'test', b'x', [dict(idx=0, path='file', size=3, offset=0, piece=16384)])
    db.update_file(job, 0, reservation=1000)
    slot = ws.slot(job, 0)
    slot.mkdir()
    (slot / 'payload').write_bytes(b'abc')
    service = Service(db, ws, s, None, None)
    await service.control(job, 'cancelled')
    assert not slot.exists()
    assert db.one('SELECT state,reservation FROM files') == {'state': 'cancelled', 'reservation': 0}


def test_two_processes_cannot_own_workspace(tmp_path):
    a, b = Workspace(Settings(tmp_path)), Workspace(Settings(tmp_path))
    a.acquire()
    try:
        with pytest.raises(RuntimeError, match='Another process'):
            b.acquire()
    finally:
        a.lock.close()
