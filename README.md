# kraken-arb-bot

Triangular arbitrage scanner and executor for Kraken. It walks every 3-leg loop
that starts and ends in your quote asset (`USD -> BTC -> ETH -> USD`), prices
each leg against **real order book depth**, charges **real fees**, and only
trades when what is left over clears a threshold you set.

It runs in three modes, and you have to walk through them in order.

| Mode | `TRADE_ENABLED` | `LIVE` | What happens |
|---|---|---|---|
| **Scan-only** (default) | `false` | `false` | Detects and logs edges. No keys needed. No orders. |
| **Paper** | `true` | `false` | Full execution path, but every order carries Kraken's `validate` flag — Kraken checks your parameters and places nothing. |
| **Live** | `true` | `true` | Real orders, real money. Requires a third confirmation string. |

---

## Read this before anything else

**Triangular arbitrage on a single exchange is, most of the time, not
profitable.** That is not pessimism, it is arithmetic, and the bot is built to
show you rather than tell you.

Kraken's tier-0 taker fee is 0.26% per leg. Three legs compounds to:

```
1 - (1 - 0.0026)^3  =  0.00778  =  ~78 basis points
```

So a triangle has to be mispriced by **more than 0.78%** before you break even —
and that mispricing has to survive the two or three hundred milliseconds between
your scan and your third fill. Market makers with colocated infrastructure and
0% maker fees close these gaps continuously; by the time a retail REST/websocket
loop sees one, it is usually a stale quote rather than an opportunity.

Here is a real 40-second run from this repo, 80 triangles across 60 symbols:

```
          -100 to -50bps :     74  ( 92.5%)
           -50 to -20bps :      3  (  3.8%)
       no fillable cycle :      3  (  3.8%)

best single observation: -22.99bps on USD -> USDC -> USDG -> USD
Nothing cleared 8.0bps net of fees.
```

The best opportunity on the exchange was still a **23 basis point loss**. This is
the normal result. Run `scan.py` yourself for an hour before you consider putting
money behind it.

What would have to change for this to work:

- **Fee tier.** At Kraken's top volume tiers taker drops toward 0.10%, cutting
  the hurdle to ~30bps. That requires millions in 30-day volume.
- **Speed.** You are competing on latency. Python over a residential connection
  is not where that competition is won.
- **A different strategy.** Cross-exchange arbitrage has wider and more
  persistent spreads — but needs capital pre-funded on both venues, and the
  spread usually reflects real transfer risk and withdrawal cost rather than free
  money.

Use this as an instrument for measuring market microstructure. Treat any live
trading as tuition.

---

## Quickstart

```bash
cd kraken-arb-bot && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
```

Look before you touch keys — this needs no credentials and places nothing:

```bash
.venv\Scripts\python scan.py 300
```

Then copy `.env.example` to `.env` and configure. To run the bot:

```bash
.venv\Scripts\python main.py
```

Tests (no network, no keys):

```bash
.venv\Scripts\python tests\test_arb.py
```

---

## Going live

Deliberately awkward, in this order:

1. Run `scan.py` for a long window. If nothing clears `MIN_PROFIT_BPS`, stop
   here — the rest of this section will only cost you money.
2. Create a Kraken API key with **Query Funds** and **Create & Modify Orders**.
   **Do not enable withdrawal permissions.** Put it in `.env`.
3. Set `TRADE_ENABLED=true`, leave `LIVE=false`. Run for a day. Compare the
   `expected_bps` and realised `pnl` columns in `logs/cycles.ndjson` — paper mode
   assumes you get the fill the scanner predicted, and the gap between that
   assumption and reality is exactly what will hurt you.
4. Only then: `LIVE=true` **and** `CONFIRM_LIVE_TRADING=I UNDERSTAND THE RISK`.
   Start with `TRADE_SIZE` you would not mind losing entirely.

**Kill switch:** create an empty file named `STOP` in the project root. The bot
notices within one scan interval and shuts down. It also refuses to start a new
cycle while that file exists.

---

## How it works

```
kraken.py    REST client — HMAC-SHA512 signing, retry rules that never
             replay an AddOrder
universe.py  AssetPairs -> liquidity filter -> asset graph -> every 3-leg
             cycle in both directions
book.py      websocket v2 `book` channel -> local depth-N books, thread-safe
arb.py       the math: depth walking, fee charging, edge in basis points
risk.py      limits that can only ever say no
execute.py   leg-by-leg IOC execution with real fill accounting
bot.py       scan/confirm/execute loop
```

Four details that matter more than the rest:

**Depth walking, not top-of-book.** Naive scanners price each leg at the best
quote and report edges that vanish the moment you try to fill them, because the
top level held $40 and you wanted $500. `arb.consume_asks` / `consume_bids` walk
actual levels and return `None` rather than a number they cannot fill.

**Sizing from actual fills.** A triangle is not atomic — three orders go out in
sequence. Each leg is sized from what the *previous* leg actually returned, never
from what the scan predicted.

**IOC everywhere.** Every leg is immediate-or-cancel. A resting limit order that
does not fill turns your arbitrage into an unhedged directional position; IOC
either crosses now or cancels.

**Re-confirmation before sending.** The edge that triggers execution is
re-evaluated against the live book at the exact size about to be sent. If it has
decayed below threshold, the trade is dropped. In practice this rejects most
candidates, which is the point.

### When a cycle breaks halfway

If leg 2 fills and leg 3 does not, you are holding an asset you did not want.
With `UNWIND_ON_FAILURE=true` (default) the bot immediately market-sells back
toward the start asset, taking a small known loss instead of an open unknown one.
With it off, it logs loudly and leaves the position for you — only do that if you
are watching the screen.

Either way the cycle is recorded to `logs/cycles.ndjson` and a cooldown starts.

---

## Configuration

Everything lives in `.env`; see `.env.example` for the full annotated list.

**Detection**
| Key | Default | Notes |
|---|---|---|
| `START_ASSETS` | `USD` | Assets a cycle starts/ends in. You must hold one. |
| `MIN_PROFIT_BPS` | `8` | Net edge required, after fees and depth. |
| `MAX_PAIRS` | `180` | Track only the N most liquid pairs. |
| `MIN_PAIR_VOLUME` | `250000` | Skip pairs below this 24h quote volume. |
| `MAX_BOOK_AGE_MS` | `1500` | Reject stale quotes. |
| `TAKER_FEE_BPS` | auto | Blank = read your real fee tier from the account. |

**Risk**
| Key | Default | Notes |
|---|---|---|
| `TRADE_SIZE` | `50` | Start-asset amount per cycle. |
| `MAX_DAILY_LOSS` | `25` | Bot stops trading for the day when hit. |
| `MAX_CYCLES_PER_HOUR` | `12` | Rate cap. |
| `COOLDOWN_AFTER_FAILURE_SECONDS` | `60` | Pause after any broken cycle. |
| `SLIPPAGE_BPS` | `5` | Buffer so IOC legs actually cross. |
| `UNWIND_ON_FAILURE` | `true` | Market out of a stranded leg. |

Daily loss and hourly counts persist in `state.json`, so restarting the bot does
not reset your limits.

---

## What this does not do

- **No cross-exchange arbitrage.** One venue only.
- **No maker orders.** Taker fees are assumed throughout, which is the
  conservative and correct assumption for crossing spreads.
- **No cycles longer than 3 legs.** Each extra leg adds another fee and another
  chance to be picked off.
- **No withdrawals or transfers.** The bot has no code path that moves funds off
  Kraken, and its API key should not have permission to.
- **It is not investment advice.** You are responsible for what it does with your
  money.

---

## Security notes

- `.env` is gitignored. Keys never go in code, config, or logs.
- Never give a bot key withdrawal permissions.
- Rotate any key that has been in a plaintext file outside `.env`, in a
  screenshot, or in a cloud-synced folder.
- Kraken nonces must strictly increase **per key** — do not run two bots on one
  API key, or both will start failing authentication.
