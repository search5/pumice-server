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

## 타이밍: "노트 콘텐츠 준비 완료" 이벤트는 없습니다

pumice 자체 노트 렌더링(`publish_render.js`)도 `DOMContentLoaded`에서 시작하고, 그 다음
추가로 비동기 작업(볼트 전체 메타데이터 캐시 가져오기, 마크다운을
`#pumice-note-root`에 렌더링, 그래프/검색/아웃라인 배선)을 합니다 — 이게 다 끝난 시점에
발생하는 커스텀 이벤트는 없습니다. `publish.js`가 로드 시점에 딱 한 번만 실행되면 되는
작업(배너 삽입, 분석 도구 세팅 등)이라면 평범한 `DOMContentLoaded`/최상위 리스너로
충분합니다. 렌더링된 노트 본문에 반응해야 하는 작업(콘텐츠 뒤에 뭔가 추가, 링크 재작성 등)
이라면 `#pumice-note-root`에 실제로 자식이 생겼는지 폴링해야 합니다 — 아래 예시 참고.

## 예시

실제로 검증된 예시가 [`docs/examples/publish.js.example`](examples/publish.js.example)에
있습니다 — 라이브 렌더링에 실제로 적용해서(고정 위치 배지 + 노트 콘텐츠가 준비된 다음에
붙는 문단) 콘솔 출력과 스크린샷으로 확인함, 위 레퍼런스만 보고 짐작으로 쓴 게 아닙니다.
