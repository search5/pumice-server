// Barnes-Hut many-body repulsion for pumice-server's graph view -- see
// 38_실제_아키텍처_전환_검색_그래프_실제화.md / 39_...(follow-up, pumice repo). Own from-scratch
// implementation (never read real Obsidian's compiled WASM bytes, only confirmed via app.js/
// sim.js's *surrounding JS* that it exports functions named init/simulate/complete/manyBody/
// visitCharge/visitCollide and uses a quadtree -- the actual algorithm here is the standard,
// widely-published Barnes-Hut approximation, not a disassembly of their binary).
//
// Quadtree built with an index-based arena (Vec<QuadNode>) instead of raw pointers/Box, so the
// whole thing stays in safe Rust except for the raw-pointer JS<->WASM boundary at the bottom.
// Compiled with: rustc --target wasm32-unknown-unknown -C opt-level=z -C lto=fat -C panic=abort
// -C strip=symbols -C codegen-units=1 --crate-type=cdylib

struct QuadNode {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    children: [i32; 4], // -1 = no child
    is_leaf: bool,
    point_index: i32, // valid only when is_leaf && count == 1 (the one point living here)
    count: f64,        // number of points aggregated under this node
    cx: f64,
    cy: f64, // center of mass of the aggregated points
}

const MAX_DEPTH: u32 = 32;
const EPS: f64 = 0.01;

fn quadrant_of(node: &QuadNode, x: f64, y: f64) -> usize {
    let mx = (node.x0 + node.x1) / 2.0;
    let my = (node.y0 + node.y1) / 2.0;
    let right = x >= mx;
    let bottom = y >= my;
    ((bottom as usize) << 1) | (right as usize)
}

fn child_bounds(node: &QuadNode, quadrant: usize) -> (f64, f64, f64, f64) {
    let mx = (node.x0 + node.x1) / 2.0;
    let my = (node.y0 + node.y1) / 2.0;
    let right = quadrant & 1 == 1;
    let bottom = quadrant & 2 == 2;
    (
        if right { mx } else { node.x0 },
        if bottom { my } else { node.y0 },
        if right { node.x1 } else { mx },
        if bottom { node.y1 } else { my },
    )
}

fn new_node(x0: f64, y0: f64, x1: f64, y1: f64) -> QuadNode {
    QuadNode { x0, y0, x1, y1, children: [-1, -1, -1, -1], is_leaf: true, point_index: -1, count: 0.0, cx: 0.0, cy: 0.0 }
}

/// Inserts point `pi` (at `px`,`py`) into the subtree rooted at `nodes[idx]`, updating every
/// ancestor's running (count, center-of-mass) as it descends -- the standard incremental
/// Barnes-Hut build, so no separate bottom-up aggregation pass is needed afterward.
fn insert(nodes: &mut Vec<QuadNode>, idx: usize, pi: i32, px: f64, py: f64, depth: u32) {
    if nodes[idx].count == 0.0 {
        nodes[idx].point_index = pi;
        nodes[idx].count = 1.0;
        nodes[idx].cx = px;
        nodes[idx].cy = py;
        return;
    }

    if depth >= MAX_DEPTH {
        // Degenerate cluster (near-identical coordinates) -- stop subdividing, just fold this
        // point into the aggregate. Slightly less precise, never incorrect: it's exactly what
        // Barnes-Hut already does for any node treated as "one far-away blob".
        let new_count = nodes[idx].count + 1.0;
        nodes[idx].cx = (nodes[idx].cx * nodes[idx].count + px) / new_count;
        nodes[idx].cy = (nodes[idx].cy * nodes[idx].count + py) / new_count;
        nodes[idx].count = new_count;
        return;
    }

    if nodes[idx].is_leaf {
        // Split: this leaf currently holds exactly one point -- push it down into a child
        // before also inserting the new one.
        let (ex_pi, ex_x, ex_y) = (nodes[idx].point_index, nodes[idx].cx, nodes[idx].cy);
        nodes[idx].is_leaf = false;
        nodes[idx].point_index = -1;
        let q = quadrant_of(&nodes[idx], ex_x, ex_y);
        let (cx0, cy0, cx1, cy1) = child_bounds(&nodes[idx], q);
        nodes.push(new_node(cx0, cy0, cx1, cy1));
        let child_idx = nodes.len() - 1;
        nodes[idx].children[q] = child_idx as i32;
        insert(nodes, child_idx, ex_pi, ex_x, ex_y, depth + 1);
    }

    let new_count = nodes[idx].count + 1.0;
    nodes[idx].cx = (nodes[idx].cx * nodes[idx].count + px) / new_count;
    nodes[idx].cy = (nodes[idx].cy * nodes[idx].count + py) / new_count;
    nodes[idx].count = new_count;

    let q = quadrant_of(&nodes[idx], px, py);
    if nodes[idx].children[q] == -1 {
        let (cx0, cy0, cx1, cy1) = child_bounds(&nodes[idx], q);
        nodes.push(new_node(cx0, cy0, cx1, cy1));
        let child_idx = nodes.len() - 1;
        nodes[idx].children[q] = child_idx as i32;
    }
    let child_idx = nodes[idx].children[q] as usize;
    insert(nodes, child_idx, pi, px, py, depth + 1);
}

/// Accumulates the repulsive displacement point `pi` (at `px`,`py`) receives from the subtree
/// rooted at `nodes[idx]`, using the same `count * k*k / dist` law (and the same direction
/// convention -- pushed away from the source) as this project's existing plain-JS layoutGraph,
/// just approximated via Barnes-Hut: a node is treated as one aggregated source at its center of
/// mass whenever its size/distance ratio is under `theta`, instead of visiting every point
/// individually (that's what turns this from O(n^2) into ~O(n log n)).
fn accumulate_force(nodes: &Vec<QuadNode>, idx: usize, pi: i32, px: f64, py: f64, theta: f64, k: f64) -> (f64, f64) {
    let node = &nodes[idx];
    if node.count == 0.0 {
        return (0.0, 0.0);
    }
    if node.is_leaf {
        if node.count == 1.0 && node.point_index == pi {
            return (0.0, 0.0);
        }
        let dx = px - node.cx;
        let dy = py - node.cy;
        let dist = (dx * dx + dy * dy).sqrt().max(EPS);
        let force = node.count * k * k / dist;
        return (dx / dist * force, dy / dist * force);
    }

    let dx = px - node.cx;
    let dy = py - node.cy;
    let dist = (dx * dx + dy * dy).sqrt().max(EPS);
    let size = node.x1 - node.x0;
    if size / dist < theta {
        let force = node.count * k * k / dist;
        return (dx / dist * force, dy / dist * force);
    }

    let mut fx = 0.0;
    let mut fy = 0.0;
    for &c in node.children.iter() {
        if c != -1 {
            let (cfx, cfy) = accumulate_force(nodes, c as usize, pi, px, py, theta, k);
            fx += cfx;
            fy += cfy;
        }
    }
    (fx, fy)
}

fn build_tree(xs: &[f64], ys: &[f64]) -> Vec<QuadNode> {
    let n = xs.len();
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        if xs[i] < x0 { x0 = xs[i]; }
        if xs[i] > x1 { x1 = xs[i]; }
        if ys[i] < y0 { y0 = ys[i]; }
        if ys[i] > y1 { y1 = ys[i]; }
    }
    if !x0.is_finite() { x0 = 0.0; x1 = 1.0; y0 = 0.0; y1 = 1.0; }
    // Barnes-Hut needs a square bounding box (quadrant splits must be symmetric) with a little
    // padding so a point exactly on the boundary still lands inside a quadrant.
    let span = (x1 - x0).max(y1 - y0).max(1.0) * 1.05;
    let cx = (x0 + x1) / 2.0;
    let cy = (y0 + y1) / 2.0;
    let half = span / 2.0;

    let mut nodes = Vec::with_capacity(n * 2);
    nodes.push(new_node(cx - half, cy - half, cx + half, cy + half));
    for i in 0..n {
        insert(&mut nodes, 0, i as i32, xs[i], ys[i], 0);
    }
    nodes
}

#[no_mangle]
pub extern "C" fn alloc_f64(n: usize) -> *mut f64 {
    let mut v: Vec<f64> = vec![0.0; n];
    let ptr = v.as_mut_ptr();
    std::mem::forget(v);
    ptr
}

#[no_mangle]
pub extern "C" fn dealloc_f64(ptr: *mut f64, n: usize) {
    unsafe {
        drop(Vec::from_raw_parts(ptr, n, n));
    }
}

/// xs/ys: n point positions. theta: Barnes-Hut approximation threshold (smaller = more accurate,
/// slower; d3-force's own default is 0.9, this project's JS fallback has no theta concept since
/// it's exact O(n^2) -- 0.9 is a reasonable, widely-used default here too). k: same repulsion
/// constant layoutGraph already computes (sqrt(width*height/n)). Writes each point's net
/// repulsive displacement into out_fx/out_fy (caller-allocated, via alloc_f64).
#[no_mangle]
pub extern "C" fn many_body_forces(
    xs_ptr: *const f64, ys_ptr: *const f64, n: usize,
    theta: f64, k: f64,
    out_fx_ptr: *mut f64, out_fy_ptr: *mut f64,
) {
    let xs = unsafe { std::slice::from_raw_parts(xs_ptr, n) };
    let ys = unsafe { std::slice::from_raw_parts(ys_ptr, n) };
    let out_fx = unsafe { std::slice::from_raw_parts_mut(out_fx_ptr, n) };
    let out_fy = unsafe { std::slice::from_raw_parts_mut(out_fy_ptr, n) };

    if n == 0 {
        return;
    }
    let nodes = build_tree(xs, ys);
    for i in 0..n {
        let (fx, fy) = accumulate_force(&nodes, 0, i as i32, xs[i], ys[i], theta, k);
        out_fx[i] = fx;
        out_fy[i] = fy;
    }
}
