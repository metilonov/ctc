import hashlib
import runpy

import libtorrent as lt
import pytest

from config import Settings
from torrent import engine as module


fixtures = runpy.run_path(str(__import__('pathlib').Path(__file__).with_name('test_integration.py')))


async def test_real_pause_resume_rechecks_partial_file(tmp_path, monkeypatch):
    source, data = fixtures['make_torrent'](tmp_path, (2*1024**2+123, 1024**2+321))
    seed, handle = fixtures['seeder'](source, data)
    _, files = module.inspect(data)
    f = files[0] | {'job': 'e'*12}
    settings = Settings(tmp_path / 'data', timeout=60)
    original = module.session_settings
    monkeypatch.setattr(module, 'session_settings', lambda: original() | {
        'download_rate_limit': 128*1024, 'local_download_rate_limit': 128*1024,
        'ignore_limits_on_local_network': False, 'enable_dht': False, 'enable_lsd': False})
    peers = [('127.0.0.1', seed.listen_port())]
    engine = module.Engine(settings, test_peers=peers)
    stopped = False

    def progress(n, rate):
        nonlocal stopped
        if n >= 256*1024:
            stopped = True
    slot = tmp_path / 'slot'
    try:
        with pytest.raises(InterruptedError):
            await engine.download(data, f, slot, progress, lambda: stopped)
        assert (slot / 'resume.bin').is_file()
        assert not engine.running
        monkeypatch.setattr(module, 'session_settings', original)
        # New engine + resume file simulates fresh process state; force_recheck
        # must validate existing partial payload before accepting completion.
        recovered = module.Engine(settings, test_peers=peers)
        result = await recovered.download(data, f, slot, lambda n, r: None, lambda: False)
        assert hashlib.sha256(result.read_bytes()).digest() == hashlib.sha256(
            (source / f['path']).read_bytes()).digest()
        assert not recovered.running
    finally:
        seed.remove_torrent(handle)


async def test_real_magnet_metadata_has_zero_payload(tmp_path, monkeypatch):
    source, data = fixtures['make_torrent'](tmp_path, (1024**2+123,))
    seed, handle = fixtures['seeder'](source, data)
    original = module.session_settings
    monkeypatch.setattr(module, 'session_settings', lambda: original() | {
        'enable_dht': False, 'enable_lsd': False})
    monkeypatch.setattr(module, 'protect_network', lambda s: None)  # loopback fixture only
    magnet = lt.make_magnet_uri(lt.torrent_info(data)) + f'&x.pe=127.0.0.1:{seed.listen_port()}'
    scratch = tmp_path / 'metadata'
    try:
        result = await module.metadata_from_magnet(magnet, scratch, timeout=30)
        _, files = module.inspect(result)
        assert files[0]['size'] == 1024**2+123
        assert not scratch.exists() or not list(scratch.rglob('*.bin'))
    finally:
        seed.remove_torrent(handle)
