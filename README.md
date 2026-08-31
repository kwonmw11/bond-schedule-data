# bond-schedule-data — 국고채·통안증권 공고 실시간 수집기

한국은행 게시판(RSS)과 KDI 경제정보센터에 올라오는 국고채·통안증권 공고(발행공고, 중도환매·모집·교환·매입, 월간 발행계획)를 **평일 15분~1시간 주기**로 수집해, 본문과 **첨부 PDF의 텍스트·표까지 추출**한 JSON을 이 레포에 커밋합니다. Claude의 예약 작업(평일 11:15·17:15 KST)이 이 JSON을 읽어 구글캘린더에 반영합니다.

## 설치 (1회, 약 3분)

1. GitHub에 **공개(public) 레포 `bond-schedule-data`** 를 새로 만든다 (私有면 Claude가 raw URL을 못 읽음. 내용물은 전부 공개 정부 공고라 공개 무방).
2. 이 폴더의 파일 전체를 레포 루트에 업로드(경로 유지: `.github/workflows/bond-monitor.yml`, `scraper/main.py`, `requirements.txt`).
3. 레포 → Actions 탭 → workflow 활성화 → `bond-monitor` → **Run workflow**로 1회 수동 실행해 정상 동작 확인.
4. (옵션) 텔레그램 즉시 알림: Settings → Secrets and variables → Actions에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 추가 — DART 스캐너에 쓰던 봇 재사용 가능. 신규 공고 감지 즉시(수집 주기 내) 텔레그램으로 제목·링크 발송.

## 수집 주기 (모두 KST, 평일)

- 10:00~12:00 15분 주기 — 입찰 결과·중도환매 결과(10:40~11:00대 게시) 포착
- 16:00~18:00 15분 주기 — 익일 실시 정보(16:30)·발행공고(17:00) 포착
- 그 외 09~18시 정시 1회
- ※ GitHub Actions cron은 수분 지연될 수 있음. 공고 게시 → 레포 반영은 통상 5~20분.

## 산출물

- `data/latest.json` — 최근 공고 60건(본문 + 첨부 텍스트·표 포함, 최신순). **Claude 소비 진입점.**
- `data/announcements/<id>.json` — 공고별 상세(영구 보관).
- `data/files/` — 첨부 원본(PDF·HWP), `data/raw/` — 최근 원문 HTML(파서 디버깅용, 40건 유지).
- `data/seen.json` — 중복 방지 레지스트리.

## 수집 소스와 한계

- 한국은행 `P0001794`(국고채 발행·환매 공고), `P0001773`(공개시장 공지사항 — 통안 경쟁입찰·중도환매·정례모집·RP). robots 허용 범위.
- KDI eiec 전재자료('국고채', '통화안정증권' 검색) — 재경부·한은 보도자료 원문 PDF(며칠 지연).
- **재경부 mofe.go.kr 게시판은 robots 차단이라 수집하지 않음** → 국고채 경쟁입찰 '결과' 원문은 이 레포에 안 들어오고, 주간(금) Claude 실행이 데스크톱 브라우저로 대조. 결과 속보는 뉴스 폴백.
- 첫 1~2일 운영 후 `data/raw/`의 실제 HTML을 보고 Claude가 본문/첨부 추출 셀렉터를 개선할 수 있음(HTML 구조가 예상과 다르면 body가 짧게 나올 수 있는데, 그 경우에도 첨부 PDF 파싱은 별도 경로라 대부분 동작).

## HWP 첨부

기본 러너에는 HWP 파서가 없어 PDF만 표 추출됩니다. HWP 텍스트가 꼭 필요하면 `requirements.txt`에 `pyhwp` 추가(추출 품질은 문서에 따라 다름).
