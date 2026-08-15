# BridgeGuard NBI–SNBI Conversion, Inventory Loading, and Bridgette Database Agent

## An implementation, traceability, and conformance study

**System studied:** BridgeGuard remote repository  
**Repository revision:** <code>8f4d00244c76a43c112380c1e26a65e01112462f</code> (<code>main</code>)  
**Code and source review date:** August 11, 2026  
**Primary application scope:** New Jersey, state FIPS code 34  
**Document status:** Technical research report and implementation audit; not an FHWA certification

---

## Contents

- [Abstract](#abstract)
- [1. Research questions](#1-research-questions)
- [2. Scope, method, and limits](#2-scope-method-and-limits)
- [3. Terminology and data model](#3-terminology-and-data-model)
- [4. Authoritative federal foundation](#4-authoritative-federal-foundation)
- [5. System architecture and lineage](#5-system-architecture-and-end-to-end-lineage)
- [6. Forward conversion orchestration](#6-forward-conversion-request-and-orchestration)
- [7. Input parsing](#7-input-parsing-in-detail)
- [8. Transition-rule engine](#8-transition-rule-engine)
- [9. Field-level forward transition behavior](#9-field-level-forward-transition-behavior)
- [10. Normalization, coverage, and validation](#10-normalization-coverage-and-validation)
- [11. Reverse conversion](#11-reverse-conversion-snbi-to-legacy-shaped-outputs)
- [12–16. Loader, storage, provenance, and history](#12-loader-request-boundary-and-validation)
- [17–23. Bridgette, Ask, and map workflows](#17-bridgette-technical-characterization)
- [24. Authentication and multi-user behavior](#24-authentication-authorization-and-shared-user-behavior)
- [25. Conformance matrix](#25-conformance-matrix-official-rule-versus-current-implementation)
- [26. Edge cases](#26-edge-cases-and-failure-analysis)
- [27. Reproducibility and evaluation](#27-reproducibility-and-evaluation-protocol)
- [28. Remediation roadmap](#28-recommended-remediation-roadmap)
- [29. Conclusion](#29-conclusion)
- [References](#references)
- [Appendix A: exact code maps](#appendix-a-exact-high-use-transition-maps-in-the-current-code)
- [Appendix B: source-code index](#appendix-b-source-code-index)
- [Appendix C: proposed conversion manifest](#appendix-c-recommended-conversion-manifest)
- [Appendix D: operational checklists](#appendix-d-operational-checklists)

---

## Abstract

BridgeGuard accepts legacy National Bridge Inventory (NBI) records, transitions selected values into the Specifications for the National Bridge Inventory (SNBI) data model, optionally merges legacy bridge-element XML, stores annual bridge snapshots in a normalized SQLite database, and exposes those snapshots through maps, editors, analytics, and the Bridgette natural-language database assistant. It can also perform a best-effort reverse transformation from SNBI JSON to legacy-shaped outputs.

This report reconstructs those behaviors from the current source code and compares them with the Federal Highway Administration (FHWA) materials that define the transition: the SNBI specification, NBI-to-SNBI Data Crosswalk, Detailed Code Mapping, JSON submittal schema, validation logic, transition questions and answers, annual NBI downloads, legacy record format, and annual element datasets. It documents the inputs, outputs, algorithms, field transformations, database transaction semantics, provenance chain, role requirements, text-to-SQL workflow, and principal edge cases.

The central finding is that BridgeGuard implements a substantial and useful **application transition pipeline**, but it should not be described as a complete FHWA transition or submission-validation engine. Many direct, recoded, conditional, and unit-conversion rules correspond to FHWA guidance. However, the application currently converts a broader population of over-records than FHWA identifies for transition, creates several convenience placeholders where the legacy data has no valid SNBI transition, preserves only part of the raw conversion provenance, and applies a local advisory validator rather than the complete FHWA validation workbook/module. The reverse conversion is necessarily lossy because the richer, repeated SNBI model cannot be represented exactly in one flat legacy NBI record.

Bridgette is a two-stage, LLM-mediated text-to-SQL workflow: a Cerebras model writes a read-only SQLite <code>SELECT</code>; BridgeGuard executes it locally; a second model call summarizes the returned rows. It does not use Nomic embeddings, inspection reports, point clouds, or visual-language evidence for statewide database questions. Its evidence chain is inspectable—question, SQL, rows, answer—but its SQL correctness and answer faithfulness remain instruction-guided rather than formally proven. The current database and cancellation state are shared across authenticated users, so the deployment is a shared application rather than a tenant-isolated system.

---

## 1. Research questions

This study answers the following questions.

1. What authoritative federal data and transition rules are the intended foundation for BridgeGuard?
2. Which legacy NBI file layouts and element files can the converter parse?
3. How are over-records, under-records, features, routes, spans, conditions, inspections, works, posting data, load ratings, and elements converted?
4. Which outputs are direct transitions, code mappings, calculated values, temporary values, application defaults, or unavailable from legacy data?
5. How are converted or native SNBI JSON records validated and loaded into the database?
6. How are annual snapshots, repeated child datasets, refreshes, provenance, deletions, and exports represented?
7. How does Bridgette turn a question into a database answer, and what evidence is given to the LLM?
8. What safeguards, multi-user behaviors, edge cases, and conformance gaps should be understood before research, operational, or submission use?

---

## 2. Scope, method, and limits

### 2.1 Scope

The report covers four connected pipelines:

1. **Forward conversion:** legacy NBI TXT/CSV plus optional legacy element XML to SNBI-shaped JSON.
2. **Reverse conversion:** SNBI JSON to flat JSON, human-readable text, legacy-shaped CSV, and element XML.
3. **Inventory loading:** SNBI-shaped JSON to annual, normalized SQLite snapshots.
4. **Bridgette:** a natural-language question to generated SQL, read-only query results, and a grounded natural-language answer.

It also distinguishes Bridgette from the separate bridge-scoped Ask/RAG workflow and the deterministic “show bridge on the map” action because those features are easy to conflate but use different data and control paths.

### 2.2 Method

The analysis combined:

- static inspection of the Python backend, React frontend, schema definitions, and tests;
- comparison of implemented code tables and transformations with current official FHWA pages and publications;
- tracing of values from upload boundary through normalized storage and onward to Bridgette;
- review of error handling, authentication, concurrency, and data-provenance behavior; and
- targeted reasoning about non-invertible mappings and database transaction semantics.

Implementation claims are linked to the relevant source file and line. Federal-policy and data-format claims are linked to primary FHWA sources. SQLite read-only behavior is linked to the SQLite URI documentation.

### 2.3 Limits

This is a source-code audit, not an FHWA acceptance test. It does not certify a generated file for NBTIS submission, replace the official FHWA transition tool or validation module, or establish engineering correctness for a particular bridge. The repository does not contain a version-pinned copy and checksum of every FHWA spreadsheet used to derive its code tables, so exact historical rule provenance cannot be reconstructed solely from the codebase.

The report reflects the backend and frontend behavior inspected on August 11, 2026. The repository had unrelated uncommitted presentation files; those do not change the conversion, loading, or Bridgette findings summarized here.

---

## 3. Terminology and data model

| Term | Meaning in this report |
|---|---|
| **NBI** | The legacy National Bridge Inventory data model associated with the FHWA Recording and Coding Guide and its flat record format. |
| **SNBI** | The Specifications for the National Bridge Inventory and its richer bridge, feature, route, span, inspection, work, posting, and element datasets. |
| **Over-record** | A legacy NBI record with Record Type 1, representing the inventory bridge and the feature carried on the bridge. |
| **Under-record** | A legacy NBI record associated with a route beneath the bridge. FHWA distinguishes primary and secondary under-records. |
| **Direct transition** | A source value moves to a target field without a substantive recoding rule, although trimming or representation normalization may occur. |
| **Code-mapped transition** | A legacy code is translated through an FHWA-derived lookup table. |
| **Derived transition** | The target value is calculated from one or more source values, such as DMS coordinates to decimal degrees. |
| **Partial transition** | FHWA indicates that assumptions, temporary codes, or incomplete source detail prevent a complete permanent mapping. |
| **No transition** | The legacy Coding Guide cannot populate the SNBI item; the agency must collect or code it separately. |
| **Temporary code** | A transition-period SNBI code that must be reviewed and replaced. FHWA permits applicable temporary transition codes for 2026 and 2027, not for 2028 and later. |
| **Snapshot** | One bridge’s inventory state for a particular <code>data_year</code>. In BridgeGuard its identity is <code>(state_code, bridge_number, data_year)</code>. |
| **Provenance** | Evidence of the source record and rule used to create or explain a stored value. |
| **Bridgette** | The statewide shared-database natural-language assistant. It is distinct from bridge-scoped report Ask and from the map command router. |

---

## 4. Authoritative federal foundation

### 4.1 SNBI specification and regulatory context

FHWA’s [Specifications for the National Bridge Inventory, March 2022](https://www.fhwa.dot.gov/bridge/snbi/snbi_march_2022_publication.pdf) states that the SNBI was developed with the National Bridge Inspection Standards and is incorporated by reference in the NBIS regulation. It specifies data reported for public highway bridges to FHWA for inclusion in the NBI. It also integrates the element-level reporting previously addressed by the Specification for the National Bridge Inventory Bridge Elements.

FHWA later published [SNBI Errata #1, March 2024](https://www.fhwa.dot.gov/bridge/snbi/errata1_to_snbi_march_2022_publication.pdf). The redlined publication states that the errata is informational and does not itself constitute an official change to the incorporated March 2022 SNBI until future rulemaking. The September 17, 2024 [memo on schema, validation, and NBIS bridge length](https://www.fhwa.dot.gov/bridge/pubs/Memo-SNBI_Updates_Schema_ValidationLogic_NBISBridgeLength.pdf) nevertheless explains that FHWA’s 2026 acceptance procedures are being adapted for data reported in accordance with SNBI and Errata #1. A current implementation must therefore distinguish the incorporated baseline, informational errata, and operational submittal procedures rather than citing only the uncorrected PDF.

The current [FHWA SNBI resource hub](https://www.fhwa.dot.gov/bridge/snbi.cfm) is the controlling index for the specification and errata, implementation memoranda, transition crosswalk, detailed code mapping, submittal schema, transition tool, and validation material. Because those resources can be revised, a production conversion should record the version/date of each artifact used.

### 4.2 FHWA transition crosswalk

The [FHWA Data Crosswalk for Over and Under Records](https://www.fhwa.dot.gov/bridge/snbi/datacrosswalk.cfm) defines relationships between Coding Guide items and SNBI tags. Its general notes are essential to interpreting any converter:

- the legacy model does not identify every repeated feature, route, main-span configuration, or approach-span configuration;
- transition therefore commonly assumes one feature of a type, one route per highway, one main span set, and one approach span set;
- those assumptions must be reviewed where actual bridge configuration differs;
- unlisted source values normally produce null;
- a clean transition maps every legacy code to a permanent SNBI code;
- a partial transition can require assumptions or temporary codes; and
- a “no” transition cannot be populated from Coding Guide data.

The [FHWA Detailed Code Mapping](https://www.fhwa.dot.gov/bridge/snbi/codemapping.cfm) supplies code-by-code transformations used where the primary crosswalk says “See table.” BridgeGuard represents many of these mappings as Python dictionaries in [snbi_rules.py](../backend/app/routers/services/snbi_rules.py#L1) and applies them through helper functions in [snbi_crosswalk.py](../backend/app/routers/services/snbi_crosswalk.py#L42).

### 4.3 Official transition population

FHWA’s crosswalk states that an over-record is transitioned when:

    Record Type = 1
    AND Item 42A ∈ {1, 4, 5, 6, 7, 8}
    AND Item 112 = Y

Under-records transition only when their corresponding over-record transitions. When both a type <code>2</code> and type <code>A</code> under-record exist, type <code>2</code> is the primary under-record. The primary becomes highway feature <code>H02</code>; other under-records become <code>H03</code>, <code>H04</code>, and so on; each associated route is <code>R01</code>.

This population rule is narrower than the current BridgeGuard behavior. The application maps every parsed group that has a type-1 over-record, regardless of Item 112, and later flags some non-highway Item 42A values for review rather than excluding them. Section 15 documents that difference precisely.

### 4.4 Transition timetable

FHWA’s [transition-data Q&A](https://www.fhwa.dot.gov/bridge/snbi/qanda/03.cfm) distinguishes transitioned NBI data from complete, verified SNBI data. FHWA’s current [SNBI validation logic page](https://www.fhwa.dot.gov/bridge/snbi/data_validation_logic.cfm) identifies the transition treatment for 2026 and 2027 and the stricter 2028 submission expectations.

BridgeGuard contains a temporary-code-expiry check for submission year 2028 and later, but only native/nonlegacy validation that reaches the generic code checks can trigger it. <code>/api/convert</code> supplies no submission year and marks every converted bridge legacy; <code>/api/load-json</code> supplies the selected year, but NBI-derived legacy rows return before that check. The local rule therefore does not currently enforce 2028 cleanup for converted legacy output.

### 4.5 Official submittal structure

The [FHWA SNBI submittal schema](https://www.fhwa.dot.gov/bridge/snbi/schema.cfm) uses JSON and repeated arrays to represent many-to-one datasets such as features, routes, spans, inspections, and elements. The schema is also the reason an SNBI bridge cannot generally be collapsed losslessly into one flat legacy NBI record.

### 4.6 Legacy data sources

FHWA’s [annual NBI ASCII archive](https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm) publishes state and agency inventories over many years. The [legacy NBI record-format page](https://www.fhwa.dot.gov/bridge/nbi/format.cfm) specifies the 445-character layout and item positions. As a concrete contemporary example, the [2025 NBI download page](https://www.fhwa.dot.gov/bridge/nbi/ascii2025.cfm) offers both fixed-width and comma-delimited files and distinguishes highway-only downloads from “all records” downloads containing non-highway and routes-under-bridges records.

FHWA states that states and federal agencies submit annual inventory data in the spring, that corrections may occur during the year, and that public-highway data is considered final by June 15. Therefore, “2025 NBI data” should be treated as a versioned source artifact rather than only a year label.

### 4.7 Legacy element data

FHWA’s [2025 NBI element-data page](https://www.fhwa.dot.gov/bridge/nbi/element2025.cfm) explains that element data is submitted annually; collection is required for National Highway System bridges and optional for non-NHS bridges. BridgeGuard’s optional XML parser consumes the legacy <code>FHWAED</code> representation and merges element rows into the converted bridge by exact structure number.

### 4.8 What BridgeGuard downloads automatically

Nothing in the current conversion or loader pipeline automatically downloads, authenticates, or verifies an FHWA annual data file. A user supplies the NBI and optional element files. The database can preserve the parsed over-record, but it does not preserve the official download URL, original filename, archive timestamp, file checksum, or source-page revision. Consequently, it is accurate to say that BridgeGuard **accepts files obtainable from FHWA**; it is not accurate to claim that every loaded record can be cryptographically traced to a particular FHWA download without external records.

---

## 5. System architecture and end-to-end lineage

~~~mermaid
flowchart LR
    A["FHWA annual NBI fixed-width or delimited file"] --> B["POST /api/convert"]
    X["Optional FHWA element XML"] --> B
    B --> C["Parse and group over/under records"]
    C --> D["Apply NBI-to-SNBI rules"]
    D --> E["Normalize shape and report local warnings"]
    E --> F["SNBI-shaped JSON plus _SourceData.nbi"]
    F --> G["POST /api/load-json with selected data year"]
    G --> H["Advisory local validation"]
    H --> I["SQLite bridge snapshot"]
    I --> J["bridges"]
    I --> K["features, routes, spans, substructures"]
    I --> L["elements, inspections, postings, works"]
    I --> M["source provenance and history"]
    J --> N["Bridgette schema prompt"]
    K --> N
    L --> N
    N --> O["Cerebras generates SELECT"]
    O --> P["Read-only SQLite execution"]
    P --> Q["Rows plus generated SQL"]
    Q --> R["Cerebras grounded summary"]
    F --> S["POST /api/reverse-convert"]
    S --> T["Lossy legacy-shaped CSV/text and element XML"]
~~~

The core source locations are:

| Concern | Primary implementation |
|---|---|
| Upload endpoints | [main.py](../backend/app/main.py#L594) |
| NBI and element parsing | [parser.py](../backend/app/routers/services/parser.py#L240) |
| Forward mapping | [mapper.py](../backend/app/routers/services/mapper.py#L63) |
| Code tables and mapping helpers | [snbi_rules.py](../backend/app/routers/services/snbi_rules.py#L1), [snbi_crosswalk.py](../backend/app/routers/services/snbi_crosswalk.py#L42) |
| Shape normalization and coverage | [snbi_schema.py](../backend/app/routers/services/snbi_schema.py#L79) |
| Local validation | [snbi_validation.py](../backend/app/routers/services/snbi_validation.py#L253) |
| Reverse conversion | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L402) |
| Database schema and loading | [database.py](../backend/app/routers/services/database.py#L338), [database.py](../backend/app/routers/services/database.py#L993) |
| Bridgette | [vision.py](../backend/app/routers/services/vision.py#L267), [Bridgette.tsx](../frontend/src/pages/Bridgette.tsx#L26) |
| Authentication and roles | [auth.py](../backend/app/auth.py#L219) |

---

## 6. Forward conversion: request and orchestration

The converter is exposed as <code>POST /api/convert</code> in [main.py](../backend/app/main.py#L594). It accepts:

- required multipart field <code>txt_file</code>: legacy NBI fixed-width or delimited text; and
- optional multipart field <code>xml_file</code>: legacy bridge-element XML.

The browser implementation is in [Converter.tsx](../frontend/src/pages/Converter.tsx#L134). Uploads are written beneath the configured temporary data directory using sanitized names and a shared size-limited reader. The default application upload limit is 50 MB unless changed through configuration.

The endpoint performs these steps in order:

1. Save the NBI upload and optional XML upload to the configured temporary directory.
2. Parse the NBI file into structure-number groups.
3. Parse optional element XML into structure-number groups.
4. For each group, require a type-1 over-record; skip groups that have only under-records.
5. Call <code>map_bridge_to_snbi(over, unders)</code>.
6. Attach any element rows whose <code>STRUCNUM</code> exactly equals the current structure number.
7. Remember the parsed over-record for source provenance.
8. Normalize the generated objects into the application’s recognized SNBI shape.
9. Keep only state code 34 records; count dropped non-New-Jersey records.
10. Reattach <code>_SourceData.nbi</code> to each retained bridge.
11. Compute batch-level field and section coverage.
12. Apply lenient local validation because all converted rows are classified as legacy NBI-derived records.
13. Delete temporary files and return the result.

The response contains the following logical fields:

    status
    bridges[]
    converted
    skipped
    dropped_non_nj
    elements_from_xml
    parity_errors[]
    parity_ok
    coverage
    section_coverage
    validation

<code>elements_from_xml</code> means an XML file was supplied; it does not prove that the parser found valid element rows. Similarly, <code>parity_ok</code> means the local shape normalizer emitted no warnings, not that the file is fully conformant with the FHWA JSON schema or that every bridge is complete.

The endpoint catches per-bridge mapper exceptions, prints an error, increments <code>skipped</code>, and continues. It does not return record-specific mapping error details. A broad final exception handler also catches <code>HTTPException</code>; as written, an intended 400 response such as “no New Jersey bridges found” can be rewrapped as a 500. This is an API error-handling defect rather than a transition rule.

---

## 7. Input parsing in detail

### 7.1 Supported legacy NBI layouts

<code>parse_nbi_txt</code> in [parser.py](../backend/app/routers/services/parser.py#L240) recognizes three layouts:

1. comma-delimited rows with a recognized header;
2. comma-delimited rows without a header, interpreted through the positional <code>field_names</code> list; and
3. no-comma fixed-width records interpreted through explicit zero-based slices corresponding to the official 445-character record.

Header detection uses a set of expected NBI column names. Fixed-width detection checks characteristic early fields, including a plausible state code, structure number, and record type. The fixed-width slice table begins near [parser.py](../backend/app/routers/services/parser.py#L18) and extends through the legacy record.

The parser creates a dictionary keyed by <code>STRUCTURE_NUMBER_008</code>:

    {
      "0313151": {
        "over": {"RECORD_TYPE_005A": "1", "...": "..."},
        "unders": [
          {"RECORD_TYPE_005A": "2", "...": "..."},
          {"RECORD_TYPE_005A": "A", "...": "..."}
        ]
      }
    }

Record type <code>1</code> becomes <code>over</code>; every other record type becomes an <code>under</code>. Rows without a structure number are silently skipped. A later type-1 record for the same structure number replaces the prior <code>over</code> value. The parser catches a file-level exception and prints it, so malformed input can result in an empty or partial conversion without a detailed row-level error report.

FHWA’s delimited files have historically used a single quote as the text qualifier. The Python parser uses the CSV library’s default quote convention. Field cleanup makes many rows usable, but a research-grade ingestion test should include representative files downloaded directly from each FHWA annual page and fields containing embedded commas or quote characters.

### 7.2 Over/under hierarchy

The grouping step preserves enough structure for the mapper to create one carried highway feature and multiple below-bridge features. It does **not** preserve the original raw line or byte offsets; it preserves parsed field-value dictionaries.

<code>determine_primary_under</code> in [utils.py](../backend/app/routers/services/utils.py#L93) implements the primary-under convention by preferring type <code>2</code>, otherwise type <code>A</code>, and then ordering other records as secondary. If malformed source data contains multiple type-<code>2</code> rows, the current loop can replace the earlier primary with the last type-<code>2</code> row it sees.

The parser’s dictionary key is only <code>STRUCTURE_NUMBER_008</code>, not <code>(STATE_CODE_001, STRUCTURE_NUMBER_008)</code>. This is a material limitation for an FHWA all-states/all-agencies input: structure numbers are not nationally unique, so an over-record from a later state can overwrite another state’s over-record and under-record lists can be mixed **before** the endpoint applies its New Jersey filter. Element XML is also grouped only by <code>STRUCNUM</code>. Until composite grouping is implemented, the safe operational input is a state-specific New Jersey file whose structure numbers are unique within the batch.

### 7.3 Element XML parsing

<code>parse_elements_xml</code> in [parser.py](../backend/app/routers/services/parser.py#L344) finds <code>.//FHWAED</code> nodes, groups them by <code>STRUCNUM</code>, and maps:

| Legacy XML field | SNBI-shaped field | Meaning |
|---|---|---|
| <code>EN</code> | <code>BE01</code> | Element number |
| <code>EPN</code> | <code>BE02</code> | Parent element number |
| <code>TOTALQTY</code> | <code>BE03</code> | Total element quantity |
| <code>CS1</code> | <code>BCS01</code> | Quantity in condition state 1 |
| <code>CS2</code> | <code>BCS02</code> | Quantity in condition state 2 |
| <code>CS3</code> | <code>BCS03</code> | Quantity in condition state 3 |
| <code>CS4</code> | <code>BCS04</code> | Quantity in condition state 4 |

For an absent child tag, <code>EPN</code>, <code>TOTALQTY</code>, and <code>CS1</code>–<code>CS4</code> default to string <code>"0"</code>; absent <code>EN</code> defaults to blank. A present-but-empty XML tag remains empty rather than being replaced by zero. The parser does not validate XML against an XSD. Namespace-qualified <code>FHWAED</code> tags may not match the simple XPath. XML parse, I/O, and generic errors are caught and produce an empty mapping rather than failing the entire bridge conversion.

### 7.4 Input boundary edge cases

- A bridge with only under-records is counted as skipped.
- An element record whose <code>STRUCNUM</code> differs in padding or normalization from the NBI key is not attached.
- An XML error is not reflected in <code>elements_from_xml</code>.
- A cross-state duplicate structure number can overwrite or mix records before the NJ filter.
- The converter is hard-coded to retain New Jersey state code 34 only.
- Source encoding problems or unusual delimiters may surface as a generic failure.
- No input-file checksum, FHWA URL, annual-release timestamp, or source schema version is recorded.
- Exceptions after temporary files are written can leave those uploads behind because cleanup is not in a <code>finally</code> block.

---

## 8. Transition-rule engine

### 8.1 Design

The forward mapper begins at [mapper.py](../backend/app/routers/services/mapper.py#L63). It reads the parsed over-record, receives an ordered list of under-records, and builds a Python dictionary whose scalar keys use SNBI tags and whose repeated datasets use arrays such as <code>Features</code>, <code>SpanSets</code>, and <code>Inspections</code>.

The implementation separates concerns into three layers:

1. [mapper.py](../backend/app/routers/services/mapper.py#L63) coordinates field population and nested-object construction.
2. [snbi_crosswalk.py](../backend/app/routers/services/snbi_crosswalk.py#L42) provides conservative cleanup, code lookup, date conversion, and multi-field derivation helpers.
3. [snbi_rules.py](../backend/app/routers/services/snbi_rules.py#L1) stores transition code tables, supported code lists, temporary codes, and field guidance.

The helper philosophy is generally conservative: when a source value cannot be mapped with sufficient confidence, return <code>None</code>; the final recursive cleanup removes blank and null values. That behavior is closer to FHWA’s instruction that unlisted values become null than inventing a permanent target code.

### 8.2 Rule taxonomy

Every generated field should be interpreted according to one of the following categories.

| Category | Operation | Example | Evidentiary meaning |
|---|---|---|---|
| Direct | Copy after trimming or type normalization | Item 27 → <code>BW01</code> | Source and target should be substantively equal. |
| Code-mapped | Translate with a lookup table | Item 22 → <code>BCL01</code> | Auditable only against the exact table version. |
| Unit-converted | Apply deterministic units and rounding | Item 49 meters → <code>BG02</code> feet | Recoverable only within rounding precision. |
| Derived | Combine or condition on several items | Items 71 and 26 → <code>BAP02</code> | Requires every source item and the derivation rule. |
| Structural | Convert a flat record into repeated SNBI datasets | Items 42A/42B → <code>Features[]</code> | Relies on FHWA transition assumptions. |
| Temporary | Emit an FHWA transition code | selected material/type mappings | Must be verified/replaced under the FHWA timetable. |
| Application default | Add a value or row for product usability | placeholder substructure set | Not authoritative legacy-derived SNBI data. |
| No transition | Omit the target field | <code>BID02</code> Bridge Name | Requires later agency collection/coding. |

This taxonomy is not currently persisted per output value. The Editor reconstructs explanatory metadata from static mappings and the stored source record, but the database does not store a rule ID/version for every field.

### 8.3 Blank and unknown values

<code>blank_if_unknown</code> and <code>map_lookup</code> in [snbi_crosswalk.py](../backend/app/routers/services/snbi_crosswalk.py#L42) trim source strings and return null for empty or unrecognized values. At the end of mapping, <code>remove_blank_values</code> recursively removes <code>None</code>, empty strings, empty arrays, and empty objects. Thus, a missing target key generally means one of four things:

- the source item was blank;
- the source code was unsupported;
- the FHWA crosswalk has no transition;
- or a conditional rule said “do not report.”

Those meanings are semantically different but are not distinguishable from the final JSON alone.

The null-first policy is not universal. Several numeric fields pass through <code>safe_int</code>, whose default is zero, so blank or invalid source text can become <code>0</code> for fields including state/county/place, skew, lanes, traffic, and traffic year. Blank/invalid Item 92A defaults <code>BIR01</code> to <code>N</code>, and a waterway crossing can receive default <code>BIR03=N</code>. The fallback span also asserts permanent continuity <code>BSP05=1</code> in addition to temporary unknown material/type. These values must be treated as defaults or fallback behavior, not as evidence that the source explicitly reported zero, no, or simple-span continuity.

---

## 9. Field-level forward transition behavior

### 9.1 Identification, location, and classification

Representative implemented mappings are:

| Legacy source | SNBI target | Application behavior | Category |
|---|---|---|---|
| Item 8 Structure Number | <code>BID01</code> | Trim source text | Direct |
| No direct legacy item | <code>BID02</code> | Omitted | No transition |
| Structure-number history unavailable | <code>BID03</code> | Set to <code>0</code> | Application default |
| Item 1 State Code | <code>BL01</code> | Use two-digit state portion | Direct/format |
| Item 3 County | <code>BL02</code> | Copy | Direct |
| Item 4 Place | <code>BL03</code> | Copy | Direct |
| Item 2 District | <code>BL04</code> | Trim leading zeroes | Direct/format |
| Item 16 Latitude | <code>BL05</code> | Packed DMS to signed decimal degrees | Derived |
| Item 17 Longitude | <code>BL06</code> | Packed DMS to signed decimal degrees | Derived |
| Items 98A/98B/99 | <code>BL07</code>–<code>BL10</code> | Border-specific conditional conversion | Mixed |
| Item 9 Location | <code>BL11</code> | Copy | Direct |
| Item 22 Owner | <code>BCL01</code> | Two-character legacy owner map | Code-mapped |
| Item 21 Maintenance | <code>BCL02</code> | Same code domain as owner | Code-mapped |
| Item 105 Federal Lands | <code>BCL03</code> | Code mapping | Partial |
| Item 37 Historic | <code>BCL04</code> | Code mapping, possibly temporary | Partial |
| Item 20 Toll | <code>BCL05</code> | Code mapping | Code-mapped |
| No legacy source | <code>BCL06</code> | Omitted | No transition |

<code>BID03=0</code> deserves specific qualification. FHWA’s crosswalk expects previous-bridge-number data to come from structure-number-change history. An unconditional zero is convenient for the application but does not prove that no previous number exists.

For <code>BID01</code>, FHWA’s transition guidance allows an agency choice about trimming leading zeros and applying that decision to historical data. BridgeGuard cleans surrounding whitespace/quotes but provides no agency-selection workflow and otherwise preserves the leading zeros. This is deterministic, but it is not the full transition-tool choice described by FHWA.

For coordinates, [utils.py](../backend/app/routers/services/utils.py#L33) decomposes packed degrees, minutes, and seconds and applies:

    decimal degrees = degrees + minutes / 60 + seconds / 3600

Latitude is negative only for American Samoa, state code 60. Longitude is negative except for specified Pacific territories, consistent with the FHWA transition notes. The reverse path uses absolute decimal magnitude to rebuild packed DMS, so exact coordinate text and sub-second precision are not guaranteed to round-trip.

Border logic includes:

- Item 99 becomes <code>BL07</code>; a blank Item 99 unconditionally becomes <code>N</code>, even if Item 98A indicates a border, and leading zeros are not removed;
- Item 98A drops the FHWA-region character and changes <code>CAN</code> to <code>CA</code> and <code>MEX</code> to <code>MX</code>;
- Item 98B is reduced to a categorical inspection-responsibility value; and
- <code>BL10</code> uses the bridge’s state where a border condition exists.

FHWA notes that legacy Item 98B describes funding responsibility, not necessarily inspection responsibility. The transition necessarily assumes those responsibilities align and must be reviewed.

### 9.2 Year built and condition ratings

The most important direct fields are:

| Legacy item | SNBI target | Check |
|---|---|---|
| Item 27 Year Built | <code>BW01</code> | Retained only for an integer between 1 and 9999 |
| Item 58 Deck Condition | <code>BC01</code> | Direct |
| Item 59 Superstructure Condition | <code>BC02</code> | Direct |
| Item 60 Substructure Condition | <code>BC03</code> | Direct |
| Item 62 Culvert Condition | <code>BC04</code> | Direct |
| Item 61 Channel/Channel Protection | <code>BC09</code> | Retained when 0–9 or <code>N</code> |

For these direct transitions, a large numerical change is not a conversion effect. For example, Item 27 value 1977 cannot legitimately become <code>BW01=1939</code>. Likewise, Item 61 value 6 cannot legitimately become <code>BC09=7</code>. Such a discrepancy indicates mismatched source-year provenance, an incorrect source row, stale metadata, a same-year refresh issue, or a later user edit—not an FHWA direct-transition rule.

Items 36A and 36B require care. A literal <code>N</code> can populate <code>BC05</code>/<code>BC06</code>, while numeric traffic-safety-feature adequacy codes are not condition ratings. The same legacy items are also mapped through temporary roadside-hardware code rules to <code>BRH01</code> and <code>BRH02</code>.

Fields such as <code>BC07</code>, <code>BC08</code>, <code>BC10</code>, <code>BC12</code>, <code>BC13</code>, and <code>BC14</code> have either no complete legacy source or conditional applicability. The mapper does not fabricate most of them. In particular, <code>BC12</code>, <code>BC13</code>, and <code>BG16</code> are treated as calculated/do-not-report values in local validation.

### 9.3 Geometry and unit conversions

Legacy NBI geometry is predominantly metric; SNBI geometry in these mappings is feet. The mapper uses:

    feet = meters / 0.3048

Ordinary bridge geometry is rounded to the nearest tenth of a foot. Clearance helpers can round downward to the lower tenth for items whose transition rule requires conservative clearance reporting:

    clearance feet = floor((meters / 0.3048) × 10) / 10

Implemented examples include:

| Legacy source | SNBI target | Conditional behavior |
|---|---|---|
| Item 49 Structure Length | <code>BG02</code> | Total length in feet |
| Item 49 | <code>BG01</code> | Populated only when source length exceeds 9.1 m |
| Item 48 Maximum Span | <code>BG03</code> | Feet, nearest tenth |
| Item 48 | <code>BG04</code> | Only when Item 45 = 1 and Item 46 = 0 |
| Item 52 Deck Width | <code>BG05</code> | Null when source equals zero |
| Item 51 Curb-to-Curb Width | <code>BG06</code> | Feet |
| Items 50A/50B | <code>BG07</code>/<code>BG08</code> | Feet |
| Item 32 Approach Width | <code>BG09</code> | Feet |
| Item 34 Skew | <code>BG11</code> | Direct integer |

FHWA warns that bridge lengths near the NBIS threshold need field verification; merely converting a legacy rounded metric value is not a substitute for that verification.

Two current implementation details do not fully match the governing transition guidance:

1. FHWA's 2024 schema/validation/NBIS-length memorandum addresses legacy Item 49 values at the 6.1 m threshold. During the 2026–2027 transition, an Item 49 value of 6.1 m is to be represented as 20.1 ft until the agency obtains an adequate field measurement. The mapper currently populates <code>BG01</code> only when Item 49 is greater than 9.1 m. It therefore omits <code>BG01</code> for bridges in the 6.1–9.1 m range and does not implement the transition-period 6.1 m treatment.
2. The FHWA crosswalk specifies nearest-tenth conversion for <code>BH12</code> from legacy Item 10. The mapper passes <code>round_down=True</code> for <code>BH12</code> on both carried and under highways in [mapper.py](../backend/app/routers/services/mapper.py#L418). That is a code-level rounding mismatch, even though downward rounding remains appropriate for other clearance items whose individual rule requires it.

Clearances for highway and navigation features are assigned conditionally. Selected exceptionally large source clearances are capped at 99.9 feet by configured helper behavior. Navigation clearance is attached to the waterway feature only when legacy navigation control indicates applicability.

### 9.4 Span sets

Legacy Items 43–46 and 107–108 are expanded into <code>SpanSets[]</code> in [mapper.py](../backend/app/routers/services/mapper.py#L185).

The mapper creates:

- a main set when Item 45 indicates main spans and the main structural type is usable;
- an approach set when Item 46 indicates approach spans;
- culvert designations for recognized culvert configurations; and
- otherwise one placeholder main set.

The designation convention is:

| Configuration | Designation |
|---|---|
| Main span | <code>M01</code> |
| Approach span | <code>A01</code> |
| Main culvert | <code>C01</code> |
| Approach culvert | <code>C02</code> |

For each available set, the mapper derives:

- <code>BSP02</code> number of spans;
- <code>BSP04</code> material;
- <code>BSP05</code> continuity;
- <code>BSP06</code> type;
- <code>BSP09</code> deck material/type;
- <code>BSP10</code> wearing surface;
- <code>BSP11</code> deck protective system; and
- <code>BSP12</code> reinforcing protective system.

FHWA labels these transitions partial because legacy data cannot identify multiple main configurations, multiple approach configurations, extensions, widenings, and all permanent SNBI material/type details. BridgeGuard follows the one-main/one-approach assumption.

If no usable source set can be built, BridgeGuard emits <code>M01</code>, a span count, and unknown/temporary material and type values. This keeps the application structurally usable but is a **placeholder requiring Editor review**, not a fact established by the NBI record.

### 9.5 Substructure sets

The SNBI substructure dataset has no direct legacy Coding Guide equivalent at the necessary detail. Nevertheless, [mapper.py](../backend/app/routers/services/mapper.py#L236) creates a single placeholder set with:

- an abutment- or pier-style designation;
- an estimated unit count;
- unknown material;
- unknown configuration;
- unknown foundation.

The placeholder includes <code>BSB01</code>, <code>BSB02</code>, <code>BSB03</code>, <code>BSB04</code>, and <code>BSB06</code>. It does not populate the protective-system fields <code>BSB05</code> or <code>BSB07</code>.

This is an explicit application compromise. The report must not call <code>BSB01</code>–<code>BSB07</code> direct NBI transitions. The actual substructure grouping, unit count, material, type, foundation, and protection need bridge-specific review and coding.

### 9.6 Feature and route reconstruction

Feature generation begins in [mapper.py](../backend/app/routers/services/mapper.py#L359). Legacy Item 42 is split into “service on” (42A) and “service under” (42B). BridgeGuard uses the following structural logic.

For Item 42A:

| Item 42A | Carried features synthesized |
|---|---|
| 1, 6, 7, 8 | Highway <code>H01</code> |
| 4 | Highway <code>H01</code> plus railroad <code>R01</code> |
| 5 | Highway <code>H01</code> plus pedestrian/trail <code>P01</code> |
| 0, 2, 3, 9, blank | May still be mapped/stored by the app but later marked <code>fhwa_omit</code> when provenance is retained |

For Item 42B:

| Item 42B | Below-bridge feature types |
|---|---|
| 1 | Highway |
| 2 | Railroad |
| 3 | Pedestrian/trail |
| 4 | Highway plus railroad |
| 5 | Waterway |
| 6 | Highway plus waterway |
| 7 | Railroad plus waterway |
| 8 | Highway plus waterway plus railroad |
| 9 | Federal land |
| 0 | Unknown |

The carried highway is <code>H01</code>. The preferred under-record becomes <code>H02</code>; additional under-records become <code>H03</code>, <code>H04</code>, and so on. Every route within a highway feature is designated <code>R01</code>. The mapper can create waterway, railroad, pedestrian/trail, and federal-land features in addition to highway features.

The <code>H01</code> highway can receive:

- functional classification;
- urban code;
- NHS designation;
- National Highway Freight Network designation;
- STRAHNET designation;
- LRS route ID and mile point;
- lanes;
- annual average daily traffic;
- truck traffic;
- traffic year;
- vertical and horizontal clearances;
- bypass/detour length; and
- nested route data.

The route record can receive:

- <code>BRT01=R01</code>;
- route number plus directional suffix;
- direction;
- route type; and
- service type.

New Jersey LRS identifiers preserve leading zeros; other states are generally trimmed. Although the mapper contains this state-sensitive behavior, the public endpoint later drops non-New-Jersey output.

The FHWA crosswalk’s “one route per highway” and “one instance of a feature type” assumptions are visible here. The converter cannot infer two carried routes, multiple named waterways, or a complex feature hierarchy when the legacy record does not represent them.

### 9.7 Load ratings and posting

Implemented load and posting mappings include:

| Legacy source | SNBI target | Transformation |
|---|---|---|
| Item 31 Design Load | <code>BLR01</code> | Code map |
| Item 63 Operating Rating Method | <code>BLR04</code> | Code map |
| Item 66 Inventory Rating | <code>BLR05</code> | Divide by method-dependent constant |
| Item 64 Operating Rating | <code>BLR06</code> | Divide by method-dependent constant |
| Item 41 Open/Posted/Closed | <code>BPS01</code> | Code map and nested posting-status row |

The rating-factor calculation is:

    factor = legacy rating / 32.4

for methods <code>6</code>, <code>7</code>, <code>8</code>, <code>D</code>, <code>E</code>, or <code>F</code>; otherwise:

    factor = legacy rating / 32.67

The result is rounded to two decimals. Because the reverse converter later always multiplies by 32.67, method-family information can be lost and round-trip values can differ.

The mapper places <code>BPS01</code> both at bridge level and in <code>PostingStatuses[]</code>. It cannot fabricate the posting-status date <code>BPS02</code>. The normalizer nevertheless declares <code>BPS02</code> required for that nested dataset, so the mapper’s own output can create a local parity warning.

### 9.8 Waterway, navigation, scour, and approach behavior

The mapper combines legacy Items 38, 39, 71, 72, 111, 113, 116, structural type, and feature context.

- Item 38 controls whether navigation fields apply to waterway feature <code>W01</code>.
- Navigation vertical clearance uses Items 39 and 116 with special handling for lift bridges.
- Item 111 maps navigation protection.
- Item 72 maps approach roadway alignment: ratings 7–9 become good, 5–6 fair, and 0–4 poor.
- Item 71 combined with functional classification Item 26 derives overtopping/flood-frequency behavior.
- Item 113 jointly influences scour vulnerability/condition and plan-of-action values.
- <code>BAP05</code> is set to <code>0</code> as an application default because complete seismic vulnerability is not available from the legacy source.

These are conditional or derived transitions. They should not be compared as simple one-field direct copies.

### 9.9 Inspection events

Inspection event generation in [mapper.py](../backend/app/routers/services/mapper.py#L303) covers:

- routine inspections from Items 90 and 91;
- NSTM/fracture-critical inspections from Items 92A and 93A;
- underwater inspections from Items 92B and 93B; and
- special inspections from Items 92C and 93C.

Legacy month/year values are normalized to the first day of a month:

    MMYY → YYYYMM01

The pivot is <code>YY &lt; 80 → 20YY</code>; otherwise <code>19YY</code>. Therefore, the day is a proxy, not observed source information.

The application sets the inspection begin and completion dates to the same derived day and fills selected “not applicable/no” event fields. FHWA’s transition guidance is more nuanced: annual history is needed to distinguish initial from routine events and handle duplicates, and several event fields do not have direct Coding Guide values. Consequently, generated <code>BIE03</code>, <code>BIE04</code>, and parts of the event history are synthesized application values.

### 9.10 Work events

[mapper.py](../backend/app/routers/services/mapper.py#L348) creates:

- an original-construction work event at Item 27 Year Built, using <code>BR1</code>; and
- a reconstruction event when Item 106 contains a reconstruction year, using the transition placeholder <code>RECONSTRUCTED</code>.

The original-construction row is an application historical convenience. The reconstructed year has a legacy source, but <code>RECONSTRUCTED</code> is a placeholder rather than a final SNBI work code and must be refined. Item 97, the proposed improvement year, is not treated as completed work.

### 9.11 Element rows

Element rows are not manufactured by the main mapper. They are appended after conversion only when optional XML produced exact structure-number matches. The output stores element number, parent number, total quantity, and four condition-state quantities. The converter does not validate that:

- state quantities sum to total quantity;
- element codes are valid for the bridge;
- NHS bridges contain a complete required element set; or
- the XML corresponds to the same annual inventory version as the NBI file.

Those issues are partly addressed by the local validator for native SNBI records, but converted records take the lenient legacy path.

### 9.12 Fields intentionally left blank

Many SNBI fields have no valid Coding Guide transition, including bridge name, emergency evacuation designation, maximum bridge height, curved/sidehill designations, detailed substructure data, beam-line counts, several protective-system details, routine permit-load classification, and later inspection-event details. In general, omission is more defensible than inventing a permanent value. BridgeGuard follows that principle for many fields but departs from it for the application defaults identified above.

---

## 10. Normalization, coverage, and validation

### 10.1 Local shape normalization

<code>normalize_snbi_payload</code> in [snbi_schema.py](../backend/app/routers/services/snbi_schema.py#L174) operates over a local whitelist of recognized scalar tags and repeated datasets:

- <code>Elements</code>
- <code>Features</code>, with nested <code>Routes</code>
- <code>Inspections</code>
- <code>PostingEvaluations</code>
- <code>PostingStatuses</code>
- <code>SpanSets</code>
- <code>SubstructureSets</code>
- <code>Works</code>

It:

- wraps a dictionary as a one-item array where an array is expected;
- converts an invalid nested scalar into an empty array;
- discards unrecognized fields;
- removes blank values;
- checks a limited required-field subset; and
- reports human-readable warnings.

The converter calls these warnings <code>parity_errors</code>. A more accurate term is **local normalization warnings**. They do not compare the result against every constraint in FHWA’s official JSON schema, and they do not establish NBI→SNBI→NBI round-trip equality.

### 10.2 Coverage

<code>summarize_payload_coverage</code> and its section variant in [snbi_schema.py](../backend/app/routers/services/snbi_schema.py#L216) compare the union of populated codes against the application’s known code set.

Coverage is batch-level presence. If one bridge contains a field, the field is “present” for the entire batch. It does not answer:

- what percentage of bridges contain the field;
- which bridges are incomplete;
- whether a value is permanent, temporary, defaulted, or invalid; or
- whether a populated field is semantically correct.

A research-quality coverage statistic should report, for every tag, populated-record count, eligible-record count, percentage, source category, temporary/default count, and validation-failure count.

### 10.3 Local validation

[snbi_validation.py](../backend/app/routers/services/snbi_validation.py#L1) explicitly states that it is not the complete FHWA validation engine. For native SNBI data it evaluates a useful high-confidence subset:

- required bridge identity, coordinates, classification, geometry, and rating fields;
- required repeated datasets;
- conditional waterway, navigation, steel, NSTM, underwater-inspection, NHS-element, route, and posting relationships;
- numeric, integer, date, and supported code-list formats;
- do-not-report fields;
- selected posting and rating safety relationships; and
- expiration of temporary transition codes for 2028 and later, but only for native/nonlegacy records that receive a submission year and reach the generic code checks.

Issues are classified as:

- critical;
- error;
- flag; or
- safety.

The validator caps returned issue objects at 250 while retaining aggregate counts. The converter and loader display only a subset in the UI.

### 10.4 Legacy validation path

Every row created by <code>/api/convert</code> is explicitly classified as legacy. Legacy rows retain universal identity and geometry checks but skip the structural datasets, conditional rules, code-list checks, date checks, and posting-safety checks that assume complete SNBI data. This prevents an overwhelming list of errors for fields legacy NBI cannot populate, but it also means a favorable converter validation result does **not** mean the output would pass the official FHWA validation module.

### 10.5 Advisory rather than blocking

Neither conversion nor loading blocks on local critical, error, flag, or safety findings. Validation is evidence for review. A file can be loaded successfully with serious warnings. Operational procedures must therefore define a separate acceptance gate before any FHWA-facing export.

---

## 11. Reverse conversion: SNBI to legacy-shaped outputs

### 11.1 Request contract

<code>POST /api/reverse-convert</code> is implemented in [main.py](../backend/app/main.py#L695). It accepts UTF-8 JSON, including UTF-8 BOM, in any of these shapes:

- one bridge object;
- an array of bridge objects; or
- an object containing <code>bridges: [...]</code>.

The endpoint normalizes the data and calls <code>convert_snbi_to_outputs</code> in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L824). It returns:

- <code>flat_json</code>: readable keys rather than raw SNBI tags;
- <code>text</code>: a human-readable report;
- <code>nbi_csv</code>: a headered, comma-delimited legacy-shaped row set;
- <code>elements_xml</code>: a simple element XML document;
- <code>count</code>; and
- up to 50 normalization warnings.

### 11.2 Why exact inversion is impossible

The transformation is not mathematically one-to-one. SNBI can contain multiple features, routes, span sets, inspection events, work events, posting records, and element records. A legacy NBI record has one fixed set of flat items. Several legacy codes can also map to the same SNBI code. Once that many-to-one mapping occurs, the original code cannot be inferred uniquely.

<code>_invert</code> in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L37) uses “first NBI code wins” when several source codes share a target, with a small number of permanent-code overrides. This provides deterministic output, not historical reconstruction.

### 11.3 Reverse algorithm

<code>snbi_to_nbi_row</code> in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L402):

1. creates an empty row over the legacy <code>field_names</code> columns;
2. assigns record type 1;
3. copies direct inverse fields;
4. applies inverse code tables;
5. converts decimal coordinates back to packed DMS;
6. converts feet back to meters;
7. reconstructs selected load-rating, work, inspection, posting, feature, route, waterway, scour, and span values;
8. chooses representative values where SNBI contains more detail than NBI; and
9. serializes the result as CSV.

Every bridge produces exactly one record-type-<code>1</code> row. The reverse mapper does not reconstruct separate legacy type-<code>2</code> or type-<code>A</code> under-record rows, even when the SNBI bridge contains several under-highway features and routes.

### 11.4 Known information loss

| Reverse operation | Information lost or approximated |
|---|---|
| Border responsibility | Shared responsibility becomes a representative 50 percent because exact legacy percentage was discarded. |
| Coordinates | Decimal degrees become absolute packed DMS; sign conventions and rounding are reapplied. |
| Geometry | Feet become meters with rounding, so the original legacy text may differ. |
| Load ratings | Values are multiplied by 32.67 even if forward conversion used 32.4 for the method family. |
| Works | Only selected construction/reconstruction events fit legacy fields. |
| Inspections | One event/date/frequency per legacy category is selected from potentially many events. |
| Features/routes | Exactly one type-1 row is emitted. A representative carried feature can populate its flat route fields, but no separate type-2/A under-record hierarchy is reconstructed. |
| Spans | The first main and first approach span set are selected; additional configurations are discarded. |
| Derived scour/approach fields | One representative legacy value is chosen from richer SNBI combinations. |
| Code inversions | When multiple NBI codes map to one SNBI code, the original code is unknowable. |

### 11.5 Known reverse-mapping defects

The posting reverse block in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L489) warrants correction. Nested SNBI posting codes can be written directly into one-character legacy fields without a complete inverse mapping. The code also appears to associate <code>BPS02</code> with legacy Item 90 inspection date, which is not a defensible field relationship. This should be treated as a probable mapping defect until corrected and tested.

The audit found four additional concrete defects:

- <code>BL12</code>, Metropolitan Planning Organization, is written to legacy Item 12 Base Highway Network in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L305), although the FHWA crosswalk gives <code>BL12</code> no legacy transition.
- <code>BIE11</code>, Inspection Note, is written to legacy Item 75A Work Proposed in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L517); those concepts are not equivalent.
- <code>BH06</code> is split after 12 characters in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L578), but legacy Items 13A and 13B occupy 10 and 2 characters, respectively.
- <code>BL08</code> is copied directly into legacy Item 98A in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L307). The reverse path does not restore the FHWA region digit and does not restore <code>CA</code>/<code>MX</code> to the legacy <code>CAN</code>/<code>MEX</code> representation.

These are semantic defects rather than unavoidable information loss. They should be corrected before the reverse export is used for comparison, migration, or archival work.

### 11.6 Output-format qualification

<code>bridges_to_nbi_csv</code> uses Python <code>csv.DictWriter</code> in [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L792). Therefore, the output is a **headered CSV**, not the official 445-character fixed-width record described by FHWA. The frontend downloads the CSV body with a <code>.txt</code> name in [Converter.tsx](../frontend/src/pages/Converter.tsx#L184), which can make the format appear more official than it is.

The element output is a simple <code>BridgeElements/FHWAED</code> XML tree. It is not validated against an FHWA XML schema.

The safest name for these outputs is **best-effort legacy-shaped export for review**, not “legacy-compatible” and not “official NBI submission file.”

### 11.7 Round-trip expectation

The correct round-trip test is not byte equality. It should classify each target as:

- exactly reversible;
- reversible within documented numeric tolerance;
- deterministically representative;
- not reversible because multiplicity was collapsed;
- not reversible because code identity was lost; or
- application-generated and therefore absent from the original source.

---

## 12. Loader: request boundary and validation

### 12.1 Browser workflow

The Loader page is implemented in [Loader.tsx](../frontend/src/pages/Loader.tsx#L52). A user:

1. selects a JSON file;
2. enters or selects an integer inventory year from 1990 through 2100; and
3. submits multipart fields <code>file</code> and <code>data_year</code> to <code>POST /api/load-json</code>.

The browser file picker prefers <code>.json</code>, but the server does not depend on the extension or MIME type; it parses the content.

### 12.2 Backend checks

The endpoint begins at [main.py](../backend/app/main.py#L750). It:

1. validates the year range;
2. reads the upload under the shared size limit;
3. parses JSON;
4. requires a nonempty top-level array;
5. verifies that the first bridge contains <code>BL01</code> and <code>BID01</code>;
6. rejects any dictionary bridge whose <code>BL01</code> is not 34;
7. removes private underscore keys from a copy used for validation;
8. identifies NBI-derived rows through <code>_SourceData.nbi</code>;
9. computes advisory local validation; and
10. calls <code>load_json_to_db</code>.

The checks do not verify every array member has the required identifiers before passing it to the loader; non-dictionary members are excluded from validation but still present in the original list. The loader’s loop then assumes a dictionary and can fail the whole batch. An invalid encoding can also reach the generic exception handler instead of receiving the reverse converter’s specific UTF-8 error.

The endpoint currently logs the complete first bridge, including potentially large or sensitive <code>_SourceData</code>, to standard output. Production logging should record identifiers and counts, not the full record payload.

### 12.3 Authorization

<code>POST /api/load-json</code> explicitly requires the **editor** role in [auth.py](../backend/app/auth.py#L249). Admins inherit editor capability; viewers do not. The frontend tool remains visible to viewers, so a viewer can open the Loader and prepare a request but receives 403 when attempting a mutation. Server-side enforcement is correct, but the UI should hide or disable controls according to the authenticated role.

### 12.4 Native SNBI shape responsibility

The loader does **not** call <code>normalize_snbi_payload</code>. Converter output is normalized before download, but independently authored SNBI JSON must already use the expected array shapes. A dictionary where <code>Features[]</code> is expected, for example, can cause iteration over string keys and a batch failure.

Unknown properties are not automatically reported by the loader. Only fields represented in <code>constants.mapping</code> and present as columns in the destination table are stored; other values are ignored.

### 12.5 Advisory validation

The loader returns the same local validation classifications described in Section 10. It does not block persistence on them. The current Loader UI shows the summary and only the first 15 issues. A successful “loaded” message therefore means database persistence succeeded, not that the batch is FHWA-valid.

---

## 13. Snapshot year and relational storage

### 13.1 Authoritative inventory year

The year selected in the Loader is authoritative. This is deliberate: inspection dates or posting dates inside a snapshot may precede the inventory year and should not determine where the whole inventory record is stored.

The fallback chain in [database.py](../backend/app/routers/services/database.py#L19) and [database.py](../backend/app/routers/services/database.py#L60) is:

    explicit loader data_year
        else year parsed from BLR03
        else year parsed from BPS02
        else year parsed from Inspections.BIE02 or BIE01
        else current calendar year

In normal browser use, the explicit year is always sent. The server does not compare that year with source-file metadata or dates inside the bridges. A user can therefore label a 2024 file as 2025. This is a controlled metadata responsibility, not an automatically verified fact.

<code>data_year</code> is an internal BridgeGuard snapshot key. It is used to select an export year and is not emitted as an SNBI field in FHWA-facing JSON.

### 13.2 Snapshot identity

The main table’s unique identity is:

    (state_code, bridge_number, data_year)

Thus bridge <code>0313151</code> in 2024 and the same bridge in 2025 are two snapshots. A second upload of bridge <code>0313151</code> for 2025 targets the existing snapshot.

When the bridge-detail read omits a year, BridgeGuard resolves the maximum available <code>data_year</code> for that bridge and retrieves the main snapshot plus same-year features and elements. Bulk export and explicit child-table reads likewise apply an explicit or resolved year to the repeated datasets they retrieve. Exact-year and latest-year behavior is covered in [test_bridge_snapshot_scoping.py](../backend/tests/test_bridge_snapshot_scoping.py#L71).

### 13.3 Database configuration

The default persistent database is:

    backend/data/snbi_bridges.db

Its root can be relocated through <code>BRIDGEGUARD_DATA_DIR</code>, as defined in [datapaths.py](../backend/app/routers/services/datapaths.py#L33).

Schema initialization in [database.py](../backend/app/routers/services/database.py#L338) enables WAL journal mode and <code>synchronous=NORMAL</code>. Operational connections enable foreign-key enforcement in [database.py](../backend/app/routers/services/database.py#L746). Loading also uses memory-backed temporary storage. The loader initializes the schema every time so deletion of the database file while the process is active does not leave a newly recreated empty file without tables.

### 13.4 Normalized fan-out

One SNBI JSON object is decomposed as follows:

| JSON location | SQLite table | Snapshot key carried |
|---|---|---|
| Top-level scalar tags | <code>bridges</code> | state, bridge, year |
| <code>Features[]</code> | <code>features</code> | state, bridge, year |
| <code>Features[].Routes[]</code> | <code>routes</code> | state, bridge, year plus parent feature type |
| <code>SpanSets[]</code> | <code>span_sets</code> | state, bridge, year |
| <code>SubstructureSets[]</code> | <code>substructure_sets</code> | state, bridge, year |
| <code>Elements[]</code> | <code>elements</code> | state, bridge, year |
| <code>Inspections[]</code> | <code>inspections</code> | state, bridge, year |
| <code>PostingEvaluations[]</code> | <code>posting_evaluations</code> | state, bridge, year |
| <code>PostingStatuses[]</code> | <code>posting_statuses</code> | state, bridge, year |
| <code>Works[]</code> | <code>works</code> | state, bridge, year |
| <code>_SourceData.nbi</code> | <code>bridge_source_records</code> | state, bridge, year, source name |

The table definitions begin at [database.py](../backend/app/routers/services/database.py#L349). Child fan-out is implemented from [database.py](../backend/app/routers/services/database.py#L1136) onward. Routes inherit the parent feature’s <code>BF01</code> as <code>feature_type</code>. Elements with no parent number receive <code>0</code>.

SNBI tags are translated to readable SQL columns through [constants.py](../backend/app/routers/services/constants.py#L32). Column filtering prevents arbitrary JSON keys from becoming SQL identifiers.

### 13.5 Schema migrations

Older databases are migrated to yearly snapshot identity in [database.py](../backend/app/routers/services/database.py#L81). Existing bridge rows infer a migration year from the first four digits of inspection completion date, falling back to the calendar year at migration. Several legacy child tables are backfilled to a year. The features migration adds <code>data_year</code> but does not visibly backfill every old feature row, so legacy feature rows with null year may disappear from year-specific reads. Migration behavior should be tested on a copy of each historical production schema before deployment.

---

## 14. Loader transaction and refresh semantics

### 14.1 Batch transaction

<code>load_json_to_db</code> begins at [database.py](../backend/app/routers/services/database.py#L993). The entire uploaded list uses one connection and normally one transaction. In a direct low-level call, records missing <code>BL01</code> or <code>BID01</code> are skipped while processing continues. Through the HTTP endpoint, a dictionary missing <code>BL01</code> is first classified as non-New-Jersey and rejects the request; a later record missing only <code>BID01</code> can reach the low-level skip path. A fatal uncaught exception rolls the batch back and returns a synthetic fatal skip result. The endpoint then normally converts the zero-insert/zero-existing tuple into HTTP 400 “No bridges were loaded,” so an internal load failure can be misclassified as bad input.

This offers batch atomicity for exceptions that propagate. However, <code>safe_insert</code> catches and prints SQLite errors rather than re-raising them. A main or child insert can therefore fail silently while the loop continues; after a swallowed main insert error, cursor row-count interpretation can also be unreliable. Parent/child completeness is not guaranteed by the response counters.

### 14.2 New insert

The main bridge uses <code>INSERT OR IGNORE</code>. If one row is inserted, <code>inserted_new</code> increments.

### 14.3 Same-year refresh

If the unique snapshot already exists:

1. incoming mapped top-level fields update the main bridge row;
2. all year-scoped child rows are deleted;
3. current child arrays are inserted;
4. field metadata and source provenance are deleted;
5. new source provenance is inserted if present; and
6. <code>already_existed</code> increments.

The behavior is asymmetric:

- **Main scalar row:** patch semantics. Only incoming mapped fields are updated, so omitted scalar fields remain from the old snapshot.
- **Child arrays:** replacement semantics. Existing children are deleted first, so an omitted child array clears the stored collection.

The code comment calls this a complete snapshot refresh, but the scalar behavior is not complete replacement.

### 14.4 Duplicate identities in one upload

There is no pre-deduplication. If the same state/bridge/year appears twice:

- the first occurrence may count as new;
- the second counts as existing;
- the final child and provenance state comes from the last occurrence; and
- reported processed counts can exceed unique snapshot count.

A deterministic loader should reject duplicate keys or explicitly document and report “last record wins.”

### 14.5 Return counters

The low-level loader returns:

- <code>inserted_new</code>;
- <code>already_existed</code>;
- <code>skipped</code>; and
- up to 50 skip reasons.

Those counters primarily reflect main-row processing. They do not prove every nested row was inserted.

### 14.6 All-existing “conflict” defect

When every submitted snapshot already exists, the following sequence occurs:

1. the database updates main values;
2. deletes and replaces children and provenance;
3. commits;
4. the endpoint returns HTTP 200 with <code>status: conflict</code>; and
5. the message says the user must delete the year and reload.

The mutation has already occurred. The UI presents the result as an error and does not update its “database last updated” marker. Therefore, the current “conflict” response means **refresh committed but reported as rejected**. The endpoint should instead return success with refreshed count, or the low-level loader should genuinely skip existing rows before mutation.

### 14.7 Partial failure risk

Although a fatal exception rolls back the batch, exceptions swallowed by <code>safe_insert</code> can create an incomplete snapshot. A research-quality load should collect table/row errors, roll back the affected bridge or entire batch, and return a verifiable inserted-row manifest.

---

## 15. Provenance and FHWA transition eligibility

### 15.1 What is preserved

The converter reattaches the parsed over-record as:

    "_SourceData": {
      "nbi": {
        "... parsed legacy fields ...": "..."
      }
    }

The loader stores that dictionary in <code>bridge_source_records</code> with <code>source_name = NBI TXT</code>; see [database.py](../backend/app/routers/services/database.py#L1118). The unique key is state, bridge, year, and source name.

The Editor later combines:

- the current exported SNBI value;
- the raw stored over-record;
- static source-field mappings; and
- transition guidance in [editor_metadata.py](../backend/app/routers/services/editor_metadata.py#L402)

to explain source value, transition note, temporary/default status, and review requirements.

### 15.2 What is not preserved

Current provenance does not retain:

- every under-record;
- which under-record was primary;
- the raw 445-character source line;
- the raw element XML node/file;
- the uploaded filename;
- the FHWA annual download URL;
- download and correction timestamps;
- file SHA-256 checksum;
- source archive checksum;
- converter Git revision;
- crosswalk and detailed-code-mapping version;
- rule identifier per field;
- transformation inputs and intermediate numeric precision;
- authenticated uploader identity;
- load timestamp and batch/job ID;
- an immutable copy or hash of the uploaded SNBI JSON; or
- prior provenance versions.

Same-year refresh deletes the current source-record row before optionally recreating it, so provenance is replace-in-place rather than append-only history. Consequently, an over-record-derived direct field can be audited reasonably well, but a feature or clearance derived from an under-record cannot always be traced to the actual input row used. Element fields can be labeled as element-XML-derived while the raw XML is no longer available.

### 15.3 Application omission flag

When <code>_SourceData.nbi</code> exists, the loader reads Item 42A. Blank, <code>0</code>, <code>2</code>, <code>3</code>, or <code>9</code> set internal <code>fhwa_omit=1</code> in [database.py](../backend/app/routers/services/database.py#L1059). The row remains in the database, and the Editor displays a review warning.

This differs from FHWA’s official transition population in two ways:

1. BridgeGuard retains instead of omitting those Item 42A records.
2. BridgeGuard does not enforce Item 112 = <code>Y</code>.

Moreover, <code>fhwa_omit</code> is internal metadata and the bulk exporter does not automatically filter flagged records. A flagged bridge can still enter an export. Therefore, <code>fhwa_omit</code> is a review signal, not a submission safeguard.

### 15.4 Minimum research-grade lineage record

For each converted bridge and target field, a stronger lineage record would store:

| Lineage attribute | Example |
|---|---|
| Source dataset | FHWA NBI 2025 New Jersey, all records |
| Source URL | Exact annual-state download |
| File identity | filename, size, SHA-256, download time |
| Source bridge key | state, structure number, record type |
| Source values | Items 49 and 52 with raw text |
| Rule | <code>BG02_METERS_TO_FEET_V2025_12_30</code> |
| Rule artifact | crosswalk version/date/checksum |
| Transformation | divide by 0.3048; round to 0.1 ft |
| Output | <code>BG02=123.4</code> |
| Status | derived, permanent, temporary, default, review |
| Software | Git revision and dependency versions |

This would allow another researcher to reproduce the exact value without trusting the application’s present code.

---

## 16. Historical side effects and deletion behavior

### 16.1 Synthetic condition-history row

When a loaded bridge has component condition ratings, the loader can create a synthetic <code>inspection_report_history</code> row:

    report_folder = JSON_IMPORT
    report_file   = JSON_LOAD
    confidence    = 1.0
    report_year   = data_year

The controlling condition is <code>BC13</code> when provided; otherwise it is the minimum available value among deck, superstructure, substructure, and culvert ratings. This supports year-over-year charts even when no inspection PDF was parsed.

On a same-year refresh, an existing synthetic row is not updated. The bridge snapshot can therefore contain new ratings while history retains the original ratings.

### 16.2 Year deletion

The synthetic history table uses <code>report_year</code>, not <code>data_year</code>. The generic year-deletion function deletes from tables that expose <code>data_year</code>, so the old synthetic history row can survive deletion and reloading of an inventory year.

### 16.3 Foreign-key ordering risk

Operational SQLite connections enable foreign keys. Child tables reference <code>bridges</code> without <code>ON DELETE CASCADE</code>. The year and single-snapshot deletion functions enumerate tables rather than defining a child-first order. If <code>bridges</code> is encountered before a child table, SQLite can raise <code>FOREIGN KEY constraint failed</code>.

The safe design is either:

- explicit child-first deletion followed by the parent;
- declarative <code>ON DELETE CASCADE</code> with migration tests; or
- a deferred-constraint transaction that is tested against the real schema.

The deletion functions should also use <code>try/finally</code> to guarantee connection closure after errors.

---

## 17. Bridgette: technical characterization

Bridgette is a **two-stage, LLM-mediated text-to-SQL pipeline**. It is not presently an autonomous agent with a loop of tool selection, reflection, and repeated actions. On its normal path:

1. Cerebras converts one natural-language question into one SQLite <code>SELECT</code>.
2. BridgeGuard executes that SQL locally against the shared inventory database.
3. Cerebras converts the SQL result into a concise natural-language answer.

No Nomic embedding, vector index, inspection report, point cloud, or visual-language result participates in a statewide Bridgette request.

The primary service is [vision.py](../backend/app/routers/services/vision.py#L267). Streaming is exposed at <code>POST /api/bridgette/stream</code> in [main.py](../backend/app/main.py#L1587); a nonstreaming compatibility endpoint exists at <code>POST /api/bridgette</code>.

---

## 18. Bridgette request-to-answer workflow

### 18.1 Sequence

~~~mermaid
sequenceDiagram
    actor U as "Authenticated user"
    participant UI as "React Bridgette"
    participant API as "FastAPI"
    participant S as "Schema builder"
    participant C1 as "Cerebras SQL call"
    participant DB as "SQLite mode=ro"
    participant C2 as "Cerebras answer call"

    U->>UI: Ask one natural-language question
    UI->>API: POST /api/bridgette/stream with question
    API->>S: Build or reuse focused schema text
    S-->>API: Tables, columns, temporal guidance
    API->>C1: Question + date + schema + SQL rules
    C1-->>API: Candidate SELECT
    API->>API: Extract SQL and add default LIMIT
    API->>DB: Execute read-only query
    DB-->>API: Columns and rows
    API-->>UI: NDJSON meta event with SQL and rows
    API->>C2: Question + SQL + row preview + grounding rules
    C2-->>API: Stream answer tokens
    API-->>UI: NDJSON token events, then done
    UI-->>U: Answer; full page can reveal SQL and rows
~~~

### 18.2 Frontend request

The full-page UI stores the typed question as a local turn, creates an <code>AbortController</code>, and submits only:

    FormData(question = current question)

to <code>/api/bridgette/stream</code>; see [Bridgette.tsx](../frontend/src/pages/Bridgette.tsx#L26). Prior visible turns are **not** sent. The sidebar implements its own independent local turns.

### 18.3 API and authentication

FastAPI rejects a blank question. The route is synchronous so the framework can execute it in a worker thread while a cancellation request remains serviceable.

When authentication is enabled, middleware protects Bridgette and most <code>/api/*</code> requests. Deliberate exceptions include CORS <code>OPTIONS</code>, <code>/api/auth/config</code>, and the password-mode login endpoint; disabled/local mode assigns a local admin identity directly. Bridgette is neither an admin route nor an editor mutation, so it requires only viewer access. Viewers, editors, and admins can all use it. The endpoint does not read the authenticated user identity after authorization, so there is no per-user data scope, history, quota, or audit association.

### 18.4 Schema preparation

<code>_bridgette_schema</code> in [vision.py](../backend/app/routers/services/vision.py#L308) introspects and describes these preferred tables:

- <code>bridges</code>
- <code>features</code>
- <code>routes</code>
- <code>span_sets</code>
- <code>elements</code>
- <code>works</code>
- <code>posting_statuses</code>
- <code>counties</code>

It exposes table and column names, not a full typed relational model. It does not include declared foreign keys, sample values, cardinality statistics, formal join paths, or field definitions. The LLM infers joins largely from column names and prompt instructions.

Before schema inspection, <code>_ensure_counties_table</code> creates or refreshes an internal New Jersey county lookup containing state code 34, all 21 county codes/names, and an Unknown row. This is a write performed during schema bootstrap; query execution later opens the database read-only.

The schema text is cached in one process-global slot. The cache is not keyed by database path and is not invalidated when the database schema changes. A long-running process can therefore prompt the model with stale columns after a migration.

### 18.5 Temporal and counting guidance

The schema text and SQL prompt instruct the model that:

- each bridge can appear once per annual <code>data_year</code>;
- simple current counts, lists, and rankings should filter to the maximum year;
- trends should group and order by year;
- a cross-year count should use one year or <code>COUNT(DISTINCT bridge_number)</code>;
- rankings should first reduce to one row per bridge;
- age questions should use the actual current calendar date/year, not the most recent inventory year; and
- county output should join the county table and display names.

The current server date/time and timezone are inserted into both prompts. This makes a question such as “How old are the bridges?” relative to the request date, while “What was the condition in 2024?” remains a snapshot query.

---

## 19. SQL generation and execution

### 19.1 SQL prompt

The first prompt in [vision.py](../backend/app/routers/services/vision.py#L466) gives Cerebras:

- a New Jersey database role statement;
- current local date, time, timezone, and year;
- an instruction to return one read-only SQLite <code>SELECT</code>;
- the latest-year/trend/ranking/counting rules;
- county-name guidance;
- the focused schema text; and
- the current user question.

The model is told to return only SQL.

### 19.2 Model selection

Higher-level code exposes <code>BRIDGE_SQL_MODEL</code> and <code>BRIDGE_ANSWER_MODEL</code> slots and accepts a model form field. The provider implementation, however, deliberately selects the server-configured <code>CEREBRAS_MODEL</code> and ignores the per-call hint. The repository default is <code>gemma-4-31b</code>; see [llm_providers.py](../backend/app/routers/services/llm_providers.py#L23) and [llm_providers.py](../backend/app/routers/services/llm_providers.py#L373).

Accordingly, the current deployment does not truly use separate SQL and answer models unless the provider behavior is changed. The apparent “retry with the capable model” can call the same server-selected model.

Generation uses temperature zero, top-p one, and normally a 1,024-token output budget. The provider retries transient Cerebras rate-limit errors, and the wrapper can retry a failed generation once more.

### 19.3 SQL extraction

<code>_extract_sql</code> in [vision.py](../backend/app/routers/services/vision.py#L351):

- extracts SQL from a code fence if present;
- searches for the first <code>SELECT</code>;
- discards preceding prose;
- removes a final semicolon; and
- trims whitespace.

The query is accepted only when the resulting text begins with <code>SELECT</code>. A valid common-table expression beginning with <code>WITH</code> is therefore rejected unless extraction finds a later <code>SELECT</code> and discards the CTE, which would normally make the query invalid.

### 19.4 Default limit

<code>_ensure_limit</code> in [vision.py](../backend/app/routers/services/vision.py#L362) appends <code>LIMIT 500</code> only when no <code>LIMIT</code> token appears anywhere in the SQL.

This is a default, not a hard cap:

- <code>LIMIT 100000</code> is left intact;
- a nested-query limit or a comment containing the word can prevent the outer limit;
- aggregate or join work can be expensive even when only 500 final rows are returned; and
- no execution-time or virtual-machine-step limit is applied.

### 19.5 Read-only execution

<code>execute_readonly_sql</code> in [database.py](../backend/app/routers/services/database.py#L2969) first permits statements whose uppercase trimmed prefix is <code>SELECT</code>, <code>PRAGMA</code>, or <code>EXPLAIN</code>. Bridgette’s earlier check limits its own path to <code>SELECT</code>.

The executor opens:

    file:<database path>?mode=ro

with SQLite URI mode. The database engine therefore refuses writes; see the [SQLite URI filename documentation](https://www.sqlite.org/uri.html). Pandas materializes the entire result, converts null-like values, and returns columns, row dictionaries, and count.

This gives Bridgette a meaningful defense-in-depth layer: even if prompt instructions fail, the executed database connection is read-only.

### 19.6 Remaining SQL control gaps

The current implementation does not:

- parse SQL into an abstract syntax tree;
- verify exactly one statement structurally;
- enforce a table or column allowlist;
- enforce <code>state_code=34</code> programmatically;
- enforce latest-year semantics programmatically;
- verify the generated query answers the question;
- cap an existing large limit;
- limit joins, subqueries, recursion, or aggregate complexity;
- install a SQLite progress handler or deadline; or
- log execution latency and user ownership in a dedicated audit table.

The schema prompt is a strong instruction, not a security boundary. The read-only connection protects integrity but not availability, privacy within the shared database, or semantic correctness.

### 19.7 Failure and retry

If SQL execution raises an exception, <code>_try_sql</code> suppresses its details and can retry using the alternate model slot. If no query succeeds, the client receives an empty meta event and a message that Bridgette could not form a runnable query. The underlying SQL error is not exposed to the user or recorded as structured evaluation data.

---

## 20. Result grounding and streaming

### 20.1 Evidence returned before narration

On successful execution, Bridgette emits one newline-delimited JSON <code>meta</code> event containing:

- generated SQL;
- returned rows;
- columns; and
- row count.

The full-page UI can display SQL and rows, making the evidence chain auditable. It renders at most the first 100 rows, although the browser receives the backend result. The sidebar ignores the meta event and shows only the generated prose.

### 20.2 Answer preview

The second model does not always receive every row:

- if the result has at most 40 rows, all are included;
- otherwise, it receives the first 20 and last 20, with an omitted-row marker.

This first-and-last strategy preserves endpoints of ordered time series, but the model cannot inspect middle rows. Queries should therefore aggregate, filter, or rank in SQL instead of expecting the narrator to reason across a large raw result.

### 20.3 Grounding prompt

The second prompt includes:

- the original question;
- exact SQL;
- columns;
- the JSON row preview;
- current date/time; and
- instructions to use only the results, never invent numbers, treat the SQL <code>WHERE</code> clause as evidence, prefer the newest year for simple counts, inspect ordered tail rows, and use county names.

The answer is grounded **after deterministic database execution**. This is materially safer than asking an LLM to recall bridge statistics from training data.

Grounding remains instruction-based. The server does not mechanically verify that every number in the prose appears in the result or that the SQL predicate matches the question.

### 20.4 Stream protocol

The generator yields:

| Event | Payload |
|---|---|
| <code>meta</code> | SQL, rows, columns, count |
| <code>token</code> | one answer text fragment |
| <code>done</code> | successful terminal event |
| <code>stopped</code> | cancellation terminal event |
| <code>error</code> | stream failure where applicable |

The React client incrementally decodes newline-delimited JSON and updates the assistant turn while tokens arrive.

---

## 21. Worked Bridgette example

Consider:

> How many poor bridges are in each county in 2025?

### 21.1 Conceptual SQL plan

The model should identify:

- <code>bridges</code> as the inventory table;
- <code>counties</code> as the code-to-name lookup;
- <code>data_year=2025</code> as an explicit snapshot;
- the application’s poor-condition criterion or controlling condition column;
- one row per bridge; and
- grouping by county name.

A representative query shape—not guaranteed to be the exact generated SQL—is:

    SELECT
        c.county_name,
        COUNT(DISTINCT b.bridge_number) AS poor_bridge_count
    FROM bridges AS b
    JOIN counties AS c
      ON c.state_code = b.state_code
     AND c.county_code = b.county_code
    WHERE b.state_code = 34
      AND b.data_year = 2025
      AND <implemented poor-condition predicate>
    GROUP BY c.county_name
    ORDER BY poor_bridge_count DESC
    LIMIT 500

The placeholder is intentional: the actual “poor” predicate must match the database’s defined condition KPI, not be guessed from this report.

### 21.2 Evidence flow

1. Cerebras emits the query.
2. SQLite runs it read-only.
3. The UI receives the actual county/count rows plus the SQL.
4. Cerebras receives those rows and explains them.
5. A reviewer can compare every claimed count with the table.

### 21.3 Failure modes

- If the model forgets <code>data_year=2025</code>, multiple snapshots may be counted.
- If it uses <code>COUNT(*)</code> after a one-to-many join, bridges may be duplicated.
- If it joins county without state code, codes could be ambiguous in a multi-state future.
- If the “poor” predicate does not match BridgeGuard KPI logic, the SQL can run while answering the wrong question.
- If more than 40 result rows exist, the narrator sees only the first and last 20.

This illustrates why text-to-SQL quality must be evaluated separately from fluent answer generation.

---

## 22. Conversation state, sessions, and cancellation

### 22.1 Apparent history versus actual model context

Both Bridgette interfaces display a sequence of turns using React state. Each request sends only the new question. There is no:

- conversation ID;
- prior-turn prompt history;
- durable chat record;
- user-linked conversation table; or
- pronoun/coreference resolution using earlier turns.

“What about Bergen?” is therefore not reliably understood after “Show poor bridges by county” unless the second question is independently complete.

The full-page component remains mounted after first opening during the same single-page-app lifetime, so turns can survive closing and reopening the modal. Sidebar turns are separate. All such history disappears on refresh, logout, or application remount.

### 22.2 Shared cancellation state

The server uses process-global threading events for “chat active” and “cancel.” <code>/api/ask-cancel</code> is shared by streaming Bridgette and normal bridge-scoped Ask.

Under concurrent use:

- one user can cancel another user’s request;
- starting a request clears the global cancellation event;
- one request finishing can clear state while another remains active; and
- any authenticated viewer can call the shared cancel route.

This is a single-user-era concurrency design. In the current one-worker deployment, the event can interfere across all concurrent users. With multiple workers, interference is limited to requests in the same worker, while a cancel request routed to a different worker may fail to reach its intended generation. The separate visual-investigation endpoint does not arm or inspect this server cancellation event: its Stop button aborts the browser request and also posts the shared cancel signal, but server-side image processing can continue. Correct multi-user cancellation needs a request ID and an authenticated owner, with a separate token per request. A cancel endpoint should verify that the caller owns the job.

### 22.3 Shared database scope

All users query one SQLite inventory. Loader changes by an editor become visible to every viewer. There is no tenant identifier, per-user row filter, separate database, or user-owned history. Cloudflare Access identifies and authorizes the user at the perimeter, but Bridgette itself does not apply the identity to the query.

The current deployment should therefore be described as:

> one organization-wide shared New Jersey bridge workspace with role-based operations

not:

> a multi-tenant data platform with isolated user datasets.

---

## 23. Bridgette versus other AI and map workflows

### 23.1 Bridge-scoped Ask

Bridge Ask is attached to a selected <code>(state_code, bridge_number)</code>. The user can explicitly choose database evidence and/or individual inspection reports. Its normal text-answer evidence can include:

- a generated SQL query intended to be scoped to that bridge;
- selected inspection-report text and tables;
- up to six retrieved text/table chunks; and
- Nomic/TEI similarity used to rank those eligible report chunks.

Report preprocessing does create image descriptions and embeddings, but normal text Ask explicitly filters image-description chunks out of its answer evidence. Those image descriptions support the separate visual-investigation path and image-oriented commands. Parsing, image description, chunking, embedding, and cache generation occur in the bridge Ask subsystem in [vision.py](../backend/app/routers/services/vision.py#L1135), not in statewide Bridgette.

Normal bridge Ask runs selected report retrieval and bridge-specific SQL context construction concurrently and combines their evidence in one grounded prompt. Database-only selection skips report ingestion; selecting no usable source ultimately produces a no-context error. The bridge SQL path adds a default <code>LIMIT 200</code>, compared with Bridgette’s 500. Bridge scope is prompt-enforced; there is no parser that mechanically proves the generated query contains the selected bridge predicate.

### 23.2 Visual investigation

“Use visual investigation” is an explicit frontend branch to a separate image-detection/vision endpoint. Once checked, it remains checked for subsequent questions while the Ask tab remains mounted, so each later question follows the visual endpoint until the user unchecks it. It resets when the relevant modal/tab unmounts and is not persisted in the backend or shared with another user. The ordinary LLM router does not autonomously turn it on. Visual investigation uses report images and a vision-capable model path; it is not part of Bridgette’s statewide SQL execution.

### 23.3 Point clouds

Point-cloud cleaning and segmentation are separate processing services and UI workflows. CPU geometry produces the numerical cleaning/segmentation results, while Cerebras can review candidate classifications. Their processed geometry is not included in Bridgette’s schema prompt or answer evidence. A database record may tell Bridgette that a bridge has associated metadata only if such metadata is stored in one of its exposed tables; the point coordinates and segmented objects are not queried by this agent.

### 23.4 “Show me bridge” map action

The sidebar checks map-action wording before calling Bridgette. A regex classifier in [mapBridgeEvents.ts](../frontend/src/lib/mapBridgeEvents.ts#L25) detects words such as show, find, locate, or zoom and extracts a bridge number or search text. The classifier is intentionally broad, so a database question containing an action word plus “bridge” can be intercepted as navigation; the sidebar waits up to 45 seconds for the map result.

It dispatches a browser event. On the first command, the map fetches and caches <code>GET /api/map-data?state_code=34</code>. With no explicit year, the backend returns the latest available snapshot for each bridge. The map code then:

1. searches this statewide latest-per-bridge cache;
2. validates and deduplicates coordinates;
3. ranks exact/partial bridge number, name, location, and token matches;
4. handles not-found and ambiguous matches;
5. switches state, county, and data year as needed; and
6. rebuilds the visible layer and tells ArcGIS to zoom to level 16 and open the existing popup/card.

This action is deterministic frontend behavior. No LLM generates SQL or chooses a bridge. Full-page Bridgette does not perform UI actions. The statewide search cache is not invalidated after a Loader update, so a newly loaded or changed bridge can remain missing or stale until the Map component remounts.

### 23.5 Comparison

| Function | Scope | Evidence | Main control path |
|---|---|---|---|
| Bridgette | Statewide shared inventory | SQLite rows | question → SQL → rows → Cerebras summary |
| Bridge Ask | One selected bridge | scoped SQL plus selected report chunks | parallel retrieval → grounded answer |
| Visual investigation | Selected bridge report images | VLM detections/observations | explicit checkbox/branch |
| Show bridge | Map dataset and coordinates | deterministic search result | event → ArcGIS zoom and popup |
| Point cloud | Selected point-cloud file | geometric points/segments | cleaning/segmentation services |

---

## 24. Authentication, authorization, and shared-user behavior

### 24.1 Role hierarchy

BridgeGuard uses three roles:

| Role | Read inventory | Use Bridgette/Ask | Convert/reverse | Load and supported mutation APIs | Raw/child-table administration |
|---|---:|---:|---:|---:|---:|
| Viewer | Yes | Yes | Yes | No | No |
| Editor | Yes | Yes | Yes | Yes | No |
| Admin | Yes | Yes | Yes | Yes | Yes |

The authoritative enforcement is in backend middleware, beginning at [auth.py](../backend/app/auth.py#L219). Conversion and reverse conversion are viewer-level because they return transformed files but do not mutate the shared inventory. Loading is explicitly editor-level. The generic raw SQL/table endpoints are admin-level.

The table describes backend authority. The current full Editor page itself depends on admin-only table-list, table-schema, and generic child-table endpoints to select and open records. A non-admin Editor can use the permitted loader and targeted mutation endpoints but may be unable to navigate the entire Editor workflow through the present UI. This is a role/UI integration gap.

### 24.2 Cloudflare Access identity

In production, the middleware can verify a Cloudflare Access JWT, including signature, issuer, and audience, extract the authenticated email, and map it to an application role. This establishes who may enter and which operation class is allowed.

It does not by itself provide:

- per-user data partitions;
- tenant separation;
- per-user Bridgette history;
- row-level database security;
- request-owned cancellation; or
- a durable audit log of question and SQL.

Those are application responsibilities.

### 24.3 Multi-user data semantics

Every authorized user sees the same loaded inventory. An editor’s same-year refresh can change the answer another viewer receives seconds later. The database supports concurrent reads well through WAL mode, but SQLite still serializes writes. A large loader transaction can delay another mutation.

Application settings selected in local React state are normally isolated by browser instance. Inventory rows, attachment records, processed caches, global schema cache, and global cancel events are server-shared. The most serious cross-user defect is the cancellation event described in Section 22.

### 24.4 Multi-tenant decision

The present NJDOT/Rowan collaborator model does not necessarily require full multi-tenancy if everyone is intended to work on one authoritative inventory. It does require **multi-user correctness**:

- user/session-scoped chat and cancellation;
- optimistic concurrency or revision checks for edits;
- audit records tied to authenticated email;
- role-aware UI;
- safe loader transactions; and
- stable snapshot publication.

Full multi-tenancy becomes necessary only if different organizations must have isolated bridges, files, configurations, billing, or legal data boundaries. Tenant ID would then need to be part of every primary/foreign key, query, cache key, attachment path, and audit record—not merely an authentication claim.

---

## 25. Conformance matrix: official rule versus current implementation

| Topic | FHWA foundation | Current BridgeGuard behavior | Assessment |
|---|---|---|---|
| Source formats | Legacy fixed-width/delimited NBI and element data | Supports fixed-width, header/positional CSV, and simple <code>FHWAED</code> XML | Substantial support; representative annual-file testing still needed |
| Batch identity | State and agency context is needed because structure numbers are not nationally unique | NBI and element groups are keyed by structure number alone | **Cross-state collision risk** |
| Transition eligibility | 42A highway code and Item 112 = Y | Maps every group with an over-record; Item 112 is not enforced | **Material gap** |
| Non-highway over-records | Do not transition under official population rule | Retained and marked <code>fhwa_omit</code> if raw provenance exists | Deliberate application deviation; export is not protected |
| Under-records | Only with a transitioned over-record; type 2 preferred to A | Groups with over-record; type 2 preferred; secondaries numbered | Mostly aligned, but parent eligibility is broader |
| Direct fields | Preserve substantive value | Items such as 27 and 61 copied with normalization/validity checks | Aligned for valid values |
| Bridge-number formatting | Agency chooses whether to trim <code>BID01</code> leading zeros and applies the choice consistently | Whitespace/quotes are trimmed, but zeros are preserved and no agency choice is offered | **Transition-tool option missing** |
| Border bridge number | Apply the Item 99 transition and specified leading-zero treatment | Blank Item 99 becomes <code>N</code> and populated values retain leading zeros | **Conditional/format gap** |
| Code maps | Use crosswalk/detailed mapping | Python dictionaries and helper functions | Substantial; rule artifacts are not version-pinned |
| Unlisted codes | Null | Conservative lookup generally returns null | Aligned |
| No-transition fields | Collect/code later | Many omitted, but selected placeholders/defaults added | Mixed; defaults need explicit labels |
| Repeated features/routes | Transition with single-instance assumptions and later review | Creates one carried route and numbered under features | Aligned with transition assumption |
| Multiple span configurations | Not recoverable from legacy model | One main and one approach; placeholder if absent | Aligned limitation, plus application default |
| Substructure detail | No direct transition | Placeholder substructure set synthesized | **Application-only value** |
| Coordinate conversion | DMS to signed decimal | Implemented with territorial sign rules | Aligned within rounding |
| Metric geometry | Convert to feet using each item's specified rounding | Unit conversion is implemented, but <code>BH12</code> is rounded down instead of to the nearest tenth | **Item-specific mismatch** |
| NBIS bridge length | Apply the 6.1 m/20.1 ft transition treatment and verify threshold cases | <code>BG01</code> is populated only above 9.1 m | **Material threshold gap** |
| Inspection history | Requires history/assumptions; some fields unavailable | Creates events from current annual row and fills proxy values | Partial; synthesized fields need review |
| Temporary codes | Transition-period only; replace by 2028 | Expiry check exists only on the native/nonlegacy path; converted legacy rows bypass it | **Not enforced for converted rows** |
| JSON structure | Official FHWA schema and repeated arrays | Local recognized subset and normalization | Useful internal shape; not full schema proof |
| Validation | Official validation logic/module | Selected local high-confidence checks | **Not equivalent** |
| Converted-row validation | Transition rules and phased requirements still apply | Most native structural/code/safety checks skipped as legacy | Prevents noise but reduces assurance |
| Source preservation | Transition tool guidance emphasizes legacy preservation | Parsed over-record stored | Partial; unders, raw XML, file/version metadata absent |
| Reverse output | No claim that rich SNBI is losslessly legacy-shaped | Deterministic best-effort collapse | Correctly possible only as lossy export |
| Reverse under-records | Rich SNBI may contain several under features/routes | Always emits one type-1 row and no type-2/A rows | **Hierarchy is not reconstructed** |
| Legacy fixed-width output | 445-character format | Headered CSV downloaded as <code>.txt</code> | **Format mismatch** |

---

## 26. Edge cases and failure analysis

### 26.1 Conversion

| Edge case | Current result | Consequence |
|---|---|---|
| No over-record | Bridge group skipped | Under data disappears, consistent only if parent is absent |
| Item 112 not Y | Still converted | Noneligible official transition record can enter database/export |
| Item 42A non-highway | Converted, later flagged when provenance exists | Review depends on preserved source and manual action |
| All-states input reuses a structure number | Group key collides before New Jersey filtering | One state's over-record can replace another and under-records can mix |
| Unknown code | Usually omitted | Correctly conservative, but reason not retained per field |
| Multiple type-2 unders | Last type-2 can become primary | Potential wrong H02 source |
| Namespaced element XML | Simple XPath may find nothing | Silent loss while response says XML supplied |
| Element structure padding mismatch | No merge | Element data detached |
| Non-NJ-only batch | Intended 400 can be rewrapped as 500 | Misleading API status |
| Mapper exception | Counted as skipped without structured reason | Difficult batch audit |
| Direct value differs after load/edit | Provenance mismatch or later mutation | Cannot be explained as valid transition |
| Item 49 equals 6.1 m or lies below 9.1 m | <code>BG01</code> omitted | Does not follow FHWA's transition-period NBIS-length treatment |
| Item 10 requires nearest-tenth conversion | <code>BH12</code> rounded downward | Clearance can differ by 0.1 ft from the crosswalk result |

### 26.2 Normalization and validation

| Edge case | Current result | Consequence |
|---|---|---|
| Unknown SNBI key | Discarded by normalizer or ignored by loader | Data loss may be quiet |
| Posting status without date | Mapper emits it; normalizer may warn | Application self-warning |
| One bridge contains a rare field | Batch coverage marks field present | Coverage overstates completeness |
| Legacy-derived bridge | Most native rules skipped | Low issue count does not prove validity |
| Critical issue in loader | Load continues | Invalid data can become operational |

### 26.3 Loading

| Edge case | Current result | Consequence |
|---|---|---|
| User selects wrong inventory year | Accepted | Entire snapshot misfiled |
| Native JSON nested object has wrong type | Loader may fail batch | No normalization safety |
| Existing snapshot only | Refresh commits, API reports conflict | UI and database disagree |
| Scalar omitted on refresh | Old scalar retained | Main row is a patch, not replacement |
| Child array omitted on refresh | Old children deleted | Child data is full replacement |
| Same key twice in upload | Last occurrence determines final children | Counts exceed unique snapshots |
| Main or child <code>safe_insert</code> error | May be swallowed | Failed or incomplete snapshot can be miscounted/reported successful |
| Refresh changes conditions | Synthetic history row not updated | Charts can disagree with inventory |
| Delete/reload year | Synthetic history may remain | Stale derived history |
| Parent-first deletion | Possible FK failure | Delete endpoint can fail |

### 26.4 Bridgette

| Edge case | Current result | Consequence |
|---|---|---|
| Follow-up “what about Bergen?” | No prior context sent | Ambiguous or wrong SQL |
| Generated SQL uses wrong join | May execute successfully | Fluent but semantically wrong answer |
| Existing very large LIMIT | Preserved | High memory/result risk |
| Expensive aggregate with LIMIT | Full work can still run | Availability risk |
| Schema migration after process start | Cached schema may be stale | Query failure or missing columns |
| More than 40 result rows | Narrator sees first/last 20 | Middle-row facts unavailable |
| One user presses Stop | Global cancel state | Another user’s request can stop |
| Sidebar answer | SQL/rows hidden | Less auditable than full page |
| Client/API supplies a model hint | Provider ignores hint | Server-configured model remains authoritative |
| Visual checkbox remains checked in mounted Ask tab | Later questions keep using visual endpoint | User must uncheck it; state is local, not backend persistence |
| Show-bridge command after a load | Statewide search cache may be stale | New/changed bridge may require Map remount |

---

## 27. Reproducibility and evaluation protocol

### 27.1 Conversion study package

A reproducible conversion experiment should preserve:

1. the exact FHWA annual download URL;
2. download time and HTTP metadata;
3. original archive and extracted-file SHA-256 hashes;
4. state, inventory year, file layout, and record count;
5. optional element-file identity and hash;
6. FHWA crosswalk, detailed-code-map, schema, and validator artifact versions/hashes;
7. BridgeGuard Git revision;
8. Python and dependency versions;
9. conversion configuration;
10. generated JSON hash;
11. local warnings and official validator output; and
12. a field-level source/rule/output manifest.

### 27.2 Golden test corpus

The corpus should deliberately include:

- type-1 over with no under;
- valid type-2 primary under;
- both type 2 and A;
- multiple secondary under-records;
- every Item 42A and 42B code;
- Item 112 Y and non-Y;
- border bridge;
- lift bridge/navigation;
- culvert;
- one-span and multi-span bridges;
- unknown/blank codes;
- every load-rating method family;
- reconstruction;
- routine, NSTM, underwater, and special inspection records;
- NHS bridge with elements;
- non-NHS bridge without elements;
- namespaced and non-namespaced XML;
- values around unit/rounding thresholds; and
- malformed records and duplicate keys.

### 27.3 Suggested conversion metrics

| Metric | Definition |
|---|---|
| Eligibility precision | Officially transition-eligible output bridges / all output bridges |
| Eligibility recall | Officially eligible bridges output / all officially eligible input bridges |
| Direct-field exactness | Valid direct fields whose substantive value exactly matches source / valid direct fields tested |
| Code-map accuracy | Outputs matching pinned detailed mapping / mapped fields tested |
| Derived-field tolerance | Outputs within documented formula/rounding tolerance / derived fields tested |
| Null correctness | Unsupported/unlisted values correctly omitted / unsupported values tested |
| Permanent-code completeness | Permanent populated codes / eligible target fields |
| Temporary-code rate | Temporary outputs / populated outputs |
| Default rate | Application-default outputs / populated outputs |
| Per-field population | Bridges populated for field / bridges eligible for field |
| Official validation pass rate | Bridges/batches passing current FHWA validator |
| Provenance completeness | Outputs with full source/rule/version lineage / outputs tested |

### 27.4 Suggested loader tests

Direct tests should cover:

- new batch insert;
- all-existing refresh;
- mixed new and existing;
- scalar omission on refresh;
- child omission on refresh;
- duplicate key in one upload;
- malformed child collection;
- child constraint failure and rollback;
- source provenance replacement/removal;
- synthetic history update;
- year and single-snapshot deletion under foreign keys;
- wrong state;
- wrong year;
- unknown keys;
- large-file limit; and
- concurrent read during WAL write.

Current tests demonstrate exact-year/latest-year snapshot retrieval, year-scoped edits, and role hierarchy, but there is no direct comprehensive test suite for <code>/api/load-json</code> or <code>load_json_to_db</code>.

### 27.5 Suggested Bridgette evaluation

Text-to-SQL and answer quality should be evaluated separately.

**SQL evaluation set**

- exact current count;
- explicit historical year;
- multi-year trend;
- county join;
- ranking without duplicate bridges;
- NHS/non-NHS filter;
- feature/route join;
- condition classification;
- null handling;
- age relative to current year;
- ambiguous question;
- impossible question; and
- attempted unsafe instruction.

**Metrics**

| Metric | Meaning |
|---|---|
| Execution accuracy | Generated SQL runs |
| Result accuracy | SQL returns the reference result |
| Scope accuracy | Correct state, year, bridge, and tenant/user scope |
| Join accuracy | No duplication or missing parent relationships |
| Answer faithfulness | Every factual claim supported by result/SQL |
| Completeness | Answer covers every requested part |
| Abstention quality | Correctly declines when schema cannot answer |
| Evidence transparency | SQL and decisive rows visible to reviewer |
| Latency | SQL-generation, execution, first-token, total |
| Resource safety | Runtime, rows, memory, and cancellation behavior within limits |

Evaluation queries and reference SQL should be versioned alongside a frozen database snapshot hash.

### 27.6 Minimal repeatable run

1. Download one official New Jersey annual “all records” file and matching element XML.
2. Record hashes and source URLs.
3. Run <code>/api/convert</code>.
4. Save raw response, normalization warnings, coverage, and validation.
5. Independently compute the official eligibility predicate and compare record sets.
6. Sample every rule category and compare with the pinned FHWA mapping.
7. Run the official FHWA validation module on the candidate submittal form.
8. Load into an empty temporary database under the declared year.
9. Export the same year and compare all normalized records.
10. Run reference Bridgette questions against a frozen DB and compare generated results with reference SQL.

---

## 28. Recommended remediation roadmap

### Priority 0: prevent incorrect submission interpretation

1. Enforce or explicitly select the official transition predicate, including Item 112.
2. Exclude <code>fhwa_omit</code> records from FHWA-facing export by default; require an explicit reviewed override.
3. Rename “parity” to “local normalization” and label the reverse file as legacy-shaped CSV.
4. Place a clear banner on converted output: “transition draft; complete/verify missing and temporary SNBI data; validate with FHWA tooling.”
5. Correct reverse posting mappings before relying on the reverse export.

### Priority 1: make provenance research-grade

1. Store original over and every under-record, including record type and ordering.
2. Store raw element input or a content-addressed immutable copy.
3. Record source filename, official URL, file hash, annual release/correction date, and user.
4. Pin and hash FHWA crosswalk, detailed mapping, schema, and validation artifacts.
5. Assign a versioned rule ID to each output field and record its source values.
6. Distinguish direct, code-mapped, derived, temporary, defaulted, user-edited, and unavailable values in the database.

### Priority 2: make loading deterministic and auditable

1. Normalize and structurally validate native JSON before mutation.
2. Make validation policy configurable: advisory, reject critical, or official-submission gate.
3. Decide that refresh is either complete replacement or explicit patch; apply the same semantics consistently.
4. Treat all-existing refresh as success.
5. Reject or report duplicate snapshot keys.
6. Stop swallowing child insert errors; use per-bridge savepoints or full rollback.
7. Update/delete synthetic history consistently with snapshot changes.
8. Use child-first deletion or cascading foreign keys.
9. Replace full-payload debug logging with structured identifiers, counts, hashes, and outcomes.

### Priority 3: harden Bridgette

1. Parse generated SQL with an SQLite-aware parser.
2. Enforce one select statement, allowed tables/columns/functions, and an outer hard row cap.
3. Programmatically inject or verify state, selected bridge, year, tenant, and user predicates.
4. Install query deadlines/progress interruption and complexity limits.
5. Key schema cache by database/schema version and invalidate after migrations.
6. Store user, question hash/text according to policy, SQL, duration, row count, outcome, and model/version in an audit log.
7. Add result-to-answer numerical/factual consistency checks.
8. Show evidence in the sidebar or link directly to the full-page evidence panel.
9. Use per-request, per-user cancellation tokens.
10. Persist conversation history only after defining retention, privacy, and whether previous turns enter future prompts.

### Priority 4: prepare multi-user operation

1. Attach every mutation and long-running job to authenticated user identity.
2. Add optimistic version checks to prevent silent edit overwrites.
3. Add rate limits and resource quotas per user.
4. Keep one shared workspace if collaboration is intended; introduce tenant IDs only when organizational isolation is a real requirement.
5. If tenants are added, include tenant scope in database keys, files, embeddings, caches, point clouds, audit records, and generated SQL enforcement.

---

## 29. Conclusion

BridgeGuard’s conversion architecture is practical: it can parse several real-world legacy NBI layouts, reconstruct the SNBI repeated hierarchy, merge bridge elements, preserve a useful portion of source data, and load annual snapshots into a queryable relational database. Its transition code contains many concrete FHWA-derived mappings and formulas rather than delegating conversion to an LLM.

The most important scientific and operational qualification is that its output is a **transition draft**. The official legacy-to-SNBI process includes eligibility rules, temporary-code cleanup, collection of fields with no legacy transition, verification of structural assumptions, and official validation. BridgeGuard currently performs only part of that process and intentionally adds some product-oriented defaults. Those defaults are useful for navigation and editing but must remain visibly distinct from authoritative source-derived values.

The loader’s state/bridge/year identity and normalized child tables provide a strong basis for historical analysis. Before relying on repeated loads as an authoritative data-management operation, refresh semantics, silent child errors, history synchronization, deletion ordering, and the misleading conflict response should be corrected.

Bridgette provides a transparent and compelling interface to the resulting database. Its strongest property is the inspectable chain from question to SQL to deterministic rows to summary. Its principal weaknesses are the absence of structural SQL verification, hard resource limits, persistent conversation identity, request-scoped cancellation, and user/tenant data scoping. These are tractable engineering problems and do not require replacing the core design.

With version-pinned federal rules, complete lineage, deterministic loading, official validation gates, a golden corpus, and hardened text-to-SQL execution, the same architecture can support a defensible research artifact and a reliable collaborative bridge-data application.

---

## References

Web resources were verified on August 11, 2026.

### Federal Highway Administration

1. Federal Highway Administration. [National Bridge Inventory Based on the SNBI: resource hub](https://www.fhwa.dot.gov/bridge/snbi.cfm). Current index for specification, errata, transition, schema, and validation resources.
2. Federal Highway Administration. [Specifications for the National Bridge Inventory, March 2022, FHWA-HIF-22-017](https://www.fhwa.dot.gov/bridge/snbi/snbi_march_2022_publication.pdf).
3. Federal Highway Administration. [Errata #1 to the March 2022 SNBI publication, March 2024](https://www.fhwa.dot.gov/bridge/snbi/errata1_to_snbi_march_2022_publication.pdf). Informational redline pending future incorporation through rulemaking.
4. Federal Highway Administration. [SNBI Updates: Schema, Validation Logic, and NBIS Bridge Length, September 17, 2024](https://www.fhwa.dot.gov/bridge/pubs/Memo-SNBI_Updates_Schema_ValidationLogic_NBISBridgeLength.pdf).
5. Federal Highway Administration. [Implementation of the Specifications for the National Bridge Inventory](https://www.fhwa.dot.gov/bridge/pubs/Memo-Implementation_Specifications_National_Bridge_Inventory_FINAL.pdf).
6. Federal Highway Administration. [Questions and Answers: Data Transition](https://www.fhwa.dot.gov/bridge/snbi/qanda/03.cfm).
7. Federal Highway Administration. [Data Crosswalk for Over Records and Under Records](https://www.fhwa.dot.gov/bridge/snbi/datacrosswalk.cfm).
8. Federal Highway Administration. [Detailed Code Mapping for Individual Data Items](https://www.fhwa.dot.gov/bridge/snbi/codemapping.cfm).
9. Federal Highway Administration. [NBI Submittal File Schema](https://www.fhwa.dot.gov/bridge/snbi/schema.cfm).
10. Federal Highway Administration. [SNBI Data Submittal Validation Logic, version 1.1](https://www.fhwa.dot.gov/bridge/snbi/data_validation_logic.cfm).
11. Federal Highway Administration. [Annual NBI ASCII Data Archive](https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm).
12. Federal Highway Administration. [2025 NBI ASCII/Delimited Data](https://www.fhwa.dot.gov/bridge/nbi/ascii2025.cfm).
13. Federal Highway Administration. [Legacy NBI Record Format](https://www.fhwa.dot.gov/bridge/nbi/format.cfm).
14. Federal Highway Administration. [2025 NBI Element Data](https://www.fhwa.dot.gov/bridge/nbi/element2025.cfm).
15. Federal Highway Administration. [Recording and Coding Guide for the Structure Inventory and Appraisal of the Nation’s Bridges](https://www.fhwa.dot.gov/bridge/mtguide.pdf).

### Software behavior

16. SQLite. [URI Filenames and <code>mode=ro</code>](https://www.sqlite.org/uri.html).
17. BridgeGuard source code at revision <code>8f4d00244c76a43c112380c1e26a65e01112462f</code>, cited through repository-relative links throughout this report.

---

## Appendix A. Exact high-use transition maps in the current code

This appendix records the current Python lookup values for audit convenience. These are **implementation facts**, not a replacement for the latest FHWA tables. The authoritative source remains the current FHWA crosswalk and detailed mapping, and differences should be resolved in favor of a versioned, reviewed rule update.

### A.1 Owner and maintenance, Items 22 and 21

| NBI | SNBI | NBI | SNBI | NBI | SNBI |
|---|---|---|---|---|---|
| 01 | S01 | 02 | L01 | 03 | L02 |
| 04 | L03 | 11 | S02 | 12 | L04 |
| 21 | SX | 25 | LX | 26 | P |
| 27 | R | 31 | S03 | 32 | L05 |
| 40 | T | 54 | F08 | 55 | F09 |
| 56 | F07 | 57 | F03 | 58 | F05 |
| 59 | D05 | 60 | FX | 61 | I |
| 62 | FL01 | 63 | FL04 | 64 | FL07 |
| 66 | FL05 | 67 | F06 | 68 | FL02 |
| 69 | FL03 | 70 | FL06 | 72 | D01 |
| 73 | D03 | 74 | D02 | 75 | F04 |
| 76 | T | 77 | D04 | 78 | F01 |
| 79 | F02 | 80 | U |  |  |

Source: [snbi_rules.py](../backend/app/routers/services/snbi_rules.py#L20).

### A.2 Classification and status

| Legacy item | Mapping |
|---|---|
| Item 105 Federal Land | <code>0→N; 1→BIA; 2→USFS; 3→BLM; 4→BIA&#124;USFS; 5→BIA&#124;BLM; 6→BLM&#124;USFS; 9→BIA&#124;BLM&#124;USFS</code> |
| Item 37 Historic | <code>1→1; 2→2; 3→T; 4→7; 5→N</code> |
| Item 20 Toll | <code>1→1; 2→3; 3→N; 4→4; 5→2</code> |
| Items 36A/36B roadside hardware | <code>0→I0-T; 1→Y-T; N→N</code> |
| Item 104 NHS | <code>0→N; 1→Y</code> |
| Item 110 NHFN | <code>0→N; 1→1-T</code> |
| Item 100 STRAHNET | <code>0→N; 1→1; 2→1; 3→2</code> |

### A.3 Functional class and urban code from Item 26

| NBI Item 26 | <code>BH01</code> functional class | <code>BH02</code> urban code |
|---|---|---|
| 01 | 1 | 99999 |
| 02 | 3 | 99999 |
| 06 | 4 | 99999 |
| 07 | 5 | 99999 |
| 08 | 6 | 99999 |
| 09 | 7 | 99999 |
| 11 | 1 | T-U |
| 12 | 2 | T-U |
| 14 | 3 | T-U |
| 16 | 4 | T-U |
| 17 | C-T | T-U |
| 19 | 7 | T-U |

### A.4 Route values

| Source | Mapping |
|---|---|
| Item 5E suffix | <code>1→N; 2→E; 3→S; 4→W</code> |
| Item 102 direction | <code>1→1-T; 2→2-T; 3→2-T</code> |
| Item 5B route type | <code>1→1; 2→2; 3→3; 4→4; 5→5; 6→6; 7→7; 8→X</code> |
| Item 5C service type | <code>0→X; 1→1; 2→2; 3→3; 4→4; 6→6; 7→7; 8→8</code> |

### A.5 Span material, Item 43A/44A

| NBI | SNBI | Interpretation note |
|---|---|---|
| 0 | X-T | Unknown temporary |
| 1 | CR-T | Concrete-related temporary |
| 2 | CR-T | Concrete-related temporary |
| 3 | S-T | Steel-related temporary |
| 4 | S-T | Steel-related temporary |
| 5 | CP-T | Prestressed-concrete temporary |
| 6 | CP-T | Prestressed-concrete temporary |
| 7 | T-T | Timber temporary |
| 8 | M-T | Masonry temporary |
| 9 | AI-T | Aluminum/iron-related temporary |

### A.6 Span type, Item 43B/44B

| NBI | SNBI | NBI | SNBI | NBI | SNBI |
|---|---|---|---|---|---|
| 00 | X-T | 01 | S-T | 02 | GB-T |
| 03 | G09 | 04 | GT-T | 05 | BM-T |
| 06 | BS-T | 07 | F-T | 08 | S-T |
| 09 | T01 | 10 | T-T | 11 | AD-T |
| 12 | AT-T | 13 | L01 | 14 | L-T |
| 15 | M01 | 16 | M02 | 17 | M03 |
| 19 | C-T | 20 | Z-T | 21 | B04 |
| 22 | GC-T |  |  |  |  |

### A.7 Span continuity derivation

The helper in [snbi_crosswalk.py](../backend/app/routers/services/snbi_crosswalk.py#L171) evaluates material/kind, type, and span count in order:

1. kind 2, 4, or 6 → <code>C-T</code>;
2. type 07 → <code>6</code>;
3. type 11 or 12 with positive spans → <code>2</code>;
4. type 13 or 14 → <code>2</code>;
5. type 19 → <code>7-T</code>;
6. any other nonblank type → <code>1</code>;
7. blank → null.

### A.8 Deck material and protection

| Source | Mapping/condition |
|---|---|
| Item 107 deck material | <code>1→CR-T; 2→CP-T; 3→S01; 4→S02; 6→S05; 7→A01; 8→T-T; 9→X-T; N→0</code> |
| Item 107 = 5 | <code>S04</code> only when structural type is 08; otherwise <code>S-T</code> |
| Item 108A wearing | <code>0→0; 1→C01; 2→C02; 3→C-T; 4→C04; 5→P-T; 6→B01; 7→T01; 8→E01; 9→X-T</code> |
| Item 108B membrane | <code>1→M01; 2→M02; 3→MX-T; 8→MU; 9→MX-T</code> |
| Item 108C reinforcing | <code>0→0; 1→C01; 2→C02; 3→C-T; 4→S-T; 6→0; 7→0; 8→X-T; 9→X-T</code> |
| Membrane 0 plus reinforcing 6 | Protective system <code>A03</code> |
| Membrane 0 plus reinforcing 7 | Protective system <code>A01</code> |
| Item 107 = N | Wearing/protection helpers return null where inapplicable |

### A.9 Design load, Item 31

| NBI | <code>BLR01</code> | NBI | <code>BLR01</code> |
|---|---|---|---|
| 0 | U | 1 | H10 |
| 2 | H15 | 3 | HS15 |
| 4 | H20 | 5 | HS20 |
| 6 | HS20M | 8 | RR |
| 9 | HS20Plus | A | HL93 |
| B | HL93Plus | C | X |

### A.10 Load-rating method, Item 63

| NBI | <code>BLR04</code> | NBI | <code>BLR04</code> |
|---|---|---|---|
| 0 | EJ | 1 | LFR |
| 2 | ASR | 3 | LRFR |
| 4 | LT | 5 | N |
| 6 | LFR | 7 | ASR |
| 8 | LRFR | A | AR |
| B | AR | C | AR |
| D | AR | E | AR |
| F | AR |  |  |

### A.11 Posting status, Item 41

| NBI | <code>BPS01</code> |
|---|---|
| A | PO |
| B | PA-T |
| D | SO |
| E | TO |
| G | N |
| K | C |
| P | PP-T |
| R | PR-T |

### A.12 Scour, Item 113

| Item 113 | <code>BAP03</code> scour vulnerability | <code>BC11</code> scour condition | <code>BAP04</code> POA |
|---|---|---|---|
| N | null | N | null |
| U | U | null | Y-T |
| T | 0 | null | Y-T |
| 9 | A | 9 | 0 |
| 8 | AB-T | MI-T | 0 |
| 7 | BCE-T | MI-T | Y-T |
| 6 | 0 | null | Y-T |
| 5 | AB-T | MI-T | 0 |
| 4 | AB-T | MO-T | 0 |
| 3 | CD-T | MI-T | Y-T |
| 2 | CD-T | MA-T | Y-T |
| 1 | CD-T | 1 | Y-T |
| 0 | CD-T | 0 | Y-T |

### A.13 Overtopping from Items 71 and 26

Let “minor/local” mean Item 26 is 08, 09, or 19.

| Item 71 | Rule for <code>BAP02</code> |
|---|---|
| 9, 8, or 5 | 1 |
| 7 | VLM-T |
| 6 | 1 for minor/local, otherwise VLM-T |
| 4 | 5 for minor/local, otherwise 1 |
| 3 | 6 for minor/local; 1 for functional class 01, 11, or 12; otherwise 5 |
| 2 or 0 | HVH-T |
| N or blank | null |

### A.14 Temporary code set recognized by local validation

The current implementation identifies the following transition codes as temporary:

    1-T, 2-T, 7-T, A-T, AD-T, AI-T, AT-T, BM-T, BS-T,
    C-T, CP-T, CR-T, F-T, GB-T, GC-T, GT-T, I0-T,
    L-T, M-T, MA-T, MI-T, MO-T, MX-T, P-T, PA-T,
    PP-T, PR-T, S-T, T-T, X-T, Y-T, Z-T, T-U,
    VLM-T, HVH-T, AB-T, BCE-T, CD-T

The complete live definitions are in [snbi_rules.py](../backend/app/routers/services/snbi_rules.py#L11). A code in this list should be treated as a conversion flag requiring verification, not merely as another permanent categorical value.

---

## Appendix B. Source-code index

### B.1 Conversion and schema

| Function or object | Location | Responsibility |
|---|---|---|
| Fixed-width field slices | [parser.py](../backend/app/routers/services/parser.py#L18) | Zero-based interpretation of the legacy 445-character record |
| <code>parse_nbi_txt</code> | [parser.py](../backend/app/routers/services/parser.py#L240) | Detect layout, parse rows, group over/under records |
| <code>parse_elements_xml</code> | [parser.py](../backend/app/routers/services/parser.py#L344) | Group legacy <code>FHWAED</code> rows and map element fields |
| <code>map_bridge_to_snbi</code> | [mapper.py](../backend/app/routers/services/mapper.py#L63) | Coordinate the entire forward field transition |
| Mapping helpers | [snbi_crosswalk.py](../backend/app/routers/services/snbi_crosswalk.py#L42) | Cleanup, code lookups, dates, spans, approach/scour derivations |
| Rule dictionaries | [snbi_rules.py](../backend/app/routers/services/snbi_rules.py#L11) | Temporary codes, code tables, supported code lists |
| Numeric helpers | [utils.py](../backend/app/routers/services/utils.py#L33) | Coordinates, feet, miles, rating factors |
| Primary under selection | [utils.py](../backend/app/routers/services/utils.py#L93) | Prefer record type 2, then A |
| <code>normalize_snbi_bridge</code> | [snbi_schema.py](../backend/app/routers/services/snbi_schema.py#L108) | Normalize one bridge to local recognized shape |
| <code>normalize_snbi_payload</code> | [snbi_schema.py](../backend/app/routers/services/snbi_schema.py#L174) | Normalize a batch |
| Coverage | [snbi_schema.py](../backend/app/routers/services/snbi_schema.py#L216) | Aggregate recognized code presence |
| Local validation | [snbi_validation.py](../backend/app/routers/services/snbi_validation.py#L253) | Selected required, conditional, type, code, and safety rules |
| Forward endpoint | [main.py](../backend/app/main.py#L594) | Upload, parse, map, normalize, NJ filter, validate, return |

### B.2 Reverse conversion

| Function | Location | Responsibility |
|---|---|---|
| <code>_invert</code> | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L37) | Build deterministic inverse lookup maps |
| <code>snbi_to_flat_json</code> | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L94) | Human-readable flat representation |
| <code>snbi_to_text</code> | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L168) | Narrative text representation |
| Geometry/date helpers | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L261) | Feet/meters, DMS, date conversion |
| <code>snbi_to_nbi_row</code> | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L402) | Collapse SNBI into one legacy-shaped row |
| <code>bridges_to_nbi_csv</code> | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L792) | Headered CSV serialization |
| <code>bridges_to_elements_xml</code> | [reverse_mapper.py](../backend/app/routers/services/reverse_mapper.py#L802) | Simple element XML serialization |
| Reverse endpoint | [main.py](../backend/app/main.py#L695) | Decode, normalize, convert, return outputs |

### B.3 Loading and persistence

| Function or schema | Location | Responsibility |
|---|---|---|
| Year extraction | [database.py](../backend/app/routers/services/database.py#L19) | Fallback inventory-year inference |
| Year migration | [database.py](../backend/app/routers/services/database.py#L81) | Migrate older non-year schema |
| <code>init_db</code> | [database.py](../backend/app/routers/services/database.py#L338) | Create main/child/provenance/history tables and indexes |
| <code>get_connection</code> | [database.py](../backend/app/routers/services/database.py#L746) | Operational connection with foreign keys |
| <code>load_json_to_db</code> | [database.py](../backend/app/routers/services/database.py#L993) | Insert/refresh/fan-out/provenance/history transaction |
| Snapshot reads | [database.py](../backend/app/routers/services/database.py#L2349) | Exact or latest year-scoped bridge details |
| Field provenance | [database.py](../backend/app/routers/services/database.py#L2438) | Reconstruct metadata from source row and static rules |
| Bulk export | [database.py](../backend/app/routers/services/database.py#L2524) | Build SNBI JSON for selected state/year |
| Read-only SQL | [database.py](../backend/app/routers/services/database.py#L2969) | Execute query through SQLite <code>mode=ro</code> |
| Loader endpoint | [main.py](../backend/app/main.py#L750) | Validate request, run advisory validation, invoke loader |

### B.4 Bridgette and related workflows

| Function or component | Location | Responsibility |
|---|---|---|
| County lookup | [vision.py](../backend/app/routers/services/vision.py#L270) | Materialize NJ county names |
| Schema builder | [vision.py](../backend/app/routers/services/vision.py#L308) | Focused table/column prompt context |
| SQL extraction/limit | [vision.py](../backend/app/routers/services/vision.py#L351) | Clean model output and append default row limit |
| Nonstreaming Bridgette | [vision.py](../backend/app/routers/services/vision.py#L386) | SQL, execute, summarize, return |
| Streaming Bridgette | [vision.py](../backend/app/routers/services/vision.py#L454) | NDJSON meta/token event generation |
| Bridge-specific SQL context | [vision.py](../backend/app/routers/services/vision.py#L530) | Prompted single-bridge SQL evidence for Ask |
| Report preprocessing | [vision.py](../backend/app/routers/services/vision.py#L1135) | Parse/cache report text, tables, and image descriptions |
| Bridge Ask retrieval | [vision.py](../backend/app/routers/services/vision.py#L1538) | Parallel report retrieval and database evidence |
| Provider model selection | [llm_providers.py](../backend/app/routers/services/llm_providers.py#L373) | Cerebras completion and streaming |
| Bridgette endpoints | [main.py](../backend/app/main.py#L1568) | Nonstreaming and streaming HTTP boundary |
| Cancel endpoint | [main.py](../backend/app/main.py#L1697) | Shared cancellation signal |
| Full-page UI | [Bridgette.tsx](../frontend/src/pages/Bridgette.tsx#L26) | Local turns, streaming, evidence panel |
| Sidebar UI | [SidebarBridgette.tsx](../frontend/src/components/SidebarBridgette.tsx#L13) | Compact local turns and map-command interception |
| Map command parser | [mapBridgeEvents.ts](../frontend/src/lib/mapBridgeEvents.ts#L25) | Detect show/find/zoom and extract query |
| Map search/action | [Map.tsx](../frontend/src/pages/Map.tsx#L140) | Rank target and synchronize map state |
| ArcGIS zoom/popup | [ArcgisMap.tsx](../frontend/src/components/ArcgisMap.tsx#L607) | Locate graphic, zoom, open card |

---

## Appendix C. Recommended conversion manifest

The following example is not a current BridgeGuard output. It is a proposed companion manifest for reproducibility.

~~~json
{
  "dataset": {
    "agency": "FHWA",
    "state": "New Jersey",
    "inventory_year": 2025,
    "source_url": "https://www.fhwa.dot.gov/bridge/nbi/ascii2025.cfm",
    "downloaded_at": "2026-08-11T14:00:00-04:00",
    "archive_filename": "example.zip",
    "archive_sha256": "<sha256>",
    "record_filename": "example.txt",
    "record_sha256": "<sha256>",
    "element_filename": "example.xml",
    "element_sha256": "<sha256>"
  },
  "rules": {
    "snbi_specification": {
      "title": "Specifications for the National Bridge Inventory",
      "revision": "March 2022 plus applicable errata",
      "sha256": "<sha256>"
    },
    "crosswalk": {
      "revision": "2023-12-05",
      "sha256": "<sha256>"
    },
    "detailed_code_mapping": {
      "revision": "<published revision>",
      "sha256": "<sha256>"
    },
    "validation_logic": {
      "revision": "1.1 / 2025-12-30",
      "sha256": "<sha256>"
    }
  },
  "software": {
    "repository": "bensidhom/remote",
    "git_commit": "8f4d00244c76a43c112380c1e26a65e01112462f",
    "environment": "<dependency lock hash>"
  },
  "conversion": {
    "started_at": "<timestamp>",
    "completed_at": "<timestamp>",
    "input_records": 0,
    "eligible_over_records": 0,
    "output_bridges": 0,
    "noneligible_retained_for_review": 0,
    "skipped": 0,
    "temporary_value_count": 0,
    "defaulted_value_count": 0,
    "official_validation_status": "<status>",
    "output_sha256": "<sha256>"
  }
}
~~~

---

## Appendix D. Operational checklists

### D.1 Before conversion

- [ ] Use the intended FHWA annual release and record whether it is highway-only or all-records.
- [ ] Preserve archive and extracted-file hashes.
- [ ] Pair NBI and element data from the same year/revision.
- [ ] Confirm New Jersey state code and expected record count.
- [ ] Pin FHWA rule artifacts and the BridgeGuard revision.
- [ ] Work on a copy; do not overwrite the only source file.

### D.2 After conversion

- [ ] Compare output bridge set with the official 42A plus Item 112 eligibility predicate.
- [ ] Review every mapper skip and dropped-non-NJ count.
- [ ] Confirm element XML produced actual element rows.
- [ ] Review local normalization warnings, not just <code>parity_ok</code>.
- [ ] Review temporary and application-default values.
- [ ] Sample direct fields for exact equality.
- [ ] Validate derived fields against documented formulas.
- [ ] Run the official FHWA validation tool/module.
- [ ] Preserve the output and manifest hashes.

### D.3 Before loading

- [ ] Confirm top-level JSON is an array.
- [ ] Confirm every bridge has <code>BL01=34</code> and <code>BID01</code>.
- [ ] Confirm nested datasets are arrays.
- [ ] Confirm the selected inventory year from independent source metadata.
- [ ] Back up the SQLite database.
- [ ] Decide whether an existing snapshot should be patched/refreshed.
- [ ] Resolve critical validation findings according to project policy.

### D.4 After loading

- [ ] Compare new/refreshed/skipped counts with unique input keys.
- [ ] Verify several main and child rows under the selected year.
- [ ] Confirm source provenance exists for converted NBI records.
- [ ] Check that conditions and synthetic history agree.
- [ ] Treat an all-existing “conflict” response as a possible committed refresh until the defect is fixed.
- [ ] Record database hash/backup and load audit metadata.

### D.5 Before accepting a Bridgette answer

- [ ] Open the full evidence view when the answer affects analysis or a decision.
- [ ] Check the generated SQL’s state, year, bridge, and county filters.
- [ ] Check for duplicate-producing joins.
- [ ] Verify that current questions use the latest year and historical questions use the requested year.
- [ ] Compare all reported numbers with returned rows.
- [ ] Rephrase follow-ups as self-contained questions.
- [ ] Treat the result as decision support, not an engineering determination.
