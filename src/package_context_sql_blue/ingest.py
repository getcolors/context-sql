"""Acquire Context Skills and reproduce a shared PostgreSQL catalog lock."""
import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .acquisition import acquire, parse_source, resolve_cli_version
from . import ingest_lock as locks
from .ingest_db import commit_import, export_lock
from .local_db import DEFAULT_STATE, DEFAULT_CONFIG


class PublicationError(Exception):
    """The database committed, but publishing the external lockfile failed."""


def parser():
    cli = argparse.ArgumentParser(prog="context-skills", description=__doc__)
    commands = cli.add_subparsers(dest='command', required=True)
    for name in ('ingest', 'sync', 'update', 'export-lock'):
        command = commands.add_parser(name)
        command.add_argument('--lockfile', type=Path, default=Path('context-skills.lock.json'))
        command.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
        command.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
        if name != 'export-lock':
            command.add_argument('--dry-run', action='store_true')
            command.add_argument('--skills-cli-version', help='Use this exact skills CLI version instead of resolving the current release')
        if name == 'ingest':
            command.add_argument('source', help='GitHub owner/repo or HTTPS repository URL, optionally /tree/ref')
            command.add_argument('--revision', help='Branch, tag, or full commit SHA')
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument('--skill', action='append', nargs='+')
            selection.add_argument('--all', dest='all_skills', action='store_true')
        elif name == 'sync':
            command.add_argument('--locked', action='store_true', required=True)
        elif name == 'export-lock':
            command.add_argument('--digest', required=True, help='SHA-256 reported by a committed import')
    return cli


@contextmanager
def locked_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = path.with_name('.' + path.name + '.guard')
    fd = os.open(guard, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def prepare_lock(path, document):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Refusing non-regular lockfile target')
    payload = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False).encode() + b'\n'
    if len(payload) > locks.MAX_LOCK_BYTES:
        raise ValueError('Generated lock exceeds 16 MiB')
    with tempfile.NamedTemporaryFile(prefix='.' + path.name + '.', suffix='.pending',
                                     dir=path.parent, delete=False) as handle:
        pending = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            pending.unlink(missing_ok=True)
            raise
    return pending


def publish_lock(pending, path):
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def acquire_document(args, existing):
    version = resolve_cli_version(args.skills_cli_version)
    bundles, resolutions = [], []
    if args.command == 'ingest':
        repository, _ = parse_source(args.source, args.revision)
        old = next((entry for entry in existing['imports'] if entry['repository'] == repository), None)
        selected = set(name for group in (args.skill or []) for name in group)
        if old:
            selected.update(skill['name'] for skill in old['skills'])
        select_all = args.all_skills or bool(old and old['selection']['all'])
        bundle, resolution = acquire(args.source, args.revision,
            skills=sorted(selected) if not select_all else None,
            all_skills=select_all, cli_version=version)
        entry = locks.entry_from_bundle(bundle, resolution, select_all)
        entries = [item for item in existing['imports'] if item['repository'] != repository] + [entry]
        bundles.append(bundle)
        resolutions.append(resolution)
        # A shared lock may have come from another machine. Retained entries
        # must be reproduced too, not merely asserted in the committed lock.
        for retained in existing['imports']:
            if retained['repository'] == repository:
                continue
            bundle, resolution = acquire(retained['repository'], retained['commit'],
                skills=[skill['name'] for skill in retained['skills']], cli_version=version)
            locks.verify_entry(retained, bundle)
            bundles.append(bundle)
            resolutions.append(resolution)
    else:
        entries = []
        for entry in existing['imports']:
            if args.command == 'sync':
                bundle, resolution = acquire(entry['repository'], entry['commit'],
                    skills=[skill['name'] for skill in entry['skills']], cli_version=version)
                locks.verify_entry(entry, bundle)
                entries.append(entry)
            else:
                select_all = entry['selection']['all']
                bundle, resolution = acquire(entry['requestedSource'], entry['requestedRef'],
                    skills=None if select_all else [skill['name'] for skill in entry['skills']],
                    all_skills=select_all, cli_version=version)
                entries.append(locks.entry_from_bundle(bundle, resolution, select_all))
            bundles.append(bundle)
            resolutions.append(resolution)
    return locks.document(entries), bundles, resolutions


def execute(args):
    path = args.lockfile.expanduser().absolute()
    dry_run = getattr(args, 'dry_run', False)
    with nullcontext() if dry_run else locked_file(path):
        if args.command == 'export-lock':
            document = locks.validate(export_lock(args.state_dir.expanduser(), args.config.expanduser(), args.digest))
            if locks.digest(document) != args.digest:
                raise ValueError('Stored lock digest differs')
            pending = prepare_lock(path, document)
            publish_lock(pending, path)
            return {'operation': 'export-lock', 'lockfile': str(path), 'lockDigest': args.digest}
        if args.command == 'ingest' and not path.exists() and not path.is_symlink():
            existing = {'format': locks.FORMAT, 'imports': []}
        else:
            existing = locks.load(path)
        document, bundles, resolutions = acquire_document(args, existing)
        lock_hash = locks.digest(document)
        summary = {
            'operation': args.command, 'dryRun': dry_run, 'lockfile': str(path), 'lockDigest': lock_hash,
            'imports': [{'repository': bundle['repository_url'], 'commit': bundle['git_commit'],
                         'skills': [skill['slug'] for skill in bundle['skills']],
                         'files': sum(len(skill['files']) for skill in bundle['skills']),
                         'acquisitionCliVersion': bundle['acquisition']['skills_cli_version']}
                        for bundle in bundles],
        }
        if dry_run:
            return summary
        # Prepare and fsync before PostgreSQL commit. Neither a verified download
        # nor a database failure may replace the previous public lockfile.
        pending = None if args.command == 'sync' else prepare_lock(path, document)
        try:
            committed = commit_import(bundles, resolutions, document,
                                      args.state_dir.expanduser(), args.config.expanduser())
            if committed != lock_hash:
                raise RuntimeError('Committed lock digest differs')
        except BaseException:
            if pending:
                pending.unlink(missing_ok=True)
            raise
        if pending:
            try:
                publish_lock(pending, path)
            except OSError as error:
                raise PublicationError(
                    f'Database import committed with lock digest {lock_hash}, but lock publication failed. '
                    f'Recover using export-lock --digest {lock_hash} --lockfile {path} '
                    f'with the same --state-dir and --config. Pending lock: {pending}.') from error
        summary['committed'] = True
        return summary


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
    except PublicationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(3)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'Ingestion failed: {error}', file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # PostgreSQL diagnostics may contain source records or connection data.
        print(f'Ingestion failed: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
