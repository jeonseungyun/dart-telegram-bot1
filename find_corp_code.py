#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DART 고유번호(corp_code) 검색 도우미.

WATCH_LIST 환경변수를 채우려면 각 기업의 8자리 corp_code 가 필요합니다.
이 스크립트는 DART 가 제공하는 전체 기업 코드 목록(corpCode.xml)을 내려받아
회사 이름(또는 일부)으로 검색할 수 있게 해줍니다.

[한 개씩 검색]
    export DART_API_KEY=발급받은키
    python find_corp_code.py 삼성전자

[여러 개(예: 200개) 한 번에 검색 - 관심 기업이 많을 때 추천]
    1. companies.txt 파일을 만들고, 한 줄에 회사 이름(또는 6자리 종목코드)을 하나씩 적습니다.
       예)
         삼성전자
         005930
         카카오
         NAVER
    2. 아래 명령어 실행:
         python find_corp_code.py --file companies.txt
    3. 실행이 끝나면 watchlist_output.json 파일과, .env 에 바로 붙여넣을 수 있는
       WATCH_LIST=... 한 줄이 화면에 출력됩니다.
    4. 혹시 못 찾은 회사가 있으면 화면에 "찾지 못함" 목록으로 따로 알려줍니다.
"""

import io
import os
import re
import sys
import json
import zipfile
import argparse
import xml.etree.ElementTree as ET

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DART_API_KEY = os.environ.get("DART_API_KEY", "").strip()
CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def download_corp_codes() -> list:
    if not DART_API_KEY:
        raise SystemExit("DART_API_KEY 환경변수가 설정되지 않았습니다.")

    resp = requests.get(CORP_CODE_URL, params={"crtfc_key": DART_API_KEY}, timeout=30)
    resp.raise_for_status()

    if not resp.content.startswith(b"PK"):
        raise SystemExit(f"고유번호 목록을 받아오지 못했습니다. 응답: {resp.text[:300]}")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes)
    companies = []
    for node in root.findall("list"):
        companies.append({
            "corp_code": (node.findtext("corp_code") or "").strip(),
            "corp_name": (node.findtext("corp_name") or "").strip(),
            "stock_code": (node.findtext("stock_code") or "").strip(),
        })
    return companies


def run_single_search(keyword: str) -> None:
    print("DART 전체 기업 코드 목록을 내려받는 중입니다... (파일이 커서 몇 초 걸릴 수 있어요)")
    companies = download_corp_codes()

    matches = [c for c in companies if keyword in c["corp_name"]]
    if not matches:
        print(f"'{keyword}' 를 포함하는 회사를 찾지 못했습니다.")
        return

    print(f"\n검색 결과 {len(matches)}건 (상장사를 찾는다면 stock_code 가 있는 항목을 확인하세요):\n")
    matches.sort(key=lambda c: (c["stock_code"] == "", c["corp_name"]))
    for c in matches[:30]:
        listed = f"종목코드 {c['stock_code']}" if c["stock_code"] else "비상장/기타"
        print(f"  corp_code={c['corp_code']}  |  {c['corp_name']}  |  {listed}")

    if len(matches) > 30:
        print(f"\n... 외 {len(matches) - 30}건 더 있습니다. 검색어를 더 구체적으로 입력해 보세요.")

    print("\n찾은 corp_code 를 WATCH_LIST 에 아래와 같은 형식으로 넣으면 됩니다:")
    print('  {"name": "회사이름", "corp_code": "위에서_찾은_8자리코드"}')


def run_bulk_search(file_path: str) -> None:
    if not os.path.exists(file_path):
        raise SystemExit(f"파일을 찾을 수 없습니다: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        raise SystemExit(f"{file_path} 파일이 비어 있습니다. 한 줄에 회사 이름(또는 종목코드) 하나씩 적어주세요.")

    print(f"{file_path} 에서 {len(lines)}개 항목을 읽었습니다.")
    print("DART 전체 기업 코드 목록을 내려받는 중입니다... (파일이 커서 몇 초 걸릴 수 있어요)")
    companies = download_corp_codes()

    by_stock_code = {c["stock_code"]: c for c in companies if c["stock_code"]}
    by_exact_name = {}
    for c in companies:
        # 같은 이름이 여러 개면(드묾) 상장사(stock_code 있는 것)를 우선한다.
        if c["corp_name"] not in by_exact_name or (c["stock_code"] and not by_exact_name[c["corp_name"]]["stock_code"]):
            by_exact_name[c["corp_name"]] = c

    found = []
    not_found = []
    seen_corp_codes = set()

    for raw_line in lines:
        query = raw_line.strip()
        match = None

        if re.fullmatch(r"\d{6}", query):
            match = by_stock_code.get(query)
        else:
            match = by_exact_name.get(query)
            if match is None:
                # 정확히 일치하는 이름이 없으면, 이름을 포함하면서 상장된 후보를 찾아본다.
                candidates = [c for c in companies if query in c["corp_name"] and c["stock_code"]]
                if len(candidates) == 1:
                    match = candidates[0]

        if match and match["corp_code"] not in seen_corp_codes:
            found.append({"name": match["corp_name"], "corp_code": match["corp_code"]})
            seen_corp_codes.add(match["corp_code"])
        elif match is None:
            not_found.append(query)
        # match가 있는데 이미 seen_corp_codes에 있으면 -> 중복 입력이므로 조용히 건너뜀

    print(f"\n찾은 기업: {len(found)}개 / 못 찾은 항목: {len(not_found)}개\n")

    if not_found:
        print("아래 항목은 정확히 매칭되지 않았습니다. 정식 회사명이나 6자리 종목코드로 다시 확인해보세요:")
        for q in not_found:
            print(f"  - {q}")
        print()

    output_path = "watchlist_output.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=2)
    print(f"찾은 목록을 {output_path} 파일로도 저장했습니다.\n")

    watch_list_line = "WATCH_LIST=" + json.dumps(found, ensure_ascii=False)
    print("아래 줄을 그대로 복사해서 .env 파일의 WATCH_LIST= 줄을 통째로 바꿔치기 하세요:\n")
    print(watch_list_line)


def main() -> None:
    parser = argparse.ArgumentParser(description="DART corp_code 검색 도우미")
    parser.add_argument("keyword", nargs="?", help="검색할 회사 이름 (한 개씩 검색할 때)")
    parser.add_argument("--file", "-f", help="회사 이름/종목코드를 한 줄씩 적은 텍스트 파일 (여러 개 한 번에 검색)")
    args = parser.parse_args()

    if args.file:
        run_bulk_search(args.file)
    elif args.keyword:
        run_single_search(args.keyword)
    else:
        print("사용법:")
        print("  한 개 검색:      python find_corp_code.py 삼성전자")
        print("  여러 개 검색:    python find_corp_code.py --file companies.txt")
        sys.exit(1)


if __name__ == "__main__":
    main()
