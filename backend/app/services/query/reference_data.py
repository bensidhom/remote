"""Small reviewed reference mappings safe to include in Bridgette prompts."""

from __future__ import annotations

from types import MappingProxyType


NEW_JERSEY_COUNTY_NAMES = MappingProxyType(
    {
        0: "Unknown",
        1: "Atlantic",
        3: "Bergen",
        5: "Burlington",
        7: "Camden",
        9: "Cape May",
        11: "Cumberland",
        13: "Essex",
        15: "Gloucester",
        17: "Hudson",
        19: "Hunterdon",
        21: "Mercer",
        23: "Middlesex",
        25: "Monmouth",
        27: "Morris",
        29: "Ocean",
        31: "Passaic",
        33: "Salem",
        35: "Somerset",
        37: "Sussex",
        39: "Union",
        41: "Warren",
    }
)


NEW_JERSEY_COUNTY_PROMPT_ROWS = tuple(
    {"county_code": code, "county_name": name}
    for code, name in NEW_JERSEY_COUNTY_NAMES.items()
)


__all__ = ["NEW_JERSEY_COUNTY_NAMES", "NEW_JERSEY_COUNTY_PROMPT_ROWS"]
