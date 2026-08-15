"""Immutable identity shared by every analytical result."""

from __future__ import annotations

from dataclasses import dataclass
import re


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256(value: str, label: str) -> str:
    normalized = str(value).strip().lower()
    if not _SHA256.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


@dataclass(frozen=True, slots=True)
class AnalysisSnapshotIdentity:
    """Exact published population and analytical specification identity."""

    workspace_id: str
    state_code: int
    data_year: int
    revision_set_sha256: str
    data_generation: int
    schema_generation: int
    specification_sha256: str

    def __post_init__(self) -> None:
        workspace = str(self.workspace_id).strip()
        if not workspace:
            raise ValueError("workspace_id is required")
        state = int(self.state_code)
        year = int(self.data_year)
        if state < 1 or state > 99:
            raise ValueError("state_code must be between 1 and 99")
        if year < 1900 or year > 2200:
            raise ValueError("data_year is outside the supported range")
        if int(self.data_generation) < 0 or int(self.schema_generation) < 0:
            raise ValueError("generation values must be non-negative")
        object.__setattr__(self, "workspace_id", workspace)
        object.__setattr__(self, "state_code", state)
        object.__setattr__(self, "data_year", year)
        object.__setattr__(
            self,
            "revision_set_sha256",
            _require_sha256(self.revision_set_sha256, "revision_set_sha256"),
        )
        object.__setattr__(
            self,
            "specification_sha256",
            _require_sha256(self.specification_sha256, "specification_sha256"),
        )

    @property
    def cache_key(self) -> tuple[str, int, int, str, int, int, str]:
        return (
            self.workspace_id,
            self.state_code,
            self.data_year,
            self.revision_set_sha256,
            self.data_generation,
            self.schema_generation,
            self.specification_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "state_code": self.state_code,
            "data_year": self.data_year,
            "revision_set_sha256": self.revision_set_sha256,
            "data_generation": self.data_generation,
            "schema_generation": self.schema_generation,
            "specification_sha256": self.specification_sha256,
        }
