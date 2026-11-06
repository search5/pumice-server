# Quartz 퍼블리시 렌더러 도입 보고서

작성일 2026-11-06 · 브랜치 `feature/quartz-renderer` (main에 합치지 않음)

## 1. 결론

**기존 렌더러를 볼트별로 선택할 수 있는 Quartz 5 렌더러를 pumice-server에 추가했습니다.** 켜지 않은 볼트는 기존 코드 경로 그대로입니다.
요청하신 운영 기능(사이트 비밀번호, 커스텀 도메인과 리다이렉트, permalink·alias·slug, 협업자 권한, `publish.css`/`publish.js`, 서버 플러그인)과
`publish_options` 15개는 모두 Quartz 경로에서 동작하도록 옮겼습니다. 다만 **일부는 기존과 동작이 다르고(§4), 몇 가지는 확인하지 못했습니다(§6).**
기존 테스트 710개를 포함해 **전체 778개가 통과**합니다.

## 2. 무엇을 했나

| 영역 | 내용 |
|---|---|
| 렌더러 | 업로드·삭제 시 발행 파일을 Quartz로 정적 빌드. 임시 디렉터리에서 빌드하고 성공했을 때만 라이브 사이트와 교체(실패하면 이전 사이트 유지) |
| 운영 기능 | 사이트 비밀번호 게이트, 커스텀 도메인·301, permalink/alias/기존 URL 해석, 협업자 업로드 시 소유자 볼트 리빌드, `publish.css` 변수 매핑, `publish.js` 주입, 서버 플러그인(기존 계약 유지) |
| 사이트 옵션 | 15개 전부 매핑. 기본값은 기존 서버 기준, 대부분 요청 시점 적용(리빌드 불필요). 상세는 [QUARTZ_OPTIONS.ko.md](QUARTZ_OPTIONS.ko.md) |
| 전환·운영 | `GET/POST /api/renderer`(전환, 롤백, 상태), 빌드 상태 기록, 시작 시 미완료 빌드 복구, 동시 빌드 수 제한, 첫 빌드 전 기존 렌더러 유지 |
| 배포 | `Dockerfile`에 Node 22와 Quartz(커밋 SHA 고정)를 이미지 빌드 시점에 설치, 환경변수 문서화 |
| 프라이버시 | Quartz 기본 설정의 방문자 추적(plausible.io), Google Fonts, CDN 요청 차단 |

운영 방법은 [QUARTZ_RENDERER.ko.md](QUARTZ_RENDERER.ko.md), 사용자 재작성 가이드는 [QUARTZ_MIGRATION.ko.md](QUARTZ_MIGRATION.ko.md),
개념 증명의 검증 기록은 [QUARTZ_POC.ko.md](QUARTZ_POC.ko.md)입니다.

## 3. 검증 결과

근거 수준: **T**=자동 테스트(실제 `web.py` 뷰, 실제 DB, 실제 Quartz 빌드), **E**=실제 서버 curl, **B**=헤드리스 Chrome, **J**=jsdom.

| 기능 | 결과 | 근거 |
|---|---|---|
| 하위 경로·커스텀 도메인 서빙 | 통과 | T, E, B |
| permalink / alias / 기존 대소문자 URL | 통과(302 이동) | T, E |
| 사이트 비밀번호 | 통과(Quartz 렌더러에 한해) | T, E |
| 커스텀 도메인 301, 리버스 프록시 헤더 | 통과(기존 코드 상속) | T |
| 협업자 권한 | 통과(권한 검사 불변, 리빌드는 소유자 볼트) | T |
| `publish.css` | 부분 통과(변수 7개 자동 매핑, 구조 선택자는 재작성) | T, B |
| `publish.js` | 부분 통과(`pumicePreload`, `#pumice-note-root` 제공) | T, B, J |
| 서버 플러그인 | 통과. 실제 `pumice-plugin-immich`로 확인 | T |
| 사이트 옵션 15개 | 통과(일부는 문자열 수준 확인) | T, B |
| 렌더러 전환·롤백·복구·상태 | 통과 | T |
| Docker 이미지 | 통과. 이미지 빌드 성공, 컨테이너 안에서 비루트 사용자(uid 1000)로 Quartz 빌드 4.5초. 이미지 크기 약 1.0GB | 실제 빌드·실행 |

## 4. 기존과 달라지는 점

1. **permalink·alias·기존 대소문자 URL은 정식 주소로 302 이동합니다.** Quartz 페이지는 자기 슬러그 깊이 기준의 상대 링크를 쓰기 때문입니다(수용 확정).
2. **비밀번호 검사는 Quartz 렌더러에만 있습니다(확정).** 기존 서버는 `passwords.json`을 저장만 하고 조회 시 검사하지 않으며, 기존 렌더러는 그대로입니다.
   그래서 같은 볼트라도 렌더러에 따라 공개 여부가 달라집니다. 비밀번호가 설정된 볼트를 Quartz로 옮기면 그동안 열려 있던 사이트가 잠깁니다.
3. `publish.css`의 `.pumice-*` 구조 선택자와 `publish.js`의 `PumicePublishRenderer.*`는 동작하지 않습니다. 대응표와 예제를 문서화했습니다.
4. 서버 플러그인은 요청 때가 아니라 **빌드 때** 한 번 실행됩니다.
5. 탐색기에는 노트만 나옵니다(기존은 이미지 등 발행 파일도 나열). `indexFile`은 루트에서 302 이동입니다.
6. Quartz는 폴더·에셋 경로를 소문자로 바꿉니다. 기존 URL은 계속 동작하지만 정식 주소는 소문자입니다.

## 5. 결정 현황

| 결정 | 상태 |
|---|---|
| permalink를 302로 처리 | 확정 |
| 비밀번호 검사는 Quartz 렌더러에만 적용(현재 유지) | 확정 |
| `strictLineBreaks`의 의미 | **미정.** 기존 코드는 true일 때 줄바꿈을 `<br>`로 바꿔 Obsidian과 반대로 보이며, 기존 클라이언트 경로에서는 거의 효과가 없었던 옵션일 수 있음. 지금은 기존 동작을 그대로 따름 |
| 수식(KaTeX)용 `cdn.jsdelivr.net` | **미정.** 방문자 IP가 CDN에 노출됨. 자체 호스팅하거나 수식 없는 볼트에서 LaTeX 플러그인을 끄는 방안 |
| 비밀번호 검사를 기존 렌더러에도 적용 | 하지 않음(현재 유지). 롤백하면 다시 공개되는 점은 알려진 위험 |

## 6. 확인하지 못한 것과 알려진 한계

**브라우저로 확인하지 못한 것**
- Chrome 확장이 연결되지 않아 헤드리스 Chrome으로 대체했습니다. 검색 입력, 그래프 조작, SPA 이동, 모바일 레이아웃의 상호작용은 확인하지 못했습니다.
- `hideTitle`, `showSearch`, `light`·`system` 테마, 파비콘 표시, `og:*` 메타, GA 전송, `noindex`는 HTML 문자열·헤더 수준으로만 확인했습니다.
- `slidingWindowMode`(Quartz `stacked-pages`)는 빌드와 플러그인 활성만 확인했고 화면 동작은 보지 못했습니다. 기존 방식과 다를 수 있습니다.
- `publish.js`는 https 커스텀 도메인 URL을 로컬에서 로드할 수 없어 jsdom과 정적 서버로 확인했습니다.

**운영 면에서 확인하지 못한 것**
- 대형 볼트 여러 개의 동시 빌드 부하(CPU·메모리 실측). 동시 빌드 수 제한만 테스트했습니다.
- 서버를 여러 프로세스로 띄운 경우(스케줄러가 프로세스 내부에 있음).
- `/serve?url=` 프록시 라우트와 `_cache.json`·`_raw`·`_search` 예약 경로를 외부 클라이언트가 쓰는지.
- 플러그인이 바꾼 노트의 링크 스타일, 재빌드 때 플러그인 디스크 캐시로 외부 호출이 생략되는지, 큰 이미지가 많은 볼트의 빌드 시간.
- 웹 대시보드에는 렌더러 전환 버튼이 없습니다(API만).

**알려진 한계**
- 비밀번호는 평문 저장이고 무차별 대입 방어(레이트리밋)가 없습니다.
- 증분 빌드가 없어 노트 하나만 바꿔도 전체를 다시 빌드합니다(1,000개 약 25초).
- `defaultTheme`가 `light`/`dark`일 때 방문자가 토글하지 않은 상태에서 OS 테마가 바뀌면 Quartz 스크립트가 테마를 덮어쓸 수 있습니다.
- Quartz 빌드는 외부 `node` 프로세스로 실행됩니다(셸 없이 인자 리스트로 호출). 이 프로세스는 서버의 환경변수를 물려받지 않고 `PATH`, `LANG`, `TZ` 등 최소한만 받으며, `HOME`과 `TMPDIR`은 임시 작업 디렉터리입니다. 비밀값(`ADMIN_PASSWORD`, `DB_PASSWORD` 등)은 전달되지 않습니다. 다만 Quartz와 npm 플러그인이 서버와 같은 OS 사용자·같은 파일시스템에서 실행되므로 `/data`(DB 파일 포함)를 읽을 수는 있습니다(컨테이너 분리나 별도 빌드 워커가 필요하면 후속 과제).
- 원시 HTML(`<script>` 등)이 같은 오리진에서 실행됩니다. 기존 렌더러에도 sanitize가 없어 동일 수준입니다.
- Quartz 5는 신생 버전이라 설정·플러그인 표면이 바뀔 수 있습니다. 그래서 커밋 SHA로 고정했습니다.

## 7. 운영 전 체크리스트
1. 서버에 `PUMICE_PUBLIC_HOST`를 설정합니다.
2. 비밀번호가 설정된 볼트를 옮길 예정이면 `PUBLISH_PASSWORD_SECRET`을 설정합니다(없으면 그 사이트는 503).
3. 먼저 비밀번호가 없는 볼트 하나로 전환해 보고(`POST /api/renderer`), 상태가 `ok`가 된 뒤 사이트를 확인합니다. 문제가 있으면 `default`로 즉시 되돌립니다.
4. 비밀번호가 설정된 볼트의 소유자에게 동작이 바뀌는 점(§4-2)을 알립니다.
5. §5의 미정 결정 두 가지를 정합니다.

## 8. 코드 위치와 테스트
- `src/pumice_server/quartz_renderer.py`(순수 로직), `src/pumice_server/web.py`(훅, 서빙, `/api/renderer`), `Dockerfile`, `.env.example`
- `tests/test_quartz_renderer.py` 68개. Quartz 체크아웃과 `node`가 없으면 그 의존 테스트는 건너뜁니다.

```bash
PUMICE_QUARTZ_DIR=/path/to/quartz uv run --frozen pytest          # 전체 (778개)
uv run --frozen pytest tests/test_quartz_renderer.py              # Quartz 없이 (의존 테스트는 스킵)
```
