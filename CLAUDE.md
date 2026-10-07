# CLAUDE.md

이 저장소는 POSTECH R&D전략팀의 **도구 모음**입니다. 루트 `index.html`은 도구 허브(랜딩
페이지)이고, 실제 도구는 `tools/` 아래에 있습니다:

1. **`tools/doc-generator.html`** — 사업 안내 공문 생성기 (Gemini 연동 웹앱). 아래 내용과
   무관한 독립 기능입니다.
2. **`sources/`, `wiki/`, `scripts/`, `dashboard/`** — POSTECH 교원 R&D 위키("LLM Wiki")와
   그 검색·추천 대시보드. 이 문서는 **이 위키의 스키마**입니다.

이 파일이 있는 한, Claude Code로 이 저장소에서 위키 관련 작업을 할 때는 아래 규칙을 따르세요.

## 배경: LLM Wiki 패턴

이 위키는 [Andrej Karpathy의 "LLM Wiki" 패턴](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)을
따릅니다 — 매번 원본 문서를 처음부터 다시 검색하는 대신, LLM이 **영속적인 위키를
점진적으로 구축·유지**합니다. 새 자료가 들어오면 단순히 색인화하는 데 그치지 않고,
핵심 정보를 추출해 기존 위키에 통합합니다.

## 3계층 구조

```
sources/                       ← 1) 원본 자료 — 절대 수정하지 않음
  faculty_profiles_source.json    POSTECH R&D 실적 데이터베이스 (교원 298명, 연 1회 수동 업로드)
  homepage_crawl.json             교원 홈페이지 크롤링 결과 + AI 요약 (자동 생성, 연 2회 갱신)
  faculty_fallback_summary.json   홈페이지 AI 요약이 없는 교원의 위키 데이터 기반 폴백 요약 (자동 생성, 연 2회 갱신)

wiki/                           ← 2) 위키 — 두 종류의 페이지가 섞여 있음
  faculty/<개인번호>-<성명>.md    [기계 생성] 교원 1인당 1페이지 — 결정론적 추출
  index.md                        [기계 생성] 전체 평면 카탈로그
  faculty-index.md                [기계 생성] 가나다순 전체 목록
  research-areas.md               [기계 생성] 연구분야 키워드 인덱스
  national-strategic-tech.md      [기계 생성] 정부 12대 국가전략기술 분야별 인덱스
  researchers.json                [기계 생성] 위 내용을 압축한 JSON (dashboard/ 가 fetch로 읽음)
  home.md                         [LLM 큐레이션] 최상위 진입점, 학과 간 공통 흐름 종합
  domain/<학과>.moc.md            [LLM 큐레이션] 학과별 연구 클러스터 종합 (mermaid 포함)
  log.md                          [LLM 큐레이션] 시간순 append-only 변경 기록
  open-questions.md               [LLM 큐레이션] 데이터 모순·미해결 이슈

dashboard/                      ← 위키를 소비하는 애플리케이션 (사람이 손으로 고치는 코드)
  index.html                       연구자 대시보드: 통계·필터·검색 + POSTECH AI API 자연어 추천
  cors-proxy/                      genai.postech.ac.kr 브라우저 직접 호출 시 CORS 차단을
                                    우회하는 프록시(Cloudflare Worker / val.town)와 배포 가이드

scripts/                        ← 3) 파이프라인
  build_wiki.py                    sources/*.json → wiki/faculty/*.md, index.md,
                                    researchers.json 등 (결정론적)
  crawl_homepages.py               홈페이지+서브페이지 크롤링 → sources/homepage_crawl.json
  summarize_homepages.py           크롤링 원문을 LLM으로 요약 → homepage_crawl.json 의 summary 필드
  summarize_faculty_fallback.py    위 요약이 없는 교원만, 위키 자체 필드(관심분야·실적·논문 등)를
                                    LLM으로 요약 → faculty_fallback_summary.json
  llm_client.py                    요약 두 스크립트가 쓰는 POSTECH AI API(게이트웨이 Claude) 호출 —
                                    로컬 .env 에 POSTECH 키가 있으면 이것, 없으면 Gemini(GitHub Actions)

tools/
  doc-generator.html              사업 안내 공문 생성기 — 이 위키와 무관한 별도 도구

.github/workflows/refresh-wiki.yml  연 2회(3월 1일·9월 1일) 위 세 스크립트를 순서대로 실행해 main에 자동 커밋
```

`sources/homepage_crawl.json` 안의 `text`/`subpages`는 크롤링 원문 그대로(연락처만 지움 — 아래 '홈페이지 크롤링')지만, `summary`
필드는 그 원문을 LLM(Gemini)이 요약한 **파생 데이터**입니다 — 편의상 같은 파일에 저장하지만
"원본 그 자체"는 아니라는 점에 유의하세요 (교원 페이지에는 "AI 생성 요약"이라고 명시해 출처를
구분합니다). `sources/faculty_fallback_summary.json`도 마찬가지로 파생 데이터입니다 — 다만
입력이 홈페이지 원문이 아니라 `faculty_profiles_source.json`의 구조화 필드(관심분야·실적·
논문 등, 이미 `wiki/faculty/*.md`에 그대로 옮겨져 있는 값들)라, 교원 페이지에는 "AI 요약
(위키 데이터 기반)"이라고 표기해 홈페이지 기반 요약과 구분합니다.

이 문서(`CLAUDE.md`)가 3번째 레이어인 **스키마**입니다.

## 페이지 두 종류 — 소유권이 다름

**[기계 생성] `wiki/faculty/*.md`, `index.md`, `faculty-index.md`, `research-areas.md`,
`national-strategic-tech.md`, `researchers.json`**
`scripts/build_wiki.py` 가 소유합니다. **직접 손으로 고치지 마세요.** 원본(`sources/`)이
바뀌면 스크립트를 다시 실행하세요 (`python3 scripts/build_wiki.py`) — 몇 번을 실행해도
같은 결과가 나와야 합니다(idempotent). 정확도가 중요한 추출 데이터라 LLM이 임의로 요약·재구성하지
않고, 원본 필드를 그대로 옮기거나 결정론적으로만 재배열합니다 (No Hallucination — POSTECH
R&D전략팀의 공문 작성 원칙과 동일).

**[LLM 큐레이션] `home.md`, `domain/*.moc.md`, `log.md`, `open-questions.md`**
LLM(Claude)이 직접 쓰고 유지합니다. **스크립트가 건드리지 않으므로**, ingest/query/lint
작업 중 LLM이 직접 파일을 읽고 고칩니다. 여기서는 종합·요약·클러스터링이 기대되는 행위입니다
— 단, 무엇을 근거로 그렇게 판단했는지(예: `관심분야` 필드 클러스터링) 명시하고, 확신이 낮은
분류는 `open-questions.md`에 남기세요.

## 원칙

1. **원본 무결성**: `wiki/faculty/*.md` 는 `sources/` 의 원본 JSON 필드를 그대로 옮기거나
   결정론적으로 재구성한 것이어야 합니다. `domain/*.moc.md` 같은 큐레이션 페이지는 종합·해석이
   들어가지만, 원본에 없는 사실을 지어내지는 않습니다.
2. **재실행 가능**: 기계 생성 페이지는 `build_wiki.py` 를 몇 번 돌려도 동일해야 합니다.
3. **점진적 병합**: 새 원본(예: 재크롤링 결과)이 들어오면 `build_wiki.py` 를 다시 실행해
   기계 생성 페이지에 반영하고, 그 영향이 큐레이션 페이지(예: 학과 MOC의 통계)에도 미치면
   해당 MOC를 갱신하세요.
4. **고유 식별자**: 파일명은 `개인번호-성명.md`. 동명이인 4쌍이 있어 이름만으로는 구분 불가
   (`open-questions.md` 참고).
5. **상호참조**: 교원 페이지 ↔ 학과 MOC는 상호 링크됩니다. 큐레이션 페이지는 텍스트 링크(탐색
   골격)와 mermaid 다이어그램(조감도)을 함께 유지합니다 — 페이지를 추가/제거할 때 둘 다 갱신하세요.

## 운영 (Operations)

**Ingest (새 원본 반영)**: 두 원본은 갱신 방식이 다릅니다.
- `faculty_profiles_source.json`(실적 데이터베이스)은 **연 1회 사람이 새 파일을 받아 수동으로
  교체**합니다 — 자동화 대상이 아닙니다.
- `homepage_crawl.json`(홈페이지 크롤링 + AI 요약)은 `.github/workflows/refresh-wiki.yml`
  이 연 2회(3월 1일·9월 1일) 자동으로 갱신합니다.

어느 쪽이든 새 원본이 들어오면 1) `sources/` 에 반영하고 2) `python3 scripts/build_wiki.py`
로 기계 생성 페이지를 갱신한 뒤 3) 영향받는 `domain/*.moc.md` 를 다시 읽고 클러스터·통계가
여전히 맞는지 확인해 필요하면 고치고 4) `log.md` 에 항목을 추가합니다. 실적 데이터베이스가
수동 교체될 때는 학과 구성이나 인원이 크게 바뀔 수 있으니 `domain/*.moc.md` 재확인이 특히
중요합니다.

**Query (질의)**: `home.md` → 관련 `domain/*.moc.md` → 개별 `faculty/*.md` 순으로 훑는
것이 색인을 임베딩 검색 없이도 효율적으로 타는 방법입니다. 좋은 답변(비교, 분석, 발견한
연결고리)은 새 페이지로 위키에 다시 저장할 가치가 있으면 그렇게 하세요.

**Lint (점검)**: 주기적으로 다음을 확인하세요 — 학과 MOC의 통계가 최신 `sources/` 와
어긋나지 않는지, 인바운드 링크 없는 고아 페이지, 언급되지만 자체 클러스터가 없는 개념,
`open-questions.md` 에 남은 항목이 해소됐는지. 발견한 것은 `open-questions.md` 에 적고
해소되면 지운 뒤 `log.md` 에 기록합니다.

## 연구자 대시보드 (dashboard/)

`dashboard/index.html` 은 `wiki/researchers.json` 을 fetch로 읽어 통계·필터·검색과 함께,
자연어 질의("LG생활건강 사업 포트폴리오에 맞는 연구자")로 POSTECH AI API
([posicube-services/llm-agent-api](https://github.com/posicube-services/llm-agent-api))를
통해 연구자를 추천하는 기능을 제공합니다. `researchers.json` 은 다른 기계 생성 페이지와
같은 원칙(원본 무결성, idempotent)을 따르는 파생물이므로 직접 고치지 말고
`build_wiki.py` 를 다시 실행하세요.

`genai.postech.ac.kr` 는 브라우저 간(CORS) 요청을 막아 두어 대시보드가 직접 호출하면
`Failed to fetch` 가 납니다 — `dashboard/cors-proxy/` 에 준비된 프록시(Cloudflare Worker
또는 val.town)를 배포해 대시보드 설정의 엔드포인트를 그 프록시 주소로 바꿔야 합니다
(`dashboard/cors-proxy/README.md` 참고).

## 홈페이지 크롤링 관련 참고사항

이 저장소를 다루는 Claude Code 원격 환경은 네트워크 정책상 `postech.ac.kr` 등 외부 도메인에
접근할 수 없습니다 (egress 차단 — WebFetch 도구도 동일하게 막힘). 따라서 크롤링은
`scripts/crawl_homepages.py` 를 **인터넷 접근이 가능한 환경**에서 실행해
`sources/homepage_crawl.json` 을 만든 뒤, `scripts/build_wiki.py` 로 위키에 반영합니다.
(2026-10-07: POSTECH 교내 서버의 Claude Code 로컬 세션에서는 인터넷 · postech.ac.kr 에 접근할 수 있어 직접 돌렸다 —
`python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt` 뒤 `.venv/bin/python scripts/…`.
실행 기록 · 로그는 git 밖 `.local/` 에 둔다.)

**자동 정기 갱신**: `.github/workflows/refresh-wiki.yml` 이 연 2회(3월 1일·9월 1일)
① 크롤러(`--force`, 전체 재크롤링) ② `summarize_homepages.py`(Gemini API로 원문 요약,
`GEMINI_API_KEY` 시크릿이 설정된 경우에만) ③ `summarize_faculty_fallback.py`(위 요약이
없는 교원만, 같은 시크릿 조건으로) ④ `build_wiki.py` 순서로 실행해 결과를 `main`에
직접 커밋합니다 — GitHub Actions 러너는 일반 인터넷에 접근할 수 있어 이 작업을 이 세션 대신
해줍니다. Actions 탭에서 수동 실행(`workflow_dispatch`)도 가능합니다. (이 워크플로가 실제로
켜지려면 `main` 브랜치에 머지되어 있어야 합니다 — GitHub는 스케줄 트리거를 기본 브랜치의
워크플로 파일 기준으로만 실행합니다.) 로컬에서 급하게 한 번 더 돌리고 싶을 때는 아래처럼
수동으로 실행해도 됩니다.

- **서브페이지(탭) 크롤링**: 홈페이지 첫 화면 안의 같은 사이트 내부 링크 중 연구/논문/CV
  등 키워드로 우선순위를 매겨 교원 1명당 기본 15개까지 함께 가져옵니다. 구성원 명단·뉴스/공지
  링크는 제외합니다 (`SUBPAGE_EXCLUDE_KEYWORDS` — 2026-10-07 졸업생 · 개인정보 처리방침 · 로그인 · 사이트맵도). 링크 글자로
  걸러지지 않은 것은 받은 페이지 제목으로 한 번 더 거르고, 주소의 http/https · www 차이는 같은 페이지로 본다.
- **저장 상한 (2026-10-07 상향)**: 첫 화면 20,000자 · 탭 하나 12,000자 · 탭 15개. 예전 상한(4,000자 ·
  2,500자 · 8개)에서는 2026-08-28 크롤링의 홈페이지 256곳 중 204곳이 문장·논문 항목 중간에서 잘렸다
  (논문·출판 탭의 75%). 이제 상한을 넘으면 줄 끝에서 자르고 `chars_total`(원래 길이) · `truncated` 를
  남기며, 교원 페이지에 "원문 N자 중 앞 M자만 저장" 줄이 붙는다.
- **연락처 지우기 (2026-10-07)**: 이 저장소는 공개라, 크롤러가 원문을 저장하기 전에 이메일 · 전화번호를
  `[이메일]` · `[전화]` 로 바꾼다(연구실 홈페이지에 학생 연락처가 섞여 있다). 교원 본인 이메일은 실적 DB 에서
  '기본 정보'에 따로 나온다. 전화는 지역번호(0 · +82)로 시작할 때만 지운다 — 논문 목록 숫자를 잘못 지우지 않게.
- **다시 받기 실패 · 넘겨주기**: 다시 받다가 실패하면 예전 글을 지우지 않고 `error` · `last_attempt_at` 만 더한다
  (교원 페이지에 "다시 받기에 실패해 … 내용을 보여 줍니다"). AI 요약 칸도 크롤링 때 지우지 않는다 — 요약 스크립트가
  원문 해시를 비교해 바뀐 교원만 다시 요약한다. 글자 없는 첫 화면이 meta refresh · `location.replace` 로
  다른 주소로 넘기면 한 번 따라간다. 중간에 멈춘 전체 갱신은 `--force --refetch-before <시작 시각>` 으로 이어 돈다.
- **학과/그룹 공통 포털 제외**: 여러 교원이 정확히 같은 URL을 홈페이지로 등록한 경우
  (`PORTAL_SHARE_THRESHOLD = 2` 이상 공유) 개인 페이지가 아니라고 보고 크롤링하지 않습니다.
- **AI 요약 — 제공자 (2026-10-07)**: 로컬 실행은 POSTECH AI API(게이트웨이 `claude-opus-5`, `scripts/llm_client.py`)를
  쓴다. 키는 저장소 루트 `.env`(git 제외)에 `SUMMARY_PROVIDER=postech` · `POSTECH_API_BASE` · `POSTECH_API_KEY` ·
  `POSTECH_CREDIT_STOP`(이번 주 사용량이 여기 닿으면 멈춤). 게이트웨이는 `temperature` 를 받지 않는다(400).
  GitHub Actions 정기 갱신은 POSTECH 키가 없으니 예전처럼 Gemini 다. 요약에 보내는 원문은 16,000자를 칸(첫 화면 ·
  탭)마다 고르게 나누고 논문·출판 탭을 마지막에 둔다 — 예전에는 앞 16,000자만 잘라 긴 탭이 연구 소개를 밀어냈다.
- **AI 요약 (Gemini)**: `scripts/summarize_homepages.py` 가 크롤링 원문(첫 화면 + 서브페이지)을
  교원 1인당 3~5문장으로 요약해 `homepage_crawl.json`의 `summary` 필드에 저장합니다.
  `index.html`(RFP 공문 생성기)과 동일하게 Gemini API를 REST로 직접 호출합니다(동적 모델
  탐색 + 폴백 후보 목록). **저장소 Settings → Secrets and variables → Actions 에서
  `GEMINI_API_KEY` 시크릿을 등록해야 이 단계가 실행됩니다** — 없으면 이 단계는 조용히
  건너뜁니다(크롤링·위키 재생성은 정상 진행). 원문 안에 다른 교원 이름이 섞여 있을 때
  잘못 귀속시키지 않도록 프롬프트에 가드레일을 넣었지만, 100% 보장되진 않으니 가끔
  스팟체크하세요 (`open-questions.md` 참고).
- **위키 데이터 기반 폴백 요약 (Gemini)**: 홈페이지가 없거나, 학과 공통 포털이라 크롤링
  대상에서 제외됐거나, 크롤링에 실패해 위 홈페이지 AI 요약이 비는 교원(약 42명)에 한해,
  `scripts/summarize_faculty_fallback.py` 가 홈페이지 원문 대신 `wiki/faculty/*.md`에 이미
  반영된 자기 자신의 필드(관심분야·실적건수·주요성과·대표연구 등)만으로 2~3문장 요약을 만들어
  `faculty_fallback_summary.json`에 저장합니다. `build_wiki.py`가 홈페이지 AI 요약이 없는
  교원 페이지에 한해 이 요약을 "AI 요약 (위키 데이터 기반)"으로 표시합니다(둘 다 있을 수는
  없음 — 홈페이지 요약이 우선).

자세한 크롤러 옵션은 `scripts/crawl_homepages.py` 의 docstring을, 요약 옵션은
`scripts/summarize_homepages.py`·`scripts/summarize_faculty_fallback.py` 의 docstring을
참고하세요.
