from datetime import datetime, timezone, timedelta
from notice_enrich import (extract_main_text, discover_attachments, parse_pdf,
                           extract_review_candidates, refresh_needed, safe_url,
                           time_near, safe_segment)


def test_extract_main_notice_body_without_nav():
    html='<body><nav>개인정보 메뉴</nav><main><h1>기말고사 일정</h1><p>2026년 12월 3일 기말고사 시행 안내</p></main></body>'
    text, status=extract_main_text(html)
    assert '개인정보 메뉴' not in text and '기말고사' in text and status=='TEXT_EXTRACTED'


def test_attachment_allowlist_only_same_host():
    html='''<a href="/File/Download?fileId=4">첨부 PDF</a>
    <a href="https://evil.test/file.pdf">PDF 다운</a>
    <a href="/ReceivedNoti/Content?l_id=2">읽기</a>'''
    urls=discover_attachments(html,'https://www.ealimi.com/ReceivedNoti/Content?l_id=4')
    assert len(urls)==1 and '/File/Download' in urls[0][0]


def test_event_candidates_unapproved_and_deduped():
    now=datetime(2026,11,16,tzinfo=timezone(timedelta(hours=9)))
    rows=extract_review_candidates('e알리미','123','선택과목 수강 신청',
        '수강 신청은 2026년 11월 27일 08시 마감합니다. 2026년 11월 27일 08시 재확인.',
        '2026-11-16','https://www.ealimi.com/ReceivedNoti/Content?l_id=123',now)
    assert rows and len({r[0] for r in rows})==len(rows)
    assert all(r[10]=='승인 대기' for r in rows)
    assert '2026-11-27' in [r[5] for r in rows]


def test_no_unrelated_date_without_event_keyword():
    assert not extract_review_candidates('x','1','제목','2026년 11월 16일은 맑습니다.',
                                         '', 'https://www.ealimi.com/x')


def test_invalid_domains_rejected():
    assert not safe_url('https://ealimi.com.evil.com/ReceivedNoti/Content')
    assert not safe_url('http://www.ealimi.com/ReceivedNoti/Content')
    assert safe_url('https://www.ealimi.com/ReceivedNoti/Content')


def test_pdf_invalid_and_segmentation():
    assert parse_pdf(b'THIS IS NOT A PDF')[1]=='NOT_PDF'
    assert len(safe_segment('a'*35000))==3


def test_refresh_stale_and_fresh():
    now=datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
    assert not refresh_needed((2,['a','b','c','d','e',now,'TEXT_EXTRACTED','']),7)
    assert refresh_needed(None,7)