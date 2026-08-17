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

## What you get access to

It's a plain `<script>` with the full page already in the DOM (once you've waited for it, see
below) — nothing is sandboxed. Concretely:

- **The whole DOM, unrestricted.** Everything documented in `PUBLISH_CSS.md`'s class/ID
  reference is fair game to read or rewrite, not just restyle: inject a banner or footer note
  (the example does this), add a "copy" button to every `<pre>`, rewrite `.wiki-link` hrefs,
  add a reading-time estimate next to `.note-title`, embed a comment widget (giscus, utterances,
  ...) after `.container`, ...
- **`window.pumicePreload`** — `{ url_prefix, path, sliding_window_mode }`, the same preload
  data pumice's own renderer uses (`interface PumicePreload` in `publish_render.ts`). Lets your
  script branch on which note it's running on, or build absolute hrefs consistent with the
  site's own url_prefix (matters on a custom domain, where it's a full origin, not a relative
  path).
- **`window.PumicePublishRenderer`** — pumice's own internal functions, exported deliberately
  (see the `api` object at the bottom of `publish_render.ts`) rather than hidden in a closure:
  `renderMarkdown`, `buildGraph`/`buildLocalGraph`, `searchNotes`/`mergeSearchResults`,
  `buildOutline`, `buildBacklinks`, `resolveWikilinkHref`, `cssColorToPixiHex`, and more. Reuse
  these instead of reimplementing wikilink resolution or search matching yourself — e.g. a
  second, differently-filtered search box, or a small "related notes" widget built from
  `buildGraph`'s own edge list.
- **Any standard browser API** — `fetch` to call your own backend or a third-party embed,
  `IntersectionObserver` for a reading-progress bar, a `ResizeObserver` on the graph canvas,
  loading additional web fonts, registering a service worker, etc. Same as any other page.

**Scope limits, by design**: it's client-side only, runs after the server has already rendered
and shipped the page — it can't change how markdown gets parsed or which files get published.
There's no plugin-hook/event system beyond the DOM and the two globals above, so anything that
needs to react to pumice's own async work (note content, search results, graph render) has to
observe the DOM directly (poll, `MutationObserver`) rather than listen for a dedicated event.
And since it's arbitrary script with full page access, whoever can set `publish.css`/
`publish.js` for a vault has the same practical power as someone doing an XSS on that vault's
published pages — which is exactly the trust model real Obsidian Publish itself uses (only the
vault owner, gated the same way via the custom-domain check above), not a gap specific to this
implementation.

## Timing: there's no "note content ready" event

pumice's own note rendering (`publish_render.js`) also runs on `DOMContentLoaded`, then does
further async work (fetching the vault-wide metadata cache, rendering markdown into
`#pumice-note-root`, wiring the graph/search/outline) — none of which is done by the time
`DOMContentLoaded` fires. There's no custom event fired when it finishes. If your `publish.js`
only needs to run once at load (inject a banner, set up analytics, etc.), a plain
`DOMContentLoaded`/top-level listener is fine. If it needs to react to the rendered note body
(add something after the content, rewrite a link, ...), poll for `#pumice-note-root` actually
having children — see the example below.

## Examples

Both verified-working, actually applied against a live render, not just written from the
reference above:

- [`docs/examples/publish.js.example`](examples/publish.js.example) — fixed corner badge +
  a paragraph appended once the note content is ready, confirmed via console output and
  screenshot.
- [`docs/examples/publish-slash-search.js.example`](examples/publish-slash-search.js.example) —
  `/` focuses and selects the search box, `Escape` blurs it, skips the shortcut when focus is
  already in an editable field. The idea comes from a
  [community publish.js snippet on the Obsidian forum](https://forum.obsidian.md/t/publish-js-code-snippet-for-slash-key-quick-search-functionality/53141)
  (real Obsidian's search input there is `.search-bar`, confirmed live) — reimplemented fresh
  against pumice's own `#pumice-search-input`, not copied, with the editable-field guard as an
  own addition the forum version didn't have. Verified via direct `KeyboardEvent` dispatch
  (`/` focuses+selects, `Escape` blurs, and pressing `/` while the search box is already
  focused and has text leaves the text untouched instead of re-selecting).
