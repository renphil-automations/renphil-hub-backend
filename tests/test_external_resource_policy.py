import pytest

from app.services.external_resource_policy import resource_policy


@pytest.mark.parametrize("url,label,supported", [
    ("https://example.org/report.PDF?download=1", "PDF", True),
    ("https://example.org/report.docx", "Word", False),
    ("https://example.org/report.xlsx", "Excel", False),
    ("https://example.org/report.xls", "Excel", False),
    ("https://example.org/image.png", "Image", False),
    ("https://airtable.com/appExample", "Airtable", False),
    ("https://docs.google.com/forms/d/test/viewform", "Google Form", False),
    ("https://docs.google.com/document/d/test/edit", "Google Docs", True),
    ("https://docs.google.com/spreadsheets/d/test/edit", "Google Sheets", True),
    ("https://drive.google.com/file/d/test/view", "Type not checked", None),
    ("https://example.org/download", "Type not checked", None),
    ("https://example.org:bad/file.pdf", "Invalid link", False),
])
def test_resource_type_and_support_are_separate_from_approval(url, label, supported):
    policy = resource_policy(url)
    assert policy.label == label
    assert policy.supported is supported


def test_worker_detection_overrides_url_suffix_and_handles_legacy_unsupported():
    assert resource_policy("https://example.org/doc.pdf", "text/html").supported is False
    assert resource_policy("https://example.org/download", "application/pdf").supported is True
    assert resource_policy("https://example.org/download", "Unknown", "unsupported_resource_type: application/octet-stream").supported is False


def test_deceptive_domains_are_not_treated_as_trusted_providers():
    assert resource_policy("https://airtable.com.example.org/download").supported is None
    assert resource_policy("https://docs.google.com.example.org/document/d/id").supported is None
