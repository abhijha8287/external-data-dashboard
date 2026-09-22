# Weather pipeline: pull → store → compute → serve

A small pipeline over [Open-Meteo](https://open-meteo.com) (free, no key). It tracks 2 m temperature
for 5 cities, stores every reading as history in SQLite, computes a handful of derived metrics, and
serves the result through a JSON API and a dashboard.

Zero dependencies — pure Python 3.9+ standard library, `http.server` for the API, hand-rolled SVG for
the chart. Nothing to `pip install`, nothing to build.

I picked Open-Meteo over Frankfurter because it refreshes every 15 minutes instead of once a day —
re-running the pull actually produces new history, which is the whole point of this exercise.

## Run it

```bash
python3 -m weatherpipe backfill --days 2     # optional: seed ~2 days of real history so the chart isn't empty
python3 -m weatherpipe pull                  # one live pull for every city — this is "a pull"
python3 -m weatherpipe serve --port 8000     # API + dashboard → http://127.0.0.1:8000/
python3 -m unittest discover -s tests -t .   # 50 tests, ~1.5s, no network
```

| Command | Does |
|---|---|
| `pull [--city london]` | One live pull. Exits non-zero only if *every* city failed. |
| `run --every 900` | Loops `pull` every N seconds — the "scheduler." |
| `serve [--pull-every 900]` | API + dashboard; add `--pull-every` to run the scheduler in the same process. |
| `backfill --days N` | Seeds real history (up to 92 days) from Open-Meteo's 15-min series. |
| `status` | Recent pull log, successes and failures. |

Want to see the failure UI without waiting for an outage?
`WEATHERPIPE_BASE_URL=http://127.0.0.1:1/x python3 -m weatherpipe pull --city london`, then reload the dashboard.

**API:** `GET /latest?entity=london` (snapshot + metrics + alerts + pipeline status), `GET /history?entity=&from=&to=&limit=&window=` (time series with a rolling average per point, `from`/`to` as `YYYY-MM-DD` or full ISO, both inclusive). Plus `/entities`, `/runs`, `/health`. Errors come back as JSON with real status codes, not stack traces.

## How it's put together

```
client.py   → network only: timeouts, bounded retries, payload validation
pull.py     → fetch → validate → store → log. Never raises on an upstream problem.
store.py    → all the SQL
metrics.py  → pure functions — no I/O, no clock, no DB. Easy to test to death.
service.py  → rows → API shapes, caching, pipeline-health status
server.py   → routes + the static dashboard
```

Each layer only calls the one below it, which is what makes `metrics.py` testable with plain lists
and `pull.py` testable against a fake HTTP server instead of the real internet.

## The data model

`snapshots` is append-only, keyed on `(entity_id, observed_at)` — that's the upstream **observation**
time, not when we happened to fetch it. That one choice does a lot of work: Open-Meteo only moves every
15 minutes, so pulling more often just re-sees the same reading, and keying on our own fetch time would
silently duplicate it and quietly wreck every average. Keying on observation time makes pulls idempotent
for free — a repeat is logged as `unchanged`, not an error — and lets backfill and live pulls interleave
safely without stepping on each other.

`pull_runs` is a separate operational log — every attempt, success or failure, with latency and error
text. A failed pull leaves no snapshot, so without this table there'd be no way to tell "no new data"
apart from "pipeline's been down for two hours."

## Derived metrics

| Metric | Definition |
|---|---|
| Change | `latest − previous`, and `/ \|previous\| × 100` for percent (`null` if previous is 0) |
| Rolling average | mean of the last **12** readings (3h at 15-min cadence), including the latest |
| Min / max | over the trailing **24 hours of time**, not a count — gaps don't distort it |
| Std dev | sample stdev over the rolling window |
| Trend | least-squares slope, °C/hour, correct even with irregular spacing |
| Rate z-score | z-score of the *latest rate of change* against the recent rates |

A couple of decisions worth flagging:

- **Percent change on Celsius is honestly a bit of a weak metric** — 0.5°C → 1.0°C is "+100%," and 0°C
  is a divide-by-zero. It's here because the brief asks for it, but the dashboard leads with the
  absolute number. Where it matters: I divide by `|previous|`, so a move from −10°C to −5°C reads as
  +50% (warming), not −50%.
- **Anomaly detection scores the rate of change, not the temperature itself.** Temperature trends hard
  — every sunrise would trip a z-score on the raw value. Scoring the step instead only flags a genuine
  departure from the recent trend. The baseline stdev is floored so a 0.1°C wobble on a flat series
  can't blow up into a z-score of 20, and it stays silent until there's enough history to have an
  opinion.
- `/history`'s rolling average is warmed up using the points *before* the requested range, so the first
  point you see isn't computed from a truncated window.

**Alerts:** a `spike` banner if the move since the last reading is ≥2°C, an `anomaly` banner if the
rate z-score is ≥3. Both configurable.

## When Open-Meteo misbehaves

Retries (3x, exponential backoff) only for things worth retrying — timeouts, 429, 5xx. A 404 fails
fast. Every payload is validated before it's allowed near the database: right shape, numeric, a
plausible temperature range, a parseable timestamp. Garbage in never becomes a row.

Cities are pulled independently, so one outage doesn't take the others down, and any surprise exception
is caught and logged rather than killing the scheduler loop.

The dashboard has four honest states instead of one silent "loading": **ok**; **failing** (red banner,
shows the actual error, keeps showing the last known good reading and its age); **stale** (amber — last
success was too long ago, pipeline's probably not running); **no data**. If the API itself is
unreachable, the page says so instead of going blank. The chart breaks the line across gaps rather than
drawing a fake straight segment through missing data.

## Caching

`/latest` is cached per `(entity, latest snapshot id, settings)`. Any new row — live or backfill —
changes that key, so a stale value can never be served; no TTL needed, correctness comes from the key
itself. Cost is one indexed lookup per request to check it. Hit/miss counts live at `/health`.

## Tradeoffs, and what I'd do with more time

- **Scheduling** — an in-process loop today, 15 min default to match Open-Meteo's cadence. With more
  time: a real cron/systemd timer or job runner, decoupled from the web process, with jitter and
  alerting on repeated failures.
- **Storage** — SQLite (WAL mode, so the dashboard never blocks on a write) is the right size for this.
  At real scale I'd reach for Postgres/Timescale plus downsampling for long history ranges.
- **Serving** — `http.server` is fine for a take-home, not for production. I'd put this behind
  gunicorn/uvicorn and a real proxy, and add auth/rate limiting.
- **Percent change on Celsius** — noted above; I'd switch to Kelvin or drop it in a real product.
- **Dashboard** — no framework, no build step, hand-drawn SVG chart with hover and gap-awareness. I
  verified it through the API and a JS syntax check, but couldn't get a browser attached in this
  session to eyeball it directly — worth a manual look before you rely on it.

## Tests

`python3 -m unittest discover -s tests -t .` — 50 tests, no network, using a local fake Open-Meteo
server that's scripted to return 500s, hangs, garbage bodies, and bad values.

- `test_metrics.py` — hand-computed expected values for every metric, including the edge cases (zero
  previous, single point, constant series, boundary of the min/max window).
- `test_pipeline.py` — append-only history, idempotent re-pulls, retries, timeouts, validation,
  one-city-fails-others-continue, backfill's forecast-slot filtering.
- `test_api.py` — real HTTP end-to-end: metrics, date filtering, cache invalidation, and all four
  dashboard states.

## Time spent

About 1 hour.
