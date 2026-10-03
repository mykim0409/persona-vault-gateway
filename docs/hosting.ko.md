# 호스팅

[English](hosting.md) | **한국어**

Gateway는 어디에 올려도 같은 방식으로 설치됩니다. 같은 image, 같은 시작 명령(`python -m gateway.server`), 같은 브라우저 설정 화면(`/setup`)이며
플랫폼별 런타임 fork는 없습니다. 설정 절차는 [setup.ko.md](setup.ko.md)입니다.

> Railway와 Render 설정 파일은 **배포하지도 검증하지도 않았습니다.** 정적 검사만 거쳤고 어느 계정도 쓰지 않았습니다. 두 곳 모두
> 유료 자원이며, 원클릭 배포 버튼이나 template ID는 없습니다. 계정, 비용, SSL, proxy 라우팅, Vault 저장소로의 SSH 도달 가능 여부는
> 운영자가 직접 확인합니다.

## 공통 계약

- port는 `PORT`(기본 `8000`), 검색은 keyword(`EMBEDDING_PROVIDER=none`)입니다.
- 영속 volume 하나를 `/data`에 붙입니다: Vault `/data/vault`, SQLite `/data/gateway.db`, 설정 `/data/setup`(private 설정·hash·key).
  volume이 없으면 재배포마다 설정과 Vault clone이 사라집니다.
- **인스턴스는 정확히 하나**입니다(Git worker와 설정 상태가 프로세스 안에 있음). replica나 autoscale을 켜지 마세요.
- 설정 전에는 `/healthz`가 200이고 `/readyz`는 503이며 검색과 capture는 꺼져 있습니다.
- setup code는 컨테이너 로그에 한 번 출력되거나, `PVG_SETUP_TOKEN`(20~200자 printable ASCII)을 플랫폼 secret으로 지정합니다.
  채팅·이슈에 붙여 넣지 마세요.
- 플랫폼이 HTTPS를 종단하고 앞단 주소를 모르므로 Railway·Render 설정은 `PVG_SECURE_COOKIES=true`를 둡니다(forwarded 헤더를 신뢰하지
  않고 Secure cookie만 강제하므로 HTTPS로만 접속해야 로그인됩니다). Compose는 기본 `false`입니다.
  [operations.ko.md](operations.ko.md#원격-접근)를 보세요.
- TLS와 접근 제어는 플랫폼 또는 운영자 책임입니다. 이 저장소는 proxy, 인증서, DDNS를 제공하지 않습니다.

## 비교

| 선택지 | 이 저장소의 표면 | 상태 | 비고 |
| --- | --- | --- | --- |
| Docker Compose | `compose.yml` | CI의 Compose smoke로 검증 | loopback `127.0.0.1:18080`. 사설 경로·TLS는 운영자 |
| Railway | `.railway/railway.ts` | 미배포·미검증 | 고정 release image. volume은 한 서비스에만 연결. domain은 수동 생성 |
| Render | `render.yaml` | 미배포·미검증 | 고정 release image. 영속 disk가 있는 유료 instance 필요 |
| Fly.io | 없음 | 조사만 | 공식 문서상 volume 가능, 제공 표면 없음 |
| DigitalOcean App Platform | 없음 | 조사만 | 영속 volume이 없어 적합하지 않음 |

## Docker Compose

[setup.ko.md](setup.ko.md#1-gateway-시작)의 명령 그대로입니다. 다른 PC에서 쓰려면 SSH tunnel 같은 암호화된 사설 경로나 운영 중인
TLS endpoint를 쓰고 평문 공개 HTTP는 쓰지 마세요.

## Railway

[공식 문서](https://docs.railway.com/infrastructure-as-code)상 새 서비스는 `railway.toml`/`railway.json`을 쓸 수 없어 TypeScript SDK 기반의
범위가 제한된 도구(`.railway/`의 lockfile 고정 SDK)를 씁니다. `.railway/railway.ts`는 서비스 하나, `/data` volume 하나, replica 1개를
선언하고 `autoUpdates`를 끈 고정 release image를 씁니다(release에서 tag가 exact digest로 바뀝니다). 소스 저장소를 감시하지 않으므로
push로 재배포되지 않고, 버전은 직접 바꿉니다.

- volume은 [한 서비스에만 연결](https://docs.railway.com/reference/volumes)됩니다. 삭제하거나 분리하지 마세요.
- 생성된 domain은 파일에서 관리하지 않으므로 UI에서 직접 만듭니다. `PVG_SETUP_TOKEN`은 대시보드에서 직접 설정하거나(선택),
  없으면 deploy log에서 읽습니다.

## Render

[Blueprint spec](https://render.com/docs/blueprint-spec)과 [disks](https://render.com/docs/disks) 기준입니다. 영속 disk는 유료 plan
(0.5 CPU / 512 MB)에서만 붙고 disk가 붙은 서비스는 단일 instance입니다. `render.yaml`은 `runtime: image`로 고정 release image(시작 명령은
image의 기본값 `python -m gateway.server`), `/data` disk, 자동 배포 끔(`autoDeployTrigger: "off"`), 생성 시 `PVG_SETUP_TOKEN` 자동
생성을 담습니다. 온라인 URL이 바로 열리므로
`/setup`을 열기 전에 대시보드 Environment에서 그 값을 읽으세요. 커스텀 도메인·proxy·DNS는 만들지 않습니다.
Blueprint를 만들면 비용이 발생하므로 확인한 뒤 직접 진행하세요.

## 조사만 한 선택지

- **Fly.io**: [configuration](https://docs.fly.io/reference/configuration)과 [volume](https://docs.fly.io/launch/volume-storage)로 이론상
  가능하지만 이 저장소는 설정을 제공하지 않습니다. 쓴다면 volume이 한 machine에 묶인다는 점을 직접 확인하세요.
- **DigitalOcean App Platform**: [공식 문서](https://docs.digitalocean.com/products/app-platform/how-to/store-data/)상 컨테이너
  파일시스템이 영속적이지 않아 `/data` 계약과 맞지 않습니다. 권장하지 않습니다.

## 운영자가 확인할 것

계정과 결제, 도메인과 SSL, proxy 라우팅, 플랫폼에서 GitHub SSH(port 22)로 나가 Vault 저장소에 닿는지, volume 백업.
이 항목들은 저장소 파일로 보장되지 않습니다. 공개 HTTPS 서비스는 `/setup`과 `/admin`도 함께 공개합니다. claim 전에는 운영자만 아는
setup code가, 후에는 admin 비밀번호·세션·CSRF·login 제한이 보호하므로 claim을 서둘러 끝내고 code를 공유하지 마세요.
제공된 Railway·Render 설정은 admin을 격리하지 않으며, 격리하려면 플랫폼의 접근 제어를 직접 추가해야 합니다. 평문 HTTP는 쓰지 마세요.
