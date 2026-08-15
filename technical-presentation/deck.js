const slides = Array.from(document.querySelectorAll(".slide"));
const indexButtons = Array.from(document.querySelectorAll(".slide-index button"));
const previousButton = document.querySelector("#previous-slide");
const nextButton = document.querySelector("#next-slide");
const currentSlideLabel = document.querySelector("#current-slide");
const totalSlidesLabel = document.querySelector("#total-slides");
const progressBar = document.querySelector("#progress-bar");

let activeSlide = 0;
let navigationLocked = false;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const initialHashIndex = slides.findIndex((slide) => `#${slide.id}` === window.location.hash);
let slideObserver = null;
if (initialHashIndex >= 0) document.documentElement.classList.add("is-initializing");

const pad = (value) => String(value).padStart(2, "0");

function updateDeckState(index, { updateHash = true } = {}) {
  activeSlide = Math.max(0, Math.min(index, slides.length - 1));

  slides.forEach((slide, slideIndex) => {
    slide.classList.toggle("is-visible", slideIndex === activeSlide);
  });

  indexButtons.forEach((button, buttonIndex) => {
    if (buttonIndex === activeSlide) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });

  currentSlideLabel.textContent = pad(activeSlide + 1);
  totalSlidesLabel.textContent = pad(slides.length);
  progressBar.style.width = `${((activeSlide + 1) / slides.length) * 100}%`;
  previousButton.disabled = activeSlide === 0;
  nextButton.disabled = activeSlide === slides.length - 1;
  document.title = `BridgeGuard — ${slides[activeSlide].dataset.title}`;

  if (updateHash && history.replaceState) {
    history.replaceState(null, "", `#${slides[activeSlide].id}`);
  }
}

function goToSlide(index, { focus = true, updateHash = true } = {}) {
  const destination = Math.max(0, Math.min(index, slides.length - 1));
  if (navigationLocked || destination === activeSlide) return;

  navigationLocked = true;
  slides[destination].scrollIntoView({
    behavior: reducedMotion.matches ? "auto" : "smooth",
    block: "start",
  });
  updateDeckState(destination, { updateHash });
  if (focus) slides[destination].focus({ preventScroll: true });

  window.setTimeout(() => {
    navigationLocked = false;
  }, 500);
}

previousButton.addEventListener("click", () => goToSlide(activeSlide - 1));
nextButton.addEventListener("click", () => goToSlide(activeSlide + 1));

indexButtons.forEach((button) => {
  button.addEventListener("click", () => goToSlide(Number(button.dataset.slide)));
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const isInteractive =
    target instanceof Element &&
    target.closest('a, button, input, textarea, select, [contenteditable="true"], [role="tab"], [role="tabpanel"], [tabindex="0"]');

  if (isInteractive || event.altKey || event.ctrlKey || event.metaKey) return;

  if (["ArrowRight", "ArrowDown", "PageDown", " "].includes(event.key)) {
    event.preventDefault();
    goToSlide(activeSlide + 1);
  } else if (["ArrowLeft", "ArrowUp", "PageUp"].includes(event.key)) {
    event.preventDefault();
    goToSlide(activeSlide - 1);
  } else if (event.key === "Home") {
    event.preventDefault();
    goToSlide(0);
  } else if (event.key === "End") {
    event.preventDefault();
    goToSlide(slides.length - 1);
  }
});

function startSlideObserver() {
  if (slideObserver || document.documentElement.classList.contains("no-observer")) return;
  if (!("IntersectionObserver" in window)) {
    document.documentElement.classList.add("no-observer");
    return;
  }

  slideObserver = new IntersectionObserver(
    (entries) => {
      if (navigationLocked) return;
      const visibleEntry = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];

      if (!visibleEntry) return;
      const index = slides.indexOf(visibleEntry.target);
      if (index >= 0 && index !== activeSlide) updateDeckState(index);
    },
    { threshold: [0.45, 0.65, 0.85] },
  );
  slides.forEach((slide) => slideObserver.observe(slide));
}

const contractData = {
  map: {
    title: "ArcGIS map",
    route: "GET /api/map-counties · GET /api/map-data",
    inputTitle: "county · snapshot year · condition key · basemap",
    inputCopy: "The current UI is New Jersey-fixed; a blank year means latest per bridge.",
    processTitle: "query + coordinate guard + client FeatureLayer",
    processCopy: "Valid records are deduped, normalized into WKID 4326, and colored by the selected numeric condition field.",
    outputTitle: "markers + condition legend + bridge popup",
    outputCopy: "The popup exposes identity, primary ratings, dimensions, and bridge actions.",
    persists: "No — read-only view state",
    guardrail: "Invalid coordinates are removed; categorical and missing ratings render gray.",
  },
  locate: {
    title: "Show me bridge",
    route: "sidebar event → GET /api/map-data?state_code=34",
    inputTitle: "desktop sidebar phrase with bridge number or name",
    inputCopy: "Example: “Show bridge 0313151 on the map.”",
    processTitle: "detect intent + rank statewide index + synchronize filters",
    processCopy: "Number, name, and location matches set the target year/county before ArcGIS queries the marker.",
    outputTitle: "zoom 16 + existing bridge popup",
    outputCopy: "Success, ambiguity, and not-found statuses return to the sidebar.",
    persists: "No — statewide search index is component-lifetime cache",
    guardrail: "Desktop sidebar only; target must have valid map coordinates.",
  },
  bridgette: {
    title: "Bridgette database analyst",
    route: "POST /api/bridgette/stream",
    inputTitle: "plain-language statewide inventory question",
    inputCopy: "Questions can request counts, comparisons, rankings, or time trends.",
    processTitle: "Cerebras SELECT + read-only guard + SQLite execution",
    processCopy: "The route emits SQL metadata and rows, then streams a result-grounded summary.",
    outputTitle: "SQL + columns + rows + NDJSON tokens",
    outputCopy: "Queries are limited to 500 rows; the UI exposes up to 100.",
    persists: "No — page conversation is React memory",
    guardrail: "Generated SQL must be read-only; cancellation is currently process-global.",
  },
  kpis: {
    title: "Inventory KPIs",
    route: "GET /api/kpis · GET /api/kpis/county",
    inputTitle: "selected inventory year",
    inputCopy: "The UI defaults to the newest loaded snapshot.",
    processTitle: "controlling rating + state/county aggregation",
    processCopy: "Minimum Deck/Super/Sub determines class; Culvert is fallback only if all three are absent.",
    outputTitle: "cards + donut + county stack + PDF",
    outputCopy: "Good 7–9, Fair 5–6, Poor below 5, and derived Unknown.",
    persists: "No — read-only snapshot calculation",
    guardrail: "Average age uses the current calendar year, not the selected inventory year.",
  },
  history: {
    title: "History and Markov forecast",
    route: "GET /api/bridge-history · POST /api/forecasts/train-bridge",
    inputTitle: "bridge identity + observations + 1–50 year horizon",
    inputCopy: "Snapshot, report-derived, and fallback observations are resolved by provenance.",
    processTitle: "fleet Markov cohort + uncertainty + maintenance simulation",
    processCopy: "Metric, rating, county, age band, traffic band, and work years select the transition matrix.",
    outputTitle: "observed/forecast lines + event timeline",
    outputCopy: "Dashed baseline, deck ±1σ band, and dotted learned-recovery what-if.",
    persists: "Forecast runs persist; simulations do not",
    guardrail: "Derived backcast is not an inspection; Culvert is not drawn in the History UI.",
  },
  lcca: {
    title: "Life-cycle cost analysis",
    route: "GET /api/lcca/bridge/{state}/{bridge}",
    inputTitle: "bridge + horizon + NHS/cost/discount assumptions",
    inputCopy: "Defaults use NJ replacement and rehab unit costs with a 3% discount rate.",
    processTitle: "Markov condition walk + fixed trigger policy + NPV",
    processCopy: "Compare do-nothing with rehab whenever 3.0 < rating ≤ 4.9; replace at ≤3.0.",
    outputTitle: "condition/cost charts + recommendation + savings",
    outputCopy: "Capital steps, routine cost, poor penalty, and discounted salvage are exposed.",
    persists: "No — read-only calculation",
    guardrail: "This is condition-triggered policy comparison, not optimization over every rehab year.",
  },
  loader: {
    title: "SNBI inventory loader",
    route: "POST /api/load-json",
    inputTitle: "nonempty NJ SNBI JSON[] + authoritative year",
    inputCopy: "First record requires BL01 and BID01; selected year must be 1990–2100.",
    processTitle: "validate + relationalize + rebuild same-year rows",
    processCopy: "Main and nine child/provenance groups are refreshed for the chosen snapshot.",
    outputTitle: "loaded/refreshed/skipped counts + validation",
    outputCopy: "Records become available to Map, KPI, History, Forecast, and Editor.",
    persists: "Yes — SQLite inventory snapshots",
    guardrail: "Editor role required; current all-existing response can say conflict after rows were refreshed.",
  },
  converter: {
    title: "NBI ⇄ SNBI converter",
    route: "POST /api/convert · POST /api/reverse-convert",
    inputTitle: "NBI TXT/CSV + optional FHWAED XML, or SNBI JSON",
    inputCopy: "Forward parses fixed-width/header/positional formats; reverse accepts object or array.",
    processTitle: "map + normalize + NJ filter; or flatten + recode",
    processCopy: "Forward reports coverage/parity. Reverse uses representative legacy values where needed.",
    outputTitle: "SNBI JSON or NBI .txt + Elements XML",
    outputCopy: "Forward output is load-ready; reverse output is explicitly best effort.",
    persists: "No — conversion returns downloadable payloads",
    guardrail: "Reverse is lossy; missing over-records are skipped.",
  },
  preprocess: {
    title: "File preprocessing",
    route: "POST /api/bridge/{state}/{bridge}/attachments · background ingestion",
    inputTitle: "bridge attachment set + file signature",
    inputCopy: "PDF, image, Office, text, and CSV uploads are accepted up to 50 MB.",
    processTitle: "store + extract + chunk + describe + embed",
    processCopy: "Text uses 500/100 chunks; Cerebras describes technical images and Nomic embeds document content.",
    outputTitle: "persistent report cache + indexing status",
    outputCopy: "Chunks, 768-D vectors, extracted images, page renders, signature, and embedding profile are reusable.",
    persists: "Yes — attachment files and parsed cache",
    guardrail: "No OCR; legacy Office and plain-text extraction is limited in the hosted parser.",
  },
  retrieval: {
    title: "Grounded retrieval",
    route: "POST /api/db-rag/stream · Nomic TEI query embedding",
    inputTitle: "bridge question + selected database/report sources",
    inputCopy: "The bridge identity and explicit source selection scope every retrieval operation.",
    processTitle: "concurrent read-only SQL + report chunk ranking",
    processCopy: "Nomic query vectors retrieve text/table chunks while image-description chunks stay behind the visual gate.",
    outputTitle: "evidence bundle + Cerebras answer stream",
    outputCopy: "Ranked context and source labels ground the NDJSON response.",
    persists: "Parsed indexes persist; question and answer state do not",
    guardrail: "Embedding-profile mismatches re-embed parsed content; vector dimensions are never mixed.",
  },
  ask: {
    title: "Bridge Ask / visual investigation",
    route: "POST /api/db-rag/stream · POST /api/bridge-visual-investigation",
    inputTitle: "bridge question + selected sources + visual checkbox",
    inputCopy: "Database and report sources are selected independently.",
    processTitle: "hard request-scoped route branch",
    processCopy: "Unchecked retrieves SQL and text/table chunks; checked retrieves candidate images and runs detection.",
    outputTitle: "text/source stream or annotated detections",
    outputCopy: "The two paths do not silently blend.",
    persists: "Parsed caches persist; checkbox and conversation do not",
    guardrail: "Checkbox defaults false; flattened PDFs may have no extracted images for bridge inspection.",
  },
  pointcloud: {
    title: "Point-cloud analysis",
    route: "POST /.../point-cloud · POST /.../analyze",
    inputTitle: "registered scan + scale/mode/AI options",
    inputCopy: "LAS, PLY, E57, XYZ, PCD, CSV, TXT, or NPZ.",
    processTitle: "CPU clean + PCA + segment + fit + Cerebras review",
    processCopy: "Labels expand to the full cloud; clearance uses cleaned road/water and soffit evidence.",
    outputTitle: "components + labels + clearance + warnings",
    outputCopy: "Successful bridge analyses save NPZ, PLY, and JSON artifacts.",
    persists: "Yes — one raw cloud and best-effort processed artifacts per bridge",
    guardrail: "One large job at a time; new raw upload deletes the prior processed result.",
  },
  editor: {
    title: "Year-scoped SNBI editor",
    route: "PUT /api/bridge/{state}/{bridge}?year=YYYY",
    inputTitle: "bridge snapshot + scalar main-row edits",
    inputCopy: "Identity, data year, and nested keys are immutable or excluded.",
    processTitle: "save one main row + refetch validation/metadata",
    processCopy: "All requests use the same selected year; validation reports but does not block.",
    outputTitle: "updated main row + validation + exports",
    outputCopy: "Nested client edits can be exported but are not persisted by Editor.",
    persists: "Main row only",
    guardrail: "The Editor UI currently depends on admin-only /api/table/* reads.",
  },
  custom: {
    title: "Custom operational tables",
    route: "POST /api/custom-tables · POST/PUT/DELETE rows",
    inputTitle: "name + columns + color + bridge/state scope",
    inputCopy: "State scope shares the definition; rows remain keyed to each bridge.",
    processTitle: "persist definition + JSON-cell row values",
    processCopy: "Cells save the full row on blur.",
    outputTitle: "editable non-SNBI bridge tables",
    outputCopy: "Definitions and rows live outside inventory import/export.",
    persists: "Yes — custom_tables + custom_table_rows",
    guardrail: "State-definition deletion is global; concurrent cell edits are last-write-wins.",
  },
};

const contractFields = {
  title: document.querySelector("#contract-title"),
  route: document.querySelector("#contract-route"),
  inputTitle: document.querySelector("#contract-input-title"),
  inputCopy: document.querySelector("#contract-input-copy"),
  processTitle: document.querySelector("#contract-process-title"),
  processCopy: document.querySelector("#contract-process-copy"),
  outputTitle: document.querySelector("#contract-output-title"),
  outputCopy: document.querySelector("#contract-output-copy"),
  persists: document.querySelector("#contract-persists"),
  guardrail: document.querySelector("#contract-guardrail"),
};

const contractTabs = Array.from(document.querySelectorAll("[data-contract]"));

function selectContract(key, { focus = false } = {}) {
  const contract = contractData[key];
  if (!contract) return;

  contractTabs.forEach((tab) => {
    const selected = tab.dataset.contract === key;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected) document.querySelector("#contract-panel").setAttribute("aria-labelledby", tab.id);
    if (selected && focus) tab.focus();
  });

  Object.entries(contractFields).forEach(([field, element]) => {
    if (element) element.textContent = contract[field];
  });

  window.BridgeGuardFeatureGraphs?.render(key);
}

contractTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectContract(tab.dataset.contract));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();

    let nextIndex = index;
    if (["ArrowDown", "ArrowRight"].includes(event.key)) nextIndex = (index + 1) % contractTabs.length;
    if (["ArrowUp", "ArrowLeft"].includes(event.key)) nextIndex = (index - 1 + contractTabs.length) % contractTabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = contractTabs.length - 1;
    selectContract(contractTabs[nextIndex].dataset.contract, { focus: true });
  });
});

navigationLocked = initialHashIndex >= 0;
updateDeckState(initialHashIndex >= 0 ? initialHashIndex : 0, { updateHash: initialHashIndex < 0 });
selectContract("map");

const tabList = document.querySelector(".contract-tabs");
const mobileTabs = window.matchMedia("(max-width: 850px)");
const updateTabOrientation = () => {
  tabList?.setAttribute("aria-orientation", mobileTabs.matches ? "horizontal" : "vertical");
};
updateTabOrientation();
mobileTabs.addEventListener?.("change", updateTabOrientation);

window.addEventListener("hashchange", () => {
  const hashIndex = slides.findIndex((slide) => `#${slide.id}` === window.location.hash);
  if (hashIndex >= 0) goToSlide(hashIndex, { focus: true, updateHash: false });
});

const initializeDeckPosition = () => {
  const hashIndex = slides.findIndex((slide) => `#${slide.id}` === window.location.hash);
  if (hashIndex >= 0) {
    slides[hashIndex].scrollIntoView({ behavior: "auto", block: "start" });
    updateDeckState(hashIndex, { updateHash: false });
  }
  window.requestAnimationFrame(() => {
    if (hashIndex >= 0) slides[hashIndex].scrollIntoView({ behavior: "auto", block: "start" });
    window.setTimeout(() => {
      document.documentElement.classList.remove("is-initializing");
      navigationLocked = false;
      startSlideObserver();
    }, 120);
  });
};

if (document.readyState === "complete") {
  window.requestAnimationFrame(initializeDeckPosition);
} else {
  window.addEventListener("load", initializeDeckPosition, { once: true });
}
