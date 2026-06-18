import os
import re
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import pandas as pd
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
CORS(app, resources={r"/*": {"origins": allowed_origins.split(",") if allowed_origins != "*" else "*"}})

ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}

# Based on the filled Yoco export/template supplied by the user.
# Keep this order exact.
YOCO_COLUMNS = [
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

ISSUE_COLUMNS = [
    "Source Sheet",
    "Row Number Approx",
    "Product Name",
    "Product ID",
    "SKU",
    "Barcode",
    "Issues",
]

# Supplier/retail column aliases. Add to these lists as new supplier formats appear.
FIELD_SYNONYMS = {
    "product_id": ["product id"],
    "product_name": [
        "product name", "item", "item description", "stock item", "article", "name", "description", "desc",
    ],
    "description": ["long description", "description 2", "details"],
    "brand": ["brand", "manufacturer", "make"],
    "category": ["category", "department", "dept", "group", "product group", "supplier", "source sheet"],
    "sku": ["sku", "product code", "prod code", "item code", "plu", "stock code", "code"],
    "barcode": ["barcode", "bar code", "ean", "ean13", "gtin", "upc"],
    "selling_price": [
        "default price", "variant price", "selling price", "sell price", "retail", "retail price", "price", "inc vat", "incl vat", "incl", "rsp",
    ],
    "cost_price": ["default cost price", "cost", "cost price", "ex vat", "excl vat", "nett", "net", "buy price", "purchase price"],
    "stock_quantity": ["stock", "stock qty", "stock quantity", "qty", "quantity", "on hand", "received", "stock received"],
    "vat_enabled": ["vat enabled", "vat", "tax", "tax type"],
    "variant_enabled": ["variant enabled", "has variants", "variant"],
    "attribute_1": ["attribute 1", "option name", "variant attribute", "size type"],
    "value_1": ["value 1", "option value", "variant value", "size", "pack size"],
    "image_url": ["image url", "image", "photo", "picture"],
    "track_stock": ["track stock", "stock tracking"],
    "modifier_group": ["modifier group", "modifiers"],
}

HEADER_KEYWORDS = set(sum(FIELD_SYNONYMS.values(), [])) | set(c.lower() for c in YOCO_COLUMNS)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def clean_text(value) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_header(value) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(value: str) -> str:
    text = clean_text(value).lower()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "product"


def parse_money(value) -> Optional[float]:
    if pd.isna(value) or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).replace("R", "").replace(",", "").strip()
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", ".", "-"}:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def format_money(value) -> str:
    parsed = parse_money(value)
    if parsed is None:
        return ""
    return f"{parsed:.2f}"


def parse_quantity(value) -> Optional[float]:
    if pd.isna(value) or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if text in {"", ".", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clean_code(value) -> str:
    text = clean_text(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def yes_no(value, default: str = "No") -> str:
    text = clean_text(value).lower()
    if not text:
        return default
    if text in {"yes", "y", "true", "1", "vat", "enabled"}:
        return "Yes"
    if text in {"no", "n", "false", "0", "disabled"}:
        return "No"
    return default


def variant_enabled(value) -> str:
    text = clean_text(value)
    if not text:
        return "No"
    if text.lower() in {"yes", "y", "true", "1"}:
        return "yes"
    if text.lower() in {"no", "n", "false", "0"}:
        return "No"
    return text


def track_stock_value(value) -> str:
    text = clean_text(value)
    if text in {"Product", "Variant"}:
        return text
    return "Product"


def find_header_row(raw: pd.DataFrame, max_scan_rows: int = 30) -> int:
    best_row = 0
    best_score = -1.0
    for idx in range(min(max_scan_rows, len(raw))):
        row = [normalize_header(v) for v in raw.iloc[idx].tolist()]
        score = 0.0
        for cell in row:
            if not cell:
                continue
            for keyword in HEADER_KEYWORDS:
                if keyword == cell or keyword in cell:
                    score += 1
                    break
        non_empty = sum(1 for c in row if c)
        score += min(non_empty, 10) * 0.1
        if score > best_score:
            best_score = score
            best_row = idx
    return best_row


def dedupe_columns(columns: List[str]) -> List[str]:
    seen = {}
    output = []
    for col in columns:
        base = normalize_header(col) or "unnamed"
        seen[base] = seen.get(base, 0) + 1
        output.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return output


def map_columns(df: pd.DataFrame) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    normalized_cols = {normalize_header(c): c for c in df.columns}

    # Pass 1: exact matches. This prevents broad words like "description"
    # or "code" from beating stronger columns like "Product Name" or "Product Code".
    for target, synonyms in FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in normalized_cols:
                mapping[target] = normalized_cols[synonym]
                break

    # Pass 2: contains matches, only when no exact match exists.
    broad_synonyms = {"product", "code", "name", "description", "desc"}
    for target, synonyms in FIELD_SYNONYMS.items():
        if target in mapping:
            continue
        for norm_col, original_col in normalized_cols.items():
            for synonym in synonyms:
                if synonym in broad_synonyms:
                    continue
                if synonym in norm_col:
                    mapping[target] = original_col
                    break
            if target in mapping:
                break
    return mapping


def read_workbook(file_storage, filename: str) -> Dict[str, pd.DataFrame]:
    ext = filename.rsplit(".", 1)[1].lower()
    if ext == "csv":
        return {"CSV Upload": pd.read_csv(file_storage, header=None, dtype=object)}
    return pd.read_excel(file_storage, sheet_name=None, header=None, dtype=object)


def get_series(df: pd.DataFrame, mapping: Dict[str, str], field: str, default=""):
    if field in mapping:
        return df[mapping[field]]
    return pd.Series([default] * len(df), index=df.index)


def normalize_sheet(sheet_name: str, raw: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty:
        return pd.DataFrame(columns=YOCO_COLUMNS), pd.DataFrame(columns=ISSUE_COLUMNS)

    header_idx = find_header_row(raw)
    headers = dedupe_columns(raw.iloc[header_idx].tolist())
    df = raw.iloc[header_idx + 1:].copy()
    df.columns = headers
    df = df.dropna(how="all")

    if df.empty:
        return pd.DataFrame(columns=YOCO_COLUMNS), pd.DataFrame(columns=ISSUE_COLUMNS)

    mapping = map_columns(df)

    product_name = get_series(df, mapping, "product_name").map(clean_text)
    product_id = get_series(df, mapping, "product_id").map(clean_code)
    description = get_series(df, mapping, "description").map(clean_text)
    brand = get_series(df, mapping, "brand").map(clean_text)
    category = get_series(df, mapping, "category", sheet_name).map(clean_text).replace("", sheet_name)
    sku = get_series(df, mapping, "sku").map(clean_code)
    barcode = get_series(df, mapping, "barcode").map(clean_code)
    default_price = get_series(df, mapping, "selling_price").map(format_money)
    cost_price = get_series(df, mapping, "cost_price").map(format_money)
    vat = get_series(df, mapping, "vat_enabled", "Yes").map(lambda v: yes_no(v, "Yes"))
    variant_flag = get_series(df, mapping, "variant_enabled", "No").map(variant_enabled)
    attribute_1 = get_series(df, mapping, "attribute_1").map(clean_text)
    value_1 = get_series(df, mapping, "value_1").map(clean_text)
    image_url = get_series(df, mapping, "image_url").map(clean_text)
    track_stock = get_series(df, mapping, "track_stock", "Product").map(track_stock_value)
    modifier_group = get_series(df, mapping, "modifier_group").map(clean_text)

    yoco = pd.DataFrame(index=df.index)
    yoco["Product ID"] = product_id.where(product_id.str.strip().ne(""), product_name.map(slugify))
    yoco["Product Name"] = product_name
    yoco["Description"] = description
    yoco["Default Price"] = default_price
    yoco["Brand"] = brand
    yoco["Category"] = category
    yoco["SKU"] = sku
    yoco["Default Cost Price"] = cost_price.replace("", "0.00")
    yoco["Ask For Quantity"] = "No"
    yoco["Default Quantity"] = "1"
    yoco["Quantity Units"] = ""
    yoco["Ask For Price"] = "No"
    yoco["VAT Enabled"] = vat
    yoco["Variant Price"] = default_price
    yoco["Variant Enabled"] = variant_flag
    yoco["Attribute 1"] = attribute_1
    yoco["Value 1"] = value_1
    yoco["Attribute 2"] = ""
    yoco["Value 2"] = ""
    yoco["Attribute 3"] = ""
    yoco["Value 3"] = ""
    yoco["Image URL"] = image_url
    yoco["Barcode"] = barcode
    yoco["Track Stock"] = track_stock
    yoco["Modifier Group"] = modifier_group

    # Remove obvious repeated header/footer rows and completely blank product lines.
    yoco = yoco[yoco["Product Name"].str.lower().ne("item")]
    yoco = yoco[yoco["Product Name"].str.lower().ne("description")]
    yoco = yoco[yoco["Product Name"].str.strip().ne("")]

    issues = []
    for idx, row in yoco.iterrows():
        row_issues = []
        if not clean_text(row.get("Product Name")):
            row_issues.append("Missing product name")
        if not clean_text(row.get("Product ID")):
            row_issues.append("Missing product ID")
        if not clean_text(row.get("Barcode")) and not clean_text(row.get("SKU")):
            row_issues.append("Missing barcode and SKU")
        if not clean_text(row.get("Default Price")):
            row_issues.append("Missing default price")
        if not clean_text(row.get("Variant Price")):
            row_issues.append("Missing variant price")
        if row_issues:
            issues.append({
                "Source Sheet": sheet_name,
                "Row Number Approx": int(idx) + header_idx + 2,
                "Product Name": row.get("Product Name", ""),
                "Product ID": row.get("Product ID", ""),
                "SKU": row.get("SKU", ""),
                "Barcode": row.get("Barcode", ""),
                "Issues": "; ".join(row_issues),
            })

    return yoco[YOCO_COLUMNS], pd.DataFrame(issues, columns=ISSUE_COLUMNS)


def add_duplicate_issues(yoco_import: pd.DataFrame) -> pd.DataFrame:
    issues = []
    if yoco_import.empty:
        return pd.DataFrame(columns=ISSUE_COLUMNS)

    barcode_series = yoco_import["Barcode"].astype(str).str.strip()
    dup_barcode_mask = yoco_import.duplicated(subset=["Barcode"], keep=False) & barcode_series.ne("")
    for barcode, group in yoco_import.loc[dup_barcode_mask].groupby("Barcode", dropna=False):
        # Yoco exports may repeat a barcode across variants of the same product.
        # Only flag when the same barcode appears across multiple Product IDs.
        product_id_count = group["Product ID"].astype(str).str.strip().nunique()
        if product_id_count <= 1:
            continue
        for _, row in group.iterrows():
            issues.append({
                "Source Sheet": "",
                "Row Number Approx": "",
                "Product Name": row.get("Product Name", ""),
                "Product ID": row.get("Product ID", ""),
                "SKU": row.get("SKU", ""),
                "Barcode": row.get("Barcode", ""),
                "Issues": "Duplicate barcode across multiple products",
            })

    sku_series = yoco_import["SKU"].astype(str).str.strip()
    dup_sku_mask = yoco_import.duplicated(subset=["SKU"], keep=False) & sku_series.ne("")
    for sku, group in yoco_import.loc[dup_sku_mask].groupby("SKU", dropna=False):
        product_id_count = group["Product ID"].astype(str).str.strip().nunique()
        if product_id_count <= 1:
            continue
        for _, row in group.iterrows():
            issues.append({
                "Source Sheet": "",
                "Row Number Approx": "",
                "Product Name": row.get("Product Name", ""),
                "Product ID": row.get("Product ID", ""),
                "SKU": row.get("SKU", ""),
                "Barcode": row.get("Barcode", ""),
                "Issues": "Duplicate SKU across multiple products",
            })

    return pd.DataFrame(issues, columns=ISSUE_COLUMNS)


def process_file(file_storage, filename: str) -> BytesIO:
    sheets = read_workbook(file_storage, filename)
    normalized_frames = []
    issue_frames = []

    for sheet_name, raw in sheets.items():
        normalized, issues = normalize_sheet(str(sheet_name), raw)
        if not normalized.empty:
            normalized_frames.append(normalized)
        if not issues.empty:
            issue_frames.append(issues)

    if normalized_frames:
        products = pd.concat(normalized_frames, ignore_index=True)
    else:
        products = pd.DataFrame(columns=YOCO_COLUMNS)

    duplicate_issues = add_duplicate_issues(products)
    if not duplicate_issues.empty:
        issue_frames.append(duplicate_issues)

    issues = pd.concat(issue_frames, ignore_index=True) if issue_frames else pd.DataFrame(columns=ISSUE_COLUMNS)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        products.to_excel(writer, index=False, sheet_name="Products")
        issues.to_excel(writer, index=False, sheet_name="Issues")

        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            for cell in sheet[1]:
                cell.font = cell.font.copy(bold=True)
            for column_cells in sheet.columns:
                max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 42)

    output.seek(0)
    return output


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "Yoco Retail Import Processor",
        "template": "Yoco Products export/import format",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


@app.get("/template-columns")
def template_columns():
    return jsonify({"sheet": "Products", "columns": YOCO_COLUMNS})


@app.post("/process-retail-file")
def process_retail_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Send multipart/form-data with field name 'file'."}), 400

    uploaded = request.files["file"]
    filename = secure_filename(uploaded.filename or "")

    if not filename or not allowed_file(filename):
        return jsonify({"error": "Unsupported file type. Upload .xlsx, .xls, or .csv."}), 400

    try:
        output = process_file(uploaded, filename)
    except Exception as exc:
        return jsonify({"error": "Processing failed", "details": str(exc)}), 500

    base_name = filename.rsplit(".", 1)[0]
    download_name = f"{base_name}_yoco_products_import.xlsx"

    return send_file(
        output,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
