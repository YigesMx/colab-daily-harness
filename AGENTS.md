# Colab Daily — production workflow

This is the standalone SQLite/VitePress project. Resolve all paths from the current project root and use the project-local skills. The old project and legacy migration material are read-only/private history; Grist, candidate preview and human-gate contracts are not active.

## Daily entry

Run the production agent with `colab_daily.prompt`. It must read this file, `colab_daily/RUNTIME.md`, `colab_daily/storage/README.md`, and the active phase/source/rating/refine/publication skills before acting.

The lifecycle is one phase followed by a terminal state:

1. `colab_daily.lifecycle begin` creates/resumes a SQLite-owned `PHASE_release` cycle and claims the single `working_tmp/` workspace.
2. Dynamically discover every `.agents/skills/sources/*/SKILL.md`; use the frozen previous complete Asia/Shanghai day window.
3. Run quality-neutral inventory/canonicalization/routing, then three isolated semantic rating contexts (`paper`, `news`, `policy`) with fixed quotas.
4. Run exactly three isolated semantic refine contexts. Do not replace scoring, full-text reading, evidence judgment or writing with simple rules.
5. `colab_daily.publication complete-refine` records each authentic context completion; `freeze` validates the unique assembly and persists only final mapped publication/assets in SQLite.
6. `colab_daily.publication deploy` renders the formal VitePress site, builds, makes a scoped commit, non-force pushes with the dedicated key and verifies the exact public artifacts. Only then is the cycle `Released`.
7. Optional `colab_daily.publication notify` sends the Released report. Notification failure/unknown never reverses release or repeats deployment.
8. `colab_daily.lifecycle cleanup` deletes only the released owner's `working_tmp` tree.

## Authority and safety

- `working_tmp/` contains all transient source/PDF/image/inventory/rating/refine/assembly documents. Retain it on failure.
- SQLite contains bounded ownership/stage/source/delivery facts and final publication/assets only—never source snapshots, full intermediate manifests, base64, or replay bundles.
- `state/storage/`, `state/backups/`, `.local/site`, migration archives, keys, `.env`, JSONL and failure material are private and Git-ignored.
- Do not emit secrets, private IDs/URLs, authorization headers, key material or webhook values.
- Production deploy uses only the configured dedicated SSH key, disables SSH agent/personal auth, stages an exact managed allowlist, never force-pushes, and reconciles unknown effects before retry.
- No source crawl, deploy, notification, scheduler mutation, root Git init/commit/push, or old-project write is allowed during maintenance/testing. Mock external effects.
- External scheduling is optional. Configure its cwd to this project and prompt to `colab_daily.prompt`; do not modify scheduler state implicitly.
