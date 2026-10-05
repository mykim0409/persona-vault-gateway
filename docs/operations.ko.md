# PersonaVault Gateway 운영

[English](operations.md) | **한국어**

최초 설치는 [setup.ko.md](setup.ko.md), 호스팅 선택지는 [hosting.ko.md](hosting.ko.md)입니다. `docker compose` 명령은 `compose.yml`을
둔 디렉토리에서 실행합니다. Vault 구조의 명령은 Vault 로컬 checkout에서, Curator 명령은 소스 checkout에서 실행합니다.

- [Vault 구조](#vault-구조)
- [Semantic 검색](#semantic-검색)
- [원격 접근](#원격-접근)
- [Hook과 Curator](#hook과-curator)
- [문제 해결](#문제-해결)
- [업그레이드](#업그레이드)
- [백업과 복구](#백업과-복구)

## Vault 구조

Vault는 Markdown 파일의 Git 저장소입니다. 형식은 [metadata.ko.md](metadata.ko.md), 정리 절차는
[CURATOR.md](CURATOR.md)(영문)를 따릅니다.

| 디렉토리 | 용도 | 수정 주체 |
| --- | --- | --- |
| `00_Inbox/` | 미분류 메모 | 사람 |
| `10_User/` | 협업 규칙(`WORKING_AGREEMENT.md`), 사용자 브리프와 장부(`PROFILE.md`, `OBSERVATIONS.md`), 사용자 기록 | 사람, 승인된 Curator |
| `20_Projects/` | 프로젝트 현황(`BRIEF.md`), 결정 장부(`DECISIONS.md`), 주제 문서 | 사람, 승인된 Curator |
| `30_Conversations/raw/` | agent 원본 대화(날짜별) | agent (Gateway가 쓰는 유일한 경로) |
| `30_Conversations/summaries/` | 대화 요약 | 사람, Curator |
| `50_Knowledge/` | 검증된 재사용 지식 | 사람, Curator |
| `90_Private/` | 개인 메모 | 사람 |

- `.git/`, `.obsidian/`, `.tmp/`는 색인·검색에서 제외됩니다. sync가 변경을 commit하므로 `.obsidian/`과
  `.tmp/`는 `.gitignore`에 넣어 Gateway를 연결하기 전에 push하세요. 이미 추적 중이면 `git rm -r --cached --ignore-unmatch -- .obsidian .tmp`
  후 commit합니다(과거 history에는 남습니다).
- 편집은 로컬 clone에서 하고 push 전에 `git pull --rebase`합니다. `30_Conversations/raw/`는 직접 수정하지 않습니다.
- `90_Private/`도 read token으로 읽히고 semantic 검색을 켜면 Cloudflare로 전송됩니다.
- 협업 규칙 템플릿은 파일이 없을 때만 받습니다.

```bash
mkdir -p 10_User
test -e 10_User/WORKING_AGREEMENT.md || curl -fsSLo 10_User/WORKING_AGREEMENT.md \
  https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/docs/WORKING_AGREEMENT.md
```

Gateway가 서버에서 Vault를 clone해 commit, `pull --rebase`, push를 주기적으로 합니다(기본 300초,
`VAULT_SYNC_INTERVAL_SECONDS`). 지원하는 Git host는 GitHub SSH URL입니다.

## Semantic 검색

기본은 keyword 검색(`EMBEDDING_PROVIDER=none`)입니다. 켜려면 아래를 **모두** 설정하고 재시작합니다.

```text
EMBEDDING_PROVIDER=cloudflare
CLOUDFLARE_ACCOUNT_ID=<account-id>
CLOUDFLARE_API_TOKEN=<workers-ai-token>
```

- **Compose**: 위 값과 `COMPOSE_PROFILES=semantic`을 셸 환경 또는 `compose.yml` 옆의 `.env`에 두고 `docker compose up -d`를
  다시 실행합니다. `semantic` profile이 고정된 Qdrant를 시작하고 `QDRANT_URL`은 `http://qdrant:6333`으로 고정되어 있습니다
  (`QDRANT_COLLECTION` 기본 `persona_vault`).
- **Railway·Render**: 제공된 설정에는 Qdrant가 없습니다. 위 값과 함께 `QDRANT_URL=<Gateway가 닿을 수 있는 Qdrant 주소>`
  (필요하면 `QDRANT_COLLECTION`)를 서비스 환경 변수로 직접 추가하고, Qdrant는 직접 운영하세요. 사설 경로로만 닿게 하세요.

- token: Cloudflare dashboard `Workers AI` → `Use REST API` → `Create a Workers AI API Token`
  (`Workers AI - Read`, `Workers AI - Edit`). 플랫폼 secret 또는 `.env`에만 저장합니다.
- 색인 chunk(`90_Private/` 포함)와 검색 query가 Cloudflare Workers AI로 전송됩니다.
  [데이터 정책](https://developers.cloudflare.com/workers-ai/platform/data-usage/).
- 시작 후 admin의 `Update RAG index`를 한 번 실행하고 `/readyz`의 `rag_indexed`가 `true`인지 확인합니다.
  이후에는 변경된 chunk만 embedding합니다. 실패하면 다시 실행하며 동시에 두 update는 돌지 않습니다.
- 색인이 없거나 오래됐거나 Qdrant가 불가하면 검색은 keyword로 응답합니다(`index.stale`로 확인).
  Cloudflare 한도를 넘으면 reset 후 자동 재시도합니다.
- 모델은 `@cf/qwen/qwen3-embedding-0.6b`(1024차원)입니다. provider·모델·차원이 바뀌면 전체를 다시 색인합니다.
- `EMBEDDING_PROVIDER=none`에서는 `Update RAG index`가 아무것도 하지 않고 기존 vector도 건드리지 않습니다.

## 원격 접근

- 컨테이너는 `PORT`(기본 `8000`)로 듣고, Compose는 이를 호스트 `GATEWAY_BIND_ADDR`(기본 loopback `127.0.0.1`)의
  `GATEWAY_HOST_PORT`(기본 `18080`)에 게시합니다. 주소를 바꾸기 전에 암호화된 사설 경로 또는 TLS endpoint를 준비하세요.
  평문 공개 HTTP로는 token·admin 비밀번호·대화가 노출됩니다. Qdrant는 비공개로 두고 포트를 공개하지 않습니다.
  공개 HTTPS로 호스팅하면 `/setup`과 `/admin`도 공개됩니다(claim 전에는 setup code, 후에는 admin 비밀번호·세션·CSRF·login 제한이 보호).
  claim을 서둘러 끝내고 code를 공유하지 마세요. 비공개 배포라면 가능한 네트워크·접근 제어로 admin을 제한하세요.
  이 저장소는 proxy·인증서·DDNS를 제공하지 않습니다.
- HTTPS를 종단하는 앞단이 정확한 IP를 알 수 있다면 환경 변수에 그 IP를 씁니다(쉼표로 여러 개).

  ```text
  FORWARDED_ALLOW_IPS=203.0.113.10
  ```

  `*`는 쓰지 않습니다. 신뢰 목록에 없는 주소의 `X-Forwarded-*`는 무시되며, 이때 Secure cookie 판단과
  login 제한은 연결 주소 기준입니다. 앞단을 거치는 모든 사용자는 앞단 주소 하나로 집계됩니다.
- 앞단 주소를 신뢰할 수 없거나 모르는 플랫폼(Railway, Render 등)에서 HTTPS로만 접속한다면 `PVG_SECURE_COOKIES=true`로 admin
  cookie에 Secure를 강제합니다. forwarded 헤더는 신뢰하지 않습니다. 평문 HTTP로 접속하면
  Secure cookie는 전송되지 않아 로그인할 수 없으니 HTTPS 전용일 때만 쓰세요.
- `PVG_TRUSTED_PROXY_HOPS`(기본 `0`)는 Gateway 앞에서 `X-Forwarded-For`에 client 주소를 덧붙이는 proxy의 개수입니다. `0`이면 login 제한이 연결 주소 기준이라
  한 proxy 뒤의 모든 사용자가 한 bucket을 공유하고, 그러면 운영자가 잠길 수 있습니다. `1`(Render·Railway 설정이 지정)이면 오른쪽에서 한 칸째 항목, 즉 플랫폼
  proxy가 본 주소 기준으로 집계되어 client가 앞에 덧붙인 값으로는 bucket을 고를 수 없습니다. 항목이 없거나 부족하거나 IP가 아니면 연결 주소로 되돌아갑니다.
  Render와 Railway는 `1`, Compose와 loopback은 `0`을 쓰고, 실제 proxy 개수보다 크게 두지 마세요(앞에 CDN이 있으면 하나 더합니다). 이 값은 admin login과 setup
  제한의 집계 기준에만 영향을 주며 Secure cookie 판단이나 `FORWARDED_ALLOW_IPS`에는 영향이 없습니다.
- Admin login은 client 주소당 5분에 5회까지입니다. 초과하면 `429`와 `Retry-After`를 반환합니다.
  프로세스 메모리에서만 동작하며 재시작하면 초기화되고 공유되지 않으므로 네트워크 접근 제어를 대체하지
  못합니다. CSRF token과 same-origin 확인은 유지됩니다.
- Admin의 변경 form은 CSRF token을 요구하고, 기존 agent ID의 생성·rotate·disable은 확인 화면을 한 번 더 거칩니다.

## Hook과 Curator

- hook은 사용자 요청, 최종 응답, subagent 위임·결과를 보냅니다. tool output과 reasoning은 제외합니다.
  새 기록은 로컬 `spool/v2/<session-hash>.jsonl`(평문)에 저장된 뒤 Stop에서 전송됩니다.
- 전송 전에 token·credential 패턴을 가리고 code·config·diff·log block을 생략하지만 휴리스틱입니다.
  대화에 비밀을 붙여 넣지 마세요. 자세한 범위는 [SECURITY.ko.md](../SECURITY.ko.md)입니다.
- Gateway가 `conversation-merge-v1`을 알리지 않으면 spool을 보존하고 전송을 멈춥니다. Gateway를 먼저
  업데이트한 뒤 각 PC의 plugin을 업데이트하세요.
- 수집을 끄려면 해당 플랫폼의 `/hooks`에서 PersonaVault hook을 비활성화합니다. 정식 `CURATOR.md`가 있는
  Vault root에서 시작한 Curator 세션은 수집되지 않습니다.
- SessionStart는 read token으로 `10_User/WORKING_AGREEMENT.md`를 읽어 세션에 전달합니다.
- 수동 memo: `pvg-agent-memo --project <name> --kind procedure --outcome success "<title>"`에 본문을 stdin으로
  전달합니다. Windows 문법은
  [Windows 안내](https://github.com/mykim0409/persona-vault-gateway/blob/main/plugins/persona-vault/skills/persona-vault/references/windows.md)입니다.

Curator(`pvg-wiki`)는 계획을 제안만 하며 승인·apply·commit·push는 사람이 합니다. 절차 전체는
[CURATOR.md](CURATOR.md)(영문)를 따르세요. CLI는 소스 checkout에서 `uv sync --frozen` 후 `uv run pvg-wiki ...`로
실행합니다.

CLI와 직접 실행하는 Python은 `EMBEDDING_PROVIDER`가 없으면 `cloudflare`가 기본입니다(Gateway 서비스 기본은 `none`).
keyword 전용 Gateway에서는 `compact-finish`에 `EMBEDDING_PROVIDER=none`을 명시합니다. semantic Gateway에서는
Gateway와 같은 Vault·DB·Qdrant 설정과 호환되는 기존 색인이 필요합니다.

읽기 전용 점검: `uv run --frozen python -m gateway.cli --vault /path/to/vault health` (`conflicts list`도 가능).

## 문제 해결

- **`/readyz`가 503**: 설정을 마치기 전(claim 또는 Vault 연결 전)에는 정상입니다. `/healthz`는 앱이 살아 있으면 200입니다.
  `/setup` 또는 `/admin/vault`에서 이어서 진행합니다. semantic을 켠 경우에는 Qdrant 연결 또는 색인 불일치일 수도 있어
  `Update RAG index`를 실행합니다.
- **setup code를 놓침**: claim 전이면 `PVG_SETUP_TOKEN`(20~200자 printable ASCII, 공백 없음)을 설정하고 재시작해 새 값을 씁니다.
  최초 시작 때 한 번 출력된 로그에서 찾을 수도 있습니다.
- **연결 오류(`/admin/vault`)**: 화면의 고정 문구를 따릅니다. 흔한 원인은 deploy key 미등록 또는 **Allow write access** 누락,
  저장소 URL 오타, commit이 없는 빈 저장소(commit을 만든 뒤 **Retry**), pinned GitHub host key 불일치입니다.
  push가 거부되면 write 권한이나 protected branch를 확인합니다. 연결 단계는 쓰기 권한을 증명하지 못합니다.
- **sync BLOCKED**: Vault의 rebase·merge·충돌 상태를 직접 해결하면 sync가 재개됩니다.
- **로그인 429**: `Retry-After` 초 뒤에 다시 시도합니다.
- API는 `/gateway/v3`입니다. `/gateway/v1`, `/gateway/v2`는 `410 client_upgrade_required`를 반환합니다.
  SQLite는 시작 시 자동 migration되며, 더 새로운 schema의 DB는 구버전 Gateway가 열지 않습니다.

## 업그레이드

새 Release로 올릴 때는 `compose.yml`을 체크섬 확인 후 같은 위치에 교체하고 `docker compose up -d`를 실행합니다(image는
digest로 고정). 플랫폼에서는 설정의 image 참조를 직접 새 release로 바꾸고 수동으로 배포합니다. 자동 재배포는 없습니다.
`persona-vault-data`(`/data`)가 그대로 유지되므로 설정과 Vault는 남습니다. 업그레이드 전에 아래
[백업과 복구](#백업과-복구)대로 백업하세요.

## 백업과 복구

1. `docker compose stop`으로 멈춥니다.
2. `/data` volume 하나를 보존합니다: Vault clone(push되지 않은 commit과 미commit 변경 포함, `/data/vault`), SQLite(`/data/gateway.db`:
   token hash, audit), 설정(`/data/setup`: admin hash, deploy key의 private key).
3. Qdrant volume을 쓴다면 Vault에서 다시 만들 수 있는 파생 데이터입니다(`Update RAG index`, Cloudflare 사용량 발생).

복사본에는 비밀이 있으니 암호화해 보관합니다. 복구는 같은 구성에 volume 내용을 되돌리고 `docker compose up -d`를 실행합니다.
SQLite가 없으면 token을 다시 발급합니다. `docker compose down -v`, `docker volume rm`, `docker system prune --volumes`는
일상 절차가 아닙니다. 복원을 확인하기 전에는 쓰지 마세요.
