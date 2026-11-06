# Quartz 퍼블리시 렌더러 운영 가이드

볼트별로 선택할 수 있는 두 번째 퍼블리시 렌더러입니다. 켜지 않은 볼트는 기존 렌더러가 그대로 서빙합니다.
검증 결과와 근거는 [QUARTZ_RENDERER_REPORT.ko.md](QUARTZ_RENDERER_REPORT.ko.md), 옵션 매핑은 [QUARTZ_OPTIONS.ko.md](QUARTZ_OPTIONS.ko.md),
`publish.css`/`publish.js`/플러그인 재작성은 [QUARTZ_MIGRATION.ko.md](QUARTZ_MIGRATION.ko.md)를 보세요.

## 1. 동작 방식

업로드나 삭제가 있으면 서버가 그 볼트의 발행 파일을 [Quartz 5](https://github.com/jackyzha0/quartz)로 정적 빌드하고, 결과를 같은 URL
(`/publish/{user}/{vault}/…` 또는 커스텀 도메인)로 서빙합니다.

- 빌드는 볼트별로 한 번에 하나, 마지막 변경 후 3초 기다려 합칩니다(디바운스). 서로 다른 볼트는 동시에 최대 2개까지 빌드합니다.
- 빌드는 임시 작업 디렉터리에서 하고, **성공했을 때만** 라이브 사이트와 교체합니다. 실패하면 이전 사이트가 그대로 남습니다.
- 사이트 옵션(`/api/options`) 대부분은 빌드가 아니라 요청 시점에 적용되어 리빌드가 필요 없습니다.
- 사이트 비밀번호(`/api/password`로 등록한 값)가 있으면 방문자에게 비밀번호를 묻습니다(7일 쿠키). **이 검사는 Quartz 렌더러에만 있고,
  기존 렌더러는 지금도 비밀번호를 검사하지 않습니다.**

## 2. 설정

환경변수(Docker 이미지에서는 `PUMICE_QUARTZ_DIR`이 이미 설정되어 있음).

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PUMICE_QUARTZ_DIR` | (이미지: `/opt/quartz`) | node_modules가 설치된 Quartz 5 체크아웃. 없으면 렌더러를 켤 수 없음(409) |
| `PUMICE_PUBLIC_HOST` | `localhost` | 서버의 공개 호스트 이름(스킴 없이). 커스텀 도메인이 없는 사이트의 canonical URL·sitemap·RSS에 쓰임. **운영에서는 반드시 설정** |
| `PUBLISH_PASSWORD_SECRET` | 없음 | 비밀번호 쿠키 서명 키. **사이트 비밀번호가 있는 Quartz 볼트에 필수.** 없으면 그 사이트는 열리지 않고 503 |
| `PUMICE_QUARTZ_MAX_CONCURRENT_BUILDS` | 2 | 동시에 빌드하는 볼트 수 |
| `PUMICE_QUARTZ_DEBOUNCE_SECONDS` | 3 | 마지막 변경 후 빌드까지 대기 |
| `PUMICE_QUARTZ_BUILD_TIMEOUT` | 600 | 빌드를 강제 종료하기까지의 초 |

Docker 이미지는 Node 22와 Quartz를 **커밋 SHA로 고정**해 빌드 시점에 설치합니다(`Dockerfile`의 `QUARTZ_REF`). 런타임에는 네트워크가 필요 없습니다.
Quartz를 올릴 때는 `QUARTZ_REF`를 바꾸고 `PUMICE_QUARTZ_DIR=<체크아웃> uv run pytest tests/test_quartz_renderer.py`를 먼저 통과시키세요.
Node와 Quartz(`node_modules`만 약 260MB)가 더해져 이미지 크기가 약 1.0GB입니다.

## 3. 렌더러 켜기, 끄기, 상태 보기

볼트 소유자만 가능합니다(`/api/options`와 같은 `vault-access` 권한. 사이트 협업자는 할 수 없음).
볼트는 다른 vault-scoped API와 같은 방식(`vault_id` 파라미터, `obs-id` 헤더, JSON 본문)으로 지정합니다.

```bash
# 상태: {"renderer": "default"|"quartz", "quartz_available": bool, "site_ready": bool, "build": {...}}
curl -H "Authorization: Bearer $TOKEN" "https://HOST/api/renderer?vault_id=MyVault"

# 켜기: 즉시 첫 빌드를 시작. 같은 요청을 다시 보내면 강제 리빌드
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"vault_id":"MyVault","renderer":"quartz"}' https://HOST/api/renderer

# 끄기(롤백): 즉시 기존 렌더러로 복귀. 빌드 결과는 남겨 두어 다시 켜기 쉬움
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"vault_id":"MyVault","renderer":"default"}' https://HOST/api/renderer
```

- **전환 직후 무중단:** 첫 Quartz 빌드가 끝날 때까지 기존 렌더러가 계속 서빙합니다. 단, 사이트 비밀번호가 있는 볼트는 보호 없이 열리지 않도록
  비밀번호 화면을 보여 주고, 인증 후에도 빌드 전이면 503을 돌려줍니다.
- **빌드 상태(`build`)**: `state`는 `pending`(대기), `building`, `ok`(`seconds` 포함), `failed`(`error`에 끝 1,500자) 중 하나이고
  시각(`started_at`, `finished_at`, 밀리초)이 붙습니다. 빌드가 실패해도 이전 사이트는 계속 서빙됩니다.
- 웹 대시보드에는 아직 전환 버튼이 없습니다(API만).

## 4. 재시작과 장애

- 상태는 `publish_meta/{owner}/{vault}/quartz_build.json`에 저장됩니다. 서버가 시작할 때 **빌드된 사이트가 없거나 `pending`/`building`에서
  멈춘 Quartz 볼트를 자동으로 다시 빌드**합니다(`recover_quartz_sites`).
- Quartz를 쓸 수 없는 상태(`PUMICE_QUARTZ_DIR` 없음, `node` 없음)에서 시작하면 오류 로그를 남기고 복구하지 않습니다.
- 빌드는 서버 프로세스 안의 스레드에서 외부 `node` 프로세스를 실행합니다. 서버를 **여러 프로세스로 띄우면** 각 프로세스가 같은 볼트를
  따로 빌드할 수 있습니다(단일 프로세스 가정).

## 5. 사용자에게 알려야 할 점

- permalink, alias, 기존 대소문자 URL(`폴더A/노트`)은 URL을 유지하지 않고 **정식 주소로 302 이동**합니다.
- `publish.css`의 구조 선택자(`.pumice-*`)와 `publish.js`의 `PumicePublishRenderer.*`는 동작하지 않습니다([가이드](QUARTZ_MIGRATION.ko.md)).
- 서버 플러그인은 요청 때가 아니라 **빌드 때** 한 번 실행됩니다.
- 비밀번호가 설정된 볼트를 옮기면, 그동안 열려 있던 사이트가 비밀번호로 잠깁니다.
- 수식(KaTeX)을 쓰는 사이트는 방문자 브라우저가 `cdn.jsdelivr.net`에 접속합니다.

## 6. 빌드 비용 참고
노트 1,000개 볼트가 약 25초(CPU), 출력 약 27MB입니다. 증분 빌드는 없어서 노트 하나만 바꿔도 전체를 다시 빌드합니다.
노트마다 OG 이미지를 만드는 Quartz 기능을 켜면 같은 볼트가 약 3분으로 늘어서 끄고 있습니다.
