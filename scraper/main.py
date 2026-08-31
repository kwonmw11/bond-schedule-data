# -*- coding: utf-8 -*-
"""
bond-data collector — 한국은행(BOK) 국고채/통안증권 공고 + KDI 전재 보도자료 수집기.

GitHub Actions 러너에서 주기 실행:
  1) BOK RSS 2종에서 신규 게시글 감지 (P0001794 국고채 발행·환매 공고 / P0001773 공개시장 공지사항)
  2) 신규 글의 view.do 원문 HTML에서 본문·첨부파일 링크 추출
  3) 첨부 PDF 다운로드 → pdfplumber로 텍스트·표 추출 (HWP는 hwp5txt 있으면 텍스트만)
  4) KDI 경제정보센터(eiec)에서 '국고채'/'통화안정증권' 신규 전재자료 + 첨부 PDF 수집
  5) data/ 아래 JSON으로 저장·커밋 → Claude 예약 작업이 raw URL로 소비
  6) (옵션) 텔레그램 알림

수집 대상은 robots가 허용하는 bok.or.kr / eiec.kdi.re.kr 만이다.
mofe.go.kr(재경부 게시판)은 robots 차단 → 여기서 수집하지 않는다(주간 브라우저 대조로 커버).
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
ANN_DIR = os.path.join(DATA, "announcements")
RAW_DIR = os.path.join(DATA, "raw")
FILE_DIR = os.path.join(DATA, "files")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) bond-data-collector/1.0 (personal schedule sync)"}
TIMEOUT = 30
MAX_FILE_MB = 25
LATEST_KEEP = 60          # latest.json에 유지할 최근 공고 수
BODY_CAP = 30000          # JSON에 넣는 본문/첨부 텍스트 길이 상한(문자)

FEEDS = [
    {
        "board": "P0001794", "menu": "200364", "label": "국고채 발행·환매 공고(한국은행)",
        "rss": "https://www.bok.or.kr/portal/bbs/P0001794/news.rss?menuNo=200364",
        "view": "https://www.bok.or.kr/portal/bbs/P0001794/view.do?nttId={ntt}&menuNo=200364",
    },
    {
        "board": "P0001773", "menu": "200037", "label": "공개시장 공지사항(통안·RP)",
        "rss": "https://www.bok.or.kr/portal/bbs/P0001773/news.rss?menuNo=200037",
        "view": "https://www.bok.or.kr/portal/bbs/P0001773/view.do?nttId={ntt}&menuNo=200037",
    },
]

KDI_SEARCHES = ["국고채", "통화안정증권"]
KDI_LIST = "https://eiec.kdi.re.kr/policy/materialList.do"
KDI_VIEW = "https://eiec.kdi.re.kr/policy/materialView.do?num={num}"
KDI_DOWN = "https://eiec.kdi.re.kr/policy/callDownload.do?num={num}&filenum={fn}"

# 알림 대상 키워드(텔레그램) — 잡음 방지용. 비우면 전부 알림.
NOTIFY_KEYWORDS = ["국고", "통화안정", "외평", "재정증권", "교환", "매입", "모집", "중도환매", "발행 계획", "발행계획"]


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z")


def ensure_dirs():
    for d in (DATA, ANN_DIR, RAW_DIR, FILE_DIR):
        os.makedirs(d, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- BOK 원문
def extract_bok_body(html: str) -> str:
    """view.do HTML에서 본문 후보 텍스트를 최대한 뽑는다(레이아웃 방어적)."""
    soup = BeautifulSoup(html, "lxml")
    for sel in ("div.bdView", "div.bd-view", "div.viewCont", "div.bbsV_cont",
                "div.board_view", "td.content", "div#content", "article"):
        node = soup.select_one(sel)
        if node:
            txt = node.get_text("\n", strip=True)
            if len(txt) > 40:
                return txt
    # fallback: 메타 설명 + 가장 긴 텍스트 블록
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    meta_txt = (meta.get("content", "") if meta else "").strip()
    blocks = sorted((t.get_text("\n", strip=True) for t in soup.find_all(["div", "td", "section"])),
                    key=len, reverse=True)
    body = blocks[0] if blocks else ""
    return (meta_txt + "\n" + body).strip()


def extract_attachments(html: str, base: str = "https://www.bok.or.kr"):
    """fileDown.do 계열 첨부 링크 (url, name) 목록."""
    out, seen = [], set()
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        blob = href + " " + (a.get("onclick") or "")
        m = re.search(r"""(/portal/cmmn/file/fileDown\.do[^"'\s)]+)""", blob)
        if not m:
            # onclick='fn_fileDown("KO_...","1")' 패턴
            m2 = re.search(r"""fileDown[^(]*\(\s*['"]([A-Z0-9_]+)['"]\s*,\s*['"]?(\d+)""", blob)
            if m2:
                url = f"{base}/portal/cmmn/file/fileDown.do?atchFileId={m2.group(1)}&fileSn={m2.group(2)}"
            else:
                continue
        else:
            url = base + m.group(1).replace("&amp;", "&")
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "name": a.get_text(strip=True) or "attachment"})
    # a 태그 밖 원시 HTML에서도 한 번 더
    for m in re.finditer(r"atchFileId=([A-Z0-9_]+)&(?:amp;)?fileSn=(\d+)", html):
        url = f"{base}/portal/cmmn/file/fileDown.do?atchFileId={m.group(1)}&fileSn={m.group(2)}"
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "name": "attachment"})
    return out


# ---------------------------------------------------------------- 파일 파싱
def parse_pdf(content: bytes):
    if pdfplumber is None:
        return {"text": "", "tables": [], "error": "pdfplumber not installed"}
    text_parts, tables = [], []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            for tb in page.extract_tables() or []:
                tables.append([[(c or "").strip() for c in row] for row in tb])
    return {"text": "\n".join(text_parts).strip()[:BODY_CAP], "tables": tables}


def parse_hwp(content: bytes):
    """pyhwp(hwp5txt) 설치 시 텍스트 추출, 아니면 스킵."""
    with tempfile.NamedTemporaryFile(suffix=".hwp", delete=False) as tf:
        tf.write(content)
        path = tf.name
    try:
        out = subprocess.run(["hwp5txt", path], capture_output=True, timeout=60)
        if out.returncode == 0:
            return {"text": out.stdout.decode("utf-8", "ignore").strip()[:BODY_CAP], "tables": []}
        return {"text": "", "tables": [], "error": "hwp5txt failed"}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "tables": [], "error": f"hwp parse unavailable: {e}"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def fetch_attachment(att: dict, key: str):
    """첨부 다운로드 + 유형별 파싱. 결과 dict 반환."""
    try:
        r = get(att["url"], stream=True)
        content = b""
        for chunk in r.iter_content(1 << 16):
            content += chunk
            if len(content) > MAX_FILE_MB << 20:
                return {**att, "error": f"file > {MAX_FILE_MB}MB, skipped"}
        ctype = (r.headers.get("Content-Type") or "").lower()
        disp = r.headers.get("Content-Disposition") or ""
        fname = att.get("name") or ""
        m = re.search(r"filename[^=]*=\"?([^\";]+)", disp)
        if m:
            try:
                fname = m.group(1).encode("latin-1").decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                fname = m.group(1)
        ext = (os.path.splitext(fname)[1] or "").lower()
        if not ext:
            ext = ".pdf" if "pdf" in ctype else (".hwp" if "hwp" in ctype or "haansoft" in ctype else ".bin")
        digest = hashlib.sha1(content).hexdigest()[:12]
        local = os.path.join(FILE_DIR, f"{key}_{digest}{ext}")
        with open(local, "wb") as f:
            f.write(content)
        parsed = {"text": "", "tables": []}
        if ext == ".pdf":
            parsed = parse_pdf(content)
        elif ext in (".hwp", ".hwpx"):
            parsed = parse_hwp(content)
        elif ext in (".txt", ".csv"):
            parsed = {"text": content.decode("utf-8", "ignore")[:BODY_CAP], "tables": []}
        return {**att, "filename": fname, "saved": os.path.relpath(local, ROOT),
                "size": len(content), **parsed}
    except Exception as e:  # noqa: BLE001
        return {**att, "error": str(e)}


# ---------------------------------------------------------------- 수집: BOK
def collect_bok(state):
    new_items = []
    for feed in FEEDS:
        try:
            parsed = feedparser.parse(get(feed["rss"]).content)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] RSS fail {feed['board']}: {e}")
            continue
        for e in parsed.entries:
            link = getattr(e, "link", "") or ""
            m = re.search(r"nttId=(\d+)", link)
            if not m:
                continue
            ntt = m.group(1)
            key = f"bok_{ntt}"
            if key in state["seen"]:
                continue
            item = {
                "id": key, "source": "bok", "board": feed["board"], "board_label": feed["label"],
                "title": getattr(e, "title", "").strip(),
                "published": getattr(e, "published", ""),
                "url": feed["view"].format(ntt=ntt),
                "collected_at": now_kst(),
                "body": "", "attachments": [],
            }
            try:
                html = get(item["url"]).text
                # 디버깅용 원문 저장(파서 개선에 사용, 최근 것만 유지됨)
                with open(os.path.join(RAW_DIR, f"{key}.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                item["body"] = extract_bok_body(html)[:BODY_CAP]
                atts = extract_attachments(html)
                item["attachments"] = [fetch_attachment(a, key) for a in atts[:8]]
            except Exception as ex:  # noqa: BLE001
                item["error"] = str(ex)
            state["seen"].append(key)
            new_items.append(item)
            save_json(os.path.join(ANN_DIR, f"{key}.json"), item)
            time.sleep(1)  # 서버 예의
    return new_items


# ---------------------------------------------------------------- 수집: KDI
def collect_kdi(state):
    new_items = []
    for kw in KDI_SEARCHES:
        try:
            html = get(KDI_LIST, params={"search_txt": kw}).text
        except Exception as e:  # noqa: BLE001
            print(f"[warn] KDI list fail {kw}: {e}")
            continue
        nums = re.findall(r"materialView\.do\?num=(\d+)", html)
        for num in dict.fromkeys(nums[:20]):
            key = f"kdi_{num}"
            if key in state["seen"]:
                continue
            item = {"id": key, "source": "kdi", "keyword": kw, "num": num,
                    "url": KDI_VIEW.format(num=num), "collected_at": now_kst(),
                    "title": "", "body": "", "attachments": []}
            try:
                vhtml = get(item["url"]).text
                soup = BeautifulSoup(vhtml, "lxml")
                h = soup.select_one("h3, h2, .view_tit, .tit")
                item["title"] = h.get_text(strip=True) if h else f"KDI 자료 {num}"
                item["body"] = extract_bok_body(vhtml)[:BODY_CAP]
                item["attachments"] = [fetch_attachment(
                    {"url": KDI_DOWN.format(num=num, fn=fn), "name": f"file{fn}"}, key) for fn in (1,)]
            except Exception as ex:  # noqa: BLE001
                item["error"] = str(ex)
            state["seen"].append(key)
            new_items.append(item)
            save_json(os.path.join(ANN_DIR, f"{key}.json"), item)
            time.sleep(1)
    return new_items


# ---------------------------------------------------------------- 알림/출력
def telegram_notify(items):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat or not items:
        return
    lines = []
    for it in items:
        title = it.get("title", "")
        if NOTIFY_KEYWORDS and not any(k in title for k in NOTIFY_KEYWORDS):
            continue
        lines.append(f"· {title}\n  {it['url']}")
    if not lines:
        return
    msg = "[bond-data] 신규 공고 감지\n" + "\n".join(lines[:15])
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg, "disable_web_page_preview": True},
                      timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] telegram fail: {e}")


def rebuild_latest():
    files = sorted(
        (os.path.join(ANN_DIR, f) for f in os.listdir(ANN_DIR) if f.endswith(".json")),
        key=os.path.getmtime, reverse=True)[:LATEST_KEEP]
    latest = [load_json(p, {}) for p in files]
    save_json(os.path.join(DATA, "latest.json"),
              {"generated_at": now_kst(), "count": len(latest), "items": latest})


def prune_raw(keep=40):
    files = sorted((os.path.join(RAW_DIR, f) for f in os.listdir(RAW_DIR)),
                   key=os.path.getmtime, reverse=True)
    for p in files[keep:]:
        os.unlink(p)


def main():
    ensure_dirs()
    state_path = os.path.join(DATA, "seen.json")
    state = load_json(state_path, {"seen": []})
    new_items = collect_bok(state) + collect_kdi(state)
    state["seen"] = state["seen"][-3000:]
    save_json(state_path, state)
    rebuild_latest()
    prune_raw()
    telegram_notify(new_items)
    print(f"new items: {len(new_items)}")
    for it in new_items:
        print(" -", it.get("title", it["id"]))


if __name__ == "__main__":
    sys.exit(main())
