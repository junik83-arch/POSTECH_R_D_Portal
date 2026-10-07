#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
summarize_faculty_fallback.py — 홈페이지 요약이 없는 교원을 위한 위키 데이터 기반 폴백 요약

`scripts/summarize_homepages.py`는 홈페이지 크롤링 원문을 요약하지만, 아래 세 경우엔
요약할 원문 자체가 없다 (build_wiki.py 의 get_homepage_summary() 참고):
  - 원본 데이터에 홈페이지 URL이 없음
  - 여러 교원이 공유하는 학과/그룹 공통 포털이라 크롤링 대상에서 제외됨 (shared_portal)
  - 크롤링을 시도했지만 본문을 가져오지 못함 (접속 실패·빈 페이지 등)

이 스크립트는 그런 교원에 한해, 홈페이지 원문 대신 **이미 위키에 반영된 자기 자신의
구조화 필드**(관심분야, 실적건수, text_public의 주요성과/대표연구 등)만을 입력으로 삼아
Gemini로 짧은 요약을 만들어 `sources/faculty_fallback_summary.json`에 저장한다. 이후
`scripts/build_wiki.py`를 실행하면 홈페이지 AI 요약이 없는 교원 페이지에 이 요약이 대신
표시된다 (라벨은 "AI 요약 (위키 데이터 기반)"으로 구분).

summarize_homepages.py와 마찬가지로 이 저장소의 index.html이 쓰는 Gemini REST 직접 호출
패턴(동적 모델 탐색 + 폴백 후보 목록)을 그대로 따른다.

설치:
    pip install -r scripts/requirements.txt   # requests 만 있으면 됨

환경변수:
    GEMINI_API_KEY   Google AI Studio에서 발급한 API 키
    SUMMARY_PROVIDER · POSTECH_API_KEY · POSTECH_API_BASE
                      (2026-10-07) 로컬에서는 POSTECH AI API 로도 요약한다 — scripts/llm_client.py
                      (https://aistudio.google.com/app/apikey)

사용법:
    python3 scripts/summarize_faculty_fallback.py            # 요약 없거나 원본이 바뀐 것만
    python3 scripts/summarize_faculty_fallback.py --force     # 전부 다시 요약
    python3 scripts/summarize_faculty_fallback.py --limit 5   # 테스트용 (앞 5명만)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
import build_wiki  # noqa: E402 — parse_text_public/get_homepage_summary 등 재사용 (원본 무결성: 파싱 로직 중복 방지)
import llm_client  # noqa: E402 — POSTECH AI API 호출(2026-10-07)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
SOURCE_FILE = build_wiki.SOURCE_FILE
CRAWL_FILE = build_wiki.CRAWL_FILE
FALLBACK_FILE = build_wiki.FALLBACK_FILE

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-pro"]
MAX_INPUT_CHARS = 16000

SYSTEM_INSTRUCTION_TEMPLATE = """당신은 대학교 R&D전략팀을 위해 교원 위키 페이지에 이미 정리된 구조화 정보(연구관심분야,
주요성과·대표논문 등)를 읽고 사실에 기반한 간결한 요약을 쓰는 도우미입니다. 이 교원은
홈페이지가 없거나 크롤링에 실패해, 아래 위키 데이터만을 근거로 요약해야 합니다.

규칙:
1. 요약 대상은 오직 "{name}" 교수 1인입니다. 제공된 데이터에 다른 사람 이름(공동저자 등)이 등장하더라도, 그 사람의 성과를 "{name}" 교수 본인의 것처럼 섞어 쓰지 마세요.
2. 아래 제공된 데이터에 명시되지 않은 사실을 지어내지 마세요 (소속·직함·연구실적 등 어떤 것도 추측 금지).
3. 제공된 데이터가 요약하기에 너무 부실하면(예: 관심분야만 한두 단어), 없는 내용을 채우지 말고 정확히 이렇게만 답하세요: "위키에 기록된 정보만으로는 의미 있는 요약을 작성하기 어렵습니다."
4. 한국어로, 2~3문장, 마크다운 서식(굵게·목록·제목 등) 없이 평문으로 작성하세요.
5. 연구분야, 대표 실적이나 논문처럼 사실 확인이 되는 내용 위주로 쓰세요. 홈페이지를 읽었다는 식의 표현은 쓰지 마세요 — 이 데이터는 홈페이지가 아니라 실적 데이터베이스에서 왔습니다.
6. 논문·과제·특허·학회발표 등의 **구체적인 개수(예: "논문 5편", "과제 12건")는 언급하지 마세요.** 이 개수는 매년 갱신되는데, 이 요약 문장은 재요약 전까지 그대로 남아있어 옛 숫자가 최신 실적 표와 어긋날 수 있습니다. 대신 "다수 발표했다", "여러 과제를 수행했다"처럼 숫자 없이 서술하세요."""


def fetch_available_models(api_key: str) -> list[str]:
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
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
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


def needs_fallback(rec: dict, crawl: dict) -> bool:
    """build_wiki.py가 홈페이지 AI 요약을 못 채우는 경우와 정확히 같은 조건 —
    homepage_summary가 실제로 비게 되는 교원만 대상으로 삼는다 (get_homepage_summary()
    참고). 크롤링 원문은 있지만 아직 summarize_homepages.py가 못 돈 경우(원문 O, 요약 X)는
    제외 — 그건 홈페이지 요약이 곧 채워질 대상이지 위키 데이터 폴백 대상이 아니다."""
    return not build_wiki.get_homepage_summary(rec, crawl)


def build_input_text(rec: dict) -> str:
    """위키 페이지가 이미 보여주는 필드 중 관심분야와 text_public의 모든 섹션을 그대로
    모아 Gemini 입력으로 쓴다 — 임의로 필드를 골라내지 않고 위키에 이미 있는 내용 전부를
    근거로 삼는다 (No Hallucination 원칙). 실적건수(논문/과제/특허 등 개수)는 일부러
    제외한다 — 매년 갱신되는 숫자라 요약 문장에 박히면 다음 실적 데이터 교체 후에도 재요약
    전까지 옛 숫자가 남아, 이미 최신화된 "실적 요약" 표와 요약 문장이 서로 다른 숫자를
    말하는 모순이 생길 수 있다 (교원 페이지의 실적 표는 build_wiki.py가 매번 원본에서 직접
    렌더링해 항상 최신이지만, 이 요약 문장은 재요약 전까지 고정된 텍스트이기 때문)."""
    parts = []
    interests = (rec.get("관심분야") or "").strip()
    if interests:
        parts.append(f"[연구관심분야]\n{build_wiki.render_list_or_text(interests)}")

    parsed = build_wiki.parse_text_public(rec.get("text_public", ""))
    for label, content in parsed.items():
        if label in build_wiki.SKIP_LABELS:
            continue
        parts.append(f"[{label}]\n{content}")

    return "\n\n".join(parts)[:MAX_INPUT_CHARS]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="처리할 최대 교원 수 (테스트용)")
    ap.add_argument("--force", action="store_true", help="원본이 안 바뀌었어도 전부 다시 요약")
    args = ap.parse_args()

    use = llm_client.provider()  # postech(로컬 .env 에 POSTECH 키) 또는 gemini(GitHub Actions)
    postech = llm_client.PostechClient() if use == "postech" else None
    api_key = os.environ.get("GEMINI_API_KEY")
    if use == "gemini" and not api_key:
        raise SystemExit(
            "GEMINI_API_KEY 환경변수가 없습니다. "
            "https://aistudio.google.com/app/apikey 에서 발급받아 설정하세요."
        )
    if not SOURCE_FILE.exists():
        raise SystemExit(f"원본 파일이 없습니다: {SOURCE_FILE}")

    records = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    build_wiki.normalize_records(records)  # build_wiki.py와 같은 정규화(홈페이지 공백 등) 적용
    crawl = json.loads(CRAWL_FILE.read_text(encoding="utf-8")) if CRAWL_FILE.exists() else {}
    fallback = json.loads(FALLBACK_FILE.read_text(encoding="utf-8")) if FALLBACK_FILE.exists() else {}

    if postech:
        models = []
        print(f"요약: POSTECH AI API ({postech.label})")
    else:
        models = fetch_available_models(api_key)
        print(f"사용 가능한 모델(우선순위 상위): {models[:5]}")

    targets = [r for r in records if needs_fallback(r, crawl)]
    if args.limit:
        targets = targets[: args.limit]

    print(f"대상 {len(targets)}명 (홈페이지 AI 요약이 없는 교원)")
    summarized = skipped = failed = 0
    for i, rec in enumerate(targets, 1):
        key = str(rec["개인번호"])
        name = rec["성명"]
        input_text = build_input_text(rec)
        if not input_text:
            continue
        h = content_hash(input_text)
        existing = fallback.get(key)
        if not args.force and existing and existing.get("source_hash") == h:
            skipped += 1
            continue

        print(f"[{i}/{len(targets)}] {name} ({key})")
        system_instruction = SYSTEM_INSTRUCTION_TEMPLATE.format(name=name)
        user_text = f"다음은 {name} 교수의 위키 페이지에 정리된 구조화 정보입니다.\n\n{input_text}"
        try:
            if postech:
                summary, used_model = postech.summarize(system_instruction, user_text)
            else:
                summary, used_model = summarize_with_fallback(api_key, models, system_instruction, user_text)
            fallback[key] = {
                "summary": summary,
                "source_hash": h,
                "model": used_model,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            summarized += 1
        except llm_client.CreditLimitReached as e:  # 키 한도 — 남은 교원은 다음에
            print(f"  멈춤: {e}")
            failed += 1
            break
        except Exception as e:  # noqa: BLE001 — 개별 실패는 기록하고 계속 진행
            print(f"  실패: {e}")
            failed += 1
        FALLBACK_FILE.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.5)

    print(f"\n완료: 요약 {summarized}건, 변경없어 건너뜀 {skipped}건, 실패 {failed}건")
    print("다음 단계: python3 scripts/build_wiki.py 를 실행해 위키에 반영하세요.")


if __name__ == "__main__":
    main()
