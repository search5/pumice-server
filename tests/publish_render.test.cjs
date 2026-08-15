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

test("renderMarkdown renders headings and a paragraph", () => {
  const html = renderer.renderMarkdown("# Title\n\nHello world.\n", {}, "/publish/alice/vault1/");
  assert.equal(html, "<h1>Title</h1>\n<p>Hello world.</p>");
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
