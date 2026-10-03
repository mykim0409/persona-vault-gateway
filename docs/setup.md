# PersonaVault Gateway 설치

서버에 Gateway를 설치하고, 각 PC에 plugin을 연결합니다. 기본은 keyword 검색입니다.
Vault 구조, semantic 검색, 원격 접근, 업그레이드, 백업, 문제 해결은 [operations.md](operations.md)에 있습니다.

필요한 것: 서버의 Docker Compose v2, 각 PC의 Node.js, GitHub 계정.
Linux에서 권한이 필요하면 `docker` 앞에 `sudo`를 붙입니다.

## 0. Vault 저장소

기존 Vault 저장소(commit이 있는 private GitHub 저장소)가 있으면 그대로 씁니다. 새로 만든다면 **private**
저장소를 만들고 commit을 하나 만듭니다(예: README 추가).

## 1. 설치 파일 준비

설치 디렉토리는 한 번 정하면 바꾸지 않습니다. Compose project 이름이 디렉토리 이름에서 정해지고,
데이터 volume이 그 이름에 묶입니다. 아래 `~/persona-vault-gateway`를 그대로 쓰는 것을 권장합니다.

**설치 bundle** — [GitHub Releases](https://github.com/mykim0409/persona-vault-gateway/releases)에서
`persona-vault-gateway-X.Y.Z-install.tar.gz`와 `.sha256`을 받습니다.

```bash
sha256sum -c persona-vault-gateway-X.Y.Z-install.tar.gz.sha256    # macOS: shasum -a 256 -c
tar -xzf persona-vault-gateway-X.Y.Z-install.tar.gz
mv persona-vault-gateway-X.Y.Z ~/persona-vault-gateway
cd ~/persona-vault-gateway
```

**소스** (release 없이 사용):

```bash
git clone https://github.com/mykim0409/persona-vault-gateway.git ~/persona-vault-gateway
cd ~/persona-vault-gateway
docker compose -f compose.yml -f compose.build.yml build persona-vault-gateway
```

## 2. 초기화와 시작

```bash
docker compose run --rm persona-vault-init
```

GitHub SSH URL(`git@github.com:OWNER/REPO.git`)과 admin 비밀번호(16~128자, `'` 불가)를 입력합니다.
설치 디렉토리에 `.env`, `secrets/persona_vault_sync`(private key), `secrets/github_known_hosts`가 생기고
**public** key가 출력됩니다. 기존 파일은 덮어쓰지 않습니다.

Vault 저장소의 Settings → Deploy keys → Add deploy key에 public key를 등록하고
**Allow write access**를 켭니다.

```bash
docker compose run --rm persona-vault-init --check    # 읽기 전용 접속 확인
docker compose up -d
```

`--check`는 쓰기 권한을 확인하지 못합니다. 쓰기 권한이 없으면 sync push가 실패합니다.
`.env`와 `secrets/`는 Git에 올리지 않습니다.

## 3. 접속과 token

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz     # 200, "semantic":"disabled"
```

`http://127.0.0.1:18080/admin/login`에서 admin 비밀번호로 로그인하고 `/admin/tokens`에서 PC마다 token을
발급합니다. token은 한 번만 표시됩니다. 일반 plugin은 `Read + Write`, 검색 전용은 `Read`입니다.

Gateway는 기본적으로 서버의 `127.0.0.1`에만 열립니다. 다른 PC에서 쓰려면 운영자가 정한 암호화된
경로(사설 네트워크 또는 TLS endpoint)로 닿는 주소를 plugin의 Gateway URL로 씁니다. 평문 HTTP로
공개하면 token과 admin 비밀번호가 그대로 노출됩니다. 자세한 내용은 [operations.md](operations.md#원격-접근)를
보세요.

주의: read token은 `90_Private/`를 포함한 Vault 전체를 읽고, 자동 수집은 대화 내용을 평문으로 Gateway와
Git에 저장합니다. [SECURITY.md](../SECURITY.md)를 읽으세요.

## 4. Agent Plugin 설정

각 PC마다 별도 agent id와 token을 씁니다. Node.js가 필요합니다.

Claude Code:

```text
/plugin marketplace add mykim0409/persona-vault-gateway
/plugin install persona-vault@persona-vault-gateway
/reload-plugins
```

Codex:

```bash
codex plugin marketplace add mykim0409/persona-vault-gateway
codex plugin add persona-vault --marketplace persona-vault-gateway
```

`/hooks`에서 PersonaVault hook 명령을 확인한 뒤 신뢰합니다.

token helper를 설치하면 Gateway URL과 token을 묻습니다. token은 에이전트 대화에 붙여 넣지 마세요.

<details>
<summary>macOS / Linux</summary>

```bash
base=https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts
d="$(mktemp -d)" && curl -fsSLo "$d/install-agent-config.sh" "$base/install-agent-config.sh" \
  && curl -fsSLo "$d/pvg-client.js" "$base/pvg-client.js" \
  && sh "$d/install-agent-config.sh"
```

token 교체: 같은 installer를 `--replace-token`으로 다시 실행합니다.
</details>

<details>
<summary>Windows PowerShell</summary>

```powershell
$base = 'https://raw.githubusercontent.com/mykim0409/persona-vault-gateway/main/plugins/persona-vault/scripts'
$d = Join-Path $env:TEMP 'persona-vault-installer'
New-Item -ItemType Directory -Force -Path $d | Out-Null
foreach ($f in 'install-agent-config.ps1', 'pvg-client.js') { Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $d $f) "$base/$f" }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'install-agent-config.ps1')
```

token 교체: `install-agent-config.ps1`을 `-ReplaceToken`으로 다시 실행합니다.
명령 문법은 [Windows 안내](https://github.com/mykim0409/persona-vault-gateway/blob/main/plugins/persona-vault/skills/persona-vault/references/windows.md)를 따릅니다.
</details>

설치 확인:

```bash
pvg-rag-search --view current "현재 운영 정책"
```

Windows는 `& "$HOME\.local\bin\pvg-rag-search.ps1" --view current "현재 운영 정책"`입니다.
