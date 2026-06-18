# Yoco Retail Import Processor - Render Service

A small Flask API that accepts supplier Excel/CSV uploads, normalizes product data, and returns a Yoco-style workbook.

This version is aligned to the filled Yoco export/template supplied by David. It outputs a `Products` sheet with these exact columns:

```text
Product ID, Product Name, Description, Default Price, Brand, Category, SKU, Default Cost Price, Ask For Quantity, Default Quantity, Quantity Units, Ask For Price, VAT Enabled, Variant Price, Variant Enabled, Attribute 1, Value 1, Attribute 2, Value 2, Attribute 3, Value 3, Image URL, Barcode, Track Stock, Modifier Group
```

The workbook also includes an `Issues` sheet for rows that need checking, such as missing product name, missing barcode/SKU, missing price, duplicate barcode, or duplicate SKU.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Local API:

```text
http://127.0.0.1:10000/process-retail-file
```

Test with curl:

```bash
curl -X POST http://127.0.0.1:10000/process-retail-file \
  -F "file=@/path/to/supplier.xlsx" \
  --output yoco_products_import.xlsx
```

## Deploy to Render

1. Push this folder to a GitHub repository.
2. In Render, choose **New > Web Service**.
3. Connect your repository.
4. Use these settings:

```text
Runtime: Python
Build command: pip install -r requirements.txt
Start command: gunicorn app:app --timeout 120
```

## Environment variables

Optional:

```text
MAX_UPLOAD_MB=25
ALLOWED_ORIGINS=*
```

For production, replace `ALLOWED_ORIGINS=*` with your dashboard domain, for example:

```text
ALLOWED_ORIGINS=https://your-dashboard-domain.com
```

## Dashboard integration

Update `static/index.html` and replace:

```js
const API_URL = "https://YOUR-RENDER-SERVICE.onrender.com/process-retail-file";
```

with your actual Render service URL.

## Useful endpoints

```text
GET  /
GET  /template-columns
POST /process-retail-file
```

## Mapping notes

The service uses best-effort column detection for supplier files. It maps common columns such as product/item/description, barcode/EAN, SKU/product code, category/department, retail/inc VAT price, ex VAT/cost price, and stock quantity.

For normal non-variant supplier rows, it defaults:

```text
Ask For Quantity = No
Default Quantity = 1
Ask For Price = No
VAT Enabled = Yes
Variant Enabled = No
Track Stock = Product
```

If a supplier file already contains Yoco-style columns, the service maps those into the same final structure.
