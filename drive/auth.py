import argparse
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ['https://www.googleapis.com/auth/drive.file']


class TokenVault:
    def __init__(self, root, key):
        self.path = Path(root) / 'google-token.enc'
        self.cipher = Fernet(key.encode())

    def save(self, credentials):
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(self.cipher.encrypt(credentials.to_json().encode()))
            f.flush()
            os.fsync(f.fileno())
        temp.replace(self.path)

    def load(self):
        info = json.loads(self.cipher.decrypt(self.path.read_bytes()))
        return Credentials.from_authorized_user_info(info, SCOPES)

    def access_token(self):
        creds = self.load()
        if not creds.valid:
            creds.refresh(Request())
            self.save(creds)
        return creds.token


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow
    from config import Settings
    parser = argparse.ArgumentParser(description='Run locally on your trusted computer')
    parser.add_argument('--client-secret', required=True)
    parser.add_argument('--output', default=None)
    args = parser.parse_args()
    s = Settings.load()
    root = Path(args.output).resolve() if args.output else s.root
    root.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    creds = flow.run_local_server(host='127.0.0.1', port=0, access_type='offline', prompt='consent')
    TokenVault(root, s.key).save(creds)
    print('Encrypted OAuth credentials saved. Transfer via a private channel, never Telegram.')


if __name__ == '__main__':
    main()
