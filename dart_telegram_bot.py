#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DART(전자공시) 신규 공시 -> Claude 요약/호재악재 판단 -> Telegram 알림 봇

동작 개요
---------
1. DART "공시검색" API(list.json) 로 오늘(또는 최근 LOOKBACK_DAYS 일) 전체 시장에 올라온
   공시 목록을 한 번에 가져온 뒤, WATCH_LIST 에 등록된 관심 기업(corp_code)에 해당하는
   것만 걸러낸다. (기업마다 따로 조회하지 않아 관심 기업이 수백 개여도 API 호출 횟수가
   거의 늘어나지 않는다.)
2. state.json 에 저장된 "이미 알림을 보낸 rcept_no 목록"과 비교해서 새 공시만 골라낸다.
3. 새 공시가 있으면 DART "공시서류원본파일" API(document.xml) 로 원문을 내려받아 텍스트만 추출한다.
4. Claude API 에게 "3줄 요약 + 호재/악재 판단 + 이유"를 JSON 형식으로 요청한다.
5. 결과를 보기 좋게 정리해서 Telegram 봇으로 전송한다.
6. 처리한 공시의 rcept_no 를 state.json 에 추가로 저장해서 중복 알림을 막는다.

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
    # 로컬(내 컴퓨터)에서 테스트할 때 .env 파일을 읽어오기 위한 라이브러리.
    # GitHub Actions 에서는 secrets 가 바로 환경변수로 들어오므로 없어도 동작합니다.
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

# 관심 기업 목록. JSON 문자열 형태의 환경변수로 받습니다.
# 예: '[{"name": "삼성전자", "corp_code": "00126380"}, {"name": "카카오", "corp_code": "00918444"}]'
# corp_code 찾는 방법은 find_corp_code.py 참고.
WATCH_LIST_RAW = os.environ.get("WATCH_LIST", "[]")

# Claude 모델. 기본값은 균형 잡힌 sonnet 모델. 비용을 더 아끼고 싶다면
# claude-haiku-4-5 로 바꿔도 됩니다 (품질은 약간 낮아질 수 있음).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# 며칠 전 공시까지 조회할지 (당일 실행이 실패했을 때를 대비해 여유를 둠).
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))

# 공시 "제목"에 이 키워드들 중 하나라도 포함되면 알림을 보내지 않고 건너뜁니다.
# 증권사가 거의 매일 형식적으로 내는 ELB/ELS 등 파생결합증권 발행 관련 공시처럼,
# 투자 판단에 의미 없는 반복성 공시를 걸러내기 위한 용도입니다.
# 쉼표(,)로 구분해서 원하는 키워드를 자유롭게 추가/삭제할 수 있습니다.
DEFAULT_EXCLUDE_KEYWORDS = (
    "증권발행실적보고서,일괄신고추가서류,일괄신고서,효력발생안내,"
    "파생결합증권,파생결합사채,ELB,ELS,DLS,DLB"
)
EXCLUDE_KEYWORDS = [
    kw.strip() for kw in os.environ.get("EXCLUDE_KEYWORDS", DEFAULT_EXCLUDE_KEYWORDS).split(",")
    if kw.strip()
]

# 공시 원문 중 Claude 에게 보낼 최대 글자 수 (너무 길면 비용/속도 문제가 생기므로 자름).
MAX_DOC_CHARS = int(os.environ.get("MAX_DOC_CHARS", "8000"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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


def fetch_all_filings(bgn_de: str, end_de: str) -> list:
    """지정 기간 동안 '전체 시장'에 올라온 공시 목록을 모두 가져온다 (페이지네이션 포함).

    관심 기업이 많을 때(예: 수십~수백 개) 기업마다 따로 API를 호출하면 호출 횟수가
    기업 수만큼 늘어나서 비효율적이다. 대신 corp_code 없이 전체 시장 공시를 한 번에
    조회한 뒤, 우리가 관심 있는 기업만 파이썬에서 걸러내는 방식이 훨씬 효율적이다.
    (단, DART 정책상 corp_code 없이 조회할 때는 조회 기간이 최대 3개월로 제한된다.
    이 봇은 기본 2일치만 조회하므로 문제되지 않는다.)
    """
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
            # "조회된 데이터가 없습니다" -> 정상, 그냥 해당 기간에 공시가 없는 것
            break
        if status != "000":
            log(f"DART API 오류: status={status}, message={data.get('message')}")
            break

        results.extend(data.get("list", []))

        total_page = int(data.get("total_page", 1))
        if page_no >= total_page:
            break
        page_no += 1
        time.sleep(0.2)  # DART API 를 너무 빠르게 연타하지 않도록 살짝 대기

    return results


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


def _strip_markup(raw_bytes: bytes) -> str:
    """DART 원문(XML/HTML)에서 태그를 걷어내고 읽을 수 있는 텍스트만 남긴다."""
    text = raw_bytes.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = text.replace("<BR>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    text = text.replace("</TD>", " ").replace("</td>", " ")
    text = text.replace("</TR>", "\n").replace("</tr>", "\n")
    text = text.replace("</P>", "\n").replace("</p>", "\n")
    text = _TAG_RE.sub(" ", text)
    # HTML/XML 엔티티 몇 가지만 간단히 치환
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
        text = text.replace(a, b)
    text = _WS_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def fetch_document_text(rcept_no: str) -> str:
    """공시 원문 파일(zip 안의 xml/html)을 내려받아 텍스트로 변환한다.
    실패하면 빈 문자열을 반환한다 (요약은 제목만으로도 시도한다)."""
    params = {"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}
    try:
        resp = requests.get(DART_DOCUMENT_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"공시 원문 다운로드 실패 (rcept_no={rcept_no}): {e}")
        return ""

    content = resp.content
    if not content.startswith(b"PK"):
        # zip 이 아니면 에러 응답(JSON)일 가능성이 높음
        try:
            err = resp.json()
            log(f"공시 원문 API 오류 (rcept_no={rcept_no}): {err}")
        except ValueError:
            log(f"공시 원문 응답을 해석할 수 없습니다 (rcept_no={rcept_no}).")
        return ""

    texts = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith((".xml", ".html", ".htm")):
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

다음 형식의 JSON 으로만 답변하세요. 다른 설명이나 코드블록 표시(```) 없이 순수 JSON 객체만 출력합니다.

{{
  "summary": ["한 줄 요약 1", "한 줄 요약 2", "한 줄 요약 3"],
  "sentiment": "호재" 또는 "악재" 또는 "중립" 중 하나,
  "reason": "그렇게 판단한 이유를 1~2문장으로"
}}

주의사항:
- summary 는 반드시 3개 항목, 각 항목은 핵심만 담아 한 문장 이내로 짧게.
- 공시가 투자심리에 미칠 영향을 기준으로 sentiment 를 판단하세요. 단순 정기공시(사업보고서 제출 등)처럼 방향성이 없으면 "중립"을 사용하세요.
- 원문이 부족해 판단이 어려우면 sentiment 를 "중립"으로 하고 reason 에 그 사실을 밝히세요.
"""


def summarize_with_claude(client, corp_name: str, report_nm: str, rcept_dt: str, document_text: str) -> dict:
    prompt = build_prompt(corp_name, report_nm, rcept_dt, document_text)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text").strip()

    # 혹시 모델이 코드블록으로 감싸서 답하면 벗겨낸다.
    if raw.startswith("```"):
        raw = re.sub(r"^```(json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log(f"Claude 응답을 JSON으로 해석하지 못했습니다. 원본 응답:\n{raw}")
        data = {
            "summary": [raw[:200]] if raw else ["요약 생성에 실패했습니다."],
            "sentiment": "중립",
            "reason": "모델 응답 형식 오류로 자동 판단을 하지 못했습니다.",
        }

    data.setdefault("summary", ["요약 없음"])
    data.setdefault("sentiment", "중립")
    data.setdefault("reason", "")
    return data


SENTIMENT_EMOJI = {"호재": "🟢", "악재": "🔴", "중립": "⚪"}


def format_telegram_message(corp_name: str, report_nm: str, rcept_dt: str, rcept_no: str, analysis: dict) -> str:
    emoji = SENTIMENT_EMOJI.get(analysis.get("sentiment"), "⚪")
    summary_lines = analysis.get("summary") or []
    summary_text = "\n".join(f"• {line}" for line in summary_lines)
    reason = analysis.get("reason", "")
    viewer_url = DART_VIEWER_URL.format(rcept_no=rcept_no)

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = (
        f"{emoji} <b>[{esc(analysis.get('sentiment', '중립'))}]</b> {esc(corp_name)}\n"
        f"<b>{esc(report_nm)}</b>\n"
        f"접수일자: {esc(rcept_dt)}\n\n"
        f"{esc(summary_text)}\n\n"
        f"판단 근거: {esc(reason)}\n\n"
        f'<a href="{viewer_url}">DART 원문 보기</a>'
    )
    return text


def send_telegram_message(text: str) -> None:
    url = TELEGRAM_SEND_URL.format(token=TELEGRAM_BOT_TOKEN)
    # 텔레그램 메시지는 최대 4096자 제한이 있어 넘으면 잘라준다.
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(생략됨)"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=20)
    if resp.status_code != 200:
        log(f"텔레그램 전송 실패: {resp.status_code} {resp.text}")
    resp.raise_for_status()


def run_test_message() -> None:
    """설정이 잘 되었는지 확인하기 위한 테스트 메시지 전송 (DART/Claude 호출 없음)."""
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

    end_de = datetime.date.today().strftime("%Y%m%d")
    bgn_de = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    log(f"전체 시장 공시 조회 중... ({bgn_de} ~ {end_de})")
    try:
        all_filings = fetch_all_filings(bgn_de, end_de)
    except requests.RequestException as e:
        raise SystemExit(f"DART API 호출 실패: {e}")

    # 전체 공시 중에서 우리 관심 기업(corp_code)에 해당하는 것만 골라낸다.
    filings = [f for f in all_filings if f.get("corp_code") in watch_map]
    log(f"전체 {len(all_filings)}건 중 관심 기업 공시 {len(filings)}건 발견.")

    # 증권사의 ELB/ELS 발행 등, 투자 판단에 의미 없는 반복성 공시 제목은 걸러낸다.
    before_exclude_count = len(filings)
    filings = [
        f for f in filings
        if not any(kw in f.get("report_nm", "") for kw in EXCLUDE_KEYWORDS)
    ]
    excluded_count = before_exclude_count - len(filings)
    if excluded_count:
        log(f"제외 키워드에 걸려 {excluded_count}건 필터링됨 (알림 대상 {len(filings)}건 남음).")

    # 오래된 것부터 순서대로 알림을 보내기 위해 rcept_dt/rcept_no 기준 정렬
    filings.sort(key=lambda f: (f.get("rcept_dt", ""), f.get("rcept_no", "")))

    new_count = 0
    for filing in filings:
        rcept_no = filing.get("rcept_no")
        if not rcept_no or rcept_no in seen:
            continue

        corp_name = watch_map.get(filing.get("corp_code"), filing.get("corp_name", "(알 수 없음)"))
        report_nm = filing.get("report_nm", "(제목 없음)")
        rcept_dt = filing.get("rcept_dt", "")
        log(f"신규 공시 발견: {corp_name} - {report_nm} ({rcept_no})")

        document_text = fetch_document_text(rcept_no)

        try:
            analysis = summarize_with_claude(client, corp_name, report_nm, rcept_dt, document_text)
        except Exception as e:  # Claude API 오류 등
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
            continue  # state 에 추가하지 않아 다음 실행 때 재시도됨

        seen.add(rcept_no)
        new_count += 1
        state["seen_rcept_no"] = sorted(seen)
        save_state(state)  # 하나 처리할 때마다 저장 (중간에 실패해도 진행 상황 보존)
        time.sleep(1)  # 텔레그램/Claude API 호출 사이 살짝 대기

    log(f"완료. 신규 알림 {new_count}건.")


if __name__ == "__main__":
    main()
