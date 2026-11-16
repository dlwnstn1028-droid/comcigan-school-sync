"""Owner-account Apps Script backup path: offline validation only."""
from unittest.mock import patch

import notice_enrich


def test_owner_backup_rejects_missing_secret(monkeypatch):
    monkeypatch.setenv("NOTICE_UPLOAD_WEBAPP_URL", "https://script.google.com/macros/s/AKfycbxMock/exec")
    monkeypatch.delenv("NOTICE_UPLOAD_TOKEN", raising=False)
    url, status = notice_enrich.archive_to_drive(b"%PDF-1.4\n", "test.pdf", "notice")
    assert url == ""
    assert status == "OWNER_BACKUP_SECRET_MISSING"


def test_owner_backup_rejects_untrusted_host(monkeypatch):
    monkeypatch.setenv("NOTICE_UPLOAD_WEBAPP_URL", "https://example.com/macros/s/AKfycbxMock/exec")
    monkeypatch.setenv("NOTICE_UPLOAD_TOKEN", "0" * 64)
    with patch.object(notice_enrich.requests, "post") as post:
        url, status = notice_enrich.archive_to_drive(b"%PDF-1.4\n", "test.pdf", "notice")
    assert (url, status) == ("", "OWNER_BACKUP_ENDPOINT_INVALID")
    post.assert_not_called()


def test_owner_backup_positive_result_is_verified(monkeypatch):
    monkeypatch.setenv("NOTICE_UPLOAD_WEBAPP_URL", "https://script.google.com/macros/s/AKfycbxMock/exec")
    monkeypatch.setenv("NOTICE_UPLOAD_TOKEN", "0" * 64)
    fid = "1" * 32
    class Response:
        status_code = 200
        def json(self):
            return {"ok": True, "status": "SAVED", "id": fid,
                    "url": f"https://drive.google.com/file/d/{fid}/view"}
    with patch.object(notice_enrich.requests, "post", return_value=Response()) as post:
        url, status = notice_enrich.archive_to_drive(b"%PDF-1.4\n", "test.pdf", "notice")
    assert status == "DRIVE_SAVED"
    assert url == f"https://drive.google.com/file/d/{fid}/view"
    assert post.call_args.kwargs["json"]["sha256"] == notice_enrich.digest(b"%PDF-1.4\n")


def test_owner_backup_does_not_accept_fake_success(monkeypatch):
    monkeypatch.setenv("NOTICE_UPLOAD_WEBAPP_URL", "https://script.google.com/macros/s/AKfycbxMock/exec")
    monkeypatch.setenv("NOTICE_UPLOAD_TOKEN", "0" * 64)
    class Response:
        status_code = 200
        def json(self):
            return {"ok": True, "status": "SAVED", "id": "not-a-real-id", "url": "https://evil.invalid/abc"}
    with patch.object(notice_enrich.requests, "post", return_value=Response()):
        assert notice_enrich.archive_to_drive(b"%PDF-1.4\n", "test.pdf", "notice") == (
            "", "OWNER_BACKUP_REJECTED"
        )
