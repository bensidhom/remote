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
    assert "hasBridge && stats && !(cloudInfo?.raw_exists" in source
    assert "point_cloud_delete_required_before_replace" not in source


def test_structured_upload_and_delete_errors_render_without_object_coercion():
    source = _source()
    assert source.count("typeof detail === 'string'") >= 2
    assert source.count("detail?.message || detail?.code") >= 2


def test_point_cloud_results_use_contextual_labels_without_global_warning_banner():
    source = _source()
    assert "Research screening only" not in source
    assert "Local visualization only." not in source
    assert 'label="B.H.12"' in source
    assert "'B.H.13'" in source
    assert "How section limits were set" not in source
    assert "source units" not in source
    assert "analysisInfo.warnings.map" not in source
    assert "SNBI context ·" not in source
    assert "AI-ASSISTED" not in source
    assert "AI-REVIEWED" not in source


def test_water_surface_proxy_is_not_presented_as_navigation_clearance():
    source = _source()
    assert "BN02" not in source
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
    assert "Other formats remain local visualization only" in source
    assert "declaration_reviewed" in source
    assert "Selected scan indices" in source
    assert "Registration RMS (metres)" in source
    assert "Maximum residual (metres)" in source
    assert "I reviewed the selected merge set and registration residuals." in source
    assert "Navigation clr." not in source


def test_saved_bridge_analysis_uses_automatic_durable_workflow_without_review_ui():
    source = _source()
    assert "Analyze Point Cloud" in source
    assert "useDurableWorkflow" in source
    assert "Analyzing the loaded point cloud" in source
    assert "A saved E57 source with a reviewed metric-ready source receipt is required." not in source
    assert "/point-cloud/runs`" in source
    assert "/point-cloud/runs/${run.run_id}/evidence" in source
    assert "/point-cloud/runs/${durableRun.run_id}/review" not in source
    assert "'If-Match': durableRun.etag" not in source
    assert "Start Durable Analysis" not in source
    assert "Discard Candidate" not in source
    assert "Save Processed Result" not in source
    assert "Saved context" not in source
    assert "Trusted SNBI fields" not in source
    assert "Cited report evidence" not in source
    assert "groupedMeasurementCandidates" not in source
    assert "POINT_CLOUD_COMPONENT_CLASSES" not in source
    assert "Download Candidate PLY" not in source
    assert "/point-cloud/runs/published" not in source
    assert "/point-cloud/saved-result" in source
    assert "/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/segment" not in source
    assert "/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/clearance" not in source


def test_ad_hoc_bridge_analysis_passes_loaded_snbi_context_and_retains_source_points():
    source = _source()
    assert "bridge_context_state_code: hasBridge ? bridgeStateCode : undefined" in source
    assert "bridge_context_bridge_number: hasBridge ? bridgeNumber : undefined" in source
    assert "Outside analysis / retained" in source
    assert "pos[i * 3] = NaN" not in source


def test_vertical_height_shows_raw_geometry_candidate_when_snbi_binding_is_pending():
    source = _source()
    assert "geometryClearanceCandidate" in source
    assert "raw_geometry_candidate" in source
    assert "label={Number.isFinite(measuredBh13) ? 'B.H.13' : 'Clearance'}" in source
    assert "measurement_planes" in source


def test_analysis_starts_without_fitted_geometry_and_hides_warning_clutter():
    source = _source()
    assert "useState(false);\n  const [showMesh, setShowMesh] = useState(false)" in source
    assert "setShowVolumes(false);" in source
    assert "setShowMesh(false);" in source
    assert "analysisInfo.warnings.map" not in source
    assert "Screening gap" not in source
    assert "                  Volumes\n" in source
    assert "                  Mesh\n" in source


def test_clearance_view_selects_retained_multi_plane_result_and_compares_snbi_items():
    source = _source()
    assert "roadway_candidates" in source
    assert "profile_samples" in source
    assert "distanceLabel" not in source
    assert "measurement_planes" in source
    assert "retainedClearancePlanes" in source
    assert 'aria-label="Visible clearance plane"' in source
    assert 'aria-label="Clearance rectangles"' in source
    assert "SNBI B.H.12" in source
    assert "SNBI B.H.13" in source
    assert "Δ BH13" not in source
    assert "dualClearance" in source


def test_raw_and_processed_clouds_have_explicit_save_and_load_actions():
    source = _source()
    assert "Load Raw" in source
    assert "Load Processed" in source
    assert "Save E57 Source" in source
    assert "Save Processed Result" not in source
    assert "Analysis complete ·" in source
    assert "setDurableEvidence(bundle.evidence)" in source
    assert "setAnalysisInfo(bundle.analysis)" in source
    assert "combineCloudResults" in source
    assert "Raw and processed point clouds shown together" in source
    assert "await loadProcessedFromBridge()" in source
    assert "save_result: hasBridge" in source
    assert "loadedBridgeSourceShaRef.current === cloudInfo.source_sha256" in source
    assert "legacy_processed_exists: true" in source


def test_duplicate_component_labels_are_grouped_in_compact_sidebar():
    source = _source()
    assert "compactComponentLabel" in source
    assert "groupAnalysisComponents" not in source
    assert "COMPONENT GROUPS" not in source
    assert "current.indices.push(index)" in source
    assert "onToggle(group.indices)" in source
