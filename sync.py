"""Fetch Comcigan and safely stage data in an existing Google Sheet.

Does NOT write to Google Calendar. Unverified live changes are never promoted
as confirmed personal timetable. No NEIS calls are made.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

KST = timezone(timedelta(hours=9))
SCHOOL_ID = 69622
SHEET_ID = os.getenv('TIMETABLE_SHEET_ID', '')

REGULAR = [
 ['체육(문지혜)','물리학실험( )','문학B(최홍준)','영어B(정의석)','화학(이규희)','미적1(박희선)'],
 ['미적1(박희선)','교양3(이윤정)','화학(이규희)','물리(이창학)','영어B(정의석)','기하(우안성)','생각2(정의석)'],
 ['기하(우안성)','화학(이규희)','정보강(정의석)','미적1(박희선)','물리(이창학)','창체','창체'],
 ['물리(이창학)','교양3(이윤정)','물리학실험( )','문학A(이현익)','영어A(정의석)','문학B(최홍준)'],
 ['진로2(오승협)','체육(문지혜)','물리학실험( )','영어A(정의석)','문학A(이현익)','미적1(박희선)','기하(우안성)']
]
TIMES = [('08:20','09:10'),('09:20','10:10'),('10:20','11:10'),('11:20','12:10'),('13:10','14:00'),('14:10','15:00'),('15:10','16:00')]
OVERRIDES = {'2026-11-12': {4:'영어A(정의석)',5:'문학A(이현익)'}}
NO_CLASS = {'2026-11-19'}


def fetch_live():
    """Read a live timetable using upstream's complete enc5 protocol.

    The maintained TimeTable client discovers rotating endpoints, request
    tokens and the AES/HMAC key from the service page. Calling decode_response
    without that key fails even when the HTTP request returned 200.
    """
    from pycomcigan import TimeTable

    table = TimeTable('휘문고등학교', school_code=SCHOOL_ID, week_num=0)
    if int(table.school_code) != SCHOOL_ID or '휘문' not in table.school_name:
        raise RuntimeError('Comcigan school identity mismatch')

    try:
        lessons = table.timetable[2][6]
    except (IndexError, TypeError, KeyError) as exc:
        raise RuntimeError('Grade 2 class 6 timetable missing') from exc
    if not isinstance(lessons, list) or len(lessons) < 6:
        raise RuntimeError('Unexpected class timetable structure')

    # Class timetable consists of day 0 placeholder, then Monday-Friday.
    count = sum(
        1 for day in lessons[1:6] for lesson in day
        if getattr(lesson, 'subject', '')
    )
    if count < 20:
        raise RuntimeError(f'Unexpectedly sparse live timetable ({count} lessons)')
    return {'school': table.school_name, 'class': (2, 6),
            'lessons': lessons, 'lesson_count': count}


def regular_rows(now):
    monday = now.date() - timedelta(days=now.weekday())
    rows = []
    for i in range(5):
        d = monday + timedelta(days=i)
        day = d.isoformat()
        if day in NO_CLASS or '2026-12-03' <= day <= '2026-12-09':
            continue
        base_day = 3 if day == '2026-11-16' else i
        for period, name in enumerate(REGULAR[base_day], 1):
            name = OVERRIDES.get(day, {}).get(period, name)
            rows.append([day, '월화수목금'[i], period, TIMES[period-1][0], TIMES[period-1][1], name, 'PERSONAL_BASELINE_NOT_LIVE_VERIFIED'])
    return rows



def live_rows(now, data):
    """Raw homeroom timetable; never equate homeroom electives with personal classes."""
    monday = now.date() - timedelta(days=now.weekday())
    out = []
    for i, day in enumerate(data['lessons'][1:6]):
        ds = (monday + timedelta(days=i)).isoformat()
        for item in day[:7]:
            subject = getattr(item, 'subject', '') or ''
            teacher = getattr(item, 'teacher', '') or ''
            orig = getattr(item, 'original', None)
            original_subject = (getattr(orig, 'subject', '') or '') if orig else ''
            original_teacher = (getattr(orig, 'teacher', '') or '') if orig else ''
            out.append([ds, '월화수목금'[i], int(item.period), subject,
                        teacher, original_subject, original_teacher,
                        'YES' if getattr(item, 'replaced', False) else 'NO'])
    return out

def sheets_session():
    raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not raw or not SHEET_ID:
        raise RuntimeError('Set GOOGLE_SERVICE_ACCOUNT_JSON and TIMETABLE_SHEET_ID GitHub secrets')
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return AuthorizedSession(creds)


def request_checked(session, method, url, **kwargs):
    r = session.request(method, url, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else {}


def write_tabs(now, rows, status, live=None):
    session = sheets_session()
    root = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
    meta = request_checked(session, 'GET', root,
                           params={'fields': 'sheets.properties(title)'})
    existing = {x['properties']['title'] for x in meta.get('sheets', [])}
    names = ['개인기준시간표', '컴시간조회상태', '컴시간원본']
    missing = [n for n in names if n not in existing]
    if missing:
        request_checked(session, 'POST', root+':batchUpdate', json={
            'requests': [{'addSheet': {'properties': {'title': n}}} for n in missing]
        })
    stamp = now.isoformat(timespec='seconds')
    raw_rows = live or []
    payload = {
      'valueInputOption': 'RAW',
      'data': [
        {'range': "'개인기준시간표'!A1:G100", 'values': [
          ['날짜', '요일', '교시', '시작', '종료', '개인 과목', '검증 상태'], *rows]},
        {'range': "'컴시간조회상태'!A1:B10", 'values': [
          ['항목', '값'], ['조회 시각(KST)', stamp], ['결과', status],
          ['학교코드', str(SCHOOL_ID)],
          ['컴시간 변경사항 자동적용', '아니오 (이동수업 매핑 미검증)'],
          ['캘린더 변경', '없음'],
          ['컴시간 원본 수업 수', str(sum(bool(row[3]) for row in raw_rows))]]},
        {'range': "'컴시간원본'!A1:H100", 'values': [
          ['날짜','요일','교시','6반 과목','6반 교사','이전 과목','이전 교사','변경 표시'],
          *raw_rows]}
      ]
    }
    # Preserve prior successful live snapshot when live fetch fails.
    # No data from a failed fetch is ever silently presented as new live data.
    for n in ['개인기준시간표', '컴시간조회상태']:
        request_checked(session, 'POST', root+f"/values/'{n}'!A1:Z500:clear", json={})
    if live is not None:
        request_checked(session, 'POST', root+"/values/'컴시간원본'!A1:Z500:clear", json={})
    if live is None:
        payload['data'] = payload['data'][:2]
    request_checked(session, 'POST', root+'/values:batchUpdate', json=payload)


def main():
    now = datetime.now(KST)
    if now.weekday() > 4 and os.getenv('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        print('Weekend; skipping')
        return
    rows = regular_rows(now)
    try:
        data = fetch_live()
        status = 'ENC5_DECODED_UNMAPPED'
        live = live_rows(now, data)
        print('Comcigan enc5 decoded; class:', data['class'],
              'lessons:', data['lesson_count'])
    except Exception as e:
        live = None
        status = 'FETCH_FAILED: '+type(e).__name__
        print('Live fetch not verified:', repr(e), file=sys.stderr)
    # Baseline and status only. No unverified substitutions or calendar writes.
    write_tabs(now, rows, status, live)
    print(f'Updated safe staging tabs; status={status}; baseline rows={len(rows)}')
    if status.startswith('FETCH_FAILED'):
        raise RuntimeError('Live Comcigan fetch failed; staging marked accordingly')

if __name__ == '__main__':
    main()
