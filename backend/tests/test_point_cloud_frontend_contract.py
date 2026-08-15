"""Static contracts for the audited point-cloud replacement workflow."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POINT_CLOUD = ROOT / "frontend" / "src" / "pages" / "PointCloud.tsx"


def _source() -> str:
    return POINT_CLOUD.read_text(encoding="utf-8")


def test_saved_cloud_must_be_deleted_with_a_reason_before_replacement():
    source = _source()
    assert "window.prompt('Why should this saved, unprocessed point-cloud source" in source
    assert "Sources referenced by durable run evidence cannot be deleted here" in source
    assert "method: 'DELETE'" in source
    assert "reason=${encodeURIComponent(reason)}" in source
    assert "Delete Saved Cloud" in source
    assert "Re-save" not in source


def test_save_action_is_hidden_while_a_saved_cloud_exists():
    source = _source()
    assert "hasBridge && stats && !cloudInfo?.exists" in source
    assert "point_cloud_delete_required_before_replace" not in source


def test_structured_upload_and_delete_errors_render_without_object_coercion():
    source = _source()
    assert source.count("typeof detail === 'string'") >= 2
    assert source.count("detail?.message || detail?.code") >= 2


def test_point_cloud_results_are_visibly_labeled_as_research_screening():
    source = _source()
    assert "Research screening only" in source
    assert "not survey measurements, accepted SNBI values" in source
    assert "SCREENING VERTICAL GAP" in source
    assert "These are screening candidates, not accepted BH12/BH13 values." in source
    assert "AI-ASSISTED" in source
    assert "AI-REVIEWED" not in source


def test_water_surface_proxy_is_not_presented_as_navigation_clearance():
    source = _source()
    assert "surface-gap proxy, not BN02" in source
    assert "water_surface_proxy: 'water-surface proxy'" in source
    assert "navigation clearance" not in source.lower()


def test_point_cloud_source_review_is_explicit_and_non_e57_is_view_only():
    source = _source()
    assert 'PanelSection title="Source Coordinates"' in source
    assert "Horizontal units" in source
    assert "Vertical units" in source
    assert "Coordinate frame" in source
    assert "Verified up axis" in source
    assert "Evidence reference" in source
    assert "I reviewed the units, frame, up direction, and cited evidence." in source
    assert "Only an original E57 file can be attached as source evidence" in source
    assert "other formats view-only" in source
    assert "declaration_reviewed" in source
    assert "Selected scan indices" in source
    assert "Registration RMS (metres)" in source
    assert "Maximum residual (metres)" in source
    assert "I reviewed the selected merge set and registration residuals." in source
    assert "Navigation clr." not in source


def test_saved_bridge_analysis_uses_durable_review_workflow_and_bounded_context():
    source = _source()
    assert "Start Durable Analysis" in source
    assert "/point-cloud/runs`" in source
    assert "/point-cloud/runs/${run.run_id}/evidence" in source
    assert "/point-cloud/runs/${durableRun.run_id}/review" in source
    assert "'If-Match': durableRun.etag" in source
    assert "Cancel This Run" in source
    assert "Accept Reviewed Result" in source
    assert "Reject Candidate" in source
    assert "Context Used" in source
    assert "Trusted SNBI fields" in source
    assert "Cited report evidence" in source
    assert "The full prompt and unrelated database records are never exposed." in source
    assert "Feature-bound clearance candidates" in source
    assert "not accepted BH12/BH13 values" in source
    assert "POINT_CLOUD_COMPONENT_CLASSES" in source
    assert "Download Candidate PLY" in source
    assert "/point-cloud/runs/published" in source
    assert "/point-cloud/processed" not in source
    assert "/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/segment" not in source
    assert "/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/clearance" not in source
