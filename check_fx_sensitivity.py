#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심 기업(WATCH_LIST)의 최신 "사업보고서"에서 환율 민감도 분석 주석을 찾아
엑셀(.xlsx) 파일로 정리하는 일회성 배치 스크립트입니다.

dart_telegram_bot.py / news_telegram_bot.py 와 달리 상시로 도는 봇이 아니라,
"지금 관심 기업들이 환율 민감도를 어떻게 공시하고 있는지 한 번 훑어보고 싶을 때"
수동으로(GitHub Actions의 Run workflow 버튼으로) 실행하는 용도입니다.

왜 이렇게 복잡한가?
--------------------
DART OpenAPI에는 "환율 민감도"를 바로 돌려주는 API가 없습니다. 이 정보는
사업보고서 재무제표의 "주석"(예: 금융위험관리, 파생상품 및 위험관리 등) 안에
서술형 문단/표로만 존재합니다. 그래서 이 스크립트는:

1. WATCH_LIST 기업마다 DART "공시검색" API(list.json)로 최근 약 15개월 내
   제출된 정기보고서(사업보고서/반기보고서/분기보고서)를 모두 찾은 뒤,
   그중 "가장 최근에 접수된 것"을 1차로 확인합니다. 최근 환율이 많이
   움직였다면 작년 사업보고서보다 이번 분기/반기보고서 쪽에 더 최신 수치가
   있을 수 있기 때문입니다.
2. "공시서류원본파일" API(document.xml)로 그 보고서 전체 원문을 내려받아
   텍스트로 변환합니다 (dart_telegram_bot.py와 동일한 방식, 단 여기서는
   글자수를 앞에서 자르지 않고 문서 전체를 봅니다 - 재무제표 주석은 보통
   문서 맨 뒤쪽에 있기 때문입니다).
3. 전체 텍스트에서 "환율" + "민감도"(또는 "위험관리") 가 가까이 나오는
   부분을 정규식으로 찾아냅니다. 가장 최근 보고서가 분기/반기보고서였는데
   여기서 못 찾았다면, 분기/반기보고서는 주석이 간략해서 원래 없을 수도
   있으므로 가장 최근 사업보고서로 한 번 더 확인합니다(사업보고서가 보통
   주석이 제일 상세합니다).
4. 찾은 부분(발췌문)만 Claude에게 보내서 "환율 10% 변동 시 영향 ○○억원"
   같은 핵심 수치를 한두 문장으로 뽑아달라고 요청합니다 (문서 전체가 아니라
   발췌문만 보내므로 비용이 크지 않습니다).
5. 회사별로 결과를 fx_sensitivity_report.xlsx 파일 한 줄씩에 정리합니다
   (엑셀 파일이라 받자마자 바로 더블클릭해서 열면 됩니다 - CSV처럼 따로
   변환할 필요가 없습니다). 어떤 보고서(사업/반기/분기보고서, 접수일자)를
   기준으로 확인했는지도 "확인한 보고서" 열에 같이 남깁니다.

주의할 점
---------
- 사업보고서는 1년에 한 번만 나오므로, 매번 다시 봐야 새로 알 게 있는
  정보가 아닙니다. 필요할 때 수동으로 한 번씩 돌리는 용도입니다.
- 회사마다 주석 작성 방식이 달라서, 실제로는 언급이 없거나 표 형태라
  텍스트 추출이 애매할 수 있습니다. 이 스크립트는 "찾았다/못 찾았다"와
  "찾았으면 Claude가 요약한 내용"을 참고용으로 보여줄 뿐, 100% 정확하다고
  보장하지 않습니다. 애매한 건이 있으면 엑셀의 원문 링크로 직접 확인하세요.
- 회사 수가 많으면(예: 350개) 실행에 시간이 꽤 걸립니다(수십 분 단위).
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
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

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
# 설정값
# ---------------------------------------------------------------------------

DART_API_KEY = os.environ.get("DART_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
WATCH_LIST_RAW = os.environ.get("WATCH_LIST", "[]")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

# 사업보고서를 몇 일치 조회할지 (사업보고서는 보통 회계연도 종료 후 약 90일 이내
# 제출되므로, 15개월 정도 여유를 두면 최신 사업보고서를 놓치지 않습니다).
REPORT_LOOKBACK_DAYS = int(os.environ.get("FX_REPORT_LOOKBACK_DAYS", "450"))

OUTPUT_XLSX = Path(os.environ.get("FX_OUTPUT_XLSX", "fx_sensitivity_report.xlsx"))

# 몇 개 회사 처리할 때마다 중간 저장할지 (실행 중간에 타임아웃/오류가 나도
# 그때까지의 결과가 파일로 남아있도록 하기 위함).
SAVE_EVERY = 10

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

REQUEST_TIMEOUT = 30


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_required_env() -> None:
    missing = []
    for name in ["DART_API_KEY", "ANTHROPIC_API_KEY", "WATCH_LIST"]:
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
        raise SystemExit(f"WATCH_LIST 환경변수가 올바른 JSON이 아닙니다: {e}")
    if not isinstance(items, list) or not items:
        raise SystemExit("WATCH_LIST 가 비어 있습니다.")
    for item in items:
        if "corp_code" not in item or "name" not in item:
            raise SystemExit(f"WATCH_LIST 항목에는 name, corp_code 가 모두 필요합니다: {item}")
    return items


# 정기보고서 종류. 환율이 최근에 많이 움직였다면 작년 사업보고서보다
# 이번 분기/반기보고서 쪽에 더 최신 수치가 있을 수 있으므로, 이 세 종류를
# 전부 후보로 놓고 "가장 최근에 접수된 것"을 우선으로 확인합니다.
PERIODIC_REPORT_KEYWORDS = ("사업보고서", "반기보고서", "분기보고서")


def find_periodic_reports(corp_code: str) -> list:
    """이 회사의 최근 정기보고서(사업/반기/분기보고서, 정정 포함) 목록을
    접수일자 오래된 것 -> 최신 순으로 반환한다. 하나도 없으면 빈 리스트."""
    end_de = datetime.date.today().strftime("%Y%m%d")
    bgn_de = (datetime.date.today() - datetime.timedelta(days=REPORT_LOOKBACK_DAYS)).strftime("%Y%m%d")

    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "pblntf_ty": "A",  # 정기공시
        "page_no": "1",
        "page_count": "100",
    }
    try:
        resp = requests.get(DART_LIST_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log(f"  공시 목록 조회 실패: {e}")
        return []

    status = data.get("status")
    if status == "013":  # 조회된 데이터 없음
        return []
    if status != "000":
        log(f"  DART API 오류: status={status}, message={data.get('message')}")
        return []

    candidates = [
        f for f in data.get("list", [])
        if any(kw in f.get("report_nm", "") for kw in PERIODIC_REPORT_KEYWORDS)
    ]
    # rcept_dt(접수일자) 기준 오름차순 정렬 (정정 보고서가 있으면 보통 원본보다
    # 늦게 접수되므로 자연스럽게 뒤로 간다).
    candidates.sort(key=lambda f: (f.get("rcept_dt", ""), f.get("rcept_no", "")))
    return candidates


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


def _strip_markup(raw_bytes: bytes) -> str:
    text = raw_bytes.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = text.replace("<BR>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    text = text.replace("</TD>", " ").replace("</td>", " ")
    text = text.replace("</TR>", "\n").replace("</tr>", "\n")
    text = text.replace("</P>", "\n").replace("</p>", "\n")
    text = _TAG_RE.sub(" ", text)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
        text = text.replace(a, b)
    text = _WS_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def fetch_full_document_text(rcept_no: str) -> str:
    """사업보고서 원문 zip 전체를 내려받아 텍스트로 합친다. 재무제표 주석은
    보통 문서 뒷부분에 있어서, dart_telegram_bot.py와 달리 여기서는 앞부분만
    자르지 않고 전체를 반환한다 (단, 지나치게 큰 경우를 대비해 200만자에서
    안전하게 자른다)."""
    params = {"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}
    try:
        resp = requests.get(DART_DOCUMENT_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"  원문 다운로드 실패: {e}")
        return ""

    content = resp.content
    if not content.startswith(b"PK"):
        return ""

    texts = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith((".xml", ".html", ".htm")):
                    texts.append(_strip_markup(zf.read(name)))
    except zipfile.BadZipFile:
        log("  원문 zip 파일이 손상되었습니다.")
        return ""

    full_text = "\n\n".join(t for t in texts if t)
    return full_text[:2_000_000]


def find_fx_sensitivity_excerpt(full_text: str) -> tuple:
    """전체 문서 텍스트에서 환율 민감도 관련 문단을 찾아 (발췌문, 신뢰도) 로 반환한다.
    못 찾으면 (None, "없음") 을 반환한다."""
    if not full_text:
        return None, "없음"

    # 1순위: "환율" 과 "민감도" 가 서로 가까이 나오는 곳 (가장 신뢰도 높음)
    for m in re.finditer("민감도", full_text):
        start = max(0, m.start() - 400)
        end = min(len(full_text), m.start() + 1600)
        window = full_text[start:end]
        if "환율" in window:
            return window, "높음"

    # 2순위: "환율" 과 "위험관리" 가 가까이 나오는 곳 (수치 없이 서술만 있을 가능성)
    for m in re.finditer("위험관리", full_text):
        start = max(0, m.start() - 400)
        end = min(len(full_text), m.start() + 1600)
        window = full_text[start:end]
        if "환율" in window:
            return window, "낮음"

    return None, "없음"


def extract_summary_with_claude(client, corp_name: str, excerpt: str) -> str:
    prompt = f"""아래는 "{corp_name}"의 사업보고서 재무제표 주석 중 일부를 발췌한 것입니다.

--- 발췌문 시작 ---
{excerpt}
--- 발췌문 끝 ---

이 발췌문에서 "환율 변동에 따른 민감도 분석" 수치를 찾아 한국어 한두 문장으로
간단히 요약하세요 (예: "환율 10% 상승 시 세전손익 약 30억원 감소, 10% 하락 시
약 30억원 증가"). 구체적인 수치가 없고 서술만 있다면 "구체적 수치 없이 위험관리
정책만 서술됨"이라고 답하세요. 이 내용과 무관한 발췌문이면 "관련 내용 없음"이라고
답하세요. 다른 설명 없이 요약 문장만 출력하세요."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if block.type == "text").strip()


def check_company(client, corp_name: str, corp_code: str) -> dict:
    """이 회사의 환율 민감도를 확인한다.

    전략: 사업/반기/분기보고서 중 "가장 최근에 접수된 것"을 먼저 확인한다. 거기서
    못 찾았고, 그게 사업보고서가 아니었다면(=분기/반기보고서였다면), 혹시 사업보고서
    쪽에는 있을 수 있으니 가장 최근 사업보고서로 한 번 더 확인한다 (사업보고서가
    보통 주석이 제일 상세하기 때문). 두 시도 모두 실패하면 "없음"으로 기록한다.
    """
    try:
        candidates = find_periodic_reports(corp_code)
    except Exception as e:
        log(f"  오류로 건너뜁니다: {e}")
        candidates = []

    if not candidates:
        return {"no_report": True}

    latest = candidates[-1]
    report_used = latest
    full_text = fetch_full_document_text(latest.get("rcept_no", ""))
    excerpt, confidence = find_fx_sensitivity_excerpt(full_text)
    fallback_note = ""

    if not excerpt and "사업보고서" not in latest.get("report_nm", ""):
        # 가장 최근 보고서가 분기/반기보고서인데 못 찾았다면, 가장 최근 사업보고서로 재시도.
        annual_candidates = [c for c in candidates if "사업보고서" in c.get("report_nm", "")]
        if annual_candidates:
            annual_latest = annual_candidates[-1]
            time.sleep(0.3)
            annual_text = fetch_full_document_text(annual_latest.get("rcept_no", ""))
            annual_excerpt, annual_confidence = find_fx_sensitivity_excerpt(annual_text)
            if annual_excerpt:
                report_used = annual_latest
                excerpt, confidence = annual_excerpt, annual_confidence
                fallback_note = "(최신 분기/반기보고서엔 없어 최근 사업보고서 기준) "

    rcept_no = report_used.get("rcept_no", "")
    report_label = f"{report_used.get('report_nm', '')} · {report_used.get('rcept_dt', '')}"
    viewer_url = DART_VIEWER_URL.format(rcept_no=rcept_no) if rcept_no else ""

    if excerpt:
        try:
            summary = fallback_note + extract_summary_with_claude(client, corp_name, excerpt)
        except Exception as e:
            log(f"  Claude 요약 실패: {e}")
            summary = "(요약 실패 - 원문 링크에서 직접 확인 필요)"
    else:
        summary = "주석에서 환율 민감도 관련 문단을 찾지 못함"

    return {
        "no_report": False,
        "report_label": report_label,
        "confidence": confidence,
        "summary": summary,
        "viewer_url": viewer_url,
    }


HEADER = ["기업명", "corp_code", "확인한 보고서", "환율 민감도 신뢰도", "환율 민감도 요약", "원문 링크"]
BASE_FONT = "Arial"

# 신뢰도 값에 따라 셀 배경/글자 색을 다르게 줘서 엑셀에서 한눈에 훑어볼 수 있게 한다.
CONFIDENCE_FILL = {
    "높음": PatternFill("solid", fgColor="C6EFCE"),  # 연한 초록 - 수치를 찾음
    "낮음": PatternFill("solid", fgColor="FFEB9C"),  # 연한 노랑 - 서술만 있음
    "없음": PatternFill("solid", fgColor="F2F2F2"),  # 연한 회색 - 못 찾음
    "-": PatternFill("solid", fgColor="F2F2F2"),
}
CONFIDENCE_FONT_COLOR = {
    "높음": "1E7B34",
    "낮음": "9C6500",
    "없음": "666666",
    "-": "666666",
}


def build_workbook():
    """헤더/열 너비/서식까지 갖춘 빈 엑셀 워크북을 만든다."""
    wb = Workbook()
    ws = wb.active
    ws.title = "환율민감도"

    header_font = Font(name=BASE_FONT, bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5597")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, title in enumerate(HEADER, start=1):
        cell = ws.cell(row=1, column=col_idx, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    ws.freeze_panes = "A2"  # 스크롤해도 헤더 행이 계속 보이도록 고정
    ws.row_dimensions[1].height = 26

    for col_idx, width in enumerate([16, 12, 30, 16, 62, 38], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    return wb, ws


def add_report_row(ws, row_idx: int, corp_name: str, corp_code: str, rcept_dt: str,
                    confidence: str, summary: str, viewer_url: str) -> None:
    body_font = Font(name=BASE_FONT, size=10.5)
    top_wrap = Alignment(vertical="top", wrap_text=True)

    ws.cell(row=row_idx, column=1, value=corp_name).font = body_font
    ws.cell(row=row_idx, column=2, value=corp_code).font = body_font
    ws.cell(row=row_idx, column=3, value=rcept_dt).font = body_font

    conf_cell = ws.cell(row=row_idx, column=4, value=confidence)
    conf_cell.font = Font(name=BASE_FONT, size=10.5, bold=True,
                           color=CONFIDENCE_FONT_COLOR.get(confidence, "000000"))
    conf_cell.fill = CONFIDENCE_FILL.get(confidence, PatternFill())
    conf_cell.alignment = Alignment(horizontal="center", vertical="top")

    ws.cell(row=row_idx, column=5, value=summary).font = body_font
    ws.cell(row=row_idx, column=5).alignment = top_wrap

    link_cell = ws.cell(row=row_idx, column=6)
    if viewer_url:
        link_cell.value = "원문 보기"
        link_cell.hyperlink = viewer_url
        link_cell.font = Font(name=BASE_FONT, size=10.5, color="0563C1", underline="single")
    else:
        link_cell.value = ""
        link_cell.font = body_font

    for col_idx in (1, 2, 3):
        ws.cell(row=row_idx, column=col_idx).alignment = top_wrap


def main() -> None:
    parser = argparse.ArgumentParser(description="관심 기업 사업보고서에서 환율 민감도 정보를 찾아 엑셀로 정리")
    parser.add_argument("--limit", type=int, default=0, help="테스트용: 앞에서 N개 기업만 처리 (0이면 전체)")
    args = parser.parse_args()

    check_required_env()
    if anthropic is None:
        raise SystemExit("anthropic 패키지가 설치되지 않았습니다. pip install -r requirements.txt 를 실행하세요.")

    watchlist = load_watchlist()
    if args.limit > 0:
        watchlist = watchlist[: args.limit]
    log(f"관심 기업 {len(watchlist)}개에 대해 환율 민감도 조회를 시작합니다. "
        f"(회사 수에 따라 수십 분 걸릴 수 있습니다)")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    wb, ws = build_workbook()

    found_count = 0
    no_report_count = 0

    for i, item in enumerate(watchlist, start=1):
        row_idx = i + 1  # 1행은 헤더
        corp_name = item["name"]
        corp_code = item["corp_code"]
        log(f"[{i}/{len(watchlist)}] {corp_name} 확인 중...")

        result = check_company(client, corp_name, corp_code)

        if result["no_report"]:
            no_report_count += 1
            add_report_row(ws, row_idx, corp_name, corp_code, "", "-",
                            "최근 사업/반기/분기보고서를 찾지 못함", "")
        else:
            if result["confidence"] in ("높음", "낮음"):
                found_count += 1
            add_report_row(
                ws, row_idx, corp_name, corp_code,
                result["report_label"], result["confidence"],
                result["summary"], result["viewer_url"],
            )

        if i % SAVE_EVERY == 0:
            wb.save(OUTPUT_XLSX)  # 중간 저장 - 도중에 실패해도 여기까지 결과는 남는다
        time.sleep(0.3)  # DART/Claude API 를 너무 빠르게 연타하지 않도록 대기

    wb.save(OUTPUT_XLSX)
    log(f"완료. 총 {len(watchlist)}개 중 환율 민감도 문단 발견 {found_count}건, "
        f"최근 사업보고서 없음 {no_report_count}건. 결과: {OUTPUT_XLSX}")


if __name__ == "__main__":
    main()
