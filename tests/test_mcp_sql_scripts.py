"""Static checks of the reviewed SQL scripts (they are never executed here).

S11: ``VALIDATE CONSTRAINT`` must run in its own transaction after the
``ADD CONSTRAINT ... NOT VALID`` commits; validating inside the same
transaction keeps the ACCESS EXCLUSIVE lock of the ADD for the whole scan.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "sql"


def _transactions(sql: str) -> list[str]:
    """Statements grouped by explicit BEGIN ... COMMIT blocks (comments removed)."""
    body = re.sub(r"--[^\n]*", "", sql)
    return [block for block in re.split(r"(?im)^\s*COMMIT\s*;", body)]


class TelemetryScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = (SQL_DIR / "mcp_telemetry_actor_kind.sql").read_text(encoding="utf-8")

    def test_validate_runs_in_a_later_transaction_than_add_not_valid(self):
        blocks = _transactions(self.sql)
        for constraint in ("bot_telemetry_runs_actor_kind_check", "bot_telemetry_runs_kind_check"):
            with self.subTest(constraint=constraint):
                add_block = next(i for i, block in enumerate(blocks) if re.search(rf"ADD CONSTRAINT {constraint}\b[\s\S]*?NOT VALID", block))
                validate_block = next(i for i, block in enumerate(blocks) if f"VALIDATE CONSTRAINT {constraint}" in block)
                self.assertGreater(validate_block, add_block)

    def test_header_requires_review_and_forbids_auto_run(self):
        for name in ("mcp_telemetry_actor_kind.sql", "mcp_oauth_create.sql"):
            header = (SQL_DIR / name).read_text(encoding="utf-8")[:1500]
            with self.subTest(name=name):
                self.assertIn("REVIEW REQUIRED", header)
                self.assertIn("NEVER auto-run", header)
                self.assertIn("approved database", header)


if __name__ == "__main__":
    unittest.main()
