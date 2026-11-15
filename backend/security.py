import hashlib
import hmac
import secrets
import sqlite3
import time
from collections import OrderedDict

from starlette.exceptions import HTTPException

COOKIE = 'tc_session'


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return f'scrypt${salt}${key.hex()}'


def password_matches(password, stored):
    try:
        algorithm, salt, _ = stored.split('$')
        return algorithm == 'scrypt' and hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, config):
        self.config = config
        self.db = sqlite3.connect(config.state / 'control.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        (config.state / 'control.sqlite3').chmod(0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS scripts (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, args TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (at REAL NOT NULL, message TEXT NOT NULL);
        ''')
        self.attempts = OrderedDict()

    def event(self, message):
        with self.db:
            self.db.execute('INSERT INTO events VALUES (?, ?)', (time.time(), message))
            self.db.execute('DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY rowid DESC LIMIT 100)')

    def login(self, username, password, ip):
        now = time.time()
        # Bound memory even when unauthenticated peers send many source addresses.
        times = [t for t in self.attempts.pop(ip, []) if t > now - 900]
        self.attempts[ip] = times
        while len(self.attempts) > 2048:
            self.attempts.popitem(last=False)
        if len(times) >= 5:
            raise HTTPException(429, 'Muitas tentativas. Aguarde 15 minutos.')
        times.append(now)
        valid_password = password_matches(password, self.config.password_hash)
        if not hmac.compare_digest(username.encode(), self.config.username.encode()) or not valid_password:
            raise HTTPException(401, 'Usuário ou senha incorretos.')
        self.attempts.pop(ip, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE expires <= ?', (now,))
            self.db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', (digest(token), csrf, now, now + self.config.session_hours * 3600))
            self.db.execute('DELETE FROM sessions WHERE id NOT IN (SELECT id FROM sessions ORDER BY created DESC LIMIT 5)')
        self.event('Nova conexão autenticada')
        return token, csrf

    def session(self, token):
        if not token or len(token) > 128:
            return None
        row = self.db.execute('SELECT * FROM sessions WHERE id = ? AND expires > ?', (digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def revoke(self, session_id):
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
