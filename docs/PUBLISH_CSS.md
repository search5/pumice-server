# Customizing a published site with `publish.css`

Every published page auto-loads a `publish.css` file if one exists at the **root** of the vault
(a same-named file inside a subfolder doesn't count — see `_find_root_asset_href` in `web.py`).
This mirrors real Obsidian Publish's own "Customize your site" feature exactly: no upload flow,
no settings toggle, just drop `publish.css` at the vault root and publish it like any other file.

## How it loads

```html
<style> ... pumice's own built-in styles ... </style>
{% if publish_css_href %}
<link rel="stylesheet" href="{{ publish_css_href }}">
{% endif %}
</head>
```

`publish.css` is linked as the **last** stylesheet in `<head>` (see `publish_page.jinja2`). By
normal CSS cascade rules, any rule in `publish.css` beats pumice's own built-in rule of equal
specificity — you don't need `!important` for a plain override, only if you're fighting a rule
that has an ID selector or similar in it already.

**A `publish.css` written for a real Obsidian Publish site will mostly have no effect here.**
Real Obsidian's own class names (`.site-body-left-column`, etc.) and CSS variable names
(`--text-normal`, `--interactive-accent`, etc.) don't exist in pumice's DOM — pumice is its own
from-scratch reimplementation with its own naming (see the class/variable reference below). Only
generic element selectors (`body`, `a`, `h1`, ...) in an imported real-Obsidian stylesheet will
carry over; everything component-specific needs to target pumice's own selectors instead.

**JS-driven behavior is untouched by any CSS you write.** Search's dynamic dropdown sizing,
graph drag-to-pan, scroll-spy, and the theme toggle all locate their elements by `id`
(`#pumice-search-input`, `#pumice-graph-collapse`, ...), not by class — restyling any of the
classes below can't break that wiring. The only way to break it is renaming/removing those `id`s
yourself via a plugin, which is out of scope for a CSS-only override.

## Theme variables (`:root`, `[data-theme]`)

Redefine any of these in `publish.css` and everything that reads them (including the graph's own
node/edge colors, computed live via `getComputedStyle` at render time) updates immediately — no
need to touch anything else.

| Variable | Dark default | Light default | Used for |
|---|---|---|---|
| `--bg-color` | `#0d1117` | `#ffffff` | Page background gradient endpoints |
| `--bg-gradient-start` / `--bg-gradient-end` | `#1c1d2e` / `#0d1117` | `#eef1f5` / `#ffffff` | `body`'s radial gradient |
| `--container-bg` | `rgba(22,27,34,.7)` | `rgba(255,255,255,.85)` | Main content card, graph mini-box, backlinks box |
| `--border-color` | `rgba(48,54,61,.8)` | `rgba(31,35,40,.15)` | All hairline borders sitewide |
| `--text-primary` | `#c9d1d9` | `#1f2328` | Body text color |
| `--text-muted` | `#8b949e` | `#59636e` | Sidebar labels, outline links, graph edge/label color |
| `--heading-color` | `#ffffff` | `#1f2328` | `h1`/`h2` heading color |
| `--accent-blue` | `#58a6ff` | `#0969da` | Site-title gradient, active outline item, graph's "current note" node |
| `--accent-purple` | `#8ab4f8` | `#8250df` | Vault badge accent, site-title gradient |
| `--wiki-link` / `--wiki-link-hover` | `#a371f7` / `#b893f9` | `#8250df` / `#6639ba` | Wikilink color, graph node color (non-current) |
| `--link-hover-color` | `#79c0ff` | `#0550ae` | Plain markdown link hover |
| `--code-bg` | `#161b22` | `#f6f8fa` | Inline code / search-input background |
| `--code-text-color` | `#e6edf3` | `#24292f` | Code block text color |

Which block actually applies depends on theme state: `:root` is the unconditional dark baseline,
`@media (prefers-color-scheme: light)` layers light on top when the OS prefers it (unless a
visitor's own toggle already picked dark), `:root[data-theme="light"]`/`[data-theme="dark"]`
override both when a `defaultTheme` site setting or a visitor's toggle click is in play. If you
only want one look regardless of theme, redefine the variable in all of `:root`,
`:root[data-theme="light"]`, and `:root[data-theme="dark"]` in your `publish.css`.

## Structural classes and IDs

Grouped by where they live on the page.

**Left sidebar** (`.pumice-nav-sidebar`, always present — see `43_사이트_제목_왼쪽_사이드바_
이동.md` / `44_그래프_TOC_왼쪽_사이드바_병합.md` in the pumice repo for how this layout was
chosen):
- `.site-title` — logo + site name link, first child
- `.pumice-icon-button` — the theme-toggle button (`#pumice-theme-toggle`)
- `.pumice-search` / `input#pumice-search-input` — the search box
- `.pumice-search-results` (`#pumice-search-results`) — the results dropdown; `.search-result-title`/`.search-result-snippet`/`.search-status` inside it
- `.pumice-search-clearance` (`#pumice-search-clearance`) — invisible spacer, sized in JS to the dropdown's live height so it never overlaps the outline below (see `48_검색_여백을_드롭다운_실제_높이에_맞춤.md`) — don't give this a `background`, it's not meant to be visible
- `#pumice-outline` (`.pumice-outline-sidebar`) — the table of contents; `.pumice-outline-children` for nested headings, `.is-active` for the scroll-spy-highlighted entry
- plain `ul`/`li`/`.pumice-nav-folder-title`/`.nav-folder` — the folder tree, when navigation is enabled

**Right column** (`.pumice-right-column`, only present when the note has a graph — see
`45_그래프_다시_오른쪽으로_이동_및_드래그_패닝.md`):
- `.pumice-graph-mini` — the always-visible mini graph box; `.pumice-graph-mini-header`/`.pumice-graph-mini-icons`/`.pumice-graph-icon-button` for its header row, `#pumice-graph-mini-canvas` for the PixiJS canvas
- `.pumice-graph-fullpage` (`#pumice-graph-fullpage`, `[hidden]` when collapsed) — the expanded, viewport-covering graph; `.pumice-graph-fullpage-header`, `#pumice-graph-fullpage-canvas`

**Main column** (`.pumice-main-column`):
- `header` / `.vault-badge` — top bar, now just the vault badge (title moved into the sidebar)
- `.container.markdown-body` — the note content itself; see the `.markdown-body *` rules for headings/code/tables/blockquotes/wikilinks
- `#pumice-backlinks` (`.pumice-sidebar`) — the backlinks box under a note's content
- `.pumice-sliding-track` / `.pumice-sliding-pane` — sliding-window mode's horizontal pane trail

**Layout wrapper**: `.layout` (`.wide`/`.has-nav`/`.has-graph` modifiers control max-width and
whether the flex 2/3-column split is active at all).

## Example

A fuller, verified-working example lives at
[`docs/examples/publish.css.example`](examples/publish.css.example) — actually applied against a
live render of this page and screenshotted (recolored accents/graph, serif headings, wider
sidebar, solid vault badge, circular theme toggle), not just written from the reference above.

```css
/* Wider left sidebar, and a house accent color that also recolors the graph. */
:root, :root[data-theme="light"], :root[data-theme="dark"] {
  --accent-blue: #ff6b6b;
  --wiki-link: #ff6b6b;
}
.pumice-nav-sidebar {
  flex-basis: 280px;
}
.pumice-graph-mini {
  border-radius: 4px; /* square corners instead of the default 10px */
}
```
