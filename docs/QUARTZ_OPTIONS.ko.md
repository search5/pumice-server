# publish_options → Quartz 매핑

"Change site options"의 옵션 15개가 Quartz 렌더러에서 어떻게 동작하는지 정리한 문서입니다.
구현은 `src/pumice_server/quartz_renderer.py`(순수 로직)와 `web.py`의 `_serve_quartz_page`입니다.
배경은 [QUARTZ_POC.ko.md](QUARTZ_POC.ko.md), 사용자용 재작성 안내는 [QUARTZ_MIGRATION.ko.md](QUARTZ_MIGRATION.ko.md)를 보세요.

## 1. 원칙

1. **기본값은 Quartz가 아니라 기존 서버의 기본값을 따릅니다.** 옵션을 설정하지 않은 볼트도 기존과 같은 모습이어야 하기 때문입니다.
   기존 기본값은 탐색기 숨김, 테마 토글 숨김, 다크 테마, 검색창 표시이고, Quartz 기본값은 탐색기 표시, 토글 표시, OS 테마 따름입니다.
2. **가능한 옵션은 요청 시점에 적용합니다.** 옵션을 바꿔도 리빌드가 필요 없고 즉시 반영됩니다.
   빌드에 박아야 하는 옵션은 `siteName`, `strictLineBreaks`, `slidingWindowMode` 세 개뿐이며, 이 값이 **실제로 바뀔 때만** 리빌드합니다
   (`quartz_renderer.BUILD_TIME_OPTION_KEYS`).
3. 기존 `/api/options` 요청·응답 형식은 바뀌지 않습니다.

## 2. 옵션별 매핑

근거: **B**=헤드리스 Chrome 실제 렌더링, **T**=자동 테스트(실제 뷰·빌드, 문자열/헤더 수준).

| 옵션 | 기본값 | Quartz에서의 동작 | 적용 시점 | 근거 | 기존과 다른 점 |
|---|---|---|---|---|---|
| `siteName` | 빈 값(볼트 ID) | Quartz `pageTitle` | 빌드 | B, T | |
| `indexFile` | 빈 값 | 루트(`/`)가 해당 노트로 **302 이동**. 발행되지 않은 파일이면 무시 | 요청 | T | 기존은 루트에서 그 노트를 그대로 서빙. 이동 방식은 permalink와 같은 이유(상대 링크) |
| (인덱스 없음) | — | 볼트에 `index.md`가 없으면 **목록 페이지를 자동 생성**(탐색기·그래프·검색에는 숨김) | 빌드 | T | 기존의 "루트에 파일 목록" 대체. 이미지 등 비노트 파일은 목록에 없음 |
| `logo` | 빈 값 | 사이트 이름 앞에 로고 표시(CSS `::before`) | 요청 | B, T | 안전하지 않은 URL 문자가 있으면 무시 |
| `noindex` | false | `<meta name="robots" content="noindex">` + `X-Robots-Tag: noindex` 헤더(에셋·404 포함) | 요청 | T | 헤더가 추가됨 |
| `hideTitle` | false | 노트 제목(`h1.article-title`) 숨김 | 요청 | T | |
| `readableLineLength` | true | false면 페이지 최대 폭 해제(`.page{max-width:none}`) | 요청 | B(false), T | true는 Quartz 기본 폭을 그대로 씀 |
| `strictLineBreaks` | false | true면 `hard-line-breaks` 플러그인(줄바꿈이 `<br>`로) | 빌드 | T | **아래 주의 참고** |
| `googleAnalytics` | 빈 값 | gtag.js 스니펫. **커스텀 도메인에서만**(기존 규칙과 동일). ID 형식 검증 | 요청 | T | |
| `showSearch` | true | false면 검색창 숨김 | 요청 | T | 검색 인덱스 파일(`contentIndex.json`)은 계속 공개됨 |
| `showNavigation` | **false** | true면 탐색기 표시 | 요청 | B | |
| `navigationOrdering` | [] | 최상위 항목 순서 | 요청 | B, T | 아래 참고 |
| `navigationHiddenItems` | [] | 최상위 항목 숨김 | 요청 | B, T | 아래 참고 |
| `slidingWindowMode` | false | true면 `stacked-pages` 플러그인(노트를 오른쪽에 쌓아 열기) | 빌드 | T(빌드·플러그인 활성만) | **동작이 근사치**. 아래 참고 |
| `defaultTheme` | **dark** | `light`/`dark`는 방문자가 직접 고르기 전까지 고정, `system`은 Quartz의 OS 테마 따름 | 요청 | B(dark), T | |
| `showThemeToggle` | **false** | true면 테마 토글 표시 | 요청 | B | |

그 밖에 기존 렌더러가 하던 일도 같이 옮겼습니다.
- **파비콘 자동 감지**: 발행된 `favicon.ico`, `favicon-NN(xNN).png`에 `<link rel="icon">`을 붙입니다(T).
- **노트별 `description`/`image`**: 노트에 지정한 값이 Quartz가 만든 `description`, `og:*`, `twitter:*` 메타를 덮어씁니다(T).

### 탐색기 정렬·숨김
기존과 같이 **최상위 항목만** 정렬·숨김을 적용합니다(폴더 안은 그대로). 순서 계산은 기존 `_build_navigation_tree`와 같은 알고리즘이며
자동 테스트로 순서가 같음을 확인했습니다.
Quartz의 탐색기는 브라우저에서 만들어지고 페이지 이동 때마다 다시 그려지며, 정렬·필터 함수를 YAML로 줄 수 없어서
**서버가 계산한 순서를 스크립트로 내려주어 브라우저에서 적용**합니다(`MutationObserver`). 실제 브라우저에서
`폴더A`→`노트1` 순서, 지정한 항목 숨김, 나머지 이름순을 확인했습니다.
다른 점: 탐색기에는 **노트만** 나옵니다(기존은 이미지, `publish.css` 같은 발행 파일도 나열).

### 주의가 필요한 두 옵션
- **`strictLineBreaks`**: 기존 서버는 이 옵션이 **true일 때** 줄바꿈을 `<br>`로 바꿉니다(서버 렌더링 경로의 `nl2br`).
  Obsidian의 "Strict line breaks"(켜면 줄바꿈을 무시)와는 의미가 반대로 보이지만, 기존 동작을 그대로 따랐습니다.
  또한 기존 클라이언트 렌더링 경로(대부분의 볼트)에는 이 옵션을 읽는 코드가 보이지 않습니다. 즉 지금까지는 거의 효과가 없던 옵션일 수
  있어, 켜져 있던 볼트를 옮기면 줄바꿈이 새로 달라 보일 수 있습니다. **의미를 어느 쪽으로 할지 확인이 필요합니다.**
- **`slidingWindowMode`**: Quartz의 `stacked-pages`(Andy Matuschak 스타일)로 대응했습니다. 빌드와 플러그인 활성은 확인했지만
  **화면 동작은 확인하지 못했고**, 기존 sliding window와 열리는 방식·URL 규칙이 다를 수 있습니다.

## 3. 같이 바로잡은 프라이버시 문제 (옵션과 무관)
Quartz 기본 설정은 모든 사이트가 방문자 정보를 외부로 보내게 합니다. 아래를 막았습니다(자동 테스트로 회귀 방지).

- `plausible.io` 분석 스크립트 → 끔(`analytics: null`). 분석은 `googleAnalytics` 옵션으로만 동작합니다.
- Google Fonts 링크(`quartz-fonts` 플러그인, `fontOrigin`) → 끄고 시스템 글꼴로 대체
- 그래프용 d3/pixi CDN 스크립트 → 로컬 번들로 전환(`cdnCaching: false`)
- 쓰지 않는 `cdnjs.cloudflare.com` preconnect → 빌드 후 제거
- 푸터의 Quartz GitHub/Discord 홍보 링크 → 제거("Created with Quartz" 표기는 남김)

**남은 외부 요청:** 수식(KaTeX)용 CSS/스크립트가 `cdn.jsdelivr.net`에서 로드됩니다. 모든 페이지에 포함되므로 방문자 IP가 해당
CDN에 노출됩니다. 자체 호스팅하거나 수식이 없는 볼트에서 LaTeX 플러그인을 끄는 방안이 후속 과제입니다.
(테스트가 허용 목록으로 이 한 곳만 통과시킵니다.)

## 4. 비밀번호 검사 (확정)
Quartz 렌더러에서는 `passwords.json`에 비밀번호가 하나라도 있으면 **항상 검사합니다.** 옵션이 아니라 기본 동작입니다.
운영 시 서버에 `publish_password_secret` 설정 또는 환경변수 `PUBLISH_PASSWORD_SECRET`이 **반드시** 있어야 하며, 없으면 사이트가 열리지 않고
503을 돌려줍니다. 기존 볼트를 옮기기 전에 소유자에게 "그동안 열려 있던 사이트가 비밀번호로 잠긴다"고 알려야 합니다.
평문 저장, 무차별 대입 방어(레이트리밋)는 아직 없습니다.

## 5. 검증하지 못한 것
- 브라우저로 직접 본 것은 기본 다크 테마, 로고, 사이트 이름, 토글 표시, 탐색기 정렬·숨김, 넓은 본문 폭, 푸터입니다.
  `hideTitle`, `showSearch`, `light`/`system` 테마, 파비콘 표시, `og:*` 메타, GA 전송, `noindex`는 **HTML 문자열·헤더 수준**으로만 확인했습니다.
- `defaultTheme`가 `light`/`dark`일 때 OS 테마가 바뀌면 Quartz 스크립트가 그 시점에 테마를 덮어쓸 수 있습니다(방문자가 토글하지 않은 경우).
- `slidingWindowMode`의 실제 화면 동작(위).
- `readableLineLength=true`의 폭이 기존 "읽기 좋은 폭"과 같은지(Quartz 기본 폭을 그대로 사용).
- 사용자 지정 `publish.css`가 옵션용 CSS와 겹칠 때의 모든 조합. `publish.css`는 옵션 CSS 뒤에 오므로 이깁니다.
