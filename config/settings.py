import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

GIB = 1024**3
MAX_WORKSPACE_BYTES = 10 * GIB


@dataclass(frozen=True)
class Settings:
    root: Path
    token: str = ''
    owner: int = 0
    key: str = ''
    limit: int = MAX_WORKSPACE_BYTES
    reserve: int = 256 * 1024**2
    min_free: int = 512 * 1024**2
    slots: int = 2
    attempts: int = 5
    timeout: int = 21600
    p2p_allowed: bool = False
    quota_confirmed: bool = False
    folder_name: str = 'Cloud Torrent Bot'

    @classmethod
    def load(cls):
        load_dotenv()
        s = cls(
            root=Path(os.getenv('DATA_DIR', './data')).resolve(),
            token=os.getenv('BOT_TOKEN', ''), owner=int(os.getenv('OWNER_TELEGRAM_ID', '0')),
            key=os.getenv('TOKEN_ENCRYPTION_KEY', ''),
            limit=int(os.getenv('MAX_WORKSPACE_BYTES', str(MAX_WORKSPACE_BYTES))),
            reserve=int(os.getenv('TECHNICAL_RESERVE_BYTES', str(256 * 1024**2))),
            min_free=int(os.getenv('MIN_FREE_DISK_BYTES', str(512 * 1024**2))),
            slots=int(os.getenv('MAX_ACTIVE_FILES', '2')),
            attempts=int(os.getenv('MAX_ATTEMPTS', '5')),
            timeout=int(os.getenv('DOWNLOAD_TIMEOUT_SECONDS', '21600')),
            p2p_allowed=os.getenv('P2P_ALLOWED', '').lower() == 'true',
            quota_confirmed=os.getenv('WORKSPACE_QUOTA_CONFIRMED', '').lower() == 'true',
            folder_name=os.getenv('DRIVE_FOLDER_NAME', 'Cloud Torrent Bot'),
        )
        if not 0 < s.limit <= MAX_WORKSPACE_BYTES or not 0 < s.reserve < s.limit:
            raise ValueError('Invalid workspace limits')
        if not 1 <= s.slots <= 4 or s.attempts < 1 or s.min_free < 0 or s.timeout < 1:
            raise ValueError('Invalid worker configuration')
        return s
