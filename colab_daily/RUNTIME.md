# Runtime ownership and temporary-document boundary

This module layers lifecycle/source adapters over SQLite storage. It does not run
agents or perform semantic scoring/refinement. Formal freeze/deployment/notification
adapters live in `colab_daily.publication`; quality, three-track isolation and quota
validators remain in force.

## Persistence contract — supersedes earlier document-replay plans

SQLite is authoritative for the frozen cycle/date/window, owner/run/context IDs,
stage status and CAS operation identity, source cursor/event identities, delivery
state and final publication data. **It is not a backup of `working_tmp`.**

* Source documents/downloads, inventory/discovery/canonical/routing documents,
  semantic scoring/triage/refinement results and temporary binary files remain
  under the single `working_tmp/` tree.
* Run contexts and stage/source receipts contain bounded identifiers, relative
  paths, hashes, counts and machine status/reason metadata. They never contain
  full text, file snapshots, base64, whole source-state copies or replay bundles.
* Final publication/business records, article content and final image assets use
  the durable publication/blob APIs. Historical imported rows/archives remain
  untouched; their original fields are not copied into new operation requests.
* Successful cached commands verify their temporary files before reusing them.
  Missing/changed files fail explicitly; SQLite cannot recreate those documents.
  A failed/running source attempt can be rerun while preserving its old temporary
  directory. A completed source/inventory/rating checkpoint cannot be reopened
  silently. Recover the original temporary material or stop for controlled repair.

Control metadata is a JSON object, canonical UTF-8 size at most 16 KiB. Allowed
namespaces are `identity`, `paths`, `counts`, `checksums`, `context_id`, `session_id`,
`status` and `reason`. Identity values are bounded scalars, counts nonnegative
integers, checksums lowercase SHA-256 strings; paths are relative names (maximum
64). This is a metadata envelope, not a semantic-output schema. For example:

```json
{
  "context_id": "paper-context",
  "session_id": "session",
  "identity": {"cycle_id": "daily-2026-01-02", "attempt": 1},
  "paths": ["rating_filter_organize/runs/example/tracks/paper/input.json"],
  "counts": {"records": 10},
  "status": "running"
}
```

Do not pass an agent transcript, inventory, scoring result or full configuration
as `--context` or checkpoint `--input`. Those flags take the small metadata
object above; place the actual intermediate document in `working_tmp` separately.
New cursor updates likewise accept bounded scalar metadata, not nested documents.
New source events accept only the identity/time/category fields defined in
`storage/metadata.py`; candidate titles, bodies and summaries are rejected.

Operation history retains small CAS receipts for safe retries, not repeated
whole-state/file snapshots. Unmentioned source identities are retained for dedupe;
this milestone does not introduce automatic historical pruning. Local failed
attempts are retained until the owner-authorized Released cleanup. Storage backups
include control state and final/legacy assets, **not temporary documents**.

## Frozen daily cycle and single workspace

`Lifecycle.begin()` computes the Shanghai display date once. The source window is
the preceding complete natural day, half-open `[since, until)`. Manual runs require
an explicit display date; scheduled runs derive it from the initial clock. An
identical retry reloads the stored date/window even across midnight. The cycle ID
is `daily-<display_date>`. Crawlers cannot substitute a rolling `--days` window.

Schema 3 adds `workspace_claims`: immutable logical owner/context, cycle, workspace
path and `active`/`cleaning`/`closed` status. A partial unique index allows only one
nonclosed claim globally. A stable `state/workspace.lock` coordinates filesystem
writers; SQLite remains the ownership authority. Neither age nor a dead PID allows
another owner to take over. Run kinds/IDs and contexts are immutable too.

Paths are anchored at `Config.load().project_root`, not the caller's CWD. Workspace
and storage path checks reject traversal/symlinks. Do not allow untrusted processes
to mutate the private directory's ancestors concurrently.

```sh
uv run --locked python -m colab_daily.lifecycle begin --trigger manual --display-date 2026-01-02 --owner example-owner --context state/input/owner-metadata.json --output working_tmp/input/cycle.json
uv run --locked python -m colab_daily.lifecycle status --owner example-owner --output working_tmp/input/lifecycle.json
uv run --locked python -m colab_daily.lifecycle claim-run --owner example-owner --run example-run --kind refine:paper --run-owner paper-context --context working_tmp/input/refine-metadata.json
uv run --locked python -m colab_daily.lifecycle checkpoint --owner example-owner --run example-run --stage refine --operation refine-1 --revision 0 --status running --input working_tmp/input/refine-metadata.json
uv run --locked python -m colab_daily.lifecycle run-state --owner example-owner --run example-run --output working_tmp/input/run-state.json
```

Prepare initial owner metadata at `state/input/owner-metadata.json` before `begin`;
an unclaimed nonempty workspace is rejected. Once claimed, all intermediate cycle
material belongs under `working_tmp`. Retry uses the same owner.

Cleanup requires the current owner, verified durable final publication/assets and
formal `Released` status. It records `cleaning` before deletion and resumes an
interrupted delete. It removes only this owner's `working_tmp`, never `state`, the
lock, deployment checkout or a later cycle. Migrated provenance is not Released.
The old `finalize_cleanup.py` JSON-journal authorization has been replaced with a
lifecycle facade; local JSON cannot authorize cleanup.

## Source, inventory and rating adapters

Company/info/policy/HuggingFace crawler CLI mains require `--owner`, accept
`--project-root` and use the frozen lifecycle window and root-anchored source/
consensus paths. Existing crawler extraction/validation algorithms are unchanged.
A source run records a request fingerprint and tree hash/file-count/byte-count
receipt. A complete retry verifies the existing source tree and does not fetch.
Failed/running directories move to `.source-attempts/<source>/<revision>` before
rerun; their contents are not serialized into SQLite. Missing/changed completed
material fails closed.

`inventory_sources.py` routes its CLI through `inventory_command`. The source
receipt and inventory remain local; SQLite holds their paths/hashes and counts.
It does not reconstruct either JSON document after loss.

The actual `coordinate_three_tracks.py` CLI routes `prepare-track`/`assemble`
through `rating_command` and requires an owner. Intermediate outputs and a local
`.runtime/<stage>.json` result remain in the rating run directory. The stage row
stores artifact receipts and, after assembly, the three isolated context IDs.
Retry verifies those artifacts rather than restoring a snapshot. The coordinator
continues to validate immutable track inputs, contracts, partitioning, independent
contexts, score scales, evidence, under-target reasons and category quotas.

The prior-identity bridge accepts only `Migrated` and `Released` previous phases.
Only a historical `Migrated` snapshot may exceed 20 rows; production remains
Paper ≤10, Policy ≤3, News+Policy ≤10, total ≤20. The production adapter compares
prior cycle/run/snapshot identity against SQLite authority. That snapshot contains
prior identity only, not old quality judgments.

## arXiv: durable local staging before emitted CAS

The actual `sync_arxiv_announcements.py` uses SQLite source state instead of a
mutable authoritative JSON state file. It performs no work during import. The
cycle-scoped run/input hash and operation `arxiv-batch:<cycle_id>` bind owner,
source, window and inputs. It retains existing announcement-key merging, half-open
window checks, metadata enrichment, pending recovery and emitted dedupe.

1. Build records, manifest, identity-only `update.json` and `receipt.json` under
   `.arxiv-batch-<digest>` in the owned workspace. Atomically write/fsync files and
   directories. Original source text exists here only, not in the database.
2. Verify record paths, hashes, manifest/update hashes and identity. Persist a
   bounded running stage intent: staging path, input/operation identity,
   manifest/update hashes, expected revision and record count.
3. Reverify/fsync local material after intent. Only then CAS the cursor/events and
   bounded receipt in one SQLite transaction. A failure cannot partially advance
   emitted, cursor revision or operation history.
4. Rename the verified local output to `working_tmp/arxiv`, sync its directory and
   complete the stage. A rename/completion crash leaves the durable local files.

On retry, look up the operation in SQLite **before** interpreting temporary files:

* A known commit (including an unknown result after COMMIT) uses verified local
  staging/output, without fetching or emitting twice. A missing/corrupt/symlinked
  committed file fails; there is no DB document mirror to reconstruct it.
* An intact uncommitted receipt can be verified and retried against its original
  CAS revision without another fetch.
* Missing/incomplete uncommitted staging or a stale CAS requires the explicit
  `--retry-uncommitted` flag. The adapter first proves no operation committed,
  archives the prior attempt under `.arxiv-attempts/<revision>`, then refetches
  against current source state. It never silently merges a stale batch or resets
  ownership/frozen window. The flag does not bypass a known committed batch.

Only changed/projected source identities enter new operation requests; historical
extra fields stay on their existing event rows without being copied into the
request journal. No schema/data remigration is required for this boundary fix.

## Formal publication handoff

The workspace owner must claim each refine run before launching its isolated child,
using `--run-owner <CONTEXT_ID>` and a context envelope whose `context_id` is the
same value. After three authentic refine contexts complete, use `python -m
colab_daily.publication complete-refine --owner <WORKSPACE_OWNER>` for each context
and `publication freeze` for the unique shared
assembly. Freeze persists only final mapped publication/assets. `publication deploy`
exports SQLite state, performs scoped VitePress build/commit/non-force push/public
readback, and alone advances a confirmed cycle to Released. Optional `publication
notify` runs only afterward; unknown POST results are SQLite-owned and never cause a
second deploy or automatic resend. Owner-authorized lifecycle cleanup may then remove
the whole workspace even if notification is disabled or unknown.

See `publication/README.md` for exact commands and delivery recovery boundaries.

## Offline verification

```sh
uv run --offline --locked python -m unittest discover -s colab_daily/storage/tests -v
uv run --offline --locked python -m unittest discover -s .agents/skills/arxiv-announcement-state/tests -v
uv run --offline --locked python -m unittest discover -s .agents/skills/rating-filter-organize/tests -v
```

Tests use isolated synthetic storage/files. Coverage includes metadata/sentinel/
large-binary absence, missing-material failures, owner/global lock and cleanup,
CAS rollback, prior/quota guards, arXiv fsync/interruption/unknown-result retries,
publication validation, scoped deployment recovery and notification unknown-result
blocking. Deterministic helpers persist only necessary control metadata and final
publication data, never whole working-state or document replay bundles. No
production crawl, semantic execution, notification POST or deployment is part of
offline checks.
