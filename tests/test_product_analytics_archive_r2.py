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
    R2_ACCOUNT_ID="0123456789abcdef0123456789abcdef", R2_ACCESS_KEY_ID="AKIAEXAMPLEKEYID",
    R2_SECRET_ACCESS_KEY="s3cr3t-value-that-is-long-enough", R2_BUCKET_NAME="renphil-hub-analytics-archive-bucket",
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
        original_head = self.store.head
        # Simulate the key being taken between our HEAD and our PUT.
        self.store.head = lambda key: None
        try:
            with self.assertRaises(archive.ArchiveIntegrityError):
                archive.ensure_archived(self.store, key="taken", payload=payload)
        finally:
            self.store.head = original_head
        self.assertEqual(self.store.get("taken"), b"someone else's bytes")

    def test_list_stat_with_metadata_and_unconditional_delete_against_the_mock(self) -> None:
        """The reconciler's calls. The delete is unconditional on purpose: the
        real R2 accepted a DeleteObject with a wrong If-Match and deleted the
        object (probe of 2026-11-04; Cloudflare's S3 page lists no
        precondition for DeleteObject), so the code sends none and relies on
        the registry re-check under the job lock and the HEAD identity check
        instead — neither of which this mock can strengthen."""
        scope = "hub-product-analytics/sandbox/"
        reconciler = archive.R2ReconcileStore(CONFIG, client=self.client)
        self.store.put(scope + "events/v3/2025/a.zip", b"aaaa", {"registry-id": "abc", "sha256": "x"})
        self.store.put(scope + "events/v3/2025/b.zip", b"bbbbbb")
        self.store.put("hub-product-analytics/production/events/v3/2025/c.zip", b"cc")
        listed = reconciler.list(scope)
        self.assertEqual([(item.key, item.size) for item in listed], [(scope + "events/v3/2025/a.zip", 4), (scope + "events/v3/2025/b.zip", 6)])
        self.assertEqual(listed[0].metadata, {})  # listings carry no user metadata
        stat = reconciler.stat(scope + "events/v3/2025/a.zip")
        self.assertEqual((stat.key, stat.size, stat.etag), (listed[0].key, listed[0].size, listed[0].etag))
        self.assertEqual(stat.metadata, {"registry-id": "abc", "sha256": "x"})
        self.assertIsNotNone(stat.last_modified.tzinfo)
        self.assertIsNone(reconciler.stat(scope + "events/v3/2025/missing.zip"))
        self.assertFalse(hasattr(self.store, "delete") or hasattr(self.store, "list"))  # the writer's class cannot list or delete
        reconciler.delete(scope + "events/v3/2025/a.zip")
        self.assertIsNone(reconciler.stat(scope + "events/v3/2025/a.zip"))
        reconciler.delete(scope + "events/v3/2025/a.zip")  # already gone: S3 answers success (idempotent)
        self.assertEqual(len(reconciler.list("hub-product-analytics/")), 2)

    def test_reconcile_store_credentials_are_a_separate_identity(self) -> None:
        settings = SimpleNamespace(R2_ACCOUNT_ID=CONFIG.account_id, R2_BUCKET_NAME=CONFIG.bucket, PRODUCT_ANALYTICS_ARCHIVE_PREFIX=CONFIG.prefix,
                                   R2_ACCESS_KEY_ID="writer-key", R2_SECRET_ACCESS_KEY="writer-secret-long-enough-xx",
                                   R2_RECONCILE_ACCESS_KEY_ID="reconcile-key", R2_RECONCILE_SECRET_ACCESS_KEY="reconcile-secret-long-enough")
        writer = archive.R2Config.from_settings(settings)
        reconciler = archive.R2Config.from_settings(settings, credentials=archive.RECONCILE_CREDENTIALS)
        self.assertEqual((writer.role, writer.access_key_id), ("writer", "writer-key"))
        self.assertEqual((reconciler.role, reconciler.access_key_id), ("reconcile", "reconcile-key"))
        with self.assertRaises(archive.ArchiveConfigError) as caught:
            archive.R2Config.from_settings(SimpleNamespace(**{**vars(settings), "R2_RECONCILE_SECRET_ACCESS_KEY": ""}),
                                           credentials=archive.RECONCILE_CREDENTIALS)
        self.assertIn("R2_RECONCILE_SECRET_ACCESS_KEY", str(caught.exception))
        self.assertNotIn("reconcile-secret", str(caught.exception))

    def test_probe_fails_for_a_missing_bucket(self) -> None:
        missing = archive.R2ArchiveStore(archive.R2Config(account_id=CONFIG.account_id, access_key_id="k", secret_access_key="s",
                                                           bucket="renphil-hub-analytics-archive-missing"), client=self.client)
        from botocore.exceptions import ClientError

        with self.assertRaises(ClientError):
            missing.probe()
        self.assertIsNone(missing.head("anything"))


if __name__ == "__main__":
    unittest.main()
