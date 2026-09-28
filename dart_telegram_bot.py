#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DART(전자공시) 신규 공시 -> Claude 요약/호재악재 판단 -> Telegram 알림 봇

동작 개요
---------
1. DART "공시검색" API(list.json) 로 오늘(또는 최근 LOOKBACK_DAYS 일) 전체 시장에 올라온
   공시 목록을 한 번에 가져온 뒤, WATCH_LIST 에 등록된 관심 기업(corp_code)에 해당하는
   것만 걸러낸다.
2. 제목에 EXCLUDE_KEYWORDS 가 들어간 공시(ELS 발행, 타인에대한담보제공, 대량보유보고 등)는 건너뛴다.
3. state.json 에 저장된 "이미 알림을 보낸 rcept_no 목록"과 비교해서 새 공시만 골라낸다.
4. 새 공시가 있으면 DART "공시서류원본파일" API(document.xml) 로 원문을 내려받아 텍스트만 추출한다.
5. Claude API 에게 "핵심 한 줄 + 주요 수치(금액/상대방/기간/비율 등) + 상세 요약 + 호재/악재
   판단 + 시사점"을 JSON 형식으로 요청한다.
6. 결과를 카드 형태로 정리해서 Telegram 봇으로 전송한다.
7. 처리한 공시의 rcept_no 를 state.json 에 추가로 저장해서 중복 알림을 막는다.

이 스크립트는 "한 번 실행하고 끝나는" 배치형 스크립트입니다.
24시간 감시는 GitHub Actions 스케줄(cron)로 이 스크립트를 반복 실행하는 방식으로 구현합니다.
(자세한 설정 방법은 README.md 참고)
"""

import os
import re
import io
import sys
import json
import time
import zipfile
import argparse
import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    anthropic = None


# ---------------------------------------------------------------------------
# 설정값 로드 (모두 환경변수에서 읽습니다. GitHub Actions 에서는 Secrets 로 주입됩니다.)
# ---------------------------------------------------------------------------

DART_API_KEY = os.environ.get("DART_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# 여러 명에게 동시에 보내고 싶으면 쉼표(,)로 구분해서 여러 chat_id 를 적으면 됩니다.
TELEGRAM_CHAT_IDS = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]

# 관심 기업 목록. JSON 문자열 형태의 환경변수로 받습니다.
# 예: '[{"name": "삼성전자", "corp_code": "00126380"}, {"name": "카카오", "corp_code": "00918444"}]'
WATCH_LIST_RAW = os.environ.get("WATCH_LIST", "[]")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# 며칠 전 공시까지 조회할지 (당일 실행이 실패했을 때를 대비해 여유를 둠).
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))

# 공시 "제목"에 이 키워드들 중 하나라도 포함되면 알림을 보내지 않고 건너뜁니다.
# (띄어쓰기는 무시하고 비교합니다. 예: "타인에대한담보제공" 은 "타인에 대한 담보제공결정" 도 걸러냄)
DEFAULT_EXCLUDE_KEYWORDS = (
    "증권발행실적보고서,일괄신고추가서류,일괄신고서,효력발생안내,"
    "파생결합증권,파생결합사채,ELB,ELS,DLS,DLB,"
    "IR개최,IR설명회,기업설명회,"
    "타인에대한담보제공,대량보유상황보고서"
)
EXCLUDE_KEYWORDS = [
    re.sub(r"\s+", "", kw) for kw in os.environ.get("EXCLUDE_KEYWORDS", DEFAULT_EXCLUDE_KEYWORDS).split(",")
    if kw.strip()
]

# 공시 원문 중 Claude 에게 보낼 최대 글자 수.
MAX_DOC_CHARS = int(os.environ.get("MAX_DOC_CHARS", "12000"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

KST = datetime.timezone(datetime.timedelta(hours=9))


def log(msg: str) -> None:
    ts = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_required_env() -> None:
    missing = []
    for name in ["DART_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        if not os.environ.get(name, "").strip():
            missing.append(name)
    if missing:
        raise SystemExit(
            "다음 환경변수가 설정되지 않았습니다: " + ", ".join(missing) +
            "\n.env 파일(로컬) 또는 GitHub Secrets(Actions) 를 확인하세요."
        )


def load_watchlist() -> list:
    try:
        items = json.loads(WATCH_LIST_RAW)
    except json.JSONDecodeError as e:
        raise SystemExit(f"WATCH_LIST 환경변수가 올바른 JSON이 아닙니다: {e}\n입력값: {WATCH_LIST_RAW}")
    if not isinstance(items, list) or not items:
        raise SystemExit(
            "WATCH_LIST 가 비어 있습니다. 예시:\n"
            '[{"name": "삼성전자", "corp_code": "00126380"}]\n'
            "corp_code 는 find_corp_code.py 로 찾을 수 있습니다."
        )
    for item in items:
        if "corp_code" not in item or "name" not in item:
            raise SystemExit(f"WATCH_LIST 항목에는 name, corp_code 가 모두 필요합니다: {item}")
    return items


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"경고: {STATE_FILE} 파일이 손상되어 새로 시작합니다.")
    return {"seen_rcept_no": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_excluded(report_nm: str) -> bool:
    compact = re.sub(r"\s+", "", report_nm or "")
    return any(kw in compact for kw in EXCLUDE_KEYWORDS)


def fetch_all_filings(bgn_de: str, end_de: str) -> list:
    """지정 기간 동안 '전체 시장'에 올라온 공시 목록을 모두 가져온다 (페이지네이션 포함)."""
    results = []
    page_no = 1
    while True:
        params = {
            "crtfc_key": DART_API_KEY,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": str(page_no),
            "page_count": "100",
        }
        resp = requests.get(DART_LIST_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status == "013":
            break
        if status != "000":
            log(f"DART API 오류: status={status}, message={data.get('message')}")
            break

        results.extend(data.get("list", []))

        total_page = int(data.get("total_page", 1))
        if page_no >= total_page:
            break
        page_no += 1
        time.sleep(0.2)

    return results


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


def _decode(raw_bytes: bytes) -> str:
    """DART 원문은 UTF-8 인 경우도, EUC-KR(CP949) 인 경우도 있다.
    예전 코드는 무조건 UTF-8 로 읽고 에러를 무시해서, EUC-KR 문서면 한글이 통째로 사라져
    Claude 가 거의 빈 문서를 받는 문제가 있었다."""
    head = raw_bytes[:200].decode("ascii", errors="ignore").lower()
    m = re.search(r'encoding=["\']?([a-z0-9_\-]+)', head)
    candidates = []
    if m:
        candidates.append(m.group(1))
    candidates += ["utf-8", "cp949"]
    for enc in candidates:
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="ignore")


def _strip_markup(raw_bytes: bytes) -> str:
    """DART 원문(XML/HTML)에서 태그를 걷어내고 읽을 수 있는 텍스트만 남긴다.
    DART XML 은 표 셀에 <TD> 뿐 아니라 <TE>, <TU>, <TH> 태그를 쓰므로 모두 칸 구분으로 처리한다."""
    text = _decode(raw_bytes)
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(td|te|tu|th)>", " | ", text)
    text = re.sub(r"(?i)</(tr|p|title|table|table-group|section-\d|cover-title)>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&cr;", "\n")]:
        text = text.replace(a, b)
    text = _WS_RE.sub(" ", text)
    # 표에서 비어 있는 칸 때문에 생기는 "|  |  |" 같은 잡음 정리
    text = re.sub(r"(\|[ ]*){2,}", "| ", text)
    text = "\n".join(line.strip(" |") for line in text.split("\n"))
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def fetch_document_text(rcept_no: str) -> str:
    """공시 원문 파일(zip 안의 xml/html)을 내려받아 텍스트로 변환한다. 실패하면 빈 문자열."""
    params = {"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}
    try:
        resp = requests.get(DART_DOCUMENT_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"공시 원문 다운로드 실패 (rcept_no={rcept_no}): {e}")
        return ""

    content = resp.content
    if not content.startswith(b"PK"):
        try:
            err = resp.json()
            log(f"공시 원문 API 오류 (rcept_no={rcept_no}): {err}")
        except ValueError:
            log(f"공시 원문 응답을 해석할 수 없습니다 (rcept_no={rcept_no}).")
        return ""

    texts = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # 본문(파일명이 rcept_no 와 같은 것)을 먼저, 첨부서류를 나중에 읽는다.
            names = sorted(
                (n for n in zf.namelist() if n.lower().endswith((".xml", ".html", ".htm"))),
                key=lambda n: (not n.startswith(rcept_no + "."), n),
            )
            for name in names:
                texts.append(_strip_markup(zf.read(name)))
    except zipfile.BadZipFile:
        log(f"공시 원문 zip 파일이 손상되었습니다 (rcept_no={rcept_no}).")
        return ""

    full_text = "\n\n".join(t for t in texts if t)
    return full_text[:MAX_DOC_CHARS]


def build_prompt(corp_name: str, report_nm: str, rcept_dt: str, document_text: str) -> str:
    doc_part = document_text if document_text else "(원문을 가져오지 못했습니다. 공시 제목만으로 판단해 주세요.)"
    return f"""당신은 한국 주식시장에 정통한 애널리스트입니다.
아래는 전자공시시스템(DART)에 새로 올라온 공시 내용입니다.

기업명: {corp_name}
공시 제목: {report_nm}
접수일자: {rcept_dt}

--- 공시 원문(일부) ---
{doc_part}
--- 원문 끝 ---

투자자가 DART 원문을 열어보지 않고 텔레그램 메시지만 읽어도 "무슨 공시이고, 숫자가 얼마이고,
주가에 어떤 의미인지" 파악할 수 있도록 정리하세요.

다음 형식의 JSON 으로만 답변하세요. 다른 설명이나 코드블록 표시(```) 없이 순수 JSON 객체만 출력합니다.

{{
  "headline": "공시 핵심을 한 문장으로 (누가/무엇을/얼마나)",
  "key_facts": [
    {{"label": "항목명", "value": "값"}}
  ],
  "summary": ["상세 요약 1", "상세 요약 2", "상세 요약 3"],
  "sentiment": "호재" 또는 "악재" 또는 "중립" 중 하나,
  "reason": "그렇게 판단한 이유를 1~2문장으로",
  "implications": ["투자 시사점 1"]
}}

작성 규칙:
- key_facts 는 원문에 실제로 적힌 핵심 수치/조건을 4~8개 뽑습니다. 공시 종류별 예시:
  · 단일판매·공급계약: 계약상대방, 계약금액, 최근 매출액 대비 비율, 계약기간, 계약내용
  · 유상증자/CB/BW/EB: 발행규모, 발행가(전환가/행사가), 할인율, 신주 수 및 기존 주식 대비 비율, 배정방식/대상자, 납입일·상장예정일, 자금용도
  · 자기주식 취득/처분/소각: 주식 수, 금액, 발행주식 대비 비율, 기간, 방법
  · 실적(잠정)/매출액 변동: 매출액·영업이익·순이익과 전년 동기 대비 증감률
  · 배당: 주당 배당금, 시가배당률, 배당 기준일, 지급 예정일
  · 타법인 주식 취득/양수도, 합병·분할: 대상, 금액, 자기자본 대비 비율, 목적, 일정
  · 최대주주 변경, 임원 변동, 소송 등: 누가→누구, 지분율, 청구금액 등
  원문에 없는 값은 지어내지 말고 빼세요. 금액은 "1,234억원" 처럼 억/조 단위로 읽기 쉽게 바꾸세요.
- summary 는 3~5개, 각 항목은 한두 문장. 숫자와 날짜를 최대한 구체적으로 포함하세요.
- implications 는 1~2개. 원문에 없는 해석이므로 "~로 보입니다" 처럼 추정임이 드러나게 쓰세요.
- 공시가 투자심리에 미칠 영향을 기준으로 sentiment 를 판단하세요. 방향성이 없으면 "중립".
- 원문이 부족해 판단이 어려우면 sentiment 를 "중립"으로 하고 reason 에 그 사실을 밝히세요.
"""


def summarize_with_claude(client, corp_name: str, report_nm: str, rcept_dt: str, document_text: str) -> dict:
    prompt = build_prompt(corp_name, report_nm, rcept_dt, document_text)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text").strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log(f"Claude 응답을 JSON으로 해석하지 못했습니다. 원본 응답:\n{raw}")
        data = {
            "summary": [raw[:300]] if raw else ["요약 생성에 실패했습니다."],
            "sentiment": "중립",
            "reason": "모델 응답 형식 오류로 자동 판단을 하지 못했습니다.",
        }

    data.setdefault("headline", "")
    data.setdefault("key_facts", [])
    data.setdefault("summary", ["요약 없음"])
    data.setdefault("sentiment", "중립")
    data.setdefault("reason", "")
    data.setdefault("implications", [])
    return data


SENTIMENT_EMOJI = {"호재": "🟢", "악재": "🔴", "중립": "⚪"}
SECTION_DIVIDER = "──────────"


def format_telegram_message(corp_name: str, report_nm: str, rcept_dt: str, rcept_no: str, analysis: dict) -> str:
    def esc(s) -> str:
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    sentiment = analysis.get("sentiment", "중립")
    emoji = SENTIMENT_EMOJI.get(sentiment, "⚪")
    viewer_url = DART_VIEWER_URL.format(rcept_no=rcept_no)

    if len(rcept_dt) == 8 and rcept_dt.isdigit():
        rcept_dt_fmt = f"{rcept_dt[:4]}.{rcept_dt[4:6]}.{rcept_dt[6:]}"
    else:
        rcept_dt_fmt = rcept_dt

    parts = [
        f"{emoji} <b>[공시/{esc(sentiment)}]</b> {esc(corp_name)}",
        f"<b>{esc(report_nm)}</b>",
        f"접수일자: {esc(rcept_dt_fmt)}",
        SECTION_DIVIDER,
    ]

    headline = analysis.get("headline")
    if headline:
        parts.append(f"📌 {esc(headline)}")
        parts.append(SECTION_DIVIDER)

    facts = []
    for f in analysis.get("key_facts") or []:
        if isinstance(f, dict) and f.get("value"):
            facts.append(f"▪ {esc(f.get('label', ''))}: <b>{esc(f.get('value'))}</b>")
        elif isinstance(f, str) and f:
            facts.append(f"▪ {esc(f)}")
    if facts:
        parts.append("<b>주요 내용</b>\n" + "\n".join(facts))
        parts.append(SECTION_DIVIDER)

    summary = [s for s in (analysis.get("summary") or []) if s]
    if summary:
        parts.append("<b>상세 요약</b>\n" + "\n".join(f"• {esc(s)}" for s in summary))
        parts.append(SECTION_DIVIDER)

    reason = analysis.get("reason")
    if reason:
        parts.append(f"<b>판단 근거</b>\n{esc(reason)}")

    implications = [s for s in (analysis.get("implications") or []) if s]
    if implications:
        parts.append("<b>시사점</b> <i>(AI 추정, 참고용)</i>\n" + "\n".join(f"• {esc(s)}" for s in implications))

    parts.append(SECTION_DIVIDER)
    parts.append(f'<a href="{viewer_url}">DART 원문 보기</a>')
    return "\n".join(parts)


def send_telegram_message(text: str) -> None:
    url = TELEGRAM_SEND_URL.format(token=TELEGRAM_BOT_TOKEN)
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(생략됨)"

    last_error = None
    success_count = 0
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, data=payload, timeout=20)
            if resp.status_code != 200:
                log(f"텔레그램 전송 실패 (chat_id={chat_id}): {resp.status_code} {resp.text}")
            resp.raise_for_status()
            success_count += 1
        except requests.RequestException as e:
            log(f"텔레그램 전송 실패 (chat_id={chat_id}): {e}")
            last_error = e

    if success_count == 0 and last_error is not None:
        raise last_error


def run_test_message() -> None:
    check_required_env()
    send_telegram_message("✅ DART 공시 알림 봇 테스트 메시지입니다. 텔레그램 연동이 정상적으로 작동합니다!")
    log("테스트 메시지를 전송했습니다. 텔레그램을 확인하세요.")


def main() -> None:
    parser = argparse.ArgumentParser(description="DART 공시 -> Claude 요약 -> Telegram 알림 봇")
    parser.add_argument("--test", action="store_true", help="텔레그램 연동 테스트 메시지만 보내고 종료")
    args = parser.parse_args()

    if args.test:
        run_test_message()
        return

    check_required_env()
    if anthropic is None:
        raise SystemExit("anthropic 패키지가 설치되어 있지 않습니다. `pip install -r requirements.txt` 를 실행하세요.")

    watchlist = load_watchlist()
    watch_map = {item["corp_code"]: item["name"] for item in watchlist}
    log(f"관심 기업 {len(watch_map)}개 감시 중.")

    state = load_state()
    seen = set(state.get("seen_rcept_no", []))

    # GitHub Actions 서버는 UTC 라서, 한국시간 기준 날짜로 계산한다.
    today_kst = datetime.datetime.now(KST).date()
    end_de = today_kst.strftime("%Y%m%d")
    bgn_de = (today_kst - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    log(f"전체 시장 공시 조회 중... ({bgn_de} ~ {end_de})")
    try:
        all_filings = fetch_all_filings(bgn_de, end_de)
    except requests.RequestException as e:
        raise SystemExit(f"DART API 호출 실패: {e}")

    filings = [f for f in all_filings if f.get("corp_code") in watch_map]
    log(f"전체 {len(all_filings)}건 중 관심 기업 공시 {len(filings)}건 발견.")

    before_exclude_count = len(filings)
    filings = [f for f in filings if not is_excluded(f.get("report_nm", ""))]
    excluded_count = before_exclude_count - len(filings)
    if excluded_count:
        log(f"제외 키워드에 걸려 {excluded_count}건 필터링됨 (알림 대상 {len(filings)}건 남음).")

    filings.sort(key=lambda f: (f.get("rcept_dt", ""), f.get("rcept_no", "")))

    new_count = 0
    for filing in filings:
        rcept_no = filing.get("rcept_no")
        if not rcept_no or rcept_no in seen:
            continue

        corp_name = watch_map.get(filing.get("corp_code"), filing.get("corp_name", "(알 수 없음)"))
        report_nm = (filing.get("report_nm") or "(제목 없음)").strip()
        rcept_dt = filing.get("rcept_dt", "")
        log(f"신규 공시 발견: {corp_name} - {report_nm} ({rcept_no})")

        document_text = fetch_document_text(rcept_no)
        log(f"  원문 텍스트 {len(document_text)}자 추출")

        try:
            analysis = summarize_with_claude(client, corp_name, report_nm, rcept_dt, document_text)
        except Exception as e:
            log(f"Claude 요약 실패 ({rcept_no}): {e}")
            analysis = {
                "summary": [report_nm],
                "sentiment": "중립",
                "reason": "요약 생성 중 오류가 발생해 제목만 전달합니다.",
            }

        message = format_telegram_message(corp_name, report_nm, rcept_dt, rcept_no, analysis)
        try:
            send_telegram_message(message)
        except requests.RequestException as e:
            log(f"텔레그램 전송 실패 ({rcept_no}): {e} - 다음 실행에서 재시도합니다.")
            continue

        seen.add(rcept_no)
        new_count += 1
        state["seen_rcept_no"] = sorted(seen)
        save_state(state)
        time.sleep(1)

    log(f"완료. 신규 알림 {new_count}건.")


if __name__ == "__main__":
    main()
