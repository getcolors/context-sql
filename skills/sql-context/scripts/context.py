#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]==3.2.10"]
# ///
"""Run bounded, parameterized SQL operations for evidence and task memory."""
import argparse
import json
import multiprocessing
import os
import re
from pathlib import Path
import stat
import sys
import uuid


DEADLINE_SECONDS = 8
MAX_BYTES = 65536
WRITE_OPERATIONS = {'start', 'adopt', 'note', 'state', 'expire'}
MEMORY_OPERATIONS = WRITE_OPERATIONS | {'runs', 'restore', 'item'}
KINDS = ('observation', 'hypothesis', 'decision', 'todo', 'tool_result', 'summary')


def bounded_int(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f'Must be between {low} and {high}')
        return number
    return parse


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest='operation', required=True)
    for operation in ('catalog', 'search', 'sections', 'section', 'file', 'start',
                      'runs', 'adopt', 'note', 'restore', 'item', 'state', 'expire'):
        command = commands.add_parser(operation)
        command.add_argument('--limit', type=bounded_int(1, 50), default=20)
        command.add_argument('--offset', type=bounded_int(0, 1000000), default=0)
        command.add_argument('--max-bytes', type=bounded_int(1024, MAX_BYTES), default=MAX_BYTES)
        if operation in ('search', 'sections', 'section', 'file'):
            command.add_argument('--skill', required=operation != 'search')
        if operation in ('sections', 'section', 'file'):
            command.add_argument('--version', required=operation != 'sections')
            command.add_argument('--path', required=operation != 'sections')
        if operation == 'search':
            command.add_argument('--query', required=True)
        if operation in ('search', 'section', 'restore', 'item', 'file'):
            command.add_argument('--length', type=bounded_int(1, 8192), default=512 if operation == 'search' else 2048)
        if operation in ('section', 'item'):
            command.add_argument('--text-offset', type=bounded_int(0, 10000000), default=0)
        if operation == 'section':
            command.add_argument('--ordinal', type=bounded_int(0, 1000000), required=True)
        if operation == 'file':
            command.add_argument('--byte-offset', type=bounded_int(0, 1000000000), default=0)
        if operation in ('start', 'adopt'):
            command.add_argument('--project', required=True)
            command.add_argument('--local-path')
        if operation == 'runs':
            identity = command.add_mutually_exclusive_group(required=True)
            identity.add_argument('--project')
            identity.add_argument('--legacy', action='store_true')
        if operation == 'start':
            command.add_argument('--task', required=True)
            command.add_argument('--description')
        if operation in ('note', 'restore', 'item', 'state', 'adopt'):
            command.add_argument('--run', type=uuid.UUID, required=True)
        if operation == 'note':
            command.add_argument('--kind', choices=KINDS, required=True)
            body = command.add_mutually_exclusive_group(required=True)
            body.add_argument('--body')
            body.add_argument('--body-file', type=Path)
            command.add_argument('--priority', type=bounded_int(-1000, 1000), default=0)
            for field in ('skill', 'version', 'path', 'section'):
                command.add_argument('--source-' + field, type=int if field == 'section' else str)
        if operation in ('item', 'state'):
            command.add_argument('--item', type=bounded_int(1, 2**63 - 1), required=True)
        if operation == 'state':
            command.add_argument('--state', choices=('active', 'superseded', 'done'), required=True)
    return cli


def connection_settings(memory):
    binding = Path(__file__).resolve().parent.parent / 'connection-path'
    default = '~/.config/context-sql/connections.json'
    if 'CONTEXT_SQL_CONFIG' not in os.environ and binding.exists():
        if binding.stat().st_size > 4097:
            raise ValueError('Installed connection path is too large')
        default = binding.read_text().rstrip('\n')
        if not Path(default).is_absolute() or '\n' in default or '\0' in default:
            raise ValueError('Installed connection path must be absolute')
    path = Path(os.environ.get('CONTEXT_SQL_CONFIG', default)).expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Connection config must be a private regular file owned by this user, mode 0600')
    if info.st_size > 16384:
        raise ValueError('Connection config is too large')
    config = json.loads(path.read_text())
    settings = config['writer' if memory else 'reader']
    if set(settings) != {'host', 'port', 'dbname', 'user', 'password'}:
        raise ValueError('Connection config must contain host, port, dbname, user, and password')
    if not isinstance(settings['host'], str) or not settings['host'].startswith('/'):
        raise ValueError('The local runner requires a Unix socket directory')
    # Ambient libpq variables must not redirect this private connection.
    for key in list(os.environ):
        if key.startswith('PG'):
            del os.environ[key]
    return dict(settings, connect_timeout=3, application_name='sql-context',
                options='-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=5000 -c search_path=pg_catalog')


def validate(args):
    values = vars(args).copy()
    for key, value in values.items():
        if isinstance(value, str) and ('\0' in value or len(value.encode('utf-8')) > 16384):
            raise ValueError(f'Invalid or oversized {key}')
    if args.operation in ('start', 'runs', 'adopt') and args.project is not None:
        if len(args.project) > 255 or not re.fullmatch(
                r'[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)+', args.project):
            raise ValueError('Project must be a lowercase namespace/name ID, at most 255 characters')
    if args.operation in ('start', 'adopt'):
        values['local_path'] = (str(Path(args.local_path).expanduser().resolve())
                                if args.local_path is not None else None)
        if values['local_path'] is not None and len(values['local_path']) > 4096:
            raise ValueError('Local path is too long')
    if args.operation == 'start':
        if not 1 <= len(args.task.strip()) <= 128:
            raise ValueError('Task key must contain 1 to 128 characters')
        values['description'] = args.description or args.task
        if len(values['description'].encode()) > 4096:
            raise ValueError('Task description exceeds 4096 bytes')
        values['new_run'] = uuid.uuid4()
    if args.operation == 'search' and not args.query.strip():
        raise ValueError('Search query cannot be empty')
    if args.operation == 'note':
        if args.body_file:
            if not args.body_file.is_file() or args.body_file.stat().st_size > 16384:
                raise ValueError('Note file must be a regular file of at most 16384 bytes')
            values['body'] = args.body_file.read_text(encoding='utf-8')
        if not values['body'] or '\0' in values['body'] or len(values['body'].encode()) > 16384:
            raise ValueError('Note body must contain 1 to 16384 UTF-8 bytes without NUL')
        source = [values['source_' + key] is not None for key in ('skill', 'version', 'path', 'section')]
        if any(source) and not all(source):
            raise ValueError('A source reference requires skill, version, path, and section together')
    values['fetch_limit'] = args.limit + 1
    return values


CITATION = """v.repository_url || '/blob/' || v.git_commit || '/' || v.source_directory || '/' || s.path ||
              '#L' || s.start_line || '-L' || s.end_line"""
SECTION_FIELDS = f"""s.skill_slug, s.version_id, s.path, s.ordinal, s.kind, s.heading,
    s.start_line, s.end_line, v.git_commit, v.evidence_status, {CITATION} AS citation"""
SECTION_JOIN = """FROM catalog.section s JOIN catalog.skill_version v USING (skill_slug, version_id)"""
CURRENT_JOIN = """JOIN catalog.skill k ON k.slug=s.skill_slug AND k.current_version=s.version_id"""
NOTE_FIELDS = f"""i.run_id, i.item_id, i.kind, i.state, i.priority, i.created_at,
    'agent_authored' AS memory_status, i.source_skill, i.source_version, i.source_path, i.source_section,
    s.heading AS source_heading, v.evidence_status, {CITATION} AS citation"""
NOTE_JOIN = """FROM working.item i
    LEFT JOIN catalog.section s ON (s.skill_slug,s.version_id,s.path,s.ordinal)=
      (i.source_skill,i.source_version,i.source_path,i.source_section)
    LEFT JOIN catalog.skill_version v ON (v.skill_slug,v.version_id)=(s.skill_slug,s.version_id)"""


def query_for(operation):
    queries = {
        'catalog': """SELECT k.slug, k.kind, v.version_id, v.routing_description, v.evidence_status
            FROM catalog.skill k JOIN catalog.skill_version v
              ON (v.skill_slug,v.version_id)=(k.slug,k.current_version)
            ORDER BY k.slug LIMIT %(fetch_limit)s OFFSET %(offset)s""",
        'search': f"""SELECT {SECTION_FIELDS}, left(s.body,%(length)s) AS body,
                length(s.body) AS total_chars
            {SECTION_JOIN} {CURRENT_JOIN}
            WHERE (%(skill)s::text IS NULL OR s.skill_slug=%(skill)s)
              AND (position(lower(%(query)s) IN lower(s.body)) > 0
                   OR s.search @@ plainto_tsquery('simple',%(query)s))
            ORDER BY (position(lower(%(query)s) IN lower(s.body)) > 0) DESC,
                ts_rank_cd(s.search,plainto_tsquery('simple',%(query)s)) DESC,
                s.skill_slug,s.path,s.ordinal
            LIMIT %(fetch_limit)s OFFSET %(offset)s""",
        'sections': f"""SELECT {SECTION_FIELDS}, length(s.body) AS total_chars
            {SECTION_JOIN} JOIN catalog.skill k ON k.slug=s.skill_slug
            WHERE s.skill_slug=%(skill)s AND s.version_id=coalesce(%(version)s,k.current_version)
              AND (%(path)s::text IS NULL OR s.path=%(path)s)
            ORDER BY s.path,s.ordinal LIMIT %(fetch_limit)s OFFSET %(offset)s""",
        'section': f"""SELECT {SECTION_FIELDS},
                substring(s.body FROM (%(text_offset)s + 1) FOR %(length)s) AS body,
                length(s.body) AS total_chars
            {SECTION_JOIN} WHERE s.skill_slug=%(skill)s AND s.version_id=%(version)s
              AND s.path=%(path)s AND s.ordinal=%(ordinal)s""",
        'file': """SELECT f.skill_slug,f.version_id,f.path,f.sha256,f.git_mode,f.line_count,
                octet_length(f.content) AS total_bytes,
                encode(substring(f.content FROM (%(byte_offset)s + 1) FOR %(length)s),'base64') AS content_base64,
                v.git_commit,v.evidence_status
            FROM catalog.source_file f JOIN catalog.skill_version v USING (skill_slug,version_id)
            WHERE f.skill_slug=%(skill)s AND f.version_id=%(version)s AND f.path=%(path)s""",
        'start': """INSERT INTO working.run (run_id,task,project_id,task_key,local_path)
            VALUES (%(new_run)s,%(description)s,%(project)s,%(task)s,%(local_path)s)
            ON CONFLICT (owner_name,project_id,task_key)
              DO UPDATE SET expires_at=greatest(working.run.expires_at,now()+interval '7 days'),
                local_path=coalesce(excluded.local_path,working.run.local_path)
            RETURNING run_id,project_id,local_path,task_key,task,created_at,expires_at""",
        'runs': """SELECT run_id,project_id,local_path,project_key,task_key,
                left(task,512) AS task,created_at,expires_at
            FROM working.run WHERE expires_at>now() AND
              ((%(legacy)s AND project_id IS NULL) OR project_id=%(project)s)
            ORDER BY created_at DESC,run_id LIMIT %(fetch_limit)s OFFSET %(offset)s""",
        'adopt': """UPDATE working.run SET project_id=%(project)s,
                local_path=coalesce(%(local_path)s,local_path)
            WHERE run_id=%(run)s AND project_id IS NULL AND task_key IS NOT NULL
                AND expires_at>now()
            RETURNING run_id,project_id,local_path,project_key,task_key,task,created_at,expires_at""",
        'note': """INSERT INTO working.item
              (run_id,kind,body,priority,source_skill,source_version,source_path,source_section)
            VALUES (%(run)s,%(kind)s,%(body)s,%(priority)s,%(source_skill)s,%(source_version)s,%(source_path)s,%(source_section)s)
            RETURNING run_id,item_id,kind,state,priority,source_skill,source_version,source_path,source_section""",
        'restore': f"""SELECT {NOTE_FIELDS}, left(i.body,%(length)s) AS body,
                length(i.body) AS total_chars, left(s.body,512) AS source_excerpt
            {NOTE_JOIN} WHERE i.run_id=%(run)s AND i.state='active'
            ORDER BY i.priority DESC,i.item_id LIMIT %(fetch_limit)s OFFSET %(offset)s""",
        'item': f"""SELECT {NOTE_FIELDS},
                substring(i.body FROM (%(text_offset)s + 1) FOR %(length)s) AS body,
                length(i.body) AS total_chars
            {NOTE_JOIN} WHERE i.run_id=%(run)s AND i.item_id=%(item)s""",
        'state': """UPDATE working.item SET state=%(state)s
            WHERE run_id=%(run)s AND item_id=%(item)s RETURNING run_id,item_id,kind,state""",
        'expire': """WITH deleted AS (DELETE FROM working.run WHERE expires_at<now() RETURNING run_id)
            SELECT count(*) AS expired_runs FROM deleted""",
    }
    return queries[operation]


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':')) + '\n').encode('utf-8')


def encode_result(operation, rows, limit=20, offset=0, max_bytes=MAX_BYTES):
    result = dict(operation=operation, rows=rows[:limit], truncated=len(rows)>limit,
                  next_offset=None, output_bytes=0)
    while True:
        if result['truncated']:
            result['next_offset'] = offset + len(result['rows'])
            result['hint'] = 'Continue with --offset; if no row fits, increase --max-bytes or reduce --length.'
        for _ in range(8):
            payload = json_bytes(result)
            if result['output_bytes'] == len(payload):
                break
            result['output_bytes'] = len(payload)
        if len(payload) <= max_bytes:
            if operation in WRITE_OPERATIONS and len(result['rows']) != len(rows):
                raise ValueError('Write acknowledgement exceeds output limit; increase --max-bytes')
            return payload
        if not result['rows']:
            raise ValueError('Output budget cannot hold the response metadata')
        result['rows'].pop()
        result['truncated'] = True


def run_operation(args):
    import psycopg
    from psycopg.rows import dict_row

    values = validate(args)
    settings = connection_settings(args.operation in MEMORY_OPERATIONS)
    with psycopg.connect(**settings, row_factory=dict_row) as connection:
        connection.read_only = args.operation not in WRITE_OPERATIONS
        privileges = connection.execute("""SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,
            has_table_privilege('catalog.skill','INSERT,UPDATE,DELETE') AS catalog_write
            FROM pg_roles WHERE rolname=session_user""").fetchone()
        if any(privileges.values()):
            raise ValueError('Runner refuses an administrative or catalog-writing login')
        if args.operation not in MEMORY_OPERATIONS and connection.execute(
                "SELECT pg_has_role(session_user,'context_writer','MEMBER') AS writer").fetchone()['writer']:
            raise ValueError('Catalog reader must not inherit context_writer')
        if args.operation in ('note', 'restore', 'item', 'state', 'adopt'):
            run = connection.execute("""SELECT run_id FROM working.run
                WHERE run_id=%s AND expires_at>now()""", (args.run,)).fetchone()
            if run is None:
                raise ValueError('Run not found, expired, or not owned by this login; use start to resume')
        rows = connection.execute(query_for(args.operation), values).fetchall()
        if args.operation == 'adopt' and not rows:
            raise ValueError('Adoption requires an unassigned run with an existing task key')
        if args.operation in ('section', 'file', 'item', 'state') and not rows:
            raise ValueError('Requested record was not found')
        for row in rows:
            if 'body' in row and 'total_chars' in row:
                end = values.get('text_offset', 0) + len(row['body'])
                row['body_truncated'] = end < row['total_chars']
                row['next_text_offset'] = end if row['body_truncated'] else None
            if 'content_base64' in row:
                end = min(row['total_bytes'], values['byte_offset'] + values['length'])
                row['next_byte_offset'] = end if end < row['total_bytes'] else None
        # Serialize before committing writes. Errors here roll back the transaction.
        return encode_result(args.operation, rows, args.limit, args.offset, args.max_bytes)


def worker(args, pipe):
    try:
        payload = run_operation(args)
        pipe.send((0, payload))
    except Exception as error:
        # Database diagnostics may contain values from notes. Do not print them.
        if type(error).__module__.startswith('psycopg'):
            message = 'Database operation failed; check service, credentials, and source references.'
            code = getattr(error, 'sqlstate', None)
            if code == '57014':
                message = 'Database statement timed out.'
        elif isinstance(error, (ValueError, FileNotFoundError, PermissionError)):
            message = str(error)[:512]
        else:
            message = 'Context operation failed; check the local configuration.'
        pipe.send((1, json_bytes(dict(error=message))))
    finally:
        pipe.close()


def run_with_deadline(args, deadline=DEADLINE_SECONDS):
    # A separate process bounds connection, SQL, and serialization time together.
    context = multiprocessing.get_context('fork')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(args, send))
    process.start()
    send.close()
    try:
        if not receive.poll(deadline):
            return 1, json_bytes(dict(error='Context operation exceeded its external deadline. Inspect task state before retrying a write.'))
        try:
            return receive.recv()
        except EOFError:
            return 1, json_bytes(dict(error='Context worker exited without a result.'))
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()
        receive.close()


def main():
    args = parser().parse_args()
    status, payload = run_with_deadline(args)
    sys.stdout.buffer.write(payload)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
