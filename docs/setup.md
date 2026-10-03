# PersonaVault Gateway 설치

이 문서는 public gateway repo 기준의 서버 bootstrap입니다. Vault는 일반 Markdown 파일의 Git
저장소이며, Obsidian은 그 Vault를 편집하는 선택 도구 중 하나입니다.

```text
Markdown 편집기 + Git client -> vault repo
gateway container -> Docker volume에 Markdown 기록
qdrant container -> vault Markdown embedding index 저장
vault-sync sidecar -> vault repo에 pull/push
```

## Vault 규칙

| 디렉토리 | 용도 | 수정 주체 |
| --- | --- | --- |
| `00_Inbox/` | 아직 분류하지 않은 메모, 임시 아이디어 | 사람 |
| `10_User/` | 전역 협업 규칙과 필요할 때 검색할 사용자 세부 기록 | 사람 또는 승인받은 Curator |
| `20_Projects/` | 프로젝트별 목표, 결정, 진행 상황 | 사람 |
| `30_Conversations/raw/YYYY/MM/DD/` | 현지 날짜별로 나눈 agent 원본 대화 로그 | agent |
| `30_Conversations/summaries/` | 요청, 결정, 기각한 대안, 미해결 질문처럼 대화 흐름이 중요한 요약 | 사람 또는 Curator |
| `40_Agents/<agent_id>/` | 이전 버전 agent memo의 legacy curation input | legacy read-only |
| `50_Knowledge/` | 프로젝트를 넘어 재사용하는 검증된 지식과 reference | 사람 또는 Curator |
| `90_Private/` | 개인적 맥락과 메모 | 사람 |
| `.obsidian/` | 선택. Obsidian을 쓸 때만 생기는 설정, 플러그인 설정. 다른 편집기를 쓰면 필요 없음 | Obsidian |

Gateway v3의 모든 write는 `30_Conversations/raw/`에만 저장됩니다. 대화와 note는
`kind=conversation|note`로 구분하며, note는 `note_type=observation|proposal|handoff`와
`note_kind`를 사용합니다. Gateway는 새 `40_Agents/` 파일을 만들지 않고 기존 파일만
legacy curation input으로 읽습니다.
동일한 agent 세션도 날짜가 바뀌면 별도 raw Markdown으로 저장하며 `session_id`로 연결합니다.
`20_Projects/`, `30_Conversations/summaries/`, `50_Knowledge/` 같은
검토된 목적지는 agent token의 allowed root로 등록할 수 없습니다.
기존 `30_Conversations/important/` 문서는 호환을 위해 읽지만 새로 만들지 않습니다.
RAG는 `vault-rag` scope가 있는 token으로 전체 Markdown vault를 읽고, `.git/`, `.obsidian/`, `.tmp/`는 색인하지 않습니다.
따라서 `90_Private/`도 검색 대상이며, 이 이름은 개인 주제를 뜻할 뿐 별도 읽기 보안 영역을 뜻하지 않습니다.

## 0. 사전 준비: Vault repo

이미 vault repo가 있으면 clone만 합니다.

```bash
git clone git@github.com:OWNER/persona-vault.git ~/Documents/persona-vault
cd ~/Documents/persona-vault
```

처음 만드는 vault repo라면 빈 repo를 만든 뒤 같은 방식으로 clone합니다.

```bash
git clone git@github.com:OWNER/persona-vault.git ~/Documents/persona-vault
cd ~/Documents/persona-vault
```

공통으로 기본 디렉토리를 보강합니다.

```bash
mkdir -p 00_Inbox 10_User 20_Projects \
  30_Conversations/raw 30_Conversations/summaries \
  50_Knowledge 90_Private
# Do not overwrite an existing 10_User/WORKING_AGREEMENT.md (download only if it is missing).
test -e 10_User/WORKING_AGREEMENT.md || curl -fsSLo 10_User/WORKING_AGREEMENT.md \
  https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/docs/WORKING_AGREEMENT.md
touch 30_Conversations/raw/.gitkeep
for p in .obsidian/ .tmp/; do
  grep -qxF "$p" .gitignore 2>/dev/null || printf '%s\n' "$p" >> .gitignore
done
git ls-files -- .obsidian .tmp   # 이미 추적 중인 파일이 있으면 아래 설명을 먼저 읽습니다
git add .
git diff --cached --quiet || git commit -m "Initialize vault structure"
git push
```

`.tmp/`는 Curator의 plan·draft·checkpoint 작업 공간이고 `.obsidian/`에는 plugin 설정과 workspace 상태가
있으므로 private Vault라도 commit하지 않는 것을 권장합니다(설정을 의도적으로 공유한다면 `.obsidian/`은 선택).
Obsidian을 쓰지 않아도 `.obsidian/` 제외 항목은 그대로 두어도 됩니다.
서버의 sync sidecar가 주기적으로 `git add .`를 실행하므로 첫 실행 전에 `.gitignore`를 push해 두세요.
`.gitignore`는 아직 추적되지 않은 파일만 제외하므로 이미 commit된 `.obsidian/`·`.tmp/` 파일은 계속 추적됩니다.
위 `git ls-files`가 무언가를 출력하면 내용을 확인한 뒤 직접 `git rm -r --cached --ignore-unmatch -- .obsidian .tmp`로
추적만 해제하고 commit하세요. 이 문서의 명령은 추적 중인 파일을 자동으로 지우지 않으며, `--cached`는
로컬 파일을 삭제하지 않지만 이미 push한 과거 commit에는 내용이 남습니다. private 내용을 history에서
지우는 일은 별도로 판단합니다.

Curator를 사용하려면 [CURATOR.md 템플릿](CURATOR.md)을 이름 변경 없이 Vault
root에 둡니다. 기존 파일은 protocol v22 템플릿으로 교체하고 과거 절차를 병합하지 않습니다.
사용자 협업 규칙은 `10_User/WORKING_AGREEMENT.md`, 프로젝트별 규칙은 해당 프로젝트 문서에 둡니다.

`10_User/WORKING_AGREEMENT.md`에는 모든 세션에 적용할 사람이 승인한 현재 규칙만
간결하게 둡니다. 사용자 세부 기록은 `10_User/`의 다른 Markdown에 나눌 수 있지만
SessionStart가 자동으로 읽는 문서는 `WORKING_AGREEMENT.md` 하나뿐입니다. 기존
`10_People/`는 자동으로 이동하거나 삭제하지 않으며 남아 있어도 RAG 검색은 계속됩니다.

## 1. 로컬 편집과 Git 동기화

Vault repo는 어떤 편집기를 쓰든 필요합니다. 편집기는 `~/Documents/persona-vault`를 열고, 변경은
Git client로 vault repo에 pull/push합니다. Obsidian, VS Code 등 어떤 Markdown 편집기든 되며
교체할 수 있습니다. 계약은 편집기가 아니라 Vault 규칙과 [metadata](metadata.md)입니다.
gateway repo는 편집기에서 Vault로 열지 않습니다.

권장 동작은 편집기와 무관합니다. 시작할 때 pull하고, push 전에 pull하며(rebase), 주기적으로
commit·push합니다. 충돌이 나면 pull한 뒤 해결하고 다시 push합니다.

운영 규칙:

| 대상 | 규칙 |
| --- | --- |
| `00_Inbox/`, `10_User/`, `20_Projects/`, `30_Conversations/summaries/`, `50_Knowledge/`, `90_Private/` | 편집기에서 수정 |
| `30_Conversations/raw/`와 기존 `40_Agents/<agent_id>/` | 직접 수정하지 않고 curation input으로만 읽음 |
| 충돌 발생 시 | pull 후 다시 commit·push |

Desktop에서는 SSH remote를 권장합니다.

### Obsidian을 쓰는 경우 (선택)

Obsidian을 쓰지 않아도 Vault repo 준비(0절)는 건너뛰지 않습니다. Obsidian은 위 clone한
`~/Documents/persona-vault`를 vault로 엽니다. Obsidian Git plugin을 쓰면 pull/push를 자동화할 수
있습니다.

권장 동작:

| 설정 | 권장값 |
| --- | --- |
| Auto pull on startup | 켬 |
| Auto commit-and-sync interval | 10분 |
| Commit message | `vault: obsidian sync` |
| Pull before push | 켬 |
| Merge strategy | rebase |

충돌이 나면 Obsidian Git에서 pull 후 다시 commit-and-sync합니다.
Mobile은 Obsidian Git의 Git 구현 제약이 커서 이 문서의 기본 운영 대상에서 제외합니다.

## 2. 서버 준비

필요한 도구:

```bash
docker compose version
git --version
command -v ssh-keygen
```

```bash
git clone https://github.com/mykim0409/persona-vault-gateway.git ~/persona-vault-gateway
cd ~/persona-vault-gateway
test -e .env || install -m 600 .env.example .env
chmod 600 .env
```

Cloudflare dashboard의 `Workers AI` -> `Use REST API`에서 Account ID를 복사하고
`Create a Workers AI API Token`으로 token을 만듭니다. custom token을 직접 구성한다면
account의 `Workers AI - Read`, `Workers AI - Edit` 권한이 모두 필요합니다. token은
아래 `.env`에만 저장하고 Git에는 올리지 않습니다.

현재 semantic 구현은 이 Account ID와 token으로 Cloudflare Workers AI REST API
(`api.cloudflare.com`)를 직접 호출합니다. 별도 Cloudflare Worker를 개발하거나 배포할 필요가
없습니다. 다른 embedding provider로의 이식은 아직 구현되지 않았고, 제공 compose는 Account ID와
token이 없으면 시작하지 않습니다.

`.env`를 수정합니다.

```text
VAULT_REPO_SSH_URL=git@github.com:OWNER/persona-vault.git
HOST_ID=persona-vault-gateway
ADMIN_PASSWORD=<long-random-password>
GATEWAY_BIND_ADDR=127.0.0.1
GATEWAY_HOST_PORT=18080
VAULT_SYNC_INTERVAL_SECONDS=300
CLOUDFLARE_ACCOUNT_ID=<cloudflare-account-id>
CLOUDFLARE_API_TOKEN=<workers-ai-api-token>
EMBEDDING_PROVIDER=cloudflare
EMBEDDING_MODEL=@cf/qwen/qwen3-embedding-0.6b
EMBEDDING_BATCH_SIZE=32
QDRANT_COLLECTION=persona_vault
```

vault sync용 deploy key를 만듭니다.

```bash
mkdir -p secrets && chmod 700 secrets
test -f secrets/persona_vault_sync || ssh-keygen -t ed25519 -C "persona-vault-sync" -N "" -f secrets/persona_vault_sync
chmod 600 secrets/persona_vault_sync
cat secrets/persona_vault_sync.pub
```

`secrets/` 디렉토리는 700, private key와 `.env`는 600이어야 합니다. 둘 다 Git에 올리지 않습니다.

GitHub host key는 `ssh-keyscan` 출력을 그대로 신뢰하지 않습니다. 같은 네트워크 경로가 보낸 값이라
검증 전에는 후보일 뿐입니다. 이미 검증해 둔 `secrets/github_known_hosts`가 있으면 이 단계는 건너뜁니다.

```bash
ssh-keyscan -t ed25519 github.com > secrets/github_known_hosts.candidate
ssh-keygen -lf secrets/github_known_hosts.candidate -E sha256
```

출력된 `SHA256:` fingerprint를 GitHub 공식 문서
[GitHub's SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)의
Ed25519 값과 직접 비교합니다. 완전히 같을 때만 다음 명령으로 사용하고, 다르면 후보 파일을 지우고
네트워크를 바꿔 다시 확인하세요.

```bash
mv secrets/github_known_hosts.candidate secrets/github_known_hosts
```

출력된 public key를 vault repo에 등록합니다.

```text
GitHub vault repo
-> Settings
-> Deploy keys
-> Add deploy key
-> Allow write access
```

## 3. 실행

```bash
sudo docker compose up -d --build
sudo docker logs --tail=50 persona-vault-qdrant
sudo docker logs --tail=50 persona-vault-sync
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
```

Gateway는 sync 서비스가 **최초 clone/checkout을 끝낸 뒤**(sync 컨테이너의 `/tmp/pv-sync-clone-ready`
marker로 판단하는 Compose `service_healthy` 의존성)에만 시작합니다. clone이 실패하면(deploy key 미등록,
host key 불일치 등) sync가 재시작을 반복하고 Gateway는 시작하지 않으니 `docker logs persona-vault-sync`를
확인하세요. 이는 최초 clone 준비 상태이며 이후 Git sync의 원자성을 보장하지 않습니다.

Vault가 커서 최초 clone이 시작 대기 시간(sync healthcheck의 `start_period` 120초와 2초 간격 30회 재시도,
합쳐 약 3분)을 넘기면 `docker compose up`이 sync가 unhealthy라는 오류로 먼저 멈출 수 있습니다. 이때
clone은 컨테이너 안에서 계속 진행 중일 수 있으며 Gateway는 아직 시작하지 않은 상태입니다. 다음으로 진행
상황을 확인합니다.

```bash
sudo docker compose ps
sudo docker logs --tail=50 persona-vault-sync
sudo docker top persona-vault-sync   # git clone 프로세스가 보이면 아직 진행 중
sudo docker inspect -f '{{.State.Health.Status}}' persona-vault-sync
```

clone이 진행 중이면 기다립니다. volume을 삭제하거나 sync 컨테이너를 재시작하지 마세요. 진행 중인 clone이
중단됩니다. 로그에 인증이나 host key 오류가 있으면 clone이 진행 중인 것이 아니므로 deploy key와
`known_hosts` 설정을 고친 뒤 다시 실행합니다. sync가 `healthy`가 되면 `sudo docker compose up -d`를
다시 실행해 Gateway를 시작합니다.
처음에는 색인이 없으므로 `/readyz` 응답의 `rag_indexed`가 `false`입니다. Qdrant에 연결할 수 없거나
색인이 현재 Vault와 맞지 않으면 `503`이며, 초기 설치에서는 아래 admin 화면의 `Update RAG index`를
한 번 실행한 뒤 `rag_indexed`가 `true`가 되는지 확인합니다.

브라우저에서 `<GATEWAY_URL>/admin/login`에 접속하고 `ADMIN_PASSWORD`로 로그인합니다.
`/admin/tokens`에서 agent token 생성, rotate, disable을 관리합니다. token은 생성 시 한 번만 표시됩니다.
Admin의 모든 변경 form은 CSRF token을 요구합니다. 이미 token이 있는 agent ID로 생성하거나 rotate·disable할 때는
확인 화면에서 한 번 더 승인해야 합니다. Login cookie는 요청이 HTTPS로 판단될 때 `Secure`가 붙으며,
reverse proxy 뒤에서는 Uvicorn의 trusted proxy 설정(`--forwarded-allow-ips` 또는 `FORWARDED_ALLOW_IPS`)이
해당 proxy를 신뢰해야 `X-Forwarded-Proto`가 반영됩니다. Admin에는 login rate limit이 없고 공개 인터넷용으로
강화되지 않았습니다. 기존 reverse proxy에서 접근을 제한하고(IP·VPN allowlist 등) TLS를 적용하세요.
이 저장소는 새 proxy 구성 요소를 추가하지 않습니다.
일반 plugin은 `Read + Write`, 검색 전용 client는 `Read`, 수집·memo 전용 client는
`Write`를 선택합니다. 목록에는 Gateway가 변환한 내부 scope가 표시됩니다.
일반 검색(`refresh=false`)은 색인을 시작하지 않습니다. 현재 Vault와 일치하는 완성된
semantic index를 사용하며, index가 없거나 갱신 중이거나 검색 서비스에 장애가
있으면 현재 Markdown의 keyword 검색으로 응답합니다.
응답의 `index.stale`이 `true`이거나 최신 문서의 semantic 검색이 필요할 때 admin 화면에서
`Update RAG index`를 실행합니다. 갱신 전에도 변경된 문서는 keyword 검색으로 찾고, 변경되지 않아
기존 index와 호환되는 문서의 semantic 후보는 현재 문서 hash·metadata를 적용해 사용할 수 있습니다.
`index.stale`은 index를 갱신할 때까지 `true`로 남습니다. 초기 설치에서는 semantic 검색을 위해 한 번 실행합니다. 이후에는
전체 Markdown을 다시 청킹하되 입력 hash가 바뀐 청크만 embedding하고, 삭제된 청크는
Qdrant에서 제거합니다. 변경 파일 하나가 checkpoint 단위이며 파일 내부 청크는 batch로
전송됩니다. 실패 후 재실행하면 완료 파일은 건너뛰고, 동시에 두 update는 실행하지 않습니다.
provider·모델·차원이 달라진 경우에만 전체 index를 다시 만듭니다. 색인 schema만 바뀌고
vector 조건이 같으면 기존 vector를 유지한 채 metadata를 승격합니다.
embedding은 Cloudflare Workers AI의 `@cf/qwen/qwen3-embedding-0.6b`를 사용하며
Qdrant index를 1024차원으로 생성합니다. 기존 다른 모델의 index가 있다면 첫 갱신이
기존 vector를 대체합니다. Cloudflare 사용 한도를 초과하면 Gateway는 reset 시각까지
추가 embedding 요청을 중단하고, 일반 검색은 keyword-only로 계속 제공합니다. reset 이후
1분이 지나면 미처리 파일을 자동 재시도하므로 별도 update나 cron이 필요하지 않습니다.
`90_Private/`를 포함한 색인 대상 Markdown chunk는 embedding을 위해 Cloudflare로 전송됩니다.
[Cloudflare Workers AI 데이터 정책](https://developers.cloudflare.com/workers-ai/platform/data-usage/)에 따르면
고객 콘텐츠는 모델 학습이나 Cloudflare·제3자 서비스 개선에 사용되지 않습니다.
`GET /gateway/v3/health`는 같은 scope로 broken reference, unresolved conflict,
stale summary, alias와 repository source 문제를 읽기 전용으로 확인합니다.
기존 SQLite DB는 Gateway 시작 시 `PRAGMA user_version` 기반으로 자동 migration되며
token과 audit 데이터는 유지됩니다. 더 새로운 schema의 DB는 구버전 Gateway가 열지 않습니다.
현재 public API는 `/gateway/v3`입니다. `/gateway/v1`, `/gateway/v2`는 token을 먼저 인증한
뒤 `410 client_upgrade_required`를 반환하며 어떤 payload도 저장하지 않습니다.

## 4. Agent Plugin 설정

각 PC마다 별도 agent id와 token을 씁니다.
plugin 설치와 token helper 설정은 별도입니다.
hook 실행에는 `node`가 필요합니다.

Claude Code:

```text
/plugin marketplace add mykim0409/persona-vault-gateway
```

```text
/plugin install persona-vault@persona-vault-gateway
```

설치 화면에 표시되는 PersonaVault hook을 확인하고 승인한 뒤 `/reload-plugins`를
실행합니다. `/hooks`에서 `SessionStart`, `UserPromptSubmit`, `SubagentStop`, `Stop`,
`SessionEnd`가 보이는지 확인합니다.

Codex:

```bash
codex plugin marketplace add mykim0409/persona-vault-gateway
codex plugin add persona-vault --marketplace persona-vault-gateway
```

Codex를 다시 열거나 새 task를 시작한 뒤 `/hooks`에서 PersonaVault hook 명령을
검토하고 신뢰합니다. 이 승인은 각 PC에 저장되며 hook 정의가 변경되면 다시
검토합니다.

token helper:

```bash
base=https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts
d="$(mktemp -d)" && curl -fsSLo "$d/install-agent-config.sh" "$base/install-agent-config.sh" \
  && curl -fsSLo "$d/pvg-client.js" "$base/pvg-client.js" \
  && sh "$d/install-agent-config.sh"
```

Windows token helper:

```powershell
$base = 'https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts'
$d = Join-Path $env:TEMP 'persona-vault-installer'
New-Item -ItemType Directory -Force -Path $d | Out-Null
foreach ($f in 'install-agent-config.ps1', 'pvg-client.js') { Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $d $f) "$base/$f" }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'install-agent-config.ps1')
```

`pvg-client.js`는 installer와 같은 디렉토리에 있어야 하며 없으면 installer가 중단됩니다.
공유 Node client는 `~/.local/share/persona-vault-gateway/pvg-client.js`(POSIX는 `XDG_DATA_HOME` 우선)에 복사됩니다.
저장된 token을 교체하려면 같은 installer를 `--replace-token`(PowerShell은 `-ReplaceToken`)으로 다시 실행하며,
새 token은 숨김 입력으로 받습니다.

Helper 설치에도 `node`가 필요하며 Python helper는 쓰지 않습니다. 두 installer는 공통 `pvg-client.js`를
배치하고 OS별 launcher만 다르게 만듭니다. Hook과 launcher가 같은 파일을 실행하므로
`pvg-rag-search`·`pvg-agent-memo` 이름과 `--long` flag가 macOS·Linux·Windows에서 같습니다.
Claude는 `hooks/hooks.json`(`command` + `args`), Codex는 `.codex-plugin/plugin.json`이 참조하는
`hooks/codex-hooks.json`을 쓰며 둘 다 같은 JS와 단일 skill을 실행합니다.

설치되는 것:

```text
~/.config/persona-vault-gateway/env
~/.local/bin/pvg-agent-memo
~/.local/bin/pvg-rag-search
```

Windows에서는 token config가 `%APPDATA%\persona-vault-gateway\env.json`에 저장되고 `~\.local\bin\pvg-agent-memo.ps1`, `~\.local\bin\pvg-rag-search.ps1`이 기본 launcher로 설치됩니다. 호환용 `.cmd`도 함께 설치되지만 선택 사항입니다.

PowerShell의 `.ps1`은 인자를 공통 `pvg-client.js`에 그대로 넘기는 얇은 launcher라 같은 `--long` flag를 씁니다. PATH가 없다는 이유로 Linux 명령이나 Unix config를 탐색하지 않습니다.

```powershell
& "$HOME\.local\bin\pvg-rag-search.ps1" --view current "현재 운영 정책"
& "$HOME\.local\bin\pvg-agent-memo.ps1" --help
```

기존 `-View`·`-Project` 표기는 호환 alias입니다. 저장 시 stdin·quoting 문법은
[Windows 명령 안내](../plugins/persona-vault/skills/persona-vault/references/windows.md)를 따릅니다.

token이 아직 없으면 helper만 설치됩니다.
`/admin/tokens`에서 token을 만든 뒤 같은 스크립트를 다시 실행하면 됩니다.
agent가 shell command를 실행할 수 있다면 다음 helper로 raw note를 남길 수 있습니다.

신뢰된 hook은 main 요청과 최종 응답, subagent에게 위임한 요청과 최종 결과를
자동으로 수집합니다. tool output과 reasoning은 저장하지 않습니다. 기록은 먼저
plugin data의 `spool/v2/<session-hash>.jsonl`에 저장되고, Stop에서 Gateway로
전송됩니다. 성공한 날짜별 payload hash는 같은 디렉토리의 checkpoint에 기록되어
변경된 날짜만 다시 전송합니다. Hook은 Gateway의 `conversation-merge-v1`
capability를 확인한 경우에만 긴 대화도 메시지 수와 UTF-8 크기에 맞춰 batch merge로 나눠 보냅니다.
Gateway는 event ID로 병합하고, hook은 성공한 batch부터 이어서 전송합니다. 날짜의 모든 batch가
성공하면 완료 hash를 기록하고 날짜가 바뀔 때 전송이 확인된 과거 record를 spool에서 정리합니다.
Capabilities 응답이 없거나 느리거나 형식이 잘못됐거나 merge를 지원하지 않으면 spool을 보존하고
업로드를 멈추며 notice를 남깁니다. Gateway를 먼저 업데이트한 다음 각 PC의 플러그인을 업데이트하면
다음 hook에서 다시 시도합니다.
같은 subagent를 재개한 후속 요청·결과도 수집하며 동일 이벤트 재시도는 중복 저장하지 않습니다.
`0.6.0`은 이전 root spool을 읽거나 재전송하지 않습니다. Raw curation 전에는 사용하는
Codex·Claude client를 모두 업데이트하고 각 활성 세션을 한 번 정상 전송합니다.

SessionStart는 `Read` scope가 있는 token으로 승인된
`10_User/WORKING_AGREEMENT.md`를 읽어 Codex와 Claude의 main session에 전달합니다.
플러그인은 `SubagentStart`를 등록하지 않으며, main agent는 subagent에 task-specific 사실과
제약만 전달합니다. Raw에 저장된 subagent 위임문은 agent가 만든 작업 지시로 표시되며 사용자
발화로 취급하지 않습니다. 파일이 없거나 승인 metadata가 맞지 않거나 구버전 Gateway가
endpoint를 제공하지 않으면 기존 PersonaVault 안내만 사용하고 세션을 막지 않습니다.

hook은 로컬 spool에 저장하기 전과 Gateway로 보낼 때마다 token, API key, password(JSON·key=value·URL·
Bearer/Basic), private key 패턴을 가리고, 인식된 fenced 코드·설정·env·diff·log block은
`[omitted <kind> block, N lines]`로 바꾸며 절대 cwd 메타데이터 대신 프로젝트 디렉터리 이름만 보냅니다
(메시지 본문의 경로는 지우지 않음). 필터가 실패하면 해당 새 이벤트는 저장하지 않고 해당 대기 batch는
전송하지 않으며(에이전트는 막지 않음, 이미 깨끗한 다른 대기 기록과 무관) `status.json`에 고정 문구만
남깁니다. 휴리스틱이므로 문장 속 코드는 남을 수 있고, 이미 저장된 기존 spool 원문은 지우지 않으며
(전송 시에만 걸러짐), 이미 Gateway에 저장된 이벤트와 내용이 달라지면 409 conflict 상태로 남습니다.
직접 API·수동 Vault 파일·Cloudflare embedding 입력은 이 필터의 대상이 아닙니다. 비밀을
대화에 붙여 넣지 않는 것이 기본 원칙입니다. 자동 수집을 원하지 않으면 해당
플랫폼의 `/hooks`에서 PersonaVault hook을 비활성화합니다.

Vault root의 정식 `CURATOR.md`에
`id: kn_persona_vault_curator_protocol_v1`이 있으면 해당 root에서 시작한
Curator 세션은 SessionStart 일반 규칙을 주입하지 않고, JSONL 생성과 Gateway
전송을 포함한 자동 수집도 하지 않습니다. Curator는 반드시 Vault root를 작업
root로 열어 시작해야 합니다. 다른 프로젝트에서 Vault 경로만 수정하는 작업은
일반 세션으로 수집되며, 환경 변수나 요청 문구로 끄는 우회 경로는 없습니다.

Curator는 사용자가 subject를 지정하지 않아도 가장 오래된 committed component를 시작점으로
같은 주제를 날짜에 걸쳐 모읍니다. 기본 한도는 8파일·60,000자이며 `--max-sources`,
`--max-characters`로 조정합니다. 초과하는 첫 파일은 잘라내지 않고 표시합니다.
Qdrant가 current이면 저장된 point ID로 후보를 보강하고, 없거나 stale이어도 같은 프로젝트의
기존 문서를 후보로 찾습니다. 기존 프로젝트 지식은 불변이 아니며 새 근거에 맞춰 재작성합니다.

```bash
uv run pvg-wiki --vault /path/to/persona-vault compact-plan --output /path/to/persona-vault/.tmp/curating/plan.json
```

Exact plan 승인 전에는 tracked Vault 파일을 수정하지 않습니다. 승인 후에도 모든 item이 판정되고
검증 probe를 통과한 날짜별 raw fragment만 whole-file로 삭제하며 새 merge receipt는 만들지 않습니다.
매 batch에서 사용자 프로필도 새 근거·반례와 비교하되, 새 의미가 없으면 기존 문서를 유지합니다.
프로필 내용과 사람이 승인한 전역 협업 규칙은 구분합니다.

기본 출력은 요약입니다. `--output`은 상세 JSON을 `.tmp/curating/` 안의 새 파일에만 쓰며 기존
계획은 덮어쓰지 않습니다. `--full`은 전체 출력이 필요한 진단용입니다.
다음 예시는 별도 계획 파일을 사용합니다. 같은 판정의 item은 `review.items`에서 `ids` 목록으로
묶을 수 있으며 검사는 여전히 각 ID의 누락·중복을 확인합니다.

```bash
uv run pvg-wiki --vault /path/to/persona-vault compact-plan --deferrals /path/to/persona-vault/.tmp/curating/deferred.json --output /path/to/persona-vault/.tmp/curating/next-plan.json
# Add drafts/probes to review, then check the drafts without modifying tracked files
uv run pvg-wiki --vault /path/to/persona-vault compact-review /path/to/persona-vault/.tmp/curating/next-plan.json
# After the independent semantic review is complete, record the hash (this is not user approval)
uv run pvg-wiki --vault /path/to/persona-vault compact-review /path/to/persona-vault/.tmp/curating/next-plan.json --record
# After the exact plan is approved and applied, run the final check, incremental indexing and search with the same Vault/DB/Qdrant settings as the Gateway
uv run pvg-wiki --vault /path/to/persona-vault compact-finish /path/to/persona-vault/.tmp/curating/next-plan.json
```

보류 목록은 자동 생성되는 서버 상태가 아닙니다. Curator가 source hash·관련 문서 hash와 재검토
조건을 기록하며, 변경된 문서는 CLI가 보류를 무효화합니다. 같은 plan의 소묶음은 마지막에만
`compact-finish`를 실행합니다. 최신 index는 재색인하지 않고, 성공한 동일 입력의 재실행은
검색 결과도 재사용합니다. Quota·검색 실패 후에는 복구 후 같은 명령으로 재시도합니다.
계획의 `before` 날짜 이상인 범위 밖 Gateway raw 추가·이벤트 증분 수신은 배치 검토를 무효화하지 않지만,
무결성 검사는 최신 raw를 포함하고 최종 색인·검색은 전체 실제 Vault의 최신 상태를 요구합니다.
검사 실행 도중 snapshot이 바뀌면 같은 명령으로 재시도합니다.
이 명령은 기존 Gateway DB와 호환되는 index가 필요하며 초기 구축·모델 교체를 하지 않습니다.
`compact-check`는 색인 없는 read-only 진단용이고, 정상 흐름에서 `health`·`conflicts list`를 반복하지 않습니다.
초안·검토·완료 checkpoint는 plan의 `.tmp/curating/` 안에만 저장합니다. 사용자 승인과
apply/DELETE/commit/push는 자동화하지 않습니다. 의미 검토 자체도 LLM API로 실행하지 않습니다.
입력 형식은 [CURATOR.md](CURATOR.md)를 따르며 추가 라이브러리나 LLM API 설정은 필요 없습니다.

```bash
printf '%s\n' "Situation: token rotate 후 기존 설정으로 인증이 실패했다.
Action: 새 token으로 helper 설정을 갱신했다.
Outcome: 인증과 검색이 정상화됐다.
Applicability: PersonaVault Gateway token rotation.
Reuse guidance: rotate 후 모든 client 설정을 함께 갱신한다.
Evidence: Gateway audit log.
Uncertainty: 없음." | \
  pvg-agent-memo --project PersonaVault \
  --kind procedure --outcome success \
  --provenance direct_observation --evidence gateway-audit-log \
  "PersonaVault: token rotation episode"
pvg-rag-search --view evidence "token rotate 후 인증 실패"
pvg-rag-search --view current "현재 token 관리 정책"
```

`pvg-agent-memo`는 helper 이름을 유지하며 v3 capture에 `kind=note`를 보냅니다. 기본
`note_type`은 `observation`이고 `--kind`는 더 구체적인 `note_kind`입니다. agent는 쓰기 전에
현재 지식과 유사 evidence를 검색하고, proposal이나 handoff는 그 성격을 명시합니다.

## 5. 읽기 전용 확인

일회성 smoke memo는 Vault에 만들지 않습니다. 서버 상태와 기존 지식 검색으로 설치를 확인합니다.

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
pvg-rag-search --view current "현재 운영 정책"
```

`readyz`가 정상이 아니면 admin 화면에서 `Update RAG index`를 실행한 뒤 다시 확인합니다.
검색 평가 실패는 [evaluation-error-book.md](evaluation-error-book.md)의 형식으로 Vault 밖의 CI artifact나
repository 문서에 기록합니다.

repository를 clone한 운영자는 필요할 때 다음 명령으로 같은 health report와 conflict queue를 확인할 수 있습니다.

```bash
uv run --frozen python -m gateway.cli --vault /path/to/persona-vault health
uv run --frozen python -m gateway.cli --vault /path/to/persona-vault conflicts list
```

## 이후 배포

```bash
cd ~/persona-vault-gateway
git pull --ff-only
sudo docker compose up -d --build
```

Compose 이미지(Qdrant, sync용 `alpine/git`, Gateway base `python:3.13-slim`)는 multi-arch index digest로
고정되어 있습니다. digest 갱신은 의도적으로 하는 변경이며, apt 패키지와 build backend는 고정되지 않아
build가 bit 단위로 같다고 보장하지 않습니다.
