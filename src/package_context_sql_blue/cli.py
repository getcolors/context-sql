"""Command line for the local SQL context Package Skill."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from blue.cli import find_up, load_yaml, read_pars, par_name
from blue.workflow import run as run_workflow
from .workflow import DEFAULTS, KEYS, validation_errors, context_workflow

EVENTS = ('build', 'create', 'status', 'start', 'stop', 'backup')
USAGE = 'blue <build|create|status|start|stop|backup> [-f colors.yml] [--dry-run]'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


async def run(*args):
    if args in (('--help',), ('-h',), ('help',)):
        return {'blue/exit': 0, 'blue/err': USAGE}
    parser = Parser(usage=USAGE, add_help=False)
    parser.add_argument('event', choices=EVENTS)
    parser.add_argument('-f', '--file')
    parser.add_argument('--dry-run', action='store_true')
    try:
        parsed = parser.parse_args(args)
        source = Path(parsed.file or find_up('colors.yml') or 'colors.yml').absolute()
        values = load_yaml(source.read_text())
        if not isinstance(values, dict) or any(not isinstance(k, str) for k in values):
            raise ValueError('colors.yml must be a mapping of string keys')
        # Other packages share COLORS_PAR_ and may export cloud credentials.
        # Do not copy unrelated settings or secrets into this local workflow.
        allowed = {par_name(key) for key in KEYS}
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        values = read_pars({**DEFAULTS, **values}, environment)
        errors = validation_errors(values)
        if errors:
            return {'blue/exit': 2, 'blue/err': '\n'.join(errors)}
        return await run_workflow(context_workflow, {
            **values, 'blue/event': parsed.event, 'blue/state-file': str(source),
            'blue/dry-run': parsed.dry_run,
        })
    except (ValueError, OSError) as error:
        return {'blue/exit': 2, 'blue/err': str(error)}
    except Exception as error:
        # YAML diagnostics can include source values. Do not echo them.
        return {'blue/exit': 2, 'blue/err': f'Invalid configuration: {type(error).__name__}'}


def main():
    result = asyncio.run(run(*sys.argv[1:]))
    if result.get('blue/err'):
        print(result['blue/err'], file=sys.stderr if result.get('blue/exit') else sys.stdout)
    if result.get('context-sql/result'):
        print(json.dumps(result['context-sql/result'], sort_keys=True))
    raise SystemExit(result.get('blue/exit') or 0)


if __name__ == '__main__':
    main()
