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
  Gateway에 보냅니다. 비밀 패턴 마스킹은 best effort이며 비밀이 지워진다는 보장은 없습니다.
  대화에 비밀을 붙여 넣지 마세요.
- **Cloudflare**: 기본 embedding은 Cloudflare Workers AI이며 Vault 문서 chunk(`90_Private/` 포함)와
  검색 query가 Cloudflare로 전송됩니다.
- **로컬 spool**: hook의 재시도용 spool은 각 PC에 평문 JSONL로 남습니다.

## 운영 전제

- 기본 바인딩은 loopback(`127.0.0.1`)입니다. 외부 공개가 필요하면 TLS와 접근 제어(VPN·IP
  allowlist 등)를 reverse proxy에서 직접 구성해야 하며 이 저장소는 그 구성을 제공하지 않습니다.
- Admin 화면과 Qdrant는 공개 인터넷에 노출하지 마세요. Admin에는 내장 login rate limit이 없습니다.
- 소스와 Git 이력이 기준이며, 운영 중인 DB·Vault·Qdrant 상태는 별도로 백업·관리해야 합니다.
- 기본값·예시 비밀번호를 쓰지 말고 `ADMIN_PASSWORD`, Cloudflare token, SSH key를 직접
  생성하세요([docs/setup.md](docs/setup.md)).

## 취약점 신고 (Reporting)

GitHub private vulnerability reporting을 사용할 계획입니다. 활성화되면 저장소의 Security 탭에서
비공개로 신고할 수 있습니다.

> **상태: 활성화 여부 미검증.** 저장소가 private인 동안 확인 API가 404를 반환했습니다.
> 공개 전에 maintainer가 저장소 Settings → Security(Code security) 설정에서 활성화하고 Security 탭에
> 비공개 신고 버튼이 나타나는지 확인해야 합니다. 방법:
> [Configuring private vulnerability reporting for a repository](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository).
> 확인 전에는 이 경로가 작동한다고 가정하지 마세요.

- 비밀, token, exploit 코드, 재현 상세를 공개 issue·PR·discussion에 올리지 마세요.
- 비공개 경로가 없으면 비밀·상세 없이 연락을 요청하는 최소한의 공개 issue만 열어 주세요.
- 응답 시간이나 수정 기한은 약속하지 않습니다. 개인 프로젝트로 best effort로만 대응합니다.
