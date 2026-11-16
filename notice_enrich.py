"""Private school notice text/PDF enrichment.

Privacy: NEVER log authenticated page bodies, full URLs, IDs, PDF bytes or
cookies. This module writes extracted PRIVATE data only to the user's Sheets;
PDF binary backup requires a separately shared private Drive folder.
Opening unread eAlimi detail pages MAY mark them read. This is user-approved.
"""
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
RAW_TAB = '학교공지_원문'
BODY_TAB = '학교공지_본문'
ATTACH_TAB = '학교공지_첨부'
REVIEW_TAB = '학교공지_검토'
RAW_HEADERS = ['source','notice_id','title','detail_url','published_at','fetched_at_kst',
               'content_status','read_effect','body_chars','attachments_found',
               'pdf_extracted','body_fingerprint','failure_reason']
BODY_HEADERS = ['source','notice_id','segment_no','kind','text','source_url',
                'saved_at_kst','content_sha256']
ATTACH_HEADERS = ['source','notice_id','attachment_id','filename','source_url','mime_type',
                  'bytes','parse_status','text_chars','fetched_at_kst','drive_file_url']
REVIEW_HEADERS = ['candidate_id','source','notice_id','category','proposed_title',
                  'event_date','start_time','end_time','evidence_excerpt','source_url',
                  'decision','calendar_event_id','last_checked_kst','notes']
SCHEMAS = {RAW_TAB: RAW_HEADERS,BODY_TAB: BODY_HEADERS,
           ATTACH_TAB: ATTACH_HEADERS,REVIEW_TAB: REVIEW_HEADERS}
MAX_PDF = 10 * 1024 * 1024
DATE_ISO = re.compile(r'(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})')
DATE_KR = re.compile(r'(?<!\d)(?:(20\d{2})\s*년\s*)?(1[0-2]|0?[1-9])\s*월\s*(3[01]|[12]\d|0?[1-9])\s*일')
DATE_MD = re.compile(r'(?<!\d)(1[0-2]|0?[1-9])\s*/\s*(3[01]|[12]\d|0?[1-9])(?!\d)')
TIME_HM = re.compile(r'(?:(오전|오후)\s*)?(\d{1,2})\s*(?::\s*([0-5]\d)|시\s*(?:([0-5]?\d)\s*분)?)')
CATEGORY_RULES = [
    ('시험', ('시험','기말','중간','고사','평가범위')),
    ('수행평가', ('수행평가','말하기 평가','실험 평가')),
    ('신청마감', ('신청','접수','마감','제출','납부','등록','수강')),
    ('발표', ('발표','세미나','콘서트','탐구')),
    ('학교일정', ('휴업','하교','개학','방학','수능','등교','행사','학사')),
]


def now_kst():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def digest(value):
    if isinstance(value,str):
        value=value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def clean_text(value):
    value = re.sub(r'[\u200b-\u200d\ufeff]', '', value or '')
    return re.sub(r'\s+', ' ', value).strip()


def safe_url(url, public=False):
    p=urlparse(url)
    hosts={'www.ealimi.com','ealimi.com'} if not public else {'www.whimoon.hs.kr','whimoon.hs.kr'}
    return p.scheme=='https' and p.hostname in hosts and not p.username and not p.password


def safe_segment(text, size=16000):
    text=text[:100000]
    return [text[i:i+size] for i in range(0,len(text),size)]


def extract_main_text(html):
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup.select('script,style,noscript,nav,header,footer,form,svg,button,[role="navigation"]'):
        tag.decompose()
    # Use candidates containing the actual notice letter if available.
    selectors=['.letter-content','.letter_content','.received_content','.receive-content',
               '.notice-content','.notice_content','.board-view','.board_view',
               '#detailContent','#contentBody','#letterContent','.v4_content_box',
               'main','article','[role="main"]']
    choices=[]
    for selector in selectors:
        for node in soup.select(selector)[:12]:
            t=clean_text(node.get_text(' ',strip=True))
            if len(t)>=15:
                choices.append(t)
    # If no trustworthy content container exists use body with a conservative cap.
    # The fallback is marked PARTIAL so it is never mistaken for full-document proof.
    if choices:
        text=max(choices,key=len)
        status='TEXT_EXTRACTED'
    else:
        text=clean_text((soup.body or soup).get_text(' ',strip=True))
        status='BODY_FALLBACK' if text else 'EMPTY'
    # Avoid logging sensitive text. Only store it in the user's private sheet.
    return text[:100000], status


def discover_attachments(html, current_url, public=False):
    soup=BeautifulSoup(html,'html.parser')
    found={}
    for node in soup.select('a[href],iframe[src],embed[src],object[data]'):
        rel=node.get('href') or node.get('src') or node.get('data') or ''
        full=urljoin(current_url,rel)
        if not safe_url(full,public=public):
            continue
        text=clean_text(node.get_text(' ',strip=True)).lower()
        lower=full.lower()
        plausible=(lower.endswith('.pdf') or '.pdf?' in lower or '.pdf&' in lower
                   or any(word in lower for word in ('download','filedown','filedownload','/file/','/attach','/pdf/'))
                   or any(word in text for word in ('pdf','첨부','다운로드','파일')))
        if not plausible:
            continue
        # Do not download a notice-detail route or generic sharing link.
        path=urlparse(full).path.lower()
        if path.startswith('/receivednoti/content') or path.startswith('/receivednoti/letterreply'):
            continue
        fn=clean_text(node.get('download') or node.get('title') or node.get_text(' ',strip=True) or '')[:130]
        if not fn:
            fn=path.rsplit('/',1)[-1][:100] or 'attachment'
        found[full]=fn
        if len(found)>=8:
            break
    return list(found.items())


def parse_pdf(content, max_pages=40):
    if not content.startswith(b'%PDF'):
        return '', 'NOT_PDF'
    try:
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            return '', 'ENCRYPTED_PDF'
        strings=[]
        for page in reader.pages[:max_pages]:
            t=page.extract_text() or ''
            if t:strings.append(t)
        text=clean_text('\n'.join(strings))[:110000]
        if not text:
            return '', 'OCR_REQUIRED'
        return text,'PDF_TEXT_EXTRACTED' if len(reader.pages)<=max_pages else 'PDF_PARTIAL_MAX_PAGES'
    except Exception:
        return '', 'PDF_PARSE_FAILED'


def categorize(text):
    for cat,words in CATEGORY_RULES:
        if any(w in text for w in words):return cat
    return ''


def time_near(text):
    m=TIME_HM.search(text)
    if not m:return ''
    hh=int(m[2]); mm=int(m[3] or m[4] or 0)
    if m[1]=='오후' and hh<12:hh+=12
    if m[1]=='오전' and hh==12:hh=0
    if hh>23:return ''
    return f'{hh:02}:{mm:02}'


def extract_review_candidates(source,notice_id,title,body,published,url,now=None):
    """Generate unapproved candidates only. Never send them to Calendar directly."""
    now=now or datetime.now(KST)
    default_year=int(published[:4]) if published and len(published)>=4 and published[:4].isdigit() else now.year
    # A bounded local context around each date prevents using unrelated words
    # at the other end of a long notice to invent a deadline.
    matches=[]
    for pattern,style in [(DATE_ISO,'ymd'),(DATE_KR,'kr'),(DATE_MD,'md')]:
        for m in pattern.finditer(body[:100000]):
            if style=='ymd':year,month,day=map(int,m.groups());year_inferred=False
            elif style=='kr':year=int(m[1]) if m[1] else default_year;month=int(m[2]);day=int(m[3]);year_inferred=not bool(m[1])
            else:year=default_year;month=int(m[1]);day=int(m[2]);year_inferred=True
            try:date=datetime(year,month,day).date().isoformat()
            except ValueError:continue
            before=body[max(0,m.start()-100):m.start()]
            after=body[m.end():m.end()+100]
            context=clean_text(before+' '+m.group(0)+' '+after)
            category=categorize(context)
            if not category:continue
            # Preserve source evidence; do not imply that this is a definite event.
            tm=time_near(after[:45])
            # Title is a readable suggestion; no automatic event creation.
            short=clean_text(title)[:85] or '학교 공지 일정'
            proposed=f'{category} · {short}'
            ident=digest('|'.join([source,notice_id,date,category,tm,short]))[:24]
            reason='연도 추정: 확인 필요' if year_inferred else '원문 날짜 확인 필요'
            matches.append([ident,source,notice_id,category,proposed,date,tm,'',
                            context[:220],url,'승인 대기','','',reason])
    uniq={r[0]:r for r in matches}
    return list(uniq.values())[:40]


def sheet_values(session,sid,tab,range_):
    from urllib.parse import quote
    loc=quote(f"'{tab}'!{range_}",safe='')
    r=session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{loc}',timeout=25)
    r.raise_for_status()
    return r.json().get('values',[])


def sheet_append(session,sid,tab,values):
    if not values:return
    from urllib.parse import quote
    loc=quote(f"'{tab}'!A:O",safe='')
    r=session.post(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{loc}:append',
                   params={'valueInputOption':'RAW','insertDataOption':'INSERT_ROWS'},
                   json={'values':values},timeout=30)
    r.raise_for_status()


def sheet_update(session,sid,tab,row,values):
    from urllib.parse import quote
    width=len(values)
    end_col=chr(64+width) if width<=26 else 'Z'
    loc=quote(f"'{tab}'!A{row}:{end_col}{row}",safe='')
    r=session.put(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{loc}',
                  params={'valueInputOption':'RAW'},json={'values':[values]},timeout=25)
    r.raise_for_status()


def ensure_tabs(session,sid):
    r=session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}',
                  params={'fields':'sheets.properties.title'},timeout=25)
    r.raise_for_status()
    have={x['properties']['title'] for x in r.json().get('sheets',[])}
    missing=[x for x in SCHEMAS if x not in have]
    if missing:
        r=session.post(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate',
                       json={'requests':[{'addSheet':{'properties':{'title':x}}} for x in missing]},timeout=25)
        r.raise_for_status()
    for tab,header in SCHEMAS.items():
        value=sheet_values(session,sid,tab,'A1:O1')
        if value and value[0][:len(header)]==header:continue
        if value and value[0]:raise RuntimeError('ENRICH_HEADER_MISMATCH:'+tab)
        sheet_update(session,sid,tab,1,header)


def initial_state(session,sid):
    raw=sheet_values(session,sid,RAW_TAB,'A2:M8000')
    body=sheet_values(session,sid,BODY_TAB,'A2:H15000')
    attached=sheet_values(session,sid,ATTACH_TAB,'A2:K10000')
    review=sheet_values(session,sid,REVIEW_TAB,'A2:N15000')
    raw_index={(r[0],r[1]):(i+2,r) for i,r in enumerate(raw) if len(r)>1}
    body_index={(r[0],r[1],r[2],r[7]) for r in body if len(r)>7}
    attach_index={(r[0],r[1],r[2]) for r in attached if len(r)>2}
    review_ids={r[0] for r in review if r}
    return raw_index,body_index,attach_index,review_ids


def refresh_needed(previous,refresh_days=7):
    if not previous:return True
    old=previous[1]
    if len(old)<8 or old[6] not in ('TEXT_EXTRACTED','BODY_FALLBACK','PDF_TEXT_EXTRACTED','PARTIAL'):
        return True
    if len(old)>5:
        try:stamp=datetime.strptime(old[5],'%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
        except ValueError:return True
        return datetime.now(KST)-stamp>timedelta(days=refresh_days)
    return True


def fetch_pdf_in_session(context,url,public=False):
    if not safe_url(url,public=public):return None,'HOST_REJECTED'
    try:
        resp=context.request.get(url,timeout=18000,fail_on_status_code=False)
        if not resp.ok:return None,'HTTP_ERROR'
        size=resp.headers.get('content-length','')
        if size.isdecimal() and int(size)>MAX_PDF:return None,'TOO_LARGE'
        body=resp.body()
        if len(body)>MAX_PDF:return None,'TOO_LARGE'
        if not body.startswith(b'%PDF'):return None,'NOT_PDF'
        return body,'PDF'
    except Exception:return None,'FETCH_FAILED'


def archive_to_drive(data,name,notice_id):
    """Optional user-owned private Drive folder, shared with service account.

    If folder is not configured/accessible, return status; do not leak bytes to
    GitHub artifacts or unauthenticated storage. Credentials never logged.
    """
    folder=os.getenv('NOTICE_DRIVE_FOLDER_ID','').strip()
    if not folder:return '', 'DRIVE_NOT_CONFIGURED'
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        raw=json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
        creds=service_account.Credentials.from_service_account_info(
            raw,scopes=['https://www.googleapis.com/auth/drive.file'])
        session=AuthorizedSession(creds)
        # Multipart upload with random boundary; private data only over HTTPS to Google.
        import secrets
        boundary='notice_'+secrets.token_hex(14)
        metadata={'name':f"{notice_id[:32]}_{re.sub(r'[^a-zA-Z0-9가-힣._-]','_',name)[:75]}.pdf",
                  'parents':[folder], 'mimeType':'application/pdf', 'description':'학교 공지 첨부 백업'}
        encoded=(('--'+boundary+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'+
                  json.dumps(metadata,ensure_ascii=False)+'\r\n--'+boundary+
                  '\r\nContent-Type: application/pdf\r\n\r\n').encode('utf-8')+
                 data+('\r\n--'+boundary+'--\r\n').encode('utf-8'))
        resp=session.post('https://www.googleapis.com/upload/drive/v3/files',
                          params={'uploadType':'multipart','fields':'id,webViewLink'},
                          data=encoded,headers={'Content-Type':f'multipart/related; boundary={boundary}'},timeout=40)
        if resp.status_code not in (200,201):return '', 'DRIVE_PERMISSION_OR_API_ERROR'
        info=resp.json()
        file_id=info.get('id','')
        return info.get('webViewLink') or ('https://drive.google.com/file/d/'+file_id+'/view' if file_id else ''),'DRIVE_SAVED'
    except Exception:return '', 'DRIVE_UPLOAD_FAILED'


def process_detail(page,record,session,sid,state,public=False):
    raw_index,body_index,attach_index,review_ids=state
    source=record['source'];nid=str(record['notice_id']);url=record['url']
    if not safe_url(url,public=public):return 'INVALID_URL',0,0,0
    note='READ_STATE_MAY_CHANGE' if not public else 'PUBLIC_PAGE'
    try:
        page.goto(url,wait_until='domcontentloaded',timeout=18000)
        page.wait_for_timeout(500)
        # Guard redirects (private page must remain on an eAlimi notice route).
        if not safe_url(page.url,public=public):raise ValueError('HOST_REDIRECT')
        if not public and '/member/signin' in urlparse(page.url).path.lower():
            raise ValueError('AUTH_EXPIRED')
        body_html=page.content()
        text,status=extract_main_text(body_html)
        attachments=discover_attachments(body_html,page.url,public=public)
        if not text and not attachments:raise ValueError('NO_BODY_OR_ATTACHMENTS')
        pdf_ok=0;stored=0;docs=[];attachment_rows=[]
        for url_file,filename in attachments[:4]:
            attachment_id=digest(url_file)[:24]
            if (source,nid,attachment_id) in attach_index:continue
            data,fetch_status=fetch_pdf_in_session(page.context,url_file,public=public)
            parsed_text='';file_url='';parse_status=fetch_status
            if data:
                parsed_text,parse_status=parse_pdf(data)
                if parsed_text:pdf_ok+=1;docs.append(('pdf',parsed_text,url_file))
                file_url,backup_status=archive_to_drive(data,filename,nid)
                if backup_status!='DRIVE_SAVED':parse_status+=';'+backup_status
            attachment_rows.append([source,nid,attachment_id,filename,url_file,
                                    'application/pdf' if data else 'unknown',len(data) if data else 0,
                                    parse_status,len(parsed_text),now_kst(),file_url])
        if text:docs.insert(0,('body',text,url))
        segments=[];candidates=[]
        for kind,content,origin in docs:
            bodyhash=digest(content)
            for n,part in enumerate(safe_segment(content),start=1):
                key=(source,nid,str(n),bodyhash)
                if key in body_index:continue
                segments.append([source,nid,str(n),kind,part,origin,now_kst(),bodyhash])
            candidates+=extract_review_candidates(source,nid,record.get('title',''),content,
                                                 record.get('published_at',''),record['url'])
        for row in candidates:row[-2]=now_kst()
        new_review=[r for r in candidates if r[0] not in review_ids]
        for row in new_review:review_ids.add(row[0])
        # Commit body before its summary; retry remains idempotent via fingerprints.
        sheet_append(session,sid,BODY_TAB,segments)
        sheet_append(session,sid,ATTACH_TAB,attachment_rows)
        sheet_append(session,sid,REVIEW_TAB,new_review)
        for x in segments:body_index.add((x[0],x[1],x[2],x[7]))
        for x in attachment_rows:attach_index.add((x[0],x[1],x[2]))
        reportstatus='PDF_TEXT_EXTRACTED' if pdf_ok else status
        rawrow=[source,nid,record.get('title',''),record['url'],record.get('published_at',''),
                now_kst(),reportstatus,note,len(text),len(attachments),pdf_ok,
                digest(text),'']
        prev=raw_index.get((source,nid))
        if prev:sheet_update(session,sid,RAW_TAB,prev[0],rawrow)
        else:
            sheet_append(session,sid,RAW_TAB,[rawrow])
            raw_index[(source,nid)]=(len(raw_index)+2,rawrow)
        return 'SAVED',len(segments),len(attachment_rows),len(new_review)
    except Exception as e:
        # Keep diagnosis private. Never print page contents or response error text.
        error_code=type(e).__name__ if not isinstance(e,ValueError) else str(e)
        rawrow=[source,nid,record.get('title',''),url,record.get('published_at',''),
                now_kst(),'FAILED',note,0,0,0,'',error_code[:55]]
        prev=raw_index.get((source,nid))
        if prev:sheet_update(session,sid,RAW_TAB,prev[0],rawrow)
        else:
            sheet_append(session,sid,RAW_TAB,[rawrow])
            raw_index[(source,nid)]=(len(raw_index)+2,rawrow)
        return 'FAILED',0,0,0


def enrich_authenticated(page,notices,session,sid):
    ensure_tabs(session,sid)
    state=initial_state(session,sid)
    max_n=max(1,min(12,int(os.getenv('EALIMI_DETAIL_MAX_PER_RUN','5'))))
    refresh=max(1,min(30,int(os.getenv('NOTICE_REFRESH_DAYS','7'))))
    candidates=[n for n in notices if refresh_needed(state[0].get((n['source'],str(n['notice_id']))),refresh)]
    stats={'discovered':len(notices),'queued':len(candidates),'processed':0,
           'failed':0,'text_segments':0,'attachments':0,'review_candidates':0}
    for record in candidates[:max_n]:
        status,body,attachments,review=process_detail(page,record,session,sid,state)
        stats['processed']+=status=='SAVED';stats['failed']+=status=='FAILED'
        stats['text_segments']+=body;stats['attachments']+=attachments;stats['review_candidates']+=review
    return stats


def enrich_public(notices_by_source,session,sid):
    """Read public detail content for new/stale notices only; no browser install."""
    ensure_tabs(session,sid)
    state=initial_state(session,sid)
    budget=max(1,min(15,int(os.getenv('PUBLIC_DETAIL_MAX_PER_RUN','6'))))
    refresh=max(1,min(30,int(os.getenv('NOTICE_REFRESH_DAYS','7'))))
    candidates=[n for group in notices_by_source.values() for n in group
                if refresh_needed(state[0].get((n['source'],str(n['notice_id']))),refresh)]
    stats={'discovered':sum(map(len,notices_by_source.values())), 'queued':len(candidates),
           'processed':0,'failed':0,'text_segments':0,'attachments':0,'review_candidates':0}
    # Public site never needs Chromium: reuse a lightweight requests adapter.
    class PublicPage:
        def __init__(self):
            from types import SimpleNamespace
            self.session=requests.Session()
            self.context=SimpleNamespace(request=SimpleNamespace(get=self.pdf_get))
            self.url=''
            self.html=''
        def pdf_get(self,url,timeout=18000,fail_on_status_code=False):
            from types import SimpleNamespace
            resp=self.session.get(url,timeout=max(1,timeout/1000))
            return SimpleNamespace(ok=resp.ok,headers=resp.headers,body=lambda:resp.content)
        def goto(self,url,wait_until=None,timeout=18000):
            resp=self.session.get(url,timeout=max(1,timeout/1000))
            resp.raise_for_status()
            self.url=resp.url
            self.html=resp.text
        def wait_for_timeout(self,unused):
            return None
        def content(self):
            return self.html
    page=PublicPage()
    for record in candidates[:budget]:
        st,body,atts,rev=process_detail(page,record,session,sid,state,public=True)
        stats['processed']+=st=='SAVED';stats['failed']+=st=='FAILED'
        stats['text_segments']+=body;stats['attachments']+=atts;stats['review_candidates']+=rev
    return stats