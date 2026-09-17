"""Blue workflow for a local, private PostgreSQL context database."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from blue import dry_run
from blue.workflow import workflow
from .local_db import LocalDB, ROOT, PORT, private_write

DEFAULTS = {
    'context-sql-state-dir': '~/.local/share/context-sql',
    'context-sql-config': '~/.config/context-sql/connections.json',
    'context-sql-skill-dir': '~/.codex/skills/sql-context',
    'context-sql-install-skill': True,
    'context-sql-service': False,
}
KEYS = {'profile', *DEFAULTS}
PATH_KEYS = ('context-sql-state-dir', 'context-sql-config', 'context-sql-skill-dir')
BOOL_KEYS = ('context-sql-install-skill', 'context-sql-service')
INSTALL_MARKER = '.context-sql-install.json'


def validation_errors(values):
    errors = []
    for key in values:
        if key not in KEYS:
            errors.append(f'Unknown configuration key: {key}')
    if not isinstance(values.get('profile'), str) or not re.fullmatch(
            r'[a-z0-9][a-z0-9-]{0,62}', values.get('profile', '')):
        errors.append('profile must contain 1 to 63 lowercase letters, digits, or hyphens and start with a letter or digit')
    for key in BOOL_KEYS:
        if type(values.get(key)) is not bool:
            errors.append(f'{key} must be true or false')
    for key in PATH_KEYS:
        value = values.get(key)
        if not isinstance(value, str) or not value.strip() or any(c in value for c in '\0\r\n'):
            errors.append(f'{key} must be a nonempty path without NUL or newlines')
        elif not (value.startswith('~/') or Path(value).is_absolute()):
            errors.append(f'{key} must be absolute or start with ~/')
        elif len(value) > 4096:
            errors.append(f'{key} is too long')
    if 'COLORS_PAR_PROFILE' in os.environ:
        errors.append('COLORS_PAR_PROFILE is forbidden; set profile in colors.yml')
    if not errors:
        paths = {k: Path(values[k]).expanduser().resolve() for k in PATH_KEYS}
        state, config, skill = (paths[k] for k in PATH_KEYS)
        if len(os.fsencode(str(state / 'socket' / f'.s.PGSQL.{PORT}'))) > 100:
            errors.append('context-sql-state-dir is too long for a PostgreSQL Unix socket')
        if state == state.parent or skill == skill.parent or skill == Path.home():
            errors.append('State and skill targets must be dedicated directories')
        if state == config or state in config.parents or config in state.parents:
            errors.append('Connection config must be separate from the state directory')
        if skill == state or skill in state.parents or state in skill.parents:
            errors.append('Skill and state directories must not overlap')
        if skill == config or skill in config.parents or config in skill.parents:
            errors.append('Skill directory and connection config must not overlap')
    return errors


def plan(opts):
    return {
        'profile': opts['profile'],
        'state-dir': opts['context-sql-state-dir'],
        'connection-config': opts['context-sql-config'],
        'skill-dir': opts['context-sql-skill-dir'],
        'install-skill': opts['context-sql-install-skill'],
        'systemd-service': opts['context-sql-service'],
        'listen': 'private Unix socket only',
        'database': 'context_sql',
        'port': PORT,
        'migrations': [p.name for p in sorted((ROOT / 'sql').glob('[0-9][0-9][0-9]_*.sql'))],
        'seed-sha256': hashlib.sha256((ROOT / 'data/skills.sql').read_bytes()).hexdigest(),
    }


def build_step(opts):
    directory = Path(opts['blue/state-file']).parent / '.colors' / opts['profile'] / 'context-sql'
    for target in (directory.parent.parent, directory.parent, directory):
        if target.is_symlink():
            raise ValueError(f'Refusing generated-directory symlink: {target}')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'plan.json'
    if target.is_symlink():
        raise ValueError('Refusing plan.json symlink')
    target.write_text(json.dumps(plan(opts), indent=2, sort_keys=True) + '\n')
    return {**opts, 'blue/exit': 0, 'context-sql/result': {'plan': str(target)}}


def skill_payload(opts):
    source = ROOT / 'skills/sql-context'
    return {
        'SKILL.md': (source / 'SKILL.md').read_bytes(),
        'scripts/context.py': (source / 'scripts/context.py').read_bytes(),
        'connection-path': (str(Path(opts['context-sql-config']).expanduser().absolute()) + '\n').encode(),
    }


def check_skill_target(opts):
    target = Path(opts['context-sql-skill-dir']).expanduser().absolute()
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError('Skill target must be a directory, not a symlink or file')
    expected = skill_payload(opts)
    manifest = target / INSTALL_MARKER
    if manifest.is_symlink():
        raise ValueError('Refusing skill manifest symlink')
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError('Invalid installed skill manifest')
    for name, content in expected.items():
        destination = target / name
        if destination.is_symlink() or destination.parent.is_symlink():
            raise ValueError(f'Refusing skill file symlink: {name}')
        if destination.exists():
            if not destination.is_file():
                raise ValueError(f'Skill file target is not a regular file: {name}')
            actual = destination.read_bytes()
            if actual != content and hashlib.sha256(actual).hexdigest() != previous.get(name):
                raise ValueError(f'Installed skill has unmanaged changes: {name}')
    # Do not claim an unrelated skill directory just because its filenames differ.
    if target.exists() and not manifest.exists():
        known = {'SKILL.md', 'scripts', 'scripts/context.py', 'scripts/__pycache__', 'connection-path'}
        for item in target.rglob('*'):
            name = item.relative_to(target).as_posix()
            if name not in known and not name.startswith('scripts/__pycache__/'):
                raise ValueError(f'Unmanaged skill target contains unexpected file: {name}')


def install_skill(opts):
    check_skill_target(opts)
    target = Path(opts['context-sql-skill-dir']).expanduser().absolute()
    payload = skill_payload(opts)
    for name, content in payload.items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        private_write(destination, content.decode('utf-8'))
    private_write(target / INSTALL_MARKER, json.dumps({
        name: hashlib.sha256(content).hexdigest() for name, content in payload.items()
    }, sort_keys=True) + '\n')


def preflight_step(opts):
    errors = []
    for binary in ('initdb', 'pg_ctl', 'psql', 'postgres', 'pg_isready', 'pg_dump'):
        if not shutil.which(binary):
            errors.append(f'Missing PostgreSQL tool on PATH: {binary}')
    if os.getuid() == 0:
        errors.append('Run as your normal Unix user; PostgreSQL cannot run as root')
    if not errors:
        version = subprocess.run(['postgres', '--version'], capture_output=True, text=True, check=True).stdout
        match = re.search(r'(\d+)\.', version)
        if not match or int(match[1]) < 16:
            errors.append('PostgreSQL 16 or newer is required')
    if opts['context-sql-service'] and (sys.platform != 'linux' or not shutil.which('systemctl')):
        errors.append('context-sql-service requires Linux with systemd user services')
    if errors:
        return {**opts, 'blue/exit': 2, 'blue/err': '\n'.join(errors)}
    if opts['blue/event'] == 'create' and opts['context-sql-install-skill']:
        check_skill_target(opts)
    if opts['blue/event'] == 'create':
        state = Path(opts['context-sql-state-dir']).expanduser().absolute()
        config = Path(opts['context-sql-config']).expanduser().absolute()
        if config.is_symlink():
            raise ValueError('Refusing connection config symlink')
        if config.exists() and not (state / 'managed.json').is_file():
            raise ValueError('Connection config already exists for an unmanaged instance; choose a new config path')
    return {**opts, 'blue/exit': 0}


@contextmanager
def locked_db(opts):
    state = Path(opts['context-sql-state-dir']).expanduser().absolute()
    config = Path(opts['context-sql-config']).expanduser().absolute()
    state.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(state.parent / ('.' + state.name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield LocalDB(state, config)


def database_step(opts):
    with locked_db(opts) as db:
        event = opts['blue/event']
        if event == 'create':
            db.init()
            if opts['context-sql-service']:
                db.enable()
            result = {'initialized': True, 'state-dir': str(db.state),
                      'connection-config': str(db.config)}
        elif event == 'status':
            db.load()
            result = {'running': db.running(), 'state-dir': str(db.state), 'systemd': db.service_enabled()}
        elif event == 'backup':
            result = {'backup': str(db.backup(None))}
        else:
            getattr(db, event)()
            result = {'command': event, 'ok': True}
    return {**opts, 'blue/exit': 0, 'context-sql/result': result}


def skill_step(opts):
    if opts['context-sql-install-skill']:
        install_skill(opts)
    return {**opts, 'blue/exit': 0}


def wire_fn(step, opts):
    event = opts['blue/event']
    graph = {'context-sql/validate': (lambda o: {**o, 'blue/exit': 0},
                                    'context-sql/build' if event == 'build' else 'context-sql/preflight'),
             'context-sql/build': (build_step,),
             'context-sql/preflight': (preflight_step, 'context-sql/database'),
             'context-sql/database': (database_step, 'context-sql/skill') if event == 'create' else (database_step,),
             'context-sql/skill': (skill_step,)}
    return graph.get(step)


context_workflow = dry_run.advise(workflow(start='context-sql/validate', wire_fn=wire_fn),
    ['context-sql/build', 'context-sql/preflight', 'context-sql/database', 'context-sql/skill'])
