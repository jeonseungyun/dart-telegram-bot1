#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심 기업 관련 뉴스(구글 뉴스 검색 RSS) -> Claude로 "알릴 만큼 중요한가?" 판단 -> Telegram 알림 봇

동작 개요
---------
1. WATCH_LIST 에 등록된 기업마다 구글 뉴스 검색 RSS
   (https://news.google.com/rss/search?q=...&hl=ko&gl=KR) 를 조회해서
   최근 뉴스 제목/링크/출처를 가져온다. (기업별로 따로 조회해야 해서,
   기업 수가 많으면 dart_telegram_bot.py 보다 요청 횟수가 훨씬 많다.
   그래서 이 봇은 15분이 아니라 1시간 정도의 느긋한 주기로 실행하는 걸 권장한다.)
2. news_state.json 에 저장된 "이미 처리한 뉴스 링크 목록"과 비교해서 새 뉴스만 골라낸다.
   -> 처음 실행할 때는 그동안 쌓인 뉴스가 전부 "새 뉴스"로 인식되어 알림이 폭탄처럼
      쏟아질 수 있으므로, news_state.json 이 아예 없는 최초 실행은 "본 것으로만 기록"
      하고 알림은 보내지 않는다 (--seed 모드가 자동으로 적용됨).
3. 뉴스 제목에 NEWS_INCLUDE_KEYWORDS 중 하나도 없으면 Claude 호출 없이 건너뛴다
   (Claude API 비용을 줄이기 위한 1차 필터. 아무 키워드나 없어도 되게 하고 싶으면
   NEWS_INCLUDE_KEYWORDS="" 로 비워두면 모든 뉴스를 Claude에게 보낸다).
4. 키워드를 통과한 뉴스는 기사 링크에 실제로 접속해서 본문 텍스트를 추출한다
   (trafilatura 라이브러리 사용). 언론사마다 페이지 구조가 달라서 가끔 실패할 수 있는데,
   실패하면 본문 없이 "제목만" 가지고 판단하는 방식으로 자동 후퇴한다.
5. Claude에게 "이 뉴스가 텔레그램으로 알릴 만큼 중요한지, 호재/악재/중립인지, 2~3줄
   요약"을 판단시키고, "중요하다"고 판단한 것만 Telegram으로 전송한다.
6. 처리한 뉴스 링크를 news_state.json 에 추가로 저장해서 중복 알림을 막는다.

dart_telegram_bot.py 와는 완전히 독립적으로 동작하는 별도 스크립트입니다.
(따로 GitHub Actions 워크플로우로 실행하세요: .github/workflows/news_bot.yml)
"""

import os
import re
import sys
import json
import time
import argparse
import datetime
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    import trafilatura
except ImportError:
    trafilatura = None

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
# 설정값 로드
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# dart_telegram_bot.py 와 동일한 WATCH_LIST 를 그대로 재사용합니다.
WATCH_LIST_RAW = os.environ.get("WATCH_LIST", "[]")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# 기업 하나당 구글 뉴스에서 최근 몇 개까지 확인할지 (너무 크면 요청/비용이 늘어남).
NEWS_PER_COMPANY = int(os.environ.get("NEWS_PER_COMPANY", "5"))

# 뉴스 "제목"에 이 키워드들 중 하나도 없으면 Claude 를 호출하지 않고 건너뜁니다.
# (증시 잡담/시황 요약 같은 뉴스가 워낙 많아서, 회사에 실제로 의미 있는 이벤트로
#  보이는 것만 1차로 걸러내는 용도. 비워두면 전부 Claude에게 보냅니다.)
DEFAULT_NEWS_INCLUDE_KEYWORDS = (
    "실적,계약,수주,인수,합병,특허,소송,리콜,화재,파업,감사의견,상장폐지,매각,"
    "유상증자,무상증자,배당,자사주,대표이사,신용등급,목표가,투자,증설,파산,부도,"
    "횡령,적자,흑자,신제품,단독,특징주"
)
NEWS_INCLUDE_KEYWORDS = [
    kw.strip() for kw in os.environ.get("NEWS_INCLUDE_KEYWORDS", DEFAULT_NEWS_INCLUDE_KEYWORDS).split(",")
    if kw.strip()
]

NEWS_STATE_FILE = Path(os.environ.get("NEWS_STATE_FILE", "news_state.json"))

# 기사 본문에서 Claude에게 보낼 최대 글자 수 (너무 길면 비용/속도 문제가 생기므로 자름).
MAX_ARTICLE_CHARS = int(os.environ.get("MAX_ARTICLE_CHARS", "3000"))

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

# 구글이 기본 User-Agent(파이썬 requests) 요청을 차단하는 경우가 있어 브라우저처럼 보이게 함.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_required_env() -> None:
    missing = []
    for name in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
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
        raise SystemExit("WATCH_LIST 가 비어 있습니다.")
    for item in items:
        if "name" not in item:
            raise SystemExit(f"WATCH_LIST 항목에는 name 이 필요합니다: {item}")
    return items


def load_state() -> tuple:
    """(state dict, 최초 실행 여부) 를 반환한다."""
    is_first_run = not NEWS_STATE_FILE.exists()
    if NEWS_STATE_FILE.exists():
        try:
            return json.loads(NEWS_STATE_FILE.read_text(encoding="utf-8")), False
        except json.JSONDecodeError:
            log(f"경고: {NEWS_STATE_FILE} 파일이 손상되어 새로 시작합니다.")
    return {"seen_links": []}, is_first_run


def save_state(state: dict) -> None:
    NEWS_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_news_for_company(corp_name: str) -> list:
    """기업명으로 구글 뉴스 RSS 를 검색해서 [{title, link, pubDate, source}, ...] 를 반환한다.
    실패하면 빈 리스트를 반환한다 (한 기업 실패가 전체 실행을 막지 않도록)."""
    # 회사명이 흔한 단어(예: "동서", "대상")인 경우 무관한 뉴스가 너무 많이 섞이는 걸
    # 줄이기 위해 "주식"을 함께 검색어로 넣는다. 완벽하진 않지만 잡음을 꽤 줄여준다.
    query = f"{corp_name} 주식"
    params = {"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    try:
        resp = requests.get(GOOGLE_NEWS_RSS_URL, params=params, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"뉴스 조회 실패 ({corp_name}): {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        log(f"뉴스 응답 파싱 실패 ({corp_name}): {e}")
        return []

    items = []
    for item in root.findall("./channel/item")[:NEWS_PER_COMPANY]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""

        # 구글 뉴스는 title 을 "실제 제목 - 언론사명" 형태로 붙여서 주는 경우가 많다.
        # source 를 알고 있으면 끝에 붙은 " - 언론사명" 부분을 제목에서 떼어낸다.
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()

        if title and link:
            items.append({"title": title, "link": link, "pubDate": pub_date, "source": source})

    return items


def should_check_with_claude(title: str) -> bool:
    if not NEWS_INCLUDE_KEYWORDS:
        return True
    return any(kw in title for kw in NEWS_INCLUDE_KEYWORDS)


def fetch_article_text(url: str) -> str:
    """뉴스 링크에 접속해서 기사 본문 텍스트만 추출한다 (광고/메뉴/관련기사 등은 제외).
    실패하면 빈 문자열을 반환한다 (이 경우 제목만으로 판단하는 방식으로 자동 후퇴).
    구글 뉴스 링크는 실제 언론사 페이지로 리다이렉트되므로, requests 가 그 리다이렉트를
    따라가서 최종 페이지의 HTML을 가져온다."""
    if trafilatura is None:
        return ""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"기사 본문 접속 실패 ({url}): {e}")
        return ""

    try:
        extracted = trafilatura.extract(resp.text, favor_precision=True)
    except Exception as e:  # trafilatura 가 이상한 페이지에서 예외를 던지는 경우 대비
        log(f"기사 본문 추출 실패 ({url}): {e}")
        return ""

    if not extracted:
        return ""
    return extracted[:MAX_ARTICLE_CHARS]


def build_news_prompt(corp_name: str, title: str, source: str, article_text: str) -> str:
    if article_text:
        content_part = f"""아래는 관심 기업과 관련된 뉴스 기사의 본문입니다.

기업명: {corp_name}
뉴스 제목: {title}
출처: {source or "알수없음"}

--- 기사 본문 ---
{article_text}
--- 본문 끝 ---"""
    else:
        content_part = f"""아래는 관심 기업과 관련된 뉴스입니다. (기사 본문을 가져오지 못해 제목만 있습니다.)

기업명: {corp_name}
뉴스 제목: {title}
출처: {source or "알수없음"}"""

    return f"""당신은 한국 주식시장에 정통한 애널리스트입니다.
{content_part}

이 뉴스가 투자자에게 텔레그램 알림으로 보낼 만큼 구체적이고 의미 있는 기업 이벤트인지
판단하세요. 단순 시황 요약, 증시 전반 뉴스, 광고성 기사, 너무 일반적이거나 모호한 내용은
알릴 필요가 없다고 판단하세요.

다음 형식의 JSON 으로만 답변하세요. 다른 설명이나 코드블록 표시(```) 없이 순수 JSON
객체만 출력합니다.

{{
  "notify": true 또는 false,
  "sentiment": "호재" 또는 "악재" 또는 "중립" 중 하나,
  "summary": ["핵심 요약 1", "핵심 요약 2"],
  "reason": "notify 를 그렇게 판단한 이유를 1문장으로"
}}

주의사항:
- summary 는 기사 본문이 있으면 본문 기준으로, 없으면 제목 기준으로 핵심만 2개 항목,
  각 항목은 한 문장 이내로 짧게 작성하세요.
- 판단이 애매하면 notify 를 false 로 하세요 (놓치는 것보다, 애매한 걸 너무 많이 보내서
  알림이 스팸처럼 되는 게 더 나쁩니다).
- 계약/수주, 실적(어닝서프라이즈/쇼크), 인수합병, 소송/제재, 리콜/사고, 경영진 변화,
  신용등급 변경, 대규모 투자/증설 등 구체적 이벤트는 notify: true 로 하세요.
"""


def judge_news_with_claude(client, corp_name: str, title: str, source: str, article_text: str = "") -> dict:
    prompt = build_news_prompt(corp_name, title, source, article_text)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
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
        data = {"notify": False, "sentiment": "중립", "summary": [title], "reason": "응답 형식 오류"}

    data.setdefault("notify", False)
    data.setdefault("sentiment", "중립")
    data.setdefault("summary", [title])
    data.setdefault("reason", "")
    return data


SENTIMENT_EMOJI = {"호재": "🟢", "악재": "🔴", "중립": "⚪"}


def format_telegram_message(corp_name: str, title: str, source: str, link: str, analysis: dict) -> str:
    emoji = SENTIMENT_EMOJI.get(analysis.get("sentiment"), "⚪")
    summary_lines = analysis.get("summary") or []
    summary_text = "\n".join(f"• {line}" for line in summary_lines)

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = (
        f"{emoji} <b>[뉴스/{esc(analysis.get('sentiment', '중립'))}]</b> {esc(corp_name)}\n"
        f"<b>{esc(title)}</b>\n"
        f"출처: {esc(source) or '알수없음'}\n\n"
        f"{esc(summary_text)}\n\n"
        f"판단 근거: {esc(analysis.get('reason', ''))}\n\n"
        f'<a href="{link}">기사 보기</a>'
    )
    return text


def send_telegram_message(text: str) -> None:
    url = TELEGRAM_SEND_URL.format(token=TELEGRAM_BOT_TOKEN)
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
    check_required_env()
    send_telegram_message("✅ 뉴스 알림 봇 테스트 메시지입니다. 텔레그램 연동이 정상적으로 작동합니다!")
    log("테스트 메시지를 전송했습니다. 텔레그램을 확인하세요.")


def main() -> None:
    parser = argparse.ArgumentParser(description="관심 기업 뉴스 -> Claude 판단 -> Telegram 알림 봇")
    parser.add_argument("--test", action="store_true", help="텔레그램 연동만 테스트 (뉴스/Claude 호출 없음)")
    args = parser.parse_args()

    if args.test:
        run_test_message()
        return

    check_required_env()
    if anthropic is None:
        raise SystemExit("anthropic 패키지가 설치되지 않았습니다. pip install -r requirements.txt 를 실행하세요.")

    watchlist = load_watchlist()
    log(f"관심 기업 {len(watchlist)}개 뉴스 감시 중.")

    state, is_first_run = load_state()
    seen = set(state.get("seen_links", []))

    if is_first_run:
        log(f"{NEWS_STATE_FILE} 파일이 없어 최초 실행으로 판단합니다. "
            f"이번 실행에서는 알림을 보내지 않고, 지금까지의 뉴스를 '본 것'으로만 기록합니다.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    new_count = 0
    checked_count = 0
    skipped_by_keyword = 0

    for item in watchlist:
        corp_name = item["name"]
        news_list = fetch_news_for_company(corp_name)
        time.sleep(0.3)  # 구글에 너무 빠르게 연타하지 않도록 대기

        for news in news_list:
            link = news["link"]
            if link in seen:
                continue

            if is_first_run:
                # 최초 실행: 알림 없이 "본 것"으로만 기록
                seen.add(link)
                continue

            title = news["title"]
            if not should_check_with_claude(title):
                skipped_by_keyword += 1
                seen.add(link)  # 다시 안 보도록 기록은 남긴다
                continue

            checked_count += 1
            article_text = fetch_article_text(link)
            if not article_text:
                log(f"기사 본문을 가져오지 못해 제목만으로 판단합니다: {corp_name} - {title}")
            try:
                analysis = judge_news_with_claude(client, corp_name, title, news["source"], article_text)
            except Exception as e:
                log(f"Claude 판단 실패 ({corp_name} - {title}): {e}")
                seen.add(link)
                continue

            if analysis.get("notify"):
                message = format_telegram_message(corp_name, title, news["source"], link, analysis)
                try:
                    send_telegram_message(message)
                except requests.RequestException as e:
                    log(f"텔레그램 전송 실패 ({corp_name} - {title}): {e} - 다음 실행에서 재시도합니다.")
                    continue  # seen 에 추가하지 않아 다음 실행 때 재시도됨
                new_count += 1
                log(f"알림 전송: {corp_name} - {title}")

            seen.add(link)
            state["seen_links"] = sorted(seen)
            save_state(state)  # 하나 처리할 때마다 저장 (중간 실패에도 진행 상황 보존)

    state["seen_links"] = sorted(seen)
    save_state(state)

    if is_first_run:
        log(f"최초 실행 완료. 뉴스 {len(seen)}건을 '본 것'으로 기록했습니다. "
            f"다음 실행부터 새 뉴스에 대해 알림이 갑니다.")
    else:
        log(f"완료. Claude로 판단한 뉴스 {checked_count}건 중 {new_count}건 알림 전송. "
            f"키워드 필터로 건너뛴 뉴스 {skipped_by_keyword}건.")


if __name__ == "__main__":
    main()
