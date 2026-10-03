# Security Policy

PersonaVault Gateway는 개인이 직접 운영하는 self-hosted 베타 소프트웨어이며 SaaS가 아닙니다.
보안 감사를 완료했다고 주장하지 않고, 어떤 보안 보장도 제공하지 않습니다
(라이선스: [MIT](LICENSE), 무보증).

## 알아야 할 데이터 경계

- **Vault 읽기 범위**: read 권한 agent token은 색인 제외 기술 디렉토리(`.git/`, `.obsidian/`,
  `.tmp/`)를 뺀 모든 Markdown을 읽을 수 있습니다. `90_Private/`도 포함됩니다.
  보안 경계는 쓰기 허용 경로에만 적용됩니다.
- **Token**: raw token은 발급 시 한 번만 표시하고 DB에는 hash만 저장합니다. SQLite DB 자체는
  암호화하지 않습니다.
- **대화 자동 수집**: plugin hook은 사용자 요청, subagent 결과, 최종 응답을 raw conversation으로
  Gateway에 보냅니다. hook은 로컬 저장 전과 전송 시 결정적 필터로 비밀 패턴을 가리고 인식된 코드·설정·
  diff·log block을 생략하며 절대 cwd 메타데이터는 프로젝트 디렉터리 이름만 남깁니다(추가 LLM 호출 없음).
  메시지 본문에 적은 경로는 일반적으로 가려지지 않습니다. best effort이며 DLP 보장이
  아닙니다. 기존 spool·Git 이력은 소급 정리되지 않고, 직접 API·수동 Vault 파일·Cloudflare 입력은
  이 필터의 대상이 아닙니다. 대화에 비밀을 붙여 넣지 마세요.
- **Cloudflare (선택)**: 기본 배포는 keyword 전용(`EMBEDDING_PROVIDER=none`)이라 Cloudflare로 아무것도
  보내지 않습니다. semantic 검색을 직접 켠 경우(`EMBEDDING_PROVIDER=cloudflare`, 자격 증명, Qdrant)에만 Vault 문서
  chunk(`90_Private/` 포함)와 검색 query가 Cloudflare Workers AI로 전송됩니다.
- **초기 설정(`/setup`)**: setup code(로그에 한 번 출력되거나 `PVG_SETUP_TOKEN`)는 최초 claim 전의 유일한 인증입니다.
  채팅·이슈·로그 공유에 붙여 넣지 마세요. claim하면 생성된 code 파일은 지워지고 `/setup`은 더 이상 열리지 않으며,
  claim 시도는 client 주소별로 제한됩니다. admin 비밀번호는 salted hash로만 저장합니다. private deploy key는 `/data/setup`의
  private 파일에만 있고 화면에는 public key만 나옵니다. 공개 HTTPS로 호스팅하면 `/setup`과 `/admin`도 같은 서비스로 공개됩니다.
  claim 전에는 운영자만 아는 setup code가, claim 후에는 admin 비밀번호·세션·CSRF·login 제한이 보호합니다. 그러니 claim을 서둘러
  끝내고 code를 공유하지 말며 `PVG_SETUP_TOKEN`은 플랫폼 secret으로 다루세요. 제공된 Railway·Render 설정은 admin을 격리하지
  않습니다. 연결 단계는 write 권한을 증명하지 않으며 push 거부는 sync 상태에 나타납니다.
- **로컬 spool**: hook의 재시도용 spool은 각 PC에 평문 JSONL로 남습니다.

## 운영 전제

- 기본 바인딩은 loopback(`127.0.0.1`)입니다. 원격 암호화 연결은 운영자 책임입니다. Gateway의 IP·port는
  암호화된 사설 경로(VPN, tunnel 등)나 이미 운영 중인 TLS endpoint로만 닿게 하세요. 특정 proxy는 필요
  없고 이 저장소는 proxy, 인증서·ACME 자동화, 서버·계정 provisioning을 제공하지 않습니다. 평문 공개
  HTTP로 노출하면 agent token, admin 비밀번호, 수집된 대화가 암호화 없이 전송되므로 안전하지 않습니다.
- Qdrant는 비공개로 두고 포트를 공개하지 마세요. Admin은 비공개 배포라면 가능한 네트워크·접근 제어로 제한하세요. Admin login에는 client 주소당 5분에 5회 제한이
  있고 초과 시 `429`와 `Retry-After`를 반환합니다. 이 제한은 프로세스 메모리에서만 동작하고 추적 수가
  제한되며, 재시작하면 초기화되고 프로세스·replica 간에 공유되지 않으므로 네트워크 보안을 대체하지
  못합니다. 기존 CSRF 보호는 그대로입니다. forwarded 헤더는 정확한 IP로 신뢰하도록 설정한 proxy에서만
  반영되며 wildcard(`*`)로 신뢰하지 마세요.
- 소스와 Git 이력이 기준이며, 운영 중인 DB·Vault·Qdrant 상태는 별도로 백업·관리해야 합니다.
  `/data` 백업에는 deploy key의 private key, admin hash, push되지 않은 Vault Git 내용, SQLite가 포함되므로 암호화해 보관하세요.
- 예시 비밀번호를 쓰지 마세요. admin 비밀번호는 `/setup`에서 직접 정하고, semantic 검색을 켠다면 Cloudflare token을 직접
  발급하세요([docs/setup.md](docs/setup.md)). 등록하는 것은 public key뿐이며 private key는 서버 밖으로 내지 마세요.
- 예전 버전의 별도 `/vault`·`/data` volume, `.env`, 호스트 key는 자동 migration되지 않습니다. 옮기기 전에 백업하고
  예전 volume을 지우지 마세요([docs/operations.md](docs/operations.md#업그레이드)).

## 남은 베타 위험

- 보안 감사 미완료, 브라우저 설정 경로는 새 코드이며 claim 전에 code가 노출되면 제3자가 먼저 claim할 수 있습니다.
- admin login 제한은 프로세스 메모리 기반이고, 단일 프로세스 Gateway는 replica로 확장할 수 없습니다.
- SQLite와 deploy key가 같은 volume에 있어 volume 접근이 곧 Vault 쓰기 권한입니다.
- Release asset과 image의 서명·provenance 검증은 제공하지 않습니다(SHA-256 checksum과 image digest만). Railway·Render 설정은 배포하거나 실제 계정에서 검증하지 않았습니다.
- HTTPS 앞단 주소를 모르는 플랫폼에서는 `PVG_SECURE_COOKIES=true`가 Secure cookie만 강제합니다. forwarded 헤더(`FORWARDED_ALLOW_IPS`)는 정확한 IP로만 신뢰하고 `*`는 쓰지 마세요.

## 취약점 신고 (Reporting)

저장소 Security 탭에 비공개 신고 버튼이 보이면 그것으로 신고하세요.

> **상태: 확인 필요.** 저장소는 공개되었지만 GitHub private vulnerability reporting이 활성화되어 있는지는
> 아직 확인되지 않았습니다. 이 문서는 해당 기능이 켜져 있다고 주장하지 않습니다. maintainer가 저장소
> Settings → Security(Code security)에서 활성화하고 Security 탭에 비공개 신고 버튼이 나타나는지 확인해야
> 합니다. 방법:
> [Configuring private vulnerability reporting for a repository](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository).

- 비밀, token, exploit 코드, 재현 상세를 공개 issue·PR·discussion에 올리지 마세요.
- 비공개 경로가 보이지 않으면 비밀·상세 없이 연락을 요청하는 최소한의 공개 issue만 열어 주세요.
- 응답 시간이나 수정 기한은 약속하지 않습니다. 개인 프로젝트로 best effort로만 대응합니다.
