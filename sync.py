"""Fetch BOTH Comcigan weeks and safely stage them in the existing Google Sheet.

week_num=0 (this week) remains in 컴시간원본 for backwards compatibility.
week_num=1 (next week) is independently requested and stored in 컴시간다음주.
Never infer the personal elective timetable or edit Google Calendar here.
NEIS is not accessed. A failed week never overwrites its last successful snapshot.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

KST = timezone(timedelta(hours=9))
SCHOOL_ID = 69622
SHEET_ID = os.getenv('TIMETABLE_SHEET_ID', '')

REGULAR = [
    ['체육(문지혜)', '물리학실험( )', '문학B(최홍준)', '영어B(정의석)', '화학(이규희)', '미적1(박희선)'],
    ['미적1(박희선)', '교양3(이윤정)', '화학(이규희)', '물리(이창학)', '영어B(정의석)', '기하(우안성)', '생각2(정의석)'],
    ['기하(우안성)', '화학(이규희)', '정보강(정의석)', '미적1(박희선)', '물리(이창학)', '창체', '창체'],
    ['물리(이창학)', '교양3(이윤정)', '물리학실험( )', '문학A(이현익)', '영어A(정의석)', '문학B(최홍준)'],
    ['진로2(오승협)', '체육(문지혜)', '물리학실험( )', '영어A(정의석)', '문학A(이현익)', '미적1(박희선)', '기하(우안성)'],
]
TIMES = [
    ('08:20', '09:10'), ('09:20', '10:10'), ('10:20', '11:10'),
    ('11:20', '12:10'), ('13:10', '14:00'), ('14:10', '15:00'),
    ('15:10', '16:00'),
]
OVERRIDES = {'2026-11-12': {4: '영어A(정의석)', 5: '문학A(이현익)'}}
NO_CLASS = {'2026-11-19'}
RAW_HEADERS = ['날짜', '요일', '교시', '6반 과목', '6반 교사', '이전 과목', '이전 교사', '변경 표시', '검증 상태']


def week_monday(now, week_num=0):
    return now.date() - timedelta(days=now.weekday()) + timedelta(weeks=week_num)


def verify_week_start(now, week_num, start_date):
    """Reject a mislabeled upstream week before replacing any snapshot."""
    expected = week_monday(now, week_num)
    try:
        actual = datetime.strptime(str(start_date).strip(), '%Y-%m-%d').date()
    except ValueError as exc:
        raise RuntimeError('Invalid upstream week start date') from exc
    if actual != expected:
        raise RuntimeError(f'Upstream week mismatch: expected {expected}, got {actual}')
    return ('CURRENT_WEEK_DATE_VERIFIED' if week_num == 0
            else 'NEXT_WEEK_DATE_VERIFIED_PERSONAL_UNMAPPED')


def fetch_live(week_num):
    """Use upstream's rotating token/key extraction; request each week separately."""
    from pycomcigan import TimeTable

    if week_num not in (0, 1):
        raise ValueError('week_num must be 0 or 1')
    table = TimeTable('휘문고등학교', school_code=SCHOOL_ID, week_num=week_num)
    if int(table.school_code) != SCHOOL_ID or '휘문' not in table.school_name:
        raise RuntimeError('Comcigan school identity mismatch')

    try:
        lessons = table.timetable[2][6]
    except (IndexError, TypeError, KeyError) as exc:
        raise RuntimeError('Grade 2 class 6 timetable missing') from exc
    if not isinstance(lessons, list) or len(lessons) < 6:
        raise RuntimeError('Unexpected class timetable structure')
    lesson_count = sum(
        bool(getattr(lesson, 'subject', ''))
        for day in lessons[1:6]
        for lesson in day[:7]
    )
    if lesson_count < 20:
        raise RuntimeError(f'Unexpectedly sparse live timetable ({lesson_count} lessons)')

    return {
        'school': table.school_name,
        'class': (2, 6),
        'lessons': lessons,
        'lesson_count': lesson_count,
        'start_date': str(getattr(table, 'start_date', '')),
        'update_date': str(getattr(table, 'update_date', '')),
    }


def regular_rows(now):
    """Personal *baseline* for both weeks, never inferred from homeroom electives."""
    rows = []
    for week_num in (0, 1):
        monday = week_monday(now, week_num)
        for i in range(5):
            date_str = (monday + timedelta(days=i)).isoformat()
            if date_str in NO_CLASS or '2026-12-03' <= date_str <= '2026-12-09':
                continue
            base_day = 3 if date_str == '2026-11-16' else i
            for period, title in enumerate(REGULAR[base_day], 1):
                title = OVERRIDES.get(date_str, {}).get(period, title)
                rows.append([
                    date_str, '월화수목금'[i], period,
                    TIMES[period - 1][0], TIMES[period - 1][1], title,
                    'PERSONAL_BASELINE_NOT_LIVE_VERIFIED',
                ])
    return rows


def live_rows(now, week_num, data, verification):
    """Never confuse the 6반 classroom with the user's 이동수업."""
    monday = week_monday(now, week_num)
    out = []
    for i, day in enumerate(data['lessons'][1:6]):
        date_str = (monday + timedelta(days=i)).isoformat()
        for period, lesson in enumerate(day[:7], start=1):
            if getattr(lesson, 'period', period) != period:
                raise RuntimeError('Unexpected Comcigan period ordering')
            original = getattr(lesson, 'original', None)
            out.append([
                date_str, '월화수목금'[i], period,
                getattr(lesson, 'subject', '') or '',
                getattr(lesson, 'teacher', '') or '',
                (getattr(original, 'subject', '') or '') if original else '',
                (getattr(original, 'teacher', '') or '') if original else '',
                'YES' if getattr(lesson, 'replaced', False) else 'NO',
                verification,
            ])
    if len(out) < 28:
        raise RuntimeError('Insufficient raw timetable rows')
    return out


def snapshot_signature(rows):
    """Ignore calendar labels and the 9th status column when comparing weeks."""
    return tuple(tuple(row[3:8]) for row in rows)


def sheets_session():
    raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not raw or not SHEET_ID:
        raise RuntimeError('Set GOOGLE_SERVICE_ACCOUNT_JSON and TIMETABLE_SHEET_ID GitHub secrets')
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return AuthorizedSession(creds)


def request_checked(session, method, url, **kwargs):
    response = session.request(method, url, timeout=30, **kwargs)
    if not response.ok:
        # Log API diagnostics without printing the service-account JSON.
        try:
            error = response.json().get('error', {})
            print('Google API:', error.get('status'), error.get('message'), file=sys.stderr)
        except (ValueError, AttributeError):
            pass
    response.raise_for_status()
    return response.json() if response.content else {}


def write_tabs(now, rows, reports):
    session = sheets_session()
    root = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
    metadata = request_checked(session, 'GET', root, params={'fields': 'sheets.properties(title)'})
    existing = {s['properties']['title'] for s in metadata.get('sheets', [])}
    names = ['개인기준시간표', '컴시간조회상태', '컴시간원본', '컴시간다음주']
    missing = [name for name in names if name not in existing]
    if missing:
        request_checked(session, 'POST', root + ':batchUpdate', json={
            'requests': [{'addSheet': {'properties': {'title': name}}} for name in missing]
        })

    ok = sum(reports[w]['rows'] is not None for w in (0, 1))
    same = reports[1]['verification'] == 'SAME_AS_THIS_WEEK_UNVERIFIED'
    if ok == 2:
        overall = 'ENC5_DECODED_BOTH_IDENTICAL_UNVERIFIED' if same else 'ENC5_DECODED_BOTH_UNMAPPED'
    elif ok == 1:
        overall = 'PARTIAL_FETCH_FAILED'
    else:
        overall = 'FETCH_FAILED_BOTH'

    status_rows = [
        ['항목', '값'],
        ['조회 시각(KST)', now.isoformat(timespec='seconds')],
        ['결과', overall],
        ['학교코드', str(SCHOOL_ID)],
        ['이번 주 범위', f"{week_monday(now, 0)} ~ {week_monday(now, 0) + timedelta(days=4)}"],
        ['다음 주 범위', f"{week_monday(now, 1)} ~ {week_monday(now, 1) + timedelta(days=4)}"],
        ['이번 주 요청 상태', reports[0]['status']],
        ['다음 주 요청 상태', reports[1]['status']],
        ['다음 주 데이터 검증', reports[1]['verification']],
        ['컴시간 원본 수업 수', str(reports[0]['count']) if reports[0]['rows'] is not None else '이전 데이터 유지'],
        ['다음 주 원본 수업 수', str(reports[1]['count']) if reports[1]['rows'] is not None else '이전 데이터 유지'],
        ['이번 주 컴시간 시작일(원문)', reports[0]['start_date']],
        ['다음 주 컴시간 시작일(원문)', reports[1]['start_date']],
        ['이번 주 컴시간 갱신일(원문)', reports[0]['update_date']],
        ['다음 주 컴시간 갱신일(원문)', reports[1]['update_date']],
        ['컴시간 변경사항 자동적용', '아니오 (이동수업 매핑 미검증)'],
        ['캘린더 변경', '없음'],
        ['실패 시 원본 처리', '각 주의 기존 성공 스냅샷 유지'],
    ]
    if same:
        status_rows.append(['주의', '두 요청의 수업 데이터가 동일함; 다음 주 확정 자료로 단정 금지'])

    # Update statuses for both weeks, but overwrite a raw tab ONLY after that
    # week's independent request succeeded. The other snapshot stays intact.
    payload = [
        {'range': "'개인기준시간표'!A1:G100", 'values': [
            ['날짜', '요일', '교시', '시작', '종료', '개인 과목', '검증 상태'], *rows]},
        {'range': "'컴시간조회상태'!A1:B30", 'values': status_rows},
    ]
    for week_num, title in ((0, '컴시간원본'), (1, '컴시간다음주')):
        raw = reports[week_num]['rows']
        if raw is not None:
            payload.append({'range': f"'{title}'!A1:I100", 'values': [RAW_HEADERS, *raw]})

    for title in ('개인기준시간표', '컴시간조회상태'):
        request_checked(session, 'POST', root + f"/values/'{title}'!A1:Z500:clear", json={})
    for week_num, title in ((0, '컴시간원본'), (1, '컴시간다음주')):
        if reports[week_num]['rows'] is not None:
            request_checked(session, 'POST', root + f"/values/'{title}'!A1:Z500:clear", json={})
    request_checked(session, 'POST', root + '/values:batchUpdate', json={
        'valueInputOption': 'RAW', 'data': payload,
    })
    return overall


def main():
    now = datetime.now(KST)
    if now.weekday() > 4 and os.getenv('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        print('Weekend schedule skipped; manual dispatch is allowed.')
        return

    reports = {}
    for week_num in (0, 1):
        label = 'THIS_WEEK' if week_num == 0 else 'NEXT_WEEK'
        report = {'rows': None, 'count': 0, 'status': '', 'verification': '',
                  'start_date': '', 'update_date': ''}
        try:
            result = fetch_live(week_num)
            report['count'] = result['lesson_count']
            report['start_date'] = result['start_date']
            report['update_date'] = result['update_date']
            report['verification'] = verify_week_start(now, week_num, result['start_date'])
            report['rows'] = live_rows(now, week_num, result, report['verification'])
            report['status'] = 'ENC5_DECODED'
            print(f'{label}: enc5 decoded; {report["count"]} classes; raw rows={len(report["rows"])}')
        except Exception as exc:
            report['status'] = 'FETCH_FAILED: ' + type(exc).__name__
            report['verification'] = 'NO_NEW_DATA'
            print(f'{label} failed; existing raw snapshot will be preserved: {exc!r}', file=sys.stderr)
        reports[week_num] = report

    if reports[0]['rows'] is not None and reports[1]['rows'] is not None:
        current, upcoming = reports[0]['rows'], reports[1]['rows']
        if snapshot_signature(current) == snapshot_signature(upcoming):
            reports[1]['verification'] = 'SAME_AS_THIS_WEEK_UNVERIFIED'
            for row in upcoming:
                row[8] = reports[1]['verification']
            print('WARNING: Comcigan returned identical lesson grids for both week_num=0/1. '
                  'Do not treat next-week dates as independently confirmed.')
        else:
            changes = sum(a != b for a, b in zip(snapshot_signature(current), snapshot_signature(upcoming)))
            print(f'Week-to-week differing timetable cells: {changes}')

    baseline = regular_rows(now)
    overall = write_tabs(now, baseline, reports)
    print(f'Sheets updated: {overall}; personal baseline rows={len(baseline)}; '
          f'calendar writes=0')
    if any(reports[w]['rows'] is None for w in (0, 1)):
        raise RuntimeError('At least one Comcigan week failed; see 컴시간조회상태; successful snapshots were preserved')


if __name__ == '__main__':
    main()
