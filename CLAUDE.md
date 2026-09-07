# dcim-platform

DCIM product built against the Datacenter Network Simulator (its device
plane). Go collector, FastAPI backend, React frontend, PostgreSQL +
TimescaleDB + Redis. Datastores run in Docker; backend, ingest worker,
collector and UI run in WSL from `~/dcim-platform` via `scripts/dev.sh`.

## Chart rulebook

Every chart in this product follows the forms and styling of the Assets
page. Reference implementations: `frontend/src/features/assets/{Charts,Trends}.tsx`,
`frontend/src/features/assets/components/{BarChart,Shapes}.tsx`,
`frontend/src/features/assets/assets.css`; `frontend/src/components/{TimeChart,Plot,Meter}.tsx`
with `frontend/src/index.css`; the range control `Seg` in
`frontend/src/components/estate.tsx` (`.seg` in `estate.css`). The alarm
trend (`frontend/src/features/home/AlarmTrend.tsx`) is the worked example
of a chart outside the Assets page built to these rules.

Pick the form from the question, reuse the component if one exists,
otherwise copy its CSS values verbatim. Never invent a new chart style.

### Shared rules

- Colour is tokens only: `var(--accent)` for a single series; `--ok`,
  `--warn`, `--critical` only when the hue IS the state; `--text-faint`
  for "absent"; `--border-strong` for "none recorded" or "later". Raw hex
  appears only in the validated multi-series ramps (`LINE_COLORS`,
  `PLOT_COLORS`), which are checked with `scripts/validate_palette.js`,
  never by eye.
- One colour per chart unless the hue carries meaning. Never a hue per bar.
- The axis starts at zero for anything encoded by length (bars, columns).
  Lines may auto-range but must print the range (`lo–hi`) on the axis.
- Values are always printed, `font-variant-numeric: tabular-nums`,
  `--text-muted`. A bar shows rank; the figure is what gets quoted. On a
  column the value rides the column top via a bottom-anchored stack
  (`padding-top: 20px`, children `flex-shrink: 0`).
- Zero still gets a foot: `min-height: 2px` on columns, `min-width: 2px`
  on bars, or the row reads as missing rather than none.
- Labels: `.k` at 0.66rem `--text-faint`, nowrap with ellipsis. A tilt
  wrapper (`rotate(-45deg)`) only for long categorical labels. A vertical
  y-axis title (`writing-mode: vertical-rl; transform: rotate(180deg)`,
  0.7rem uppercase `--text-faint`) whenever the figures need naming.
- Hover uses `useHoverTip()`; tip content is `<b>label</b> value`. Line
  charts snap the crosshair to a real sample and never interpolate.
- SVGs carry `role="img"` and a sentence `aria-label`. A legend only when
  there is more than one series; swatch 9–10px, radius 2.
- Empty state is a `<p className="muted">` sentence that says why. One
  snapshot is a dot and a sentence, not a line. Loading is `.asset-skeleton`.
- Panel chrome is `.asset-panel`: 1px `--border`, `--radius`,
  `--bg-raised`, padding 14px 16px. Its `h3` is uppercase 0.86rem,
  letter-spacing .03em, `--text-muted`. A total floats right in
  `--text-faint` with a lowercase unit. The maximize glyph (`.asset-max`)
  sits top-right and `MaxModal` renders the same children, so there is one
  state.
- Filters. Dimension filters are `<select>`s in `.asset-panel-filters`
  (26px tall, 0.76rem, gap 6px, max-width 200px, "All …" as the first
  option; dependent lists follow their parent and a selection the narrowed
  data no longer holds is cleared, never shown empty). Range filters are
  the `Seg` segmented control (`.seg`: hairline-divided, 34px, 11px/600
  uppercase .09em, active cell `--bg-raised` with `inset 0 -2px 0
  var(--accent)`), keys `30D` `90D` `180D` `1Y`, placed on the title row
  right-aligned. Top-N limits are `.asset-chart-controls` buttons with
  `.is-current` in accent.
- A range or filter slices data already fetched wherever the response
  covers it; never refetch to switch a range.
- Dense charts decide what to hide from measured pixels per bar
  (ResizeObserver), not from bar count: values into the tooltip under
  ~34px a bar, every k-th date label so each keeps ~46px.

### Which form for which question

1. **Horizontal bar list** (`BarChart`): categoricals with long labels.
   Grid `label | track | value`; track 15px `--bg-inset` radius 3, fill
   accent; axis ticks 0, a nice midpoint, max; sorted descending unless
   `sorted={false}` for a sequence; Top 10/25/All with hidden rows summed
   as "and N others" in muted so the bars still add to the panel total;
   `of` makes a ratio bar that prints "628 / 664" against one shared
   denominator.
2. **Vertical columns** (`VColumns`, the histogram, the alarm trend):
   short-label sequences such as quarters, bands or dates. The order given
   is the order drawn. Height 150 (histogram 132; a full-width sheet may
   use 220), gap 8, bar radius `3 3 0 0`, `.v` 0.76rem with 1px bottom
   margin and line-height 1.1, `.k` 0.66rem with 5px top margin.
3. **Diverging columns** (`Diverging`): signed deltas. Up is accent, down is
   warn, the zero line is `1px solid var(--border-strong)`, one shared
   scale, height 140.
4. **Paired columns** (`Paired`): two series per bucket in accent and warn
   on one shared scale, legend above, bars 40% wide with a 2px gap,
   height 120.
5. **Trend line** (`TrendLine`): a daily snapshot series. SVG 260×90 with
   `preserveAspectRatio="none"`, line accent 2px, area accent at 0.12,
   end dot r3; the axis row reads `start | lo–hi | end · last value`. A
   single point renders as a large value and a "Recording since" sentence.
6. **Time series** (`TimeChart`): telemetry. 720×180, left pad 52. One
   chart per unit and never a second y-axis. `LINE_COLORS` in fixed order
   (blue, orange, purple, green, cyan, amber); facet rather than exceed
   three series. Lines break across gaps longer than three buckets. Ticks
   10px mono, unit top-left, tooltip an SVG rect that flips left near the
   edge, time printed at the data's precision with no fake seconds.
7. **Analytics plot** (`Plot`): projections and PUE. Uncertainty band
   filled at 0.16 because a forecast without its band reads as a
   measurement; reference lines dashed `5 4` labelled at the right; the
   projected half dashed `6 4`; a single point becomes a dot; reference
   levels are included in the y-range so a capacity line is never off
   frame.
8. **Donut** (`Donut`): one part-to-whole only. 148px, ring 20, starts at
   12 o'clock, centre figure 1.35rem/600 with a sub-label, legend prints
   count and percent. Never a pie.
9. **Gauge** (`Gauge`): a bounded ratio with thresholds, such as
   utilisation. 190×108, bands ok → warn → critical at 0.6 / 0.85 / 1,
   quarter ticks inside the ring, value and "of max" underneath, label in
   muted.
10. **Meter** (`Meter`): a utilisation bar with an unknown state. A limit
    nobody recorded renders as a hatched track with the usage printed and
    no percentage, because "no limit recorded" is not 0%. Tones at 80/95.
11. **Stacked bar** (`.asset-stack`): a partition of one whole, 14px tall,
    `is-used` accent, `is-held` warn, `is-free` inset with an outline.
