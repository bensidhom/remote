"""Static contracts for the Editor's exact-year inventory workflow.

The frontend build catches TypeScript/JSX defects; these checks freeze the
cross-layer safety properties that are otherwise easy to regress into legacy
row-by-row writes or local-only child drafts.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "frontend" / "src" / "pages" / "Editor.tsx"


def _source() -> str:
    return EDITOR.read_text(encoding="utf-8")


def test_editor_loads_one_exact_inventory_document_and_its_version_headers():
    source = _source()
    assert "/api/bridge-document/${bridge.state_code}/" in source
    assert "response.data?.canonical_document" in source
    assert "response.headers.etag" in source
    assert "response.data?.data_generation" in source
    assert "setPublishedDocument(deepCopy(canonical))" in source
    assert "setPublishedEtag(etag)" in source
    assert "setDataGeneration(generation)" in source
    # A separate state request can race the snapshot read and pair the ETag
    # with a generation from a different publication.
    assert "axios.get('/api/revisions/state')" not in source


def test_editor_saves_one_complete_document_with_concurrency_preconditions():
    source = _source()
    assert "createMergePatch(publishedDocument, canonicalDocument)" in source
    assert "patch[key] = null" in source
    assert "axios.put(" in source
    assert "'If-Match': publishedEtag" in source
    assert "expected_data_generation: dataGeneration" in source
    assert "canonical_document: canonicalDocument" in source
    assert "/api/revisions/snapshots/" not in source


def test_editor_round_trips_every_child_collection_and_can_clear_rows():
    source = _source()
    for table in (
        "features",
        "routes",
        "span_sets",
        "substructure_sets",
        "elements",
        "inspections",
        "posting_evaluations",
        "posting_statuses",
        "works",
    ):
        assert f"'{table}'" in source
    assert "const mainBridge: Record<string, any> = deepCopy(publishedDocument)" in source
    assert "if (isPlainObject(row?.extensions)) out.extensions = deepCopy(row.extensions)" in source
    assert "mainBridge[schemaKey] = mappedRows" in source
    assert "feature.Routes = routes" in source
    assert "removeChildRow(activeTab, idx)" in source
    assert "Every route row must identify its parent feature in BF01" in source
    assert "Route rows reference missing parent feature(s)" in source


def test_editor_displays_server_validation_after_direct_save():
    source = _source()
    assert "setStagedValidation(response.data?.validation" in source
    assert "setStagedValidationIssues(Array.isArray(issues)" in source
    assert "Saved snapshot changes" in source
    assert "Server validation result" in source
    assert "Server validation evidence" in source
    assert "Saved the ${selectedExportYear} inventory snapshot directly to snbi_bridges.db" in source


def test_editor_selector_uses_bounded_exact_inventory_endpoint():
    source = _source()
    assert "axios.get('/api/bridge-snapshots'" in source
    assert "state_code: selectedStateCode" in source
    assert "year: selectedExportYear" in source
    assert "limit: 10000" in source
    assert "/api/table/bridges" not in source
    assert "/api/available-years/" in source


def test_editor_exports_only_the_exact_loaded_inventory_document():
    source = _source()
    assert "downloadPublishedRevisionPackage" in source
    assert "JSON.stringify([publishedDocument], null, 2)" in source
    assert "application/json" in source
    assert "_inventory.json" in source
    assert "/api/export-bridges/" not in source
    assert "/api/validate-bridges/" not in source
    assert "/api/validate-bridge/" not in source
    assert "JSZip" not in source
    assert "Download Table" not in source
    assert "downloadTable" not in source
    assert "tableToCsv" not in source
    assert "escapeCsvValue" not in source
