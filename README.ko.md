# herdr-slackbot

[English](README.md) | **한국어**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6)
![Herdr 0.8.2+](https://img.shields.io/badge/Herdr-0.8.2%2B-6f42c1)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)

**내 PC에서 돌고 있는 Claude Code / Codex 에이전트를 Slack에서 조종합니다.** 작업이 끝나면 DM이 오고, 권한 확인은 버튼으로 답하고, 다음 프롬프트도 휴대폰에서 보낼 수 있습니다.

## 왜 필요한가요

코딩 에이전트는 자주 멈춰서 기다립니다. 권한 확인, 질문, 계획 승인 같은 것들입니다. 자리를 비운 사이라면 돌아올 때까지 그대로 멈춰 있습니다. herdr-slackbot은 이런 에이전트를 Slack 봇 DM에 연결하는 [Herdr](https://herdr.dev) 플러그인이라, 어디서든 작업을 계속 진행시킬 수 있습니다.

- **알림:** 에이전트가 작업을 끝내거나(✅ done) 답을 기다릴 때(⚠️ blocked) DM으로 알려 줍니다. 결과 본문도 함께 옵니다.
- **Slack에서 답하기:** 권한 확인, 질문(AskUserQuestion), 계획 승인 같은 대화 상자가 **Slack 버튼**으로 옵니다.
- **원격으로 조종:** 슬래시 명령이나 Home 탭에서 에이전트 목록을 보고, 새 에이전트를 띄우고, 실행 중인 에이전트에 프롬프트를 보냅니다.
- **에이전트마다 스레드 하나:** 스레드에 답장하면 그 에이전트에게 프롬프트로 전달됩니다.
- **본인 전용:** 사용자마다 **자기 전용 Slack 앱**을 만들고, 봇은 **페어링한 본인 Slack 계정의 요청만** 처리합니다. Socket Mode라 공개 URL이나 포트 개방이 필요 없습니다.

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

마법사는 manifest가 미리 채워진 Slack 앱 만들기 화면을 열고, 토큰 두 개를 받아 Slack에 직접 확인한 뒤, 브리지를 시작하고 페어링까지 안내합니다. Ctrl+C로 언제든 멈출 수 있고, 다시 실행하면 이어서 진행합니다. 자세한 내용: [설정 마법사](docs/GUIDE.ko.md#설정-마법사). 직접 설정하려면 [수동 설정](docs/GUIDE.ko.md#수동-설정)을 보세요.

## 페어링

봇은 페어링한 Slack 계정의 요청만 받습니다. `.env`의 `SLACK_OWNER_USER_ID`가 비어 있으면(처음 시작할 때처럼) 브리지가 6자리 코드를 `herdr-slack` pane과 Herdr 알림으로 보여 줍니다. Slack 왼쪽 **앱** 목록에서 봇을 열고, DM 창에서 다음을 보냅니다.

```
/herdr-kim pair 123456
```

"paired ✅"가 오면 끝입니다. 재시작은 필요 없고, 봇이 DM으로 사용법을 보내 줍니다. 코드 만료, 시도 제한, 다른 계정으로 다시 페어링하는 방법은 [페어링 자세히](docs/GUIDE.ko.md#페어링-자세히)를 보세요.

## Slack에서 쓰기

아래에서 `/herdr`는 본인의 슬래시 명령을 뜻합니다(예: `/herdr-kim`). 봇 DM 창에서 사용합니다.

| 명령 | 동작 |
|---|---|
| `/herdr list` | 에이전트 목록 (워크스페이스별, 상태 이모지) |
| `/herdr new` | 모달로 새 에이전트 시작: 워크스페이스, cwd, claude/codex, 모델, effort, 권한 모드, 이름, 프롬프트 |
| `/herdr send` | idle 상태인 에이전트에 프롬프트 전송. 모달에 지금까지의 대화가 표시됩니다. |
| `/herdr status` | 브리지 상태 |
| `/herdr` | 사용법 |

- **스레드:** 에이전트마다 DM 스레드가 하나씩 생깁니다. 스레드에 답장하면 그 에이전트에게 프롬프트로 전달되고, 에이전트가 질문에 답을 기다리는 중이면 그 질문의 답이 됩니다.
- **알림:** PC에서 직접 시작한 에이전트도 알립니다. 보고 있지 않은 탭에서 끝난 작업과 모든 대화 상자가 옵니다. 🔕 버튼으로 에이전트별 알림을 끌 수 있습니다.
- **대화 상자:** Claude 권한 확인, AskUserQuestion(단일·복수 선택, 직접 입력), 계획 승인, 폴더 신뢰 확인, Codex 명령 승인이 모두 버튼으로 옵니다. 버튼을 누르면 먼저 화면을 다시 확인하므로, PC에서 이미 답했다면 키를 보내지 않습니다.
- **Home 탭:** 모든 에이전트를 보여 주는 대시보드입니다. New Agent / Send / Refresh 버튼이 있습니다.

전체 명령, 결과 본문 규칙, 대화 상자 상세는 [사용 가이드](docs/GUIDE.ko.md#slack에서-쓰기)를 보세요.

## 보안

- **본인 전용:** 슬래시 명령, 버튼, 모달, DM 메시지 모두 `SLACK_OWNER_USER_ID` 한 명만 처리합니다. 다른 사용자가 명령을 쓰면 본인에게만 보이는 거절 메시지가 갑니다.
- **페어링:** 값이 비어 있으면 `/herdr pair <code>`만 받습니다. 코드는 PC 화면(브리지 pane, Herdr 알림)에만 표시되고 로그에는 남지 않습니다. 6자리 난수이고, 비교는 상수 시간으로 합니다. 모두 5번 틀리거나 15분이 지나면 바뀌고, 한 사람이 10분에 5번 넘게 시도할 수는 없습니다. 페어링하기 전에는 슬래시 명령 이름을 아는 같은 워크스페이스 사람도 `pair`를 시도할 수 있으니, 설치 직후 바로 페어링하세요.
- **코드 출력이 Slack으로 나갑니다.** 에이전트의 답변, 화면 끝부분, 작업 폴더 경로, 터미널 제목이 Slack DM에 올라갑니다. 민감한 저장소에서는 🔕 음소거를 쓰거나 브리지를 끄세요(`stop` 액션).
- Slack에서 새 에이전트를 띄울 때 고를 수 있는 권한 모드는 `manual`/`acceptEdits`/`auto`/`plan`뿐입니다. `bypassPermissions`는 없습니다.
- 토큰은 플러그인 설정 폴더의 `.env`에만 둡니다. 이 파일은 git 저장소 밖에 있고, 저장소의 `.gitignore`에도 `.env`가 들어 있습니다. 로그에 찍히는 `xoxb-`/`xapp-` 토큰은 가려집니다.
- Socket Mode는 PC에서 Slack으로 나가는 연결만 씁니다. 외부에서 들어오는 포트나 공개 URL이 없습니다.

## 문서

- [사용 가이드](docs/GUIDE.ko.md): 설정 마법사, 페어링 상세, 수동 설정, 시작 / 재시작, Slack 사용법 전체, 문제 해결
- [docs/LIVE_TEST.md](docs/LIVE_TEST.md): 실제 Slack으로 처음 돌려 볼 때의 체크리스트
- [docs/SPEC.md](docs/SPEC.md): 설계와 결정 사항

## 개발

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest -q                       # 오프라인 테스트
$env:HERDR_LIVE_TESTS=1; .venv\Scripts\python -m pytest tests/test_live.py   # 실행 중인 Herdr 대상 (읽기 전용)
```

설계와 결정 사항은 `docs/SPEC.md`에, 마일스톤별 진행 기록은 `docs/progress/`에 있습니다.

## 라이선스

[MIT](LICENSE)
