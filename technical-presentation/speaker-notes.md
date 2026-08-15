# BridgeGuard Technical Presentation — Detailed Speaker Notes

This document is the detailed narration companion for the 24-slide BridgeGuard HTML presentation in `technical-presentation/index.html`. It is written so that it can be read as a presentation script, shortened into speaker notes, or used as stand-alone technical documentation. Each slide explains what the audience is seeing, the inputs that enter the feature, the behavior that occurs in the application and backend, the outputs returned to the user, and the edge cases or limitations that should be stated explicitly.

---

## Slide 1 — From inventory to evidence.

BridgeGuard is presented here as a bridge-centered decision workspace, not simply as a map and not simply as a chatbot. The application brings together structured SNBI inventory data, older NBI source data, inspection reports, office documents, images, and point clouds. Those sources have very different structures and levels of reliability, so the product’s main job is to keep them connected to a stable bridge identity and turn them into traceable evidence. A user should be able to begin with a statewide question, narrow the result geographically, open a specific bridge, inspect its condition history, forecast deterioration, compare life-cycle actions, and then return to the original files or database rows supporting the result.

The cover graphic summarizes that movement from raw inventory to an evidence-backed decision. The hosted application is a React interface served through a FastAPI application. Its durable operational data is stored in SQLite and on persistent file storage. Cerebras is used for language generation and visual-language judgment, while the exact pinned Nomic model creates retrieval embeddings. CPU geometry—not an LLM—is authoritative for point-cloud distances, fitted shapes, and clearance measurements. Cloudflare Access is the identity gate in front of the private application. This separation of responsibilities is deliberate: language models interpret and summarize, deterministic code validates and measures, and the database preserves the record.

The key message for the audience is that every answer should retain a visible evidence chain. BridgeGuard does not replace engineering judgment or an official inspection. It helps a reviewer find the relevant record, understand how a result was produced, see where inference has been introduced, and decide what requires human verification.

**Presenter emphasis:** The product promise is “one place to follow the evidence,” not “an AI makes the engineering decision.”

---

## Slide 2 — One bridge is the unit of work.

This slide explains the organizing principle used throughout the application. The stable unit of work is a bridge in a particular inventory year, identified primarily by state code, bridge number, and data year. A bridge number by itself is not always enough because the same structure can have multiple annual snapshots, and a year by itself is not enough because every annual table contains many structures. When a bridge card opens, this identity is passed to its history, attached files, point-cloud analysis, Ask workflow, LCCA calculation, custom operational data, and editor. That prevents a report or forecast from silently drifting to another bridge or another year.

The visual stack represents five evidence layers. The inventory layer contains normalized SNBI fields and related child tables. The report layer contains parsed text, tables, images, captions, and page references. The image layer supports visual inspection only when the user explicitly enables that branch. The point-cloud layer contains the uploaded source, processed labels, fitted geometric artifacts, and clearance results. The history layer distinguishes actual annual observations from report-derived values and inferred fallback values. Each layer can be missing without invalidating the bridge record; the interface should expose what is present rather than fabricate completeness.

This design also explains the transition between statewide and bridge-scoped features. The map, KPIs, and Bridgette can search across the inventory. Selecting a result establishes the bridge identity and opens the evidence stack for one structure. From that point forward, the default behavior is bridge-scoped unless the interface explicitly returns to a statewide view. If two users later work concurrently, the same model remains valid, but chat threads, cancellation tokens, active documents, and temporary selections must be associated with the authenticated user or tenant rather than kept in process-global state.

**Presenter emphasis:** The bridge-and-year key is the thread connecting every feature; it is the first thing to verify when a result appears inconsistent.

---

## Slide 3 — Geography is a query, not a background image.

The ArcGIS map is generated from inventory data rather than used as a decorative basemap. Its principal inputs are the selected county, inventory year, condition field, and basemap. The current interface is fixed to New Jersey, represented by state code 34. The year selection determines which counties are available, and the county-plus-year selection determines which bridge rows are returned. The application calls the county endpoint and map-data endpoint, validates coordinates, removes duplicate bridge features, converts valid records to point features in WKID 4326, and loads them into an ArcGIS client-side FeatureLayer. Changing the basemap changes only the visual context; it does not alter the inventory query.

The condition selector controls how the same returned bridge set is symbolized. For numeric component ratings, values from 7 through 9 are shown as Good in green, values 5 and 6 are Fair in amber, and values from 0 through 4 are Poor in red. Missing, nonnumeric, or categorical values are gray because they cannot be placed honestly on the numeric condition scale. Some sparse fields—such as railings, bearings, or joints—therefore produce many gray markers. Categorical fields such as scour can also remain gray unless a separate categorical renderer is defined. There is no additional “Critical” visual class in the current renderer, so a value of 0 belongs to the Poor class instead of receiving a fourth color.

The output is an interactive layer whose popup carries the bridge identity and selected condition context. Only features with usable latitude and longitude can be displayed and focused. A bridge can exist in the database and still be absent from the map if its coordinates are missing or invalid. Similarly, a count on the KPI page can exceed the number of map markers because the KPI is an inventory calculation while the map has a geospatial validity requirement.

**Presenter emphasis:** A map refresh is a database snapshot transformed into spatial features. A gray or absent point is often a data-shape issue, not evidence that the bridge does not exist.

---

## Slide 4 — “Show bridge 0313151 on the map.”

The “show bridge” command in the desktop sidebar is a deterministic navigation feature. It is intentionally separate from Bridgette’s full database-analysis workflow because the user’s goal is not to generate a narrative answer; it is to find one known bridge and place it in view. The command parser detects the show/find/locate intent and extracts the likely bridge identifier or name. The application searches a statewide index built from the latest available map data and ranks candidates using bridge number first, then bridge name, and then location text. A bridge-number match is preferred because names and facility descriptions can be repeated or inconsistently abbreviated.

Once a unique bridge is found, the interface synchronizes the selected inventory year and county to the bridge record. It waits for the appropriate map layer to load, locates the corresponding feature, zooms to approximately level 16, and opens the popup or bridge card. This synchronization is important: zooming before the county or year refresh completes would focus a point from the wrong snapshot or fail because the feature has not yet been created. The visible output is therefore more than a camera movement. It is a consistent UI state containing the right year, county, feature, popup, and bridge identity.

Several guardrails are part of the behavior. If the query matches multiple bridges, the application should show an ambiguity message rather than silently pick an arbitrary structure. If there is no match, it should keep the current map state and report that the bridge was not found. If the inventory record exists but has invalid coordinates, the application can identify the bridge but cannot focus it spatially. This command also depends on the statewide lookup being current; if a newly loaded bridge is not yet represented in the index, the user may need a refresh before it becomes discoverable.

**Presenter emphasis:** This is a precise UI command with deterministic ranking and state synchronization, not a free-form LLM guess.

---

## Slide 5 — Every switch rebuilds one comparable snapshot.

This slide shows the behavior of the county, year, and condition controls as a connected query system. The user may change any of these controls, but they do not all affect the data in the same way. Changing the year first constrains the list of counties that actually have rows for that year. Changing the county or year changes the set of bridge records returned. Changing the condition field normally keeps the bridge identities fixed and recomputes only the displayed condition value, legend, and symbology. The basemap remains presentation-only. Treating these controls separately avoids accidental comparisons between different bridge populations.

The example chart uses actual Burlington County distributions from 2021 through 2025. The important idea is not merely that the bars change; it is that every bar belongs to a clearly labeled year and to a consistent rating rule. When the selected component has a valid 0–9 numeric rating, the application classifies it as Good, Fair, or Poor. When the selected field is categorical or largely missing, the gray portion increases. A gray result must not be interpreted as structurally poor. It means the chosen visualization rule cannot translate the stored value into the selected numeric condition categories.

This behavior also affects how screenshots and verbal comparisons should be presented. A statement such as “the county has more Poor bridges” is meaningful only when the selected year, county boundary, component field, and missing-value treatment are held constant. The application rebuilds the snapshot after each relevant input change so that the map, popup values, count summaries, and any bridge selection all describe the same state. If a selected county has no records in a newly selected year, the interface must clear or replace the invalid county selection instead of leaving stale markers visible.

**Presenter emphasis:** Always name the county, year, condition field, and missing-data rule when presenting a visual comparison.

---

## Slide 6 — Bridgette turns language into audited rows.

Bridgette is the statewide database analyst. Its input is a natural-language question about the structured inventory, such as “How many Poor bridges are in each county in 2025?” or “Show the trend in deck condition for Burlington County.” The request includes the conversation text and the available database schema. Cerebras generates a candidate SQL statement, but the statement is not executed immediately. Backend guards require a single read-only `SELECT`, reject mutation or unsafe constructs, and add a maximum `LIMIT 500` when the query does not already contain a smaller bound. The protected SQL is then executed against SQLite.

The output is streamed as newline-delimited JSON. Early metadata events expose the generated SQL and returned rows so the user can audit what was actually queried. A later stream contains the grounded natural-language explanation produced from those rows. The interface displays at most 100 rows even though the backend query limit is 500, keeping the page usable while preserving a bounded analytic result. Current-count questions should use the latest data year, trend questions should group by year, and county questions require the proper county relation or normalized field instead of searching arbitrary report text.

This workflow deliberately separates row retrieval from narrative generation. If the SQL is wrong, the visible rows and query provide evidence for correction. If no rows are returned, the model must not invent a statewide conclusion. Conversation context currently lives in the page session rather than a durable per-user history, and cancellation is still process-global. That is acceptable for a limited pilot but must be moved to user-scoped state before robust concurrent use. Bridgette should also not be confused with the sidebar “show bridge” command: Bridgette analyzes database rows, while the sidebar command navigates the map to one structure.

**Presenter emphasis:** The auditable unit is not the prose answer; it is the guarded SQL plus the rows from which the answer was written.

---

## Slide 7 — A snapshot becomes a statewide condition profile.

The KPI view summarizes one selected inventory year. Its first calculation is the controlling condition rating for each bridge. The application takes the minimum valid rating among deck, superstructure, and substructure because the weakest primary component controls the simplified profile. A culvert rating is used only as a fallback when all three primary component ratings are absent. The controlling value is then classified as Good at 7 or higher, Fair from 5 through 6, and Poor below 5. This is a transparent product rule for statewide summarization; it is not a replacement for every agency-specific performance definition.

For the 2025 snapshot shown in the deck, the database contains 6,825 bridges: 2,125 Good, 4,316 Fair, and 384 Poor under this controlling-rating rule. The profile also reports an average age of 59.5 years and 4,549 bridges built before 1980. The age calculation currently uses the present calendar year rather than the selected inventory year. That implementation detail should be made clear when comparing historical KPI pages, because selecting an older inventory year does not rewind the “current-year minus year-built” age formula.

The outputs include summary cards, a condition donut, county comparison bars, and a sortable bridge table. The export is a rendered PDF or screenshot of the visible analysis rather than a new official inventory file. Missing component ratings can reduce the number of bridges with a controlling condition even though those bridges remain part of the total inventory. County distributions and statewide totals should therefore show or explain missing-data counts when the audience is using them for management decisions.

**Presenter emphasis:** KPI cards are a reproducible view of one year under a stated controlling-rating rule; they are not independent measurements.

---

## Slide 8 — The bridge card opens the evidence stack.

The bridge card is the transition from statewide exploration to a bridge-specific workspace. It receives the bridge identity and selected data year from the map, table, or search result. The card exposes the core inventory facts and opens a modal or tabbed stack containing History, Files, Point Cloud, LCCA, Ask, Custom, and Edit. Each child feature inherits the same bridge number and year so that the user does not need to reselect the structure for every task.

The tabs represent different kinds of work rather than interchangeable views. History shows annual observed and inferred condition values. Files stores and preprocesses attached reports and images. Point Cloud cleans, segments, fits, and measures 3D data. LCCA turns a deterioration path and cost assumptions into discounted policy comparisons. Ask retrieves evidence for a question about this bridge. Custom stores operational fields that are not part of the SNBI schema. Edit modifies permitted main inventory fields for the selected year and exports nested changes. Opening a tab may trigger its own API calls, but those calls remain anchored to the parent bridge identity.

The workspace is intentionally tolerant of incomplete evidence. A bridge may have inventory history but no report, a report but no extracted image, or a point cloud without known metric scale. The user should see an empty state explaining the missing layer rather than a fabricated result. The workspace should also display the active year prominently because a user can otherwise mistake a current file attachment or point-cloud result for an older inventory snapshot. In a future multi-user design, files and analysis artifacts can remain bridge-shared while chat history, selections, temporary edits, and job ownership become user-scoped.

**Presenter emphasis:** The card is a routing boundary that preserves context. Every opened tool should be able to answer, “Which bridge and which year am I operating on?”

---

## Slide 9 — Observed, report-derived, and inferred are not the same.

The History view constructs a condition timeline using a strict source precedence. An actual year-scoped database snapshot is the strongest source. A value extracted from an inspection report is secondary and should retain its file and page provenance. If no historical observation is available, the UI may display a derived backcast as a visual fallback. The fallback formula is `min(9, current_rating + 0.08 × year_gap)`, which assumes that older condition was modestly better and caps the result at 9. It is an inference used to keep the chart interpretable, not an official inspection record.

The line style communicates this provenance. Solid segments represent observed historical values. Forecast segments are dashed. A maintenance or intervention scenario is dotted. The deck forecast can include a plus-or-minus one standard deviation band to show uncertainty around its expected path. Culvert condition is not currently drawn as a separate History series, so culvert-only structures may have less complete charts even though a culvert value can participate elsewhere as a KPI fallback.

When the user runs a production forecast, the result can be saved and displayed with the historical series. A simulation or temporary scenario remains read-only unless the user explicitly saves it. The chart should not visually join points across missing years in a way that suggests annual inspections occurred. If an observed snapshot and a report-derived value disagree, the snapshot wins under the current precedence, but the discrepancy should be available for review rather than silently erased. Dates, source labels, and line styles are therefore part of the evidence model, not simply cosmetic chart choices.

**Presenter emphasis:** The audience should be able to distinguish “measured,” “extracted,” “modeled,” and “scenario” without reading source code.

---

## Slide 10 — The production forecast is fleet-trained Markov.

The deterioration forecast uses a discrete-state Markov approach trained from changes observed across the bridge fleet. It does not use Prophet. The main inputs are the selected component metric, the bridge’s current 0–9 condition state, an age band, county, traffic band, and recorded work years. The training process counts one-year transitions between condition states for comparable records and builds a transition matrix. A cohort must contain at least 25 transition-years; if it is too small, the system progressively relaxes traffic, age, and county filters until it reaches a usable statewide cohort.

The model advances a probability vector with `p(t+1) = p(t) × T`, where `T` is the selected transition matrix. The expected condition rating is the probability-weighted state value, and the standard deviation represents dispersion across possible states. For a cold start with insufficient history, the fallback assumes a 10 percent probability of declining one state in a year and otherwise remaining stable. Recorded work years that coincide with a condition increase are retained as recovery samples. Unexplained increases are excluded from ordinary deterioration transitions so that data corrections or inconsistent ratings do not teach the model that untreated bridges improve spontaneously.

The deck cites an internal 2025 holdout backtest of 18,841 predictions, with mean absolute error 0.09, bias of -0.051, and 98.5 percent of predictions within plus or minus one rating point. These numbers describe performance on the available internal annual data and should not be presented as external engineering validation. The output includes the expected trajectory, uncertainty, selected cohort information, and fallback level. Bridge 0107152 is used as an example. Sparse cohorts, missing current ratings, and future policy changes can materially alter the appropriate matrix.

**Presenter emphasis:** The forecast is an empirically trained state-transition model with visible fallbacks; the uncertainty and cohort definition matter as much as the center line.

---

## Slide 11 — Condition drives the policy. Discounting drives the comparison.

The LCCA feature compares a fixed condition-trigger policy with a baseline over a user-selected horizon. Inputs include the bridge, a horizon from 5 to 100 years, an NHS selection, optional unit-cost overrides, and a discount rate. The interface exposes common horizons of 20, 30, 40, 50, 75, and 100 years. Default cost assumptions are a blended New Jersey replacement cost of approximately $800 per square foot—$840 for NHS and $775 for non-NHS—rehabilitation at $300 per square foot, routine maintenance at $1.50 per square foot per year, a Poor-condition user or risk penalty at $1.00 per square foot per year, and a 3 percent annual discount rate.

Condition evolves through the Markov walk. Under the current trigger policy, a rating greater than 3.0 and no more than 4.9 causes rehabilitation, while a rating of 3.0 or below causes replacement and resets the condition state to 9. The present value is the sum of discounted routine maintenance, Poor-condition penalty, and capital actions, minus any terminal salvage value. The comparison asks whether the trigger policy has a lower net present cost than continuing the baseline path under the same assumptions.

This is a transparent policy simulation, not an exhaustive optimization of every possible intervention year and treatment. If the horizon ends before deterioration reaches a trigger, the model may recommend no capital action. In the examples, bridge 0101150 shows no recommendation over 40 years, while bridge 0220154 over 100 years triggers rehabilitation in 2026 and 2096 and produces approximately $847,000 in modeled savings. Such long-horizon results are highly sensitive to discount rate, deck area, transition behavior, and unit costs. Missing geometry or invalid costs require a guarded fallback or a clear error rather than a confident recommendation.

**Presenter emphasis:** LCCA makes assumptions explicit and comparable; it does not claim to produce a final capital program without agency review.

---

## Slide 12 — One SNBI object fans into a year-scoped database.

The Loader accepts an array of SNBI bridge objects together with an authoritative data year between 1990 and 2100. Each record must provide the state identifier in BL01 and bridge identifier in BID01, and the current deployment is intended for New Jersey records. Before mutation, the loader validates the record shape and key fields. A single SNBI object is then decomposed into the main bridge table and related route, span, substructure, element, inspection, posting, work, feature, and provenance groups. This normalized fan-out allows the map and statewide queries to remain efficient while preserving repeated and nested SNBI structures.

The year is not inferred casually from a filename. It is supplied as an authoritative import parameter and becomes part of the row identity. Loading a bridge for a year that is not already present inserts its main and child rows. Reloading the same bridge and year refreshes the year-scoped rows so updated source data does not produce duplicate annual snapshots. The response reports how many records were loaded, refreshed, skipped, or rejected, together with validation details.

The loader is privileged because it changes shared inventory data. It should run behind an Editor or Admin role, use a transaction, preserve enough provenance to identify the source batch, and produce a recoverable audit record. Partial failures must not leave a main bridge row without its required child updates. A known response-path issue can report a conflict for an all-existing batch after mutation has already occurred; that mismatch should be treated as an implementation defect in the status reporting, not evidence that the refresh was rolled back. The year and bridge key should always be verified before a bulk upload.

**Presenter emphasis:** One file upload is a controlled database refresh, not a passive attachment. The output counts and transaction outcome should be reviewed before trusting the new snapshot.

---

## Slide 13 — Forward conversion is mapped. Reverse conversion is lossy.

The Converter supports migration between legacy NBI material and SNBI-oriented application data. In the forward direction, the input is an NBI TXT or CSV file and, optionally, FHWA element XML. The parser detects fixed-width, header-based, or positional layouts, separates main and over/under records, applies field mappings and code normalization, filters for New Jersey when configured, and validates the resulting SNBI objects. The result is downloadable SNBI JSON plus coverage, parity, skipped-record, and validation information. Those diagnostics are essential because a syntactically valid conversion can still omit fields that were not present or could not be mapped.

In the reverse direction, the input is an SNBI object or batch and the output is a best-effort legacy NBI text file plus Elements XML. This direction is inherently lossy. SNBI can represent structures and values that have no one-to-one legacy item, and multiple normalized SNBI child records may need to collapse into a single older-format field. The reverse converter should therefore be used for exchange or review, not treated as a perfect reconstruction of the original source file.

A “direct transition” label means the FHWA mapping connects a source item directly to a target field; it does not guarantee that the displayed values will be identical across every screen. The legacy TXT value and the loaded SNBI snapshot can originate from different dates or source batches. Numeric parsing, leading-zero handling, type normalization, missing-value rules, or an explicit code recode can also change the visible representation. Missing over-records are skipped, and invalid or unsupported element XML can produce zero converted elements while the main bridge conversion still succeeds. Every discrepancy should be diagnosed with source value, mapping rule, normalized value, data year, and provenance.

**Presenter emphasis:** Mapping lineage explains where a field came from; parity diagnostics explain whether the conversion retained the information.

---

## Slide 14 — Main fields persist. Nested edits export.

The Edit feature operates on one bridge and one data year. It retrieves the year-scoped record, presents permitted values, and accepts a guarded update. Scalar fields in the main bridge table can be persisted. Identity fields such as bridge number, state code, and data year are immutable because changing them in place would break the relationship to child tables, attachments, history, and map features. The backend validates values and reports warnings or errors, but the current workflow does not make every validation message a hard blocker.

Nested structures are handled differently. Changes to repeated routes, spans, elements, inspections, and other related data are assembled into an export package rather than written through the same simple form. The user can download JSON, CSV, or ZIP artifacts for review and controlled loading. This distinction limits the risk of partially updating a complex SNBI object through a flat editor. It also explains why a user may see a changed nested value in an exported artifact but not in the shared database after reopening the bridge.

The output of a successful main-field update should identify the bridge, year, changed fields, and saved values. The map or workspace may require a data refresh before the new value is visible. Two editors changing the same row currently follow last-write behavior unless explicit optimistic concurrency is added. There is also a role-boundary issue to watch: portions of the editor UI depend on raw table endpoints that may be Admin-only even though an Editor is expected to update approved fields. A production hardening pass should separate the safe editor API from unrestricted database administration.

**Presenter emphasis:** “Edit” is intentionally narrower than “rewrite the SNBI object.” Persistent scalar updates and reviewed nested exports are two different contracts.

---

## Slide 15 — Shared structure. Bridge-local rows.

Custom data gives the team a place to record operational information that does not belong in the official SNBI schema—for example, a local review status, district action owner, field note category, or research annotation. A custom table definition can be scoped to one bridge or shared at the state level. A state-level definition means the column structure is reusable across many bridges, while each saved row remains partitioned by bridge identity. Thus two bridges can use the same “Research Review” form without sharing the values entered into that form.

The feature accepts a table name, scope, column definitions, and per-bridge row values. The interface saves the whole row when the user leaves a field. The output is a bridge-local operational record that reappears when that bridge and custom definition are opened again. This is useful for collaborative workflows because teams can introduce small structured datasets without altering the normalized SNBI tables or waiting for a schema migration.

The flexibility introduces governance concerns. Whole-row save on blur means simultaneous edits can overwrite one another under a last-write-wins rule. Duplicate or normalized column names can collide, so definitions require validation. Deleting a state-scoped definition is a global action: it removes the shared structure and can remove rows associated with all bridges using it. That should require Admin authorization, explicit confirmation, and a recoverable audit path. Custom data should also be labeled clearly so users do not mistake a local operational note for an FHWA inventory field or an official inspection result.

**Presenter emphasis:** The schema may be shared, but the data row is still bridge-specific. Scope and deletion impact must be visible before a user changes the structure.

---

## Slide 16 — Choose evidence, then ask one bridge.

Ask is the bridge-scoped evidence assistant. Its inputs are the current bridge identity, the user’s question, and explicit source selections. A user can choose the structured database, attached reports, or both. Database evidence is produced through a bridge-limited text-to-SQL path. Report evidence is produced by retrieving relevant parsed text and table chunks from the bridge’s persistent index. These two retrieval tasks can run concurrently, after which the selected evidence is supplied to Cerebras to generate a streamed answer with source labels.

The interface uses newline-delimited JSON so status, evidence, and answer text can arrive incrementally. A database-only question skips the report index. A reports-only question avoids an unnecessary SQL query. If no sources are selected, the assistant has no bridge evidence and should return a clear prompt to choose a source rather than answer from general model memory. Normal Ask excludes image-description chunks. That separation is intentional because visual anomaly detection has a different confidence and output contract and is only enabled through the visual-inspection checkbox described later.

The answer should cite database fields, report names, pages, tables, or image references wherever possible. A command asking to show a report page or image can display the evidence without running defect detection. Missing parsed content, empty retrieval results, or an invalid SQL result should remain visible as limitations. The assistant must not combine evidence from another bridge simply because its report text is semantically similar. Current cancellation and conversation state still need stronger user scoping for concurrent production use.

**Presenter emphasis:** The user controls the evidence boundary. The model writes from selected, bridge-scoped context rather than searching every file and modality indiscriminately.

---

## Slide 17 — Upload once. Persist the parsed evidence.

File preprocessing begins when a user uploads a supported attachment to a bridge. Accepted formats include PDF, DOC or DOCX, XLS or XLSX, TXT, CSV, PNG, JPG, and JPEG. The current report and converter upload limit is 50 MB. The source file is stored durably, an attachment row is created, and background ingestion updates a status that the interface polls. This allows the original evidence to remain available while derived text, tables, images, and embeddings are rebuilt as needed.

The parser is format-aware. PDF processing uses PyMuPDF to collect machine-readable text, embedded images, nearby captions, and rendered page images. Stand-alone image files are preserved directly. Modern Office files are ZIP packages, so their internal media can be extracted while text and tables are parsed using the appropriate document or spreadsheet libraries. TXT and CSV provide direct textual or tabular content. Legacy Office formats and unusual document encodings have more limited support. Extracted text is divided into approximately 500-word chunks with 100-word overlap. Cerebras produces descriptions for selected images, and the pinned Nomic model embeds searchable chunks.

The persistent index records a cache signature and embedding profile so an attachment does not have to be parsed and embedded for every question. However, the pipeline does not currently perform OCR. A scanned PDF can therefore have page-render images but little or no searchable text. A flattened PDF may also have no separately extracted embedded images even though the page looks visual to a human. The status should distinguish stored, parsing, ready, partially indexed, and failed states. Deleting a file must remove both the source association and its derived searchable artifacts without affecting files attached to other bridges.

**Presenter emphasis:** “Uploaded” means the original was stored; “ready” means the derived evidence was parsed, described, embedded, and validated for retrieval.

---

## Slide 18 — Old and new files share one pinned vector space.

The retrieval pipeline uses the exact Nomic model `nomic-ai/nomic-embed-text-v1.5` at revision `20d1e6057a2e1fa08b45123e41ce9381fc338f0e`. It is served with TEI 1.9.3, uses mean pooling, produces 768-dimensional float32 vectors, normalizes them, and applies `search_document:` to stored chunks and `search_query:` to questions. Pinning all of these details matters because a vector’s numeric dimension alone does not prove compatibility. Pooling strategy, prefixes, normalization, model revision, and output type all affect the vector space.

When a query arrives, the system verifies the cached embedding profile, row count, vector width, and finite numeric values. A cache is reused only when the entire profile matches. If an older attachment was processed with a different service or dimension, the original parsed content is retained and the embeddings are regenerated with the pinned model. The system never pads, truncates, or mixes incompatible vectors. This is how previously uploaded files remain usable when the embedding provider changes: the durable asset is the parsed evidence, while the vector cache is reproducible.

The question is embedded in the same profile, cosine similarity ranks report chunks, and the top evidence is combined with any selected structured database rows before Cerebras writes the response. Retrieval quality depends on the source text, chunk boundaries, query wording, and available provenance. An empty text extraction cannot be repaired by embedding. A cache with the correct width but NaN values must be rejected. The language model is not the embedding model, and Cerebras does not need to produce the vectors for the pipeline to use Cerebras for the final answer.

**Presenter emphasis:** Compatibility is a full embedding contract, not merely “both vectors have 768 numbers.” Parsed source content is preserved so that safe re-embedding is always possible.

---

## Slide 19 — The checkbox is a hard branch.

The visual-inspection checkbox is request-scoped user interface state. It defaults to false, is disabled while a request is running, and disappears or resets when the Ask modal is destroyed. It is not persisted as a shared backend preference and one user’s selection should not affect another user. The request payload carries the checkbox value explicitly, so the backend can choose one of two distinct evidence paths.

When the box is unchecked, Ask uses structured database rows and selected report text or tables. Image-description chunks are excluded, and the response is a text evidence answer. When the box is checked, the system searches image-description embeddings for likely visual candidates, keeps up to eight candidates above an approximate cosine floor of 0.32, and asks Cerebras to refine anomaly descriptions or bounding boxes. The output can include matched images, annotations, and a visual explanation. This is a hard branch rather than an automatic router: the LLM must not silently perform visual inspection simply because the question contains words such as “crack” or “spalling.”

The distinction manages both cost and trust. A report statement that says “cracking observed” is textual evidence; an image region that may show cracking is a separate visual inference. Page-display commands can show an image or report page without enabling detection. A flattened or scanned PDF may have rendered page images but no extracted embedded images, while the current visual search scans extracted image assets rather than all rendered pages. In that case the checked path can return no candidates even though a human sees photographs in the PDF. The correct output is an explanation of the missing visual evidence, not a fallback claim based on text.

**Presenter emphasis:** The checkbox expresses user intent and changes the backend evidence pipeline. It is not a sticky model hint and is not shared among users.

---

## Slide 20 — CPU geometry finds structure; Cerebras reviews candidates.

The point-cloud cleaning pipeline accepts LAS, PLY, E57, XYZ, PCD, CSV, TXT, and NPZ formats. The production override currently allows files up to 250 MB, while the backend’s general default is 500 MB. The request includes whether the scale is known, the analysis mode—clearance or structural—and hosted AI options. Local SAM is disabled. At least 200 usable points are required for the full workflow. To bound memory and processing time on the CPU-only server, the pipeline samples up to roughly 300,000 points for clearance work and about 600,000 for structural work.

Deterministic geometry does the primary work. The pipeline parses coordinates, removes invalid values, applies cleaning and octree logic when metric scale is known, estimates principal axes with PCA, identifies the bridge footprint and under-bridge region, segments candidate surfaces and clusters, and fits preliminary deck, roadway, pier, girder, or parapet geometry. Structural mode is deliberately recall-first so it is less likely to discard a real component before classification. Cerebras receives multi-view renderings and structured candidate summaries to review or relabel ambiguous candidates; it does not directly invent the measured coordinates.

When metric scale is unknown, threshold-based octree cleaning that depends on meters is skipped because its distance cutoffs would be meaningless. The pipeline must report that limitation rather than relabel arbitrary units as meters. Sparse, noisy, vegetation-heavy, or incomplete scans may still produce candidates but lower-confidence classification. Because the deployment is CPU-only, no laptop or server GPU is required. This makes processing slower than a dedicated GPU workflow but keeps deployment predictable and prevents GPU-specific crashes.

**Presenter emphasis:** Numerical geometry is deterministic and CPU-authoritative. The vision-language model is a reviewer of candidates, not the source of the clearance value.

---

## Slide 21 — Labels, fitted solids, clearance, and reusable artifacts.

The point-cloud output contains more than a colored preview. Each original point receives a packed label aligned to its source order. The main classes are ground or road, substructure, soffit, deck or superstructure, unknown, and noise or vegetation. From these labels and fitted candidates, the pipeline can create a deck loft, roadway ribbon, cylindrical or prism pier models, I- or box-girder approximations, and parapet wall geometry. An optional Poisson mesh can be generated when the point distribution is suitable, but the classified points remain the more direct evidence.

Clearance is computed over a 64-by-64 grid in the bridge coordinate frame. Robust cell statistics reduce the influence of isolated outliers. The result can include minimum clearance, center-lane clearance, maximum clearance, and the location associated with the controlling measurement. Waterway cases use different semantic interpretation from roadway cases, so the reported surface pair must be identified. A value should only be labeled in meters when scale is known.

Reusable outputs are written as `processed_cloud.npz`, a processed `.ply`, and `analysis.json`. The NPZ preserves point arrays and labels for machine use, the PLY supports interoperable visualization, and the JSON records measurements, fitted objects, confidence information, and processing metadata. Fewer than 20 usable points cannot support segmentation; fewer than 200 should stop full analysis; a vertical span below approximately 3 meters can trigger a deck-only fallback. If Cerebras is unavailable, deterministic geometry can still produce partial results, but the missing AI review must be stated. Uploading a new cloud for the same bridge currently replaces or deletes the prior processed result, so versioned artifact retention is a future requirement.

**Presenter emphasis:** The screenshot is only one view of the result. The durable product is a labeled, measurable, reusable 3D evidence package with known fallback conditions.

---

## Slide 22 — Inspect each function as input, behavior, output, and guardrail.

This slide is an interactive catalog of fourteen runtime contracts: ArcGIS, Show Bridge, Bridgette, KPIs, History and Forecast, LCCA, Loader, Converter, File Preparation, Retrieval, Ask and Vision, Point Cloud, Editor, and Custom Data. Selecting a tab renders a local Graphviz flow diagram. Each diagram begins with the feature’s inputs, passes through the important processing and decision nodes, and ends with visible outputs. Guardrails, persistence points, and fallback branches are included so the graph communicates actual behavior rather than a marketing happy path.

The diagrams are particularly useful when two features look similar in the interface but have different contracts. Show Bridge produces navigation state; Bridgette produces guarded SQL rows and a summary. Ask uses evidence selected for one bridge; visual inspection follows a separate checked branch. Loader mutates a normalized year-scoped database; Converter produces exchange artifacts and diagnostics. History displays values with provenance; Forecast advances a probability distribution. The contract view lets a technical audience compare these responsibilities without reading every endpoint.

Graphviz is vendored and rendered locally in the presentation, so the deck does not depend on an external graph service or CDN. Seventeen inline feature graphs and the fourteen contract tabs use the same visual language. If the WebAssembly renderer cannot initialize, the page provides a text fallback rather than leaving an empty panel. The graphs are explanatory documentation, not dynamically generated runtime traces, so they must be updated when an endpoint, model profile, role, or persistence rule changes.

**Presenter emphasis:** Read each graph left to right and ask four questions: What enters? What changes? What is returned? What prevents a misleading or unsafe result?

---

## Slide 23 — One application boundary. Specialized model roles.

The hosted architecture has a public landing surface and a Cloudflare Access gate protecting the application. Approved users authenticate with a one-time PIN, and identity headers are forwarded to the FastAPI application. FastAPI serves the React experience and the application APIs. Role checks separate Viewer capabilities from Editor mutations and Admin-only raw database or runtime operations. The durable state consists of SQLite data plus stored reports, parsed artifacts, embedding caches, point clouds, and analysis outputs on the server volume.

External model responsibilities are separated. Cerebras handles natural-language generation, SQL drafting under backend guards, image-description or visual-language judgment, and review of point-cloud candidates. It is not used as the embedding engine. The retrieval system uses the pinned 768-dimensional Nomic model and profile, hosted without Ollama. CPU libraries perform parsing, validation, database queries, Markov simulation, LCCA arithmetic, and point-cloud geometry. The hosted server therefore does not need a GPU, and the local laptop GPU should never be invoked as part of deployment or preprocessing.

The single application boundary is operationally simple but creates concurrency work. Files and bridge records are naturally shared assets, while authentication identity, chat threads, active document selection, cancellation, upload job ownership, and personal history should be scoped by user and eventually by tenant. Cloudflare authenticates access but does not automatically create application-level row ownership. Strong multi-user behavior requires the backend to store the authenticated user ID with session and job records, authorize every bridge or file operation, and replace process-global flags with user- or request-scoped state.

**Presenter emphasis:** “One app” does not mean “one model.” Each model or library has a narrow role, and identity must continue past Cloudflare into application data ownership.

---

## Slide 24 — Technically ready. Concurrency still needs hardening.

The closing slide describes the current operating envelope honestly. Report and converter uploads are limited to 50 MB. Point-cloud uploads use a 250 MB production override. The initial Oracle Cloud deployment target is an x86_64 CPU instance with approximately 2 vCPUs, 12 GB of memory, and persistent storage; no GPU is required. Map, KPI, and editor queries are scoped by year. Uploaded reports, parsed caches, embeddings, point clouds, and analysis results survive application restarts when the persistent volume is mounted correctly.

The role model is Viewer, Editor, and Admin. Viewers can use read-only inventory, maps, chat, and analysis. Editors can upload or delete permitted bridge evidence and update approved main-row fields. Admins retain raw database and runtime controls. Cloudflare Access restricts entry to approved email identities, but application authorization must still verify the forwarded identity and role at each privileged endpoint. Deployment readiness therefore includes a production build, database and volume backup, environment secrets, health checks, reverse-proxy or tunnel configuration, and a CI/CD workflow that tests and packages the same source revision that is deployed.

The remaining hardening milestone is multi-user concurrency. The visual-inspection checkbox is already request-local, but cancellation is process-global, some active-document state is process-global, and chat history is not yet durable per user. A production-ready multi-user design should add authenticated user and tenant identifiers, user-scoped conversations, job records and queues, per-user cancellation tokens, attachment ownership rules, optimistic concurrency for edits, audit logs, quotas, and versioned point-cloud results. These changes do not require a separate deployment for every user; they require explicit isolation inside the shared application.

The final message is that the technical feature set is integrated and deployable for a controlled pilot, while scale and concurrency are a defined engineering phase rather than an invisible assumption. The audience should leave knowing both what works now and what must be completed before wider operational reliance.

**Presenter emphasis:** Readiness is bounded: the application is suitable for a controlled authorized pilot, and the next release should make every temporary or mutable state explicitly user-scoped.

