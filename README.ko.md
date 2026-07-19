# Pumice server

[🇺🇸 English](README.md) | 🇰🇷 한국어

[Pumice](https://github.com/search5/pumice) 옵시디언 플러그인을 위한 자체 호스팅 동기화/버전 히스토리/퍼블리시 백엔드입니다.
Python 3.13+와 Twisted(`asyncioreactor`)로 만들어졌습니다: 동기화 RPC(`Delta`, `UploadFiles`,
`DownloadFiles` 등)는 리액터의 이벤트 루프에서 직접 구동되는 네이티브 gRPC-Web `Resource`
(`src/pumice_server/grpc_web_resource.py`)가 처리하고, Pyramid 앱이 퍼블리시 사이트·REST 엔드포인트·웹
로그인/관리자 대시보드를 담당합니다 -- 둘 다 같은 HTTP 포트를 공유합니다.

## 설치

이 프로젝트는 의존성 관리에 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```bash
cp .env.example .env   # 필요에 맞게 수정 (DB 타입, 포트, 데이터 디렉터리, 관리자 계정)
uv run server
```

`ADMIN_USER`와 `ADMIN_PASSWORD`는 서버가 시작되기 전에 `.env`에 반드시 설정되어 있어야 합니다 --
그 외에는 최초 계정을 만들 방법이 없습니다. 서버는 첫 시작 시 이 관리자 계정을 생성합니다.

### Docker

GHCR에 미리 빌드된 이미지를 받아서 바로 실행할 수 있습니다 (멀티 아키텍처: `linux/amd64`,
`linux/arm64`):

```bash
docker run -d --name pumice-server -p 8080:8080 \
  --env-file .env \
  -v pumice-data:/data \
  ghcr.io/search5/pumice-server:latest
```

또는 직접 소스에서 빌드할 수도 있습니다:

```bash
docker build -t pumice-server .
docker run -d --name pumice-server -p 8080:8080 \
  --env-file .env \
  -v pumice-data:/data \
  pumice-server
```

이미지 안에서 `DATA_DIR`은 기본값이 `/data`입니다 (위 `-v pumice-data:/data` 볼륨과 대응) --
재시작 후에도 남아있어야 하는 것들(DB(`DB_TYPE=sqlite`일 때), 동기화된 vault 내용, 버전 히스토리
백업, 퍼블리시된 사이트)이 전부 여기 저장됩니다. 그 외 설정은 전부 `.env`/`--env-file`에서
오며, 이미지에 미리 박아넣지 않습니다.

#### Docker Compose

`docker-compose.yml`은 `pumice-server`와 CUBRID 서비스를 같은 컴포즈 네트워크에 묶어놓은
구성입니다:

```bash
docker compose up -d --build
```

이건 기존에 떠 있는 DB를 가리키는 설정이 아니라, pumice-server 자체가 컨테이너로 뜰 때 외부
DB를 올바르게 연결하는 방법을 보여주는 템플릿입니다. 컨테이너 안에서 `127.0.0.1`은(호스트에서
직접 실행할 때는 적절한 `DB_HOST` 값이지만) 그 컨테이너 자기 자신의 loopback을 가리킵니다 --
옆에 있는 다른 컨테이너도, 호스트도 아닙니다. `docker-compose.yml`은 `DB_HOST`를 CUBRID
서비스의 이름(`cubrid`)으로 덮어써서, 컴포즈 네트워크 안의 내장 DNS로 정상적으로 해석되도록
합니다. 나머지 `DB_*`/`ADMIN_*` 값들은 여전히 `env_file:`을 통해 `.env`에서 옵니다. 여기 있는
CUBRID 서비스는 비어있는 상태로 시작합니다 -- 기존에 이미 있는 외부 DB를 가리키고 싶다면
`cubrid` 서비스를 지우고 `DB_HOST`/`DB_PORT`를 `.env`(또는 `environment:`)에 그 DB의 실제
주소로 설정하면 됩니다.

## 설정 (`.env`)

전체 목록은 `.env.example`을 참고하세요. 중요한 항목들:

- `ADMIN_USER` / `ADMIN_PASSWORD` — 필수. 첫 시작 시 최초(관리자) 계정을 만드는 데 사용됩니다.
- `DB_TYPE`:
  - `sqlite` (기본값) — 별도 설정 없이 로컬 파일 하나로 동작.
  - `mysql` / `mariadb` / `postgresql` / `cubrid` — 외부 데이터베이스 서버를 가리키도록
    `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME`을 설정.

## 계정과 인증

자체 회원가입 기능은 없습니다. 새 계정은 관리자가 대시보드나 `POST /api/admin/users/create`를 통해서만
생성할 수 있습니다. 계정이 존재하면:

- 자격증명은 정확히 **한 가지** 종류뿐입니다: `device_tokens` 행(token, username, device_name,
  created_at_ms). 로그인할 때마다 — 브라우저 대시보드든 옵시디언 플러그인이든 — 새로 하나씩
  발급됩니다. 다른 기기/브라우저에서 다시 로그인해도 이전 세션은 무효화되지 않습니다; 각각이
  독립된 행이라 개별적으로 폐기할 수 있습니다.
- **브라우저(웹 대시보드)**: `POST /user/login`이 이 토큰을 `HttpOnly`, `SameSite=Lax`인
  `session_token` 쿠키로 심어줍니다 (HTTPS로 서빙될 때는 `Secure`도 함께). 대시보드 JS는 토큰을 직접
  다루지 않습니다 — `fetch()`가 브라우저의 쿠키 자동 전송에 그냥 의존합니다. "토큰 재발급" UI는
  더 이상 없습니다; 세션을 끝내려면 로그아웃(해당 행 삭제)하거나 디바이스 관리 UI(아래 참고)에서
  폐기하면 됩니다.
- **옵시디언 플러그인**: "로그인" 버튼이 시스템 브라우저에서
  `/login?redirect=obsidian://pumice-auth&device_name=...`을 엽니다; 성공하면 사용자가 토큰을 직접
  복사/붙여넣기 하는 대신 `obsidian://` 콜백으로 페이지가 토큰을 건네줍니다. 이후 플러그인은 이
  동일한 토큰을 *모든 곳에* 사용합니다 — gRPC 동기화 메타데이터는 물론, 모든 HTTP 호출(퍼블리시,
  버전 히스토리)에도 엔드포인트에 따라 `Authorization: Bearer`, `obs-token`, 또는 JSON 본문의
  `token` 필드로 실어 보냅니다. 서버는 이 중 무엇이든, 그리고 쿠키까지도 동일하게 인식합니다
  (`web.py`의 `extract_token()`이 `get_device_token()`으로 검증).
- **디바이스 관리**: 모든 계정은 `/api/user/devices`에서 자신의 세션을 조회/폐기할 수 있고
  (대시보드의 "내 정보 관리" 탭에 노출), 관리자는 `/api/admin/users/{username}/devices`에서 *모든*
  사용자의 세션을 조회/폐기할 수 있습니다 (관리자 사용자 테이블에 사용자별로 노출).
- 모든 vault는 정확히 하나의 계정에 속하며, 계정 간 우회 접근은 불가능합니다 -- 이게 어떻게
  강제되는지는 아래 "Vault 식별자" 항목을 참고하세요; `is_admin`은 계정 관리 권한(다른 사용자
  생성/삭제/초기화)만 부여할 뿐, 그들의 vault에 대한 접근권을 주지 않습니다.

## Vault 식별자

vault의 진짜 식별자는 `(owner_username, vault_id)` 쌍이지, `vault_id` 단독이 아닙니다. `vault_id`는
옵시디언 클라이언트의 vault가 로컬에서 불리는 이름(`vault.getName()`)일 뿐이라 전역적으로 유일하지
않습니다 -- "Obsidian Vault"는 문자 그대로 옵시디언 자체의 기본 vault 이름입니다. `owner_username`은
항상 호출자 본인의 인증된 신원에서 나오지, 클라이언트가 보낸 값에서 나오지 않습니다 -- 그래서
호출자는 언제나 자기 자신의 이름으로만 vault에 접근할 수 있고, "임자 없는 vault_id를 선점"하는
단계도, 잘못될 여지가 있는 계정 간 조회도 필요 없습니다. DB 테이블(`file_metadata`, `file_history`,
`published_files`)과 물리 저장 경로(`data_dir/{vaults,history,publish_meta,tmp}/{owner_username}/{vault_id}`,
이는 기존의 `data_dir/published/{owner_username}/{vault_id}` 구조와 동일한 방식) 전부 이 방식으로
스코프됩니다. `get_history_by_id()`도 `owner_username`을 받기 때문에, 히스토리 행을 vault/계정
경계를 넘어 ID만으로 가져올 수 없습니다.

## 권한 검사(Authorization)

권한 검사는 뷰마다 임시방편으로 짜여진 `if`문이 아니라 실제 Pyramid ACL입니다. `DeviceTokenSecurityPolicy`
(`web.py`)가 `extract_token()`으로 호출자의 신원을 확인하고, 모든 뷰는 `@view_config`에
`permission='authenticated' | 'admin' | 'vault-access'`를 선언하며 Pyramid가 뷰 본문이 실행되기 전에
이를 강제합니다 (거부되면 `forbidden_view`로 바로 넘어가며, 예전에 수동으로 나누던 401 대 403 구분을
그대로 재현합니다: 신원 자체가 없으면 401, 신원은 있지만 여기 접근 권한이 없으면 403). 기본
권한값은 `'authenticated'`입니다 — 새 라우트는 뷰가 명시적으로 `permission=NO_PERMISSION_REQUIRED`로
빠지지 않는 한 기본적으로 비공개이며, 이는 예전 tween이 수동으로 관리하던 "공개 경로 목록" 방식과
정반대입니다.

Vault 범위의 라우트(퍼블리시, 버전 히스토리, 관리자의 vault별 파일 조회)는 라우트의 `factory=`로
`VaultContext`를 사용합니다: 해당 엔드포인트의 호출 관례에 따라(`matchdict`, 쿼리 파라미터, `obs-id`
헤더, 또는 JSON 본문 중 어디서든) `vault_id`를 알아내고, `owner`는 호출자 본인의 신원으로
설정합니다 -- 그래서 `permission='vault-access'`는 사실상 "로그인했는지" 정도만 확인하는 셈이고,
`vault_id`/`owner`는 뷰가 쓰기 편하도록 `request.context`에 미리 정리되어 있습니다.

## 언어

로그인·대시보드 페이지는 서버 사이드에서 번역되며, 요청마다 브라우저의 `Accept-Language` 헤더를
기준으로 언어가 결정됩니다(현재 `ko` 또는 `en`) — 클라이언트 쪽 언어 전환 로직은 없습니다. 번역
문자열은 `web.py`에 인라인으로 박혀있지 않고 `src/pumice_server/locale/{ko,en}.json`에 있습니다.

## 하는 일

- `/` — 세션 쿠키가 있는지 여부에 따라 `/dashboard` 또는 `/login`으로 리다이렉트.
- `Delta` / `UploadFiles` / `DownloadFiles` (gRPC-Web) — 핵심 파일 동기화 프로토콜.
- `GetFileHistory` / `DownloadHistoryVersion` / `RestoreHistoryVersion` (gRPC-Web) — 파일별 버전
  히스토리, 변경이 있을 때마다 물리적 백업(가능하면 하드링크)으로 뒷받침됩니다.
- `UploadFilesStream` (`/obsidian.sync.v1.SyncService/UploadFilesStream`, 옵트인) — `fetch()`
  요청 바디 스트리밍을 지원하는 브라우저용 진짜 client-streaming 업로드. 위 gRPC-Web 디스패치와는
  완전히 별개로 처리됩니다(아래 "스트리밍 업로드" 참고).
- `/api/*` (HTTP, Pyramid) — 퍼블리시(업로드/목록/삭제/다운로드), 퍼블리시 공유(이메일로 초대, 초대
  코드로 수락), 버전 히스토리 REST 미러, 사용자 계정, 디바이스 관리, 관리자 대시보드.
- `/publish/{username}/{vault}/...` — 실제로 퍼블리시된 사이트, 마크다운을 즉석에서 렌더링
  (위키링크 해석, YAML 프런트매터 제거).

## 스트리밍 업로드 (`UploadFilesStream`)

`UploadFiles`(위)는 바디 바이트를 단 하나도 처리하기 전에 요청 바디 전체를 메모리에 버퍼링합니다
(`grpc_web_resource.py`의 `request.content.read()`) — 지금 튜닝된 배치 크기에서는 문제없지만,
훨씬 큰 단일 요청이 이 방식에 의존하게 둘 이유는 없습니다. `UploadFilesStream`은 같은 결과(파일이
디스크에 기록되고, `UploadFiles`와 완전히 동일하게 해시 검증·백업·기록됨)를 내는 두 번째, 옵트인
경로로, 바디 바이트가 도착하는 대로 점진적으로 파싱합니다 — `src/pumice_server/streaming.py`
(`EnvelopeStreamParser`)와 `src/pumice_server/streaming_upload_resource.py`
(`StreamingUploadRequest`/`StreamingUploadResource`)로 구현됨. 생성된 gRPC-Web 스텁이 아니라
클라이언트의 손으로 짠 `fetch()` + envelope 프레이밍으로 호출됩니다 — 브라우저의 grpc-web/connect-es
라이브러리는 애초에 client-streaming을 지원하지 않기 때문입니다.

독립 PoC(`/home/jiho/twisted-streaming-poc`, 테스트 28개)로 먼저 TDD로 만들고 실제 소켓까지
검증한 뒤, 운영 관심사 3가지를 얹어 이식했습니다(`tests/test_streaming_upload_request.py`,
`tests/test_upload_accumulator.py`, 테스트 17개 추가):

- **스트리밍 시작 전 인증** — `Authorization` 헤더를 몸체 바이트가 파서에 단 1바이트도 전달되기
  *전에* 디바이스/소유자 신원으로 해석합니다(Twisted가 `gotLength()`를 호출하는 시점엔 이미 헤더
  전체가 파싱 완료된 상태 — 바디 바이트보다 먼저). 토큰이 없거나 잘못됐으면 맨 `401`만 응답하고
  (공격자가 조작했을 수도 있는) 바디를 처리하기 전에 커넥션을 끊습니다.
- **백프레셔** — 블로킹 작업(토큰 조회, 각 프레임의 디스크 I/O)이 진행되는 동안은 트랜스포트를
  일시정지시켜(실제 TCP 트랜스포트에서는 `stopReading()`) 소켓에서 더 이상 읽지 않으므로, 빠르거나
  악의적인 송신자가 실제로 디스크에 기록된 양보다 한없이 앞서서 큐를 쌓을 수 없습니다.
- **블로킹 I/O 회피** — 토큰 조회와 모든 파일 I/O는 `twisted.internet.threads.deferToThread`를
  거칩니다 — `service.py`가 `UploadFiles`의 동일한 작업에 이미 쓰고 있는 `asyncio.to_thread`
  관례와 맞춘 것입니다. 실측으로 확인함: 느리게 트리클되는 업로드 하나가 진행되는 동안 별도
  커넥션으로 `Ping`을 259번 연속 호출해도 매번 5ms 미만으로 응답 — 업로드의 디스크 I/O 때문에
  리액터가 멈추는 일이 없음을 확인했습니다.

실제로 구동 중인 서버에 직접 붙여봐서만 발견된 버그도 하나 있습니다(`StringTransport` 기반
테스트만으로는 안 보였음): 본문 전체가 아주 작아서 스레드 기반 인증 확인이 끝나기도 전에 다
도착해버리는 요청은, `owner_username`이 아직 정해지지 않은 채로 `render()`까지 먼저 도달해버리는
경합이 있었습니다 — `pauseProducing()`은 *앞으로의* 소켓 읽기만 막을 뿐, 현재 `dataReceived()`
호출 안에서 이미 전달된 바이트에는 영향이 없기 때문입니다. `requestReceived()` 자체를 인증이 실제로
끝날 때까지 미루도록 고쳐서 해결했고(`StreamingUploadRequest._onAuthResolved`/`_onAuthFailed`),
회귀 테스트(`test_small_full_body_arriving_before_auth_resolves_does_not_race_render`)로 고정해
뒀습니다. 같은 실측에서 발견된 두 번째 버그: 파일이 끝날 때마다 요청 도중에 ack를 바로 써버렸는데,
이건 응답을 깨뜨립니다 — 이 앱 서버는 TLS/HTTP2를 직접 종단하지 않는 순수 HTTP/1.1 커넥션이라서,
`Request.write()`를 Twisted API 상 아무리 일찍 호출할 수 있다 해도 요청 전체가 도착하기 전에는
응답을 시작할 수 없기 때문입니다. 이제 ack는 버퍼링해뒀다가 `render_POST()`에서 한 번에 몰아서
쓰도록 고쳤고, 이는 `UploadFiles`의 기존 "본문 전체 수신 후 ack" 동작과 정확히 일치합니다.

### 타임아웃 & 관측성

위 내용에 이어 추가한 운영 관심사 2가지(이 기능 전체 테스트 총 48개):

- **인증 조회 타임아웃**(`AUTH_TIMEOUT_SECONDS = 10`) — DB 스레드 조회가 멈추면(풀 고갈, 데드락)
  요청과 그 일시정지된 트랜스포트가 더 이상 무한정 대기하지 않습니다. `503`으로 응답하며, `401`("자격
  증명이 틀림")과는 구분됩니다("서버가 너무 느렸음"). 단, `deferToThread`는 실제로 아직 블로킹 중인
  워커 스레드를 인터럽트할 수 없으므로 이 Deferred가 더 이상 기다리지 않게 만들 뿐입니다.
- **최대 업로드 시간 상한**(`MAX_UPLOAD_SECONDS = 600`) — Twisted 자체의 idle 타임아웃(아래 참고)은
  바이트가 하나라도 도착할 때마다 리셋되므로, 절대 완전히 멈추지는 않지만 끝나지도 않는 slow-loris류
  전송을 잡아내지 못합니다. 이건 활동 여부와 무관한 별도의 상한으로, `gotLength()`에서 예약하고 본문이
  전부 도착하거나 다른 이유로 요청이 중단되면 취소합니다.
- **전역 idle 타임아웃** — 알고 보니 이미 존재하고 있었습니다(`Site`/`HTTPFactory.__init__`이 기본값
  `timeout=60`을 `buildProtocol()`을 통해 모든 `HTTPChannel`에 적용) — "타임아웃이 아예 없다"는 최초
  가정을 검증 없이 믿지 않고 Twisted 소스를 직접 읽어 확인한 것입니다(처음엔 실제 인스턴스 값이 아니라
  `HTTPChannel.timeOut`의 *클래스* 기본값을 확인하는 실수를 했습니다). 이제 `main.py`에 명시적으로
  적어뒀습니다(`Site(root_resource, timeout=60)`) — 나중에 Twisted 기본값이 바뀌어도 암묵적으로
  영향받지 않도록 문서화된 결정으로 남기기 위함입니다.
- **메트릭**(`StreamingUploadMetrics`, 새 의존성 없음 — 기존 메트릭 스택 자체가 없음) — 활성/누적
  업로드 수, 수신 바이트 수, 파일 성공/실패 카운트(실패는 사유별로: 잘못된 vault/경로, 임시파일 오류,
  경로 불일치, 해시 불일치), 거부 카운트(인증 실패/타임아웃, 프레임 초과, 프레임 손상, 최대 시간 초과),
  백프레셔 일시정지 빈도/누적 시간. `GET /api/admin/streaming-stats`(`permission='admin'`, 기존 admin
  API 관례와 동일)와 5분 주기 로그 요약(`main.py`의 `log_streaming_upload_stats`, 기존 임시파일 GC
  태스크와 같은 LoopingCall 방식)으로 노출됩니다.

메트릭을 연결하다가 실제 데드락 버그를 하나 더 발견했습니다: `_UploadAccumulator._enqueue()`가
inflight/`resumeProducing()` 정리 로직엔 `addCallback`을, 로깅엔 별도의 `addErrback`을 썼는데 — 이는
블로킹 핸들러에서 *예상치 못한* 예외(예: 디스크 꽉 찬 상태에서의 `OSError`)가 나면 정리 로직 자체가
통째로 건너뛰어져서 트랜스포트가 영원히 일시정지 상태로 남는다는 뜻이었습니다. 둘을 하나의 `addBoth`로
합쳐서 고쳤고, 고치기 전엔 실패(행)하는 회귀 테스트(`test_unhandled_exception_in_blocking_handler_still_resumes_transport`)로 고정해뒀습니다.

가정하지 않고 실측으로 확인한 것 하나 더: `Request.notifyFinish()`(`active_uploads` 추적에 사용)는
`StringTransport` 기반 테스트에서 `loseConnection()`만 호출해서는 발동하지 않습니다 — 직접 실험으로
확인한 뒤, 구현이 아니라 *테스트* 쪽에서 `channel.connectionLost()`를 명시적으로 시뮬레이션하도록
고쳤습니다 — 실제 리액터/소켓에서 이게 실제로 어떻게 완료되는지에 맞춘 것입니다.

## 후원

이 프로젝트를 후원하고 싶으시면 search5@gmail.com으로 연락해주세요. 후원해주시면 개발에 더 많은
시간을 쏟는 데 실질적인 도움이 됩니다.
