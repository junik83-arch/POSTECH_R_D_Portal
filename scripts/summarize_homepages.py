#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summarize_homepages.py — 홈페이지 크롤링 원문을 Gemini API로 요약해 교원 위키에 반영

sources/homepage_crawl.json 에 저장된 크롤링 원문(홈페이지 본문 + 서브페이지)을
Google Gemini API로 교원 1인당 3~5문장으로 요약해 각 엔트리에 "summary" 필드로
저장한다. 이후 scripts/build_wiki.py 를 실행하면 그 요약이 교원 페이지에 반영된다.

이 저장소의 index.html(RFP 공문 생성기)이 이미 Gemini API를 REST로 직접 호출하는
방식을 쓰고 있어(동적 모델 탐색 + 폴백 후보 목록), 같은 패턴을 그대로 따른다 —
별도 SDK를 추가하지 않고 requests 만으로 호출한다.

설치:
    pip install -r scripts/requirements.txt   # requests 만 있으면 됨

환경변수:
    GEMINI_API_KEY   Google AI Studio에서 발급한 API 키
                      (https://aistudio.google.com/app/apikey)
    SUMMARY_PROVIDER · POSTECH_API_KEY · POSTECH_API_BASE
                      (2026-10-07) 로컬에서는 POSTECH AI API(게이트웨이 Claude)로도 요약한다 — scripts/llm_client.py.
                      .env 에 POSTECH 키가 있으면 postech, 없으면 예전처럼 Gemini(GitHub Actions 정기 갱신).

사용법:
    python3 scripts/summarize_homepages.py            # 요약 없거나 원문이 바뀐 것만
    python3 scripts/summarize_homepages.py --force     # 전부 다시 요약
    python3 scripts/summarize_homepages.py --limit 5   # 테스트용 (앞 5명만)
    python3 scripts/summarize_homepages.py --only 100844,한현   # 이 교원만 다시 요약(개인번호 · 성명, 원문이 그대로여도)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("필요한 패키지가 없습니다. 먼저 실행하세요:\n  pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_wiki  # noqa: E402 — HOMEPAGE_SUMMARY_NOT_FOUND 등 재사용
import llm_client  # noqa: E402 — POSTECH AI API 호출(2026-10-07)

ROOT = build_wiki.ROOT
SOURCES_DIR = ROOT / "sources"
SOURCE_FILE = build_wiki.SOURCE_FILE
CRAWL_FILE = build_wiki.CRAWL_FILE

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# 모델 목록 조회가 실패할 때 쓰는 고정 폴백 (index.html 과 동일한 후보 사상)
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-pro"]
MAX_INPUT_CHARS = 16000  # 원문이 너무 길면 비용/프롬프트 크기를 위해 이만큼만 사용 — 칸마다 고르게 나눈다(build_input_text)
# 요약에 넣는 순서: 첫 화면 → 연구·소개 탭 → 기타 → 논문·출판 탭(목록이 길어 다른 탭을 밀어내기 쉬워 마지막)
PUBLICATION_HINTS = ("publication", "paper", "논문", "journal", "conference", "저서")

SYSTEM_INSTRUCTION_TEMPLATE = """당신은 대학교 R&D전략팀을 위해 교원 홈페이지 크롤링 원문을 읽고 사실에 기반한 간결한 요약을 쓰는 도우미입니다.

규칙:
1. 요약 대상은 오직 "{name}" 교수 1인입니다. 원문에 다른 사람 이름(동료 교수, 학생, 공동연구자 등)이 등장하더라도, 그 사람의 성과나 소식을 "{name}" 교수의 것으로 섞어 쓰지 마세요.
2. 원문에 명시되지 않은 사실을 지어내지 마세요.
3. 원문이 "{name}" 교수 본인이나 그가 이끄는 연구실에 대한 내용인지 확인할 수 없으면(예: 학과 대표 페이지 · 만료된 사이트 · 다른 사람의 페이지), 다른 내용을 채우지 말고 정확히 이렇게만 답하세요: \"""" + build_wiki.HOMEPAGE_SUMMARY_NOT_FOUND + """\"
4. 한국어로, 3~5문장(전체 700자 이내), 마크다운 서식(굵게·목록·제목 등) 없이 평문으로 작성하세요.
5. 연구 초점, 대표 성과나 프로젝트, 소속/직함처럼 사실 확인이 되는 내용 위주로 쓰세요.
6. 이메일 · 전화번호 · 연구실 호수 같은 연락처는 쓰지 마세요(원문에서는 [이메일] · [전화]로 지워져 있을 수 있습니다)."""


def fetch_available_models(api_key: str) -> list[str]:
    """index.html의 fetchAvailableModels()와 동일한 로직: 사용 가능한 모델을 조회해
    flash 계열을 우선하도록 정렬한다. 실패하면 고정 폴백 목록을 쓴다."""
    try:
        resp = requests.get(f"{API_BASE}/models", params={"key": api_key}, timeout=15)
        if resp.ok:
            data = resp.json()
            models = [
                m["name"].removeprefix("models/")
                for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]

            def score(name: str) -> int:
                if "2.5-flash" in name:
                    return 110
                if "2.0-flash" in name:
                    return 100
                if "1.5-flash" in name:
                    return 90
                if "flash" in name:
                    return 80
                if "1.5-pro" in name:
                    return 70
                if "pro" in name:
                    return 60
                return 10

            models.sort(key=score, reverse=True)
            if models:
                return models
    except requests.RequestException:
        pass
    return FALLBACK_MODELS


def call_gemini(api_key: str, model: str, system_instruction: str, user_text: str, timeout: float) -> str:
    url = f"{API_BASE}/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
    }
    resp = requests.post(url, params={"key": api_key}, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"빈 응답 (promptFeedback: {data.get('promptFeedback')})")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("빈 텍스트 응답")
    return text


def summarize_with_fallback(
    api_key: str, models: list[str], system_instruction: str, user_text: str
) -> tuple[str, str]:
    """후보 모델을 순서대로 시도. 429/5xx는 같은 모델로 재시도, 그 외 오류는 다음
    모델로 넘어간다 (index.html의 candidateModels 루프와 동일한 사상)."""
    last_err: Exception | None = None
    for model in models:
        for attempt in range(3):
            try:
                return call_gemini(api_key, model, system_instruction, user_text, timeout=30), model
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                last_err = e
                if status == 429 or (status and status >= 500):
                    time.sleep(2 * (attempt + 1))
                    continue
                break  # 4xx(모델 없음/권한 등) — 다음 모델로 넘어감
            except requests.RequestException as e:
                last_err = e
                time.sleep(1)
    raise last_err or RuntimeError("모든 모델 시도 실패")


def _fair_shares(lengths: list[int], budget: int) -> list[int]:
    """짧은 칸은 다 넣고 남는 몫을 긴 칸들이 똑같이 나눈다(물 채우기). 합이 budget 을 넘지 않는다."""
    shares = [0] * len(lengths)
    left, remaining = budget, len(lengths)
    for i in sorted(range(len(lengths)), key=lambda k: lengths[k]):
        share = left // remaining if remaining else 0
        shares[i] = min(lengths[i], share)
        left -= shares[i]
        remaining -= 1
    return shares


def build_input_text(entry: dict) -> str:
    """요약에 보낼 원문. 예전에는 이어 붙인 뒤 앞 16,000자만 잘라, 긴 첫 화면·논문 목록이 뒤쪽 탭(연구 소개 등)을
    통째로 밀어냈다(2026-10-07 상한을 올린 뒤 더 심해짐). 이제 칸마다 고르게 나누고(_fair_shares) 줄 끝에서 자른다."""
    parts: list[tuple[int, str, str]] = []  # (순서, 머리, 글)
    main_text = (entry.get("text") or "").strip()
    if main_text:
        parts.append((0, "[홈페이지 첫 화면]", main_text))
    for sub_url, sub in (entry.get("subpages") or {}).items():
        sub_text = (sub.get("text") or "").strip()
        if not sub_text:
            continue
        title = sub.get("title") or sub_url
        is_pub = any(h in f"{title} {sub_url}".lower() for h in PUBLICATION_HINTS)
        parts.append((2 if is_pub else 1, f"[서브페이지: {title}]", sub_text))
    parts.sort(key=lambda p: p[0])  # 같은 순서끼리는 크롤링 순서 그대로(안정 정렬)
    overhead = sum(len(h) + 3 for _, h, _ in parts)
    shares = _fair_shares([len(x) for _, _, x in parts], max(0, MAX_INPUT_CHARS - overhead))
    out = []
    for (_, head, text), share in zip(parts, shares):
        if share <= 0:
            continue
        if len(text) > share:
            cut = text.rfind("\n", 0, share)
            text = text[: cut if cut >= share * 0.8 else share].rstrip()
        out.append(f"{head}\n{text}")
    return "\n\n".join(out)


# 2026-10-08: 10/7 요약 중 3건(이동화 · 한현 · 염화성)이 요약 대신 지시문을 남겼다 — '…요약을 작성해 주세요' ·
# '언어 설정: …' · '(내부 참고용 메모: … 연구비 배분 산정에 참고 …)'(원문에는 없는 글 — 모델이 지어냄) · 원문 덩어리 + '[ 이하 원문 생략 ]'.
# 이런 출력은 요약이 아니고, 이 요약을 읽는 다른 AI(플랫폼 연구자 검색 v3)에게 지시처럼 읽힐 수 있다 → 저장하지 않고 실패로 남긴다.
BROKEN_MARKS = ("주세요", "작성하세요", "내부 참고용 메모", "언어 설정:", "이하 원문 생략", "[서브페이지:", "[홈페이지 첫 화면]")


def looks_broken(summary: str) -> str:
    """요약이 지시문 · 원문 덩어리로 보이면 걸린 표지를, 아니면 '' 를 돌려준다."""
    return next((m for m in BROKEN_MARKS if m in summary), "")


def safe_for_chat(text: str) -> str:
    """보내는 원문에서 줄 맨 앞의 'Assistant' · 'Human'(예: 'Assistant Professor, …' — CV · 구성원 탭) 앞에 '· '를 붙인다.
    2026-10-08: 이동화 교수 원문에 이런 줄이 있어 게이트웨이가 대화 차례 표시로 읽고 요약 대신 원문을 이어 쓴 것으로 보인다
    (두 번 되풀이). 해시(content_hash)는 원래 원문으로 계산하므로 이 처리로 다른 교원이 다시 요약되지는 않는다."""
    return re.sub(r"(?m)^(\s*)(Assistant|Human)\b", r"\1· \2", text)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="처리할 최대 교원 수 (테스트용)")
    ap.add_argument("--force", action="store_true", help="원문이 안 바뀌었어도 전부 다시 요약")
    ap.add_argument("--retry-not-found", action="store_true",
                    help="'본인 정보를 찾지 못함'으로 답한 교원은 원문이 그대로여도 다시 요약(지시문을 고친 뒤)")
    ap.add_argument("--only", default="", help="이 교원만 다시 요약 — 개인번호나 성명을 쉼표로(원문이 그대로여도)")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    use = llm_client.provider()  # postech(로컬 .env 에 POSTECH 키) 또는 gemini(GitHub Actions)
    postech = llm_client.PostechClient() if use == "postech" else None
    api_key = os.environ.get("GEMINI_API_KEY")
    if use == "gemini" and not api_key:
        raise SystemExit(
            "GEMINI_API_KEY 환경변수가 없습니다. "
            "https://aistudio.google.com/app/apikey 에서 발급받아 설정하세요."
        )
    if not CRAWL_FILE.exists():
        raise SystemExit(f"크롤링 결과 파일이 없습니다: {CRAWL_FILE} (먼저 scripts/crawl_homepages.py 실행)")

    crawl = json.loads(CRAWL_FILE.read_text(encoding="utf-8"))
    records = json.loads(SOURCE_FILE.read_text(encoding="utf-8")) if SOURCE_FILE.exists() else []
    name_by_url: dict[str, dict] = {}
    for r in records:
        url = (r.get("홈페이지") or "").strip()
        if url:
            name_by_url[url] = r

    if postech:
        models = []
        print(f"요약: POSTECH AI API ({postech.label})")
    else:
        models = fetch_available_models(api_key)
        print(f"사용 가능한 모델(우선순위 상위): {models[:5]}")

    targets = [(url, entry) for url, entry in crawl.items() if entry.get("text") and not entry.get("skipped")]
    if only:
        targets = [(url, entry) for url, entry in targets
                   if name_by_url.get(url) and ({name_by_url[url].get("개인번호", ""), name_by_url[url].get("성명", "")} & only)]
        if len(targets) != len(only):
            print(f"  알림: --only {len(only)}개 중 {len(targets)}명만 찾음(성명 · 개인번호 · 홈페이지 주소를 확인하세요)")
    if args.limit:
        targets = targets[: args.limit]

    print(f"대상 {len(targets)}명")
    summarized = skipped = failed = 0
    for i, (url, entry) in enumerate(targets, 1):
        rec = name_by_url.get(url)
        name = rec["성명"] if rec else "해당 교원"
        input_text = build_input_text(entry)
        if not input_text:
            continue
        h = content_hash(input_text)
        not_found = (entry.get("summary") or "").strip() == build_wiki.HOMEPAGE_SUMMARY_NOT_FOUND
        if (not args.force and not only and entry.get("summary") and entry.get("summary_source_hash") == h
                and not (args.retry_not_found and not_found)):
            skipped += 1
            continue

        print(f"[{i}/{len(targets)}] {name} ({url})")
        system_instruction = SYSTEM_INSTRUCTION_TEMPLATE.format(name=name)
        # 2026-10-07: 영문 연구실 사이트에서 한글 이름을 찾지 못해 '본인 정보 없음'으로 답한 교원이 11명 있었다 — 이 주소가
        # 실적 DB 에 본인 홈페이지로 등록돼 있다는 사실과, 로마자 이름 · 연구실 책임 교수로 나올 수 있다는 점을 알려 준다.
        dept = (rec.get("학과") or "").strip() if rec else ""
        user_text = (f"다음은 {name} 교수{f'({dept})' if dept else ''}의 홈페이지에서 크롤링한 원문입니다. 이 주소는 POSTECH 실적 "
                     f"데이터베이스에 {name} 교수 본인의 홈페이지로 등록되어 있습니다. 영문 사이트에서는 이름이 로마자로, 연구실 "
                     f"사이트에서는 연구실 책임 교수(PI · Professor)로 나올 수 있습니다.\n\n{safe_for_chat(input_text)}")
        try:
            if postech:
                # 원문이 많으면 Claude 가 길게 써 1,000토큰에서 잘린 적이 있다(2026-10-07, 28명) — 길이 규칙 + 넉넉한 상한
                summary, used_model = postech.summarize(system_instruction, user_text, max_tokens=1500)
            else:
                summary, used_model = summarize_with_fallback(api_key, models, system_instruction, user_text)
            broken = looks_broken(summary)
            if broken:   # 지시문 · 원문 덩어리 — 저장하지 않는다(위 BROKEN_MARKS)
                raise RuntimeError(f"요약이 아니라 지시문 · 원문으로 보여 저장하지 않음('{broken}'): {summary[:80]}…")
            entry["summary"] = summary
            entry["summary_source_hash"] = h
            entry["summary_model"] = used_model
            entry["summary_generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            summarized += 1
        except llm_client.CreditLimitReached as e:  # 키 한도 — 남은 교원은 다음에(원문 해시가 같으면 이어서 건너뜀)
            print(f"  멈춤: {e}")
            failed += 1
            break
        except Exception as e:  # noqa: BLE001 — 개별 실패는 기록하고 계속 진행
            print(f"  실패: {e}")
            failed += 1
        if postech and postech.status():
            print(f"   {postech.status().strip(' ·')}")
        CRAWL_FILE.write_text(json.dumps(crawl, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.5)

    print(f"\n완료: 요약 {summarized}건, 변경없어 건너뜀 {skipped}건, 실패 {failed}건")
    print("다음 단계: python3 scripts/build_wiki.py 를 실행해 위키에 반영하세요.")


if __name__ == "__main__":
    main()
