# Contributing

개인용 self-hosted 베타 프로젝트입니다. 이 문서가 개발·테스트의 단일 안내입니다.
운영 설치는 [docs/setup.md](docs/setup.md), 취약점은 [SECURITY.md](SECURITY.md)를 보세요.

## 환경

- Python 3.13, Node 22, uv `0.12.17` (CI와 Docker가 같은 버전을 고정)
- 의존성은 `uv.lock` 그대로 설치합니다: `uv sync --frozen`
- 빌드 backend는 setuptools(>=77)이고 license는 [MIT](LICENSE)입니다.
- Compose 이미지(Qdrant, `alpine/git`, Gateway base `python:3.13-slim`)는 multi-arch index digest로
  고정합니다. digest 갱신은 의도적인 변경이며, apt 패키지와 build backend는 고정되지 않아 build가
  bit 단위로 같다고 보장하지 않습니다.

## 테스트

CI(`.github/workflows/ci.yml`)와 같은 명령입니다. 모두 실제 API·서비스 없이 동작합니다.

```bash
uv sync --frozen
uv run --frozen python tests/test_core.py
uv run --frozen python tests/test_admin.py
uv run --frozen python tests/test_sync.py
uv run --frozen python tests/test_rag_benchmark.py
uv run --frozen python tests/test_wiki.py
uv run --frozen python tests/test_compaction.py
node tests/test_hook.js
uv run --frozen python tests/test_capture_integration.py
```

| 영역 | 파일 |
| --- | --- |
| 핵심·auth·path 정책 | `test_core.py` |
| admin 화면 | `test_admin.py` |
| vault sync | `test_sync.py` |
| Compose 계약·초기 clone readiness | `test_sync.py` (compose.yml 텍스트와 sync script를 임시 경로에서 검사) |
| RAG 검색 | `test_rag_benchmark.py` |
| wiki·compaction | `test_wiki.py`, `test_compaction.py` |
| hook·capture | `test_hook.js`, `test_capture_integration.py` |

- `node tests/test_client_windows.js`는 네이티브 Windows(CI `windows-latest`)용입니다. 그 외 환경에서
  실행하지 못했다면 **skip이지 pass가 아닙니다.**
- 개인 Vault, 실제 token, `.env`, 설치된 agent 설정에 대해 테스트를 실행하지 마세요. 실제 Cloudflare
  API를 호출하는 테스트도 두지 않습니다.

## Compose smoke (Docker 필요)

위 목록과 별개이며 Docker CLI, Compose v2 plugin(`docker compose`), 실행 중인 Docker daemon, 이미지
pull·build용 네트워크가 필요합니다. CI에서는 `compose-smoke` job(ubuntu)이 같은 명령을 실행합니다.

```bash
python tests/test_compose_smoke.py
```

실제 `compose.yml`에 최소 override를 얹어 고유한 `pvg-smoke-*` project로 실행합니다. 컨테이너 이름, Gateway
image tag(`persona-vault-gateway:local`은 쓰지 않음), volume은 모두 project 전용이고 port는 loopback
ephemeral입니다. 임시 bare Git repo(합성 Markdown 한 건)를 sync에만 `file://`로 mount하며 dummy
secret·`--env-file`만 쓰고 개인 `.env`는 읽지 않습니다. 확인 항목: 최초 clone이 끝나기 전에는 Gateway가
시작하지 않음, `/healthz`, 색인 전 `rag_indexed`가 true가 아님, `hash` provider로 색인 후 `/readyz` 200,
인증된 search·capture. 종료 시 자기 project의 container·volume·image tag·임시 파일만 지웁니다.

- 아래 중 하나라도 해당하면 exit 2(`SKIPPED`)이며 **skip이지 pass가 아닙니다.** Docker CLI가 없음,
  `docker compose version`이 실패함(Compose v2 plugin 없음), 전체 실행에서 `docker info`가 실패함(daemon
  없음). `--prepare-only`도 Docker CLI와 Compose v2가 필요하며(`docker compose config`를 실행), daemon
  없이 임시 파일 생성과 그 검사까지만 합니다.
- 이 skip 조건은 `test_sync.py`가 mock으로 검증하며 실제 Docker는 호출하지 않습니다.
- 한계: SSH deploy key·known_hosts 경로, Cloudflare embedding, 의미 검색 품질, clone 실패 시 Compose
  동작은 검증하지 않습니다(clone 실패의 marker 처리는 `test_sync.py`의 script 수준 테스트만 다룹니다).
  CI는 amd64 한 환경이라 arm64 실행은 확인하지 않습니다. 최초 clone 준비 상태를 보는 것이며 이후 Git
  sync의 원자성은 검증하지 않습니다.

## 로컬 실행 (격리·합성 데이터)

개인 설정이나 API 없이 임시 합성 경로로만 띄웁니다. `hash` provider는 테스트용이라 실제 의미 검색
품질을 약속하지 않으며, 이 데모 API로 검색 품질을 판단하지 마세요. 프로젝트에 기본 `.env`를 만들지 않습니다.

```bash
tmp="$(mktemp -d)" && mkdir "$tmp/vault"
VAULT_DIR="$tmp/vault" DB_PATH="$tmp/gateway.db" ADMIN_PASSWORD="$(openssl rand -hex 16)" \
  EMBEDDING_PROVIDER=hash uv run --frozen uvicorn gateway.app:app --host 127.0.0.1 --port 8000
```

`curl http://127.0.0.1:8000/healthz`로 확인하고, 끝나면 `$tmp`를 지웁니다.

## 릴리스 전 secret 점검 (선택)

[gitleaks](https://github.com/gitleaks/gitleaks)를 설치한 뒤 저장소 루트의 `.gitleaks.toml`
(기본 규칙 + 커스텀 pvg 규칙)로 Git 이력 전체를 검사합니다.

```bash
gitleaks git . --log-opts="--branches --remotes --tags --full-history" --redact=100 --ignore-gitleaks-allow
```

- 작업 디렉토리 검사(`dir .`)는 쓰지 마세요. 로컬 `.env` 등 비밀 파일이 포함됩니다.
- 결과에 실제 credential 값을 출력하거나 issue·PR에 붙이지 말고, 실제 credential을
  allowlist에 넣지 마세요. 노출된 credential은 먼저 폐기(revoke)합니다.

## Plugin 구조 원칙

하나의 공유 Node client(`pvg-client.js`)와 하나의 `persona-vault` skill을 Codex·Claude가 함께 씁니다.
host manifest와 OS별 launcher만 표면 차이를 가집니다. 명령 이름·flag는 모든 OS에서 같게 유지하세요.

Language: skills and references, agent-facing templates, source comments and docstrings, and user-facing UI/CLI text are authored in English. This does not require Vault content to be in English, and it does not replace the localized README/docs or multilingual test fixtures.

## 브랜치와 commit

- `develop`에서 feature branch를 만들어 PR합니다. `main`에 직접 commit하지 않습니다.
- commit은 하나의 일관된 변경 단위로 나눕니다.

## 버전

Gateway package, plugin, API 버전은 서로 독립입니다. 현재 Gateway package `0.1.0`, plugin `0.7.3`
(Codex는 build suffix 포함), API `v3`입니다. 일반 변경에서 버전을 올리지 않습니다.

## 문서와 알려진 한계

- 운영·설치는 `docs/setup.md`, Wiki 정리 protocol은 `docs/CURATOR.md`입니다. Curator는 선택 사항이며
  Gateway 사용에 필수가 아닙니다.
- 검색은 semantic+keyword 혼합이며 API `v3`는 의미 검색 정확도를 보장하는 계약이 아닙니다.
- 알려진 QA 공백: Windows launcher는 CI에서 PowerShell 5.1/7 회귀 검증을 통과했지만, 실제 데스크톱·agent host 통합과 공개 전 전체 QA는 모두 끝나지 않았고 보안 점검도 끝나지 않았습니다.
- 의존성을 바꾸면 `uv.lock`도 함께 갱신하고 `uv lock --check`와 `uv sync --frozen`을 확인하세요.
