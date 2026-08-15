"""Deterministic KPI JSON/CSV/PDF export package."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any, Iterable
import zipfile

from app.services.spreadsheet_csv import spreadsheet_safe_value


EXPORT_FORMAT = "bridgeguard-kpi-export-v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _flatten(value: object, path: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _flatten(value[key], f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{path}/{index}")
    elif value is None or isinstance(value, (str, int, float, bool)):
        yield path or "/", value


def _csv_bytes(report: dict[str, object]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(("json_pointer", "value"))
    for path, value in _flatten(report):
        writer.writerow((spreadsheet_safe_value(path), spreadsheet_safe_value("" if value is None else value)))
    return stream.getvalue().encode("utf-8")


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_bytes(report: dict[str, object]) -> bytes:
    title = str(report.get("label") or "BridgeGuard KPI report")
    subtitle = f"Year {report.get('year', 'n/a')} | definition {report.get('definition_version', 'n/a')}"
    receipt = hashlib.sha256(_canonical(report)).hexdigest()
    lines = [title, subtitle, f"Report SHA-256: {receipt}", ""]
    for path, value in _flatten(report):
        if path.startswith("/registry"):
            continue
        text = f"{path}: {'null' if value is None else value}"
        while len(text) > 105:
            lines.append(text[:105])
            text = "  " + text[105:]
        lines.append(text)
    pages = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [[title]]
    objects: list[bytes] = []
    page_object_numbers: list[int] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_lines in pages:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_object_numbers.append(page_number)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>".encode("ascii")
        )
        commands = ["BT", "/F1 9 Tf", "44 750 Td", "12 TL"]
        for line in page_lines:
            commands.append(f"({_pdf_escape(str(line))}) Tj")
            commands.append("T*")
        commands.append("ET")
        content = "\n".join(commands).encode("latin-1", errors="replace")
        objects.append(b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Count {len(page_object_numbers)} /Kids [{' '.join(f'{number} 0 R' for number in page_object_numbers)}] >>".encode("ascii")
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(output)


def build_kpi_export(report: dict[str, object], *, export_kind: str) -> tuple[str, bytes, dict[str, object]]:
    kind = str(export_kind).strip().lower()
    if kind not in {"operational", "acceptable_condition", "age_condition_area", "program", "fhwa_tpm"}:
        raise ValueError("unsupported KPI export kind")
    report_json = _canonical(report)
    artifacts = {
        "report.json": report_json,
        "values.csv": _csv_bytes(report),
        "review.pdf": _pdf_bytes(report),
    }
    artifact_receipts = {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
        for name, content in sorted(artifacts.items())
    }
    manifest = {
        "format": EXPORT_FORMAT,
        "export_kind": kind,
        "definition_version": report.get("definition_version"),
        "data_generation": report.get("data_generation"),
        "revision_set_sha256": report.get("revision_set_sha256"),
        "report_sha256": hashlib.sha256(report_json).hexdigest(),
        "artifacts": artifact_receipts,
    }
    manifest_bytes = _canonical(manifest)
    artifacts["manifest.json"] = manifest_bytes
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(artifacts.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    payload = stream.getvalue()
    export_id = hashlib.sha256(payload).hexdigest()
    return export_id, payload, {**manifest, "export_id": export_id, "size_bytes": len(payload)}


__all__ = ["EXPORT_FORMAT", "build_kpi_export"]
