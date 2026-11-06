"""The Airtable → Hub sync (AI Docs/plan_airtable_sync_v2_2026-10-21.md).

  settings_service   the schedule row and the Designation → role mapping
  airtable_reader    read-only Airtable access (never writes to Airtable):
                     the Designation choices and a run's four-table snapshot
  normalize          pure value normalizers (trim, emails, dates, sets…)
  runner             the run lock, background execution, heartbeat, preview
  steps              the four steps a run executes, in their fixed order
  scopes_step        the Clusters and Programs steps: scopes, C2 aliases,
                     sync edges
  users_step         the Users step: K2 matching, M7, person-level items,
                     user creation, the HR fields, headshots, user_deleted
  hr_fields          the HR fields' three-way merge and E1 (pure)
  r2                 headshots on Cloudflare R2 (upload / delete, boto3)
  lock_sweep         releases edit locks after the sync removes access
  program_roles_step the Program Roles step: (employee, program) pairs →
                     sync-managed role assignments, three-way, pair items
  items              conflicts / warnings: open, refresh, supersede, remember
                     an answer, auto-close
  resolutions        the items API: list items, resolve one (C4), bulk
                     resolve
  runs_service       run history, a run's log, the tab's status payload
"""
