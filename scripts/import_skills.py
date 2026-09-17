#!/usr/bin/env python3
"""Acquire skills with a pinned CLI, verify upstream bytes, and emit a database seed."""
import argparse
import base64
import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tempfile
import yaml

SKILLS = ('agent-network-doks agent-network-kubernetes agent-network-single-node '
          'automq-multi-node clickhouse-replicated langfuse-multi-node n8n-single-node '
          'neon-multi-node neon-single-node posthog-single-node redis-operator-kubernetes '
          'redis-single-node refresh-oci-token rybbit-single-node').split()
IMPORTER_VERSION = '2'
SOURCE_URL = 'https://github.com/getcolors/skills'
SKILLS_CLI_VERSION = '1.6.0'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], timeout=120,
                                   env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})


def source_lines(text):
    # Source citations use LF boundaries, not Unicode paragraph separators.
    lines = text.split('\n')
    return [line + '\n' for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def fence_line(line, fence):
    marker = re.match(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)[\r\n]*$', line)
    if not marker:
        return fence, False
    token, suffix = marker.groups()
    if fence is None:
        if token[0] == '`' and '`' in suffix:
            return None, False
        return token, True
    if token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
        return None, True
    return fence, True


def sections(text, path):
    """Non-overlapping, lossless line slices; ignore headings within fenced code."""
    lines = source_lines(text)
    if not lines:
        return []
    starts, stack, fence = [], [], None
    for i, line in enumerate(lines):
        fence, is_fence = fence_line(line, fence)
        if is_fence:
            continue
        match = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line) if fence is None else None
        if match:
            depth, title = len(match[1]), match[2]
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, title))
            starts.append((i, title, [s[1] for s in stack]))
    if not starts or starts[0][0] != 0:
        starts.insert(0, (0, 'Preamble', []))
    kind = ('routing' if path == 'SKILL.md' else
            'failure' if 'failure' in path else 'pins' if 'pins' in path else
            'acceptance' if 'acceptance' in path else
            'contract' if any(x in path for x in ('contract', '/api.')) else 'prose')
    return [dict(ordinal=n, kind=kind, heading=title, heading_path=ancestors,
                 start_line=start+1, end_line=(starts[n+1][0] if n+1<len(starts) else len(lines)),
                 body=''.join(lines[start:starts[n+1][0] if n+1<len(starts) else len(lines)]))
            for n, (start,title,ancestors) in enumerate(starts)]


def pin_rows(section):
    """Keep cells verbatim; interpretation and version comparison need review."""
    lines = source_lines(section['body'])
    result = []
    in_table = False
    fence = None
    for offset,line in enumerate(lines):
        fence, is_fence = fence_line(line, fence)
        if is_fence or fence is not None:
            in_table = False
            continue
        if not line.strip().startswith('|'):
            in_table = False
            continue
        cells = [cell.strip() for cell in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
        if all(re.fullmatch(r':?-{2,}:?', cell) for cell in cells):
            in_table = True
            continue
        if in_table and len(cells) >= 2:
            result.append(dict(section_ordinal=section['ordinal'],
                               line_number=section['start_line']+offset,
                               component=cells[0], value_text=' | '.join(cells[1:]), cells=cells))
    return result


def source_inventory(repo, commit):
    """Read the authoritative inventory without checking out or executing source files."""
    inventory = {}
    entries = {}
    for entry in git(repo, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        path = name.decode('utf-8')
        parts = path.split('/')
        if (PurePosixPath(path).is_absolute() or any(p in ('', '.', '..') for p in parts)
                or '\\' in path or any(ord(c) < 32 for c in path)):
            raise ValueError(f'Unsafe source path: {path!r}')
        mode, kind, object_id = metadata.decode('ascii').split()
        entries[path] = (mode, kind, object_id)
    for path, (mode, kind, object_id) in entries.items():
        selected = path.split('/', 1)[0] in SKILLS
        # Discovery may read unselected metadata. Permit only direct links to
        # regular tracked files inside the repository, such as CLAUDE.md -> AGENTS.md.
        if mode == '120000' and not selected:
            target = git(repo, 'cat-file', 'blob', object_id).decode('utf-8')
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            target_mode = entries.get(resolved, (None, None, None))[0]
            if (not target.startswith('/') and '\\' not in target
                    and resolved != '..' and not resolved.startswith('../')
                    and target_mode in ('100644', '100755')):
                continue
            raise ValueError(f'Unsafe unselected source symlink: {path}')
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError(f'Unsupported Git mode {mode}: {path}')
        # The installer discovers unselected skills too, so validate the whole tree.
        if not selected:
            continue
        inventory[path] = dict(raw=git(repo, 'cat-file', 'blob', object_id), mode=mode)
    for slug in SKILLS:
        if f'{slug}/SKILL.md' not in inventory:
            raise ValueError(f'Missing required skill {slug}')
    return inventory


def verify_payload(payload_root, inventory):
    """Return installed bytes only after exact inventory and content verification."""
    payload_root = Path(payload_root)
    if payload_root.is_symlink() or not payload_root.is_dir():
        raise ValueError('Missing or unsafe installed skill directory')
    installed = {}
    for directory, dirs, files in os.walk(payload_root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f'Installed symlink is not allowed: {path.relative_to(payload_root)}')
        for name in files:
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError(f'Unsupported installed file: {path.relative_to(payload_root)}')
            installed[path.relative_to(payload_root).as_posix()] = path.read_bytes()
    missing = sorted(inventory.keys() - installed.keys())
    extra = sorted(installed.keys() - inventory.keys())
    if missing or extra:
        raise ValueError(f'Installed inventory differs: missing={missing}, extra={extra}')
    payload = {}
    for path in sorted(inventory):
        raw = installed[path]
        if raw != inventory[path]['raw']:
            raise ValueError(f'Installed content differs from upstream: {path}')
        payload[path] = dict(raw=raw, mode=inventory[path]['mode'])
    return payload


def install_command(source, commit):
    return ['npx', '--yes', f'skills@{SKILLS_CLI_VERSION}', 'add',
            f'{source}/tree/{commit}', '--skill', *SKILLS,
            '--agent', 'codex', '--copy', '--yes']


def acquisition_record(payload, source, commit):
    manifest = [dict(path=path, mode=file['mode'], sha256=hashlib.sha256(file['raw']).hexdigest())
                for path, file in sorted(payload.items())]
    acquisition = dict(repository_url=source, git_commit=commit,
                       skills_cli_version=SKILLS_CLI_VERSION,
                       command=install_command(source, commit),
                       acquired_at=datetime.now(timezone.utc).isoformat(),
                       manifest_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                       verification_method='git-tree-and-byte-comparison-v1')
    acquisition['acquisition_id'] = hashlib.sha256(
        json.dumps(acquisition, sort_keys=True).encode()).hexdigest()
    return acquisition


def frontmatter(text, slug):
    lines = source_lines(text)
    if not lines or lines[0].rstrip('\r\n') != '---':
        raise ValueError(f'Missing YAML frontmatter for {slug}')
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip('\r\n') == '---'), None)
    if end is None:
        raise ValueError(f'Unclosed YAML frontmatter for {slug}')
    try:
        metadata = yaml.safe_load(''.join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError(f'Invalid YAML frontmatter for {slug}: {error}') from error
    if not isinstance(metadata, dict):
        raise ValueError(f'Frontmatter must be a mapping for {slug}')
    description = metadata.get('description')
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise ValueError(f'Invalid routing description for {slug}')
    if metadata.get('name') != slug:
        raise ValueError(f'Frontmatter name differs for {slug}')
    return metadata


def build_bundle(payload, commit, acquisition):
    bundle = dict(format_version=2, importer_version=IMPORTER_VERSION,
                  repository_url=SOURCE_URL, git_commit=commit, acquisition=acquisition, skills=[])
    for slug in SKILLS:
        files = []
        for full_path in sorted(p for p in payload if p.startswith(slug+'/')):
            raw = payload[full_path]['raw']
            path = full_path[len(slug)+1:]
            # Every file is preserved, even if it has no searchable text projection.
            text = None
            if path.endswith('.md') or (path.startswith('evals/') and path.endswith('.json')):
                try:
                    text = raw.decode('utf-8')
                except UnicodeDecodeError as error:
                    raise ValueError(f'Invalid UTF-8 in projected source: {full_path}') from error
                if '\0' in text:
                    raise ValueError(f'NUL in projected source: {full_path}')
            record = dict(path=path,sha256=hashlib.sha256(raw).hexdigest(),
                          content_base64=base64.b64encode(raw).decode(),
                          line_count=raw.count(b'\n') + int(bool(raw) and not raw.endswith(b'\n')),
                          git_mode=payload[full_path]['mode'],
                          sections=sections(text,path) if path.endswith('.md') else [],pins=[],evals=[])
            for section in record['sections']:
                if section['kind'] == 'pins':
                    record['pins'].extend(pin_rows(section))
            if path.startswith('evals/') and path.endswith('.json'):
                record['evals'] = json.loads(text)['evals']
            files.append(record)
        skill_text = base64.b64decode(next(f['content_base64'] for f in files if f['path']=='SKILL.md')).decode()
        metadata = frontmatter(skill_text, slug)
        version_id = hashlib.sha256((commit+'\n'+slug+'\n'+IMPORTER_VERSION+'\n'+
                      '\n'.join(f["path"]+':'+f['git_mode']+':'+f['sha256'] for f in files)).encode()).hexdigest()
        bundle['skills'].append(dict(slug=slug,kind='generic' if slug=='refresh-oci-token' else 'context',
                                     version_id=version_id,routing_description=metadata['description'],files=files))
    return bundle


def build(source=SOURCE_URL, revision=None):
    if source != SOURCE_URL:
        raise ValueError(f'Source must be {SOURCE_URL}; local checkouts are not acquisition sources')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Revision must be a full 40-character upstream commit SHA')
    with tempfile.TemporaryDirectory(prefix='context-sql-acquire-') as temporary:
        root = Path(temporary)
        upstream = root / 'upstream.git'
        upstream.mkdir()
        git(upstream, 'init', '--bare', '-q')
        git(upstream, 'fetch', '--quiet', '--depth=1', source, revision)
        commit = git(upstream, 'rev-parse', 'FETCH_HEAD^{commit}').decode().strip()
        if commit != revision:
            raise ValueError(f'Upstream resolved to {commit}, expected {revision}')
        inventory = source_inventory(upstream, commit)
        project = root / 'project'
        project.mkdir()
        command = install_command(source, commit)
        result = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=300,
                                env={**os.environ, 'DISABLE_TELEMETRY': '1', 'CI': '1',
                                     'GIT_TERMINAL_PROMPT': '0'})
        if result.returncode:
            raise ValueError(f'Skills acquisition failed: {result.stdout}\n{result.stderr}')
        payload = verify_payload(project / '.agents' / 'skills', inventory)
        return build_bundle(payload, commit, acquisition_record(payload, source, commit))


def lit(value):
    if value is None: return 'NULL'
    if isinstance(value,int): return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def sql(bundle):
    out = ['-- Generated by scripts/import_skills.py. Source claims are not independently verified.',
           'BEGIN;', 'SET LOCAL standard_conforming_strings = on;']
    def insert(table, values):
        out.append('INSERT INTO '+table+' ('+', '.join(values)+') VALUES ('+
                   ', '.join(v if isinstance(v,SQL) else lit(v) for v in values.values())+
                   ') ON CONFLICT DO NOTHING;')
    acquisition = dict(bundle['acquisition'])
    acquisition['command'] = json.dumps(acquisition['command'], ensure_ascii=False)
    insert('catalog.acquisition', acquisition)
    for skill in bundle['skills']:
        slug,version=skill['slug'],skill['version_id']
        key=dict(skill_slug=slug,version_id=version)
        insert('catalog.skill',dict(slug=slug,kind=skill['kind']))
        insert('catalog.skill_version',dict(**key,repository_url=bundle['repository_url'],
               git_commit=bundle['git_commit'],source_directory=slug,
               routing_description=skill['routing_description'],importer_version=IMPORTER_VERSION))
        insert('catalog.skill_acquisition', dict(acquisition_id=acquisition['acquisition_id'], **key))
        for file in skill['files']:
            fkey=dict(**key,path=file['path'])
            insert('catalog.source_file',dict(**fkey,content=SQL('decode('+lit(file['content_base64'])+", 'base64')"),
                   sha256=file['sha256'],line_count=file['line_count'],git_mode=file['git_mode']))
            for section in file['sections']:
                values=dict(**fkey,**section)
                values['heading_path']=SQL('ARRAY['+','.join(map(lit,values['heading_path']))+']::text[]')
                insert('catalog.section',values)
            for pin in file['pins']:
                values=dict(**fkey,**pin); values['cells']=json.dumps(values['cells'],ensure_ascii=False)
                insert('catalog.pin_row',values)
            for n,evaluation in enumerate(file['evals']):
                insert('catalog.eval_case',dict(**fkey,ordinal=n,source_id=str(evaluation['id']) if 'id' in evaluation else None,
                       name=evaluation.get('name'),prompt=evaluation['prompt'],expected_output=evaluation.get('expected_output',evaluation.get('expected_behaviour',evaluation.get('expected_behavior'))),
                       assertions=json.dumps(evaluation.get('assertions',[]),ensure_ascii=False),
                       raw_case=json.dumps(evaluation,ensure_ascii=False)))
        out.append('UPDATE catalog.skill SET current_version='+lit(version)+' WHERE slug='+lit(slug)+';')
    out.append('COMMIT;')
    return '\n'.join(out)+'\n'


class SQL(str):
    pass


def check_artifacts(bundle, output):
    """Reacquire content without replacing the recorded acquisition's timestamp."""
    recorded = json.loads((output / 'skills.json').read_text(encoding='utf-8'))
    provenance = dict(recorded['acquisition'])
    acquisition_id = provenance.pop('acquisition_id')
    if acquisition_id != hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest():
        raise ValueError('Recorded acquisition digest differs')

    def logical(value):
        result = dict(value)
        result['acquisition'] = {key: val for key, val in value['acquisition'].items()
                                 if key not in ('acquisition_id', 'acquired_at')}
        return result

    if logical(bundle) != logical(recorded):
        raise ValueError('Verified snapshot differs: skills.json')
    if (output / 'skills.sql').read_bytes() != sql(recorded).encode('utf-8'):
        raise ValueError('Deterministic snapshot differs: skills.sql')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default=SOURCE_URL,help='Upstream repository URL')
    parser.add_argument('--revision',required=True,help='Full upstream commit SHA to acquire and verify')
    parser.add_argument('--output',type=Path,default=Path('data'))
    parser.add_argument('--check',action='store_true',help='Reacquire and verify recorded content and SQL without rewriting provenance')
    args=parser.parse_args()
    try:
        bundle=build(args.source,args.revision)
        if args.check:
            check_artifacts(bundle, args.output)
        else:
            artifacts={'skills.json':json.dumps(bundle,ensure_ascii=False,indent=2)+'\n','skills.sql':sql(bundle)}
            args.output.mkdir(parents=True,exist_ok=True)
            for name,content in artifacts.items():
                (args.output/name).write_bytes(content.encode('utf-8'))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Import failed: {error}\n')
    counts=dict(skills=len(bundle['skills']),files=sum(len(s['files']) for s in bundle['skills']))
    for field in ('sections','pins','evals'):
        counts[field]=sum(len(f[field]) for s in bundle['skills'] for f in s['files'])
    print(json.dumps(dict(commit=bundle['git_commit'],**counts),indent=2))


if __name__=='__main__':
    main()
