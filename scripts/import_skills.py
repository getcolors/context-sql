#!/usr/bin/env python3
"""Snapshot tracked skill artifacts at an immutable Git commit; never execute them."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import yaml

SKILLS = ('agent-network-doks agent-network-kubernetes agent-network-single-node '
          'automq-multi-node clickhouse-replicated langfuse-multi-node n8n-single-node '
          'neon-multi-node neon-single-node posthog-single-node redis-operator-kubernetes '
          'redis-single-node refresh-oci-token rybbit-single-node').split()
IMPORTER_VERSION = '1'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def sections(text, path):
    """Non-overlapping, lossless line slices; ignore headings within fenced code."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    starts, stack, fence = [], [], None
    for i, line in enumerate(lines):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
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
    lines = section['body'].splitlines()
    result = []
    in_table = False
    for offset,line in enumerate(lines):
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


def build(repo, revision='HEAD'):
    origin=git(repo,'remote','get-url','origin').decode().strip().removesuffix('.git')
    if origin not in ('git@github.com:getcolors/skills','https://github.com/getcolors/skills','ssh://git@github.com/getcolors/skills'):
        raise ValueError('Source origin must be getcolors/skills to produce trustworthy GitHub citations')
    commit = git(repo,'rev-parse',f'{revision}^{{commit}}').decode().strip()
    entries = git(repo,'ls-tree','-r','-z',commit).decode().rstrip('\0').split('\0')
    modes = {entry.split('\t',1)[1]:entry.split(' ',1)[0] for entry in entries}
    tracked = sorted(modes)
    bundle = dict(format_version=1, importer_version=IMPORTER_VERSION,
                  repository_url='https://github.com/getcolors/skills', git_commit=commit, skills=[])
    for slug in SKILLS:
        paths = sorted(p for p in tracked if p.startswith(slug+'/'))
        if not paths or slug+'/SKILL.md' not in paths:
            raise ValueError(f'Missing required skill {slug}')
        files = []
        for full_path in paths:
            if modes[full_path] not in ('100644','100755'):
                raise ValueError(f'Unsupported Git mode {modes[full_path]}: {full_path}')
            raw = git(repo,'show',f'{commit}:{full_path}')
            path = full_path[len(slug)+1:]
            text = raw.decode('utf-8')
            record = dict(path=path,sha256=hashlib.sha256(raw).hexdigest(),
                          content_base64=base64.b64encode(raw).decode(), line_count=len(text.splitlines()),
                          sections=sections(text,path) if path.endswith('.md') else [],pins=[],evals=[])
            for section in record['sections']:
                if section['kind'] == 'pins':
                    record['pins'].extend(pin_rows(section))
            if path.startswith('evals/') and path.endswith('.json'):
                record['evals'] = json.loads(text)['evals']
            files.append(record)
        skill_text = base64.b64decode(next(f['content_base64'] for f in files if f['path']=='SKILL.md')).decode()
        frontmatter = yaml.safe_load(skill_text.split('---',2)[1])
        description=frontmatter.get('description')
        if not isinstance(description,str) or not 1 <= len(description) <= 1024:
            raise ValueError(f'Invalid routing description for {slug}')
        if frontmatter['name'] != slug:
            raise ValueError(f'Frontmatter name differs for {slug}')
        version_id = hashlib.sha256((commit+'\n'+slug+'\n'+IMPORTER_VERSION+'\n'+
                      '\n'.join(f["path"]+':'+f['sha256'] for f in files)).encode()).hexdigest()
        bundle['skills'].append(dict(slug=slug,kind='generic' if slug=='refresh-oci-token' else 'context',
                                     version_id=version_id,routing_description=frontmatter['description'],files=files))
    return bundle


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
    for skill in bundle['skills']:
        slug,version=skill['slug'],skill['version_id']
        key=dict(skill_slug=slug,version_id=version)
        insert('catalog.skill',dict(slug=slug,kind=skill['kind']))
        insert('catalog.skill_version',dict(**key,repository_url=bundle['repository_url'],
               git_commit=bundle['git_commit'],source_directory=slug,
               routing_description=skill['routing_description'],importer_version=IMPORTER_VERSION))
        for file in skill['files']:
            fkey=dict(**key,path=file['path'])
            insert('catalog.source_file',dict(**fkey,content=SQL('decode('+lit(file['content_base64'])+", 'base64')"),
                   sha256=file['sha256'],line_count=file['line_count']))
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path('../skills'))
    parser.add_argument('--revision',default='HEAD',help='Git commit/ref to snapshot; worktree edits are ignored')
    parser.add_argument('--output',type=Path,default=Path('data'))
    parser.add_argument('--check',action='store_true',help='Fail if committed data differs from deterministic snapshot')
    args=parser.parse_args()
    bundle=build(args.source,args.revision)
    artifacts={'skills.json':json.dumps(bundle,ensure_ascii=False,indent=2)+'\n','skills.sql':sql(bundle)}
    if args.check:
        for name,content in artifacts.items():
            if (args.output/name).read_text() != content:
                raise SystemExit(f'Deterministic snapshot differs: {name}')
    else:
        args.output.mkdir(parents=True,exist_ok=True)
        for name,content in artifacts.items():
            (args.output/name).write_text(content)
    counts=dict(skills=len(bundle['skills']),files=sum(len(s['files']) for s in bundle['skills']))
    for field in ('sections','pins','evals'):
        counts[field]=sum(len(f[field]) for s in bundle['skills'] for f in s['files'])
    print(json.dumps(dict(commit=bundle['git_commit'],**counts),indent=2))


if __name__=='__main__':
    main()
