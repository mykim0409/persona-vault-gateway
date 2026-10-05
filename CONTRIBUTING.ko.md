# Contributing

[English](CONTRIBUTING.md) | **한국어**

개인용 self-hosted 베타 프로젝트입니다. 이 문서가 개발·테스트의 단일 안내입니다.
최초 설치는 [docs/setup.ko.md](docs/setup.ko.md), 호스팅은 [docs/hosting.ko.md](docs/hosting.ko.md), 운영은
[docs/operations.ko.md](docs/operations.ko.md), 취약점은
[SECURITY.ko.md](SECURITY.ko.md)를 보세요.

## 환경

- Python 3.13, Node 22, uv `0.12.17` (CI와 Docker가 같은 버전을 고정)
- 의존성은 `uv.lock` 그대로 설치합니다: `uv sync --frozen`
- 빌드 backend는 setuptools(>=77)이고 license는 [MIT](LICENSE)입니다.
- Compose 이미지(Qdrant, Gateway base `python:3.13-slim`)는 multi-arch index digest로
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
uv run --frozen python tests/test_bootstrap.py
uv run --frozen python tests/test_onboarding.py
uv run --frozen python tests/test_deployment_surfaces.py
uv run --frozen python tests/test_release_bundle.py
node tests/test_hook.js
uv run --frozen python tests/test_capture_integration.py
```

| 영역 | 파일 |
| --- | --- |
| 핵심·auth·path 정책 | `test_core.py` |
| admin 화면·login limiter | `test_admin.py` |
| Compose smoke 보조 로직(skip 규칙, backup·restore, 테스트 전용 transport)과 네이티브 `gateway.server` 첫 사용 흐름 | `test_sync.py` |
| RAG 검색 | `test_rag_benchmark.py` |
| wiki·compaction | `test_wiki.py`, `test_compaction.py` |
| SSH deploy key·known_hosts 보조 함수(`gateway.bootstrap`) | `test_bootstrap.py` |
| 릴리스 설치 bundle(번역 문서 쌍 포함) | `test_release_bundle.py` |
| hook·capture | `test_hook.js`, `test_capture_integration.py` |
| 브라우저 설정(`/setup` claim, deploy key, 합성 clone, Gateway 내장 Git sync, readiness) | `test_onboarding.py` |
| 배포 표면 정적 검사(`compose.yml`, `render.yaml`, `.railway/railway.ts`) | `test_deployment_surfaces.py` |

위 Python 테스트는 임시 디렉토리와 합성 데이터만 쓰고 Docker daemon·네트워크·실제 Git host·registry·클라우드 계정이
필요 없습니다(SSH transport는 로컬 bare repository로 대체). 모두 CI에서 실행됩니다.

- `node tests/test_client_windows.js`는 네이티브 Windows(CI `windows-latest`)용입니다. 그 외 환경에서
  실행하지 못했다면 **skip이지 pass가 아닙니다.**
- 개인 Vault, 실제 token, `.env`, 설치된 agent 설정에 대해 테스트를 실행하지 마세요. 실제 Cloudflare
  API를 호출하는 테스트도 두지 않습니다.

## Compose smoke (Docker 필요)

위 목록과 별개이며 Docker CLI, Compose v2 plugin(`docker compose`), 실행 중인 Docker daemon, 이미지
pull·build용 네트워크가 필요합니다. 실제 Docker 검증은 CI의 `compose-smoke` job(ubuntu)이 같은 명령으로 합니다.

```bash
python tests/test_compose_smoke.py
```

실제 `compose.yml`과 `compose.build.yml`(소스 build)에 최소 override를 얹어 고유한 `pvg-smoke-*` project로
실행합니다. 컨테이너 이름, Gateway image tag, volume은 모두 project 전용이고 port는 loopback ephemeral입니다.
Gateway는 첫 설치처럼 claim 전 상태로 시작하며 개인 `.env`나 실제 secret은 읽지 않습니다. 테스트 전용으로 mount한
`sitecustomize.py`가 SSH transport를 합성 bare repository로 대체하고, 운영 코드에는 그런 스위치가 없습니다.
종료 시 자기 project의 container·volume·image tag·임시 파일만 지웁니다. 두 project를 차례로 실행합니다.

- `keyword`: 기본 배포(`EMBEDDING_PROVIDER=none`, Cloudflare 값 없음, Qdrant 없음). pending(`/healthz` 200, `/readyz` 503) →
  claim(잘못된 code 거부) → 합성 clone → search·capture → push·pull → 컨테이너 재생성 후 로그인·token·Vault 유지(`/setup`은
  닫힘) → 중지한 `/data` volume을 새 volume으로 복사해 backup 복원 확인.
- `semantic`: `semantic` profile과 실제 고정 Qdrant, `hash` provider. 색인 전 `rag_indexed`가 true가 아님, 색인 후
  `/readyz` 200, 인증된 search·capture.

- 아래 중 하나라도 해당하면 exit 2(`SKIPPED`)이며 **skip이지 pass가 아닙니다.** Docker CLI가 없음,
  `docker compose version`이 실패함(Compose v2 plugin 없음), 전체 실행에서 `docker info`가 실패함(daemon
  없음). `--prepare-only`도 Docker CLI와 Compose v2가 필요하며(`docker compose config`를 실행), daemon
  없이 임시 파일 생성과 그 검사까지만 합니다.
- Railway·Render 설정은 정적 검사만 합니다(`test_deployment_surfaces.py`와 CI의 Railway typecheck: `.railway`에서
  `npm ci --ignore-scripts && npm run typecheck`). 실제 계정에서 배포하거나 유료 플랫폼에서 테스트하지 마세요. 도구가 없어
  typecheck를 못 돌렸다면 skip이지 pass가 아닙니다.
- 이 skip 조건은 `test_sync.py`가 mock으로 검증하며 실제 Docker는 호출하지 않습니다.
- 한계: 실제 SSH·GitHub deploy key 경로, Cloudflare embedding, 의미 검색 품질, 호스팅 플랫폼 동작은 검증하지 않습니다.
  CI는 amd64 한 환경이라 arm64 실행은 확인하지 않으며 Git sync의 원자성도 검증하지 않습니다.

## 로컬 실행 (격리·합성 데이터)

개인 설정이나 API 없이 임시 합성 경로로만 띄웁니다. `hash` provider는 테스트용이라 실제 의미 검색
품질을 약속하지 않으며, 이 데모 API로 검색 품질을 판단하지 마세요. 프로젝트에 기본 `.env`를 만들지 않습니다.
직접 실행하는 Python 런타임(`uvicorn gateway.app:app`)은 `EMBEDDING_PROVIDER`가 없으면 `cloudflare`가 기본이고(기존 연동 호환),
`gateway.server`와 Compose 기본값 `none`과 다릅니다. 그래서 아래처럼 항상 명시합니다.

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

언어: skill과 reference, 에이전트가 읽는 템플릿, 소스 주석과 docstring, 사용자에게 보이는 UI·CLI 텍스트는 영어로 작성합니다.
Vault 내용을 영어로 써야 한다는 뜻은 아니며, 번역된 README·문서나 다국어 테스트 fixture를 대체하지도 않습니다.

## 브랜치와 commit

- `develop`에서 feature branch를 만들어 PR합니다. `main`에 직접 commit하지 않습니다.
- commit은 하나의 일관된 변경 단위로 나눕니다.

## 버전

Gateway package, plugin, API 버전은 서로 독립입니다. 현재 Gateway package `0.1.0`, plugin `0.7.4`
(Codex는 build suffix 포함), API `v3`입니다. 일반 변경에서 버전을 올리지 않으며, plugin 계약이 바뀔 때 plugin 버전을 올립니다(이번 변경이 그 경우입니다).

## 문서와 알려진 한계

- 설치는 `docs/setup.md`, 호스팅은 `docs/hosting.md`, 운영은 `docs/operations.md`, Wiki 정리 protocol은 `docs/CURATOR.md`입니다.
  `docs/operations.md`는 릴리스 bundle에 포함되어야 합니다(`scripts/build_release_bundle.py` ALLOWLIST).
- 문서는 영어(기본 `.md`)와 한국어(`.ko.md`)로 짝을 이룹니다. 짝이 있는 문서는 같은 변경에서 함께 고치세요. 릴리스 bundle에
  들어가는 문서는 `.ko.md`도 ALLOWLIST에 있어야 합니다. 에이전트용 템플릿(`docs/CURATOR.md`, `docs/WORKING_AGREEMENT.md` 등)은
  영어 원본이 기준이고 한국어 문서는 읽는 사람을 위한 번역입니다. 런타임이 읽는 경로, 템플릿 download URL, `CURATOR.md`
  정식 파일명은 영어 그대로 둡니다.
- 기본 배포는 keyword 검색입니다. semantic 검색은 명시적으로 켠 경우(`EMBEDDING_PROVIDER=cloudflare` +
  `COMPOSE_PROFILES=semantic`)에만 쓰이며 API `v3`는 의미 검색 정확도를 보장하는 계약이 아닙니다.
- 알려진 QA 공백: Windows launcher는 CI에서 PowerShell 5.1/7 회귀 검증을 통과했지만, 실제 데스크톱·agent host 통합과 공개 전 전체 QA는 모두 끝나지 않았고 보안 점검도 끝나지 않았습니다.
- Docker daemon이 없는 환경에서는 Compose smoke를 실행할 수 없으며 그 결과는 통과가 아니라 skip입니다. 네이티브 Windows와 arm64 실행은 해당 환경에서 직접 돌리기 전에는 확인했다고 주장하지 않습니다.
- 의존성을 바꾸면 `uv.lock`도 함께 갱신하고 `uv lock --check`와 `uv sync --frozen`을 확인하세요.

## 릴리스 (maintainer)

- 수동 `Release` workflow(`workflow_dispatch`)를 기본 branch에서 기존 tag `gateway-vX.Y.Z`로 실행합니다.
  tag 버전은 `pyproject.toml`의 Gateway package 버전과 같아야 합니다.
- 전체 CI(테스트, Compose smoke, Windows client)가 통과해야 이미지를 GHCR에 build하고 설치 bundle
  `persona-vault-gateway-X.Y.Z-install.tar.gz`, 단독 `compose.yml`, 각 `.sha256`을 **draft** release로 만듭니다.
  bundle과 `compose.yml`, `render.yaml`, `.railway/railway.ts`의 image 참조는 exact digest로 바뀝니다.
- draft를 publish하기 전에 GHCR 패키지를 public으로 바꾸고 익명 pull을 확인합니다. 전역 Docker 로그인은
  그대로 두고 임시 설정 디렉토리를 씁니다.

  ```bash
  DOCKER_CONFIG="$(mktemp -d)" docker pull ghcr.io/OWNER/REPO@sha256:<digest>
  ```

- 이미지 digest는 draft의 release notes에 있습니다.
- 공개 Release와 public GHCR 게시는 위 QA(CI, 익명 pull 확인)가 끝난 뒤에만 합니다. Render·Railway 설정은 배포하지 않은
  상태로 유지합니다.
