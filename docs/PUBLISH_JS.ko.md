# `publish.js`로 퍼블리시 사이트 커스터마이징

`publish.js`도 [`publish.css`](PUBLISH_CSS.ko.md)와 같은 볼트 루트 컨벤션을 따릅니다 — 볼트
루트에 정확히 `publish.js`라는 파일이 있으면 자동으로 감지됩니다(`web.py`의
`_find_root_asset_href`). 하지만 중요한 차이가 하나 있습니다.

## 커스텀 도메인이 있어야만 활성화됩니다

```python
publish_js_href = _find_root_asset_href(published_files, "publish.js", url_prefix) if is_custom_domain else None
```

`publish.css`와 달리 `publish.js`(그리고 Google Analytics)는 `is_custom_domain` 뒤에
게이트돼 있습니다 — 실제 옵시디언 퍼블리시와 정확히 동일합니다. `obsidian.asar`의
`isCustomDomain()`(`!!this.customurl`)을 직접 읽어서 확인한 내용이고, 전체 조사 과정은
pumice 저장소의 `26_커스텀_도메인_지원.md`에 있습니다. 볼트가 "커스텀 도메인이 있다"고
카운트되는 건 소유자가 실제로 커스텀 도메인을 설정했을 때뿐입니다(플러그인의 "Custom
domain" 설정 → `POST /api/customurl`) — 그리고 일단 설정되면, 그 볼트로 들어오는 **모든**
URL에서 `publish.js`가 로드됩니다. 커스텀 호스트네임으로 직접 들어온 요청뿐 아니라 기존
`/publish/{username}/{vault}/...` 경로로 들어와도 마찬가지입니다. "이 요청이 커스텀
도메인을 통해 들어왔는가"가 아니라 "이 사이트에 커스텀 도메인이 설정돼 있는가"를 봅니다.

볼트 루트에 `publish.js`를 올려뒀는데 커스텀 도메인을 설정 안 했다면 조용히 무시됩니다 —
파일 자체는 다른 에셋처럼 퍼블리시돼 있지만 `publish_js_href`는 항상 `None`이라 아무것도
로드하지 않습니다.

## 로드 방식

```html
<link rel="stylesheet" href="{{ publish_css_href }}">   <!-- publish.css가 있으면 -->
<script src="{{ publish_js_href }}" defer></script>      <!-- publish.js가 있고 커스텀 도메인일 때만 -->
{% if google_analytics %}...{% endif %}                  <!-- 이것도 커스텀 도메인 게이트 -->
```

`defer`는 다운로드는 나머지 페이지와 병렬로 하되, 실행은 문서 순서대로, 파싱이 끝난 후
`DOMContentLoaded`가 발생하기 전에 한다는 뜻입니다.

## 할 수 있는 것

그냥 평범한 `<script>`고(콘텐츠 준비를 기다려야 하는 건 아래 참고), 페이지 전체 DOM에
아무 제약 없이 접근합니다 — 샌드박스 같은 거 없습니다. 구체적으로:

- **DOM 전체, 제약 없음.** `PUBLISH_CSS.md`의 클래스/ID 레퍼런스에 나온 것들 전부 스타일만
  바꾸는 게 아니라 읽고 고쳐 쓸 수 있습니다 — 배너나 푸터 문구 삽입(예시 파일이 이걸 함),
  모든 `<pre>`에 "복사" 버튼 추가, `.wiki-link` href 재작성, `.note-title` 옆에 예상 읽기
  시간 표시, `.container` 뒤에 댓글 위젯(giscus, utterances 등) 임베드 등.
- **`window.pumicePreload`** — `{ url_prefix, path, sliding_window_mode }`, pumice 자체
  렌더러가 쓰는 것과 같은 preload 데이터(`publish_render.ts`의 `interface PumicePreload`).
  스크립트가 어느 노트에서 실행 중인지에 따라 분기하거나, 사이트 자체 url_prefix와 일치하는
  절대경로 href를 만들 때 씁니다(커스텀 도메인에서는 이게 상대경로가 아니라 전체
  origin이라 중요).
- **`window.PumicePublishRenderer`** — pumice 내부 함수들을 클로저에 숨기지 않고 일부러
  노출해둔 것(`publish_render.ts` 맨 아래 `api` 객체 참고): `renderMarkdown`,
  `buildGraph`/`buildLocalGraph`, `searchNotes`/`mergeSearchResults`, `buildOutline`,
  `buildBacklinks`, `resolveWikilinkHref`, `cssColorToPixiHex` 등. 위키링크 해석이나 검색
  매칭 로직을 직접 다시 구현하지 말고 이걸 재사용하세요 — 예를 들어 다르게 필터링된 두
  번째 검색창이나, `buildGraph`의 엣지 목록으로 만든 "관련 노트" 위젯 같은 것.
- **표준 브라우저 API 전부** — `fetch`로 자기 백엔드나 제3자 임베드 호출, 읽기 진행률
  바용 `IntersectionObserver`, 그래프 캔버스용 `ResizeObserver`, 추가 웹폰트 로딩, 서비스
  워커 등록 등. 다른 평범한 페이지와 동일합니다.

**설계상 한계**: 클라이언트 사이드에서만 동작하고 서버가 이미 렌더링해서 보낸 페이지 위에서
실행되는 거라, 마크다운이 어떻게 파싱되는지나 어떤 파일이 퍼블리시되는지는 바꿀 수 없습니다.
DOM과 위 두 전역 객체 말고는 별도 플러그인 훅/이벤트 시스템이 없어서, pumice 자체의
비동기 작업(노트 콘텐츠, 검색 결과, 그래프 렌더링)에 반응해야 하는 건 전용 이벤트를
기다리는 대신 DOM을 직접 관찰(폴링, `MutationObserver`)해야 합니다. 그리고 페이지 전체에
접근 가능한 임의 스크립트라, 어떤 볼트의 `publish.css`/`publish.js`를 설정할 수 있는 사람은
그 볼트의 퍼블리시된 페이지에 XSS를 건 것과 실질적으로 같은 힘을 갖습니다 — 이건 이
구현만의 허점이 아니라 실제 옵시디언 퍼블리시 자체가 쓰는 신뢰 모델 그대로입니다(볼트
소유자만, 위의 커스텀 도메인 체크로 게이트).

## 타이밍: "노트 콘텐츠 준비 완료" 이벤트는 없습니다

pumice 자체 노트 렌더링(`publish_render.js`)도 `DOMContentLoaded`에서 시작하고, 그 다음
추가로 비동기 작업(볼트 전체 메타데이터 캐시 가져오기, 마크다운을
`#pumice-note-root`에 렌더링, 그래프/검색/아웃라인 배선)을 합니다 — 이게 다 끝난 시점에
발생하는 커스텀 이벤트는 없습니다. `publish.js`가 로드 시점에 딱 한 번만 실행되면 되는
작업(배너 삽입, 분석 도구 세팅 등)이라면 평범한 `DOMContentLoaded`/최상위 리스너로
충분합니다. 렌더링된 노트 본문에 반응해야 하는 작업(콘텐츠 뒤에 뭔가 추가, 링크 재작성 등)
이라면 `#pumice-note-root`에 실제로 자식이 생겼는지 폴링해야 합니다 — 아래 예시 참고.

## 예시

둘 다 라이브 렌더링에 실제로 적용해서 검증됨, 위 레퍼런스만 보고 짐작으로 쓴 게 아닙니다:

- [`docs/examples/publish.js.example`](examples/publish.js.example) — 고정 위치 배지 +
  노트 콘텐츠가 준비된 다음에 붙는 문단, 콘솔 출력과 스크린샷으로 확인함.
- [`docs/examples/publish-slash-search.js.example`](examples/publish-slash-search.js.example)
  — `/`를 누르면 검색창에 포커스+전체선택, `Esc`면 blur, 이미 다른 입력 필드에 포커스가
  있으면 단축키를 건너뜀. 아이디어는
  [옵시디언 포럼의 커뮤니티 publish.js 스니펫](https://forum.obsidian.md/t/publish-js-code-snippet-for-slash-key-quick-search-functionality/53141)
  에서 가져왔고(실제 옵시디언의 검색창은 거기서 `.search-bar` 클래스를 씀, 라이브로 확인된
  내용) — pumice 자체의 `#pumice-search-input`을 겨냥해서 새로 구현했습니다, 그대로
  복사하지 않았습니다. 포럼 버전엔 없던 "이미 편집 가능한 필드에 포커스 중이면 건너뛰기"
  가드는 자체 추가분입니다. `KeyboardEvent`를 직접 디스패치해서 검증함(`/` → 포커스+선택,
  `Esc` → blur, 검색창에 이미 포커스가 있고 텍스트가 있는 상태에서 `/`를 눌러도 텍스트가
  그대로 유지되고 재선택되지 않는 것까지 확인).
