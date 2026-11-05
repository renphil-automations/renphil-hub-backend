"""Archive format, integrity validation, idempotent verified uploads and
storage configuration for the product analytics archive (no database)."""

from __future__ import annotations

import hashlib
import json
import logging
import unittest
from datetime import date, datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from app.services import product_analytics_archive as archive
from app.services.product_analytics_archive import (
    ArchiveConfigError, ArchiveIntegrityError, MemoryArchiveStore, R2ArchiveStore, R2Config,
    archive_key, ensure_archived, make_archive, put_and_verify, validate_archive,
)

AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _event(**overrides) -> dict:
    row = {"id": 1, "event_id": "e-1", "environment": "sandbox", "event_name": "root_tab.selected", "actor_email": "a@renphil.org",
           "properties": {"tab_kind": "root", "depth": 1}, "occurred_at": AT, "received_at": AT, "page_id": None}
    row.update(overrides)
    return row


def _engagement(**overrides) -> dict:
    row = {"id": 1, "engagement_id": "g-1", "environment": "sandbox", "actor_email": "a@renphil.org", "active_seconds": 5,
           "started_at": AT, "ended_at": AT, "received_at": AT}
    row.update(overrides)
    return row


class ArchiveFormatTests(unittest.TestCase):
    def test_archive_contains_versioned_manifest_members_and_utc_timestamps(self) -> None:
        payload = make_archive(environment="sandbox", events=[_event()], engagements=[_engagement(id=9, ended_at=AT.replace(hour=13))])
        with ZipFile(BytesIO(payload)) as zipped:
            self.assertEqual(sorted(zipped.namelist()), ["engagements.csv", "engagements.jsonl", "events.csv", "events.jsonl", "manifest.json"])
            manifest = json.loads(zipped.read("manifest.json"))
            self.assertEqual((manifest["format_version"], manifest["environment"], manifest["events"], manifest["engagements"]), (3, "sandbox", 1, 1))
            self.assertEqual(manifest["record_types"], ["events", "engagements"])
            self.assertEqual(manifest["time_range"]["events"], {"min": "2026-01-01T12:00:00+00:00", "max": "2026-01-01T12:00:00+00:00"})
            self.assertEqual(manifest["time_range"]["engagements"], {"min": "2026-01-01T13:00:00+00:00", "max": "2026-01-01T13:00:00+00:00"})
            self.assertEqual(manifest["id_range"], {"events": {"min": 1, "max": 1}, "engagements": {"min": 9, "max": 9}})
            self.assertIn("page_id", manifest["columns"]["events"])
            self.assertIn("tab_id", manifest["columns"]["engagements"])
            self.assertEqual(manifest["members"]["events.jsonl"]["rows"], 1)
            self.assertEqual(json.loads(zipped.read("events.jsonl"))["occurred_at"], "2026-01-01T12:00:00+00:00")
            # Fixed member timestamps: nothing in the ZIP depends on "now".
            self.assertEqual({info.date_time for info in zipped.infolist()}, {(1980, 1, 1, 0, 0, 0)})
        self.assertEqual(validate_archive(payload)["record_digest"], hashlib.sha256(b"e-1\ng-1").hexdigest())

    def test_csv_is_safe_and_matches_jsonl(self) -> None:
        import csv
        from io import StringIO
        payload = make_archive(environment="sandbox", events=[_event(actor_email="  =1+1", properties={"text": "=cmd", "line": "a\nb"})])
        self.assertEqual(validate_archive(payload)["events"], 1)
        with ZipFile(BytesIO(payload)) as zipped:
            rows = list(csv.DictReader(StringIO(zipped.read("events.csv").decode("utf-8"))))
            self.assertEqual(rows[0]["actor_email"], "'  =1+1")
            self.assertIn("=cmd", rows[0]["properties"])
            members = {name: zipped.read(name) for name in zipped.namelist()}
        members["events.csv"] += b"tampered"
        output = BytesIO()
        with ZipFile(output, "w") as zipped:
            for name, data in members.items():
                zipped.writestr(name, data)
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(output.getvalue())

    def _rebuilt(self, payload: bytes, **replace: bytes) -> bytes:
        with ZipFile(BytesIO(payload)) as original:
            members = {name: original.read(name) for name in original.namelist()}
        members.update(replace)
        output = BytesIO()
        with ZipFile(output, "w") as zipped:
            for name, data in members.items():
                zipped.writestr(name, data)
        return output.getvalue()

    def _csv_rows(self, payload: bytes, member: str = "events.csv") -> tuple[list[str], list[dict]]:
        import csv
        from io import StringIO

        with ZipFile(BytesIO(payload)) as zipped:
            text = zipped.read(member).decode("utf-8")
        reader = csv.DictReader(StringIO(text, newline=""))
        rows = list(reader)
        return list(reader.fieldnames or []), rows

    def test_csv_and_jsonl_carry_the_same_records_across_awkward_values(self) -> None:
        """Unicode, commas, quotes, embedded newlines, nested JSON, nulls,
        timestamps and excluded View As fields: the CSV cell is the JSONL
        value rendered as text (JSON for nested values, ISO-8601 for
        timestamps, empty for null) and the two members always agree."""
        awkward = _event(
            id=7, event_id="e-7", actor_email="zoë, \"quoted\" <z@renphil.org>", page_route="/:nav_tab/:tab",
            properties={"text": "line one\nline two", "list": [1, "two", None], "nested": {"b": True, "a": 1.5}, "unicode": "名前 ✓"},
            target_id=None, cascade_id="c-1", occurred_at=AT.replace(microsecond=123456),
            view_as_role_key="role", view_as_scope_key="scope", view_as_context={"user_id": 4},
        )
        payload = make_archive(environment="sandbox", events=[awkward], exclude_columns=archive.VIEW_AS_COLUMNS)
        manifest = validate_archive(payload)
        fieldnames, rows = self._csv_rows(payload)
        self.assertEqual(fieldnames, manifest["columns"]["events"])
        self.assertNotIn("view_as_context", fieldnames)
        self.assertEqual(len(rows), 1)
        with ZipFile(BytesIO(payload)) as zipped:
            record = json.loads(zipped.read("events.jsonl").decode("utf-8"))
            self.assertNotIn(b"view_as", zipped.read("events.csv"))
        self.assertNotIn("view_as_context", record)
        cell = rows[0]
        self.assertEqual(cell["actor_email"], 'zoë, "quoted" <z@renphil.org>')
        self.assertEqual(json.loads(cell["properties"]), record["properties"])
        self.assertEqual(json.loads(cell["properties"])["text"], "line one\nline two")
        self.assertEqual(cell["target_id"], "")
        self.assertIsNone(record["target_id"])
        self.assertEqual(cell["occurred_at"], record["occurred_at"])
        self.assertEqual(cell["occurred_at"], "2026-01-01T12:00:00.123456+00:00")
        self.assertEqual(cell["id"], "7")
        self.assertEqual(manifest["members"]["events.csv"]["rows"], 1)
        self.assertEqual(manifest["members"]["events.csv"]["sha256"], hashlib.sha256(self._member(payload, "events.csv")).hexdigest())
        # An engagements archive carries an empty events member pair and vice versa.
        engagements_only = make_archive(environment="sandbox", engagements=[_engagement()])
        self.assertEqual(self._csv_rows(engagements_only, "events.csv"), (validate_archive(engagements_only)["columns"]["events"], []))
        self.assertEqual(len(self._csv_rows(engagements_only, "engagements.csv")[1]), 1)

    def _member(self, payload: bytes, name: str) -> bytes:
        with ZipFile(BytesIO(payload)) as zipped:
            return zipped.read(name)

    def test_empty_archive_has_header_only_csv_members_and_validates(self) -> None:
        payload = make_archive(environment="sandbox")
        manifest = validate_archive(payload)
        self.assertEqual((manifest["events"], manifest["engagements"], manifest["record_types"]), (0, 0, []))
        for member in ("events.csv", "engagements.csv"):
            fieldnames, rows = self._csv_rows(payload, member)
            self.assertEqual(rows, [])
            self.assertEqual(fieldnames, manifest["columns"][member.split(".")[0]])
            self.assertEqual(manifest["members"][member]["rows"], 0)
        self.assertEqual(self._member(payload, "events.jsonl"), b"")

    def test_csv_formula_cells_are_neutralised_including_leading_whitespace_and_control_characters(self) -> None:
        cases = {
            "=1+1": "'=1+1", "+1": "'+1", "-1": "'-1", "@SUM(A1)": "'@SUM(A1)", "|cmd": "'|cmd",
            "  =1+1": "'  =1+1", "\t=1+1": "'\t=1+1", "\r=1+1": "'\r=1+1", "\n@x": "'\n@x",
            " =1+1": "' =1+1", " +x": "' +x", "\x00=x": "'\x00=x", "\x1f": "'\x1f",
            "\t": "'\t", "plain": "plain", " plain": " plain", "1-2": "1-2", "e=mc2": "e=mc2", "": "",
        }
        for value, expected in cases.items():
            with self.subTest(repr(value)):
                self.assertEqual(archive._csv_cell(value), expected)
        # Alternating whitespace and control characters before the formula character.
        for value in (" \x00 =1+1", "\x00 \x00\t=cmd", " \x1f  -1", " \x7f @x", "\t \x00 |x"):
            with self.subTest(repr(value)):
                self.assertEqual(archive._csv_cell(value), "'" + value)
        # Whitespace and control characters followed by ordinary text are not a formula and stay as they are.
        self.assertEqual(archive._csv_cell(" \x00 safe"), " \x00 safe")
        self.assertEqual(archive._csv_cell(None), "")
        self.assertEqual(archive._csv_cell(-5), "'-5")  # an honest negative number is prefixed too; the JSONL keeps -5
        self.assertEqual(archive._csv_cell({"k": "=v"}), '{"k":"=v"}')  # nested JSON starts with `{`, never a formula
        self.assertEqual(archive._csv_cell(AT), "2026-01-01T12:00:00+00:00")
        payload = make_archive(environment="sandbox", events=[_event(actor_email="=HYPERLINK(\"x\")", target_id="\t@cmd", properties={"p": "=x"})])
        _, rows = self._csv_rows(payload)
        self.assertEqual((rows[0]["actor_email"], rows[0]["target_id"]), ("'=HYPERLINK(\"x\")", "'\t@cmd"))
        self.assertEqual(rows[0]["properties"], '{"p":"=x"}')
        with ZipFile(BytesIO(payload)) as zipped:
            self.assertEqual(json.loads(zipped.read("events.jsonl"))["actor_email"], "=HYPERLINK(\"x\")")  # JSONL untouched
        validate_archive(payload)

    def test_tampered_csv_members_are_rejected_and_a_v2_archive_with_csv_members_is_not_a_v2_archive(self) -> None:
        payload = make_archive(environment="sandbox", events=[_event(), _event(id=2, event_id="e-2")])
        with ZipFile(BytesIO(payload)) as zipped:
            manifest = json.loads(zipped.read("manifest.json"))
            csv_bytes = zipped.read("events.csv")
        header, first, second = csv_bytes.decode("utf-8").split("\n")[:3]
        columns = header.split(",")
        swapped = (header + "\n" + second + "\n" + first + "\n").encode("utf-8")
        reordered_header = ",".join(reversed(columns)) + "\n" + "\n".join(csv_bytes.decode("utf-8").split("\n")[1:])
        edited = csv_bytes.replace(b"e-2", b"e-3")
        dropped = (header + "\n" + first + "\n").encode("utf-8")
        for name, replacement in {
            "row order": swapped, "header order": reordered_header.encode("utf-8"), "cell edited": edited,
            "row dropped": dropped, "appended": csv_bytes + b"extra\n", "empty": b"",
        }.items():
            with self.subTest(name), self.assertRaises(ArchiveIntegrityError):
                validate_archive(self._rebuilt(payload, **{"events.csv": replacement}))
        # A consistent CSV with a manifest that lies about it is still refused.
        for field in ("sha256", "bytes", "rows"):
            lying = json.loads(json.dumps(manifest))
            lying["members"]["events.csv"][field] = 0 if field != "sha256" else "0" * 64
            with self.subTest(field), self.assertRaises(ArchiveIntegrityError):
                validate_archive(self._rebuilt(payload, **{"manifest.json": json.dumps(lying).encode()}))
        # Extra or missing members break the format either way.
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(self._rebuilt(payload, **{"extra.txt": b"x"}))
        # A record carrying a column the manifest declares omitted is refused
        # (omitted means omitted in the manifest, the CSV and the JSONL).
        omitting = make_archive(environment="sandbox", events=[_event(view_as_role_key="role")], exclude_columns=["view_as_role_key"])
        with ZipFile(BytesIO(omitting)) as zipped:
            self.assertNotIn(b"view_as_role_key", zipped.read("events.jsonl"))
            smuggled = json.loads(zipped.read("events.jsonl"))
            omitting_manifest = json.loads(zipped.read("manifest.json"))
        smuggled["view_as_role_key"] = "role"
        line = (json.dumps(smuggled, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        omitting_manifest["members"]["events.jsonl"].update({"sha256": hashlib.sha256(line).hexdigest(), "bytes": len(line)})
        with self.assertRaises(ArchiveIntegrityError) as caught:
            validate_archive(self._rebuilt(omitting, **{"events.jsonl": line, "manifest.json": json.dumps(omitting_manifest).encode()}))
        self.assertIn("declares omitted", str(caught.exception))
        v2_manifest = {**manifest, "format_version": 2}
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(self._rebuilt(payload, **{"manifest.json": json.dumps(v2_manifest).encode()}))

    def test_legacy_v2_archive_remains_readable(self) -> None:
        payload = make_archive(environment="sandbox", events=[_event()])
        with ZipFile(BytesIO(payload)) as zipped:
            members = {name: zipped.read(name) for name in zipped.namelist() if not name.endswith(".csv")}
        manifest = json.loads(members["manifest.json"])
        manifest["format_version"] = 2
        manifest["members"] = {key: value for key, value in manifest["members"].items() if not key.endswith(".csv")}
        members["manifest.json"] = json.dumps(manifest).encode()
        output = BytesIO()
        with ZipFile(output, "w") as zipped:
            for name, data in members.items():
                zipped.writestr(name, data)
        self.assertEqual(validate_archive(output.getvalue())["format_version"], 2)

    def test_archive_bytes_are_deterministic_for_the_same_rows(self) -> None:
        rows = [_event(id=i, event_id=f"e-{i}", properties={"b": 1, "a": 2}) for i in range(3)]
        first = make_archive(environment="sandbox", events=rows)
        second = make_archive(environment="sandbox", events=[dict(row) for row in rows])
        self.assertEqual(first, second)
        self.assertNotEqual(first, make_archive(environment="sandbox", events=rows[:2]))

    def test_archive_rejects_cross_environment_naive_timestamps_and_run_specific_values(self) -> None:
        with self.assertRaises(ValueError):
            make_archive(environment="sandbox", events=[_event(environment="production")])
        with self.assertRaises(ValueError):
            make_archive(environment="sandbox", events=[_event(occurred_at=datetime(2026, 1, 1))])
        with self.assertRaises(ValueError):
            make_archive(environment="unconfigured", events=[])

    def test_validate_archive_detects_tampering_and_wrong_format(self) -> None:
        payload = make_archive(environment="sandbox", events=[_event(), _event(id=2, event_id="e-2")])
        self.assertEqual(validate_archive(payload)["events"], 2)
        with self.assertRaises(ArchiveIntegrityError):
            validate_archive(b"not a zip")

        def rebuilt(**replace: bytes) -> bytes:
            with ZipFile(BytesIO(payload)) as original:
                members = {name: original.read(name) for name in original.namelist()}
            members.update(replace)
            output = BytesIO()
            with ZipFile(output, "w") as zipped:
                for name, data in members.items():
                    zipped.writestr(name, data)
            return output.getvalue()

        with ZipFile(BytesIO(payload)) as original:
            manifest = json.loads(original.read("manifest.json"))
        tampered_rows = b'{"event_id":"e-1","environment":"sandbox"}\n{"event_id":"e-2","environment":"sandbox"}\n'
        for name, data in {
            "member edited": {"events.jsonl": tampered_rows},
            "row dropped": {"events.jsonl": b""},
            "manifest version": {"manifest.json": json.dumps({**manifest, "format_version": 1}).encode()},
            "manifest environment": {"manifest.json": json.dumps({**manifest, "environment": "production"}).encode()},
            "manifest count": {"manifest.json": json.dumps({**manifest, "events": 1}).encode()},
        }.items():
            with self.subTest(name), self.assertRaises(ArchiveIntegrityError):
                validate_archive(rebuilt(**data))

    def test_omitted_columns_are_recorded_in_the_manifest(self) -> None:
        row = _event(view_as_role_key="role", view_as_scope_key="scope", view_as_context={"user_id": 4})
        full = validate_archive(make_archive(environment="sandbox", events=[row]))
        self.assertIn("view_as_context", full["columns"]["events"])
        self.assertEqual(full["omitted_columns"], [])
        payload = make_archive(environment="sandbox", events=[{k: v for k, v in row.items() if k not in archive.VIEW_AS_COLUMNS}],
                               exclude_columns=archive.VIEW_AS_COLUMNS)
        manifest = validate_archive(payload)
        self.assertEqual(manifest["omitted_columns"], ["view_as_context", "view_as_role_key", "view_as_scope_key"])
        self.assertNotIn("view_as_context", manifest["columns"]["events"])
        self.assertNotIn("view_as_context", manifest["columns"]["engagements"])
        with ZipFile(BytesIO(payload)) as zipped:
            self.assertNotIn(b"view_as", zipped.read("events.jsonl"))

    def test_call_with_deadline_returns_raises_or_times_out(self) -> None:
        import threading
        from app.services.product_analytics_archive import ArchiveTimeoutError, call_with_deadline

        self.assertEqual(call_with_deadline(lambda: 42, 5, what="x"), 42)
        self.assertEqual(call_with_deadline(lambda: 42, None, what="x"), 42)
        with self.assertRaises(KeyError):
            call_with_deadline(lambda: {}["missing"], 5, what="x")
        gate = threading.Event()
        with self.assertRaises(ArchiveTimeoutError) as caught:
            call_with_deadline(gate.wait, 0.2, what="upload (PUT)")
        self.assertIn("upload (PUT) did not complete within 0.2 s", str(caught.exception))
        gate.set()

    def test_archive_key_layout_contains_no_personal_data(self) -> None:
        key = archive_key(prefix="hub-product-analytics", environment="sandbox", table="events",
                          first_day=date(2025, 3, 9), first_id=10, last_id=5009, digest="abcdef0123456789ffff")
        self.assertEqual(key, "hub-product-analytics/sandbox/events/v3/2025/2025-03-09_10-5009_abcdef0123456789.zip")


class VerifiedUploadTests(unittest.TestCase):
    def test_ensure_archived_uploads_once_then_recognises_the_verified_object(self) -> None:
        store = MemoryArchiveStore()
        payload = make_archive(environment="sandbox", events=[_event()])
        digest, uploaded = ensure_archived(store, key="k/batch.zip", payload=payload)
        self.assertEqual((digest, uploaded), (hashlib.sha256(payload).hexdigest(), True))
        self.assertEqual(store.metadata["k/batch.zip"], {"sha256": digest, "format-version": "3"})
        self.assertEqual(ensure_archived(store, key="k/batch.zip", payload=payload), (digest, False))
        self.assertEqual([call for call, _ in store.calls], ["head", "put", "get", "head", "get"])

    def test_a_key_taken_between_head_and_put_is_never_overwritten(self) -> None:
        """Another writer lands right after our HEAD. The conditional PUT is
        refused by the store, and the other writer's object is read back:
        identical bytes count as archived, different bytes stop the run."""
        payload = make_archive(environment="sandbox", events=[_event()])
        digest = hashlib.sha256(payload).hexdigest()

        class RacingStore(MemoryArchiveStore):
            def __init__(self, other: bytes) -> None:
                super().__init__()
                self.other = other

            def head(self, key):
                size = super().head(key)
                if size is None:
                    MemoryArchiveStore.put(self, key, self.other)  # the other writer
                return size

        identical = RacingStore(payload)
        self.assertEqual(ensure_archived(identical, key="k", payload=payload), (digest, False))
        self.assertEqual([call for call, _ in identical.calls], ["head", "put", "put", "get"])
        different = RacingStore(b"someone else's bytes")
        with self.assertRaises(ArchiveIntegrityError):
            ensure_archived(different, key="k", payload=payload)
        self.assertEqual(different.objects["k"], b"someone else's bytes")

    def test_concurrent_writers_of_one_key_upload_once_and_all_verify(self) -> None:
        import threading
        import time

        payload = make_archive(environment="sandbox", events=[_event(id=i, event_id=f"e-{i}") for i in range(50)])
        digest = hashlib.sha256(payload).hexdigest()

        class SlowStore(MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                time.sleep(0.02)
                super().put(key, data, metadata, if_none_match=if_none_match)

        store = SlowStore()
        results: list[tuple[str, bool]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def writer() -> None:
            barrier.wait()
            outcome = ensure_archived(store, key="k", payload=payload)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=writer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 6)
        self.assertEqual({result[0] for result in results}, {digest})
        self.assertEqual(sum(1 for _, uploaded in results if uploaded), 1)
        self.assertEqual(store.objects["k"], payload)

    def test_ensure_archived_never_overwrites_a_different_object(self) -> None:
        store = MemoryArchiveStore()
        store.put("k/batch.zip", b"older different bytes")
        with self.assertRaises(ArchiveIntegrityError):
            ensure_archived(store, key="k/batch.zip", payload=b"new bytes")
        self.assertEqual(store.objects["k/batch.zip"], b"older different bytes")

    def test_upload_requires_successful_verified_readback(self) -> None:
        payload = b"archive bytes"

        class CorruptStore(MemoryArchiveStore):
            def get(self, key):
                return b"corrupt"

        class FailingStore(MemoryArchiveStore):
            def put(self, key, data, metadata=None, *, if_none_match=False):
                raise OSError("upload failed")

        with self.assertRaises(ArchiveIntegrityError):
            ensure_archived(CorruptStore(), key="k", payload=payload)
        with self.assertRaises(OSError):
            ensure_archived(FailingStore(), key="k", payload=payload)
        store = MemoryArchiveStore()
        self.assertEqual(put_and_verify(store, key="k", payload=payload), hashlib.sha256(payload).hexdigest())
        with self.assertRaises(ArchiveIntegrityError, msg="verification failed"):
            put_and_verify(CorruptStore(), key="k", payload=payload)
        with self.assertRaises(ValueError):
            ensure_archived(store, key="", payload=payload)


class StorageConfigTests(unittest.TestCase):
    GOOD = dict(R2_ACCOUNT_ID="0123456789abcdef0123456789abcdef", R2_ACCESS_KEY_ID="AKIAEXAMPLEKEYID",
                R2_SECRET_ACCESS_KEY="s3cr3t-value-that-is-long-enough", R2_BUCKET_NAME="renphil-hub-analytics-archive-bucket",
                PRODUCT_ANALYTICS_ARCHIVE_PREFIX="hub-product-analytics")

    def test_valid_settings_build_the_endpoint_and_hide_secrets(self) -> None:
        config = R2Config.from_settings(SimpleNamespace(**self.GOOD))
        self.assertEqual(config.endpoint_url, "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com")
        self.assertEqual(config.describe(), "bucket=renphil-hub-analytics-archive-bucket prefix=hub-product-analytics")
        for secret in (self.GOOD["R2_SECRET_ACCESS_KEY"], self.GOOD["R2_ACCESS_KEY_ID"], self.GOOD["R2_ACCOUNT_ID"]):
            self.assertNotIn(secret, repr(config))
            self.assertNotIn(secret, str(config))
            self.assertNotIn(secret, config.describe())

    def test_invalid_settings_are_refused_naming_the_variable_only(self) -> None:
        cases = {
            "missing": ({"R2_SECRET_ACCESS_KEY": "", "R2_BUCKET_NAME": None}, "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME"),
            "account": ({"R2_ACCOUNT_ID": "not valid!"}, "R2_ACCOUNT_ID"),
            "bucket": ({"R2_BUCKET_NAME": "Bad_Bucket"}, "R2_BUCKET_NAME"),
            "secret": ({"R2_SECRET_ACCESS_KEY": "short"}, "R2_SECRET_ACCESS_KEY"),
            "prefix": ({"PRODUCT_ANALYTICS_ARCHIVE_PREFIX": "../escape"}, "PRODUCT_ANALYTICS_ARCHIVE_PREFIX"),
        }
        for name, (overrides, expected) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ArchiveConfigError) as caught:
                    R2Config.from_settings(SimpleNamespace(**{**self.GOOD, **overrides}))
                self.assertIn(expected, str(caught.exception))
                for secret in (self.GOOD["R2_SECRET_ACCESS_KEY"], self.GOOD["R2_ACCESS_KEY_ID"], self.GOOD["R2_ACCOUNT_ID"]):
                    self.assertNotIn(secret, str(caught.exception))

    def test_r2_store_builds_a_scoped_s3_client_without_logging_credentials(self) -> None:
        config = R2Config.from_settings(SimpleNamespace(**self.GOOD))
        with patch("boto3.client") as client, self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("app.services.product_analytics_archive").debug("building store")
            R2ArchiveStore(config)
        _, kwargs = client.call_args
        self.assertEqual((kwargs["endpoint_url"], kwargs["region_name"]), (config.endpoint_url, "auto"))
        self.assertEqual(kwargs["aws_secret_access_key"], self.GOOD["R2_SECRET_ACCESS_KEY"])
        self.assertEqual(kwargs["config"].signature_version, "s3v4")
        self.assertEqual(kwargs["config"].request_checksum_calculation, "when_required")
        self.assertNotIn(self.GOOD["R2_SECRET_ACCESS_KEY"], "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
