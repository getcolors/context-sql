#!/usr/bin/env python3
"""Manage a private, Unix-socket-only PostgreSQL instance for SQL context."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path.home() / '.local/share/context-sql'
DEFAULT_CONFIG = Path.home() / '.config/context-sql/connections.json'
PORT = 55440
MARKER = 'context-sql-local-v1'


def private_dir(path):
    if path.is_symlink():
        raise ValueError(f'Refusing symbolic link: {path}')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ValueError(f'Directory belongs to another user: {path}')
    path.chmod(0o700)


def private_write(path, content):
    if path.is_symlink():
        raise ValueError(f'Refusing symbolic link: {path}')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def atomic_script(text):
    # Remove only the outer wrapper; payload SQL strings can contain these words.
    lines = text.split('\n')
    first = next((i for i, line in enumerate(lines)
                  if line.strip() and not line.lstrip().startswith('--')), None)
    if first is not None and lines[first].strip().upper() == 'BEGIN;':
        last = max(i for i, line in enumerate(lines) if line.strip().upper() == 'COMMIT;')
        del lines[last]
        del lines[first]
    return '\n'.join(lines)


class LocalDB:
    def __init__(self, state, config):
        self.state = state.absolute()
        self.config = config.absolute()
        self.pg = self.state / 'pg'
        self.socket = self.state / 'socket'
        self.owner = self.state / 'owner.json'
        self.credentials = None

    def command(self, binary, *args, sql=None, database=None, check=True):
        executable = shutil.which(binary)
        if not executable:
            raise ValueError(f'Missing PostgreSQL tool: {binary}')
        env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
        if database:
            env.update(PGHOST=str(self.socket), PGPORT=str(PORT), PGDATABASE=database,
                       PGUSER='context_admin', PGPASSWORD=self.credentials['admin'],
                       PGCONNECT_TIMEOUT='5')
        result = subprocess.run([executable, *map(str, args)], input=sql, text=True,
                                capture_output=True, env=env)
        if check and result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or 'command failed'
            for secret in (self.credentials or {}).values():
                message = message.replace(secret, '[redacted]')
            raise ValueError(f'{binary}: {message}')
        return result

    def query(self, sql, database='context_sql'):
        return self.command('psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                            sql=sql, database=database).stdout.strip()

    def load(self):
        marker = self.state / 'managed.json'
        if marker.is_symlink() or not marker.is_file():
            raise ValueError('Not a managed context-sql directory; run init with an empty directory')
        if json.loads(marker.read_text()) != {'format': MARKER}:
            raise ValueError('Unknown managed-directory format')
        for path in (self.state, self.owner):
            if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise ValueError(f'Expected a private, owned path: {path}')
        self.credentials = json.loads(self.owner.read_text())

    def running(self):
        return self.command('pg_ctl', '-D', self.pg, 'status', check=False).returncode == 0

    def service_enabled(self):
        return (self.state / 'systemd-unit').exists()

    def wait_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready = self.command('pg_isready', '-h', self.socket, '-p', PORT,
                                 '-U', 'context_admin', '-d', 'context_sql', '-t', '1', check=False)
            if ready.returncode == 0:
                return
            time.sleep(0.1)
        raise ValueError('Managed PostgreSQL did not become ready within 10 seconds')

    def start(self):
        self.load()
        if self.service_enabled():
            self.command('systemctl', '--user', 'start', 'context-sql.service')
            self.wait_ready()
        elif not self.running():
            self.command('pg_ctl', '-D', self.pg, '-l', self.state / 'postgres.log', '-w', 'start')

    def stop(self):
        self.load()
        if self.service_enabled():
            self.command('systemctl', '--user', 'stop', 'context-sql.service')
        elif self.running():
            self.command('pg_ctl', '-D', self.pg, '-m', 'fast', '-w', 'stop')

    def init(self):
        if not (self.state / 'managed.json').exists():
            if self.state.exists() and any(self.state.iterdir()):
                raise ValueError('Initialization requires an empty state directory')
            private_dir(self.state)
            self.credentials = {key: secrets.token_urlsafe(36) for key in ('admin', 'reader', 'writer')}
            private_write(self.owner, json.dumps(self.credentials) + '\n')
            private_write(self.state / 'managed.json', json.dumps({'format': MARKER}) + '\n')
        self.load()
        private_dir(self.socket)
        # PostgreSQL Unix socket paths have a platform-dependent bound near 108 bytes.
        if len(os.fsencode(str(self.socket / f'.s.PGSQL.{PORT}'))) > 100:
            raise ValueError('State directory path is too long for a PostgreSQL Unix socket')
        if not (self.pg / 'PG_VERSION').exists():
            password_file = self.state / 'init-password'
            private_write(password_file, self.credentials['admin'] + '\n')
            try:
                self.command('initdb', '-D', self.pg, '-U', 'context_admin',
                             '--auth-local=scram-sha-256', '--auth-host=scram-sha-256',
                             '--pwfile', password_file, '--no-locale', '-E', 'UTF8')
            finally:
                password_file.unlink(missing_ok=True)
        settings = ("listen_addresses = ''\n" + f'port = {PORT}\n' +
                    f'unix_socket_directories = {literal(self.socket)}\n' +
                    "unix_socket_permissions = 0700\npassword_encryption = 'scram-sha-256'\n")
        private_write(self.pg / 'postgresql.auto.conf', settings)
        self.start()
        if not self.query("SELECT 1 FROM pg_database WHERE datname='context_sql'", 'postgres'):
            self.query('CREATE DATABASE context_sql', 'postgres')
        self.query('CREATE TABLE IF NOT EXISTS public.context_migration '
                   '(name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()); '
                   'REVOKE ALL ON public.context_migration FROM PUBLIC; '
                   'CREATE TABLE IF NOT EXISTS public.context_seed '
                   '(sha256 text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()); '
                   'REVOKE ALL ON public.context_seed FROM PUBLIC;')
        for path in sorted((ROOT / 'sql').glob('[0-9][0-9][0-9]_*.sql')):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            previous = self.query(f'SELECT sha256 FROM public.context_migration WHERE name={literal(path.name)}')
            if previous:
                if previous != digest:
                    raise ValueError(f'Applied migration changed: {path.name}')
                continue
            self.query('BEGIN;\n' + atomic_script(path.read_bytes().decode('utf-8')) +
                       f'\nINSERT INTO public.context_migration(name,sha256) VALUES ({literal(path.name)},{literal(digest)});\nCOMMIT;')
        for role, group, key in [('context_catalog', 'context_reader', 'reader'),
                                 ('context_memory', 'context_writer', 'writer')]:
            if not self.query(f'SELECT 1 FROM pg_roles WHERE rolname={literal(role)}'):
                self.query(f'CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {literal(self.credentials[key])}; GRANT {group} TO {role};')
        self.query('REVOKE ALL ON DATABASE context_sql FROM PUBLIC; '
                   'GRANT CONNECT ON DATABASE context_sql TO context_catalog, context_memory; '
                   'REVOKE CREATE ON SCHEMA public FROM PUBLIC;')
        if not self.query('SELECT 1 FROM public.context_seed LIMIT 1'):
            self.import_seed()
        private_dir(self.config.parent)
        connections = {}
        for key, role in [('reader', 'context_catalog'), ('writer', 'context_memory')]:
            connections[key] = dict(host=str(self.socket), port=PORT, dbname='context_sql',
                                    user=role, password=self.credentials[key])
        if self.config.exists() and json.loads(self.config.read_text()) != connections:
            raise ValueError('Connection file already belongs to another instance; use --config')
        private_write(self.config, json.dumps(connections, indent=2) + '\n')

    def import_seed(self):
        self.load()
        seed = ROOT / 'data/skills.sql'
        digest = hashlib.sha256(seed.read_bytes()).hexdigest()
        self.query('BEGIN;\n' + atomic_script(seed.read_bytes().decode('utf-8')) +
                   f'\nINSERT INTO public.context_seed(sha256) VALUES ({literal(digest)}) ON CONFLICT DO NOTHING;\nCOMMIT;')

    def backup(self, output):
        self.load()
        if output is None:
            directory = self.state / 'backups'
            private_dir(directory)
            output = directory / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.dump')
        output = output.absolute()
        # Never overwrite an existing backup or follow a symlink.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            self.command('pg_dump', '--format=custom', '--file', output, database='context_sql')
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return output

    def enable(self):
        self.load()
        unit = Path.home() / '.config/systemd/user/context-sql.service'
        unit.parent.mkdir(parents=True, exist_ok=True)
        def quoted(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        postgres = shutil.which('postgres')
        if not postgres:
            raise ValueError('Missing PostgreSQL tool: postgres')
        if any(c in str(self.pg) for c in '\n\r'):
            raise ValueError('Unsupported newline in state path')
        content = ('[Unit]\nDescription=Local SQL context database\n\n[Service]\nType=simple\n' +
                   f'ExecStart={quoted(Path(postgres).resolve())} -D {quoted(self.pg)}\n' +
                   'Restart=on-failure\nKillSignal=SIGINT\nTimeoutStopSec=120\nUMask=0077\n\n' +
                   '[Install]\nWantedBy=default.target\n')
        if unit.exists() and unit.read_text() != content:
            raise ValueError('A different context-sql user service already exists')
        private_write(unit, content)
        self.command('systemctl', '--user', 'daemon-reload')
        if not self.service_enabled() and self.running():
            self.stop()
        self.command('systemctl', '--user', 'enable', '--now', 'context-sql.service')
        private_write(self.state / 'systemd-unit', str(unit) + '\n')
        self.wait_ready()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'start', 'stop', 'status', 'backup', 'enable', 'import'])
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output', type=Path, help='New backup file path')
    args = parser.parse_args()
    db = LocalDB(args.state_dir, args.config)
    try:
        os.umask(0o077)
        # Lock outside the data directory so init can still reject nonempty directories.
        db.state.parent.mkdir(parents=True, exist_ok=True)
        lock_path = db.state.parent / ('.' + db.state.name + '.lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == 'status':
                db.load()
                print(json.dumps({'state_dir': str(db.state), 'running': db.running(),
                                  'systemd': db.service_enabled()}))
            elif args.command == 'backup':
                print(json.dumps({'backup': str(db.backup(args.output))}))
            else:
                getattr(db, 'import_seed' if args.command == 'import' else args.command)()
                print(json.dumps({'command': args.command, 'ok': True}))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f'local_db: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
