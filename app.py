import io
import os
import re
import json
import math
import hashlib
from typing import Any, Dict, List, Tuple, Optional

import pandas as pd
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS


app = Flask(__name__)
CORS(app)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

YOCO_PRODUCTS_COLUMNS = [
    "Product ID",
    "Product Name",
    "Description",
    "Default Price",
    "Brand",
    "Category",
    "SKU",
    "Default Cost Price",
    "Ask For Quantity",
    "Default Quantity",
    "Quantity Units",
    "Ask For Price",
    "VAT Enabled",
    "Variant Price",
    "Variant Enabled",
    "Attribute 1",
    "Value 1",
    "Attribute 2",
    "Value 2",
    "Attribute 3",
    "Value 3",
    "Image URL",
    "Barcode",
    "Track Stock",
    "Modifier Group",
]

# Common supplier/export headers mapped into the internal row shape.
COLUMN_ALIASES = {
    "product_name": [
        "product name", "name", "item", "item name", "description", "product", "title",
        "item description", "product description", "stock item", "stock description",
    ],
    "description": ["description", "details", "product details", "long description"],
    "category": ["category", "department", "group", "section", "product category", "cat"],
    "brand": ["brand", "make", "manufacturer"],
    "barcode": ["barcode", "bar code", "ean", "upc", "gtin", "scancode", "scan code"],
    "sku": ["sku", "stock code", "item code", "product code", "code", "plu", "variant sku"],
    "selling_price": [
        "selling price", "sell price", "sale price", "retail price", "price", "price incl",
        "price inc", "incl vat", "inc vat", "including vat", "vat incl", "rrp", "shelf price",
    ],
    "cost_price": [
        "cost price", "cost", "default cost price", "ex vat", "excl vat", "nett", "net price",
        "wholesale", "buying price", "purchase price", "supplier price",
    ],
    "quantity": ["qty", "quantity", "stock", "stock on hand", "soh", "default quantity"],
    "image_url": ["image url", "image", "image src", "photo", "picture", "img_url"],
    "product_id": ["product id", "handle", "slug", "product_id"],
    "variant_enabled": ["variant enabled"],
    "attribute_1": ["attribute 1", "option1 name", "option 1 name"],
    "value_1": ["value 1", "option1 value", "option 1 value"],
    "attribute_2": ["attribute 2", "option2 name", "option 2 name"],
    "value_2": ["value 2", "option2 value", "option 2 value"],
    "attribute_3": ["attribute 3", "option3 name", "option 3 name"],
    "value_3": ["value 3", "option3 value", "option 3 value"],
}


def normalise_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def normalise_for_compare(value: Any) -> str:
    text = normalise_text(value).lower()
    text = text.replace("×", "x")
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(value: Any) -> str:
    text = normalise_text(value).lower().replace("×", "x")
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "product"


def parse_money(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
        return round(float(value), 2)

    text = normalise_text(value)
    if not text:
        return 0.0

    text = text.replace("R", "").replace("r", "")
    text = text.replace(" ", "")
    text = text.replace("\u00a0", "")

    # If comma is decimal separator and no decimal point exists, convert comma to point.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")

    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", "-", "."}:
        return 0.0

    try:
        return round(float(text), 2)
    except ValueError:
        return 0.0


def clean_code(value: Any) -> str:
    text = normalise_text(value)
    if not text:
        return ""
    # Preserve leading * because some supplier barcodes include it.
    text = re.sub(r"\s+", "", text)
    # Excel often turns numeric codes into 6001844005002.0
    text = re.sub(r"\.0$", "", text)
    return text


def row_uid(row: Dict[str, Any], index: int) -> str:
    seed = json.dumps(row, sort_keys=True, default=str) + f"::{index}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def best_code(row: Dict[str, Any]) -> str:
    candidates = [
        row.get("barcode"),
        row.get("sku"),
        row.get("code"),
        row.get("Barcode"),
        row.get("SKU"),
        row.get("Product Code"),
        row.get("Code"),
    ]
    cleaned = [clean_code(c) for c in candidates if clean_code(c)]
    if not cleaned:
        return ""
    # Prefer the longest code because scannable barcodes are usually longest.
    return sorted(cleaned, key=lambda c: (len(re.sub(r"\D", "", c)), len(c)), reverse=True)[0]


def get_price(row: Dict[str, Any]) -> float:
    for key in ["calculated_price", "selling_price", "variant_price", "Default Price", "Variant Price", "price"]:
        if key in row and normalise_text(row.get(key)) != "":
            price = parse_money(row.get(key))
            if price != 0:
                return price
    return 0.0


def get_cost(row: Dict[str, Any]) -> float:
    for key in ["cost_price", "Default Cost Price", "cost", "ex_vat", "Ex VAT"]:
        if key in row and normalise_text(row.get(key)) != "":
            return parse_money(row.get(key))
    return 0.0


def title_value(row: Dict[str, Any]) -> str:
    return normalise_text(row.get("product_name") or row.get("Product Name") or row.get("title") or row.get("name"))


def product_id_value(row: Dict[str, Any]) -> str:
    return normalise_text(row.get("product_id") or row.get("Product ID"))


def set_product_id(row: Dict[str, Any], value: str) -> None:
    row["product_id"] = value
    row["Product ID"] = value


def set_title(row: Dict[str, Any], value: str) -> None:
    row["product_name"] = value
    row["Product Name"] = value


def true_duplicate_key(row: Dict[str, Any]) -> Tuple[str, str, float, float]:
    return (
        normalise_for_compare(title_value(row)),
        clean_code(best_code(row)).lower(),
        round(get_price(row), 2),
        round(get_cost(row), 2),
    )


def preflight_products_for_frontend(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean product rows before returning them to the frontend.

    1. True dedupe: if title + code + price + cost are identical, keep first only.
    2. SKU/code enrichment: if duplicate generic product_id rows have different codes,
       append code to title and product_id so they become unique.
    3. Adds stable _uid for frontend actions. The frontend should use _uid for edits/removals,
       never product_id, because product_id can be edited by the user.
    """
    deduped: List[Dict[str, Any]] = []
    seen_true_duplicates = set()

    for index, original in enumerate(products):
        row = dict(original)
        title = title_value(row)
        code = best_code(row)
        price = get_price(row)
        cost = get_cost(row)

        if code:
            row["barcode"] = row.get("barcode") or code
            row["Barcode"] = row.get("Barcode") or code
            row["sku"] = row.get("sku") or row.get("SKU") or code
            row["SKU"] = row.get("SKU") or row.get("sku") or code

        if title:
            set_title(row, title)

        if not product_id_value(row):
            set_product_id(row, slugify(title or code or f"product-{index + 1}"))

        row["selling_price"] = price
        row["calculated_price"] = price
        row["cost_price"] = cost

        key = true_duplicate_key(row)
        # Only dedupe if we have at least a title and code. Otherwise keep for user review.
        if key[0] and key[1] and key in seen_true_duplicates:
            continue
        seen_true_duplicates.add(key)
        deduped.append(row)

    # Find duplicate product IDs on single rows. Variants may intentionally share an ID.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in deduped:
        variant_enabled = normalise_text(row.get("variant_enabled") or row.get("Variant Enabled")).lower()
        if variant_enabled == "yes":
            continue
        pid = product_id_value(row)
        if not pid:
            continue
        groups.setdefault(pid.lower(), []).append(row)

    for _pid, rows in groups.items():
        if len(rows) <= 1:
            continue

        codes = {clean_code(best_code(row)).lower() for row in rows if clean_code(best_code(row))}
        if len(codes) <= 1:
            # Same product_id and same/blank code. If not true duplicates, keep for frontend review.
            continue

        # Same generic product_id but different manufacturing/barcode/SKU codes.
        # Make each row unique by appending code to title and slug.
        used_ids = set()
        for row in rows:
            code = clean_code(best_code(row))
            if not code:
                continue

            title = title_value(row)
            code_norm = normalise_for_compare(code)
            title_norm = normalise_for_compare(title)

            if code_norm and code_norm not in title_norm:
                new_title = f"{title} - {code}" if title else code
                set_title(row, new_title)
            else:
                new_title = title

            base_slug = slugify(new_title)
            code_slug = slugify(code)
            new_pid = base_slug
            if code_slug and not new_pid.endswith(code_slug):
                new_pid = f"{new_pid}-{code_slug}"
            new_pid = re.sub(r"-+", "-", new_pid).strip("-")

            original_pid = new_pid
            counter = 2
            while new_pid in used_ids:
                new_pid = f"{original_pid}-{counter}"
                counter += 1
            used_ids.add(new_pid)
            set_product_id(row, new_pid)

    # Add stable IDs after dedupe/enrichment.
    for index, row in enumerate(deduped):
        row["_uid"] = row.get("_uid") or row_uid(row, index)
        row["_preflighted"] = True

    return deduped


def find_header_row(df: pd.DataFrame, max_scan_rows: int = 15) -> int:
    """Find likely header row by scoring known column names."""
    best_idx = 0
    best_score = -1
    alias_words = {alias for aliases in COLUMN_ALIASES.values() for alias in aliases}

    for idx in range(min(len(df), max_scan_rows)):
        values = [normalise_for_compare(v) for v in df.iloc[idx].tolist()]
        score = 0
        for value in values:
            if value in alias_words:
                score += 3
            elif any(alias in value for alias in alias_words if len(alias) > 4):
                score += 1
        non_empty = sum(1 for v in values if v)
        score += min(non_empty, 8) * 0.1
        if score > best_score:
            best_score = score
            best_idx = idx

    return best_idx


def normalise_header(header: Any) -> str:
    return normalise_for_compare(header)


def map_columns(columns: List[Any]) -> Dict[str, str]:
    header_map = {normalise_header(c): c for c in columns}
    mapped: Dict[str, str] = {}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            alias_norm = normalise_header(alias)
            if alias_norm in header_map:
                mapped[canonical] = header_map[alias_norm]
                break
        if canonical in mapped:
            continue
        # Fuzzy contains match as fallback.
        for h_norm, original in header_map.items():
            if any(alias_norm in h_norm for alias_norm in [normalise_header(a) for a in aliases] if alias_norm):
                mapped[canonical] = original
                break

    return mapped


def dataframe_from_sheet(raw_df: pd.DataFrame) -> pd.DataFrame:
    raw_df = raw_df.dropna(how="all").dropna(axis=1, how="all")
    if raw_df.empty:
        return pd.DataFrame()

    header_idx = find_header_row(raw_df)
    headers = raw_df.iloc[header_idx].tolist()
    df = raw_df.iloc[header_idx + 1:].copy()
    df.columns = [normalise_text(h) or f"Column {i + 1}" for i, h in enumerate(headers)]
    df = df.dropna(how="all")
    return df


def row_from_dataframe_record(record: Dict[str, Any], source_sheet: str, index: int) -> Optional[Dict[str, Any]]:
    mapped = map_columns(list(record.keys()))

    def get(canonical: str, default: Any = "") -> Any:
        col = mapped.get(canonical)
        if col is None:
            return default
        return record.get(col, default)

    title = normalise_text(get("product_name"))
    # Fallback: find the longest text-ish cell if no title column was mapped.
    if not title:
        text_cells = [normalise_text(v) for v in record.values() if normalise_text(v)]
        text_cells = [v for v in text_cells if not re.fullmatch(r"[Rr]?\s*[0-9,.\-]+", v)]
        if text_cells:
            title = max(text_cells, key=len)

    code = clean_code(get("barcode") or get("sku"))
    sku = clean_code(get("sku") or code)
    category = normalise_text(get("category")) or source_sheet or "Uncategorised"
    brand = normalise_text(get("brand"))
    description = normalise_text(get("description"))
    selling_price = parse_money(get("selling_price"))
    cost_price = parse_money(get("cost_price"))
    image_url = normalise_text(get("image_url"))

    if not title and not code:
        return None

    product_id = normalise_text(get("product_id")) or slugify(title or code or f"product-{index + 1}")

    row = {
        "product_id": product_id,
        "product_name": title,
        "description": description,
        "category": category,
        "brand": brand,
        "sku": sku,
        "barcode": code,
        "selling_price": selling_price,
        "calculated_price": selling_price,
        "cost_price": cost_price,
        "image_url": image_url,
        "variant_enabled": normalise_text(get("variant_enabled")) or "No",
        "attr1_name": normalise_text(get("attribute_1")),
        "attr1_val": normalise_text(get("value_1")),
        "attr2_name": normalise_text(get("attribute_2")),
        "attr2_val": normalise_text(get("value_2")),
        "attr3_name": normalise_text(get("attribute_3")),
        "attr3_val": normalise_text(get("value_3")),
        "source_sheet": source_sheet,
        "source_row": index + 1,
    }
    return row


def parse_uploaded_file(file_storage) -> List[Dict[str, Any]]:
    filename = file_storage.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    products: List[Dict[str, Any]] = []

    if ext == "csv":
        raw = file_storage.read()
        df = pd.read_csv(io.BytesIO(raw), dtype=object, header=None)
        cleaned = dataframe_from_sheet(df)
        for idx, record in enumerate(cleaned.to_dict(orient="records")):
            row = row_from_dataframe_record(record, "CSV", idx)
            if row:
                products.append(row)
        return products

    if ext in {"xlsx", "xls", "xl"}:
        engine = "openpyxl" if ext == "xlsx" else None
        sheets = pd.read_excel(file_storage, sheet_name=None, dtype=object, header=None, engine=engine)
        for sheet_name, raw_df in sheets.items():
            cleaned = dataframe_from_sheet(raw_df)
            if cleaned.empty:
                continue
            for idx, record in enumerate(cleaned.to_dict(orient="records")):
                row = row_from_dataframe_record(record, str(sheet_name), idx)
                if row:
                    products.append(row)
        return products

    raise ValueError(f"Unsupported file type: .{ext}")


def product_to_yoco_row(row: Dict[str, Any], track_stock: str = "Product", vat_enabled: str = "Yes") -> Dict[str, Any]:
    price = get_price(row)
    cost = get_cost(row)
    product_id = product_id_value(row) or slugify(title_value(row))
    product_name = title_value(row)
    code = best_code(row)

    variant_enabled = normalise_text(row.get("variant_enabled") or row.get("Variant Enabled"))
    variant_enabled = "Yes" if variant_enabled.lower() == "yes" else "No"

    return {
        "Product ID": product_id,
        "Product Name": product_name,
        "Description": normalise_text(row.get("description") or row.get("Description")),
        "Default Price": price,
        "Brand": normalise_text(row.get("brand") or row.get("Brand")),
        "Category": normalise_text(row.get("category") or row.get("Category")) or "Uncategorised",
        "SKU": normalise_text(row.get("sku") or row.get("SKU") or code),
        "Default Cost Price": cost,
        "Ask For Quantity": "No",
        "Default Quantity": normalise_text(row.get("quantity") or row.get("Default Quantity")),
        "Quantity Units": normalise_text(row.get("quantity_units") or row.get("Quantity Units")),
        "Ask For Price": "No",
        "VAT Enabled": vat_enabled,
        "Variant Price": price,
        "Variant Enabled": variant_enabled,
        "Attribute 1": normalise_text(row.get("attr1_name") or row.get("Attribute 1")),
        "Value 1": normalise_text(row.get("attr1_val") or row.get("Value 1")),
        "Attribute 2": normalise_text(row.get("attr2_name") or row.get("Attribute 2")),
        "Value 2": normalise_text(row.get("attr2_val") or row.get("Value 2")),
        "Attribute 3": normalise_text(row.get("attr3_name") or row.get("Attribute 3")),
        "Value 3": normalise_text(row.get("attr3_val") or row.get("Value 3")),
        "Image URL": normalise_text(row.get("image_url") or row.get("Image URL")),
        "Barcode": code,
        "Track Stock": track_stock,
        "Modifier Group": normalise_text(row.get("modifier_group") or row.get("Modifier Group")),
    }


def build_issues(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    id_counts: Dict[str, int] = {}
    name_variant_counts: Dict[str, int] = {}

    for row in products:
        if normalise_text(row.get("variant_enabled")).lower() != "yes":
            pid = product_id_value(row).lower()
            if pid:
                id_counts[pid] = id_counts.get(pid, 0) + 1

        combo = "::".join([
            normalise_for_compare(title_value(row)),
            normalise_for_compare(row.get("attr1_val")),
            normalise_for_compare(row.get("attr2_val")),
            normalise_for_compare(row.get("attr3_val")),
        ])
        name_variant_counts[combo] = name_variant_counts.get(combo, 0) + 1

    for idx, row in enumerate(products):
        uid = row.get("_uid")
        if not title_value(row):
            issues.append({"uid": uid, "row": idx + 1, "level": "error", "issue": "Missing product name"})
        if get_price(row) < 0:
            issues.append({"uid": uid, "row": idx + 1, "level": "error", "issue": "Negative price"})
        if get_price(row) == 0:
            issues.append({"uid": uid, "row": idx + 1, "level": "warning", "issue": "Missing or zero price"})
        category = normalise_text(row.get("category"))
        if not category or category.lower() == "uncategorised":
            issues.append({"uid": uid, "row": idx + 1, "level": "warning", "issue": "Missing category"})
        pid = product_id_value(row).lower()
        if normalise_text(row.get("variant_enabled")).lower() != "yes" and pid and id_counts.get(pid, 0) > 1:
            issues.append({"uid": uid, "row": idx + 1, "level": "error", "issue": "Duplicate product ID on single item rows"})

    return issues


def products_to_workbook(products: List[Dict[str, Any]]) -> io.BytesIO:
    yoco_rows = [product_to_yoco_row(row) for row in products]
    products_df = pd.DataFrame(yoco_rows, columns=YOCO_PRODUCTS_COLUMNS)
    issues_df = pd.DataFrame(build_issues(products))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        products_df.to_excel(writer, index=False, sheet_name="Products")
        issues_df.to_excel(writer, index=False, sheet_name="Issues")
    output.seek(0)
    return output


@app.get("/")
def health_check():
    return jsonify({
        "status": "ok",
        "service": "Yoco retail file processor",
        "endpoints": [
            "POST /process-retail-file-json",
            "POST /process-retail-file",
            "POST /export-yoco-file",
        ],
    })


@app.post("/process-retail-file-json")
def process_retail_file_json():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Use multipart field name 'file'."}), 400

    uploaded_file = request.files["file"]
    try:
        raw_products = parse_uploaded_file(uploaded_file)
        products = preflight_products_for_frontend(raw_products)
        issues = build_issues(products)
        return jsonify({
            "products": products,
            "issues": issues,
            "summary": {
                "raw_rows": len(raw_products),
                "products": len(products),
                "removed_true_duplicates": len(raw_products) - len(products),
                "errors": sum(1 for i in issues if i["level"] == "error"),
                "warnings": sum(1 for i in issues if i["level"] == "warning"),
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/process-retail-file")
def process_retail_file_xlsx():
    """Legacy/download endpoint: upload file, get Yoco XLSX back.

    Your updated frontend should normally call /process-retail-file-json first,
    let the user fix issues, then call /export-yoco-file for final download.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Use multipart field name 'file'."}), 400

    uploaded_file = request.files["file"]
    try:
        raw_products = parse_uploaded_file(uploaded_file)
        products = preflight_products_for_frontend(raw_products)
        output = products_to_workbook(products)
        return send_file(
            output,
            as_attachment=True,
            download_name="yoco_import_ready.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/export-yoco-file")
def export_yoco_file():
    """Export final frontend-edited products to Yoco XLSX.

    Expects JSON body:
    {
      "products": [ ... frontend edited rows ... ]
    }
    """
    payload = request.get_json(silent=True) or {}
    products = payload.get("products")
    if not isinstance(products, list):
        return jsonify({"error": "JSON body must include products: []"}), 400

    try:
        # Run preflight again as a safety net before export.
        products = preflight_products_for_frontend(products)
        output = products_to_workbook(products)
        return send_file(
            output,
            as_attachment=True,
            download_name="yoco_import_ready.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
