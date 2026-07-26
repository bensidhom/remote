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
    if (buttonIndex === activeSlide) {
      button.setAttribute("aria-current", "true");
    } else {
      button.removeAttribute("aria-current");
    }
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
  slides[destination].scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "start" });
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

  if ("IntersectionObserver" in window) {
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
  } else {
    document.documentElement.classList.add("no-observer");
  }
}

const contractData = {
  map: {
    title: "Map explorer",
    route: "GET /api/map-data",
    inputTitle: "state · county · year · condition key",
    inputCopy: "Filter selections or a Bridgette focus request.",
    processTitle: "query + normalize + render",
    processCopy: "Returns map-safe identity, location and rating fields.",
    outputTitle: "markers + selected bridge card",
    outputCopy: "Actionable map context keyed by state and structure ID.",
    persists: "No — read-only view state",
    guardrail: "Invalid coordinates are excluded from rendering.",
  },
  bridgette: {
    title: "Bridgette",
    route: "POST /api/bridgette/stream",
    inputTitle: "plain-language bridge question",
    inputCopy: "A statewide question, aggregation request or map navigation command.",
    processTitle: "route intent + query data",
    processCopy: "Map requests resolve locally; analytical questions stream through the database assistant.",
    outputTitle: "streamed answer or map focus",
    outputCopy: "Text, tabular findings or a focused bridge card.",
    persists: "No — conversation is session state",
    guardrail: "Generated SQL is constrained to read-only database access.",
  },
  converter: {
    title: "SNBI converter",
    route: "POST /api/convert",
    inputTitle: "NBI TXT + optional element XML",
    inputCopy: "Legacy inventory and element exchange files.",
    processTitle: "parse + map + normalize + validate",
    processCopy: "Builds nested SNBI records and reports coverage and parity issues.",
    outputTitle: "SNBI JSON + validation summary",
    outputCopy: "A load-ready New Jersey bridge array plus actionable warnings.",
    persists: "No — conversion returns a downloadable payload",
    guardrail: "Hosted build retains New Jersey state code 34 only.",
  },
  loader: {
    title: "Inventory loader",
    route: "POST /api/load-json",
    inputTitle: "SNBI JSON[] + data year",
    inputCopy: "Normalized bridge records with BL01 and BID01 identity.",
    processTitle: "schema check + relational load",
    processCopy: "Validates identity, state scope and nested table relationships.",
    outputTitle: "year-versioned bridge dataset",
    outputCopy: "Queryable map, KPI, history and editor records.",
    persists: "Yes — SQLite inventory tables",
    guardrail: "Editor role required; non-NJ records are rejected.",
  },
  reports: {
    title: "Bridge report ingest + Q&A",
    route: "POST /.../reports → POST /api/db-rag/stream",
    inputTitle: "bridge file, then bridge question",
    inputCopy: "Upload evidence up to 50 MB; query the selected bridge in a separate request.",
    processTitle: "store + background ingest; retrieve + answer",
    processCopy: "The upload starts a cached 768-D Nomic index; Q&A streams from database and selected report sources.",
    outputTitle: "upload receipt; grounded answer",
    outputCopy: "The first route returns status, ID and ingestion state. The second streams answer content and sources.",
    persists: "Yes — source file, parse cache and embeddings",
    guardrail: "Hosted extraction is best-effort and has no OCR; profile mismatch triggers re-embedding.",
  },
  vision: {
    title: "Document and image vision",
    route: "POST /api/vision-rag",
    inputTitle: "file or image + question",
    inputCopy: "Standalone visual/document investigation with grounding controls.",
    processTitle: "ingest + retrieve + visual reasoning",
    processCopy: "Indexes document context, then routes text and visual questions.",
    outputTitle: "answer + cited regions/pages",
    outputCopy: "Structured or narrative result with image/page references.",
    persists: "Cached index; session chooses the active document",
    guardrail: "Cancel and reset endpoints bound long-running work.",
  },
  pointcloud: {
    title: "Point-cloud upload + analysis",
    route: "POST /.../point-cloud → POST /.../analyze → GET /.../processed/download",
    inputTitle: "raw file, then registered scan or point payload",
    inputCopy: "Upload E57, LAS, PLY, XYZ, PCD, CSV, TXT or NPZ; analyze it in a separate request.",
    processTitle: "parse + CPU clean + segment + fit",
    processCopy: "Numerical geometry is combined with bounded Cerebras scene judgment.",
    outputTitle: "analysis JSON + saved artifacts",
    outputCopy: "The analysis response describes components and clearance; PLY is downloaded through a separate route.",
    persists: "Best-effort — bridge analysis saves NPZ, PLY and JSON on success",
    guardrail: "Editors upload/delete; current route enforcement also lets viewers run analysis. Operate one large job at a time.",
  },
  editor: {
    title: "SNBI editor",
    route: "PUT /api/bridge/{state}/{bridge} + GET /api/validate-bridge/...",
    inputTitle: "bridge identity + main-record field edits",
    inputCopy: "Scalar fields from the primary bridge row; nested feature, element, span and work tables are excluded.",
    processTitle: "update row; validate separately",
    processCopy: "The save endpoint updates the main row. The editor requests SNBI validation as a separate operation.",
    outputTitle: "updated row + validation response",
    outputCopy: "Saved bridge data is returned first; flags and errors come from the validation endpoint.",
    persists: "Yes — primary bridge row in SQLite",
    guardrail: "Editor role required for the main row; nested-table mutation is outside this save and requires admin/raw-table routes.",
  },
  decision: {
    title: "Forecast and LCCA",
    route: "GET /api/forecasts/bridge/... · GET /api/lcca/bridge/...",
    inputTitle: "history + horizon + treatment assumptions",
    inputCopy: "Observed component ratings and user-adjustable planning parameters.",
    processTitle: "forecast + simulate + discount costs",
    processCopy: "Generates condition paths and compares intervention scenarios.",
    outputTitle: "condition curve + lifecycle cost",
    outputCopy: "Bridge-level timing, risk and cost evidence.",
    persists: "No for LCCA — read-only calculation using separately persisted forecast state",
    guardrail: "Forecast confidence depends on available historical observations.",
  },
  admin: {
    title: "Database administration",
    route: "/api/table-rows/* · /api/table/*/row · /api/sql-query",
    inputTitle: "table, query or row mutation",
    inputCopy: "Schema-aware database maintenance reserved for the owner.",
    processTitle: "authorize + validate + execute",
    processCopy: "Exposes controlled table reads, edits and system information.",
    outputTitle: "rows, schema or mutation result",
    outputCopy: "Administrative database state with explicit success/failure response.",
    persists: "Yes — direct database administration",
    guardrail: "Admin role required; editors cannot access raw database controls.",
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
    element.textContent = contract[field];
  });
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
  tabList.setAttribute("aria-orientation", mobileTabs.matches ? "horizontal" : "vertical");
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
