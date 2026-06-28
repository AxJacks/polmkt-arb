"""Candidate pair suggester (Claude Haiku). Proposes Kalshi<->Polymarket US pairs
by matching titles, writing them to candidates.yaml with a confidence note and the
fields that differ. NOTHING here is tradable: a suggestion only becomes tradable
when the operator hand-verifies it and copies a fully-specified entry into
mappings.yaml. The detector/executor never read candidates.yaml as tradable.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import yaml

log = logging.getLogger("arb.suggester")

SYSTEM = """You match prediction-market contracts across two venues (Kalshi and
Polymarket US) that resolve to the SAME real-world outcome. Be conservative: only
propose a pair when the underlying event, threshold, and timeframe plausibly
match. For every proposal, list the fields that may DIFFER and must be verified by
a human (resolution source, exact threshold, settlement time, tie/void handling).
Return ONLY JSON: a list of objects with keys: kalshi_ticker, polyus_slug,
confidence (0..1), title_kalshi, title_polyus, differing_fields (list of strings).
Never claim two markets are identical; you are proposing candidates for review."""


def _market_brief(m: dict, venue: str) -> dict:
    if venue == "kalshi":
        return {"ticker": m.get("ticker"), "title": m.get("title") or m.get("yes_sub_title") or m.get("subtitle")}
    return {"slug": m.get("marketSlug") or m.get("slug"), "title": m.get("title") or m.get("question")}


def suggest(cfg, kalshi_markets: list[dict], polyus_markets: list[dict],
            model: str | None = None) -> list[dict]:
    from anthropic import Anthropic

    if not cfg.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = Anthropic(api_key=cfg.anthropic_api_key)
    model = model or cfg.suggester_model

    k_brief = [_market_brief(m, "kalshi") for m in kalshi_markets]
    p_brief = [_market_brief(m, "polyus") for m in polyus_markets]

    user = (
        "KALSHI markets:\n" + json.dumps(k_brief, indent=0)
        + "\n\nPOLYMARKET US markets:\n" + json.dumps(p_brief, indent=0)
        + "\n\nReturn the JSON list of candidate matches."
    )
    resp = client.messages.create(
        model=model, max_tokens=4096, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip()
    try:
        matches = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Haiku returned non-JSON: %s", e)
        return []
    out = []
    for m in matches:
        m["model"] = model
        m["suggested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        m["status"] = "needs_review"
        out.append(m)
    return out


def write_candidates(path: str, candidates: list[dict]) -> None:
    p = Path(path)
    existing = []
    if p.exists():
        existing = yaml.safe_load(p.read_text()) or []
    # de-dupe by (kalshi_ticker, polyus_slug)
    seen = {(c.get("kalshi_ticker"), c.get("polyus_slug")) for c in existing}
    for c in candidates:
        key = (c.get("kalshi_ticker"), c.get("polyus_slug"))
        if key not in seen:
            existing.append(c)
            seen.add(key)
    p.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
    log.info("wrote %d candidates to %s (%d total)", len(candidates), path, len(existing))


def main() -> None:
    import argparse
    import logging as _logging

    from config import load_config
    from .store import Store
    from .venues.kalshi import KalshiClient
    from .venues.polyus import PolyUSClient

    ap = argparse.ArgumentParser(description="Suggest candidate pairs (writes candidates.yaml, never tradable)")
    ap.add_argument("--max", type=int, default=300, help="max markets per venue to consider")
    args = ap.parse_args()
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    cfg = load_config()
    k = KalshiClient(cfg).list_markets(max=args.max)
    p = PolyUSClient(cfg).list_markets(limit=args.max)
    cands = suggest(cfg, k, p)
    write_candidates(cfg.candidates_path, cands)
    store = Store(cfg.db_path)
    for c in cands:
        store.record_candidate(c)


if __name__ == "__main__":
    main()
