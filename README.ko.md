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
- `/mcp` — Streamable HTTP 기반 MCP(Model Context Protocol) 서버로, AI 클라이언트에게 동기화된
  vault에 대한 읽기/쓰기 접근을 제공합니다 (아래 "MCP" 참고).

## MCP (Model Context Protocol)

`/mcp`는 동기화된 vault를 MCP 호환 AI 클라이언트(Claude Code, Claude Desktop 등)에 Streamable
HTTP(프로토콜 버전 `2025-06-18`)로 노출합니다. 공식 `mcp` Python SDK를 사용합니다
(`src/pumice_server/mcp_resource.py`). 인증은 다른 모든 곳과 완전히 동일한 디바이스 토큰을 그대로
재사용합니다 — MCP 전용 별도 자격증명은 없습니다.

### 클라이언트 연결하기

```json
{
  "mcpServers": {
    "pumice": {
      "type": "http",
      "url": "https://your-server/mcp",
      "headers": { "Authorization": "Bearer <device-token>" }
    }
  }
}
```

여기엔 어떤 디바이스 토큰이든 사용할 수 있습니다 — 옵시디언 플러그인이나 브라우저 세션이 이미
쓰고 있는 것과 같은 토큰이면 됩니다("계정과 인증" 참고). 클라이언트는 그 토큰이 속한 계정의
vault만 볼 수 있습니다. 서버의 다른 모든 곳과 동일합니다.

### 도구 목록

**읽기 전용:**

- `list_vaults` — 이 계정이 동기화한 모든 vault 목록.
- `list_files` — vault 내 현재(삭제되지 않은) 파일 목록, 경로/크기/해시/수정 시각 포함.
- `get_recent_notes` — 최근 수정된 파일을 최신순으로.
- `get_file_history` — 파일 하나의 버전 히스토리.
- `read_file_version` — `history_id`로 특정 과거 버전의 내용을 읽기.
- `read_note` — 경로로 노트의 현재 내용을 바로 읽기 (`get_file_history` + `read_file_version`을
  한 번에 대체).
- `search_notes` — vault의 현재 파일 전체를 대상으로 하는 전문(full-text) 검색.
- `search_by_frontmatter` — YAML frontmatter의 특정 키가 특정 값을 포함하는 노트 찾기.
- `list_published_files` — vault에서 현재 퍼블리시(공개)된 파일 목록.

**쓰기:**

- `write_note` — 노트를 새로 만들거나 덮어쓰기.
- `delete_note` — 소프트 삭제(완전 삭제가 아니라 히스토리로 복구 가능).
- `rename_note` — 노트 이동/이름변경, 내용과 버전 히스토리 보존; 이미 존재하는 활성 노트를
  덮어쓰려 하면 거부.

전체 인자 스키마는 표준 MCP `tools/list` 호출로 제공됩니다 — 실제 코드와 어긋나는 걸 막기 위해
여기서는 더 자세히 중복 기술하지 않습니다.

### 종단간 암호화(E2EE)

이 서버는 E2EE 복호화 키를 영구적으로 보관하지 않습니다 — 애초에 특정 vault가 E2EE를 켜놨는지
자체를 알지 못합니다(클라이언트 로컬 설정이라 전송된 적이 없음). 파일 내용을 다루는 모든 도구는
대신 호출마다 선택적인 `e2ee_password` 인자를 받습니다:

- **읽기**(`read_file_version`, `read_note`, `search_notes`, `search_by_frontmatter`)는 비밀번호가
  주어지면 그때그때 복호화하고, 응답을 만들고 나면 그 키를 즉시 버립니다. 비밀번호가 없는데
  내용이 UTF-8 텍스트로 디코딩되지 않으면 `e2ee_password`를 설정해서 다시 시도하라는 명확한
  에러를 돌려줍니다(`search_notes`/`search_by_frontmatter`는 대신 텍스트로 읽을 수 없는 파일을
  조용히 건너뜁니다 — vault 전체를 스캔할 때는 일부 읽을 수 없는 파일이 있는 게 정상이기
  때문입니다).
- **`write_note`**는 읽기처럼 잘못 짐작해도 되는 여유가 없습니다 — 잘못 짐작하면 즉시 다른
  기기로 동기화되어 나가서 해당 파일이 깨집니다. `e2ee_password`가 주어지면 항상 그대로 신뢰해서
  사용합니다 — 서버가 실제 클라이언트와 동일한 방식으로 콘텐츠를 암호화합니다(동일한 키 유도와
  결정론적 IV, 실제 클라이언트 출력과 바이트 단위로 대조 검증됨). 주어지지 않으면 먼저 vault의
  기존 파일 몇 개를 샘플링해보고, vault가 암호화된 것처럼 보이면 평문을 조용히 저장하는 대신 쓰기
  자체를 거부합니다.
- **`delete_note`/`rename_note`**는 비밀번호가 전혀 필요 없습니다 — 삭제는 바이트를 제거할
  뿐이고, 이름변경은 바이트를 옮길 뿐이라 둘 다 파일 내용을 건드리지 않습니다.
- 메타데이터만 다루는 도구들(`list_vaults`, `list_files`, `get_recent_notes`, `get_file_history`,
  `list_published_files`)은 애초에 파일 내용을 건드리지 않으므로 이 문제와 무관합니다.

### 알려진 제한사항

서버발 알림(Streamable HTTP의 GET/SSE 절반)은 아직 구현되어 있지 않습니다 — `GET /mcp`는 `501`을
반환합니다. 현재로선 서버가 연결된 MCP 클라이언트에게 능동적으로 보내야 할 게 없습니다.

## 스트리밍 업로드 (`UploadFilesStream`)

`UploadFiles`(위)는 바디 바이트를 단 하나도 처리하기 전에 요청 바디 전체를 메모리에 버퍼링합니다
(`grpc_web_resource.py`의 `request.content.read()`) — 원래 의도한 배치 크기에서는 문제없지만,
훨씬 큰 단일 요청이 이 방식에 의존할 이유는 없습니다. `UploadFilesStream`은 바디 바이트가 도착하는
대로 점진적으로 파싱하는 옵트인 대안으로(`src/pumice_server/streaming.py`의
`EnvelopeStreamParser`, `src/pumice_server/streaming_upload_resource.py`의
`StreamingUploadRequest`/`StreamingUploadResource`), 파일이 디스크에 기록되고 `UploadFiles`와
완전히 동일하게 해시 검증·백업·기록되며, 바디 전체를 메모리에 버퍼링하지 않습니다. 생성된 gRPC-Web
스텁이 아니라 클라이언트가 직접 짠 `fetch()` + envelope 프레이밍으로 호출됩니다 — 브라우저의
grpc-web/connect-es 라이브러리는 애초에 client-streaming을 지원하지 않기 때문입니다.

보장하는 것들:

- **스트리밍 시작 전 인증** — `Authorization` 헤더를 바디 바이트가 파서에 단 1바이트도 전달되기
  전에 디바이스/소유자 신원으로 해석합니다. 토큰이 없거나 잘못됐으면 맨 `401`만 응답하고 (공격자가
  조작했을 수도 있는) 바디를 처리하기 전에 커넥션을 끊습니다.
- **백프레셔** — 블로킹 작업(토큰 조회, 각 프레임의 디스크 I/O)이 진행되는 동안은 트랜스포트를
  일시정지시켜, 빠르거나 악의적인 송신자가 실제로 디스크에 기록된 양보다 한없이 앞서서 큐를 쌓을 수
  없습니다.
- **블로킹 I/O 회피** — 토큰 조회와 모든 파일 I/O는 `twisted.internet.threads.deferToThread`를
  거칩니다 — `service.py`가 `UploadFiles`의 동일한 작업에 이미 쓰고 있는 `asyncio.to_thread`
  관례와 맞춘 것으로, 업로드 I/O 때문에 리액터가 멈추는 일이 없습니다.

### 타임아웃 & 관측성

- **인증 조회 타임아웃**(`AUTH_TIMEOUT_SECONDS = 10`) — DB 스레드 조회가 멈추면(풀 고갈, 데드락)
  요청과 그 일시정지된 트랜스포트가 무한정 대기하지 않고 `503`으로 응답합니다. `401`("자격증명이
  틀림")과는 구분됩니다("서버가 너무 느렸음").
- **최대 업로드 시간 상한**(`MAX_UPLOAD_SECONDS = 600`) — 아래 idle 타임아웃과 별개로, 활동 여부와
  무관한 상한입니다 — 바이트를 완전히 멈추지는 않을 만큼만 드문드문 보내는 slow-loris류 전송도
  커넥션을 무한정 붙잡을 수 없습니다.
- **전역 idle 타임아웃**(60초) — `main.py`에서 `Site(root_resource, timeout=60)`으로 명시적으로
  설정되며, Twisted의 `HTTPFactory`/`HTTPChannel`을 통해 모든 커넥션에 적용됩니다.
- **메트릭**(`StreamingUploadMetrics`) — 활성/누적 업로드 수, 수신 바이트 수, 파일 성공/실패
  카운트(실패는 사유별로: 잘못된 vault/경로, 임시파일 오류, 경로 불일치, 해시 불일치), 거부
  카운트(인증 실패/타임아웃, 프레임 초과, 프레임 손상, 최대 시간 초과), 백프레셔 일시정지 빈도/누적
  시간. `GET /api/admin/streaming-stats`(`permission='admin'`)와 주기적 로그 요약(`main.py`의
  `log_streaming_upload_stats`)으로 노출됩니다.

## 서버 플러그인

퍼블리시 페이지가 렌더링할 수 있는 것을 확장하기 위한 서버 사이드 플러그인의 발견/활성화
생명주기입니다 — 완전히 서버 로컬입니다: vault에서 동기화되는 내용이 전혀 아닙니다. 현재는
인프라만 갖춰진 상태입니다 — 퍼블리시 렌더러 연동이 아직 없어서, 활성화된 플러그인이라도 실제로
퍼블리시된 페이지에 아무 영향을 주지 않습니다. 이 부분을 실제 플러그인 로직과 동시에 설계하지
않아도 되도록, 먼저 따로 갖춰둔 것입니다.

런타임 설치 단계는 의도적으로 없습니다. 플러그인 코드는 웹 서버 자신과 완전히 같은 파이썬
프로세스 안에서 돌기 때문에, 플러그인을 추가하는 것은 다른 서버 의존성을 추가하는 것과 똑같은
무게를 가집니다 — 관리자 대시보드로 업로드하는 게 아니라 `pyproject.toml`에 선언되고 Docker
이미지 빌드 시점에 포함되어, 이 서버가 의존하는 다른 모든 것과 동일한 검토/빌드/재배포 경로를
거칩니다. 이는 곧 플러그인 자신의 의존성도 (직접 번들링할 필요 없이) 빌드 시점에 `uv`로 제대로
해석된다는 뜻이기도 합니다.

### 플러그인 만들기

플러그인은 `pumice_server.plugins` 그룹에 entry point를 선언한 평범한 파이썬 패키지입니다:

```toml
# pyproject.toml
[project]
name = "harang-immich"
version = "0.1.0"
description = "퍼블리시된 페이지에서 harang-immich 임베드를 해석합니다."

[project.entry-points."pumice_server.plugins"]
harang-immich = "harang_immich_plugin:resolve"
```

### 서버에 추가하기

pumice-server의 평범한 의존성으로 추가하고(`uv add harang-immich-plugin`, 또는
`[tool.uv.sources]`를 통한 경로/git 소스) 이미지를 다시 빌드하면 됩니다. `discover_plugins()`
(`src/pumice_server/plugin_registry.py`)는 pytest나 Flask가 자기 플러그인을 찾는 것과 같은
방식 — 표준 라이브러리 자체의 플러그인 발견 메커니즘인
`importlib.metadata.entry_points(group="pumice_server.plugins")` — 로 찾아냅니다.
pumice-server만의 특별한 게 없고, 위의 entry point 선언 외에 따로 설정할 것도 없습니다.

### 활성화하기

활성화는 관리자가 대신 해주는 것이 아니라 셀프 서비스입니다 — 로그인한 각 계정이 **마이페이지**에서
설치된 플러그인을 스스로 켜고 끕니다. 범위는 vault 단위가 아니라 소유자 계정 단위로만 정해집니다 —
플러그인을 켜면 그 계정이 소유한 모든 vault에 적용됩니다. vault별 토글이 없는 이유는, 플러그인이
실제로 필요로 하는 vault가 어떤 것인지는 이미 퍼블리시 요청 자체의
경로(`/publish/{username}/{vault_id}/...`)에서 알 수 있기 때문입니다.

## 후원

이 프로젝트를 후원하고 싶으시면 search5@gmail.com으로 연락해주세요. 후원해주시면 개발에 더 많은
시간을 쏟는 데 실질적인 도움이 됩니다.
