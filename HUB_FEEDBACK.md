# Hub and Bot feedback in Neon

This feature uses two independent tables: `hub_feedback` for Hub/page/component feedback, and `bot_feedback` for message-level Agent feedback. Neither is a usage-analytics or telemetry table.

## User behavior and permissions

- Authenticated viewers can submit feedback on pages they can open and components whose content they can access. Submission does not require edit permission.
- Hub's floating feedback form offers Hub-wide or current host-page feedback, defaulting to the current page when a valid binding exists. Built-in screens with no resource binding offer Hub-wide feedback. Component controls bind to the exact occurrence and its containing page; mirror occurrences remain separate from originals.
- Component controls appear on hover or keyboard focus in view mode and permanently in Edit Mode. Touch devices show the control without hover. Restricted placeholders and unsaved/v1 components do not get a control.
- Editors can read global Hub feedback and feedback on resources they can currently edit. They can see the submitter's email, as explicitly approved. All Scopes admins see all Hub feedback. A viewer with no edit grants cannot read lists or details.
- Bot feedback remains under Bot Management and requires its existing All Scopes admin gate. Hub feedback appears under Hub Analytics. Each reads its own table.
- Feedback submission and reading are unavailable through View As parameters. The real authenticated user supplies identity; client email or role hints are not trusted. Like other View As requests, a caller deliberately stripping all markers is indistinguishable from their normal authenticated session.
- Global feedback can retain an opaque validated page identifier and a privacy-safe route template privately for diagnosis. The route is only a client-reported hint, sanitized again by the server; no full URL/query string/title slug is retained. Private context is not returned in ordinary submission, list, detail or export responses, even on the ordinary admin list. No diagnostic UI/API is included in this release.
- Names are resolved from live records. Deletion does not cascade into feedback. Editors lose access to deleted scoped targets; admins see an unavailable target with no old name/link. A moved component keeps its original binding, but access follows current component permissions; inaccessible historical page context is withheld.
- Sandbox and production are locked to `HUB_ENVIRONMENT` server-side. Local may compare environments. An unconfigured environment refuses new writes and reads rather than guessing.
- Feedback is retained until a separate policy is agreed. Product-analytics retention does not purge these tables. No feedback purge job is enabled.

## API and compatibility

- `POST /v2/hub-feedback`: JSON `submission_id` UUID, `scope` (`hub`, `page`, `component`), `message` (nonblank, at most 2000 characters), optional opaque `page_id`, and `component_link` for component scope. The backend validates ancestry and current access. Body limit 16 KB; no client identity/environment accepted.
- `GET /v2/hub-feedback`: scope/page/component/environment filters and bounded pagination. Live permission filtering occurs before counting and pagination. Editors cannot widen the result by changing identifiers. Detail requests use the same predicate.
- Retries of a new Hub submission reuse its UUID. Same caller/key with different content is 409; simultaneous identical submissions store one record.
- The existing `/data/feedbacks` route remains compatible. `source=Bot` writes only `bot_feedback`; `Hub`/missing source writes Hub-wide feedback. Legacy responses preserve their existing aliased field names. Authenticated identity and environment are derived server-side. Identical Bot feedback is deduplicated by caller/content. Old global clients without a retry key retain their legacy create-per-request behavior; the new form uses UUID retry protection.
- Existing Bot Management feedback reads/negative-feedback Excel reports now use Neon and no longer read the mixed Airtable table. The existing loaded-record limit remains 500; it is not an exhaustive full-history report. Excel disables automatic formulas and URLs for feedback strings. Bot conversation context is user-reported, not cryptographically verified answer content.
- Reads have no shared feedback cache. New Hub responses are private/no-store, including validation/denial responses. Database errors log exception classes only, without feedback text, people, URLs or conversation content.
- New Hub feedback submissions use the existing advisory Upstash limiter, 10 per user per minute. Its existing fail-open behavior is unchanged; this is not a strict abuse ceiling under concurrent requests.

## Rollout and import

1. Review the exact target database and set `HUB_ENVIRONMENT` (`sandbox`, `production`, or `local`). There are no new secret variables.
2. Run `python -m scripts.create_hub_feedback_tables` against that target before rolling out code that requires the tables. This creates only the two feedback tables and their indexes, is repeatable, and refuses a known older table shape rather than claiming it is upgraded. It does not import, alter or delete Airtable records.
3. Export a reviewed Airtable JSON snapshot. Separate or explicitly classify environment and feedback source first. Never infer them from message text, URL or a missing field. Review sensitive snapshot handling; do not commit the file.
4. Preview with `python -m scripts.import_feedback_snapshot snapshot.json --environment sandbox`. Preview is the default and performs no database writes. At most 20 MB / 20000 records per snapshot; split larger reviewed batches. Field mappings use the existing Airtable settings; Date & Time is the existing computed field, with Airtable createdTime as a fallback.
5. Blank/unknown source records stop the import. Only after reviewing them, use `--missing-source hub` or `--missing-source bot` for blank sources, or explicitly `--skip-ambiguous`. Unknown environments can remain `unknown`; they are not automatically visible to sandbox/production readers.
6. Add `--apply --expected-host <reviewed-endpoint> --expected-database <reviewed-name>` only after review of the preview and target. The configured target must match both flags. Unchanged `(environment, Airtable record id)` values are skipped. Changed source content or classification fails and rolls back the batch for reconciliation. Imports retain original timestamps and author email as historical source data; they do not provision or impersonate Hub users. Logs contain counts only.
7. Coordinate the backend cutover with old instances: a snapshot taken before cutover can miss final Airtable submissions. After all old instances stop writing there, import a final delta snapshot through the same repeatable process. No dual-write or automatic Airtable deletion is performed.
8. Deploy the matching frontend after backend/schema readiness. Recheck viewer submission, editor reads/revocation, admin Bot isolation, environment lock, View As, duplicates and inaccessible targets.
9. Rollback must preserve both databases. Older application code will not display new Neon-only submissions; keep the data and plan reconciliation explicitly. Do not delete the new tables or silently copy private context back into legacy fields.

### Portable PowerShell runner

`scripts/run_feedback_migration.ps1` travels with the integrated backend branch. It reads the target's normal environment variables (or a private `-EnvFile`), checks the configured host and database against the supplied expected values and checks `HUB_ENVIRONMENT`. No source editing is needed for another environment. Set `HUB_ENVIRONMENT` in the actual deployment configuration separately; the runner does not modify that configuration or create tables. After separately reviewing and creating the schema and snapshot, run from the backend root, replacing the placeholders:

```powershell
.\scripts\run_feedback_migration.ps1 `
  -Snapshot 'C:\private\reviewed-snapshot.json' `
  -Environment sandbox `
  -ExpectedHost 'reviewed-neon-host' -ExpectedDatabase 'reviewed-database' `
  -Python 'C:\path\to\venv\Scripts\python.exe' `
  -EnvFile 'C:\private\target.env' `
  -ApprovedBlankSourceIsHub
```

Omit `-ApprovedBlankSourceIsHub` unless every blank-source row in that particular snapshot was reviewed and approved as Hub. This defaults to validation only; repeat with `-Apply` only after confirming the target and preview. Apply both reviewed private batches separately, and review and import the final delta after old Airtable writers stop. Dry run does not reconcile existing rows; an apply skips unchanged IDs and rejects changed records. Never commit snapshots or credentials. For production, use its own reviewed target and classification, set `HUB_ENVIRONMENT=production` in its configuration and pass `-Environment production` only after the appropriate review.

## Release status

The local combined Feedback work is based on the Analytics changes merged into sandbox. It remains uncommitted and undeployed. In the user-approved development database, the Feedback tables have been created and 24 historical Hub records and 21 Bot records were imported with the `sandbox` environment; the original Airtable data remains. This dev operation does not set up or migrate any deployment target. A fresh reviewed snapshot, final delta, verified target and cutover/rollback plan remain necessary for each deployment. The user reports that live permission revocation worked; this check has no separately captured result. Deployed-environment checks remain release gates. The workspace QA handoff records exact tests and limitations; do not include its private snapshots or review documents in a PR.
