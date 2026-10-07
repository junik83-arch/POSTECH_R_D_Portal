#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_client.py — 요약 스크립트가 함께 쓰는 POSTECH AI API(게이트웨이) 호출 (2026-10-07 추가)

summarize_homepages.py · summarize_faculty_fallback.py 는 원래 Gemini API 만 불렀다. 대시보드가 이미
POSTECH AI API 를 쓰고 있고 학교 키가 있으므로, 로컬에서 요약할 때는 POSTECH 게이트웨이의 Claude 를 쓸 수 있게 했다.
GitHub Actions 정기 갱신(.github/workflows/refresh-wiki.yml)에는 GEMINI_API_KEY 시크릿만 있으므로 그쪽은 예전처럼 Gemini 다.

어느 쪽을 쓰나 (provider())
    SUMMARY_PROVIDER=postech | gemini 로 정한다. 없으면 POSTECH_API_KEY · POSTECH_API_BASE 가 있을 때 postech,
    아니면 gemini. 키는 저장소 루트 .env(git 제외 — .gitignore) 나 환경변수에 둔다. 키 값은 어디에도 찍지 않는다.

    .env 예)
        SUMMARY_PROVIDER=postech
        POSTECH_API_BASE=<발급받은 게이트웨이 주소 — …/a27 까지 또는 …/anthropic/messages 까지>
        POSTECH_API_KEY=<키>
        POSTECH_CREDIT_STOP=70000      # (선택) 키의 이번 주 사용량이 이 값에 닿으면 멈춘다

게이트웨이 호출 모양 (RnDstrategyReport/lib/llm.py 와 같은 규칙을 requests 로만 옮겼다 — SDK 를 더하지 않는다)
    POST {base}/anthropic/messages, 헤더 x-api-key · anthropic-version, 본문은 Anthropic Messages 형식.
    모델은 claude-opus-5(POSTECH_MODEL 로 바꿀 수 있다). 스트리밍은 쓰지 않는다(게이트웨이 스트림에 event 줄이 없다).
    응답 헤더 x-role-ratelimit-credit-usage / -limit 로 이번 주 사용량을 읽고, 429(reason ROLE)면 한도라 바로 멈춘다.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DEFAULT_MODEL = "claude-opus-5"


class CreditLimitReached(RuntimeError):
    """키의 주간 한도(게이트웨이 429 · ROLE) 또는 POSTECH_CREDIT_STOP 에 닿았다 — 남은 건은 돌리지 않는다."""


def load_env(path: Path = ENV_FILE) -> None:
    """.env 의 KEY=VALUE 줄을 환경변수로 올린다. 이미 있는 값(명령 줄 · Actions 시크릿)은 덮지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def provider() -> str:
    """postech 또는 gemini (위 docstring 의 규칙)."""
    load_env()
    name = os.environ.get("SUMMARY_PROVIDER", "").strip().lower()
    if not name:
        name = "postech" if os.environ.get("POSTECH_API_KEY") and os.environ.get("POSTECH_API_BASE") else "gemini"
    if name not in ("postech", "gemini"):
        raise SystemExit(f"SUMMARY_PROVIDER='{name}' 는 없습니다 — postech 또는 gemini")
    return name


def _messages_url(base: str) -> str:
    """발급 주소가 …/a27 이든 …/a27/anthropic/messages 든 같은 끝 주소로 맞춘다."""
    root = base.rstrip("/")
    for tail in ("/anthropic/messages", "/anthropic"):
        if root.endswith(tail):
            root = root[: -len(tail)]
    return root + "/anthropic/messages"


class PostechClient:
    """POSTECH 게이트웨이 Claude. summarize(system, user) → (요약, 모델 이름표)."""

    def __init__(self) -> None:
        load_env()
        try:
            self.key = os.environ["POSTECH_API_KEY"]
            self.url = _messages_url(os.environ["POSTECH_API_BASE"])
        except KeyError as e:
            raise SystemExit(f"{e.args[0]} 가 없습니다 — 저장소 루트 .env 에 두세요(llm_client.py 머리 설명).") from None
        self.model = os.environ.get("POSTECH_MODEL", DEFAULT_MODEL)
        self.stop_at = float(os.environ.get("POSTECH_CREDIT_STOP", "0") or 0)
        self.usage: float | None = None   # 게이트웨이가 알려 준 이 키의 이번 주 사용량
        self.limit: float | None = None
        self.calls = 0

    @property
    def label(self) -> str:
        return f"postech:{self.model}"

    def _read_limits(self, headers) -> None:
        for attr, name in (("usage", "x-role-ratelimit-credit-usage"), ("limit", "x-role-ratelimit-credit-limit")):
            try:
                setattr(self, attr, float(headers.get(name)))
            except (TypeError, ValueError):
                pass

    def summarize(self, system: str, user: str, max_tokens: int = 1000, timeout: float = 120) -> tuple[str, str]:
        if self.stop_at and self.usage is not None and self.usage >= self.stop_at:
            raise CreditLimitReached(f"이번 주 사용량 {self.usage:,.0f} — POSTECH_CREDIT_STOP {self.stop_at:,.0f} 에 닿음")
        # temperature 는 보내지 않는다 — claude-opus-5 는 "`temperature` is deprecated for this model" 로 400 을 준다(2026-10-07 실측)
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last: Exception | None = None
        for attempt in range(4):
            try:
                resp = requests.post(self.url, headers=headers, json=body, timeout=timeout)
            except requests.RequestException as e:  # 연결 · 시간 초과 — 잠깐 쉬고 다시
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            self._read_limits(resp.headers)
            if resp.status_code == 429:
                if (resp.headers.get("x-ratelimit-reason") or "").upper() == "ROLE":
                    raise CreditLimitReached(f"게이트웨이 주간 한도 (사용 {self.usage} / 한도 {self.limit})")
                last = RuntimeError("429 — 잠시 뒤 다시")
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code >= 500:
                last = RuntimeError(f"게이트웨이 {resp.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            self.calls += 1
            if data.get("stop_reason") == "max_tokens":
                raise RuntimeError(f"요약이 max_tokens={max_tokens} 에서 잘렸다")
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
            if not text:
                raise RuntimeError(f"빈 응답 (stop_reason={data.get('stop_reason')})")
            return text, self.label
        raise last or RuntimeError("게이트웨이 호출 실패")

    def status(self) -> str:
        """진행 줄에 붙일 '이번 주 사용량' (키 값은 찍지 않는다)."""
        if self.usage is None:
            return ""
        return f" · 키 이번 주 {self.usage:,.0f}" + (f"/{self.limit:,.0f}" if self.limit else "")
