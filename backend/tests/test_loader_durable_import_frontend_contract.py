from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _loader() -> str:
    return (ROOT / "frontend/src/pages/Loader.tsx").read_text(encoding="utf-8")


def test_loader_uses_owner_scoped_durable_two_phase_import_contract():
    source = _loader()

    assert "/api/revisions/stage-upload" not in source
    assert "axios.post<ImportJobResponse>(\n        '/api/imports'" in source
    assert "`/api/imports/${encodeURIComponent(importId)}`" in source
    assert "params: { include_bridges: true }" in source
    assert "`/api/imports/${encodeURIComponent(job.import_id)}/commit`" in source

    for field in (
        "inventory_year",
        "state_code",
        "agency_id",
        "profile_id",
    ):
        assert f"formData.append('{field}'" in source
    for commit_field in (
        "preflight_digest",
        "expected_data_generation",
        "action",
        "validation_policy",
        "atomic_mode: 'batch'",
        "override_reason",
    ):
        assert commit_field in source

    assert "'Idempotency-Key': idempotencyKey" in source
    assert "'X-Request-ID': newOperationId()" in source
    assert "expected_data_generation: preflight.data_generation" in source
    assert "Commit to staging only" in source
    assert "It does not publish or activate bridge data." in source


def test_loader_polls_cancellably_resumes_and_invalidates_stale_preflight():
    source = _loader()

    assert "new AbortController()" in source
    assert "pollControllerRef.current?.abort()" in source
    assert "waitForNextPoll(controller.signal)" in source
    assert "job.status === 'failed' || job.status === 'cancelled'" in source
    assert "bridgeguard_active_import_id" in source
    assert "localStorage.getItem('bridgeguard_active_import_id')" in source
    assert "job.preflight.data_generation !== generation" in source
    assert "details.code === 'stale_data_generation'" in source
    assert "details.code === 'stale_snapshot_preflight'" in source
    assert "refreshDataGeneration()" in source


def test_loader_renders_authoritative_inventory_evidence_before_commit():
    source = _loader()

    for label in (
        "Server-authoritative preflight",
        "Field-loss check:",
        "Per-bridge review",
        "action:",
        "eligibility:",
        "Validation issues",
        "Field diff",
        "Embedded manifest conflicts",
    ):
        assert label in source
    assert "importJob.preflight.valid" in source
    assert "preflightStale" in source
    assert "The untouched file goes to the" in source
    assert "formData.append('file', jsonFile, jsonFile.name)" in source

