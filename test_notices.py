from notice_sync import parse_public_anchors, fingerprint


def test_parse_known_public_board_link():
    html = '''<a href="/?module=Board&amp;action=SiteBoard&amp;sMode=VIEW_FORM&amp;iBrdNo=6&amp;iBrdContNo=681">649 2026학년도 수행평가 유의사항 작성자 교무기획부작성날짜 2026.10.06조회수 550</a>'''
    r = parse_public_anchors(html, 6)
    assert len(r) == 1
    assert r[0]['notice_id'] == '681'
    assert r[0]['published_at'] == '2026-10-06'
    assert '수행평가' in r[0]['title']
    assert parse_public_anchors(html, 7) == []


def test_fingerprint_stable():
    assert fingerprint('a', 'b') == fingerprint('a', 'b')
    assert fingerprint('a', 'b') != fingerprint('b', 'a')
