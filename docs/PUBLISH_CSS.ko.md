# `publish.css`로 퍼블리시 사이트 커스터마이징

볼트 **루트**에 `publish.css` 파일이 있으면 모든 퍼블리시 페이지가 자동으로 그 파일을
불러옵니다(하위 폴더에 같은 이름 파일이 있어도 인정 안 함 — `web.py`의
`_find_root_asset_href` 참고). 실제 옵시디언 퍼블리시의 "Customize your site" 기능과 동일한
방식입니다 — 별도 업로드 화면이나 설정 토글 없이, `publish.css`를 볼트 루트에 두고 다른
파일처럼 그냥 퍼블리시하면 됩니다.

## 로드 방식

```html
<style> ... pumice 자체 내장 스타일 ... </style>
{% if publish_css_href %}
<link rel="stylesheet" href="{{ publish_css_href }}">
{% endif %}
</head>
```

`publish.css`는 `<head>`에서 **가장 마지막**에 로드되는 스타일시트입니다(`publish_page.jinja2`
참고). 일반적인 CSS 캐스케이드 규칙에 따라 같은 명시도(specificity)라면 `publish.css`의
규칙이 pumice 내장 규칙을 이깁니다 — 단순 오버라이드엔 `!important`가 필요 없고, 이미 ID
셀렉터처럼 명시도가 높은 규칙과 싸울 때만 필요합니다.

**실제 옵시디언 퍼블리시 사이트에서 뽑아온 `publish.css`는 여기선 대부분 안 먹습니다.**
실제 옵시디언의 클래스명(`.site-body-left-column` 등)과 CSS 변수명(`--text-normal`,
`--interactive-accent` 등)이 pumice의 DOM엔 없기 때문 — pumice는 완전히 새로 만든 자체
구현이라 이름 체계 자체가 다릅니다(아래 클래스/변수 표 참고). 가져온 실제 옵시디언
스타일시트 중에선 `body`, `a`, `h1` 같은 범용 태그 셀렉터만 적용되고, 컴포넌트별 스타일은
전부 pumice 자체 셀렉터를 새로 겨냥해야 합니다.

**작성한 CSS가 JS 동작을 건드리진 않습니다.** 검색 결과 드롭다운의 동적 크기 계산, 그래프
드래그 패닝, 스크롤스파이, 테마 토글 전부 클래스가 아니라 `id`(`#pumice-search-input`,
`#pumice-graph-collapse` 등)로 엘리먼트를 찾습니다 — 아래 클래스들을 아무리 재스타일링해도
이 배선은 안 깨집니다. 깨지는 유일한 경우는 플러그인 등으로 그 `id` 자체를
바꾸거나/지우는 것인데, 이건 CSS만으로는 할 수 없는 범위 밖의 일입니다.

## 테마 변수 (`:root`, `[data-theme]`)

`publish.css`에서 아래 변수를 재정의하면 그걸 참조하는 모든 곳(그래프 노드/엣지 색깔도
포함 — 렌더링 시점에 `getComputedStyle`로 실시간으로 읽어옴)이 바로 반영됩니다. 다른 걸
건드릴 필요 없습니다.

| 변수 | 다크 기본값 | 라이트 기본값 | 용도 |
|---|---|---|---|
| `--bg-color` | `#0d1117` | `#ffffff` | 페이지 배경 그라디언트 끝점 |
| `--bg-gradient-start` / `--bg-gradient-end` | `#1c1d2e` / `#0d1117` | `#eef1f5` / `#ffffff` | `body`의 radial 그라디언트 |
| `--container-bg` | `rgba(22,27,34,.7)` | `rgba(255,255,255,.85)` | 본문 카드, 그래프 미니박스, 백링크 박스 |
| `--border-color` | `rgba(48,54,61,.8)` | `rgba(31,35,40,.15)` | 사이트 전역 얇은 테두리 전부 |
| `--text-primary` | `#c9d1d9` | `#1f2328` | 본문 텍스트 색 |
| `--text-muted` | `#8b949e` | `#59636e` | 사이드바 라벨, 아웃라인 링크, 그래프 엣지/라벨 색 |
| `--heading-color` | `#ffffff` | `#1f2328` | `h1`/`h2` 헤딩 색 |
| `--accent-blue` | `#58a6ff` | `#0969da` | 사이트 제목 그라디언트, 아웃라인 활성 항목, 그래프 "현재 노트" 노드 |
| `--accent-purple` | `#8ab4f8` | `#8250df` | 볼트 배지 강조색, 사이트 제목 그라디언트 |
| `--wiki-link` / `--wiki-link-hover` | `#a371f7` / `#b893f9` | `#8250df` / `#6639ba` | 위키링크 색, 그래프 노드 색(현재 노트 아닌 것) |
| `--link-hover-color` | `#79c0ff` | `#0550ae` | 일반 마크다운 링크 hover |
| `--code-bg` | `#161b22` | `#f6f8fa` | 인라인 코드 / 검색창 배경 |
| `--code-text-color` | `#e6edf3` | `#24292f` | 코드 블록 텍스트 색 |

실제 어느 블록이 적용되는지는 테마 상태에 따라 다릅니다: `:root`는 무조건 적용되는 다크
기본값, `@media (prefers-color-scheme: light)`는 OS가 라이트를 선호할 때 그 위에 라이트를
얹음(단, 방문자가 이미 다크로 토글했으면 안 얹힘), `:root[data-theme="light"]`/
`[data-theme="dark"]`는 사이트의 `defaultTheme` 설정이나 방문자의 토글 클릭이 있을 때 둘
다를 덮어씀. 테마와 무관하게 한 가지 톤만 쓰고 싶다면 `publish.css`에서 `:root`,
`:root[data-theme="light"]`, `:root[data-theme="dark"]` 세 군데 전부에 같은 값으로
재정의하면 됩니다.

## 구조 클래스/ID

페이지 위치별로 묶었습니다.

**왼쪽 사이드바** (`.pumice-nav-sidebar`, 항상 존재 — 이 배치가 어떻게 정해졌는지는 pumice
저장소의 `43_사이트_제목_왼쪽_사이드바_이동.md` / `44_그래프_TOC_왼쪽_사이드바_병합.md`
참고):
- `.site-title` — 로고+사이트명 링크, 첫 번째 자식
- `.pumice-icon-button` — 테마토글 버튼(`#pumice-theme-toggle`)
- `.pumice-search` / `input#pumice-search-input` — 검색창
- `.pumice-search-results`(`#pumice-search-results`) — 결과 드롭다운, 안에 `.search-result-title`/`.search-result-snippet`/`.search-status`
- `.pumice-search-clearance`(`#pumice-search-clearance`) — 보이지 않는 spacer, JS가 드롭다운의 실제 높이에 맞춰 크기를 조정해서 아래 아웃라인을 절대 안 덮음(`48_검색_여백을_드롭다운_실제_높이에_맞춤.md` 참고) — 여기에 `background`를 넣지 마세요, 원래 안 보이는 용도입니다
- `#pumice-outline`(`.pumice-outline-sidebar`) — 목차(TOC), 중첩 헤딩은 `.pumice-outline-children`, 스크롤스파이로 활성화된 항목은 `.is-active`
- 평범한 `ul`/`li`/`.pumice-nav-folder-title`/`.nav-folder` — 내비게이션이 켜져 있을 때의 폴더트리

**오른쪽 컬럼** (`.pumice-right-column`, 노트에 그래프가 있을 때만 존재 —
`45_그래프_다시_오른쪽으로_이동_및_드래그_패닝.md` 참고):
- `.pumice-graph-mini` — 항상 보이는 미니 그래프 박스, 헤더 행은 `.pumice-graph-mini-header`/`.pumice-graph-mini-icons`/`.pumice-graph-icon-button`, PixiJS 캔버스는 `#pumice-graph-mini-canvas`
- `.pumice-graph-fullpage`(`#pumice-graph-fullpage`, 접혀있을 땐 `[hidden]`) — 확장된, 뷰포트 전체를 덮는 그래프. `.pumice-graph-fullpage-header`, `#pumice-graph-fullpage-canvas`

**메인 컬럼** (`.pumice-main-column`):
- `header` / `.vault-badge` — 상단 바, 이제 볼트 배지만 남음(제목은 사이드바로 이동)
- `.container.markdown-body` — 노트 본문 자체. 헤딩/코드/표/인용/위키링크 스타일은 `.markdown-body *` 규칙 참고
- `#pumice-backlinks`(`.pumice-sidebar`) — 노트 본문 밑 백링크 박스
- `.pumice-sliding-track` / `.pumice-sliding-pane` — 슬라이딩 윈도우 모드의 가로 스크롤 패널 트레일

**레이아웃 래퍼**: `.layout`(`.wide`/`.has-nav`/`.has-graph` 수식자가 최대 너비와 flex
2/3분할 레이아웃 활성 여부를 결정).

## 예시

```css
/* 왼쪽 사이드바를 넓히고, 그래프까지 같이 바뀌는 브랜드 강조색 적용 */
:root, :root[data-theme="light"], :root[data-theme="dark"] {
  --accent-blue: #ff6b6b;
  --wiki-link: #ff6b6b;
}
.pumice-nav-sidebar {
  flex-basis: 280px;
}
.pumice-graph-mini {
  border-radius: 4px; /* 기본 10px 대신 각진 모서리 */
}
```
