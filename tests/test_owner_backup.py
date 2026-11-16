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


def test_archive_backfill_saves_existing_private_pdf(monkeypatch):
    monkeypatch.setenv('NOTICE_UPLOAD_WEBAPP_URL',
                       'https://script.google.com/macros/s/AKfycbxMock/exec')
    monkeypatch.setenv('NOTICE_UPLOAD_TOKEN', '0'*64)
    initial=['e알리미','notice-1','attach-1','file.pdf',
             'https://www.ealimi.com/File/Download?id=1','application/pdf',
             '512','PDF_TEXT_EXTRACTED;DRIVE_NOT_CONFIGURED','201','2026-11-16 21:00:00','']
    rows=[initial,
          ['e알리미','notice-2','attach-2','notpdf','https://www.ealimi.com/file2',
           'unknown',0,'NOT_PDF',0,'',''],
          ['휘문고_공지','notice-3','attach-3','public.pdf',
           'https://www.whimoon.hs.kr/test.pdf','application/pdf',512,
           'PDF_TEXT_EXTRACTED',201,'','']]
    link='https://drive.google.com/file/d/'+'1'*30+'/view'
    with patch.object(notice_enrich,'sheet_values',return_value=rows), (
         patch.object(notice_enrich,'fetch_pdf_in_session',return_value=(b'%PDF-1.4','PDF'))) as fetch, (
         patch.object(notice_enrich,'archive_to_drive',return_value=(link,'DRIVE_SAVED'))) as archive, (
         patch.object(notice_enrich,'sheet_update')) as update:
        stats=notice_enrich.backfill_archive(object(),object(),'sheet-id',public=False)
    assert stats=={'queued':1,'saved':1,'failed':0}
    assert fetch.call_count==1 and archive.call_count==1
    row=update.call_args.args[4]
    assert update.call_args.args[3]==2
    assert row[7]=='PDF_TEXT_EXTRACTED;DRIVE_SAVED'
    assert row[10]==link


def test_archive_backfill_does_not_mark_failed_saves(monkeypatch):
    monkeypatch.setenv('NOTICE_UPLOAD_WEBAPP_URL',
                       'https://script.google.com/macros/s/AKfycbxMock/exec')
    monkeypatch.setenv('NOTICE_UPLOAD_TOKEN', '0'*64)
    original=['e알리미','notice-1','attach-1','file.pdf',
              'https://www.ealimi.com/file.pdf','application/pdf',
              512,'PDF_TEXT_EXTRACTED',201,'','']
    with patch.object(notice_enrich,'sheet_values',return_value=[original]), (
         patch.object(notice_enrich,'fetch_pdf_in_session',return_value=(b'%PDF-1.4','PDF'))), (
         patch.object(notice_enrich,'archive_to_drive',return_value=('', 'OWNER_BACKUP_REJECTED'))), (
         patch.object(notice_enrich,'sheet_update')) as update:
        stats=notice_enrich.backfill_archive(object(),object(),'sheet-id')
    assert stats=={'queued':1,'saved':0,'failed':1}
    update.assert_not_called()


def test_archive_backfill_skips_if_not_deployed(monkeypatch):
    monkeypatch.delenv('NOTICE_UPLOAD_WEBAPP_URL',raising=False)
    monkeypatch.delenv('NOTICE_UPLOAD_TOKEN',raising=False)
    with patch.object(notice_enrich,'sheet_values') as read:
        result=notice_enrich.backfill_archive(object(),object(),'sheet-id')
    assert result=={'queued':0,'saved':0,'failed':0}
    read.assert_not_called()
