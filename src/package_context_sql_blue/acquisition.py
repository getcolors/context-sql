"""Resolve upstream skills once, then acquire and verify their complete payload."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

try:
    from . import snapshot
except ImportError:
    spec = importlib.util.spec_from_file_location(
        'context_snapshot', Path(__file__).resolve().parents[2] / 'scripts/import_skills.py')
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)

SEMVER = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z')
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')


def parse_source(source, revision=None):
    """Return a canonical GitHub repository URL and one unambiguous Git ref."""
    if not isinstance(source, str) or any(ord(c) < 33 for c in source):
        raise ValueError('Source must be a GitHub HTTPS URL or owner/repository')
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source):
        source = 'https://github.com/' + source
    parsed = urlsplit(source)
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com'
            or parsed.query or parsed.fragment or '%' in parsed.path or '\\' in source):
        raise ValueError('Source must be a GitHub HTTPS repository URL without credentials or query')
    parts = parsed.path.strip('/').split('/')
    if (len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts[:2])
            or any(p in ('', '.', '..') for p in parts)):
        raise ValueError('Invalid repository URL')
    owner, repo = (part.lower() for part in parts[:2])
    repo = repo.removesuffix('.git')
    if not repo:
        raise ValueError('Invalid repository name')
    url_ref = None
    if len(parts) > 2:
        if parts[2] != 'tree' or len(parts) < 4:
            raise ValueError('Expected repository URL or tree/<revision> URL')
        url_ref = '/'.join(parts[3:])
    if revision is not None and url_ref is not None and revision != url_ref:
        raise ValueError('Conflicting URL revision and --revision')
    ref = revision if revision is not None else (url_ref or 'HEAD')
    if (not isinstance(ref, str) or not ref or ref.startswith('-') or
            any(c.isspace() or ord(c) < 32 for c in ref) or
            any(token in ref for token in ('..', '@{', ':', '~', '^', '?', '*', '[', '\\'))):
        raise ValueError('Invalid upstream revision')
    return f'https://github.com/{owner}/{repo}', ref


def discover(repo, commit):
    """Discover validated skill names without running any upstream code."""
    result = {}
    entries = snapshot.git(repo, 'ls-tree', '-r', '-z', commit).split(b'\0')
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        path = raw_path.decode('utf-8')
        if path != 'SKILL.md' and not path.endswith('/SKILL.md'):
            continue
        mode, kind, object_id = metadata.decode('ascii').split()
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError(f'Unsupported skill metadata mode: {path}')
        text = snapshot.git(repo, 'cat-file', 'blob', object_id).decode('utf-8')
        # Read only YAML delimiters, then use the shared full metadata validator.
        lines = snapshot.source_lines(text)
        if not lines or lines[0].rstrip('\r\n') != '---':
            raise ValueError(f'Missing YAML frontmatter: {path}')
        end = next((i for i in range(1, len(lines)) if lines[i].rstrip('\r\n') == '---'), None)
        if end is None:
            raise ValueError(f'Unclosed YAML frontmatter: {path}')
        try:
            metadata = snapshot.yaml.safe_load(''.join(lines[1:end]))
        except snapshot.yaml.YAMLError as error:
            raise ValueError(f'Invalid YAML frontmatter: {path}') from error
        slug = metadata.get('name') if isinstance(metadata, dict) else None
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or len(slug) > 64:
            raise ValueError(f'Invalid skill name: {path}')
        snapshot.frontmatter(text, slug)
        if slug in result:
            raise ValueError(f'Ambiguous skill name: {slug}')
        result[slug] = path.rsplit('/', 1)[0] if '/' in path else '.'
    return result


def resolve_cli_version(version=None):
    if version is None:
        result = subprocess.run(['npm', 'view', 'skills', 'version', '--json'],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError('Could not resolve the skills CLI version')
        try:
            version = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ValueError('Invalid skills CLI registry response') from error
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ValueError('The skills CLI version must be an exact semantic version')
    return version


def acquire(source, revision=None, skills=None, all_skills=False, cli_version=None):
    repository, ref = parse_source(source, revision)
    if bool(skills) == bool(all_skills):
        raise ValueError('Select skills explicitly or use --all')
    if skills and (not all(isinstance(s, str) and SLUG.fullmatch(s) for s in skills)
                   or len(skills) != len(set(skills))):
        raise ValueError('Skill names must be valid and unique')
    version = resolve_cli_version(cli_version)
    with tempfile.TemporaryDirectory(prefix='context-sql-acquire-') as temporary:
        root = Path(temporary)
        upstream = root / 'upstream.git'
        upstream.mkdir()
        snapshot.git(upstream, 'init', '--bare', '-q')
        snapshot.git(upstream, 'fetch', '--quiet', '--depth=1', repository, ref)
        commit = snapshot.git(upstream, 'rev-parse', 'FETCH_HEAD^{commit}').decode().strip()
        if not SHA.fullmatch(commit) or (SHA.fullmatch(ref) and ref != commit):
            raise ValueError('Resolved upstream commit does not match the requested revision')
        resolution = dict(requested_source=source, requested_ref=ref,
                          resolved_at=datetime.now(timezone.utc).isoformat())
        available = discover(upstream, commit)
        selected = sorted(available if all_skills else skills)
        if not selected:
            raise ValueError('No skills found')
        missing = sorted(set(selected) - available.keys())
        if missing:
            raise ValueError(f'Skills not found: {missing}')
        directories = {slug: available[slug] for slug in selected}
        inventory = snapshot.source_inventory(upstream, commit, directories)
        project = root / 'project'
        project.mkdir()
        command = snapshot.install_command(repository, commit, selected, version)
        result = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=300,
                                env={**os.environ, 'DISABLE_TELEMETRY': '1', 'CI': '1',
                                     'GIT_TERMINAL_PROMPT': '0'})
        if result.returncode:
            raise ValueError('Skills acquisition failed')
        payload = snapshot.verify_payload(project / '.agents' / 'skills', inventory)
        acquisition = snapshot.acquisition_record(payload, repository, commit, selected, version)
        return snapshot.build_bundle(payload, commit, acquisition, repository, directories), resolution
