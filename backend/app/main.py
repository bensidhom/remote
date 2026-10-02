# backend/app/main.py

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from starlette.concurrency import run_in_threadpool
import asyncio
import uvicorn
import hashlib
import os
import socket
import shutil
import sqlite3
from pathlib import Path
import json
import math
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional
from uuid import uuid4

try:
    from .auth import AuthMiddleware, router as auth_router
except ImportError:
    from auth import AuthMiddleware, router as auth_router

try:
    from .services.http_limits import RequestBodyLimitMiddleware
except ImportError:
    from services.http_limits import RequestBodyLimitMiddleware


def _load_dotenv_early() -> None:
    """Load backend/.env (KEY=VALUE lines) into the environment BEFORE the service
    imports below run (importing them triggers datapaths.py). Real environment
    variables always win (setdefault), which is how AWS/EB config overrides .env.
    datapaths.py additionally loads a repo-root .env; either location works.
    Dependency-free on purpose (no python-dotenv needed)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"  # backend/.env
    try:
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv_early()

MAX_HISTORY_CONVERSION_BYTES = (
    int(os.getenv("BRIDGEGUARD_MAX_CONVERSION_UPLOAD_MB", "100")) * 1024 * 1024
)

# Revision persistence imports the environment-backed database paths.  Keep it
# behind the early dotenv load just like the other persistence services below.
try:
    from .revision_api import router as revision_router
except ImportError:
    from revision_api import router as revision_router

try:
    from .imports_api import (
        IMPORT_PREFLIGHT_OPERATION,
        ImportApiError,
        MAX_IMPORT_BYTES,
        process_import_preflight,
        router as imports_router,
    )
except ImportError:
    from imports_api import (
        IMPORT_PREFLIGHT_OPERATION,
        ImportApiError,
        MAX_IMPORT_BYTES,
        process_import_preflight,
        router as imports_router,
    )

try:
    from .forecast_api import (
        FORECAST_TRAIN_OPERATION,
        ForecastApiError,
        process_forecast_training_job,
        router as forecast_v2_router,
    )
except ImportError:
    from forecast_api import (
        FORECAST_TRAIN_OPERATION,
        ForecastApiError,
        process_forecast_training_job,
        router as forecast_v2_router,
    )

try:
    from .lcca_api import router as lcca_v2_router
except ImportError:
    from lcca_api import router as lcca_v2_router

try:
    from .kpi_api import MAX_PROGRAM_KPI_BYTES, router as kpi_v2_router
except ImportError:
    from kpi_api import MAX_PROGRAM_KPI_BYTES, router as kpi_v2_router

try:
    from .services.operations.deployment import (
        DeploymentMode,
        evaluate_deployment_policy,
        require_deployment_policy,
    )
    from .services.operations.deployment_database import (
        verify_dedicated_database_binding,
    )
    from .services.operations.jobs import (
        JobStoreError,
        claim_next_job,
        complete_job,
        enqueue_job,
        fail_job,
        get_job,
        initialize_job_store,
        request_job_cancellation,
        update_job_progress,
    )
    from .services.operations.quotas import QuotaExceeded
    from .services.operations.http_quotas import OperationQuotaMiddleware
    from .services.operations.legacy_api_guard import LegacyApiGuardMiddleware
    from .services.operations.retention import (
        RetentionError,
        initialize_retention_store,
        process_attachment_deletion_outbox,
        request_attachment_deletion,
        run_retention_sweep,
        set_retention_policy,
    )
    from .services.operations.storage_paths import (
        StoragePathError,
        portable_confined_path,
        resolve_confined_path,
    )
except ImportError:
    from services.operations.deployment import (
        DeploymentMode,
        evaluate_deployment_policy,
        require_deployment_policy,
    )
    from services.operations.deployment_database import (
        verify_dedicated_database_binding,
    )
    from services.operations.jobs import (
        JobStoreError,
        claim_next_job,
        complete_job,
        enqueue_job,
        fail_job,
        get_job,
        initialize_job_store,
        request_job_cancellation,
        update_job_progress,
    )
    from services.operations.quotas import QuotaExceeded
    from services.operations.http_quotas import OperationQuotaMiddleware
    from services.operations.legacy_api_guard import LegacyApiGuardMiddleware
    from services.operations.retention import (
        RetentionError,
        initialize_retention_store,
        process_attachment_deletion_outbox,
        request_attachment_deletion,
        run_retention_sweep,
        set_retention_policy,
    )
    from services.operations.storage_paths import (
        StoragePathError,
        portable_confined_path,
        resolve_confined_path,
    )

try:
    from .map_api import router as map_router
except ImportError:
    from map_api import router as map_router

try:
    from .site_intelligence_api import router as site_intelligence_router
except ImportError:
    from site_intelligence_api import router as site_intelligence_router

# Point-cloud storage/size settings are read while its router imports, so load
# backend/.env first just like the other environment-backed services below.
try:
    from .routers.point_cloud_api import (
        point_cloud_health as point_cloud_readiness,
        router as point_cloud_router,
    )
except ImportError:
    from routers.point_cloud_api import (
        point_cloud_health as point_cloud_readiness,
        router as point_cloud_router,
    )

# Import services
try:
    from .routers.services.utils import resource_path, init_db_if_needed
    from .routers.services.parser import parse_nbi_txt, parse_elements_xml, extract_inspection_reports_to_schema
    from .routers.services.mapper import map_bridge_to_snbi
    from .routers.services.reverse_mapper import convert_snbi_to_outputs
    from .routers.services.snbi_schema import (
        normalize_snbi_payload,
        summarize_payload_coverage,
        summarize_payload_coverage_by_section,
    )
    from .routers.services.snbi_validation import validate_snbi_payload
    from .routers.services.forecasting import (
        rebuild_training_db,
        train_prophet_forecasts,
        train_condition_forecasts,
        get_forecast_summary,
        get_forecast_bridges,
        get_bridge_forecast,
        ensure_training_data,
        simulate_bridge_forecast,
    )
    from .routers.services.lcca import compute_bridge_lcca
    from .routers.services.constants import STATE_MAP
    from .routers.services.sqlite_runtime import (
        inspect_sqlite_wal_runtime,
        require_safe_sqlite_wal_runtime,
    )
    from .routers.services.db_schema import LATEST_SCHEMA_VERSION
    from .services.conversion.element_xml import parse_element_xml
    from .services.conversion.nbi_parser import (
        CsvDialect as StrictCsvDialect,
        IssuePolicy as StrictIssuePolicy,
        NBIFormat as StrictNBIFormat,
        NBIParseError,
        parse_nbi as parse_nbi_strict,
    )
    from .services.conversion.pipeline import (
        StrictConversionError,
        StrictConversionOptions,
        convert_strict_parse_results,
    )
    from .services.conversion.inspection_history import (
        InspectionHistoryError,
        ParsedAnnualSource,
        parse_inspection_history_manifest,
        prepare_inspection_history,
    )
    from .services.conversion.source_artifacts import (
        ArtifactErrorCode,
        SourceArtifactError,
        SourceArtifactStore,
    )
    from .routers.services.database import (
        init_db,
        load_json_to_db,
        get_bridges_for_map,
        get_map_counties,
        get_state_kpis,
        get_county_kpis,
        delete_bridges_by_year,
        get_state_condition_history,
        get_bridge_details as get_bridge_details_db,
        get_bridge_field_metadata,
        get_all_tables,
        get_table_data,
        get_table_schema,
        get_bridge_history as get_bridge_history_db,
        update_bridge_main,
        insert_table_row,
        update_table_row,
        delete_table_row,
        clear_all_data,
        export_all_bridges_snbi,
        get_available_years,
        get_loaded_years,
        execute_readonly_sql,
        save_inspection_report_extractions,
        get_inspection_report_extractions,
        validate_inspection_report_records,
        apply_validated_inspection_report_records,
        delete_single_bridge,
        create_custom_table, list_custom_tables_for_bridge, add_custom_row,
        update_custom_row, delete_custom_row, delete_custom_table,
    )
    from .routers.services.vision import (
        process_vision_rag,
        process_vision_query_only,
        process_db_rag,
        process_db_rag_with_files,
        process_bridge_ask,
        process_bridge_ask_stream,
        process_bridge_visual_investigation,
        bridgette_answer,
        bridgette_answer_stream,
        begin_chat,
        end_chat,
        cancel_chat,
        ChatCancelled,
        DocumentUploadError,
        DOCUMENT_MAX_UPLOAD_BYTES,
    )
    from .routers.services.rag_engine import (
        RAG_WORKING_DIR,
        reset_rag,
        get_last_doc_hash,
        get_document_page_count,
        get_extracted_image_count,
        CancelledError,
    )
except ImportError:
    # Support running directly as a script with `python main.py`
    from routers.services.utils import resource_path, init_db_if_needed
    from routers.services.parser import parse_nbi_txt, parse_elements_xml, extract_inspection_reports_to_schema
    from routers.services.mapper import map_bridge_to_snbi
    from routers.services.reverse_mapper import convert_snbi_to_outputs
    from routers.services.snbi_schema import (
        normalize_snbi_payload,
        summarize_payload_coverage,
        summarize_payload_coverage_by_section,
    )
    from routers.services.snbi_validation import validate_snbi_payload
    from routers.services.forecasting import (
        rebuild_training_db,
        train_prophet_forecasts,
        train_condition_forecasts,
        get_forecast_summary,
        get_forecast_bridges,
        get_bridge_forecast,
        ensure_training_data,
        simulate_bridge_forecast,
    )
    from routers.services.lcca import compute_bridge_lcca
    from routers.services.constants import STATE_MAP
    from routers.services.sqlite_runtime import (
        inspect_sqlite_wal_runtime,
        require_safe_sqlite_wal_runtime,
    )
    from routers.services.db_schema import LATEST_SCHEMA_VERSION
    from services.conversion.element_xml import parse_element_xml
    from services.conversion.nbi_parser import (
        CsvDialect as StrictCsvDialect,
        IssuePolicy as StrictIssuePolicy,
        NBIFormat as StrictNBIFormat,
        NBIParseError,
        parse_nbi as parse_nbi_strict,
    )
    from services.conversion.pipeline import (
        StrictConversionError,
        StrictConversionOptions,
        convert_strict_parse_results,
    )
    from services.conversion.inspection_history import (
        InspectionHistoryError,
        ParsedAnnualSource,
        parse_inspection_history_manifest,
        prepare_inspection_history,
    )
    from services.conversion.source_artifacts import (
        ArtifactErrorCode,
        SourceArtifactError,
        SourceArtifactStore,
    )
    from routers.services.database import (
        init_db,
        load_json_to_db,
        get_bridges_for_map,
        get_map_counties,
        get_state_kpis,
        get_county_kpis,
        delete_bridges_by_year,
        get_state_condition_history,
        get_bridge_details as get_bridge_details_db,
        get_bridge_field_metadata,
        get_all_tables,
        get_table_data,
        get_table_schema,
        get_bridge_history as get_bridge_history_db,
        update_bridge_main,
        insert_table_row,
        update_table_row,
        delete_table_row,
        clear_all_data,
        export_all_bridges_snbi,
        get_available_years,
        get_loaded_years,
        execute_readonly_sql,
        save_inspection_report_extractions,
        get_inspection_report_extractions,
        validate_inspection_report_records,
        apply_validated_inspection_report_records,
        delete_single_bridge,
        create_custom_table, list_custom_tables_for_bridge, add_custom_row,
        update_custom_row, delete_custom_row, delete_custom_table,
    )
    from routers.services.vision import (
        process_vision_rag,
        process_vision_query_only,
        process_db_rag,
        process_db_rag_with_files,
        process_bridge_ask,
        process_bridge_ask_stream,
        process_bridge_visual_investigation,
        bridgette_answer,
        bridgette_answer_stream,
        begin_chat,
        end_chat,
        cancel_chat,
        ChatCancelled,
        DocumentUploadError,
        DOCUMENT_MAX_UPLOAD_BYTES,
    )
    from routers.services.rag_engine import (
        RAG_WORKING_DIR,
        reset_rag,
        get_last_doc_hash,
        get_document_page_count,
        get_extracted_image_count,
        CancelledError,
    )

try:
    from .visual_jobs import (
        VISUAL_JOB_OPERATION,
        VisualJobCancelled,
        VisualJobError,
        enqueue_visual_investigation,
        get_owned_visual_job,
        load_visual_job_result,
        process_visual_investigation_job,
        public_visual_job,
    )
except ImportError:
    from visual_jobs import (
        VISUAL_JOB_OPERATION,
        VisualJobCancelled,
        VisualJobError,
        enqueue_visual_investigation,
        get_owned_visual_job,
        load_visual_job_result,
        process_visual_investigation_job,
        public_visual_job,
    )

try:
    from .point_cloud_jobs import (
        POINT_CLOUD_JOB_OPERATION,
        PointCloudJobCancelled,
        PointCloudJobError,
        mark_point_cloud_job_failed,
        process_point_cloud_analysis_job,
    )
except ImportError:
    from point_cloud_jobs import (
        POINT_CLOUD_JOB_OPERATION,
        PointCloudJobCancelled,
        PointCloudJobError,
        mark_point_cloud_job_failed,
        process_point_cloud_analysis_job,
    )


def is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def select_backend_host_port() -> tuple[str, int]:
    # Loopback by default for local development. Production gunicorn binds the
    # container interface explicitly and relies on server-enforced auth + TLS.
    host = os.getenv('HOST', '127.0.0.1')
    if os.getenv('PORT'):
        port = int(os.getenv('PORT'))
        if is_port_free(host, port):
            return host, port
        raise RuntimeError(f'Port {port} is already in use on {host}')

    candidates = [5000, 5001, 5002, 8000, 8001, 8002]
    for candidate in candidates:
        if is_port_free(host, candidate):
            return host, candidate
    raise RuntimeError('No available backend port found; all candidate ports are in use.')

from contextlib import asynccontextmanager


_REPORT_JOB_OPERATION = "report_index"
_REPORT_JOB_STOP = None
_REPORT_JOB_THREAD = None
_REPORT_JOB_HEALTH: Dict[str, object] = {
    "started": False,
    "worker_alive": False,
    "last_error_code": None,
    "last_claim_at": None,
    "retention_error_code": None,
}


def _report_job_db() -> Path:
    return Path(USER_DB).parent / "operation_jobs.db"


def _visual_result_artifact_root() -> Path:
    return Path(USER_DB).parent / "conversion_sources"


def _queue_report_index(
    state_code: str,
    bridge_number: str,
    *,
    owner_id: str,
    idempotency_key: str,
):
    """Persist one restart-safe report-index request; never spawn per-job threads."""

    return enqueue_job(
        _report_job_db(),
        workspace_id=SHARED_WORKSPACE_ID,
        owner_id=owner_id,
        operation=_REPORT_JOB_OPERATION,
        idempotency_key=idempotency_key,
        payload={
            "state_code": str(state_code),
            "bridge_number": str(bridge_number),
        },
        max_attempts=3,
    )


def _start_report_job_worker():
    """Start the one supported worker over the durable report-index queue."""

    global _REPORT_JOB_STOP, _REPORT_JOB_THREAD
    import threading

    initialize_job_store(_report_job_db())
    stop = threading.Event()
    worker_id = f"report-worker-{os.getpid()}"

    def work() -> None:
        try:
            from .routers.services.vision import ingest_bridge_reports
        except ImportError:
            from routers.services.vision import ingest_bridge_reports
        _REPORT_JOB_HEALTH.update({
            "started": True,
            "worker_alive": True,
            "last_error_code": None,
        })
        next_reconcile = 0.0
        next_retention_sweep = 0.0
        while not stop.wait(0.25):
            now = time.monotonic()
            if now >= next_reconcile:
                try:
                    _enqueue_stale_report_indexes()
                    next_reconcile = now + 30.0
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = (
                        "report_reconcile_failed"
                    )
                    next_reconcile = now + 5.0
            if now >= next_retention_sweep:
                try:
                    _REPORT_JOB_HEALTH["retention"] = _run_retention_maintenance()
                    _REPORT_JOB_HEALTH["retention_error_code"] = None
                    next_retention_sweep = now + 60.0
                except Exception:
                    _REPORT_JOB_HEALTH["retention_error_code"] = "retention_sweep_failed"
                    next_retention_sweep = now + 10.0
            try:
                job = claim_next_job(
                    _report_job_db(),
                    worker_id=worker_id,
                    # Interactive imports are checked first so a startup
                    # report-index backlog cannot starve user-visible
                    # validation work in the supported one-worker process.
                    allowed_operations=(IMPORT_PREFLIGHT_OPERATION,),
                    lease_seconds=900,
                )
                if job is None:
                    job = claim_next_job(
                        _report_job_db(),
                        worker_id=worker_id,
                        allowed_operations=(POINT_CLOUD_JOB_OPERATION,),
                        lease_seconds=1800,
                    )
                if job is None:
                    job = claim_next_job(
                        _report_job_db(),
                        worker_id=worker_id,
                        allowed_operations=(FORECAST_TRAIN_OPERATION,),
                        lease_seconds=1800,
                    )
                if job is None:
                    job = claim_next_job(
                        _report_job_db(),
                        worker_id=worker_id,
                        allowed_operations=(VISUAL_JOB_OPERATION,),
                        lease_seconds=1800,
                    )
                if job is None:
                    job = claim_next_job(
                        _report_job_db(),
                        worker_id=worker_id,
                        allowed_operations=(_REPORT_JOB_OPERATION,),
                        lease_seconds=900,
                    )
                _REPORT_JOB_HEALTH["last_claim_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                _REPORT_JOB_HEALTH["last_error_code"] = None
            except Exception:
                # A transient coordination-store error must not kill the only
                # durable worker. Readiness becomes false until a later claim
                # succeeds, while the daemon retries at a bounded cadence.
                _REPORT_JOB_HEALTH["last_error_code"] = "job_claim_failed"
                stop.wait(1.0)
                continue
            if job is None:
                continue
            try:
                if job.operation == IMPORT_PREFLIGHT_OPERATION:
                    job = update_job_progress(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        progress={"phase": "parse_validate", "completed": 0, "total": 1},
                    )
                    public_result = process_import_preflight(job)
                    job = update_job_progress(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        progress={"phase": "complete", "completed": 1, "total": 1},
                    )
                elif job.operation == POINT_CLOUD_JOB_OPERATION:
                    job = update_job_progress(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        progress={
                            "phase": "verify_source_and_context",
                            "completed": 0,
                            "total": 8,
                        },
                    )
                    public_result = process_point_cloud_analysis_job(
                        job=job,
                        database_path=USER_DB,
                        job_database_path=_report_job_db(),
                    )
                elif job.operation == FORECAST_TRAIN_OPERATION:
                    job = update_job_progress(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        progress={
                            "phase": "project_fit_evaluate",
                            "completed": 0,
                            "total": 1,
                        },
                    )
                    public_result = process_forecast_training_job(
                        job=job,
                        database_path=USER_DB,
                        job_database_path=_report_job_db(),
                    )
                    job = update_job_progress(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        progress={"phase": "complete", "completed": 1, "total": 1},
                    )
                elif job.operation == VISUAL_JOB_OPERATION:
                    job = update_job_progress(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        progress={
                            "phase": "verify_evidence",
                            "completed": 0,
                            "total": 3,
                        },
                    )
                    public_result = asyncio.run(
                        process_visual_investigation_job(
                            job=job,
                            database_path=USER_DB,
                            job_database_path=_report_job_db(),
                            artifact_root=_visual_result_artifact_root(),
                            progress_callback=lambda phase, completed, total: (
                                update_job_progress(
                                    _report_job_db(),
                                    job=job,
                                    worker_id=worker_id,
                                    progress={
                                        "phase": phase,
                                        "completed": completed,
                                        "total": total,
                                        "provider_interruption": (
                                            "application_boundaries_only"
                                            if phase == "visual_analysis"
                                            else None
                                        ),
                                    },
                                )
                            ),
                        )
                    )
                else:
                    result = ingest_bridge_reports(
                        str(job.payload["state_code"]),
                        str(job.payload["bridge_number"]),
                        report_progress=False,
                        workspace_id=job.workspace_id,
                        analysis_mode="text",
                    )
                    public_result = {
                        "indexed": True,
                        "chunk_count": len(result.get("chunks") or []),
                    }
                complete_job(
                    _report_job_db(),
                    job=job,
                    worker_id=worker_id,
                    result=public_result,
                )
            except PointCloudJobCancelled:
                try:
                    fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code="cancelled_by_user",
                        retry=False,
                    )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
            except PointCloudJobError as exc:
                try:
                    finished = fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code=exc.code,
                        retry=exc.retry,
                    )
                    if finished.status == "failed":
                        mark_point_cloud_job_failed(
                            database_path=USER_DB,
                            job=job,
                            error_code=exc.code,
                        )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
            except VisualJobCancelled:
                try:
                    fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code="cancelled_by_user",
                        retry=False,
                    )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
            except VisualJobError as exc:
                try:
                    fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code=exc.code,
                        retry=exc.retry,
                    )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
            except ImportApiError as exc:
                try:
                    fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code=exc.code,
                        retry=False,
                    )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
            except ForecastApiError as exc:
                try:
                    fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code=exc.code,
                        retry=False,
                    )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
            except Exception:
                # Public job state contains a bounded typed code, never provider,
                # parser, path, or report content.
                try:
                    error_code = (
                        "point_cloud_analysis_failed"
                        if job.operation == POINT_CLOUD_JOB_OPERATION
                        else (
                            "visual_investigation_failed"
                            if job.operation == VISUAL_JOB_OPERATION
                            else (
                                "forecast_training_failed"
                                if job.operation == FORECAST_TRAIN_OPERATION
                                else "report_index_failed"
                            )
                        )
                    )
                    finished = fail_job(
                        _report_job_db(),
                        job=job,
                        worker_id=worker_id,
                        error_code=error_code,
                        retry=True,
                    )
                    if job.operation == POINT_CLOUD_JOB_OPERATION and finished.status == "failed":
                        mark_point_cloud_job_failed(
                            database_path=USER_DB,
                            job=job,
                            error_code=error_code,
                        )
                except Exception:
                    _REPORT_JOB_HEALTH["last_error_code"] = "job_finish_failed"
        _REPORT_JOB_HEALTH["worker_alive"] = False

    thread = threading.Thread(
        target=work,
        name="durable-report-worker",
        daemon=True,
    )
    thread.start()
    _REPORT_JOB_STOP = stop
    _REPORT_JOB_THREAD = thread
    return stop, thread


def _enqueue_stale_report_indexes() -> None:
    """Queue report inventories without reading or hashing attachment bytes.

    Attachment SHA-256 receipts are calculated during upload.  Startup hashes
    only small database metadata, so lifespan is independent of report size.
    Per-bridge internal owners avoid the public per-user queue ceiling.  Queue
    capacity/errors are recorded and skipped rather than aborting startup.
    """

    if os.getenv("BRIDGEGUARD_REINDEX_STALE_REPORTS", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return
    with sqlite3.connect(
        f"file:{Path(USER_DB).resolve().as_posix()}?mode=ro", uri=True
    ) as connection:
        bridges = connection.execute(
            "SELECT state_code,bridge_number,COUNT(*),MAX(id),MAX(uploaded_at),"
            "GROUP_CONCAT(COALESCE(sha256,''),'|') "
            "FROM bridge_attachments "
            "WHERE attachment_type='inspection_report' "
            "AND (workspace_id=? OR workspace_id IS NULL) "
            "GROUP BY state_code,bridge_number ORDER BY state_code,bridge_number",
            (SHARED_WORKSPACE_ID,),
        ).fetchall()
    queued = 0
    skipped = 0
    for state_code, bridge_number, count, max_id, uploaded_at, digests in bridges:
        inventory = json.dumps(
            {
                "bridge": [str(state_code), str(bridge_number)],
                "count": int(count),
                "max_id": int(max_id),
                "latest": str(uploaded_at or ""),
                "digests": str(digests or ""),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        identity = hashlib.sha256(inventory.encode("utf-8")).hexdigest()
        internal_owner = (
            "system+"
            + hashlib.sha256(
                f"{state_code}:{bridge_number}".encode("utf-8")
            ).hexdigest()[:20]
            + "@bridgeguard"
        )
        try:
            _queue_report_index(
                str(state_code),
                str(bridge_number),
                owner_id=internal_owner,
                idempotency_key=(
                    f"startup:{state_code}:{bridge_number}:{identity}"
                ),
            )
            queued += 1
        except JobStoreError as exc:
            skipped += 1
            _REPORT_JOB_HEALTH["last_error_code"] = exc.code
            if exc.code in {
                "workspace_job_queue_full",
                "workspace_job_queue_reserved",
            }:
                break
        except Exception:
            skipped += 1
            _REPORT_JOB_HEALTH["last_error_code"] = "startup_enqueue_failed"
    _REPORT_JOB_HEALTH["startup_queued"] = queued
    _REPORT_JOB_HEALTH["startup_skipped"] = skipped


def _run_retention_maintenance() -> dict[str, int]:
    """Bounded scheduled cleanup, independent of request traffic."""

    try:
        job_days = int(os.getenv("BRIDGEGUARD_JOB_RETENTION_DAYS", "30"))
    except ValueError:
        job_days = 30
    return run_retention_sweep(
        USER_DB,
        job_database_path=_report_job_db(),
        workspace_id=SHARED_WORKSPACE_ID,
        data_root=Path(USER_DB).parent,
        rag_working_dir=RAG_WORKING_DIR,
        job_retention_days=max(1, min(job_days, 3650)),
    )


# Custom JSON encoder to handle NaN, Inf, etc.
class SafeEncoder(json.JSONEncoder):
    def encode(self, obj):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
        return super().encode(obj)
    
    def iterencode(self, obj, _one_shot=False):
        for chunk in super().iterencode(obj, _one_shot):
            yield chunk

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize the databases. Generation is hosted by Cerebras;
    # embeddings are served by the pinned Nomic TEI sidecar.
    # init_db enables WAL and can run schema writes, so reject an unsafe
    # SQLite WAL runtime before the first database mutation.
    deployment_policy = require_deployment_policy(
        data_directory=Path(USER_DB).parent,
        require_commercial=os.getenv(
            "BRIDGEGUARD_REQUIRE_COMMERCIAL_READY", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
    )
    if deployment_policy.mode is DeploymentMode.DEDICATED_CUSTOMER:
        # This proof is deliberately before WAL selection, migrations,
        # workspace creation, or legacy attachment adoption.
        verify_dedicated_database_binding(USER_DB, deployment_policy)
    # Honour the same explicit local/test journal-mode selection used by
    # ``init_db``.  Production still defaults to WAL and therefore retains the
    # patched-runtime gate; a developer can choose DELETE journalling without
    # making a false WAL-safety assertion.
    configured_journal_mode = os.getenv(
        "BRIDGEGUARD_SQLITE_JOURNAL_MODE", ""
    ).strip().lower()
    if configured_journal_mode and configured_journal_mode not in {"wal", "delete"}:
        raise RuntimeError(
            "BRIDGEGUARD_SQLITE_JOURNAL_MODE must be 'wal' or 'delete'"
        )
    if not configured_journal_mode:
        configured_journal_mode = (
            "delete"
            if os.getenv("BRIDGEGUARD_ENV", "").strip().lower() == "test"
            else "wal"
        )
    require_safe_sqlite_wal_runtime(configured_journal_mode)
    init_db(USER_DB)
    # The HTTP adapter exposes one server-owned research workspace.  Clients
    # cannot create or select workspaces; the row is initialized here through
    # the same checksummed schema and revision service used by requests.
    try:
        from .revision_api import SHARED_WORKSPACE_ID, SHARED_WORKSPACE_NAME
        from .services.conversion.revision_store import ensure_workspace
    except ImportError:
        from revision_api import SHARED_WORKSPACE_ID, SHARED_WORKSPACE_NAME
        from services.conversion.revision_store import ensure_workspace
    ensure_workspace(USER_DB, SHARED_WORKSPACE_ID, SHARED_WORKSPACE_NAME)
    conn = sqlite3.connect(
        f"file:{Path(USER_DB).resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()
    sqlite_status = inspect_sqlite_wal_runtime(journal_mode)
    if not sqlite_status.approved:
        message = (
            f"SQLite WAL safety check failed for {sqlite_status.runtime_version}: "
            f"{sqlite_status.reason}"
        )
        if os.getenv("BRIDGEGUARD_ENV", "").strip().lower() == "production":
            raise RuntimeError(message)
        print(f"[startup] WARNING: {message}")
    _init_attachments_db()
    initialize_retention_store(USER_DB)
    # Sweep once before serving traffic; the durable worker repeats this at a
    # bounded cadence so expiry and deletion retries do not depend on API use.
    _REPORT_JOB_HEALTH["retention"] = _run_retention_maintenance()
    _REPORT_JOB_HEALTH["retention_error_code"] = None
    print("Database initialized")
    print("[startup] BridgeGuard API ready — Cerebras generation + pinned Nomic TEI embeddings.")
    stop, worker = _start_report_job_worker()
    yield
    stop.set()
    worker.join(timeout=5)
    # Shutdown: the separately managed TEI sidecar owns the embedding model.


app = FastAPI(title="SNBI Bridge API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(point_cloud_router)
app.include_router(revision_router)
app.include_router(imports_router)
app.include_router(forecast_v2_router)
app.include_router(lcca_v2_router)
app.include_router(kpi_v2_router)
app.include_router(map_router)
app.include_router(site_intelligence_router)
# FastAPI 0.116+ keeps included routes behind a lazy wrapper in this runtime;
# expose this security-sensitive endpoint directly so the SPA catch-all cannot
# mask it (the Bridgette/AI adapters use the same reviewed pattern below).
app.router.routes.extend(map_router.routes)
app.router.routes.extend(site_intelligence_router.routes)
# Keep the durable import lifecycle ahead of the SPA catch-all as concrete
# routes as well.  This runtime represents include_router entries lazily; the
# explicit entries make multipart/status/commit endpoints directly routable
# and inspectable by the same security harness used for the map adapter.
app.router.routes.extend(imports_router.routes)
app.router.routes.extend(forecast_v2_router.routes)
app.router.routes.extend(lcca_v2_router.routes)
app.router.routes.extend(kpi_v2_router.routes)

# CORS. In the single-server deployment the SPA is served same-origin so CORS is
# not strictly required, but keep it configurable for split (separate-host)
# deployments. Production is same-origin by default; cross-origin access must be
# explicitly enabled with a comma-separated CORS_ORIGINS allowlist.
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Accept", "Authorization", "Content-Type", "X-Requested-With",
        "Idempotency-Key", "If-Match", "X-Request-ID",
    ],
    expose_headers=[
        "ETag",
        "X-Request-ID",
        "X-BridgeGuard-Data-Version",
        "X-BridgeGuard-Export-ID",
        "X-BridgeGuard-KPI-Definition",
        "X-Content-SHA256",
        "Content-Disposition",
    ],
)
# Added before AuthMiddleware so Starlette's reverse wrapping order makes
# authentication the outer boundary and quota identity is always server-owned.
app.add_middleware(
    OperationQuotaMiddleware,
    workspace_id=os.getenv(
        "BRIDGEGUARD_WORKSPACE_ID", "shared-research-workspace"
    ).strip() or "shared-research-workspace",
)
# Retired projection mutations remain usable by local/research compatibility
# clients only.  Add this before AuthMiddleware so authentication remains the
# outer boundary and an unauthenticated caller cannot use the typed retirement
# response to probe the API surface.
app.add_middleware(LegacyApiGuardMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    RequestBodyLimitMiddleware,
    path_limits={
        # Bounded multipart headers/form fields in addition to the PDF itself.
        "/api/vision-rag": DOCUMENT_MAX_UPLOAD_BYTES + (2 * 1024 * 1024),
        "/api/imports": MAX_IMPORT_BYTES + (2 * 1024 * 1024),
        "/api/imports/": MAX_IMPORT_BYTES + (2 * 1024 * 1024),
        "/api/analytics/kpis/program/import": MAX_PROGRAM_KPI_BYTES + (2 * 1024 * 1024),
        "/api/analytics/kpis/program/import/": MAX_PROGRAM_KPI_BYTES + (2 * 1024 * 1024),
        "/api/convert-v2/inspection-history": (
            MAX_HISTORY_CONVERSION_BYTES + (2 * 1024 * 1024)
        ),
        "/api/convert-v2/inspection-history/": (
            MAX_HISTORY_CONVERSION_BYTES + (2 * 1024 * 1024)
        ),
    },
)


def clean_nan_values(obj):
    """Recursively replace NaN and Inf values with None"""
    if isinstance(obj, dict):
        return {k: clean_nan_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan_values(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def _is_legacy_nbi(bridge) -> bool:
    """True when a bridge originated from an NBI record (older NBI-only data).

    Converted/loaded NBI bridges carry ``_SourceData.nbi``. Such rows are
    validated leniently because the SNBI specification (and its structural
    datasets, code lists and date formats) does not apply to legacy NBI data.
    """
    if not isinstance(bridge, dict):
        return False
    source = bridge.get("_SourceData")
    return isinstance(source, dict) and isinstance(source.get("nbi"), dict)


# Centralized data paths (single repo-relative data/ folder; see datapaths.py).
try:
    from .routers.services.datapaths import DATA_DIR, USER_DB, ATTACHMENTS_DIR, TEMP_DIR
except ImportError:
    from routers.services.datapaths import DATA_DIR, USER_DB, ATTACHMENTS_DIR, TEMP_DIR

try:
    from .ai_api import create_ai_router
    from .services.ai.context import (
        DEFAULT_AI_REQUEST_REGISTRY,
        AiAnalysisMode,
        AiRequestCancelled,
        AiRequestContext,
        AiRequestStatus,
        VisualAnalysisLimits,
    )
    from .services.bridge_identity import (
        BridgeIdentityError,
        BridgeSnapshotIdentity,
        resolve_published_bridge_identity,
    )
    from .services.ai.sessions import (
        MAX_TURN_CHARACTERS,
        SessionLimitExceeded,
        SessionNotFound,
        add_unverified_assistant_turn,
        add_user_turn,
        get_session,
    )
except ImportError:
    from ai_api import create_ai_router
    from services.ai.context import (
        DEFAULT_AI_REQUEST_REGISTRY,
        AiAnalysisMode,
        AiRequestCancelled,
        AiRequestContext,
        AiRequestStatus,
        VisualAnalysisLimits,
    )
    from services.bridge_identity import (
        BridgeIdentityError,
        BridgeSnapshotIdentity,
        resolve_published_bridge_identity,
    )
    from services.ai.sessions import (
        MAX_TURN_CHARACTERS,
        SessionLimitExceeded,
        SessionNotFound,
        add_unverified_assistant_turn,
        add_user_turn,
        get_session,
    )


# Bridgette's HTTP surface is deliberately late-bound here: tests can replace
# this one service seam without starting a model provider or opening a deployed
# database, while production always enters the reviewed semantic-query service.
try:
    from .query_api import (
        BoundedResultStore,
        BridgetteStreamRequest,
        create_bridgette_router,
    )
    from .revision_api import SHARED_WORKSPACE_ID
    from .services.query.registry import DEFAULT_REQUEST_REGISTRY
    from .services.query.http_service import (
        register_csv_result,
        stream_bridgette_semantic_query,
    )
except ImportError:
    from query_api import BoundedResultStore, BridgetteStreamRequest, create_bridgette_router
    from revision_api import SHARED_WORKSPACE_ID
    from services.query.registry import DEFAULT_REQUEST_REGISTRY
    from services.query.http_service import (
        register_csv_result,
        stream_bridgette_semantic_query,
    )


def _bridgette_service_stream(request: BridgetteStreamRequest):
    """Late-bound seam over the production semantic-query runtime."""

    return stream_bridgette_semantic_query(
        database_path=USER_DB,
        workspace_id=SHARED_WORKSPACE_ID,
        request=request,
        registry=DEFAULT_REQUEST_REGISTRY,
        result_download_hook=lambda query_request, evidence: register_csv_result(
            BRIDGETTE_RESULT_STORE,
            query_request,
            evidence,
        ),
    )


BRIDGETTE_RESULT_STORE = BoundedResultStore()


bridgette_query_router = create_bridgette_router(
    stream_handler_getter=lambda: _bridgette_service_stream,
    registry_getter=lambda: DEFAULT_REQUEST_REGISTRY,
    workspace_id=SHARED_WORKSPACE_ID,
    state_code=34,
    result_store_getter=lambda: BRIDGETTE_RESULT_STORE,
)
# Keep these request-scoped routes directly visible on the application router.
# FastAPI 0.116+ stores ``include_router`` entries in a lazy private wrapper;
# explicit visibility is needed by the focused security harness and avoids a
# catch-all SPA route masking the late-added API endpoints.
app.router.routes.extend(bridgette_query_router.routes)

ai_router = create_ai_router(
    database_path=USER_DB,
    workspace_id=SHARED_WORKSPACE_ID,
    registry=DEFAULT_AI_REQUEST_REGISTRY,
)
app.router.routes.extend(ai_router.routes)


class _AiRequestLifecycle:
    """Finish one registered AI request exactly once on every exit path.

    Routes record only the intended terminal outcome.  Quota release and the
    durable-in-process terminal status are committed from ``__exit__``, so an
    early return, task cancellation, or closed streaming response cannot skip
    cleanup or release the same lease twice.
    """

    def __init__(
        self,
        context: AiRequestContext,
        *,
        evidence: dict[str, object] | None = None,
    ) -> None:
        self.context = context
        self._base_evidence = dict(evidence or {})
        self._status: AiRequestStatus | None = None
        self._terminal_evidence: dict[str, object] = {}
        self._finished = False

    def __enter__(self) -> "_AiRequestLifecycle":
        return self

    def _set_terminal(
        self,
        status: AiRequestStatus,
        evidence: dict[str, object] | None = None,
    ) -> None:
        # The first terminal event is authoritative.  In particular, closing a
        # stream after receiving its done event must not rewrite completion as
        # stopped when GeneratorExit is injected at the terminal yield.
        if self._status is None:
            self._status = status
            self._terminal_evidence = dict(evidence or {})

    def completed(self, evidence: dict[str, object] | None = None) -> None:
        self._set_terminal(AiRequestStatus.COMPLETED, evidence)

    def stopped(self, evidence: dict[str, object] | None = None) -> None:
        self._set_terminal(AiRequestStatus.STOPPED, evidence)

    def error(self, evidence: dict[str, object] | None = None) -> None:
        self._set_terminal(AiRequestStatus.ERROR, evidence)

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._finished:
            return False
        if self._status is None:
            cancellation_types = (
                asyncio.CancelledError,
                GeneratorExit,
                CancelledError,
                ChatCancelled,
                AiRequestCancelled,
            )
            is_cancelled = bool(
                exc_type is not None
                and issubclass(exc_type, cancellation_types)
            )
            self._status = (
                AiRequestStatus.STOPPED if is_cancelled else AiRequestStatus.ERROR
            )
        evidence = dict(self._base_evidence)
        evidence.update(self._terminal_evidence)
        self._finished = True
        DEFAULT_AI_REQUEST_REGISTRY.finish(
            self.context,
            status=self._status,
            evidence=evidence,
        )
        return False


def _register_ai_request(
    request: Request,
    *,
    mode: AiAnalysisMode,
    operation: str,
    conversation_id: str | None,
    state_code: int | None,
    bridge_number: str | None,
    data_year: int | None = None,
    bridge_identity: BridgeSnapshotIdentity | None = None,
):
    request_ids = request.headers.getlist("X-Request-ID")
    if len(request_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="X-Request-ID is required exactly once",
        )
    user = getattr(request.state, "user", None)
    owner_id = str(getattr(user, "email", "")).strip().lower()
    if not owner_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if conversation_id:
        try:
            get_session(
                USER_DB,
                workspace_id=SHARED_WORKSPACE_ID,
                owner_id=owner_id,
                session_id=conversation_id,
            )
        except (SessionNotFound, ValueError):
            # Cross-owner and absent IDs are intentionally indistinguishable.
            raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return DEFAULT_AI_REQUEST_REGISTRY.register(
            request_id=request_ids[0],
            owner_id=owner_id,
            workspace_id=SHARED_WORKSPACE_ID,
            conversation_id=conversation_id,
            mode=mode,
            operation=operation,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
            bridge_identity=bridge_identity,
            timeout_seconds=300,
        )
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail="AI request capacity is temporarily exhausted.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except ValueError as exc:
        duplicate = "already registered" in str(exc)
        raise HTTPException(
            status_code=409 if duplicate else 422,
            detail=(
                "Request ID is already active."
                if duplicate
                else "Invalid AI request context."
            ),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI request service is temporarily unavailable.",
        ) from exc


def _published_identity_evidence(
    *,
    state_code: int | None,
    bridge_number: str | None,
    data_year: int | None = None,
    expected_snapshot_revision_id: str | None = None,
) -> tuple[dict, int | None, BridgeSnapshotIdentity | None]:
    if state_code is None or bridge_number is None:
        return {"status": "not_bridge_scoped"}, None, None
    try:
        identity = resolve_published_bridge_identity(
            USER_DB,
            workspace_id=SHARED_WORKSPACE_ID,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
        )
        expected_revision = str(expected_snapshot_revision_id or "").strip()
        if expected_revision and identity.snapshot_revision_id != expected_revision:
            return {
                "status": "published_identity_mismatch",
                "identity": None,
            }, None, None
        return (
            {"status": "resolved", "identity": identity.public_payload()},
            identity.data_year,
            identity,
        )
    except BridgeIdentityError:
        # Legacy projection rows may not yet have a published canonical
        # revision. State that gap explicitly; never invent a revision ID.
        return {"status": "published_identity_unavailable", "identity": None}, None, None


def _append_conversation_user_turn(
    context: AiRequestContext,
    content: str,
) -> None:
    """Persist one owned user turn after request registration succeeds."""
    if context.conversation_id is None:
        return
    try:
        add_user_turn(
            USER_DB,
            workspace_id=context.workspace_id,
            owner_id=context.owner_id,
            session_id=context.conversation_id,
            content=content,
            request_id=context.request_id,
            reserve_assistant_turn=True,
        )
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    except (SessionLimitExceeded, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Conversation turn could not be accepted.",
        ) from exc


def _append_conversation_assistant_turn(
    context: AiRequestContext,
    *,
    content: str,
    identity_evidence: dict,
    evidence: dict,
) -> None:
    """Persist model output with honest, explicitly unverified provenance.

    A published revision may be cited as the inventory scope used, but this
    helper never calls the verified-answer append path: ordinary Ask output has
    not passed the separate deterministic answer-verification workflow.
    """
    if context.conversation_id is None:
        return
    full_content = str(content or "")
    stored_content = full_content[:MAX_TURN_CHARACTERS]
    receipt_payload = {
        "schema_version": "chat-evidence-v1",
        "request_id": context.request_id,
        "analysis_mode": context.mode.value,
        "answer_sha256": hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
        "answer_truncated_for_retention": len(full_content) > len(stored_content),
        "bridge_snapshot": identity_evidence,
        "evidence": clean_nan_values(evidence),
    }
    receipt = hashlib.sha256(
        json.dumps(
            receipt_payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    identity = identity_evidence.get("identity") if isinstance(identity_evidence, dict) else None
    cited_revision = (
        str(identity.get("snapshot_revision_id"))
        if isinstance(identity, dict) and identity.get("snapshot_revision_id")
        else None
    )
    add_unverified_assistant_turn(
        USER_DB,
        workspace_id=context.workspace_id,
        owner_id=context.owner_id,
        session_id=context.conversation_id,
        content=stored_content,
        request_id=context.request_id,
        cited_revision=cited_revision,
        evidence_receipt_sha256=receipt,
    )

# ====================== BRIDGE ATTACHMENTS (inspection reports) ======================
import re
# Only formats with an implemented, tested parser are accepted. Legacy binary
# Office .doc/.xls files previously passed upload and then failed asynchronously.
REPORT_EXTS = {'.pdf', '.docx', '.xlsx', '.txt', '.csv', '.png', '.jpg', '.jpeg'}
MAX_RPT_BYTES = int(os.getenv("BRIDGEGUARD_MAX_UPLOAD_MB", "50")) * 1024 * 1024
MAX_CONVERSION_BYTES = (
    int(os.getenv("BRIDGEGUARD_MAX_CONVERSION_UPLOAD_MB", "100")) * 1024 * 1024
)
os.makedirs(os.path.join(ATTACHMENTS_DIR, "reports"), exist_ok=True)

_SAFE_BN_RE = re.compile(r'[^A-Za-z0-9_\-]')


def _safe_bridge_id(state_code: str, bridge_number: str) -> str:
    sc = re.sub(r'[^0-9]', '', str(state_code))
    bn = _SAFE_BN_RE.sub('_', str(bridge_number))
    if not sc or not bn:
        raise ValueError("Invalid state_code or bridge_number")
    return f"{sc}_{bn}"


def _safe_upload_name(filename: str | None, fallback: str = "upload") -> str:
    """Strip directories and unsafe characters from an untrusted upload name."""
    basename = Path(filename or fallback).name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", basename).strip("._")
    return safe[:180] or fallback


async def _read_upload_limited(file: UploadFile, limit: int = MAX_RPT_BYTES) -> bytes:
    """Read an upload without allowing an oversized body to accumulate in RAM."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"File too large (max {limit // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


async def _stream_upload_to_private_path(
    file: UploadFile,
    destination: Path,
    *,
    limit: int = MAX_RPT_BYTES,
    chunk_size: int = 1024 * 1024,
) -> tuple[int, str]:
    """Stream an upload to a preselected private path and return size/hash.

    The destination must not be derived from the submitted filename.  Partial
    files are removed on every failure, including size-limit violations.
    """

    if limit <= 0 or chunk_size <= 0 or chunk_size > limit:
        raise ValueError("invalid upload streaming limits")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("xb") as output:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {limit // (1024 * 1024)} MB)",
                    )
                digest.update(chunk)
                output.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")
            output.flush()
            os.fsync(output.fileno())
        return size, digest.hexdigest()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _init_attachments_db():
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS bridge_attachments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code      TEXT    NOT NULL,
            bridge_number   TEXT    NOT NULL,
            attachment_type TEXT    NOT NULL,
            filename        TEXT    NOT NULL,
            filepath        TEXT    NOT NULL,
            size_bytes      INTEGER,
            sha256          TEXT,
            workspace_id    TEXT,
            uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    columns = {row[1] for row in c.execute("PRAGMA table_info(bridge_attachments)")}
    dedicated = (
        os.getenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")
        .strip()
        .lower()
        == "dedicated_customer"
    )
    if dedicated and not {"sha256", "workspace_id"}.issubset(columns):
        conn.close()
        raise RuntimeError("dedicated attachment catalog is not bootstrap-complete")
    if "sha256" not in columns:
        c.execute("ALTER TABLE bridge_attachments ADD COLUMN sha256 TEXT")
    if "workspace_id" not in columns:
        c.execute("ALTER TABLE bridge_attachments ADD COLUMN workspace_id TEXT")
    # This release supports one server-owned workspace per isolated deployment.
    # Adopt pre-workspace rows into that exact workspace once, transactionally;
    # future dedicated instances start with a customer-specific workspace ID.
    if not dedicated:
        c.execute(
            "UPDATE bridge_attachments SET workspace_id=? WHERE workspace_id IS NULL",
            (SHARED_WORKSPACE_ID,),
        )
    conn.commit()
    conn.close()


def _portable_attachment_path(filepath: str) -> str:
    """Store paths relative to the persistent data root when possible."""
    try:
        return portable_confined_path(Path(USER_DB).parent, filepath)
    except StoragePathError as exc:
        raise ValueError("attachment path is outside the application data root") from exc


def _resolve_attachment_path(filepath: str) -> Path:
    try:
        return resolve_confined_path(Path(USER_DB).parent, filepath)
    except StoragePathError as exc:
        raise ValueError("attachment path is outside the application data root") from exc


@app.post("/api/bridges/{state_code}/{bridge_number}/reports")
async def upload_bridge_report(
    state_code: str,
    bridge_number: str,
    request: Request,
    file: UploadFile = File(...),
):
    """Attach an inspection report file to a specific bridge."""
    try:
        bid = _safe_bridge_id(state_code, bridge_number)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state_code or bridge_number")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in REPORT_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported type '{ext}'. Allowed: {', '.join(sorted(REPORT_EXTS))}")
    rpt_dir = Path(ATTACHMENTS_DIR) / "reports" / bid
    filepath = rpt_dir / f"{uuid4().hex}{ext}"
    size_bytes, source_sha256 = await _stream_upload_to_private_path(
        file, filepath, limit=MAX_RPT_BYTES
    )
    if ext == ".pdf":
        with filepath.open("rb") as source:
            if source.read(5) != b"%PDF-":
                filepath.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF")
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("INSERT INTO bridge_attachments (state_code, bridge_number, attachment_type, filename, filepath, size_bytes, sha256, workspace_id) VALUES (?,?,?,?,?,?,?,?)",
              (state_code, bridge_number, 'inspection_report', file.filename,
               _portable_attachment_path(str(filepath)), size_bytes, source_sha256,
               SHARED_WORKSPACE_ID))
    report_id = c.lastrowid
    conn.commit()
    conn.close()
    # Kick off ingestion (extract → embed) in the background with progress so the
    # UI can show a bar; the result is cached for an instant first "Ask".
    try:
        owner_id = str(request.state.user.email).strip().lower()
        job = _queue_report_index(
            state_code,
            bridge_number,
            owner_id=owner_id,
            idempotency_key=(
                f"upload:{report_id}:{source_sha256}"
            ),
        )
    except JobStoreError as exc:
        with sqlite3.connect(USER_DB) as cleanup:
            cleanup.execute(
                "DELETE FROM bridge_attachments WHERE workspace_id=? AND id=?",
                (SHARED_WORKSPACE_ID, report_id),
            )
        filepath.unlink(missing_ok=True)
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    return {
        "status": "success",
        "id": report_id,
        "filename": file.filename,
        "size_bytes": size_bytes,
        "ingestion_started": True,
        "job": job.public_dict(),
    }


@app.get("/api/bridges/{state_code}/{bridge_number}/reports")
def list_bridge_reports(state_code: str, bridge_number: str):
    """List all inspection reports attached to a bridge."""
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("SELECT id, filename, size_bytes, uploaded_at FROM bridge_attachments WHERE workspace_id=? AND state_code=? AND bridge_number=? AND attachment_type='inspection_report' ORDER BY id DESC",
              (SHARED_WORKSPACE_ID, state_code, bridge_number))
    rows = c.fetchall()
    conn.close()
    return {"reports": [{"id": r[0], "filename": r[1], "size_bytes": r[2], "uploaded_at": r[3]} for r in rows]}


@app.get("/api/bridges/{state_code}/{bridge_number}/reports-status")
def bridge_reports_status(state_code: str, bridge_number: str):
    """Whether this bridge's reports are parsed+embedded and ready to query (no parse
    is triggered just to check). If not ready, kicks off background indexing so the
    UI can show 'Indexing…' then 'Ready'."""
    try:
        from .routers.services.vision import bridge_reports_index_status as _status
    except ImportError:
        from routers.services.vision import bridge_reports_index_status as _status
    try:
        st = _status(
            state_code,
            bridge_number,
            workspace_id=SHARED_WORKSPACE_ID,
            analysis_mode="text",
        )
    except Exception:
        return {
            "file_count": 0,
            "ready": False,
            "indexing": False,
            "status": "status_unavailable",
            "error": "Report index status is unavailable",
        }
    return st


@app.get("/api/jobs/{job_id}")
def operation_job_status(job_id: str, request: Request):
    """Return one authenticated caller-owned durable operation receipt."""
    try:
        owner_id = str(request.state.user.email).strip().lower()
        job = get_job(
            _report_job_db(),
            workspace_id=SHARED_WORKSPACE_ID,
            owner_id=owner_id,
            job_id=job_id,
        )
    except JobStoreError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    return {"job": job.public_dict()}


@app.get("/api/bridges/{state_code}/{bridge_number}/reports/{report_id}")
def download_bridge_report(state_code: str, bridge_number: str, report_id: int):
    """Download a specific inspection report for a bridge."""
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("SELECT filepath, filename FROM bridge_attachments WHERE workspace_id=? AND id=? AND state_code=? AND bridge_number=? AND attachment_type='inspection_report'",
              (SHARED_WORKSPACE_ID, report_id, state_code, bridge_number))
    row = c.fetchone()
    conn.close()
    try:
        resolved = _resolve_attachment_path(row[0]) if row else None
    except ValueError:
        resolved = None
    if not row or resolved is None or not resolved.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(resolved, filename=row[1], media_type="application/octet-stream")


@app.delete("/api/bridges/{state_code}/{bridge_number}/reports/{report_id}")
def delete_bridge_report(
    state_code: str,
    bridge_number: str,
    report_id: int,
    request: Request,
    reason: str,
):
    """Request an audited report deletion and immediately try its outbox."""
    try:
        receipt = request_attachment_deletion(
            USER_DB,
            workspace_id=SHARED_WORKSPACE_ID,
            attachment_id=report_id,
            attachment_type="inspection_report",
            actor_id=str(request.state.user.email),
            reason=reason,
            state_code=state_code,
            bridge_number=bridge_number,
        )
        finalized = process_attachment_deletion_outbox(
            USER_DB,
            data_root=Path(USER_DB).parent,
            rag_working_dir=RAG_WORKING_DIR,
            workspace_id=SHARED_WORKSPACE_ID,
            deletion_id=receipt.id,
        )
    except RetentionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    public = (finalized[0] if finalized else receipt).public_dict()
    return JSONResponse(
        {"status": "deleted" if public["status"] == "complete" else "pending", "deletion": public},
        status_code=200 if public["status"] == "complete" else 202,
    )


@app.put("/api/bridges/{state_code}/{bridge_number}/reports/{report_id}/retention")
def update_bridge_report_retention(
    state_code: str,
    bridge_number: str,
    report_id: int,
    request: Request,
    reason: str,
    legal_hold: bool = False,
    retain_until: Optional[str] = None,
):
    """Set or clear explicit retention metadata with an authenticated audit."""
    connection = sqlite3.connect(USER_DB)
    try:
        exists = connection.execute(
            "SELECT 1 FROM bridge_attachments WHERE workspace_id=? AND id=? "
            "AND state_code=? AND bridge_number=? AND attachment_type='inspection_report'",
            (SHARED_WORKSPACE_ID, report_id, state_code, bridge_number),
        ).fetchone()
    finally:
        connection.close()
    if exists is None:
        raise HTTPException(status_code=404, detail="attachment_not_found")
    try:
        policy = set_retention_policy(
            USER_DB,
            workspace_id=SHARED_WORKSPACE_ID,
            resource_type="attachment",
            resource_id=report_id,
            actor_id=str(request.state.user.email),
            reason=reason,
            retain_until=retain_until,
            legal_hold=legal_hold,
        )
    except RetentionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    return {"retention": policy.public_dict()}


# ====================== CONVERTER ======================
@app.post("/api/convert-v2")
async def convert_v2(
    txt_file: UploadFile = File(...),
    xml_file: Optional[UploadFile] = File(None),
    source_agency_context: str = Form(...),
    encoding: str = Form(...),
    input_format: str = Form(...),
    issue_policy: str = Form("reject"),
    csv_delimiter: Optional[str] = Form(None),
    csv_quotechar: Optional[str] = Form(None),
    csv_escapechar: Optional[str] = Form(None),
    conversion_profile: str = Form(...),
    bid01_policy: Optional[str] = Form(None),
    claimed_element_schema: Optional[str] = Form(None),
):
    """Strict, traceable converter introduced alongside the legacy endpoint.

    PR2 exposes the research-import transition engine with per-field lineage.
    Operational FHWA rule bundles remain fail-closed until their registered
    artifacts are locally verified. Accepted source bytes remain immutable for
    later replay under a verified bundle.
    """

    nbi_receipt = None
    element_receipt = None
    try:
        try:
            selected_format = StrictNBIFormat(input_format)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unsupported_nbi_format",
                    "message": "Select one explicit supported NBI input format.",
                    "supported": [item.value for item in StrictNBIFormat],
                },
            ) from exc
        try:
            selected_policy = StrictIssuePolicy(issue_policy)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unsupported_issue_policy",
                    "message": "Select reject, quarantine, or accept_with_issue.",
                },
            ) from exc

        csv_dialect = None
        if selected_format in {
            StrictNBIFormat.FHWA_DOWNLOAD_CSV,
            StrictNBIFormat.NAMED_CSV,
            StrictNBIFormat.LEGACY_POSITIONAL_CSV,
        }:
            if csv_delimiter is None or csv_quotechar is None:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "csv_dialect_required",
                        "message": (
                            "CSV input requires explicit delimiter and quote character."
                        ),
                    },
                )
            try:
                csv_dialect = StrictCsvDialect(
                    delimiter=csv_delimiter,
                    quotechar=csv_quotechar,
                    escapechar=csv_escapechar or None,
                )
                csv_dialect.reader_kwargs()
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_csv_dialect", "message": str(exc)},
                ) from exc

        artifact_store = SourceArtifactStore(
            Path(DATA_DIR) / "conversion_sources",
            max_bytes=MAX_CONVERSION_BYTES,
        )
        await txt_file.seek(0)
        nbi_receipt = await run_in_threadpool(
            artifact_store.ingest,
            txt_file.file,
            original_filename=txt_file.filename or "bridges.txt",
        )
        nbi_bytes = await run_in_threadpool(
            artifact_store.read_bytes, nbi_receipt.artifact_id
        )
        nbi_result = await run_in_threadpool(
            parse_nbi_strict,
            nbi_bytes,
            source_agency_context=source_agency_context,
            encoding=encoding,
            input_format=selected_format,
            issue_policy=selected_policy,
            csv_dialect=csv_dialect,
        )

        element_result = None
        if xml_file is not None and xml_file.filename:
            await xml_file.seek(0)
            element_receipt = await run_in_threadpool(
                artifact_store.ingest,
                xml_file.file,
                original_filename=xml_file.filename,
            )
            element_bytes = await run_in_threadpool(
                artifact_store.read_bytes, element_receipt.artifact_id
            )
            element_result = await run_in_threadpool(
                parse_element_xml,
                element_bytes,
                claimed_schema=claimed_element_schema,
            )

        allowed_states = tuple(
            value.strip()
            for value in os.getenv("BRIDGEGUARD_ALLOWED_STATE_CODES", "34").split(",")
            if value.strip()
        )
        return await run_in_threadpool(
            convert_strict_parse_results,
            nbi_result,
            nbi_artifact=nbi_receipt.to_public_dict(),
            element_result=element_result,
            element_artifact=(
                element_receipt.to_public_dict() if element_receipt else None
            ),
            options=StrictConversionOptions(
                conversion_profile=conversion_profile,
                allowed_state_codes=allowed_states,
                bid01_policy=bid01_policy,
            ),
        )
    except HTTPException:
        raise
    except SourceArtifactError as exc:
        status_code = (
            413
            if exc.code == ArtifactErrorCode.UPLOAD_TOO_LARGE.value
            else 400
            if exc.code
            in {
                ArtifactErrorCode.EMPTY_UPLOAD.value,
                ArtifactErrorCode.INVALID_FILENAME.value,
            }
            else 500
        )
        raise HTTPException(status_code=status_code, detail=exc.to_public_dict()) from exc
    except NBIParseError as exc:
        detail: Dict[str, object] = {
            "code": "nbi_parse_rejected",
            "message": "The NBI source failed strict parsing.",
            "issue": jsonable_encoder(exc.issue),
        }
        if nbi_receipt is not None:
            detail["source"] = nbi_receipt.to_public_dict()
        raise HTTPException(status_code=422, detail=detail) from exc
    except StrictConversionError as exc:
        detail = exc.public_dict()
        detail["sources"] = {
            "nbi": nbi_receipt.to_public_dict() if nbi_receipt else None,
            "elements": (
                element_receipt.to_public_dict() if element_receipt else None
            ),
        }
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_conversion_request", "message": str(exc)},
        ) from exc
    except Exception as exc:
        print(f"[convert-v2] unexpected failure: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "conversion_failed",
                "message": "Strict conversion failed unexpectedly.",
            },
        ) from exc


@app.post("/api/convert-v2/inspection-history")
async def convert_inspection_history_v2(
    manifest: str = Form(...),
    annual_files: List[UploadFile] = File(...),
):
    """Convert one target annual NBI source with explicit multi-year events.

    This companion workflow requires an immutable file for every declared
    inventory year.  It runs the annual reducer before canonical normalization
    and validation, and it never changes the single-file converter's deliberate
    refusal to infer initial/routine inspections from one row.
    """

    receipts = []
    parsed_manifest = None
    try:
        parsed_manifest = parse_inspection_history_manifest(manifest)
        uploads_by_name: Dict[str, UploadFile] = {}
        for upload in annual_files:
            raw_name = str(upload.filename or "")
            canonical_name = SourceArtifactStore.sanitize_original_filename(raw_name)
            if canonical_name != raw_name or canonical_name in uploads_by_name:
                raise InspectionHistoryError(
                    "annual_source_upload_ambiguous",
                    "Uploaded annual filenames must be unique canonical part names.",
                    status_code=400,
                )
            uploads_by_name[canonical_name] = upload
        expected_names = {item.part_name for item in parsed_manifest.annual_sources}
        if set(uploads_by_name) != expected_names:
            raise InspectionHistoryError(
                "annual_source_cardinality_mismatch",
                "Uploaded annual files do not match the manifest part names.",
                status_code=400,
                details={
                    "expected_part_names": sorted(expected_names),
                    "uploaded_part_names": sorted(uploads_by_name),
                },
            )

        csv_dialect = None
        if parsed_manifest.input_format in {
            StrictNBIFormat.FHWA_DOWNLOAD_CSV,
            StrictNBIFormat.NAMED_CSV,
            StrictNBIFormat.LEGACY_POSITIONAL_CSV,
        }:
            try:
                csv_dialect = StrictCsvDialect(
                    delimiter=parsed_manifest.csv_delimiter or "",
                    quotechar=parsed_manifest.csv_quotechar or "",
                    escapechar=parsed_manifest.csv_escapechar,
                )
                csv_dialect.reader_kwargs()
            except ValueError as exc:
                raise InspectionHistoryError(
                    "invalid_history_csv_dialect",
                    "The annual-history CSV dialect is invalid.",
                    status_code=400,
                ) from exc

        artifact_store = SourceArtifactStore(
            Path(DATA_DIR) / "conversion_sources",
            max_bytes=MAX_CONVERSION_BYTES,
        )
        parsed_sources = []
        total_bytes = 0
        for source_spec in parsed_manifest.annual_sources:
            upload = uploads_by_name[source_spec.part_name]
            await upload.seek(0)
            receipt = await run_in_threadpool(
                artifact_store.ingest,
                upload.file,
                original_filename=source_spec.part_name,
            )
            receipts.append((source_spec, receipt))
            total_bytes += receipt.size_bytes
            if total_bytes > MAX_CONVERSION_BYTES:
                raise InspectionHistoryError(
                    "annual_history_upload_too_large",
                    "Combined annual source bytes exceed the conversion limit.",
                    status_code=413,
                    details={"max_bytes": MAX_CONVERSION_BYTES},
                )
            source_bytes = await run_in_threadpool(
                artifact_store.read_bytes, receipt.artifact_id
            )
            parsed = await run_in_threadpool(
                parse_nbi_strict,
                source_bytes,
                source_agency_context=parsed_manifest.source_agency_context,
                encoding=parsed_manifest.encoding,
                input_format=parsed_manifest.input_format,
                issue_policy=parsed_manifest.issue_policy,
                csv_dialect=csv_dialect,
            )
            parsed_sources.append(
                ParsedAnnualSource(
                    spec=source_spec,
                    artifact=receipt.to_public_dict(),
                    parse_result=parsed,
                )
            )

        allowed_states = tuple(
            value.strip()
            for value in os.getenv("BRIDGEGUARD_ALLOWED_STATE_CODES", "34").split(",")
            if value.strip()
        )
        prepared = await run_in_threadpool(
            prepare_inspection_history,
            parsed_manifest,
            parsed_sources,
            allowed_state_codes=allowed_states,
        )
        history_evidence = prepared.public_dict()
        if not prepared.history_ready:
            return {
                "status": "blocked",
                "engine": history_evidence["engine"],
                "submission_ready": False,
                "submission_label": history_evidence["submission_label"],
                "converted": 0,
                "skipped": len(prepared.target_bridge_keys),
                "bridges": [],
                "inspection_history": history_evidence,
            }

        history_context = {
            "rule_bundle_id": history_evidence["rule_bundle_id"],
            "history_scope": history_evidence["history_scope"],
            "source_records": history_evidence["source_records"],
        }
        target_source = prepared.target_source
        conversion = await run_in_threadpool(
            convert_strict_parse_results,
            target_source.parse_result,
            nbi_artifact=target_source.artifact,
            inspection_history_results=prepared.attachment_results(),
            inspection_history_context=history_context,
            options=StrictConversionOptions(
                conversion_profile="research_historical_import",
                allowed_state_codes=allowed_states,
                bid01_policy=parsed_manifest.bid01_policy,
            ),
        )
        conversion["inspection_history"] = history_evidence
        conversion["sources"]["annual_history"] = [
            {
                "part_name": source.spec.part_name,
                "inventory_year": source.spec.inventory_year,
                **dict(source.artifact),
            }
            for source in prepared.sources
        ]
        return conversion
    except HTTPException:
        raise
    except InspectionHistoryError as exc:
        detail = exc.public_dict()
        if receipts:
            detail["sources"] = [
                {
                    "part_name": spec.part_name,
                    "inventory_year": spec.inventory_year,
                    "artifact": receipt.to_public_dict(),
                }
                for spec, receipt in receipts
            ]
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except SourceArtifactError as exc:
        status_code = (
            413
            if exc.code == ArtifactErrorCode.UPLOAD_TOO_LARGE.value
            else 400
            if exc.code
            in {
                ArtifactErrorCode.EMPTY_UPLOAD.value,
                ArtifactErrorCode.INVALID_FILENAME.value,
            }
            else 500
        )
        detail = exc.to_public_dict()
        if receipts:
            detail["sources"] = [
                {
                    "part_name": spec.part_name,
                    "inventory_year": spec.inventory_year,
                    "artifact": receipt.to_public_dict(),
                }
                for spec, receipt in receipts
            ]
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except NBIParseError as exc:
        current_source = receipts[-1] if receipts else None
        detail: Dict[str, object] = {
            "code": "annual_nbi_parse_rejected",
            "message": "An annual NBI source failed strict parsing.",
            "issue": jsonable_encoder(exc.issue),
            "sources": [
                {
                    "part_name": spec.part_name,
                    "inventory_year": spec.inventory_year,
                    "artifact": receipt.to_public_dict(),
                }
                for spec, receipt in receipts
            ],
        }
        if current_source is not None:
            detail["failed_part_name"] = current_source[0].part_name
            detail["failed_inventory_year"] = current_source[0].inventory_year
        raise HTTPException(status_code=422, detail=detail) from exc
    except StrictConversionError as exc:
        detail = exc.public_dict()
        detail["sources"] = [
            {
                "part_name": spec.part_name,
                "inventory_year": spec.inventory_year,
                "artifact": receipt.to_public_dict(),
            }
            for spec, receipt in receipts
        ]
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_inspection_history_request", "message": str(exc)},
        ) from exc


@app.post("/api/convert")
async def convert(txt_file: UploadFile = File(...), xml_file: Optional[UploadFile] = File(None)):
    temporary_paths: List[Path] = []
    try:
        # The current endpoint retains its response contract while using unique,
        # private temporary identities. The submitted filename is metadata only;
        # it is never a filesystem identity.
        tmp_dir = Path(TEMP_DIR)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        import uuid

        operation_id = uuid.uuid4().hex
        txt_path = tmp_dir / f"convert-{operation_id}.nbi"
        temporary_paths.append(txt_path)
        xml_path = None
        await _stream_upload_to_private_path(txt_file, txt_path)
        if xml_file and xml_file.filename:
            xml_path = tmp_dir / f"convert-{operation_id}.elements.xml"
            temporary_paths.append(xml_path)
            await _stream_upload_to_private_path(xml_file, xml_path)

        elements_supplied = xml_path is not None

        bridges_data = parse_nbi_txt(txt_path)
        elements_by_struc = parse_elements_xml(xml_path) if xml_path else {}

        json_bridges: List[Dict] = []
        source_data_by_bridge: Dict[str, Dict] = {}
        skipped = 0

        for struc, bridge_data in bridges_data.items():
            old = bridge_data.get("over")
            if not old:
                skipped += 1
                continue
            unders = bridge_data.get("unders", [])
            try:
                snbi = map_bridge_to_snbi(old, unders)
                if elems := elements_by_struc.get(struc):
                    snbi["Elements"] = elems
                source_data_by_bridge[str(snbi.get("BID01") or struc)] = {"nbi": old}
                json_bridges.append(snbi)
            except Exception as e:
                print(f"Mapping error for {struc}: {e}")
                skipped += 1

        normalized_bridges, parity_errors = normalize_snbi_payload(json_bridges)

        # NJ-only build: keep only New Jersey (state code 34) bridges.
        def _is_nj(v) -> bool:
            try:
                return int(str(v).strip()) == 34
            except (TypeError, ValueError):
                return False
        before_nj = len(normalized_bridges)
        normalized_bridges = [b for b in normalized_bridges if _is_nj(b.get('BL01'))]
        dropped_non_nj = before_nj - len(normalized_bridges)
        if not normalized_bridges:
            raise HTTPException(
                status_code=400,
                detail="This build only converts New Jersey bridges (state code 34). No NJ bridges were found in the upload.",
            )

        for bridge in normalized_bridges:
            bridge_id = str(bridge.get("BID01") or "")
            if bridge_id in source_data_by_bridge:
                bridge["_SourceData"] = source_data_by_bridge[bridge_id]
        coverage = summarize_payload_coverage(normalized_bridges)
        section_coverage = summarize_payload_coverage_by_section(normalized_bridges)
        validation_payload = [
            {k: v for k, v in bridge.items() if not str(k).startswith("_")}
            for bridge in normalized_bridges
        ]
        # Every converted bridge originates from an NBI record, so it is legacy:
        # validate it leniently (skip SNBI-only structural/code/date checks).
        legacy_flags = [_is_legacy_nbi(bridge) for bridge in normalized_bridges]
        validation = validate_snbi_payload(validation_payload, legacy_flags=legacy_flags)

        return {
            "status": "success",
            "bridges": normalized_bridges,
            "converted": len(normalized_bridges),
            "skipped": skipped,
            "dropped_non_nj": dropped_non_nj,
            "elements_from_xml": elements_supplied,
            "parity_errors": parity_errors,
            "parity_ok": len(parity_errors) == 0,
            "coverage": coverage,
            "section_coverage": section_coverage,
            "validation": validation,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


# ====================== REVERSE CONVERTER ======================
@app.post("/api/reverse-convert")
async def reverse_convert(file: UploadFile = File(...)):
    """Generate explicitly lossy, review-only projections from SNBI JSON."""
    try:
        content = await _read_upload_limited(file)
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded JSON file is empty")

        # Accept UTF-8 BOM files as well.
        decoded = content.decode("utf-8-sig")
        data = json.loads(decoded)

        # Accept both raw bridge arrays and wrapped conversion responses.
        # Supported inputs:
        # 1) [ { ...bridge... }, ... ]
        # 2) { ...bridge... }
        # 3) { "bridges": [ ... ] }
        if isinstance(data, dict) and isinstance(data.get("bridges"), list):
            data = data["bridges"]
        elif isinstance(data, dict):
            data = [data]

        if not isinstance(data, list) or len(data) == 0:
            raise HTTPException(
                status_code=400,
                detail="JSON must be an array of SNBI bridges, a single SNBI bridge object, or an object with a 'bridges' array"
            )

        if not all(isinstance(item, dict) for item in data):
            raise HTTPException(status_code=400, detail="Each bridge entry must be a JSON object")

        normalized_data, parity_errors = normalize_snbi_payload(data)

        result = convert_snbi_to_outputs(normalized_data)
        return {
            "status": "success",
            "flat_json": result["flat_json"],
            "text": result["text"],
            "nbi_csv": result["nbi_csv"],
            "elements_xml": result["elements_xml"],
            "count": result["count"],
            "legacy_projection": result["legacy_projection"],
            "loss_manifest": result["loss_manifest"],
            "warnings": parity_errors[:50] if parity_errors else [],
        }
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid file encoding. Please upload UTF-8 JSON.")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ====================== LOADER ======================
@app.post("/api/load-json")
async def load_json(
    request: Request,
    file: UploadFile = File(...),
    data_year: Optional[int] = Form(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    request_id: Optional[str] = Header(None, alias="X-Request-ID"),
):
    """Deprecated alias for exact-byte revision staging; it never publishes."""

    try:
        from .revision_api import (
            _bounded_upload_digest,
            _prepare_store,
            stage_revision_upload,
        )
    except ImportError:
        from revision_api import (
            _bounded_upload_digest,
            _prepare_store,
            stage_revision_upload,
        )
    if data_year is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "data_year_required", "message": "data_year is required"},
        )
    _, generation = await run_in_threadpool(_prepare_store)
    digest, _ = await run_in_threadpool(_bounded_upload_digest, file)
    response = await run_in_threadpool(
        stage_revision_upload,
        request,
        file,
        int(data_year),
        generation,
        idempotency_key or f"legacy-load:{digest}:{int(data_year)}",
        request_id or f"legacy-load:{digest[:24]}",
    )
    if isinstance(response, JSONResponse) and response.status_code == 201:
        response.status_code = 200
    return response


async def _deprecated_load_json_unmounted(file: UploadFile, data_year: Optional[int]):
    """Deprecated multipart adapter: preserve source bytes and stage only."""
    try:
        if data_year is None:
            raise HTTPException(status_code=400, detail="An explicit data year is required")
        if not (1990 <= int(data_year) <= 2100):
            raise HTTPException(status_code=400, detail="Data year must be between 1990 and 2100")

        artifact_store = SourceArtifactStore(
            Path(DATA_DIR) / "conversion_sources", max_bytes=MAX_CONVERSION_BYTES
        )
        await file.seek(0)
        receipt = await run_in_threadpool(
            artifact_store.ingest,
            file.file,
            original_filename=file.filename or "bridges.json",
        )
        content = await run_in_threadpool(
            artifact_store.read_bytes, receipt.artifact_id
        )
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("bridges"), list):
            data = data["bridges"]
        elif isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="JSON must contain bridge objects")
        
        if len(data) == 0:
            raise HTTPException(status_code=400, detail="JSON array is empty")
        
        if len(data) > 10_000:
            raise HTTPException(status_code=413, detail="A batch may contain at most 10,000 bridges")
        if not all(isinstance(item, dict) for item in data):
            raise HTTPException(status_code=400, detail="Each bridge entry must be an object")

        # NJ-only build: reject any non-New-Jersey (state code 34) bridges.
        def _is_nj(v) -> bool:
            try:
                return int(str(v).strip()) == 34
            except (TypeError, ValueError):
                return False
        non_nj = [b for b in data if isinstance(b, dict) and not _is_nj(b.get('BL01'))]
        if non_nj:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This build only accepts New Jersey bridges (state code 34). "
                    f"{len(non_nj)} of {len(data)} record(s) are from other states."
                ),
            )

        dict_bridges = [bridge for bridge in data if isinstance(bridge, dict)]
        validation_payload = [
            {k: v for k, v in bridge.items() if not str(k).startswith("_")}
            for bridge in dict_bridges
        ]
        try:
            from .revision_api import RESEARCH_PROFILE, SHARED_WORKSPACE_ID, stage_server_documents
            from .services.conversion.revision_store import ensure_workspace, get_workspace_state
        except ImportError:
            from revision_api import RESEARCH_PROFILE, SHARED_WORKSPACE_ID, stage_server_documents
            from services.conversion.revision_store import ensure_workspace, get_workspace_state

        ensure_workspace(USER_DB, SHARED_WORKSPACE_ID, "BridgeGuard research")
        generation = get_workspace_state(USER_DB, SHARED_WORKSPACE_ID).data_generation
        items = []
        for index, bridge in enumerate(dict_bridges):
            lineage_raw = bridge.get("_TransitionLineage")
            lineage = []
            if isinstance(lineage_raw, dict):
                lineage = [
                    {**value, "target_pointer": value.get("target_pointer") or pointer}
                    for pointer, value in sorted(lineage_raw.items())
                    if isinstance(value, dict)
                ]
            items.append(
                {
                    "canonical_document": validation_payload[index],
                    "data_year": int(data_year),
                    "source_records": [
                        {
                            "source_artifact_id": receipt.artifact_id,
                            "source_record_id": f"{receipt.artifact_id}:{index}",
                            "record_kind": "json_upload_record",
                            "record_pointer": f"/{index}",
                            "payload": bridge,
                        }
                    ],
                    "lineage": lineage,
                }
            )
        staged_result = await run_in_threadpool(
            stage_server_documents,
            database_path=USER_DB,
            actor_id="legacy-load-json-adapter",
            request_id=f"legacy-load:{receipt.artifact_id}",
            idempotency_key=f"legacy-load:{receipt.sha256}:{data_year}",
            expected_data_generation=generation,
            profile_id=RESEARCH_PROFILE,
            data_year=int(data_year),
            items=items,
            artifact_receipts=(
                {
                    **receipt.to_public_dict(),
                    "media_type": "application/json",
                    "artifact_kind": "immutable_json_upload",
                },
            ),
        )
        inserted = 0
        already_existed = sum(1 for item in staged_result.revisions if not item.created)
        skip_count = 0
        skip_reasons = []
        validation = {
            "validation_label": "BridgeGuard server-owned revision validation",
            "official_validation": False,
            "summary": {"staged": len(staged_result.revisions)},
            "issues": [],
        }

        return {
            "status": "staged",
            "loaded": 0,
            "staged": len(staged_result.revisions),
            "already_existed": already_existed,
            "skipped": 0,
            "skipped_reasons": [],
            "data_year": data_year,
            "data_generation": staged_result.data_generation,
            "message": (
                f"Staged {len(staged_result.revisions)} immutable bridge revision(s) "
                f"for data year {data_year}. Review and publish them through the revision workflow."
            ),
            "validation": validation,
            "source": receipt.to_public_dict(),
            "revisions": [
                {
                    "revision_id": item.revision_id,
                    "state_code": item.state_code,
                    "bridge_number": item.bridge_number,
                    "data_year": item.data_year,
                    "outcome": "created" if item.created else "noop",
                }
                for item in staged_result.revisions
            ],
        }

        if inserted == 0 and already_existed == 0:
            detail = (
                f"No bridges were loaded. All {len(data)} records were skipped "
                f"(missing BL01/BID01 fields or mapping errors)."
            )
            if skip_reasons:
                detail += " Reasons: " + "; ".join(skip_reasons[:10])
            raise HTTPException(status_code=400, detail=detail)

        if inserted == 0 and already_existed > 0:
            return {
                "status": "conflict",
                "loaded": 0,
                "already_existed": already_existed,
                "skipped": skip_count,
                "skipped_reasons": skip_reasons,
                "message": (
                    f"All {already_existed} bridge snapshot(s) already exist for data year "
                    f"{data_year}; no destructive deletion is required."
                ),
                "data_year": data_year,
                "validation": validation,
            }

        year_suffix = f" for data year {data_year}" if data_year is not None else ""
        msg = f"Loaded {inserted} new bridge snapshot(s){year_suffix} into the database."
        if already_existed:
            msg += f" ({already_existed} existing snapshot(s) refreshed.)"
        if skip_count:
            msg += f" {skip_count} record(s) were skipped — see details below."
        return {
            "status": "success",
            "loaded": inserted,
            "already_existed": already_existed,
            "skipped": skip_count,
            "skipped_reasons": skip_reasons,
            "data_year": data_year,
            "message": msg,
            "validation": validation,
        }
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except HTTPException:
        raise
    except SourceArtifactError as exc:
        status_code = 413 if exc.code == ArtifactErrorCode.UPLOAD_TOO_LARGE.value else 400
        raise HTTPException(status_code=status_code, detail=exc.to_public_dict()) from exc
    except Exception as e:
        try:
            from .services.conversion.revision_store import RevisionStoreError
        except ImportError:
            from services.conversion.revision_store import RevisionStoreError
        if isinstance(e, RevisionStoreError):
            raise HTTPException(
                status_code=e.status,
                detail={"code": e.code, "message": e.message, "details": e.details},
            ) from e
        raise HTTPException(status_code=500, detail="The JSON revision batch could not be staged") from e


@app.post("/api/inspection-reports/extract")
async def extract_inspection_reports(
    files: List[UploadFile] = File(...),
    save_to_history: bool = Form(False),
):
    """Extract schema fields from uploaded inspection PDF folder files."""
    try:
        payloads: List[Dict] = []
        for f in files:
            content = await _read_upload_limited(f)
            payloads.append({'filename': f.filename, 'content': content})

        extracted = extract_inspection_reports_to_schema(payloads)
        successful = [x for x in extracted if x.get('status') == 'success']

        saved = 0
        if save_to_history and successful:
            saved = save_inspection_report_extractions(USER_DB, successful)

        return {
            'status': 'success',
            'total_files': len(files),
            'extracted_count': len(successful),
            'saved_count': saved,
            'records': clean_nan_values(extracted),
        }
    except Exception as e:
        print(f"Inspection report extraction error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.get('/api/inspection-reports/history')
def inspection_report_history(limit: int = 100):
    """List recent extracted inspection report rows."""
    try:
        rows = get_inspection_report_extractions(USER_DB, limit=limit)
        return {'status': 'success', 'count': len(rows), 'rows': clean_nan_values(rows)}
    except Exception as e:
        print(f"Inspection report history error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.post('/api/inspection-reports/validate')
async def validate_inspection_reports(payload: dict):
    """Validate extracted inspection report records before DB apply."""
    try:
        rows = payload.get('records') or []
        if not isinstance(rows, list) or len(rows) == 0:
            return JSONResponse(status_code=400, content={'status': 'error', 'detail': 'records must be a non-empty array'})

        validation = validate_inspection_report_records(USER_DB, rows)
        return {'status': 'success', **clean_nan_values(validation)}
    except Exception as e:
        print(f"Inspection report validation error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.post('/api/inspection-reports/apply')
async def apply_inspection_reports(payload: dict):
    """Apply validated inspection report records to history and optionally bridge snapshot."""
    try:
        rows = payload.get('records') or []
        update_bridge_snapshot = bool(payload.get('update_bridge_snapshot', False))
        if not isinstance(rows, list) or len(rows) == 0:
            return JSONResponse(status_code=400, content={'status': 'error', 'detail': 'records must be a non-empty array'})

        result = apply_validated_inspection_report_records(
            USER_DB,
            rows,
            update_bridge_snapshot=update_bridge_snapshot,
        )
        return {'status': 'success', **clean_nan_values(result)}
    except Exception as e:
        print(f"Inspection report apply error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})



@app.post("/api/vision-rag")
async def vision_rag(
    request: Request,
    file: UploadFile = File(...),
    question: str = Form(...),
    model: str = Form(''),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    analysis_mode: str = Form('text'),
    defect_segmentation_mode: str = Form('balanced'),
    conversation_id: str = Form(None),
):
    if analysis_mode.strip().lower() != "text":
        raise HTTPException(
            status_code=422,
            detail=(
                "Normal document Ask requires analysis_mode=text; "
                "use the explicit bridge visual-investigation control"
            ),
        )
    mode = AiAnalysisMode.TEXT
    context = _register_ai_request(
        request,
        mode=mode,
        operation="document_ingest_query",
        conversation_id=conversation_id,
        state_code=None,
        bridge_number=None,
    )
    with _AiRequestLifecycle(
        context,
        evidence={"analysis_mode": mode.value},
    ) as lifecycle:
        try:
            _append_conversation_user_turn(context, question)
            response = await process_vision_rag(
                file,
                question,
                model=model,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
                analysis_mode="text",
                defect_segmentation_mode=defect_segmentation_mode,
                request_context=context,
            )
            response["event_schema_version"] = "ai-event-v1"
            response["request_id"] = context.request_id
            _append_conversation_assistant_turn(
                context,
                content=str(response.get("answer") or ""),
                identity_evidence={"status": "not_bridge_scoped"},
                evidence={
                    "analysis_mode": "text",
                    "document_sha256": response.get("doc_hash"),
                    "sources": response.get("source_chunks") or [],
                },
            )
            lifecycle.completed({
                "visual_processing_ran": bool(response.get("visual_processing_ran")),
                "visual_evidence": response.get("visual_evidence") or {},
                "document_sha256": response.get("doc_hash"),
            })
            return response
        except DocumentUploadError as exc:
            lifecycle.error({"errors": [{"code": exc.code}]})
            return JSONResponse(
                status_code=exc.status_code,
                content={"status": "error", "code": exc.code, "detail": str(exc)},
            )
        except (asyncio.CancelledError, GeneratorExit):
            lifecycle.stopped()
            raise
        except (CancelledError, ChatCancelled, AiRequestCancelled):
            lifecycle.stopped()
            return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Processing cancelled by user."})
        except HTTPException as exc:
            lifecycle.error({"errors": [{"code": "request_rejected"}]})
            if exc.status_code >= 500:
                raise HTTPException(
                    status_code=500,
                    detail="Document processing could not complete.",
                ) from exc
            raise
        except Exception as exc:
            print(f"Vision RAG error: {exc}")
            lifecycle.error({"errors": [{"code": "document_rag_failed"}]})
            return JSONResponse(status_code=500, content={"status": "error", "detail": "Document processing could not complete."})


@app.post("/api/vision-query")
async def vision_query(
    request: Request,
    question: str = Form(...),
    model: str = Form(''),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    analysis_mode: str = Form('text'),
    defect_segmentation_mode: str = Form('balanced'),
    doc_hash: str = Form(''),
    conversation_id: str = Form(None),
):
    if analysis_mode.strip().lower() != "text":
        raise HTTPException(
            status_code=422,
            detail=(
                "Normal document Ask requires analysis_mode=text; "
                "use the explicit bridge visual-investigation control"
            ),
        )
    mode = AiAnalysisMode.TEXT
    context = _register_ai_request(
        request,
        mode=mode,
        operation="document_query",
        conversation_id=conversation_id,
        state_code=None,
        bridge_number=None,
    )
    with _AiRequestLifecycle(
        context,
        evidence={"analysis_mode": mode.value},
    ) as lifecycle:
        try:
            _append_conversation_user_turn(context, question)
            response = await process_vision_query_only(
                question,
                model=model,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
                analysis_mode="text",
                defect_segmentation_mode=defect_segmentation_mode,
                doc_hash=doc_hash.strip() or None,
                request_context=context,
            )
            response["event_schema_version"] = "ai-event-v1"
            response["request_id"] = context.request_id
            _append_conversation_assistant_turn(
                context,
                content=str(response.get("answer") or ""),
                identity_evidence={"status": "not_bridge_scoped"},
                evidence={
                    "analysis_mode": "text",
                    "document_sha256": response.get("doc_hash"),
                    "sources": response.get("source_chunks") or [],
                },
            )
            lifecycle.completed({
                "visual_processing_ran": bool(response.get("visual_processing_ran")),
                "visual_evidence": response.get("visual_evidence") or {},
                "document_sha256": response.get("doc_hash"),
            })
            return response
        except (asyncio.CancelledError, GeneratorExit):
            lifecycle.stopped()
            raise
        except (CancelledError, ChatCancelled, AiRequestCancelled):
            lifecycle.stopped()
            return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Processing cancelled by user."})
        except HTTPException as exc:
            lifecycle.error({"errors": [{"code": "request_rejected"}]})
            if exc.status_code >= 500:
                raise HTTPException(
                    status_code=500,
                    detail="Document query could not complete.",
                ) from exc
            raise
        except Exception as exc:
            print(f"Vision query error: {exc}")
            lifecycle.error({"errors": [{"code": "document_query_failed"}]})
            return JSONResponse(status_code=500, content={"status": "error", "detail": "Document query could not complete."})


@app.post("/api/db-rag")
async def db_rag(
    request: Request,
    question: str = Form(...),
    model: str = Form(''),
    state_code: int = Form(None),
    bridge_number: str = Form(None),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    selected_sources: str = Form(None),
    analysis_mode: str = Form('text'),
    conversation_id: str = Form(None),
    data_year: int = Form(None),
    snapshot_revision_id: str = Form(None),
):
    if analysis_mode.strip().lower() != "text":
        raise HTTPException(
            status_code=422,
            detail="Normal Ask requires analysis_mode=text; use the explicit visual control",
        )
    identity_evidence, identity_year, bridge_identity = _published_identity_evidence(
        state_code=state_code,
        bridge_number=bridge_number,
        data_year=data_year,
        expected_snapshot_revision_id=snapshot_revision_id,
    )
    if bridge_number and bridge_identity is None:
        raise HTTPException(
            status_code=409,
            detail="Bridge Ask requires the exact published bridge revision.",
        )
    context = _register_ai_request(
        request,
        mode=AiAnalysisMode.TEXT,
        operation="bridge_ask" if bridge_number else "database_ask",
        conversation_id=conversation_id,
        state_code=state_code,
        bridge_number=bridge_number,
        data_year=identity_year,
        bridge_identity=bridge_identity,
    )
    with _AiRequestLifecycle(
        context,
        evidence={
            "analysis_mode": "text",
            "visual_processing_ran": False,
            "bridge_snapshot": identity_evidence,
        },
    ) as lifecycle:
        try:
            _append_conversation_user_turn(context, question)
            # Optional source filter (JSON list of 'Database' + filenames). None = all.
            sources = None
            if selected_sources:
                try:
                    parsed = json.loads(selected_sources)
                    if isinstance(parsed, list):
                        sources = parsed
                except Exception:
                    sources = None
            if bridge_number:
                # Per-bridge multimodal Ask: full Vision-style routing over the bridge's
                # report files (any format) blended with its database record.
                response = await process_bridge_ask(
                    question,
                    model=model,
                    state_code=state_code,
                    bridge_number=bridge_number,
                    strict_grounding=strict_grounding,
                    output_format=output_format,
                    target_fields=target_fields,
                    selected_sources=sources,
                    analysis_mode="text",
                    request_context=context,
                )
            else:
                # Database-wide Ask (no specific bridge): DB record only. Run off the
                # event loop so the exact-request cancellation endpoint stays responsive.
                response = await asyncio.to_thread(
                    process_db_rag_with_files,
                    question,
                    model=model,
                    state_code=state_code,
                    bridge_number=bridge_number,
                    strict_grounding=strict_grounding,
                    output_format=output_format,
                    target_fields=target_fields,
                    selected_sources=sources,
                    analysis_mode="text",
                    request_context=context,
                )
            result = clean_nan_values(response)
            if isinstance(result, dict):
                result.update({
                    "event_schema_version": "ai-event-v1",
                    "request_id": context.request_id,
                    "analysis_mode": "text",
                    "visual_processing_ran": False,
                    "bridge_snapshot": identity_evidence,
                })
            _append_conversation_assistant_turn(
                context,
                content=str(result.get("answer") or "") if isinstance(result, dict) else str(result),
                identity_evidence=identity_evidence,
                evidence={
                    "analysis_mode": "text",
                    "sources": result.get("source_chunks") or [] if isinstance(result, dict) else [],
                    "citations": result.get("citations") or [] if isinstance(result, dict) else [],
                },
            )
            lifecycle.completed({
                "source": result.get("source") if isinstance(result, dict) else None,
            })
            return result
        except (asyncio.CancelledError, GeneratorExit):
            lifecycle.stopped()
            raise
        except (ChatCancelled, AiRequestCancelled):
            lifecycle.stopped()
            return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Stopped by user."})
        except HTTPException as exc:
            lifecycle.error({"errors": [{"code": "request_rejected"}]})
            if exc.status_code >= 500:
                raise HTTPException(
                    status_code=500,
                    detail="Ask could not complete.",
                ) from exc
            raise
        except Exception as exc:
            print(f"Database RAG error: {exc}")
            lifecycle.error({"errors": [{"code": "database_ask_failed"}]})
            return JSONResponse(status_code=500, content={"status": "error", "detail": "Ask could not complete."})


@app.post('/api/vision-reset')
def vision_reset_index(request: Request):
    """Remove only the authenticated owner's document-cache associations."""
    try:
        user = getattr(request.state, "user", None)
        owner_id = str(getattr(user, "email", "")).strip().lower()
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        reset_rag(
            owner_id=owner_id,
            workspace_id=SHARED_WORKSPACE_ID,
        )
        return {'status': 'success', 'message': 'Your document index association was removed.'}
    except Exception as e:
        print(f"Vision reset error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.post('/api/vision-cancel')
def vision_cancel():
    """Retired process-global cancellation endpoint."""
    raise HTTPException(
        status_code=410,
        detail="Global Vision cancellation was retired; cancel /api/ai/requests/{request_id}.",
    )


@app.get('/api/vision-progress')
def vision_progress():
    """Retired process-global progress endpoint."""
    raise HTTPException(
        status_code=410,
        detail="Global Vision progress was retired; read /api/ai/requests/{request_id}/evidence.",
    )


@app.get('/api/vision-index-status')
def vision_index_status(request: Request, doc_hash: str = ''):
    """Return index status only within the authenticated owner's cache scope."""
    try:
        resolved_hash = None
        try:
            from routers.services.rag_engine import resolve_doc_hash
        except ImportError:
            from .routers.services.rag_engine import resolve_doc_hash

        user = getattr(request.state, "user", None)
        owner_id = str(getattr(user, "email", "")).strip().lower()
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        resolved_hash = resolve_doc_hash(
            doc_hash.strip() or None,
            owner_id=owner_id,
            workspace_id=SHARED_WORKSPACE_ID,
        )
        if not resolved_hash:
            return {
                'status': 'success',
                'indexed': False,
                'doc_hash': None,
                'pages': 0,
                'images': 0,
            }

        return {
            'status': 'success',
            'indexed': True,
            'doc_hash': resolved_hash,
            'pages': get_document_page_count(resolved_hash),
            'images': get_extracted_image_count(resolved_hash),
        }
    except Exception as e:
        print(f"Vision index status error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


# ====================== FIXED AI PROVIDER STATUS ======================
@app.get('/api/ai-provider')
def get_ai_provider_setting():
    """Compatibility status endpoint for the fixed Cerebras + TEI deployment."""
    try:
        from routers.services import llm_providers
    except ImportError:
        from .routers.services import llm_providers
    cerebras_key_set = bool(os.getenv('CEREBRAS_API_KEY', '').strip())
    embedding = llm_providers.embedding_profile()
    message = 'Cerebras serves text and vision; pinned Nomic TEI serves 768-D embeddings.'
    if not cerebras_key_set:
        message += ' WARNING: CEREBRAS_API_KEY is not set.'
    return {
        'provider': 'cerebras',
        'cerebras_key_set': cerebras_key_set,
        'embedding_provider': embedding['provider'],
        'embedding_model': embedding['model'],
        'embedding_revision': embedding['revision'],
        'embedding_dimension': embedding['dimension'],
        'message': message,
    }


@app.get('/api/api-usage')
def get_api_usage():
    """Local daily hosted API counters for QA, images, and embeddings."""
    try:
        try:
            from routers.services import llm_providers as _providers
        except ImportError:
            from .routers.services import llm_providers as _providers
        return clean_nan_values(_providers.usage_snapshot())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ====================== RAG SETTINGS ======================
@app.get('/api/rag-settings')
def get_rag_settings():
    """Return the current RAG pipeline settings."""
    try:
        from routers.services import llm_providers, rag_engine
    except ImportError:
        from .routers.services import llm_providers, rag_engine
    embedding = llm_providers.embedding_profile()
    return {
        'embedding_provider': embedding['provider'],
        'embed_model': embedding['model'],
        'embed_dim': embedding['dimension'],
        'embed_revision': embedding['revision'],
        'embed_input_policy': embedding['input_policy'],
        'vision_model': rag_engine.VISION_MODEL,
        'llm_model': rag_engine.LLM_MODEL,
        'chunk_size': getattr(rag_engine, 'CHUNK_SIZE', 500),
        'chunk_overlap': getattr(rag_engine, 'CHUNK_OVERLAP', 100),
    }


@app.post('/api/rag-settings')
def update_rag_settings(body: dict):
    """Update runtime-safe RAG settings.

    The embedding profile is restart-scoped because changing it requires a new
    vector namespace and re-index. Ignore the legacy ``embed_model`` field
    rather than letting the UI claim that an unsafe hot switch succeeded.
    """
    try:
        from routers.services import rag_engine
    except ImportError:
        from .routers.services import rag_engine
    if 'chunk_size' in body:
        rag_engine.CHUNK_SIZE = max(100, int(body['chunk_size']))
    if 'chunk_overlap' in body:
        rag_engine.CHUNK_OVERLAP = max(0, int(body['chunk_overlap']))
    return {'status': 'success', 'message': 'Settings updated.'}


# ====================== OTHER USEFUL ENDPOINTS ======================
@app.get("/api/tables")
def get_tables():
    """Get list of all available tables in the database"""
    return get_all_tables(USER_DB)


@app.get("/api/states")
def get_states():
    """Return all state codes and names that currently have data in the bridges table."""
    import sqlite3 as _sql
    conn = _sql.connect(USER_DB, check_same_thread=False)
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT state_code FROM bridges ORDER BY state_code")
        rows = cur.fetchall()
    finally:
        conn.close()
    result = []
    for (code,) in rows:
        name = STATE_MAP.get(code, f"State {code}")
        result.append({"state_code": code, "state_name": name})
    return result


@app.get("/api/table/{table_name}")
def get_table(table_name: str, state_code: int = None, bridge_number: str = None, limit: int = None, year: int = None):
    """Get data from a specific table, optionally filtered by state_code, bridge_number, year and limited."""
    try:
        data = get_table_data(USER_DB, table_name, state_code, bridge_number, limit, year)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return clean_nan_values(data)


# ====================== DB ADMIN ENDPOINTS ======================
@app.get("/api/table-schema/{table_name}")
def table_schema(table_name: str):
    """Get column schema for a table."""
    schema = get_table_schema(USER_DB, table_name)
    if not schema:
        raise HTTPException(status_code=404, detail="Table not found")
    return schema


@app.get("/api/table-rows/{table_name}")
def table_rows(table_name: str, limit: int = 100, offset: int = 0):
    """Get rows from a table with rowid for editing. Supports pagination."""
    import sqlite3 as _sql
    conn = _sql.connect(USER_DB, check_same_thread=False)
    try:
        cur = conn.cursor()
        # Validate table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Table not found")

        # Get total count
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        total = cur.fetchone()[0]

        # Get rows WITH rowid
        cur.execute(f"SELECT rowid, * FROM {table_name} LIMIT ? OFFSET ?", (limit, offset))
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]

        return clean_nan_values({"rows": rows, "total": total, "columns": columns})
    finally:
        conn.close()


@app.post("/api/table/{table_name}/row")
async def add_table_row(table_name: str, payload: dict):
    """Insert a new row into a table."""
    try:
        new_id = insert_table_row(USER_DB, table_name, payload)
        return {"status": "success", "rowid": new_id}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.put("/api/table/{table_name}/row/{row_id}")
async def edit_table_row(table_name: str, row_id: int, payload: dict):
    """Update a row by rowid."""
    try:
        updated = update_table_row(USER_DB, table_name, row_id, payload)
        if updated == 0:
            return JSONResponse(status_code=404, content={"status": "error", "detail": "Row not found or no valid columns"})
        return {"status": "success", "updated": updated}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.delete("/api/table/{table_name}/row/{row_id}")
async def remove_table_row(table_name: str, row_id: int):
    """Delete a row by rowid."""
    try:
        deleted = delete_table_row(USER_DB, table_name, row_id)
        if deleted == 0:
            return JSONResponse(status_code=404, content={"status": "error", "detail": "Row not found"})
        return {"status": "success", "deleted": deleted}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.delete("/api/clear-data")
async def clear_all_database_data():
    """Delete all rows from all tables (bridges, features, elements, etc.) and reset sequences."""
    try:
        deleted = clear_all_data(USER_DB)
        return {"status": "success", "deleted": deleted}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.post("/api/sql-query")
async def run_sql_query(payload: dict):
    """Execute a read-only SQL query."""
    sql = payload.get("sql", "").strip()
    if not sql:
        return JSONResponse(status_code=400, content={"status": "error", "detail": "No SQL provided"})
    try:
        result = execute_readonly_sql(USER_DB, sql)
        return clean_nan_values(result)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.get("/api/loaded-years")
def get_all_loaded_years():
    """Return all distinct data_year values that have been loaded into the bridges table."""
    try:
        years = get_loaded_years(USER_DB)
        return {"years": years}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/available-years/{state_code}")
def get_available_years_for_state(state_code: int):
    """Return distinct inspection report years loaded for a state."""
    try:
        years = get_available_years(USER_DB, state_code)
        return {"years": years}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _loaded_document_etag(document: dict) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def _loaded_inventory_legacy_flags(
    state_code: int,
    documents: List[Dict],
    *,
    year: int | None,
) -> List[bool]:
    """Classify direct inventory documents from source evidence in this DB.

    Published revisions used to carry source records into validation. The
    direct yearly-inventory editor instead exports only canonical SNBI fields,
    so it must recover the same evidence from ``bridge_source_records``. This
    keeps NBI-transition rows on the research/legacy validation profile while
    native SNBI rows continue through strict SNBI validation.
    """

    bridge_numbers = {
        str(document.get("BID01") or "")
        for document in documents
        if isinstance(document, dict) and str(document.get("BID01") or "")
    }
    if not bridge_numbers:
        return [False for _document in documents]

    connection = sqlite3.connect(
        f"file:{Path(USER_DB).resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        if year is not None:
            rows = connection.execute(
                "SELECT DISTINCT bridge_number FROM bridge_source_records "
                "WHERE state_code=? AND data_year=? AND source_name='NBI TXT'",
                (int(state_code), int(year)),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT DISTINCT s.bridge_number FROM bridge_source_records s "
                "JOIN bridges b ON b.state_code=s.state_code "
                "AND b.bridge_number=s.bridge_number AND b.data_year=s.data_year "
                "WHERE s.state_code=? AND s.source_name='NBI TXT' "
                "AND (b.data_year IS NULL OR b.data_year=("
                "SELECT MAX(b2.data_year) FROM bridges b2 "
                "WHERE b2.state_code=b.state_code AND b2.bridge_number=b.bridge_number))",
                (int(state_code),),
            ).fetchall()
    finally:
        connection.close()

    legacy_bridge_numbers = {str(row[0]) for row in rows}
    return [
        isinstance(document, dict)
        and str(document.get("BID01") or "") in legacy_bridge_numbers
        for document in documents
    ]


@app.get("/api/bridge-snapshots")
def list_loaded_bridge_snapshots(
    state_code: int,
    year: int,
    limit: int = 10_000,
):
    """List exact yearly bridge rows for selectors without publication pointers."""

    maximum = max(1, min(int(limit), 10_000))
    connection = sqlite3.connect(f"file:{Path(USER_DB).resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT state_code,bridge_number,bridge_name,data_year,fhwa_omit "
            "FROM bridges WHERE state_code=? AND data_year=? "
            "ORDER BY bridge_number LIMIT ?",
            (int(state_code), int(year), maximum + 1),
        ).fetchall()
        state = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (SHARED_WORKSPACE_ID,),
        ).fetchone()
    finally:
        connection.close()
    if len(rows) > maximum:
        raise HTTPException(status_code=422, detail="Bridge selector exceeds the 10,000-row limit")
    return {
        "snapshots": [dict(row) for row in rows],
        "data_generation": int(state[0]) if state else 0,
        "schema_generation": int(state[1]) if state else 0,
    }


@app.get("/api/bridge-document/{state_code}/{bridge_number}/{year}")
def get_loaded_bridge_document(state_code: int, bridge_number: str, year: int):
    """Return one complete editable SNBI document from the exact yearly row."""

    documents = export_all_bridges_snbi(
        USER_DB,
        int(state_code),
        year=int(year),
        bridge_number=str(bridge_number),
    )
    if not documents:
        raise HTTPException(status_code=404, detail="Bridge snapshot not found")
    if len(documents) != 1:
        raise HTTPException(status_code=409, detail="Bridge snapshot identity is ambiguous")
    document = clean_nan_values(documents[0])
    legacy_flags = _loaded_inventory_legacy_flags(
        int(state_code),
        [document],
        year=int(year),
    )
    connection = sqlite3.connect(f"file:{Path(USER_DB).resolve().as_posix()}?mode=ro", uri=True)
    try:
        state = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (SHARED_WORKSPACE_ID,),
        ).fetchone()
    finally:
        connection.close()
    etag = _loaded_document_etag(document)
    return JSONResponse(
        content={
            "canonical_document": document,
            "snapshot_id": f"loaded-inventory:{int(state_code)}:{str(bridge_number)}:{int(year)}",
            # Keep the concurrency token in the body as well as the HTTP
            # header. Reverse proxies may weaken or rewrite entity-tag
            # headers, but the Editor must send the original strong token
            # back in If-Match when saving.
            "etag": etag,
            "data_generation": int(state[0]) if state else 0,
            "schema_generation": int(state[1]) if state else 0,
            "validation": clean_nan_values(
                validate_snbi_payload(
                    [document],
                    issue_limit=1000,
                    submission_year=int(year),
                    legacy_flags=legacy_flags,
                )
            ),
        },
        headers={"ETag": etag, "Cache-Control": "no-store"},
    )


@app.put("/api/bridge-document/{state_code}/{bridge_number}/{year}")
def replace_loaded_bridge_document(
    state_code: int,
    bridge_number: str,
    year: int,
    payload: dict,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Atomically replace one exact yearly snapshot and all of its child rows."""

    document = payload.get("canonical_document")
    if not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="canonical_document is required")
    current = get_loaded_bridge_document(state_code, bridge_number, year)
    current_etag = current.headers.get("etag")
    if not if_match or if_match != current_etag:
        raise HTTPException(status_code=412, detail="The inventory snapshot changed; reload before saving")
    if str(document.get("BID01", "")).strip() != str(bridge_number):
        raise HTTPException(status_code=400, detail="BID01 cannot change in this editor")
    try:
        if int(document.get("BL01")) != int(state_code):
            raise HTTPException(status_code=400, detail="BL01 cannot change in this editor")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="BL01 must match the selected state")

    connection = sqlite3.connect(USER_DB)
    try:
        state_row = connection.execute(
            "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
            (SHARED_WORKSPACE_ID,),
        ).fetchone()
        source_row = connection.execute(
            "SELECT source_payload_json FROM bridge_source_records "
            "WHERE state_code=? AND bridge_number=? AND data_year=? AND source_name='NBI TXT' "
            "ORDER BY id DESC LIMIT 1",
            (int(state_code), str(bridge_number), int(year)),
        ).fetchone()
    finally:
        connection.close()
    expected_generation = payload.get("expected_data_generation")
    if expected_generation is not None and state_row is not None and int(expected_generation) != int(state_row[0]):
        raise HTTPException(status_code=409, detail="The inventory changed; reload before saving")

    document_for_load = dict(document)
    if source_row is not None:
        try:
            nbi_source = json.loads(str(source_row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            nbi_source = None
        if isinstance(nbi_source, dict):
            document_for_load["_SourceData"] = {"nbi": nbi_source}

    inserted, existed, skipped, reasons = load_json_to_db(
        USER_DB,
        [document_for_load],
        data_year_override=int(year),
    )
    if skipped or not (inserted or existed):
        raise HTTPException(status_code=422, detail=(reasons[0] if reasons else "Snapshot replacement failed"))
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    connection = sqlite3.connect(USER_DB)
    try:
        connection.execute(
            "UPDATE workspace_state SET data_generation=data_generation+1,updated_at=? WHERE workspace_id=?",
            (now, SHARED_WORKSPACE_ID),
        )
        connection.commit()
    finally:
        connection.close()
    response = get_loaded_bridge_document(state_code, bridge_number, year)
    response.status_code = 200
    return response


@app.get("/api/export-bridges/{state_code}")
def export_bridges_by_state(state_code: int, year: int = None):
    """Export all bridges for a state as SNBI JSON objects (one dict per bridge)."""
    try:
        data = export_all_bridges_snbi(USER_DB, state_code, year=year)
        return clean_nan_values(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/validate-bridges/{state_code}")
def validate_bridges_by_state(state_code: int, year: int = None):
    """Validate exported SNBI JSON for a state/year without blocking export."""
    try:
        data = export_all_bridges_snbi(USER_DB, state_code, year=year)
        legacy_flags = _loaded_inventory_legacy_flags(
            int(state_code), data, year=year
        )
        return clean_nan_values(
            validate_snbi_payload(
                data,
                submission_year=year,
                legacy_flags=legacy_flags,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/validate-bridge/{state_code}/{bridge_number}")
def validate_single_bridge(state_code: int, bridge_number: str, year: int = None):
    """Validate one exported SNBI bridge and return field-indexed issues."""
    try:
        state_data = export_all_bridges_snbi(USER_DB, state_code, year=year)
        bridge_number_text = str(bridge_number)
        bridge_payload = [
            bridge for bridge in state_data
            if str(bridge.get("BID01", "")) == bridge_number_text
        ]
        if not bridge_payload:
            raise HTTPException(status_code=404, detail="Bridge not found for validation")
        legacy_flags = _loaded_inventory_legacy_flags(
            int(state_code), bridge_payload, year=year
        )
        return clean_nan_values(
            validate_snbi_payload(
                bridge_payload,
                issue_limit=1000,
                submission_year=year,
                legacy_flags=legacy_flags,
            )
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bridge-field-metadata/{state_code}/{bridge_number}")
def bridge_field_metadata(state_code: int, bridge_number: str, year: int = None):
    """Return editor-only source/default/temporary metadata for one bridge."""
    try:
        return clean_nan_values(get_bridge_field_metadata(USER_DB, state_code, bridge_number, year=year))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bridge/{state_code}/{bridge_number}")
def get_bridge_details(state_code: int, bridge_number: str, year: int = None):
    """Get one yearly bridge snapshot, defaulting to the bridge's latest year."""
    data = get_bridge_details_db(USER_DB, state_code, bridge_number, year=year)
    if data is None:
        raise HTTPException(status_code=404, detail="Bridge not found")
    return clean_nan_values(data)


@app.put("/api/bridge/{state_code}/{bridge_number}")
async def update_bridge(state_code: int, bridge_number: str, payload: dict, year: int = None):
    """Persist edits for exactly one immutable yearly bridge snapshot."""
    try:
        updated = update_bridge_main(USER_DB, state_code, bridge_number, payload, year=year)
        if updated == 0:
            return JSONResponse(status_code=400, content={"status": "error", "detail": "No editable fields were provided or bridge was not found."})
        response_year = year if year is not None else payload.get("data_year")
        data = get_bridge_details_db(USER_DB, state_code, bridge_number, year=response_year)
        return {"status": "success", "updated": updated, "data": clean_nan_values(data)}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        print(f"Bridge update error: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.get("/api/map-data")
def get_map_data(state_code: int = None, county_code: int = None, year: int = None, limit: int = None):
    data = get_bridges_for_map(
        USER_DB,
        state_code=state_code,
        county_code=county_code,
        year=year,
        limit=limit,
    )
    return clean_nan_values(data)


@app.get("/api/map-counties")
def get_map_county_options(state_code: int = None, year: int = None):
    data = get_map_counties(USER_DB, state_code=state_code, year=year)
    return clean_nan_values(data)


@app.get("/api/kpis")
def get_kpis(year: int = None):
    data = get_state_kpis(USER_DB, year=year)
    return clean_nan_values(data)


@app.get("/api/kpis/county")
def get_kpis_county(state_code: int = 34, year: int = None):
    """Per-county KPIs for a state (default New Jersey)."""
    data = get_county_kpis(USER_DB, state_code=state_code, year=year)
    return clean_nan_values(data)


@app.delete("/api/bridges/by-year")
def delete_bridges_for_year(state_code: int = 34, data_year: int = None):
    """Delete all bridges for a state + data year (NJ-only build defaults to 34)."""
    if data_year is None:
        raise HTTPException(status_code=400, detail="data_year is required")
    deleted = delete_bridges_by_year(USER_DB, state_code=state_code, data_year=data_year)
    return {"status": "success", "deleted": deleted, "state_code": state_code, "data_year": data_year}


@app.get("/api/bridges/{state_code}/{bridge_number}/years")
def bridge_data_years(state_code: int, bridge_number: str):
    """Distinct data years this bridge has a snapshot for (newest first)."""
    conn = sqlite3.connect(USER_DB)
    try:
        rows = conn.execute(
            "SELECT DISTINCT data_year FROM bridges WHERE state_code=? AND bridge_number=? "
            "AND data_year IS NOT NULL ORDER BY data_year DESC",
            (state_code, bridge_number)).fetchall()
    finally:
        conn.close()
    return {"years": [r[0] for r in rows]}


@app.delete("/api/bridges/{state_code}/{bridge_number}/year/{data_year}")
def delete_one_bridge_year(state_code: int, bridge_number: str, data_year: int):
    """Delete a SINGLE bridge's snapshot for one year (SNBI tables only; custom
    tables + attachments are left intact)."""
    deleted = delete_single_bridge(USER_DB, state_code, bridge_number, data_year)
    return {"status": "success", "deleted": deleted, "state_code": state_code,
            "bridge_number": bridge_number, "data_year": data_year}


# ====================== USER CUSTOM TABLES (separate from SNBI) ======================
@app.get("/api/bridges/{state_code}/{bridge_number}/custom-tables")
def get_custom_tables(state_code: int, bridge_number: str):
    """Custom tables visible for this bridge (its own + state-wide), with its rows."""
    return {"tables": list_custom_tables_for_bridge(USER_DB, state_code, bridge_number)}


@app.post("/api/custom-tables")
async def create_custom_table_ep(request: Request):
    """Create a custom table. Body: {state_code, bridge_number, name, columns[], color, scope}."""
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if b.get("state_code") is None:
        raise HTTPException(status_code=400, detail="state_code is required")
    scope = b.get("scope") or "bridge"
    if scope == "bridge" and not b.get("bridge_number"):
        raise HTTPException(status_code=400, detail="bridge_number is required for a per-bridge table")
    tid = create_custom_table(USER_DB, int(b["state_code"]), b.get("bridge_number"),
                              name, b.get("columns") or [], b.get("color") or "#8b5cf6", scope)
    return {"status": "success", "id": tid}


@app.post("/api/custom-tables/{table_id}/rows")
async def add_custom_table_row(table_id: int, request: Request):
    """Add a row to a custom table for a specific bridge. Body: {bridge_number, cells{}}."""
    b = await request.json()
    if not b.get("bridge_number"):
        raise HTTPException(status_code=400, detail="bridge_number is required")
    rid = add_custom_row(USER_DB, table_id, str(b["bridge_number"]), b.get("cells") or {})
    return {"status": "success", "id": rid}


@app.put("/api/custom-tables/rows/{row_id}")
async def edit_custom_table_row(row_id: int, request: Request):
    b = await request.json()
    update_custom_row(USER_DB, row_id, b.get("cells") or {})
    return {"status": "success"}


@app.delete("/api/custom-tables/rows/{row_id}")
def remove_custom_table_row(row_id: int):
    delete_custom_row(USER_DB, row_id)
    return {"status": "success"}


@app.delete("/api/custom-tables/{table_id}")
def remove_custom_table(table_id: int):
    """Delete a custom-table definition and all its rows (across every bridge)."""
    delete_custom_table(USER_DB, table_id)
    return {"status": "success"}


@app.post("/api/bridgette")
def bridgette(question: str = Form(...), model: str = Form('')):
    raise HTTPException(
        status_code=410,
        detail=(
            "This endpoint was retired. Use /api/bridgette/stream with a "
            "canonical X-Request-ID."
        ),
    )
    """Bridgette — natural-language question over the NJ bridge DB (text-to-SQL).

    Sync `def` on purpose: FastAPI runs it in the threadpool so the event loop
    stays free to serve a parallel /api/ask-cancel and actually stop generation.
    """
    if not question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    try:
        # SQL results can contain NaN (NULL numerics) which is not JSON-compliant;
        # sanitize like the other DB endpoints before returning.
        return clean_nan_values(bridgette_answer(question.strip(), model=model))
    except ChatCancelled:
        return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Stopped by user."})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def bridgette_stream(question: str = Form(...), model: str = Form('')):
    """Streamed Bridgette: emits newline-delimited JSON events (meta → tokens →
    done) so the UI renders the answer as it generates. Sync `def` → runs in the
    threadpool, keeping the loop free for exact-request cancellation."""
    if not question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    def gen():
        try:
            for evt in bridgette_answer_stream(question.strip(), model=model):
                yield json.dumps(clean_nan_values(evt)) + "\n"
        except ChatCancelled:
            yield json.dumps({"type": "stopped"}) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/db-rag/stream")
async def db_rag_stream(
    request: Request,
    question: str = Form(...),
    model: str = Form(''),
    state_code: int = Form(None),
    bridge_number: str = Form(None),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    selected_sources: str = Form(None),
    analysis_mode: str = Form('text'),
    conversation_id: str = Form(None),
    data_year: int = Form(None),
    snapshot_revision_id: str = Form(None),
):
    """Streamed per-bridge Ask: NDJSON events (meta → tokens → done), with
    image/page routes returned as a single 'final' event. Falls back to the
    non-streaming DB-wide ask when no bridge is given."""
    if analysis_mode.strip().lower() != "text":
        raise HTTPException(
            status_code=422,
            detail="Normal Ask requires analysis_mode=text; use the explicit visual control",
        )
    identity_evidence, identity_year, bridge_identity = _published_identity_evidence(
        state_code=state_code,
        bridge_number=bridge_number,
        data_year=data_year,
        expected_snapshot_revision_id=snapshot_revision_id,
    )
    if bridge_number and bridge_identity is None:
        raise HTTPException(
            status_code=409,
            detail="Bridge Ask requires the exact published bridge revision.",
        )
    context = _register_ai_request(
        request,
        mode=AiAnalysisMode.TEXT,
        operation="bridge_ask_stream" if bridge_number else "database_ask_stream",
        conversation_id=conversation_id,
        state_code=state_code,
        bridge_number=bridge_number,
        data_year=identity_year,
        bridge_identity=bridge_identity,
    )
    sources = None
    if selected_sources:
        try:
            parsed = json.loads(selected_sources)
            if isinstance(parsed, list):
                sources = parsed
        except Exception:
            sources = None

    async def gen():
        terminal = False
        answer_parts: list[str] = []
        turn_evidence: dict = {
            "analysis_mode": "text",
            "sources": [],
            "reports_used": [],
        }
        with _AiRequestLifecycle(
            context,
            evidence={
                "analysis_mode": "text",
                "visual_processing_ran": False,
                "bridge_snapshot": identity_evidence,
            },
        ) as lifecycle:
            try:
                _append_conversation_user_turn(context, question)
                # The first byte is deliberately inside the lifecycle. Closing
                # the client immediately after acceptance therefore injects
                # GeneratorExit through the managed cleanup path.
                yield json.dumps({
                    "event_schema_version": "ai-event-v1",
                    "request_id": context.request_id,
                    "type": "accepted",
                    "analysis_mode": "text",
                    "bridge_snapshot": identity_evidence,
                }) + "\n"
                if bridge_number:
                    async for evt in process_bridge_ask_stream(
                        question, model=model, state_code=state_code, bridge_number=bridge_number,
                        strict_grounding=strict_grounding, output_format=output_format, target_fields=target_fields,
                        selected_sources=sources, analysis_mode="text",
                        request_context=context,
                    ):
                        event_type = evt.get("type")
                        if event_type == "evidence":
                            turn_evidence.update({
                                "sources": evt.get("sources") or [],
                                "reports_used": evt.get("reports_used") or [],
                            })
                        elif event_type == "token":
                            answer_parts.append(str(evt.get("text") or ""))
                        elif event_type == "done":
                            _append_conversation_assistant_turn(
                                context,
                                content="".join(answer_parts),
                                identity_evidence=identity_evidence,
                                evidence=turn_evidence,
                            )
                            lifecycle.completed()
                            terminal = True
                        elif event_type == "stopped":
                            lifecycle.stopped()
                            terminal = True
                        elif event_type == "error":
                            lifecycle.error({"errors": [{"code": "bridge_ask_failed"}]})
                            terminal = True
                        yield json.dumps(clean_nan_values(evt)) + "\n"
                        if terminal:
                            break
                    if not terminal:
                        lifecycle.error({"errors": [{"code": "stream_ended_without_terminal_event"}]})
                        terminal = True
                        yield json.dumps({
                            "event_schema_version": "ai-event-v1",
                            "request_id": context.request_id,
                            "type": "error",
                            "detail": "Ask could not complete.",
                        }) + "\n"
                else:
                    resp = await asyncio.to_thread(
                        process_db_rag_with_files, question, model=model, state_code=state_code,
                        bridge_number=bridge_number, strict_grounding=strict_grounding,
                        output_format=output_format, target_fields=target_fields,
                        selected_sources=sources, analysis_mode="text",
                        request_context=context,
                    )
                    yield json.dumps(clean_nan_values({
                        "event_schema_version": "ai-event-v1",
                        "request_id": context.request_id,
                        "type": "evidence",
                        "sources": resp.get("source_chunks") or [],
                        "reports_used": resp.get("reports_used") or [],
                        "images": [],
                        "analysis_mode": "text",
                        "visual_processing_ran": False,
                    })) + "\n"
                    yield json.dumps({
                        "event_schema_version": "ai-event-v1",
                        "request_id": context.request_id,
                        "type": "token",
                        "text": str(resp.get("answer") or ""),
                    }) + "\n"
                    _append_conversation_assistant_turn(
                        context,
                        content=str(resp.get("answer") or ""),
                        identity_evidence=identity_evidence,
                        evidence={
                            "analysis_mode": "text",
                            "sources": resp.get("source_chunks") or [],
                            "reports_used": resp.get("reports_used") or [],
                        },
                    )
                    lifecycle.completed()
                    terminal = True
                    yield json.dumps({
                        "event_schema_version": "ai-event-v1",
                        "request_id": context.request_id,
                        "type": "done",
                    }) + "\n"
            except (asyncio.CancelledError, GeneratorExit):
                lifecycle.stopped()
                raise
            except (ChatCancelled, AiRequestCancelled):
                lifecycle.stopped()
                if not terminal:
                    terminal = True
                    yield json.dumps({
                        "event_schema_version": "ai-event-v1",
                        "request_id": context.request_id,
                        "type": "stopped",
                    }) + "\n"
            except HTTPException:
                lifecycle.error({"errors": [{"code": "request_rejected"}]})
                if not terminal:
                    terminal = True
                    yield json.dumps({
                        "event_schema_version": "ai-event-v1",
                        "request_id": context.request_id,
                        "type": "error",
                        "detail": "Ask could not complete.",
                    }) + "\n"
            except Exception:
                lifecycle.error({"errors": [{"code": "database_ask_failed"}]})
                if not terminal:
                    terminal = True
                    yield json.dumps({
                        "event_schema_version": "ai-event-v1",
                        "request_id": context.request_id,
                        "type": "error",
                        "detail": "Ask could not complete.",
                    }) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/bridge-visual-investigation")
async def bridge_visual_investigation(
    request: Request,
    query: str = Form(...),
    state_code: int = Form(...),
    bridge_number: str = Form(...),
    selected_sources: str = Form(None),
    defect_segmentation_mode: str = Form('fast'),
    analysis_mode: str = Form(...),
    conversation_id: str = Form(None),
    data_year: int = Form(None),
    snapshot_revision_id: str = Form(None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Dedicated report-image detection endpoint.

    Normal Ask stays text/DB/index based; this route performs on-demand VLM bbox
    detection only when Ask explicitly enables visual investigation. Candidate
    images are selected automatically from indexed image descriptions, with a
    fallback scan when the index signal is weak.
    """
    durable_idempotency_key = (
        idempotency_key.strip()
        if isinstance(idempotency_key, str) and idempotency_key.strip()
        else None
    )
    if analysis_mode.strip().lower() != "visual":
        raise HTTPException(
            status_code=422,
            detail="Visual investigation requires analysis_mode=visual",
        )
    if not query.strip():
        raise HTTPException(status_code=422, detail="query is required")
    sources = None
    if selected_sources:
        try:
            parsed = json.loads(selected_sources)
        except (json.JSONDecodeError, TypeError) as exc:
            if durable_idempotency_key:
                raise HTTPException(
                    status_code=422, detail="selected_sources must be a JSON array"
                ) from exc
            parsed = None
        if isinstance(parsed, list):
            sources = parsed
        elif durable_idempotency_key:
            raise HTTPException(
                status_code=422, detail="selected_sources must be a JSON array"
            )
    identity_evidence, identity_year, bridge_identity = _published_identity_evidence(
        state_code=state_code,
        bridge_number=bridge_number,
        data_year=data_year,
        expected_snapshot_revision_id=snapshot_revision_id,
    )
    if bridge_identity is None:
        raise HTTPException(
            status_code=409,
            detail="Visual investigation requires a published bridge revision.",
        )

    deployment_mode = os.getenv(
        "BRIDGEGUARD_DEPLOYMENT_MODE", DeploymentMode.RESEARCH_SHARED.value
    ).strip().lower()
    durable_required = bool(
        deployment_mode == DeploymentMode.DEDICATED_CUSTOMER.value
        or os.getenv("BRIDGEGUARD_ENV", "").strip().lower() == "production"
        or os.getenv("BRIDGEGUARD_REQUIRE_COMMERCIAL_READY", "false")
        .strip()
        .lower() in {"1", "true", "yes", "on"}
    )
    if durable_idempotency_key or durable_required:
        if not durable_idempotency_key:
            raise HTTPException(
                status_code=428,
                detail="Idempotency-Key is required for durable visual investigation.",
            )
        request_ids = request.headers.getlist("X-Request-ID")
        idempotency_values = request.headers.getlist("Idempotency-Key")
        if len(request_ids) != 1:
            raise HTTPException(
                status_code=422, detail="X-Request-ID is required exactly once"
            )
        if len(idempotency_values) != 1:
            raise HTTPException(
                status_code=422, detail="Idempotency-Key is required exactly once"
            )
        owner_id = str(
            getattr(getattr(request.state, "user", None), "email", "")
        ).strip().lower()
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        if conversation_id:
            try:
                get_session(
                    USER_DB,
                    workspace_id=SHARED_WORKSPACE_ID,
                    owner_id=owner_id,
                    session_id=conversation_id,
                )
            except (SessionNotFound, ValueError) as exc:
                raise HTTPException(status_code=404, detail="Conversation not found") from exc
        try:
            job, replay = await run_in_threadpool(
                enqueue_visual_investigation,
                database_path=USER_DB,
                job_database_path=_report_job_db(),
                workspace_id=SHARED_WORKSPACE_ID,
                owner_id=owner_id,
                idempotency_key=durable_idempotency_key,
                request_id=request_ids[0],
                query=query,
                bridge_identity=bridge_identity,
                selected_sources=sources,
                defect_segmentation_mode=defect_segmentation_mode,
                conversation_id=conversation_id,
            )
        except VisualJobError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc
        except JobStoreError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc
        status_url = f"/api/bridge-visual-investigation/jobs/{job.id}"
        return JSONResponse(
            status_code=202,
            content={
                "execution_mode": "durable_two_phase",
                "idempotent_replay": replay,
                "job": public_visual_job(job),
                "status_url": status_url,
                "result_url": f"{status_url}/result",
                "cancel_url": f"{status_url}/cancel",
            },
            headers={
                "Location": status_url,
                "X-Request-ID": request_ids[0],
            },
        )

    # Explicit research compatibility boundary. Dedicated/production callers
    # can never enter this request-owned provider path.
    context = _register_ai_request(
        request,
        mode=AiAnalysisMode.VISUAL,
        operation="bridge_visual_investigation",
        conversation_id=conversation_id,
        state_code=state_code,
        bridge_number=bridge_number,
        data_year=identity_year,
        bridge_identity=bridge_identity,
    )
    with _AiRequestLifecycle(
        context,
        evidence={
            "analysis_mode": "visual",
            "bridge_snapshot": identity_evidence,
        },
    ) as lifecycle:
        try:
            _append_conversation_user_turn(context, query.strip())
            result = await process_bridge_visual_investigation(
                query=query.strip(),
                state_code=state_code,
                bridge_number=bridge_number,
                selected_sources=sources,
                defect_segmentation_mode=defect_segmentation_mode,
                request_context=context,
                limits=VisualAnalysisLimits(),
            )
            result["event_schema_version"] = "ai-event-v1"
            result["request_id"] = context.request_id
            result["bridge_snapshot"] = identity_evidence
            result["execution_mode"] = "research_legacy_request_owned"
            result = clean_nan_values(result)
            _append_conversation_assistant_turn(
                context,
                content=str(result.get("answer") or ""),
                identity_evidence=identity_evidence,
                evidence={
                    "analysis_mode": "visual",
                    "inspected_images": result.get("inspected_images") or [],
                    "errors": result.get("errors") or [],
                    "visual_evidence": result.get("visual_evidence") or {},
                },
            )
            lifecycle.completed({
                "visual_evidence": result.get("visual_evidence", {}),
                "inspected_images": result.get("inspected_images", []),
                "errors": result.get("errors", []),
            })
            return result
        except (asyncio.CancelledError, GeneratorExit):
            lifecycle.stopped()
            raise
        except AiRequestCancelled:
            lifecycle.stopped()
            return JSONResponse(
                status_code=499,
                content={"status": "cancelled", "detail": "Visual investigation stopped."},
            )
        except HTTPException as exc:
            lifecycle.error({"errors": [{"code": "request_rejected"}]})
            if exc.status_code >= 500:
                raise HTTPException(
                    status_code=500,
                    detail="Visual investigation could not complete.",
                ) from exc
            raise
        except Exception:
            lifecycle.error({"errors": [{"code": "visual_investigation_failed"}]})
            raise HTTPException(status_code=500, detail="Visual investigation could not complete.")


@app.get("/api/bridge-visual-investigation/jobs/{job_id}")
def visual_investigation_job_status(job_id: str, request: Request):
    """Return one path-free status receipt under exact owner/workspace scope."""

    owner_id = str(
        getattr(getattr(request.state, "user", None), "email", "")
    ).strip().lower()
    try:
        job = get_owned_visual_job(
            _report_job_db(),
            workspace_id=SHARED_WORKSPACE_ID,
            owner_id=owner_id,
            job_id=job_id,
        )
        return {"job": public_visual_job(job)}
    except VisualJobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    except JobStoreError as exc:
        raise HTTPException(status_code=exc.status, detail="visual_job_not_found") from exc


@app.get("/api/bridge-visual-investigation/jobs/{job_id}/result")
def visual_investigation_job_result(job_id: str, request: Request):
    """Verify and return the immutable result artifact; never expose its path."""

    owner_id = str(
        getattr(getattr(request.state, "user", None), "email", "")
    ).strip().lower()
    try:
        job = get_owned_visual_job(
            _report_job_db(),
            workspace_id=SHARED_WORKSPACE_ID,
            owner_id=owner_id,
            job_id=job_id,
        )
        return load_visual_job_result(
            job=job,
            artifact_root=_visual_result_artifact_root(),
        )
    except VisualJobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    except JobStoreError as exc:
        raise HTTPException(status_code=exc.status, detail="visual_job_not_found") from exc


@app.post("/api/bridge-visual-investigation/jobs/{job_id}/cancel")
def cancel_visual_investigation_job(job_id: str, request: Request):
    """Durably stop queued work or signal running work at safe boundaries."""

    owner_id = str(
        getattr(getattr(request.state, "user", None), "email", "")
    ).strip().lower()
    try:
        # First use the operation-specific lookup so another operation's UUID
        # is indistinguishable from a missing visual job.
        get_owned_visual_job(
            _report_job_db(),
            workspace_id=SHARED_WORKSPACE_ID,
            owner_id=owner_id,
            job_id=job_id,
        )
        job = request_job_cancellation(
            _report_job_db(),
            workspace_id=SHARED_WORKSPACE_ID,
            owner_id=owner_id,
            job_id=job_id,
        )
        return {
            "job": public_visual_job(job),
            "provider_interruption": "application_boundaries_only",
        }
    except VisualJobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    except JobStoreError as exc:
        raise HTTPException(status_code=exc.status, detail="visual_job_not_found") from exc


@app.post("/api/ask-cancel")
def ask_cancel():
    """Compatibility stop signal for legacy per-bridge Ask generation only.

    Bridgette uses ``/api/bridgette/requests/{request_id}/cancel`` and never
    observes this historical process-global flag.
    """
    raise HTTPException(
        status_code=410,
        detail="Global Ask cancellation was retired; cancel /api/ai/requests/{request_id}.",
    )


@app.get("/api/state-history/{state_code}")
def get_state_history(state_code: int):
    data = get_state_condition_history(USER_DB, state_code)
    return clean_nan_values(data)


@app.get("/api/bridge-history/{state_code}/{bridge_number}")
def get_bridge_history(state_code: int, bridge_number: str):
    """Get historical data for a specific bridge (currently returns current data, extend for multi-year)."""
    data = get_bridge_history_db(USER_DB, state_code, bridge_number)
    if data is None:
        raise HTTPException(status_code=404, detail="Bridge not found")
    return clean_nan_values(data)


@app.post("/api/forecasts/build-training-db")
def build_forecast_training_db():
    """Rebuild the separate forecast training database from loaded SNBI snapshots."""
    try:
        return clean_nan_values(rebuild_training_db(USER_DB))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/forecasts/train")
def train_forecasts(horizon_years: int = 10):
    """Train Prophet forecasts for bridge condition components."""
    try:
        if horizon_years < 1 or horizon_years > 50:
            raise HTTPException(status_code=400, detail="horizon_years must be between 1 and 50")
        result = train_condition_forecasts(horizon_years=horizon_years)
        if result.get("status") == "error":
            return JSONResponse(status_code=400, content=clean_nan_values(result))
        return clean_nan_values(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/forecasts/train-bridge/{state_code}/{bridge_number}")
def train_bridge_forecasts(state_code: int, bridge_number: str, horizon_years: int = 10):
    """Train Prophet forecasts only for one bridge so future predictions appear quickly."""
    try:
        if horizon_years < 1 or horizon_years > 50:
            raise HTTPException(status_code=400, detail="horizon_years must be between 1 and 50")
        # Auto-build training rows for this bridge if missing, so the history
        # modal can generate a forecast on open without a manual "Build" step.
        ensure_training_data(state_code, bridge_number)
        result = train_condition_forecasts(
            horizon_years=horizon_years,
            state_code=state_code,
            bridge_number=bridge_number,
        )
        if result.get("status") == "error":
            return JSONResponse(status_code=400, content=clean_nan_values(result))
        return clean_nan_values(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/lcca/bridge/{state_code}/{bridge_number}")
def bridge_lcca(
    state_code: int, bridge_number: str, horizon_years: int = 40,
    nhs: bool = None,
    replace_per_sqft: float = None,
    rehab_per_sqft: float = None,
    annual_maint_per_sqft: float = None,
    poor_penalty_per_sqft: float = None,
    discount_rate: float = None,
):
    """Forecast-driven life-cycle cost analysis for one bridge: do-nothing vs a
    condition-triggered rehab policy, using the Markov deterioration model. All
    cost assumptions are editable via the optional query params; `nhs` picks the
    FHWA NBI NJ replacement unit cost (NHS vs non-NHS) when no explicit
    `replace_per_sqft` is supplied."""
    try:
        result = compute_bridge_lcca(
            state_code, bridge_number, horizon_years=horizon_years,
            nhs=nhs,
            replace_per_sqft=replace_per_sqft,
            rehab_per_sqft=rehab_per_sqft,
            annual_maint_per_sqft=annual_maint_per_sqft,
            poor_penalty_per_sqft=poor_penalty_per_sqft,
            discount_rate=discount_rate,
        )
        if result.get("status") == "error":
            return JSONResponse(status_code=400, content=clean_nan_values(result))
        return clean_nan_values(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/summary")
def forecast_summary():
    """Return forecast training/prediction summary and latest run."""
    try:
        return clean_nan_values(get_forecast_summary())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/backtest")
def forecast_backtest(holdout_years: int = 1):
    """Rolling-origin backtest: how well the Markov engine predicted the
    held-out most-recent inspection year(s). MAE/bias/within-1 per component."""
    try:
        from .routers.services.forecasting import backtest_markov
    except ImportError:
        from routers.services.forecasting import backtest_markov
    try:
        return clean_nan_values(backtest_markov(holdout_years=holdout_years))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/bridges")
def forecast_bridges(limit: int = 500):
    """Return bridges available in the forecast training database."""
    try:
        return clean_nan_values({"bridges": get_forecast_bridges(limit=limit)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/bridge/{state_code}/{bridge_number}")
def bridge_forecast(state_code: int, bridge_number: str):
    """Return observed and forecasted condition components for one bridge."""
    try:
        return clean_nan_values(get_bridge_forecast(state_code, bridge_number))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/bridge/{state_code}/{bridge_number}/simulate")
def simulate_bridge(state_code: int, bridge_number: str, maintenance_year: int, horizon_years: int = 10):
    """What-if: simulate a maintenance event in ``maintenance_year`` and return its
    effect on the rest of the forecast (a bump then continued degradation)."""
    try:
        if horizon_years < 1 or horizon_years > 50:
            raise HTTPException(status_code=400, detail="horizon_years must be between 1 and 50")
        ensure_training_data(state_code, bridge_number)
        result = simulate_bridge_forecast(
            state_code, bridge_number, maintenance_year, horizon_years=horizon_years
        )
        # The chosen year must fall inside the projected future window.
        future_years = [
            point["year"]
            for metric in result.get("metrics", {}).values()
            for point in metric.get("forecast", [])
        ]
        if future_years and not (min(future_years) <= maintenance_year <= max(future_years)):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"maintenance_year must be between {min(future_years)} and {max(future_years)} "
                    "(within the forecast horizon)."
                ),
            )
        return clean_nan_values(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system-info")
def system_info():
    """Return CPU, RAM, disk usage and database stats. GPU/VRAM telemetry was
    removed — this service runs no local inference (all AI uses hosted APIs)."""
    info: Dict = {}
    info["gpus"] = []  # no local GPU is used; kept for response-shape stability

    # ── CPU & RAM via psutil (guarded; falls back to /proc on Linux) ──
    try:
        import psutil
        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = round(mem.total / (1024**3), 1)
        info["ram_used_gb"] = round(mem.used / (1024**3), 1)
        info["ram_percent"] = mem.percent
    except ImportError:
        # Fallback: read /proc on Linux when psutil is not installed.
        try:
            with open("/proc/meminfo") as f:
                mi = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mi[parts[0].rstrip(":")] = int(parts[1])
                total = mi.get("MemTotal", 0)
                avail = mi.get("MemAvailable", mi.get("MemFree", 0))
                info["ram_total_gb"] = round(total / (1024**2), 1)
                info["ram_used_gb"] = round((total - avail) / (1024**2), 1)
                info["ram_percent"] = round((total - avail) / total * 100, 1) if total else 0
        except Exception:
            pass
        info.setdefault("cpu_percent", None)
    except Exception as e:
        info["resource_error"] = str(e)[:300]

    # ── Database stats ──
    try:
        db_path = USER_DB
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            info["db_path"] = db_path
            info["db_type"] = "SQLite"
            info["db_size_bytes"] = db_size
            info["db_size_mb"] = round(db_size / (1024**2), 2)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cursor.fetchall()]
            table_info = []
            for t in tables:
                cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
                count = cursor.fetchone()[0]
                table_info.append({"name": t, "rows": count})
            info["tables"] = table_info
            info["table_count"] = len(tables)
            conn.close()

            # Disk usage
            disk = shutil.disk_usage(os.path.dirname(db_path) or ".")
            info["disk_total_gb"] = round(disk.total / (1024**3), 1)
            info["disk_used_gb"] = round(disk.used / (1024**3), 1)
            info["disk_free_gb"] = round(disk.free / (1024**3), 1)
    except Exception as e:
        info["db_error"] = str(e)

    return info


@app.get("/health")
def health():
    return {"status": "ok", "message": "SNBI Bridge API is running"}


@app.get("/ready")
def ready():
    """Readiness probe: storage, database, and pinned TEI backend are usable."""
    strict_runtime = (
        os.getenv("BRIDGEGUARD_ENV", "").strip().lower() == "production"
        or os.getenv("BRIDGEGUARD_REQUIRE_COMMERCIAL_READY", "false")
        .strip().lower() in {"1", "true", "yes", "on"}
    )
    checks: Dict[str, bool] = {
        "deployment_policy": False,
        "data_directory": False,
        "database": False,
        "sqlite_wal_runtime": False,
        "embeddings": False,
        "job_store": False,
        "report_worker": False,
        "point_cloud": False,
    }
    details: Dict[str, object] = {}
    try:
        deployment = evaluate_deployment_policy(data_directory=Path(USER_DB).parent)
        checks["deployment_policy"] = deployment.approved
        details["deployment"] = deployment.public_dict()
    except Exception:
        details["deployment"] = {"approved": False, "blockers": ["policy_evaluation_failed"]}
    try:
        data_dir = Path(USER_DB).parent
        checks["data_directory"] = data_dir.is_dir() and os.access(data_dir, os.W_OK)
        conn = sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("SELECT 1").fetchone()
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            foreign_keys = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
            trusted_schema = bool(conn.execute("PRAGMA trusted_schema").fetchone()[0])
            synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
            busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            quick_check_row = conn.execute("PRAGMA quick_check(1)").fetchone()
            quick_check = str(quick_check_row[0]) if quick_check_row else "unavailable"
            if journal_mode.strip().lower() == "wal":
                checkpoint_row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                wal_checkpoint: object = (
                    {
                        "busy": int(checkpoint_row[0]),
                        "log_frames": int(checkpoint_row[1]),
                        "checkpointed_frames": int(checkpoint_row[2]),
                    }
                    if checkpoint_row and len(checkpoint_row) >= 3
                    else "unavailable"
                )
            else:
                wal_checkpoint = "not_applicable"
        finally:
            conn.close()
        checks["database"] = True
        sqlite_status = inspect_sqlite_wal_runtime(journal_mode)
        checks["sqlite_wal_runtime"] = sqlite_status.approved
        details["sqlite"] = {
            **sqlite_status.public_dict(),
            "foreign_keys": foreign_keys,
            "trusted_schema": trusted_schema,
            "synchronous": synchronous,
            "busy_timeout_ms": busy_timeout_ms,
            "wal_checkpoint": wal_checkpoint,
            "quick_check": quick_check,
            "user_version": user_version,
            "latest_schema_version": LATEST_SCHEMA_VERSION,
            "migration_current": user_version == LATEST_SCHEMA_VERSION,
        }
        checks["database"] = bool(
            foreign_keys
            and not trusted_schema
            and quick_check == "ok"
            and user_version == LATEST_SCHEMA_VERSION
        )
    except Exception:
        pass
    try:
        job_path = _report_job_db().resolve()
        if job_path.is_file():
            with sqlite3.connect(
                f"file:{job_path.as_posix()}?mode=ro", uri=True, timeout=2
            ) as job_connection:
                job_connection.execute("PRAGMA query_only=ON")
                job_connection.execute("SELECT COUNT(*) FROM operation_jobs").fetchone()
                job_quick_check_row = job_connection.execute("PRAGMA quick_check(1)").fetchone()
            checks["job_store"] = bool(
                job_quick_check_row and str(job_quick_check_row[0]) == "ok"
            )
        else:
            job_quick_check_row = None
            checks["job_store"] = not strict_runtime
        worker_alive = bool(
            _REPORT_JOB_THREAD is not None and _REPORT_JOB_THREAD.is_alive()
        )
        checks["report_worker"] = bool(
            (
                worker_alive
                and _REPORT_JOB_HEALTH.get("last_error_code") is None
                and _REPORT_JOB_HEALTH.get("retention_error_code") is None
            )
            or (not strict_runtime and not _REPORT_JOB_HEALTH.get("started"))
        )
        details["jobs"] = {
            **_REPORT_JOB_HEALTH,
            "required": strict_runtime,
            "store_quick_check": (
                str(job_quick_check_row[0]) if job_quick_check_row else "unavailable"
            ),
        }
    except Exception:
        details["jobs"] = {
            "worker_alive": bool(
                _REPORT_JOB_THREAD is not None and _REPORT_JOB_THREAD.is_alive()
            ),
            "store_quick_check": "unavailable",
            "last_error_code": "job_readiness_failed",
        }
    try:
        point_cloud = point_cloud_readiness()
        point_cloud_ready = bool(
            point_cloud.get("status") == "ok"
            and point_cloud.get("pipeline_available")
            and point_cloud.get("durable_workflow_available")
            and point_cloud.get("parser_ready")
            and point_cloud.get("multimodal_model_ready")
            and point_cloud.get("evaluation_release_ready")
        )
        checks["point_cloud"] = point_cloud_ready or not strict_runtime
        details["point_cloud"] = point_cloud
    except Exception:
        details["point_cloud"] = {
            "status": "blocked",
            "error_code": "point_cloud_readiness_failed",
        }
    try:
        try:
            from routers.services import llm_providers as _providers
        except ImportError:
            from .routers.services import llm_providers as _providers
        _providers.verify_embedding_backend(timeout=5)
        checks["embeddings"] = True
    except Exception:
        pass
    if not all(checks.values()):
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks, "details": details},
        )
    return {"status": "ready", "checks": checks, "details": details}


# ====================== STATIC FRONTEND (single-server deploy) ======================
# One app serves BOTH the JSON API (/api/*, /health) and the built React SPA.
# The dist dir is resolved from FRONTEND_DIST, else the first existing of
# <repo>/backend/static then <repo>/frontend/dist. When present, hashed assets
# are mounted and every non-/api path falls back to index.html (client routing).
def _resolve_frontend_dist() -> Optional[Path]:
    candidates = []
    env_dir = os.getenv("FRONTEND_DIST", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    # main.py = <repo>/backend/app/main.py -> parents[2] = <repo>
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "backend" / "static")
    candidates.append(repo_root / "frontend" / "dist")
    for c in candidates:
        try:
            if c.is_dir() and (c / "index.html").is_file():
                return c
        except Exception:
            continue
    return None


_FRONTEND_DIST = _resolve_frontend_dist()
if _FRONTEND_DIST is not None:
    _INDEX_HTML = _FRONTEND_DIST / "index.html"
    _assets_dir = _FRONTEND_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        """Serve the SPA. Unknown /api/* paths return 404 JSON; any other path
        returns index.html so the React router handles it client-side."""
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        # Serve real files that live at the dist root (favicon, icons, etc.).
        if full_path:
            candidate = (_FRONTEND_DIST / full_path).resolve()
            try:
                if candidate.is_file() and str(candidate).startswith(str(_FRONTEND_DIST.resolve())):
                    return FileResponse(str(candidate))
            except Exception:
                pass
        return FileResponse(str(_INDEX_HTML))

    print(f"[startup] Serving frontend SPA from {_FRONTEND_DIST}")
else:
    print("[startup] No frontend build found (set FRONTEND_DIST or run build.ps1/build.sh); API-only mode.")


if __name__ == "__main__":
    host, port = select_backend_host_port()
    print(f"Starting SNBI Bridge API on http://{host}:{port}...")
    reload_enabled = os.getenv("SNBI_BACKEND_RELOAD", "0") == "1"
    uvicorn.run(app, host=host, port=port, reload=reload_enabled)
