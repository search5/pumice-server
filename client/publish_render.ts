/*
 * Client-side note renderer for pumice-server's Publish pages -- see
 * 27_실제_퍼블리시_사이트_아키텍처_조사.md / 28_실제_아키텍처_전환_Phase1_노트본문.md /
 * 37_실제_아키텍처_전환_TOC_재설계.md / 38_실제_아키텍처_전환_검색_그래프_실제화.md /
 * 40_publish_render_TypeScript_전환.md (pumice repo).
 *
 * Real Obsidian Publish serves raw markdown + a whole-vault metadata cache and renders client-
 * side. This file is pumice-server's own from-scratch reimplementation of that idea -- verified
 * two ways as this project evolved: live behavioral observation (curl, then later Claude in
 * Chrome browser automation against real published sites), and, for the pieces observation alone
 * couldn't settle (outline scroll-spy, search, graph), reading the actual
 * publish.obsidian.md/app.js bundle directly -- same client-only-reversing methodology this
 * project already used against the desktop app's obsidian.asar (reference behavior, never copy
 * code, reimplement fresh). It targets the same markdown subset python-markdown's
 * `extra`/`codehilite`/`toc` extensions already produce server-side (see web.py's
 * _render_publish_page), not full CommonMark compliance.
 *
 * TypeScript, compiled with `tsc` (see package.json's "build" script, tsconfig.json) into
 * static/publish_render.js at the same path it's always been served from -- no ES module
 * import/export in this file (module: "none" in tsconfig) so the compiled output stays a single
 * plain script, runtime-identical to before: loadable via <script defer src="/static/
 * publish_render.js"> in the browser, via `importScripts()` in static/graph_worker.js, and via
 * plain `require()` in Node's `node:test` (see tests/publish_render.test.cjs, which requires the
 * compiled JS in src/pumice_server/static/ -- run `npm run build` first, or `npm test`, which
 * does both). static/pixi.min.js (graph rendering) stays a vendored, unmodified, MIT-licensed
 * plain script (the "pixi.js-legacy" build) -- not an npm dependency of this file, loaded via its
 * own <script> tag exactly the way real Obsidian Publish's own page loads it, so it's typed here
 * via a small ambient `declare namespace PIXI` covering only what's actually used, not a real
 * @types package.
 */

// ─── Ambient/global surface this file straddles three JS environments through (browser main
// thread, a Web Worker via importScripts, and Node's CommonJS test runner) -- see the module
// export guard at the very bottom. `module`/`self.PumicePublishRenderer` are typed loosely
// (`any`) deliberately: this is a well-established interop-shim pattern (the same one UMD
// bundles use), not a gap in the rest of this file's typing. ────────────────────────────────────

declare var module: { exports: unknown } | undefined;

interface Window {
  pumicePreload?: PumicePreload;
  PumicePublishRenderer?: unknown;
}

declare namespace PIXI {
  interface ApplicationOptions {
    width: number;
    height: number;
    backgroundAlpha?: number;
    antialias?: boolean;
    resolution?: number;
    autoDensity?: boolean;
    forceCanvas?: boolean;
  }
  interface Stage {
    addChild(child: Graphics | Text): void;
    x: number;
    y: number;
  }
  class Application {
    constructor(options: ApplicationOptions);
    view: HTMLCanvasElement;
    stage: Stage;
  }
  class Graphics {
    lineStyle(width: number, color: number, alpha?: number): Graphics;
    moveTo(x: number, y: number): Graphics;
    lineTo(x: number, y: number): Graphics;
    beginFill(color: number): Graphics;
    drawCircle(x: number, y: number, radius: number): Graphics;
    endFill(): Graphics;
    x: number;
    y: number;
    eventMode: string;
    cursor: string;
    on(event: string, handler: () => void): void;
  }
  interface TextStyle {
    fontFamily?: string;
    fontSize?: number;
    fill?: number;
  }
  class Text {
    constructor(text: string, style?: TextStyle);
    x: number;
    y: number;
  }
  namespace utils {
    function isWebGLSupported(): boolean;
  }
}

// ─── Data shapes -- these mirror metadata_cache.py's extract_metadata_cache_entry (headings/
// links/searchText) and web.py's _search_note_bodies (ServerSearchResult) on the server side. ──

interface Heading {
  heading: string;
  level: number;
}

interface WikiLinkRef {
  link: string;
}

interface CacheEntry {
  headings?: Heading[];
  links?: WikiLinkRef[];
  searchText?: string;
  permalink?: string;
  frontmatterPos?: number[];
}

type PublishCache = Record<string, CacheEntry | null | undefined>;

interface OutlineNode {
  level: number;
  heading: string;
  slug: string;
  children: OutlineNode[];
}

interface HeadingTop {
  slug: string;
  top: number;
}

interface BacklinkResult {
  path: string;
  href: string;
}

interface SearchIndexEntry {
  path: string;
  title: string;
  href: string;
}

interface SearchResult {
  path: string;
  title: string;
  href: string;
  snippet: string;
}

interface ServerSearchResult {
  path: string;
  snippet: string;
}

interface GraphNode {
  path: string;
  title: string;
}

interface GraphEdge {
  source: string;
  target: string;
}

interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

interface Position {
  x: number;
  y: number;
}

type Positions = Record<string, Position>;

interface LayoutGraphOptions {
  width?: number;
  height?: number;
  iterations?: number;
}

interface ManyBodyForces {
  fx: number[];
  fy: number[];
}

interface GraphForcesWasmExports {
  memory: WebAssembly.Memory;
  alloc_f64(n: number): number;
  dealloc_f64(ptr: number, n: number): void;
  many_body_forces(
    xsPtr: number, ysPtr: number, n: number,
    theta: number, k: number,
    outFxPtr: number, outFyPtr: number,
  ): void;
}

interface ListItem {
  html: string;
  children: ListTree;
}

interface ListTree {
  ordered: boolean | null;
  items: ListItem[];
}

interface PumicePreload {
  url_prefix: string;
  path: string | null;
  sliding_window_mode?: boolean;
}

(function () {
  "use strict";

  function escapeHtml(text: string): string {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(text: string): string {
    return escapeHtml(text).replace(/"/g, "&quot;");
  }

  // Mirrors urllib.parse.quote(x, safe='/') closely enough for this renderer's purposes: encode
  // every path segment, but keep '/' as a literal separator.
  function encodePath(path: string): string {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  /** Strips a note's raw markdown down to just its body, using the same frontmatterPos this
   * note's own cache entry carries (see metadata_cache.py) -- avoids re-detecting the `---`
   * block client-side when the server already computed exactly where it ends. */
  function stripFrontmatter(rawMd: string, cacheEntry: CacheEntry | null | undefined): string {
    if (!cacheEntry || !cacheEntry.frontmatterPos) return rawMd;
    var endOffset = cacheEntry.frontmatterPos[5];
    var rest = rawMd.slice(endOffset);
    return rest.replace(/^\n/, "");
  }

  /** Resolves a `[[target|label]]`-style wikilink target to an href, the same way
   * parse_wikilinks()/`_build_path_to_permalink` do server-side (see web.py, 19_permalink_지원.md)
   * -- prefer the target file's permalink (merged into the cache by the `_cache.json` endpoint),
   * falling back to the literal target path (with a trailing .md stripped) under url_prefix. */
  function resolveWikilinkHref(target: string, cache: PublishCache, urlPrefix: string): string {
    var entry = cache && Object.prototype.hasOwnProperty.call(cache, target) ? cache[target] : null;
    if (entry && entry.permalink) {
      return urlPrefix + encodePath(entry.permalink).replace(/^\/+/, "");
    }
    var urlTarget = target;
    if (/\.md$/i.test(target)) {
      urlTarget = target.slice(0, -3);
    }
    return urlPrefix + encodePath(urlTarget);
  }

  /** Finds the actual cache key a wikilink `target` resolves to (if any) -- unlike
   * resolveWikilinkHref, which always returns *some* href even for an unresolved target (falling
   * back to a best-guess literal path), this returns null when there's no real cache entry to
   * point at. Used to tag rendered wikilinks with the note's real vault path (see
   * renderInline's `data-note-path`) for sliding-window mode (Phase 5,
   * 28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up) to fetch/render without having to
   * reverse-engineer a path back out of a pretty permalink URL. */
  function resolveNoteTargetPath(target: string, cache: PublishCache | null | undefined): string | null {
    if (!cache) return null;
    var keys = Object.keys(cache);
    for (var i = 0; i < keys.length; i++) {
      if (cache[keys[i]] && linkTargetMatchesPath(target, keys[i])) return keys[i];
    }
    return null;
  }

  /** Turns heading text into an anchor-safe id, deduplicated the same way python-markdown's
   * `toc` extension (used server-side for plugin-enabled vaults) already does: a repeated
   * heading text gets `-1`, `-2`, ... appended. `renderMarkdown` and `buildOutline` each create
   * their own slugger over the same ordered heading-text sequence (the rendered body and the
   * cache's `headings` array are built from the same frontmatter-stripped, fence-skipped scan --
   * see metadata_cache.py), so their slugs agree without needing to share state directly. */
  function slugify(text: string): string {
    var slug = text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return slug || "section";
  }

  function makeSlugger(): (text: string) => string {
    var counts: Record<string, number> = {};
    return function (text: string): string {
      var base = slugify(text);
      var count = counts[base] || 0;
      counts[base] = count + 1;
      return count === 0 ? base : base + "-" + count;
    };
  }

  /** Note outline (Phase 2 of 28_실제_아키텍처_전환_Phase1_노트본문.md; nesting/positioning
   * redesigned in 37_실제_아키텍처_전환_TOC_재설계.md after live-browser verification against real
   * Obsidian Publish sites) -- the current note's cache entry's `headings`, slugged to match the
   * ids renderMarkdown puts on the actual rendered `<hN>` elements, built into a nested tree the
   * same way real Obsidian's own outline does: each heading becomes a child of the nearest
   * preceding heading with a strictly lower level (so e.g. an H1 followed by H5s nests all of them
   * under that H1, skipped levels and all -- confirmed live via `window.app.outline.treeView` on
   * lab.marconoris.com/now, which does exactly this for a real H1→H5 note). No level is filtered
   * out (confirmed live: a note's own body H1 shows up in the outline same as any other level). */
  function buildOutline(cacheEntry: CacheEntry | null | undefined): OutlineNode[] {
    if (!cacheEntry || !cacheEntry.headings) return [];
    var slugger = makeSlugger();
    var root: OutlineNode[] = [];
    var stack: OutlineNode[] = [];
    cacheEntry.headings.forEach(function (h) {
      var node: OutlineNode = { level: h.level, heading: h.heading, slug: slugger(h.heading), children: [] };
      while (stack.length && stack[stack.length - 1].level >= node.level) stack.pop();
      (stack.length ? stack[stack.length - 1].children : root).push(node);
      stack.push(node);
    });
    return root;
  }

  /** Renders an outline tree (buildOutline's output) as nested `<ul>`s -- own class names
   * (`pumice-outline-*`), not real Obsidian's (`tree-item`/`tree-item-children`/...): this is a
   * from-scratch reimplementation informed by observed behavior, not copied markup. */
  function renderOutlineHtml(tree: OutlineNode[]): string {
    if (!tree.length) return "";
    return "<ul>" + tree.map(renderOutlineItem).join("") + "</ul>";
  }

  function renderOutlineItem(node: OutlineNode): string {
    var childrenHtml = node.children.length
      ? '<ul class="pumice-outline-children">' + node.children.map(renderOutlineItem).join("") + "</ul>"
      : "";
    return '<li><a href="#' + escapeAttr(node.slug) + '">' + escapeHtml(node.heading) + "</a>" + childrenHtml + "</li>";
  }

  /** Outline scroll-spy (37_실제_아키텍처_전환_TOC_재설계.md's follow-up correction) -- real
   * Obsidian Publish's own `onScroll` handler calls `outline.highlightLine(currentLine)`, which
   * walks the note's headings in document order and picks the *last* one whose source position
   * has already been scrolled past (confirmed live by reading app.js: this DOES exist and DOES
   * update a `mod-active` class on scroll -- an earlier check in this same investigation wrongly
   * concluded it didn't, because it inspected the wrong DOM node). Real Obsidian's version keys
   * off a line number (its renderer has a CodeMirror-style line coordinate system); pumice's
   * renderer doesn't render into lines, so this keys off each heading's actual viewport position
   * instead -- same "last one already passed" rule, adapted to pixels rather than lines.
   * `headingTops` is `[{slug, top}]` in document order, `top` being that heading's
   * getBoundingClientRect().top (viewport-relative) at the moment of the scroll event. */
  function findActiveHeadingSlug(headingTops: HeadingTop[]): string | null {
    var active: string | null = null;
    for (var i = 0; i < headingTops.length; i++) {
      if (headingTops[i].top <= 0) active = headingTops[i].slug;
    }
    return active;
  }

  /** Wires the outline sidebar's scroll-spy: on every scroll (rAF-throttled, matching the
   * pattern real Obsidian's own debounced onScroll uses for the same purpose), recomputes which
   * heading is active from `contentEl`'s actual rendered `<hN id>` elements and toggles
   * `.is-active` on the matching outline link. Also runs once immediately so the correct item is
   * already highlighted before any scrolling happens. */
  function wireOutlineScrollSpy(contentEl: HTMLElement, outlineEl: HTMLElement): void {
    var headingEls = Array.prototype.slice.call(
      contentEl.querySelectorAll("h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]"),
    ) as HTMLElement[];
    if (!headingEls.length) return;

    var current: string | null = null;
    var ticking = false;

    function update(): void {
      ticking = false;
      var headingTops: HeadingTop[] = headingEls.map(function (h) {
        return { slug: h.id, top: h.getBoundingClientRect().top };
      });
      var active = findActiveHeadingSlug(headingTops);
      if (active === current) return;
      if (current) {
        var prevLink = outlineEl.querySelector('a[href="#' + CSS.escape(current) + '"]');
        if (prevLink) prevLink.classList.remove("is-active");
      }
      current = active;
      if (current) {
        var nextLink = outlineEl.querySelector('a[href="#' + CSS.escape(current) + '"]');
        if (nextLink) nextLink.classList.add("is-active");
      }
    }

    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(update);
    });
    update();
  }

  /** Mirrors the target-matching parse_wikilinks()/_build_path_to_permalink do server-side (see
   * web.py, 19_permalink_지원.md): a wikilink target matches a stored path either literally, or
   * with/without a trailing .md. */
  function linkTargetMatchesPath(target: string, storedPath: string): boolean {
    if (target === storedPath) return true;
    var pathIsMd = /\.md$/i.test(storedPath);
    if (pathIsMd && target === storedPath.slice(0, -3)) return true;
    if (!pathIsMd && target + ".md" === storedPath) return true;
    return false;
  }

  /** Backlinks (Phase 2): every other file in this vault whose own `links[]` targets the current
   * note, resolved to that file's real href the same way a wikilink to it would be. Computed
   * from the whole-vault `_cache.json` the page already fetched -- no extra server endpoint
   * needed, real Obsidian's own /cache/{uid} export is exactly this shape too. */
  function buildBacklinks(cache: PublishCache, currentPath: string, urlPrefix: string): BacklinkResult[] {
    var results: BacklinkResult[] = [];
    Object.keys(cache).forEach(function (otherPath) {
      if (otherPath === currentPath) return;
      var entry = cache[otherPath];
      if (!entry || !entry.links) return;
      var linksHere = entry.links.some(function (link) {
        return linkTargetMatchesPath(link.link, currentPath);
      });
      if (linksHere) {
        results.push({ path: otherPath, href: resolveWikilinkHref(otherPath, cache, urlPrefix) });
      }
    });
    return results;
  }

  var SEARCH_RESULT_LIMIT = 20;
  var SEARCH_DEBOUNCE_MS = 300;

  function titleFromPath(path: string): string {
    var base = path.indexOf("/") === -1 ? path : path.slice(path.lastIndexOf("/") + 1);
    return /\.md$/i.test(base) ? base.slice(0, -3) : base;
  }

  /** Search index (Phase 3, 28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up; redesigned in
   * 38_실제_아키텍처_전환_검색_그래프_실제화.md after reading real Obsidian's app.js) -- one entry
   * per markdown file in the already-fetched `_cache.json`, title/filename only. Real Obsidian's
   * own client never receives note bodies at all (confirmed live: `_cache.json`-equivalent
   * entries carry only structural metadata -- headings/links/listItems/blocks -- no text; a
   * `POST {host}/search` call is what actually finds body-text matches, per app.js's
   * `getCachedResults()`/`runFullSearch()`). `searchNotes` below mirrors `getCachedResults()`:
   * instant, synchronous, title-only. */
  function buildSearchIndex(cache: PublishCache, urlPrefix: string): SearchIndexEntry[] {
    var index: SearchIndexEntry[] = [];
    Object.keys(cache).forEach(function (path) {
      var entry = cache[path];
      if (!entry || !/\.md$/i.test(path)) return;
      index.push({
        path: path,
        title: titleFromPath(path),
        href: resolveWikilinkHref(path, cache, urlPrefix),
      });
    });
    return index;
  }

  /** Case-insensitive substring match against title/filename only, capped at
   * SEARCH_RESULT_LIMIT results (own reasonable default -- a dropdown, not a full results
   * page) in index order. Mirrors real Obsidian's own instant `getCachedResults()` -- see
   * buildSearchIndex's doc comment. */
  function searchNotes(index: SearchIndexEntry[], query: string): SearchResult[] {
    var trimmed = (query || "").trim();
    if (!trimmed) return [];
    var lower = trimmed.toLowerCase();
    var results: SearchResult[] = [];
    for (var i = 0; i < index.length && results.length < SEARCH_RESULT_LIMIT; i++) {
      var item = index[i];
      if (item.title.toLowerCase().indexOf(lower) === -1) continue;
      results.push({ path: item.path, title: item.title, href: item.href, snippet: "" });
    }
    return results;
  }

  /** Merges the instant title-only results with the (slower, debounced) server-side body-text
   * results, mirroring real Obsidian's own merge in `updateSearch()`: title matches keep their
   * place, then any NEW path from the body-search results (deduplicated) is appended with its
   * server-built snippet, capped at SEARCH_RESULT_LIMIT overall. */
  function mergeSearchResults(
    titleResults: SearchResult[], serverResults: ServerSearchResult[], cache: PublishCache, urlPrefix: string,
  ): SearchResult[] {
    var seen: Record<string, boolean> = {};
    titleResults.forEach(function (r) { seen[r.path] = true; });
    var merged = titleResults.slice();
    serverResults.forEach(function (r) {
      if (seen[r.path]) return;
      seen[r.path] = true;
      merged.push({
        path: r.path,
        title: titleFromPath(r.path),
        href: resolveWikilinkHref(r.path, cache, urlPrefix),
        snippet: r.snippet || "",
      });
    });
    return merged.slice(0, SEARCH_RESULT_LIMIT);
  }

  /** POSTs to this vault's own `_search` reserved path (web.py's `_search_note_bodies`) --
   * pumice's own server-side counterpart to real Obsidian's `POST {host}/search`. Unlike real
   * Obsidian (which fetches each shown result's raw content separately just to build a snippet,
   * confirmed live via network capture), pumice's server already has the matched text in hand
   * while searching, so it returns `{path, snippet}` directly in one round trip -- an intentional
   * improvement, not a missed detail (same category of deliberate deviation as keeping slug
   * anchors over real Obsidian's raw-heading-text URL hash: same visitor-visible behavior, fewer
   * requests). No retry-with-backoff (real Obsidian's client does up to 3, confirmed via app.js)
   * -- this is a single self-hosted server, not a globally-distributed CDN-backed service, so
   * that resilience isn't worth the complexity here; a failed search just resolves to []. */
  function runServerSearch(query: string, urlPrefix: string): Promise<ServerSearchResult[]> {
    return fetch(urlPrefix + "_search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query }),
    }).then(function (r) {
      if (!r.ok) return [];
      return r.json();
    }).then(function (res) {
      return (res && res.results) || [];
    }).catch(function () {
      return [];
    });
  }

  /** Graph view (Phase 4, 28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up; graph *rendering*
   * redesigned in 38_실제_아키텍처_전환_검색_그래프_실제화.md after reading real Obsidian's app.js)
   * -- built from the same whole-vault `_cache.json` already fetched (this part was never
   * confirmed to be real Obsidian's own wire format either, but it doesn't need to be: the graph
   * is derived from data the page already has, real Obsidian's own client does the same). Scope:
   * only markdown files that are actual nodes in this vault's cache, only links that resolve to
   * another such file (an unresolved/external link target doesn't get a node) -- matches
   * buildBacklinks' own resolved-links-only scope. A link that appears more than once, or in both
   * directions between the same two files, collapses into a single undirected edge (the graph is
   * for showing connectivity, not link direction/count). */
  function buildGraph(cache: PublishCache): Graph {
    var nodes: GraphNode[] = [];
    Object.keys(cache).forEach(function (path) {
      var entry = cache[path];
      if (!entry || !/\.md$/i.test(path)) return;
      nodes.push({ path: path, title: titleFromPath(path) });
    });

    var seenEdgeKeys: Record<string, boolean> = {};
    var edges: GraphEdge[] = [];
    nodes.forEach(function (node) {
      var entry = cache[node.path];
      (entry!.links || []).forEach(function (link) {
        var targetPath: string | null = null;
        for (var i = 0; i < nodes.length; i++) {
          if (linkTargetMatchesPath(link.link, nodes[i].path)) { targetPath = nodes[i].path; break; }
        }
        if (!targetPath || targetPath === node.path) return;
        var key = [node.path, targetPath].sort().join("|");
        if (seenEdgeKeys[key]) return;
        seenEdgeKeys[key] = true;
        edges.push({ source: node.path, target: targetPath });
      });
    });

    return { nodes: nodes, edges: edges };
  }

  /** Local graph (40_그래프_뷰_실제_UI_형태.md, pumice repo's follow-up to 38_...md) -- real
   * Obsidian Publish's own graph defaults to a "local graph" (just the current note and its
   * direct neighbors), with a separate toggle to switch to the full vault ("global") graph --
   * confirmed live by reading `window.app.graph.global` (a boolean) on lab.marconoris.com/now and
   * watching the rendered node count change when toggling it. The exact neighbor depth real
   * Obsidian's Publish local graph uses wasn't confirmable (no `depth` property was exposed on
   * the live `graph` object to read) -- depth 1 (the current note plus anything directly linked
   * to or from it, including edges between those neighbors themselves) is this project's own
   * reasonable default for "local", not a confirmed exact match. */
  function buildLocalGraph(graph: Graph, currentPath: string | null): Graph {
    if (!currentPath) return graph;
    var included: Record<string, boolean> = {};
    included[currentPath] = true;
    graph.edges.forEach(function (edge) {
      if (edge.source === currentPath) included[edge.target] = true;
      if (edge.target === currentPath) included[edge.source] = true;
    });
    var nodes = graph.nodes.filter(function (n) { return included[n.path]; });
    var edges = graph.edges.filter(function (e) { return included[e.source] && included[e.target]; });
    return { nodes: nodes, edges: edges };
  }

  var GRAPH_DEFAULT_ITERATIONS = 200;

  /** WASM-accelerated many-body repulsion (39_그래프_뷰_WASM_Barnes-Hut.md, pumice repo's
   * follow-up to 38_...md) -- compiled from wasm/graph_forces.rs (own from-scratch Barnes-Hut
   * quadtree implementation, see that file's own header comment; never read real Obsidian's
   * actual compiled WASM bytes, only confirmed via sim.js's surrounding JS that a WASM module
   * with this shape exists). Real Obsidian's own graph physics runs as compiled WASM too
   * (confirmed by reading sim.js directly) -- for the same reason this project measured
   * directly: the plain O(n^2) JS loop below takes ~1.1s at 500 nodes and ~17s at 2000 (200
   * iterations each), which is why it's now only the fallback, not the primary path.
   *
   * Base64-embedded rather than fetched separately so it can be instantiated synchronously (no
   * `await`) -- same reason real Obsidian's own bundle embeds its WASM inline instead of a
   * second network request -- which keeps layoutGraph a plain synchronous function callable
   * identically from the browser, the Worker (static/graph_worker.js), and `node --test`. */
  var GRAPH_FORCES_WASM_B64 =
    "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gAAAC0cBAX8jgICAgABBIGsiAySAgICAACADIAE2AhAgAyAANgIMIANBATsBHCADIAI2AhggAyADQQxqNgIUIANBFGoQm4CAgAAAC0kAAkAgAkGAgMQARg0AIAAgAiABKAIQEYCAgIAAgICAgABFDQBBAQ8LAkAgAw0AQQAPCyAAIANBACABKAIMEYGAgIAAgICAgAALOAIBfwF+I4CAgIAAQRBrIgEkgICAgAAgACkCACECIAEgADYCDCABIAI3AgQgAUEEahChgICAAAALhgcCC38BfiOAgICAAEEQayICJICAgIAAQQohAyAAKAIAIgQhBQJAAkAgBEHoB0kNAEEKIQYgBCEAA0AgBkF8aiIDQQpPDQIgAkEGaiAGaiIGQXxqIAAgAEGQzgBuIgVBkM4AbGsiB0H//wNxQeQAbiIIQQF0LwDkh8CAADsAACAGQX5qIAcgCEHkAGxrQf//A3FBAXQvAOSHwIAAOwAAIABB/6ziBEshByADIQYgBSEAIAcNAAsLAkACQCAFQQlLDQAgBSEADAELIAJBBmogA0F+aiIDaiAFIAVB//8DcUHkAG4iAEHkAGxrQf//A3FBAXQvAOSHwIAAOwAACwJAAkAgBEUNACAARQ0BCyACQQZqIANBf2oiA2ogAEEBdC0A5YfAgAA6AAALQStBgIDEACABKAIIIgZBgICAAXEiABshBCAGQYCAgARxQRd2IQkgAkEGaiADaiEKAkACQCAAQRV2QQogA2siC2oiByABLwEMIghPDQACQAJAAkAgBkGAgIAIcQ0AIAggB2shDEEAIQBBACEIAkACQAJAIAZBHXZBA3EOBAIAAQACCyAMIQgMAQsgDEH+/wNxQQF2IQgLIAZB////AHEhByABKAIEIQMgASgCACEFA0AgAEH//wNxIAhB//8DcU8NAkEBIQYgAEEBaiEAIAUgByADKAIQEYCAgIAAgICAgABFDQAMBQsLIAEgASkCCCINp0GAgID/eXFBsICAgAJyNgIIQQEhBiABKAIAIgMgASgCBCIFIAQgCRCagICAAA0DQQAhACAIIAdrQf//A3EhBwNAIABB//8DcSAHTw0CQQEhBiAAQQFqIQAgA0EwIAUoAhARgICAgACAgICAAEUNAAwECwtBASEGIAUgAyAEIAkQmoCAgAANAiAFIAogCyADKAIMEYGAgIAAgICA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";

  var graphForcesWasm: GraphForcesWasmExports | null = (function (): GraphForcesWasmExports | null {
    try {
      var binary = atob(GRAPH_FORCES_WASM_B64);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      var mod = new WebAssembly.Module(bytes);
      return new WebAssembly.Instance(mod, {}).exports as unknown as GraphForcesWasmExports;
    } catch (e) {
      return null; // WASM unavailable -- layoutGraph falls back to the plain-JS loop below.
    }
  })();

  // Same default d3-force itself uses for forceManyBody's own Barnes-Hut approximation --
  // smaller is more accurate/slower, larger is faster/coarser.
  var GRAPH_BARNES_HUT_THETA = 0.9;

  /** Computes every node's net repulsive displacement via graphForcesWasm's Barnes-Hut quadtree
   * (~O(n log n)) instead of the plain-JS O(n^2) pairwise loop -- xs/ys are parallel arrays in
   * the same order as the nodes being laid out. Returns {fx: number[], fy: number[]}. */
  function manyBodyForcesWasm(xs: number[], ys: number[], k: number): ManyBodyForces {
    var wasm = graphForcesWasm!;
    var n = xs.length;
    var xsPtr = wasm.alloc_f64(n), ysPtr = wasm.alloc_f64(n);
    var fxPtr = wasm.alloc_f64(n), fyPtr = wasm.alloc_f64(n);
    try {
      var mem = new Float64Array(wasm.memory.buffer);
      mem.set(xs, xsPtr / 8);
      mem.set(ys, ysPtr / 8);
      wasm.many_body_forces(xsPtr, ysPtr, n, GRAPH_BARNES_HUT_THETA, k, fxPtr, fyPtr);
      // many_body_forces allocates internally (the quadtree) and may have grown WASM memory,
      // detaching the ArrayBuffer `mem` was viewing -- re-view fresh before reading results.
      mem = new Float64Array(wasm.memory.buffer);
      return {
        fx: Array.prototype.slice.call(mem, fxPtr / 8, fxPtr / 8 + n),
        fy: Array.prototype.slice.call(mem, fyPtr / 8, fyPtr / 8 + n),
      };
    } finally {
      wasm.dealloc_f64(xsPtr, n);
      wasm.dealloc_f64(ysPtr, n);
      wasm.dealloc_f64(fxPtr, n);
      wasm.dealloc_f64(fyPtr, n);
    }
  }

  /** Deterministic force-directed layout (Fruchterman-Reingold) -- no Math.random(), so results
   * are reproducible and testable: nodes start on a circle (ordered by index) and every
   * iteration pushes every node pair apart while pulling edge-connected pairs together, with a
   * cooling "temperature" that shrinks the max per-iteration move so the layout settles instead
   * of oscillating forever. Returns {[path]: {x, y}}.
   *
   * Run inside static/graph_worker.js (a real Web Worker), not on the main thread -- confirmed
   * live by reading real Obsidian's app.js: it runs its own equivalent physics in a dedicated
   * `new Worker("/sim.js", {name: "Graph Worker"})` rather than blocking the UI thread. This
   * function itself is unchanged/still synchronous -- wireGraph (below) is what moved the call
   * into a worker; layoutGraph stays plain, pure, and directly unit-testable either way. */
  function layoutGraph(graph: Graph, opts?: LayoutGraphOptions): Positions {
    opts = opts || {};
    var width = opts.width || 600;
    var height = opts.height || 600;
    var iterations = opts.iterations || GRAPH_DEFAULT_ITERATIONS;
    var nodes = graph.nodes;
    var n = nodes.length;
    var positions: Positions = {};
    if (n === 0) return positions;

    var cx = width / 2, cy = height / 2;
    var radius = Math.min(width, height) / 3;
    nodes.forEach(function (node, i) {
      var angle = (2 * Math.PI * i) / n;
      positions[node.path] = { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
    });
    if (n === 1) return positions;

    var k = Math.sqrt((width * height) / n);

    for (var iter = 0; iter < iterations; iter++) {
      var disp: Positions = {};
      nodes.forEach(function (node) { disp[node.path] = { x: 0, y: 0 }; });

      if (graphForcesWasm) {
        var xs = nodes.map(function (node) { return positions[node.path].x; });
        var ys = nodes.map(function (node) { return positions[node.path].y; });
        var forces = manyBodyForcesWasm(xs, ys, k);
        nodes.forEach(function (node, i) {
          disp[node.path].x += forces.fx[i];
          disp[node.path].y += forces.fy[i];
        });
      } else {
        for (var i = 0; i < n; i++) {
          for (var j = i + 1; j < n; j++) {
            var a = nodes[i].path, b = nodes[j].path;
            var dx = positions[a].x - positions[b].x;
            var dy = positions[a].y - positions[b].y;
            var dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
            var force = (k * k) / dist;
            var fx = (dx / dist) * force, fy = (dy / dist) * force;
            disp[a].x += fx; disp[a].y += fy;
            disp[b].x -= fx; disp[b].y -= fy;
          }
        }
      }

      graph.edges.forEach(function (edge) {
        var dx = positions[edge.source].x - positions[edge.target].x;
        var dy = positions[edge.source].y - positions[edge.target].y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        var force = (dist * dist) / k;
        var fx = (dx / dist) * force, fy = (dy / dist) * force;
        disp[edge.source].x -= fx; disp[edge.source].y -= fy;
        disp[edge.target].x += fx; disp[edge.target].y += fy;
      });

      var temperature = (width / 10) * (1 - iter / iterations);
      nodes.forEach(function (node) {
        var d = disp[node.path];
        var dlen = Math.sqrt(d.x * d.x + d.y * d.y) || 0.01;
        var limited = Math.min(dlen, temperature);
        var pos = positions[node.path];
        pos.x = Math.min(width!, Math.max(0, pos.x + (d.x / dlen) * limited));
        pos.y = Math.min(height!, Math.max(0, pos.y + (d.y / dlen) * limited));
      });
    }

    return positions;
  }

  /** Converts a CSS color value (hex `#rrggbb` or `rgb(a)(...)`, the only two forms this page's
   * own theme variables ever use -- see publish_page.jinja2's `:root` custom properties) into the
   * 0xRRGGBB integer PixiJS' Graphics fill/line color wants. Falls back to a neutral gray for
   * anything else (an unparsed/empty computed style value) rather than throwing, since a slightly
   * wrong graph color is harmless. Pure and unit-testable, unlike the PixiJS drawing calls that
   * consume it (renderGraphPixi below), which need a real canvas/WebGL context. */
  function cssColorToPixiHex(cssColor: string | null | undefined): number {
    var value = (cssColor || "").trim();
    var hexMatch = /^#([0-9a-f]{6})$/i.exec(value);
    if (hexMatch) return parseInt(hexMatch[1], 16);
    var rgbMatch = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(value);
    if (rgbMatch) {
      return (parseInt(rgbMatch[1], 10) << 16) + (parseInt(rgbMatch[2], 10) << 8) + parseInt(rgbMatch[3], 10);
    }
    return 0x888888;
  }

  /** Renders a laid-out graph via PixiJS/WebGL (static/pixi.min.js, loaded as a plain global
   * `PIXI` the same way real Obsidian Publish itself loads it -- confirmed live: its own page
   * loads a separate `<script src=".../lib/pixi.min.js">` before app.js, no bundler on their side
   * either -- pixi.min.js itself stays an unbuilt vendored script even though this file now has
   * its own TS build step, same reason: it's a third-party global, not this project's own code).
   * Replaces
   * the earlier plain-SVG renderer -- real Obsidian's graph is WebGL-rendered, confirmed by
   * reading app.js's PIXI.* usage. Theme-aware: colors are read from this page's own CSS custom
   * properties (via cssColorToPixiHex) rather than hardcoded, so the graph matches whichever
   * light/dark theme is active (33_실제_아키텍처_전환_테마_전환.md). Drag-to-pan: plain pointer
   * events on the canvas translate app.stage, independent of PIXI's own pointertap handling on
   * individual node circles below -- dragging and click-to-navigate coexist because PIXI's tap
   * only fires when the same target is hit-tested at both pointerdown and pointerup, which a
   * real drag away from a node naturally fails. Confirmed live afterward against
   * lab.marconoris.com/now (dragging from either empty canvas or directly on a node pans the
   * *entire* graph uniformly, not an individual node -- reversing the drag reverses the pan
   * exactly; scroll-wheel over the graph produces no meaningful zoom, page scroll passes
   * through instead; plain click still navigates) -- this implementation already matched
   * without changes, see 45_그래프_다시_오른쪽으로_이동_및_드래그_패닝.md's follow-up note.
   * Known scope cut still remaining: no zoom (real Obsidian doesn't appear to have it either,
   * per that same check). */
  function renderGraphPixi(
    containerEl: HTMLElement, graph: Graph, positions: Positions, cache: PublishCache, urlPrefix: string,
    currentPath: string | null, width?: number, height?: number,
  ): PIXI.Application {
    width = width || 600;
    height = height || 600;
    containerEl.innerHTML = "";

    var styles = getComputedStyle(document.documentElement);
    var edgeColor = cssColorToPixiHex(styles.getPropertyValue("--border-color"));
    var nodeColor = cssColorToPixiHex(styles.getPropertyValue("--wiki-link"));
    var currentColor = cssColorToPixiHex(styles.getPropertyValue("--accent-blue"));
    var textColor = cssColorToPixiHex(styles.getPropertyValue("--text-muted"));

    // PIXI.Application's own auto-detection doesn't reliably fall back to CanvasRenderer when
    // WebGL is unavailable even on the pixi.js-legacy build that bundles it (observed directly:
    // it throws "Unable to auto-detect a suitable renderer" instead of falling through) --
    // checking PIXI.utils.isWebGLSupported() up front and passing forceCanvas explicitly avoids
    // relying on that auto-detection at all.
    var app = new PIXI.Application({
      width: width, height: height, backgroundAlpha: 0, antialias: true,
      resolution: window.devicePixelRatio || 1, autoDensity: true,
      forceCanvas: !PIXI.utils.isWebGLSupported(),
    });
    containerEl.appendChild(app.view);

    var edgesGraphics = new PIXI.Graphics();
    graph.edges.forEach(function (edge) {
      var s = positions[edge.source], t = positions[edge.target];
      if (!s || !t) return;
      edgesGraphics.lineStyle(1, edgeColor, 1).moveTo(s.x, s.y).lineTo(t.x, t.y);
    });
    app.stage.addChild(edgesGraphics);

    graph.nodes.forEach(function (node) {
      var p = positions[node.path];
      if (!p) return;
      var isCurrent = node.path === currentPath;
      var r = isCurrent ? 7 : 5;

      var circle = new PIXI.Graphics();
      circle.beginFill(isCurrent ? currentColor : nodeColor).drawCircle(0, 0, r).endFill();
      circle.x = p.x;
      circle.y = p.y;
      circle.eventMode = "static";
      circle.cursor = "pointer";
      var href = resolveWikilinkHref(node.path, cache, urlPrefix);
      circle.on("pointertap", function () { window.location.href = href; });
      app.stage.addChild(circle);

      var label = new PIXI.Text(node.title, { fontFamily: "Inter, sans-serif", fontSize: 11, fill: textColor });
      label.x = p.x + r + 4;
      label.y = p.y - 6;
      app.stage.addChild(label);
    });

    enableGraphDragPan(app.view, app.stage);

    return app;
  }

  /** Lets the visitor drag inside a graph view to pan it -- plain pointer events on the canvas
   * element translating app.stage, independent of PixiJS' own per-node pointertap handling (see
   * the comment above renderGraphPixi for why the two don't conflict). Pan state lives only on
   * the stage's x/y for the lifetime of this one PIXI.Application; renderInto below always tears
   * down and recreates the app on toggle/re-render, so panning resets then -- consistent with
   * this project's existing "always redraw from scratch" choice for the graph rather than a new
   * exception carved out just for pan state. */
  function enableGraphDragPan(view: HTMLCanvasElement, stage: PIXI.Stage): void {
    var dragging = false;
    var lastClientX = 0;
    var lastClientY = 0;
    view.style.cursor = "grab";
    view.style.touchAction = "none";

    view.addEventListener("pointerdown", function (e: PointerEvent) {
      dragging = true;
      lastClientX = e.clientX;
      lastClientY = e.clientY;
      view.style.cursor = "grabbing";
      view.setPointerCapture(e.pointerId);
    });

    view.addEventListener("pointermove", function (e: PointerEvent) {
      if (!dragging) return;
      stage.x += e.clientX - lastClientX;
      stage.y += e.clientY - lastClientY;
      lastClientX = e.clientX;
      lastClientY = e.clientY;
    });

    function stopDragging(): void {
      dragging = false;
      view.style.cursor = "grab";
    }
    view.addEventListener("pointerup", stopDragging);
    view.addEventListener("pointercancel", stopDragging);
    view.addEventListener("pointerleave", stopDragging);
  }

  function renderInline(text: string, cache: PublishCache, urlPrefix: string): string {
    var escaped = escapeHtml(text);

    // 1. Inline code spans first, pulled out to placeholders so nothing inside them is touched
    // by the formatting rules below.
    var codeSpans: string[] = [];
    escaped = escaped.replace(/`([^`]+)`/g, function (_, code) {
      codeSpans.push(code);
      return " CODE" + (codeSpans.length - 1) + " ";
    });

    // 2. Image embeds: ![[image.png|width]] and standard ![alt](url).
    escaped = escaped.replace(/!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, function (_, target, alias) {
      target = target.trim();
      var src = resolveWikilinkHref(target, cache, urlPrefix);
      var widthAttr = "";
      if (alias && /^\d+$/.test(alias.trim())) {
        widthAttr = ' width="' + alias.trim() + '"';
      }
      return '<img src="' + escapeAttr(src) + '" alt="' + escapeAttr(target) + '"' + widthAttr + ">";
    });
    escaped = escaped.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function (_, alt, url) {
      return '<img src="' + escapeAttr(url) + '" alt="' + escapeAttr(alt) + '">';
    });

    // 3. Document wikilinks: [[target|label]] / [[target]].
    escaped = escaped.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, function (_, target, alias) {
      target = target.trim();
      var label = alias ? alias.trim() : target;
      var href = resolveWikilinkHref(target, cache, urlPrefix);
      var notePath = resolveNoteTargetPath(target, cache);
      var pathAttr = notePath ? ' data-note-path="' + escapeAttr(notePath) + '"' : "";
      return '<a href="' + escapeAttr(href) + '" class="wiki-link"' + pathAttr + '>' + escapeHtml(label) + "</a>";
    });

    // 4. Standard markdown links: [text](url).
    escaped = escaped.replace(/\[([^\]]*)\]\(([^)]+)\)/g, function (_, label, url) {
      return '<a href="' + escapeAttr(url) + '">' + label + "</a>";
    });

    // 5. Bold / italic (bold before italic so **x** doesn't get eaten by the italic rule first).
    escaped = escaped.replace(/(\*\*|__)(.+?)\1/g, "<strong>$2</strong>");
    escaped = escaped.replace(/(\*|_)(.+?)\1/g, "<em>$2</em>");

    // 6. Restore code spans.
    escaped = escaped.replace(/ CODE(\d+) /g, function (_, i) {
      return "<code>" + codeSpans[Number(i)] + "</code>";
    });

    return escaped;
  }

  var FENCE_RE = /^(\s*)(`{3,}|~{3,})\s*(\S*)\s*$/;
  var HEADING_RE = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  var HR_RE = /^ {0,3}([-*_])( *\1){2,} *$/;
  var LIST_ITEM_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  var TABLE_SEP_RE = /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/;

  function splitTableRow(line: string): string[] {
    var trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map(function (c) { return c.trim(); });
  }

  function renderList(lines: string[], startIndex: number, cache: PublishCache, urlPrefix: string): { html: string; next: number } {
    // Builds a tree of {ordered, items:[{html, children}]} from an indent stack, matching the
    // same indent-based nesting metadata_cache.py's _extract_list_items uses server-side.
    var root: ListTree = { ordered: null, items: [] };
    var stack: { indent: number; node: ListTree }[] = [{ indent: -1, node: root }];
    var i = startIndex;
    for (; i < lines.length; i++) {
      var line = lines[i];
      if (line.trim() === "") { i++; break; }
      var m = LIST_ITEM_RE.exec(line);
      if (!m) break;
      var indent = m[1].length;
      var ordered = /^\d+[.)]$/.test(m[2]);
      while (stack.length > 1 && stack[stack.length - 1].indent >= indent) stack.pop();
      var parent = stack[stack.length - 1].node;
      if (parent.ordered === null) parent.ordered = ordered;
      var item: ListItem = { html: renderInline(m[3], cache, urlPrefix), children: { ordered: null, items: [] } };
      parent.items.push(item);
      stack.push({ indent: indent, node: item.children });
    }

    function renderNode(node: ListTree): string {
      if (!node.items.length) return "";
      var tag = node.ordered ? "ol" : "ul";
      var out = "<" + tag + ">";
      node.items.forEach(function (item) {
        out += "<li>" + item.html + renderNode(item.children) + "</li>";
      });
      out += "</" + tag + ">";
      return out;
    }

    return { html: renderNode(root), next: i };
  }

  /** Renders a markdown subset (matching what python-markdown's extra/codehilite/toc extensions
   * produce server-side) to an HTML string. `cache` is the parsed _cache.json response (used to
   * resolve wikilinks/embeds), `urlPrefix` is window.pumicePreload.url_prefix. */
  function renderMarkdown(text: string, cache: PublishCache, urlPrefix: string): string {
    var lines = text.split("\n");
    var out: string[] = [];
    var i = 0;
    var slugger = makeSlugger();
    while (i < lines.length) {
      var line = lines[i];

      if (line.trim() === "") { i++; continue; }

      var fenceMatch = FENCE_RE.exec(line);
      if (fenceMatch) {
        var fenceChar = fenceMatch[2][0];
        var lang = fenceMatch[3] || "";
        var codeLines: string[] = [];
        var j = i + 1;
        for (; j < lines.length; j++) {
          var closeMatch = FENCE_RE.exec(lines[j]);
          if (closeMatch && closeMatch[2][0] === fenceChar && !closeMatch[3]) { j++; break; }
          codeLines.push(lines[j]);
        }
        var classAttr = lang ? ' class="language-' + escapeAttr(lang) + '"' : "";
        out.push("<pre><code" + classAttr + ">" + escapeHtml(codeLines.join("\n")) + "</code></pre>");
        i = j;
        continue;
      }

      var headingMatch = HEADING_RE.exec(line);
      if (headingMatch) {
        var level = headingMatch[1].length;
        var slug = slugger(headingMatch[2]);
        out.push('<h' + level + ' id="' + escapeAttr(slug) + '">' + renderInline(headingMatch[2], cache, urlPrefix) + "</h" + level + ">");
        i++;
        continue;
      }

      if (HR_RE.test(line)) {
        out.push("<hr>");
        i++;
        continue;
      }

      if (/^\s*>/.test(line)) {
        var quoteLines: string[] = [];
        var k = i;
        for (; k < lines.length && /^\s*>/.test(lines[k]); k++) {
          quoteLines.push(lines[k].replace(/^\s*>\s?/, ""));
        }
        out.push("<blockquote>" + renderMarkdown(quoteLines.join("\n"), cache, urlPrefix) + "</blockquote>");
        i = k;
        continue;
      }

      if (LIST_ITEM_RE.test(line)) {
        var listResult = renderList(lines, i, cache, urlPrefix);
        out.push(listResult.html);
        i = listResult.next;
        continue;
      }

      if (line.indexOf("|") !== -1 && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
        var headerCells = splitTableRow(line);
        var rowsHtml = "<table><thead><tr>" + headerCells.map(function (c) {
          return "<th>" + renderInline(c, cache, urlPrefix) + "</th>";
        }).join("") + "</tr></thead><tbody>";
        var r = i + 2;
        for (; r < lines.length && lines[r].trim() !== "" && lines[r].indexOf("|") !== -1; r++) {
          var cells = splitTableRow(lines[r]);
          rowsHtml += "<tr>" + cells.map(function (c) {
            return "<td>" + renderInline(c, cache, urlPrefix) + "</td>";
          }).join("") + "</tr>";
        }
        rowsHtml += "</tbody></table>";
        out.push(rowsHtml);
        i = r;
        continue;
      }

      // Paragraph: consume consecutive non-blank lines that aren't the start of another block.
      var paraLines: string[] = [];
      var p = i;
      for (; p < lines.length; p++) {
        var l = lines[p];
        if (l.trim() === "") break;
        if (FENCE_RE.test(l) || HEADING_RE.test(l) || HR_RE.test(l) || LIST_ITEM_RE.test(l) || /^\s*>/.test(l)) break;
        paraLines.push(l);
      }
      out.push("<p>" + renderInline(paraLines.join(" "), cache, urlPrefix) + "</p>");
      i = p;
    }
    return out.join("\n");
  }

  /** Wires the search box: instant title-only results on every keystroke (searchNotes, no
   * network), then a debounced `POST _search` for body-text matches merged in once it resolves
   * -- mirrors real Obsidian's own updateSearch()/runFullSearch() two-step flow. A response for
   * a query that's no longer in the input (the visitor kept typing) is discarded, matching real
   * Obsidian's own `t.value !== e` staleness check. */
  function wireSearch(inputEl: HTMLInputElement, resultsEl: HTMLElement, cache: PublishCache, urlPrefix: string): void {
    var index = buildSearchIndex(cache, urlPrefix);
    var debounceTimer: ReturnType<typeof setTimeout> | null = null;

    function renderResults(results: SearchResult[]): void {
      resultsEl.innerHTML = results.map(function (r) {
        var snippetHtml = r.snippet ? '<div class="search-result-snippet">' + escapeHtml(r.snippet) + "</div>" : "";
        return '<a href="' + escapeAttr(r.href) + '"><div class="search-result-title">' + escapeHtml(r.title) + "</div>" + snippetHtml + "</a>";
      }).join("");
    }

    inputEl.addEventListener("input", function () {
      var query = inputEl.value;
      var titleResults = searchNotes(index, query);
      renderResults(titleResults);

      if (debounceTimer) clearTimeout(debounceTimer);
      if (!query.trim()) return;
      var statusEl = document.createElement("div");
      statusEl.className = "search-status";
      statusEl.textContent = "Searching...";
      resultsEl.appendChild(statusEl);
      debounceTimer = setTimeout(function () {
        runServerSearch(query, urlPrefix).then(function (serverResults) {
          if (inputEl.value !== query) return;
          renderResults(mergeSearchResults(titleResults, serverResults, cache, urlPrefix));
        });
      }, SEARCH_DEBOUNCE_MS);
    });
  }

  /** Wires the graph view -- matches real Obsidian Publish's own UI shape exactly (confirmed live
   * on lab.marconoris.com/now, `window.app.graph`), not the earlier click-to-open dark-modal
   * design: a small graph is *always visible* in the sidebar (not hidden behind a toggle button),
   * with two icons -- one expands it to take over the page, the other switches between the
   * default "local" graph (buildLocalGraph -- current note + direct neighbors) and the full-vault
   * "global" graph (confirmed live: `window.app.graph.global` toggles exactly this, node count
   * visibly changes). Expanding replaces the whole viewport outright, not a dimmed overlay over
   * dimmed content -- also confirmed live (`.graph-view-container.mod-expanded` takes over, no
   * backdrop).
   *
   * Physics runs in static/graph_worker.js (a real Web Worker, see layoutGraph's own doc comment)
   * rather than on this thread, so re-rendering after a scope/size change doesn't freeze the
   * page. */
  function wireGraph(
    miniCanvasEl: HTMLElement, fullpageEl: HTMLElement, fullpageCanvasEl: HTMLElement,
    scopeToggleEl: HTMLElement, expandEl: HTMLElement, collapseEl: HTMLElement,
    cache: PublishCache, urlPrefix: string, currentPath: string | null,
  ): void {
    var fullGraph = buildGraph(cache);
    var isGlobal = false;
    var isExpanded = false;

    function renderInto(containerEl: HTMLElement): void {
      var graph = isGlobal ? fullGraph : buildLocalGraph(fullGraph, currentPath);
      var rect = containerEl.getBoundingClientRect();
      var width = Math.max(120, rect.width || 240);
      var height = Math.max(120, rect.height || 240);
      var worker = new Worker("/static/graph_worker.js");
      worker.onmessage = function (e: MessageEvent<Positions>) {
        renderGraphPixi(containerEl, graph, e.data, cache, urlPrefix, currentPath, width, height);
        worker.terminate();
      };
      worker.postMessage({ graph: graph, opts: { width: width, height: height } });
    }

    renderInto(miniCanvasEl);

    scopeToggleEl.addEventListener("click", function () {
      isGlobal = !isGlobal;
      renderInto(isExpanded ? fullpageCanvasEl : miniCanvasEl);
    });

    expandEl.addEventListener("click", function () {
      isExpanded = true;
      (fullpageEl as HTMLElement & { hidden: boolean }).hidden = false;
      renderInto(fullpageCanvasEl);
    });

    collapseEl.addEventListener("click", function () {
      isExpanded = false;
      (fullpageEl as HTMLElement & { hidden: boolean }).hidden = true;
    });
  }

  /** A sliding-window pane's inner HTML (Phase 5, 28_실제_아키텍처_전환_Phase1_노트본문.md's
   * follow-up) -- title + rendered body, the same shape the initial note page itself uses. A
   * deliberate scope cut from real Obsidian's own sliding panes: only the note body is
   * duplicated per pane, not outline/backlinks/graph (those stay scoped to the original page's
   * note, not to whichever pane happens to be scrolled into view). */
  function renderNotePaneHtml(path: string, cache: PublishCache, rawMd: string, urlPrefix: string): string {
    var entry = cache[path];
    var body = stripFrontmatter(rawMd, entry);
    var title = titleFromPath(path);
    return '<h1 class="note-title">' + escapeHtml(title) + "</h1>" + renderMarkdown(body, cache, urlPrefix);
  }

  /** Wires sliding-window navigation (real Obsidian's slidingWindowMode: clicking a note link
   * opens the target as a new pane sliding in from the right instead of a full page navigation,
   * building up a horizontally-scrollable trail). Only intercepts clicks on wikilinks the
   * renderer itself already tagged with a resolved `data-note-path` (see renderInline) -- plain
   * markdown-style links to arbitrary/external URLs are left to navigate normally.
   *
   * Known scope cut vs real Obsidian: browser back/forward doesn't reconstruct the pane trail --
   * each pane open does `history.pushState` so the URL bar/reload/share-link always reflects the
   * most recently opened note, but going back just lands on a normal full page load of whatever
   * was pushed before it, not a live removal of the last pane. Full history reconciliation would
   * need meaningfully more state tracking for a UX difference that's easy to live without. */
  function wireSlidingWindow(trackEl: HTMLElement, cache: PublishCache, urlPrefix: string): void {
    trackEl.addEventListener("click", function (e) {
      var target = e.target as HTMLElement | null;
      var a = target && target.closest ? target.closest("a.wiki-link[data-note-path]") as HTMLAnchorElement | null : null;
      if (!a || !trackEl.contains(a)) return;
      e.preventDefault();
      var path = a.getAttribute("data-note-path")!;
      var href = a.getAttribute("href")!;

      fetch(urlPrefix + "_raw/" + encodePath(path)).then(function (r) { return r.text(); }).then(function (rawMd) {
        var pane = document.createElement("div");
        pane.className = "pumice-sliding-pane";
        pane.innerHTML = renderNotePaneHtml(path, cache, rawMd, urlPrefix);
        trackEl.appendChild(pane);
        pane.scrollIntoView({ behavior: "smooth", inline: "start" });
        history.pushState({}, "", href);
      });
    });
  }

  async function main(): Promise<void> {
    var preload = window.pumicePreload;
    if (!preload) return;

    var cachePromise: Promise<PublishCache> = fetch(preload.url_prefix + "_cache.json").then(function (r) { return r.json(); });

    var root = document.getElementById("pumice-note-root");
    if (root && preload.path) {
      try {
        var cache = await cachePromise;
        var rawResp = await fetch(preload.url_prefix + "_raw/" + encodePath(preload.path));
        var rawMd = await rawResp.text();

        var entry = cache[preload.path];
        var body = stripFrontmatter(rawMd, entry);

        if (preload.sliding_window_mode) {
          var trackEl = document.createElement("div");
          trackEl.className = "pumice-sliding-track";
          var firstPane = document.createElement("div");
          firstPane.className = "pumice-sliding-pane";
          firstPane.innerHTML = renderMarkdown(body, cache, preload.url_prefix);
          trackEl.appendChild(firstPane);
          root.innerHTML = "";
          root.appendChild(trackEl);
          wireSlidingWindow(trackEl, cache, preload.url_prefix);
        } else {
          root.innerHTML = renderMarkdown(body, cache, preload.url_prefix);
        }

        var outlineEl = document.getElementById("pumice-outline");
        if (outlineEl) {
          outlineEl.innerHTML = renderOutlineHtml(buildOutline(entry));
          wireOutlineScrollSpy(root, outlineEl);
        }

        var backlinksEl = document.getElementById("pumice-backlinks");
        if (backlinksEl) {
          var backlinks = buildBacklinks(cache, preload.path, preload.url_prefix);
          backlinksEl.innerHTML = backlinks.length
            ? "<h2>Backlinks</h2><ul>" + backlinks.map(function (b) {
                return '<li><a href="' + escapeAttr(b.href) + '">' + escapeHtml(b.path) + "</a></li>";
              }).join("") + "</ul>"
            : "";
        }
      } catch (e) {
        root.textContent = "Failed to render note: " + e;
      }
    }

    var searchInputEl = document.getElementById("pumice-search-input") as HTMLInputElement | null;
    var searchResultsEl = document.getElementById("pumice-search-results");
    if (searchInputEl && searchResultsEl) {
      try {
        wireSearch(searchInputEl, searchResultsEl, await cachePromise, preload.url_prefix);
      } catch (e) {
        // Search is a progressive-enhancement affordance on top of the page's real content --
        // a failed cache fetch here shouldn't surface an error where the note body already
        // succeeded (or where there's no note body at all, on the file-listing page).
      }
    }

    var graphMiniCanvasEl = document.getElementById("pumice-graph-mini-canvas");
    var graphFullpageEl = document.getElementById("pumice-graph-fullpage");
    var graphFullpageCanvasEl = document.getElementById("pumice-graph-fullpage-canvas");
    var graphScopeToggleEl = document.getElementById("pumice-graph-scope-toggle");
    var graphExpandEl = document.getElementById("pumice-graph-expand");
    var graphCollapseEl = document.getElementById("pumice-graph-collapse");
    if (graphMiniCanvasEl && graphFullpageEl && graphFullpageCanvasEl && graphScopeToggleEl && graphExpandEl && graphCollapseEl) {
      try {
        wireGraph(
          graphMiniCanvasEl, graphFullpageEl, graphFullpageCanvasEl, graphScopeToggleEl, graphExpandEl, graphCollapseEl,
          await cachePromise, preload.url_prefix, preload.path,
        );
      } catch (e) {
        // Same progressive-enhancement reasoning as search above.
      }
    }
  }

  var api = {
    escapeHtml: escapeHtml,
    encodePath: encodePath,
    stripFrontmatter: stripFrontmatter,
    resolveWikilinkHref: resolveWikilinkHref,
    renderInline: renderInline,
    renderMarkdown: renderMarkdown,
    buildOutline: buildOutline,
    renderOutlineHtml: renderOutlineHtml,
    findActiveHeadingSlug: findActiveHeadingSlug,
    linkTargetMatchesPath: linkTargetMatchesPath,
    buildBacklinks: buildBacklinks,
    buildSearchIndex: buildSearchIndex,
    searchNotes: searchNotes,
    mergeSearchResults: mergeSearchResults,
    buildGraph: buildGraph,
    buildLocalGraph: buildLocalGraph,
    layoutGraph: layoutGraph,
    manyBodyForcesWasm: manyBodyForcesWasm,
    isGraphForcesWasmLoaded: function () { return !!graphForcesWasm; },
    cssColorToPixiHex: cssColorToPixiHex,
    resolveNoteTargetPath: resolveNoteTargetPath,
    renderNotePaneHtml: renderNotePaneHtml,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (typeof document === "undefined") {
    // Web Worker context (see static/graph_worker.js, loaded via importScripts) -- no
    // window/document here, and main() reads both, so just expose the API and stop. `self` is
    // typed via the DOM lib's `Window & typeof globalThis` here (this file deliberately doesn't
    // include the "webworker" lib, which conflicts with "dom" if both are present) -- a type-
    // checking-time fiction, but a safe one: the only property touched through it is one this
    // file itself declared (Window.PumicePublishRenderer, above).
    self.PumicePublishRenderer = api;
  } else {
    window.PumicePublishRenderer = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", main);
    } else {
      main();
    }
  }
})();
