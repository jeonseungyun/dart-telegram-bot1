# DART 공시 → Claude 요약 → 텔레그램 알림 봇

관심 기업의 새 전자공시(DART)가 올라오면, Claude API로 3줄 요약과 호재/악재 판단을 만들어
텔레그램으로 자동 전송해주는 파이썬 봇입니다. GitHub Actions로 돌리면 내 컴퓨터를 꺼두어도
평일 낮 시간 동안 무료로 자동 감시됩니다.

## 파일 구성

- `dart_telegram_bot.py` : 실제로 공시를 확인하고 알림을 보내는 메인 스크립트
- `find_corp_code.py` : 관심 기업의 DART 고유번호(corp_code)를 찾아주는 보조 스크립트
- `requirements.txt` : 필요한 파이썬 라이브러리 목록
- `.env.example` : 로컬 테스트용 설정 파일 예시
- `state.json` : 이미 알림을 보낸 공시 목록(중복 알림 방지용, 자동 관리됨)
- `.github/workflows/dart_bot.yml` : GitHub Actions 자동 실행 설정

---

## 1단계. API 키 3종 발급받기

### 1-1. DART(전자공시) OpenAPI 키

1. https://opendart.fss.or.kr 접속 후 우측 상단 **회원가입** (이메일 인증만 하면 됩니다, 무료)
2. 로그인 후 상단 메뉴에서 **인증키 신청/관리** 클릭
3. "인증키 신청" 버튼을 눌러 이용 목적 등 간단한 정보 입력 후 신청 (보통 즉시 발급됩니다)
4. 발급된 40자리 인증키를 복사해둡니다 → 이것이 `DART_API_KEY` 입니다
   - 참고: 하루 최대 20,000회 호출까지 무료입니다. 이 봇은 그 정도로 많이 호출하지 않으니 걱정하지 않아도 됩니다.

### 1-2. Anthropic Claude API 키

1. https://console.anthropic.com 접속 후 회원가입/로그인
2. 좌측 메뉴에서 **Billing** 으로 들어가 결제수단을 등록하고 소액 크레딧을 충전합니다
   (Claude API는 콘솔 가입만으로는 무료 크레딧이 자동 지급되지 않을 수 있어, 소액 충전이 필요할 수 있습니다.
   호출량이 적은 개인용 봇이라 실제 비용은 한 달에 몇백 원~몇천 원 수준일 가능성이 높습니다.)
3. 좌측 메뉴에서 **API Keys** 클릭 → **Create Key** 로 새 키 생성
4. `sk-ant-...` 로 시작하는 키를 복사해둡니다 → 이것이 `ANTHROPIC_API_KEY` 입니다
   - 비용을 더 아끼고 싶다면 코드의 `CLAUDE_MODEL` 값을 `claude-sonnet-5` 대신 `claude-haiku-4-5` 로 바꿔도 됩니다 (속도 빠르고 저렴, 품질은 약간 낮을 수 있음).

### 1-3. 텔레그램 봇 만들기

1. 텔레그램 앱에서 **@BotFather** 를 검색해 대화를 시작합니다
2. `/newbot` 명령 입력 → 봇 이름과 아이디(예: `my_dart_alert_bot`, 반드시 bot으로 끝나야 함)를 순서대로 입력
3. 생성이 완료되면 `123456789:ABCdefGhIJKlmNoPQRstuVWxyZ` 형태의 토큰을 알려줍니다 → 이것이 `TELEGRAM_BOT_TOKEN` 입니다
4. 이제 **내 chat_id**(알림을 받을 대화방 번호)를 알아내야 합니다:
   - 방금 만든 봇을 텔레그램에서 검색해 대화창을 열고 아무 메시지나 하나 보냅니다 (예: "안녕")
   - 웹 브라우저에서 아래 주소에 접속합니다 (토큰 부분을 본인 것으로 교체)
     ```
     https://api.telegram.org/bot<내토큰>/getUpdates
     ```
   - 결과 JSON 안에서 `"chat":{"id": 123456789, ...}` 부분의 숫자를 찾습니다 → 이것이 `TELEGRAM_CHAT_ID` 입니다
   - 팁: 메시지를 보낸 직후에 접속해야 결과가 보입니다. 결과가 비어 있다면 봇에게 메시지를 다시 보내고 새로고침하세요.

---

## 2단계. 관심 기업의 corp_code 찾기

DART API는 회사를 종목코드가 아닌 8자리 `corp_code` 로 구분합니다. 아래 스크립트로 쉽게 찾을 수 있습니다.

```bash
pip install -r requirements.txt
export DART_API_KEY=발급받은_DART_키
python find_corp_code.py 삼성전자
```

실행하면 아래처럼 후보가 출력됩니다. 종목코드(stock_code)가 있는 항목이 상장사입니다.

```
corp_code=00126380  |  삼성전자  |  종목코드 005930
```

이렇게 찾은 `corp_code` 들을 모아 아래 형태의 JSON 배열로 만들어두세요. 이것이 `WATCH_LIST` 값이 됩니다.

```json
[{"name": "삼성전자", "corp_code": "00126380"}, {"name": "카카오", "corp_code": "00918444"}]
```

---

## 3단계. 로컬에서 먼저 테스트해보기 (선택이지만 강력 추천)

1. `.env.example` 파일을 복사해 `.env` 로 이름을 바꾸고, 1~2단계에서 얻은 값들을 채워 넣습니다.
2. 라이브러리 설치:
   ```bash
   pip install -r requirements.txt
   ```
3. 텔레그램 연동만 먼저 테스트 (DART/Claude 호출 없이 테스트 메시지만 전송):
   ```bash
   python dart_telegram_bot.py --test
   ```
   텔레그램으로 "✅ 테스트 메시지" 가 오면 성공입니다.
4. 전체 동작 테스트 (실제로 최근 공시를 조회하고 요약까지 진행):
   ```bash
   python dart_telegram_bot.py
   ```
   관심 기업에 최근(기본 2일 이내) 공시가 있었다면 텔레그램으로 요약이 도착합니다.
   같은 공시는 `state.json` 에 기록되어 다시 실행해도 중복 알림이 오지 않습니다.
   (다시 테스트해보고 싶다면 `state.json` 의 `seen_rcept_no` 배열을 비워보세요.)

---

## 4단계. GitHub Actions로 24시간 자동화하기

내 컴퓨터를 꺼도 GitHub 서버가 대신 정해진 시간마다 스크립트를 실행해줍니다. Public 저장소는 완전 무료,
Private 저장소도 한 달 2,000분까지 무료라 개인용으로는 충분합니다.

### 4-1. GitHub 저장소 만들고 코드 올리기

1. https://github.com 에서 새 저장소(Repository)를 하나 만듭니다 (Private 로 만드는 것을 추천 — API 키가 코드에는 없지만, 그래도 관심 기업 목록 등은 비공개로 두는 게 안전합니다)
2. 이 폴더의 내용을 그 저장소에 업로드합니다. 터미널을 쓸 수 있다면:
   ```bash
   git init
   git add .
   git commit -m "init: dart telegram bot"
   git branch -M main
   git remote add origin https://github.com/내계정/내저장소.git
   git push -u origin main
   ```
   (git이 익숙하지 않다면 GitHub 웹사이트의 "Add file → Upload files" 기능으로 폴더를 통째로 드래그해서 올려도 됩니다. 단, `.env` 파일은 만들었더라도 절대 업로드하지 마세요.)

### 4-2. Secrets(비밀값) 등록하기

1. 저장소 페이지에서 **Settings → Secrets and variables → Actions** 로 이동
2. **New repository secret** 을 눌러 아래 5개를 하나씩 등록합니다 (이름은 정확히 똑같이 입력):

   | Name | Value |
   |---|---|
   | `DART_API_KEY` | 1-1에서 발급받은 DART 인증키 |
   | `ANTHROPIC_API_KEY` | 1-2에서 발급받은 Claude API 키 |
   | `TELEGRAM_BOT_TOKEN` | 1-3에서 발급받은 봇 토큰 |
   | `TELEGRAM_CHAT_ID` | 1-3에서 찾은 chat_id |
   | `WATCH_LIST` | 2단계에서 만든 JSON 배열 (예: `[{"name": "삼성전자", "corp_code": "00126380"}]`) |

### 4-3. 동작 확인하기

1. 저장소 상단 **Actions** 탭으로 이동
2. 왼쪽에 "DART 공시 알림 봇" 워크플로우가 보이면 클릭
3. 처음 push한 직후라면 GitHub이 스케줄 워크플로우 인식에 몇 분 걸릴 수 있습니다. 안 보이면 잠시 후 새로고침하세요.
4. 우측의 **Run workflow** 버튼으로 수동 실행해서 정상 작동하는지 먼저 확인해보세요.
5. 이후에는 `.github/workflows/dart_bot.yml` 에 설정된 대로 평일 09:00~19:00(한국시간) 사이 15분마다 자동으로 실행됩니다.

### 4-4. 알아두면 좋은 점

- GitHub의 스케줄(cron) 실행은 정확히 정시에 실행되지 않고 몇 분씩 밀릴 수 있습니다 (GitHub 서버 부하에 따라 다름). 실시간 초단위 알림이 필요한 용도는 아니라는 점 참고하세요.
- 저장소에 **60일 이상 아무 활동(커밋 등)이 없으면 GitHub이 스케줄 워크플로우를 자동으로 비활성화**합니다. 이 봇은 매번 `state.json`을 커밋하기 때문에 새 공시가 있는 한 활동이 계속 생겨 문제없지만, 관심 기업들에 공시가 60일 넘게 전혀 없다면 한 번씩 Actions 탭에서 수동 실행을 해주는 것도 좋습니다.
- 감시 시간대나 주기를 바꾸고 싶다면 `dart_bot.yml` 의 `cron: "*/15 0-10 * * 1-5"` 부분을 수정하면 됩니다 (형식: 분 시 일 월 요일, 시간은 UTC 기준입니다).
- 공시 원문이 매우 긴 문서(사업보고서 등)는 앞부분 일부만 잘라서 요약에 사용합니다 (`MAX_DOC_CHARS` 로 조절 가능). 짧은 수시공시류는 대부분 문제없이 전체가 반영됩니다.
- API 키가 외부에 노출되지 않도록 `.env` 파일과 `WATCH_LIST` 값을 다른 사람과 공유하지 마세요.

---

## 문제 해결(트러블슈팅)

- **텔레그램 메시지가 안 와요** → `python dart_telegram_bot.py --test` 로 먼저 텔레그램 연동만 확인해보세요. 토큰/chat_id 를 잘못 입력한 경우가 대부분입니다.
- **"WATCH_LIST 가 비어 있습니다" 오류** → JSON 형식이 올바른지 확인하세요 (따옴표, 쉼표, 대괄호). https://jsonlint.com 같은 사이트에 붙여넣어 검증할 수 있습니다.
- **Claude 응답 파싱 실패 로그가 보여요** → 가끔 모델이 형식을 살짝 벗어난 답을 줄 수 있습니다. 봇은 이 경우에도 제목만으로 대체 메시지를 보내니 알림 자체는 계속 옵니다.
- **GitHub Actions 로그 보는 법** → Actions 탭 → 실행 기록 클릭 → "run-bot" 잡 클릭 → 각 단계별 로그 확인 (스크립트의 `log()` 출력이 여기에 모두 남습니다).
