"""Whimoon public board + authorised eAlimi inbox LIST watcher.

Never send passwords or private notice content to GitHub logs.
Never open an individual unread eAlimi notice or submit a form.
Stop with error (instead of claiming success) if the inbox is not recognised.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
BOARD_URL = 'https://www.whimoon.hs.kr/?module=Board&action=SiteBoard&sMode=SELECT_FORM&iBrdNo={}'
BOARDS = {'휘문고_공지': 6, '휘문고_학사': 7, '휘문고_시험': 1}
HEADERS = ['source', 'notice_id', 'published_at', 'title', 'url', 'first_seen_kst',
           'last_seen_kst', 'fingerprint', 'attachment_urls', 'detail_status']
NEW_HEADERS = ['detected_at_kst', 'source', 'kind', 'title', 'published_at', 'url', 'notice_id', 'fingerprint']
STAT_HEADERS = ['source', 'checked_at_kst', 'result', 'count', 'reason']
SOURCES = tuple(BOARDS) + ('e알리미',)
SHEET_NAMES = {'index': '학교공지_목록', 'news': '학교공지_새소식', 'status': '학교공지_상태'}


def now_kst():
    return datetime.now(tz=KST).strftime('%Y-%m-%d %H:%M:%S')


def fingerprint(*parts):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:24]


def parse_public_anchors(html, board_no):
    """Return verified board links only; never treat navigation links as notices."""
    soup = BeautifulSoup(html, 'html.parser')
    seen = set()
    result = []
    for link in soup.select('a[href*="iBrdContNo="]'):
        href = urljoin('https://www.whimoon.hs.kr/', link.get('href', ''))
        args = parse_qs(urlparse(href).query)
        if args.get('iBrdNo', [''])[0] != str(board_no) or args.get('sMode', [''])[0] != 'VIEW_FORM':
            continue
        uid = args.get('iBrdContNo', [''])[0]
        if not uid.isdigit() or uid in seen:
            continue
        seen.add(uid)
        text = ' '.join(link.stripped_strings)
        title = re.split(r'작성자\s*', text, maxsplit=1)[0].strip()
        title = re.sub(r'^\d+\s+', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        m = re.search(r'작성날짜\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2})', text)
        date = m.group(1).replace('.', '-').replace('/', '-') if m else ''
        if title:
            result.append({'notice_id': uid, 'title': title[:220], 'published_at': date, 'url': href})
    return result


def scrape_public():
    sess = requests.Session()
    sess.headers['User-Agent'] = 'WhimoonNoticeReader/1.0 (personal low-frequency educational monitoring)'
    by_source = {}
    for source, code in BOARDS.items():
        r = sess.get(BOARD_URL.format(code), timeout=25)
        r.raise_for_status()
        records = parse_public_anchors(r.text, code)
        if not records:
            raise RuntimeError(f'{source}: NO_BOARD_ROWS (page content/layout changed)')
        # Read each listed detail page and index its PDF/document attachment links.
        # Never download the attached documents into a public GitHub runner artifact.
        for rec in records:
            detail = sess.get(rec['url'], timeout=25)
            detail.raise_for_status()
            doc = BeautifulSoup(detail.text, 'html.parser')
            attachments = []
            for a in doc.select('a[href*="download.php"]'):
                href = urljoin(rec['url'], a.get('href', ''))
                if urlparse(href).hostname == 'www.whimoon.hs.kr' and href not in attachments:
                    attachments.append(href)
            rec['source'] = source
            rec['attachment_urls'] = ' ; '.join(attachments[:12])
            rec['detail_status'] = 'DETAIL_LINKS_OK'
            rec['fingerprint'] = fingerprint(rec['title'], rec['published_at'], rec['attachment_urls'])
            time.sleep(0.20)
        by_source[source] = records
        time.sleep(0.4)
    return by_source


def is_ealimi_url(url):
    u = urlparse(url)
    return u.scheme == 'https' and (u.hostname == 'www.ealimi.com' or u.hostname == 'ealimi.com')


def collect_ealimi():
    """Read only the list rendered for an authorised user, without opening items.

    The post-login layout is NOT verified; if it changes or cannot be recognised,
    fail with EALIMI_LAYOUT_UNVERIFIED. No claim of complete inbox coverage.
    """
    from playwright.sync_api import sync_playwright
    user = os.getenv('EALIMI_ID', '').strip()
    password = os.getenv('EALIMI_PASSWORD', '')
    if not user or not password:
        raise RuntimeError('EALIMI_SECRETS_MISSING')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(locale='ko-KR', timezone_id='Asia/Seoul')
        page = context.new_page()
        try:
            page.goto('https://www.ealimi.com/Member/SignIn', wait_until='domcontentloaded', timeout=30000)
            username = page.locator('input[type="text"]:visible, input[type="tel"]:visible, input[type="email"]:visible').first
            passwd = page.locator('input[type="password"]:visible').first
            if username.count() == 0 or passwd.count() == 0:
                raise RuntimeError('EALIMI_LOGIN_LAYOUT_CHANGED')
            username.fill(user)
            passwd.fill(password)
            button = page.get_by_role('button', name=re.compile('로그인')).first
            if button.count():
                button.click(timeout=15000)
            else:
                page.get_by_text('로그인', exact=True).last.click(timeout=15000)
            page.wait_for_timeout(2800)
            if '/Member/SignIn' in page.url:
                # This covers incorrect login / 2FA / CAPTCHA / login layout changes.
                raise RuntimeError('EALIMI_AUTH_UNVERIFIED_OR_EXTRA_STEP')
            target = os.getenv('EALIMI_LIST_URL', '').strip()
            navigation_clicked = False
            if target:
                if not is_ealimi_url(target):
                    raise RuntimeError('EALIMI_LIST_URL_NOT_ALLOWED')
                page.goto(target, wait_until='domcontentloaded', timeout=30000)
                navigation_clicked = True
            else:
                # A hidden menu entry may appear before the visible one.
                # Only click a visible navigation entry, never a notice detail.
                for name in ['받은 알리미', '받은알리미', '알리미 모아보기']:
                    matches = page.get_by_text(name, exact=False)
                    for idx in range(min(matches.count(), 12)):
                        item = matches.nth(idx)
                        if item.is_visible():
                            try:
                                item.click(timeout=5000)
                                navigation_clicked = True
                                break
                            except Exception:
                                continue
                    if navigation_clicked:
                        break
            # Allow the authenticated notice list time to render asynchronously.
            page.wait_for_timeout(2500)
            if '/Member/SignIn' in page.url:
                raise RuntimeError('EALIMI_AUTH_EXPIRED')
            if '받은알리미' not in page.locator('body').inner_text().replace(' ', ''):
                raise RuntimeError('EALIMI_INBOX_NAVIGATION_UNVERIFIED')
            soup = BeautifulSoup(page.content(), 'html.parser')
            # Only consider rows/cards in list structures (do not open notices).
            candidates = soup.select('table tbody tr, [class*="list"] li, [class*="List"] li, [class*="notice"] li, [class*="Notice"] li')
            results = []
            for el in candidates:
                text = ' '.join(el.stripped_strings)
                if len(text) < 9 or len(text) > 400:
                    continue
                if any(x in text for x in ['로그인', '비밀번호 찾기', '회원가입', '계정선택']):
                    continue
                a = el.select_one('a[href]')
                href = urljoin(page.url, a.get('href', '')) if a else page.url
                if not is_ealimi_url(href):
                    continue
                title = (a.get_text(' ', strip=True) if a else text)[:200]
                if not title or len(title) < 4:
                    continue
                m = re.search(r'(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
                date = f'{m[1]}-{int(m[2]):02}-{int(m[3]):02}' if m else ''
                cls = ' '.join(el.get('class', [])) + ' ' + ' '.join(el.parent.get('class', []) if el.parent else [])
                if not m and not re.search(r'alimi|receive|notice|list', cls, re.I):
                    continue
                uid = fingerprint(href, title, date)
                results.append({'source': 'e알리미', 'notice_id': uid,
                                'title': title, 'published_at': date, 'url': href,
                                'fingerprint': fingerprint(text), 'attachment_urls': '',
                                'detail_status': 'LIST_ONLY;NO_UNREAD_OPEN'})
            # Dedupe and guard against treating unrelated homepage menus as announcements.
            results = list({r['notice_id']: r for r in results}.values())
            if not results:
                # Diagnostics are aggregate element COUNTS only. Never log HTML,
                # notice text/titles, URLs, screenshots, member IDs or secrets.
                structure = {
                    'navigation_attempted': navigation_clicked,
                    'login_button_present': bool(page.locator('#signInSubmitBtn').count()),
                    'table_count': len(soup.select('table')),
                    'table_body_rows': len(soup.select('table tbody tr')),
                    'list_items': len(soup.select('li')),
                    'article_count': len(soup.select('article')),
                    'iframe_count': len(soup.select('iframe')),
                    'link_count': len(soup.select('a[href]')),
                    'candidate_nodes': len(candidates),
                    'card_class_nodes': len(soup.select('[class*="card"], [class*="Card"]')),
                    'item_class_nodes': len(soup.select('[class*="item"], [class*="Item"]')),
                    'role_rows': len(soup.select('[role="row"]')),
                }
                print('EALIMI_STRUCTURE_COUNTS', json.dumps(structure, ensure_ascii=False))
                raise RuntimeError('EALIMI_LAYOUT_UNVERIFIED (login may work; inbox list not recognised)')
            return results
        finally:
            context.close()
            browser.close()


def sheet_session():
    raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    sid = os.getenv('TIMETABLE_SHEET_ID')
    if not raw or not sid:
        raise RuntimeError('EXISTING_GOOGLE_SHEETS_SECRETS_MISSING')
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return AuthorizedSession(creds), sid


def api(session, method, url, **kwargs):
    resp = session.request(method, url, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def range_name(tab, cells):
    return f"'{tab}'!{cells}"


def get_values(session, sid, tab, cells):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{requests.utils.quote(range_name(tab, cells), safe="")}'
    return api(session, 'GET', url).get('values', [])


def put_values(session, sid, tab, cells, values):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{requests.utils.quote(range_name(tab, cells), safe="")}'
    return api(session, 'PUT', url, params={'valueInputOption': 'RAW'}, json={'values': values})


def append_rows(session, sid, tab, rows):
    if not rows:
        return
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{requests.utils.quote(range_name(tab,"A:J"),safe="")}:append'
    api(session, 'POST', url, params={'valueInputOption':'RAW', 'insertDataOption':'INSERT_ROWS'}, json={'values': rows})


def ensure_tabs(session, sid):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}'
    meta = api(session, 'GET', url, params={'fields':'sheets.properties.title'})
    existing = {s['properties']['title'] for s in meta.get('sheets', [])}
    create = [name for name in SHEET_NAMES.values() if name not in existing]
    if create:
        api(session, 'POST', url + ':batchUpdate', json={'requests': [{'addSheet': {'properties': {'title': x}}} for x in create]})
    for name, cols in [(SHEET_NAMES['index'], HEADERS), (SHEET_NAMES['news'], NEW_HEADERS), (SHEET_NAMES['status'], STAT_HEADERS)]:
        v = get_values(session, sid, name, 'A1:J1')
        if not v or not v[0]:
            put_values(session, sid, name, 'A1:J1', [cols])
        elif v[0][:len(cols)] != cols:
            raise RuntimeError('TAB_HEADER_MISMATCH:' + name)


def upsert_notices(session, sid, by_source):
    records = get_values(session, sid, SHEET_NAMES['index'], 'A2:J10000')
    old = {(row[0],row[1]): (idx + 2, row) for idx, row in enumerate(records) if len(row) >= 2}
    baseline = {src: not any(k[0] == src for k in old) for src in by_source}
    updates, newly, changes = [], [], []
    for source, items in by_source.items():
        for obj in items:
            key = (source, obj['notice_id'])
            previous = old.get(key)
            row = [source, obj['notice_id'], obj['published_at'], obj['title'],
                   obj['url'], previous[1][5] if previous and len(previous[1])>5 else now_kst(),
                   now_kst(), obj['fingerprint'], obj['attachment_urls'], obj['detail_status']]
            if not previous:
                newly.append(row)
                if not baseline[source]:
                    changes.append([now_kst(), source, '신규', obj['title'], obj['published_at'], obj['url'],obj['notice_id'],obj['fingerprint']])
            elif len(previous[1]) < 8 or previous[1][7] != obj['fingerprint']:
                updates.append((previous[0], row))
                changes.append([now_kst(), source, '제목/목록변경', obj['title'], obj['published_at'], obj['url'], obj['notice_id'], obj['fingerprint']])
    if updates:
        url = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchUpdate'
        body = {'valueInputOption':'RAW','data':[{'range':range_name(SHEET_NAMES['index'],f'A{i}:J{i}'),'values':[row]} for i,row in updates]}
        api(session, 'POST', url, json=body)
    append_rows(session, sid, SHEET_NAMES['index'], newly)
    append_rows(session, sid, SHEET_NAMES['news'], changes)
    return {'new':len(newly), 'changed':len(updates), 'alerts':len(changes), 'baseline_sources':[s for s, v in baseline.items() if v]}


def update_status(session, sid, statuses):
    rows = [STAT_HEADERS] + [[name, now_kst(), status, str(count), reason[:180]] for name, status, count, reason in statuses]
    put_values(session, sid, SHEET_NAMES['status'], f'A1:E{len(rows)}', rows)


def main():
    mode = os.getenv('RUN_MODE', 'school').lower()
    if mode not in ('school', 'ealimi', 'all'):
        raise ValueError('RUN_MODE must be school/ealimi/all')
    session, sid = sheet_session()
    ensure_tabs(session, sid)
    combined = {}
    results = []
    failures = []
    if mode in ('school', 'all'):
        try:
            public = scrape_public()
            combined.update(public)
            results.extend([(s, 'OK', len(x), '') for s,x in public.items()])
        except Exception as e:
            failures.append('PUBLIC_BOARD_FAILED')
            results.extend([(s, 'FAILED', 0, type(e).__name__) for s in BOARDS])
    if mode in ('ealimi', 'all'):
        try:
            arr = collect_ealimi()
            combined['e알리미'] = arr
            results.append(('e알리미', 'OK_LIST_ONLY', len(arr), 'Login and summary rows; detail/attachments unverified'))
        except Exception as e:
            failures.append('EALIMI_FAILED')
            reason = str(e)
            if 'EALIMI_' not in reason:
                reason = 'EALIMI_UNEXPECTED_ERROR_' + type(e).__name__
            results.append(('e알리미', 'FAILED', 0, reason))
    if combined:
        counts = upsert_notices(session, sid, combined)
        print('SHEET_SAVED; counts=', json.dumps(counts, ensure_ascii=False))
    update_status(session, sid, results)
    for src,status,count,reason in results:
        print('SOURCE',src,status,count,reason)  # no private title, URL, identifier or credentials
    if failures:
        print('FAILURE_FLAGS', ','.join(failures))
        return 1
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('FATAL', type(exc).__name__, 'details suppressed to protect private data')
        sys.exit(2)
