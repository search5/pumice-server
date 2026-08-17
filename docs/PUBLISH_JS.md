# Customizing a published site with `publish.js`

`publish.js` follows the same vault-root convention as
[`publish.css`](PUBLISH_CSS.md) — a file named exactly `publish.js` at the vault root gets
auto-detected (`_find_root_asset_href` in `web.py`) — but with one important difference:

## It only activates on a custom domain

```python
publish_js_href = _find_root_asset_href(published_files, "publish.js", url_prefix) if is_custom_domain else None
```

Unlike `publish.css`, `publish.js` (and Google Analytics) is gated behind `is_custom_domain` —
this matches real Obsidian Publish exactly, confirmed via reading `obsidian.asar`'s own
`isCustomDomain()` (`!!this.customurl`, see `26_커스텀_도메인_지원.md` in the pumice repo for
the full investigation). A vault only counts as having a custom domain once its owner actually
configures one (the plugin's "Custom domain" setting → `POST /api/customurl`) — and once
configured, `publish.js` loads on **every** URL that reaches that vault, including the old
`/publish/{username}/{vault}/...` path, not just requests that arrive via the custom hostname
itself. It's "does this site have a custom domain configured", not "did this particular request
come in through it".

If you drop a `publish.js` at the vault root but haven't configured a custom domain, it's
silently ignored — the file sits there published like any other asset, but `publish_js_href` is
always `None` and nothing loads it.

## How it loads

```html
<link rel="stylesheet" href="{{ publish_css_href }}">   <!-- if publish.css -->
<script src="{{ publish_js_href }}" defer></script>      <!-- if publish.js AND custom domain -->
{% if google_analytics %}...{% endif %}                  <!-- also custom-domain-gated -->
```

`defer` means it downloads in parallel with the rest of the page but executes in document order,
after parsing finishes and before `DOMContentLoaded` fires.

## Timing: there's no "note content ready" event

pumice's own note rendering (`publish_render.js`) also runs on `DOMContentLoaded`, then does
further async work (fetching the vault-wide metadata cache, rendering markdown into
`#pumice-note-root`, wiring the graph/search/outline) — none of which is done by the time
`DOMContentLoaded` fires. There's no custom event fired when it finishes. If your `publish.js`
only needs to run once at load (inject a banner, set up analytics, etc.), a plain
`DOMContentLoaded`/top-level listener is fine. If it needs to react to the rendered note body
(add something after the content, rewrite a link, ...), poll for `#pumice-note-root` actually
having children — see the example below.

## Example

A verified-working example lives at
[`docs/examples/publish.js.example`](examples/publish.js.example) — actually applied against a
live render (fixed corner badge + a paragraph appended once the note content is ready) and
confirmed via console output and screenshot, not just written from the reference above.
