// TDD for static/publish_render.js's pure functions -- see
// 28_실제_아키텍처_전환_Phase1_노트본문.md (pumice repo). No build tooling/npm deps in this repo
// (same as static/dashboard.js and static/login.js), so this uses Node's built-in test runner
// directly against the same file the browser loads, run as CommonJS (see the module.exports
// guard at the bottom of publish_render.js).
//
// Run with: node --test tests/publish_render.test.cjs

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

test("renderMarkdown resolves a wikilink to a permalink from the cache", () => {
  const cache = { "Target": { permalink: "/nice-url/" } };
  const html = renderer.renderMarkdown("See [[Target|Custom Label]].\n", cache, "/publish/alice/vault1/");
  assert.equal(html, '<p>See <a href="/publish/alice/vault1/nice-url/" class="wiki-link">Custom Label</a>.</p>');
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

test("buildOutline turns a cache entry's headings into a slugged, ordered outline", () => {
  const entry = { headings: [{ heading: "Intro", level: 1 }, { heading: "Sub Point", level: 2 }] };
  assert.deepEqual(renderer.buildOutline(entry), [
    { level: 1, heading: "Intro", slug: "intro" },
    { level: 2, heading: "Sub Point", slug: "sub-point" },
  ]);
});

test("buildOutline de-duplicates identical heading text the same way as renderMarkdown", () => {
  const entry = { headings: [{ heading: "Same", level: 1 }, { heading: "Same", level: 1 }] };
  assert.deepEqual(renderer.buildOutline(entry).map((o) => o.slug), ["same", "same-1"]);
});

test("buildOutline returns an empty list for a note with no headings", () => {
  assert.deepEqual(renderer.buildOutline({ headings: [] }), []);
  assert.deepEqual(renderer.buildOutline(null), []);
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

// ─── Phase 3: search (28_실제_아키텍처_전환_Phase1_노트본문.md's Phase 3 follow-up) ──────────────

test("buildSearchIndex includes only markdown entries, deriving a filename-based title", () => {
  const cache = {
    "Notes/Deep Dive.md": { searchText: "some body text" },
    "publish.js": null,
    "image.png": null,
  };
  const index = renderer.buildSearchIndex(cache, "/publish/alice/vault1/");
  assert.deepEqual(index, [
    { path: "Notes/Deep Dive.md", title: "Deep Dive", href: "/publish/alice/vault1/Notes/Deep%20Dive", searchText: "some body text" },
  ]);
});

test("buildSearchIndex resolves a permalink into the entry's href when present", () => {
  const cache = { "Note.md": { searchText: "", permalink: "/nice/" } };
  const index = renderer.buildSearchIndex(cache, "/publish/alice/vault1/");
  assert.equal(index[0].href, "/publish/alice/vault1/nice/");
});

test("searchNotes returns nothing for an empty or whitespace-only query", () => {
  const index = [{ path: "Note.md", title: "Note", href: "/x", searchText: "hello" }];
  assert.deepEqual(renderer.searchNotes(index, ""), []);
  assert.deepEqual(renderer.searchNotes(index, "   "), []);
});

test("searchNotes matches case-insensitively against the title", () => {
  const index = [{ path: "Note.md", title: "Cooking Tips", href: "/x", searchText: "" }];
  assert.equal(renderer.searchNotes(index, "cooking").length, 1);
});

test("searchNotes matches against searchText and includes a snippet around the match", () => {
  const index = [{ path: "Note.md", title: "Note", href: "/x", searchText: "the quick brown fox jumps" }];
  const results = renderer.searchNotes(index, "brown");
  assert.equal(results.length, 1);
  assert.match(results[0].snippet, /brown/);
});

test("searchNotes excludes non-matching entries", () => {
  const index = [
    { path: "A.md", title: "Apples", href: "/a", searchText: "fruit" },
    { path: "B.md", title: "Bicycles", href: "/b", searchText: "wheels" },
  ];
  const results = renderer.searchNotes(index, "fruit");
  assert.deepEqual(results.map((r) => r.path), ["A.md"]);
});

test("searchNotes caps the number of results", () => {
  const index = Array.from({ length: 30 }, (_, i) => ({ path: `N${i}.md`, title: `Note ${i}`, href: `/${i}`, searchText: "match" }));
  const results = renderer.searchNotes(index, "match");
  assert.ok(results.length <= 20);
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

test("renderGraphSvg emits a line per edge and a linked circle per node, highlighting the current node", () => {
  const graph = { nodes: [{ path: "A.md", title: "A" }, { path: "B.md", title: "B" }], edges: [{ source: "A.md", target: "B.md" }] };
  const positions = { "A.md": { x: 10, y: 20 }, "B.md": { x: 30, y: 40 } };
  const svg = renderer.renderGraphSvg(graph, positions, {}, "/publish/alice/vault1/", "A.md", 100, 100);
  assert.match(svg, /<line x1="10" y1="20" x2="30" y2="40"/);
  assert.match(svg, /class="graph-node graph-node-current"/);
  assert.match(svg, /href="\/publish\/alice\/vault1\/A"/);
  assert.match(svg, /href="\/publish\/alice\/vault1\/B"/);
});

test("renderGraphSvg resolves node hrefs through the cache's permalinks", () => {
  const graph = { nodes: [{ path: "A.md", title: "A" }], edges: [] };
  const positions = { "A.md": { x: 0, y: 0 } };
  const cache = { "A.md": { permalink: "/nice/" } };
  const svg = renderer.renderGraphSvg(graph, positions, cache, "/publish/alice/vault1/", null, 50, 50);
  assert.match(svg, /href="\/publish\/alice\/vault1\/nice\/"/);
});
