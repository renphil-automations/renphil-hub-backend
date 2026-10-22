"""The Airtable → Hub sync (AI Docs/plan_airtable_sync_v2_2026-10-21.md).

  settings_service   the schedule row and the Designation → role mapping
  airtable_reader    read-only Airtable access (never writes to Airtable):
                     the Designation choices and a run's four-table snapshot
  normalize          pure value normalizers (trim, emails, dates, sets…)
  runner             the run lock, background execution, heartbeat, preview
  steps              the four steps a run executes, in their fixed order
  runs_service       run history, a run's log, the tab's status payload
"""
