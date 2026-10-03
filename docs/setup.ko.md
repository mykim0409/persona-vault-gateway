# PersonaVault Gateway 설치

[English](setup.md) | **한국어**

Gateway를 서버에 올리고 브라우저(`/setup`)에서 설정한 뒤, 각 PC에 plugin을 연결합니다. 기본은 keyword 검색입니다.
Docker, Railway, Render 모두 같은 image와 같은 설정 화면을 씁니다. 플랫폼은 [hosting.ko.md](hosting.ko.md), Vault 구조·semantic 검색·
원격 접근·업그레이드·백업·문제 해결은 [operations.ko.md](operations.ko.md)를 보세요.

필요한 것: 서버의 Docker Compose v2, 각 PC의 Node.js, GitHub 계정.
Linux에서 권한이 필요하면 `docker` 앞에 `sudo`를 붙입니다.

## 0. Vault 저장소

**private** GitHub 저장소를 준비하고 commit을 하나 이상 만들어 둡니다(예: README 추가). 빈 저장소는 연결할 수 없습니다.

## 1. Gateway 시작 (서버에서)

새 설치는 전용 영구 디렉토리에서 하세요(Compose project와 volume 식별이 디렉토리에 달려 있어 업그레이드 때도 유지합니다).

```bash
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml
curl -fsSLO https://github.com/mykim0409/persona-vault-gateway/releases/latest/download/compose.yml.sha256
sha256sum -c compose.yml.sha256        # macOS: shasum -a 256 -c
docker compose up -d
docker compose logs                    # one-time setup code
```

`.env`나 key 파일은 필요 없습니다. 데이터(Vault clone, SQLite, 설정)는 volume `persona-vault-data`(컨테이너 `/data`) 하나에
저장됩니다. `PVG_SETUP_TOKEN`(20~200자)을 직접 지정하면 그 값이 setup code이고, 아니면 로그에 한 번 출력됩니다.
소스에서 쓰려면 저장소를 clone하고 `docker compose -f compose.yml -f compose.build.yml up -d --build`를 실행합니다.

## 2. 브라우저에서 설정 (내 PC에서)

Compose는 기본적으로 포트를 서버의 `127.0.0.1`에만 게시하므로, 서버가 아닌 PC에서는 SSH tunnel을 쓸 수 있습니다. PC에서 아래 명령을
실행하고 열어 둡니다(출력은 없습니다).

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:18080 user@SERVER_IP
```

`user`는 서버의 로그인 이름, `SERVER_IP`는 서버의 주소나 호스트 이름입니다. tunnel이 열려 있는 동안 `http://127.0.0.1:18080`이
브라우저와 plugin 설치 프로그램의 Gateway URL이며, 나중에 plugin을 쓸 때도 tunnel을 열어 두어야 합니다.
서버가 이 PC라면 tunnel은 필요 없습니다. 호스팅 서비스나 이미 구성된 암호화 주소는 해당 HTTPS URL을 Gateway URL로 씁니다.

Gateway URL의 `/setup`(예: `http://127.0.0.1:18080/setup`)을 엽니다. 설정을 마치기 전에는 `/healthz`만 200이고 `/readyz`는 503이며 검색과 capture는 꺼져 있습니다.

1. setup code와 admin 비밀번호(16~128자)를 입력해 **Claim this Gateway**.
2. **Repository SSH URL**에 `git@github.com:OWNER/REPO.git`을 입력하고 **Generate deploy key**.
3. 표시된 **public** key를 저장소의 Settings → Deploy keys에 등록하고 **Allow write access**를 켭니다.
4. **Connect and clone**(실패하면 **Retry**). 완료되면 Vault가 연결되고 `/readyz`가 200이 됩니다.
5. Agent tokens(`/admin/tokens`)에서 PC마다 token을 발급합니다. token은 한 번만 표시됩니다. 일반 plugin은 `Read + Write`, 검색 전용은 `Read`입니다.

연결 단계는 쓰기 권한을 증명하지 않습니다. 권한이 없으면 이후 sync의 push가 거부되고 Vault sync 화면에 표시됩니다.
setup code는 채팅에 붙여 넣지 마세요.

Compose는 포트를 서버의 `127.0.0.1`에만 게시합니다(네이티브 서버는 `0.0.0.0`, 호스팅 서비스는 공개 HTTPS라 loopback이 아닙니다).
평문 HTTP를 공개하지 마세요. token과 admin 비밀번호가 노출됩니다. [operations.ko.md](operations.ko.md#원격-접근)를 보세요.

주의: read token은 `90_Private/`를 포함한 Vault 전체를 읽고, 자동 수집은 대화 내용을 평문으로 Gateway와
Git에 저장합니다. [SECURITY.ko.md](../SECURITY.ko.md)를 읽으세요.

## 3. Agent Plugin 설정 (각 PC에서)

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

설치 확인은 메모 하나를 저장하고 다시 찾는 것입니다(tunnel을 쓴다면 열어 둔 채로). helper를 전체 경로로 호출하므로 PATH 새로고침이 필요 없습니다.
macOS / Linux:

```bash
printf '%s\n' "For this project, record the reason for deployment decisions." \
  | "$HOME/.local/bin/pvg-agent-memo" --title "Deployment notes" --project Example
"$HOME/.local/bin/pvg-rag-search" --view evidence "deployment decisions"
```

Windows PowerShell:

```powershell
"For this project, record the reason for deployment decisions." |
  & "$HOME\.local\bin\pvg-agent-memo.ps1" --title 'Deployment notes' --project Example
& "$HOME\.local\bin\pvg-rag-search.ps1" --view evidence "deployment decisions"
```

memo 명령은 저장된 `path`(`30_Conversations/raw/` 아래)를 포함한 JSON을 출력하고, 검색에는 같은 메모가 나와야 합니다. 이는 Vault에 raw note를
쓰는 것이며 승인된 지식이 아니므로, 새 Vault에서는 `--view current`가 비거나 abstain하는 것이 정상일 수 있습니다.
