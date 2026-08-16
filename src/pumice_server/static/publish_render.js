/*
 * Client-side note renderer for pumice-server's Publish pages -- see
 * 27_실제_퍼블리시_사이트_아키텍처_조사.md / 28_실제_아키텍처_전환_Phase1_노트본문.md (pumice repo).
 *
 * Real Obsidian Publish serves raw markdown + a whole-vault metadata cache and renders client-
 * side (confirmed live via curl against publish-01.obsidian.md). This file is pumice-server's own
 * from-scratch reimplementation of that idea -- not a copy of Obsidian's app.js, which was never
 * read, only observed from the outside (network payloads/content-types). It targets the same
 * markdown subset python-markdown's `extra`/`codehilite`/`toc` extensions already produce
 * server-side (see web.py's _render_publish_page), not full CommonMark compliance.
 *
 * Plain vanilla JS, no build step, no dependencies -- same convention as the existing
 * static/dashboard.js and static/login.js. Loaded directly via <script defer src="/static/
 * publish_render.js"> and also loadable under Node's `node:test` (see
 * tests/publish_render.test.mjs) via the CommonJS export guard at the bottom.
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

  /** Note outline (Phase 2 of 28_실제_아키텍처_전환_Phase1_노트본문.md) -- just the current
   * note's own cache entry's `headings`, slugged to match the ids renderMarkdown puts on the
   * actual rendered `<hN>` elements so outline links can jump to them. */
  function buildOutline(cacheEntry) {
    if (!cacheEntry || !cacheEntry.headings) return [];
    var slugger = makeSlugger();
    return cacheEntry.headings.map(function (h) {
      return { level: h.level, heading: h.heading, slug: slugger(h.heading) };
    });
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
  var SEARCH_SNIPPET_CONTEXT_CHARS = 40;

  function titleFromPath(path) {
    var base = path.indexOf("/") === -1 ? path : path.slice(path.lastIndexOf("/") + 1);
    return /\.md$/i.test(base) ? base.slice(0, -3) : base;
  }

  /** Search index (Phase 3, 28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up) -- one entry
   * per markdown file in the already-fetched `_cache.json`, using the `searchText` field the
   * server computed at upload time (see metadata_cache.py's _plain_text_for_search -- this is
   * pumice's own design, not a confirmed real-Obsidian search wire format: 27_...md's curl sweep
   * only found /options, /cache and /access, no dedicated search endpoint). */
  function buildSearchIndex(cache, urlPrefix) {
    var index = [];
    Object.keys(cache).forEach(function (path) {
      var entry = cache[path];
      if (!entry || !/\.md$/i.test(path)) return;
      index.push({
        path: path,
        title: titleFromPath(path),
        href: resolveWikilinkHref(path, cache, urlPrefix),
        searchText: entry.searchText || "",
      });
    });
    return index;
  }

  function extractSnippet(text, query, contextChars) {
    var idx = text.toLowerCase().indexOf(query.toLowerCase());
    if (idx === -1) return text.slice(0, contextChars * 2);
    var start = Math.max(0, idx - contextChars);
    var end = Math.min(text.length, idx + query.length + contextChars);
    return (start > 0 ? "..." : "") + text.slice(start, end) + (end < text.length ? "..." : "");
  }

  /** Case-insensitive substring match against title or searchText, capped at
   * SEARCH_RESULT_LIMIT results (own reasonable default -- a dropdown, not a full results
   * page) in index order. */
  function searchNotes(index, query) {
    var trimmed = (query || "").trim();
    if (!trimmed) return [];
    var lower = trimmed.toLowerCase();
    var results = [];
    for (var i = 0; i < index.length && results.length < SEARCH_RESULT_LIMIT; i++) {
      var item = index[i];
      var titleMatch = item.title.toLowerCase().indexOf(lower) !== -1;
      var textMatch = item.searchText.toLowerCase().indexOf(lower) !== -1;
      if (!titleMatch && !textMatch) continue;
      results.push({
        path: item.path, title: item.title, href: item.href,
        snippet: textMatch ? extractSnippet(item.searchText, trimmed, SEARCH_SNIPPET_CONTEXT_CHARS) : "",
      });
    }
    return results;
  }

  /** Graph view (Phase 4, 28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up) -- like search
   * (Phase 3), real Obsidian's own graph wire format is unconfirmed (27_...md's curl sweep only
   * found /options, /cache, /access), so this is pumice's own design built from the same
   * whole-vault `_cache.json` already fetched. Scope: only markdown files that are actual nodes
   * in this vault's cache, only links that resolve to another such file (an unresolved/external
   * link target doesn't get a node) -- matches buildBacklinks' own resolved-links-only scope. A
   * link that appears more than once, or in both directions between the same two files, collapses
   * into a single undirected edge (the graph is for showing connectivity, not link direction/
   * count). */
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
   * of oscillating forever. Returns {[path]: {x, y}}. */
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

  /** Renders a laid-out graph as an inline SVG string -- each node is a clickable, permalink-
   * resolved <a><circle></a> (reusing resolveWikilinkHref, same as every other cross-note link
   * this renderer produces), the current note's node gets a distinguishing class. */
  function renderGraphSvg(graph, positions, cache, urlPrefix, currentPath, width, height) {
    width = width || 600;
    height = height || 600;
    var edgesSvg = graph.edges.map(function (edge) {
      var s = positions[edge.source], t = positions[edge.target];
      if (!s || !t) return "";
      return '<line x1="' + s.x + '" y1="' + s.y + '" x2="' + t.x + '" y2="' + t.y + '" class="graph-edge"></line>';
    }).join("");
    var nodesSvg = graph.nodes.map(function (node) {
      var p = positions[node.path];
      if (!p) return "";
      var href = resolveWikilinkHref(node.path, cache, urlPrefix);
      var isCurrent = node.path === currentPath;
      var cls = "graph-node" + (isCurrent ? " graph-node-current" : "");
      var r = isCurrent ? 7 : 5;
      return '<a href="' + escapeAttr(href) + '"><circle cx="' + p.x + '" cy="' + p.y + '" r="' + r + '" class="' + cls + '"><title>' + escapeHtml(node.title) + "</title></circle></a>";
    }).join("");
    return '<svg viewBox="0 0 ' + width + " " + height + '" xmlns="http://www.w3.org/2000/svg">' + edgesSvg + nodesSvg + "</svg>";
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
      return '<a href="' + escapeAttr(href) + '" class="wiki-link">' + escapeHtml(label) + "</a>";
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

  function wireSearch(inputEl, resultsEl, cache, urlPrefix) {
    var index = buildSearchIndex(cache, urlPrefix);
    inputEl.addEventListener("input", function () {
      var results = searchNotes(index, inputEl.value);
      resultsEl.innerHTML = results.map(function (r) {
        var snippetHtml = r.snippet ? '<div class="search-result-snippet">' + escapeHtml(r.snippet) + "</div>" : "";
        return '<a href="' + escapeAttr(r.href) + '"><div class="search-result-title">' + escapeHtml(r.title) + "</div>" + snippetHtml + "</a>";
      }).join("");
    });
  }

  /** Wires the graph-view toggle button/overlay -- the layout is computed lazily on first open
   * (force-directed layout over a whole vault isn't free, no reason to pay for it on every page
   * load if the visitor never opens the graph) and cached for subsequent opens. */
  function wireGraph(toggleEl, closeEl, overlayEl, containerEl, cache, urlPrefix, currentPath) {
    var rendered = false;
    toggleEl.addEventListener("click", function () {
      if (!rendered) {
        var graph = buildGraph(cache);
        var rect = containerEl.getBoundingClientRect();
        var size = Math.max(200, Math.min(rect.width || 600, rect.height || 600));
        var positions = layoutGraph(graph, { width: size, height: size });
        containerEl.innerHTML = renderGraphSvg(graph, positions, cache, urlPrefix, currentPath, size, size);
        rendered = true;
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
        root.innerHTML = renderMarkdown(body, cache, preload.url_prefix);

        var outlineEl = document.getElementById("pumice-outline");
        if (outlineEl) {
          var outline = buildOutline(entry);
          outlineEl.innerHTML = outline.length
            ? "<ul>" + outline.map(function (o) {
                return '<li class="outline-level-' + o.level + '"><a href="#' + escapeAttr(o.slug) + '">' + escapeHtml(o.heading) + "</a></li>";
              }).join("") + "</ul>"
            : "";
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
    linkTargetMatchesPath: linkTargetMatchesPath,
    buildBacklinks: buildBacklinks,
    buildSearchIndex: buildSearchIndex,
    searchNotes: searchNotes,
    buildGraph: buildGraph,
    layoutGraph: layoutGraph,
    renderGraphSvg: renderGraphSvg,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    window.PumicePublishRenderer = api;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", main);
    } else {
      main();
    }
  }
})();
