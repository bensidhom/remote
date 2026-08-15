from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_bridge_ask_uses_exact_request_ids_and_drops_stale_events():
    source = _text("frontend/src/components/BridgeDetailModal.tsx")
    assert "crypto.randomUUID()" in source
    assert "'X-Request-ID': requestId" in source
    assert "evt.request_id !== requestId" in source
    assert "/api/ai/requests/${encodeURIComponent(requestId)}/cancel" in source
    assert "/api/ask-cancel" not in source


def test_visual_control_is_explicit_one_request_and_discloses_privacy_cap():
    source = _text("frontend/src/components/BridgeDetailModal.tsx")
    assert "fd.append('analysis_mode', 'visual')" in source
    assert "fd.append('analysis_mode', 'text')" in source
    assert "setUseVisual(false)" in source
    assert "raw images may be sent to the configured model" in source
    assert "estimated cap $0.25" in source


def test_private_browser_storage_is_user_workspace_namespaced_and_cleared():
    access = _text("frontend/src/lib/access.ts")
    vision = _text("frontend/src/pages/Vision.tsx")
    app = _text("frontend/src/App.tsx")
    assert "bg:private:v1:" in access
    assert "workspace_id: string" in access
    assert "clearPrivateBrowserState(userEmail, workspaceId)" in access
    assert "privateStorageNamespace(ownerNamespace, workspaceNamespace)" in vision
    assert "signOut(user.email, user.workspace_id)" in app


def test_normal_document_ask_is_text_only_and_has_exact_cancellation():
    source = _text("frontend/src/pages/Vision.tsx")
    assert "formData.append('analysis_mode', 'text')" in source
    assert "Force defect overlays" not in source
    assert "forceDefectSegmentation" not in source
    assert "'/api/vision-rag'" in source
    assert "'/api/vision-query'" in source
    assert "'X-Request-ID': requestId" in source
    assert "/api/ai/requests/${encodeURIComponent(requestId)}/cancel" in source
    assert "/api/vision-cancel" not in source
    assert "/api/vision-progress" not in source
    assert 'accept=".pdf,application/pdf"' in source
    assert "DOCUMENT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024" in source
    assert "Document Ask accepts PDF files only." in source


def test_report_index_status_is_bridge_scoped_not_process_global():
    source = _text("frontend/src/components/BridgeReportsModal.tsx")
    assert "/reports-status`" in source
    assert "/api/vision-progress" not in source
