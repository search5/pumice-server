# Quartz 렌더러 PoC 검토 문서

작성일 2026-11-06 · 브랜치 `poc/quartz-renderer` (워크트리 `../pumice-server-quartz-poc`, 미커밋)

## 1. 요약

**결론: Quartz 5로 교체 가능합니다. 다만 "그대로 갈아끼우기"는 안 되고, 몇 가지는 동작이 달라지며 몇 가지는 추가 구현이 필요합니다.**

- 사용자가 요청한 6개 운영 기능(+협업자)은 모두 Quartz 경로에서 **동작하는 형태로 이식**했고 자동 테스트로 확인했습니다. 단, 일부는 **기존과 동작이 다릅니다**(§4).
- 이 PoC는 기존 렌더러를 건드리지 않습니다. 볼트별 옵트인(`publish_meta/{owner}/{vault}/renderer.json`)이며 옵트인하지 않은 볼트는 기존 코드 경로 그대로입니다.
- 기존 테스트 710개 + 신규 48개 = **758개 전부 통과**(회귀 없음).
- **`publish_options` 15개 전부 매핑**했습니다(동작·근거·차이: [QUARTZ_OPTIONS.ko.md](QUARTZ_OPTIONS.ko.md)). 기본값은 기존 서버를 따르고, 대부분 요청 시점에 적용되어 리빌드가 필요 없습니다.
- **비밀번호 검사는 켜기로 확정**했습니다(§4 ②). 운영에는 `PUBLISH_PASSWORD_SECRET`이 필수입니다.
- Quartz 기본 설정의 **방문자 추적·외부 글꼴/CDN 요청을 차단**했습니다(남은 외부 요청은 수식용 KaTeX 한 곳, QUARTZ_OPTIONS §3).
- **서버 플러그인은 계약을 그대로 유지**합니다(§4 ⑤). 실제 `pumice-plugin-immich`로 검증했습니다.
- 사용자용 재작성 안내는 [QUARTZ_MIGRATION.ko.md](QUARTZ_MIGRATION.ko.md)에 따로 있습니다(`publish.css`, `publish.js`, 플러그인).
- 확정된 결정: **permalink는 302 이동 방식을 수용**(§4 ①).
- **가장 큰 위험은 두 가지**입니다. (a) 폴더 슬러그 소문자화로 인한 기존 URL 불일치(리다이렉트로 완화), (b) Quartz가 기본으로 켜 두는 방문자 추적·외부 글꼴/CDN 요청(차단 완료, 남은 외부 요청은 수식용 KaTeX 한 곳).

## 2. 방법과 환경

| 항목 | 값 |
|---|---|
| Quartz | 5.0.0 (`97a2d05`), 설정은 YAML + `@quartz-community/*` 플러그인 |
| Node | v26.0.0 (Quartz 요구: 22 이상) |
| 검증 대상 | 실제 `web.py` 뷰 함수 + 실제 SQLite 리포지토리 + 실제 Quartz 빌드 |
| 추가 검증 | 실제 Pyramid 앱 기동 후 curl, 헤드리스 Chrome 스크린샷/DOM, jsdom |

기존 렌더러를 목업으로 흉내 내지 않고, `_render_publish_page`에 분기를 하나 추가해 **기존 라우팅(커스텀 도메인 술어, 301 리다이렉트, permalink/alias DB 조회, 플러그인 러너)을 그대로 통과**시켰습니다.

## 3. 구조

```
업로드/삭제 뷰 ──notify──▶ BuildScheduler(볼트별 디바운스·직렬화)
                              │
                              ▼
                  build_site(): 워크스페이스에 발행 파일 스테이징
                   ├ .md: 프런트매터 분리 → 서버 플러그인 transform → 재결합
                   ├ quartz.config.yaml 생성 (baseUrl, ko-KR, og-image/encrypted-pages 끔)
                   ├ node quartz build
                   ├ publish.css → Quartz 변수 매핑본으로 교체
                   └ 성공 시에만 out 디렉터리를 rename 2회로 교체 (실패 시 기존 사이트 유지)
                              │
요청 ─▶ _render_publish_page ─▶ (기존) 커스텀 도메인 301 ─▶ renderer==quartz ?
                                                         └▶ _serve_quartz_page:
                                                            비밀번호 게이트 → URL 해석 → publish.css/js 주입 → 응답
```

- 신규 파일: `src/pumice_server/quartz_renderer.py`(순수 로직), `tests/test_quartz_renderer.py`.
- 변경 파일: `web.py`(훅 4곳: import, 업로드/삭제 후 리빌드 알림, 렌더 분기, 서빙 함수).
- 링크가 **상대경로**라서 하위 경로(`/publish/{user}/{vault}/`)와 커스텀 도메인에서 같은 빌드 산출물을 씁니다.

## 4. 기능별 검증 결과

근거 수준: **T**=자동 테스트(실제 뷰·빌드), **E**=실제 서버 curl, **B**=헤드리스 Chrome, **J**=jsdom.

| 기능 | 결과 | 근거 | 기존과 다른 점 |
|---|---|---|---|
| 하위 경로 서빙 | 통과 | T, E, B | 없음. 한국어 UI·그래프·목차·백링크·탐색기·검색창 렌더 확인 |
| permalink | 통과 | T, E | 기존은 URL 유지하며 서빙, **PoC는 302로 정식 슬러그에 이동** (아래 ①) |
| alias | 통과 | T, E | 기존은 클라이언트 `replaceState`, PoC는 302. 의미상 동일 |
| slug(기존 URL) | 통과(완화) | T, E | **Quartz는 폴더 슬러그를 소문자화**(`폴더A`→`폴더a`). 기존 `폴더A/노트2(.md)` URL은 302로 보존 |
| 사이트 비밀번호 | 통과 | T, E | **기존에는 조회 시 검사 자체가 없었음** (아래 ②) |
| 커스텀 도메인 | 통과 | T | 라우팅·`x-obsidian-custom-domain` 헤더·301은 기존 코드 그대로 상속 |
| 커스텀 도메인 리다이렉트 | 통과 | T | 기존 301 로직이 분기보다 앞에 있어 그대로 동작 |
| 협업자 권한 | 통과 | T | 권한 검사는 변경 없음. 협업자 업로드도 **소유자 볼트**를 리빌드(`("alice","vault1")`) |
| publish.css | 부분 통과 | T, B | 테마 변수 7개만 매핑. 구조 클래스는 비호환 (아래 ③) |
| publish.js | 부분 통과 | T, J | 커스텀 도메인 게이트·주입은 동일. 내부 API 비호환 (아래 ④) |
| 서버 플러그인 | 통과 | T | 계약 유지. 실제 Immich 플러그인으로 확인 (아래 ⑤) |
| 업로드→리빌드→반영/삭제 | 통과 | T | 실제 업로드/삭제 뷰 → 스케줄러 → 빌드 → 페이지 생성/404 |

### 기존과 달라지는 점 (결정 필요)

① **permalink/alias는 서빙이 아니라 리다이렉트입니다 (수용 확정).** Quartz 페이지의 링크는 자기 슬러그 깊이 기준 상대경로(`../노트1`)입니다. 다른 깊이의 URL(permalink)로 같은 페이지를 그대로 내려주면 모든 링크가 틀어집니다. 그래서 정식 슬러그로 302 합니다. 주소창에 permalink가 남지 않습니다. 대안이던 `<base href>` 주입은 페이지 내 `#앵커` 링크가 깨질 수 있어 채택하지 않았습니다(미검증).

② **비밀번호 보호는 기존에 "저장만 되고 검사는 되지 않는" 기능입니다.** 지금 서버에는 비밀번호를 등록하는 API(`/api/password`)가 있고 값은 `passwords.json`에 평문으로 저장되지만, 방문자가 페이지를 볼 때 그 값을 확인하는 코드가 어디에도 없습니다. 즉 **비밀번호를 설정해 둔 볼트도 지금은 누구나 볼 수 있습니다.** Quartz 경로에서는 처음으로 실제 검사를 하는 게이트를 넣었습니다(HMAC 서명 쿠키 7일, HttpOnly, SameSite=Lax, https면 Secure. 비밀번호가 바뀌면 기존 쿠키가 무효화되고, 서버 시크릿이 없으면 열리지 않고 503으로 닫힙니다).
   - **결정(확정): 켠다.** 기존에 비밀번호를 등록해 둔 볼트를 Quartz로 옮기면 이 검사가 적용됩니다.
   - **켠다(권장):** 소유자가 등록한 의도대로 실제로 보호됩니다. 다만 그동안 자유롭게 보던 방문자는 갑자기 비밀번호 입력 화면을 보게 되므로 사전 공지가 필요합니다.
   - **끈다:** 지금 동작과 같지만 등록된 비밀번호가 계속 아무 의미가 없습니다.
   - 어느 쪽이든 `/api/password`의 요청/응답 형식은 바뀌지 않습니다.

③ **publish.css**: `--bg-color`, `--border-color`, `--text-muted`, `--text-primary`, `--heading-color`, `--accent-blue`, `--wiki-link`와 `[data-theme]` 선택자는 Quartz 변수/`[saved-theme]`로 **자동 매핑**됩니다. `.pumice-*` 구조 클래스, `--container-bg`, `--code-bg` 등은 Quartz DOM에 없어 **효과가 없으므로 직접 다시 써야 합니다.** 선택자 대응표와 전/후 예제는 [QUARTZ_MIGRATION.ko.md](QUARTZ_MIGRATION.ko.md) §1, 예제는 `docs/examples/publish.quartz.css.example`이며 브라우저 렌더링으로 확인했습니다.

④ **publish.js**: `window.pumicePreload`(주입)와 `#pumice-note-root`(Quartz `<article>`에 별칭을 붙이는 shim)는 자동 제공됩니다. **`window.PumicePublishRenderer`(renderMarkdown, buildGraph, searchNotes 등)는 제공할 수 없고**, Quartz는 SPA라 페이지 이동 때 스크립트가 다시 실행되지 않으므로 `nav` 이벤트 처리가 필요합니다. 대응표와 예제(`docs/examples/publish.quartz.js.example`, 실제 브라우저에서 실행 확인)는 [QUARTZ_MIGRATION.ko.md](QUARTZ_MIGRATION.ko.md) §2.

⑤ **서버 플러그인 (계약 유지):** 기존 러너(`run_enabled_plugins`)를 그대로 쓰며, 플러그인에는 **기존과 같은 입력(프런트매터 제거, 위키링크 해석 완료)** 을 넘깁니다. 링크 주소는 접두사 없는 상대 경로로 해석해 Quartz가 계속 링크를 인식합니다(그래프·백링크 엣지 유지). 플러그인이 내용을 바꾼 노트만 그 결과를 쓰고, 나머지 노트는 Quartz 고유의 위키링크 처리를 유지합니다. 예외를 낸 플러그인은 건너뛰는 동작, 설정 복호화, 디스크 캐시도 그대로입니다. **실제 `pumice-plugin-immich`** 를 가짜 Immich 서버와 함께 빌드에 연결해 임베드 치환, 미설정 프로필의 원문 유지, 링크 보존, 검색 인덱스 비대화 없음을 확인했습니다. 플러그인 활성화/비활성화/설정 저장·삭제도 이제 리빌드를 트리거합니다. 다른 점: **플러그인은 요청 때마다가 아니라 빌드 때 한 번 실행됩니다**(외부 서비스가 내려가 있던 시점의 결과가 다음 빌드까지 남을 수 있음).

## 5. 성능

| 조건 | 시간 |
|---|---|
| 노트 4개 | 약 8초 (대부분 기동·플러그인 로딩) |
| 노트 1,000개, 기본 설정 | **약 3분** (병목: `og-image`) |
| 노트 1,000개, `og-image` 끔 | **약 24초**, 출력 27MB |
| 증분 빌드 | 없음. 1개만 바꿔도 전체 재빌드 |

`og-image`와 `encrypted-pages`는 PoC에서 끕니다. 빌드는 볼트별 한 번에 하나만 돌고, 빌드 중 들어온 변경은 정확히 1회 추가 빌드로 합쳐집니다. 실패한 빌드는 기존 사이트를 건드리지 않고 임시 워크스페이스도 정리됩니다.

## 6. 검증하지 못한 것 / 알려진 공백

**기존 기능 중 이번에 다루지 않은 것**
- **사이트 옵션**: 15개 모두 매핑 완료([QUARTZ_OPTIONS.ko.md](QUARTZ_OPTIONS.ko.md)). 남은 것은 그 문서 §5(브라우저로 보지 못한 옵션, `slidingWindowMode` 화면 동작, `strictLineBreaks` 의미 확인)입니다.
- `/serve?url=` 프록시 라우트, `/api/site`, `/_search`·`/_cache.json`·`/_raw` 예약 경로(Quartz 경로에서는 의미 없음, 외부 클라이언트가 의존하는지 확인 필요).

**구현하지 않은 것**
- **리빌드 트리거**: 업로드/삭제와 플러그인 enable/disable/설정 저장·삭제는 연결했습니다. **커스텀 도메인 변경은 아직 아닙니다**(baseUrl은 사이트맵·canonical에만 쓰이고, `publish.js` 로드는 요청 시점에 판단하므로 영향이 작음).
- **플러그인 관련 미확인**: 플러그인이 바꾼 노트의 링크에 Quartz `.internal` 스타일이 붙는지, 재빌드 때 디스크 캐시로 외부 호출이 실제로 생략되는지, 큰 이미지를 가진 노트가 많은 볼트의 빌드 시간·페이지 크기(Immich 플러그인은 이미지를 `data:` URI로 HTML에 인라인함).
- 렌더러를 quartz로 **전환하는 순간의 첫 빌드**, 서버 재시작 시 대기 중이던 빌드(메모리 상 타이머라 유실), 다중 프로세스 환경(스케줄러가 프로세스 내부).
- Docker 이미지에 Node/Quartz 설치, 플러그인 버전 고정(빌드 시 `install-plugins`가 git에서 받음).
- 비밀번호 무차별 대입 방어(레이트리밋), 비밀번호 평문 저장 개선.

**검증 방법의 한계**
- Chrome 확장 연결 실패로 **헤드리스 Chrome(스크린샷/DOM)** 으로 대체했습니다. 그래서 **검색 입력·그래프 클릭/드래그·SPA 내비게이션·모바일 레이아웃·다크모드 토글의 상호작용은 확인하지 못했습니다**(정적 렌더 결과와 요소 존재만 확인).
- `publish.js`는 https 커스텀 도메인 URL을 로컬에서 로드할 수 없어 **jsdom에서 스크립트와 shim을 직접 실행**했습니다. 실제 브라우저의 로드 순서(`defer`)는 확인하지 않았습니다.
- 대형 볼트의 동시 다중 빌드, 메모리 사용량, 동일 서버 다수 테넌트 부하는 측정하지 않았습니다.

**그 밖의 관찰**
- 페이지 푸터에 "Created with Quartz" 브랜딩이 나옵니다(라이선스 확인 및 레이아웃 설정 필요).
- 기본 설정이 Google Fonts CDN을 사용합니다(외부 요청, CSP/프라이버시 영향).
- 원시 HTML(`<script>` 등)은 Quartz도 그대로 통과시킵니다. 기존 렌더러에도 sanitize가 없어 동일 수준이지만, 같은 오리진에서 실행됩니다.
- 깨진 위키링크(`[[없는노트]]`)는 일반 링크처럼 보입니다.

## 7. 재현 방법

```bash
# Quartz 체크아웃 + 의존성 (한 번)
git clone --depth 1 https://github.com/jackyzha0/quartz.git /path/quartz && (cd /path/quartz && npm ci)

# PoC 테스트 (Quartz 없으면 Quartz 의존 테스트 34개는 스킵)
PUMICE_QUARTZ_DIR=/path/quartz uv run pytest tests/test_quartz_renderer.py

# 전체 회귀
PUMICE_QUARTZ_DIR=/path/quartz uv run pytest
```

설정 키: `quartz_dir`(또는 env `PUMICE_QUARTZ_DIR`), `publish_password_secret`(또는 env `PUBLISH_PASSWORD_SECRET`), `public_host`, `quartz_debounce_seconds`.

## 8. 다음 단계 제안

1. 남은 결정: `strictLineBreaks`의 의미(QUARTZ_OPTIONS §2 주의), 수식용 KaTeX CDN을 어떻게 할지. permalink(①)와 비밀번호(②)는 확정, `publish.css`/`.js` 안내(③④)는 문서화 완료.
2. (완료) `publish_options` 매핑. 이어서 `slidingWindowMode` 화면 동작과 브라우저 미확인 옵션 확인.
3. 리빌드 트리거 보강(플러그인/도메인 변경, 렌더러 전환 시 첫 빌드, 재시작 복구).
4. Docker 이미지 반영(Node 22+, Quartz/플러그인 버전 고정) 및 빌드 큐 부하 측정.
5. 실제 브라우저에서 검색·그래프·SPA·모바일 상호작용 확인.
6. 볼트 단위로 단계 전환(기존 렌더러 병행) 후 문제없으면 기본값 전환.
