<p align="center">
  <img src="docs/assets/persona-vault-icon.svg" width="64" height="64" alt="PersonaVault 아이콘: 호박색·테라코타·가넷색 퇴적층 돌 세 개를 쌓은 케언과, 그 위에 놓인 원시 증거 알갱이 두 개">
</p>

# PersonaVault Gateway

**에이전트는 바뀌어도, 지식은 남습니다.**

PersonaVault는 에이전트나 실행 환경에 종속되지 않는, 사용자 소유의 지식 저장소입니다.
서로 다른 PC와 연결된 AI 에이전트가 같은 Markdown Vault를 검색하고 기록합니다.
도구를 바꿔도 쌓아 온 맥락은 남고, 무엇을 지식으로 삼을지는 사용자가 검토하고 승인합니다.
Obsidian, VS Code 등 Markdown 편집기는 자유롭게 선택할 수 있습니다.

[English](README.md) | **한국어**

[시작하기](#시작하기) · [동작 방식](#동작-방식) · [데이터와 통제](#데이터와-통제)

![PersonaVault 컨셉 일러스트: "Agents come and go. Your knowledge settles." 에이전트·기기·도구가 남긴 원시 증거 알갱이가 "you review" 선 위에 머물고, 그 아래에는 사용자가 남기기로 한 지식(decisions, context, preferences, lessons)이 따뜻한 색의 퇴적층 돌 케언으로 쌓여 있습니다. 이 지식은 평범한 Markdown과 Git에 보관되며 연결된 에이전트가 불러올 수 있습니다. 그림 속 문구는 영어입니다.](docs/assets/persona-vault-overview.svg)

> **Self-hosted 베타.** 개인 self-hosting용입니다. 보안 점검과 공개 전 QA가 끝나지 않았으니
> 먼저 [데이터와 통제](#데이터와-통제)와 [SECURITY.ko.md](SECURITY.ko.md)를 읽으세요.

## 왜 PersonaVault인가

- **에이전트마다 흩어지지 않는 지식.** 어느 PC에서든 연결된 에이전트가 같은 이전 세션과 메모를
  검색하므로 에이전트나 기기를 바꿔도 프로젝트를 다시 설명하지 않아도 됩니다.
- **결정의 이유를 되찾습니다.** `evidence` view는 결정 뒤에 있는 raw 대화를 돌려주고, `history`와
  `conflicts`는 결정이 어떻게 바뀌었는지 보여 줍니다.
- **raw 대화를 승인된 지식으로 만듭니다.** 세션은 raw Markdown으로 쌓이고, 무엇을 지식으로 삼을지는
  사람이 정합니다. 자동 승격은 없습니다.
- **일반 Markdown을 소유합니다.** Vault는 private Git 저장소입니다. metadata와 파일 배치가 계약이라
  특정 편집기나 에이전트에 묶이지 않습니다.

## 이렇게 씁니다

연결된 에이전트에게 하는 요청 예시입니다.

```text
"지난달에 retry 정책을 왜 바꿨지? evidence로 확인해 줘."
"현재 token rotation 절차가 뭐야?"
"이번 디버깅에서 배운 점을 procedure 메모로 남겨 줘."
```

## 시작하기

### 1. Gateway를 서버에 한 번 실행

Docker Compose(또는 [docs/hosting.ko.md](docs/hosting.ko.md)의 플랫폼)와 commit이 하나 이상 있는 GitHub private
Vault 저장소가 필요합니다. 기본 배포는 keyword 검색입니다. 소스에서 빌드하려면
`docker compose -f compose.yml -f compose.build.yml up -d --build`를 실행하세요.

Docker 명령은 **서버**에서 실행합니다. 새 설치는 전용 영구 디렉토리에서 하세요(Compose project와 volume 식별이 디렉토리에 달려 있어 업그레이드 때도 유지합니다).

```bash
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml.sha256
sha256sum -c compose.yml.sha256            # macOS: shasum -a 256 -c
docker compose up -d
docker compose logs                        # 일회용 setup code
```

Compose는 기본적으로 포트를 서버의 `127.0.0.1`에만 바인딩합니다. 다른 PC에서는 먼저 SSH tunnel을 열고, 나중에 plugin을 쓸 때도
계속 실행해 두세요(`user`는 서버 로그인 이름, `SERVER_IP`는 서버 주소로 바꿉니다).

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:18080 user@SERVER_IP
```

그러면 `http://127.0.0.1:18080`이 Gateway URL입니다. 서버가 이 PC라면 tunnel은 필요 없고, 호스팅 서비스나 이미 구성된 암호화
주소는 해당 HTTPS URL을 씁니다.

Gateway URL의 `/setup`(예: `http://127.0.0.1:18080/setup`)을 열어 setup code로 claim하고 admin 비밀번호를 정한 뒤 GitHub 저장소 SSH URL을
입력하고, 화면의 **public** deploy key를 Vault 저장소에 쓰기 권한으로 등록합니다. 그다음 연결(필요하면
재시도)하고 agent token을 발급합니다. 설정이 끝나기 전에는 `/healthz`만 200이고 `/readyz`는 503이며 검색과
capture는 꺼져 있습니다. 연결 단계는 쓰기 권한을 증명하지 못합니다.
단계별 안내는 [docs/setup.ko.md](docs/setup.ko.md)입니다. Docker, Railway, Render 모두 같은 방식으로 설치하며 Railway·Render
설정은 준비만 되었고 실제 계정에서 검증하지 않았습니다([docs/hosting.ko.md](docs/hosting.ko.md)). 업그레이드·백업·
semantic 검색은 [docs/operations.ko.md](docs/operations.ko.md)입니다.

다른 PC에서는 암호화된 경로로만 접근하세요. 평문 공개 HTTP는 agent token과 admin 비밀번호를 노출합니다.

### 2. Plugin 설치 (각 PC)

현재 지원하는 에이전트 플랫폼은 Codex와 Claude Code이며, 아래 공용 plugin으로 연결합니다.
[Custom GPT Actions](docs/gpt-actions.ko.md)는 대체 경로입니다. 그 밖의 client는 Gateway API와의 연동을
직접 구현해야 하며, 자동 수집은 현재 제공되는 plugin에서만 됩니다.

hook과 token helper에는 Node.js가 필요합니다.

```text
# Claude Code
/plugin marketplace add mykim0409/persona-vault-gateway
/plugin install persona-vault@persona-vault-gateway
/reload-plugins
```

```bash
# Codex
codex plugin marketplace add mykim0409/persona-vault-gateway
codex plugin add persona-vault --marketplace persona-vault-gateway
```

hook 명령을 확인한 뒤에 신뢰하세요(`/hooks`). 그다음 token helper를 설치하고 Gateway URL과 token을
[docs/setup.ko.md](docs/setup.ko.md) 3절대로 입력합니다. Windows PowerShell 방법도 거기에 있습니다.
token을 에이전트 대화에 붙여 넣지 마세요.

### 3. 사용해 보기

```bash
printf '%s\n' "For this project, record the reason for deployment decisions." \
  | "$HOME/.local/bin/pvg-agent-memo" --title "Deployment notes" --project Example
"$HOME/.local/bin/pvg-rag-search" --view evidence "deployment decisions"
```

raw note(승인된 지식 아님)를 저장하고 다시 찾습니다. 새 Vault에서는 `--view current`가 비어 있을 수 있습니다.
Windows PowerShell 버전: [docs/setup.ko.md](docs/setup.ko.md).

## 동작 방식

1. **기록.** hook이 세션을 Gateway로 보내고, Gateway는 `30_Conversations/raw/` 아래에만 씁니다.
2. **승인.** 사람이 raw 대화를 knowledge로 정리합니다. 선택 사항인 Curator(`pvg-wiki`)는 계획을
   제안하는 실험적 로컬 CLI이며 스스로 apply·commit·push하지 않습니다. 백그라운드 서비스도
   아닙니다.
3. **검색.** keyword 검색과, 사용할 수 있을 때의 semantic 검색이 `answer_state`와 함께 결과를
   돌려줍니다. `answer_state`는 신호일 뿐 사실을 보증하지 않습니다.

제품의 핵심은 기록, 일반 Markdown, keyword 검색입니다. semantic 검색은 그 위에 얹는 선택적 강화
기능이고, Curator는 별도의 선택 도구입니다. 아래 표는 현재 무엇이 각각을 구현하는지 보여 줍니다.

| 계층 | 현재 구현 |
| --- | --- |
| 지식 저장소 | Markdown 파일의 private Git 저장소. Gateway가 직접 동기화 |
| Gateway | FastAPI와 SQLite: API, auth, 경로 정책, Markdown writer, admin |
| semantic 검색 (선택) | Qdrant, embedding은 Cloudflare Workers AI REST API |
| Curator (선택) | `pvg-wiki`, 실험적 로컬 CLI |

Gateway는 기본이 keyword 전용 검색(`EMBEDDING_PROVIDER=none`)이며 embedding provider나 Qdrant를
호출하지 않습니다. semantic 검색은 `EMBEDDING_PROVIDER=cloudflare`, Cloudflare 자격 증명, Qdrant 서비스를
**모두** 갖춰야 켜집니다. 직접 실행하는 Python 런타임은 기존 연동 호환을 위해
`EMBEDDING_PROVIDER`가 없으면 여전히 `cloudflare`가 기본이므로, keyword 전용 Gateway에 대한 CLI
`compact-finish` 등에서는 `none`처럼 명시적으로 설정하세요.

Cloudflare Workers AI는 현재 유일한 production embedding 경로입니다. REST로 직접 호출하므로 별도
Cloudflare Worker를 개발·배포할 필요가 없습니다. 다른 provider는 아직 구현되지 않았고, 코드의 hash
embedding은 테스트 전용입니다. [docs/operations.ko.md](docs/operations.ko.md)를 보세요.

## 데이터와 통제

- **자동 수집은 프라이버시 경계가 아닙니다.** hook은 사용자의 요청, main agent의 최종 응답, subagent 위임과
  결과를 보냅니다. 독립된 tool·reasoning block은 제외하지만 사용자 메시지와 최종 응답은 수집됩니다.
  hook은 새 기록을 로컬에 저장하기 전, 그리고 전송할 때마다 결정적 로컬 필터(추가 LLM 호출·새 의존성
  없음)를 적용합니다. 흔한 credential(key/value·JSON 필드, Bearer/Basic 헤더, URL 비밀번호, private
  key, 대표적 token 형식)을 가리고, 인식된 fenced 코드·설정·env·diff·log block(과 명백한 fence 없는
  diff/env/log 덩어리)을 내용 없는 `[omitted <kind> block, N lines]` 표시로 바꾸며, 작업 디렉터리(cwd)
  메타데이터는 절대 경로 대신 프로젝트 디렉터리 이름만 남깁니다(메시지 본문에 적은 경로는 일반적으로
  지우지 않습니다). 휴리스틱이며 DLP 보장이 아닙니다. 문장 속 코드나 인식하지 못한
  형식은 남을 수 있고, 기존 spool 데이터는 소급해서 지우지 않습니다(대기 중인 기존 기록은 전송 시에만
  걸러지고 로컬 파일 원문은 그대로입니다). 보호 대상은 hook뿐이며 직접 API 호출, 수동 작성한 Vault 파일,
  Cloudflare embedding 입력은 포함되지 않습니다. 수집되는 대화에 credential, private key, 기밀 소스
  코드, 그 밖의 민감한 데이터를 붙여 넣지 마세요.
- **수집한 텍스트는 평문으로 저장되고 Git에 남습니다.** 로컬 spool은 평문 JSONL이며 Gateway로
  그 내용이 전송됩니다. Vault는 Git으로 동기화되므로 나중에 파일을 지워도 이력은 지워지지 않습니다.
- **read token은 Vault 전체를 읽습니다.** `.git/`, `.obsidian/`, `.tmp/`를 뺀 모든 Markdown이며
  `90_Private/`도 포함됩니다. 쓰기는 `30_Conversations/raw/`로 제한됩니다.
- **semantic 검색을 쓰면 텍스트가 서버 밖으로 나갑니다.** 직접 켠 경우에만 색인 chunk(`90_Private/`
  포함)와 검색 query가 embedding을 위해 Cloudflare Workers AI로 전송됩니다.
  [Cloudflare 데이터 정책](https://developers.cloudflare.com/workers-ai/platform/data-usage/)을
  보세요.
- **Admin login 제한은 네트워크 보안이 아닙니다.** client 주소당 5분에 5회까지 허용하고 초과하면 `429`와
  `Retry-After`를 반환합니다. 프로세스 메모리에서만 동작하며 추적 수가 제한되고, 재시작하면 초기화되며
  프로세스 간에 공유되지 않습니다. forwarded 헤더는 정확히 신뢰하도록 설정한 proxy에서만 반영되고(wildcard
  금지) CSRF 보호는 그대로입니다. 암호화된 경로나 TLS와 접근 제어 뒤에서 비공개로 운영하세요.
- **정리에는 사람이 필요합니다.** 모든 plan은 사람이 승인하며 raw 삭제의 원자성은 보장되지
  않습니다. [Curator protocol](docs/CURATOR.ko.md)을 보세요.

이 베타는 보안 점검을 마치지 않았습니다. [SECURITY.ko.md](SECURITY.ko.md)를 보세요.

## 문서

| 문서 | 내용 |
| --- | --- |
| [docs/setup.ko.md](docs/setup.ko.md) | 최초 설치, plugin, token helper |
| [docs/hosting.ko.md](docs/hosting.ko.md) | 호스팅 선택지: Compose, Railway, Render, 기타 |
| [docs/operations.ko.md](docs/operations.ko.md) | Vault 구조, semantic 검색, 원격 접근, 업그레이드, 백업 |
| [Windows 안내](plugins/persona-vault/skills/persona-vault/references/windows.md) | Windows 명령 문법 |
| [docs/gpt-actions.ko.md](docs/gpt-actions.ko.md) | plugin 대신 쓰는 Custom GPT Actions |
| [docs/metadata.ko.md](docs/metadata.ko.md) | Markdown metadata 계약 |
| [docs/CURATOR.ko.md](docs/CURATOR.ko.md) | Curator protocol |
| [CONTRIBUTING.ko.md](CONTRIBUTING.ko.md), [SECURITY.ko.md](SECURITY.ko.md) | 개발과 보안 정책 |

[MIT License](LICENSE)로 배포됩니다.
