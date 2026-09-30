# 첫 실제 Slack 테스트 체크리스트

실제 Slack 앱과 토큰으로 처음 돌려 볼 때 순서대로 확인하는 목록입니다. 항목마다 결과(✅/❌)와 이상한 점을 적어 두면 다음 수정에 바로 쓸 수 있습니다.

아래 `/herdr`는 본인의 `SLASH_COMMAND`(예: `/herdr-kim`)를 뜻합니다.

`.venv\Scripts\python`은 플러그인 폴더 기준 경로입니다. 로컬 링크로 설치했다면 `D:\Git\herdr-slackbot`입니다.

## 0. 준비

- [ ] 브리지가 멈춰 있는지 확인합니다: `herdr plugin action invoke status --plugin herdr-slackbot`
- [ ] Herdr에 테스트용 워크스페이스가 하나 있고, 거기서 claude 에이전트를 띄울 수 있습니다.
- [ ] 테스트용 탭에서는 쉬운 프롬프트만 씁니다(예: "2+2는?"). 에이전트는 Slack으로 코드 출력을 보냅니다.

## 1. Slack 앱 만들기

설정 마법사(`herdr plugin action invoke setup --plugin herdr-slackbot`)로 1·2·페어링을 한 번에 해도 됩니다. 그때는 마법사가 연 브라우저 화면(manifest가 미리 채워졌는지)과 토큰 확인 메시지를 기록하고, 아래 확인 항목만 따라갑니다.

- [ ] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`을 실행합니다. 출력된 슬래시 명령과 봇 이름을 확인합니다.
- [ ] <https://api.slack.com/apps> → **Create New App** → **From a manifest**를 누르고, 설정 폴더의 `slack-app-manifest.json`을 붙여 넣어 앱을 만듭니다.
  - manifest 검증 경고나 오류가 뜨면 그 문구를 그대로 기록합니다.
- [ ] **App-Level Token**을 만듭니다(scope: `connections:write`). 받은 `xapp-...`가 `SLACK_APP_TOKEN`입니다.
- [ ] **Install to Workspace**를 누릅니다. 받은 `xoxb-...`가 `SLACK_BOT_TOKEN`입니다.
- [ ] 앱 설정 **OAuth & Permissions**의 Bot Token Scopes가 `chat:write`, `commands`, `files:write`, `im:history`, `im:write` 5개인지 확인합니다.
- [ ] 앱 설정에서 확인합니다:
  - Socket Mode가 켜져 있습니다.
  - Interactivity가 켜져 있습니다.
  - Event Subscriptions에 `message.im`이 있습니다.
  - 슬래시 명령 이름이 `.env`와 같습니다.

## 2. `.env` 채우기

- [ ] `notepad "$(herdr plugin config-dir herdr-slackbot)\.env"`로 파일을 엽니다. `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`을 채웁니다. `SLACK_OWNER_USER_ID`는 비워 둡니다(페어링으로 채워짐).
- [ ] `.venv\Scripts\python -m herdr_slackbot check`를 실행합니다. `slack config: ok`, `owner: not paired yet ...`이고 Herdr 에이전트 목록이 나오면 됩니다.

## 2a. 시작과 페어링

- [ ] `herdr plugin action invoke start --plugin herdr-slackbot`를 실행합니다.
- [ ] `herdr-slack` 워크스페이스의 브리지 pane에 `Slack pairing code:  NNNNNN` 상자가 나오고, Herdr 알림 "Slack pairing code: NNNNNN"이 뜹니다.
- [ ] `status` 액션이 `slack: waiting for pairing (code in herdr-slack pane)`을 보여 줍니다.
- [ ] 봇 DM에서 `/herdr list`를 보내면 "not paired yet" 안내가 본인에게만 보입니다. Home 탭을 열어도 같은 안내가 보입니다.
- [ ] `/herdr pair 000000`(틀린 코드)을 보내면 "Wrong pairing code (4 attempts left ...)"가 옵니다.
- [ ] (가능하면 다른 계정으로) 5번 틀리면 코드가 바뀌고, 그 계정은 "⏳ Too many wrong codes from you"를 받습니다. 본인 계정은 새 코드로 바로 페어링할 수 있습니다.
- [ ] `/herdr pair <pane의 코드>`를 보내면 "paired ✅"가 오고, 봇 DM에 사용법 안내(👋 Paired!)가 올라옵니다.
- [ ] `.env`에 `SLACK_OWNER_USER_ID=U...`가 들어갔고, 주석과 다른 값은 그대로입니다.
- [ ] 재시작 없이 `/herdr list`가 바로 동작합니다. `STATE_DIR\pairing.json`은 지워졌습니다. `bridge.log`에 코드가 없습니다.
- [ ] (가능하면) 다른 계정으로 `/herdr pair <code>`를 보내면 "⛔ ... only accepts requests from its owner."가 옵니다.

## 3. 마커 왕복 확인 (중요)

Slack이 응답을 잃어버렸을 때, 브리지는 메시지에 숨겨 둔 마커를 다시 찾아 중복 전송을 막습니다. 마커는 메시지 metadata와 block_id 두 군데에 들어갑니다. 이 두 곳이 실제 Slack에서 보존되는지는 오프라인에서 확인할 수 없어서 여기서 확인합니다.

- [ ] `.venv\Scripts\python -m herdr_slackbot marker-check`를 실행합니다.
  - 봇 DM에 "marker check" 메시지와 스레드 답장이 하나씩 올라옵니다. 확인 후 지워도 됩니다.
  - `root`와 `reply` 두 줄에 metadata / block_id / find_message 결과가 나옵니다.
- [ ] 마지막 줄이 `PASS`인지 확인합니다.
  - `metadata=no`라도 `block_id=yes`이고 `find_message=found`이면 괜찮습니다(fallback 동작).
  - `ERROR at …`나 `read ERROR`가 나오면 Slack 호출 자체가 실패한 것입니다(예: `missing_scope`는 `im:history` 권한 누락, `invalid_auth`는 토큰 문제). 마커 보존 여부와는 별개이니, 권한이나 토큰을 고친 뒤 다시 실행합니다.
  - `FAIL`이거나 `UNKNOWN`이면 두 줄 출력을 그대로 기록해 주세요. 이 진단 결과는 브리지 동작을 바꾸지 않습니다. 브리지는 전송마다 따로 판단합니다.
    - 조회가 불완전하거나 마커가 안 보이면 다시 보내지 않고 보류하거나 버립니다. 이 경우 드물게 알림이 빠질 수 있습니다.
    - 조회가 끝까지 됐는데 마커가 없으면(5초 이상 지난 뒤) 다시 보냅니다. 이때 Slack이 마커를 지웠다면 중복이 생길 수 있습니다.
    - 그래서 "중복 없음"은 이 확인이 `PASS`일 때만 기대할 수 있습니다.

## 4. 시작

- [ ] `herdr plugin action invoke restart --plugin herdr-slackbot`를 실행합니다(페어링 뒤 첫 재시작).
- [ ] `herdr-slack` 워크스페이스의 브리지 pane에 `bridge running: /herdr-... for owner U...` 로그가 나옵니다.
- [ ] `status` 액션을 실행하면 `running (pid …, pane …)`과 `slack: ready (owner U...)`가 나옵니다.

## 5. 명령과 모달

- [ ] DM에서 `/herdr`를 입력하면 사용법이 본인에게만 보이게(ephemeral) 나옵니다.
- [ ] `/herdr list`: 워크스페이스별 에이전트 목록과 상태 이모지가 나옵니다.
- [ ] `/herdr status`: 브리지 상태(uptime, 구독 pane 수 등)가 나옵니다.
- [ ] `/herdr new`를 입력하면 모달이 먼저 "loading"으로 열리고, 곧 폼으로 바뀝니다.
  - [ ] 워크스페이스를 바꾸면 cwd가 그 워크스페이스 기준으로 다시 채워집니다.
  - [ ] 종류를 codex로 바꾸면 모델/effort 목록이 바뀌고, 권한 모드 칸이 사라집니다.
  - [ ] 이미 있는 이름을 입력하고 제출하면 모달에 오류가 표시됩니다.
  - [ ] 정상 제출: 해당 워크스페이스에 새 탭이 생깁니다. DM에 `🚀` 스레드 루트가 생기고, 이어서 `⏳ started`, 그다음 `✅` 결과가 옵니다.
- [ ] `/herdr new <워크스페이스> 2+2는?` (인자로 바로 실행): 모달 없이 위와 같은 흐름입니다.
- [ ] `/herdr send`: 모달에서 에이전트를 고르고 프롬프트를 보냅니다. 결과는 그 에이전트의 스레드로 옵니다.
  - [ ] 에이전트를 고르면 Agent와 Prompt 사이에 잠깐 "불러오는 중…"이 보이고, 곧 "마지막 응답 · n분 전 · 소요 시간"과 응답 내용으로 바뀝니다.
  - [ ] 응답이 긴 에이전트는 "…(앞부분 생략)"으로 시작하는 끝부분만 보입니다. 코드 블록이 깨지지 않는지 확인합니다.
  - [ ] 프롬프트를 입력하다가 에이전트를 바꿔도 입력한 내용이 지워지지 않습니다. 빠르게 A→B로 바꿔도 B의 응답만 남습니다.
  - [ ] 작업 중인 에이전트는 "작업 중입니다 — 아직 응답이 없습니다"가 한 줄 나오고, 보내기는 평소처럼 됩니다(busy 거절).
  - [ ] Home 탭 에이전트 줄의 [보내기]로 열면, 그 에이전트가 선택된 상태로 응답이 바로 불러와집니다.
- [ ] `/herdr send <이름> <프롬프트>`: 바로 전송됩니다.
- [ ] 작업 중(working)인 에이전트에게 보내면 "busy"라고 거절됩니다.

## 6. 스레드 답장

- [ ] 에이전트 스레드에 답장을 쓰면 그 에이전트에게 프롬프트로 전달됩니다. 결과도 같은 스레드에 옵니다.
- [ ] 스레드 밖(DM 본문)에 일반 메시지를 쓰면 사용법만 ephemeral로 나옵니다.
- [ ] 에이전트 탭을 PC에서 닫은 뒤 그 스레드에 답장하면 "exited" 안내가 나옵니다.

## 7. PC 에이전트 알림

- [ ] PC에서 직접 띄운 claude에 작업을 시킵니다. 그 탭을 보고 있지 않은 상태로 끝나면(done) 새 스레드와 `✅` 결과가 옵니다.
- [ ] 같은 작업을 탭을 보면서 끝내면(idle) 알림이 오지 않습니다(정상).
- [ ] 권한 확인이 필요한 작업을 시켜 blocked 상태로 만들면 `⚠️ … needs confirmation on PC`가 옵니다.
- [ ] 스레드 알림이 휴대폰이나 데스크톱에서 실제로 울리는지 적어 둡니다. 스레드 답장만으로 충분히 눈에 띄는지 판단하기 위한 기록입니다.

## 8. 음소거

- [ ] 스레드 루트의 🔕 버튼을 누르면 버튼이 🔔로 바뀝니다. 이후 그 에이전트의 PC 작업 알림(done/blocked)은 오지 않습니다.
- [ ] 음소거 상태에서도 Slack에서 보낸 작업의 결과는 옵니다.
- [ ] 🔔를 다시 누르면 알림이 돌아옵니다.

## 9. 긴 결과와 [전체 보기]

- [ ] 긴 답변이 나오는 프롬프트를 보냅니다(예: "파이썬 표준 라이브러리 모듈 60개를 한 줄 설명과 함께 표로"). 결과가 잘려서 오고 **[전체 보기]** 버튼이 붙습니다.
- [ ] **[전체 보기]**를 누르면 스레드에 `.md` 파일이 올라오고, 파일 안에 전체 내용이 있습니다.

## 10. 재시작 복구

- [ ] Slack에서 오래 걸리는 작업을 보냅니다. `⏳ started`가 온 뒤 `stop` 액션으로 브리지를 멈춥니다.
- [ ] 작업이 끝날 때까지 기다린 뒤 `start` 액션을 실행합니다. 결과가 원래 스레드에 한 번만 옵니다.
- [ ] `restart` 액션: 터미널에 Ctrl+C를 입력하지 않고, 브리지가 stop 요청을 받아 스스로 종료합니다(pane에 `^C`가 찍히지 않음). 이어서 새 탭에서 다시 시작하고, 이전 브리지 탭은 비어 있으면 닫힙니다.
- [ ] 이전 브리지 탭에서 다른 프로그램을 실행해 둔 상태로 `start`를 누릅니다. 새 탭에서 시작하고, 프로그램이 도는 이전 탭은 닫히지 않습니다(브리지 pane에 `previous bridge shell is busy; left alone`).

## 10-1. Home 탭 (manifest 갱신 필요)

Home 탭은 manifest가 바뀌어야 동작합니다. 앱을 이전 manifest로 만들었다면 먼저 반영합니다.

- [ ] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`로 manifest를 다시 만듭니다. `.env`는 그대로 유지됩니다.
- [ ] 앱 설정 → **App Manifest**에 새 `slack-app-manifest.json` 내용을 붙여 넣고 **Save Changes**를 누릅니다.
  - 재설치를 요구하면 **Reinstall to Workspace**를 누릅니다.
  - 저장 후 **App Home**에 Home Tab이 켜져 있는지, **Event Subscriptions**에 `app_home_opened`가 있는지 확인합니다.
- [ ] 브리지를 다시 시작합니다(`restart` 액션).
- [ ] Slack에서 봇 → **홈** 탭을 엽니다. 제목, 상태 줄(uptime · 에이전트 수 · 슬래시 명령 · 갱신 시각), 버튼 3개, 워크스페이스별 에이전트 목록이 보입니다.
- [ ] **[➕ 새 에이전트]**를 누르면 새 에이전트 모달이 열리고, 제출하면 평소처럼 DM 스레드가 생깁니다.
- [ ] **[📤 보내기]**를 누르면 보내기 모달이 열립니다(에이전트 미선택 상태).
- [ ] idle/done 에이전트 줄의 **[보내기]**를 누르면 그 에이전트가 미리 선택된 모달이 열립니다. working/blocked 줄에는 버튼이 없습니다.
- [ ] **[🔄 새로고침]**을 누르면 갱신 시각이 바뀝니다.
- [ ] 홈 탭을 열어 둔 채 PC에서 에이전트에 작업을 시킵니다. 몇 초 안에 상태가 working으로, 끝나면 done/idle로 바뀝니다.
- [ ] 브리지 로그에 `publishing the Home tab failed`가 없는지 확인합니다.
- [ ] (가능하면) 동료가 이 봇의 홈 탭을 열었을 때 에이전트 정보가 보이지 않는지 확인합니다.

## 11. 소유자 외 거절 (가능하면)

- [ ] 동료에게 봇 DM에서 `/herdr list`를 입력해 달라고 부탁합니다. 동료에게 "⛔ This Herdr bridge only accepts requests from its owner."가 보이고, 동료의 요청은 실행되지 않습니다.
- [ ] 브리지 로그에 `rejected … from non-owner`가 남습니다.

## 12. 끝나고

- [ ] `STATE_DIR\bridge.log`에 토큰(`xoxb-`, `xapp-`)이 그대로 찍혀 있지 않은지 확인합니다(`[redacted]`로 보여야 함).
- [ ] 테스트로 만든 탭은 PC에서 정리합니다. `tab close`를 하면 에이전트도 종료됩니다.
- [ ] 실패한 항목, 이상한 문구, 로그 발췌를 모아 전달합니다.
