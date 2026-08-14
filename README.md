# Yoco Retail Import Processor — Render Service

Flask API that turns any supplier price list into a Yoco-ready workbook.

**Spreadsheets (`.xlsx` `.xlsm` `.xls` `.csv`) come here. PDFs and images stay on the
browser's Gemini path.** One request per workbook instead of one model call per
sheet-chunk, and — for a template that has been seen before — zero model calls.

Output is a `Products` sheet with these exact columns:

```text
Product ID, Product Name, Description, Default Price, Brand, Category, SKU, Default Cost Price,
Ask For Quantity, Default Quantity, Quantity Units, Ask For Price, VAT Enabled, Variant Price,
Variant Enabled, Attribute 1, Value 1, Attribute 2, Value 2, Attribute 3, Value 3, Image URL,
Barcode, Track Stock, Modifier Group
```

plus an `Issues` sheet for rows needing attention.

## How it handles "every file is a different layout"

Writing Python that understands every supplier template is unwinnable. So the model
never parses products — it only says *where the data is*:

```
sample the workbook  ->  Gemini returns a JSON layout plan  ->  Python executes the plan
                                                                         |
                                                              deterministic audit
                                                             /                    \
                                                        passed                  failed
                                                           |                       |
                                              store plan for this           re-plan once with
                                              template fingerprint          the audit failures
```

Four properties fall out of this:

1. **Universal.** A new layout needs a new *plan*, not new code.
2. **Consistent.** Same plan, same output, every time — no per-file drift.
3. **Cheap and fast.** One planner call per new template, zero for a repeat.
4. **Verifiable.** Every extraction is scored before the user sees it.

### The audit
`audit_extraction()` runs with no model calls and reports:

| Metric | Fails when |
|---|---|
| Coverage — products vs candidate source rows | below 90% (30% for pre-exploded exports) |
| Selling price below cost | more than 20% of rows (a cost/ex-VAT column was chosen) |
| Junk rate — priceless rows with sentence-like names | more than 5% |
| Duplicate barcodes | more than 10% |
| Variant integrity — `Variant Enabled = Yes` with no `Value 1` | any |
| Inflation — far more rows than source rows, with exact repeats | ratio > 3 and 25% repeats |

Coverage is deliberately one-sided: it only catches rows silently going *missing*,
because side-by-side layouts legitimately produce two or more products per row.

Results come back in `summary.audit`, so the dashboard can show "3 812 of 3 940
source rows extracted (96.8%)" instead of quietly dropping a section.

### Candidate selection
The plan path, the re-plan and the legacy heuristic parsers all run as candidates
and are scored on the same scale; the best-scoring one wins. There is no fixed
preference order to be wrong about.

### The template registry
Plans are keyed on a **structure-only fingerprint** (sheet names, header text,
column count) — not on cell values. Next month's file from the same supplier reuses
the accepted plan: no model call, identical output. Inspect with
`GET /plan-registry`, drop one with `POST /plan-registry/forget`.

## Endpoints

```text
GET  /                       health + endpoint list
GET  /health                 version
GET  /dashboard              the RetailScan dashboard (static/index.html)
POST /process-retail-file-json   upload -> products + issues + audit  (main endpoint)
POST /export-yoco-file           edited products -> Yoco XLSX
POST /process-retail-file        upload -> Yoco XLSX in one shot (legacy)
POST /resolve-price-conflict     record a price-conflict choice
GET  /plan-registry              stored template plans
POST /plan-registry/forget       {"fingerprint": "..."} -> re-plan next time
GET  /template-columns           the Yoco column list
GET  /debug-drive-config         Drive setup diagnostics
```

### `POST /process-retail-file-json`

Multipart fields:

| Field | Default | Meaning |
|---|---|---|
| `file` | required | the spreadsheet |
| `parse_mode` | `variant` | `variant` groups sizes/options; `single` keeps one row per line |
| `ai_instructions` | empty | operator instructions, e.g. "ignore cleaning products" |
| `vat_enabled` | `true` | prices are VAT-inclusive |
| `track_stock` | `true` | populate Track Stock |
| `gemini_api_key` | empty | override; prefer the `GEMINI_API_KEY` env var |

Response: `products[]`, `issues[]`, `price_conflicts[]`, `summary{}` (including
`layout_strategy`, `coverage`, `planner_calls`, `replans`, `template_fingerprint`,
`audit`).

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...        # optional; without it, deterministic parsers only
python app.py
```

Then open http://127.0.0.1:10000/dashboard, or:

```bash
curl -X POST http://127.0.0.1:10000/process-retail-file \
  -F "file=@/path/to/supplier.xlsx" --output yoco_products_import.xlsx
```

## Deploy to Render

Push to GitHub, then **New > Web Service**:

```text
Runtime:       Python
Build command: pip install -r requirements.txt
Start command: gunicorn app:app --timeout 180
```

`render.yaml` carries the rest. Set `GEMINI_API_KEY` in the Render dashboard so no
key ships in client HTML. Free instances sleep, so the first request after idle can
take ~50 s — the dashboard pings `/health` on load to warm it.

## Dashboard

`static/index.html` is the full RetailScan dashboard, served at `/dashboard`. When
hosted elsewhere, set near the top of its module script:

```js
const RENDER_API_URL_CONFIGURED = "https://your-service.onrender.com";
```

Left as the placeholder, it calls its own origin — which is why `/dashboard` works
with no CORS setup at all. `static/simple-upload.html` is the old minimal
upload-and-download page, kept for smoke testing.

Spreadsheets go to this service; PDFs and images continue through the in-browser
Gemini path. If the service is unreachable, the dashboard rebuilds the old per-sheet
text and falls back to browser extraction, so a cold or down instance never blocks
the user.

## Regression tests

```bash
cp your-supplier-files/*.xlsx tests/goldens/files/
python tests/run_goldens.py --update
python tests/run_goldens.py          # non-zero exit on drift
```

See `tests/goldens/README.md`. Run it before every deploy: this is what stops a
prompt tweak from fixing one supplier and quietly breaking another.

## Defaults applied to non-variant supplier rows

```text
Ask For Quantity = (blank)   Yoco default; only set when the behaviour is wanted
Ask For Price    = (blank)   same
Default Quantity = 1
VAT Enabled      = Yes       follows the vat_enabled field
Variant Enabled  = No
Track Stock      = Product   Variant on multi-row variant products
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | unset | layout planner; unset means deterministic parsers only |
| `GEMINI_LAYOUT_MODEL` | `gemini-2.5-flash-preview-09-2025` | planner model |
| `PLAN_REGISTRY_PATH` | `/tmp/yoco_plan_registry.json` | where accepted plans are cached |
| `PLAN_REGISTRY_DISABLED` | `false` | force a fresh plan every upload |
| `PREFIX_CATEGORY_IN_NAME` | `false` | legacy "CATEGORY - Product" naming |
| `MAX_UPLOAD_MB` | `32` | upload ceiling |
| `ALLOWED_ORIGINS` | `*` | set to your dashboard domain in production |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | unset | Drive backup of every upload |
| `GOOGLE_SHARED_DRIVE_ID` / `GOOGLE_DRIVE_FOLDER_ID` | unset | Drive target |
| `DRIVE_UPLOAD_REQUIRED` | `false` | fail the request when the Drive backup fails |

## What changed in this version

`APP_VERSION = 2026-08-14-plan-audit-registry-v3`

1. **Variants no longer collapse on the plan path.** The executor ignored the
   plan's `variant` block entirely, so `variant_export` fell through to the
   standard-table branch and every variant became its own single product. Only
   Shopify and Yoco exports escaped, via their dedicated parsers.
2. **`parse_money_opt()` added.** `parse_money()` returns `0.0` for junk, which made
   two guards in the executor dead code: junk rows became R0.00 products, and
   category-heading detection never fired, so `column_a_state` categories were lost.
   The new function also refuses cells carrying real words or unit suffixes, so
   "Amstel Lager 660ml" can no longer be read as a price of 660.
3. **`description`, `brand`, `image_url`** are now read from the plan.
4. **Sampler widened.** Was rows 1–60, columns A–R only — a Shopify export's
   variant columns start at column I and its price sits at W, so the planner was
   working blind. Now head + two mid-file windows + tail, across the full used width.
5. **Audit + one re-plan**, with the previous plan and its concrete failures fed back.
6. **Template registry** keyed on a structure-only fingerprint.
7. **Attribute names carried across continuation rows**, since Yoco rejects an
   import where Attribute 1 differs between rows of one product.
8. **Totals/header rows rejected**, and category names no longer prefixed into
   product names by default.
9. `Ask For Quantity` / `Ask For Price` export blank instead of `No`.
10. Dashboard served at `/dashboard`; golden-file harness added.
