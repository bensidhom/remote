(() => {
  const featureGraphs = {
    map: {
      caption: "Map controls become one normalized ArcGIS layer; invalid coordinates exit before rendering.",
      nodes: [
        ["controls", "input", "Map controls", "county · year · rating field"],
        ["query", "behavior", "Inventory query", "county list + map records"],
        ["valid", "decision", "Valid WGS84\ncoordinate?", ""],
        ["layer", "behavior", "FeatureLayer", "dedupe · normalize · symbolize"],
        ["map", "output", "Map response", "markers · legend · popup"],
        ["excluded", "guard", "Excluded", "invalid or duplicate record"],
      ],
      edges: [
        ["controls", "query", "GET"],
        ["query", "valid", "rows"],
        ["valid", "layer", "yes"],
        ["layer", "map", "render"],
        ["valid", "excluded", "no", "dashed"],
      ],
    },
    locate: {
      caption: "The desktop sidebar resolves intent first, then synchronizes the map before focusing the bridge.",
      nodes: [
        ["phrase", "input", "Sidebar phrase", "number · name · location"],
        ["intent", "behavior", "Intent parser", "detect “show bridge” request"],
        ["index", "behavior", "Statewide index", "number/name/location ranking"],
        ["match", "decision", "Unique\nmatch?", ""],
        ["sync", "behavior", "Synchronize view", "set year + county"],
        ["focus", "output", "Focused bridge", "zoom 16 · popup · card"],
        ["status", "guard", "Status", "ambiguous or not found"],
      ],
      edges: [
        ["phrase", "intent", "message"],
        ["intent", "index", "lookup"],
        ["index", "match", "rank"],
        ["match", "sync", "yes"],
        ["sync", "focus", "ArcGIS"],
        ["match", "status", "no", "dashed"],
      ],
    },
    conditions: {
      caption: "County and year select one comparable inventory snapshot; the condition field changes only its visual encoding.",
      nodes: [
        ["controls", "input", "View controls", "county · year · condition field"],
        ["snapshot", "behavior", "Comparable snapshot", "available counties + bridge rows for year"],
        ["value", "behavior", "Condition accessor", "Deck · Super · Channel · other field"],
        ["numeric", "decision", "Numeric\nrating?", ""],
        ["classify", "behavior", "Marker class", "Good 7–9 · Fair 5–6 · Poor 0–4"],
        ["view", "output", "Comparable view", "markers · legend · year distribution"],
        ["unknown", "guard", "No-data style", "categorical, N, or null → gray"],
      ],
      edges: [
        ["controls", "snapshot", "query"],
        ["snapshot", "value", "rows"],
        ["value", "numeric", "selected field"],
        ["numeric", "classify", "yes"],
        ["classify", "view", "color"],
        ["numeric", "unknown", "no", "dashed"],
        ["unknown", "view", "gray", "dashed"],
      ],
    },
    bridgette: {
      caption: "Bridgette exposes generated SQL and rows before streaming a result-grounded narrative.",
      nodes: [
        ["question", "input", "Inventory question", "counts · rankings · trends"],
        ["sql", "behavior", "Cerebras SQL", "generate one SELECT"],
        ["guard", "decision", "Read-only\nSELECT?", ""],
        ["db", "behavior", "SQLite", "execute · limit 500"],
        ["meta", "output", "Query evidence", "SQL · columns · rows"],
        ["summary", "output", "Streamed answer", "NDJSON tokens · done"],
        ["error", "guard", "Safe failure", "invalid query or runtime error"],
      ],
      edges: [
        ["question", "sql", "prompt"],
        ["sql", "guard", "candidate"],
        ["guard", "db", "yes"],
        ["db", "meta", "result"],
        ["meta", "summary", "ground"],
        ["guard", "error", "no", "dashed"],
      ],
    },
    kpis: {
      caption: "The selected snapshot changes the population, while the condition classification formula stays fixed.",
      nodes: [
        ["year", "input", "Snapshot year", "latest loaded year by default"],
        ["ratings", "input", "Bridge ratings", "Deck · Super · Sub · Culvert"],
        ["control", "behavior", "Controlling rating", "min(Deck, Super, Sub)"],
        ["fallback", "guard", "Culvert fallback", "only when all three are absent"],
        ["classify", "behavior", "Condition class", "Good ≥7 · Fair 5–6 · Poor <5"],
        ["aggregate", "behavior", "Aggregation", "statewide + county"],
        ["dashboard", "output", "KPI dashboard", "cards · donut · bars · PDF"],
      ],
      edges: [
        ["year", "ratings", "scope"],
        ["ratings", "control", "available"],
        ["ratings", "fallback", "all absent", "dashed"],
        ["fallback", "control", "substitute", "dashed"],
        ["control", "classify", "rating"],
        ["classify", "aggregate", "class"],
        ["aggregate", "dashboard", "render"],
      ],
    },
    history: {
      caption: "History preserves observation provenance, then branches into a persistent baseline forecast and a read-only what-if.",
      nodes: [
        ["identity", "input", "Bridge + horizon", "1–50 years"],
        ["sources", "input", "Observation sources", "snapshot · report · derived"],
        ["precedence", "behavior", "Provenance merge", "snapshot > report > fallback"],
        ["cohort", "behavior", "Markov cohort", "metric · age · county · traffic"],
        ["baseline", "behavior", "Baseline forecast", "expected state + ±1σ"],
        ["scenario", "behavior", "Maintenance what-if", "learned recovery bump"],
        ["chart", "output", "History view", "solid · dashed · dotted + events"],
        ["saved", "state", "Forecast rows", "persistent SQLite run"],
      ],
      edges: [
        ["identity", "sources", "scope"],
        ["sources", "precedence", "resolve"],
        ["precedence", "cohort", "observed state"],
        ["cohort", "baseline", "transition"],
        ["cohort", "scenario", "selected year"],
        ["baseline", "chart", "baseline"],
        ["scenario", "chart", "what-if"],
        ["baseline", "saved", "POST", "dashed"],
      ],
    },
    forecast: {
      caption: "The forecast selects the most specific supported fleet cohort, advances state probabilities, and exposes uncertainty.",
      nodes: [
        ["inputs", "input", "Forecast variables", "metric · state · age · county · traffic · work"],
        ["cohort", "behavior", "Cohort selector", "county + age band + traffic"],
        ["support", "decision", "At least 25\ntransition-years?", ""],
        ["fallback", "guard", "Progressive fallback", "remove traffic, age, county → statewide"],
        ["markov", "behavior", "Markov walk", "p(t+1) = p(t) × T"],
        ["uncertainty", "behavior", "Expected state", "Σ state × p · ±1σ"],
        ["result", "output", "Forecast result", "component paths · confidence · backtest"],
      ],
      edges: [
        ["inputs", "cohort", "calibrate"],
        ["cohort", "support", "sample size"],
        ["support", "markov", "yes"],
        ["support", "fallback", "no", "dashed"],
        ["fallback", "markov", "supported matrix", "dashed"],
        ["markov", "uncertainty", "annual state"],
        ["uncertainty", "result", "render"],
      ],
    },
    lcca: {
      caption: "LCCA compares a do-nothing path with a fixed condition-triggered policy; it does not optimize every possible year.",
      nodes: [
        ["inputs", "input", "Scenario inputs", "bridge · horizon · unit costs · rate"],
        ["walk", "behavior", "Markov condition walk", "age-aware expected annual drop"],
        ["trigger", "decision", "Condition\nthreshold?", ""],
        ["routine", "behavior", "Do nothing", "routine + poor penalty"],
        ["capital", "behavior", "Triggered policy", "rehab 3.0–4.9 · replace ≤3.0"],
        ["npv", "behavior", "Discounted cash flow", "annual costs + capital − salvage"],
        ["decision", "output", "Planning result", "charts · NPV · savings · recommendation"],
      ],
      edges: [
        ["inputs", "walk", "assumptions"],
        ["walk", "trigger", "year state"],
        ["trigger", "routine", "no"],
        ["trigger", "capital", "yes"],
        ["routine", "npv", "cash flow"],
        ["capital", "npv", "cash flow"],
        ["npv", "decision", "compare"],
      ],
    },
    loader: {
      caption: "The loader validates one authoritative NJ snapshot, relationalizes it, and refreshes that year transactionally.",
      nodes: [
        ["payload", "input", "SNBI JSON[] + year", "nonempty · NJ · 1990–2100"],
        ["validate", "decision", "Schema\nvalid?", ""],
        ["fanout", "behavior", "Relational fan-out", "main + child + provenance groups"],
        ["refresh", "behavior", "Year refresh", "replace rows for selected snapshot"],
        ["receipt", "output", "Load receipt", "loaded · refreshed · skipped"],
        ["products", "output", "Available inventory", "Map · KPI · History · Editor"],
        ["reject", "guard", "Validation report", "record errors; no partial load"],
      ],
      edges: [
        ["payload", "validate", "upload"],
        ["validate", "fanout", "yes"],
        ["fanout", "refresh", "transaction"],
        ["refresh", "receipt", "counts"],
        ["receipt", "products", "visible"],
        ["validate", "reject", "no", "dashed"],
      ],
    },
    converter: {
      caption: "Forward conversion is load-oriented; reverse conversion is a visibly separate, lossy export path.",
      nodes: [
        ["legacy", "input", "Legacy input", "NBI TXT/CSV + optional XML"],
        ["snbi", "input", "Modern input", "SNBI JSON object or array"],
        ["forward", "behavior", "Forward convert", "parse · map · normalize · NJ filter"],
        ["reverse", "behavior", "Reverse convert", "flatten · recode · representative values"],
        ["json", "output", "SNBI JSON", "coverage + parity + load-ready rows"],
        ["files", "output", "Legacy downloads", "NBI .txt + Elements XML"],
        ["loss", "guard", "Lossy boundary", "missing over-records skipped"],
      ],
      edges: [
        ["legacy", "forward", "POST /convert"],
        ["forward", "json", "emit"],
        ["snbi", "reverse", "POST /reverse"],
        ["reverse", "files", "emit"],
        ["reverse", "loss", "best effort", "dashed"],
      ],
    },
    preprocess: {
      caption: "A bridge attachment becomes a reusable, profile-pinned evidence cache before any question is answered.",
      nodes: [
        ["file", "input", "Stored bridge attachment", "PDF · image · Office · text · CSV"],
        ["extract", "behavior", "Format extraction", "page text · media · page renders"],
        ["text", "behavior", "Text pipeline", "500-word chunks · 100 overlap"],
        ["image", "behavior", "Image pipeline", "Cerebras technical descriptions"],
        ["embed", "behavior", "Nomic document vectors", "search_document: · 768-D"],
        ["cache", "output", "Parsed evidence cache", "chunks · vectors · images · profile · status"],
      ],
      edges: [
        ["file", "extract", "background ingest"],
        ["extract", "text", "text"],
        ["extract", "image", "images"],
        ["text", "embed", "chunks"],
        ["image", "embed", "descriptions"],
        ["embed", "cache", "profile match"],
      ],
    },
    retrieval: {
      caption: "Bridge SQL evidence and ranked report text run concurrently, then merge into one source-labeled answer context.",
      nodes: [
        ["question", "input", "Bridge question", "selected database + reports"],
        ["db", "behavior", "Database evidence", "bridge-scoped read-only SQL"],
        ["rank", "behavior", "Nomic report retrieval", "query vector · profile check · text/table rank"],
        ["reembed", "guard", "Re-embed cache", "model/revision/dimension mismatch"],
        ["context", "behavior", "Grounded context", "merge rows + ranked chunks + labels"],
        ["answer", "output", "Cerebras stream", "NDJSON tokens + sources"],
      ],
      edges: [
        ["question", "db", "parallel"],
        ["question", "rank", "parallel"],
        ["rank", "reembed", "profile mismatch", "dashed"],
        ["db", "context", "rows"],
        ["rank", "context", "chunks"],
        ["context", "answer", "ground"],
      ],
    },
    ask: {
      caption: "The checkbox is a hard, request-scoped gate: text/database retrieval and visual detection never silently blend.",
      nodes: [
        ["request", "input", "Bridge question", "selected sources + checkbox"],
        ["gate", "decision", "Visual inspection\nchecked?", ""],
        ["text", "behavior", "Text / DB route", "read-only SQL + text/table chunks"],
        ["images", "behavior", "Visual route", "image-description candidate retrieval"],
        ["detect", "behavior", "Cerebras detection", "bbox + anomaly refinement"],
        ["answer", "output", "Grounded answer", "streamed text + source labels"],
        ["overlay", "output", "Inspection result", "annotated matches + metadata"],
      ],
      edges: [
        ["request", "gate", "submit"],
        ["gate", "text", "no"],
        ["text", "answer", "stream"],
        ["gate", "images", "yes"],
        ["images", "detect", "candidates"],
        ["detect", "overlay", "matches only"],
      ],
    },
    pointcloud: {
      caption: "Hosted analysis is CPU-first; Cerebras reviews geometry decisions without invoking the laptop GPU.",
      nodes: [
        ["scan", "input", "Scan + analysis options", "registered points · scale · mode · hosted AI"],
        ["sample", "behavior", "Validate + sample", "clearance or structural cap"],
        ["clean", "behavior", "CPU cleaning", "octree candidates · clutter decision"],
        ["segment", "behavior", "Geometry pipeline", "PCA · segment · cluster · fit"],
        ["review", "behavior", "Cerebras review", "multi-view label + material"],
        ["result", "output", "Analysis result", "labels · components · clearance · NPZ/PLY/JSON"],
      ],
      edges: [
        ["scan", "sample", "points"],
        ["sample", "clean", "sample"],
        ["clean", "segment", "kept points"],
        ["segment", "review", "candidate fits"],
        ["review", "result", "expand labels"],
      ],
    },
    editor: {
      caption: "The selected year scopes every request; only scalar main-row fields persist from the current editor.",
      nodes: [
        ["scope", "input", "Bridge + year", "selected snapshot identity"],
        ["read", "behavior", "Scoped read", "GET row + validation + metadata"],
        ["edit", "decision", "Main scalar\nfield?", ""],
        ["save", "behavior", "Persist main row", "PUT immutable keys excluded"],
        ["export", "behavior", "Client-only nested edit", "retain for export"],
        ["refresh", "behavior", "Refetch truth", "validation reports; does not block"],
        ["result", "output", "Editor result", "saved row + JSON/CSV exports"],
      ],
      edges: [
        ["scope", "read", "GET"],
        ["read", "edit", "user edit"],
        ["edit", "save", "yes"],
        ["edit", "export", "nested", "dashed"],
        ["save", "refresh", "PUT"],
        ["refresh", "result", "server truth"],
        ["export", "result", "download"],
      ],
    },
    custom: {
      caption: "Definitions can be statewide while rows stay bridge-scoped; global deletion and last-write-wins are explicit boundaries.",
      nodes: [
        ["definition", "input", "Table definition", "name · columns · color · scope"],
        ["persist", "behavior", "Persist definition", "bridge or statewide"],
        ["partition", "behavior", "Bridge row partition", "JSON values keyed by bridge"],
        ["edit", "behavior", "Editable grid", "save entire row on blur"],
        ["table", "output", "Operational table", "non-SNBI bridge data"],
        ["delete", "guard", "State delete", "removes global definition"],
        ["conflict", "guard", "Concurrency", "last write wins"],
      ],
      edges: [
        ["definition", "persist", "create"],
        ["persist", "partition", "scope"],
        ["partition", "edit", "rows"],
        ["edit", "table", "render"],
        ["persist", "delete", "delete", "dashed"],
        ["edit", "conflict", "simultaneous", "dashed"],
      ],
    },
  };

  const graphRoot = document.querySelector("#contract-graph");
  const graphShell = document.querySelector("#contract-panel .contract-graphviz");
  const graphCaption = document.querySelector("#contract-graph-caption");
  const graphRuntime = document.querySelector("#contract-graph-runtime");
  const compactLayout = window.matchMedia("(max-width: 700px)");
  const inlineMounts = [
    ["#slide-3", "map", ".runtime-trace"],
    ["#slide-4", "locate", ".sequence-board"],
    ["#slide-5", "conditions", ".switch-controls"],
    ["#slide-6", "bridgette", ".bridgette-flow"],
    ["#slide-7", "kpis", ".kpi-audit"],
    ["#slide-9", "history", ".provenance-board"],
    ["#slide-10", "forecast", ".cohort-funnel"],
    ["#slide-11", "lcca", ".parameter-ledger"],
    ["#slide-12", "loader", ".fanout-board"],
    ["#slide-13", "converter", ".dual-pipeline"],
    ["#slide-14", "editor", ".snapshot-board"],
    ["#slide-15", "custom", ".scope-diagram"],
    ["#slide-16", "retrieval", ".ask-flow"],
    ["#slide-17", "preprocess", ".ingest-funnel"],
    ["#slide-18", "retrieval", ".cache-decision"],
    ["#slide-19", "ask", ".vision-gate"],
    ["#slide-20", "pointcloud", ".cleaning-waterfall"],
  ];
  const renderTokens = new WeakMap();
  let vizPromise = null;
  let currentKey = "";

  const palette = {
    input: { background: "#f1ddd4", border: "#b95434", kicker: "#8e3d26", text: "#17282b" },
    behavior: { background: "#dcebe7", border: "#4f8f81", kicker: "#386d62", text: "#17282b" },
    output: { background: "#f5e7ad", border: "#a97916", kicker: "#76540f", text: "#17282b" },
    guard: { background: "#f4f6f4", border: "#82908d", kicker: "#5c6b68", text: "#17282b" },
    state: { background: "#253b3f", border: "#8cc5b8", kicker: "#8cc5b8", text: "#f8faf8" },
  };

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function escapeDot(value) {
    return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
  }

  function wrapText(value, maxLength = 28) {
    const words = String(value).split(/\s+/);
    const lines = [];
    let line = "";

    words.forEach((word) => {
      const candidate = line ? `${line} ${word}` : word;
      if (candidate.length > maxLength && line) {
        lines.push(line);
        line = word;
      } else {
        line = candidate;
      }
    });
    if (line) lines.push(line);
    return lines;
  }

  function htmlLines(value, color, size) {
    return wrapText(value)
      .map((line) => `<FONT FACE="Arial" POINT-SIZE="${size}" COLOR="${color}">${escapeHtml(line)}</FONT>`)
      .join("<BR ALIGN=\"LEFT\"/>");
  }

  function buildNode([id, type, title, detail]) {
    if (type === "decision") {
      return `${id} [shape=diamond, width=1.18, height=.78, margin=".08,.04", style="filled", fillcolor="#fff8df", color="#a97916", penwidth=1.35, fontname="Arial", fontsize=9.5, fontcolor="#5d4517", label="${escapeDot(title)}"];`;
    }

    const colors = palette[type] || palette.behavior;
    const kicker = type === "state" ? "PERSISTENCE" : type.toUpperCase();
    const detailRow = detail
      ? `<TR><TD ALIGN="LEFT" BALIGN="LEFT">${htmlLines(detail, colors.text, 8)}</TD></TR>`
      : "";

    return `${id} [shape=plain, label=<
      <TABLE BORDER="1" COLOR="${colors.border}" CELLBORDER="0" CELLSPACING="0" CELLPADDING="6" BGCOLOR="${colors.background}" WIDTH="158">
        <TR><TD ALIGN="LEFT"><FONT FACE="Courier New" POINT-SIZE="7" COLOR="${colors.kicker}"><B>${kicker}</B></FONT></TD></TR>
        <TR><TD ALIGN="LEFT" BALIGN="LEFT">${htmlLines(title, colors.text, 11)}</TD></TR>
        ${detailRow}
      </TABLE>
    >];`;
  }

  function buildEdge([from, to, label, style]) {
    const attributes = [
      `label="${escapeDot(label)}"`,
      style === "dashed" ? 'style="dashed"' : "",
      style === "dashed" ? 'color="#8a9794"' : "",
    ]
      .filter(Boolean)
      .join(", ");
    return `${from} -> ${to} [${attributes}];`;
  }

  function buildDot(spec) {
    const rankdir = compactLayout.matches ? "TB" : "LR";
    const denseGraph = spec.nodes.length >= 8;
    const nodeSeparation = compactLayout.matches ? ".18" : denseGraph ? ".14" : ".2";
    const rankSeparation = compactLayout.matches ? ".32" : denseGraph ? ".26" : ".4";

    return `digraph feature_contract {
      graph [
        rankdir="${rankdir}",
        bgcolor="transparent",
        pad=".08",
        margin="0",
        nodesep="${nodeSeparation}",
        ranksep="${rankSeparation}",
        splines="polyline",
        outputorder="edgesfirst"
      ];
      node [fontname="Arial"];
      edge [
        color="#6d827d",
        fontcolor="#536461",
        fontname="Courier New",
        fontsize=7.5,
        penwidth=1.25,
        arrowsize=.62
      ];
      ${spec.nodes.map(buildNode).join("\n")}
      ${spec.edges.map(buildEdge).join("\n")}
    }`;
  }

  function getViz() {
    if (!vizPromise) {
      if (!window.Viz?.instance) {
        return Promise.reject(new Error("Viz.js is unavailable"));
      }
      vizPromise = window.Viz.instance();
    }
    return vizPromise;
  }

  async function renderInto(key, root, shell, caption, runtime, fallbackTarget = null) {
    const spec = featureGraphs[key];
    if (!spec || !root || !shell) return;

    const token = (renderTokens.get(root) || 0) + 1;
    renderTokens.set(root, token);
    shell.classList.add("is-loading");
    shell.removeAttribute("data-error");
    root.setAttribute("aria-busy", "true");
    if (!shell.classList.contains("is-ready")) {
      root.innerHTML = '<span class="graphviz-loading">Laying out execution graph&hellip;</span>';
    }
    if (caption) caption.textContent = spec.caption;

    try {
      const viz = await getViz();
      const svg = viz.renderSVGElement(buildDot(spec), { engine: "dot" });
      if (token !== renderTokens.get(root)) return;

      svg.removeAttribute("width");
      svg.removeAttribute("height");
      svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      root.replaceChildren(svg);
      root.setAttribute("aria-label", `${key} input, behavior, output, and guardrail graph`);
      root.setAttribute("aria-busy", "false");
      shell.classList.add("is-ready");
      shell.classList.remove("is-loading");
      if (runtime) runtime.textContent = `Graphviz ${viz.graphvizVersion} · DOT`;
      if (fallbackTarget) fallbackTarget.setAttribute("aria-hidden", "true");
    } catch (error) {
      if (token !== renderTokens.get(root)) return;
      root.setAttribute("aria-busy", "false");
      root.textContent = "Execution graph unavailable; the original feature trace remains visible.";
      shell.classList.remove("is-loading");
      shell.dataset.error = "true";
      console.warn("BridgeGuard feature graph could not render.", error);
    }
  }

  async function render(key) {
    currentKey = key;
    return renderInto(key, graphRoot, graphShell, graphCaption, graphRuntime);
  }

  function mountInlineGraph([slideSelector, key, targetSelector]) {
    const slide = document.querySelector(slideSelector);
    const target = slide?.querySelector(targetSelector);
    const spec = featureGraphs[key];
    if (!slide || !target || !spec || slide.querySelector(`.feature-graph-inline[data-feature-graph="${key}"]`)) return;

    const figure = document.createElement("figure");
    figure.className = "contract-graphviz feature-graph-inline";
    figure.dataset.featureGraph = key;
    figure.setAttribute("aria-label", `${key} execution graph`);
    figure.innerHTML = `
      <header class="graphviz-toolbar">
        <div>
          <small>Feature execution graph</small>
          <strong class="inline-graph-runtime">Graphviz runtime · DOT</strong>
        </div>
        <div class="graphviz-legend" aria-label="Graph node legend">
          <span class="input">Input</span>
          <span class="behavior">Behavior</span>
          <span class="output">Output</span>
          <span class="boundary">Boundary</span>
        </div>
      </header>
      <div class="graphviz-canvas" role="img" aria-busy="true">
        <span class="graphviz-loading">Laying out execution graph&hellip;</span>
      </div>
      <figcaption class="graphviz-caption">${escapeHtml(spec.caption)}</figcaption>
    `;

    target.classList.add("graphviz-inline-fallback");
    target.before(figure);
    renderInto(
      key,
      figure.querySelector(".graphviz-canvas"),
      figure,
      figure.querySelector(".graphviz-caption"),
      figure.querySelector(".inline-graph-runtime"),
      target,
    );
  }

  function renderAllInlineGraphs() {
    document.querySelectorAll(".feature-graph-inline").forEach((figure) => {
      const key = figure.dataset.featureGraph;
      renderInto(
        key,
        figure.querySelector(".graphviz-canvas"),
        figure,
        figure.querySelector(".graphviz-caption"),
        figure.querySelector(".inline-graph-runtime"),
        figure.nextElementSibling?.classList.contains("graphviz-inline-fallback") ? figure.nextElementSibling : null,
      );
    });
  }

  inlineMounts.forEach(mountInlineGraph);
  compactLayout.addEventListener?.("change", () => {
    render(currentKey || "map");
    renderAllInlineGraphs();
  });

  window.BridgeGuardFeatureGraphs = {
    render,
    keys: Object.keys(featureGraphs),
  };
})();
