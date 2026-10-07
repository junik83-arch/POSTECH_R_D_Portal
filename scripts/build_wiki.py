#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_wiki.py — POSTECH 교원 LLM Wiki 생성기

sources/faculty_profiles_source.json (원본, 필수)
sources/homepage_crawl.json          (홈페이지 크롤링 결과, 선택 — scripts/crawl_homepages.py 로 생성)

위 두 소스만 읽어서 wiki/ 아래 마크다운 파일들을 결정론적으로 (재실행해도 동일한 결과가
나오도록) 생성합니다. wiki/**/*.md 는 직접 손으로 수정하지 마세요 — 원본을 고치고 다시
이 스크립트를 실행하세요. 자세한 설계 원칙은 CLAUDE.md 참고.

사용법:
    python3 scripts/build_wiki.py
"""
from __future__ import annotations

import json
import re
from collections import Counter, OrderedDict, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "sources"
WIKI_DIR = ROOT / "wiki"
SOURCE_FILE = DATA_DIR / "faculty_profiles_source.json"
CRAWL_FILE = DATA_DIR / "homepage_crawl.json"
FALLBACK_FILE = DATA_DIR / "faculty_fallback_summary.json"

BUILD_DATE = date.today().isoformat()

# text_public 안에서 "라벨: 내용" 한 줄짜리 필드로 나오는 라벨들
KV_LINE_RE = re.compile(r"^([^:\[\]]{1,24}):\s?(.*)$")
# text_public 안에서 "[라벨] 내용" 형태로 나오는 필드들
BRACKET_LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")

# 교원 페이지 안에서 이미 별도 섹션(관심분야/실적건수)으로 렌더링하므로
# text_public 파싱 결과에서는 건너뛰는 라벨
SKIP_LABELS = {"성명", "연구관심분야"}

# wiki/researchers.json 의 ai_summary 필드(대시보드 자연어 검색이 AI API로 보내는
# 압축 프로필)에 쓰이는 글자수 제한 — 토큰/전송량을 억제하기 위한 값
AI_SUMMARY_LIMITS = {"interests": 220, "keywords": 160, "highlight": 200}

# 정부 "12대 국가전략기술" 공식 분류 (원본 text_public의 "국가전략기술" 필드가
# 이 분류를 참조하는 자유 서술형 텍스트라, 표준 명칭으로 정규화해 인덱스를 만든다)
NATIONAL_TECH_CATEGORIES = [
    "반도체·디스플레이", "이차전지", "첨단 모빌리티", "차세대 원자력", "첨단 바이오",
    "우주항공·해양", "수소", "사이버보안", "인공지능", "차세대 통신",
    "첨단로봇·제조", "양자",
]
NATIONAL_TECH_FIELD_RE = re.compile(r"국가전략기술:\s*(.*)")


def _norm_tech_key(s: str) -> str:
    return re.sub(r"[\s·/]", "", s)


_TECH_KEY_TO_CANON = {_norm_tech_key(c): c for c in NATIONAL_TECH_CATEGORIES}


def parse_national_tech(text_public: str) -> list[str]:
    """text_public의 '국가전략기술' 필드에서 12대 국가전략기술 카테고리를 추출한다.
    자유 서술형(번호 매김·괄호 세부사항·쉼표 나열이 뒤섞여 있음) 텍스트라 완벽하지
    않을 수 있다 — 매칭 안 되는 항목은 조용히 버리지 않고 open-questions.md 에서
    다룬다. 원본에 없는 카테고리를 지어내지 않고, 표준 12개 명칭에 매칭되는 것만 뽑는다."""
    m = NATIONAL_TECH_FIELD_RE.search(text_public or "")
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw or raw in {"기재X", "게재X"} or raw.startswith("12대 국가전략기술"):
        return []
    raw = re.sub(r"^\s*\d+\.\s*", "", raw)  # 맨 앞 "1. " 제거
    parts = re.split(r"\d+\.\s*|,\s*(?![^(]*\))", raw)  # 괄호 안 쉼표는 보존
    found: list[str] = []
    for part in parts:
        part = part.strip().strip(",")
        if not part:
            continue
        head = part.split("(")[0].strip()
        key = _norm_tech_key(head)
        canon = _TECH_KEY_TO_CANON.get(key)
        if not canon:
            for k, c in _TECH_KEY_TO_CANON.items():
                if len(k) >= 2 and (k in key or key in k):
                    canon = c
                    break
        if canon and canon not in found:
            found.append(canon)
    return found


def slugify_dept(name: str) -> str:
    return (name or "미분류").strip()


def dept_link(dept: str) -> str:
    return f"[{dept}](../domain/{slugify_dept(dept)}.moc.md)"


def faculty_filename(rec: dict) -> str:
    return f"{rec['개인번호']}-{rec['성명']}.md"


def faculty_link_from(rec: dict, relative_prefix: str = "../faculty/") -> str:
    return f"[{rec['성명']}]({relative_prefix}{faculty_filename(rec)})"


def parse_text_public(text: str) -> "OrderedDict[str, str]":
    """text_public을 줄 단위로 훑어 {섹션명: 내용} 순서를 보존한 딕셔너리로 변환."""
    sections: "OrderedDict[str, str]" = OrderedDict()
    current = None
    for raw_line in (text or "").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = BRACKET_LINE_RE.match(line)
        if m:
            label, content = m.group(1).strip(), m.group(2).strip()
            sections[label] = content
            current = label
            continue
        m = KV_LINE_RE.match(line)
        if m:
            label, content = m.group(1).strip(), m.group(2).strip()
            sections[label] = content
            current = label
            continue
        # 어느 패턴에도 안 맞으면 직전 섹션의 연속으로 취급
        if current is not None:
            sections[current] += " " + line.strip()
    return sections


def _split_outside_parens(text: str, sep: str) -> list[str]:
    """`sep`로 나누되, 괄호(중첩 포함) 안에 있는 occurrence는 구분자로 보지 않는다.
    논문 인용이 "제목 (연도; 저널명 (도시))"처럼 괄호 안에도 같은 구분자를 쓰는
    경우가 있어, 괄호 깊이를 세면서 깊이 0일 때의 occurrence만 실제 구분자로
    인정한다."""
    parts = []
    depth = 0
    start = i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


def split_list_items(text: str) -> list[str] | None:
    """￭ 또는 반복되는 '; ' 로 나열된 텍스트를 항목 리스트로 쪼갠다. 나열형이 아니면
    (구분자가 2개 미만이면) None을 돌려줘 원문 그대로 쓰게 한다. 순수 표시 형식만
    바꾸는 것이라 원본 내용은 그대로 유지된다 (No Hallucination 원칙 준수)."""
    if not text:
        return None
    if "￭" in text:
        parts = [p.strip() for p in text.split("￭") if p.strip()]
        if len(parts) >= 2:
            return parts
    parts = [p.strip() for p in _split_outside_parens(text, "; ") if p.strip()]
    if len(parts) >= 2:
        return parts
    return None


def render_list_or_text(text: str) -> str:
    """나열형 필드는 불릿 리스트로, 아니면 원문 그대로 렌더링 — 긴 한 덩어리 문단이
    되는 것을 막아 가독성을 높인다."""
    items = split_list_items(text)
    if items:
        return "\n".join(f"- {item}" for item in items)
    return text


def clip_note(item: dict) -> list[str]:
    """크롤러가 상한에서 자른 글이면 '원문 N자 중 앞 M자만' 줄 (2026-10-07 — crawl_homepages.py 의 chars_total · truncated)."""
    if not item.get("truncated"):
        return []
    return [f"_원문 {item.get('chars_total', 0):,}자 중 앞 {len(item.get('text', '').strip()):,}자만 저장했습니다 (크롤러 저장 상한)._"]


def render_details(summary: str, body: str) -> list[str]:
    """접이식 블록. 크롤링한 홈페이지 원문처럼 길고 스캔하기 어려운 텍스트를
    기본은 접어두고, 필요하면 펼쳐볼 수 있게 한다."""
    return ["<details>", f"<summary>{summary}</summary>", "", body, "", "</details>"]


def perf_total(perf: dict) -> int:
    return sum((v or 0) for v in (perf or {}).values())


def truncate(text: str, limit: int) -> str:
    s = (text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def render_perf_table(perf: dict) -> str:
    if not perf:
        return "_실적 데이터 없음_\n"
    header = "| 구분 | 건수 |\n|---|---|\n"
    rows = "".join(f"| {k} | {v} |\n" for k, v in perf.items())
    return header + rows


def normalize_records(records: list[dict]) -> None:
    """레코드를 제자리에서 정규화한다 (원본 JSON 파일은 건드리지 않음 — sources/ 는
    불변). 다른 스크립트(summarize_faculty_fallback.py 등)도 build_wiki.py 와 같은
    레코드 상태를 보도록 이 함수를 공유한다."""
    for r in records:
        r["학과"] = (r.get("학과") or "").strip() or "미분류"
        # 홈페이지 URL 끝에 개행이 섞인 원본 데이터가 있어(예: 염한웅 등 9명) crawl 딕셔너리
        # 조회("crawl.get(homepage)")가 어긋나 AI 요약이 있는데도 안 뜨는 문제가 있었다 —
        # 순수 공백 정규화라 원본 내용은 그대로 유지된다 (No Hallucination 원칙 준수).
        r["홈페이지"] = (r.get("홈페이지") or "").strip()


# summarize_homepages.py가 크롤링 원문에서 해당 교원 본인 정보를 못 찾았을 때 Gemini에게
# 정확히 이 문장만 답하도록 지시하는 고정 문구 — 크롤링·요약 자체는 "성공"해 summary 필드가
# 채워지지만, 내용은 실질적으로 빈 것이나 마찬가지라 get_homepage_summary()에서 빈 문자열과
# 동일하게 취급한다(학과 공통 홈페이지가 크롤링되었지만 본인 소개가 없는 경우 등에서 발생 —
# 37명 확인, open-questions.md 참고). 두 스크립트가 같은 문구를 쓰도록 여기서 한 번만 정의.
HOMEPAGE_SUMMARY_NOT_FOUND = "홈페이지 원문에서 본인 관련 정보를 명확히 찾지 못했습니다."


def get_homepage_summary(rec: dict, crawl: dict) -> str:
    """홈페이지 크롤링 원문을 Gemini가 요약한 필드. 크롤링 원문 자체가 없거나(홈페이지
    미등록·학과 공통 포털 제외·크롤링 실패), 크롤링·요약은 됐지만 Gemini가 본인 정보를
    못 찾았다고 답한 경우(HOMEPAGE_SUMMARY_NOT_FOUND) 빈 문자열."""
    homepage = rec.get("홈페이지", "") or ""
    crawled = crawl.get(homepage) if homepage else None
    summary = (crawled.get("summary") or "").strip() if crawled and crawled.get("text") else ""
    return "" if summary == HOMEPAGE_SUMMARY_NOT_FOUND else summary


def render_faculty_page(rec: dict, crawl: dict, fallback: dict) -> str:
    name = rec["성명"]
    dept = rec.get("학과", "").strip() or "미분류"
    email = rec.get("이메일", "") or "_기재 없음_"
    homepage = rec.get("홈페이지", "") or ""
    interests = rec.get("관심분야", "") or "_기재 없음_"
    perf = rec.get("실적건수", {}) or {}

    parsed = parse_text_public(rec.get("text_public", ""))

    # 홈페이지 크롤링 + AI 요약은 페이지 아래쪽 "홈페이지 추가 정보"에서도 쓰므로 여기서
    # 한 번만 조회해 둔다 — 뒤 내용(실적·논문 목록 등)이 방대해 훑기 어려우니, 요약이
    # 있으면 페이지 상단(연구관심분야보다 앞)에도 미리 보여준다.
    crawled = crawl.get(homepage) if homepage else None
    homepage_summary = get_homepage_summary(rec, crawl)
    # 홈페이지 요약이 없는 경우(미등록·공통 포털 제외·크롤링 실패)에만 위키 자체 필드
    # (관심분야·실적·논문 등) 기반 폴백 요약을 쓴다 — scripts/summarize_faculty_fallback.py 참고.
    fallback_entry = fallback.get(str(rec["개인번호"])) if not homepage_summary else None
    fallback_summary = (fallback_entry.get("summary") or "").strip() if fallback_entry else ""

    lines = []
    lines.append("---")
    lines.append(f"id: {rec['개인번호']}")
    lines.append(f"name: {name}")
    lines.append(f"department: {dept}")
    lines.append(f"email: {email}")
    lines.append(f"homepage: {homepage}")
    lines.append(f"updated: {BUILD_DATE}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name} ({dept})")
    lines.append("")
    lines.append("## 기본 정보")
    lines.append(f"- 학과: {dept_link(dept)}")
    lines.append(f"- 이메일: {email}")
    if homepage:
        lines.append(f"- 홈페이지: <{homepage}>")
    else:
        lines.append("- 홈페이지: _등록된 홈페이지 없음_")
    lines.append("")
    if homepage_summary:
        lines.append("> [!TIP]")
        lines.append("> **AI 요약** _(Gemini가 홈페이지를 읽고 요약 · 자세한 내용은 아래 실적·논문 목록 참고)_")
        lines.append(">")
        for summary_line in homepage_summary.split("\n"):
            lines.append(f"> {summary_line}")
        lines.append("")
    elif fallback_summary:
        lines.append("> [!NOTE]")
        lines.append(
            "> **AI 요약 (위키 데이터 기반)** _(홈페이지 크롤링 정보가 없어, 이 위키에 기록된 "
            "연구관심분야·실적·논문 정보를 바탕으로 AI가 작성 · 자세한 내용은 아래 실적·논문 목록 참고)_"
        )
        lines.append(">")
        for summary_line in fallback_summary.split("\n"):
            lines.append(f"> {summary_line}")
        lines.append("")
    lines.append("## 연구관심분야")
    lines.append(render_list_or_text(interests))
    lines.append("")
    lines.append("## 실적 요약")
    lines.append(render_perf_table(perf))

    for label, content in parsed.items():
        if label in SKIP_LABELS:
            continue
        lines.append(f"## {label}")
        lines.append(render_list_or_text(content))
        lines.append("")

    lines.append("## 홈페이지 추가 정보")
    if crawled and crawled.get("skipped") == "shared_portal":
        lines.append(
            f"_이 URL은 다른 교원과 함께 쓰는 학과/그룹 공통 포털로 판단되어 "
            f"(총 {crawled.get('shared_by', '?')}명이 동일 URL 등록) 크롤링하지 않았습니다._"
        )
    elif crawled and crawled.get("text"):
        lines.append(f"> 크롤링 시각: {crawled.get('fetched_at', '알 수 없음')} · 출처: <{homepage}>")
        lines.append("")

        if homepage_summary:
            lines.append(
                f"_AI 요약은 이 페이지 맨 위에서 볼 수 있습니다 "
                f"(Gemini 자동 요약 · {crawled.get('summary_generated_at', '')}). 원문은 아래에서 확인 가능합니다._"
            )
            lines.append("")

        if crawled.get("error"):
            # 다시 받기에 실패해 예전 글을 그대로 둔 경우(crawl_homepages.py 2026-10-07) — 언제 글인지 위 크롤링 시각이 알려 준다
            lines.append(
                f"_{crawled.get('last_attempt_at', '최근')} 다시 받기에 실패해({crawled['error'][:120]}) "
                f"위 크롤링 시각의 내용을 보여 줍니다._"
            )
            lines.append("")

        main_text = crawled["text"].strip()
        if len(main_text) > 300:
            lines.extend(render_details("홈페이지 원문 보기", main_text))
        else:
            lines.append(main_text)
        lines.extend(clip_note(crawled))
        lines.append("")

        subpages = {u: s for u, s in (crawled.get("subpages") or {}).items() if s.get("text")}
        if subpages:
            lines.append(f"### 홈페이지 내 세부 페이지 ({len(subpages)}개)")
            lines.append("")
            for sub_url, sub in subpages.items():
                sub_title = sub.get("title") or sub_url
                lines.append(f"#### {sub_title}")
                lines.append(f"> 출처: <{sub_url}>")
                lines.append("")
                sub_text = sub["text"].strip()
                if len(sub_text) > 300:
                    lines.extend(render_details("내용 보기", sub_text))
                else:
                    lines.append(sub_text)
                lines.extend(clip_note(sub))
                lines.append("")
    elif not homepage:
        lines.append("_등록된 홈페이지가 없어 크롤링 대상이 아닙니다._")
    elif crawled is not None:
        # 크롤링을 시도는 했으나 텍스트를 얻지 못한 경우 (접속 실패, 빈 페이지 등)
        reason = crawled.get("error") or (f"HTTP {crawled.get('status')}" if crawled.get("status") else "알 수 없는 사유")
        lines.append(
            f"_크롤링을 시도했지만 내용을 가져오지 못했습니다 ({reason}). "
            f"홈페이지가 자바스크립트로 렌더링되거나 접근이 제한되어 있을 수 있습니다. "
            f"시도 시각: {crawled.get('fetched_at', '알 수 없음')}_"
        )
    else:
        lines.append(
            "_아직 크롤링되지 않았습니다. `scripts/crawl_homepages.py` 를 인터넷 접근이 "
            "가능한 환경에서 실행해 `sources/homepage_crawl.json` 을 만든 뒤 "
            "`scripts/build_wiki.py` 를 다시 실행하면 이 섹션이 채워집니다._"
        )
    lines.append("")

    lines.append("## 출처")
    lines.append(f"- POSTECH R&D 실적 데이터베이스 (개인번호 {rec['개인번호']})")
    lines.append(f"- 최종 갱신: {BUILD_DATE}")
    lines.append("")

    return "\n".join(lines)


def render_index(records: list[dict], by_dept: dict[str, list[dict]]) -> str:
    """색인(index.md) — 기계가 유지하는 평면 카탈로그. 도메인을 어떻게 읽어야 하는지는
    큐레이션된 wiki/home.md 와 wiki/domain/*.moc.md 쪽을 참고 (이 파일들은 스크립트가
    아니라 LLM이 직접 쓰고 유지한다 — CLAUDE.md 참고)."""
    lines = []
    lines.append("---")
    lines.append("title: POSTECH 교원 R&D 위키 색인")
    lines.append(f"faculty_count: {len(records)}")
    lines.append(f"updated: {BUILD_DATE}")
    lines.append("---")
    lines.append("")
    lines.append("# 색인")
    lines.append("")
    lines.append(
        "이 파일은 `scripts/build_wiki.py` 가 매번 다시 생성하는 **평면 카탈로그**입니다. "
        "큐레이션된 진입점은 [home.md](home.md), 구조·갱신 규칙은 [CLAUDE.md](../CLAUDE.md) 를 보세요."
    )
    lines.append("")
    lines.append(f"- 전체 교원: **{len(records)}명**")
    lines.append(f"- 학과 수: **{len(by_dept)}개**")
    lines.append(f"- 최종 생성일: {BUILD_DATE}")
    lines.append("")
    lines.append("## 학과별 교원 목록")
    lines.append("")
    for dept in sorted(by_dept.keys()):
        members = by_dept[dept]
        lines.append(f"- {dept} ({len(members)}명) — MOC: [domain/{slugify_dept(dept)}.moc.md](domain/{slugify_dept(dept)}.moc.md)")
    lines.append("")
    lines.append("## 기타")
    lines.append("- [home.md](home.md) — 큐레이션된 진입점")
    lines.append("- [연구분야 키워드 인덱스](research-areas.md)")
    lines.append("- [국가전략기술 인덱스](national-strategic-tech.md)")
    lines.append("- [전체 교원 가나다순 목록](faculty-index.md)")
    lines.append("- [log.md](log.md) — 변경 이력")
    lines.append("- [open-questions.md](open-questions.md) — 모순·미해결 이슈")
    lines.append("")
    return "\n".join(lines)


def render_faculty_flat_index(records: list[dict]) -> str:
    lines = ["# 전체 교원 가나다순 목록", ""]
    for r in sorted(records, key=lambda r: r["성명"]):
        dept = r.get("학과", "").strip() or "미분류"
        lines.append(f"- {faculty_link_from(r, 'faculty/')} ({dept})")
    lines.append("")
    lines.append("[← 전체 인덱스로](index.md)")
    lines.append("")
    return "\n".join(lines)


def render_research_areas(records: list[dict]) -> str:
    counter: Counter[str] = Counter()
    keyword_to_faculty: defaultdict[str, list[dict]] = defaultdict(list)
    for r in records:
        raw = (r.get("관심분야") or "").replace("￭", "|")
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        for p in parts:
            counter[p] += 1
            keyword_to_faculty[p].append(r)

    lines = ["# 연구분야 키워드 인덱스", ""]
    lines.append(
        "원본 `관심분야` 필드를 그대로 분리·집계한 목록입니다 (자유 서술형 텍스트라 완전히 "
        "정규화되어 있지는 않습니다). 2명 이상이 공유하는 키워드만 표시합니다."
    )
    lines.append("")
    shared = [(k, v) for k, v in counter.items() if v >= 2]
    shared.sort(key=lambda kv: (-kv[1], kv[0]))
    for keyword, count in shared:
        names = ", ".join(faculty_link_from(r, "faculty/") for r in keyword_to_faculty[keyword])
        lines.append(f"- **{keyword}** ({count}명): {names}")
    lines.append("")
    lines.append("[← 전체 인덱스로](index.md)")
    lines.append("")
    return "\n".join(lines)


def render_national_tech(records: list[dict]) -> str:
    """정부 12대 국가전략기술 분류별 교원 인덱스. 원본 `text_public`의 '국가전략기술'
    필드(자유 서술형)를 표준 12개 명칭으로 정규화해 집계한다 — RFP/공모사업의 기술
    분야와 매칭되는 교원을 찾을 때 쓴다."""
    tech_to_faculty: defaultdict[str, list[dict]] = defaultdict(list)
    unmatched_n = 0
    tagged_n = 0
    for r in records:
        tags = parse_national_tech(r.get("text_public", ""))
        if tags:
            tagged_n += 1
        for t in tags:
            tech_to_faculty[t].append(r)

    lines = ["# 국가전략기술 인덱스", ""]
    lines.append(
        "정부 12대 국가전략기술 분류를 기준으로 정리한 교원 인덱스입니다. 원본 `text_public`의 "
        "'국가전략기술' 필드(자유 서술형)를 표준 명칭으로 정규화해 집계했습니다 — RFP·공모사업의 "
        "기술 분야에 맞는 교원을 빠르게 찾는 용도입니다."
    )
    lines.append("")
    lines.append(f"- 원본 데이터에 국가전략기술 태그가 있는 교원: **{tagged_n}명** / 298명")
    lines.append("")
    for cat in NATIONAL_TECH_CATEGORIES:
        members = tech_to_faculty.get(cat, [])
        lines.append(f"## {cat} ({len(members)}명)")
        if members:
            for r in sorted(members, key=lambda r: r["성명"]):
                dept = r.get("학과", "").strip() or "미분류"
                lines.append(f"- {faculty_link_from(r, 'faculty/')} ({dept})")
        else:
            lines.append("_해당 분야로 태그된 교원 없음_")
        lines.append("")
    lines.append(
        "_원문이 자유 서술형이라 매칭이 완벽하지 않을 수 있습니다 — 한계는 "
        "[open-questions.md](open-questions.md) 참고._"
    )
    lines.append("")
    lines.append("[← 전체 인덱스로](index.md)")
    lines.append("")
    return "\n".join(lines)


def build_researchers_json(records: list[dict], crawl: dict, fallback: dict) -> dict:
    """대시보드(dashboard/index.html)가 fetch로 읽는 경량 JSON 인덱스를 만든다.

    wiki/faculty/*.md 와 같은 원본(sources/faculty_profiles_source.json)에서 결정론적으로
    파생되는 산출물이다 — 손으로 고치지 말고 이 스크립트를 다시 실행할 것. wiki/faculty/*.md
    와 마찬가지로 나열형 필드는 render_list_or_text로 불릿 리스트화해 대시보드 상세 모달의
    가독성을 맞춘다 (CLAUDE.md 참고).
    """
    by_dept: "OrderedDict[str, int]" = OrderedDict()
    researchers = []
    for r in sorted(records, key=lambda r: r["성명"]):
        dept = (r.get("학과") or "").strip() or "미분류"
        by_dept[dept] = by_dept.get(dept, 0) + 1

        homepage = r.get("홈페이지", "") or ""
        # 홈페이지 크롤링을 Gemini가 요약한 것 — wiki/faculty/*.md 상단 AI 요약과 같은
        # 필드(원본 그대로, 대시보드용으로 별도로 자르거나 손대지 않음).
        homepage_summary = get_homepage_summary(r, crawl)
        # 홈페이지 요약이 없을 때만 쓰는 위키 데이터 기반 폴백 요약 — wiki/faculty/*.md 와
        # 같은 우선순위(홈페이지 요약이 있으면 폴백은 안 씀).
        fallback_entry = fallback.get(str(r["개인번호"])) if not homepage_summary else None
        fallback_summary = (fallback_entry.get("summary") or "").strip() if fallback_entry else ""

        interests_raw = (r.get("관심분야") or "").strip()
        parsed = parse_text_public(r.get("text_public", ""))
        keywords = parsed.get("연구키워드", "")
        highlight = (
            parsed.get("대표연구·최근 주도논문(제1/교신)")
            or parsed.get("주요성과")
            or ""
        )
        perf = r.get("실적건수", {}) or {}
        sections = OrderedDict(
            (label, render_list_or_text(content))
            for label, content in parsed.items()
            if label not in SKIP_LABELS
        )

        researchers.append(
            {
                "id": r["개인번호"],
                "name": r["성명"],
                "department": dept,
                "email": r.get("이메일", ""),
                "homepage": homepage,
                "homepage_summary": homepage_summary,
                "fallback_summary": fallback_summary,
                "interests": render_list_or_text(interests_raw),
                "perf": perf,
                "perf_total": perf_total(perf),
                # 정부 12대 국가전략기술 표준 명칭으로 정규화된 태그 — national-strategic-tech.md
                # 와 같은 parse_national_tech() 결과를 재사용 (원본 무결성: 새로 판단하지 않음)
                "national_tech": parse_national_tech(r.get("text_public", "")),
                "sections": sections,
                "wiki_path": f"faculty/{faculty_filename(r)}",
                # AI 자연어 추천이 외부 API로 전송하는 압축 프로필 — 원본 필드를 그대로
                # 잘라낸 것일 뿐 창작하지 않는다 (원본 무결성 원칙, CLAUDE.md 참고).
                # 불릿 마커 없이 " · " 로 이어붙인 압축 버전을 써서 토큰을 아낀다.
                "ai_summary": {
                    "interests": truncate(
                        re.sub(r"\s+", " ", interests_raw.replace("￭", " · ")).strip(" ·"),
                        AI_SUMMARY_LIMITS["interests"],
                    ),
                    "keywords": truncate(keywords, AI_SUMMARY_LIMITS["keywords"]),
                    "highlight": truncate(highlight, AI_SUMMARY_LIMITS["highlight"]),
                },
            }
        )

    return {
        "generated": BUILD_DATE,
        "count": len(researchers),
        "departments": [{"name": k, "count": v} for k, v in by_dept.items()],
        # 대시보드의 국가전략기술 필터가 쓰는 표준 12개 분야 순서 — NATIONAL_TECH_CATEGORIES
        # 를 그대로 노출해 대시보드 쪽에 하드코딩하지 않게 한다.
        "tech_categories": NATIONAL_TECH_CATEGORIES,
        "researchers": researchers,
    }


def main() -> None:
    if not SOURCE_FILE.exists():
        raise SystemExit(f"원본 파일이 없습니다: {SOURCE_FILE}")

    records = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    crawl = {}
    if CRAWL_FILE.exists():
        crawl = json.loads(CRAWL_FILE.read_text(encoding="utf-8"))
    fallback = {}
    if FALLBACK_FILE.exists():
        fallback = json.loads(FALLBACK_FILE.read_text(encoding="utf-8"))

    normalize_records(records)
    by_dept: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_dept[r["학과"]].append(r)

    faculty_dir = WIKI_DIR / "faculty"
    faculty_dir.mkdir(parents=True, exist_ok=True)

    for r in records:
        page = render_faculty_page(r, crawl, fallback)
        (faculty_dir / faculty_filename(r)).write_text(page, encoding="utf-8")

    (WIKI_DIR / "index.md").write_text(render_index(records, by_dept), encoding="utf-8")
    (WIKI_DIR / "faculty-index.md").write_text(render_faculty_flat_index(records), encoding="utf-8")
    (WIKI_DIR / "research-areas.md").write_text(render_research_areas(records), encoding="utf-8")
    (WIKI_DIR / "national-strategic-tech.md").write_text(render_national_tech(records), encoding="utf-8")

    researchers_json = build_researchers_json(records, crawl, fallback)
    (WIKI_DIR / "researchers.json").write_text(
        json.dumps(researchers_json, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(f"생성 완료: 교원 {len(records)}명, 학과 {len(by_dept)}개")
    print(f"  wiki/faculty/      {len(records)} 개 파일 (결정론적 생성)")
    print("  wiki/index.md, wiki/faculty-index.md, wiki/research-areas.md, wiki/national-strategic-tech.md")
    print("  wiki/researchers.json  (dashboard/index.html 이 읽는 경량 인덱스)")
    print(
        "  wiki/home.md, wiki/domain/*.moc.md, wiki/log.md, wiki/open-questions.md 는 "
        "이 스크립트가 건드리지 않습니다 (LLM이 직접 쓰고 유지하는 큐레이션 레이어 — CLAUDE.md 참고)"
    )
    if not crawl:
        print(
            "참고: sources/homepage_crawl.json 이 없어 '홈페이지 추가 정보' 섹션은 "
            "플레이스홀더로 채워졌습니다. scripts/crawl_homepages.py 참고."
        )
    if not fallback:
        print(
            "참고: sources/faculty_fallback_summary.json 이 없어 홈페이지 요약이 없는 교원은 "
            "AI 요약 없이 표시됩니다. scripts/summarize_faculty_fallback.py 참고."
        )


if __name__ == "__main__":
    main()
