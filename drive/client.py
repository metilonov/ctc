import asyncio
import hashlib
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
FIELDS = 'id,size,md5Checksum,trashed'
CHUNK = 8 * 1024**2


def md5_file(path):
    h = hashlib.md5(usedforsecurity=False)
    with path.open('rb') as f:
        while data := f.read(1024**2):
            h.update(data)
    return h.hexdigest()


def session_url(url):
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname != 'www.googleapis.com' or u.port not in (None, 443):
        raise RuntimeError('Unexpected Google upload session host')
    return url


class Drive:
    def __init__(self, db, vault, folder_name='Cloud Torrent Bot', client=None):
        self.db, self.vault, self.folder_name = db, vault, folder_name
        self.http = client or httpx.AsyncClient(timeout=90, follow_redirects=False, trust_env=False)
        self.lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()

    async def request(self, method, url, **kwargs):
        # Avoid two concurrent refreshes overwriting rotating credentials.
        async with self.auth_lock:
            token = await asyncio.to_thread(self.vault.access_token)
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = 'Bearer ' + token
        r = await self.http.request(method, url, headers=headers, **kwargs)
        if r.status_code not in (200, 201, 308, 404):
            # Do not stringify response/request: they can contain credentials/session URLs.
            raise RuntimeError(f'Google Drive HTTP {r.status_code}; retry or reconnect OAuth')
        return r

    async def folder(self, name, parent, key):
        async with self.lock:
            known = self.db.get('folder:' + key)
            if known:
                return known
            escaped = name.replace('\\', '\\\\').replace("'", "\\'")
            q = f"trashed=false and mimeType='application/vnd.google-apps.folder' and name='{escaped}'"
            q += f" and '{parent}' in parents"
            r = await self.request('GET', API, params={'q': q, 'fields': 'files(id)', 'pageSize': 100})
            found = r.json().get('files', [])
            if found:
                fid = found[0]['id']
            else:
                r = await self.request('POST', API, json={'name': name,
                                       'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent]})
                fid = r.json()['id']
            self.db.put('folder:' + key, fid)
            return fid

    async def parent(self, job, path):
        root = await self.folder(self.folder_name, 'root', 'root')
        parent = await self.folder(job, root, job)
        prefix = job
        for part in PurePosixPath(path).parts[:-1]:
            prefix += '/' + part
            parent = await self.folder(part, parent, prefix)
        return parent

    async def verify(self, fid, size, digest):
        r = await self.request('GET', f'{API}/{fid}', params={'fields': FIELDS})
        if r.status_code == 404:
            raise RuntimeError('Drive file missing; local copy retained')
        data = r.json()
        if data.get('trashed') or int(data.get('size', -1)) != size or data.get('md5Checksum') != digest:
            raise RuntimeError('Drive size/checksum mismatch; local copy retained')

    async def upload(self, f, path, interrupted=lambda: False, progress=lambda n, rate: None):
        job, idx, size = f['job'], f['idx'], f['size']
        digest = await asyncio.to_thread(md5_file, path)
        self.db.update_file(job, idx, md5=digest)
        # Persist a preallocated ID before any remote creation. Retrying a completed
        # but unacknowledged upload updates the SAME file, never creates a duplicate.
        fid = f.get('drive_id')
        if not fid:
            r = await self.request('GET', API + '/generateIds', params={'count': 1, 'space': 'drive'})
            fid = r.json()['ids'][0]
            self.db.update_file(job, idx, drive_id=fid)
        r = await self.request('GET', f'{API}/{fid}', params={'fields': FIELDS})
        exists = r.status_code == 200
        if exists and not r.json().get('trashed') and int(r.json().get('size', -1)) == size and r.json().get('md5Checksum') == digest:
            return fid
        parent = await self.parent(job, f['path'])
        uri = f.get('upload_session')
        for restart in range(2):
            if not uri:
                body = {'name': PurePosixPath(f['path']).name}
                if exists:
                    method, url = 'PATCH', f'{UPLOAD}/{fid}'
                else:
                    method, url = 'POST', UPLOAD
                    body.update(id=fid, parents=[parent])
                r = await self.request(method, url,
                    params={'uploadType': 'resumable', 'fields': FIELDS}, json=body,
                    headers={'X-Upload-Content-Length': str(size),
                             'X-Upload-Content-Type': 'application/octet-stream'})
                uri = session_url(r.headers['Location'])
                self.db.update_file(job, idx, upload_session=uri)
            r = await self.request('PUT', session_url(uri), content=b'',
                                   headers={'Content-Range': f'bytes */{size}'})
            if r.status_code in (200, 201):
                await self.verify(fid, size, digest)
                return fid
            if r.status_code == 404:
                uri = None
                self.db.update_file(job, idx, upload_session=None)
                # Reconcile possible completion before creating another session.
                check = await self.request('GET', f'{API}/{fid}', params={'fields': FIELDS})
                exists = check.status_code == 200
                if exists and int(check.json().get('size', -1)) == size and check.json().get('md5Checksum') == digest:
                    await self.verify(fid, size, digest)
                    return fid
                continue
            offset = self.offset(r, size)
            with path.open('rb') as source:
                while offset < size or size == 0:
                    if interrupted():
                        raise InterruptedError('Upload paused')
                    source.seek(offset)
                    data = source.read(min(CHUNK, size - offset))
                    started = time.monotonic()
                    crange = f'bytes {offset}-{offset + len(data) - 1}/{size}' if size else 'bytes */0'
                    r = await self.request('PUT', uri, content=data,
                                          headers={'Content-Range': crange,
                                                   'Content-Type': 'application/octet-stream'})
                    if r.status_code in (200, 201):
                        await self.verify(fid, size, digest)
                        progress(size, len(data) / max(.001, time.monotonic() - started))
                        return fid
                    if r.status_code == 404:
                        self.db.update_file(job, idx, upload_session=None)
                        raise RuntimeError('Upload session expired; retry scheduled')
                    next_offset = self.offset(r, size)
                    if next_offset <= offset:
                        raise RuntimeError('Upload made no progress')
                    offset = next_offset
                    self.db.update_file(job, idx, uploaded=offset)
                    progress(offset, len(data) / max(.001, time.monotonic() - started))
        raise RuntimeError('Unable to resume upload')

    @staticmethod
    def offset(r, size):
        value = r.headers.get('Range')
        offset = int(value.rsplit('-', 1)[1]) + 1 if value else 0
        if not 0 <= offset <= size:
            raise RuntimeError('Invalid Drive upload range')
        return offset
