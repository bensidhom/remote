"""Static contracts for the Editor's canonical revision workflow.

The frontend build catches TypeScript/JSX defects; these checks freeze the
cross-layer safety properties that are otherwise easy to regress into legacy
row-by-row writes or local-only child drafts.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "frontend" / "src" / "pages" / "Editor.tsx"


def _source() -> str:
    return EDITOR.read_text(encoding="utf-8")


def test_editor_loads_one_exact_published_revision_and_its_version_headers():
    source = _source()
    assert "/api/revisions/snapshots/${bridge.state_code}/" in source
    assert "response.data?.canonical_document" in source
    assert "response.headers.etag" in source
    assert "response.headers['x-bridgeguard-data-version']" in source
    assert "setPublishedDocument(deepCopy(canonical))" in source
    assert "setPublishedEtag(etag)" in source
    assert "setDataGeneration(generation)" in source
    # A separate state request can race the snapshot read and pair the ETag
    # with a generation from a different publication.
    assert "axios.get('/api/revisions/state')" not in source


def test_editor_stages_one_complete_merge_patch_with_all_preconditions():
    source = _source()
    assert "createMergePatch(publishedDocument, canonicalDocument)" in source
    assert "patch[key] = null" in source
    assert "axios.patch(" in source
    assert "'If-Match': publishedEtag" in source
    assert "'Idempotency-Key': crypto.randomUUID()" in source
    assert "'X-Request-ID': requestId" in source
    assert "expected_data_generation: dataGeneration" in source
    assert "axios.put(" not in source
    assert "/publish`" not in source


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


def test_editor_displays_candidate_diff_and_server_validation_without_publishing():
    source = _source()
    assert "setLastRevisionDiff(diff)" in source
    assert "setStagedValidation(response.data?.validation" in source
    assert "setStagedValidationIssues(issues)" in source
    assert "Staged canonical diff" in source
    assert "Server validation result" in source
    assert "Server validation evidence" in source
    assert "It is not published" in source


def test_editor_selector_uses_bounded_published_snapshot_pages_not_raw_tables():
    source = _source()
    assert "axios.get('/api/revisions/snapshots'" in source
    assert "state_code: selectedStateCode" in source
    assert "data_year: selectedExportYear" in source
    assert "limit: 500" in source
    assert "response.data?.next_cursor" in source
    assert "x-bridgeguard-data-version" in source
    assert "More than 10,000 published bridges" in source
    assert "/api/table/bridges" not in source
    assert "/api/available-years/" not in source


def test_editor_exports_only_exact_revision_package_with_server_manifest():
    source = _source()
    assert "downloadPublishedRevisionPackage" in source
    assert "/api/revisions/${encodeURIComponent(publishedRevisionId)}/exports" in source
    assert "export_kind: 'canonical_profile'" in source
    assert "profile_id: 'research_snbi_export_v1'" in source
    assert "responseType: 'blob'" in source
    assert "server-generated manifest" in source
    assert "/api/export-bridges/" not in source
    assert "/api/validate-bridges/" not in source
    assert "/api/validate-bridge/" not in source
    assert "JSZip" not in source
    assert "Download Table" not in source
    assert "downloadTable" not in source
    assert "tableToCsv" not in source
    assert "escapeCsvValue" not in source
