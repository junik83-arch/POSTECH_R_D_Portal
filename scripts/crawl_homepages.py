#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawl_homepages.py — 교원 홈페이지 크롤러 (로컬 전용)

⚠️ 이 스크립트는 Claude Code 원격 실행 환경(이 세션)에서는 동작하지 않습니다.
   네트워크 정책상 postech.ac.kr 등 외부 도메인 egress가 차단되어 있기 때문입니다.
   인터넷 접근이 가능한 로컬 PC 등에서 실행하세요.

동작:
    sources/faculty_profiles_source.json 의 "홈페이지" URL(중복 제거)을 순회하며
    0) 여러 교원이 정확히 같은 URL을 쓰는 경우(학과/그룹 공통 포털)는 개인 정보와
       무관하다고 보고 아예 크롤링하지 않습니다.
    1) 홈페이지 첫 화면을 가져오고
    2) 그 안에서 같은 사이트 내부로 연결되는 링크(연구/논문/CV 등 탭)를 찾아
       최대 --max-subpages 개(기본 15)까지 함께 가져옵니다. 구성원/뉴스·공지 링크는 제외합니다.
    결과를 sources/homepage_crawl.json 에 아래 형태로 저장합니다:

        {
          "<홈페이지 URL>": {
            "title": ..., "text": ..., "status": 200, "fetched_at": "...",
            "subpages": {
              "<서브페이지 URL>": {"title": ..., "text": ..., "status": 200, "fetched_at": "..."},
              ...
            }
          },
          "<학과 공통 포털 URL>": {"skipped": "shared_portal", "shared_by": 8, "fetched_at": "..."},
          ...
        }

    이후 `python3 scripts/build_wiki.py` 를 다시 실행하면 wiki/faculty/*.md 의
    "홈페이지 추가 정보" 섹션이 자동으로 채워집니다.

설치:
    pip install -r scripts/requirements.txt

사용법:
    python3 scripts/crawl_homepages.py                 # 전체 크롤링 (이미 끝난 항목은 건너뜀)
    python3 scripts/crawl_homepages.py --limit 10       # 앞 10개 교원만 (테스트용)
    python3 scripts/crawl_homepages.py --delay 2.0      # 요청 간 대기시간(초), 기본 1.0
    python3 scripts/crawl_homepages.py --force          # 이미 끝난 항목도 전부 다시 시도
    python3 scripts/crawl_homepages.py --insecure       # SSL 인증서 오류 나는 서버도 시도 (주의)
    python3 scripts/crawl_homepages.py --max-subpages 20  # 교원 1명당 최대 서브페이지 수 (기본 15)
    python3 scripts/crawl_homepages.py --force --refetch-before 2026-10-07T03:00  # 멈춘 전체 갱신을 이어서

저장 규칙 (2026-10-07):
    - 첫 화면 20,000자 · 탭 하나 12,000자 · 탭 15개까지. 넘으면 줄 끝에서 자르고 chars_total · truncated 칸에 남긴다.
    - 이메일 · 전화번호는 [이메일] · [전화]로 바꿔 저장한다(이 파일과 위키는 공개 저장소에 올라간다).
    - 다시 받다가 실패하면 예전 글을 지우지 않고 error · last_attempt_at 만 더한다. AI 요약 칸도 그대로 둔다.
    - 글자 없는 첫 화면이 meta refresh · location.replace 로 다른 주소로 넘기면 한 번 따라간다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("필요한 패키지가 없습니다. 먼저 실행하세요:\n  pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "sources"
SOURCE_FILE = DATA_DIR / "faculty_profiles_source.json"
OUTPUT_FILE = DATA_DIR / "homepage_crawl.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; POSTECH-RD-Wiki-Bot/1.0; +for internal faculty wiki)"
}
# 저장 상한 (2026-10-07 상향). 예전 상한(첫 화면 4,000자 · 탭 2,500자 · 탭 8개)에서는 2026-08-28 크롤링의
# 홈페이지 256곳 중 204곳이 잘렸다 — 특히 논문·출판 탭의 75%, 연구 소개 탭의 29%가 문장·논문 항목 중간에서 끊겼다.
# 상한을 넘으면 줄 끝에서 자르고(clip) 원래 길이를 chars_total · truncated 칸에 남긴다(build_wiki 가 "앞 N자만" 으로 표시).
MAX_CHARS = 20000         # 홈페이지 첫 화면에서 저장할 최대 텍스트 길이
MAX_CHARS_SUBPAGE = 12000  # 서브페이지 1개당 저장할 최대 텍스트 길이
DEFAULT_MAX_SUBPAGES = 15

# 연락처 지우기 (2026-10-07). 이 결과 파일과 위키 페이지는 공개 저장소에 올라가는데, 연구실 홈페이지 원문에는
# 학생·직원 이메일과 전화번호가 섞여 있다. 저장하기 전에 [이메일] · [전화] 로 바꾼다.
# 교원 본인 이메일은 실적 DB(faculty_profiles_source.json)에서 따로 교원 페이지 '기본 정보'에 나온다.
# 규칙은 RnDstrategyReport/lib/privacy.py 와 같다('@' 앞뒤 빈칸 · 쉼표 오타 · (at) 표기 · "054) 279-5681" 꼴 전화).
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+\s?@\s?[A-Za-z0-9-]+([.,][A-Za-z0-9-]+)*[.,][A-Za-z]{2,}")
EMAIL_AT = re.compile(r"[A-Za-z0-9._%+-]+\s*[\[(]\s*at\s*[\])]\s*[A-Za-z0-9.-]+(\s*[\[(]?\s*dot\s*[\])]?\s*[A-Za-z]+)*", re.I)
# 전화는 지역번호가 0 또는 +82 로 시작할 때만 본다 — 플랫폼 규칙(0 생략 허용)은 논문 목록의 "Vol. 1 2023 2024" 같은
# 숫자 줄까지 지울 수 있어서 좁혔다. 앞뒤가 숫자로 이어지면 전화가 아니다.
PHONE = re.compile(r"(?<!\d)(?:\+82[-\s]?0?\d{1,2}[-.\s)]\s?|0\d{1,2}(?:\)\s?|[-.\s]))\d{3,4}[-.\s]\d{4}(?!\d)")

# 첫 화면이 비어 있을 때 따라가 볼 '다른 주소로 넘기기' 표시 (2026-08-28 크롤링에서 글자 없이 끝난 5곳 중 3곳이 이것)
META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["']?\s*\d+\s*;\s*url=([^"'>\s]+)""", re.I)
JS_REDIRECT = re.compile(r"""location\.(?:replace\(|href\s*=\s*)\s*["']([^"']+)["']""", re.I)

SKIP_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".hwp", ".hwpx",
    ".zip", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".mp4", ".mp3", ".avi",
}

# 연구실/교원 홈페이지에서 실제 내용이 담긴 탭일 가능성이 높은 키워드
# (링크 텍스트 + href 양쪽에 대해 대소문자 구분 없이 매칭)
SUBPAGE_KEYWORDS = [
    "research", "publication", "paper", "cv", "biograph", "profile", "about",
    "award", "project", "lab", "course", "teaching", "vitae", "bio",
    "연구", "논문", "성과", "수상", "프로젝트", "연구실",
    "소개", "이력", "경력", "강의", "교육", "연구원", "업적", "저서",
]

# 이 키워드가 포함된 링크는 아예 서브페이지 후보에서 제외한다
# (구성원/멤버 명단, 뉴스/공지 게시판은 교수 개인의 연구 정보와 관련이 적음)
SUBPAGE_EXCLUDE_KEYWORDS = [
    "member", "people", "news", "구성원", "멤버", "뉴스", "공지",
    # 2026-10-07 추가 — 졸업생 명단 · 개인정보 처리방침 · 로그인 · 사이트맵 · 약관은 연구 정보가 아니다
    "alumni", "졸업생", "privacy", "개인정보", "login", "로그인", "sitemap", "사이트맵", "약관",
]

# 여러 교원이 정확히 같은 URL을 "홈페이지"로 등록한 경우(학과/그룹 공통 포털)의
# 판단 기준: 이 값 이상 겹치면 개인 페이지가 아니라 공통 포털로 본다
PORTAL_SHARE_THRESHOLD = 2

ZERO_WIDTH_CHARS = "﻿​‌‍\xa0"


def normalize_url(url: str) -> str:
    """요청에 실제로 사용할 URL을 정리한다 (BOM/제로폭 공백 제거, 스킴 누락 보정).

    sources/homepage_crawl.json 의 최상위 키는 원본 그대로 유지해야 build_wiki.py 의
    조회가 맞아떨어지므로, 이 함수의 결과는 요청에만 쓰고 저장 키에는 쓰지 않는다.
    """
    cleaned = url.strip()
    for ch in ZERO_WIDTH_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    if "://" not in cleaned:
        cleaned = "https://" + cleaned
    return cleaned


def extract_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    joined = "\n".join(lines)
    return title, joined


def scrub_contacts(text: str) -> str:
    """이메일은 [이메일], 전화번호는 [전화]로 바꾼다 (공개 저장소에 학생·직원 연락처가 올라가지 않게)."""
    text = EMAIL.sub("[이메일]", text)
    text = EMAIL_AT.sub("[이메일]", text)
    return PHONE.sub("[전화]", text)


def clip(text: str, limit: int) -> tuple[str, bool]:
    """상한을 넘으면 줄 끝에서 자른다(문장·논문 항목 중간에서 끊기지 않게). (자른 글, 잘렸는지).
    상한 안쪽 마지막 줄바꿈이 너무 앞(상한의 80% 미만)에 있으면 그냥 상한에서 자른다."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    if cut < limit * 0.8:
        cut = limit
    return text[:cut].rstrip(), True


def stored_text(raw: str, limit: int) -> dict:
    """저장할 글 칸들: 연락처를 지우고 상한에서 자른 text, 원래 길이 chars_total, 잘렸는지 truncated."""
    text = scrub_contacts(raw)
    clipped, truncated = clip(text, limit)
    return {"text": clipped, "chars_total": len(text), "truncated": truncated}


def _site(netloc: str) -> str:
    """같은 사이트인지 견줄 이름 — www. 를 떼고 끝 세 마디(예: postech.ac.kr)."""
    host = netloc.lower().split(":")[0].removeprefix("www.")
    return ".".join(host.split(".")[-3:])


def redirect_target(html: str, base_url: str) -> str | None:
    """글자가 없는 첫 화면이 meta refresh 나 location.replace/href 로 **같은 사이트 안** 다른 주소로 넘기면 그 주소.
    다른 사이트로 넘기면 따라가지 않는다 — 호스팅이 끝난 연구실 사이트가 호스팅 업체 오류 페이지로 넘기는 경우가
    있었다(2026-10-07 mbme → mailplug '연결 실패' 페이지)."""
    m = META_REFRESH.search(html) or JS_REDIRECT.search(html)
    if not m:
        return None
    target = urljoin(base_url, m.group(1))
    return target if _site(urlparse(target).netloc) == _site(urlparse(base_url).netloc) else None


def _decode(resp) -> str:
    """응답 글자. 서버가 문자 인코딩을 알려 주지 않으면 requests 는 latin-1 로 읽어 한글이 'ì°ê²°' 처럼 깨진다
    (2026-08-28 · 10-07 크롤링에서 bnl · iras · crg 등). 그때는 내용으로 짐작한 인코딩(apparent_encoding)을 쓴다."""
    if (resp.encoding or "").lower() in ("iso-8859-1", "latin-1", "latin1") and resp.apparent_encoding:
        resp.encoding = resp.apparent_encoding
    return resp.text


def first_frame_src(html: str, base_url: str) -> str | None:
    """옛날 대학 부서 홈페이지는 <frameset>으로 실제 내용을 다른 페이지에 넣어두는
    경우가 많다. <body>가 비어 있을 때 첫 번째 <frame>이 가리키는 실제 내용 페이지
    URL을 찾아준다 (없으면 None)."""
    soup = BeautifulSoup(html, "html.parser")
    frame = soup.find("frame") or soup.find("iframe")
    if frame and frame.get("src"):
        return urljoin(base_url, frame["src"])
    return None


def fetch_page(url: str, timeout: float, insecure: bool) -> dict:
    """한 페이지를 가져와 {html, content_url, title, text, status} 를 돌려준다.
    frameset이면 실제 내용 프레임을 한 번 더 따라간다. 실패하면 예외를 던진다."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=not insecure)
    resp.raise_for_status()
    html, content_url, status = _decode(resp), url, resp.status_code
    title, text = extract_text(html)

    if not text:
        # 글자가 없으면 ① 다른 주소로 넘기는 페이지인지(meta refresh · location.replace — 2026-10-07 추가) ② frameset 인지 본다.
        # 넘기는 주소는 한 번만 따라간다(서로 넘기는 고리에 빠지지 않게).
        frame_url = redirect_target(html, resp.url) or first_frame_src(html, url)
        if frame_url:
            frame_resp = requests.get(frame_url, headers=HEADERS, timeout=timeout, verify=not insecure)
            frame_resp.raise_for_status()
            html, content_url, status = _decode(frame_resp), frame_url, frame_resp.status_code
            frame_title, frame_text = extract_text(html)
            title, text = (title or frame_title), frame_text

    return {"html": html, "content_url": content_url, "title": title, "text": text, "status": status}


def discover_subpage_links(html: str, base_url: str, homepage_url: str, limit: int) -> list[str]:
    """홈페이지 안에서 같은 사이트 내부로 연결되는, 내용이 있을 법한 링크를 찾는다.

    scope(같은 사이트로 볼 범위)는 사이트 종류에 따라 다르게 잡는다:
      - sites.google.com 은 여러 교원이 같은 도메인을 공유하므로 경로의 앞부분
        (/view/아이디, /site/아이디)까지 일치해야 같은 사이트로 본다.
      - 그 외에는 같은 netloc(서브도메인)이면 같은 사이트로 본다.
    """
    parsed_home = urlparse(base_url)
    if parsed_home.netloc == "sites.google.com":
        parts = [p for p in parsed_home.path.split("/") if p]
        scope_prefix = "/" + "/".join(parts[:2]) if len(parts) >= 2 else parsed_home.path
    else:
        scope_prefix = None  # netloc만 비교

    # http/https · www 차이는 같은 주소로 본다(2026-10-07 — 같은 논문 목록이 두 주소로 잡혀 두 번 저장된 곳이 7곳 있었다)
    def key(u: str) -> str:
        return re.sub(r"^https?://(www\.)?", "", u).rstrip("/")

    homepage_norm = key(homepage_url)
    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []  # (score, 등장순서, url)

    soup = BeautifulSoup(html, "html.parser")
    for idx, a in enumerate(soup.find_all("a", href=True)):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href).split("#")[0]
        if not abs_url.startswith(("http://", "https://")):
            continue
        parsed = urlparse(abs_url)
        if any(parsed.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        if parsed.netloc != parsed_home.netloc:
            continue
        if scope_prefix is not None and not parsed.path.startswith(scope_prefix):
            continue
        norm = key(abs_url)
        if norm == homepage_norm or norm in seen:
            continue
        seen.add(norm)

        haystack = f"{a.get_text(' ', strip=True)} {href}".lower()
        if any(kw in haystack for kw in SUBPAGE_EXCLUDE_KEYWORDS):
            continue
        score = sum(1 for kw in SUBPAGE_KEYWORDS if kw in haystack)
        scored.append((score, idx, abs_url))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [url for _, _, url in scored[:limit]]


def load_urls() -> list[str]:
    records = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    urls = []
    seen = set()
    for r in records:
        url = (r.get("홈페이지") or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def load_portal_share_counts() -> dict[str, int]:
    """여러 교원이 정확히 같은 URL을 '홈페이지'로 등록한 경우, 그 URL이 몇 명에게
    쓰이는지 센다. PORTAL_SHARE_THRESHOLD 이상이면 개인 페이지가 아니라 학과/그룹
    공통 포털로 보고 크롤링 대상에서 제외한다 (교수 개인 연구와 무관한 내용이므로)."""
    records = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for r in records:
        url = (r.get("홈페이지") or "").strip()
        if url:
            counts[url] = counts.get(url, 0) + 1
    return {url: c for url, c in counts.items() if c >= PORTAL_SHARE_THRESHOLD}


def save(result: dict) -> None:
    """결과를 저장한다. 백신·클라우드 동기화(OneDrive 등)가 파일을 순간적으로 잠그면
    Windows에서 PermissionError가 나는 경우가 있어, 몇 번 재시도하고 그래도 안 되면
    (크롤링 전체를 중단시키지 않도록) 경고만 남기고 계속 진행한다 — 이번에 못 쓴
    내용은 다음 저장 때 같이 반영된다."""
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    tmp_path = OUTPUT_FILE.with_suffix(".json.tmp")
    for attempt in range(5):
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(OUTPUT_FILE)  # 원자적 교체: 쓰다가 중단돼도 기존 파일은 안전
            return
        except PermissionError:
            if attempt < 4:
                time.sleep(1.5)
    print(
        f"  경고: {OUTPUT_FILE.name} 저장 실패(파일이 잠겨 있는 것 같습니다 — 백신/OneDrive 등을 "
        f"확인해보세요). 이번 결과는 다음 저장 때 다시 시도합니다."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="처리할 최대 교원(홈페이지) 수 (테스트용)")
    ap.add_argument("--delay", type=float, default=1.0, help="요청 간 대기시간(초)")
    ap.add_argument("--timeout", type=float, default=15.0, help="요청 타임아웃(초)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="이미 끝난 항목(홈페이지+서브페이지)도 전부 다시 시도 (기본값: 안 끝난 것만)",
    )
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="SSL 인증서 검증을 건너뜀 (postech.ac.kr 일부 서브도메인의 인증서 오류 우회용, 주의해서 사용)",
    )
    ap.add_argument(
        "--max-subpages",
        type=int,
        default=DEFAULT_MAX_SUBPAGES,
        help=f"교원 1명당 추가로 가져올 서브페이지(탭) 최대 개수 (기본 {DEFAULT_MAX_SUBPAGES}, 0이면 서브페이지 안 가져옴)",
    )
    ap.add_argument(
        "--refetch-before",
        default=None,
        metavar="ISO시각",
        help="--force 와 함께: 이 시각(예: 2026-10-07T03:00) 전에 받은 항목만 다시 받는다 — 중간에 멈춘 전체 갱신을 "
             "처음부터가 아니라 이어서 돌릴 때 쓴다(시각은 fetched_at 과 같은 UTC)",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="URL",
        help="이 홈페이지(faculty_profiles_source.json 의 '홈페이지' 값 그대로)만 처리한다 — 여러 번 줄 수 있다. "
             "일부 사이트만 고쳐 다시 받을 때(--force 와 함께)",
    )
    args = ap.parse_args()
    if args.refetch_before and not args.force:
        ap.error("--refetch-before 는 --force 와 함께 쓴다")

    if not SOURCE_FILE.exists():
        raise SystemExit(f"원본 파일이 없습니다: {SOURCE_FILE}")

    if args.insecure:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    urls = load_urls()
    portal_shares = load_portal_share_counts()
    if args.only:
        missing = [u for u in args.only if u not in urls]
        if missing:
            raise SystemExit(f"--only 의 주소가 원본에 없습니다: {missing}")
        urls = [u for u in urls if u in args.only]
    if args.limit:
        urls = urls[: args.limit]

    result: dict = {}
    if OUTPUT_FILE.exists():
        result = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))

    print(f"대상 교원 {len(urls)}명 (홈페이지 URL 중복 제거됨)")
    if portal_shares:
        print(f"  이 중 {len(portal_shares)}개 URL은 여러 교원이 공유하는 학과/그룹 포털로 보고 제외합니다.")
    for i, url in enumerate(urls, 1):
        if url in portal_shares:
            print(f"[{i}/{len(urls)}] {url} — 학과/그룹 공통 포털({portal_shares[url]}명 공유)로 판단되어 건너뜀")
            result[url] = {
                "skipped": "shared_portal",
                "shared_by": portal_shares[url],
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            save(result)
            continue

        existing = result.get(url, {})
        done = existing.get("text") and "subpages" in existing
        if done and not args.force:
            print(f"[{i}/{len(urls)}] {url} — 이미 완료(서브페이지 포함), 건너뜀")
            continue
        if args.refetch_before and (existing.get("fetched_at") or "") >= args.refetch_before:
            print(f"[{i}/{len(urls)}] {url} — {args.refetch_before} 뒤에 이미 받음, 건너뜀")
            continue

        print(f"[{i}/{len(urls)}] {url}")
        entry = {
            "title": existing.get("title", ""),
            "text": existing.get("text", ""),
            "status": existing.get("status"),
            "fetched_at": existing.get("fetched_at"),
            "subpages": {} if args.force else existing.get("subpages", {}),
        }
        # AI 요약 칸은 그대로 둔다(2026-10-07) — 예전에는 다시 크롤링하면 요약이 지워져, 원문이 그대로인
        # 교원까지 전부 다시 요약해야 했다. summarize_homepages.py 가 원문 해시(summary_source_hash)를 비교해
        # 바뀐 교원만 다시 요약한다.
        for key in ("summary", "summary_source_hash", "summary_model", "summary_generated_at"):
            if key in existing:
                entry[key] = existing[key]

        try:
            fetch_url = normalize_url(url)
            page = fetch_page(fetch_url, args.timeout, args.insecure)
            entry.update(
                title=page["title"],
                **stored_text(page["text"], MAX_CHARS),  # 연락처 지우고 줄 끝에서 자름 · 원래 길이 기록
                status=page["status"],
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            entry.pop("error", None)

            if args.max_subpages > 0:
                sub_links = discover_subpage_links(
                    page["html"], page["content_url"], url, args.max_subpages
                )
                for j, sub_url in enumerate(sub_links, 1):
                    if not args.force and entry["subpages"].get(sub_url, {}).get("text"):
                        continue
                    print(f"    - 서브페이지 [{j}/{len(sub_links)}] {sub_url}")
                    time.sleep(args.delay)
                    try:
                        sub_page = fetch_page(sub_url, args.timeout, args.insecure)
                        # 링크 글자가 아이콘 등이라 걸러지지 않은 구성원 명단 · 개인정보 처리방침은 받은 페이지 제목으로 한 번 더 거른다
                        if any(kw in (sub_page["title"] or "").lower() for kw in SUBPAGE_EXCLUDE_KEYWORDS):
                            print(f"      건너뜀(제목: {sub_page['title'][:40]})")
                            continue
                        entry["subpages"][sub_url] = {
                            "title": sub_page["title"],
                            **stored_text(sub_page["text"], MAX_CHARS_SUBPAGE),
                            "status": sub_page["status"],
                            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        }
                    except Exception as e:  # noqa: BLE001
                        print(f"      실패: {e}")
                        entry["subpages"][sub_url] = {
                            "title": "", "text": "", "status": None, "error": str(e),
                            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        }
        except Exception as e:  # noqa: BLE001 — 크롤링 스크립트 특성상 개별 실패는 기록하고 계속 진행
            print(f"  실패: {e}")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if existing.get("text"):
                # 예전에 받은 글이 있으면 지우지 않는다(2026-10-07) — 서버가 잠깐 안 될 때 위키 내용이 통째로 사라지지 않게.
                # fetched_at 은 예전 값 그대로라 위키의 '크롤링 시각'이 언제 글인지 정확히 보여 준다.
                entry = {**existing, "error": str(e), "last_attempt_at": now}
            else:
                entry.update(title="", text="", status=None, error=str(e), fetched_at=now)

        result[url] = entry
        save(result)
        time.sleep(args.delay)

    skipped_n = sum(1 for v in result.values() if v.get("skipped"))
    ok = sum(1 for v in result.values() if v.get("text"))
    sub_ok = sum(len([s for s in v.get("subpages", {}).values() if s.get("text")]) for v in result.values())
    print(
        f"\n완료: 홈페이지 {ok}/{len(result) - skipped_n} 성공"
        f" ({skipped_n}개는 공통 포털로 판단해 제외), 서브페이지 {sub_ok}개 성공. 결과 저장: {OUTPUT_FILE}"
    )
    print("다음 단계: python3 scripts/build_wiki.py 를 다시 실행해 위키에 반영하세요.")


if __name__ == "__main__":
    main()
