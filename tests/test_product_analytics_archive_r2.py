"""The R2 store against an S3-compatible mock (moto). Never touches a real bucket."""

from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from app.services import product_analytics_archive as archive

try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover - optional test dependency
    boto3 = None
    mock_aws = None

CONFIG = archive.R2Config.from_settings(SimpleNamespace(
    ANALYTICS_ARCHIVE_R2_ACCOUNT_ID="0123456789abcdef0123456789abcdef", ANALYTICS_ARCHIVE_R2_ACCESS_KEY_ID="AKIAEXAMPLEKEYID",
    ANALYTICS_ARCHIVE_R2_SECRET_ACCESS_KEY="s3cr3t-value-that-is-long-enough", ANALYTICS_ARCHIVE_R2_BUCKET_NAME="renphil-hub-analytics-archive-bucket",
    PRODUCT_ANALYTICS_ARCHIVE_PREFIX="hub-product-analytics",
))


@unittest.skipUnless(mock_aws, "moto is not installed")
class R2StoreMockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        # moto intercepts the standard S3 endpoint; the production client is
        # built against the R2 endpoint, so the mocked client is injected.
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        self.client.create_bucket(Bucket=CONFIG.bucket)
        self.store = archive.R2ArchiveStore(CONFIG, client=self.client)

    def test_put_head_get_and_probe(self) -> None:
        payload = archive.make_archive(environment="sandbox", events=[])
        self.assertIsNone(self.store.head("hub-product-analytics/sandbox/events/v2/2025/x.zip"))
        self.store.put("hub-product-analytics/sandbox/events/v2/2025/x.zip", payload, {"sha256": "abc"})
        self.assertEqual(self.store.head("hub-product-analytics/sandbox/events/v2/2025/x.zip"), len(payload))
        self.assertEqual(self.store.get("hub-product-analytics/sandbox/events/v2/2025/x.zip"), payload)
        head = self.client.head_object(Bucket=CONFIG.bucket, Key="hub-product-analytics/sandbox/events/v2/2025/x.zip")
        self.assertEqual((head["ContentType"], head["Metadata"]), ("application/zip", {"sha256": "abc"}))
        self.store.probe()

    def test_ensure_archived_is_idempotent_and_verified_against_the_mock(self) -> None:
        payload = archive.make_archive(environment="sandbox", events=[{"id": 1, "event_id": "e", "environment": "sandbox"}])
        key = "hub-product-analytics/sandbox/events/v2/2025/2025-01-01_1-1_abc.zip"
        digest, uploaded = archive.ensure_archived(self.store, key=key, payload=payload)
        self.assertEqual((digest, uploaded), (hashlib.sha256(payload).hexdigest(), True))
        self.assertEqual(archive.ensure_archived(self.store, key=key, payload=payload), (digest, False))
        with self.assertRaises(archive.ArchiveIntegrityError):
            archive.ensure_archived(self.store, key=key, payload=payload + b"x")
        self.assertEqual(archive.validate_archive(self.store.get(key))["events"], 1)

    def test_a_staged_upload_is_promoted_by_a_server_side_copy_that_keeps_its_metadata(self) -> None:
        """The job's path against the S3 API mock: PUT to the staging key,
        CopyObject to the final key with `MetadataDirective=COPY`, readback;
        the staging object is deleted afterwards and only a staging key may
        be deleted. This proves the client wiring (moto implements
        CopyObject as S3 does); CopyObject against the real R2 service is in
        the runbook's probe plan."""
        payload = archive.make_archive(environment="sandbox", events=[{"id": 1, "event_id": "e", "environment": "sandbox"}])
        key = "hub-product-analytics/sandbox/events/v3/2025/2025-01-01_1-1_abc.zip"
        staged = archive.staging_key(prefix="hub-product-analytics", environment="sandbox")
        digest, uploaded = archive.ensure_archived(self.store, key=key, payload=payload, metadata={"registry-id": "abc"}, staging_key=staged)
        self.assertEqual((digest, uploaded), (hashlib.sha256(payload).hexdigest(), True))
        head = self.client.head_object(Bucket=CONFIG.bucket, Key=key)
        self.assertEqual((head["ContentType"], head["Metadata"]), ("application/zip", {"registry-id": "abc", "sha256": digest, "format-version": "3"}))
        self.assertEqual(self.store.get(key), payload)
        self.assertEqual(self.store.head(staged), len(payload))  # the job deletes it after its commit
        self.store.delete_staging(staged)
        self.assertIsNone(self.store.head(staged))
        self.store.delete_staging(staged)  # absent: not an error
        again = archive.staging_key(prefix="hub-product-analytics", environment="sandbox")
        self.assertEqual(archive.ensure_archived(self.store, key=key, payload=payload, staging_key=again), (digest, False))
        self.assertIsNone(self.store.head(again))  # recognised: nothing was staged
        with self.assertRaises(ValueError):
            self.store.delete_staging(key)
        self.assertEqual(self.store.head(key), len(payload))

    def test_conditional_put_is_refused_by_the_s3_api_mock(self) -> None:
        """moto enforces `If-None-Match: *` with HTTP 412, as S3 and R2
        document. This proves the client wiring, not the real R2 service
        (see the runbook's probe plan)."""
        self.store.put("k", b"first", if_none_match=True)
        with self.assertRaises(archive.ArchiveObjectExistsError):
            self.store.put("k", b"second", if_none_match=True)
        self.assertEqual(self.store.get("k"), b"first")
        payload = archive.make_archive(environment="sandbox", events=[{"id": 2, "event_id": "f", "environment": "sandbox"}])
        self.client.put_object(Bucket=CONFIG.bucket, Key="taken", Body=b"someone else's bytes")
        original_stat = self.store.stat
        # Simulate the key being taken between our HEAD and our PUT.
        self.store.stat = lambda key: None
        try:
            with self.assertRaises(archive.ArchiveIntegrityError):
                archive.ensure_archived(self.store, key="taken", payload=payload)
        finally:
            self.store.stat = original_stat
        self.assertEqual(self.store.get("taken"), b"someone else's bytes")

    def test_conditional_copy_sends_r2s_destination_header_per_call_and_maps_a_412(self) -> None:
        """moto implements CopyObject but NOT R2's beta
        `cf-copy-destination-if-none-match` header, so the mock cannot prove
        the refusal. What it proves: the header is on the wire exactly when
        asked (`*`), absent otherwise, isolated per call; and a 412 from the
        service is mapped to `ArchiveObjectExistsError` only for a
        conditional copy. The real service is the probe plan's business."""
        from botocore.exceptions import ClientError

        scope = "hub-product-analytics/sandbox/"
        seen: list[dict] = []
        self.client.meta.events.register("before-send.s3.CopyObject", lambda request, **_: seen.append({k.lower(): (v.decode() if isinstance(v, bytes) else v) for k, v in request.headers.items()}))
        self.store.put(scope + "_staging/s.zip", b"bytes", {"registry-id": "abc"})
        self.store.copy(scope + "_staging/s.zip", scope + "events/v3/2025/a.zip", if_none_match=True)
        self.assertEqual(seen[-1].get(archive.R2ArchiveStore.COPY_DESTINATION_IF_NONE_MATCH), "*")
        self.assertEqual(seen[-1].get("x-amz-metadata-directive"), "COPY")
        self.store.copy(scope + "_staging/s.zip", scope + "events/v3/2025/b.zip")
        self.assertNotIn(archive.R2ArchiveStore.COPY_DESTINATION_IF_NONE_MATCH, seen[-1])
        self.assertEqual(self.store.stat(scope + "events/v3/2025/a.zip").metadata, {"registry-id": "abc"})
        # Simulated, not evidence: moto ignores the header, so a conditional
        # copy onto the existing key "succeeds" here. Against R2 this must be
        # a 412 — the probe step that is still open.
        self.store.copy(scope + "_staging/s.zip", scope + "events/v3/2025/a.zip", if_none_match=True)
        self.assertEqual(seen[-1].get(archive.R2ArchiveStore.COPY_DESTINATION_IF_NONE_MATCH), "*")

        class Refusing:
            meta = SimpleNamespace(events=SimpleNamespace(register=lambda *args, **kwargs: None))
            calls = 0

            def copy_object(self, **kwargs):
                self.calls += 1
                raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "At least one of the pre-conditions you specified did not hold"},
                                   "ResponseMetadata": {"HTTPStatusCode": 412}}, "CopyObject")

        refusing = archive.R2ArchiveStore(CONFIG, client=Refusing())
        with self.assertRaises(archive.ArchiveObjectExistsError):
            refusing.copy(scope + "_staging/s.zip", scope + "events/v3/2025/a.zip", if_none_match=True)
        with self.assertRaises(ClientError):  # an unconditional copy never asked for a precondition: not an "exists" outcome
            refusing.copy(scope + "_staging/s.zip", scope + "events/v3/2025/a.zip")

    def test_ensure_archived_verifies_a_competing_object_after_a_412_against_the_mock(self) -> None:
        """The mock plus a thin shim that refuses the conditional copy with
        412 when the destination exists — R2's documented behaviour, which
        moto lacks — so that the full job path (HEAD absent → staged PUT →
        refused COPY → HEAD → GET → verdict) runs through the S3 client."""
        from botocore.exceptions import ClientError

        scope = "hub-product-analytics/sandbox/"
        key = scope + "events/v3/2025/2025-01-01_1-1_abc.zip"
        payload = archive.make_archive(environment="sandbox", events=[{"id": 1, "event_id": "e", "environment": "sandbox"}])
        digest = hashlib.sha256(payload).hexdigest()
        stamped = {"registry-id": "abc", "sha256": digest, "format-version": "3"}
        mock_client = self.client

        class DestinationConditionShim:
            def __getattr__(self, name):
                return getattr(mock_client, name)

            def copy_object(self, **kwargs):
                try:
                    mock_client.head_object(Bucket=kwargs["Bucket"], Key=kwargs["Key"])
                except ClientError:
                    return mock_client.copy_object(**kwargs)
                raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "precondition failed"},
                                   "ResponseMetadata": {"HTTPStatusCode": 412}}, "CopyObject")

        store = archive.R2ArchiveStore(CONFIG, client=DestinationConditionShim())
        original_stat = store.stat

        def competing(other: bytes, metadata: dict):
            """The key appears right after our HEAD: a writer committed `other`."""
            def stat(k):
                info = original_stat(k)
                if info is None:
                    mock_client.put_object(Bucket=CONFIG.bucket, Key=k, Body=other, Metadata=metadata)
                return info
            return stat

        store.stat = competing(payload, stamped)
        staged = archive.staging_key(prefix="hub-product-analytics", environment="sandbox")
        self.assertEqual(archive.ensure_archived(store, key=key, payload=payload, metadata={"registry-id": "abc"}, staging_key=staged), (digest, False))
        self.assertEqual(store.get(key), payload)
        mock_client.delete_object(Bucket=CONFIG.bucket, Key=key)
        store.stat = competing(b"someone else's bytes", stamped)
        with self.assertRaises(archive.ArchiveIntegrityError):
            archive.ensure_archived(store, key=key, payload=payload, metadata={"registry-id": "abc"}, staging_key=staged + ".2")
        self.assertEqual(store.get(key), b"someone else's bytes")
        mock_client.delete_object(Bucket=CONFIG.bucket, Key=key)
        store.stat = competing(payload, {**stamped, "registry-id": "other"})
        with self.assertRaises(archive.ArchiveIntegrityError):
            archive.ensure_archived(store, key=key, payload=payload, metadata={"registry-id": "abc"}, staging_key=staged + ".3")
        self.assertEqual(store.stat(key).metadata["registry-id"], "other")

    def test_reconcile_store_lists_inspects_and_deletes_staging_objects_only(self) -> None:
        """No R2 store has an operation that deletes an object under a final
        key: the unsafe HEAD-then-DELETE is not expressible, not merely
        disabled. Staging objects are deleted unconditionally — safe because
        nothing at a staging key is ever an archive."""
        scope = "hub-product-analytics/sandbox/"
        reconciler = archive.R2ReconcileStore(CONFIG, client=self.client)
        self.store.put(scope + "events/v3/2025/a.zip", b"aaaa", {"registry-id": "abc", "sha256": "x"})
        self.store.put(scope + "events/v3/2025/b.zip", b"bbbbbb")
        self.store.put(scope + "_staging/c.zip", b"cc", {"registry-id": "abc"})
        self.store.put("hub-product-analytics/production/events/v3/2025/d.zip", b"dd")
        listed = reconciler.list(scope)
        self.assertEqual([(item.key, item.size) for item in listed],
                         [(scope + "_staging/c.zip", 2), (scope + "events/v3/2025/a.zip", 4), (scope + "events/v3/2025/b.zip", 6)])
        self.assertEqual(listed[0].metadata, {})  # listings carry no user metadata
        stat = reconciler.stat(scope + "events/v3/2025/a.zip")
        self.assertEqual((stat.key, stat.size, stat.etag), (listed[1].key, listed[1].size, listed[1].etag))
        self.assertEqual(stat.metadata, {"registry-id": "abc", "sha256": "x"})
        self.assertIsNotNone(stat.last_modified.tzinfo)
        self.assertIsNone(reconciler.stat(scope + "events/v3/2025/missing.zip"))
        self.assertFalse(hasattr(self.store, "delete") or hasattr(self.store, "list"))  # the writer's class cannot list; no store can delete a final key
        self.assertFalse(hasattr(reconciler, "delete"))
        for final in (scope + "events/v3/2025/a.zip", scope + "events/v3/2025/b.zip"):
            with self.assertRaises(ValueError):
                reconciler.delete_staging(final)
            with self.assertRaises(ValueError):
                self.store.delete_staging(final)
            self.assertIsNotNone(reconciler.stat(final))
        with self.assertRaises(archive.ArchiveConfigError):
            reconciler.put(scope + "events/v3/2025/a.zip", b"replacement")
        with self.assertRaises(archive.ArchiveConfigError):
            reconciler.copy(scope + "_staging/c.zip", scope + "events/v3/2025/e.zip")
        self.assertEqual(self.store.get(scope + "events/v3/2025/a.zip"), b"aaaa")
        self.assertIsNone(reconciler.stat(scope + "events/v3/2025/e.zip"))
        reconciler.delete_staging(scope + "_staging/c.zip")
        self.assertIsNone(reconciler.stat(scope + "_staging/c.zip"))
        reconciler.delete_staging(scope + "_staging/c.zip")  # absent: not an error
        self.assertEqual(len(reconciler.list("hub-product-analytics/")), 3)

    def test_reconcile_store_credentials_are_a_separate_identity(self) -> None:
        settings = SimpleNamespace(ANALYTICS_ARCHIVE_R2_ACCOUNT_ID=CONFIG.account_id, ANALYTICS_ARCHIVE_R2_BUCKET_NAME=CONFIG.bucket, PRODUCT_ANALYTICS_ARCHIVE_PREFIX=CONFIG.prefix,
                                   ANALYTICS_ARCHIVE_R2_ACCESS_KEY_ID="writer-key", ANALYTICS_ARCHIVE_R2_SECRET_ACCESS_KEY="writer-secret-long-enough-xx",
                                   ANALYTICS_ARCHIVE_R2_RECONCILE_ACCESS_KEY_ID="reconcile-key", ANALYTICS_ARCHIVE_R2_RECONCILE_SECRET_ACCESS_KEY="reconcile-secret-long-enough")
        writer = archive.R2Config.from_settings(settings)
        reconciler = archive.R2Config.from_settings(settings, credentials=archive.RECONCILE_CREDENTIALS)
        self.assertEqual((writer.role, writer.access_key_id), ("writer", "writer-key"))
        self.assertEqual((reconciler.role, reconciler.access_key_id), ("reconcile", "reconcile-key"))
        with self.assertRaises(archive.ArchiveConfigError) as caught:
            archive.R2Config.from_settings(SimpleNamespace(**{**vars(settings), "ANALYTICS_ARCHIVE_R2_RECONCILE_SECRET_ACCESS_KEY": ""}),
                                           credentials=archive.RECONCILE_CREDENTIALS)
        self.assertIn("ANALYTICS_ARCHIVE_R2_RECONCILE_SECRET_ACCESS_KEY", str(caught.exception))
        self.assertNotIn("reconcile-secret", str(caught.exception))

    def test_cli_delete_refuses_before_any_storage_call_when_configuration_is_incomplete(self) -> None:
        """With the test environment's placeholder settings (no reconciliation
        credentials) `--delete` is refused before a client is built; the
        removed `--delete-reviewed` flag is an argument error."""
        from scripts import reconcile_product_analytics_archives as reconcile_job

        lines = []
        self.assertEqual(reconcile_job.run(["--delete"], out=lines.append), reconcile_job.EXIT_CONFIG)
        self.assertTrue(lines[0].startswith("reconcile refused:") and lines[0].endswith("Nothing was touched."), lines)
        with self.assertRaises(SystemExit):
            reconcile_job.run(["--delete-reviewed", "hub-product-analytics/sandbox/example.zip"], out=lines.append)

    def test_probe_fails_for_a_missing_bucket(self) -> None:
        missing = archive.R2ArchiveStore(archive.R2Config(account_id=CONFIG.account_id, access_key_id="k", secret_access_key="s",
                                                           bucket="renphil-hub-analytics-archive-missing"), client=self.client)
        from botocore.exceptions import ClientError

        with self.assertRaises(ClientError):
            missing.probe()
        self.assertIsNone(missing.head("anything"))


if __name__ == "__main__":
    unittest.main()
