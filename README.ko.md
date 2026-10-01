# herdr-slackbot

[English](README.md) | **한국어**

[Herdr](https://herdr.dev) 플러그인입니다. **내 PC의 Herdr 에이전트(Claude Code / Codex)를 Slack 봇 DM으로 연결**합니다.

- 에이전트가 작업을 끝내거나(✅ done) 답을 기다릴 때(⚠️ blocked) DM으로 알려 줍니다. 결과 본문도 함께 옵니다.
- 권한 확인, 질문(AskUserQuestion), 계획 승인 같은 대화 상자는 **Slack 버튼으로 바로 답할 수 있습니다.**
- Slack에서 슬래시 명령으로 에이전트 목록을 보고, 새 에이전트를 띄우고, 실행 중인 에이전트에 프롬프트를 보낼 수 있습니다.
- 에이전트마다 DM 스레드가 하나씩 생기고, 그 스레드에 답장하면 해당 에이전트에게 프롬프트로 전달됩니다.

사용자 한 명(PC 한 대)마다 **자기 전용 Slack 앱**을 하나 만듭니다. 봇은 페어링한 **본인 Slack 계정의 요청만** 처리합니다.

```
Slack DM ──(Socket Mode)──> herdr-slackbot 브리지 ──(named pipe)──> Herdr ──> claude / codex 에이전트
            (공개 URL 불필요)   (Herdr 워크스페이스 herdr-slack의 pane에서 실행)
```

## 스크린샷

**Home 탭:** 봇을 열면 PC의 에이전트가 워크스페이스별로 보입니다. 각 줄에는 상태 이모지 · 이름/pane · 종류 · 상태 · 터미널 제목이 표시됩니다. 위쪽 버튼으로 새 에이전트를 띄우거나 프롬프트를 보낼 수 있고, idle/done인 에이전트는 줄마다 있는 **[Send]**로 바로 보낼 수 있습니다.

![Home 탭: 워크스페이스별 에이전트 목록과 New Agent / Send / Refresh 버튼](docs/images/home-tab.png)

**새 에이전트 (`/herdr new`, [➕ New Agent]):** 고른 워크스페이스에 새 탭을 만들고 에이전트를 시작합니다. 모델·effort·권한 모드를 고를 수 있고, 기본값은 Opus · high · auto입니다. 이름을 비우면 `slack-<N>`이 붙습니다.

![새 에이전트 모달: Model, Effort, Permission mode, Name, Prompt 입력](docs/images/new-agent-modal.png)

**보내기 (`/herdr send`, [📤 Send]):** 실행 중인 에이전트를 골라 프롬프트를 보냅니다. 에이전트를 고르면 지금까지의 대화(내가 보낸 프롬프트와 에이전트의 답변, 최신이 맨 아래)가 모달에 표시됩니다. 작업 결과는 에이전트별 DM 스레드로 옵니다.

![보내기 모달: Agent 선택과 Prompt 입력](docs/images/send-modal.png)

## 빠른 시작

1. **준비물:** Windows 10/11, **Herdr 0.8.2 이상**, **Python 3.11 이상**. Python이 없으면 먼저 설치합니다.

   ```powershell
   winget install Python.Python.3.12
   ```

   Slack 워크스페이스에서 앱을 만들고 설치할 권한도 필요합니다. 관리자가 관리하는 워크스페이스에서는 앱 설치에 승인이 필요할 수 있습니다.

2. **플러그인 설치:** Herdr가 설치 단계에서 `scripts/setup.ps1`을 실행해 venv와 의존성, 설정 뼈대를 만듭니다.

   ```powershell
   herdr plugin install paper3300/herdr-slackbot
   ```

3. **설정 마법사:** 현재 워크스페이스에 **Slack setup** 탭이 열리고 그 안에서 마법사가 실행됩니다.

   ```powershell
   herdr plugin action invoke setup --plugin herdr-slackbot
   ```

   Herdr 명령 팔레트에서 *Slack bridge: setup wizard* 액션을 골라도 같습니다. 플러그인 액션 자체는 입력을 받을 수 없어서(stdin이 연결되지 않음), 액션은 새 탭을 열고 그 탭의 셸에 마법사 명령을 입력합니다.

마법사가 하는 일:

1. 설정 폴더에 `.env` 뼈대와 Slack 앱 manifest를 만듭니다. 슬래시 명령과 봇 이름은 `.env`에 값이 없을 때만 묻습니다. Enter를 누르면 괄호 안의 기본값을 씁니다.
2. 브라우저로 Slack의 앱 만들기 화면을 엽니다. manifest가 미리 채워져 있으니 워크스페이스를 고르고 **Next → Create**만 누르면 됩니다. 링크와 manifest 파일 경로도 출력하므로, 브라우저가 안 열리거나 양식이 비어 있으면 파일 내용을 직접 붙여 넣습니다.
3. 어느 페이지에서 무엇을 복사할지 알려 주고 토큰 두 개를 받습니다.
   - `xapp-...`: **Basic Information → App-Level Tokens → Generate Token and Scopes**, scope `connections:write`
   - `xoxb-...`: **Install App → Install to Workspace** 후 *Bot User OAuth Token*

   받은 토큰은 Slack에 직접 확인합니다(`apps.connections.open`, `auth.test`). 틀리면 Slack이 돌려준 오류를 보여 주고 다시 묻습니다. `.env`에 이미 유효한 토큰이 있으면 건너뛰고, 바꿀지만 묻습니다. 토큰 전체를 화면에 다시 출력하지 않습니다.
4. 토큰을 `.env`에 **제자리에서** 씁니다. 주석과 다른 값은 그대로 둡니다.
5. 브리지를 시작합니다. 이미 돌고 있으면 재시작합니다.
6. 아직 페어링하지 않았다면 페어링 코드를 보여 주고 페어링될 때까지(최대 10분) 기다립니다. 아래 **페어링**을 보세요.
7. 슬래시 명령, 봇 DM 위치, 재시작·상태 확인 방법을 요약해 줍니다.

Ctrl+C로 언제든 멈출 수 있습니다. 그때까지 입력한 값은 `.env`에 남아 있고, 다시 실행하면 이어서 진행합니다.

## 페어링 (본인 Slack 계정 연결)

봇이 누구의 요청을 받을지는 `.env`의 `SLACK_OWNER_USER_ID`로 정합니다. 멤버 ID를 직접 찾을 필요는 없습니다. 값이 비어 있으면 브리지가 **페어링 모드**로 시작합니다.

1. 브리지가 6자리 코드를 만듭니다. 코드는 `herdr-slack` 워크스페이스의 브리지 pane에 크게 표시되고, Herdr 알림("Slack pairing code: NNNNNN")으로도 뜹니다. 마법사를 쓰는 중이면 마법사 화면에도 나옵니다.
2. Slack 왼쪽 **앱** 목록에서 봇을 열고, DM 창에서 다음을 보냅니다.

   ```
   /herdr-kim pair 123456
   ```

3. "paired ✅"가 오면 브리지가 `.env`에 `SLACK_OWNER_USER_ID`를 써 넣고, **재시작 없이** 본인 전용 모드를 시작합니다. 준비가 끝나면 봇 DM으로 사용법 안내(👋 Paired!)가 옵니다. 그 전에 명령을 보내면 "still starting" 안내가 나옵니다. 시작에 실패하면(몇 번 다시 시도한 뒤) pane과 Herdr 알림, 마법사가 `restart`를 하라고 알려 줍니다. 저장된 계정으로 정상 시작합니다.

- 페어링 모드에서는 `pair` 명령만 받습니다. 다른 명령, 버튼, 모달, DM 메시지, Home 탭에는 "not paired yet" 안내만 보냅니다. 에이전트 알림도 페어링 후에 시작합니다.
- 코드는 15분이 지나거나 틀린 코드가 모두 5번 들어오면 새 코드로 바뀝니다(pane과 알림에 다시 표시). 또 Slack 사용자 한 명이 10분 안에 5번 틀리면 **그 사람만** 10분 동안 시도할 수 없습니다. 틀린 적이 없는 사람(본인)은 막히지 않으므로, 누가 코드를 찍어 보더라도 본인의 페어링을 막을 수 없습니다.
- 한 번 페어링하면 `pair`도 다른 사람의 요청처럼 거절합니다. **다른 계정으로 다시 페어링**하려면 `.env`에서 `SLACK_OWNER_USER_ID=` 값을 지우고 `restart` 액션을 실행합니다.
- 페어링을 기다리는 동안 `status` 액션은 `slack: waiting for pairing (code in herdr-slack pane)`을 보여 줍니다. 페어링 후에는 브리지가 실제로 본인 전용 모드로 돌고 있을 때만 `slack: ready (owner U...)`가 나옵니다. `.env`에 멤버 ID만 있고 브리지가 준비되지 않았으면 `not ready`, 시작에 실패했으면 `owner mode failed to start`로 표시합니다. 마법사도 브리지가 준비됐다고 알릴 때까지(최대 60초) 기다린 뒤에만 성공이라고 말합니다.

## 수동 설정

마법사를 쓰지 않을 때의 방법입니다. 마법사가 하는 일을 손으로 합니다.

### 1. 설치

GitHub에서 설치하려면 위의 `herdr plugin install`을 쓰면 됩니다. 저장소를 직접 받아 로컬 링크로 쓸 수도 있습니다.

```powershell
git clone https://github.com/paper3300/herdr-slackbot D:\Git\herdr-slackbot
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Git\herdr-slackbot\scripts\setup.ps1
herdr plugin link D:\Git\herdr-slackbot
herdr plugin list                                # herdr-slackbot ... enabled 확인
```

`link`로 설치했는데 venv가 없으면, 첫 시작 때 플러그인이 알아서 만듭니다. 이 경우에도 `setup` 액션(마법사)을 쓸 수 있습니다.

### `setup.ps1`이 하는 일

1. 플러그인 폴더에 `.venv`를 만들고 의존성(`slack_bolt`, `slack_sdk`)을 설치합니다.
2. 기본값을 정합니다.
   - 슬래시 명령: `/herdr-<윈도우 사용자명>` (소문자, 32자 이내)
   - 봇 이름: `Herdr (<사용자명>)`
3. 플러그인 설정 폴더(`herdr plugin config-dir herdr-slackbot`, 보통 `%APPDATA%\herdr\plugins\config\herdr-slackbot`)에 파일 두 개를 씁니다.
   - `.env`: 설정 뼈대입니다. 파일이 이미 있으면 **기존 값은 절대 덮어쓰지 않고** 빠진 키만 덧붙입니다.
   - `slack-app-manifest.json`: Slack 앱을 만들 때 붙여 넣을 manifest입니다.
4. 다음에 할 일을 출력합니다.

`setup.ps1`은 입력을 받지 않습니다(Herdr의 설치 단계로도 실행되기 때문입니다). `-Wizard`를 붙이면 설정 단계 대신 마법사를 실행하는데, 이때는 터미널에서 실행해야 합니다.

슬래시 명령과 봇 이름은 옵션으로 바꿀 수 있습니다. `.env`에 이미 키가 있으면 빈 값이라도 그대로 두고, 옵션은 적용되지 않았다고 알려 줍니다. manifest는 항상 실제로 적용될 설정(빈 값이면 기본값)으로 만들어집니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -SlashCommand /herdr-kim -DisplayName "Herdr (김)"
```

> 슬래시 명령 이름은 **Slack 워크스페이스 전체에서 겹치면 안 됩니다.** 같은 이름이면 가장 나중에 설치한 앱이 명령을 가져갑니다. 그래서 사용자마다 `/herdr-<이름>`처럼 다르게 정합니다.

### 2. Slack 앱 만들기 (manifest 사용)

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest**를 누릅니다.
2. 사용할 워크스페이스를 고릅니다.
3. **JSON** 탭에서 기존 내용을 지우고 `slack-app-manifest.json`의 내용을 붙여 넣은 뒤 **Next** → **Create**를 누릅니다.
4. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**:
   - 이름은 아무거나 넣고, scope는 `connections:write`를 추가한 뒤 Generate를 누릅니다.
   - 나온 `xapp-...` 토큰이 `SLACK_APP_TOKEN`입니다.
5. **Install App** → **Install to Workspace** → 허용을 누릅니다.
   - **Bot User OAuth Token** `xoxb-...`이 `SLACK_BOT_TOKEN`입니다.
6. Slack 왼쪽 **앱** 목록에서 방금 만든 봇을 열면 DM 창이 생깁니다. 메시지 탭이 막혀 있으면 앱 설정의 **App Home** → *Allow users to send Slash commands and messages from the messages tab*을 켭니다.

manifest에는 아래 설정이 들어 있습니다. 코드에서 실제로 호출하는 Slack API를 기준으로 뽑았고, `tests/test_slack_manifest.py`가 코드와 어긋나지 않는지 검사합니다.

| 항목 | 값 |
|---|---|
| Socket Mode | 켜짐 (공개 URL이나 포트 개방이 필요 없음) |
| Interactivity | 켜짐 (모달, 버튼) |
| 이벤트 | `message.im` (DM 스레드 답장), `app_home_opened` (Home 탭 새로고침) |
| App Home | Home 탭 켜짐, 메시지 탭 켜짐(입력 가능) |
| 슬래시 명령 | `.env`의 `SLASH_COMMAND` |
| Bot scopes | `chat:write` (메시지 전송·수정·ephemeral), `commands`, `im:write` (DM 열기), `im:history` (DM 메시지 수신), `files:write` ([View full] 파일 업로드) |

#### 이미 만든 앱의 manifest 갱신하기 (예: Home 탭 추가)

새 버전에서 manifest가 바뀌면 이미 만든 Slack 앱에도 반영해야 합니다.

1. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`를 실행합니다. 설정 폴더의 `slack-app-manifest.json`이 새로 만들어집니다. `.env` 값은 건드리지 않습니다.
2. <https://api.slack.com/apps>에서 앱 → **App Manifest**를 엽니다. JSON 내용을 새 파일 내용으로 바꾸고 **Save Changes**를 누릅니다.
3. Slack이 재설치를 요구하면(**Install App → Reinstall to Workspace**) 재설치합니다. 토큰이 바뀌었다면 `.env`도 고칩니다.
4. `restart` 액션으로 브리지를 다시 시작합니다.

### 3. `.env` 채우기

`.env`는 플러그인 설정 폴더에 있습니다. 위치는 아래 명령으로 확인합니다.

```powershell
herdr plugin config-dir herdr-slackbot
notepad "$(herdr plugin config-dir herdr-slackbot)\.env"
```

| 키 | 필수 | 설명 |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✔ | `xoxb-...` (Install App 페이지) |
| `SLACK_APP_TOKEN` | ✔ | `xapp-...` (App-Level Token, `connections:write`) |
| `SLACK_OWNER_USER_ID` | | **본인** Slack 멤버 ID (`U`로 시작). 비워 두면 페어링으로 채워집니다. 이 사용자만 봇을 쓸 수 있습니다. |
| `SLASH_COMMAND` | | 기본 `/herdr-<사용자명>`. **manifest의 명령과 같아야 합니다.** |
| `BOT_DISPLAY_NAME` | | 기본 `Herdr (<사용자명>)` |
| `STATE_DIR`, `LOG_LEVEL`, `RESULT_MAX_CHARS`, `FALLBACK_LINES`, `READ_LINES`, `BRIDGE_WORKSPACE`, `START_TIMEOUT_MS`, `CODEX_PROMPT_DELAY`, `STALL_WAIT`, `HERDR_BIN`, `HERDR_SOCKET_PATH` | | 선택 설정. 설명은 `.env.example`에 있습니다. |

토큰을 채운 뒤 `restart` 액션으로 브리지를 시작하고, 위의 **페어링**을 합니다. 멤버 ID를 직접 적어도 됩니다: Slack에서 내 프로필 사진 → **프로필** → 오른쪽 위 **⋮**(더보기) → **멤버 ID 복사**. `U0123ABCD` 같은 형태입니다.

`SLASH_COMMAND`를 바꿨다면 manifest도 다시 만들어 Slack 앱에 반영해야 합니다. `setup.ps1`을 다시 실행하면(위와 같은 `powershell -NoProfile -ExecutionPolicy Bypass -File ...` 형식) `slack-app-manifest.json`이 새로 생성됩니다. 그 내용을 앱 설정의 **App Manifest**에 붙여 넣고 저장합니다.

## 시작 / 재시작

- **자동 시작:** Herdr 서버가 시작될 때 플러그인 startup hook이 실행됩니다.
  1. 워크스페이스 `herdr-slack`이 없으면 만듭니다. 이때 포커스는 가져가지 않습니다.
  2. 그 워크스페이스의 pane에서 브리지(`python -m herdr_slackbot run`)를 실행합니다.
  3. 브리지는 인스턴스 잠금(`STATE_DIR\bridge.lock`)을 잡기 때문에 두 개가 동시에 돌지 않습니다.
  4. 시작·중지·재시작은 `STATE_DIR\lifecycle.lock` 하나로 한 번에 하나씩만 진행됩니다. 브리지도 시작할 때 같은 잠금 안에서 자기 잠금을 잡기 때문에, 상태는 항상 멈춤 / 시작 중 / 실행 중 셋 중 하나로 보입니다. 시작 중(최대 60초)에 들어온 다른 시작 요청은 무시되고, 이때 `stop`을 하면 그 시작이 취소됩니다.
  5. 시작할 때마다 `herdr-slack`에 새 탭을 만들고(포커스는 가져가지 않음) 거기에만 명령을 입력합니다. 기존 pane에는 절대 입력하지 않습니다. 새 브리지가 시작을 확인하면 이전 브리지 탭을 닫는데, 다음을 모두 만족할 때만 닫습니다: 같은 Herdr 서버에 있고, 기록된 탭/pane/터미널 ID가 그대로이고, 탭에 pane이 하나뿐이고, 에이전트나 실행 중인 프로그램이 없습니다. 하나라도 어긋나면 그 탭은 그대로 둡니다.
- startup hook은 Herdr 서버가 시작될 때만 실행됩니다. 그래서 `herdr plugin link`/`install` 직후에는 돌지 않습니다. 설정 마법사가 브리지를 시작해 주고, 마법사를 쓰지 않았다면 `start` 액션으로 바로 시작하세요.
- **플러그인 액션:**

  ```powershell
  herdr plugin action invoke setup   --plugin herdr-slackbot   # 설정 마법사 (새 탭에서 실행)
  herdr plugin action invoke start   --plugin herdr-slackbot   # 실행 중이면 아무것도 안 함
  herdr plugin action invoke restart --plugin herdr-slackbot   # .env 수정 후
  herdr plugin action invoke stop    --plugin herdr-slackbot
  herdr plugin action invoke status  --plugin herdr-slackbot   # Herdr 알림으로도 표시
  ```

  `stop`/`restart`는 터미널에 아무것도 입력하지 않습니다. 브리지가 남긴 `bridge.runtime.json`(pid와 프로세스 생성 시각)으로 실행 중인 브리지를 확인한 뒤, 그 프로세스 앞으로 `stop.request`를 남깁니다. 브리지는 이 요청을 1초마다 확인하고 스스로 정상 종료합니다. 15초 안에 끝나지 않으면 pid와 생성 시각이 여전히 맞는 경우에만 프로세스를 종료합니다. 확인이 안 되면 아무것도 하지 않고 알려 줍니다. 액션의 출력은 `herdr plugin log list --plugin herdr-slackbot`에서 볼 수 있습니다.
- **단축키:** 플러그인 manifest로는 키를 지정할 수 없습니다. Herdr `config.toml`에 직접 추가하고 `herdr server reload-config`를 실행합니다.

  ```toml
  [[keys.command]]
  key = "prefix+shift+s"
  type = "plugin_action"
  command = "herdr-slackbot.restart"
  description = "restart Slack bridge"

  [[keys.command]]
  key = "prefix+s"
  type = "plugin_action"
  command = "herdr-slackbot.status"
  description = "Slack bridge status"
  ```

- **수동 실행:** 개발할 때는 아래처럼 직접 실행합니다.

  ```powershell
  .venv\Scripts\python -m herdr_slackbot check     # 설정·Herdr 연결 점검
  .venv\Scripts\python -m herdr_slackbot status
  .venv\Scripts\python -m herdr_slackbot manifest  # 현재 설정 기준 manifest 출력
  .venv\Scripts\python -m herdr_slackbot marker-check  # 실제 Slack에서 중복 방지 마커 왕복 확인
  ```

토큰이 없거나 틀려도 브리지는 죽거나 재시작을 반복하지 않습니다. `herdr-slack` pane에 이유와 다음 할 일을 한 번 출력하고 종료하며, 셸은 그대로 남습니다. 설정을 고친 뒤 `restart` 액션을 실행하면 됩니다.

처음 실제 Slack으로 돌려 볼 때는 `docs/LIVE_TEST.md`의 체크리스트를 순서대로 따라 하세요.

## Slack에서 쓰기

아래에서 `/herdr`는 본인의 `SLASH_COMMAND`를 뜻합니다(예: `/herdr-kim`). 봇 DM 창에서 사용합니다.

| 명령 | 동작 |
|---|---|
| `/herdr` | 사용법 |
| `/herdr list` | 에이전트 목록 (워크스페이스별, 상태 이모지) |
| `/herdr new` | 모달로 새 에이전트 시작: 워크스페이스, cwd, 종류(claude/codex), 모델, effort, 권한 모드, 이름, 프롬프트 |
| `/herdr new <workspace> [name=..] [kind=claude\|codex] [model=..] [effort=..] [mode=..] [cwd=..] <prompt>` | 모달 없이 바로 시작 |
| `/herdr send` | 모달로 실행 중인 에이전트에 프롬프트 전송. 에이전트를 고르면 Agent와 Prompt 사이에 **지금까지의 대화**가 표시됩니다. 내가 보낸 프롬프트(PC에서 입력한 것 포함)와 에이전트의 최종 답변이 오래된 것부터 차례로 나옵니다. 긴 답변은 끝부분 약 2500자, 긴 프롬프트는 앞부분만 보여 주고, 다 들어가지 않는 오래된 메시지는 "… N earlier messages not shown" 한 줄로 줄입니다. 작업 중이거나 응답을 기다리는(blocked) 에이전트는 대화와 함께 안내 한 줄이 붙습니다. Claude는 세션 기록(JSONL)에서 읽고, Codex이거나 기록을 찾지 못하면 이전처럼 **마지막 응답**(시각·소요 시간 포함)을 보여 줍니다. Home 탭의 Send 버튼에서도 같은 모달이 열립니다. |
| `/herdr send <에이전트 이름\|pane id> <prompt>` | 바로 전송 |
| `/herdr status` | 브리지 상태 |
| `/herdr pair <code>` | 페어링 모드에서 본인 계정 연결 (위의 **페어링** 참고). 페어링 후에는 거절됩니다. |

- **새 에이전트:** 선택한 워크스페이스에 새 탭을 만들고 거기서 시작합니다. 기본값은 claude `--model opus --effort high --permission-mode auto`입니다. 이름을 비우면 `slack-<N>`이 붙습니다.
- **전송 규칙:** 대상이 `idle`/`done`일 때만 보냅니다.
  - `working`이면 "busy"라고 답합니다.
  - `blocked`이면 "스레드의 버튼으로 답하라"고 안내합니다 (`/herdr send`, 모달).
- **스레드:** 에이전트마다 DM 스레드가 하나씩 생깁니다.
  - Slack에서 보낸 작업은 "⏳ started"와 완료 결과가 스레드 답장으로 옵니다.
  - 스레드에 답장하면 그 에이전트에게 프롬프트로 전달됩니다. 에이전트가 답을 기다리는 중이면 답장은 그 질문의 직접 입력 답이 됩니다 (아래 **대화 상자에 답하기**).
  - 에이전트가 종료되었거나 같은 pane에 다른 세션이 뜬 경우에는 전달하지 않고 안내만 합니다.
- **알림:** PC에서 직접 시작한 에이전트도 알립니다.
  - 보고 있지 않은 탭에서 끝난 작업(`done`)을 알립니다.
  - `blocked` 상태가 되면 대화 상자를 버튼과 함께 보냅니다.
  - 스레드의 🔕 버튼으로 해당 에이전트 알림을 끌 수 있습니다. Slack에서 보낸 작업의 결과는 음소거와 상관없이 옵니다.
- **결과 본문:**
  - claude는 세션 JSONL에서 마지막 답변을 읽고, 읽지 못하면 화면에서 파싱합니다. codex는 화면 끝부분을 보냅니다.
  - 3000자가 넘으면 잘라서 보내고, **[View full]** 버튼을 누르면 전체 내용을 `.md` 파일로 올립니다.

### 대화 상자에 답하기

에이전트가 `blocked`가 되면 스레드에 **"⚠️ <이름> · <워크스페이스> is waiting for your answer"** 메시지가 옵니다. 화면의 대화 상자를 읽어 선택지마다 버튼을 붙입니다.

- **지원:** Claude 권한 확인(Bash 등), AskUserQuestion(단일 선택, 복수 선택, 여러 질문, 직접 입력), 계획 승인(ExitPlanMode), 시작할 때의 폴더 신뢰 확인(Claude, Codex), Codex 명령 승인.
- **버튼:** 선택지마다 `1. Yes` 같은 버튼이 있습니다. "항상 허용 / 다시 묻지 않기" 선택지도 그대로 버튼으로 나옵니다. 복수 선택은 ☐/☑ 버튼으로 켜고 끄고 **[Next →]**로 다음 탭으로 넘어갑니다. 항상 **[Esc]**와 **[Show screen]**(화면 끝 40줄을 스레드에 올림)이 있습니다.
- **직접 입력:** "Type something." / "Tell Claude what to change" 버튼을 누르면 입력 모달이 열립니다. 스레드에 답장해도 같은 답이 됩니다. 줄바꿈은 공백으로 바뀝니다. 직접 입력 선택지가 없는 질문에 답장하면 "This question needs one of the buttons above."라고 안내합니다.
- **계획 승인:** 계획 파일(`~\.claude\plans\…md`)을 읽어 본문을 보여 주고, 길면 **[View full]**로 전체를 올립니다.
- **안전장치:** 버튼을 누르면 먼저 에이전트와 화면을 다시 확인합니다. 그 사이 PC에서 답했거나 질문이 바뀌었으면 키를 보내지 않고 메시지만 갱신합니다. 두 번 눌러도 키는 한 번만 갑니다.
- **답한 뒤:** 다음 질문이 나오면 같은 메시지가 새 질문으로 바뀝니다. 에이전트가 계속 진행하면 `✅ <선택> — answered from Slack`으로 바뀌고 버튼이 사라집니다. PC에서 답하면 `✅ answered on PC`가 됩니다. 5초 안에 화면이 바뀌지 않으면 버튼을 남겨 두고 "Could not confirm the answer"를 알립니다.
- **화면을 읽지 못할 때:** `1`–`4`, `↑`, `↓`, Enter, Esc 키패드와 화면 끝 15줄을 보여 줍니다.
- **Slack에서 띄운 에이전트가 시작하자마자 폴더 신뢰를 물으면:** 스레드를 만들어 그 질문을 보내고, 답하면 원래 프롬프트를 보냅니다.
- **Codex:** `~/.codex/config.toml`에서 승인을 `approvals_reviewer = "auto_review"`로 넘기도록 설정했다면 Codex가 승인 화면을 띄우지 않으므로 Slack에도 오지 않습니다. 버그가 아니라 설정입니다.

### Home 탭

Slack 왼쪽 **앱** 목록에서 봇을 열고 **홈** 탭을 누르면 대시보드가 나옵니다.

- **맨 위:** 브리지 상태(uptime, 에이전트 수, 슬래시 명령, 갱신 시각)와 버튼 3개가 있습니다.
  - **[➕ New Agent]**: `/herdr new` 모달이 열립니다.
  - **[📤 Send]**: `/herdr send` 모달이 열립니다.
  - **[🔄 Refresh]**: 화면을 즉시 다시 그립니다.
- **아래:** 워크스페이스별 에이전트 목록(상태 이모지 · 이름/pane · 종류 · 상태 · 터미널 제목)이 있습니다. idle/done인 에이전트 줄의 **[Send]**를 누르면, 그 에이전트가 미리 선택된 보내기 모달이 열립니다.
- **자동 갱신:** 홈을 열 때마다 새로 그립니다. 한 번 연 뒤에는 에이전트 상태가 바뀔 때도 자동으로 갱신됩니다. 몰아서 최소 5초 간격으로 갱신합니다.
- **표시 한도:** 한 화면에 블록 100개까지 표시되고, 넘치면 "N more agents (see the `list` command)" 한 줄로 줄입니다.
- **소유자 전용:** 다른 사용자가 홈 탭을 열면 아무것도 게시하지 않습니다. 그 사람에게 에이전트 정보는 보이지 않습니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 슬래시 명령에 반응이 없음 | 1. `herdr-slack` 워크스페이스 pane의 출력을 봅니다.<br>2. `status` 액션을 실행합니다.<br>3. `STATE_DIR\bridge.log`를 확인합니다. |
| pane에 `missing settings in ...` | `.env`에 토큰 두 개(`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`)를 채운 뒤 `restart` 액션을 실행합니다. 마법사(`setup` 액션)를 써도 됩니다. |
| "🔗 This Herdr bridge is not paired yet" | 페어링 모드입니다. `herdr-slack` pane이나 Herdr 알림의 코드로 `/herdr pair <code>`를 보냅니다. |
| "❌ Wrong pairing code" / "⌛ That code expired" | pane에 표시된 **가장 최근** 코드를 씁니다. 5번 틀리거나 15분이 지나면 코드가 바뀝니다. |
| "⏳ Too many wrong codes from you" | 10분 안에 5번 틀렸습니다. 안내된 시간만큼 기다린 뒤 pane에 표시된 **가장 최근** 코드로 다시 보냅니다. |
| "You are paired, but the bridge could not start" | 계정은 저장되었지만 본인 전용 모드 시작이 실패했습니다. pane의 오류를 확인하고 `restart` 액션을 실행합니다. |
| `Slack connection failed: invalid_auth` | `xoxb`/`xapp` 토큰이 맞는지, 앱을 워크스페이스에 설치했는지, App-Level Token에 `connections:write`가 있는지 확인합니다. |
| `/herdr-...`가 "dispatch_failed" 또는 다른 앱이 응답 | 브리지가 떠 있는지 확인합니다. 같은 이름의 명령을 쓰는 다른 앱이 있는지도 확인합니다(이름은 워크스페이스 전체에서 유일해야 함). |
| "⛔ This Herdr bridge only accepts requests from its owner." | 다른 계정으로 페어링되어 있습니다. `.env`의 `SLACK_OWNER_USER_ID`를 비우고 `restart` 후 다시 페어링합니다. |
| DM 창에 입력이 안 됨 | App Home의 messages tab 설정을 확인합니다(수동 설정 2단계 6번). |
| `another bridge is running (pid N)` | 이미 실행 중입니다. `status` 액션으로 확인하고, 필요하면 `restart` 액션을 실행합니다. |
| 마법사가 "needs a terminal"이라며 멈춤 | 입력을 받을 수 없는 곳(플러그인 로그, 파이프)에서 실행했습니다. `setup` 액션을 쓰거나 터미널에서 실행합니다. |
| 플러그인 액션이 아무것도 안 하는 것 같음 | `herdr plugin log list --plugin herdr-slackbot`에서 stdout/stderr를 봅니다. |
| venv가 깨짐 | 플러그인 폴더의 `.venv`를 지우고 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`을 다시 실행합니다. |
| `cannot verify the running bridge` | 실행 중인 브리지를 확인할 수 없어 멈추지 않은 경우입니다. `herdr-slack` 워크스페이스의 브리지 pane에서 Ctrl+C로 직접 멈춥니다. |

## 보안

- **본인 전용:** 슬래시 명령, 버튼, 모달, DM 메시지 모두 `SLACK_OWNER_USER_ID` 한 명만 처리합니다. 다른 사용자가 명령을 쓰면 본인에게만 보이는 거절 메시지가 갑니다.
- **페어링:** 값이 비어 있으면 `/herdr pair <code>`만 받습니다. 코드는 PC 화면(브리지 pane, Herdr 알림)에만 표시되고 로그에는 남지 않습니다. 6자리 난수이고, 비교는 상수 시간으로 합니다. 모두 5번 틀리거나 15분이 지나면 바뀌고, 한 사람이 10분에 5번 넘게 시도할 수는 없습니다. 페어링하기 전에는 슬래시 명령 이름을 아는 같은 워크스페이스 사람도 `pair`를 시도할 수 있으니, 설치 직후 바로 페어링하세요.
- **코드 출력이 Slack으로 나갑니다.** 에이전트의 답변, 화면 끝부분, 작업 폴더 경로, 터미널 제목이 Slack DM에 올라갑니다. 민감한 저장소에서는 🔕 음소거를 쓰거나 브리지를 끄세요(`stop` 액션).
- Slack에서 새 에이전트를 띄울 때 고를 수 있는 권한 모드는 `manual`/`acceptEdits`/`auto`/`plan`뿐입니다. `bypassPermissions`는 없습니다.
- 토큰은 플러그인 설정 폴더의 `.env`에만 둡니다. 이 파일은 git 저장소 밖에 있고, 저장소의 `.gitignore`에도 `.env`가 들어 있습니다. 로그에 찍히는 `xoxb-`/`xapp-` 토큰은 가려집니다.
- Socket Mode는 PC에서 Slack으로 나가는 연결만 씁니다. 외부에서 들어오는 포트나 공개 URL이 없습니다.

## 개발

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest -q                       # 오프라인 테스트
$env:HERDR_LIVE_TESTS=1; .venv\Scripts\python -m pytest tests/test_live.py   # 실행 중인 Herdr 대상 (읽기 전용)
```

설계와 결정 사항은 `docs/SPEC.md`에, 마일스톤별 진행 기록은 `docs/progress/`에 있습니다.
