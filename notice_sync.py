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


def permit_session_replacement(result_code, dialog_type, already_accepted, http_status):
    """One approved session replacement, only for eAlimi's explicit code 93.

    Do not accept a generic confirmation dialog or repeat a forced login.
    Result code 93 is PASS_OTHERS_USE in the site's public login script.
    """
    return (
        str(result_code) == '93'
        and dialog_type == 'confirm'
        and not already_accepted
        and http_status == 200
    )

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
            # eAlimi reports failed sign-ins with native JavaScript alert()/confirm().
            # Playwright otherwise auto-dismisses these dialogs: HTML/body text
            # checks alone cannot distinguish incorrect secrets from network errors.
            # Do not print dialog text or capture any credential-bearing network data.
            auth_diag = {
                'auth_post_seen': False,
                'auth_post_status': None,
                'js_alert_seen': False,
                'js_confirm_seen': False,
                'credentials_rejected': False,
                'lockout_detected': False,
                'verification_prompt': False,
                'concurrent_session_prompt': False,
                'server_error_alert': False,
                'other_alert': False,
                'page_js_error_seen': False,
                'session_conflict_result': False,
                'force_login_accepted': False,
                'auth_post_count': 0,
                'response_result_code': None,
            }

            def handle_login_dialog(dialog):
                # The login page's public signIn.js creates a native confirm()
                # exclusively for ResultCode.PASS_OTHERS_USE (numeric result 93).
                # Proceed only when the server JSON CONFIRMS code 93. This may
                # end a different device's eAlimi session (user-approved).
                message = dialog.message or ''
                if dialog.type == 'confirm':
                    auth_diag['js_confirm_seen'] = True
                    
                    if (
                        auth_diag.get('login_click_started', False)
                        and not auth_diag['force_login_accepted']
                        and urlparse(page.url).path.lower().rstrip('/') == '/member/signin'
                    ):

                        auth_diag['force_login_accepted'] = True
                        auth_diag['session_conflict_result'] = True
                        dialog.accept()
                        return
                    # Unknown confirmations must not be accepted.
                    dialog.dismiss()
                    return
                if dialog.type == 'alert':
                    auth_diag['js_alert_seen'] = True
                    if any(token in message for token in ('비밀번호가 일치하지', '아이디 또는 비밀번호가 일치하지', '아이디 또는 비밀번호를 확인')):
                        auth_diag['credentials_rejected'] = True
                    elif any(token in message for token in ('30분간 로그인이 제한', '로그인이 차단된 상태', '5회 틀려', '인증에 실패하여 로그인이 차단')):
                        auth_diag['lockout_detected'] = True
                    elif any(token in message for token in ('추가 인증', '인증번호', '본인인증', '기기 인증')):
                        auth_diag['verification_prompt'] = True
                    elif 'Code:' in message or '관리자에게 문의' in message:
                        auth_diag['server_error_alert'] = True
                    else:
                        auth_diag['other_alert'] = True
                # Never log/emit dialog messages, account IDs or any tokens.
                dialog.dismiss()

            def handle_auth_response(response):
                try:
                    req = response.request
                    if req.method != 'POST' or urlparse(response.url).path.lower().rstrip('/') != '/member/signin':
                        return
                    auth_diag['auth_post_seen'] = True
                    auth_diag['auth_post_count'] += 1
                    auth_diag['auth_post_status'] = response.status
                    auth_diag['response_result_code'] = None
                    if response.status == 200:
                        # Read the authenticated AJAX result IN MEMORY only;
                        # never print/store the response body or its message.
                        data = response.json()
                        if isinstance(data, dict) and str(data.get('result')) == '93':
                            auth_diag['response_result_code'] = 93
                except Exception:
                    # If JSON/response inspection is unavailable, fail closed.
                    auth_diag['response_result_code'] = None

            page.on('dialog', handle_login_dialog)
            page.on('response', handle_auth_response)
            page.on('pageerror', lambda _error: auth_diag.__setitem__('page_js_error_seen', True))
            username.fill(user)
            passwd.fill(password)
            auth_diag['login_click_started'] = True

            button = page.get_by_role('button', name=re.compile('로그인')).first
            if button.count():
                button.click(timeout=15000)
            else:
                page.get_by_text('로그인', exact=True).last.click(timeout=15000)
            # eAlimi authenticates using asynchronous JavaScript; do not mistake
            # a slow post-click transition for an authentication failure.
            # ONE login attempt only, to avoid account lockouts.
            try:
                page.wait_for_url(
                    lambda url: '/member/signin' not in urlparse(str(url)).path.lower(),
                    timeout=12000,
                )
            except Exception as wait_error:
                # A timeout by itself is not proof that the credentials are wrong.
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
                if not isinstance(wait_error, PlaywrightTimeoutError):
                    raise
            if '/member/signin' in urlparse(page.url).path.lower():
                # Only print boolean categories, never error messages, HTML,
                # member identifiers, URLs, cookies, passwords, or screenshots.
                body_text = page.locator('body').inner_text(timeout=5000)
                credential_rejected = auth_diag['credentials_rejected'] or ('아이디 또는 비밀번호가 일치하지' in body_text)
                locked_out = auth_diag['lockout_detected'] or any(term in body_text for term in (
                    '30분간 로그인이 제한', '로그인이 차단된 상태', '비밀번호를 5회',
                ))
                verification_prompt = auth_diag['verification_prompt'] or any(term in body_text for term in (
                    '추가 인증', '인증번호 입력', '휴대폰 본인인증',
                ))
                # Print only whitelisted flags and HTTP status codes.
                # No dialog text, user IDs, passwords, HTML, cookies, headers,
                # request payloads, URLs, screenshots, or account content.
                print('EALIMI_AUTH_DIAG', json.dumps({
                    'stayed_on_signin': True,
                    'credential_rejected': bool(credential_rejected),
                    'lockout_detected': bool(locked_out),
                    'verification_prompt': bool(verification_prompt),
                    'auth_post_seen': auth_diag['auth_post_seen'],
                    'auth_post_http_status': auth_diag['auth_post_status'],
                    'js_alert_seen': auth_diag['js_alert_seen'],
                    'js_confirm_seen': auth_diag['js_confirm_seen'],
                    'concurrent_session_prompt': auth_diag['concurrent_session_prompt'],
                    'server_error_alert': auth_diag['server_error_alert'],
                    'other_alert': auth_diag['other_alert'],
                    'page_js_error_seen': auth_diag['page_js_error_seen'],
                    'session_conflict_code_93': auth_diag['session_conflict_result'],
                    'force_login_accepted': auth_diag['force_login_accepted'],
                    'auth_post_count': auth_diag['auth_post_count'],
                }, ensure_ascii=False))
                if locked_out:
                    raise RuntimeError('EALIMI_AUTH_LOCKOUT_STOP_RETRYING')
                if credential_rejected:
                    raise RuntimeError('EALIMI_AUTH_CREDENTIALS_REJECTED_CHECK_SECRETS')
                if verification_prompt:
                    raise RuntimeError('EALIMI_AUTH_EXTRA_VERIFICATION_REQUIRED')
                if auth_diag['force_login_accepted']:
                    raise RuntimeError('EALIMI_FORCE_LOGIN_ACCEPTED_BUT_NOT_REDIRECTED')
                if auth_diag['js_confirm_seen']:
                    raise RuntimeError('EALIMI_AUTH_CONFIRMATION_NOT_VERIFIED_AS_SESSION_CONFLICT')
                if auth_diag['server_error_alert']:
                    raise RuntimeError('EALIMI_AUTH_SERVER_ERROR_ALERT')
                if auth_diag['other_alert']:
                    raise RuntimeError('EALIMI_AUTH_ALERT_UNCLASSIFIED')
                if not auth_diag['auth_post_seen']:
                    raise RuntimeError('EALIMI_AUTH_REQUEST_NOT_SENT')
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
            
            # Verify inbox using its URL, not its displayed title.
            page.wait_for_timeout(3500)

            actual_path = urlparse(page.url).path.lower().rstrip('/')

            if actual_path.startswith('/member/signin'):
                raise RuntimeError('EALIMI_AUTH_EXPIRED')

            if target:
                expected_path = urlparse(target).path.lower().rstrip('/')
                inbox_reached = (
                    actual_path == expected_path
                    or actual_path.startswith(expected_path + '/')
                )

                print('EALIMI_ROUTE_DIAG', json.dumps({
                    'inbox_route_reached': inbox_reached,
                    'redirected_to_main': actual_path.startswith('/main'),
                    'login_route': actual_path.startswith('/member/signin'),
                }))

                if not inbox_reached:
                    raise RuntimeError('EALIMI_INBOX_ROUTE_MISMATCH')

            elif '받은알리미' not in page.locator('body').inner_text().replace(' ', ''):
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
                
                if title == 'URL(주소) 복사':

                    continue

                if (

                    urlparse(href).path.lower().rstrip('/')

                    == urlparse(page.url).path.lower().rstrip('/')

                ):

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
    """Keep the latest health of ALL sources, even when running one mode.

    The old implementation only overwrote the top rows and left misleading
    stale status rows. Maintain one row per source in a stable order.
    """
    prior_rows = get_values(session, sid, SHEET_NAMES['status'], 'A2:E12')
    by_source = {
        row[0]: row[:5] for row in prior_rows
        if len(row) >= 5 and row[0] in SOURCES
    }
    for name, status, count, reason in statuses:
        by_source[name] = [name, now_kst(), status, str(count), reason[:180]]
    rows = [STAT_HEADERS] + [by_source[name] for name in SOURCES if name in by_source]
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
