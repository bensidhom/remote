from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from app import main as main_module
from app.routers.services import rag_engine, vision
from app.services.ai.context import AiAnalysisMode, AiRequestContext
from app.services.http_limits import RequestBodyLimitMiddleware


def _context(*, cancellation: threading.Event | None = None) -> AiRequestContext:
    return AiRequestContext(
        request_id=str(uuid4()),
        owner_id="document-user@example.com",
        workspace_id="document-workspace",
        conversation_id=None,
        mode=AiAnalysisMode.TEXT,
        operation="document_ingest_query",
        state_code=None,
        bridge_number=None,
        data_year=None,
        bridge_identity=None,
        deadline_monotonic=time.monotonic() + 60,
        cancellation_token=cancellation or threading.Event(),
    )


def _upload(
    name: str,
    body: bytes,
    content_type: str = "application/pdf",
) -> UploadFile:
    return UploadFile(
        filename=name,
        file=io.BytesIO(body),
        headers=Headers({"content-type": content_type}),
    )


def test_upload_is_chunked_opaque_request_owned_and_cleanup_is_exact(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = test_workspace / "uploads"
    monkeypatch.setattr(vision, "DOCUMENT_UPLOAD_ROOT", root)
    monkeypatch.setattr(vision, "DOCUMENT_UPLOAD_CHUNK_BYTES", 3)

    path = asyncio.run(
        vision.save_upload_to_temp(
            _upload("../../shared-name.pdf", b"%PDF-1.7\nbody"),
            request_context=_context(),
        )
    )

    assert path.read_bytes() == b"%PDF-1.7\nbody"
    assert path.name != "shared-name.pdf"
    assert path.suffix == ".pdf"
    assert path.parent.parent.resolve() == root.resolve()
    assert len(path.stem) == 36

    vision._cleanup_uploaded_temp(path)
    assert not path.exists()
    assert not path.parent.exists()


@pytest.mark.parametrize(
    ("name", "body", "media_type", "status", "code"),
    [
        ("report.txt", b"%PDF-1.7", "application/pdf", 415, "unsupported_document_type"),
        ("report.pdf", b"%PDF-1.7", "text/plain", 415, "unsupported_document_media_type"),
        ("report.pdf", b"not a pdf", "application/pdf", 415, "invalid_pdf_signature"),
        ("report.pdf", b"", "application/pdf", 422, "empty_document"),
    ],
)
def test_upload_rejects_extension_media_magic_and_empty_files_without_residue(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    body: bytes,
    media_type: str,
    status: int,
    code: str,
):
    root = test_workspace / "uploads"
    monkeypatch.setattr(vision, "DOCUMENT_UPLOAD_ROOT", root)

    with pytest.raises(vision.DocumentUploadError) as error:
        asyncio.run(
            vision.save_upload_to_temp(
                _upload(name, body, media_type), request_context=_context(),
            )
        )

    assert error.value.status_code == status
    assert error.value.code == code
    assert not root.exists() or list(root.iterdir()) == []


def test_oversized_upload_stops_at_bound_and_removes_partial_file(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = test_workspace / "uploads"
    monkeypatch.setattr(vision, "DOCUMENT_UPLOAD_ROOT", root)
    monkeypatch.setattr(vision, "DOCUMENT_UPLOAD_CHUNK_BYTES", 4)
    monkeypatch.setattr(vision, "DOCUMENT_MAX_UPLOAD_BYTES", 8)

    with pytest.raises(vision.DocumentUploadError) as error:
        asyncio.run(
            vision.save_upload_to_temp(
                _upload("large.pdf", b"%PDF-123456789"), request_context=_context(),
            )
        )

    assert error.value.status_code == 413
    assert error.value.code == "document_too_large"
    assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.parametrize("fail", [False, True])
def test_process_upload_always_removes_request_temp_file(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
):
    root = test_workspace / "uploads"
    observed: list[Path] = []
    monkeypatch.setattr(vision, "DOCUMENT_UPLOAD_ROOT", root)

    async def fake_process(path: Path, *_args, **_kwargs):
        observed.append(path)
        assert path.exists()
        if fail:
            raise RuntimeError("parser failed")
        return {"status": "success"}

    monkeypatch.setattr(vision, "_process_vision_rag_from_path", fake_process)
    operation = vision.process_vision_rag(
        _upload("report.pdf", b"%PDF-1.7\nbody"),
        "summarize",
        request_context=_context(),
    )
    if fail:
        with pytest.raises(RuntimeError, match="parser failed"):
            asyncio.run(operation)
    else:
        assert asyncio.run(operation) == {"status": "success"}

    assert len(observed) == 1
    assert not observed[0].exists()
    assert not observed[0].parent.exists()


class _FakePdf:
    def __init__(self, pages):
        self.pages = list(pages)
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True

    def extract_image(self, _xref):
        return None


class _TextPage:
    def __init__(self, text: str = "text"):
        self.text = text
        self.rect = SimpleNamespace(x0=0, y0=0, x1=612, y1=792, width=612, height=792)

    def get_text(self, kind):
        if kind == "text":
            return self.text
        return {"blocks": []}

    def get_images(self, full=True):
        return []


def _fake_pymupdf(pdf: _FakePdf):
    return SimpleNamespace(
        open=lambda _path: pdf,
        Matrix=lambda *args: args,
        Rect=lambda x0, y0, x1, y1: SimpleNamespace(
            x0=x0, y0=y0, x1=x1, y1=y1,
            width=x1 - x0, height=y1 - y0,
        ),
    )


def test_pdf_page_and_text_caps_fail_before_unbounded_processing(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(rag_engine, "RAG_WORKING_DIR", str(test_workspace))

    too_many_pages = _FakePdf([_TextPage(), _TextPage(), _TextPage()])
    monkeypatch.setattr(rag_engine, "PDF_MAX_PAGES", 2)
    monkeypatch.setattr(rag_engine, "_get_pymupdf_module", lambda: _fake_pymupdf(too_many_pages))
    with pytest.raises(rag_engine.DocumentLimitError) as pages_error:
        rag_engine._extract_text_and_images("ignored.pdf", "a" * 64)
    assert pages_error.value.code == "pdf_page_limit"
    assert too_many_pages.closed is True

    text_bomb = _FakePdf([_TextPage("x" * 11)])
    monkeypatch.setattr(rag_engine, "PDF_MAX_PAGES", 2)
    monkeypatch.setattr(rag_engine, "PDF_MAX_EXTRACTED_TEXT_CHARS", 10)
    monkeypatch.setattr(rag_engine, "_get_pymupdf_module", lambda: _fake_pymupdf(text_bomb))
    with pytest.raises(rag_engine.DocumentLimitError) as text_error:
        rag_engine._extract_text_and_images("ignored.pdf", "b" * 64)
    assert text_error.value.code == "pdf_text_limit"
    assert text_bomb.closed is True


def test_pdf_render_pixel_cap_fails_before_pixmap_allocation(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    page = _TextPage()
    page.get_pixmap = lambda **_kwargs: pytest.fail("pixmap allocated before pixel check")
    pdf = _FakePdf([page])
    monkeypatch.setattr(rag_engine, "RAG_WORKING_DIR", str(test_workspace))
    monkeypatch.setattr(rag_engine, "PDF_MAX_PAGE_PIXELS", 1_000_000)
    monkeypatch.setattr(rag_engine, "_get_pymupdf_module", lambda: _fake_pymupdf(pdf))

    with pytest.raises(rag_engine.DocumentLimitError) as error:
        rag_engine._render_pdf_pages("ignored.pdf", "c" * 64, dpi=300)

    assert error.value.code == "pdf_pixel_limit"
    assert pdf.closed is True


def test_pdf_parser_checks_request_cancellation_inside_page_loop(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cancellation = threading.Event()

    class CancellingPage(_TextPage):
        def get_text(self, kind):
            value = super().get_text(kind)
            if kind == "text":
                cancellation.set()
            return value

    pdf = _FakePdf([CancellingPage(), _TextPage("must not be read")])
    monkeypatch.setattr(rag_engine, "RAG_WORKING_DIR", str(test_workspace))
    monkeypatch.setattr(rag_engine, "_get_pymupdf_module", lambda: _fake_pymupdf(pdf))

    with pytest.raises(rag_engine.CancelledError):
        rag_engine._extract_text_and_images(
            "ignored.pdf", "d" * 64, cancellation,
        )
    assert pdf.closed is True


def test_ingest_runs_parser_and_renderer_off_event_loop(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = test_workspace / "input.pdf"
    source.write_bytes(b"%PDF-1.7\nfake")
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fake_extract(_path, _hash, _cancellation):
        worker_threads.append(threading.get_ident())
        return ([{"page": 1, "text": "bounded text"}], [])

    def fake_render(_path, _hash, _dpi, _cancellation):
        worker_threads.append(threading.get_ident())
        return []

    async def fake_kg(_hash, _text):
        return False

    monkeypatch.setattr(rag_engine, "_ingested", {})
    monkeypatch.setattr(rag_engine, "_active_session_hashes", {})
    monkeypatch.setattr(rag_engine, "_load_manifest", lambda: {})
    monkeypatch.setattr(rag_engine, "_extract_text_and_images", fake_extract)
    monkeypatch.setattr(rag_engine, "_render_pdf_pages", fake_render)
    monkeypatch.setattr(rag_engine, "_compute_embeddings", lambda *_args: None)
    monkeypatch.setattr(rag_engine, "_build_kg", fake_kg)
    monkeypatch.setattr(rag_engine, "_save_manifest", lambda: None)

    result = asyncio.run(
        rag_engine.ingest_document(
            str(source),
            cancellation=threading.Event(),
            owner_id="document-user@example.com",
            workspace_id="document-workspace",
        )
    )

    assert result["status"] == "success"
    assert len(worker_threads) == 2
    assert all(thread_id != caller_thread for thread_id in worker_threads)


def test_show_all_derivatives_enforces_item_and_encoded_byte_budgets(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    paths = []
    for index in range(6):
        path = test_workspace / f"page_{index}.png"
        path.write_bytes(bytes([index + 1]) * 6)
        paths.append(str(path))
    oversized = test_workspace / "oversized.png"
    oversized.write_bytes(b"x" * 20)

    monkeypatch.setattr(rag_engine, "DERIVATIVE_RESPONSE_MAX_IMAGES", 3)
    monkeypatch.setattr(rag_engine, "DERIVATIVE_RESPONSE_MAX_BYTES", 20)
    monkeypatch.setattr(rag_engine, "DERIVATIVE_RESPONSE_MAX_SINGLE_BYTES", 10)
    monkeypatch.setattr(
        rag_engine,
        "_ingested",
        {
            "doc": {
                "page_images": [str(oversized), *paths],
                "extracted_images": [
                    {"path": path, "ext": "png", "page": index + 1}
                    for index, path in enumerate(paths)
                ],
            }
        },
    )

    pages = rag_engine.get_all_page_images_b64("doc")
    images = rag_engine.get_all_extracted_images_b64("doc", limit=99)

    assert len(pages) == 2
    assert len(images) == 2
    assert sum(len(item["data"]) for item in pages) <= 20
    assert sum(len(item["data"]) for item in images) <= 20
    assert all(item["filename"] != oversized.name for item in pages)


def test_parser_input_validation_rejects_spoofed_and_oversized_files(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    spoofed = test_workspace / "spoofed.pdf"
    spoofed.write_bytes(b"not-pdf")
    with pytest.raises(rag_engine.DocumentFormatError) as format_error:
        rag_engine._validate_pdf_input(str(spoofed))
    assert format_error.value.code == "invalid_pdf_signature"

    large = test_workspace / "large.pdf"
    large.write_bytes(b"%PDF-" + b"x" * 12)
    monkeypatch.setattr(rag_engine, "PDF_MAX_INPUT_BYTES", 8)
    with pytest.raises(rag_engine.DocumentLimitError) as size_error:
        rag_engine._validate_pdf_input(str(large))
    assert size_error.value.code == "document_too_large"


def test_document_http_route_returns_bounded_client_error_not_generic_500():
    context = _context()
    request = Request({"type": "http", "headers": []})
    rejection = vision.DocumentUploadError(
        "PDF exceeds the upload limit.",
        status_code=413,
        code="document_too_large",
    )
    with (
        patch.object(main_module, "_register_ai_request", return_value=context),
        patch.object(main_module, "_append_conversation_user_turn"),
        patch.object(
            main_module, "process_vision_rag", AsyncMock(side_effect=rejection),
        ),
        patch.object(main_module.DEFAULT_AI_REQUEST_REGISTRY, "finish") as finish,
    ):
        response = asyncio.run(
            main_module.vision_rag(
                request=request,
                file=_upload("large.pdf", b"%PDF-large"),
                question="summarize",
                model="",
                strict_grounding=False,
                output_format="text",
                target_fields="",
                analysis_mode="text",
                defect_segmentation_mode="balanced",
                conversation_id=None,
            )
        )

    assert response.status_code == 413
    assert json.loads(response.body) == {
        "status": "error",
        "code": "document_too_large",
        "detail": "PDF exceeds the upload limit.",
    }
    assert finish.call_args.kwargs["evidence"]["errors"] == [
        {"code": "document_too_large"}
    ]


@pytest.mark.parametrize("with_length", [True, False])
def test_transport_body_limit_runs_before_multipart_spooling(with_length: bool):
    downstream_called = False

    async def downstream(_scope, receive, send):
        nonlocal downstream_called
        downstream_called = True
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestBodyLimitMiddleware(
        downstream, path_limits={"/api/vision-rag": 8},
    )
    messages = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    headers = [(b"content-length", b"10")] if with_length else []
    asyncio.run(
        middleware(
            {
                "type": "http",
                "path": "/api/vision-rag",
                "headers": headers,
            },
            receive,
            send,
        )
    )

    assert sent[0]["status"] == 413
    assert b"request_body_too_large" in sent[-1]["body"]
    assert downstream_called is (not with_length)
