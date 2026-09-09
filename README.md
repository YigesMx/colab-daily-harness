# Colab Daily

A production agent workflow that discovers daily AI/robotics sources, performs three isolated semantic rating/refinement tracks, stores final publications in SQLite, and formally publishes a VitePress site.

## Setup

Requirements: Python 3.10, `uv`, Node/npm for the site checkout, and private runtime configuration.

```sh
uv sync --locked
cp .env.example .env              # fill private local values; keep mode 0600
uv run --locked python -m colab_daily.storage init
uv run --locked python -m colab_daily.publication preflight
```

Paths are resolved from the project root, not the invoking cwd. Defaults are `state/storage`, `state/backups`, `working_tmp`, and `.local/site`.

## Production entry

Run an agent with [`colab_daily.prompt`](colab_daily.prompt). The authoritative orchestration is [`.agents/skills/phase-release/SKILL.md`](.agents/skills/phase-release/SKILL.md); the historical name is retained for skill discovery, but the active phase is `PHASE_release` and there is no human gate.

Useful deterministic commands:

```sh
uv run --locked python -m colab_daily.lifecycle --help
uv run --locked python -m colab_daily.publication --help
uv run --locked python -m colab_daily.storage --help

# Formal publication after semantic assembly has been validated/frozen:
uv run --locked python -m colab_daily.publication deploy --cycle daily-YYYY-MM-DD

# Optional, only after Released:
uv run --locked python -m colab_daily.publication notify --cycle daily-YYYY-MM-DD

# Remove only the Released owner's transient workspace:
uv run --locked python -m colab_daily.lifecycle cleanup --owner <OWNER> --cycle daily-YYYY-MM-DD
```

`working_tmp/` is the only transient document boundary. SQLite stores bounded control/source/delivery facts and final publication/assets, never crawler snapshots, PDFs, base64, or complete intermediate manifests. Failed workspace material is retained; successful Released cleanup deletes only the owning workspace.

## Backup and restore

Create backups with the storage CLI into a new directory under the configured backup root:

```sh
uv run --locked python -m colab_daily.storage backup --output state/backups/backup-YYYYMMDD
uv run --locked python -m colab_daily.storage restore --input state/backups/backup-YYYYMMDD --output state/verification/restored-storage
uv run --locked python -m colab_daily.storage status
```

See [`colab_daily/storage/README.md`](colab_daily/storage/README.md) for exact migration, verification, authority, and restore contracts; see [`colab_daily/RUNTIME.md`](colab_daily/RUNTIME.md) for owner/source/recovery behavior.

## Deployment and notification safety

The formal site checkout is separate and ignored. Deployment stages only managed public files, uses a dedicated SSH key with agent/personal credentials disabled, never force-pushes, and requires exact remote plus public artifact verification before `Released`. Notification is optional and post-Release: unknown POST results are recorded in SQLite and are not automatically retried; notification cannot trigger another deployment or undo release.

An external scheduler may invoke the production prompt (and optionally the notification CLI) with this repository as cwd. Scheduler registration/configuration is intentionally outside this repository and is never changed implicitly.

## Offline checks

```sh
uv run --offline --locked python -m unittest discover -s colab_daily/storage/tests -v
uv run --offline --locked python -m unittest discover -s colab_daily/publication/tests -v
uv run --offline --locked python -m unittest discover -s .agents/skills/rating-filter-organize/tests -v
uv run --offline --locked python scripts/public_tree_scan.py .
npm --prefix site_template run build
```

Runtime DB/assets/backups, deployment checkout, workspace, credentials, keys, JSONL, logs and migration archives must never be committed. Third-party package licenses remain in their original upstream/package locations.
