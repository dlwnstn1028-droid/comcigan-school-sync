"""Fetch BOTH Comcigan weeks and safely stage them in the existing Google Sheet.

week_num=0 (this week) remains in 컴시간원본 for backwards compatibility.
week_num=1 (next week) is independently requested and stored in 컴시간다음주.
Never infer the personal elective timetable or edit Google Calendar here.
NEIS is not accessed. A failed week never overwrites its last successful snapshot.
"""

import json
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

KST = timezone(timedelta(hours=9))
SCHOOL_ID = 69622
SHEET_ID = os.getenv('TIMETABLE_SHEET_ID', '')

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
            else 'NEXT_WEEK_DATE_VERIFIED')


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
    if lesson_count > 35:
        raise RuntimeError('Invalid class lesson count')

    return {
        'school': table.school_name,
        'class': (2, 6),
        'lessons': lessons,
        'lesson_count': lesson_count,
        'start_date': str(getattr(table, 'start_date', '')),
        'update_date': str(getattr(table, 'update_date', '')),
    }


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
    if len(out) != 35:
        raise RuntimeError('Expected 35 explicit weekday/period cells')
    for row in out:
        if bool(row[3]) != bool(row[4]):
            raise RuntimeError('Incomplete subject/teacher pair')
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


def normalized(rows, width):
    return [[str(v) if v is not None else '' for v in (r + [''] * width)[:width]] for r in rows]


def digest(rows):
    return hashlib.sha256(json.dumps(normalized(rows, 9), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def write_tabs(now, reports):
    session = sheets_session()
    root = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
    metadata = request_checked(session, 'GET', root, params={'fields': 'sheets.properties(sheetId,title)'})
    sheets = {x['properties']['title']: x['properties']['sheetId'] for x in metadata['sheets']}
    names = ['컴시간조회상태', '컴시간원본', '컴시간다음주']
    missing = [name for name in names if name not in sheets]
    if missing:
        raise RuntimeError('Required original tabs missing; preserve existing data: ' + ', '.join(missing))
    ok = sum(reports[w]['rows'] is not None for w in (0, 1))
    overall = 'COLLECTION_BOTH_DATE_VERIFIED' if ok == 2 else 'PARTIAL_FETCH_FAILED' if ok else 'FETCH_FAILED_BOTH'
    status = [['항목', '값'], ['조회 시각(KST)', now.isoformat(timespec='seconds')], ['결과', overall],
              ['학교코드', str(SCHOOL_ID)], ['학년', '2'], ['반', '6'],
              ['이번 주 범위', str(week_monday(now, 0))], ['다음 주 범위', str(week_monday(now, 1))],
              ['이번 주 요청 상태', reports[0]['status']], ['다음 주 요청 상태', reports[1]['status']],
              ['다음 주 데이터 검증', reports[1]['verification']],
              ['컴시간 원본 수업 수', str(reports[0]['count']) if reports[0]['rows'] is not None else '이전 데이터 유지'],
              ['다음 주 원본 수업 수', str(reports[1]['count']) if reports[1]['rows'] is not None else '이전 데이터 유지'],
              ['이번 주 컴시간 시작일(원문)', reports[0]['start_date']], ['다음 주 컴시간 시작일(원문)', reports[1]['start_date']],
              ['이번 주 컴시간 갱신일(원문)', reports[0]['update_date']], ['다음 주 컴시간 갱신일(원문)', reports[1]['update_date']],
              ['수집기 역할', '학교 원본 수집 및 저장 재조회'], ['실패 시 원본 처리', '실패한 주 기존 스냅샷 유지'],
              ['이번 주 원본 SHA256', digest([RAW_HEADERS, *reports[0]['rows']]) if reports[0]['rows'] is not None else ''],
              ['다음 주 원본 SHA256', digest([RAW_HEADERS, *reports[1]['rows']]) if reports[1]['rows'] is not None else '']]
    writes = [('컴시간조회상태', status, 2)]
    for w, title in ((0, '컴시간원본'), (1, '컴시간다음주')):
        if reports[w]['rows'] is not None:
            writes.append((title, [RAW_HEADERS, *reports[w]['rows']], 9))
    previous = {}
    for title, rows, width in writes:
        previous[title] = request_checked(session, 'GET', root + '/values/' + title + '!A1:' + ('B40' if width == 2 else 'I100')).get('values', [])
    def update(entries):
        requests = []
        for title, rows, width in entries:
            requests.append({'updateCells': {'range': {'sheetId': sheets[title], 'startRowIndex': 0, 'endRowIndex': 40 if width == 2 else 100, 'startColumnIndex': 0, 'endColumnIndex': width},
                'rows': [{'values': [{'userEnteredValue': {'stringValue': v}} for v in r]} for r in normalized(rows, width)], 'fields': 'userEnteredValue'}})
        request_checked(session, 'POST', root + ':batchUpdate', json={'requests': requests})
    def read_all():
        return {title: request_checked(session, 'GET', root + '/values/' + title + '!A1:' + ('B40' if width == 2 else 'I100')).get('values', [])
                for title, _, width in writes}

    def matches(actual, expected):
        return all(normalized(actual[title], width) == normalized(expected[title], width)
                   for title, _, width in writes)

    # One atomic Sheets request. A lost response is resolved by reads, never a
    # blind write retry. If reads also fail, report unknown completion.
    expected = {title: rows for title, rows, _ in writes}
    write_error = None
    try:
        update(writes)
    except Exception as exc:
        write_error = exc
    actual = read_all()
    if not matches(actual, expected):
        if write_error is not None and matches(actual, previous):
            raise write_error
        mismatch = next(title for title, _, width in writes
                        if normalized(actual[title], width) != normalized(expected[title], width))
        # Only an observed mismatch permits restoring the captured original.
        # A lost rollback response also requires readback, without retrying it.
        rollback_error = None
        try:
            update([(title, previous[title], width) for title, _, width in writes])
        except Exception as exc:
            rollback_error = exc
        restored = read_all()
        for title, rows, width in writes:
            if normalized(restored[title], width) != normalized(previous[title], width):
                raise RuntimeError('Rollback verification failed: ' + title) from rollback_error
        print('COLLECTOR_ROLLBACK_VERIFIED', file=sys.stderr)
        raise RuntimeError('Sheet readback mismatch: ' + mismatch + '; original restored and verified') from write_error
    evidence = {'schema': 2, 'schoolCode': SCHOOL_ID, 'grade': 2, 'classNumber': 6, 'observedAt': now.isoformat(), 'sheetReadback': True,
                'weeks': [{'week': w, 'startDate': reports[w]['start_date'], 'rowCount': len(reports[w]['rows']) if reports[w]['rows'] is not None else 0,
                           'sha256': digest([RAW_HEADERS, *reports[w]['rows']]) if reports[w]['rows'] is not None else None} for w in (0, 1)]}
    print('COLLECTOR_EVIDENCE ' + json.dumps(evidence, ensure_ascii=False, separators=(',', ':')))
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

    overall = write_tabs(now, reports)
    print(f'Collection and sheet readback: {overall}')
    if any(reports[w]['rows'] is None for w in (0, 1)):
        raise RuntimeError('At least one Comcigan week failed; see 컴시간조회상태; successful snapshots were preserved')


if __name__ == '__main__':
    main()
