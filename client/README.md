# client/

TypeScript source for pumice-server's client-side static JS (see
`40_publish_render_TypeScript_전환.md`, pumice repo). Compiled by `tsc` into
`src/pumice_server/static/` -- `publish_render.ts` becomes `static/publish_render.js`, the exact
path it's always been served from (Pyramid's static view, and the Jinja2 template's
`<script src="/static/publish_render.js">`, are both unaware this changed).

Only `publish_render.js` moved here. `static/dashboard.js`, `static/login.js`, and
`static/graph_worker.js` are still plain untyped JS -- not part of this conversion, no reason
they can't move here too later if there's a reason to.

## Building

```sh
npm install   # once
npm run build # tsc -> src/pumice_server/static/publish_render.js
npm run watch # tsc --watch, for iterating
npm test      # tsc, then node --test tests/publish_render.test.cjs
```

The compiled `static/publish_render.js` **is committed** (same as before this conversion) --
it's the actual file the server serves at runtime, and this project's deploy story doesn't run a
build step, so there's nothing else that would produce it. Rebuild and commit the output whenever
`publish_render.ts` changes.

## Why TypeScript here but not the rest of static/

No principled reason this file specifically had to become TypeScript over any other static JS
file -- this project's earlier "vanilla JS, no build step" framing (still true for
`dashboard.js`/`login.js`/`graph_worker.js`/the vendored `pixi.min.js`) was this codebase's own
established style, not a hard external constraint. `publish_render.js` is by a wide margin the
largest and most logic-heavy file in `static/` (outline/search/graph/markdown rendering, a
WASM-accelerated module, three different JS execution environments), so real type-checking value
there specifically justified setting up a build step now.

`tsconfig.json` (repo root) targets `module: "none"` deliberately -- no ES module import/export
in `publish_render.ts`, so `tsc` emits a plain script, not a module wrapper. That's what lets the
compiled output keep working identically in all three environments it always ran in: a plain
`<script>` in the browser, `importScripts()` from `static/graph_worker.js` (a Web Worker), and a
bare `require()` from `tests/publish_render.test.cjs` (Node's CommonJS).
