# 컴시간 기반 학교 시간표 자동화 (검증 단계)

- 대상: 휘문고등학교 2학년 6반 / 내부 코드 69622
- 나이스(NEIS): **사용하지 않음**
- GitHub Actions: 한국시간 평일 06:20 (GitHub 예약 실행은 지연 가능)
- 이 버전은 컴시간 `enc5` 복호화 및 **기존 Google Sheets의 별도 검증 탭 기록**을 수행합니다.
- **주의:** 컴시간의 학급별 교시 변경을 개인 이동수업으로 확정 변환하는 코드는 아직 검증되지 않았습니다. 따라서 기존 `시간표` 탭과 Google Calendar는 절대로 덮어쓰지 않습니다.

## 1. GitHub에 업로드

이 압축 파일의 `sync.py`, `requirements.txt`, `.github/workflows/sync.yml`을 저장소 루트에 넣습니다. `.github` 폴더 경로를 그대로 유지하세요.

## 2. Google 인증

Google Cloud에서 Google Sheets API를 활성화하고 서비스 계정을 만듭니다. 서비스 계정의 이메일 주소를 기존 Google Sheets 문서의 **편집자**로 공유합니다. 서비스 계정 JSON을 GitHub 저장소 Settings → Secrets and variables → Actions → New repository secret에 `GOOGLE_SERVICE_ACCOUNT_JSON`이라는 이름으로 저장합니다. **JSON 키를 코드나 공개 저장소에 커밋하지 마세요.**

동일한 화면에서 `TIMETABLE_SHEET_ID`를 추가하고 기존 시트 ID `1iZ5W1jeqYCfukZQh4oQxbeOuSSA0DYJwerbFyq70dyE`를 값으로 저장합니다.

## 3. 테스트

Actions → Comcigan timetable fetch → Run workflow를 실행합니다. 실행 로그의 `Comcigan decoded` 및 Google Sheets의 `컴시간조회상태` 탭을 확인합니다. `decode_response`의 패키지 버전 호환성은 아직 실서버에서 검증되지 않았습니다. 실패 시 로그를 보고 수정해야 합니다.

## 4. 다음 단계

`자료178`과 `자료153`의 주차·학급·교시 매핑, 이동수업 변환을 실제 응답으로 검증한 후에만 Calendar 쓰기를 활성화합니다. 현재 ChatGPT의 06:40 자동화가 Sheets를 신뢰하도록 변경한 것은 아니므로, 원본 검증 탭만으로 자동 반영이 완료되었다고 간주하면 안 됩니다.
