/*
 * Graph-view physics worker -- see 38_실제_아키텍처_전환_검색_그래프_실제화.md (pumice repo).
 *
 * Real Obsidian Publish runs its own graph's force-directed layout in a dedicated
 * `new Worker("/sim.js", {name: "Graph Worker"})` rather than on the main thread (confirmed by
 * reading app.js directly). This mirrors that: reuses publish_render.js's own already-tested,
 * pure `layoutGraph` (via importScripts, so the algorithm has one source of truth instead of a
 * second copy that could drift) and just adds the postMessage plumbing around it.
 */
importScripts("publish_render.js");

self.onmessage = function (e) {
  var positions = self.PumicePublishRenderer.layoutGraph(e.data.graph, e.data.opts);
  self.postMessage(positions);
};
