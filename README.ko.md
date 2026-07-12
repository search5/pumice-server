# Pumice server

[🇺🇸 English](README.md) | 🇰🇷 한국어

[Pumice](https://github.com/search5/pumice) 옵시디언 플러그인을 위한 자체 호스팅 동기화/버전 히스토리/퍼블리시 백엔드입니다.
Python 3.13+와 Twisted(`asyncioreactor`)로 만들어졌습니다: 동기화 RPC(`Delta`, `UploadFiles`,
`DownloadFiles` 등)는 리액터의 이벤트 루프에서 직접 구동되는 네이티브 gRPC-Web `Resource`
(`src/server/grpc_web_resource.py`)가 처리하고, Pyramid 앱이 퍼블리시 사이트·REST 엔드포인트·웹
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
문자열은 `web.py`에 인라인으로 박혀있지 않고 `src/server/locale/{ko,en}.json`에 있습니다.

## 하는 일

- `/` — 세션 쿠키가 있는지 여부에 따라 `/dashboard` 또는 `/login`으로 리다이렉트.
- `Delta` / `UploadFiles` / `DownloadFiles` (gRPC-Web) — 핵심 파일 동기화 프로토콜.
- `GetFileHistory` / `DownloadHistoryVersion` / `RestoreHistoryVersion` (gRPC-Web) — 파일별 버전
  히스토리, 변경이 있을 때마다 물리적 백업(가능하면 하드링크)으로 뒷받침됩니다.
- `/api/*` (HTTP, Pyramid) — 퍼블리시(업로드/목록/삭제/다운로드), 퍼블리시 공유(이메일로 초대, 초대
  코드로 수락), 버전 히스토리 REST 미러, 사용자 계정, 디바이스 관리, 관리자 대시보드.
- `/publish/{username}/{vault}/...` — 실제로 퍼블리시된 사이트, 마크다운을 즉석에서 렌더링
  (위키링크 해석, YAML 프런트매터 제거).
