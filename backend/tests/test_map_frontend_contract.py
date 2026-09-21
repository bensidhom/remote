from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_popup_database_values_are_never_interpolated_as_html():
    arcgis = _text("frontend/src/components/ArcgisMap.tsx")
    assert ".innerHTML" not in arcgis
    assert ".textContent" in arcgis
    assert "document.createTextNode" in arcgis
    assert "title: 'Bridge details'" in arcgis


def test_both_bridgette_surfaces_use_the_shared_server_resolver():
    full = _text("frontend/src/pages/Bridgette.tsx")
    sidebar = _text("frontend/src/components/SidebarBridgette.tsx")
    shared = _text("frontend/src/lib/mapBridgeEvents.ts")
    for surface in (full, sidebar):
        assert "requestShowBridgeOnMap(question" in surface
        assert "signal: controller.signal" in surface
        assert "mapMatches" in surface
    assert "fetch('/api/map/resolve-bridge'" in shared
    assert "SERVER_WORKSPACE" not in shared
    assert "revision.workspace_id" in shared
    assert "workspace_id" not in shared.split("const body:", 1)[1].split("const response", 1)[0]
    assert "state_code" not in shared.split("const body:", 1)[1].split("const response", 1)[0]


def test_map_detector_requires_direct_navigation_not_analytical_show_language():
    shared = _text("frontend/src/lib/mapBridgeEvents.ts")

    assert "const directLookup" in shared
    assert "if (!directLookup) return false" in shared
    assert "const analytical" in shared
    assert "one\\s+row\\s+per" in shared


def test_map_resolution_cache_and_navigation_are_revision_and_request_scoped():
    shared = _text("frontend/src/lib/mapBridgeEvents.ts")
    map_page = _text("frontend/src/pages/Map.tsx")
    for token in (
        "revision.schema_generation",
        "revision.data_generation",
        "resolutionCache.clear()",
        "MAP_DATA_REVISION_EVENT",
        "controller.abort",
        "result?.requestId === requestId",
    ):
        assert token in shared
    assert "searchableBridgesRef" not in map_page
    assert "activeNavigationRef.current !== focus.requestId" in map_page
    assert "revisionEpoch" in map_page


def test_map_has_keyboard_focus_and_accessible_card_fallback():
    arcgis = _text("frontend/src/components/ArcgisMap.tsx")
    map_page = _text("frontend/src/pages/Map.tsx")
    assert "popup.focus({ preventScroll: true })" in arcgis
    assert 'role="status"' in map_page
    assert 'aria-live="polite"' in map_page
    assert "fallbackCardRef.current?.focus" in map_page
