"""Portable catalog lock documents; acquisition timestamps stay in PostgreSQL."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .acquisition import parse_source, snapshot, SEMVER

FORMAT = 1
MAX_LOCK_BYTES = 16 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def normalized_skills(bundle):
    return sorted([{**skill, 'source_directory': skill.get('source_directory', skill['slug'])}
                   for skill in bundle['skills']], key=lambda skill: skill['slug'])


def entry_from_bundle(bundle, resolution, all_skills=False):
    records = normalized_skills(bundle)
    manifest = [dict(path=skill['slug'] + '/' + file['path'],
                     mode=file['git_mode'], sha256=file['sha256'])
                for skill in records for file in sorted(skill['files'], key=lambda file: file['path'])]
    return {
        'repository': bundle['repository_url'],
        'requestedSource': resolution['requested_source'],
        'requestedRef': resolution['requested_ref'],
        'commit': bundle['git_commit'],
        'parserVersion': bundle['importer_version'],
        'selection': {'all': all_skills},
        'skills': [dict(name=s['slug'], sourceDirectory=s['source_directory'], versionId=s['version_id'])
                   for s in records],
        'manifest': manifest,
        'manifestSha256': bundle['acquisition']['manifest_sha256'],
        'projectionSha256': digest(records),
        'acquisitionCliVersion': bundle['acquisition']['skills_cli_version'],
    }


def document(entries):
    result = {'format': FORMAT, 'imports': sorted(entries, key=lambda entry: entry['repository'])}
    validate(result)
    return result


def safe_path(value, root_allowed=False):
    return (isinstance(value, str) and bool(value) and
            (value == '.' and root_allowed or
             not PurePosixPath(value).is_absolute() and
             all(part not in ('', '.', '..') for part in value.split('/')) and
             '\\' not in value and all(ord(c) >= 32 for c in value)))


def hash_string(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def validate(lock):
    if not isinstance(lock, dict) or set(lock) != {'format', 'imports'} or type(lock['format']) is not int or lock['format'] != FORMAT:
        raise ValueError('Unsupported context-skills lock format')
    if not isinstance(lock['imports'], list) or not lock['imports']:
        raise ValueError('Lock imports must be a nonempty list')
    repositories, names = set(), set()
    required = {'repository', 'requestedSource', 'requestedRef', 'commit', 'parserVersion',
                'selection', 'skills', 'manifest', 'manifestSha256', 'projectionSha256', 'acquisitionCliVersion'}
    for entry in lock['imports']:
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError('Invalid lock import fields')
        repository, _ = parse_source(entry['repository'])
        if repository != entry['repository'] or repository in repositories:
            raise ValueError('Lock repositories must be canonical and unique')
        repositories.add(repository)
        requested_repository, _ = parse_source(entry['requestedSource'], entry['requestedRef'])
        if requested_repository != repository:
            raise ValueError('Lock requested source disagrees with repository')
        if not isinstance(entry['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', entry['commit']):
            raise ValueError('Lock commits must be full upstream SHAs')
        if entry['parserVersion'] != snapshot.IMPORTER_VERSION:
            raise ValueError('Lock parser version is unsupported; use its matching importer release')
        if not isinstance(entry['acquisitionCliVersion'], str) or not SEMVER.fullmatch(entry['acquisitionCliVersion']):
            raise ValueError('Invalid recorded acquisition CLI version')
        if (not isinstance(entry['selection'], dict) or set(entry['selection']) != {'all'} or
                type(entry['selection']['all']) is not bool):
            raise ValueError('Invalid lock selection')
        if not isinstance(entry['skills'], list) or not entry['skills']:
            raise ValueError('Lock must select skills')
        selected = set()
        for skill in entry['skills']:
            if not isinstance(skill, dict) or set(skill) != {'name', 'sourceDirectory', 'versionId'}:
                raise ValueError('Invalid locked skill')
            slug = skill['name']
            if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9]+(-[a-z0-9]+)*', slug) or slug in names:
                raise ValueError('Locked skill names must be valid and globally unique')
            names.add(slug)
            selected.add(slug)
            if not safe_path(skill['sourceDirectory'], root_allowed=True) or not hash_string(skill['versionId']):
                raise ValueError('Invalid locked skill directory or version')
        if not isinstance(entry['manifest'], list) or not entry['manifest']:
            raise ValueError('Missing lock file manifest')
        paths = set()
        for item in entry['manifest']:
            if not isinstance(item, dict) or set(item) != {'path', 'mode', 'sha256'}:
                raise ValueError('Invalid locked file')
            path = item['path']
            if not safe_path(path) or path.split('/')[0] not in selected or '/' not in path or path in paths:
                raise ValueError('Invalid or duplicate locked file path')
            paths.add(path)
            if item['mode'] not in ('100644', '100755') or not hash_string(item['sha256']):
                raise ValueError('Invalid locked file mode or digest')
        if any(slug + '/SKILL.md' not in paths for slug in selected):
            raise ValueError('Locked skill lacks SKILL.md')
        # The original acquisition manifest uses this serialization recipe.
        actual = hashlib.sha256(json.dumps(entry['manifest'], sort_keys=True).encode()).hexdigest()
        if actual != entry['manifestSha256'] or not hash_string(entry['projectionSha256']):
            raise ValueError('Lock manifest digest differs or projection digest is invalid')
    return lock


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOCK_BYTES:
        raise ValueError('Lock must be a regular file of at most 16 MiB, not a symlink')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return validate(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_pairs))


def verify_entry(entry, bundle):
    candidate = entry_from_bundle(bundle, {
        'requested_source': entry['requestedSource'], 'requested_ref': entry['requestedRef'],
    }, entry['selection']['all'])
    # Acquisition tooling may change; the verified payload and projections may not.
    candidate['acquisitionCliVersion'] = entry['acquisitionCliVersion']
    if candidate != entry:
        raise ValueError(f'Locked content or projections differ for {entry["repository"]}')
