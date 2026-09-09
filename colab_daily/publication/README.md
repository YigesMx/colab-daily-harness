# Formal publication adapters

This module is the deterministic boundary between semantic agent artifacts, SQLite final state, formal VitePress deployment, and optional post-Release notification.

## Commands

```sh
python -m colab_daily.publication preflight
python -m colab_daily.publication sync-template
python -m colab_daily.lifecycle claim-run --owner <WORKSPACE_OWNER> --run <RUN_ID> --kind refine:<track> --run-owner <CONTEXT_ID> --context <PATH>
python -m colab_daily.publication complete-refine --owner <WORKSPACE_OWNER> --run <RUN_ID> --manifest <PATH>
python -m colab_daily.publication freeze --owner <WORKSPACE_OWNER> --assembly <PATH>
python -m colab_daily.publication deploy --cycle <CYCLE_ID>
python -m colab_daily.publication notify --cycle <CYCLE_ID>
```

`preflight` and `sync-template` do not use the network. `deploy` and `notify` are production side-effect commands and must be mocked in maintenance tests.

## Refine/freeze boundary

Semantic agents write evidence, image audits, final Markdown, taxonomy and manifests under their isolated `working_tmp` generations. The workspace owner claims each run while recording a distinct child context owner. `complete-refine` uses the workspace owner for authorization, verifies that authentic SQLite child-run identity, and records only bounded artifact receipts. `freeze` validates the unique shared assembly against grouped selection and all three contexts, then maps only final publication fields and deterministically decoded, orientation-applied, metadata-free PNG display images into SQLite/content-addressed storage.

No source body, PDF, intermediate inventory/rating/refine document, base64 value, agent transcript or directory snapshot is persisted. Missing temporary evidence fails closed and remains in the owned workspace.

## Deployment

Deployment exports the immutable SQLite publication and renders a safe, deterministic site write set. Current public pages contain category-local rank and display content, not internal score/scale/track fields or the private SQLite freeze receipt. The exact prospective write set is scanned against private configuration fingerprints and credential/key patterns before a SQLite intent or checkout write. The adapter rejects path traversal, symlinks, date collisions, unowned modifications/staged files, unsafe Markdown/URLs and unmanaged assets. It uses a dedicated 0600 SSH key with agent/global/personal credentials disabled.

A retry after an unknown push reuses the exact local commit and intended files. Push is non-force. `Released` requires a verified local build, exact remote commit, commit-bound public artifact manifest, and byte verification of every public artifact. Failure/unknown leaves `PHASE_release` and preserves intent/workspace.

## Notification

Notification is optional and can start only after `Released`. It renders a bounded summary from the SQLite final publication and reads the webhook only from private environment configuration. SQLite stores a fixed intent before POST, but not the webhook or request payload. Only explicit endpoint acceptance confirms delivery; timeout/ambiguous responses become `unknown`, and later calls refuse to POST again. A preexisting `pending` intent is also unresolved and never authorizes a second POST; confirmed retries return before optional webhook configuration is loaded. Notification never changes release state or invokes deployment.

After Released, lifecycle cleanup may delete the owning `working_tmp` even when notification is disabled or unknown because delivery recovery is SQLite-owned.
