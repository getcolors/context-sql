"""Authenticated HTTPS SQL service; database logins remain server-side."""
import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
import threading
import time
import uuid

import psycopg
from psycopg.types.json import Jsonb
from pglast import parser
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

MAX_BYTES = 65536
MAX_REQUEST = 131072


def normalize(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def encode(value):
    return json.dumps(normalize(value), default=lambda v: {'base64': base64.b64encode(bytes(v)).decode()} if isinstance(v, (bytes, memoryview)) else str(v), ensure_ascii=False, separators=(',', ':')).encode()


def validate_sql(sql, params=None):
    if not isinstance(sql, str) or not sql.strip() or len(sql.encode()) > 65536:
        raise ValueError('SQL must contain 1 to 65536 bytes')
    try:
        # Match the pinned driver's placeholder conversion, including %% escapes
        # and quoted text. Parameter values never become SQL source.
        if params is not None:
            from psycopg._queries import _query2pg
            parsed_sql = _query2pg(sql.encode(), 'utf-8')[0].decode()
        else:
            parsed_sql = sql
        tree = json.loads(parser.parse_sql_json(parsed_sql))
    except Exception:
        raise ValueError('Invalid SQL syntax') from None
    statements = tree.get('stmts', [])
    allowed = {'SelectStmt', 'InsertStmt', 'UpdateStmt', 'DeleteStmt', 'MergeStmt'}
    if len(statements) != 1 or not set(statements[0]['stmt']).issubset(allowed):
        raise ValueError('Submit one SELECT, INSERT, UPDATE, DELETE, or MERGE statement')

    def visit(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key[:1].isupper() and key.endswith('Stmt') and key not in allowed:
                    raise ValueError('SQL contains a forbidden statement')
                if key == 'intoClause':
                    raise ValueError('SELECT INTO is not supported')
                if key == 'FuncCall':
                    names = [n.get('String', {}).get('sval', '').lower() for n in value.get('funcname', [])]
                    if names and names[-1] in {'set_config', 'pg_advisory_lock', 'pg_advisory_lock_shared', 'pg_try_advisory_lock', 'pg_try_advisory_lock_shared'}:
                        raise ValueError('Session-changing functions are not supported')
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(tree)


def connect(settings, **extra):
    settings = {**settings, 'connect_timeout': 3, 'options': '-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=5000 -c search_path=pg_catalog', **extra}
    return psycopg.connect(**settings)


def worker(payload):
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    response = {'query_id': payload['query_id'], 'prompt_id': payload['prompt_id'], 'columns': [], 'rows': [], 'truncated': False, 'commit_state': 'rolled_back'}
    commit_started = False
    try:
        with connect(payload['settings'], application_name=payload['query_id']) as conn:
            # A separate first output line precedes caller SQL. These immutable
            # backend coordinates survive caller changes to application_name.
            started = conn.execute('SELECT backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()').fetchone()[0]
            sys.stdout.buffer.write(encode({'pid': conn.info.backend_pid, 'backend_start': started}) + b'\n')
            sys.stdout.buffer.flush()
            conn.rollback()
            if payload['role'] == 'reader':
                conn.execute('SET TRANSACTION READ ONLY')
            with conn.cursor() as cur:
                cur.execute(payload['sql'], payload.get('params'), prepare=True)
                response['row_count'] = cur.rowcount
                if cur.description:
                    response['columns'] = [column.name for column in cur.description]
                    for index, row in enumerate(cur):
                        if index >= 50:
                            response['truncated'] = True
                            break
                        response['rows'].append(list(row))
                        if len(encode(response)) > MAX_BYTES - 1024:
                            response['rows'].pop()
                            response['truncated'] = True
                            break
            # Explicit commit: an exception before this point rolls back.
            commit_started = True
            conn.commit()
            response['commit_state'] = 'committed'
            response['outcome'] = 'succeeded'
    except Exception as error:
        response = {'query_id': payload['query_id'], 'prompt_id': payload['prompt_id'], 'outcome': 'unknown' if commit_started else 'failed', 'commit_state': 'unknown' if commit_started else 'rolled_back', 'error': 'SQL execution failed', 'sqlstate': getattr(error, 'sqlstate', None)}
    if len(encode(response)) > MAX_BYTES:
        response = {'query_id': payload['query_id'], 'prompt_id': payload['prompt_id'], 'outcome': 'succeeded', 'commit_state': 'committed', 'rows': [], 'truncated': True}
    sys.stdout.buffer.write(encode(response))


def execute(payload):
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    process = subprocess.Popen([sys.executable, '-m', 'package_context_sql_blue.remote_service', '--worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    output = b''
    try:
        output, _ = process.communicate(encode(payload), timeout=8)
        if process.returncode != 0 or len(output) > MAX_BYTES + 1024:
            raise ValueError('Worker failed')
        return json.loads(output.split(b'\n', 1)[-1])
    except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        output = getattr(error, 'output', None) or output
        try:
            backend = json.loads(output.split(b'\n', 1)[0])
            with connect(payload['settings'], autocommit=True) as conn:
                conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=session_user AND pid=%s AND backend_start=%s', (backend['pid'], backend['backend_start']))
        except Exception:
            pass
        process.kill()
        process.communicate()
        return {'query_id': payload['query_id'], 'prompt_id': payload['prompt_id'], 'outcome': 'unknown', 'commit_state': 'unknown', 'error': 'Execution deadline or worker failure; inspect trace before retrying writes'}


def load_config(path):
    path = Path(path).expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024**2:
        raise ValueError('Server config must be a private regular file owned by this user')
    config = json.loads(path.read_text())
    if config.get('format') != 1 or not isinstance(config.get('accounts'), list) or not config.get('trace'):
        raise ValueError('Invalid server config')
    for account in config['accounts']:
        uuid.UUID(account['account_id'])
        if len(account['token_sha256']) != 64:
            raise ValueError('Invalid token digest')
    return config


def create_app(config, config_path=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    semaphore = threading.BoundedSemaphore(8)
    recent = {}
    rate_lock = threading.Lock()

    @app.middleware('http')
    async def authenticate(request, call_next):
        if config_path is not None:
            try:
                updated = load_config(config_path)
            except Exception:
                return JSONResponse({'error': 'Service configuration unavailable'}, status_code=503)
            config.clear()
            config.update(updated)
        header = request.headers.get('authorization', '')
        digest = hashlib.sha256(header[7:].encode()).hexdigest() if header.startswith('Bearer ') and len(header) <= 1024 else ''
        account = next((item for item in config['accounts'] if hmac.compare_digest(item['token_sha256'], digest)), None)
        if account is None:
            return JSONResponse({'error': 'Authentication required'}, status_code=401)
        with rate_lock:
            now = time.monotonic()
            calls = [stamp for stamp in recent.get(account['account_id'], []) if now - stamp < 60]
            if len(calls) >= 120:
                return JSONResponse({'error': 'Rate limit exceeded'}, status_code=429)
            recent[account['account_id']] = calls + [now]
        if not semaphore.acquire(blocking=False):
            return JSONResponse({'error': 'Service is busy'}, status_code=429)
        request.state.account = account
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse({'error': 'Service operation failed'}, status_code=503)
        finally:
            semaphore.release()

    async def body(request):
        import asyncio
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > MAX_REQUEST:
                        raise HTTPException(413, 'Request exceeds 131072 bytes')
        except TimeoutError:
            raise HTTPException(408, 'Request body deadline exceeded') from None
        try:
            def reject_constant(value):
                raise ValueError('JSON constants must be finite')
            value = json.loads(raw, parse_constant=reject_constant)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Request must be a JSON object') from None

    def trace():
        return connect(config['trace'])

    @app.post('/v1/prompts')
    async def prompt(request: Request):
        data = await body(request)
        if set(data) - {'text', 'kind', 'project', 'task', 'parent_id', 'session_id'}:
            raise HTTPException(400, 'Unknown prompt field')
        for field in ('text', 'project', 'task'):
            if not isinstance(data.get(field), str) or not data[field].strip() or len(data[field].encode()) > (65536 if field == 'text' else 255):
                raise HTTPException(400, 'Invalid prompt text, project, or task')
        if data.get('kind', 'prompt') not in {'prompt', 'summary'}:
            raise HTTPException(400, 'Invalid prompt kind')
        if data.get('session_id') is not None and (not isinstance(data['session_id'], str) or not 1 <= len(data['session_id']) <= 255):
            raise HTTPException(400, 'Invalid session_id')
        prompt_id = str(uuid.uuid4())
        with trace() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (request.state.account['account_id'],))
            sequence = conn.execute('SELECT coalesce(max(sequence),-1)+1 FROM trace.prompt WHERE account_id=%s', (request.state.account['account_id'],)).fetchone()[0]
            if data.get('parent_id') and not conn.execute('SELECT 1 FROM trace.prompt WHERE prompt_id=%s AND account_id=%s', (data['parent_id'], request.state.account['account_id'])).fetchone():
                raise HTTPException(404, 'Parent prompt not found')
            conn.execute('INSERT INTO trace.prompt(prompt_id,account_id,text,kind,project,task,parent_prompt_id,session_id,sequence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', (prompt_id, request.state.account['account_id'], data['text'], data.get('kind', 'prompt'), data['project'], data['task'], data.get('parent_id'), data.get('session_id'), sequence))
        return {'prompt_id': prompt_id, 'sequence': sequence}

    @app.post('/v1/sql')
    async def sql(request: Request):
        import asyncio
        data = await body(request)
        account = request.state.account
        if set(data) - {'prompt_id', 'sql', 'params', 'role', 'capture_result'} or data.get('role', 'reader') not in {'reader', 'writer'} or not isinstance(data.get('sql'), str) or not isinstance(data.get('params', []), (list, dict)) or not isinstance(data.get('capture_result', False), bool):
            raise HTTPException(400, 'Invalid SQL request')
        try:
            prompt_id = str(uuid.UUID(data['prompt_id']))
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, 'Valid prompt_id required') from None
        query_id = str(uuid.uuid4())
        with trace() as conn:
            if not conn.execute('SELECT 1 FROM trace.prompt WHERE prompt_id=%s AND account_id=%s', (prompt_id, account['account_id'])).fetchone():
                raise HTTPException(404, 'Prompt not found')
            conn.execute('INSERT INTO trace.query(query_id,account_id,prompt_id,sql,parameters,role) VALUES (%s,%s,%s,%s,%s,%s)', (query_id, account['account_id'], prompt_id, data['sql'], Jsonb(data.get('params')), data.get('role', 'reader')))
        try:
            validate_sql(data['sql'], data.get('params'))
        except ValueError as error:
            result = {'query_id': query_id, 'prompt_id': prompt_id, 'outcome': 'failed', 'commit_state': 'not_started', 'error': str(error)}
        else:
            payload = {**data, 'query_id': query_id, 'prompt_id': prompt_id, 'role': data.get('role', 'reader'), 'settings': account[data.get('role', 'reader')]}
            payload['sql'] = f'/* context_sql: prompt_id={prompt_id} query_id={query_id} */\n' + data['sql']
            result = await asyncio.to_thread(execute, payload)
        stored = result if data.get('capture_result', False) else {key: value for key, value in result.items() if key not in {'rows', 'columns'}}
        try:
            with trace() as conn:
                conn.execute('UPDATE trace.query SET finished_at=clock_timestamp(),outcome=%s,response=%s WHERE query_id=%s AND account_id=%s', (result['outcome'], Jsonb(stored), query_id, account['account_id']))
        except Exception:
            result['trace_completion'] = 'failed; attempt remains recorded'
        return JSONResponse(result)

    @app.get('/v1/schema')
    def schema(request: Request):
        with connect(request.state.account['reader']) as conn:
            rows = conn.execute("SELECT table_schema,table_name,column_name,data_type FROM information_schema.columns WHERE table_schema IN ('catalog','memory','remote','trace') ORDER BY table_schema,table_name,ordinal_position LIMIT 300").fetchall()
        result = {'columns': [], 'limit': 300, 'truncated': len(rows) == 300}
        for row in rows:
            result['columns'].append(list(row))
            if len(encode(result)) > MAX_BYTES - 128:
                result['columns'].pop()
                result['truncated'] = True
                break
        return result

    return app


def main():
    if sys.argv[1:] == ['--worker']:
        worker(json.loads(sys.stdin.buffer.read(MAX_REQUEST * 2)))
        return
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--config', required=True)
    cli.add_argument('--host', default='127.0.0.1')
    cli.add_argument('--port', type=int, default=8443)
    cli.add_argument('--certfile', required=True)
    cli.add_argument('--keyfile', required=True)
    args = cli.parse_args()
    import uvicorn
    uvicorn.run(create_app(load_config(args.config), args.config), host=args.host, port=args.port, ssl_certfile=args.certfile, ssl_keyfile=args.keyfile, access_log=False, log_level='warning', limit_concurrency=16, timeout_keep_alive=5)


if __name__ == '__main__':
    main()
