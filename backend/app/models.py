# backend/app/models.py

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime


# ====================== RESPONSE MODELS ======================

class ConvertResponse(BaseModel):
    status: str = "success"
    bridges: List[Dict[str, Any]] = Field(default_factory=list)
    converted: int
    skipped: int


class LoadResponse(BaseModel):
    status: str = "success"
    loaded: int
    message: Optional[str] = None


class BridgeLocation(BaseModel):
    bridge_name: Optional[str] = None
    bridge_number: str
    latitude: float
    longitude: float
    state_code: int


class MapDataResponse(BaseModel):
    status: str = "success"
    bridges: List[BridgeLocation] = Field(default_factory=list)


class KpiRecord(BaseModel):
    state_code: int
    state_name: str
    total_bridges: int
    good_bridges: int
    fair_bridges: int
    poor_bridges: int
    avg_age: Optional[float] = None
    total_deck_area: float
    high_traffic_bridges: int
    old_bridges: int
    pct_good: float
    pct_fair: float
    pct_poor: float
    pct_old: float


class KpiResponse(BaseModel):
    status: str = "success"
    kpis: List[KpiRecord] = Field(default_factory=list)


class TableDataResponse(BaseModel):
    status: str = "success"
    table_name: str
    data: List[Dict[str, Any]] = Field(default_factory=list)
    count: int


# ====================== REQUEST MODELS ======================

class BridgeIdentifier(BaseModel):
    state_code: int
    bridge_number: str


class UpdateBridgeIdRequest(BaseModel):
    old_state_code: int
    old_bridge_number: str
    new_state_code: int
    new_bridge_number: str


# ====================== SNBI MODELS (Optional - for validation) ======================

class SnbiFeatureRoute(BaseModel):
    BRT01: str
    BRT02: str
    BRT03: Optional[str] = None
    BRT04: Optional[str] = None
    BRT05: Optional[str] = None


class SnbiFeature(BaseModel):
    BF01: str
    BF02: str
    BF03: Optional[str] = None
    BH01: Optional[str] = None
    BH02: Optional[str] = None
    BH06: Optional[str] = None
    BH09: Optional[int] = None
    Routes: Optional[List[SnbiFeatureRoute]] = None


class SnbiSpanSet(BaseModel):
    BSP01: str
    BSP02: int
    BSP04: Optional[str] = None
    BSP06: Optional[str] = None
    BSP09: Optional[str] = None


class SnbiElement(BaseModel):
    BE01: str
    BE02: str = "0"
    BE03: str = "0"
    BCS01: str = "0"
    BCS02: str = "0"
    BCS03: str = "0"
    BCS04: str = "0"


class SnbiWork(BaseModel):
    BW02: int
    BW03: str


class SnbiBridge(BaseModel):
    BL01: int
    BID01: str
    BL05: Optional[float] = None
    BL06: Optional[float] = None
    BW01: Optional[int] = None
    BC01: Optional[str] = None
    BC02: Optional[str] = None
    BC03: Optional[str] = None
    Features: Optional[List[SnbiFeature]] = None
    SpanSets: Optional[List[SnbiSpanSet]] = None
    Elements: Optional[List[SnbiElement]] = None
    Works: Optional[List[SnbiWork]] = None


class SnbiBridgeList(BaseModel):
    bridges: List[SnbiBridge] = Field(default_factory=list)