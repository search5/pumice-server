# 서버 플러그인 개발 가이드

pumice-server 플러그인은 **퍼블리시된 페이지**가 렌더링할 수 있는 것을 확장합니다. 완전히
서버 사이드에서, 웹 서버 자신과 같은 파이썬 프로세스 안에서 돌고, 이미 퍼블리시된 콘텐츠
(`data_dir/published/...`)만 건드립니다 — vault 동기화, 버전 히스토리, MCP 도구와는 아무 관계가
없습니다. 아직 [README.md](../README.ko.md)의 "서버 플러그인" 절을 안 읽으셨다면 거기서 전체
생명주기부터 보고 오세요; 이 문서는 실제로 플러그인을 만들 때 필요한 상세 가이드입니다.

`pumice-plugin-immich`(이 저장소 기준 `../pumice-plugin-immich`에 있는 sibling 패키지)가 완전히
동작하는 실제 예시입니다. 이 가이드 곳곳에서 그 코드를 구체적으로 참조하니, 문서와 같이 그
소스를 읽는 게 가장 빠르게 이해하는 방법입니다.

## 1. 사고 모델

- **코어는 당신의 문법을 전혀 모릅니다.** pumice-server는 `![immich:...]`가 뭘 뜻하는지 모르고,
  앞으로도 모를 겁니다 — 그 패턴은 pumice-plugin-immich 코드 안에서만 정의되고 매칭됩니다.
  당신의 플러그인은 페이지 전체 콘텐츠를 통째로 넘겨받아서, 그 안에서 자기 문법을 스스로
  찾아 치환할 책임이 있습니다. 이건 의도된 설계입니다 — 플러그인이 코어로부터, 그리고 서로부터
  분리된 상태를 유지하는 유일한 방법이거든요.
- **플러그인은 순수한 `content -> content` 함수입니다.** 파일시스템 접근도, DB 접근도,
  pumice-server 내부로의 네트워크 접근도 없습니다. 자격증명이나 다른 설정이 필요하면 매개변수로
  전달받습니다([설정](#4-설정선택) 참고) — 당신이 직접 pumice-server 저장소에 손대는 일은 없습니다.
- **런타임 설치는 없습니다.** 관리자 업로드 흐름도, 플러그인 마켓플레이스도 없습니다. 플러그인은
  `pyproject.toml` 의존성으로 선언되고 빌드 시점에 Docker 이미지에 포함되는 평범한 파이썬
  패키지입니다 — 다른 서버 의존성을 추가하는 것과 똑같은 무게를 가지고, 이 서버가 의존하는
  다른 모든 것과 같은 검토/빌드/재배포 경로를 거칩니다.

## 2. 최소 골격

```
my-plugin/
  pyproject.toml
  my_plugin/
    __init__.py
  tests/
    test_resolve.py
```

```toml
# pyproject.toml
[project]
name = "my-plugin"
version = "0.1.0"
description = "퍼블리시된 페이지에서 my-plugin의 임베드 문법을 해석합니다."
requires-python = ">=3.13"
dependencies = []

[project.entry-points."pumice_server.plugins"]
my-plugin = "my_plugin:resolve"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```python
# my_plugin/__init__.py
def resolve(content: str, *, owner_username: str = None, vault_id: str = None,
            config_entries: dict = None, **_kwargs) -> str:
    return content  # 여기에 실제 변환 로직을 채우세요
```

이게 계약의 전부입니다. 아래 내용은 전부 이 위에 얹히는 세부 사항입니다.

## 3. `resolve()` 계약

```python
def resolve(content: str, *, owner_username: str, vault_id: str, config_entries: dict) -> str:
    ...
```

- **`content`** — 노트의 가공된 마크다운. frontmatter는 이미 제거됐고, 위키링크(`[[노트]]`)도
  이미 실제 `<a>`/`<img>` 태그로 해석된 직후, 마크다운→HTML 변환 직전 시점입니다. 위키링크
  해석 결과가 그렇듯, 진짜 HTML 태그를 그대로 반환해도 똑같이 동작합니다 — 마크다운으로 만들
  필요 없습니다.
- **`owner_username` / `vault_id`** — 누구의 어떤 vault인지. 거의 항상 `config_entries`에서
  올바른 항목을 찾는 데만 쓰입니다; 이걸로 별도 저장소에 접근하려 하지 마세요 — 애초에 그런
  저장소가 없습니다.
- **`config_entries`** — `{entry_id: {key: value, ...}}`, 이미 복호화되어 바로 쓸 수 있는
  상태로 전달됩니다. 플러그인이 `CONFIG_FIELDS`를 선언하지 않았거나, vault 소유자가 아직 설정한
  항목이 없으면 빈 딕셔너리입니다. [설정](#4-설정선택) 참고.
- **반환값** — (가능하다면) 변환된 콘텐츠. 인식하지 못했거나 지금 처리할 수 없는 부분은
  `content`를 그대로 반환하세요 — 이해하지 못하는 부분을 변형하거나 삭제하면 안 됩니다.
- **`**kwargs`를 받으세요.** 위 시그니처가 현재 계약의 전부지만, 항상 `**kwargs`를 받거나(또는
  모든 키워드에 기본값을 주거나) 해서 나중에 코어가 새 키워드를 추가해도 플러그인이 깨지지
  않게 하세요.

### 언제 호출되는가

`resolve()`는 `plugin_loader.run_enabled_plugins()`가 주도하는 두 지점에서 호출됩니다:

1. **업로드 시점** (`POST /api/upload`) — 노트가 퍼블리시된 직후, best-effort로 즉시 한 번
   시도해서, 실제 첫 방문자가 플러그인의 외부 의존성을 기다리거나 그 위험을 감수하지 않게
   합니다. 여기서 실패해도 로그만 남고 조용히 무시됩니다 — 업로드 자체는 플러그인이 뭘 하든
   항상 성공합니다.
2. **조회 시점** (`GET /publish/...`) — 캐시 미스일 때만 지연 호출됩니다([캐싱](#6-캐싱과-그게-플러그인에-의미하는-것) 참고).

두 호출 지점 모두 같은 콘텐츠에 대해 동일한 인자를 넘깁니다 — `resolve()`를 입력에 대한 순수
함수로만 작성하면 어느 호출 지점인지 신경 쓸 필요가 없습니다.

### 에러 처리

가능하면 예기치 못한 예외가 새어나가지 않게 하세요. 하지만 새어나가도 괜찮습니다 —
pumice-server가 잡아서 로그를 남기고 그 렌더링에서 당신의 플러그인만 건너뜁니다(페이지는
정상적으로 렌더링되고, 당신의 문법은 그대로 남습니다). 공개 페이지를 보는 방문자는 플러그인의
버그나 그게 의존하는 상위 서비스가 다운된 것 때문에 500을 보게 되면 절대 안 됩니다.

권장하는 패턴(`pumice_plugin_immich._resolve_one` 참고): 임베드 하나 단위로 스스로 실패를 잡고,
그 임베드의 원본 문법을 그대로 남기는 방식으로 폴백하세요. 이러면 이미지 하나가 깨져도 페이지
전체를 끌고 내려가지 않고, pumice-server의 일반적인 "플러그인이 예외를 던짐" 메시지보다 훨씬
구체적인 로그를 남길 기회도 얻습니다.

## 4. 설정(선택)

vault 소유자로부터 자격증명이나 설정을 받아야 한다면, 수집할 필드를 나열한 평범한 리스트를
가리키는 두 번째(선택적) entry point를 선언하세요:

```toml
[project.entry-points."pumice_server.plugin_config_fields"]
my-plugin = "my_plugin:CONFIG_FIELDS"
```

```python
CONFIG_FIELDS = [
    {"key": "server_url", "label": "My service URL", "type": "text"},
    {"key": "token", "label": "API token", "type": "password"},
]
```

이걸 선언하면 vault 소유자의 **퍼블리시** 탭에서 진짜 라벨 붙은 설정 폼이 렌더링됩니다 — 절대
JSON 텍스트박스가 아닙니다 — 그러면서도 pumice-server 자체 코드는 당신의 키가 뭘 뜻하는지 전혀
모릅니다. 그냥 당신이 선언한 스키마대로 렌더링할 뿐입니다.

- **`type`**은 `"text"` 또는 `"password"`입니다. `"password"` 필드는 폼에서 마스킹되고, 이게
  중요한데 — **저장된 값이 저장 이후 다시는 브라우저로 돌아오지 않습니다**; 폼은 항상 빈 칸으로
  보여줍니다. 그래서 vault 소유자가 기존 항목을 *수정*할 때 password 필드를 빈 칸으로 두면
  "기존 값 유지"를 뜻하지 "값을 지운다"는 뜻이 아닙니다 — 이 병합 로직은 pumice-server 안에
  이미 구현돼 있으니 당신이 따로 구현할 건 없습니다.
- **항목은 `(owner_username, vault_id, plugin_id, entry_id)` 단위**로 저장되어, 하나의
  플러그인이 여러 개의 이름 붙은 설정을 지원할 수 있습니다(예: pumice-plugin-immich는 Immich
  프로필당 하나씩). `entry_id`는 vault 소유자가 항목을 추가할 때 직접 입력하는 자유 텍스트
  이름입니다 — 당신의 임베드 문법이 "profile id" 같은 걸 참조한다면, vault 소유자가 `entry_id`를
  그거랑 맞춰서 입력하도록 문서화하세요(harang-immich의 `![immich:profileId/assetId]`와 그
  설정 항목들은 순전히 관례로 이렇게 연결돼 있고, pumice-server가 강제하는 건 아닙니다).
- **저장 시 암호화**됩니다(서버가 들고 있는 `PLUGIN_CONFIG_ENCRYPTION_KEY` 키로), 당신의
  `resolve()`가 보기 전에 이미 복호화되어 있습니다 — 암호문을 직접 다룰 일은 없습니다.
- **알려진 한계:** `label` 문자열은 지금은 그냥 평문·비지역화 텍스트로 렌더링됩니다 — 플러그인이
  선언한 설정 필드 라벨에 대한 i18n 훅은 아직 없습니다. 이게 생기기 전까지는 라벨을 짧고
  언어 중립적으로(또는 그냥 명확한 영어로) 유지하세요.

`CONFIG_FIELDS`를 선언하지 않으면 `config_entries`는 항상 `{}`이고 당신의 플러그인을 위한 설정
UI 자체가 없습니다 — 설정이 전혀 필요 없는 플러그인이라면 이걸로 충분합니다.

## 5. 자동 채움(선택)

`CONFIG_FIELDS`가 요구하는 값 중 일부는 사실 비밀값이 아니고, 이미 vault 동기화 파일 안에
들어있을 수도 있습니다. pumice-plugin-immich의 `server_url`과 프로필 id가 좋은 예시입니다 —
harang-immich 옵시디언 플러그인이 이미 이 둘을 평문으로
`.obsidian/plugins/harang-immich/data.json`에 써두고 있고(진짜 Immich API 키만 `secretStorage`에
따로 있음), 그 파일은 그냥 pumice-server가 이미 동기화하고 저장하고 있는 평범한 vault 파일일
뿐입니다. vault 소유자가 자기 vault 안에 이미 있는 UUID를 보고 URL을 손으로 다시 옮겨 적고
`entry_id`를 그거랑 맞춰 추측하게 만드는 건 불필요한 수고입니다. 두 개의 추가 선택적 entry
point로 플러그인이 값을 직접 제안할 수 있습니다:

```toml
[project.entry-points."pumice_server.plugin_autofill_paths"]
my-plugin = "my_plugin:AUTOFILL_PATHS"

[project.entry-points."pumice_server.plugin_autofill"]
my-plugin = "my_plugin:suggest_autofill"
```

```python
AUTOFILL_PATHS = [".obsidian/plugins/other-plugin/data.json"]

def suggest_autofill(vault_files: dict) -> list:
    raw = vault_files.get(AUTOFILL_PATHS[0])
    if not raw:
        return []
    ...
    return [{"entry_id": "...", "label": "...", "values": {"server_url": "..."}}, ...]
```

- **`AUTOFILL_PATHS`**는 읽고 싶은 vault-relative 파일 목록을 선언합니다 — 그 외엔 아무것도
  없습니다. pumice-server는 정확히 그 파일들만(vault 루트 기준 상대경로로) 읽고, 없거나 읽을 수
  없는 파일은 `vault_files`에서 그냥 `None`으로 매핑됩니다 — pumice-server의 다른 best-effort
  호출들과 마찬가지로.
- **`suggest_autofill(vault_files)`**는 선언한 경로들에 대해 `{relative_path: content_또는_None}`
  을 받아서, `{"entry_id", "label", "values"}` 형태의 제안 리스트를 반환합니다. Publish 탭의
  설정 모달이 열릴 때 이걸 호출해서 각 제안을 `entry_id`와 당신이 제안한 필드들을 채워주는
  원클릭 버튼으로 보여줍니다 — 실제 비밀값은 여전히 vault 소유자가 직접 입력해야 합니다
  (`values` 안에 `password` 타입 `CONFIG_FIELDS` 키가 있어도 무엇을 반환하든 서버 쪽에서
  제거됩니다 — 플러그인이 비밀이 아닌 vault 파일에서 진짜 비밀값을 정당하게 얻어낼 방법은
  없으니까요).
- 두 entry point 모두 선택적이고 서로 독립적입니다 — 선언하지 않으면 그냥 제안이 없을 뿐,
  설정 모달이 깨지는 일은 없습니다(누락/로드 실패/예외 전부 "제안 없음"으로 처리되는 건
  `CONFIG_FIELDS`의 실패 처리 방식과 동일합니다).
- 이건 당신의 플러그인을 다른 플러그인의 *저장 포맷*에 결합시키는 것이고, 이 계약 전체가
  원래 피하려는 것([사고 모델](#1-사고-모델) 참고)이라, 이미 동기화되어 있는 안정적인 비밀
  아닌 데이터 소스가 진짜로 있을 때만 쓰세요 — 그 다른 플러그인의 데이터 형식이 바뀌면 같이
  맞춰줘야 한다는 것도 감안하시고요.

## 6. 캐싱과 그게 플러그인에 의미하는 것

퍼블리시된 페이지는 자체 서버사이드 HTML 캐시가 없습니다 — 조회할 때마다 노트를 처음부터 다시
파싱합니다. 대신 캐싱되는 건 각 플러그인의 `resolve()` 출력이고, `(owner, vault, plugin_id,
content, config)`로 키가 결정됩니다 — `plugin_loader.py`와 `plugin_resolve_disk_cache.py` 참고.
이건 당신에게는 투명합니다(cache_get/cache_set을 직접 볼 일은 없음), 하지만 딱 하나, 플러그인을
어떻게 작성해야 하는지에 직접 영향을 주는 동작이 있습니다:

> **`resolve()`의 출력은 입력과 다를 때만 캐싱됩니다.**

플러그인이 `content`를 완전히 그대로 반환한다면 — 그 안에 당신의 문법이 아예 없어서든, 내부
실패를 잡고 전부 그대로 두는 걸로 폴백해서든 — 그 호출은 **절대 캐싱되지 않고**, 실제로 다른
결과를 낼 때까지 매 조회/업로드마다 다시 라이브로 시도됩니다. 이건 의도된 자가 치유
동작입니다: 일시적 실패(외부 의존성이 잠깐 다운된 것)가 진짜 결과처럼 영구적으로 "구워지는" 일이
없습니다.

실무적으로 의미하는 것:

- **부분** 성공(임베드 몇 개는 해석되고, 깨진 하나는 그대로 남은 상태)은 전체가 하나로
  캐싱됩니다. 대상이 진짜로 존재하지 않아서 깨진 거라면(예: 삭제된 asset), 노트나 설정이 바뀔
  때까지 계속 깨진 채로 보이는 게 맞습니다. 일시적 장애 때문에 깨진 거라면 애초에 아무것도
  캐싱되지 않았으니 다음 번엔 페이지 전체가 다시 리졸브되면서 그 부분도 다시 기회를 얻습니다.
- pumice-server 캐시가 이미 처리해주는 것(리졸브 결과 자체)을 위해 호출 *간에* 별도의 장기
  캐싱을 직접 구현할 필요는 없습니다. 다만 저렴하고 프로세스 로컬인 건 계속 캐싱해도 됩니다 —
  예를 들어 하나의 `resolve()` 호출 안에서 여러 임베드에 재사용할 인증 토큰을 한 번만 얻어서
  쓰는 것 — 이건 한 호출 안에서 중복 작업을 피하는 문제지, 바깥쪽 캐시가 책임지는 영역이
  아닙니다.
- 외부 호출은 적당히 짧은 타임아웃으로 제한하세요 — `resolve()`가 느리면 업로드 응답이나 페이지
  렌더링이 그만큼 막힙니다.

## 7. 플러그인 테스트하기

이 코드베이스에서는 TDD가 기본 작업 방식이고(메인 저장소 자체 테스트 스위트의 스타일 참고),
플러그인에도 똑같이 적용됩니다:

- 실제 서비스를 직접 호출하지 말고 외부 HTTP 의존성을 모킹하세요(pumice-plugin-immich의 테스트
  스위트는 [`responses`](https://github.com/getsentry/responses) 라이브러리를 씁니다).
- 우아한 성능 저하 경로를 명시적으로 커버하세요: 특정 id에 설정이 없는 경우, 설정이 불완전한
  경우(필수 키 누락), 외부 호출이 아예 실패하는 경우, 같은 페이지에서 임베드 하나가 깨져도
  다른 게 막히지 않는 경우.
- 기존 Obsidian 클라이언트 플러그인의 임베드 문법을 그대로 따라간다면, 그 클라이언트 플러그인
  자체 패턴과 비교하는 정규식 일치 테스트를 추가해서 둘이 조용히 어긋나지 않게 하세요.
- 출력 HTML에 끼워 넣는 건 뭐든 이스케이프하세요(alt 텍스트, 라벨, 사용자가 작성한 텍스트가
  섞일 수 있는 모든 것) — 이걸 명시적으로 테스트하세요. 원시 HTML을 내보내는 플러그인은
  이스케이프를 안 하면 진짜 XSS 표면이 됩니다.

플러그인 자체 단위 테스트를 넘어서 전체 흐름(pumice-server가 실제로 이걸 호출하고, 캐싱하고,
올바르게 렌더링하는지)까지 확인하려면, 로컬에서 스크래치 pumice-server 인스턴스를 띄우고 당신의
패키지를 editable 의존성으로 추가한 뒤(`uv add --editable ../my-plugin`) 실제 HTTP 흐름을
직접 돌려보세요: vault에 플러그인을 활성화하고, 설정 항목을 저장하고, 노트를 퍼블리시하고,
렌더링된 페이지를 조회.

## 8. pumice-server에 설치하기

로컬 개발 시에는 editable 경로 의존성으로 추가하세요:

```toml
# pumice-server의 pyproject.toml
[tool.uv.sources]
my-plugin = { path = "../my-plugin", editable = true }
```

```
uv add my-plugin
```

프로덕션에서는 `[tool.uv.sources]`가 로컬 경로 대신 패키지가 실제로 있는 곳(git 소스, 프라이빗
인덱스 등)을 가리키게 하고 Docker 이미지를 다시 빌드하세요. `discover_plugins()`
(`src/pumice_server/plugin_registry.py`)는
`importlib.metadata.entry_points(group="pumice_server.plugins")`로 찾아냅니다 — pytest나
Flask가 자기 플러그인을 찾는 것과 같은 메커니즘입니다. 위의 entry point 선언 외에 따로 설정할
건 없습니다.

## 9. 활성화하고 사용하기

활성화는 셀프 서비스이고 계정이 아니라 **vault 단위**로 범위가 정해집니다 — 마이페이지가 아니라
vault 소유자의 **퍼블리시** 탭에서, 지금 선택된 vault에 대해서만. 이건 옵시디언 자체가 플러그인을
계정이 아니라 vault마다 범위를 정하는 것과 같은 방식입니다: 실제로 당신의 문법을 쓰는 vault만
플러그인을 켜두면 되고, 같은 계정이 소유한 (그 문법이 나올 일이 없는) 다른 vault에서 불필요하게
스캔하지 않아도 됩니다.

`CONFIG_FIELDS`를 선언했다면, vault 소유자는 같은 탭에서 플러그인이 실제로 뭔가 하려면 필요한
이름 붙은 설정 항목을 하나 이상 추가해야 합니다 — [설정](#4-설정선택) 참고.

## 10. 배포 전 체크리스트

- [ ] `resolve()`가 예상 가능한 실패(설정 없음, 서비스 접근 불가, 잘못된 형식의 임베드)에
      대해 절대 예외를 던지지 않는다 — 대신 스스로 잡고 우아하게 성능 저하 처리한다.
- [ ] `resolve()`가 `**kwargs`를 받거나(또는 모든 키워드에 기본값이 있어서) 향후 호환성이
      보장된다.
- [ ] 출력 HTML에 끼워 넣는 모든 값이 이스케이프된다.
- [ ] `plugin_id`(entry point 이름)를 신중하게 골랐다 — 이건 활성화·설정 저장의 기본 키라서,
      나중에 이름을 바꾸면 그 이름으로 활성화/설정해뒀던 모든 vault가 새 이름으로 다시
      해야 한다.
- [ ] 단위 테스트가 다음을 커버한다: 정상 경로, 설정 없음, 불완전한 설정, 외부 호출 실패, 같은
      페이지에서 임베드 하나가 깨져도 다른 게 막히지 않는 것.
- [ ] `CONFIG_FIELDS`를 선언했다면, password 타입 키가 실제로 비밀값이 맞다(반대로 되어 있지
      않다) — `"password"`로 표시된 건 마스킹되고 다시 돌려주지 않는다.
- [ ] 플러그인 자체 단위 테스트뿐 아니라 실제 스크래치 pumice-server 인스턴스로 엔드투엔드
      검증했다.
