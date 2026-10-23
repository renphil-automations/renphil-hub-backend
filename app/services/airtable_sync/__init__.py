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
                     user creation, user_deleted (no HR fields yet)
  items              conflicts / warnings: open, refresh, supersede, remember
                     an answer, auto-close
  resolutions        the items API: list items, resolve one (C4)
  runs_service       run history, a run's log, the tab's status payload
"""
