"""Production publication commands; preflight/sync-template never use network."""
import argparse
from ..config import Config
from ..storage import StorageError
from . import freeze, complete_refine, deploy, notify, preflight, sync_template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('preflight')
    subs.add_parser('sync-template')
    p = subs.add_parser('freeze')
    p.add_argument('--owner', required=True)
    p.add_argument('--assembly', required=True)
    p = subs.add_parser('complete-refine')
    p.add_argument('--owner', required=True, help='workspace owner; authentic child context is read from SQLite')
    p.add_argument('--run', required=True)
    p.add_argument('--manifest', required=True)
    p = subs.add_parser('deploy', help='PRODUCTION side effects: build, scoped commit, nonforce push and public readback')
    p.add_argument('--cycle', required=True)
    p = subs.add_parser('notify', help='OPTIONAL post-Released Feishu POST with unknown-result blocking')
    p.add_argument('--cycle', required=True)
    args = parser.parse_args()
    config = Config.load(args.project_root)
    try:
        if args.command == 'preflight': preflight(config)
        elif args.command == 'sync-template': sync_template(config)
        elif args.command == 'freeze': freeze(config, args.owner, args.assembly)
        elif args.command == 'complete-refine': complete_refine(config, args.owner, args.run, args.manifest)
        elif args.command == 'deploy': deploy(config, args.cycle)
        elif args.command == 'notify': notify(config, args.cycle)
    except (StorageError, OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'Publication command failed closed; preserve state/workspace and inspect private inputs.\n')
    print('Publication command complete')


if __name__ == '__main__':
    main()
