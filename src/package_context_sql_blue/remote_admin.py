"""Provision account-specific database logins and private HTTPS service credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import uuid

from psycopg import sql

from .ingest_db import connection
from .local_db import DEFAULT_CONFIG, DEFAULT_STATE, PORT


def read_private(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a private regular configuration file')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Configuration must be owned by this user and mode 0600')
    return json.loads(path.read_text())


def publish(path, document):
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid():
        raise ValueError("Configuration directory must be owned and not a symlink")
    if path.is_symlink():
        raise ValueError('Refusing symbolic link destination')
    if path.exists():
        read_private(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(document, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def login(cursor, name, password, group):
    cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name), sql.Literal(password)))
    cursor.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(group), sql.Identifier(name)))
    cursor.execute(sql.SQL("ALTER ROLE {} SET search_path TO pg_catalog").format(sql.Identifier(name)))
    cursor.execute(sql.SQL("ALTER ROLE {} SET statement_timeout TO '15s'").format(sql.Identifier(name)))
    cursor.execute(sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout TO '15s'").format(sql.Identifier(name)))


def credentials(state, name, password):
    return dict(host=str(Path(state).expanduser().absolute() / 'socket'), port=PORT,
                dbname='context_sql', user=name, password=password, connect_timeout=5)


def initialize(state, local_config, server_config):
    if Path(server_config).exists() or Path(server_config).is_symlink():
        raise ValueError('Server configuration already exists')
    name = 'ctx_trace_' + uuid.uuid4().hex[:16]
    password = secrets.token_urlsafe(36)
    with connection(Path(state), Path(local_config)) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('trace.prompt')")
        if cur.fetchone()[0] is None:
            raise ValueError('Initialize the database with migration 007 first')
        login(cur, name, password, 'context_trace_service')
    # Publish only after database commit; a publication failure leaves an unused login.
    publish(server_config, dict(format=1, trace=credentials(state, name, password), accounts=[]))


def add_account(state, local_config, server_config, label, token_file, tenant=None):
    server_config = Path(server_config).expanduser().absolute()
    token_file = Path(token_file).expanduser().absolute()
    if token_file == server_config or token_file.exists() or token_file.is_symlink():
        raise ValueError('Token output must be a new file, separate from server configuration')
    # Serialize read/modify/publication for all administrator processes.
    import fcntl
    fd = os.open(str(server_config) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        config = read_private(server_config)
        if config.get('format') != 1 or not isinstance(config.get('accounts'), list):
            raise ValueError('Unsupported service configuration')
        account_id = uuid.uuid4()
        token = secrets.token_urlsafe(48)
        account = dict(account_id=str(account_id), token_sha256=hashlib.sha256(token.encode()).hexdigest())
        with connection(Path(state), Path(local_config)) as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO remote.account(account_id,label) VALUES(%s,%s)', (account_id, label))
            if tenant:
                tenant_id = uuid.UUID(tenant)
                cur.execute('INSERT INTO remote.membership(account_id,tenant_id) VALUES(%s,%s)', (account_id, tenant_id))
            for mode in ('reader', 'writer'):
                name = 'ctx_' + account_id.hex + '_' + mode[0]
                password = secrets.token_urlsafe(36)
                login(cur, name, password, 'context_remote_' + mode)
                cur.execute('INSERT INTO remote.login_identity(role_name,account_id) VALUES(%s,%s)', (name, account_id))
                account[mode] = credentials(state, name, password)
        # Failure after commit can leave an unused account; never claim cross-resource atomicity.
        publish(token_file, dict(account_id=str(account_id), token=token))
        config['accounts'].append(account)
        publish(server_config, config)
    return str(account_id)


def revoke_account(state, local_config, server_config, account_id):
    import fcntl
    server_config = Path(server_config).expanduser().absolute()
    fd = os.open(str(server_config) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        config = read_private(server_config)
        matches = [a for a in config['accounts'] if a['account_id'] == str(account_id)]
        if not matches:
            raise ValueError('Account not found')
        with connection(Path(state), Path(local_config)) as conn, conn.cursor() as cur:
            cur.execute('SELECT role_name FROM remote.login_identity WHERE account_id=%s', (account_id,))
            for (name,) in cur.fetchall():
                cur.execute(sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(name)))
                cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s AND pid<>pg_backend_pid()', (name,))
        config['accounts'] = [a for a in config['accounts'] if a['account_id'] != str(account_id)]
        publish(server_config, config)


def add_tenant(state, local_config, label):
    with connection(Path(state), Path(local_config)) as conn, conn.cursor() as cur:
        cur.execute('INSERT INTO remote.tenant(label) VALUES(%s) RETURNING tenant_id', (label,))
        return str(cur.fetchone()[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='Managed local database connection file')
    parser.add_argument('--server-config', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='Create private service configuration and trace login')
    account = sub.add_parser('add-account')
    account.add_argument('--label', required=True)
    account.add_argument('--token-file', type=Path, required=True)
    account.add_argument('--tenant', help='Existing tenant UUID')
    tenant = sub.add_parser('add-tenant')
    tenant.add_argument('--label', required=True)
    revoke = sub.add_parser('revoke-account')
    revoke.add_argument('--account', required=True, type=uuid.UUID)
    membership = sub.add_parser('join-tenant')
    membership.add_argument('--account', required=True, type=uuid.UUID)
    membership.add_argument('--tenant', required=True, type=uuid.UUID)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            initialize(args.state_dir, args.config, args.server_config)
            print('Service configuration created; keep it private. The service reloads account changes for each request.')
        elif args.command == 'add-account':
            print(add_account(args.state_dir, args.config, args.server_config, args.label, args.token_file, args.tenant))
        elif args.command == 'add-tenant':
            print(add_tenant(args.state_dir, args.config, args.label))
        elif args.command == 'revoke-account':
            revoke_account(args.state_dir,args.config,args.server_config,args.account)
            print('Account logins revoked and token mapping removed; new requests will be rejected')
        else:
            with connection(args.state_dir, args.config) as conn:
                conn.execute('INSERT INTO remote.membership(account_id,tenant_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (args.account, args.tenant))
            print('Membership added')
    except Exception as exc:
        # Database errors may contain SQL with role passwords; never print their details.
        parser.exit(2, f'Provisioning failed ({type(exc).__name__}); check private configuration and database state.\n')


if __name__ == '__main__':
    main()
