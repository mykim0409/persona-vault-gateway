# PersonaVault Gateway 운영

최초 설치는 [setup.md](setup.md)입니다. `docker compose` 명령은 설치 디렉토리에서 실행합니다. Vault 구조의 명령은
Vault 로컬 checkout에서, Curator 명령은 소스 checkout에서 실행합니다.

- [Vault 구조](#vault-구조)
- [다른 Git host](#다른-git-host)
- [Semantic 검색](#semantic-검색)
- [원격 접근](#원격-접근)
- [Hook과 Curator](#hook과-curator)
- [문제 해결](#문제-해결)
- [업그레이드](#업그레이드)
- [백업과 복구](#백업과-복구)

## Vault 구조

Vault는 Markdown 파일의 Git 저장소입니다. 형식은 [metadata.md](metadata.md), 정리 절차는
[CURATOR.md](CURATOR.md)를 따릅니다.

| 디렉토리 | 용도 | 수정 주체 |
| --- | --- | --- |
| `00_Inbox/` | 미분류 메모 | 사람 |
| `10_User/` | 협업 규칙(`WORKING_AGREEMENT.md`), 사용자 기록 | 사람, 승인된 Curator |
| `20_Projects/` | 프로젝트 목표·결정 | 사람, 승인된 Curator |
| `30_Conversations/raw/` | agent 원본 대화(날짜별) | agent (Gateway가 쓰는 유일한 경로) |
| `30_Conversations/summaries/` | 대화 요약 | 사람, Curator |
| `50_Knowledge/` | 검증된 재사용 지식 | 사람, Curator |
| `90_Private/` | 개인 메모 | 사람 |

- `.git/`, `.obsidian/`, `.tmp/`는 색인·검색에서 제외됩니다. sync가 `git add .`를 실행하므로 `.obsidian/`과
  `.tmp/`는 `.gitignore`에 넣어 첫 실행 전에 push하세요. 이미 추적 중이면 `git rm -r --cached --ignore-unmatch -- .obsidian .tmp`
  후 commit합니다(과거 history에는 남습니다).
- 편집은 로컬 clone에서 하고 push 전에 `git pull --rebase`합니다. `30_Conversations/raw/`는 직접 수정하지 않습니다.
- `90_Private/`도 read token으로 읽히고 semantic 검색을 켜면 Cloudflare로 전송됩니다.
- 협업 규칙 템플릿은 파일이 없을 때만 받습니다.

```bash
mkdir -p 10_User
test -e 10_User/WORKING_AGREEMENT.md || curl -fsSLo 10_User/WORKING_AGREEMENT.md \
  https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/docs/WORKING_AGREEMENT.md
```

## 다른 Git host

`persona-vault-init`은 GitHub SSH URL만 지원합니다(`--check`도 동일). 다른 host는 직접 준비합니다.

```bash
mkdir -p secrets && chmod 700 secrets
ssh-keygen -t ed25519 -C persona-vault-sync -N "" -f secrets/persona_vault_sync
chmod 600 secrets/persona_vault_sync
test -f .env || install -m 600 .env.example .env     # VAULT_REPO_SSH_URL, ADMIN_PASSWORD 설정
```

`secrets/github_known_hosts`에는 host 공식 문서의 fingerprint와 직접 대조한 host key만 넣습니다.
`ssh-keyscan` 출력은 검증 전에는 후보일 뿐입니다. `secrets/persona_vault_sync.pub`를 write 권한
deploy key로 등록한 뒤 `docker compose up -d`를 실행합니다.

## Semantic 검색

`.env`에 **모두** 설정하고 `docker compose up -d`를 다시 실행합니다.

```text
EMBEDDING_PROVIDER=cloudflare
COMPOSE_PROFILES=semantic
CLOUDFLARE_ACCOUNT_ID=<account-id>
CLOUDFLARE_API_TOKEN=<workers-ai-token>
```

- token: Cloudflare dashboard `Workers AI` → `Use REST API` → `Create a Workers AI API Token`
  (`Workers AI - Read`, `Workers AI - Edit`). `.env`에만 저장합니다.
- 색인 chunk(`90_Private/` 포함)와 검색 query가 Cloudflare Workers AI로 전송됩니다.
  [데이터 정책](https://developers.cloudflare.com/workers-ai/platform/data-usage/).
- 시작 후 admin의 `Update RAG index`를 한 번 실행하고 `/readyz`의 `rag_indexed`가 `true`인지 확인합니다.
  이후에는 변경된 chunk만 embedding합니다. 실패하면 다시 실행하며 동시에 두 update는 돌지 않습니다.
- 색인이 없거나 오래됐거나 Qdrant가 불가하면 검색은 keyword로 응답합니다(`index.stale`로 확인).
  Cloudflare 한도를 넘으면 reset 후 자동 재시도합니다.
- 모델은 `@cf/qwen/qwen3-embedding-0.6b`(1024차원)입니다. provider·모델·차원이 바뀌면 전체를 다시 색인합니다.
- `EMBEDDING_PROVIDER=none`에서는 `Update RAG index`가 아무것도 하지 않고 기존 vector도 건드리지 않습니다.

## 원격 접근

- Gateway는 `GATEWAY_BIND_ADDR=127.0.0.1`(기본), `GATEWAY_HOST_PORT=18080`에 열립니다. 주소를 바꾸기 전에
  암호화된 사설 경로 또는 TLS endpoint를 준비하세요. 평문 공개 HTTP로는 token·admin 비밀번호·대화가
  노출됩니다. Admin과 Qdrant는 공개 인터넷에 노출하지 않습니다.
- TLS를 종단하는 앞단이 있으면 `.env`에 그 앞단의 정확한 IP를 씁니다(쉼표로 여러 개).

  ```text
  FORWARDED_ALLOW_IPS=203.0.113.10
  ```

  `*`는 쓰지 않습니다. 신뢰 목록에 없는 주소의 `X-Forwarded-*`는 무시되며, 이때 Secure cookie 판단과
  login 제한은 연결 주소 기준입니다. 앞단을 거치는 모든 사용자는 앞단 주소 하나로 집계됩니다.
- Admin login은 client 주소당 5분에 5회까지입니다. 초과하면 `429`와 `Retry-After`를 반환합니다.
  프로세스 메모리에서만 동작하며 재시작하면 초기화되고 공유되지 않으므로 네트워크 접근 제어를 대체하지
  못합니다. CSRF token과 same-origin 확인은 유지됩니다.
- Admin의 변경 form은 CSRF token을 요구하고, 기존 agent ID의 생성·rotate·disable은 확인 화면을 한 번 더 거칩니다.

## Hook과 Curator

- hook은 사용자 요청, 최종 응답, subagent 위임·결과를 보냅니다. tool output과 reasoning은 제외합니다.
  새 기록은 로컬 `spool/v2/<session-hash>.jsonl`(평문)에 저장된 뒤 Stop에서 전송됩니다.
- 전송 전에 token·credential 패턴을 가리고 code·config·diff·log block을 생략하지만 휴리스틱입니다.
  대화에 비밀을 붙여 넣지 마세요. 자세한 범위는 [SECURITY.md](../SECURITY.md)입니다.
- Gateway가 `conversation-merge-v1`을 알리지 않으면 spool을 보존하고 전송을 멈춥니다. Gateway를 먼저
  업데이트한 뒤 각 PC의 plugin을 업데이트하세요.
- 수집을 끄려면 해당 플랫폼의 `/hooks`에서 PersonaVault hook을 비활성화합니다. 정식 `CURATOR.md`가 있는
  Vault root에서 시작한 Curator 세션은 수집되지 않습니다.
- SessionStart는 read token으로 `10_User/WORKING_AGREEMENT.md`를 읽어 세션에 전달합니다.
- 수동 memo: `pvg-agent-memo --project <name> --kind procedure --outcome success "<title>"`에 본문을 stdin으로
  전달합니다. Windows 문법은
  [Windows 안내](https://github.com/mykim0409/persona-vault-gateway/blob/main/plugins/persona-vault/skills/persona-vault/references/windows.md)입니다.

Curator(`pvg-wiki`)는 계획을 제안만 하며 승인·apply·commit·push는 사람이 합니다. 절차 전체는
[CURATOR.md](CURATOR.md)를 따르세요. CLI는 소스 checkout에서 `uv sync --frozen` 후 `uv run pvg-wiki ...`로
실행합니다.

CLI와 직접 실행하는 Python은 `EMBEDDING_PROVIDER`가 없으면 `cloudflare`가 기본입니다(Compose 기본은 `none`).
keyword 전용 Gateway에서는 `compact-finish`에 `EMBEDDING_PROVIDER=none`을 명시합니다. semantic Gateway에서는
Gateway와 같은 Vault·DB·Qdrant 설정과 호환되는 기존 색인이 필요합니다.

읽기 전용 점검: `uv run --frozen python -m gateway.cli --vault /path/to/vault health` (`conflicts list`도 가능).

## 문제 해결

- **Gateway가 시작하지 않음**: sync가 최초 clone을 끝내야 시작합니다. `docker compose ps`,
  `docker logs --tail=50 persona-vault-sync`로 확인합니다.
- **`Permission denied`**: deploy key가 등록되지 않았거나 저장소 이름이 틀립니다. **`Host key verification failed`**:
  `secrets/github_known_hosts`가 고정된 GitHub key와 다릅니다. 고친 뒤 `--check`를 다시 실행합니다.
- **`--check`가 EMPTY**: 저장소에 commit을 만듭니다.
- **큰 Vault에서 `up`이 sync unhealthy로 멈춤**(약 3분): clone이 계속 진행 중일 수 있습니다.
  `docker top persona-vault-sync`에 `git clone`이 보이면 기다립니다. volume 삭제나 sync 재시작은 하지 마세요.
  인증·host key 오류가 로그에 있으면 진행 중이 아니니 고치고, sync가 `healthy`가 되면 `docker compose up -d`를
  다시 실행합니다.
- **sync BLOCKED**: `/vault`의 rebase·merge·충돌 상태를 직접 해결하면 sync가 재개됩니다.
- **`/readyz` 503**: semantic을 켠 경우 Qdrant 연결 또는 색인 불일치입니다. `Update RAG index`를 실행합니다.
- **로그인 429**: `Retry-After` 초 뒤에 다시 시도합니다.
- API는 `/gateway/v3`입니다. `/gateway/v1`, `/gateway/v2`는 `410 client_upgrade_required`를 반환합니다.
  SQLite는 시작 시 자동 migration되며, 더 새로운 schema의 DB는 구버전 Gateway가 열지 않습니다.

## 업그레이드

항상 같은 설치 디렉토리에서 합니다. 디렉토리를 옮기거나 이름을 바꾸면 새 빈 volume이 생기므로, 불가피하면
모든 명령에 기존 이름으로 `docker compose -p <기존 이름> ...`을 씁니다. volume 삭제 명령은 쓰지 않습니다.

**먼저:** 예전 `.env`가 `EMBEDDING_PROVIDER`를 비우거나 `cloudflare` 기본값으로 semantic을 쓰고 있었다면
`.env`에 `EMBEDDING_PROVIDER=cloudflare`와 `COMPOSE_PROFILES=semantic`을 명시합니다. 현재 기본은 `none`이고
Qdrant는 `semantic` profile에서만 관리됩니다.

- **bundle**: 새 bundle을 기존 설치 디렉토리에 풀어 compose 파일만 교체합니다(`.env`, `secrets/`는 bundle에
  없어 그대로 남습니다).

  ```bash
  tar -xzf persona-vault-gateway-X.Y.Z-install.tar.gz --strip-components=1 -C ~/persona-vault-gateway
  cd ~/persona-vault-gateway && docker compose up -d
  ```

- **소스**:

  ```bash
  cd ~/persona-vault-gateway && git pull --ff-only
  docker compose -f compose.yml -f compose.build.yml build persona-vault-gateway
  docker compose up -d
  ```

Compose 이미지는 digest로 고정되어 있으며 갱신은 의도적인 변경입니다.

## 백업과 복구

1. `docker compose stop persona-vault-gateway persona-vault-sync`로 멈춥니다.
2. 보존: `.env`, `secrets/`(private key 포함), Vault volume(push되지 않은 commit과 미commit 변경 포함),
   Gateway data volume의 SQLite(`gateway.db`: token hash, audit).
3. Qdrant volume은 Vault에서 다시 만들 수 있는 파생 데이터입니다(`Update RAG index`, Cloudflare 사용량 발생).

복사본에는 비밀이 있으니 암호화해 보관합니다. 복구는 같은 설치 디렉토리(또는 같은 `-p` 이름)에 파일과
volume 내용을 되돌리고 `docker compose up -d`를 실행합니다. SQLite가 없으면 token을 다시 발급합니다.

`docker compose down -v`, `docker volume rm`, `docker system prune --volumes`는 일상 절차가 아닙니다.
복원을 확인하기 전에는 쓰지 마세요.
