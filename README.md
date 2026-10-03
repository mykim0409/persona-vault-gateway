# PersonaVault Gateway

> **상태: 개인용 self-hosted 베타. 아직 공개 사용 준비가 끝나지 않았습니다.**
> 보안 점검과 공개 전 QA가 완료되지 않았고, 안정성·보안·데이터 보존을 보장하지 않습니다.
> [남은 확인](#공개-전-남은-확인) 참고.

Markdown/Obsidian 호환 Vault에 AI 에이전트(Codex, Claude Code)의 대화와 메모를 기록하고,
전체 Vault를 검색하게 해 주는 FastAPI gateway입니다. 저장소에는 코드만 있고 실제 Vault는
별도 private Git 저장소에 둡니다.

## 무엇을 하나

- **기록**: 에이전트 대화와 구조화 메모를 `30_Conversations/raw/`에 Markdown으로 저장
  (`session_id` 기반 멱등 갱신, 실패 시 로컬 spool 재시도)
- **검색**: Qdrant semantic 검색 + keyword 검색, `current`·`evidence`·`history`·`conflicts` view
- **관리**: admin 화면에서 agent token 발급·rotate·disable (token은 hash만 저장)
- **정리**: 사람이 승인하는 로컬 CLI(Curator)로 raw를 Wiki에 통합 (선택)

자동 canonical 승격은 하지 않습니다. 검색 결과의 `answer_state`는 신호일 뿐 승격이나 사실 보증이 아닙니다.

## 사용 예

Plugin 설치 후 에이전트가 hook으로 자동 기록하고, 아래 두 명령으로 검색·메모를 남깁니다.
명령 이름과 `--long` flag는 macOS·Linux·Windows에서 같습니다.

```bash
pvg-rag-search --view current "현재 token 관리 정책"
pvg-rag-search --view evidence "token rotate 후 인증 실패"
pvg-agent-memo --project PersonaVault --kind procedure --outcome success \
  --provenance direct_observation --evidence gateway-audit-log \
  "PersonaVault: token rotation episode" < memo.txt
```

`pvg-agent-memo`는 v3 `kind=note` raw evidence를 보냅니다. 진행 로그, smoke 출력, 임시 오류,
문서 복사본은 저장하지 않습니다. Windows 명령 문법은
[Windows 안내](plugins/persona-vault/skills/persona-vault/references/windows.md)를 따릅니다.

## 구성 요소

| 구성 | 필수 | 역할 |
| --- | --- | --- |
| Gateway (FastAPI + SQLite) | 필수 | API, auth, 경로 정책, Markdown writer, admin |
| Qdrant | 필수 | semantic 색인 (장애 시 keyword 검색으로 동작) |
| Cloudflare Workers AI | 필수 (기본 embedding) | `@cf/qwen/qwen3-embedding-0.6b`, 1024차원 |
| Vault Git 저장소 + sync sidecar | 필수 (제공 compose 기준) | Vault pull/push. **SSH deploy key와 `known_hosts` secret 필요** |
| Agent plugin (Codex·Claude) | 선택 | 자동 capture, SessionStart 주입, 검색·메모 명령 |
| Custom GPT Actions | 선택 | plugin 대신 쓰는 대체 경로 |
| Curator (`pvg-wiki`) | 선택 | 사람이 승인하는 로컬 CLI. 서비스가 아님 |

제공되는 `compose.yml`은 Gateway·Qdrant·sync sidecar를 함께 띄우므로 sync 경로의 의존성
(Vault SSH URL, deploy key, `known_hosts`)이 실제로 필요합니다. `ADMIN_PASSWORD`와
Cloudflare 값은 필수이며 기본·예시 비밀번호를 운영에 쓰면 안 됩니다.

Plugin은 하나의 `persona-vault` skill과 하나의 Node client(`pvg-client.js`)를 공유하고, host
manifest와 OS별 launcher만 다릅니다. hook 실행과 helper 설치에는 `node`가 필요합니다.

```text
gateway/      FastAPI app, core(auth·path·writer), wiki 분석, CLI, compaction
plugins/persona-vault/   Codex·Claude 공용 plugin (hooks, skill, installer, pvg-client.js)
tests/        Python·Node 테스트
docs/         setup, metadata, gpt-actions, CURATOR 등
```

## Quickstart

전체 절차(Vault repo 준비, deploy key 생성, `.env`, 실행, token 발급)는
[docs/setup.md](docs/setup.md)가 기준입니다. 요약:

1. private Vault 저장소를 만들고 [Vault 규칙](docs/setup.md)을 따릅니다.
2. 서버에서 `.env`(`.env.example` 참고)와 `secrets/persona_vault_sync`,
   `secrets/github_known_hosts`를 직접 생성합니다. 비밀번호는 긴 무작위 값으로 정합니다.
3. `docker compose up -d --build`로 실행하고 `/healthz`로 기동(liveness)을 확인합니다.
4. `/admin/login`에서 PC마다 별도 agent token을 만들고, 처음에는 admin의 `Update RAG index`를
   실행합니다. index가 준비되기 전 `/readyz`는 503을 반환할 수 있으며, 색인 후 `/readyz`를 확인합니다.
5. 각 PC에 plugin과 token helper를 설치합니다 (아래).

기본 바인딩은 `127.0.0.1`입니다. 외부 접근이 필요하면 TLS와 접근 제어가 있는 reverse proxy를
직접 구성하세요. 이 저장소는 그 구성을 제공하지 않습니다.

개발·테스트 환경은 [CONTRIBUTING.md](CONTRIBUTING.md)를 보세요.

### Plugin 설치

Claude Code:

```text
/plugin marketplace add mykim0409/persona-vault-gateway
/plugin install persona-vault@persona-vault-gateway
```

설치 화면에서 hook 명령을 확인·승인한 뒤 `/reload-plugins`를 실행하고 `/hooks`에서
`SessionStart`, `UserPromptSubmit`, `SubagentStop`, `Stop`, `SessionEnd`를 확인합니다.

Codex:

```bash
codex plugin marketplace add mykim0409/persona-vault-gateway
codex plugin add persona-vault --marketplace persona-vault-gateway
```

새 task에서 `/hooks`로 hook 명령을 검토하고 신뢰합니다. 정의가 바뀌면 PC마다 다시 승인합니다.

Token helper(POSIX, Windows PowerShell 포함)의 설치·교체 방법, 설치 위치,
Curator·SessionStart 동작은 [docs/setup.md](docs/setup.md)의 Agent Plugin 절에 있습니다.

## 데이터 경계

- **Vault 전체가 읽힙니다.** read token은 `.git/`, `.obsidian/`, `.tmp/`를 뺀 모든 Markdown을
  읽을 수 있고 `90_Private/`도 포함됩니다. 보안 경계는 쓰기 허용 경로에만 적용됩니다.
- **Cloudflare로 전송됩니다.** 색인 chunk(`90_Private/` 포함)와 검색 query가 embedding을 위해
  Cloudflare Workers AI로 갑니다.
  [Cloudflare 데이터 정책](https://developers.cloudflare.com/workers-ai/platform/data-usage/)은
  고객 콘텐츠를 모델 학습이나 서비스 개선에 쓰지 않는다고 명시합니다.
- **대화가 자동 수집됩니다.** hook은 사용자의 main 요청, subagent 위임·결과, main agent의 최종
  응답을 보냅니다. tool output과 reasoning은 저장하지 않습니다. 비밀 패턴 마스킹은 best effort이므로
  대화에 비밀을 붙여 넣지 마세요.
- **로컬 spool은 평문 JSONL**입니다. DB는 token hash만 저장하지만 암호화하지는 않습니다.
- Admin에는 login rate limit이 없으므로 공개 인터넷에 노출하지 마세요.

자세한 내용은 [SECURITY.md](SECURITY.md)에 있습니다.

## API와 검색 요약

API major version은 URL의 `/gateway/v3`이고 Bearer token 하나만 씁니다. `/gateway/v1`, `/gateway/v2`는
인증 후 write 없이 `410 client_upgrade_required`를 반환합니다. 주요 endpoint는 `capture`,
`search`, `capabilities`, `working-agreement`, `health`이며 정확한 schema는 코드와
[docs/gpt-actions.md](docs/gpt-actions.md)를 기준으로 합니다.

- 모든 capture는 `30_Conversations/raw/`에만 쓰며 Gateway는 새 `40_Agents/` 파일을 만들지 않습니다.
- 일반 검색은 색인을 시작하지 않습니다. index가 없거나 갱신 중이거나 Qdrant·embedding 장애면
  keyword 검색으로 응답하고 `index.search_mode=keyword`로 표시합니다. Vault가 바뀌면
  `index.stale=true`이며 `refresh=true` 또는 admin의 `Update RAG index`가 변경분만 반영합니다.
- `answer_state`: `supported`(현재 지식 근거 있음), `evidence`(raw 근거만 있음), `abstain`(근거 없음·
  환경 불일치), `review_required`(unresolved conflict 확인 필요). 어느 값도 canonical 승격이 아닙니다.
  `machine_corroborated`는 내부 검색 신호이며 공개 API 필드가 아닙니다.
- SQLite는 시작 시 순서대로 migration하며 더 새로운 DB version은 열지 않고 시작을 중단합니다.
- Admin 변경 form은 CSRF token을 요구합니다. Proxy 뒤에서 `Secure` cookie가 붙으려면 Uvicorn의
  `--forwarded-allow-ips`(또는 `FORWARDED_ALLOW_IPS`)가 해당 proxy를 신뢰해야 합니다.

## Curator (선택)

`pvg-wiki`는 Vault를 읽어 health report, compaction 계획, conflict 목록을 보여 주는 로컬 CLI입니다.
자동 apply·삭제·commit·push는 없으며 사람이 승인한 exact plan만 Wiki에 반영합니다.

```bash
uv run pvg-wiki --vault ../persona-vault health
uv run pvg-wiki --vault ../persona-vault conflicts list
```

절차와 승인 경계는 [docs/CURATOR.md](docs/CURATOR.md)에 있습니다. Gateway 사용에 필수가 아닙니다.

## 공개 전 남은 확인

- 보안 점검(위 데이터 경계와 admin·proxy 구성 포함)과 공개 전 QA가 끝나지 않았습니다.
- GitHub private vulnerability reporting은 활성화 여부가 확인되지 않았습니다
  ([SECURITY.md](SECURITY.md)).
- Curator는 실험적입니다. 공유 sync가 finish 검증 전에 commit할 수 있고 raw 삭제가 원자적이지 않은
  동시성 한계가 알려져 있어 안전한 자동 기능이 아닙니다. 사람이 확인하며 쓰세요.
- Windows launcher는 CI(`node tests/test_client_windows.js`)에서 PowerShell 5.1/7 회귀 검증을
  통과했지만, 실제 데스크톱·agent host 통합과 공개 전 전체 QA는 모두 끝나지 않았습니다.
- 위 항목이 끝나기 전에는 공개 사용 준비가 됐다고 가정하지 마세요.

## 문서

- [docs/setup.md](docs/setup.md): 설치와 운영
- [docs/metadata.md](docs/metadata.md): Markdown metadata 계약
- [docs/gpt-actions.md](docs/gpt-actions.md): Custom GPT Actions 설정
- [docs/CURATOR.md](docs/CURATOR.md): Curator protocol (선택)
- [docs/WORKING_AGREEMENT.md](docs/WORKING_AGREEMENT.md): 전역 협업 규칙
- [docs/evaluation-error-book.md](docs/evaluation-error-book.md): 검색 평가 실패 기록 방식
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [LICENSE](LICENSE) (MIT)
