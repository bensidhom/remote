from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_one_app_level_observer_is_bounded_visibility_aware_and_workspace_scoped():
    observer = _text("frontend/src/lib/dataGeneration.ts")
    app = _text("frontend/src/App.tsx")
    frontend_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "frontend/src").rglob("*.ts*")
    )

    assert frontend_sources.count("startDataGenerationObserver({") == 1
    assert "expectedWorkspaceId: user.workspace_id" in app
    assert "fetch('/api/revisions/state'" in observer
    assert "credentials: 'same-origin'" in observer
    assert "cache: 'no-store'" in observer
    assert "document.visibilityState === 'hidden'" in observer
    assert "visibilitychange" in observer
    assert "controller?.abort()" in observer
    assert "DEFAULT_MAX_BACKOFF_MS" in observer
    assert "Math.min(" in observer
    assert "state.workspace_id !== expectedWorkspaceId" in observer
    assert "Number.isSafeInteger(state.data_generation)" in observer
    assert "Number.isSafeInteger(state.schema_generation)" in observer
    assert "console." not in observer


def test_generation_signal_invalidates_map_resolver_and_refreshes_inventory_surfaces():
    shared_map = _text("frontend/src/lib/mapBridgeEvents.ts")
    kpis = _text("frontend/src/pages/Kpis.tsx")
    loader = _text("frontend/src/pages/Loader.tsx")
    explorer = _text("frontend/src/pages/Explorer.tsx")
    bridgette = _text("frontend/src/pages/Bridgette.tsx")
    sidebar = _text("frontend/src/components/SidebarBridgette.tsx")

    assert "DATA_GENERATION_CHANGE_EVENT" in shared_map
    assert "resolutionCache.clear()" in shared_map
    assert "MAP_DATA_REVISION_EVENT" in shared_map

    assert "useDataGenerationEpoch()" in kpis
    assert "[year, dataGenerationEpoch]" in kpis
    assert "[dataGenerationEpoch]" in kpis
    assert "useDataGenerationEpoch()" in loader
    assert "refreshDataGeneration()" in loader
    assert "useDataGenerationEpoch()" in explorer
    assert "hasLoadedDataRef.current" in explorer
    assert "setSearchTerm('')" in explorer

    for surface in (bridgette, sidebar):
        assert "useDataGenerationEpoch()" in surface
        assert "stale: true" in surface
        assert "Outdated inventory answer" in surface
        assert "abortRef.current?.abort()" in surface

