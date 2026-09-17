# 24 — Connectivity: graph-based power, cooling and network topology

Plan for the `/connectivity` page. Written 2026-09-16 against the working
tree. Supersedes `12-frontend-spec.md` §3.4, which specified a generic
node-link view laid out by `dagre` / `cola` / `fcose`; §7 of this document
says why that recommendation is withdrawn.

---

## 1. What exists today (verified)

| Piece | Where | State |
|---|---|---|
| `connection` table, one graph per `layer_t` | `alembic/versions/0001_baseline.py:246` | Built. `redundancy_side`, `oper_state`, polymorphic a/b terminations |
| Per-layer graph query, recursive CTE, bidirectional walk | `backend/app/repositories/topology.py` | Built. Node cap 2000, degree-ordered truncation, `graph_version` stamp |
| Topology service + 30 s Redis cache | `backend/app/services/topology.py` | Built |
| `GET /topology`, `GET /topology/impact/{id}` | `backend/app/api/v1/topology.py` | Built |
| Impact analysis (cut-off vs degraded, per layer) | `backend/app/services/impact.py` | Built, correct, and **not reachable from the UI** |
| Upstream direction, one definition | `backend/app/core/layers.py` | Built |
| Layered SVG view, structure-memoised layout | `frontend/src/features/topology/{TopologyView.tsx,layout.ts}` | Built, reachable only at `/topology`, absent from the nav |
| Link alarm correlation across a cable | `backend/app/alarms/link_correlation.py` | Built |

Reference fixture scale (`docs/16` §3.1): **664 devices, 2566 edges** —
power 1080, management 644, production 436, cooling 356, fieldbus 50. The
live estate is larger (two halls per DC, ~1560 servers/DC in the fleet
build) but the same order: low thousands of nodes, not hundreds of
thousands. **This single fact decides §7.**

### 1.1 Three defects found while surveying

1. **`/connectivity` is mis-wired.** `App.tsx:214` routes it to
   `<PlatformHealth />` — the *monitoring-health* page, whose own docstring
   says it exists to tell a dead collector from a quiet datacenter. The nav
   promises connectivity and delivers collector lag. The real topology view
   sits at `/topology`, which nothing links to.
2. **The A/B side colours are raw hex and are the exact colliding pair the
   chart rulebook names.** `index.css:430` `.topo-edge.side-a { stroke:
   #3b82f6 }` and `:431` `.side-b { stroke: #a855f7 }` — two call-site
   hexes in a codebase whose rule is tokens only, and per `CLAUDE.md` that
   pair is ~0.6 CIEDE2000 apart to a deuteranope, i.e. one colour. A-side
   versus B-side is a *safety* distinction; it must never be hue-only.
3. **`api.impact` is not in `frontend/src/api/client.ts`.** Only
   `api.topology` is. The most operationally valuable endpoint in the
   subsystem has no caller.

These are fixed as part of Phase 0 (§9), not deferred.

---

## 2. How real datacenters actually draw this

Required by `CLAUDE.md`: state the production practice before designing.
The headline conclusion is that **"a graph view with a layer dropdown" is
the wrong primary experience**, because the three domains do not share a
diagram grammar.

### 2.1 Power — a one-line (single-line) diagram, not a graph

Industry practice, near-universal: utility feed → switchgear → ATS (with
generator on the alternate inlet) → UPS → RPP/panelboard → rack PDU →
PSU. Conventions an electrical engineer expects, and without which the
diagram gets rejected:

* **Source at the top, load at the bottom.** Reading order *is* the diagram.
* **A-side and B-side drawn as two parallel columns** that converge only at
  a dual-corded load. Sunbird states this explicitly: "the power source is
  displayed at the top of the diagram so that the power path can easily be
  followed downstream from node to node and redundant power paths can be
  visualized side-by-side."
* **Ratings on the line.** Each node prints actual against rated — breaker
  amps, UPS kW, phase balance. A one-line without capacity is a picture.
* Protective devices (breakers) appear *in* the line.

Data and protocols behind it, all of which this platform already collects:
rack PDU and RPP per-outlet current over SNMP (Raritan/APC/Vertiv MIBs);
UPS over SNMP (RFC 1628 `upsMIB` plus vendor extensions — Eaton, Vertiv,
Schneider); switchgear, ATS and energy monitors over Modbus/TCP;
BMS-fronted gear over BACnet/IP. Vendor-dependent: per-outlet metering is
standard on intelligent strips and absent on basic ones — the UI must
render "not metered" rather than zero.

**Gap to state honestly:** there is no breaker or panelboard entity in the
schema. `outlet.rated_amps`, `outlet.phase`, `outlet.branch`,
`power_supply.rated_watts` and `device.rated_power_w` exist
(`0001_baseline.py:106,139,228-243`); breakers do not. A production SLD
shows them. We will print the outlet/branch rating we have and label the
level of detail, rather than draw a breaker symbol the data does not
support. Modelling breakers is a separate data-model change (§11).

### 2.2 Cooling — a loop schematic, closer to a simplified P&ID

Chilled-water plant is a **cycle**, not a tree: cooling tower → condenser
loop → chiller → primary pumps → secondary pumps → CHW supply header →
CRAH / CDU → room → CHW return header → back to the chiller. Supply and
return are two distinct paths between the same endpoints, and flow
direction is the content of the drawing. Liquid-cooled halls add the CDU's
secondary loop (TCS) and its heat exchanger as a nested loop.

A generic layered layout is actively wrong here: `layout.ts:rankNodes`
bounds cycles so it terminates, but it still renders a loop as a ladder
with the return leg pointing back up the page. Cooling needs a **fixed
stage template** — the plant stages are a known, small, ordered set — not a
general-purpose graph layout.

Protocols: chillers, CRAH and BMS over BACnet/IP (vendor objects differ —
Carrier, Trane, Daikin and Vertiv expose different point names); pumps,
valves, CDUs and plant instruments over Modbus/TCP, here behind the Moxa
gateways the simulator models. Flow, ΔT, valve position and pump speed are
the points that make the schematic live.

### 2.3 Network — genuinely a graph, drawn by tier

Production fabric is Clos: core → spine → leaf → ToR → host, with
MLAG/dual-homed pairs at the ToR. Management is a separate, simpler tree:
every device's BMC/console to an OOB switch to an OOBM aggregation.
Fieldbus is a star: one gateway fronting many field devices.

All three are *layered* graphs with a natural rank, which is precisely what
the existing `layout.ts` computes. Real discovery is LLDP/CDP; here it is
`interface`-to-`interface` connections from the simulator export, which is
the only layer where both terminations are ports (`docs/16` §3.1, finding
A3).

### 2.4 Consequence for the design

**Three diagram grammars, one connection table, one page:**

| Layer | Renderer | Why |
|---|---|---|
| `power` | One-line diagram (A/B columns, source→load, ratings on node) | Industry convention; engineers will not accept anything else |
| `cooling` | Loop schematic on a fixed stage template, flow arrows, supply/return distinguished | It is a cycle, not a tree |
| `network` / `management` / `fieldbus` | Layered (tier) graph — the existing renderer, improved | They are layered DAG-ish graphs, and a tier diagram is how fabric is drawn |

---

## 3. UX research — what to borrow, what to reject

Sources in §13.

### 3.1 Borrow

* **Auto-generated, always-correct diagrams.** Sunbird's stated value is
  that the SLD is generated from asset and circuit data and updates itself
  on any change. We already derive from `connection`; never allow a
  hand-placed diagram to exist alongside it.
* **Hop-level detail at every connection.** Sunbird surfaces item class,
  make, model, location, cabinet position, connector type, phase and
  amperage per hop. Our `Termination` already carries type, id and a label
  built from outlet number + connector; extend with rated amps and phase.
* **Tabular twin of every diagram.** Sunbird ships "2D, 3D, and tabular
  circuit visualization"; NetBox's cable trace is fundamentally a rendered
  path plus a status verdict — "Trace Completed", "Path split",
  "Asymmetric Path". The table is what gets pasted into a change ticket,
  and it is the accessibility story (§8.2).
* **Semantic zoom, in discrete levels.** The literature is consistent that
  semantic zoom — changing *what* is drawn, not just its size — beats
  geometric zoom for large structures. Three levels, tied to scope:
  datacenter → room/plant blocks; room → devices with the leaf tier rolled
  up; rack/device → ports and terminations.
* **Focus+context by dimming, on selection.** Selecting a node dims
  everything not on its path. Focus+context keeps the focus inside the
  context rather than in a separate pane.
* **Aggregation over truncation.** The hairball literature's practical
  answer is hierarchical aggregation plus filtering, not faster rendering.
  A hall's power layer is ~800 loads; rolled up to racks it is ~60 nodes.
* **Parallel-edge collapse with a count.** Already implemented
  (`layout.ts:collapseEdges`) — seven conductors between a UPS and an RPP
  draw as one line labelled ×7. Keep it, and extend it to the SLD.
* **Never re-layout on a live update.** Already the code's stated design
  (`layout.ts` header) and `docs/12` §3.4's rule. Hold the line.

### 3.2 Reject, with reasons

* **3D.** Sunbird markets it. It answers no operational question the 2D
  view does not, costs a WebGL renderer and a camera model, and cannot be
  printed into a method statement. Out of scope, permanently.
* **Force-directed layout for power or cooling.** A force simulation
  destroys the reading order that *is* the one-line diagram, and it
  re-converges (and jitters) on every nudge, which makes "do not move on
  live update" a fight with the algorithm instead of a property of the
  design. `layout.ts` already argues this; it is right.
* **Continuous fisheye / distortion.** Disorienting when the operator is
  reading a chain they must then act on physically.
* **Edge bundling.** The standard answer to a hairball, but it *destroys
  individual path traceability*, which is this page's entire purpose.
  Bundling is for aggregate flow; we need "which cord, which outlet".
* **A minimap at room scope.** ~60–250 nodes after roll-up does not need
  one. Reconsider only at estate scope.
* **User-editable diagram positions.** Two sources of truth; the diagram
  goes stale the first time someone drags a box.

---

## 4. The page: what it is for

`/connectivity` answers three questions, and every decision below serves
one of them. They are the questions asked in a change-approval meeting and
at 3 a.m.

1. **Trace — "what feeds this, and what does it feed?"**
   Pick a device, see the chain to source and the chain to loads, on any
   layer, with terminations and ratings at every hop.
2. **Impact — "what breaks if I pull this?"**
   Already computed by `services/impact.py`, and the distinction it makes
   is the product: **cut off** (goes dark) versus **degraded** (loses a
   redundancy side, usually an accepted cost of a planned window). Wire it
   to the diagram: select a node, press *Simulate removal*, the cut-off set
   goes red on the diagram and lists in the drawer.
3. **Redundancy audit — "where is the redundancy not actually there?"**
   The one that earns the page, and that no current screen answers. Three
   findings, all common-mode failures a per-device view cannot see:
   * **Single-fed loads** — one cord on a device that should have two.
   * **Same-side dual-cording** — two cords, both landing on A.
   * **Upstream convergence** — two nominally independent sides that meet
     at a shared ancestor (both RPPs off one UPS). `impact.analyse` already
     computes reachability from sources with a node removed, which is
     exactly the primitive this needs.

A fourth, lower priority: **path between two devices** (`GET
/topology/path` is specified in `docs/10` §6 and not built) — "is this
server actually on the same power chain as that one?"

---

## 5. Information architecture

```
CONNECTIVITY                                                    [estate ▾]
──────────────────────────────────────────────────────────────────────────
POWER │ COOLING │ NETWORK │ MANAGEMENT │ FIELDBUS      ← layer strip (Seg)
Scope: [DC1 ▾] [Server Hall A ▾] [all racks ▾]   Depth 0/1/2   ⌕ find device
──────────────────────────────────────────────────────────────────────────
┌────────────────────────────────────────────────┬───────────────────────┐
│                                                │  DETAIL DRAWER        │
│   DIAGRAM                                      │  (selection-driven)   │
│   · power   → one-line, A | B columns          │                       │
│   · cooling → loop schematic                   │  Device               │
│   · others  → layered tier graph               │  Trace ▸ table        │
│                                                │  Impact ▸ cut/degrade │
│                                                │  Alarms on this chain │
├────────────────────────────────────────────────┴───────────────────────┤
│  DIAGRAM │ TRACE TABLE │ REDUNDANCY AUDIT          ← view tabs          │
│  847 devices rolled up to 61 racks · 1080 conductors as 214 lines       │
└────────────────────────────────────────────────────────────────────────┘
```

* **Layer strip** is the `Seg` segmented control (`components/estate.tsx`,
  `.seg`), per the rulebook — not the ad-hoc `.overlay-picker` buttons the
  current `TopologyView` uses.
* **Scope** is cascading `<select>`s in `.asset-panel-filters` form, 26 px,
  "All …" first, dependent lists following their parent. `depth` stays but
  is relabelled in words: *"also show what feeds this room"* rather than a
  bare integer.
* **View tabs** — diagram / trace table / redundancy audit — are the same
  data in three readings. The table is not a fallback; it is the artifact
  people paste into tickets.
* **Drawer, not modal.** A modal over a diagram kills the comparison the
  diagram exists for. Rulebook carve-out applies: charts inside the drawer
  still get the maximise glyph, and while a modal is up Escape belongs to
  the modal.
* The diagram panel is `.asset-panel` chrome and gets the maximise glyph
  like every other panel.

### 5.1 Node rendering

| Layer | Node shows |
|---|---|
| power | name · type glyph · **load / rating** as a `Meter` (hatched when no rating is recorded — "no limit recorded" is not 0 %) · phase where metered |
| cooling | name · type · the one number that matters for the stage (chiller: kW and COP; pump: speed/flow; CRAH: supply/return ΔT; CDU: secondary ΔT and flow) |
| network | name · type · port count up/down · uplink utilisation |

Status is `--ok` / `--warn` / `--major` / `--critical` / `--unknown` on the
4 px left rail (as today) **plus a glyph**, because `docs/12` §3.3 already
established that colour alone fails for colour-blind operators and in a
printed runbook — and a topology diagram gets printed more than most
screens.

### 5.2 Edge rendering, and the A/B fix

* One line per collapsed bundle, labelled `×7` when it stands for seven
  conductors.
* **Redundancy side is encoded by position first** — A-side in the left
  column, B-side in the right — **then by dash pattern** (A solid, B
  dashed), **then by a label at the junction**. Hue is the *fourth*
  channel, and when used it is `--series-1` / `--series-2` resolved via
  `useChartColors()`, never a call-site hex. This removes the
  `#3b82f6`/`#a855f7` violation and satisfies the rulebook's "hue is never
  the only channel".
* `oper_state = down` is `--critical` and dashed, as today.
* Flow direction on power and cooling is an arrowhead at the downstream
  end; ethernet layers get none, because the a/b ends of a cable are not a
  direction (`repositories/topology.py` header).
* Hover gives `useHoverTip()` content: both terminations, connector type,
  rated amps, conductor count, oper state.

### 5.3 Roll-up (what makes a hall readable)

At room scope on the power layer the leaf tier is ~800 servers. They render
as **one node per rack**: `R12 · 20 loads · 8.4 kW · 1 offline`. Clicking a
rack node expands it in place to its devices (a structure change, so a
re-layout is legitimate and expected — the user asked for it). A
`rollup=rack|none` parameter on the API does this server-side so the wire
carries 61 nodes rather than 847 (§6.2).

This replaces `truncated: true` as the *normal* answer. Truncation stays as
the backstop for a genuinely unbounded request, but "narrow the scope" is
an error message, and roll-up is a feature.

### 5.4 States

* **Loading** — `.asset-skeleton`, diagram frame at its measured height so
  the page does not jump.
* **Empty** — a `<p className="muted">` sentence that says *why*: "No
  cooling connections are recorded in Electrical Room 1. Plant serving this
  room sits in Chiller Hall 1 — raise depth to 1 to pull it in."
* **Truncated** — inline warning naming the count and offering the
  narrower scope as a button, not as prose.
* **Stale/live** — the footer's live-feed indicator covers the socket. The
  diagram additionally prints the graph's own age, because a 30 s cache
  plus a 15 s poll means the picture can be 45 s old and the operator is
  about to touch hardware based on it.

---

## 6. Backend work

### 6.1 Fixes

* Route `/connectivity` to the connectivity page; keep `/topology` as a
  redirect (bookmarks outlive a rename — the codebase already makes this
  argument for `/utilization` → `/capacity` at `App.tsx:210`).
* Add `api.impact(deviceId)` to `frontend/src/api/client.ts`.

### 6.2 `GET /topology` — add `rollup`

`rollup=none|rack` (default `none`; the UI sends `rack` at room and DC
scope). Server-side aggregation: leaf devices sharing a `rack_id` and a
device class collapse into one synthetic node `rack:{id}` carrying
`device_count`, `offline_count`, summed `power_w`, max `inlet_temp_c` and
max severity. Edges to collapsed members re-point at the synthetic node and
collapse by `(source, target, redundancy_side)` with a count. Synthetic ids
are namespaced so the client can tell them from device ids and knows they
do not link through to `/devices/{id}`.

### 6.3 `GET /topology/trace/{device_id}` — SHIPPED, and revised

```
GET /topology/trace/{device_id}?layer=power
```

Ordered hops from the device to its sources: device, termination at each
end, connector, rated amps, phase, branch, redundancy side, `oper_state`,
and a per-path verdict — `complete` (reached a device nothing feeds) or
`incomplete` (closed on itself or hit the hop bound first). This is the
trace table, and it is also what the SLD renderer will consume for its A
and B columns.

**Two revisions the live data forced, both worth recording.**

**Upstream only.** The plan said `direction=up|down|both`. Downstream is
not a path question: `/topology/impact/{id}` already answers it, and in
the terms that matter — what goes dark versus what merely loses a side.
Enumerating downstream routes from a UPS would produce four hundred
near-identical lists and answer neither question. What people mean by
"what hangs off this" is one hop, so the response carries the immediate
neighbours and a count, and nothing else.

**One path per CORD, not one per route.** The first implementation
enumerated every simple path to a source. That multiplies out every fork
*above* the device, and in a 2N estate that is a lot of forks: a
dual-corded server behind a switchgear pair with a utility and two
generators came back as **six** six-hop paths — thirty-six rows, identical
from the ATS down, differing only in which source sat at the top. The walk
now follows each cord upward one feeder at a time and records the feeders
it did not take as `alternates` on that hop. Two cords, two chains, and
the fact that losing SWGR1 alone does not drop the A side survives at the
hop where it is true. NetBox does the same thing when a cable trace forks,
for the same reason: a trace that guesses silently is worse than one that
says where it chose. The choice is deterministic and stays on the side it
started on — an A cord traced up through the B board would be a fiction.

Implementation: `repo.layer_edges_detailed(layer)` — whole-layer, already
normalised upstream→downstream via `core/layers.UPSTREAM_COL`, and cheaper
to fetch once than to answer with a recursive query per device.

### 6.4 `GET /topology/redundancy` — new

```
GET /topology/redundancy?scope=room:{id}&layer=power
```

Three lists, each with device, finding, and the shared ancestor where one
applies:

* `single_fed` — loads with one upstream path where their device class
  expects two.
* `same_side` — loads whose feeds all carry the same `redundancy_side`.
* `converged` — loads whose two sides share an ancestor; the ancestor is
  named, and it is exactly the device whose removal cuts the load off. This
  falls out of `impact.analyse`: a load that appears in `cut_off` despite
  having two sides is a convergence.

Cost control: naively this is O(nodes × ancestors). Compute it in one pass
— for each load, intersect the ancestor sets of its distinct sides; a
non-empty intersection is a convergence and its members are the
common-mode devices. Cache on `graph_version` like the graph itself.

### 6.5 `GET /topology/path` — build the specified endpoint

`?src=&dst=&layer=` — shortest path plus, where they exist, edge-disjoint
alternates. Answers "are these two racks on independent power?".

### 6.6 Live updates

`topology:{layer}` already exists in the websocket spec (`docs/11`,
`topology_change` event). Subscribe on the connectivity page and patch
`oper_state` and node status in place. The structure key
(`layout.ts:structureKey`) guards against a re-layout; a `topology_change`
that alters *structure* invalidates the query, and a re-layout there is
correct.

### 6.7 Termination detail

Extend `_TERM_LABEL_SQL` (`repositories/topology.py`) to carry
`outlet.rated_amps`, `outlet.phase`, `outlet.branch` and
`power_supply.rated_watts` alongside the label, so the trace table and the
hover tip can print the electrical facts without a second round trip.

---

## 7. Technology — evaluation and decision

### 7.1 The constraint that decides it

Three facts:

1. **Scale is low thousands, not hundreds of thousands.** 664 devices /
   2566 edges in the reference fixture; a room-scope power graph after
   roll-up is ~60–250 nodes and ~200–400 lines. Published thresholds put
   the SVG ceiling at roughly **3,000–5,000 DOM elements** before it
   degrades, with canvas the answer in the tens of thousands and WebGL
   beyond that. We are one to two orders of magnitude below the point where
   rendering technology is the problem.
2. **The house style is hand-rolled SVG with token-only colour**, enforced
   in CI by `scripts/validate_palette.js`. `frontend/package.json` has
   **four runtime dependencies** and no visualisation library at all.
3. **The three diagram grammars in §2 are domain-specific.** No generic
   graph library produces an A/B-column one-line diagram or a chilled-water
   loop schematic. Whatever we adopt, the placement logic for power and
   cooling is ours either way.

### 7.2 The candidates, honestly

| Option | Rendering | Licence | Verdict |
|---|---|---|---|
| **Cytoscape.js** | Canvas | MIT | Richest algorithm set (shortest path, centrality) and many layouts. But it owns styling through its own stylesheet language, which fights `--token` colour and the palette CI, and canvas puts the diagram outside the DOM — no CSS theming, no native focus, a11y from scratch. ~500 KB. **No.** |
| **sigma.js + graphology** | WebGL | MIT | Built for 100k+ nodes. We have 664. Custom node content (an SLD symbol, a `Meter` inside a node) is genuinely hard in WebGL. Solves a problem we do not have, at the cost of the one we do. **No.** |
| **AntV G6** | Canvas/SVG/WebGL | MIT | Capable and flexible; docs partly Chinese; large; same styling-ownership problem. **No.** |
| **React Flow (`@xyflow/react`)** | DOM nodes + SVG edges | MIT | The closest fit: nodes are React components styled with our own tokens, so `Meter`, `StatusChip` and `CategoryGlyph` drop straight in, and pan/zoom/minimap come free. But it is a *node editor* — handles, drag, connection gestures — and we want a read-only derived diagram; effort goes into switching its interaction model off. It also lays nothing out (§7.3). **Reconsider only if we ever ship an editable diagram.** |
| **elkjs** (layout only) | n/a | **EPL-2.0 OR GPL-3.0-or-later** | Best-in-class layered (Sugiyama) layout with real port support and orthogonal routing. The licence is a genuine consideration for a commercial product — file-level reciprocal with a patent-retaliation clause, and a bundler mixing minified EPL code with MIT code has been argued to create a derivative. Needs a licence sign-off before it lands, and it must run in a Web Worker (it is a GWT-compiled blob). **Conditional — §7.4.** |
| **dagre** (layout only) | n/a | MIT | Drop-in layered layout, no port support, effectively unmaintained. Fine, but it does roughly what `layout.ts` already does. |
| **d3-force** | n/a | ISC | Rejected on domain grounds in §3.2 and by `layout.ts`'s own header. |
| **Own code (status quo)** | SVG | — | `layout.ts` is 154 lines, is a pure function of structure, and cannot jitter. Full token control, full DOM a11y, zero bundle cost. |

### 7.3 A point that is easy to miss

Rendering and layout are separate decisions, and **layout is the harder
one**. React Flow, for instance, positions nothing — every React Flow
layout tutorial pairs it with dagre or elkjs. "Adopt React Flow" does not
remove the layout work; it adds a renderer on top of it.

### 7.4 Decision — SUPERSEDED 2026-09-17, see §7.5

**Render: hand-rolled SVG + React. No graph library.**

Justification: we are 10–40× below the SVG ceiling; the diagram grammars
are ours to write regardless; SVG keeps every node in the DOM, which is how
we get CSS tokens, dark/light themes, `:focus-visible`, real tab order and
`role`/`aria-label` for free; and the bundle stays four dependencies deep.

**Layout: three purpose-built pure functions, extending `layout.ts`.**

* `layoutOneLine(nodes, edges)` — power. Ranks by *electrical stage*
  (utility / switchgear / ATS / generator / UPS / RPP / PDU / load) rather
  than by graph distance, splits into A and B columns by
  `redundancy_side`, and places a dual-corded load once, centred, with a
  cord to each column.
* `layoutLoop(nodes, edges)` — cooling. A fixed stage template (tower →
  condenser → chiller → primary pump → secondary pump → header → terminal)
  with the return leg routed down the outside, drawn as a closed circuit.
* `layoutLayered(nodes, edges)` — network / management / fieldbus. Today's
  `rankNodes`, plus **crossing reduction**: 2–4 sweeps of the Sugiyama
  median heuristic over each rank. That is ~120 lines and is the single
  biggest readability win available on the fabric layers.

All three keep the existing invariant: a pure function of structure,
memoised on `structureKey`, so live state cannot move a node.

**One conditional dependency: `elkjs`, network layer only.** If, after the
median heuristic, crossings on a full fabric are still unreadable, add
elkjs in a Web Worker for `layoutLayered` only — and only after a licence
review signs off EPL-2.0/GPL-3.0 for this product. Everything else stays
ours. Record the review's outcome in this document.

**Escape hatch, written down now so nobody reaches for a library in a
panic:** if an estate-scope view is ever demanded and exceeds ~3,000
rendered elements, add a `<canvas>` layer for *edges only* beneath the SVG
node layer. Nodes stay in the DOM. That is a ~150-line change and preserves
theming and accessibility, which swapping to a canvas library would not.

**Revisit triggers:** sustained >3,000 rendered elements; a requirement for
user-editable diagrams; or a need for graph algorithms heavier than BFS
(community detection, centrality) on the client.

### 7.5 What was actually built: React Flow, and why §7.4 was wrong

Shipped 2026-09-17. The canvas is **`@xyflow/react` v12** — the same library
the simulator's own topology view uses, so the two products behave the same
way under the same hands.

**Where §7.4 reasoned badly.** It scored the options on whether they could
*draw* 664 nodes, decided SVG could, and stopped. Drawing was never the
constraint. What a hand-rolled SVG could not give without reinventing each of
them: pan and zoom, a node that is a real focusable hoverable element rather
than a `<rect>`, a label that stays upright while the picture scales, and a
minimap. §7.4 listed React Flow as "the closest fit" and then rejected it for
being a node editor — but its editing behaviour is four props to switch off,
and that is a much smaller cost than the four features it replaces. The
"generic renderer gives us none of the diagram grammars" argument was the
soundest part and it survives: see below.

**What did not change, and this is the point.** React Flow lays nothing out.
Every layout example it ships pairs it with dagre or elkjs, so adopting it
removes none of the layout work and none of the domain content:

* `layout.ts` is unchanged in substance — the structural rank, the A/B column
  split derived from the conductors, the middle gutter for dual-fed loads.
* Positions are still a pure function of structure, and **a live update still
  cannot move a node**: positions are rebuilt only when the layout key
  changes, and a fifteen-second poll walks the existing nodes and swaps their
  `data`. That now also means a node someone dragged stays where they put it,
  which the SVG could not offer at all.
* Colour is still tokens only, and the palette check still passes — nodes are
  DOM elements, so they take the same CSS variables as the rest of the
  product and follow the theme.

**Costs, stated.** The bundle goes 655 kB → 856 kB (180 → 246 kB gzipped);
the connectivity route is the obvious thing to code-split and has not been.
The node card is bigger than the old box (196×56 against 132×30), so a hall
is wider and is panned rather than seen at once — mitigated by a zoom floor
of 0.62, below which fit-to-view stops shrinking and the canvas pans instead,
because a diagram whose every label is five pixels of grey is not a fit.

**What §7.4 still gets right, and what the escape hatch becomes.** The three
diagram grammars are still ours to write, and no library would have produced
the one-line. `elkjs` is still unadopted and still needs a licence review
before it is. The canvas escape hatch is no longer "add a `<canvas>` edge
layer" — it is React Flow's own `onlyRenderVisibleElements`, which is already
on.

---

## 8. Cross-cutting requirements

### 8.1 Performance budgets

| Thing | Budget |
|---|---|
| `GET /topology?layer=power&scope=room` | < 400 ms (`docs/15` 4.1 — already met) |
| `GET /topology/redundancy?scope=room` | < 800 ms cold, cached on `graph_version` |
| Client layout, room scope after roll-up | < 50 ms; > 100 ms moves it to a Web Worker |
| Rendered SVG elements | ≤ 3,000; the roll-up is what keeps this true |
| Live patch (status / `oper_state`) | zero re-layout, zero node movement — asserted in a test |

### 8.2 Accessibility

A node-link diagram is the hardest thing in this product to make
accessible, and the honest answer is that the diagram is not the only way
to get the information.

* **The trace table is a first-class view, not a fallback.** Same data,
  fully navigable, sortable, exportable — and it is what operators want
  anyway.
* **Keyboard on the diagram:** Tab reaches the diagram as one widget; then
  arrow keys walk the graph — ↑ upstream, ↓ downstream, ← / → between
  siblings in a rank — Enter opens the drawer, Escape clears selection.
  This mirrors the structure rather than the pixels, which is how a
  keyboard user traces a chain.
* Each node is a `role="button"` with an `aria-label` that reads as a
  sentence: "PDU-A-R12, rack PDU, online, 8.4 of 16 kW, fed by RPPA-DC1".
  The SVG root keeps `role="img"` and a summary label, as today.
* Colour is never the only channel — status carries a glyph, A/B carries
  position and dash. Verified by the palette validator in CI.

### 8.3 Testing

* Backend: golden-graph tests per layer on the `dual_dc_enterprise.json`
  fixture — node/edge counts per layer must match `docs/16` §3.1 exactly.
* Impact: the cascade case `impact.py`'s docstring describes — a load whose
  "second" feeder is itself fed only through the candidate — must come back
  `cut_off`, not `degraded`. Regression-guard it.
* Redundancy audit: a purpose-built fixture with one single-fed load, one
  same-side pair and one convergence; assert one finding each and no false
  positives on a correctly 2N room.
* Layout: pure functions, so snapshot the placement. Assert `structureKey`
  stability — a status-only refresh must return the identical placement
  object.
* Two-datacenter isolation, per the pattern the simulator side already
  proved valuable: a finding in DC1 must not appear under DC2.

---

## 9. Phased delivery

Each phase ships something usable on its own.

**Status at 2026-09-16: phases 0 and 1 are shipped, pushed and verified
against the live estate.** What each one actually cost and what it found is
recorded under it.

### Phase 0 — make the page honest (0.5 day) — DONE

* Route `/connectivity` to the topology view; `/topology` redirects.
* Replace `#3b82f6` / `#a855f7` with `useChartColors()` tokens **and** make
  A/B carry dash + position, not hue alone.
* Add `api.impact` to the client.
* Swap `.overlay-picker` for the `Seg` control; scope filters into
  `.asset-panel-filters` form; panel chrome to `.asset-panel` with the
  maximise glyph.

*Acceptance:* the nav goes where it says; `npm run validate:palette`
passes; the page looks like the rest of the product. **Met.**

*Found on the way:* moving the A/B stroke out of CSS and into the component
broke it silently. An SVG **presentation attribute loses to any CSS
declaration that sets the same property**, so `.topo-edge { stroke: … }`
beat the palette value on every line and the whole diagram drew in the
border colour. It looked merely pale rather than broken, because the dash
pattern survived — the one channel that was never meant to carry the
distinction alone was the only one still carrying it. The base rule now
sets `fill` only; `down` stays in CSS deliberately, because it is a state
rather than a side and CSS outranking the attribute is what makes it
impossible to forget at a call site.

### Phase 1 — roll-up and the trace table (2–3 days) — DONE

* `rollup=rack` on `GET /topology`, with `device_count` / `conductor_count`
  beside `node_count` / `edge_count` so the view can say what it is hiding.
* `GET /topology/trace/{id}` — see §6.3 for the two revisions.
* Trace table view tab, paged with `components/Pagination.tsx`, CSV export.
* Drawer on node select, carrying that node's chain per side.

*Acceptance:* Server Hall A on the power layer renders in one screen
without truncation; any device's chain to source reads as a table an
engineer can paste into a method statement. **Met** — the hall draws as
"55 boxes for 149 devices · 61 lines for 245 conductors", and SRV01's trace
prints both cords, source first, down to `Out-2 · C13 · 10 A · L1-L2 · br 1
→ PSU1 · C14 · 1100 W`.

*Found on the way:*

* The client's edge collapse counted one per row, so a rack's forty merged
  cords would have drawn as a single conductor. It adds `count` now.
* Clicking a node used to navigate to the device record, which lost the
  diagram every time somebody checked what a box was. Selection opens the
  drawer; the device page is a link inside it.
* A rolled-up node has a synthetic id. It has no device page and no chain
  of its own, so it offers the rack elevation rather than asking the server
  for a trace it would refuse with a 400.
* The trace table printed `unknown` down a State column on every power and
  cooling row. **Only ethernet reports link state** — a cord lands on an
  outlet and a pipe on a stub, which is why `alarms/link_correlation`
  watches only the two port layers as well. A column that is always
  `unknown` teaches the reader to skip it on the one layer where it carries
  a fact, so it now appears only where it is measured.
* A/B cannot use position in a table row, so the badge prints the letter
  and the tint only reinforces it.

### Phase 2 — the one-line diagram (3–4 days) — DONE

* `layoutOneLine`: A/B columns, stage ranks, dual-corded loads centred.
* `Meter` in each node: actual against rated, hatched where no rating is
  recorded.
* Termination detail (amps, phase, branch) in hover and drawer.

*Acceptance:* a power one-line for a room that an electrical engineer
recognises without explanation; no node claims a capacity the data does not
hold. **Met.**

*Built differently from the plan:* the plan said `layoutOneLine` would rank
by *electrical stage* — utility / switchgear / ATS / UPS / RPP / PDU / load.
It does not. That is a device-type table, it would need keeping in step with
the estate, and it would be wrong about exactly the cases worth seeing: the
RPP that ended up feeding both sides, the load somebody corded twice to A.
The structural rank already puts sources at the top; the only thing the
one-line adds is the **column split, and the side is read off the conductors
touching each node**. A node whose conductors disagree, or carry none, goes
in the middle where both columns reach it — which is also exactly where a
dual-corded load belongs. Where nothing carries a side the columns collapse
back to the centred rank, because two empty gutters around a full middle is
a worse picture than no columns at all.

*Found on the way, and it is the point of the phase:*

**39 of 90 servers in DC1 Server Hall A draw more than their recorded
nameplate**, up to 177 %. Every other device class is comfortable — CRAH
6.6 %, PDU a median 22 %, CDU 49 %, UPS 6 %. The rating is the model's:
`Supermicro SYS-121H-TNR LCC`, recorded at **800 W**, on a chassis whose own
PSUs are **2 × 1100 W**. A 1U dual-socket Sapphire Rapids box does not have
1100 W supplies fitted to run at 800 W, so the nameplate is the number that
is wrong, not the draw.

This is a real inconsistency between the simulator's power model and the
DCIM's model catalogue, and it was invisible until a live draw was printed
next to a rating. It is also a problem for this page as it stands: every
server rack paints red, and a diagram that is always red stops being read.
**It needs a decision before phase 3** — correct the catalogue rating (the
likely fix), or correct the simulator's draw. Not silently suppressed here:
the bar is telling the truth about what is recorded.

### Phase 3 — impact on the diagram (2 days)

* *Simulate removal* on the selected node: cut-off set red, degraded set
  amber-outlined, counts in the drawer, list exportable.
* The same control from the maintenance-window screens, which is where the
  question actually gets asked.

*Acceptance:* removing a UPS in a 2N room shows degraded-not-dark for
dual-corded loads and dark for every single-corded one, and the list
matches `services/impact.py` exactly.

### Phase 4 — redundancy audit (2–3 days)

* `GET /topology/redundancy`; the third view tab; findings link into the
  diagram with the offending path highlighted.

*Acceptance:* a seeded convergence (both RPPs off one UPS) is found and
names the UPS; a correctly 2N room reports nothing.

### Phase 5 — cooling loop (3–4 days)

* `layoutLoop` on the plant stage template; supply/return distinguished;
  flow arrows; per-stage numbers (ΔT, flow, COP, valve position).

*Acceptance:* the CHW loop reads as a circuit, the return leg does not
point back up the page, and a stopped tower bank or a flow-interlock-shed
chiller is visible on the diagram — the two failures the live exhaustion
campaign found invisible elsewhere.

### Phase 6 — fabric readability and path (2–3 days)

* Median-heuristic crossing reduction in `layoutLayered`; measure crossings
  before and after and record the numbers here.
* `GET /topology/path` and an "are these independent?" control.
* Decide elkjs on the measurement, not on taste.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Roll-up hides the device someone is looking for | Device search always locates and auto-expands its rack |
| The SLD implies breakers we do not model | Print only ratings we hold; label the detail level; never draw a breaker symbol without data (§2.1) |
| Cooling stage template does not fit a future plant topology | Template is data, not code — keyed by `device_type`; unknown types fall back to `layoutLayered` rather than rendering wrongly |
| `redundancy` endpoint is expensive at DC scope | Cache on `graph_version`; compute at room scope by default; DC scope is an explicit, slower request |
| A 30 s cache + 15 s poll means acting on a 45 s-old picture | Print the graph's age on the diagram; drop this endpoint's TTL if operators object |
| elkjs licence | Review before it lands; the default path does not need it |

## 11. Deferred, deliberately

* **Breaker and panelboard entities.** A true SLD wants them. It is a data
  model change plus an importer change plus a simulator change, and it is
  not on the critical path for the three questions in §4.
* **3D.** See §3.2.
* **Editable diagrams.** See §3.2.
* **Estate-scope single diagram.** The roll-up makes room scope work;
  estate scope is a list of rooms, not one graph.

## 12. Open questions

1. Is the connectivity page aimed at the change-approval workflow (trace +
   impact + audit, as planned here) or at fault triage during an incident?
   The plan assumes the former; triage would push impact and a live alarm
   overlay ahead of the one-line diagram.
2. Is EPL-2.0 / GPL-3.0 (`elkjs`) acceptable in this product? If not, the
   conditional branch in §7.4 closes and Phase 6 is hand-rolled only.
3. Is liquid cooling (CDU secondary loops) in scope for Phase 5, or air
   only? The fixture has 12 CDUs, so the data supports it.

## 13. Sources

* Sunbird DCIM — Data Center Connectivity (3D/2D/tabular circuit views,
  auto single-line diagrams, QuickConnect, redundancy indicators):
  <https://www.sunbirddcim.com/product/data-center-connectivity>
* Sunbird DCIM — What Is a Single-Line Diagram:
  <https://www.sunbirddcim.com/glossary/single-line-diagram>
* Sunbird DCIM — Automatic, Dynamic, and Interactive Single-Line Diagrams
  (AN015):
  <https://www.sunbirddcim.com/sites/default/files/AN015_Sunbird_Application_Note_Single_Line_Diagram.pdf>
* NetBox — cable/connection management and cable-trace SVG rendering, path
  verdicts:
  <https://deepwiki.com/netbox-community/netbox/3.3-cable-and-connection-management>
* NetBox — trace entire cable path from power port to upstream panel (open
  feature request): <https://github.com/netbox-community/netbox/issues/17335>
* EkkoSense — power management using the electrical one-line diagram:
  <https://www.ekkosense.com/resources/tech-tips/power-management-in-data-centers-using-the-3d-mechanical-and-data-center-electrical-one-line-diagram/>
* Uptime Intelligence — AI and cooling: chilled water system topologies:
  <https://intelligence.uptimeinstitute.com/resource/ai-and-cooling-chilled-water-system-topologies>
* Consulting-Specifying Engineer — how to design piping systems for data
  centers that require liquid cooling:
  <https://www.csemag.com/how-to-design-piping-systems-for-data-centers-that-require-liquid-cooling/>
* Linkurious — Top JavaScript graph visualization libraries (rendering
  technology, licences, scale):
  <https://linkurious.com/blog/top-javascript-graph-libraries/>
* PkgPulse — Cytoscape.js vs vis-network vs Sigma.js, 2026:
  <https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026>
* Cylynx — a comparison of JavaScript graph/network visualisation
  libraries:
  <https://www.cylynx.io/blog/a-comparison-of-javascript-graph-network-visualisation-libraries/>
* Domrös et al. — The Eclipse Layout Kernel (ELK layered = Sugiyama with
  ports): <https://arxiv.org/pdf/2311.00533>
* React Flow — layouting overview and the elkjs example (React Flow does
  not lay out): <https://reactflow.dev/learn/layouting/layouting>
* elkjs licence discussion (EPL-2.0 alongside MIT):
  <https://github.com/libredb/libredb-studio/issues/544>
* xyflow — open source / MIT: <https://xyflow.com/open-source>
* Horak et al. — Comparing rendering performance of common web technologies
  for large graphs: <https://imld.de/cnt/uploads/Horak-2018-Graph-Performance.pdf>
* ApexCharts — SVG vs Canvas charts: what actually matters (2026
  thresholds): <https://apexcharts.com/blog/svg-vs-canvas-charts/>
* SVG vs Canvas vs WebGL performance comparison:
  <https://www.svggenie.com/blog/svg-vs-canvas-vs-webgl-performance-2025>
* Cockburn, Karlson & Bederson — A review of overview+detail, zooming and
  focus+context interfaces:
  <https://www.researchgate.net/publication/220566544>
* Dunsmuir — Semantic Zoom View: a focus+context technique:
  <https://summit.sfu.ca/_flysystem/fedora/sfu_migrate/11587/etd6479_DDunsmuir.pdf>
* Effective visualization of large-scale data center network topology:
  <https://www.researchgate.net/publication/392710649>
* Untangling Hairballs (hierarchical aggregation over faster rendering):
  <https://link.springer.com/chapter/10.1007/978-3-662-45803-7_9>
* Archambault — Bundling-aware graph drawing (GD 2024):
  <https://drops.dagstuhl.de/storage/00lipics/lipics-vol320-gd2024/LIPIcs.GD.2024.15/LIPIcs.GD.2024.15.pdf>
* WebAIM — Keyboard accessibility: <https://webaim.org/techniques/keyboard/>
