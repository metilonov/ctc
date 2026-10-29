"""Read-only local diagnostic. Does not claim P2P/quotas were verified."""
import json
import platform
import shutil

import libtorrent as lt

from config import Settings


def main():
    s = Settings.load()
    existing = s.root if s.root.exists() else s.root.parent
    free = shutil.disk_usage(existing).free
    print(json.dumps({
        'python': platform.python_version(), 'libtorrent': lt.version,
        'platform': platform.system(), 'disk_free_bytes': free,
        'workspace_limit_bytes': s.limit, 'technical_reserve_bytes': s.reserve,
        'owner_configured': s.owner > 0, 'bot_token_configured': bool(s.token),
        'encryption_key_configured': bool(s.key),
        'encrypted_oauth_file_exists': (s.root / 'google-token.enc').is_file(),
        'p2p_permission_asserted_by_operator': s.p2p_allowed,
        'hard_quota_asserted_by_operator': s.quota_confirmed,
        'hosting_network_and_persistence': 'NOT VERIFIED by this diagnostic',
    }, indent=2))


if __name__ == '__main__':
    main()
