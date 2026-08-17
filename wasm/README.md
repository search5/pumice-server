# wasm/

Source for the graph view's WASM-accelerated physics (see
`38_실제_아키텍처_전환_검색_그래프_실제화.md` and its follow-up in the pumice repo). Real
Obsidian Publish's own graph runs its force simulation as a compiled WASM module (confirmed by
reading `sim.js` directly) rather than plain JS -- `graph_forces.rs` is this project's own
from-scratch reimplementation of the same idea (Barnes-Hut quadtree approximation for the
many-body repulsion force), not a disassembly of Obsidian's actual binary.

This is **not** part of the normal dev/test/deploy flow -- there's no Rust toolchain wired into
`uv run pytest`/`node --test`/CI. It's compiled once, out of band, and the resulting `.wasm`
bytes are base64-embedded directly into `src/pumice_server/static/publish_render.js` (next to
`layoutGraph`, which calls into it) -- same reasoning real Obsidian's own bundle embeds its WASM
inline rather than a separate fetch: it lets the module be instantiated synchronously, no `await`
needed, so `layoutGraph` can stay a plain synchronous function callable from both the browser and
Node's `node --test`.

## Rebuilding

Requires `rustup target add wasm32-unknown-unknown` (one-time, if not already installed):

```sh
rustc --target wasm32-unknown-unknown \
  -C opt-level=z -C lto=fat -C panic=abort -C strip=symbols -C codegen-units=1 \
  --crate-type=cdylib \
  -o graph_forces.wasm graph_forces.rs

python3 -c "
import base64
print(base64.b64encode(open('graph_forces.wasm', 'rb').read()).decode('ascii'))
" > graph_forces.b64.txt
```

Then paste `graph_forces.b64.txt`'s contents into `GRAPH_FORCES_WASM_B64` near the top of
`static/publish_render.js`. If WASM instantiation fails for any reason (unsupported browser,
corrupted embed), `layoutGraph` falls back to its original plain-JS O(n^2) repulsion loop --
correct either way, just slower for large graphs (see the follow-up doc's benchmark numbers).
