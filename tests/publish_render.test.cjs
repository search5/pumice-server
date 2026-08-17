// TDD for static/publish_render.js's pure functions -- see
// 28_실제_아키텍처_전환_Phase1_노트본문.md / 40_publish_render_TypeScript_전환.md (pumice repo).
// The source is now client/publish_render.ts (compiled by tsc into static/publish_render.js,
// same path it's always been served from), so this uses Node's built-in test runner directly
// against the compiled output the browser also loads, run as CommonJS (see the module.exports
// guard at the bottom of publish_render.ts).
//
// Run with: npm test (builds then tests) or, if already built, node --test
// tests/publish_render.test.cjs directly.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const renderer = require(path.join(__dirname, "..", "src", "pumice_server", "static", "publish_render.js"));

test("stripFrontmatter removes the frontmatter block using the cache entry's frontmatterPos", () => {
  const raw = "---\ntitle: Hello\n---\nBody text.\n";
  const entry = { frontmatterPos: [0, 0, 0, 2, 3, 20] };
  assert.equal(renderer.stripFrontmatter(raw, entry), "Body text.\n");
});

test("stripFrontmatter returns the text unchanged when there is no frontmatter", () => {
  const raw = "Just a body.\n";
  assert.equal(renderer.stripFrontmatter(raw, null), raw);
});

test("resolveWikilinkHref prefers the target's permalink when the cache has one", () => {
  const cache = { "Note.md": { permalink: "/pretty/path/" } };
  assert.equal(renderer.resolveWikilinkHref("Note.md", cache, "/publish/alice/vault1/"), "/publish/alice/vault1/pretty/path/");
});

test("resolveWikilinkHref falls back to the literal target with .md stripped", () => {
  assert.equal(renderer.resolveWikilinkHref("Note.md", {}, "/publish/alice/vault1/"), "/publish/alice/vault1/Note");
  assert.equal(renderer.resolveWikilinkHref("Folder/Note", {}, "/publish/alice/vault1/"), "/publish/alice/vault1/Folder/Note");
});

test("renderMarkdown renders headings (with a slugified id for outline anchors) and a paragraph", () => {
  const html = renderer.renderMarkdown("# Title\n\nHello world.\n", {}, "/publish/alice/vault1/");
  assert.equal(html, '<h1 id="title">Title</h1>\n<p>Hello world.</p>');
});

test("renderMarkdown de-duplicates heading ids the same way buildOutline does", () => {
  const html = renderer.renderMarkdown("# Same\n\n# Same\n", {}, "/publish/alice/vault1/");
  assert.equal(html, '<h1 id="same">Same</h1>\n<h1 id="same-1">Same</h1>');
});

test("renderMarkdown renders bold, italic and inline code without cross-contamination", () => {
  const html = renderer.renderMarkdown("**bold** and *italic* and `code`\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<p><strong>bold</strong> and <em>italic</em> and <code>code</code></p>");
});

test("renderMarkdown resolves a wikilink to a permalink from the cache, tagging its resolved note path", () => {
  const cache = { "Target": { permalink: "/nice-url/" } };
  const html = renderer.renderMarkdown("See [[Target|Custom Label]].\n", cache, "/publish/alice/vault1/");
  assert.equal(
    html,
    '<p>See <a href="/publish/alice/vault1/nice-url/" class="wiki-link" data-note-path="Target">Custom Label</a>.</p>'
  );
});

test("renderMarkdown omits data-note-path for a wikilink that doesn't resolve to a known cache entry", () => {
  const html = renderer.renderMarkdown("See [[Nowhere]].\n", {}, "/publish/alice/vault1/");
  assert.doesNotMatch(html, /data-note-path/);
});

test("renderMarkdown renders a fenced code block verbatim, without inline formatting applied", () => {
  const html = renderer.renderMarkdown("```js\nconst a = **not bold**;\n```\n", {}, "/publish/alice/vault1/");
  assert.equal(html, '<pre><code class="language-js">const a = **not bold**;</code></pre>');
});

test("renderMarkdown renders a nested unordered list", () => {
  const html = renderer.renderMarkdown("- parent\n  - child\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<ul><li>parent<ul><li>child</li></ul></li></ul>");
});

test("renderMarkdown renders an ordered list separately from an unordered one", () => {
  const html = renderer.renderMarkdown("1. one\n2. two\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<ol><li>one</li><li>two</li></ol>");
});

test("renderMarkdown renders a blockquote", () => {
  const html = renderer.renderMarkdown("> quoted text\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<blockquote><p>quoted text</p></blockquote>");
});

test("renderMarkdown renders a horizontal rule", () => {
  const html = renderer.renderMarkdown("above\n\n---\n\nbelow\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<p>above</p>\n<hr>\n<p>below</p>");
});

test("renderMarkdown renders a GFM-style pipe table", () => {
  const html = renderer.renderMarkdown("| A | B |\n| - | - |\n| 1 | 2 |\n", {}, "/publish/alice/vault1/");
  assert.equal(
    html,
    "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
  );
});

test("renderMarkdown renders an image embed resolved against the cache", () => {
  const html = renderer.renderMarkdown("![[photo.png]]\n", {}, "/publish/alice/vault1/");
  assert.equal(html, '<p><img src="/publish/alice/vault1/photo.png" alt="photo.png"></p>');
});

test("renderInline escapes raw HTML in the source text", () => {
  const html = renderer.renderMarkdown("<script>alert(1)</script>\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>");
});

// ─── Phase 2: outline + backlinks (28_실제_아키텍처_전환_Phase1_노트본문.md's Phase 2 follow-up) ──

test("buildOutline nests a heading under the nearest preceding heading of a strictly lower level", () => {
  const entry = { headings: [{ heading: "Intro", level: 1 }, { heading: "Sub Point", level: 2 }] };
  assert.deepEqual(renderer.buildOutline(entry), [
    {
      level: 1, heading: "Intro", slug: "intro",
      children: [{ level: 2, heading: "Sub Point", slug: "sub-point", children: [] }],
    },
  ]);
});

test("buildOutline nests skipped levels (H1 then H5) under the H1, matching real Obsidian Publish's own outline tree (confirmed live via window.app.outline.treeView on lab.marconoris.com/now)", () => {
  const entry = {
    headings: [
      { heading: "Title", level: 1 },
      { heading: "A", level: 5 },
      { heading: "B", level: 5 },
    ],
  };
  const outline = renderer.buildOutline(entry);
  assert.equal(outline.length, 1);
  assert.equal(outline[0].heading, "Title");
  assert.deepEqual(outline[0].children.map((c) => c.heading), ["A", "B"]);
});

test("buildOutline starts a new sibling once a heading returns to a shallower level", () => {
  const entry = {
    headings: [
      { heading: "H2 One", level: 2 },
      { heading: "H3 Under One", level: 3 },
      { heading: "H2 Two", level: 2 },
    ],
  };
  const outline = renderer.buildOutline(entry);
  assert.equal(outline.length, 2);
  assert.equal(outline[0].heading, "H2 One");
  assert.deepEqual(outline[0].children.map((c) => c.heading), ["H3 Under One"]);
  assert.equal(outline[1].heading, "H2 Two");
  assert.deepEqual(outline[1].children, []);
});

test("buildOutline does not filter out H1 headings (confirmed live: a note's own body H1 shows up in the real outline)", () => {
  const entry = { headings: [{ heading: "Body H1", level: 1 }] };
  assert.equal(renderer.buildOutline(entry)[0].heading, "Body H1");
});

test("buildOutline de-duplicates identical heading text the same way as renderMarkdown", () => {
  const entry = { headings: [{ heading: "Same", level: 1 }, { heading: "Same", level: 1 }] };
  assert.deepEqual(renderer.buildOutline(entry).map((o) => o.slug), ["same", "same-1"]);
});

test("buildOutline returns an empty list for a note with no headings", () => {
  assert.deepEqual(renderer.buildOutline({ headings: [] }), []);
  assert.deepEqual(renderer.buildOutline(null), []);
});

test("renderOutlineHtml renders a flat outline as a single <ul>", () => {
  const tree = [{ level: 1, heading: "Intro", slug: "intro", children: [] }];
  assert.equal(renderer.renderOutlineHtml(tree), '<ul><li><a href="#intro">Intro</a></li></ul>');
});

test("renderOutlineHtml nests children in their own <ul class=\"pumice-outline-children\">", () => {
  const tree = [{
    level: 1, heading: "Intro", slug: "intro",
    children: [{ level: 2, heading: "Sub", slug: "sub", children: [] }],
  }];
  assert.equal(
    renderer.renderOutlineHtml(tree),
    '<ul><li><a href="#intro">Intro</a><ul class="pumice-outline-children"><li><a href="#sub">Sub</a></li></ul></li></ul>',
  );
});

test("renderOutlineHtml escapes heading text and slug attributes", () => {
  const tree = [{ level: 1, heading: "<script>", slug: 'a"b', children: [] }];
  assert.equal(renderer.renderOutlineHtml(tree), '<ul><li><a href="#a&quot;b">&lt;script&gt;</a></li></ul>');
});

test("renderOutlineHtml returns an empty string for an empty outline", () => {
  assert.equal(renderer.renderOutlineHtml([]), "");
});

test("findActiveHeadingSlug picks the last heading already scrolled past (top <= 0)", () => {
  const tops = [
    { slug: "a", top: -500 },
    { slug: "b", top: -100 },
    { slug: "c", top: 200 },
  ];
  assert.equal(renderer.findActiveHeadingSlug(tops), "b");
});

test("findActiveHeadingSlug returns null when no heading has been scrolled past yet", () => {
  const tops = [{ slug: "a", top: 50 }, { slug: "b", top: 300 }];
  assert.equal(renderer.findActiveHeadingSlug(tops), null);
});

test("findActiveHeadingSlug returns the very first heading when it's exactly at the top", () => {
  assert.equal(renderer.findActiveHeadingSlug([{ slug: "a", top: 0 }]), "a");
});

test("findActiveHeadingSlug returns null for an empty heading list", () => {
  assert.equal(renderer.findActiveHeadingSlug([]), null);
});

test("linkTargetMatchesPath matches a wikilink target against the .md-suffixed stored path", () => {
  assert.equal(renderer.linkTargetMatchesPath("Note", "Note.md"), true);
  assert.equal(renderer.linkTargetMatchesPath("Note.md", "Note.md"), true);
  assert.equal(renderer.linkTargetMatchesPath("Other", "Note.md"), false);
});

test("buildBacklinks finds every other note whose links[] targets the current path", () => {
  const cache = {
    "Home.md": { links: [{ link: "Note" }] },
    "Note.md": { links: [] },
    "Unrelated.md": { links: [{ link: "SomethingElse" }] },
  };
  const backlinks = renderer.buildBacklinks(cache, "Note.md", "/publish/alice/vault1/");
  assert.deepEqual(backlinks, [{ path: "Home.md", href: "/publish/alice/vault1/Home" }]);
});

test("buildBacklinks excludes the current note itself and skips null cache entries", () => {
  const cache = {
    "Note.md": { links: [{ link: "Note" }] },
    "publish.js": null,
  };
  assert.deepEqual(renderer.buildBacklinks(cache, "Note.md", "/publish/alice/vault1/"), []);
});

test("buildBacklinks resolves the backlinking note's own permalink when it has one", () => {
  const cache = {
    "Home.md": { links: [{ link: "Note" }], permalink: "/welcome/" },
    "Note.md": { links: [] },
  };
  const backlinks = renderer.buildBacklinks(cache, "Note.md", "/publish/alice/vault1/");
  assert.deepEqual(backlinks, [{ path: "Home.md", href: "/publish/alice/vault1/welcome/" }]);
});

// ─── Phase 3: search (28_실제_아키텍처_전환_Phase1_노트본문.md's Phase 3 follow-up; redesigned in
// 38_실제_아키텍처_전환_검색_그래프_실제화.md to split instant title matching (client) from
// body-text matching (server, see web.py's _search_note_bodies) after reading real Obsidian's
// app.js confirmed that's the real split) ────────────────────────────────────────────────────

test("buildSearchIndex includes only markdown entries, deriving a filename-based title (no searchText -- real Obsidian's client never gets note bodies)", () => {
  const cache = {
    "Notes/Deep Dive.md": { headings: [] },
    "publish.js": null,
    "image.png": null,
  };
  const index = renderer.buildSearchIndex(cache, "/publish/alice/vault1/");
  assert.deepEqual(index, [
    { path: "Notes/Deep Dive.md", title: "Deep Dive", href: "/publish/alice/vault1/Notes/Deep%20Dive" },
  ]);
});

test("buildSearchIndex resolves a permalink into the entry's href when present", () => {
  const cache = { "Note.md": { headings: [], permalink: "/nice/" } };
  const index = renderer.buildSearchIndex(cache, "/publish/alice/vault1/");
  assert.equal(index[0].href, "/publish/alice/vault1/nice/");
});

test("searchNotes returns nothing for an empty or whitespace-only query", () => {
  const index = [{ path: "Note.md", title: "Note", href: "/x" }];
  assert.deepEqual(renderer.searchNotes(index, ""), []);
  assert.deepEqual(renderer.searchNotes(index, "   "), []);
});

test("searchNotes matches case-insensitively against the title only (no body text available client-side anymore)", () => {
  const index = [{ path: "Note.md", title: "Cooking Tips", href: "/x" }];
  assert.equal(renderer.searchNotes(index, "cooking").length, 1);
  assert.deepEqual(renderer.searchNotes(index, "nonexistent-body-word"), []);
});

test("searchNotes excludes non-matching entries", () => {
  const index = [
    { path: "A.md", title: "Apples", href: "/a" },
    { path: "B.md", title: "Bicycles", href: "/b" },
  ];
  const results = renderer.searchNotes(index, "apples");
  assert.deepEqual(results.map((r) => r.path), ["A.md"]);
});

test("searchNotes caps the number of results", () => {
  const index = Array.from({ length: 30 }, (_, i) => ({ path: `N${i}.md`, title: `Match ${i}`, href: `/${i}` }));
  const results = renderer.searchNotes(index, "match");
  assert.ok(results.length <= 20);
});

test("mergeSearchResults keeps title results and appends new server (body-text) results with their snippet", () => {
  const titleResults = [{ path: "A.md", title: "A", href: "/a", snippet: "" }];
  const serverResults = [{ path: "B.md", snippet: "...found it here..." }];
  const cache = { "B.md": {} };
  const merged = renderer.mergeSearchResults(titleResults, serverResults, cache, "/publish/alice/vault1/");
  assert.deepEqual(merged, [
    { path: "A.md", title: "A", href: "/a", snippet: "" },
    { path: "B.md", title: "B", href: "/publish/alice/vault1/B", snippet: "...found it here..." },
  ]);
});

test("mergeSearchResults deduplicates a path that's already in the title results", () => {
  const titleResults = [{ path: "A.md", title: "A", href: "/a", snippet: "" }];
  const serverResults = [{ path: "A.md", snippet: "shouldn't appear twice" }];
  const merged = renderer.mergeSearchResults(titleResults, serverResults, {}, "/publish/alice/vault1/");
  assert.equal(merged.length, 1);
  assert.equal(merged[0].snippet, "");
});

test("mergeSearchResults caps the combined total at the result limit", () => {
  const titleResults = Array.from({ length: 15 }, (_, i) => ({ path: `T${i}.md`, title: `T${i}`, href: `/${i}`, snippet: "" }));
  const serverResults = Array.from({ length: 15 }, (_, i) => ({ path: `S${i}.md`, snippet: "x" }));
  const merged = renderer.mergeSearchResults(titleResults, serverResults, {}, "/");
  assert.equal(merged.length, 20);
});

// ─── Phase 4: graph view (28_실제_아키텍처_전환_Phase1_노트본문.md's Phase 4 follow-up) ──────────

test("buildGraph turns cache links into deduplicated nodes/edges between known markdown files", () => {
  const cache = {
    "A.md": { links: [{ link: "B" }] },
    "B.md": { links: [{ link: "C" }] },
    "C.md": { links: [] },
  };
  const graph = renderer.buildGraph(cache);
  assert.deepEqual(graph.nodes.map((n) => n.path), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(graph.edges, [
    { source: "A.md", target: "B.md" },
    { source: "B.md", target: "C.md" },
  ]);
});

test("buildGraph excludes self-links, unresolved link targets, and non-markdown entries", () => {
  const cache = {
    "A.md": { links: [{ link: "A" }, { link: "Missing" }, { link: "image.png" }] },
    "image.png": null,
  };
  const graph = renderer.buildGraph(cache);
  assert.deepEqual(graph.nodes.map((n) => n.path), ["A.md"]);
  assert.deepEqual(graph.edges, []);
});

test("buildGraph collapses a link that appears twice (or in both directions) into one edge", () => {
  const cache = {
    "A.md": { links: [{ link: "B" }, { link: "B" }] },
    "B.md": { links: [{ link: "A" }] },
  };
  const graph = renderer.buildGraph(cache);
  assert.equal(graph.edges.length, 1);
});

test("layoutGraph returns an empty position map for an empty graph", () => {
  assert.deepEqual(renderer.layoutGraph({ nodes: [], edges: [] }, {}), {});
});

test("layoutGraph returns finite positions for every node", () => {
  const graph = { nodes: [{ path: "A.md" }, { path: "B.md" }], edges: [{ source: "A.md", target: "B.md" }] };
  const positions = renderer.layoutGraph(graph, { width: 300, height: 300, iterations: 50 });
  Object.keys(positions).forEach((path) => {
    assert.ok(Number.isFinite(positions[path].x));
    assert.ok(Number.isFinite(positions[path].y));
  });
});

test("layoutGraph pulls connected nodes closer together than to an unconnected node", () => {
  const graph = {
    nodes: [{ path: "A.md" }, { path: "B.md" }, { path: "C.md" }],
    edges: [{ source: "A.md", target: "B.md" }],
  };
  const positions = renderer.layoutGraph(graph, { width: 400, height: 400, iterations: 150 });
  const dist = (p, q) => Math.hypot(p.x - q.x, p.y - q.y);
  const distAB = dist(positions["A.md"], positions["B.md"]);
  const distAC = dist(positions["A.md"], positions["C.md"]);
  assert.ok(distAB < distAC, `expected connected AB (${distAB}) closer than unconnected AC (${distAC})`);
});

// ─── WASM-accelerated many-body force (39_그래프_뷰_WASM_Barnes-Hut.md's follow-up to
// 38_실제_아키텍처_전환_검색_그래프_실제화.md) -- Node has native WebAssembly support, so this
// actually exercises the real compiled module, not just a mock. ───────────────────────────────

test("the WASM graph-forces module loads successfully", () => {
  assert.equal(renderer.isGraphForcesWasmLoaded(), true);
});

test("manyBodyForcesWasm pushes two points directly apart along the line between them", () => {
  // Two points only -- no aggregation possible, so Barnes-Hut must visit both leaves exactly
  // (matches the brute-force k*k/dist formula exactly, not just approximately).
  const { fx, fy } = renderer.manyBodyForcesWasm([0, 10], [0, 0], 5);
  assert.ok(fx[0] < 0, "point 0 should be pushed in -x, away from point 1");
  assert.ok(fx[1] > 0, "point 1 should be pushed in +x, away from point 0");
  assert.equal(fy[0], 0);
  assert.equal(fy[1], 0);
  const k = 5;
  const expectedForce = (k * k) / 10;
  assert.ok(Math.abs(Math.abs(fx[0]) - expectedForce) < 1e-6, `expected exact k*k/dist force, got ${fx[0]}`);
});

test("manyBodyForcesWasm returns a zero-length result for an empty input", () => {
  const { fx, fy } = renderer.manyBodyForcesWasm([], [], 5);
  assert.deepEqual(fx, []);
  assert.deepEqual(fy, []);
});

test("manyBodyForcesWasm approximates brute-force k*k/dist repulsion closely for a small cluster", () => {
  const xs = [0, 50, 100, 30, 70];
  const ys = [0, 20, 0, 60, 55];
  const k = 8;
  const { fx, fy } = renderer.manyBodyForcesWasm(xs, ys, k);

  const bruteFx = xs.map(() => 0), bruteFy = ys.map(() => 0);
  for (let i = 0; i < xs.length; i++) {
    for (let j = 0; j < xs.length; j++) {
      if (i === j) continue;
      const dx = xs[i] - xs[j], dy = ys[i] - ys[j];
      const dist = Math.hypot(dx, dy) || 0.01;
      const force = (k * k) / dist;
      bruteFx[i] += (dx / dist) * force;
      bruteFy[i] += (dy / dist) * force;
    }
  }
  for (let i = 0; i < xs.length; i++) {
    const err = Math.hypot(fx[i] - bruteFx[i], fy[i] - bruteFy[i]);
    const mag = Math.hypot(bruteFx[i], bruteFy[i]) || 1;
    assert.ok(err / mag < 0.5, `point ${i}: WASM force too far from brute force (relative error ${err / mag})`);
  }
});

test("layoutGraph stays fast (WASM Barnes-Hut, not the O(n^2) JS fallback) for a graph with hundreds of nodes", () => {
  const n = 300;
  const nodes = Array.from({ length: n }, (_, i) => ({ path: `n${i}.md` }));
  const edges = Array.from({ length: n }, (_, i) => ({ source: `n${i}.md`, target: `n${(i + 1) % n}.md` }));
  const start = Date.now();
  const positions = renderer.layoutGraph({ nodes, edges }, { width: 600, height: 600 });
  const elapsedMs = Date.now() - start;
  assert.equal(Object.keys(positions).length, n);
  // Generous ceiling (this project's own benchmark measured ~60ms here; the plain-JS fallback
  // this replaced measured ~400ms at the same size) -- catches a regression back to O(n^2)
  // without being flaky about exact timing.
  assert.ok(elapsedMs < 2000, `expected WASM-accelerated layout well under 2s for ${n} nodes, took ${elapsedMs}ms`);
});

// Graph *rendering* moved from renderGraphSvg (pure, string-returning, testable) to
// renderGraphPixi (imperative PixiJS/WebGL canvas drawing -- needs a real browser canvas context,
// so it's untested here just like wireGraph/wireSearch/wireSlidingWindow already are). Only the
// pure color-conversion helper it depends on is unit-tested.

test("cssColorToPixiHex converts a #rrggbb hex color to a 0xRRGGBB integer", () => {
  assert.equal(renderer.cssColorToPixiHex("#8ab4f8"), 0x8ab4f8);
});

test("cssColorToPixiHex converts an rgb()/rgba() color to a 0xRRGGBB integer, ignoring alpha", () => {
  assert.equal(renderer.cssColorToPixiHex("rgba(48, 54, 61, 0.8)"), 0x30363d);
  assert.equal(renderer.cssColorToPixiHex("rgb(255, 0, 128)"), 0xff0080);
});

test("cssColorToPixiHex falls back to a neutral gray for an unparsed value", () => {
  assert.equal(renderer.cssColorToPixiHex(""), 0x888888);
  assert.equal(renderer.cssColorToPixiHex("not-a-color"), 0x888888);
});

// ─── Phase 5: navigation sidebar + sliding window (28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up) ──

test("resolveNoteTargetPath finds a cache entry matching the literal or .md-suffixed target", () => {
  const cache = { "Note.md": { searchText: "" } };
  assert.equal(renderer.resolveNoteTargetPath("Note", cache), "Note.md");
  assert.equal(renderer.resolveNoteTargetPath("Note.md", cache), "Note.md");
});

test("resolveNoteTargetPath returns null for an unresolved target or a null cache entry", () => {
  const cache = { "Note.md": { searchText: "" }, "publish.js": null };
  assert.equal(renderer.resolveNoteTargetPath("Missing", cache), null);
  assert.equal(renderer.resolveNoteTargetPath("publish.js", cache), null);
});

test("renderNotePaneHtml renders a pane's title and body from raw markdown", () => {
  const cache = { "Note.md": {} };
  const html = renderer.renderNotePaneHtml("Note.md", cache, "# Note\n\nHello.\n", "/publish/alice/vault1/");
  assert.equal(html, '<h1 class="note-title">Note</h1><h1 id="note">Note</h1>\n<p>Hello.</p>');
});

test("renderNotePaneHtml strips frontmatter using the cache entry's frontmatterPos", () => {
  const raw = "---\ntitle: x\n---\nBody.\n";
  const cache = { "Note.md": { frontmatterPos: [0, 0, 0, 2, 3, 16] } };
  const html = renderer.renderNotePaneHtml("Note.md", cache, raw, "/publish/alice/vault1/");
  assert.doesNotMatch(html, /title: x/);
  assert.match(html, /Body\./);
});
