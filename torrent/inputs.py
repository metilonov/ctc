import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

MAX_METADATA = 8 * 1024**2


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        import asyncio
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family)
        if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
            raise ValueError('URL resolves to a non-public address')
        return [dict(hostname=host, host=r[4][0], port=port, family=r[0],
                     proto=r[2], flags=0) for r in records]

    async def close(self):
        pass


async def fetch_torrent(url):
    u = urlsplit(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443):
        raise ValueError('Only public HTTPS URLs on port 443 are accepted')
    try:
        if not ipaddress.ip_address(u.hostname).is_global:
            raise ValueError('Private address forbidden')
    except ValueError as e:
        if 'forbidden' in str(e):
            raise
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.get(url, allow_redirects=False) as r:
            if r.status != 200:
                raise ValueError('Torrent URL must return HTTP 200 without redirect')
            data = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_METADATA:
                    raise ValueError('Metadata exceeds 8 MiB')
            return bytes(data)
