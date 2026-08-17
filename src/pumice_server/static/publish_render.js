/*
 * Client-side note renderer for pumice-server's Publish pages -- see
 * 27_실제_퍼블리시_사이트_아키텍처_조사.md / 28_실제_아키텍처_전환_Phase1_노트본문.md /
 * 37_실제_아키텍처_전환_TOC_재설계.md / 38_실제_아키텍처_전환_검색_그래프_실제화.md (pumice repo).
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
 * Plain vanilla JS, no build step, no own dependencies -- same convention as the existing
 * static/dashboard.js and static/login.js. static/pixi.min.js (graph rendering) is the one
 * exception: a vendored, unmodified, MIT-licensed build (the "pixi.js-legacy" variant
 * specifically, which auto-falls-back to Canvas 2D when WebGL isn't available -- own choice
 * beyond what was confirmed live), loaded via its own <script> tag exactly the way real Obsidian
 * Publish's own page loads it too (a separate script before app.js, no bundler on their side
 * either). Loaded directly via <script defer src="/static/
 * publish_render.js"> and also loadable under Node's `node:test` (see
 * tests/publish_render.test.cjs) via the CommonJS export guard at the bottom.
 */
(function () {
  "use strict";

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, "&quot;");
  }

  // Mirrors urllib.parse.quote(x, safe='/') closely enough for this renderer's purposes: encode
  // every path segment, but keep '/' as a literal separator.
  function encodePath(path) {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  /** Strips a note's raw markdown down to just its body, using the same frontmatterPos this
   * note's own cache entry carries (see metadata_cache.py) -- avoids re-detecting the `---`
   * block client-side when the server already computed exactly where it ends. */
  function stripFrontmatter(rawMd, cacheEntry) {
    if (!cacheEntry || !cacheEntry.frontmatterPos) return rawMd;
    var endOffset = cacheEntry.frontmatterPos[5];
    var rest = rawMd.slice(endOffset);
    return rest.replace(/^\n/, "");
  }

  /** Resolves a `[[target|label]]`-style wikilink target to an href, the same way
   * parse_wikilinks()/`_build_path_to_permalink` do server-side (see web.py, 19_permalink_지원.md)
   * -- prefer the target file's permalink (merged into the cache by the `_cache.json` endpoint),
   * falling back to the literal target path (with a trailing .md stripped) under url_prefix. */
  function resolveWikilinkHref(target, cache, urlPrefix) {
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
  function resolveNoteTargetPath(target, cache) {
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
  function slugify(text) {
    var slug = text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return slug || "section";
  }

  function makeSlugger() {
    var counts = {};
    return function (text) {
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
  function buildOutline(cacheEntry) {
    if (!cacheEntry || !cacheEntry.headings) return [];
    var slugger = makeSlugger();
    var root = [];
    var stack = [];
    cacheEntry.headings.forEach(function (h) {
      var node = { level: h.level, heading: h.heading, slug: slugger(h.heading), children: [] };
      while (stack.length && stack[stack.length - 1].level >= node.level) stack.pop();
      (stack.length ? stack[stack.length - 1].children : root).push(node);
      stack.push(node);
    });
    return root;
  }

  /** Renders an outline tree (buildOutline's output) as nested `<ul>`s -- own class names
   * (`pumice-outline-*`), not real Obsidian's (`tree-item`/`tree-item-children`/...): this is a
   * from-scratch reimplementation informed by observed behavior, not copied markup. */
  function renderOutlineHtml(tree) {
    if (!tree.length) return "";
    return "<ul>" + tree.map(renderOutlineItem).join("") + "</ul>";
  }

  function renderOutlineItem(node) {
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
  function findActiveHeadingSlug(headingTops) {
    var active = null;
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
  function wireOutlineScrollSpy(contentEl, outlineEl) {
    var headingEls = Array.prototype.slice.call(contentEl.querySelectorAll("h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]"));
    if (!headingEls.length) return;

    var current = null;
    var ticking = false;

    function update() {
      ticking = false;
      var headingTops = headingEls.map(function (h) { return { slug: h.id, top: h.getBoundingClientRect().top }; });
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
  function linkTargetMatchesPath(target, storedPath) {
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
  function buildBacklinks(cache, currentPath, urlPrefix) {
    var results = [];
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

  function titleFromPath(path) {
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
  function buildSearchIndex(cache, urlPrefix) {
    var index = [];
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
  function searchNotes(index, query) {
    var trimmed = (query || "").trim();
    if (!trimmed) return [];
    var lower = trimmed.toLowerCase();
    var results = [];
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
  function mergeSearchResults(titleResults, serverResults, cache, urlPrefix) {
    var seen = {};
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
  function runServerSearch(query, urlPrefix) {
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
  function buildGraph(cache) {
    var nodes = [];
    var nodePaths = {};
    Object.keys(cache).forEach(function (path) {
      var entry = cache[path];
      if (!entry || !/\.md$/i.test(path)) return;
      nodePaths[path] = true;
      nodes.push({ path: path, title: titleFromPath(path) });
    });

    var seenEdgeKeys = {};
    var edges = [];
    nodes.forEach(function (node) {
      var entry = cache[node.path];
      (entry.links || []).forEach(function (link) {
        var targetPath = null;
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

  var GRAPH_DEFAULT_ITERATIONS = 200;

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
  function layoutGraph(graph, opts) {
    opts = opts || {};
    var width = opts.width || 600;
    var height = opts.height || 600;
    var iterations = opts.iterations || GRAPH_DEFAULT_ITERATIONS;
    var nodes = graph.nodes;
    var n = nodes.length;
    var positions = {};
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
      var disp = {};
      nodes.forEach(function (node) { disp[node.path] = { x: 0, y: 0 }; });

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
        pos.x = Math.min(width, Math.max(0, pos.x + (d.x / dlen) * limited));
        pos.y = Math.min(height, Math.max(0, pos.y + (d.y / dlen) * limited));
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
  function cssColorToPixiHex(cssColor) {
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
   * either, matching this project's own "vanilla JS, no build step" convention already). Replaces
   * the earlier plain-SVG renderer -- real Obsidian's graph is WebGL-rendered, confirmed by
   * reading app.js's PIXI.* usage. Theme-aware: colors are read from this page's own CSS custom
   * properties (via cssColorToPixiHex) rather than hardcoded, so the graph matches whichever
   * light/dark theme is active (33_실제_아키텍처_전환_테마_전환.md). Known scope cut vs real
   * Obsidian: no pan/zoom -- the static laid-out positions from the worker are rendered as-is,
   * matching this project's existing (pre-Pixi) graph's own already-static behavior. */
  function renderGraphPixi(containerEl, graph, positions, cache, urlPrefix, currentPath, width, height) {
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

    return app;
  }

  function renderInline(text, cache, urlPrefix) {
    var escaped = escapeHtml(text);

    // 1. Inline code spans first, pulled out to placeholders so nothing inside them is touched
    // by the formatting rules below.
    var codeSpans = [];
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

  function splitTableRow(line) {
    var trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map(function (c) { return c.trim(); });
  }

  function renderList(lines, startIndex, cache, urlPrefix) {
    // Builds a tree of {ordered, items:[{html, children}]} from an indent stack, matching the
    // same indent-based nesting metadata_cache.py's _extract_list_items uses server-side.
    var root = { ordered: null, items: [] };
    var stack = [{ indent: -1, node: root }];
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
      var item = { html: renderInline(m[3], cache, urlPrefix), children: { ordered: null, items: [] } };
      parent.items.push(item);
      stack.push({ indent: indent, node: item.children });
    }

    function renderNode(node) {
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
  function renderMarkdown(text, cache, urlPrefix) {
    var lines = text.split("\n");
    var out = [];
    var i = 0;
    var slugger = makeSlugger();
    while (i < lines.length) {
      var line = lines[i];

      if (line.trim() === "") { i++; continue; }

      var fenceMatch = FENCE_RE.exec(line);
      if (fenceMatch) {
        var fenceChar = fenceMatch[2][0];
        var lang = fenceMatch[3] || "";
        var codeLines = [];
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
        var quoteLines = [];
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
      var paraLines = [];
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
  function wireSearch(inputEl, resultsEl, cache, urlPrefix) {
    var index = buildSearchIndex(cache, urlPrefix);
    var debounceTimer = null;

    function renderResults(results) {
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

  /** Wires the graph-view toggle button/overlay -- the layout is computed lazily on first open
   * (force-directed layout over a whole vault isn't free, no reason to pay for it on every page
   * load if the visitor never opens the graph) and cached for subsequent opens.
   *
   * Physics runs in static/graph_worker.js (a real Web Worker, see layoutGraph's own doc comment)
   * rather than on this thread, so opening the graph doesn't freeze the page while it settles --
   * the overlay opens immediately and the canvas fills in once the worker posts back positions. */
  function wireGraph(toggleEl, closeEl, overlayEl, containerEl, cache, urlPrefix, currentPath) {
    var rendered = false;
    toggleEl.addEventListener("click", function () {
      if (!rendered) {
        rendered = true;
        var graph = buildGraph(cache);
        var rect = containerEl.getBoundingClientRect();
        var size = Math.max(200, Math.min(rect.width || 600, rect.height || 600));
        var worker = new Worker("/static/graph_worker.js");
        worker.onmessage = function (e) {
          renderGraphPixi(containerEl, graph, e.data, cache, urlPrefix, currentPath, size, size);
          worker.terminate();
        };
        worker.postMessage({ graph: graph, opts: { width: size, height: size } });
      }
      overlayEl.hidden = false;
    });
    closeEl.addEventListener("click", function () {
      overlayEl.hidden = true;
    });
    overlayEl.addEventListener("click", function (e) {
      if (e.target === overlayEl) overlayEl.hidden = true;
    });
  }

  /** A sliding-window pane's inner HTML (Phase 5, 28_실제_아키텍처_전환_Phase1_노트본문.md's
   * follow-up) -- title + rendered body, the same shape the initial note page itself uses. A
   * deliberate scope cut from real Obsidian's own sliding panes: only the note body is
   * duplicated per pane, not outline/backlinks/graph (those stay scoped to the original page's
   * note, not to whichever pane happens to be scrolled into view). */
  function renderNotePaneHtml(path, cache, rawMd, urlPrefix) {
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
  function wireSlidingWindow(trackEl, cache, urlPrefix) {
    trackEl.addEventListener("click", function (e) {
      var a = e.target.closest ? e.target.closest("a.wiki-link[data-note-path]") : null;
      if (!a || !trackEl.contains(a)) return;
      e.preventDefault();
      var path = a.getAttribute("data-note-path");
      var href = a.getAttribute("href");

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

  async function main() {
    var preload = window.pumicePreload;
    if (!preload) return;

    var cachePromise = fetch(preload.url_prefix + "_cache.json").then(function (r) { return r.json(); });

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

    var searchInputEl = document.getElementById("pumice-search-input");
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

    var graphToggleEl = document.getElementById("pumice-graph-toggle");
    var graphOverlayEl = document.getElementById("pumice-graph-overlay");
    var graphCloseEl = document.getElementById("pumice-graph-close");
    var graphContainerEl = document.getElementById("pumice-graph-container");
    if (graphToggleEl && graphOverlayEl && graphCloseEl && graphContainerEl) {
      try {
        wireGraph(graphToggleEl, graphCloseEl, graphOverlayEl, graphContainerEl, await cachePromise, preload.url_prefix, preload.path);
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
    layoutGraph: layoutGraph,
    cssColorToPixiHex: cssColorToPixiHex,
    resolveNoteTargetPath: resolveNoteTargetPath,
    renderNotePaneHtml: renderNotePaneHtml,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (typeof document === "undefined") {
    // Web Worker context (see static/graph_worker.js, loaded via importScripts) -- no
    // window/document here, and main() reads both, so just expose the API and stop.
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
