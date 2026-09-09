# SQLite storage: local deterministic state

This independent Python submodule owns migration, cycle identity, frozen publication
storage, asset durability, delivery journals and the final release gate. It performs
**no network requests, source discovery, semantic scoring, refinement, rendering,
Git operations, notification sends or scheduling**. Those callers must use the
project's semantic agents and deterministic integration validators; a receipt here
is not proof that this module itself ran those validators or verified a website.

## Setup and configuration

From the project root:

```sh
uv sync --locked
uv run --locked python -m colab_daily.storage init
uv run --locked python -m unittest discover -s colab_daily/storage/tests -v
```

Python 3.10 and the root locked environment are used; `sqlite3` is builtin and
`python-dotenv` is already a locked dependency. No dependency/lockfile change is
needed. `python -m colab_daily.storage --help` lists all commands.

`Config.load(project_root=None, environ=None)` is in `colab_daily.config`:

* Root selection: explicit argument / CLI `--project-root`, then **process**
  `PROJECT_ROOT`, then the directory containing the `colab_daily` package.
  `PROJECT_ROOT` is a bootstrap setting, not a redirect inside `.env`.
* Read that root's `.env`; process environment values override its values.
* `COLAB_STORAGE_DIR=state/storage`, `COLAB_BACKUP_DIR=state/backups`,
  `COLAB_SITE_DIR=.local/site`, `COLAB_WORKING_DIR=working_tmp`. All relative config
  and CLI paths, including paths
  inside the freeze assets mapping, are relative to **project root, never cwd**.
* CLI `--storage-dir` overrides the configured storage directory. Supply global
  flags before the subcommand. From another cwd, use `uv run --directory /path/to/project
  --locked python -m colab_daily.storage ...`.
* Keep `.env` private, outside version control. Never put tokens, webhook secrets,
  authorization headers, private keys or complete configuration objects into
  publication/metadata/delivery JSON. Store hashes and configuration-reference
  names in intent/evidence, not credentials. Storage does not load legacy API keys.

## Files, transactions and schema

A store is relocatable as a directory:

```text
state/storage/
  storage.sqlite3
  assets/<first-two-sha256-characters>/<sha256>
```

Directories are mode `0700`, DB/assets/atomic outputs mode `0600`. Paths containing
parent traversal or symlink components are rejected, including SQLite journal
paths, import inputs, attachment paths, outputs and backup/restore paths. Logical
asset names are strict relative POSIX paths. The threat boundary is a private local
user-owned directory; do not let untrusted processes mutate its ancestor directories
concurrently. Content-addressed bytes are immutable; identical attachment bytes may
share one blob while retaining every original attachment ID/metadata mapping.

Every write method uses a new SQLite connection, foreign keys, `BEGIN IMMEDIATE`,
30-second busy timeout and FULL synchronous durability. Readers needing multiple
queries use a single read transaction. WAL supports concurrent readers. Schema
version 3 is stored in both `PRAGMA user_version` and `schema_migrations`; unknown
versions fail closed. Versions 1 and 2 upgrade transactionally to version 3 without
rewriting legacy rows/assets. Initialization is idempotent. No public API exposes arbitrary
phase mutation or updates to imported/frozen rows. Direct SQLite writes bypass API
invariants and are not part of the production workflow.

Tables:

* `publish`: original integer IDs for legacy rows, unique `cycle_id`, phase,
  source phase, immutable begin metadata and frozen assembly/validation digest.
* `records`: original IDs for legacy rows; FK to Publish and unique
  `(publish_id, candidate_id)`; raw fields plus identity-only projection.
* `attachments`, `record_attachments`: original IDs, exact metadata, durable hash,
  size/path and ordered FK references (including repeated source references).
* `imports`, `legacy_rows`, `import_anomalies`: original full export **bytes**,
  exact scalar/list/null/bool values and inert malformed legacy image text.
* `publication_assets`: per-cycle logical names mapped to immutable blobs.
* `deliveries`, `delivery_events`: immutable attempt request, current result and
  append-only transition evidence.

Assets are fsynced before the DB transaction commits. A failure can leave an
unreferenced immutable blob; it cannot commit partial rows or missing references.
Orphan bytes are deliberately retained, not automatically deleted. No cleanup
should touch `state/storage`.

## Workspace and persistence boundary

`working_tmp/` is the only current-cycle workspace: downloads, rating/refine agent
inputs, transient assembly and external-process JSON live there. On failure, retain
it for recovery. **Only after full durable persistence and formal Released success**
may the integration cleanup delete the entire directory. Storage itself never
cleans the workspace. Required ownership/context/checkpoints/receipts are SQLite
facts, not hidden files underneath working_tmp. Durable final assets live under
`state/storage/assets`; source archives/backups/few diagnostic logs and regenerable
exports live under ignored `state/`. The deployment checkout stays `.local/site`,
separate from both working_tmp and state.

JSON output files shown below are regenerable process inputs/exports. They are not
parallel state authorities. Config/deploy secrets are separate private files, not
SQLite data. The old `.local` migration/store/backup copies are retained as migration
baselines; the active default and newly verified archive/backup are under state.

## Git publication boundary

Commit the Python storage code, versioned schema/migrations, synthetic tests and
module documentation. **Never commit the runtime database or attachment library**:
`state/` excludes the DB, WAL/SHM/rollback journals, assets, backups, migration
archives and logs; `.local/` preserves private legacy/deployment copies outside
root version control. Database suffixes (including `.db-journal`) and JSONL are
also ignored outside these directories. `working_tmp/`, credentials and keys are
ignored. Ignoring is not permission to force-add private files: parent/release
staging must use explicit public-file allowlists and audit the index.

Only final, validated display images may be copied into the independently ignored
deployment checkout for the VitePress publication. Never bulk-copy or stage the
storage attachment library in the new root repository. The deployment repository's
own managed-public-file allowlist is a separate integration responsibility.

`tests/test_git_boundary.py` checks actual Git ignore semantics for private runtime
paths and publishable code/schema/tests/docs, using existing repository metadata
read-only. It creates no Git repository or index; environments without existing
Git metadata explicitly skip that test and must run the final staging audit later.

## Lossless legacy import

Required export envelope:

```json
{
  "tables": {
    "Publish": {"records": [{"id": 1, "fields": {"CycleID": "example-cycle"}}]},
    "Records": {"records": []}
  },
  "columns": {"Publish": {"columns": []}, "Records": {"columns": []}},
  "attachments": [],
  "attachment_failures": []
}
```

The abbreviated example illustrates the envelope only: actual rows must contain
the original fields, timestamps, Publish references and column metadata. Each
attachment is `{id, metadata: {id, fields}, path, bytes}`, with `path` relative to
the export file's parent. Metadata `fileSize`, when present, must match bytes.
The importer supports the original `PreviewImage` attachment-list relation (`["L",
id, ...]`) and validates all declared Ref/RefList targets.

```sh
uv run --locked python -m colab_daily.storage import-legacy --input state/migration/export.json
uv run --locked python -m colab_daily.storage verify-legacy --input state/migration/export.json
uv run --locked python -m colab_daily.storage export-legacy --output state/exports/legacy-export.json
uv run --locked python -m colab_daily.storage status
```

`Store.import_legacy(export_path)` requires an empty operational DB on first
import. There is one immutable legacy snapshot; retry requires identical original
export bytes and re-verifies all source/durable bytes and stored rows. Changed
whitespace also fails, intentionally. Missing/partial files, dangling references,
duplicate IDs/identities, malformed reference lists, unknown export envelope and
reported export failures reject the transaction. Source files are never modified
or removed; diagnostics are generic so CLI errors do not disclose private data.

Historic nonempty **string** attachment cells are not reference lists. They are
preserved verbatim in raw rows and `import_anomalies`, with an explicit reason and
no fabricated attachment relation. Inspect those private anomaly rows if needed;
this does not certify that the legacy cell has a usable image. Empty/null cells
produce no reference. All actual attachment files are imported, including unlinked
ones.

`verify_legacy()` returns counts/booleans after comparing raw export bytes (thus
columns/table envelopes too), every original field/ID and operational identity,
source phase, ordered reference, metadata and exact attachment bytes, plus SQLite
integrity/FK checks. `export_legacy()` writes the **exact original JSON bytes**, not
a new self-contained attachment download tree; keep the original migration folder
or use the complete backup bundle described below.

Legacy Publish rows become `Migrated`, with original phase in `source_phase` and
raw `Phase` unchanged. `Selected` and `HumanFinished` remain inert historical data.
**Migrated does not mean Released**; old cycles cannot be frozen, notified or
released by this API.

## Public cycle/freeze/export API

```python
from colab_daily.config import Config
from colab_daily.storage import Store, StorageError, VALIDATION_CHECKS, digest

store = Store(Config.load().storage_dir)
row = store.begin_cycle("example-cycle", {
    "display_date": "2026-01-01",
    "window_since": "2025-12-31T00:00:00+00:00",
    "window_until": "2026-01-01T00:00:00+00:00"
})
```

* `begin_cycle(cycle_id, metadata: dict) -> dict`: allocates stable Publish ID in
  `PHASE_release`. Identical metadata retry returns the existing row (even after
  release); changed metadata or collision with legacy fails.
* `status(cycle_id=None) -> dict`: global counts/phases or cycle phase, provenance,
  frozen hash and delivery state summary.
* `freeze_publication(cycle_id, publication, assets, validation) -> str`: returns
  the immutable `frozen_sha256` after transactional persistence.
* `export_publication(cycle_id, destination=None) -> dict`: verifies DB digest and
  all durable bytes, returns the envelope below; optional atomic immutable file.

The publication is an extensible JSON object with required fields:

```json
{
  "schema_version": 3,
  "cycle_id": "example-cycle",
  "display_date": "2026-01-01",
  "records": [{
    "candidate_id": "example-candidate",
    "title": "Example title",
    "category": "Paper",
    "canonical_url": "https://example.org/source",
    "normalized_arxiv_id": null,
    "source_identities": ["https://example.org/source"],
    "content": "Persist the complete final refinement here",
    "preview_image": "images/example.png"
  }]
}
```

Additional final publication/record fields (final schema, section taxonomy and
final validation evidence) are persisted losslessly as JSON values. Do not embed
complete temporary inventory/canonical/rating/refine documents or replay bundles
in this envelope; map final business content and necessary final evidence only.
The integration validator defines their detailed semantic schema. Storage
requires nonempty unique candidates, date identity, typed source identities and
**new production quotas**: total ≤20, Paper ≤10, Policy ≤3, News+Policy ≤10;
only exact `Paper`, `News`, `Policy` categories. It does not replace agent judgment.

`assets` maps logical relative names to source `Path`s. Bytes are copied before
commit, and the publication should refer to those names, not transient source
paths. Asset-role/image-format/ownership checks belong to the integration validator.
`validation` must contain:

```python
validation = {
    "publication_sha256": digest(publication),
    "checks": {name: True for name in VALIDATION_CHECKS},
    # Optional durable validator reports/identities may be added here.
}
```

The exact required check names are `grouped_selection`, `refine`, `schema`,
`taxonomy`, `images`, `ownership`, `quotas`, `evidence`. **Only actual successful
validators may produce these booleans**; the snippet describes shape, not a way to
skip validation. A receipt is a trusted integration boundary, not cryptographic
proof of execution.

Export envelope:

```text
{
  publication: <complete final object>,
  assets: {logical_name: {sha256, size, path: "assets/xx/hash"}},
  validation: <hash-bound receipt>,
  frozen_sha256: <digest of the other three fields>
}
```

All digests use UTF-8 JSON with sorted object keys, compact separators, Unicode
preserved and non-finite numbers rejected. Array order and scalar types matter;
object insertion order does not. Export JSON uses this canonical order. An
order-sensitive site manifest must be **reconstructed in its required top-level
key order by the integration renderer**, not copied directly from storage JSON.
Asset `path` is relative to `Store.root`, not the export file's directory.

Same complete publication/assets/validation retry is a no-op; changed values or
asset bytes fail. After transient input cleanup, use `status()` then
`export_publication()` rather than trying to reread deleted source files to freeze
again. Export is the durable recovery API and works without `working_tmp`.

```sh
uv run --locked python -m colab_daily.storage begin-cycle --cycle example-cycle --metadata working_tmp/input/begin.json
uv run --locked python -m colab_daily.storage freeze-publication --cycle example-cycle --publication working_tmp/input/publication.json --assets working_tmp/input/assets.json --validation working_tmp/input/validation.json
uv run --locked python -m colab_daily.storage export-publication --cycle example-cycle --output state/exports/publication.json
```

## Prior-cycle identity bridge

`prior_identity(cycle_id, run_id, previous_cycle_id=None) -> dict` selects the
highest earlier stable Publish integer ID whose phase is `Migrated` or `Released`.
The optional explicit previous cycle must meet that same eligibility condition.
This is stable insertion order, **not mutable update-time ordering**; create cycles
in chronological order. New unfinished/frozen cycles never qualify as history.
It exports **all rows of that previous cycle**, not all history, never filtering
old Selected/category. It neither drops nor truncates historical rows.

```sh
uv run --locked python -m colab_daily.storage prior-identity --cycle example-cycle --run example-rating-run --output state/exports/prior_cycle_identity.json
```

Exact inspected coordinator contract:

```text
{
  schema_version: 3, cycle_id, run_id, decisions: [],
  snapshot: {
    first_cycle: bool,
    previous_publish: {row_id, cycle_id, phase, updated} | null,
    records: [{row_id, candidate_id, title, publish_row_id, updated,
               normalized_arxiv_id, canonical_url, source_identities}],
    before: {publish: <previous_publish>,
             records: [{row_id, candidate_id, updated}, ...]} | null,
    after: <identical before value>
  }
}
```

The first-cycle snapshot is explicitly empty with null previous/before/after.
On first call, the exact prior envelope is frozen in `prior_snapshots` for the
cycle/run pair in the same transaction as its read. Repeated export reuses this
authoritative DB snapshot even if another older cycle subsequently releases. An
explicit changed previous cycle fails closed. Before/after therefore describe one
SQLite snapshot. Records/readback are in row-ID
order; timestamps are UTC ISO strings. No scores, summaries, Selected, categories,
publication content or raw source payload are included. Legacy identity projection
retains CandidateID/title and exact source URL spelling from JSON source fields or
older Markdown URLs; unavailable canonical/arXiv values remain null. Legacy URL
extraction is mechanical, not a semantic deduplication decision. The agent still
fills `decisions` for the **current** canonical candidates; storage never fabricates
those decisions.

The actual coordinator now accepts this bridge through an owner-guarded CLI:
Migrated is historical-only, and only Migrated snapshots may exceed 20 prior rows.
Production calls also compare the snapshot to SQLite authority. See
`../RUNTIME.md` for lifecycle, global ownership, source adapters and recovery.

## Durable run ownership, stages and source state (schemas 2–3)

Run state is in `runs`, `run_stages`, `stage_operations`; prior identity state is
in `prior_snapshots`. SQLite owns control state, **not intermediate documents**.
`context` and stage/source `receipt` objects must be bounded control metadata:
`identity`, `paths`, `counts`, `checksums`, `context_id`, `session_id`, `status`,
`reason` (canonical UTF-8 maximum 16 KiB). See `../RUNTIME.md` and
`metadata.py` for scalar/path/hash limits and examples. Full text, temporary file
snapshots, base64, inventory/semantic result objects and replay bundles are rejected.
Intermediate source/canonical/scoring/refinement JSON and binaries stay in
`working_tmp`; missing material must fail or use explicitly controlled recovery.

* `claim_run(cycle_id, run_id, kind, owner, context: dict) -> {run_id, owner,
  created_now}`: immutable logical owner/context; run ID is unique and only one
  run of a given `kind` may own a cycle. Use separate kinds such as `rating:paper`,
  `rating:news`, `rating:policy`, then isolated refine kinds. Identical retry returns
  `created_now=false`; conflicting owner/context/run ID fails closed. An owner is
  a durable logical context identity, not a PID or expiring lease. No automatic
  owner takeover is implemented.
* `run_state(run_id) -> {run_id,publish_id,kind,owner,context,stages}` returns core
  metadata and each stage's `{revision,status,payload}`.
* `checkpoint_stage(run_id, owner, stage, operation_id, expected_revision, status,
  payload: dict) -> revision`: CAS from revision 0 for a new stage. Status is
  `running`, `failed`, or `complete`; failed/running may advance, complete cannot
  change. Persist only bounded metadata/path/hash/count receipts, never full
  outputs or evidence documents. Each operation's small original request is
  retained for exact retry; changed input, stale revision or different owner fails
  closed. New checkpoints cannot mutate Released cycles. An exact original
  operation retry remains safe and returns its original revision.

Source state is in `source_cursors`, `source_events`, `source_operations`:

* `source_state(source) -> {source,revision,cursor,events}`; missing source returns
  revision 0 and empty cursor/events. Events are sorted by key.
* `update_source_state(source, operation_id, expected_revision, cursor: dict,
  events: list, receipt=None) -> revision`: atomically CAS a bounded scalar cursor
  and upsert identity-only event deltas plus an optional bounded receipt.
  **Unmentioned events are retained**, not copied into every new request or pruned.
  Events require a unique nonempty `key` and status `seen`, `pending`, or `emitted`.
  New fields are restricted by `metadata.EVENT_FIELDS`; nested documents, candidate
  bodies and summaries are rejected. Original extra fields on historical event
  rows are preserved in place, not duplicated into new operation requests.
  Exact operation retry is a no-op with its original revision; changed retry/stale
  revision rejects the whole transaction. Emitted cannot regress/change cycles.
* `source_batch(source, operation_id) -> {revision, receipt} | None` looks up the
  durable operation result; it does not return document/file replay data. It is
  used before deciding whether an interrupted arXiv attempt may be rerun.

For arXiv, use `source='arxiv'` and `<source_id>:<announcement_family>` event keys.
Cursor fields include schema/retention settings and last successful sync time.
The actual adapter stages/fsyncs and verifies records locally, writes minimal
intent, reverifies files, then atomically commits cursor/events/receipt before
renaming output visible. Known commits reuse intact local files without fetching;
missing committed files fail closed. Explicit `--retry-uncommitted` is permitted
only after proving no operation committed. See `../RUNTIME.md` for crash handling.
Historical migration archives/data remain untouched; the normal runtime update
API is no longer a full legacy-event import channel.

```sh
# context/input below contain bounded metadata, not semantic output documents:
uv run --locked python -m colab_daily.storage claim-run --cycle example-cycle --run paper-run --kind rating:paper --owner paper-context --context working_tmp/input/context-metadata.json --output state/exports/run-claim.json
uv run --locked python -m colab_daily.storage checkpoint-stage --run paper-run --owner paper-context --stage rating --operation checkpoint-1 --revision 0 --status complete --input working_tmp/input/rating-receipt.json
uv run --locked python -m colab_daily.storage run-state --run paper-run --output state/exports/run-state.json
uv run --locked python -m colab_daily.storage source-state --source arxiv --output working_tmp/input/arxiv-state.json
# input is {"cursor": {...}, "events": [...]}, containing bounded identities only:
uv run --locked python -m colab_daily.storage update-source-state --source arxiv --operation arxiv-update-1 --revision 0 --input working_tmp/input/arxiv-update.json
```

Online backup retains these small operation histories, prior identity snapshots,
final delivery evidence and final/legacy assets. It deliberately does not back up
`working_tmp` or reconstruct intermediate documents. Final image assets use the
durable blob mapping. No automatic pruning of historical identities/operations is
introduced here; bounded receipts avoid repeated whole-state mirroring.

## Delivery journal and formal release gate

Storage never sends anything. The caller must persist intent before a side effect:

* `start_delivery(cycle_id, channel, attempt_id, request) -> dict`, channel is
  `deployment` or `notification`; request must include matching `frozen_sha256`.
  Include intended `artifact_sha256` and known `commit_sha` for deployment; if
  present they must match confirmation evidence. Include `message_sha256` and a
  non-secret destination configuration reference for notification, never its URL.
* The returned **`created_now` is true only for the one newly inserted intent**.
  Only that caller may issue the initial side effect. Retry returns the existing
  intent with `created_now=false`: inspect/reconcile, **do not send again**, even
  when its state is `pending`. Concurrent calls have exactly one owner.
* `record_delivery(cycle_id, channel, attempt_id, state, evidence) -> state`, where
  state is `unknown`, `failed`, or `confirmed`. Every evidence object requires
  `frozen_sha256` and a nonempty `reason`. No credentials belong in evidence.
* `delivery_history(cycle_id, channel=None) -> list` returns parsed original
  requests, final evidence, and ordered event history for recovery/readback.

`pending` and `unknown` block new attempt IDs. Resolve the **same** attempt after
read-only remote/deployment readback. `failed` requires verified
`side_effect_absent: true`; it then permits a new attempt ID. Terminal failed or
confirmed evidence cannot be changed; identical result retry is a no-op. This
explicit absence proof must come from integration, not a timeout guess. An unknown
notification POST without provider readback cannot safely be retried automatically;
keep it unknown and keep the publication Released. There is no human phase/gate.

Deployment `confirmed` additionally requires:

```json
{
  "frozen_sha256": "the exact frozen digest",
  "reason": "explanation of verified readbacks",
  "local_verified": true,
  "push_verified": true,
  "public_verified": true,
  "commit_sha": "40 or 64 lowercase hexadecimal characters",
  "artifact_sha256": "64 lowercase hexadecimal characters"
}
```

These confirmations mean: exact generated assets/build/content passed locally,
non-force push reconciled to exact commit, and public deployment readback matches
that exact committed artifact. The **next integration worker must implement those
checks**. Unknown push is not permission to force-push or create a new commit.

`release(cycle_id) -> "Released"` requires frozen data/assets to verify and a
confirmed deployment satisfying all three checks and hashes. It is idempotent;
there is no human phase. Notification cannot start before Released; notification
confirmation requires `accepted: true`. Notification failure/unknown does not
revert Released or permit a duplicate deployment.

```sh
uv run --locked python -m colab_daily.storage start-delivery --cycle example-cycle --channel deployment --attempt deploy-1 --input working_tmp/input/deploy-intent.json --output state/exports/receipts/deploy-start.json
uv run --locked python -m colab_daily.storage record-delivery --cycle example-cycle --channel deployment --attempt deploy-1 --state unknown --input working_tmp/input/deploy-result.json
uv run --locked python -m colab_daily.storage delivery-history --cycle example-cycle --channel deployment --output state/exports/receipts/deploy-history.json
# After actual local/push/public checks and same-attempt reconciliation:
uv run --locked python -m colab_daily.storage record-delivery --cycle example-cycle --channel deployment --attempt deploy-1 --state confirmed --input working_tmp/input/deploy-confirmed.json
uv run --locked python -m colab_daily.storage release --cycle example-cycle
```

CLI stdout contains only counts/generic success/phase; private payloads go to
explicit output files. Status with `--cycle --output` writes full identity status;
without output it prints only phase/frozen boolean. Keep CLI argument logs private
if cycle IDs themselves contain identifying information.

## Backups and safe restore

```sh
uv run --locked python -m colab_daily.storage backup --output state/backups/example-snapshot
uv run --locked python -m colab_daily.storage restore --input state/backups/example-snapshot --output state/restored-storage
uv run --locked python -m colab_daily.storage --storage-dir state/restored-storage status
```

`backup(destination)` uses SQLite's online backup API, then copies **all referenced
legacy and publication blobs** from that DB snapshot, verifying hashes/bytes and
FK/integrity. A manifest records every file hash/size. An atomic directory rename
publishes the bundle only after fsync. Destination must be new and outside the live
store. Backup includes raw archive/provenance, anomalies and delivery events. It
excludes unreferenced orphan blobs and unrelated secrets/config/deploy keys.

`Store.restore(backup_dir, destination) -> Store` verifies manifest, every byte,
SQLite version/integrity/FKs and completeness of referenced assets, then atomically
creates a **new** store directory. Version-1 bundles are accepted and upgraded to
schema 3 after verified restoration. Neither backup nor restore overwrites existing
data. Interrupted hidden staging directories remain private for diagnosis. Stop
workflow writers, verify the restored store, and explicitly change local
`COLAB_STORAGE_DIR` to adopt it; do not replace/copy an open live SQLite file.
Back up `.env`/deploy keys separately with appropriate secret protection.

## Milestone boundary

This submodule is ready for release integration, **not a claim that the whole
project's retired skills/site have been migrated**. Next integration owns semantic
agent orchestration, detailed grouped/refine/schema/image/ownership validators,
ordered schema-v3 site manifest generation, formal deployment verification and
optional notification adapters. The public site template must remove one-off
historical-identity exceptions; any historical compatibility must be generic and
limited to legacy content. Deployment must use only the configured dedicated key,
with `IdentityAgent=none` and `SSH_AUTH_SOCK` disabled. None of those network or
production operations were exercised by storage tests.
