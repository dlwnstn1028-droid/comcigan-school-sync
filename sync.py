"""Fetch Comcigan and safely stage data in an existing Google Sheet.

Does NOT write to Google Calendar. Unverified live changes are never promoted
as confirmed personal timetable. No NEIS calls are made.
"""
import base64
import importlib
import json
import os
import pkgutil
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

KST = timezone(timedelta(hours=9))
SCHOOL_ID = 69622
SHEET_ID = os.getenv('TIMETABLE_SHEET_ID', '')
BASE = 'http://comci.net:4082'
ENDPOINT = '666823'  # last observed; may change
PREFIX = '18066_'   # last observed; may change

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


def find_decoder():
    """Find decoder already used in previous Colab tests (pycomcigan)."""
    package = importlib.import_module('pycomcigan')
    modules = [package]
    if hasattr(package, '__path__'):
        for info in pkgutil.walk_packages(package.__path__, package.__name__ + '.'):
            try:
                modules.append(importlib.import_module(info.name))
            except (ImportError, RuntimeError):
                pass
    for mod in modules:
        fn = getattr(mod, 'decode_response', None)
        if callable(fn):
            return fn
    raise RuntimeError('pycomcigan decode_response not found; check package version')


def fetch_live():
    s = requests.Session()
    s.headers.update({'User-Agent':'Mozilla/5.0','Referer':BASE+'/st'})
    # Discover from the live page if available; never claim discovery when fallback used.
    endpoint, prefix = ENDPOINT, PREFIX
    try:
        html = s.get(BASE+'/st', timeout=12).text
        match = re.search(r"sc_data\s*\(\s*['\"](\d+_)['\"]", html)
        if match:
            prefix = match.group(1)
        match = re.search(r"(?:4082/|['\"]/)\s*(\d{5,6})(?:\?|['\"])", html)
        if match:
            endpoint = match.group(1)
    except requests.RequestException:
        pass
    # Week selector 0 was observed, but its date semantics have not been proven.
    query = base64.b64encode(f'{prefix}{SCHOOL_ID}_0_0_enc5'.encode()).decode()
    url = f'{BASE}/{endpoint}?{quote(query, safe="")}'
    resp = s.get(url, timeout=25)
    resp.raise_for_status()
    if not resp.text.startswith('enc5:'):
        raise RuntimeError('Unexpected response (not enc5)')
    decoded = find_decoder()(resp.text)
    if not isinstance(decoded, dict) or not all(k in decoded for k in ('자료109','자료126','자료178')):
        raise RuntimeError('Decoder returned unexpected data; aborting sheet write')
    if decoded.get('학교명') and '휘문' not in str(decoded['학교명']):
        raise RuntimeError('School mismatch; aborting sheet write')
    return decoded


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

    if not r.ok:
        try:
            err = r.json().get("error", {})
            print("Google API 오류 코드:", err.get("code"))
            print("Google API 오류 사유:", err.get("status"))
            print("Google API 메시지:", err.get("message"))
        except ValueError:
            print("Google API 응답을 해석할 수 없습니다.")

    r.raise_for_status()
    return r.json() if r.content else {}



def write_tabs(now, rows, status):
    session = sheets_session()
    root = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}'
    meta = request_checked(session,'GET',root,params={'fields':'sheets.properties(title)'})
    existing = {x['properties']['title'] for x in meta.get('sheets',[])}
    names = ['개인기준시간표','컴시간조회상태']
    missing = [n for n in names if n not in existing]
    if missing:
        request_checked(session,'POST',root+':batchUpdate',json={
            'requests':[{'addSheet':{'properties':{'title':n}}} for n in missing]
        })
    stamp = now.isoformat(timespec='seconds')
    payload = {
      'valueInputOption':'RAW',
      'data':[
        {'range':"'개인기준시간표'!A1:G100",'values':[
          ['날짜','요일','교시','시작','종료','개인 과목','검증 상태'],*rows]},
        {'range':"'컴시간조회상태'!A1:B10",'values':[
          ['항목','값'],['조회 시각(KST)',stamp],['결과',status],
          ['학교코드',str(SCHOOL_ID)],['컴시간 변경사항 적용','아니오 (미검증)'],
          ['캘린더 변경','없음']]}]
    }
    # Clear only automation-owned tabs, NEVER the existing original '시간표' tab.
    for n in names:
        request_checked(session,'POST',root+f"/values/'{n}'!A1:Z500:clear",json={})
    request_checked(session,'POST',root+'/values:batchUpdate',json=payload)


def main():
    now = datetime.now(KST)
    if now.weekday() > 4 and os.getenv('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        print('Weekend; skipping')
        return
    rows = regular_rows(now)
    try:
        data = fetch_live()
        status = 'ENC5_DECODED_UNMAPPED'
        print('Comcigan decoded. Keys:', ', '.join(k for k in ('자료109','자료126','자료178') if k in data))
    except Exception as e:
        status = 'FETCH_FAILED: '+type(e).__name__
        print('Live fetch not verified:', repr(e), file=sys.stderr)
    # Baseline and status only. No unverified substitutions or calendar writes.
    write_tabs(now, rows, status)
    print(f'Updated safe staging tabs; status={status}; baseline rows={len(rows)}')
    if status.startswith('FETCH_FAILED'):
        raise RuntimeError('Live Comcigan fetch failed; staging marked accordingly')

if __name__ == '__main__':
    main()
