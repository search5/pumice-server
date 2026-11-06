# Quartz 렌더러 마이그레이션 가이드 (publish.css · publish.js · 서버 플러그인)

기존 pumice 렌더러에서 Quartz 렌더러로 옮기는 볼트 소유자와 플러그인 작성자를 위한 문서입니다.
배경과 검증 범위는 [QUARTZ_POC.ko.md](QUARTZ_POC.ko.md)를 보세요. 이 문서의 매핑표와 예제는
**Quartz 5.0.0 실제 빌드 산출물**에서 확인한 값입니다(확인 방법은 각 절 끝에 표기).

| 대상 | 자동 처리 | 직접 고쳐야 하는 것 |
|---|---|---|
| `publish.css` | 테마 변수 7개, `[data-theme]` 선택자 | `.pumice-*` 구조 선택자, 매핑 없는 변수 |
| `publish.js` | `window.pumicePreload`, `#pumice-note-root` | `PumicePublishRenderer.*` 호출, 페이지 이동 처리 |
| 서버 플러그인 | **없음(고칠 것 없음)**. 계약 유지 | — |

---

## 1. publish.css

### 1.1 자동으로 변환되는 것
빌드할 때 `publish.css`의 아래 변수 선언 옆에 Quartz 쪽 변수 선언을 **자동으로 덧붙입니다.**
원본 규칙은 그대로 남습니다.

| pumice 변수 | Quartz 변수 | 쓰임 |
|---|---|---|
| `--bg-color` | `--light` | 배경 |
| `--border-color` | `--lightgray` | 테두리, 구분선 |
| `--text-muted` | `--gray` | 보조 텍스트 |
| `--text-primary` | `--darkgray` | 본문 텍스트 |
| `--heading-color` | `--dark` | 제목 |
| `--accent-blue` | `--secondary` | 링크, 활성 항목, 그래프의 현재 노트 |
| `--wiki-link` | `--tertiary` | 호버/보조 강조 |

`:root[data-theme="dark"]`/`[data-theme="light"]` 선택자는 Quartz가 쓰는 `:root[saved-theme="dark"]`/`[saved-theme="light"]`로 바뀝니다.

**변환되지 않는 것(Quartz에 대응이 없어 효과가 없음):** `--bg-gradient-start/end`, `--container-bg`,
`--wiki-link-hover`, `--link-hover-color`, `--code-bg`, `--code-text-color`, `--accent-purple`.
Quartz 변수를 직접 쓰세요(1.3).

주의: `@media (prefers-color-scheme: light)` 안의 변수는 Quartz의 테마 전환(`saved-theme` 속성)과
연동되지 않습니다. 라이트/다크를 따로 지정하려면 `:root[saved-theme="light"]`/`[saved-theme="dark"]`를 쓰세요.

### 1.2 구조 선택자 매핑 (직접 고쳐야 함)

| pumice | Quartz | 비고 |
|---|---|---|
| `.site-title` | `.page-title` | 왼쪽 상단 사이트 이름 (`h2.page-title`) |
| `.pumice-nav-sidebar` | `.left.sidebar` | 폭은 `flex-basis`가 아니라 페이지 그리드로 조절(아래) |
| `.pumice-right-column` | `.right.sidebar` | |
| `.pumice-main-column` | `.center` | |
| `.container.markdown-body` | `article` | 노트 본문. 제목은 `h1.article-title` |
| `.markdown-body h1, h2, h3` | `article h1, article h2, article h3` | |
| `.wiki-link` | `a.internal` | 내부 링크 (별칭 링크는 `a.internal.alias`) |
| `.pumice-graph-mini` | `.graph > .graph-outer` | 내용 영역은 `.graph-container` |
| `.pumice-graph-fullpage` | `.global-graph-outer` / `.global-graph-container` | |
| `.pumice-search`, `#pumice-search-input` | `.search`, `input.search-bar` | 열린 검색창은 `.search-container` |
| `#pumice-outline` | `.toc` | 목록은 `.toc-content` |
| `#pumice-backlinks` | `.backlinks` | |
| `.pumice-icon-button` (테마 토글) | `.darkmode` | 리더 모드 토글은 `.readermode` |
| 폴더 트리 `.nav-folder` | `.explorer`, `.explorer-ul` | |
| `.vault-badge` | (없음) | 사이트 이름은 `.page-title`에 표시됨 |
| `.pumice-sliding-track`, `.pumice-sliding-pane` | (없음) | sliding window 모드는 Quartz 렌더러에 없음 |

사이드바 폭 조절 예:

```css
@media (min-width: 801px) {
  #quartz-body { grid-template-columns: 360px auto 320px; } /* 왼쪽 | 본문 | 오른쪽 */
}
```

### 1.3 Quartz 기본 변수 (직접 쓸 수 있음)
`--light`, `--lightgray`, `--gray`, `--darkgray`, `--dark`, `--secondary`, `--tertiary`, `--highlight`,
`--textHighlight`, 글꼴 `--titleFont`, `--headerFont`, `--bodyFont`, `--codeFont`. 라이트/다크 값은
각각 `:root`, `:root[saved-theme=dark]`에 정의됩니다. Obsidian 호환 이름(`--background-primary`,
`--text-normal`, `--text-muted`, `--text-accent` 등)도 위 변수를 참조하도록 함께 정의돼 있습니다
(존재 확인만 했고, Obsidian용 CSS가 실제로 얼마나 맞는지는 검증하지 않았습니다).

### 1.4 우선순위
Quartz의 기본 규칙은 `@layer quartz-base` 안에 있고, `publish.css`는 `<head>`의 **마지막** 스타일시트이며
레이어 밖입니다. 그래서 같은 대상을 고치는 일반 규칙은 `!important` 없이 이깁니다.

### 1.5 전/후 예제
`docs/examples/publish.css.example`(기존)을 옮긴 것이 [`docs/examples/publish.quartz.css.example`](examples/publish.quartz.css.example)입니다.

- 변수 4개 블록 → Quartz 변수 3개로 정리(자동 변환에 맡겨도 같은 효과)
- `.site-title`/`.markdown-body hN` → `.page-title`/`article hN`
- `.pumice-nav-sidebar { flex-basis: 260px }` → `#quartz-body { grid-template-columns: 360px auto 320px }`
- `.pumice-graph-mini` → `.graph > .graph-outer`
- `.vault-badge` 규칙은 대응이 없어 삭제

확인: 이 예제를 `publish.css`로 넣고 빌드해 헤드리스 Chrome으로 렌더링했습니다. 세리프 제목, 넓어진 왼쪽 열,
네모난 두꺼운 그래프 박스, 주황 강조색이 적용됐습니다. `.darkmode`의 원형 처리는 아이콘 버튼에 테두리가 없어
화면으로는 구별되지 않았습니다.

---

## 2. publish.js

활성 조건은 기존과 같습니다. 볼트 루트의 `publish.js`이고, **커스텀 도메인이 설정된 사이트에서만** 로드됩니다.

### 2.1 가장 큰 차이: 페이지 이동 때 스크립트가 다시 실행되지 않습니다
Quartz는 SPA입니다. 내부 링크를 누르면 본문만 교체되고 `publish.js`는 다시 실행되지 않으며, 본문(`<article>`)에
붙였던 요소는 이동하면 사라집니다.

- **한 번만 하면 되는 일**(고정 배지, 분석 코드)은 그대로 둡니다.
- **페이지마다 해야 하는 일**은 함수로 만들어 최초 1회와 `nav` 이벤트에서 모두 호출합니다.

```js
function onEveryPage() { /* article에 요소 추가 등 (중복 추가 방지 필수) */ }
onEveryPage();
document.addEventListener("nav", onEveryPage);
```

Quartz가 `document`에 발생시키는 이벤트: `nav`(이동 완료, `detail.url`), `prenav`(이동 직전), `themechange`(테마 전환).
현재 페이지는 `document.body.dataset.slug`, 사이트 기준 경로는 `document.body.dataset.basepath`입니다.

### 2.2 pumice 전역 API 대응표

| pumice | Quartz 렌더러에서 | 대체 방법 |
|---|---|---|
| `window.pumicePreload` | **제공함** | `{ url_prefix, path }`. publish.js 앞에서 자동 주입 |
| `#pumice-note-root` | **제공함 (호환 shim)** | `<article>`에 같은 id를 붙여 줍니다. 이동 후에도 다시 붙임. 새 코드는 `document.querySelector("article")` 권장 |
| `PumicePublishRenderer.renderMarkdown` | 없음 | 마크다운은 **빌드 때** 렌더링됩니다. 클라이언트 렌더링 불가 |
| `buildGraph`, `buildLocalGraph` | 없음 | `static/contentIndex.json`의 `links`로 직접 계산. 그래프 자체는 Quartz 내장 |
| `searchNotes`, `mergeSearchResults` | 없음 | `contentIndex.json`의 `title`/`content`로 직접 검색, 또는 Quartz 내장 검색 사용 |
| `buildOutline` | 없음 | DOM의 `.toc` |
| `buildBacklinks` | 없음 | DOM의 `.backlinks`, 또는 `contentIndex.json`에서 역계산(예제 참고) |
| `resolveWikilinkHref` | 없음 | 슬러그 규칙 `${basepath}/${slug}`, 링크 요소의 `data-slug` |
| `cssColorToPixiHex` | 없음 | 해당 없음(그래프가 내장) |

`contentIndex.json` 형식: `{ [slug]: { slug, filePath, title, links[], tags[], content } }`
(`filePath`는 볼트 원본 경로, `slug`는 Quartz 슬러그).
위치는 `${basepath}/static/contentIndex.json`입니다. 주의: `content`에 노트 본문 텍스트가 들어 있어
**사이트 방문자가 누구나 내려받을 수 있습니다**(발행된 노트만 포함).

커스텀 도메인 루트에 배포되면 `data-basepath`는 빈 문자열일 것으로 예상됩니다(하위 경로 배포에서만 확인함).
코드에서는 `document.body.dataset.basepath || ""`처럼 쓰세요.

### 2.3 전/후 예제
`docs/examples/publish.js.example`(기존)을 옮긴 것이 [`docs/examples/publish.quartz.js.example`](examples/publish.quartz.js.example)입니다.

- 고정 배지: 그대로
- "노트가 준비될 때까지 기다리는" `whenNoteReady`(폴링): 삭제. 스크립트가 `defer`라 DOM이 이미 있음
- 본문 문단 추가: `onEveryPage()`로 바꾸고 `nav`에 연결, 중복 추가 방지 클래스 사용
- 백링크 계산: `contentIndex.json`을 fetch해 `links` 역계산(`PumicePublishRenderer.buildBacklinks` 대체)

확인: 이 예제를 정적 서버에 올려 헤드리스 Chrome에서 실행했습니다. 스크립트가 로드되고 `contentIndex.json`을
읽었으며, 배지와 본문 문단이 추가됐습니다. `nav` 이벤트 재실행(최초 1회, 이동 후 1회, 중복 이벤트에도 1개)은
브라우저가 아니라 jsdom에서 이벤트를 직접 발생시켜 확인했습니다.

---

## 3. 서버 플러그인 (작성자 안내)

**기존 플러그인은 고칠 필요가 없습니다.** 계약은 그대로입니다.

```python
def resolve(content: str, *, owner_username: str, vault_id: str, config_entries: dict) -> str: ...
```

- 입력은 기존과 같이 **프런트매터가 제거되고 위키링크가 해석된 마크다운**입니다
  (`[[노트]]` → `<a href="노트" class="wiki-link">노트</a>`, `![[그림.png]]` → `<img ...>`).
  링크의 `href`는 접두사 없는 상대 경로입니다(기존은 `/publish/{user}/{vault}/…`). 링크 주소 형태에 의존하는
  플러그인이 있다면 영향받을 수 있습니다.
- 출력의 HTML은 그대로 통과합니다(예: Immich 플러그인의 `data:` URI `<img>`).
- 예외를 낸 플러그인은 건너뛰고 다음 플러그인이 적용되는 동작, 설정(`config_entries`) 복호화, 디스크 캐시는 기존 러너를 그대로 씁니다.
- **빌드 시점에 한 번 실행됩니다.** 요청 때마다가 아닙니다. 플러그인 활성화·비활성화·설정 저장·삭제와 업로드·삭제가
  있으면 해당 볼트가 다시 빌드됩니다.
- **플러그인이 내용을 바꾼 노트만** 해석된 형태로 Quartz에 전달됩니다. 바꾸지 않은 노트는 Quartz 고유의
  위키링크/임베드 처리를 그대로 씁니다. 바꾼 노트에서도 링크는 그래프와 백링크에 유지됩니다.

확인: 실제 `pumice-plugin-immich`(엔트리 포인트 로딩, 암호화된 볼트별 설정, 가짜 Immich 서버에 대한 HTTP 요청)를
빌드에 연결해 확인했습니다. `![immich:profile/asset|alt]`가 `data:` URI 이미지로 치환되고, 설정이 없는 프로필은
원문이 유지되며, 플러그인이 바꾼 노트의 링크 엣지와 바꾸지 않은 노트의 위키링크가 모두 유지되고, `data:` URI가
검색 인덱스를 부풀리지 않았습니다.

**확인하지 않은 것:** 플러그인이 바꾼 노트의 링크에 Quartz의 `.internal` 스타일이 붙는지, 재빌드 때 디스크
캐시로 외부 호출이 실제로 생략되는지, 대용량 이미지를 가진 노트가 많은 볼트의 빌드 시간과 페이지 크기.
