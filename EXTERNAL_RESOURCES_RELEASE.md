# External Resources release

The Hub backend registers links when a component is saved. The Worker processes eligible resources and owns inspection, extraction, OCR, retry and lifecycle state. The Agent owns embeddings and vector writes through its authenticated commands API. External retrieval is controlled by the Agent and rechecks the current registry mapping and live Hub ACL.

## Deployment order

1. Confirm the actual sandbox branch, Neon host and database, target schema and role configuration. Check that the Agent and Worker use the same registry and that the Agent's external vector collection and command state are isolated by deployment environment.
2. Stop any older Worker schedule before changing the registry. The backend setup is additive. Review the backup and rollback plan, then use the backend environment to run:

   ```powershell
   python -m scripts.setup_external_worker_operations --expected-host '<reviewed-neon-host>' --expected-database '<reviewed-neon-database>'
   ```

   The command uses `HUB_ENVIRONMENT` and checks the configured V2 database before beginning. It creates or extends the registry, prepares operational tables and rollups, and anchors the database to the declared environment. A mismatched existing environment rolls back the transaction.
3. Deploy the matching Agent and Worker with a dedicated high-entropy Worker token and matching `EXTERNAL_DOCUMENT_INGESTION_ENVIRONMENT` and `EXTERNAL_RESOURCE_WORKER_ENVIRONMENT`. Use HTTPS for the Worker endpoint and the approved shared Redis state store for Agent command idempotency. The Agent adds the deployment environment to the Redis key prefix. The Agent expects RenPhilExternalDocsKnowledgeBaseSandbox in sandbox, RenPhilExternalDocsKnowledgeBase in production, and RenPhilExternalDocsKnowledgeBaseLocal in local development. A sandbox collection with the legacy name requires a reviewed rebuild under the sandbox name. Keep retrieval disabled during initial ingestion verification.
4. Deploy the backend and frontend together. Check External Resources as an All Scopes administrator and as a scoped user. Check direct navigation, View As, registry decisions, queued retries, status and run details, settings history and CSV exports.
5. Verify the host can run OCRmyPDF, Tesseract with English trained data, Ghostscript and PyMuPDF. Run the Worker dry preview before any live run. Then process a small reviewed batch with explicit link IDs and verify ingestion, unchanged checks, ACL refresh, archive and delete. Rehearse paused mode before enabling a recurring schedule.
6. Enable the scheduler only after the bounded sandbox smoke and soak pass. Alert on nonzero exit, stale completion, interrupted runs and environment mismatch. Enable external retrieval separately after the current-ACL browser and Agent checks pass against the sandbox collection.

## Pause, history and metrics

The Worker reads the active pause version when a run starts. Pause stops new inspections and ingests. Deletions, archival, ACL refresh and pending rejected-ingest vector cleanup continue. The run records the environment, effective settings version, queue depth and safe configuration values.

Worker events increment daily UTC rollups in the same transaction that records the detail, before any pruning. Admin audit events are excluded from Worker pruning and from analytics totals. Worker detail keeps the latest 50 entries per resource. Old events for removed resources and finished run details age out after 180 days. Pre-environment records remain historical and are not relabeled or counted in new environment-specific trends. Metrics retain event counts and elapsed-time totals for the recorded operations. Records predating instrumentation have no measured duration.

## Rollback

Pause or stop the new Worker schedule first. Keep the additive registry and operations schema and preserve audit history. Disable the Agent's external retrieval flag before removing Worker or Agent configuration. Preserve the authoritative registry. If a rollback needs vectors reconciled, use a reviewed link-level reconciliation and record its result. Restore a previously reviewed schedule only after confirming that it targets the correct registry and uses a compatible command API.

## Local verification commands

- Backend API and schema: `pytest -q tests/test_content_link_registry.py tests/test_external_resources_operations.py` with `EXTERNAL_RESOURCES_TEST_DATABASE_URL` pointing to disposable PostgreSQL.
- Worker: `pytest -q tests` with `WORKER_TEST_POSTGRES_DSN` set to disposable PostgreSQL. `tests/test_ocr_quality.py` exercises generated digital, scanned and mixed PDFs when all OCR binaries are available.
- Agent: `pytest -q tests/test_external_document_endpoint.py tests/test_external_document_ingestion.py tests/test_external_document_retrieval.py tests/test_external_document_state.py`.
- Frontend: `npm test`, `npm run build`, `npm run security:browser-boundary`.

### Drive folder processing

The worker resolves a reviewed Drive folder to individual registry children. Each child requires an explicit ingestion decision for the audience of its mapped Hub component. The same Drive file linked directly or under another folder gets an independent lifecycle and authorization mapping. Folder discovery walks nested folders, skips shortcuts, and refuses to reconcile partial listings. It supports up to 250 files, 40 folders and 40 listing pages per scan. A complete rescan invalidates absent children before vector cleanup. Agent retrieval rechecks Hub access to the current component mapping. Drive access is checked using the worker service identity on inspections. It is not checked as each Hub user.

Deploy the backend operations schema before the Agent and Worker changes. The new effective mapping view is required even for individual file commands. Keep retrieval disabled until direct and folder-derived paths pass permission smoke tests.

The Worker worktree includes example systemd service and timer units under deploy/systemd. Provision the dedicated service user, installed worker and OCR runtime, and readable private environment and credentials before installing them. Test the service once in the reviewed environment, inspect the run in the Hub and journal, then enable the timer. Monitor nonzero service exit status, stale runs, and repeated per-resource failures in the Worker tab. The timer waits 15 minutes after a run completes before starting another. Resource rescans follow EXTERNAL_RECHECK_INTERVAL_HOURS independently.

## Run details: what the Worker noticed in a document (OCR note)

The Worker records at most one note per document per run in `external_worker_run_items.ocr_note` (`VARCHAR(64)`, nullable): `ocr_date_corrected`, `ocr_implausible_date`, `ocr_unreadable_text` or `ocr_page_rotated`. The column is added by `scripts.setup_external_worker_operations` (`ADD COLUMN IF NOT EXISTS`, additive). Run it **before** starting a Worker version that writes the note; the Worker tolerates a missing column and the run-items API returns `ocr_note: null` before the migration. The API returns only these four codes (anything else is hidden) and the Hub shows each as a plain sentence in Run details. A note is a hint to a person; it never changes what was indexed, and a historical item without a note is not guessed.

## Admin API behavior worth knowing

- **Search** (`GET /v2/external-resources/resources?search=`) matches the URL (without query or fragment), provider and type as plain text: `%` and `_` are ordinary characters, not wildcards. A numeric search also matches a resource or folder id.
- **Events and history** use one rule: events of the configured environment plus events written before environments existed (environment `NULL`). Those older rows keep an empty environment in exports; they are never relabeled.
- **CSV exports** are read in id-ordered pages and have no 10,000-row failure. A guard stops at 500,000 rows with a clear message. An empty dataset still returns its header row. Cells that could be read as formulas are prefixed.
- **Schema check**: the request guard remembers a complete schema for 60 seconds per database engine instead of reflecting `information_schema` on every request. Only a positive result is remembered; a missing schema is checked every time.
- Every call is limited to Admin with All Scopes and refuses View As. The Hub page treats a `403` from any call as lost access: it removes the registry, run details and settings from the screen and shows "Access denied".

## Document preview

Each document in the Resources tab shows a small link preview under its name: the title, where it lives, and the opening of its indexed text, loaded by itself when the card scrolls into view (no button). It is read-only and never shows access information.

- Chain: browser -> Hub backend `GET /v2/external-resources/resources/{id}/preview` (Admin + All Scopes, View As refused, `Cache-Control: private, no-store`) -> Agent `GET /internal/external-documents/{id}/preview` using the shared service token (`AGENT_API_URL`, `AGENT_SYNC_TOKEN`; the same settings the retrieval status check uses).
- The backend answers `available: false` without calling the Agent unless the registry says the resource is indexed, enabled, not deleted, not archived and not an unsupported format. The Agent re-reads the registry, reads only the first chunks of the current content version and returns a trimmed excerpt, an optional title and the section count. Failures are summarized, never detailed.
- Documents that are not indexed yet say "No preview yet. It appears once the worker has indexed this document." The preview is the text that was indexed, so a scanned page can read as run-together columns exactly as OCR produced it.
- Deploy the Agent change before relying on previews; until then the backend reports the preview as unavailable and nothing else is affected. The Worker receives no new credentials.
- **Show more (whole document).** `GET /v2/external-resources/resources/{id}/content?start=N&version=V` (same Admin + All Scopes, View As and registry checks as the preview) -> Agent `GET /internal/external-documents/{id}/content`. Each call returns up to 12 stored sections (at most 12,000 characters; the backend caps the text at 20,000) in order, with the overlap between neighbouring sections removed, plus `next` (the section to ask for next, or null at the end) and `version` (the first 16 characters of the content fingerprint). Every section is read by its deterministic id and checked for this document and version; a missing or mismatched section ends the page instead of being skipped. If the document was re-indexed between two calls the reply is `changed: true` and the Hub restarts from the top. Deploy the Agent change first; until then "Show more" says the rest could not be loaded.
