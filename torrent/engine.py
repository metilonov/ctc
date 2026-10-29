"""Isolated file handles avoid stale have-pieces after deleting previous files.

Each active file has its own session and directory, including its boundary
partfile. A session is closed before upload/cleanup. No boundary data is shared
with another file's storage. This trades extra boundary traffic for safety.
"""
import asyncio
import ipaddress
import os
import time

import libtorrent as lt

from storage.workspace import safe_path
from torrent.inputs import MAX_METADATA


class StorageSynchronizationError(RuntimeError):
    """Stop the process rather than reuse disk with unknown pending C++ I/O."""


def session_settings():
    return {'alert_mask': int(lt.alert.category_t.all_categories),
            'enable_upnp': False, 'enable_natpmp': False,
            'connections_limit': 40, 'active_downloads': 1,
            'listen_interfaces': '0.0.0.0:0', 'max_metadata_size': MAX_METADATA}


def protect_network(session):
    ipfilter = lt.ip_filter()
    # Trackers and peer destinations must not reach local services.
    networks = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
                '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/3',
                '::/128', '::1/128', '::ffff:0:0/96', 'fc00::/7', 'fe80::/10', 'ff00::/8']
    for text in networks:
        network = ipaddress.ip_network(text)
        ipfilter.add_rule(str(network.network_address), str(network.broadcast_address), 1)
    session.set_ip_filter(ipfilter)


def inspect(data):
    if len(data) > MAX_METADATA:
        raise ValueError('Metadata exceeds 8 MiB')
    raw = lt.bdecode(data)
    info = raw[b'info']
    safe_path(info[b'name'].decode('utf-8'))
    for entry in info.get(b'files', []):
        safe_path('/'.join(p.decode('utf-8') for p in entry[b'path']))
    def check_tree(tree, prefix=''):
        for name, child in tree.items():
            if name:
                path = prefix + name.decode('utf-8')
                safe_path(path)
                check_tree(child, path + '/')
    check_tree(info.get(b'file tree', {}))
    ti = lt.torrent_info(data)
    fs = ti.files()
    if fs.num_files() > 10000 or ti.piece_length() > 64 * 1024**2:
        raise ValueError('Metadata exceeds supported file/piece limits')
    files, seen = [], set()
    for i in range(fs.num_files()):
        flags = fs.file_flags(i)
        if flags & lt.file_storage.flag_symlink:
            raise ValueError('Symlinks forbidden')
        path = safe_path(fs.file_path(i).replace(os.sep, '/'))
        if path.casefold() in seen:
            raise ValueError('Duplicate/case-colliding paths')
        seen.add(path.casefold())
        if flags & lt.file_storage.flag_pad_file:
            continue
        files.append(dict(idx=i, path=path, size=fs.file_size(i),
                          offset=fs.file_offset(i), piece=ti.piece_length()))
    if not files:
        raise ValueError('Empty torrent')
    # Reject file/directory collisions before creating any path.
    paths = {f['path'].casefold() for f in files}
    for p in paths:
        parts = p.split('/')
        if any('/'.join(parts[:n]) in paths for n in range(1, len(parts))):
            raise ValueError('File/directory collision')
    return ti, files


async def metadata_from_magnet(magnet, scratch, timeout=120):
    if len(magnet) > 16384 or not magnet.startswith('magnet:?'):
        raise ValueError('Invalid magnet')
    params = lt.parse_magnet_uri(magnet)
    # URL seeds are disabled: only peer traffic may supply torrent data.
    params.url_seeds = []
    params.http_seeds = []
    params.save_path = str(scratch)
    params.flags |= lt.torrent_flags.upload_mode | lt.torrent_flags.default_dont_download
    params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
    s = lt.session(session_settings())
    protect_network(s)
    h = s.add_torrent(params)
    try:
        async with asyncio.timeout(timeout):
            while not h.status().has_metadata:
                s.pop_alerts()
                await asyncio.sleep(.25)
            data = bytes(lt.bencode(lt.create_torrent(h.torrent_file()).generate()))
            inspect(data)
            return data
    finally:
        s.remove_torrent(h)
        del h
        del s


class Engine:
    def __init__(self, settings, test_peers=()):
        self.settings = settings
        self.running = {}
        self.test_peers = test_peers

    def pause_all(self):
        for _, h in self.running.values():
            h.pause()

    async def download(self, metadata, file, slot, progress, interrupted):
        ti, _ = inspect(metadata)
        payload = slot / 'payload'
        payload.mkdir(parents=True, exist_ok=True)
        target = payload / file['path']
        params = lt.add_torrent_params()
        resume = slot / 'resume.bin'
        if resume.exists():
            try:
                params = lt.read_resume_data(resume.read_bytes())
            except Exception:
                params = lt.add_torrent_params()
        params.ti = ti
        params.save_path = str(payload)
        params.url_seeds = []
        params.http_seeds = []
        priorities = [0] * ti.num_files()
        priorities[file['idx']] = 4
        params.file_priorities = priorities
        params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused
                          | lt.torrent_flags.seed_mode)
        params.flags |= lt.torrent_flags.default_dont_download
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        settings = session_settings()
        if self.test_peers:
            settings.update(enable_dht=False, enable_lsd=False)
        s = lt.session(settings)
        if not self.test_peers:
            protect_network(s)
        h = s.add_torrent(params)
        key = (file['job'], file['idx'])
        self.running[key] = (s, h)
        # Never trust resume's have-pieces after an unclean restart.
        h.force_recheck()
        started = time.monotonic()
        last_save = started
        first = file['offset'] // file['piece']
        last = (file['offset'] + file['size'] - 1) // file['piece']
        checked = False
        paused_ack = False
        try:
            while True:
                if interrupted():
                    raise InterruptedError('Task paused/cancelled or disk guard active')
                for a in s.pop_alerts():
                    if isinstance(a, lt.torrent_checked_alert):
                        checked = True
                        # Explicit piece priorities, consistent with the selected file.
                        h.prioritize_pieces([4 if first <= p <= last and file['size'] else 0
                                             for p in range(ti.num_pieces())])
                    if isinstance(a, lt.torrent_paused_alert):
                        paused_ack = True
                    if isinstance(a, lt.save_resume_data_alert):
                        temp = slot / 'resume.tmp'
                        temp.write_bytes(bytes(lt.write_resume_data_buf(a.params)))
                        temp.replace(resume)
                    if isinstance(a, (lt.torrent_error_alert, lt.file_error_alert)):
                        raise RuntimeError('libtorrent storage/network error')
                st = h.status(lt.status_flags_t.query_pieces)
                for peer in self.test_peers:
                    h.connect_peer(peer)
                progress(min(file['size'], h.file_progress()[file['idx']]), st.download_payload_rate)
                if interrupted():
                    raise InterruptedError('Task paused/cancelled or disk guard active')
                checking = st.state in (lt.torrent_status.checking_files,
                                        lt.torrent_status.checking_resume_data)
                if checked and not checking and (file['size'] == 0 or (
                    len(st.pieces) > last and all(st.pieces[p] for p in range(first, last + 1))
                )):
                    # have-piece bits mean torrent hash verification passed, including boundaries.
                    h.pause()
                    async with asyncio.timeout(30):
                        while True:
                            alerts = s.pop_alerts()
                            if any(isinstance(a, lt.torrent_paused_alert) for a in alerts):
                                paused_ack = True
                                break
                            await asyncio.sleep(.05)
                    break
                now = time.monotonic()
                if now - started > self.settings.timeout:
                    raise TimeoutError('Torrent download timed out')
                if now - last_save > 30:
                    h.save_resume_data()
                    last_save = now
                await asyncio.sleep(.5)
        finally:
            h.pause()
            # torrent_removed_alert alone does not guarantee disk jobs completed.
            # torrent_paused_alert explicitly guarantees files are closed.
            try:
                if not paused_ack:
                    async with asyncio.timeout(30):
                        while True:
                            if any(isinstance(a, lt.torrent_paused_alert) for a in s.pop_alerts()):
                                break
                            await asyncio.sleep(.05)
                h.save_resume_data()
                async with asyncio.timeout(30):
                    saved = False
                    while not saved:
                        for alert in s.pop_alerts():
                            if isinstance(alert, lt.save_resume_data_alert):
                                temp = slot / 'resume.tmp'
                                temp.write_bytes(bytes(lt.write_resume_data_buf(alert.params)))
                                temp.replace(resume)
                                saved = True
                            if isinstance(alert, lt.save_resume_data_failed_alert):
                                saved = True  # resume is only a hint; startup always rechecks
                        if not saved:
                            await asyncio.sleep(.05)
                s.remove_torrent(h)
                async with asyncio.timeout(30):
                    while True:
                        if any(isinstance(a, lt.torrent_removed_alert) for a in s.pop_alerts()):
                            break
                        await asyncio.sleep(.05)
            except TimeoutError:
                raise StorageSynchronizationError('Unable to synchronize libtorrent shutdown') from None
            self.running.pop(key, None)
            del h
            del s
        if file['size'] == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
        if target.is_symlink() or not target.is_file() or target.stat().st_size != file['size']:
            raise RuntimeError('Verified file size mismatch')
        return target
