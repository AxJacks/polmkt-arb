"""Load and validate mappings.yaml. A pair is TRADABLE only if every required
field is present and human-verified. Missing metadata => raise (fail loud).

This is the single most important safety boundary in the system: two markets that
look identical can resolve differently. We refuse to treat any pair as tradable
unless the operator has recorded, by hand, the YES-definition, resolution source,
settlement time, and tie/void handling for BOTH venues.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


class MappingError(ValueError):
    """A mapping entry is missing required metadata or is malformed."""


@dataclass(frozen=True)
class VenueLeg:
    market_id: str         # kalshi ticker OR polyus slug
    yes_means: str         # the real-world outcome that "YES" denotes
    resolution_source: str
    settlement_time: datetime
    tie_void: str
    invert: bool = False   # native YES is the opposite of mapped outcome A


@dataclass(frozen=True)
class MappedPair:
    id: str
    kalshi: VenueLeg
    polyus: VenueLeg
    verified_by: str
    verified_at: str
    enabled: bool = True


def _parse_time(value, where: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise MappingError(f"{where}: bad settlement time {value!r}: {e}") from e
    else:
        raise MappingError(f"{where}: settlement time must be ISO-8601, got {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _require(d: dict, key: str, where: str):
    if key not in d or d[key] in (None, ""):
        raise MappingError(f"{where}: missing required field {key!r}")
    return d[key]


def _build_leg(raw, pair_id: str, side: str) -> VenueLeg:
    if not isinstance(raw, dict):
        raise MappingError(f"pair {pair_id}: {side} leg must be a mapping")
    where = f"pair {pair_id} [{side}]"
    return VenueLeg(
        market_id=str(_require(raw, "market_id", where)),
        yes_means=str(_require(raw, "yes_means", where)),
        resolution_source=str(_require(raw, "resolution_source", where)),
        settlement_time=_parse_time(_require(raw, "settlement_time", where), where),
        tie_void=str(_require(raw, "tie_void", where)),
        invert=bool(raw.get("invert", False)),
    )


def parse_pair(raw: dict) -> MappedPair:
    if not isinstance(raw, dict):
        raise MappingError("each mapping entry must be a YAML mapping")
    pair_id = _require(raw, "id", "mapping entry")
    return MappedPair(
        id=str(pair_id),
        kalshi=_build_leg(_require(raw, "kalshi", f"pair {pair_id}"), str(pair_id), "kalshi"),
        polyus=_build_leg(_require(raw, "polyus", f"pair {pair_id}"), str(pair_id), "polyus"),
        verified_by=str(_require(raw, "verified_by", f"pair {pair_id}")),
        verified_at=str(_require(raw, "verified_at", f"pair {pair_id}")),
        enabled=bool(raw.get("enabled", True)),
    )


def load_mappings(path: str) -> list[MappedPair]:
    p = Path(path)
    if not p.exists():
        raise MappingError(f"mappings file not found: {path}")
    data = yaml.safe_load(p.read_text()) or []
    if not isinstance(data, list):
        raise MappingError("mappings.yaml must be a YAML list of pairs")
    pairs = [parse_pair(entry) for entry in data]
    ids = [p.id for p in pairs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise MappingError(f"duplicate pair ids in mappings.yaml: {sorted(dupes)}")
    return pairs
