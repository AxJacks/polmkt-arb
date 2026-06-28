# polmkt-arb — Kalshi ⇄ Polymarket US cross-platform arbitrage engine

A two-phase system for **fee-adjusted arbitrage** between Kalshi and Polymarket
US, both CFTC-regulated US venues with official trading APIs.

- **Phase 1 — detector (built, this release).** Compares prices for the *same*
  event across both venues and alerts when a fee-adjusted edge exists. **No order
  placement anywhere.**
- **Phase 2 — executor (not built yet).** Will place both legs atomically behind
  hard safety gates. Off by default. The code seams exist (`src/venues/base.py`
  order stubs, the `orders`/`paper_fills`/`positions`/`pnl_daily` tables) so it
  slots in without refactoring Phase 1.

---

## ⚖️ Legal scope (read this)

- **Execution venues: Kalshi and Polymarket US ONLY.** Both are CFTC-regulated
  and legal for a US person (PA) to trade via their official APIs with your own
  funds.
- **Polymarket Global (the on-chain Polygon platform) is NEVER traded.** It is
  geoblocked and its terms prohibit US persons from trading via UI, API, or
  agents. This repo contains **no** Global order placement and **no** VPN / geo
  evasion. Global may only ever be read for price reference, never traded.
- This software trades real money. The danger is financial — a bug, a leg that
  doesn't fill, or two markets that aren't actually the same. The design is
  defensive around exactly those failures.

---

## The core discipline: market mapping

Two contracts that *look* identical can resolve differently (different source,
expiry, or tie/void rules). A mismatch turns a hedge into a double-sided loss.

- A pair is **tradable only if it exists in `mappings.yaml`**, fully specified by
  hand: each side's market id, the YES-definition (which real outcome = YES),
  resolution source, settlement time, and tie/void handling. The loader
  (`src/mapping.py`) **raises** on any missing field.
- The **Haiku suggester** (`src/suggester.py`) proposes candidate pairs into
  `candidates.yaml` with a confidence note and the fields that differ.
  **Candidates are never tradable** — you promote one only by hand-verifying it
  and copying a complete entry into `mappings.yaml`.
- At evaluation time the detector also **skips** any pair within
  `SETTLEMENT_WINDOW_S` of either leg's settlement, or whose feed is stale.

---

## Verified API surfaces (2026-06)

| | Kalshi | Polymarket US |
|---|---|---|
| REST base | `https://external-api.kalshi.com/trade-api/v2` | `https://api.polymarket.us/v1` |
| WebSocket | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (`orderbook_delta`) | `wss://api.polymarket.us/v1/ws/markets` (`SUBSCRIPTION_TYPE_MARKET_DATA`) |
| Auth | RSA-PSS/SHA256; headers `KALSHI-ACCESS-{KEY,TIMESTAMP,SIGNATURE}`; sign `ts+METHOD+path` | Ed25519; headers `X-PM-{Access-Key,Timestamp,Signature}`; sign `ts+METHOD+path` (body not signed) |
| Fee | `ceil_to_cent(θ·C·p·(1-p))`, θ per-series (`fee_multiplier`, ~0.07 std, higher sports) | `bankers_round(θ·C·p·(1-p))`, taker θ=0.05, **maker θ=−0.0125 (rebate)** |

> **Fee reality check.** Polymarket US taker fees are **~1.25% at p=0.50** (not the
> ~0.01% sometimes reported). Combined both-taker fees near 50/50 are ≈ **$0.03
> per matched contract**, so a pure-taker arb needs **>~3% gross divergence** to
> clear. Resting the Poly leg as a **maker earns a rebate**, which is why the
> detector reports both a `taker_edge` (alert gate) and a `maker_edge` (upper
> bound). Kalshi's θ is pulled live per series and **never assumed** — missing fee
> data raises.

WebSocket field parsing (esp. Kalshi `_fp` sizes / delta shape and whether Poly
US exposes an independent NO book) is implemented defensively and marked in code;
verify against live payloads on first connect.

---

## Setup

```bash
cd polmkt-arb
python3.13 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env          # fill in keys (never commit .env)
cp mappings.example.yaml mappings.yaml   # then curate BY HAND
```

Credentials (env only, never in code):

- **Kalshi:** `KALSHI_API_KEY_ID` + an RSA private key file at
  `KALSHI_PRIVATE_KEY_PATH` (PEM, `chmod 600`).
- **Polymarket US:** `POLYUS_KEY_ID` + `POLYUS_SECRET_KEY` (base64), minted at
  `polymarket.us/developer` after in-app KYC.
- **Suggester:** `ANTHROPIC_API_KEY` (model `claude-haiku-4-5`).

---

## Running Phase 1

**Prove it offline first (no keys, synthetic feed):**

```bash
./.venv/bin/python scripts/demo_synthetic.py
```

This drives the real `Detector` through an edge appearing → persisting → closing,
and prints the `edges` table with persistence timing plus a written `recos.xlsx`.

**Live detector** (needs keys + a non-empty `mappings.yaml`):

```bash
./.venv/bin/python -m src.detector --log-level INFO
```

It subscribes to top-of-book for every mapped market on both venues, maintains
best bid/ask + size with a staleness timestamp, computes per-pair edges every
`EDGE_POLL_MS`, logs to `arb.log`, writes actionable edges to `recos.xlsx`
(+ optional `ALERT_WEBHOOK_URL`), and records persistence timing in `arb.db`.

**Candidate suggester** (writes `candidates.yaml`, never tradable):

```bash
./.venv/bin/python -m src.suggester --max 300
```

**Tests:**

```bash
./.venv/bin/python -m pytest -q
```

---

## Configuration & limits

All config is env / `.env` (see `config.py`). Phase 1 keys:

| Key | Default | Meaning |
|---|---|---|
| `MIN_EDGE` | 0.02 | actionable taker edge after fees + slippage buffer |
| `MIN_SIZE` | 10 | minimum matched contracts to alert |
| `SLIPPAGE_BUFFER` | 0.005 | subtracted from edge before "actionable" |
| `STALENESS_MS` | 2000 | feed older than this ⇒ pair untradeable + alert |
| `SETTLEMENT_WINDOW_S` | 3600 | skip pairs within this of either settlement |
| `EDGE_POLL_MS` | 500 | edge recompute cadence |

Phase 2 limits (present, **enforced when the executor is built**; trading stays
off until then): `ARMED`, `MODE` (paper/live), `MAX_CONTRACTS_PER_MARKET`,
`MAX_TOTAL_CAPITAL`, `MAX_CONCURRENT_ARBS`, `MAX_ORDER_SIZE`, `MAX_DAILY_LOSS`
(kill-switch trip), `LEG2_TIMEOUT_MS`, `KILL_SWITCH_FILE`.

---

## Phase 2 plan (NOT built — do not enable)

When you approve building it:

- **paper mode (default):** simulate fills against live books, track PnL after
  real fees in SQLite. Must run end-to-end before live is allowed.
- **live mode:** refuses to start unless **all** of `--live` flag **+**
  `ARMED=true` **+** funded balances confirmed on both venues **+** a non-empty
  verified `mappings.yaml`.
- **both-legs-or-cancel state machine:** place the thinner leg first as limit IOC;
  on fill, fire the hedge as taker IOC; if leg 2 misses within `LEG2_TIMEOUT_MS`,
  unwind leg 1 at market and log the realized cost. Client order ids; crash-
  recoverable order state.
- **hard limits in code**, `MAX_DAILY_LOSS` kill switch, sentinel-file kill
  switch checked before every order, startup reconciliation of open orders /
  positions, never auto-move funds between venues.

### How to arm / disarm (future)

Arming will require *all* of: launch with `--live`, set `ARMED=true`, confirmed
funded balances on both venues, and a non-empty `mappings.yaml`. Disarm by
removing any one gate, or instantly via the kill switch: `touch ./HALT` (the
path in `KILL_SWITCH_FILE`) halts all new orders; deleting the file re-enables.

---

## What this repo will never do

Trade on fuzzy/auto title matches · place orders on Polymarket Global · add any
VPN/geo-evasion · enable live trading before paper mode + tested limits/kill
switch · hardcode keys · move funds between venues automatically · add leverage,
martingale, or any position-growing-on-losses logic.
