# FazerCards → G2A bridge

An automated bridge between a supplier REST API (FazerCards) and a store REST API
(G2A Seller / Import API).

**Phase 1 — built and tested:** catalogue sync, product mapping, deduplication,
pricing, and automatic price/stock synchronisation of G2A offers.
**Phase 2 — not built yet:** the order → buy key → deliver flow. See
[What is not built yet](#what-is-not-built-yet) before deploying.

---

## Quick start

```bash
cp .env.example .env
python -m app.cli keygen          # paste the result into ENCRYPTION_KEY
# fill in FAZERCARDS_API_KEY, G2A_CLIENT_ID, G2A_CLIENT_SECRET, POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec api python -m app.cli doctor
```

`doctor` is the first thing to run: it checks every credential, fetches a G2A
token and reads a page of each catalogue. If it is clean, everything else will
work; if it is not, it tells you exactly which of the two APIs is unhappy.

Then, once:

```bash
docker compose exec api python -m app.cli sync store-catalog    # mirror G2A's catalogue
docker compose exec api python -m app.cli sync supplier         # pull FazerCards
docker compose exec api python -m app.cli map run               # match them
docker compose exec api python -m app.cli map pending           # review what is unsure
docker compose exec api python -m app.cli sync offers --dry-run # see what would be pushed
docker compose exec api python -m app.cli sync offers           # push for real
```

After that the `worker` container keeps everything in sync on its own.

---

## How it works

```
  FazerCards API                    bridge                       G2A API
  ──────────────                ─────────────                ─────────────
  /gamekeys      ──┐                                       ┌── /v1/products
  /giftcards     ──┼──> supplier_products                  │   (mirrored into
                   │           │                           │    store_products)
                   │           ├──> product_mappings <─────┘
                   │           │      (1:1, unique both ways)
                   │           │
                   │           └──> store_offers ──────────┬──> POST /v1/offers
                   │                 (remembers what        └──> PATCH /v1/offers/{id}
                   │                  was last pushed)
                   └──> POST /order  <──── Phase 2 ────  order webhook / poll
```

The database in the middle is not a cache for speed — it is load-bearing:

- **G2A's product list cannot be searched by name.** It only pages, 20 at a time.
  There is no "does G2A have a product called X" endpoint. So the catalogue is
  mirrored locally and matching runs against an indexed table.
- **Mapping has to survive restarts.** It is the thing that keeps prices and
  stock attached to the right offer over time.
- **Deduplication needs a place to remember what already exists.**

### The rules that stop the expensive mistakes

| Risk | What the code does |
|---|---|
| Two offers for one product | `product_mappings` is unique on *both* sides. A claimed store product is invisible to every other supplier SKU. |
| Selling a 100 HKD card as a 350 HKD one | Face value is a **hard gate** in matching, not a similarity signal. Different denominations score zero. |
| Selling a TURKEY key as an INDIA key | Region and platform are hard gates too. |
| Being rate-limited on price changes | G2A caps price changes per product per hour. The bridge counts its own changes and *defers* rather than sending a call it knows will fail. |
| Burning API quota | Every offer remembers what was last pushed. An unchanged sync sends **zero** requests. |
| Losing the catalogue to a flaky response | A product missing from the feed is deactivated only after N consecutive misses (`DEACTIVATE_MISSING_AFTER_RUNS`). |
| A retry buying a second key | Non-idempotent POSTs are never retried unless an idempotency key is attached. |
| Pricing off a stale exchange rate | If the FX provider has been down longer than `FX_MAX_AGE_HOURS`, pricing **stops** instead of using an old rate. |
| A 202 that silently failed | G2A offer writes are asynchronous. The bridge tracks the job and clears its "pushed" state if the job failed. |

---

## Configuration

Everything lives in `.env`; nothing is hardcoded. See `.env.example` for the
full list. The ones you will actually tune:

| Variable | Default | Meaning |
|---|---|---|
| `DEFAULT_MARKUP_PERCENT` | `15` | Margin over supplier cost. Overridable per mapping. |
| `FX_RATE_USD_EUR` | *(empty)* | Empty = fetch a live rate daily. Set a number to pin it. |
| `PRICE_STOCK_SYNC_MINUTES` | `12` | How often prices and stock are pushed. |
| `CATALOG_SYNC_MINUTES` | `60` | How often the supplier catalogue is re-read. |
| `MATCH_AUTO_ACCEPT_SCORE` | `0.92` | Above this, map without asking. |
| `MATCH_REVIEW_SCORE` | `0.70` | Between the two, queue for review. Below, discard. |
| `G2A_PRICE_CHANGES_PER_PRODUCT_PER_HOUR` | `5` | G2A's documented cap. |
| `G2A_PRICE_CHANGE_SAFETY_MARGIN` | `1` | Kept in reserve for manual changes in the panel. |
| `MIN_OFFER_PRICE` | `0.30` | Floor, whatever the markup maths produces. |

Secrets — `FAZERCARDS_API_KEY`, `G2A_CLIENT_SECRET`, `ENCRYPTION_KEY`,
`ADMIN_API_TOKEN` — are read from the environment only. They are scrubbed out of
the request log, and `.env` is gitignored.

---

## Commands

```
python -m app.cli doctor                  check config + reach both APIs
python -m app.cli keygen                  generate an ENCRYPTION_KEY

python -m app.cli db init                 create the schema (dev; prod uses migrations/)
python -m app.cli db check                row counts

python -m app.cli sync supplier           pull the FazerCards catalogue
python -m app.cli sync store-catalog      mirror G2A's product catalogue
    --max-pages N                           stop early (the full crawl is slow: 20/page)
    --since-hours N                         only what changed recently
python -m app.cli sync offers             push prices and stock
    --dry-run                               report what would change, send nothing
    --limit N
python -m app.cli sync adopt              attach offers that already exist on G2A

python -m app.cli map run                 match unmapped products
    --all                                   re-examine everything (human decisions kept)
python -m app.cli map pending             list what needs a decision
python -m app.cli map approve ID [--store-product-id N]
python -m app.cli map reject ID [--note "..."]

python -m app.cli worker                  run the scheduler in the foreground
```

### Taking over an account that already has offers

Your G2A account already has ~558 offers. Do **not** let the bridge create them
again. After mapping:

```bash
python -m app.cli sync adopt
```

This reads your existing offers, attaches them to the matching mappings, and
records their current price and stock, so the first real sync sends only genuine
differences. `sync adopt` also reports how many live offers it could not match to
any mapping — those are the ones to look at by hand.

---

## Admin API

Runs on port 8000 (bound to localhost by compose). Every endpoint except
`/health` needs `X-Admin-Token`. Interactive docs at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness + a real database round-trip. Safe to gate deploys on. |
| `GET /status` | Catalogue, mapping and offer counts, plus the last 8 sync runs. |
| `GET /mappings?status=pending` | The review queue, with alternatives for each. |
| `POST /mappings/{id}/approve` | Confirm, optionally overriding the target product. |
| `POST /mappings/{id}/reject` | Reject. The pair is never suggested again. |
| `GET /mappings/{id}/candidates` | Re-score against the catalogue right now. |
| `GET /logs/api-calls?failed_only=true` | Every request, attempt by attempt, secrets scrubbed. |

`/health` reports only the database on purpose. A health check that also called
FazerCards and G2A would fail every time a supplier had a bad minute, and would
roll back deployments that were perfectly fine.

---

## Database

Schema: `migrations/001_initial.sql`, applied automatically by the Postgres
container on first start.

It is **generated** from `app/models.py`:

```bash
python -m app.tools.dump_schema > migrations/001_initial.sql
```

Regenerate after any model change, so the SQL and the code cannot drift apart.

| Table | Holds |
|---|---|
| `suppliers`, `supplier_products` | The supplier catalogue, keyed on the supplier's own ids |
| `store_products` | The mirrored G2A catalogue |
| `product_mappings` | The 1:1 link, its score, and the runners-up |
| `store_offers` | Our offers, and what was last pushed to each |
| `price_change_log` | Price changes, for the hourly budget |
| `store_orders`, `order_items`, `delivered_keys` | Phase 2 order flow (tables exist, flow not built) |
| `idempotency_keys` | Exactly-once protection |
| `sync_runs`, `api_calls` | What ran, what it did, and every request it made |
| `fx_rates` | Cached exchange rates |

Purchased keys are encrypted with `ENCRYPTION_KEY` before they are stored, and
indexed by a keyed hash so the same code can never be delivered twice. A database
dump on its own is not a pile of resellable keys.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q          # 70 tests, ~30s
```

The suite runs the **real adapters** against in-process fakes of both APIs
(`tests/fakes.py`) wired in through httpx's transport hook. Mocking our own
methods would prove nothing about whether the request we build matches the API;
here a wrong payload fails the test. The fakes also inject the things that are
hard to trigger deliberately against a live account: 429s with `Retry-After`,
500s, timeouts, async jobs that fail after returning 202, and the price-change
cap.

The slow ~30s is the token-bucket rate limiter doing real work — it is not
stubbed out, because throttling is part of what is being tested.

---

## What is not built yet

Stated plainly so nothing here reads as more finished than it is.

1. **Order fulfilment (Phase 2).** The order side — catch a G2A order, buy the
   key from FazerCards, deliver it back — is **not implemented**. What exists
   today is the groundwork it needs: the `store_orders` / `order_items` /
   `delivered_keys` tables, the encrypted key store, the exactly-once
   `run_once()` guard (tested, including the crash-mid-purchase case), and
   `FazerCardsAdapter.purchase()` with the `Idempotency-Key` header. The G2A
   side of it still has to be written, and there is a decision to make first —
   see below.

2. **G2A order intake: two options, and they are not equivalent.**
   - *Merchant Stock Contract API* — G2A calls **us** at `/reservation` and
     `/order`, and we buy the key on demand. This is the mechanism G2A intends
     for dropshipping and the only one with no delivery lag. It needs a public
     HTTPS endpoint on your domain and your server's IP allowlisted with G2A.
   - *Order polling* — we poll `GET /v4/seller/orders`. No inbound endpoint
     needed, but delivery lags by the polling interval.

   Which one you want changes what Phase 2 looks like, so it is worth deciding
   before it is built rather than after.

3. **Exact G2A endpoint paths are unverified.** G2A's published documentation
   page names each operation but does not render every URL, and their
   machine-readable spec sits behind a WAF. The paths the adapter uses follow the
   documented pattern and are all collected in one dict — `PATHS` at the top of
   `app/stores/g2a.py`. The first live `doctor` run confirms or corrects them,
   and a correction is a one-line change in that one place. Nothing else in the
   codebase hardcodes a URL.

4. **Only the FazerCards adapter exists.** The supplier interface
   (`app/suppliers/base.py`) is written so a second supplier is one new class
   plus a row in `suppliers` — mapping, pricing, offers and orders all go through
   it — but no second adapter has been written.

5. **No alerting.** Failures land in `sync_runs` and `api_calls` and are visible
   through `/status`. Nothing emails or messages you yet.

---

## Operational notes

- **The first G2A catalogue crawl is slow.** 20 products per page over a
  marketplace-sized catalogue is a lot of requests. Run it once with
  `sync store-catalog`, let it finish, and after that the worker only asks for
  what changed. Use `--max-pages` to sample it first.
- **Rate limits are configurable** (`FAZERCARDS_RATE_PER_SECOND`,
  `G2A_RATE_PER_SECOND`). If a catalogue pull is not finishing inside its
  interval, that is the first knob to turn — and the worker skips a tick rather
  than starting a second overlapping pull.
- **`docker compose logs -f worker`** shows every sync with its counters.
- **Nothing writes a log file into the repo.** Container logs go to stdout, and
  httpx's own INFO logging is pinned to WARNING because it echoes query strings.
