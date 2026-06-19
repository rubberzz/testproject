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

# Explicit CORS for browser/blob origins used by the dashboard preview.
# This prevents opaque "TypeError: Failed to fetch" failures when the
# dashboard is opened from a blob/usercontent origin or local file preview.
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    supports_credentials=False,
    expose_headers=["Content-Disposition", "Content-Type"],
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
)

@app.after_request
def add_cors_headers(response):
    return make_cors_response(response)


@app.before_request
def handle_preflight_options():
    # Some blob/usercontent preview origins send an OPTIONS preflight before
    # multipart uploads. Answer it explicitly so the browser does not report
    # an opaque "TypeError: Failed to fetch".
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        response.headers["Access-Control-Max-Age"] = "86400"
        return response


def compact_product_for_frontend(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return only fields the dashboard needs.

    The previous JSON response was about 5 MB for the Lasher workbook. Some
    browser preview/sandbox environments fail large cross-origin POST responses
    as a generic `TypeError: Failed to fetch`. Keeping the response slim makes
    uploads much more reliable while preserving all export-relevant fields.
    """
    keep = [
        "_uid",
        "product_id",
        "product_name",
        "description",
        "selling_price",
        "calculated_price",
        "cost_price",
        "category",
        "brand",
        "barcode",
        "sku",
        "variant_enabled",
        "attr1_name",
        "attr1_val",
        "attr2_name",
        "attr2_val",
        "attr3_name",
        "attr3_val",
        "image_url",
        "vat_enabled",
        "track_stock",
        "source_sheet",
        "source_row",
        "unique_id",
        "composite_key",
        "category_code_key",
        "retail_identity_key",
        "_casePrice",
        "_packQty",
        "_originalCasePrice",
        "_basePrice",
        "_correctedPrice",
        "_caseApproved",
        "_caseSuggestionBasis",
    ]
    out: Dict[str, Any] = {}
    for key in keep:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        if value == "":
            continue
        out[key] = value
    return out


def compact_conflict_option_for_frontend(option: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a price-conflict option into the same row shape the frontend expects.

    build_price_conflict_payload stores useful values such as price/cost/title at
    the option wrapper level and the original product at option["row"]. The old
    compacting function only kept row-like keys, dropping price/cost/title and
    causing the UI to render R 0.00. This function preserves both.
    """
    source_row = option.get("row") if isinstance(option.get("row"), dict) else {}
    row = dict(source_row)

    uid = normalise_text(option.get("uid") or row.get("_uid"))
    title = normalise_text(option.get("title") or row.get("product_name") or row.get("Product Name"))
    price = parse_money(option.get("price") if option.get("price") is not None else row.get("calculated_price") or row.get("selling_price"))
    cost = parse_money(option.get("cost") if option.get("cost") is not None else row.get("cost_price") or row.get("Default Cost Price"))
    code = clean_code(option.get("code") or option.get("barcode") or row.get("barcode") or row.get("Barcode") or row.get("sku") or row.get("SKU"))

    if uid:
        row["_uid"] = uid
    if title:
        row["product_name"] = title
        row["Product Name"] = title
    if option.get("product_id"):
        row["product_id"] = option.get("product_id")
        row["Product ID"] = option.get("product_id")
    if option.get("category"):
        row["category"] = option.get("category")
        row["Category"] = option.get("category")
    if code:
        row["barcode"] = code
        row["Barcode"] = code
        row["sku"] = row.get("sku") or code
        row["SKU"] = row.get("SKU") or code
    row["selling_price"] = price
    row["calculated_price"] = price
    row["cost_price"] = cost
    row["Default Cost Price"] = cost
    row["unique_id"] = option.get("unique_id") or row.get("unique_id")
    row["composite_key"] = option.get("composite_key") or row.get("composite_key")
    row["category_code_key"] = option.get("composite_key") or row.get("category_code_key")
    row["source_sheet"] = option.get("source_sheet") or row.get("source_sheet")
    row["source_row"] = option.get("source_row") or row.get("source_row")

    compact = compact_product_for_frontend(row)
    # Keep wrapper fields too because the existing JS reads option.price/cost/title.
    compact.update({
        "uid": uid or compact.get("_uid"),
        "title": title or compact.get("product_name"),
        "price": price,
        "cost": cost,
        "code": code,
        "row": compact_product_for_frontend(row),
    })
    return compact


def compact_conflict_for_frontend(conflict: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        key: conflict.get(key)
        for key in ["conflict_id", "code", "title", "category", "reason", "message", "unique_id", "composite_key"]
        if conflict.get(key) not in (None, "")
    }
    out["options"] = [compact_conflict_option_for_frontend(option) for option in conflict.get("options", [])]
    return out


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    # Always return JSON + CORS instead of letting the connection die silently.
    import traceback
    app.logger.exception("Unhandled backend error")
    return cors_json({
        "error": str(exc),
        "type": exc.__class__.__name__,
        "traceback_tail": traceback.format_exc().splitlines()[-12:],
    }, 500)


MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Lightweight in-memory store for conflict choices from the frontend.
# For production you can replace this with Firestore/Postgres/S3/etc.
PRICE_CONFLICT_DECISIONS: Dict[str, Dict[str, Any]] = {}



def make_cors_response(response):
    """Force CORS headers on every response, including error responses."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Type"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


def cors_json(payload: Dict[str, Any], status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    return make_cors_response(response)


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
        # Highest priority: final price the shop should charge.
        "selling", "selling price", "sell price", "sale price", "retail/trade",
        "retail trade", "retail", "retail price", "trade price", "dealer price",
        "customer price", "shelf price", "rrp", "list price", "each price",
        # Lower priority: price including VAT; only use when no final Selling/Retail column exists.
        "price", "price incl", "price inc", "incl vat", "inc vat", "including vat", "vat incl",
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

def clean_product_title(value: Any) -> str:
    """Clean product/category text at extraction time.

    This keeps source wording intact, but fixes recurring OCR/AI artefacts before
    rows reach variant inference or the frontend table.
    """
    text = normalise_text(value)
    if not text:
        return ""
    text = text.replace("×", "x")
    # Common Gemini/OCR error in liquor lists: "@&" or "@ &" should be "&".
    text = re.sub(r"@\s*&", "&", text)
    text = re.sub(r"&\s*&", "&", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Cosmetic spacing around punctuation.
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s*([&/])\s*", r" \1 ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Values that are often product descriptors/brand text, not true variants, when
# an AI/parser has produced rows like Product="Black 750ml", Value="Label".
# Those should become standalone names such as "Black Label 750ml".
_NOT_SAFE_AS_IMPLIED_VARIANT = {
    "label", "black label", "double malt", "lager", "lite", "milk stout", "stout",
    "scotch", "scotch whisky", "white scotch whisky", "whisky", "gin", "vodka", "rum",
    "crown", "extra", "pilsner", "draught", "draft", "premium", "original",
}
_SAFE_IMPLIED_VARIANT_WORDS = {
    "apple", "manic mango", "mango", "ruby", "ruby apple", "blackberry", "watermelon",
    "cranberry", "peach", "strawberry", "hawaiian", "hawian", "margarita", "pina colada",
    "tropical", "orange", "lemon", "pressed lemon", "berry", "red berries", "wild berry",
    "dry", "gold", "spin", "storm", "citrus", "classic", "blush", "mimosa", "cola",
    "tonic", "pink tonic", "pink & tonic", "guarana",
}

_CLOTHING_SIZE_TOKEN_RE = re.compile(r"\b(?:XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL|6XL|Small|Medium|Large)\b", re.I)
_CLOTHING_NUMERIC_SIZE_RE = re.compile(r"(?:^|/)\s*\d{2,3}\s*(?:$|/)")
_TRAILING_COLOUR_RE = re.compile(
    r"\b("
    r"black|white|olive|green|khaki|navy|blue|red|grey|gray|charcoal|stone|sand|tan|brown|"
    r"beige|cream|orange|yellow|purple|pink|maroon|burgundy|camo|camouflage|denim"
    r")\s*$",
    re.I,
)


def _looks_like_clothing_size_variant_value(value: Any) -> bool:
    """True for variant values such as 'olive / S / 36' or 'XL / 48'.

    The muddled-name repair step must not convert these back to singles.
    """
    text = clean_product_title(value)
    if not text or "/" not in text:
        return False
    return bool(_CLOTHING_SIZE_TOKEN_RE.search(text) or _CLOTHING_NUMERIC_SIZE_RE.search(text))


def split_trailing_colour_from_base(base: str) -> Tuple[str, str]:
    """Move a trailing colour from a flattened Shopify title into the variant value.

    Example:
      base='Men\'s Bush Shirt: Long-Sleeve (Tech) olive'
      -> ('Men\'s Bush Shirt: Long-Sleeve (Tech)', 'olive')
    """
    base = clean_product_title(base)
    match = _TRAILING_COLOUR_RE.search(base)
    if not match:
        return base, ""
    colour = match.group(1).strip()
    clean_base = base[:match.start()].strip(" -·•,/\\")
    if len(clean_base) < 4:
        return base, ""
    return clean_base, colour


def _is_suspicious_embedded_descriptor(value: Any) -> bool:
    v = clean_product_title(value).lower()
    v_norm = normalise_for_compare(v)
    if not v_norm:
        return False
    # Values such as 'olive / S / 36' are true clothing variants, not muddled product descriptors.
    if _looks_like_clothing_size_variant_value(value):
        return False
    if v_norm in {normalise_for_compare(x) for x in _SAFE_IMPLIED_VARIANT_WORDS}:
        return False
    if v_norm in {normalise_for_compare(x) for x in _NOT_SAFE_AS_IMPLIED_VARIANT}:
        return True
    # Product descriptors with liquor/category terms should stay in the name.
    if re.search(r"\b(label|malt|lager|lite|stout|whisky|whiskey|scotch|vodka|gin|rum|draught|draft|pilsner)\b", v, re.I):
        return True
    # Long phrases are usually not simple flavour/size variants.
    return len(v.split()) >= 3


def _insert_descriptor_before_trailing_size(base_name: str, descriptor: str) -> str:
    base = clean_product_title(base_name)
    desc = clean_product_title(descriptor)
    if not base or not desc:
        return clean_product_title(" ".join([base, desc]))
    base = re.sub(r"\s*-\s*$", "", base).strip()
    # Avoid duplicating if the descriptor is already present.
    if normalise_for_compare(desc) in normalise_for_compare(base):
        return base
    size_match = re.search(r"\b\d+(?:[,.]\d+)?\s*(?:ml|l|lt|litre|liter|g|kg)\b\s*$", base, re.I)
    if size_match:
        prefix = base[:size_match.start()].strip()
        size = size_match.group(0).strip()
        return clean_product_title(f"{prefix} {desc} {size}")
    return clean_product_title(f"{base} {desc}")


def repair_muddled_extracted_variant_names(products: List[Dict[str, Any]]) -> int:
    """Undo over-aggressive variant splitting from AI/original extraction.

    Some supplier rows like "Black Label 750ml" were incorrectly returned as
    product_name="Black 750ml", Value 1="Label". This converts only suspicious
    descriptor variants back into standalone products while preserving real
    dash/flavour variants such as "Brutal Fruit 620ml - apple".
    """
    changed = 0
    for row in products:
        if not row_is_variant(row):
            # Still clean simple text artefacts.
            cleaned = clean_product_title(title_value(row))
            if cleaned and cleaned != title_value(row):
                set_title(row, cleaned)
                changed += 1
            continue
        attr_values = [
            row.get("attr1_val") or row.get("Value 1"),
            row.get("attr2_val") or row.get("Value 2"),
            row.get("attr3_val") or row.get("Value 3"),
        ]
        values = [clean_product_title(v) for v in attr_values if clean_product_title(v)]
        if not values:
            continue
        # Repair only one-axis suspicious descriptor rows. Multi-axis Shopify variants remain intact.
        if len(values) == 1 and _is_suspicious_embedded_descriptor(values[0]):
            new_title = _insert_descriptor_before_trailing_size(title_value(row), values[0])
            if new_title:
                set_title(row, new_title)
                set_product_id(row, slugify(new_title or best_code(row)))
            set_variant_fields(row, False)
            clear_variant_attributes(row)
            price = get_price(row)
            if price > 0:
                row["selling_price"] = price
                row["calculated_price"] = price
                row["variant_price"] = price
            changed += 1
        else:
            cleaned_title = clean_product_title(title_value(row))
            if cleaned_title and cleaned_title != title_value(row):
                set_title(row, cleaned_title)
                changed += 1
            # Clean attribute values too, especially @& -> &.
            for idx in range(1, 4):
                key = f"attr{idx}_val"
                ykey = f"Value {idx}"
                val = row.get(key) or row.get(ykey)
                cval = clean_product_title(val)
                if cval and cval != normalise_text(val):
                    row[key] = row[ykey] = cval
                    changed += 1
    return changed


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
    """Return the best available code/barcode for a row.

    Performance note: this function is called thousands of times during preflight.
    Cache the result on the row so we do not repeatedly clean/sort/regex the same
    values. Also avoid sorted() because max() is cheaper and enough here.
    """
    cached = row.get("_best_code")
    if isinstance(cached, str):
        return cached

    candidates = [
        row.get("barcode"),
        row.get("sku"),
        row.get("code"),
        row.get("Barcode"),
        row.get("SKU"),
        row.get("Product Code"),
        row.get("Code"),
        row.get("product_code"),
        row.get("manufacturing_code"),
    ]

    cleaned: List[str] = []
    seen = set()
    for candidate in candidates:
        c = clean_code(candidate)
        if not c or c in seen:
            continue
        seen.add(c)
        cleaned.append(c)

    if not cleaned:
        row["_best_code"] = ""
        return ""

    # Prefer the longest numeric/scannable code, then longest overall string.
    # Use str(c) defensively because pandas/numpy scalars can leak into rows.
    best = max(cleaned, key=lambda c: (sum(ch.isdigit() for ch in str(c)), len(str(c))))
    row["_best_code"] = str(best)
    return row["_best_code"]


def get_price(row: Dict[str, Any]) -> float:
    cached = row.get("_price")
    if isinstance(cached, (int, float)) and not (isinstance(cached, float) and math.isnan(cached)):
        return float(cached)
    for key in ["calculated_price", "selling_price", "variant_price", "Default Price", "Variant Price", "price"]:
        if key in row and normalise_text(row.get(key)) != "":
            price = parse_money(row.get(key))
            if price != 0:
                return price
    return 0.0


def get_cost(row: Dict[str, Any]) -> float:
    cached = row.get("_cost")
    if isinstance(cached, (int, float)) and not (isinstance(cached, float) and math.isnan(cached)):
        return float(cached)
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



def category_code_key(row: Dict[str, Any]) -> str:
    """Composite identity: active category + code.

    This is intentionally category-aware so the same supplier/manufacturing code
    under different sheet/category headers remains a separate retail item.
    """
    cached = row.get("category_code_key") or row.get("composite_key")
    if isinstance(cached, str) and cached:
        return cached

    category = normalise_text(
        row.get("active_category")
        or row.get("_active_category")
        or row.get("block_category")
        or row.get("category")
        or row.get("Category")
        or row.get("source_sheet")
        or row.get("Source Sheet")
    )
    code = clean_code(row.get("_best_code") or best_code(row)).lower()
    if not code:
        return ""
    key = f"{slugify(category) or 'uncategorised'}_{code}"
    row["category_code_key"] = key
    row["composite_key"] = key
    # Keep unique_id at category+code level unless a retail_identity_key is later
    # set for rows that need title-level separation.
    row["unique_id"] = row.get("unique_id") or key
    return key


def pricing_conflict_identity(row: Dict[str, Any]) -> str:
    """Identity used for pricing conflicts: category + code + product title.

    Some supplier sheets reuse the same code for different physical items. We
    should not ask the user to choose a price between different products. A
    pricing conflict should only appear when the same category+code+title has
    conflicting prices/costs.
    """
    base = category_code_key(row)
    title_key = slugify(title_value(row))
    if not base:
        return ""
    key = f"{base}_{title_key}" if title_key else base
    row["retail_identity_key"] = key
    return key


def set_category_code_identity(row: Dict[str, Any], category: str = "") -> None:
    """Store a stable composite key used by frontend/backends/database inserts."""
    if category:
        row["active_category"] = category
        row["_active_category"] = category
    key = category_code_key(row)
    if key:
        row["unique_id"] = key
        row["composite_key"] = key
        row["category_code_key"] = key
        row["retail_identity_key"] = pricing_conflict_identity(row)


def raw_cell_text(raw_df: pd.DataFrame, raw_index: int, col_index: int) -> str:
    try:
        if raw_index in raw_df.index and raw_df.shape[1] > col_index:
            return normalise_text(raw_df.loc[raw_index].iloc[col_index])
    except Exception:
        pass
    return ""


def looks_like_category_cell(value: str, row_title: str) -> bool:
    """True when Column A looks like a category/state value, not the product itself."""
    text = normalise_text(value)
    if not text:
        return False
    text_norm = normalise_for_compare(text)
    title_norm = normalise_for_compare(row_title)
    if text_norm == title_norm:
        return False
    # If Column A is just the first part of a split product name (e.g.
    # "Denova" + "tissue" -> "Denova tissue"), do not treat it as a category.
    if title_norm.startswith(text_norm + " "):
        return False
    if parse_money(text) > 0:
        return False
    # Category labels are generally short text labels, not long item descriptions.
    if len(text) > 60:
        return False
    # Avoid treating a plain numeric/product code cell as a category.
    if re.fullmatch(r"[A-Za-z]{0,4}\d+[A-Za-z0-9*\-_/]*", text):
        return False
    return True

def true_duplicate_key(row: Dict[str, Any]) -> Tuple[str, str, str, str, float, float]:
    """Identity for true duplicate removal.

    Earlier versions only deduped rows that had a barcode/code. That left
    duplicated no-code rows in sectioned price lists, and the later duplicate-ID
    fixer made their product names ugly by appending source context and row
    numbers. A true duplicate can still be safely removed without a code when
    category + title + variant value + price + cost are identical.
    """
    code = clean_code(row.get("_best_code") or best_code(row)).lower()
    category = normalise_text(row.get("active_category") or row.get("_active_category") or row.get("category") or row.get("Category") or row.get("source_sheet") or row.get("Source Sheet"))
    title_key = normalise_for_compare(title_value(row))
    variant_key = normalise_for_compare("|".join([
        normalise_text(row.get("attr1_name") or row.get("Attribute 1")),
        normalise_text(row.get("attr1_val") or row.get("Value 1")),
        normalise_text(row.get("attr2_name") or row.get("Attribute 2")),
        normalise_text(row.get("attr2_val") or row.get("Value 2")),
        normalise_text(row.get("attr3_name") or row.get("Attribute 3")),
        normalise_text(row.get("attr3_val") or row.get("Value 3")),
    ])) if row_is_variant(row) else ""
    identity = category_code_key(row) or code or f"{slugify(category) or 'uncategorised'}_{title_key}"
    return (
        identity,
        title_key,
        variant_key,
        code,
        round(get_price(row), 2),
        round(get_cost(row), 2),
    )



def get_category(row: Dict[str, Any]) -> str:
    return normalise_text(row.get("category") or row.get("Category"))


def get_export_title(row: Dict[str, Any]) -> str:
    return title_value(row)


def build_price_conflict_payload(rows: List[Dict[str, Any]], group_index: int) -> Dict[str, Any]:
    """Build the compact conflict object consumed by the frontend."""
    code = best_code(rows[0])
    composite_key = category_code_key(rows[0])
    active_category = normalise_text(rows[0].get("active_category") or rows[0].get("_active_category") or rows[0].get("category") or rows[0].get("Category"))
    title_counts: Dict[str, int] = {}
    for row in rows:
        title = get_export_title(row) or "Untitled product"
        title_counts[title] = title_counts.get(title, 0) + 1
    title = sorted(title_counts.items(), key=lambda item: item[1], reverse=True)[0][0]

    options = []
    for row in rows:
        uid = normalise_text(row.get("_uid")) or row_uid(row, len(options))
        row["_uid"] = uid
        option_row = dict(row)
        options.append({
            "uid": uid,
            "title": get_export_title(row),
            "category": get_category(row),
            "code": best_code(row),
            "unique_id": category_code_key(row),
            "composite_key": category_code_key(row),
            "price": get_price(row),
            "cost": get_cost(row),
            "product_id": product_id_value(row),
            "barcode": clean_code(row.get("barcode") or row.get("Barcode") or best_code(row)),
            "sku": clean_code(row.get("sku") or row.get("SKU")),
            "source_sheet": normalise_text(row.get("source_sheet") or row.get("Source Sheet")),
            "source_row": row.get("source_row") or row.get("Source Row") or "",
            "row": option_row,
        })

    return {
        "type": "price_conflict",
        "conflict_id": hashlib.sha1(((composite_key or clean_code(code).lower()) + "::" + str(group_index)).encode("utf-8")).hexdigest()[:16],
        "title": title,
        "category": active_category,
        "code": clean_code(code),
        "unique_id": composite_key,
        "composite_key": composite_key,
        "message": f"Pricing Conflict found for {title} (Category: {active_category or 'Uncategorised'}, Barcode: {clean_code(code)})",
        "options": options,
        "option_count": len(options),
    }




def is_price_conflict_code_eligible(code: str, rows: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Avoid grouping generic stock/treatment codes as barcode price conflicts.

    Codes like CCA appear on 100+ different timber rows and are not a unique
    manufacturing/scanning code. Strong candidates are real barcodes or long
    supplier/manufacturing codes.
    """
    code = clean_code(code)
    if not code:
        return False
    digits = re.sub(r"\D", "", code)
    if len(digits) >= 8:
        return True
    if len(code) >= 8 and len(digits) >= 4:
        return True
    # If a code appears on a very large number of dissimilar rows, treat it as generic.
    if rows and len(rows) > 20:
        return False
    return False

def split_price_conflicts(products: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Remove pricing/code collisions from the flat products list and return them as grouped conflicts.

    A pricing collision is two or more rows with the same composite identity
    (active_category + manufacturing/scanning code) but different price and/or cost metrics.
    The same code in different categories is deliberately kept separate.
    """
    by_identity: Dict[str, List[Dict[str, Any]]] = {}
    for row in products:
        # Use title-aware retail identity for conflict detection. The same code
        # can appear under the same supplier category for different items; those
        # must remain separate products, not a forced price-choice conflict.
        identity = row.get("retail_identity_key") or pricing_conflict_identity(row)
        if not identity:
            continue
        by_identity.setdefault(identity, []).append(row)

    conflict_uids = set()
    conflicts: List[Dict[str, Any]] = []
    for group_index, rows in enumerate(by_identity.values()):
        if len(rows) < 2:
            continue
        code = rows[0].get("_best_code") or best_code(rows[0])
        if not is_price_conflict_code_eligible(code, rows):
            continue
        price_cost_signatures = {
            (round(get_price(row), 2), round(get_cost(row), 2))
            for row in rows
        }
        price_values = {round(get_price(row), 2) for row in rows}
        # Primary trigger is different price. Cost difference is also included because
        # supplier files often expose the same problem as different cost metrics.
        if len(price_values) <= 1 and len(price_cost_signatures) <= 1:
            continue
        conflict = build_price_conflict_payload(rows, group_index)
        conflicts.append(conflict)
        for option in conflict["options"]:
            conflict_uids.add(option["uid"])

    if not conflicts:
        return products, []

    remaining = [row for row in products if normalise_text(row.get("_uid")) not in conflict_uids]
    return remaining, conflicts



# Case/pack detection must be conservative. Many product names contain
# dimensions such as 100x150mm, 40*40*5mm, 6.00MM2X4+E or 25x300.
# Those are NOT pack quantities. We only flag common retail case/pack
# quantities, and only when the syntax looks like packaging language.
_COMMON_PACK_QTYS = {6, 12, 18, 20, 24, 30, 36, 48}
_PACK_WORDS = r"pack|case|carton|ctn|box|tray|dozen|pcs|pieces|units?|count"
_PACK_MULTIPLIER_RE = re.compile(
    rf"(?:^|[\s,;:/\-])(?:x|×)\s*(\d{{1,3}})(?:\s*(?:{_PACK_WORDS}))?(?=$|[\s,;:/\-])",
    re.I,
)
_PACK_WORD_RE = re.compile(
    rf"\b(\d{{1,3}})\s*(?:{_PACK_WORDS})\b",
    re.I,
)
_PACK_OF_RE = re.compile(
    rf"\b(?:pack|case|carton|ctn|box|tray)\s+of\s+(\d{{1,3}})\b",
    re.I,
)

_DIMENSION_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:x|×|\*)\s*\d+(?:[.,]\d+)?(?:\s*(?:mm|cm|m|ml|l|kg|g|v|w))?",
    re.I,
)


def _looks_like_dimension_context(value: str, start: int, end: int) -> bool:
    """Return True when the matched x/× is part of a physical dimension."""
    window = value[max(0, start - 18): min(len(value), end + 18)]
    if _DIMENSION_RE.search(window):
        return True
    # Electrical / cable dimensions often look like MM2X4+E or 3CX2.5.
    if re.search(r"\b(?:mm2|mm²|core|cable|surfix|pvc)\b", window, re.I):
        return True
    return False


def _is_common_pack_qty(qty: int) -> bool:
    return qty in _COMMON_PACK_QTYS


def extract_pack_qty_from_text(text: Any) -> Optional[int]:
    """Return a conservative case/pack multiplier.

    Flags clear pack/case syntax like:
      - 100ml x 24
      - x 12 pack
      - 6 pack
      - case of 24

    Does NOT flag dimensions like:
      - 100x150mm
      - 40*40*5mm
      - 6.00MM2X4+E
      - 25x300
    """
    value = normalise_text(text)
    if not value:
        return None

    for regex in (_PACK_OF_RE, _PACK_WORD_RE, _PACK_MULTIPLIER_RE):
        for match in regex.finditer(value):
            try:
                qty = int(match.group(1))
            except Exception:
                continue
            if not _is_common_pack_qty(qty):
                continue
            if regex is _PACK_MULTIPLIER_RE and _looks_like_dimension_context(value, match.start(), match.end()):
                continue
            return qty
    return None


def extract_pack_qty(row: Dict[str, Any]) -> Optional[int]:
    """Detect case quantity from product name, description, or Pack attribute."""
    for key in ["product_name", "Product Name", "title", "description", "Description"]:
        qty = extract_pack_qty_from_text(row.get(key))
        if qty:
            return qty

    # Also support already-structured variant attributes such as Pack Size = x 24.
    attr_pairs = [
        (row.get("attr1_name") or row.get("Attribute 1"), row.get("attr1_val") or row.get("Value 1")),
        (row.get("attr2_name") or row.get("Attribute 2"), row.get("attr2_val") or row.get("Value 2")),
        (row.get("attr3_name") or row.get("Attribute 3"), row.get("attr3_val") or row.get("Value 3")),
    ]
    for name, value in attr_pairs:
        name_text = normalise_text(name).lower()
        if any(word in name_text for word in ["pack", "case", "qty", "quantity", "count", "unit"]):
            qty = extract_pack_qty_from_text(value)
            if qty:
                return qty
    return None


def strip_pack_multiplier(title: Any) -> str:
    """Remove x N / N pack wording while preserving normal size text."""
    text = normalise_text(title)
    if not text:
        return ""
    text = _PACK_MULTIPLIER_RE.sub(" ", text)
    text = _PACK_WORD_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -·•,/\\")


def case_base_key(row: Dict[str, Any]) -> str:
    """Key used to match 'Coke 440ml x24' to 'Coke 440ml'."""
    return normalise_for_compare(strip_pack_multiplier(title_value(row)))


def flag_case_pack_prices(products: List[Dict[str, Any]]) -> int:
    """Flag x N pack/case rows for user review before export.

    We deliberately do NOT auto-change the price. The dashboard onboarding flow
    lets the user choose between keeping the listed price and approving the
    suggested case total. If a matching single-unit row exists, the suggestion is
    single_unit_price * qty. Otherwise the conservative suggestion is
    listed_price * qty.
    """
    single_by_base: Dict[str, List[Dict[str, Any]]] = {}
    for row in products:
        if extract_pack_qty(row):
            continue
        key = normalise_for_compare(title_value(row))
        if key:
            single_by_base.setdefault(key, []).append(row)

    flagged = 0
    for row in products:
        qty = extract_pack_qty(row)
        if not qty:
            continue
        listed = get_price(row)
        if listed <= 0:
            continue

        base_key = case_base_key(row)
        base_rows = [r for r in single_by_base.get(base_key, []) if get_price(r) > 0]
        base_price = get_price(base_rows[0]) if base_rows else 0.0
        suggested = round((base_price if base_price > 0 else listed) * qty, 2)
        basis = "matched_single_unit" if base_price > 0 else "listed_price_times_quantity"

        row["_casePrice"] = True
        row["_packQty"] = qty
        row["_originalCasePrice"] = round(listed, 2)
        row["_basePrice"] = round(base_price, 2) if base_price > 0 else 0.0
        row["_correctedPrice"] = suggested
        row["_caseApproved"] = False
        row["_caseSuggestionBasis"] = basis

        # Case packs must remain separate products, not variants of the single unit.
        set_variant_fields(row, False)
        clear_variant_attributes(row)
        set_product_id(row, slugify(title_value(row) or best_code(row) or f"case-pack-{flagged + 1}"))

        existing_description = normalise_text(row.get("description") or row.get("Description"))
        existing_description = re.sub(r"\s*\[CASE PRICE[^\]]*\]\s*", " ", existing_description, flags=re.I).strip()
        if base_price > 0:
            note = f"[CASE PRICE — verify: listed R{listed:.2f} for x{qty} units. Matching single-unit price R{base_price:.2f}; suggested case total R{suggested:.2f}.]"
        else:
            note = f"[CASE PRICE — verify: listed R{listed:.2f} for x{qty} units. Suggested case total R{suggested:.2f} if listed price is per unit.]"
        row["description"] = f"{existing_description} {note}".strip()
        row["Description"] = row["description"]
        flagged += 1
    return flagged


def _variant_axis_values(row: Dict[str, Any]) -> Tuple[Tuple[str, str], Tuple[str, str], Tuple[str, str]]:
    return (
        (normalise_text(row.get("attr1_name") or row.get("Attribute 1")), normalise_text(row.get("attr1_val") or row.get("Value 1"))),
        (normalise_text(row.get("attr2_name") or row.get("Attribute 2")), normalise_text(row.get("attr2_val") or row.get("Value 2"))),
        (normalise_text(row.get("attr3_name") or row.get("Attribute 3")), normalise_text(row.get("attr3_val") or row.get("Value 3"))),
    )


def _append_variant_axis_value_to_title(title: Any, value: Any) -> str:
    """Add a split-out option value to the product title only when useful."""
    base = clean_product_title(title)
    val = clean_product_title(value)
    if not base:
        return val
    if not val:
        return base
    if normalise_for_compare(val) in normalise_for_compare(base):
        return base
    return clean_product_title(f"{base} - {val}")


def _rewrite_variant_axes_after_dropping_axis(row: Dict[str, Any], drop_axis_idx: int) -> None:
    """Shift Attribute/Value columns left after removing one option axis."""
    remaining: List[Tuple[str, str]] = []
    for idx in range(1, 4):
        if idx == drop_axis_idx:
            continue
        name = normalise_text(row.get(f"attr{idx}_name") or row.get(f"Attribute {idx}"))
        value = normalise_text(row.get(f"attr{idx}_val") or row.get(f"Value {idx}"))
        if name or value:
            remaining.append((name or f"Option {len(remaining) + 1}", value))

    clear_variant_attributes(row)
    if not remaining:
        set_variant_fields(row, False)
        row["track_stock"] = row["Track Stock"] = "Product"
        return

    set_variant_fields(row, True)
    for dest_idx, (name, value) in enumerate(remaining[:3], start=1):
        row[f"attr{dest_idx}_name"] = row[f"Attribute {dest_idx}"] = name
        row[f"attr{dest_idx}_val"] = row[f"Value {dest_idx}"] = value


def _set_default_price_by_product_id(rows: List[Dict[str, Any]]) -> None:
    """Keep Yoco product-level Default Price consistent per variant product."""
    by_pid: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        pid = product_id_value(row)
        if pid:
            by_pid.setdefault(pid, []).append(row)
    for group_rows in by_pid.values():
        if not any(row_is_variant(row) for row in group_rows):
            continue
        prices = [get_price(row) for row in group_rows if get_price(row) > 0]
        default_price = round(min(prices), 2) if prices else 0.0
        for row in group_rows:
            row["selling_price"] = row["Default Price"] = default_price
            row["calculated_price"] = row.get("calculated_price") or row.get("variant_price") or get_price(row) or default_price


def normalise_sparse_variant_matrices_for_yoco(products: List[Dict[str, Any]]) -> int:
    """Make Shopify/Yoco multi-option variant groups import-safe without muddling values.

    The previous implementation collapsed sparse 2-axis Shopify variants into a
    single combined axis, for example Attribute 1 = "Colour / Size" and
    Value 1 = "olive / S / 36". That imports, but it is not a clean Yoco
    variant structure and it makes the product look wrong.

    This version keeps the real option structure by splitting sparse or
    constant-leading-axis groups by the first option axis (normally Colour), then
    moving that value into the product name/Product ID and shifting the remaining
    axes left. Example:
      Product: Men's Bush Shirt, Colour=olive, Size=S / 36
      -> Product: Men's Bush Shirt - olive, Attribute 1=Size, Value 1=S / 36

    Complete multi-axis groups are left alone. Sparse groups become separate
    one-axis products, which avoids Yoco's full-matrix validation problem without
    combining Colour and Size into one value.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in products:
        if not row_is_variant(row):
            continue
        pid = product_id_value(row)
        if pid:
            groups.setdefault(pid, []).append(row)

    changed = 0
    touched_rows: List[Dict[str, Any]] = []

    for _pid, rows in groups.items():
        if len(rows) < 2:
            continue

        axes: List[Dict[str, Any]] = []
        for idx in range(1, 4):
            name_counts: Dict[str, int] = {}
            values: List[str] = []
            seen_values = set()
            for row in rows:
                name = normalise_text(row.get(f"attr{idx}_name") or row.get(f"Attribute {idx}"))
                value = normalise_text(row.get(f"attr{idx}_val") or row.get(f"Value {idx}"))
                if name:
                    name_counts[name] = name_counts.get(name, 0) + 1
                value_key = value.lower()
                if value and value_key not in {"default", "default title"} and value_key not in seen_values:
                    seen_values.add(value_key)
                    values.append(value)
            if values:
                axis_name = sorted(name_counts.items(), key=lambda item: item[1], reverse=True)[0][0] if name_counts else f"Option {idx}"
                axes.append({"idx": idx, "name": axis_name, "values": values})

        if len(axes) < 2:
            continue

        expected = 1
        for axis in axes:
            expected *= max(1, len(axis["values"]))
        actual_combos = {
            tuple(normalise_text(row.get(f"attr{axis['idx']}_val") or row.get(f"Value {axis['idx']}")).lower() for axis in axes)
            for row in rows
        }

        first_axis = axes[0]
        should_split_first_axis = len(first_axis["values"]) == 1 or len(actual_combos) < expected
        if not should_split_first_axis:
            continue

        split_idx = int(first_axis["idx"])
        for row in rows:
            split_value = normalise_text(row.get(f"attr{split_idx}_val") or row.get(f"Value {split_idx}"))
            if not split_value:
                continue
            original_title = title_value(row)
            original_pid = product_id_value(row) or slugify(original_title)
            new_title = _append_variant_axis_value_to_title(original_title, split_value)
            new_pid = slugify(f"{original_pid}-{split_value}")
            set_title(row, new_title)
            set_product_id(row, new_pid)
            _rewrite_variant_axes_after_dropping_axis(row, split_idx)
            touched_rows.append(row)
            changed += 1

    if touched_rows:
        _set_default_price_by_product_id(touched_rows)

    return changed


def make_skus_unique_for_yoco(products: List[Dict[str, Any]]) -> int:
    """Yoco requires SKU to be unique across products/variants.

    Preserve the first occurrence. Later duplicates are suffixed with a stable
    variant/product identifier rather than silently dropped.
    """
    seen: Dict[str, int] = {}
    changed = 0
    for idx, row in enumerate(products, start=1):
        sku = clean_code(row.get("sku") or row.get("SKU"))
        if not sku:
            continue
        key = sku.lower()
        if key not in seen:
            seen[key] = 1
            row["sku"] = sku
            row["SKU"] = sku
            continue
        seen[key] += 1
        suffix_source = variant_label_for_row(row) or product_id_value(row) or str(idx)
        suffix = slugify(suffix_source) or str(seen[key])
        new_sku = f"{sku}-{suffix}"
        # Guard against duplicate suffixes too.
        while new_sku.lower() in seen:
            seen[key] += 1
            new_sku = f"{sku}-{suffix}-{seen[key]}"
        seen[new_sku.lower()] = 1
        row["sku"] = new_sku
        row["SKU"] = new_sku
        changed += 1
    return changed

def preflight_products_payload(products: List[Dict[str, Any]], parse_mode: Any = "variant") -> Dict[str, Any]:
    """Full frontend payload preflight: apply mode, dedupe, enrich IDs, then group conflicts."""
    mode = normalise_parse_mode(parse_mode)
    before_count = len(products)
    products = apply_parse_mode(products, mode)
    repaired_muddled_variant_names = repair_muddled_extracted_variant_names(products)
    cleaned = preflight_products_for_frontend(products)
    normalised_sparse_variant_groups = normalise_sparse_variant_matrices_for_yoco(cleaned)
    fixed_duplicate_skus = make_skus_unique_for_yoco(cleaned)
    flagged_case_packs = flag_case_pack_prices(cleaned)
    flat_products, price_conflicts = split_price_conflicts(cleaned)
    enriched_remaining_duplicate_ids = enrich_remaining_duplicate_product_ids(flat_products)
    conflict_rows = sum(len(conflict.get("options", [])) for conflict in price_conflicts)
    return {
        "products": flat_products,
        "price_conflicts": price_conflicts,
        "metadata": {
            "raw_rows": before_count,
            "products": len(flat_products),
            "removed_true_duplicates": before_count - len(cleaned),
            "price_conflict_groups": len(price_conflicts),
            "price_conflict_rows": conflict_rows,
            "enriched_remaining_duplicate_ids": enriched_remaining_duplicate_ids,
            "flagged_case_packs": flagged_case_packs,
            "normalised_sparse_variant_groups": normalised_sparse_variant_groups,
            "fixed_duplicate_skus": fixed_duplicate_skus,
            "repaired_muddled_variant_names": repaired_muddled_variant_names,
            "parse_mode": mode,
        },
    }

def preflight_products_for_frontend(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean product rows before returning them to the frontend.

    Hardened for large workbooks: cache code/price/cost/product_id values so the
    route does not time out while repeatedly running regex/sorting over 5k+ rows.
    """
    deduped: List[Dict[str, Any]] = []
    seen_true_duplicates = set()

    for index, original in enumerate(products):
        row = dict(original)

        # Compute expensive fields once per row and cache them.
        code = best_code(row)
        row["_best_code"] = code
        title = title_value(row)
        price = get_price(row)
        cost = get_cost(row)
        row["_price"] = price
        row["_cost"] = cost

        if code:
            row["barcode"] = row.get("barcode") or code
            row["Barcode"] = row.get("Barcode") or code
            row["sku"] = row.get("sku") or row.get("SKU") or code
            row["SKU"] = row.get("SKU") or row.get("sku") or code

        if title:
            set_title(row, title)

        if not product_id_value(row):
            set_product_id(row, slugify(title or code or f"product-{index + 1}"))

        # For variants, selling_price is the product Default Price while
        # calculated_price is the row/Variant Price. Preserve that distinction.
        if row_is_variant(row):
            default_price = parse_money(row.get("selling_price") or row.get("Default Price"))
            row["selling_price"] = default_price if default_price > 0 else price
            row["calculated_price"] = price
        else:
            row["selling_price"] = price
            row["calculated_price"] = price
        row["cost_price"] = cost

        # Composite category+code identity is cached here.
        set_category_code_identity(row)

        if not normalise_text(row.get("_uid")):
            row["_uid"] = row_uid(row, index)

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
        pid = product_id_value(row).lower()
        if pid:
            groups.setdefault(pid, []).append(row)

    for _pid, rows in groups.items():
        if len(rows) <= 1:
            continue
        codes = {clean_code(row.get("_best_code") or best_code(row)).lower() for row in rows if clean_code(row.get("_best_code") or best_code(row))}
        if len(codes) <= 1:
            continue
        for row in rows:
            code = clean_code(row.get("_best_code") or best_code(row))
            if not code:
                continue
            title = title_value(row)
            if code.lower() not in title.lower():
                set_title(row, f"{title} - {code}".strip(" -"))
            base_pid = product_id_value(row) or slugify(title_value(row))
            code_slug = slugify(code)
            if code_slug and not base_pid.endswith("-" + code_slug) and base_pid != code_slug:
                set_product_id(row, f"{base_pid}-{code_slug}")
            # Product ID changed; refresh composite key remains category+code, not product ID.

    return deduped


def enrich_remaining_duplicate_product_ids(products: List[Dict[str, Any]]) -> int:
    """Make remaining same-ID singles unique after true dedupe and price-conflict extraction.

    This handles generic rows where the code is not a unique barcode (e.g. CCA timber treatment)
    and the product name alone is too generic (Pole/Dropper/Lath). We append source_context,
    price, or source row to make the Yoco Product ID unique without forcing the user through
    100+ duplicate cards.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in products:
        if normalise_text(row.get("variant_enabled") or row.get("Variant Enabled")).lower() == "yes":
            continue
        pid = product_id_value(row).lower()
        if pid:
            groups.setdefault(pid, []).append(row)

    changed = 0
    for _pid, rows in groups.items():
        if len(rows) <= 1:
            continue
        used_ids = set()
        for index, row in enumerate(rows):
            title = title_value(row) or "Product"
            context = normalise_text(row.get("source_context"))
            price = get_price(row)
            code = best_code(row)
            bits: List[str] = []
            if context and normalise_for_compare(context) not in normalise_for_compare(title):
                bits.append(context)
            # Do not rely on very generic codes as the only discriminator.
            if code and is_price_conflict_code_eligible(code) and normalise_for_compare(code) not in normalise_for_compare(title):
                bits.append(code)
            if price:
                bits.append(f"R{price:.2f}")
            bits.append(f"row {row.get('source_row') or index + 1}")
            suffix = " ".join([b for b in bits if b]).strip()
            # Keep the visible product name clean. Only the Product ID needs a
            # disambiguating suffix for Yoco uniqueness. Previous versions
            # appended source_context/price/row into product_name, which created
            # ugly names like "Strongbow ... - sectioned table block ...".
            new_title = title
            set_title(row, new_title)
            base_pid = slugify(f"{title} {suffix}" if suffix else title)
            new_pid = base_pid
            counter = 2
            while new_pid in used_ids:
                new_pid = f"{base_pid}-{counter}"
                counter += 1
            used_ids.add(new_pid)
            if product_id_value(row) != new_pid:
                set_product_id(row, new_pid)
                changed += 1
    return changed



def normalise_parse_mode(value: Any) -> str:
    mode = normalise_text(value).lower().strip()
    if mode in {"single", "singles", "single items", "single_items"}:
        return "single"
    return "variant"


def row_is_variant(row: Dict[str, Any]) -> bool:
    return normalise_text(row.get("variant_enabled") or row.get("Variant Enabled")).lower() == "yes"


def set_variant_fields(row: Dict[str, Any], enabled: bool) -> None:
    value = "Yes" if enabled else "No"
    row["variant_enabled"] = value
    row["Variant Enabled"] = value


def clear_variant_attributes(row: Dict[str, Any]) -> None:
    for key in [
        "attr1_name", "attr1_val", "attr2_name", "attr2_val", "attr3_name", "attr3_val",
        "Attribute 1", "Value 1", "Attribute 2", "Value 2", "Attribute 3", "Value 3",
    ]:
        row[key] = ""


def variant_label_for_row(row: Dict[str, Any]) -> str:
    vals = []
    for idx in range(1, 4):
        value = normalise_text(row.get(f"attr{idx}_val") or row.get(f"Value {idx}"))
        if value and value.lower() not in {"default", "default title"}:
            vals.append(value)
    return " · ".join(vals)


def unique_slug(base: str, used: set, fallback: str = "product") -> str:
    root = slugify(base) or fallback
    candidate = root
    counter = 2
    while candidate in used:
        candidate = f"{root}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def flatten_variants_to_single_items(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Single mode: every row becomes an independent Yoco product."""
    out: List[Dict[str, Any]] = []
    used_ids = set()
    for idx, original in enumerate(products):
        row = dict(original)
        title = title_value(row)
        label = variant_label_for_row(row)
        if row_is_variant(row) and label and normalise_for_compare(label) not in normalise_for_compare(title):
            title = f"{title} - {label}".strip(" -")
            set_title(row, title)
        set_variant_fields(row, False)
        clear_variant_attributes(row)
        price = get_price(row)
        row["selling_price"] = price
        row["calculated_price"] = price
        code = best_code(row)
        pid_seed = " ".join([title_value(row), code]).strip() or f"product {idx + 1}"
        set_product_id(row, unique_slug(pid_seed, used_ids, f"product-{idx + 1}"))
        set_category_code_identity(row)
        if not normalise_text(row.get("_uid")):
            row["_uid"] = row_uid(row, idx)
        out.append(row)
    return out


def variant_group_key(row: Dict[str, Any]) -> str:
    return "::".join([
        normalise_for_compare(row.get("category") or row.get("Category")),
        product_id_value(row).lower() or slugify(title_value(row)),
    ])


def normalise_existing_variant_default_prices(products: List[Dict[str, Any]]) -> None:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in products:
        if row_is_variant(row):
            groups.setdefault(variant_group_key(row), []).append(row)
    for rows in groups.values():
        if len(rows) <= 1:
            continue
        actual_prices = [get_price(row) for row in rows if get_price(row) > 0]
        if not actual_prices:
            continue
        default_price = round(min(actual_prices), 2)
        for row in rows:
            actual = get_price(row)
            row["selling_price"] = default_price
            row["calculated_price"] = actual if actual > 0 else default_price


# Common flavour/option vocabulary used by generic variant inference.
# This is intentionally broad retail language, not supplier/file-name specific.
_FLAVOUR_PHRASES = {
    "blackberry", "watermelon", "cranberry", "hawian", "hawaiian", "margarita",
    "peach", "pina colada", "strawberry", "tropical", "apple", "green apple",
    "gold apple", "red berries", "red berry", "berries red", "berry red", "ruby",
    "ruby apple", "manic mango", "mango", "orange", "lemon", "pressed lemon",
    "dry lemon", "tonic", "pink tonic", "pink & tonic",
    "cola", "mojito", "guarana", "rose spritzer", "wild berry", "berry",
    "classic", "blush", "mimosa", "sweet red", "sweet rose", "sweet white",
    "red", "rose", "white", "natural sweet rose", "natural s/rose",
    "punch ice", "punch peach", "punch tequila", "ice", "tequila",
}

_SIZE_RE = re.compile(r"\b\d+(?:[,.]\d+)?\s*(?:ml|l|litre|liter|g|kg|mm|cm|m)\b", re.I)
_TRAILING_SIZE_RE = re.compile(r"\b\d+(?:[,.]\d+)?\s*(?:ml|l|litre|liter|g|kg|mm|cm|m)\s*$", re.I)


def clean_option_base(base: str) -> str:
    base = normalise_text(base)
    base = re.sub(r"\s+(?:&|and)\s*$", "", base, flags=re.I).strip()
    return base


def infer_flavour_variant_from_title(title: str) -> Optional[Tuple[str, str, str]]:
    """Infer flavour/option variants from names like 'Breezer Blackberry 440ml'.

    The size remains part of the base title so Yoco sees one product with
    different flavour values, e.g.:
      Breezer Blackberry 440ml + Breezer Watermelon 440ml
      -> product 'Breezer 440ml', Attribute 'Flavour'.

    We only emit candidates for known retail option/flavour phrases. The global
    grouping step then requires at least two rows with the same base and distinct
    values before any row is converted to a variant, so standalone products stay
    standalone.
    """
    title = normalise_text(title)
    if not title:
        return None
    size_match = _TRAILING_SIZE_RE.search(title)
    if not size_match:
        return None
    size = size_match.group(0).strip()
    before_size = title[:size_match.start()].strip(" -·•,/\\")
    if not before_size:
        return None

    # Longest phrase first so 'red berries' wins over 'berries'.
    for phrase in sorted(_FLAVOUR_PHRASES, key=len, reverse=True):
        pattern = r"(?:^|\s|&|/)" + re.escape(phrase) + r"\s*$"
        m = re.search(pattern, before_size, flags=re.I)
        if not m:
            continue
        option = before_size[m.start():].strip(" &/-")
        base = clean_option_base(before_size[:m.start()])
        if len(base) < 2 or not option:
            continue
        # Avoid turning product families like 'Castle Lager 500ml' into variants;
        # 'lager/lite/stout/dry/gold' are deliberately not in the phrase list.
        return f"{base} {size}".strip(), "Flavour", option
    return None


def variant_candidates_from_title(title: str) -> List[Tuple[str, str, str, str]]:
    """Return possible variant parses as (base, attribute, value, kind)."""
    title = normalise_text(title)
    if not title:
        return []
    candidates: List[Tuple[str, str, str, str]] = []

    # ── Middle-dot separator (·) — pre-exploded Shopify/WooCommerce rows ──────
    # Pattern: "Product Name - Colour · Size / Number"
    # e.g. "Men's Bush Shirt: Long-Sleeve (Tech) - olive · S / 36"
    if "\u00b7" in title:
        dot_idx = title.rfind("\u00b7")
        base = title[:dot_idx].strip(" -\u00b7\u2022,/\\")
        value = title[dot_idx + 1:].strip()
        if base and value and len(base) >= 3:
            candidates.append((base, "Variant", value, "middle-dot"))

    # ── Slash-separated variant suffix ────────────────────────────────────────
    # Pattern: "Product Name colour / Size / Number"
    # e.g. "Men's Bush Shirt: Long-Sleeve (Tech) olive / S / 36"
    # Guard: skip dimension codes like "100x200 / CCA / SEA" and price-like values.
    if " / " in title and "\u00b7" not in title:
        parts = [p.strip() for p in title.split(" / ")]
        # Only accept slash-separated variants when at least one segment is non-numeric.
        # "S / 36", "XL / 48" are valid variant labels (size + collar).
        # "R 52.00 / 36.00" should be rejected — all segments are pure money values.
        def _is_pure_money(s: str) -> bool:
            return bool(re.fullmatch(r"[Rr]?\s*[0-9][0-9,. ]*", s.strip()))
        if len(parts) >= 3:
            base = " / ".join(parts[:-2]).strip(" -\u00b7\u2022,/\\")
            value_parts = parts[-2:]
            value = " / ".join(value_parts).strip()
            # Shopify clothing rows often arrive already flattened as:
            #   Product Name colour / S / 36
            # Move the trailing colour from the base into the variant value so
            # the product groups correctly as Product Name with variants
            # 'colour / S / 36', not as Product Name colour.
            clean_base, colour = split_trailing_colour_from_base(base)
            if colour and not value.lower().startswith(colour.lower() + " /"):
                base = clean_base
                value = f"{colour} / {value}".strip(" /")
            # Reject only if ALL value segments look like standalone prices
            if base and value and len(base) >= 5 and not all(_is_pure_money(p) for p in value_parts):
                candidates.append((base, "Variant", value, "slash"))
        elif len(parts) == 2:
            base = parts[0].strip(" -\u00b7\u2022,/\\")
            value = parts[1].strip()
            if base and value and len(base) >= 5 and not _is_pure_money(value):
                candidates.append((base, "Variant", value, "slash"))

    # ── Dash separator ────────────────────────────────────────────────────────
    # Skip if middle-dot or slash already found — they are more explicit signals.
    if "\u00b7" not in title and " / " not in title:
        dash = re.match(r"^(.{3,}?)\s+-\s+(.{1,40})$", title)
        if dash:
            base = dash.group(1).strip()
            value = dash.group(2).strip()
            if base and value and not parse_money(value):
                candidates.append((base, "Option", value, "dash"))

    clothing = re.search(r"\b(XXXL|XXL|XL|XS|S|M|L|Small|Medium|Large|Extra Large)\b$", title, flags=re.I)
    if clothing:
        value = clothing.group(1).strip()
        base = title[:clothing.start()].strip(" -·•,/\\")
        if len(base) >= 3:
            candidates.append((base, "Size", value, "clothing-size"))

    flavour = infer_flavour_variant_from_title(title)
    if flavour:
        candidates.append((flavour[0], flavour[1], flavour[2], "flavour"))

    # Case/pack multipliers like "100ml x 24" are NOT product variants.
    # They are handled by flag_case_pack_prices() so the user can confirm
    # whether the listed price is a unit price or the total case price.
    size_pattern = r"(?:(?:\d+(?:[,.]\d+)?\s*(?:ml|l|litre|liter|g|kg|mm|cm|m))|(?:\d+(?:\s*[x×*]\s*\d+)+(?:\s*(?:mm|cm|m))?))$"
    size = re.search(size_pattern, title, flags=re.I)
    if size:
        value = size.group(0).strip()
        base = title[:size.start()].strip(" -·•,/\\")
        if len(base) >= 3 and normalise_for_compare(base) != normalise_for_compare(title):
            candidates.append((base, "Size", value, "size"))
    return candidates


def infer_variant_from_title(title: str) -> Optional[Tuple[str, str, str]]:
    candidates = variant_candidates_from_title(title)
    if not candidates:
        return None
    base, attr, value, _kind = candidates[0]
    return base, attr, value


def infer_variants_from_titles(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Variant mode for generic spreadsheet rows.

    This now supports both classic size variants and embedded flavour variants.
    It builds all safe candidate groups first, then applies only groups that have
    multiple rows and multiple values. This avoids the old issue where
    'Breezer Blackberry 440ml' was treated as a standalone size product instead
    of a Breezer flavour variant, while still allowing 'Autumn Rose 1.5L/750ml'
    to become a Size variant.
    """
    candidate_groups: Dict[Tuple[str, str, str], List[Tuple[Dict[str, Any], str, str, str]]] = {}

    for row in products:
        if normalise_text(row.get("source_context")).lower().startswith("yoco products"):
            continue
        if row_is_variant(row):
            continue
        title = title_value(row)
        for base, attr_name, attr_value, kind in variant_candidates_from_title(title):
            category_key = normalise_for_compare(row.get("category") or row.get("Category"))
            key = (category_key, normalise_for_compare(base), attr_name)
            candidate_groups.setdefault(key, []).append((row, attr_value, base, kind))

    valid_groups: List[Tuple[int, int, Tuple[str, str, str], List[Tuple[Dict[str, Any], str, str, str]]]] = []
    kind_priority = {"middle-dot": 0, "slash": 1, "dash": 2, "flavour": 3, "clothing-size": 4, "size": 5}
    for key, rows in candidate_groups.items():
        if len(rows) <= 1:
            continue
        values = [normalise_for_compare(value) for _, value, _, _ in rows if value]
        if len(set(values)) <= 1:
            continue
        best_kind = min(kind_priority.get(kind, 9) for _, _, _, kind in rows)
        valid_groups.append((-len(rows), best_kind, key, rows))

    # Larger groups first, then more explicit patterns. A row can only belong to
    # one inferred group.
    assigned: set = set()
    for _neg_size, _priority, _key, rows in sorted(valid_groups, key=lambda item: (item[0], item[1])):
        rows = [(row, value, base, kind) for row, value, base, kind in rows if id(row) not in assigned]
        if len(rows) <= 1:
            continue
        values = [normalise_for_compare(value) for _, value, _, _ in rows]
        if len(set(values)) <= 1:
            continue
        attr_name = _key[2]
        # Prefer the most common/canonical base spelling inside the group.
        base_counts: Dict[str, int] = {}
        for _row, _value, base, _kind in rows:
            base_counts[base] = base_counts.get(base, 0) + 1
        base_title = sorted(base_counts.items(), key=lambda item: (-item[1], len(item[0])))[0][0]
        prices = [get_price(row) for row, _value, _base, _kind in rows if get_price(row) > 0]
        default_price = round(min(prices), 2) if prices else 0.0
        base_pid = slugify(base_title)
        for row, value, _base, _kind in rows:
            actual_price = get_price(row)
            set_title(row, base_title)
            set_product_id(row, base_pid)
            set_variant_fields(row, True)
            row["attr1_name"] = row["Attribute 1"] = attr_name
            row["attr1_val"] = row["Value 1"] = value
            row["attr2_name"] = row["Attribute 2"] = ""
            row["attr2_val"] = row["Value 2"] = ""
            row["attr3_name"] = row["Attribute 3"] = ""
            row["attr3_val"] = row["Value 3"] = ""
            row["selling_price"] = default_price if default_price > 0 else actual_price
            row["calculated_price"] = actual_price if actual_price > 0 else default_price
            assigned.add(id(row))
    return products


def apply_parse_mode(products: List[Dict[str, Any]], parse_mode: Any) -> List[Dict[str, Any]]:
    """Apply dashboard Single/Variant mode to Python-backend rows.

    Single: clears all variant attributes and makes every row its own product.
    Variant: preserves Shopify/Yoco variant rows and groups only obvious title
    variants, then normalises Yoco default price per variant group.
    """
    mode = normalise_parse_mode(parse_mode)
    if mode == "single":
        return flatten_variants_to_single_items(products)

    prepared = [dict(row) for row in products]
    for index, row in enumerate(prepared):
        # Preserve existing variant attributes from Shopify/Yoco-like exports.
        if row_is_variant(row) or variant_label_for_row(row):
            set_variant_fields(row, True)
        else:
            set_variant_fields(row, False)
        if not product_id_value(row):
            set_product_id(row, slugify(title_value(row) or best_code(row) or f"product-{index + 1}"))
        if not normalise_text(row.get("_uid")):
            row["_uid"] = row_uid(row, index)

    infer_variants_from_titles(prepared)
    normalise_existing_variant_default_prices(prepared)
    for row in prepared:
        set_category_code_identity(row)
    return prepared

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


# Column mapping is called for every row, but a sheet has the same headers for
# thousands of rows. Cache by the header tuple and pre-normalise aliases once.
# This avoids the worker spending seconds repeatedly normalising headers/aliases.
_COLUMN_ALIAS_NORMS = {
    canonical: tuple(x for x in (normalise_header(a) for a in aliases) if x)
    for canonical, aliases in COLUMN_ALIASES.items()
}
_MAP_COLUMNS_CACHE: Dict[Tuple[str, ...], Dict[str, str]] = {}


def map_columns(columns: List[Any]) -> Dict[str, str]:
    key = tuple(normalise_text(c) for c in columns)
    cached = _MAP_COLUMNS_CACHE.get(key)
    if cached is not None:
        return dict(cached)

    header_map = {normalise_header(c): c for c in columns if normalise_header(c)}
    mapped: Dict[str, str] = {}

    for canonical, alias_norms in _COLUMN_ALIAS_NORMS.items():
        # Exact match first.
        for alias_norm in alias_norms:
            original = header_map.get(alias_norm)
            if original is not None:
                mapped[canonical] = original
                break
        if canonical in mapped:
            continue

        # Fuzzy contains match as fallback. This is intentionally simple and
        # uses pre-normalised aliases so it does not rebuild lists per row.
        for h_norm, original in header_map.items():
            matched = False
            for alias_norm in alias_norms:
                if alias_norm in h_norm:
                    mapped[canonical] = original
                    matched = True
                    break
            if matched:
                break

    if len(_MAP_COLUMNS_CACHE) < 256:
        _MAP_COLUMNS_CACHE[key] = dict(mapped)
    return mapped




def choose_best_column(columns: List[Any], preferred_terms: List[str], banned_terms: Optional[List[str]] = None) -> Optional[str]:
    """Choose a column using explicit priority terms.

    map_columns() is broad and fuzzy. For prices we need stricter priority so
    a sheet with both "Inc Vat" and "Selling" uses Selling, not Inc Vat.
    """
    banned_terms = banned_terms or []
    scored: List[Tuple[int, str]] = []
    for col in columns:
        label = normalise_header(col)
        if not label:
            continue
        if any(term in label for term in banned_terms):
            continue
        for priority, term in enumerate(preferred_terms):
            term_norm = normalise_header(term)
            if label == term_norm:
                scored.append((priority * 10, col))
                break
            if term_norm and term_norm in label:
                scored.append((priority * 10 + 5, col))
                break
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def choose_selling_price_column(columns: List[Any], mapped: Dict[str, str]) -> Optional[str]:
    # Put actual retail/selling columns before Inc VAT. This fixes sheets like
    # Voltex/Positec and also Vermont's "Retail/Trade" column.
    preferred = [
        "selling", "selling price", "retail/trade", "retail trade", "retail",
        "retail price", "trade price", "dealer price", "customer price",
        "shelf price", "rrp", "list price", "each price", "price",
        "price incl", "price inc", "incl vat", "inc vat", "vat incl", "including vat",
    ]
    chosen = choose_best_column(columns, preferred, banned_terms=["cost", "ex vat", "excl vat", "nett", "net price", "wholesale"])
    return chosen or mapped.get("selling_price")


def choose_cost_price_column(columns: List[Any], mapped: Dict[str, str]) -> Optional[str]:
    preferred = ["cost price", "default cost price", "cost", "ex vat", "excl vat", "nett", "net price", "wholesale", "buying price", "purchase price", "supplier price"]
    return choose_best_column(columns, preferred, banned_terms=["selling", "retail", "inc vat", "incl vat"]) or mapped.get("cost_price")






def is_yoco_products_export_sheet(raw_df: pd.DataFrame) -> bool:
    """Detect Yoco Products import/export template sheets.

    These have a fixed 25-column header containing Product ID, Product Name,
    Default Price, Variant Price, Variant Enabled, Attribute 1, Barcode, etc.
    They must be parsed directly because Default Price and Variant Price have
    different meanings; a generic price-column selector can collapse variants.
    """
    if raw_df.empty:
        return False
    required = {"product id", "product name", "default price", "variant price", "variant enabled"}
    for ridx in range(min(len(raw_df), 10)):
        headers = {normalise_header(v) for v in raw_df.iloc[ridx].tolist() if normalise_header(v)}
        if required.issubset(headers):
            return True
    return False


def find_yoco_header_row(raw_df: pd.DataFrame) -> Optional[int]:
    required = {"product id", "product name", "default price", "variant price", "variant enabled"}
    for ridx in range(min(len(raw_df), 10)):
        headers = {normalise_header(v) for v in raw_df.iloc[ridx].tolist() if normalise_header(v)}
        if required.issubset(headers):
            return ridx
    return None


def parse_yoco_products_export_sheet(raw_df: pd.DataFrame, source_sheet: str) -> List[Dict[str, Any]]:
    """Parse a Yoco Products sheet while preserving variants correctly."""
    header_idx = find_yoco_header_row(raw_df)
    if header_idx is None:
        return []
    headers = [normalise_text(v) or f"Column {i + 1}" for i, v in enumerate(raw_df.iloc[header_idx].tolist())]
    data = raw_df.iloc[header_idx + 1:].copy()
    data.columns = headers
    hmap = {normalise_header(h): h for h in headers if normalise_header(h)}

    def col(name: str) -> Optional[str]:
        return hmap.get(normalise_header(name))

    products: List[Dict[str, Any]] = []
    for offset, (_, series) in enumerate(data.iterrows(), start=header_idx + 2):
        record = series.to_dict()
        product_id = normalise_text(record.get(col("Product ID"))) if col("Product ID") else ""
        product_name = normalise_text(record.get(col("Product Name"))) if col("Product Name") else ""
        if not product_id and not product_name:
            continue
        default_price = parse_money(record.get(col("Default Price"))) if col("Default Price") else 0.0
        variant_price = parse_money(record.get(col("Variant Price"))) if col("Variant Price") else 0.0
        price = variant_price or default_price
        variant_enabled_raw = normalise_text(record.get(col("Variant Enabled"))) if col("Variant Enabled") else ""
        variant_enabled = "Yes" if variant_enabled_raw.lower() == "yes" else "No"
        sku = clean_code(record.get(col("SKU"))) if col("SKU") else ""
        barcode = clean_code(record.get(col("Barcode"))) if col("Barcode") else ""
        code = barcode or sku
        row = {
            "product_id": product_id or slugify(product_name or code or f"product-{offset}"),
            "product_name": product_name or product_id,
            "description": normalise_text(record.get(col("Description"))) if col("Description") else "",
            "category": normalise_text(record.get(col("Category"))) if col("Category") else source_sheet or "Uncategorised",
            "brand": normalise_text(record.get(col("Brand"))) if col("Brand") else "",
            "sku": sku or code,
            "barcode": barcode or code,
            "selling_price": float(default_price or price),
            "calculated_price": float(price or default_price),
            "variant_price": float(price or default_price),
            "cost_price": parse_money(record.get(col("Default Cost Price"))) if col("Default Cost Price") else 0.0,
            "image_url": normalise_text(record.get(col("Image URL"))) if col("Image URL") else "",
            "variant_enabled": variant_enabled,
            "attr1_name": normalise_text(record.get(col("Attribute 1"))) if col("Attribute 1") else "",
            "attr1_val": normalise_text(record.get(col("Value 1"))) if col("Value 1") else "",
            "attr2_name": normalise_text(record.get(col("Attribute 2"))) if col("Attribute 2") else "",
            "attr2_val": normalise_text(record.get(col("Value 2"))) if col("Value 2") else "",
            "attr3_name": normalise_text(record.get(col("Attribute 3"))) if col("Attribute 3") else "",
            "attr3_val": normalise_text(record.get(col("Value 3"))) if col("Value 3") else "",
            "track_stock": normalise_text(record.get(col("Track Stock"))) if col("Track Stock") else ("Variant" if variant_enabled == "Yes" else "Product"),
            "source_sheet": source_sheet,
            "source_row": int(offset),
            "source_context": "Yoco Products sheet",
        }
        set_category_code_identity(row, row.get("category"))
        products.append(row)
    return products

def is_shopify_export_sheet(raw_df: pd.DataFrame) -> bool:
    """Detect Shopify product export style sheets.

    Shopify exports are wide, already one-row-per-variant tables with columns
    like Handle, Title, Option1 Name, Option1 Value, Variant SKU and Variant Price.
    They need a dedicated parser because product-level fields are only populated
    on the first row of each handle and should be forward-filled within the handle.
    """
    if raw_df.empty:
        return False
    for ridx in range(min(len(raw_df), 8)):
        headers = [normalise_header(v) for v in raw_df.iloc[ridx].tolist()]
        header_set = set(h for h in headers if h)
        required = {"handle", "title", "variant price"}
        optionish = any(h.startswith("option1") for h in header_set)
        skuish = "variant sku" in header_set or "variant barcode" in header_set
        if required.issubset(header_set) and optionish and skuish:
            return True
    return False


def find_shopify_header_row(raw_df: pd.DataFrame) -> Optional[int]:
    for ridx in range(min(len(raw_df), 12)):
        headers = [normalise_header(v) for v in raw_df.iloc[ridx].tolist()]
        header_set = set(h for h in headers if h)
        if {"handle", "title", "variant price"}.issubset(header_set):
            return ridx
    return None


def shopify_category_to_yoco(value: Any) -> str:
    text = normalise_text(value)
    if not text:
        return "Uncategorised"
    # Shopify taxonomy strings look like Apparel & Accessories > Clothing > Shirts.
    # Use the broadest useful category for Yoco, not the full long path.
    parts = [p.strip() for p in text.split(">") if p.strip()]
    if not parts:
        return text
    # Keep Clothing/Apparel readable if present.
    for part in parts:
        pn = normalise_for_compare(part)
        if "clothing" in pn or "apparel" in pn:
            return "Clothing & Apparel"
    return parts[-1] if len(parts[-1]) <= 40 else parts[0]


def clean_html_text(value: Any) -> str:
    text = normalise_text(value)
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_shopify_export_sheet(raw_df: pd.DataFrame, source_sheet: str) -> List[Dict[str, Any]]:
    """Parse Shopify product exports without hardcoding one store format.

    Supports the common Shopify columns: Handle, Title, Product Category,
    Option1/2/3 Name + Value, Variant SKU, Variant Barcode, Variant Price,
    Cost per item, Image Src, and Status. Product-level cells are forward-filled
    per handle, but option/variant cells stay row-specific.
    """
    header_idx = find_shopify_header_row(raw_df)
    if header_idx is None:
        return []

    headers = [normalise_text(v) or f"Column {i+1}" for i, v in enumerate(raw_df.iloc[header_idx].tolist())]
    data = raw_df.iloc[header_idx + 1:].copy()
    data.columns = headers

    # Build a case-insensitive lookup from normalised header to original header.
    hmap = {normalise_header(h): h for h in headers if normalise_header(h)}

    def col(*names: str) -> Optional[str]:
        for name in names:
            n = normalise_header(name)
            if n in hmap:
                return hmap[n]
        for hnorm, original in hmap.items():
            for name in names:
                n = normalise_header(name)
                if n and n in hnorm:
                    return original
        return None

    c_handle = col("Handle")
    c_title = col("Title")
    c_body = col("Body (HTML)", "Body HTML", "Description")
    c_category = col("Product Category", "Category", "Type")
    c_vendor = col("Vendor", "Brand")
    c_sku = col("Variant SKU", "SKU")
    c_barcode = col("Variant Barcode", "Barcode")
    c_price = col("Variant Price", "Price / South Africa", "Price")
    c_cost = col("Cost per item", "Cost", "Default Cost Price")
    c_image = col("Variant Image", "Image Src", "Image URL")
    c_status = col("Status")

    option_cols = []
    for idx in range(1, 4):
        option_cols.append((
            col(f"Option{idx} Name", f"Option {idx} Name"),
            col(f"Option{idx} Value", f"Option {idx} Value"),
        ))

    products: List[Dict[str, Any]] = []
    current: Dict[str, str] = {"handle": "", "title": "", "category": "", "brand": "", "description": "", "image_url": ""}
    current_option_names = ["", "", ""]

    for offset, (_, series) in enumerate(data.iterrows(), start=header_idx + 2):
        record = series.to_dict()
        handle = normalise_text(record.get(c_handle)) if c_handle else ""
        title = normalise_text(record.get(c_title)) if c_title else ""
        if handle and handle != current.get("handle"):
            current_option_names = ["", "", ""]
            current["handle"] = handle
        elif handle:
            current["handle"] = handle
        if title:
            current["title"] = title
        if c_category and normalise_text(record.get(c_category)):
            current["category"] = shopify_category_to_yoco(record.get(c_category))
        if c_vendor and normalise_text(record.get(c_vendor)):
            current["brand"] = normalise_text(record.get(c_vendor))
        if c_body and normalise_text(record.get(c_body)):
            current["description"] = clean_html_text(record.get(c_body))
        if c_image and normalise_text(record.get(c_image)):
            current["image_url"] = normalise_text(record.get(c_image))

        if not current["handle"] and not current["title"]:
            continue

        price = parse_money(record.get(c_price)) if c_price else 0.0
        # Shopify exports may have regional price columns; use them if Variant Price blank.
        if price <= 0:
            for hnorm, original in hmap.items():
                if hnorm.startswith("price") and "compare" not in hnorm:
                    price = parse_money(record.get(original))
                    if price > 0:
                        break
        cost = parse_money(record.get(c_cost)) if c_cost else 0.0
        sku = clean_code(record.get(c_sku)) if c_sku else ""
        barcode = clean_code(record.get(c_barcode)) if c_barcode else ""
        code = barcode or sku

        attr_data = []
        for opt_index, (name_col, val_col) in enumerate(option_cols):
            name = normalise_text(record.get(name_col)) if name_col else ""
            value = normalise_text(record.get(val_col)) if val_col else ""
            if name and name.lower() != "title":
                current_option_names[opt_index] = name
            elif name.lower() == "title":
                name = ""
            if value and value.lower() not in {"default title", "default"}:
                attr_data.append((current_option_names[opt_index] or name or f"Option {len(attr_data)+1}", value))

        if price <= 0 and not sku and not barcode and not attr_data:
            # Product image/detail continuation row, not a sellable variant.
            continue

        product_id = slugify(current["handle"] or current["title"])
        product_name = current["title"] or current["handle"]
        row = {
            "product_id": product_id,
            "product_name": product_name,
            "description": current.get("description", ""),
            "category": current.get("category") or "Uncategorised",
            "brand": current.get("brand", ""),
            "sku": sku or code,
            "barcode": barcode or code,
            "selling_price": float(price or 0),
            "calculated_price": float(price or 0),
            "cost_price": float(cost or 0),
            "image_url": current.get("image_url", ""),
            "variant_enabled": "Yes" if attr_data else "No",
            "attr1_name": attr_data[0][0] if len(attr_data) > 0 else "",
            "attr1_val": attr_data[0][1] if len(attr_data) > 0 else "",
            "attr2_name": attr_data[1][0] if len(attr_data) > 1 else "",
            "attr2_val": attr_data[1][1] if len(attr_data) > 1 else "",
            "attr3_name": attr_data[2][0] if len(attr_data) > 2 else "",
            "attr3_val": attr_data[2][1] if len(attr_data) > 2 else "",
            "source_sheet": source_sheet,
            "source_row": int(offset),
            "source_context": "Shopify product export",
            "shopify_handle": current.get("handle", ""),
        }
        set_category_code_identity(row, current.get("category", ""))
        products.append(row)

    return products


def combine_adjacent_title_fragments(record: Dict[str, Any], mapped: Dict[str, str], title: str) -> str:
    """Combine split product names in simple product/price sheets.

    Example:
      PRODUCTS |       | PRICE
      Denova   | tissue| R10.00
    becomes "Denova tissue" instead of only "Denova".

    Guardrails avoid merging SKU/barcode/cost/category/price columns or values
    that look like pure codes/prices.
    """
    if not title:
        return title
    headers = list(record.keys())
    product_col = mapped.get("product_name")
    price_col = mapped.get("selling_price")
    if product_col not in headers or price_col not in headers:
        return title
    try:
        pidx = headers.index(product_col)
        price_idx = headers.index(price_col)
    except ValueError:
        return title
    if pidx >= price_idx or price_idx - pidx > 5:
        return title

    excluded = {mapped.get(k) for k in ["barcode", "sku", "cost_price", "category", "brand", "product_id"] if mapped.get(k)}
    fragments: List[str] = []
    for col in headers[pidx:price_idx]:
        if col in excluded:
            continue
        text = normalise_text(record.get(col))
        if not text:
            continue
        if parse_money(text) > 0:
            continue
        code_candidate = clean_code(text)
        # Treat as a code only when it contains digits or is clearly an uppercase
        # stock-code token. Lowercase words like "tissue" are name fragments.
        if code_candidate and re.fullmatch(r"[A-Z0-9*./-]{5,}", code_candidate) and (any(ch.isdigit() for ch in code_candidate) or text == text.upper()):
            continue
        # Skip repeated headers/price labels accidentally present in data.
        if normalise_header(text) in {"price", "sell", "selling", "products", "product"}:
            continue
        fragments.append(text)
    if len(fragments) <= 1:
        return title
    combined = " ".join(fragments)
    return re.sub(r"\s+", " ", combined).strip() or title


def source_context_from_record(record: Dict[str, Any], mapped: Dict[str, str], title: str, code: str, selling_price: float, cost_price: float) -> str:
    """Capture extra row details that help disambiguate generic names.

    Example: the Diggersrest timber table has rows like Pole / CCA / 3m / 50-75 / 79.90.
    Earlier we only kept "Pole" as the name and "CCA" as the code, creating 100+ duplicates.
    This preserves the distinguishing values (3m, 50-75) so the preflight can enrich names.
    """
    excluded_cols = {c for c in mapped.values() if c}
    parts: List[str] = []
    seen = set()
    for col, value in record.items():
        text = normalise_text(value)
        if not text:
            continue
        if text == title or text == code:
            continue
        # Drop values that are exactly the selected selling/cost price.
        money = parse_money(text)
        if money and (abs(money - selling_price) < 0.005 or abs(money - cost_price) < 0.005):
            continue
        # Keep short descriptor/dimension values. Avoid long descriptions already in the name.
        if len(text) > 40:
            continue
        key = normalise_for_compare(text)
        if not key or key in seen:
            continue
        # Pure margin/profit decimals are not helpful context.
        if re.fullmatch(r"0?\.\d+", text):
            continue
        parts.append(text)
        seen.add(key)
        if len(parts) >= 4:
            break
    return " ".join(parts).strip()




def find_type_treatment_header_row(raw_df: pd.DataFrame) -> Optional[int]:
    """Find an inline timber table header: TYPE / TREATMENT / LENGTH / DIAMETER / UNIT PRICE."""
    for idx in range(len(raw_df)):
        values = {normalise_header(v) for v in raw_df.iloc[idx].tolist() if normalise_text(v)}
        if {"type", "treatment", "length", "diameter"}.issubset(values) and any("unit price" in v or v == "price" for v in values):
            return idx
    return None


def parse_type_treatment_section(raw_df: pd.DataFrame, source_sheet: str) -> List[Dict[str, Any]]:
    """Parse timber-style inline tables without reusing the previous table's headers."""
    header_idx = find_type_treatment_header_row(raw_df)
    if header_idx is None:
        return []
    header_values = [normalise_header(v) for v in raw_df.iloc[header_idx].tolist()]
    col_by_name = {name: idx for idx, name in enumerate(header_values) if name}
    type_idx = col_by_name.get("type")
    treatment_idx = col_by_name.get("treatment")
    length_idx = col_by_name.get("length")
    diameter_idx = col_by_name.get("diameter")
    unit_price_idx = None
    for idx, name in enumerate(header_values):
        if "unit price" in name or name == "price":
            unit_price_idx = idx
            break
    if type_idx is None or unit_price_idx is None:
        return []

    products: List[Dict[str, Any]] = []
    for ridx in range(header_idx + 1, len(raw_df)):
        row = raw_df.iloc[ridx]
        kind = normalise_text(row.iloc[type_idx]) if type_idx < len(row) else ""
        treatment = normalise_text(row.iloc[treatment_idx]) if treatment_idx is not None and treatment_idx < len(row) else ""
        length = normalise_text(row.iloc[length_idx]) if length_idx is not None and length_idx < len(row) else ""
        diameter = normalise_text(row.iloc[diameter_idx]) if diameter_idx is not None and diameter_idx < len(row) else ""
        price = parse_money(row.iloc[unit_price_idx]) if unit_price_idx < len(row) else 0.0
        if not kind and not price:
            continue
        if not kind or price <= 0:
            continue
        name_parts = [kind, length, diameter, treatment]
        title = " ".join([p for p in name_parts if p]).strip()
        product_id = slugify(title)
        products.append({
            "product_id": product_id,
            "product_name": title,
            "description": "",
            "category": source_sheet or "Uncategorised",
            "brand": "",
            "sku": treatment,
            "barcode": "",
            "selling_price": price,
            "calculated_price": price,
            "cost_price": 0.0,
            "image_url": "",
            "variant_enabled": "No",
            "attr1_name": "",
            "attr1_val": "",
            "attr2_name": "",
            "attr2_val": "",
            "attr3_name": "",
            "attr3_val": "",
            "source_sheet": source_sheet,
            "source_row": ridx + 1,
            "source_context": " ".join([p for p in [length, diameter, treatment] if p]),
        })
    return products

def looks_like_headerless_pair_sheet(raw_df: pd.DataFrame) -> bool:
    """Detect sheets made of repeated code/cost/price blocks with no header row.

    The "China blomme en potte" sheet is two side-by-side lists:
      code | cost | price | blank | code | cost | price
    Without this parser the first product row becomes the header and prices are lost.
    """
    df = raw_df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty or df.shape[1] < 3:
        return False
    sample = df.head(8)
    scored_rows = 0
    for _, row in sample.iterrows():
        values = [normalise_text(v) for v in row.tolist()]
        row_score = 0
        for i in range(0, len(values) - 2):
            code = values[i]
            p1 = parse_money(values[i + 1])
            p2 = parse_money(values[i + 2])
            if code and not re.fullmatch(r"[Rr]?\s*[0-9,.]+", code) and p1 > 0 and p2 > 0:
                row_score += 1
        if row_score:
            scored_rows += 1
    return scored_rows >= 3


def parse_headerless_pair_sheet(raw_df: pd.DataFrame, source_sheet: str) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    df = raw_df.dropna(how="all").dropna(axis=1, how="all")
    for ridx, row in df.iterrows():
        values = [normalise_text(v) for v in row.tolist()]
        # Split on blank columns, then parse each block as code | cost | price.
        blocks: List[List[str]] = []
        current: List[str] = []
        for value in values:
            if not value:
                if current:
                    blocks.append(current)
                    current = []
            else:
                current.append(value)
        if current:
            blocks.append(current)

        for block in blocks:
            if len(block) < 3:
                continue
            code = clean_code(block[0])
            cost = parse_money(block[1])
            selling = parse_money(block[2])
            if not code or selling <= 0:
                continue
            products.append({
                "product_id": slugify(code),
                "product_name": code,
                "description": "",
                "category": source_sheet or "Uncategorised",
                "brand": "",
                "sku": code,
                "barcode": code,
                "selling_price": selling,
                "calculated_price": selling,
                "cost_price": cost,
                "image_url": "",
                "variant_enabled": "No",
                "attr1_name": "",
                "attr1_val": "",
                "attr2_name": "",
                "attr2_val": "",
                "attr3_name": "",
                "attr3_val": "",
                "source_sheet": source_sheet,
                "source_row": int(ridx) + 1,
                "source_context": "headerless code/cost/price block",
            })
    return products

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

    # Use price-specific selectors instead of generic fuzzy matching. This avoids
    # selecting Inc Vat when a final Selling/Retail column exists.
    selling_col = choose_selling_price_column(list(record.keys()), mapped)
    cost_col = choose_cost_price_column(list(record.keys()), mapped)
    if selling_col:
        mapped["selling_price"] = selling_col
    if cost_col:
        mapped["cost_price"] = cost_col

    def get(canonical: str, default: Any = "") -> Any:
        col = mapped.get(canonical)
        if col is None:
            return default
        return record.get(col, default)

    # Title selection is intentionally stateful-layout aware.
    # Some supplier sheets use Column A as a category/state column named "Product:"
    # and the actual sellable item title in a separate "Product description:" column:
    #   Product:      Product/Barcode:   Product description:   Ex Vat   Inc Vat
    #   Angle Iron    SEA04005006        6m 40*40*5mm           245      281.75
    # Older logic mapped "Product:" as the product title, which prevented the
    # category state from being detected and caused same-code items from different
    # categories to be grouped incorrectly.
    headers = list(record.keys())

    def first_header_matching(needles: List[str]) -> Optional[Any]:
        for col in headers:
            hn = normalise_header(col)
            if any(n in hn for n in needles):
                return col
        return None

    primary_title = normalise_text(get("product_name"))
    primary_title_col = mapped.get("product_name")
    primary_title_col_norm = normalise_header(primary_title_col) if primary_title_col is not None else ""

    description_title_col = first_header_matching([
        "product description",
        "item description",
        "stock description",
        "product desc",
        "item desc",
    ])
    description_title = normalise_text(record.get(description_title_col, "")) if description_title_col is not None else ""

    # Prefer the explicit description/title column when the mapped product_name
    # column is a generic state/category column like "Product:".
    title_from_description = False
    if description_title and primary_title_col_norm in {"product", "category", "section", "group"}:
        title = description_title
        title_from_description = True
    elif description_title and primary_title and normalise_for_compare(description_title) != normalise_for_compare(primary_title) and primary_title_col_norm == "product":
        title = description_title
        title_from_description = True
    else:
        title = primary_title

    # Fallback: find the longest text-ish cell if no title column was mapped.
    if not title:
        text_cells = [normalise_text(v) for v in record.values() if normalise_text(v)]
        text_cells = [v for v in text_cells if not re.fullmatch(r"[Rr]?\s*[0-9,.\-]+", v)]
        if text_cells:
            title = max(text_cells, key=len)

    # Generic split-name support for simple product/price layouts.
    title = combine_adjacent_title_fragments(record, mapped, title)

    code = clean_code(get("barcode") or get("sku"))
    sku = clean_code(get("sku") or code)
    category = normalise_text(get("category")) or source_sheet or "Uncategorised"
    brand = normalise_text(get("brand"))
    description = "" if title_from_description else normalise_text(get("description"))
    selling_price = parse_money(get("selling_price"))
    cost_price = parse_money(get("cost_price"))
    image_url = normalise_text(get("image_url"))

    if not title and not code:
        return None

    product_id = normalise_text(get("product_id")) or slugify(title or code or f"product-{index + 1}")
    source_context = source_context_from_record(record, mapped, title, code, selling_price, cost_price)

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
        "source_context": source_context,
    }
    return row




def row_has_price_metrics(row: Dict[str, Any]) -> bool:
    """Return True when this parsed row looks like a real sellable product row.

    Category-heading rows such as "Lintels" or "Cement" usually have a title
    but no selling price, no cost, no barcode/SKU, and no variant fields. Those
    should be used as state, not exported as products.
    """
    return get_price(row) > 0 or get_cost(row) > 0


def row_has_identity_code(row: Dict[str, Any]) -> bool:
    return bool(best_code(row) or normalise_text(row.get("sku") or row.get("SKU")))


def row_has_variant_or_description(row: Dict[str, Any]) -> bool:
    fields = [
        "description", "Description",
        "attr1_name", "attr1_val", "attr2_name", "attr2_val", "attr3_name", "attr3_val",
        "Attribute 1", "Value 1", "Attribute 2", "Value 2", "Attribute 3", "Value 3",
    ]
    return any(normalise_text(row.get(f)) for f in fields)


def is_category_heading_row(row: Dict[str, Any]) -> bool:
    """Detect block heading rows that should not become products.

    Example source layout:
        Lintels
        150mm x 3m     R160,00
        115mm x 3m     R125,00

    "Lintels" is a category state. The priced rows become:
        Lintels - 150mm x 3m
        Lintels - 115mm x 3m

    Guardrails:
    - Do not classify rows with barcode/SKU as headings.
    - Do not classify rows with cost/price as headings.
    - Do not classify rows with variant fields/descriptions as headings.
    """
    title = title_value(row)
    if not title:
        return False
    if row_has_price_metrics(row):
        return False
    if row_has_identity_code(row):
        return False
    if row_has_variant_or_description(row):
        return False
    # Headings are generally short labels, not long product descriptions.
    return len(title) <= 80


def apply_category_prefix(row: Dict[str, Any], current_category: str) -> Dict[str, Any]:
    """Prefix a product title with the active category and rebuild product_id.

    Standalone priced rows still remain products. If there is no active category
    they pass through unchanged. If the title is already prefixed, do not double
    prefix it.
    """
    category = normalise_text(current_category)
    title = title_value(row)
    if not category or not title:
        return row

    title_norm = normalise_for_compare(title)
    cat_norm = normalise_for_compare(category)
    if title_norm == cat_norm or title_norm.startswith(cat_norm + " "):
        new_title = title
    else:
        new_title = f"{category} - {title}"

    set_title(row, new_title)
    set_product_id(row, slugify(new_title))
    # Preserve sheet/category if something meaningful already exists, but for
    # generic sheet names use the detected block heading as category.
    existing_category = normalise_text(row.get("category") or row.get("Category"))
    generic_cats = {"", "uncategorised", normalise_text(row.get("source_sheet")).lower()}
    if existing_category.lower() in generic_cats:
        row["category"] = category
    set_category_code_identity(row, category)
    return row


def raw_row_is_blank(raw_df: pd.DataFrame, raw_index: int) -> bool:
    if raw_index not in raw_df.index:
        return False
    values = raw_df.loc[raw_index].tolist()
    return all(not normalise_text(v) for v in values)


def blank_gap_between(raw_df: pd.DataFrame, previous_raw_index: Optional[int], current_raw_index: int) -> bool:
    """True when one or more blank source rows separate two parsed rows.

    This lets a blank row end a category block, so a later standalone priced row
    like "Prefab Slabs  R79,00" does not incorrectly become "Cement - Prefab Slabs".
    """
    if previous_raw_index is None:
        return False
    try:
        start = int(previous_raw_index) + 1
        end = int(current_raw_index)
    except Exception:
        return False
    if end <= start:
        return False
    for ridx in range(start, end):
        if raw_row_is_blank(raw_df, ridx):
            return True
    return False


def parse_cleaned_rows_with_category_state(cleaned: pd.DataFrame, raw_df: pd.DataFrame, source_sheet: str) -> List[Dict[str, Any]]:
    """Parse rows while tracking spreadsheet category blocks.

    Category headings are rows with text but no price/cost/code. They update
    current_category and are dropped from the product output. Priced rows are
    kept and, while inside a category block, receive a title prefix and product_id
    generated from "Category - Item". Blank source rows reset the active category.
    """
    parsed: List[Dict[str, Any]] = []
    current_category = ""
    previous_raw_index: Optional[int] = None

    for raw_index, series in cleaned.iterrows():
        if blank_gap_between(raw_df, previous_raw_index, int(raw_index)):
            current_category = ""

        record = series.to_dict()
        row = row_from_dataframe_record(record, source_sheet, int(raw_index))
        previous_raw_index = int(raw_index)

        if not row:
            continue

        # Composite-layout support: Column A may be a stateful category column.
        # If Column A has text, it updates active_category; blank Column A retains
        # the previous category. This covers layouts like:
        #   Palisade | SEA04005006 | ... | R342.2
        #            | SEA05003006 | ... | R268.8
        #   Angle Iron | SEA04005006 | ... | R343.7
        # The same code under different active_category values stays separate.
        col_a = raw_cell_text(raw_df, int(raw_index), 0)
        if looks_like_category_cell(col_a, title_value(row)):
            current_category = col_a

        if is_category_heading_row(row):
            current_category = title_value(row)
            continue

        if row_has_price_metrics(row):
            row = apply_category_prefix(row, current_category)
        else:
            set_category_code_identity(row, current_category)

        parsed.append(row)

    return parsed



# ─────────────────────────────────────────────────────────────────────────────
# Universal sectioned spreadsheet parser
# ─────────────────────────────────────────────────────────────────────────────
# Many retail price lists are not a single rectangular table. They are visually
# arranged as repeated blocks such as:
#   No. | QUARTS | SELL | blank | No. | CANS & LONG TOMS | SELL
#   1   | Item A | 22   |       | 1   | Item B            | 20
# Later in the same sheet another header row may appear:
#   No. | WINE   | SELL | blank | No. | SPIRIT | SELL | 200ml | 300ml
# A generic pandas header mapper only reads one block and loses the others. This
# parser detects those repeated visual blocks without hard-coding the category
# names. It treats the header's middle label as the category, and extracts every
# priced item beneath each block.

def is_no_header(value: Any) -> bool:
    h = normalise_header(value)
    return bool(h) and (h == "no" or h == "no." or h.endswith(" no") or h in {"2 no", "no 2"})


def is_sell_header(value: Any) -> bool:
    h = normalise_header(value)
    return h in {"sell", "selling", "selling price", "price", "each price", "retail", "retail price", "trade", "retail trade", "retail/trade"}


def looks_like_extra_price_header(value: Any) -> bool:
    text = normalise_text(value)
    h = normalise_header(value)
    if not text or is_no_header(value) or is_sell_header(value):
        return False
    # Size/volume headers like 200ml, 300ml, 1L, 6 pack.
    if re.search(r"\b\d+(?:[,.]\d+)?\s*(ml|l|litre|liter|g|kg|pack|pcs|units?)\b", text, re.I):
        return True
    # Short labels are often option headers. Avoid long product-like text.
    return len(text) <= 16 and not parse_money(text)


def find_sectioned_table_blocks(raw_df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Find repeated visual table blocks inside a sheet.

    Returns blocks with header row, product column, price column, category label,
    and optional extra price columns. This is generic: it does not know category
    names like QUARTS/WINE/SPIRIT in advance.
    """
    blocks: List[Dict[str, Any]] = []
    if raw_df.empty:
        return blocks
    ncols = raw_df.shape[1]
    for ridx in range(len(raw_df)):
        row = raw_df.iloc[ridx]
        for c in range(0, max(0, ncols - 2)):
            if not is_no_header(row.iloc[c]):
                continue
            category = normalise_text(row.iloc[c + 1]) if c + 1 < ncols else ""
            price_header = row.iloc[c + 2] if c + 2 < ncols else ""
            if not category or not is_sell_header(price_header):
                continue
            extra_cols: List[Tuple[int, str]] = []
            j = c + 3
            while j < ncols:
                val = row.iloc[j]
                if not normalise_text(val):
                    break
                # Stop if another block begins.
                if is_no_header(val):
                    break
                if looks_like_extra_price_header(val):
                    extra_cols.append((j, normalise_text(val)))
                    j += 1
                    continue
                break
            blocks.append({
                "header_row": ridx,
                "no_col": c,
                "product_col": c + 1,
                "price_col": c + 2,
                "category": category,
                "extra_price_cols": extra_cols,
            })
    return blocks


def price_from_embedded_text(title: str) -> Tuple[str, float]:
    """Extract prices embedded in labels like '6 pack ... @R130,00'."""
    text = normalise_text(title)
    if not text:
        return "", 0.0
    match = re.search(r"(?:@|\bat\b)\s*R?\s*(\d+(?:[,.]\d{1,2})?)", text, re.I)
    if not match:
        return text, 0.0
    price = parse_money(match.group(1))
    cleaned = re.sub(r"\s*(?:@|\bat\b)\s*R?\s*\d+(?:[,.]\d{1,2})?", "", text, flags=re.I).strip()
    return cleaned or text, price



def is_price_value_cell(value: Any) -> bool:
    """True for actual price cells, false for labels like 200ml/300ml."""
    return parse_money(value) > 0 and not looks_like_extra_price_header(value)


def infer_extra_price_columns_from_first_data_row(raw_df: pd.DataFrame, start: int, price_col: int) -> List[Tuple[int, str]]:
    """Some sheets put extra price headers on the first product row.

    Example:
      No | SPIRIT | SELL |     |
      1  | Amarula 750ml | 200 | 200ml | 300ml
      2  | Ayoba 110ml   | 15  | 30    | 40
    Here 200ml/300ml are headers, not prices for Amarula. We infer them and
    only use them on rows where the cell contains a true price value.
    """
    if start >= len(raw_df):
        return []
    ncols = raw_df.shape[1]
    row = raw_df.iloc[start]
    extras: List[Tuple[int, str]] = []
    j = price_col + 1
    while j < ncols:
        label = normalise_text(row.iloc[j]) if j < len(row) else ""
        if not label:
            break
        if not looks_like_extra_price_header(label):
            break
        has_numeric_below = False
        for ridx in range(start + 1, min(len(raw_df), start + 8)):
            if is_price_value_cell(raw_df.iloc[ridx].iloc[j]):
                has_numeric_below = True
                break
        if has_numeric_below:
            extras.append((j, label))
        j += 1
    return extras

def title_with_replaced_size(title: str, new_size: str) -> str:
    """Replace the first size token in a title with new_size, otherwise append."""
    title = normalise_text(title)
    new_size = normalise_text(new_size)
    if not title or not new_size:
        return title
    pattern = r"\b\d+(?:[,.]\d+)?\s*(?:ml|l|litre|liter|g|kg)\b"
    if re.search(pattern, title, flags=re.I):
        return re.sub(pattern, new_size, title, count=1, flags=re.I).strip()
    return f"{title} {new_size}".strip()


def make_sectioned_product(title: str, category: str, price: float, source_sheet: str, ridx: int, code: str = "") -> Dict[str, Any]:
    title = clean_product_title(title)
    category = clean_product_title(category) or source_sheet or "Uncategorised"
    code = clean_code(code)
    product_id = slugify(title or code or f"row-{ridx + 1}")
    row = {
        "product_id": product_id,
        "product_name": title,
        "description": "",
        "category": category,
        "brand": "",
        "sku": code,
        "barcode": code,
        "selling_price": float(price or 0),
        "calculated_price": float(price or 0),
        "cost_price": 0.0,
        "image_url": "",
        "variant_enabled": "No",
        "attr1_name": "",
        "attr1_val": "",
        "attr2_name": "",
        "attr2_val": "",
        "attr3_name": "",
        "attr3_val": "",
        "source_sheet": source_sheet,
        "source_row": int(ridx) + 1,
        "source_context": f"sectioned table block: {category}",
    }
    set_category_code_identity(row, category)
    return row


def parse_sectioned_multi_table_sheet(raw_df: pd.DataFrame, source_sheet: str) -> List[Dict[str, Any]]:
    """Parse sheets with multiple side-by-side/repeated visual tables."""
    blocks = find_sectioned_table_blocks(raw_df)
    if not blocks:
        return []

    products: List[Dict[str, Any]] = []
    header_rows = sorted({int(b["header_row"]) for b in blocks})
    nrows = len(raw_df)

    for block in blocks:
        start = int(block["header_row"]) + 1
        later_headers = [r for r in header_rows if r > int(block["header_row"])]
        end = later_headers[0] if later_headers else nrows
        product_col = int(block["product_col"])
        price_col = int(block["price_col"])
        no_col = int(block["no_col"])
        category = normalise_text(block["category"])
        extra_price_cols = block.get("extra_price_cols") or []
        if not extra_price_cols:
            extra_price_cols = infer_extra_price_columns_from_first_data_row(raw_df, start, price_col)

        for ridx in range(start, end):
            row = raw_df.iloc[ridx]
            title = normalise_text(row.iloc[product_col]) if product_col < len(row) else ""
            if not title:
                continue
            # Skip accidental repeated headers inside the range.
            if is_sell_header(title) or is_no_header(title):
                continue

            price = parse_money(row.iloc[price_col]) if price_col < len(row) else 0.0
            if price <= 0:
                title2, embedded_price = price_from_embedded_text(title)
                if embedded_price > 0:
                    title = title2
                    price = embedded_price
            if price > 0:
                products.append(make_sectioned_product(title, category, price, source_sheet, ridx))

            # Optional extra price columns become additional size-specific products.
            # Example: SPIRIT section has SELL plus 200ml and 300ml columns.
            for extra_col, extra_label in extra_price_cols:
                if extra_col >= len(row):
                    continue
                cell_value = row.iloc[extra_col]
                # Do not treat header labels like 200ml/300ml as prices.
                if not is_price_value_cell(cell_value):
                    continue
                extra_price = parse_money(cell_value)
                extra_title = title_with_replaced_size(title, extra_label)
                # Avoid creating an identical duplicate if the normal title already
                # uses the same size and price.
                if normalise_for_compare(extra_title) == normalise_for_compare(title) and abs(extra_price - price) < 0.005:
                    continue
                products.append(make_sectioned_product(extra_title, category, extra_price, source_sheet, ridx))

    # Only trust this parser when it found a meaningful number of products. This
    # prevents a stray 'No / Category / Sell' note from hijacking a normal table.
    if len(products) < 5:
        return []
    return products




def make_product_row(
    product_name: str,
    category: str,
    selling_price: float,
    cost_price: float = 0.0,
    barcode: str = "",
    sku: str = "",
    source_sheet: str = "",
    source_row: int = 0,
    description: str = "",
    brand: str = "",
    image_url: str = "",
) -> Dict[str, Any]:
    code = clean_code(barcode or sku)
    title = clean_product_title(product_name)
    row = {
        "product_id": slugify(title or code or f"product-{source_row}"),
        "product_name": title,
        "description": normalise_text(description),
        "category": clean_product_title(category) or source_sheet or "Uncategorised",
        "brand": normalise_text(brand),
        "sku": clean_code(sku or code),
        "barcode": clean_code(barcode or code),
        "selling_price": float(selling_price or 0),
        "calculated_price": float(selling_price or 0),
        "variant_price": float(selling_price or 0),
        "cost_price": float(cost_price or 0),
        "image_url": normalise_text(image_url),
        "variant_enabled": "No",
        "attr1_name": "",
        "attr1_val": "",
        "attr2_name": "",
        "attr2_val": "",
        "attr3_name": "",
        "attr3_val": "",
        "source_sheet": source_sheet,
        "source_row": source_row,
    }
    row["_uid"] = row_uid(row, source_row or 0)
    return row

# ---------------------------------------------------------------------------
# Gemini-assisted layout planning layer
# ---------------------------------------------------------------------------
# Goal: AI decides *where the data is* and Python performs the full extraction.
# The model receives only a compact workbook sample and must return a strict
# JSON layout plan. It never extracts all products itself.

_AI_LAYOUT_CACHE: Dict[str, Dict[str, Any]] = {}


def column_letter_to_index(col: Any) -> Optional[int]:
    if col is None:
        return None
    if isinstance(col, int):
        return col
    text = str(col).strip()
    if not text:
        return None
    if text.isdigit():
        # Allow both zero and one-based indexes; most AI plans use letters.
        n = int(text)
        return max(0, n - 1)
    text = re.sub(r"[^A-Za-z]", "", text).upper()
    if not text:
        return None
    total = 0
    for ch in text:
        total = total * 26 + (ord(ch) - ord('A') + 1)
    return total - 1


def cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


def safe_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, list):
        return [safe_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): safe_jsonable(v) for k, v in value.items()}
    return str(value)


def workbook_sample_from_bytes(raw_bytes: bytes, ext: str, max_rows: int = 60, max_cols: int = 18) -> Dict[str, Any]:
    """Create a compact, model-friendly representation of the workbook.

    This is intentionally small: sheet names, row/column coordinates and values
    for the first rows only. Python still processes the full workbook later.
    """
    sample: Dict[str, Any] = {"file_type": ext, "sheets": []}
    if ext == "csv":
        df = pd.read_csv(io.BytesIO(raw_bytes), dtype=object, header=None)
        sheets = {"CSV": df}
    else:
        engine = "openpyxl" if ext in {"xlsx", "xlsm"} else None
        sheets = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=None, dtype=object, header=None, engine=engine)

    for sheet_name, df in sheets.items():
        rows = []
        nrows = min(len(df), max_rows)
        ncols = min(len(df.columns), max_cols)
        for r in range(nrows):
            cells = []
            for c in range(ncols):
                val = cell_to_text(df.iat[r, c])
                if val:
                    cells.append({"r": r + 1, "c": c + 1, "col": index_to_excel_col(c), "v": val[:120]})
            if cells:
                rows.append({"row": r + 1, "cells": cells})
        sample["sheets"].append({
            "sheet_name": str(sheet_name),
            "rows": rows,
            "max_sampled_row": nrows,
            "max_sampled_col": ncols,
        })
    return sample


def index_to_excel_col(idx: int) -> str:
    idx += 1
    letters = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def ai_layout_prompt(sample: Dict[str, Any], user_instructions: str = "") -> str:
    return (
        "You are a retail spreadsheet layout planner for a Yoco POS import tool. "
        "You DO NOT extract all products. You only inspect the sampled rows and return a JSON extraction plan that Python can apply to the full workbook.\n\n"
        "Return strict JSON only with this shape:\n"
        "{\n"
        '  "confidence": 0.0,\n'
        '  "layout_type": "standard_table|side_by_side_tables|category_blocks|variant_export|headerless_price_list|unknown",\n'
        '  "notes": "short reason",\n'
        '  "sheets": [\n'
        '    {"sheet_name":"...", "layout_type":"...", "header_row":1, "start_row":2,\n'
        '     "category_rule":"none|column_a_state|header_blocks",\n'
        '     "columns":{"product_name":"B", "description":"C", "sku":"A", "barcode":"D", "selling_price":"E", "cost_price":"F", "category":"A", "brand":"", "image_url":""},\n'
        '     "tables":[{"category":"QUARTS", "header_row":2, "start_row":3, "columns":{"product_name":"B", "selling_price":"C", "sku":"A"}}],\n'
        '     "variant":{"enabled":false, "product_id":"Handle", "attribute_1":"Option1 Name", "value_1":"Option1 Value", "attribute_2":"Option2 Name", "value_2":"Option2 Value", "price":"Variant Price", "sku":"Variant SKU"}\n'
        "    }\n"
        "  ],\n"
        '  "warnings": []\n'
        "}\n\n"
        "Rules:\n"
        "- Use Excel column letters for columns whenever possible.\n"
        "- For side-by-side tables, create one table object per visible block.\n"
        "- If Column A contains a category that applies to following rows, use category_rule column_a_state.\n"
        "- Identify selling/retail/customer price, not cost/wholesale, unless wholesale is clearly the only customer price.\n"
        "- Product names containing x 6, x12, x 24, case, pack, pcs or units are case/pack rows. Do not mark them as normal variants; Python will flag them for user confirmation.\n"
        "- If confidence is below 0.70, return layout_type unknown and explain why.\n\n"
        "User/operator instructions to respect when choosing columns or category rules:\n"
        + (normalise_text(user_instructions) or "None")
        + "\n\nWorkbook sample:\n" + json.dumps(safe_jsonable(sample), ensure_ascii=False)
    )


def call_gemini_layout_planner(sample: Dict[str, Any], api_key_override: str = "", user_instructions: str = "") -> Optional[Dict[str, Any]]:
    """Call Gemini if GEMINI_API_KEY is configured. Otherwise return None.

    The AI only returns a workbook layout plan. It does not extract products.
    Uses urllib so the requirements file does not need a Google SDK package.
    Key source priority:
      1. request-provided Gemini Canvas key via api_key_override
      2. GEMINI_API_KEY / GOOGLE_API_KEY / GOOGLE_GENERATIVE_AI_API_KEY / API_KEY env var
      3. no AI plan; Python fallback
    Environment variables:
      - GEMINI_LAYOUT_MODEL, defaults to gemini-2.5-flash-preview-09-2025
    """
    api_key = (
        str(api_key_override or "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY", "").strip()
        or os.environ.get("API_KEY", "").strip()
    )
    if not api_key:
        return None

    model = os.environ.get("GEMINI_LAYOUT_MODEL", "gemini-2.5-flash-preview-09-2025").strip() or "gemini-2.5-flash-preview-09-2025"
    cache_key = hashlib.sha256(json.dumps({"sample": safe_jsonable(sample), "user_instructions": normalise_text(user_instructions)}, sort_keys=True).encode("utf-8")).hexdigest()
    if cache_key in _AI_LAYOUT_CACHE:
        return _AI_LAYOUT_CACHE[cache_key]

    import urllib.request
    import urllib.parse
    import urllib.error

    prompt = (
        "Return only strict JSON. No markdown, no prose.\n\n"
        + ai_layout_prompt(sample, user_instructions=user_instructions)
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    # Gemini API endpoint. The API key is URL encoded to avoid issues with special characters.
    encoded_key = urllib.parse.quote(api_key, safe="")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={encoded_key}"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        app.logger.warning("Gemini layout planner unavailable: %s", exc)
        return None

    text = ""
    try:
        candidates = body.get("candidates", []) if isinstance(body, dict) else []
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    except Exception:
        text = ""

    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        plan = json.loads(text)
        if isinstance(plan, dict):
            _AI_LAYOUT_CACHE[cache_key] = plan
            return plan
    except Exception as exc:
        app.logger.warning("Gemini layout planner returned invalid JSON: %s", exc)
    return None


# Backwards-compatible alias so older code paths keep working.
def call_openai_layout_planner(sample: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return call_gemini_layout_planner(sample)


def get_cell_by_plan(row: List[Any], col: Any) -> str:
    idx = column_letter_to_index(col)
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return cell_to_text(row[idx])


def extract_products_with_ai_plan(raw_bytes: bytes, ext: str, plan: Dict[str, Any], parse_mode: str = "variant") -> List[Dict[str, Any]]:
    """Apply an AI layout plan to the full workbook using deterministic Python.

    This intentionally supports broad layout families. If the plan is missing
    required fields or yields too few rows, the caller falls back to the legacy
    Python parsers.
    """
    if not plan or float(plan.get("confidence") or 0) < 0.70:
        return []
    if ext == "csv":
        sheets = {"CSV": pd.read_csv(io.BytesIO(raw_bytes), dtype=object, header=None)}
    else:
        engine = "openpyxl" if ext in {"xlsx", "xlsm"} else None
        sheets = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=None, dtype=object, header=None, engine=engine)

    sheet_plans = {str(sp.get("sheet_name", "")): sp for sp in plan.get("sheets", []) if isinstance(sp, dict)}
    products: List[Dict[str, Any]] = []

    for sheet_name, df in sheets.items():
        sp = sheet_plans.get(str(sheet_name))
        if not sp:
            # If there is only one sheet, allow a single unnamed plan to apply.
            if len(sheet_plans) == 1:
                sp = next(iter(sheet_plans.values()))
            else:
                continue
        layout_type = sp.get("layout_type") or plan.get("layout_type") or "standard_table"
        tables = sp.get("tables") or []
        if layout_type == "side_by_side_tables" and tables:
            for table in tables:
                cols = table.get("columns") or {}
                start_row = int(table.get("start_row") or (int(table.get("header_row") or 1) + 1))
                category = cell_to_text(table.get("category") or "")
                for ridx in range(max(0, start_row - 1), len(df)):
                    row = list(df.iloc[ridx].values)
                    name = get_cell_by_plan(row, cols.get("product_name") or cols.get("name") or cols.get("description"))
                    price = parse_money(get_cell_by_plan(row, cols.get("selling_price") or cols.get("price") or cols.get("sell")))
                    if not name or price is None:
                        continue
                    code = get_cell_by_plan(row, cols.get("barcode") or cols.get("sku") or cols.get("code"))
                    cost = parse_money(get_cell_by_plan(row, cols.get("cost_price") or cols.get("cost"))) or 0
                    title = clean_product_title(name)
                    products.append(make_product_row(
                        product_name=title,
                        category=category or str(sheet_name),
                        selling_price=price,
                        cost_price=cost,
                        barcode=code,
                        sku=code,
                        source_sheet=str(sheet_name),
                        source_row=ridx + 1,
                    ))
            continue

        if layout_type in {"standard_table", "category_blocks", "headerless_price_list", "variant_export"}:
            cols = sp.get("columns") or {}
            header_row = int(sp.get("header_row") or 1)
            start_row = int(sp.get("start_row") or (header_row + 1))
            category_rule = sp.get("category_rule") or "none"
            active_category = ""
            for ridx in range(max(0, start_row - 1), len(df)):
                row = list(df.iloc[ridx].values)
                cat_cell = get_cell_by_plan(row, cols.get("category"))
                name = get_cell_by_plan(row, cols.get("product_name") or cols.get("description") or cols.get("name"))
                price = parse_money(get_cell_by_plan(row, cols.get("selling_price") or cols.get("price") or cols.get("sell")))
                cost = parse_money(get_cell_by_plan(row, cols.get("cost_price") or cols.get("cost"))) or 0
                code = get_cell_by_plan(row, cols.get("barcode") or cols.get("sku") or cols.get("code"))

                if category_rule == "column_a_state" and cat_cell:
                    active_category = clean_product_title(cat_cell)
                if name and price is None and not code and not cost and category_rule in {"column_a_state", "header_blocks"}:
                    active_category = clean_product_title(name)
                    continue
                if not name or price is None:
                    continue
                category = cat_cell if category_rule == "none" else (active_category or cat_cell)
                title = clean_product_title(name)
                if category and category_rule in {"column_a_state", "header_blocks"}:
                    # Prefix only when the title does not already contain the category.
                    if category.lower() not in title.lower():
                        title = f"{category} - {title}"
                products.append(make_product_row(
                    product_name=title,
                    category=category or str(sheet_name),
                    selling_price=price,
                    cost_price=cost,
                    barcode=code,
                    sku=code,
                    source_sheet=str(sheet_name),
                    source_row=ridx + 1,
                ))
    return products



def parse_known_structured_export_from_bytes(raw_bytes: bytes, ext: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse already-structured POS/e-commerce exports before AI planning.

    Shopify/Yoco exports already contain explicit variant rows and option columns.
    Sending them through the Gemini layout planner can flatten or miss variants,
    especially when product-level cells are blank on continuation rows. This
    fast path preserves the native row-per-variant structure and only falls back
    to AI/general parsers for unstructured supplier price lists.
    """
    ext = normalise_text(ext).lower().strip().lstrip(".")
    if ext not in {"xlsx", "xls", "xlsm", "xl"}:
        return [], {"structured_export_detected": False}

    engine = "openpyxl" if ext in {"xlsx", "xlsm"} else None
    try:
        sheets = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=None, dtype=object, header=None, engine=engine)
    except Exception:
        return [], {"structured_export_detected": False}

    products: List[Dict[str, Any]] = []
    detected_kinds: List[str] = []
    detected_sheets: List[str] = []

    for sheet_name, raw_df in sheets.items():
        source_sheet = str(sheet_name)
        if is_yoco_products_export_sheet(raw_df):
            rows = parse_yoco_products_export_sheet(raw_df, source_sheet)
            if rows:
                products.extend(rows)
                detected_kinds.append("yoco_products_export")
                detected_sheets.append(source_sheet)
            continue

        if is_shopify_export_sheet(raw_df):
            rows = parse_shopify_export_sheet(raw_df, source_sheet)
            if rows:
                products.extend(rows)
                detected_kinds.append("shopify_products_export")
                detected_sheets.append(source_sheet)
            continue

    if not products:
        return [], {"structured_export_detected": False}

    return products, {
        "structured_export_detected": True,
        "layout_strategy": "+".join(sorted(set(detected_kinds))) or "structured_export",
        "structured_sheets": detected_sheets,
        "structured_rows": len(products),
        "ai_rows": 0,
    }



_CATEGORY_KEYWORDS = [
    ("Cleaning", ["clean", "detergent", "soap", "bleach", "mop", "broom", "dishwash", "laundry", "sanit", "polish"]),
    ("Beverages", ["juice", "drink", "cooldrink", "soda", "water", "cola", "tonic", "energy"]),
    ("Beer", ["beer", "lager", "pilsner", "draught", "stout", "castle", "heineken", "carling"]),
    ("Wine", ["wine", "merlot", "chardonnay", "sauvignon", "cabernet", "rose", "pinot"]),
    ("Spirits", ["whisky", "whiskey", "vodka", "gin", "rum", "brandy", "tequila", "liqueur"]),
    ("Snacks", ["chips", "crisps", "sweets", "chocolate", "biscuit", "cracker", "popcorn"]),
    ("Bakery", ["bread", "roll", "bun", "cake", "muffin", "pastry"]),
    ("Dairy", ["milk", "cheese", "yoghurt", "yogurt", "butter", "cream"]),
    ("Clothing & Apparel", ["shirt", "pants", "trouser", "jacket", "shorts", "dress", "shoe", "cap", "sock"]),
    ("Hardware", ["bolt", "screw", "nail", "paint", "cement", "pipe", "cable", "wire", "tool"]),
]



def infer_category_from_title_for_instructions(row: Dict[str, Any]) -> str:
    text = normalise_for_compare(" ".join([
        title_value(row),
        normalise_text(row.get("description") or row.get("Description")),
        normalise_text(row.get("brand") or row.get("Brand")),
    ]))
    if not text:
        return ""
    for category, words in _CATEGORY_KEYWORDS:
        if any(word in text for word in words):
            return category
    return ""


def _instruction_rename_rules(user_instructions: str) -> List[tuple]:
    """Extract category rename rules from operator instructions.

    Supported natural language patterns:
      - rename wine to best wines
      - rename category spirits to top spirits
      - rename "wine" to "best wines"
      - change wine category to best wines
      - call wine "best wines"
      - wine → best wines
      - wine -> best wines
      - wine = best wines
    Returns a list of (from_normalised, to_display) tuples.
    """
    text = user_instructions.strip()
    if not text:
        return []

    rules: List[tuple] = []
    seen: set = set()

    patterns = [
        # rename [category] X to Y  /  rename X [category] to Y
        r"rename\s+(?:category\s+)?[\"']?([^\"'\n\-–>=/]+?)[\"']?\s+(?:category\s+)?to\s+[\"']?([^\"'\n;]+?)[\"']?\s*(?:;|$|\n)",
        # change X [category] to Y  /  change category X to Y
        r"change\s+(?:category\s+)?[\"']?([^\"'\n\-–>=/]+?)[\"']?\s+(?:category\s+)?to\s+[\"']?([^\"'\n;]+?)[\"']?\s*(?:;|$|\n)",
        # call X Y  /  call X "Y"
        r"call\s+[\"']?([^\"'\n\-–>=/]+?)[\"']?\s+[\"']([^\"'\n]+)[\"']",
        # X → Y  /  X -> Y
        r"[\"']?([^\"'\n\-–>=/]{2,40}?)[\"']?\s*(?:→|->)\s*[\"']?([^\"'\n;]{2,60}?)[\"']?\s*(?:;|$|\n)",
        # X = Y  (only when used in rename context; guarded by leading keyword)
        r"rename\s+[\"']?([^\"'\n=]{2,40}?)[\"']?\s*=\s*[\"']?([^\"'\n;]{2,60}?)[\"']?\s*(?:;|$|\n)",
    ]

    # Normalise whitespace and ensure trailing newline so $ anchors work
    normalised = re.sub(r"[ \t]+", " ", text.lower()) + "\n"

    for pat in patterns:
        for m in re.finditer(pat, normalised, flags=re.I):
            frm = m.group(1).strip().strip("\"'")
            to = m.group(2).strip().strip("\"'")
            frm_key = normalise_for_compare(frm)
            if frm_key and to and frm_key not in seen and len(frm_key) >= 2:
                seen.add(frm_key)
                # Preserve original casing from user input for the display value
                # Find the original casing in the raw input
                raw_match = re.search(re.escape(to), text, flags=re.I)
                display_to = raw_match.group(0) if raw_match else to.title()
                rules.append((frm_key, display_to))

    return rules


def _instruction_exclusion_terms(user_instructions: str) -> List[str]:
    """Extract simple ignore/exclude/remove terms from operator instructions.

    Examples supported:
      - ignore cleaning products
      - exclude tobacco and vapes
      - skip category: Hardware
    """
    text = normalise_text(user_instructions).lower()
    if not text:
        return []
    terms: List[str] = []
    for match in re.finditer(r"\b(?:ignore|exclude|remove|skip|do not include|dont include|don't include)\b\s*[:\-]?\s*([^\.\n;]+)", text, flags=re.I):
        phrase = match.group(1).strip()
        phrase = re.sub(r"\b(products?|items?|category|categories|rows?|please|from the file|from export)\b", " ", phrase, flags=re.I)
        for part in re.split(r",|/|\band\b|\bor\b", phrase):
            term = normalise_for_compare(part)
            if term and len(term) >= 3 and term not in {"all", "the", "any"}:
                terms.append(term)
    # Preserve order and uniqueness.
    out: List[str] = []
    seen = set()
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


def apply_ai_instruction_postprocess(products: List[Dict[str, Any]], user_instructions: str = "", vat_enabled: str = "Yes", track_stock_enabled: bool = True, gemini_api_key: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply the dashboard AI-instruction controls to deterministic Python rows.

    The HTML chat window calls Gemini Canvas AI to interpret the user's raw instructions
    into precise directives before storing them. By the time instructions reach this
    function (via the user_instructions form field), they are already AI-clarified.
    This function applies those clarified directives to spreadsheet-extracted rows.
    """
    raw_instructions = (user_instructions or "").strip()
    if not raw_instructions:
        return products, {
            "ai_instruction_rows_removed": 0,
            "ai_instruction_exclusion_terms": [],
            "ai_instruction_categories_inferred": 0,
            "ai_instruction_categories_renamed": 0,
            "ai_instruction_names_cleaned": 0,
            "ai_instruction_barcodes_filled": 0,
            "ai_instruction_stock_filled": 0,
            "ai_instructions_applied": False,
        }

    instructions = normalise_text(raw_instructions)
    exclusions = _instruction_exclusion_terms(instructions)
    rename_rules = _instruction_rename_rules(user_instructions)
    out: List[Dict[str, Any]] = []
    removed = 0
    categories_inferred = 0
    categories_renamed = 0
    cleaned_names = 0
    barcode_filled = 0
    stock_filled = 0

    for original in products:
        row = dict(original)
        haystack = normalise_for_compare(" ".join([
            title_value(row),
            normalise_text(row.get("category") or row.get("Category")),
            normalise_text(row.get("description") or row.get("Description")),
            normalise_text(row.get("brand") or row.get("Brand")),
        ]))
        if exclusions and any(term in haystack for term in exclusions):
            removed += 1
            continue

        cleaned_title = clean_product_title(title_value(row))
        if cleaned_title and cleaned_title != title_value(row):
            set_title(row, cleaned_title)
            cleaned_names += 1

        category = normalise_text(row.get("category") or row.get("Category"))
        if not category or category.lower() == "uncategorised":
            inferred = infer_category_from_title_for_instructions(row)
            if inferred:
                row["category"] = row["Category"] = inferred
                categories_inferred += 1
                category = inferred

        # Apply rename rules: match current category against each rule's from-key.
        if rename_rules and category:
            cat_key = normalise_for_compare(category)
            for frm_key, to_display in rename_rules:
                if frm_key in cat_key or cat_key in frm_key:
                    row["category"] = to_display
                    row["Category"] = to_display
                    # Also update active_category fields used in composite key
                    if row.get("active_category"):
                        row["active_category"] = to_display
                    if row.get("_active_category"):
                        row["_active_category"] = to_display
                    categories_renamed += 1
                    break

        code = best_code(row)
        if code:
            if not clean_code(row.get("barcode") or row.get("Barcode")):
                row["barcode"] = row["Barcode"] = code
                barcode_filled += 1
            if not clean_code(row.get("sku") or row.get("SKU")):
                row["sku"] = row["SKU"] = code

        row["vat_enabled"] = "Yes" if str(vat_enabled).lower() in {"yes", "true", "1", "y"} else "No"
        row["VAT Enabled"] = row["vat_enabled"]

        if not normalise_text(row.get("track_stock") or row.get("Track Stock")):
            row["track_stock"] = row["Track Stock"] = ("Variant" if row_is_variant(row) else "Product") if track_stock_enabled else "No"
            stock_filled += 1
        out.append(row)

    return out, {
        "ai_instruction_rows_removed": removed,
        "ai_instruction_exclusion_terms": exclusions,
        "ai_instruction_categories_inferred": categories_inferred,
        "ai_instruction_categories_renamed": categories_renamed,
        "ai_instruction_names_cleaned": cleaned_names,
        "ai_instruction_barcodes_filled": barcode_filled,
        "ai_instruction_stock_filled": stock_filled,
        "ai_instructions_applied": bool(instructions),
    }

def parse_uploaded_file_ai_assisted(file_storage, parse_mode: str = "variant", gemini_api_key: str = "", user_instructions: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """New architecture entrypoint.

    1. Python samples workbook structure.
    2. Optional Gemini AI returns a layout plan.
    3. Python executes the plan at full scale.
    4. If AI is unavailable/low-confidence, fallback to existing parsers.
    """
    filename = file_storage.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    raw_bytes = file_storage.read()

    # Structured exports (Shopify/Yoco) already describe variants explicitly.
    # Parse them directly before AI planning so continuation rows with blank
    # product-level fields are not mistaken for non-variant/detail rows.
    structured_products, structured_meta = parse_known_structured_export_from_bytes(raw_bytes, ext)
    if structured_products:
        return structured_products, structured_meta

    sample = workbook_sample_from_bytes(raw_bytes, ext)
    plan = call_gemini_layout_planner(sample, api_key_override=gemini_api_key, user_instructions=user_instructions)
    ai_products: List[Dict[str, Any]] = []
    if plan:
        ai_products = extract_products_with_ai_plan(raw_bytes, ext, plan, parse_mode=parse_mode)
    if ai_products:
        return ai_products, {"layout_strategy": "ai_plan", "layout_plan": plan, "ai_rows": len(ai_products)}

    # Fallback: feed a fresh in-memory file to the existing Python parser.
    from werkzeug.datastructures import FileStorage
    fallback_file = FileStorage(stream=io.BytesIO(raw_bytes), filename=filename)
    fallback_products = parse_uploaded_file(fallback_file)
    return fallback_products, {
        "layout_strategy": "python_fallback",
        "layout_plan": plan,
        "ai_rows": 0,
        "fallback_rows": len(fallback_products),
        "ai_available": bool((os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip() or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY", "").strip() or os.environ.get("API_KEY", "").strip())),
    }

def parse_uploaded_file(file_storage) -> List[Dict[str, Any]]:
    filename = file_storage.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    products: List[Dict[str, Any]] = []

    if ext == "csv":
        raw = file_storage.read()
        df = pd.read_csv(io.BytesIO(raw), dtype=object, header=None)
        cleaned = dataframe_from_sheet(df)
        products.extend(parse_cleaned_rows_with_category_state(cleaned, df, "CSV"))
        return products

    if ext in {"xlsx", "xls", "xlsm", "xl"}:
        engine = "openpyxl" if ext in {"xlsx", "xlsm"} else None
        sheets = pd.read_excel(file_storage, sheet_name=None, dtype=object, header=None, engine=engine)
        for sheet_name, raw_df in sheets.items():
            source_sheet = str(sheet_name)

            # Yoco Products sheets are already in final import/export format.
            # Parse them directly so Default Price and Variant Price are not collapsed.
            if is_yoco_products_export_sheet(raw_df):
                products.extend(parse_yoco_products_export_sheet(raw_df, source_sheet))
                continue

            # E-commerce exports such as Shopify are already structured as one
            # row per variant and use repeated blank product-level cells. Parse
            # them before visual-block/headerless heuristics.
            if is_shopify_export_sheet(raw_df):
                products.extend(parse_shopify_export_sheet(raw_df, source_sheet))
                continue

            # First try the universal visual-block parser. It handles versatile
            # layouts with repeated side-by-side sections such as:
            #   No. | QUARTS | SELL | blank | No. | CANS | SELL
            # and later sections on the same sheet. If it produces enough rows,
            # trust it and skip the rectangular-table parser.
            sectioned_products = parse_sectioned_multi_table_sheet(raw_df, source_sheet)
            if sectioned_products:
                products.extend(sectioned_products)
                continue

            inline_header_idx = find_type_treatment_header_row(raw_df)
            inline_products: List[Dict[str, Any]] = []
            normal_df = raw_df
            if inline_header_idx is not None:
                inline_products = parse_type_treatment_section(raw_df, source_sheet)
                normal_df = raw_df.iloc[:inline_header_idx].copy()

            cleaned = dataframe_from_sheet(normal_df)
            if cleaned.empty:
                products.extend(inline_products)
                continue

            mapped = map_columns(list(cleaned.columns))
            # Only use the headerless side-by-side parser when no real headers were detected.
            # This prevents proper sheets like VERMONT or Diggersrest from being mistaken for
            # code/cost/price blocks.
            if not mapped and looks_like_headerless_pair_sheet(raw_df):
                products.extend(parse_headerless_pair_sheet(raw_df, source_sheet))
                continue

            products.extend(parse_cleaned_rows_with_category_state(cleaned, normal_df, source_sheet))
            products.extend(inline_products)
        return products

    raise ValueError(f"Unsupported file type: .{ext}")


def product_to_yoco_row(row: Dict[str, Any], track_stock: str = "Product", vat_enabled: str = "Yes") -> Dict[str, Any]:
    variant_price = get_price(row)
    default_price = parse_money(row.get("default_price") or row.get("selling_price") or row.get("Default Price")) or variant_price
    cost = get_cost(row)
    product_id = product_id_value(row) or slugify(title_value(row))
    product_name = title_value(row)
    code = best_code(row)

    variant_enabled = normalise_text(row.get("variant_enabled") or row.get("Variant Enabled"))
    variant_enabled = "Yes" if variant_enabled.lower() == "yes" else "No"
    if variant_enabled != "Yes":
        default_price = variant_price

    row_track_stock = normalise_text(row.get("track_stock") or row.get("Track Stock"))
    if not row_track_stock:
        row_track_stock = "Variant" if variant_enabled == "Yes" else track_stock

    default_quantity = normalise_text(row.get("quantity") or row.get("Default Quantity") or row.get("default_quantity")) or "1"

    return {
        "Product ID": product_id,
        "Product Name": product_name,
        "Description": normalise_text(row.get("description") or row.get("Description")),
        "Default Price": default_price,
        "Brand": normalise_text(row.get("brand") or row.get("Brand")),
        "Category": normalise_text(row.get("category") or row.get("Category")) or "Uncategorised",
        "SKU": normalise_text(row.get("sku") or row.get("SKU") or code),
        "Default Cost Price": cost,
        "Ask For Quantity": "No",
        "Default Quantity": default_quantity,
        "Quantity Units": normalise_text(row.get("quantity_units") or row.get("Quantity Units")),
        "Ask For Price": "No",
        "VAT Enabled": normalise_text(row.get("vat_enabled") or row.get("VAT Enabled")) or vat_enabled,
        "Variant Price": variant_price,
        "Variant Enabled": variant_enabled,
        "Attribute 1": normalise_text(row.get("attr1_name") or row.get("Attribute 1")),
        "Value 1": normalise_text(row.get("attr1_val") or row.get("Value 1")),
        "Attribute 2": normalise_text(row.get("attr2_name") or row.get("Attribute 2")),
        "Value 2": normalise_text(row.get("attr2_val") or row.get("Value 2")),
        "Attribute 3": normalise_text(row.get("attr3_name") or row.get("Attribute 3")),
        "Value 3": normalise_text(row.get("attr3_val") or row.get("Value 3")),
        "Image URL": normalise_text(row.get("image_url") or row.get("Image URL")),
        "Barcode": code,
        "Track Stock": row_track_stock,
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
            "GET /",
            "GET /health",
            "POST /process-retail-file-json",
            "POST /process-retail-file",
            "POST /export-yoco-file",
            "POST /resolve-price-conflict",
        ],
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Yoco retail file processor"})




@app.post("/debug-upload")
def debug_upload():
    """Tiny upload diagnostic endpoint.

    Use this to confirm whether the browser can POST the selected file to Render
    at all. It does not parse the workbook, so any failure here is CORS, request
    size, network, or Render routing rather than parser code.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Use multipart field name 'file'."}), 400
    uploaded_file = request.files["file"]
    stream = uploaded_file.stream
    pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(pos)
    return jsonify({
        "status": "ok",
        "filename": uploaded_file.filename,
        "bytes": size,
        "maxUploadMB": MAX_UPLOAD_MB,
    })


@app.post("/process-retail-file-json")
def process_retail_file_json():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Use multipart field name 'file'."}), 400

    uploaded_file = request.files["file"]
    try:
        parse_mode = request.form.get("parse_mode", "variant")
        gemini_api_key = (
            request.form.get("gemini_api_key", "")
            or request.form.get("apiKey", "")
            or request.form.get("api_key", "")
        )
        user_instructions = (
            request.form.get("ai_instructions", "")
            or request.form.get("user_instructions", "")
            or request.form.get("instructions", "")
            or request.form.get("ai_notes", "")
        )
        vat_enabled = "Yes" if str(request.form.get("vat_enabled", "true")).lower() in {"true", "yes", "1", "y"} else "No"
        track_stock_enabled = str(request.form.get("track_stock", "true")).lower() in {"true", "yes", "1", "y"}
        raw_products, layout_meta = parse_uploaded_file_ai_assisted(uploaded_file, parse_mode=parse_mode, gemini_api_key=gemini_api_key, user_instructions=user_instructions)
        raw_products, instruction_meta = apply_ai_instruction_postprocess(raw_products, user_instructions=user_instructions, vat_enabled=vat_enabled, track_stock_enabled=track_stock_enabled, gemini_api_key=gemini_api_key)
        payload = preflight_products_payload(raw_products, parse_mode=parse_mode)
        products = payload["products"]
        issues = build_issues(products)
        summary = dict(payload.get("metadata") or {})
        summary.update(layout_meta or {})
        summary.update(instruction_meta or {})
        summary.update({
            "errors": sum(1 for i in issues if i["level"] == "error"),
            "warnings": sum(1 for i in issues if i["level"] == "warning"),
        })
        # Return a compact payload by default. This prevents large cross-origin
        # POST responses from being reported by browsers as opaque
        # `TypeError: Failed to fetch` errors.
        return jsonify({
            "products": [compact_product_for_frontend(row) for row in products],
            "price_conflicts": [
                compact_conflict_for_frontend(conflict)
                for conflict in payload.get("price_conflicts", [])
            ],
            "issues": issues,
            "summary": summary,
        })
    except Exception as exc:
        import traceback
        app.logger.exception("process-retail-file-json failed")
        return cors_json({
            "error": str(exc),
            "type": exc.__class__.__name__,
            "traceback_tail": traceback.format_exc().splitlines()[-12:],
        }, 500)


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
        parse_mode = request.form.get("parse_mode", "variant")
        gemini_api_key = (
            request.form.get("gemini_api_key", "")
            or request.form.get("apiKey", "")
            or request.form.get("api_key", "")
        )
        user_instructions = (
            request.form.get("ai_instructions", "")
            or request.form.get("user_instructions", "")
            or request.form.get("instructions", "")
            or request.form.get("ai_notes", "")
        )
        vat_enabled = "Yes" if str(request.form.get("vat_enabled", "true")).lower() in {"true", "yes", "1", "y"} else "No"
        track_stock_enabled = str(request.form.get("track_stock", "true")).lower() in {"true", "yes", "1", "y"}
        raw_products, layout_meta = parse_uploaded_file_ai_assisted(uploaded_file, parse_mode=parse_mode, gemini_api_key=gemini_api_key, user_instructions=user_instructions)
        raw_products, _instruction_meta = apply_ai_instruction_postprocess(raw_products, user_instructions=user_instructions, vat_enabled=vat_enabled, track_stock_enabled=track_stock_enabled, gemini_api_key=gemini_api_key)
        raw_products = apply_parse_mode(raw_products, parse_mode)
        products = preflight_products_for_frontend(raw_products)
        normalise_sparse_variant_matrices_for_yoco(products)
        make_skus_unique_for_yoco(products)
        enrich_remaining_duplicate_product_ids(products)
        output = products_to_workbook(products)
        return send_file(
            output,
            as_attachment=True,
            download_name="yoco_import_ready.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        import traceback
        app.logger.exception("process-retail-file failed")
        return cors_json({
            "error": str(exc),
            "type": exc.__class__.__name__,
            "traceback_tail": traceback.format_exc().splitlines()[-12:],
        }, 500)


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
        parse_mode = payload.get("parse_mode")
        if parse_mode:
            products = apply_parse_mode(products, parse_mode)
        products = preflight_products_for_frontend(products)
        normalise_sparse_variant_matrices_for_yoco(products)
        make_skus_unique_for_yoco(products)
        output = products_to_workbook(products)
        return send_file(
            output,
            as_attachment=True,
            download_name="yoco_import_ready.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500



@app.post("/resolve-price-conflict")
def resolve_price_conflict():
    """Record the user's price-conflict choice.

    The frontend removes the conflict card immediately for speed and sends this
    asynchronously. Because /export-yoco-file receives the final edited products
    from the browser, this endpoint is intentionally lightweight. Swap the
    in-memory dictionary for a persistent store in production if needed.
    """
    payload = request.get_json(silent=True) or {}
    conflict_id = normalise_text(payload.get("conflict_id"))
    selected_uid = normalise_text(payload.get("selected_uid"))
    if not conflict_id or not selected_uid:
        return jsonify({"error": "conflict_id and selected_uid are required"}), 400

    rejected_uids = payload.get("rejected_uids") or []
    if not isinstance(rejected_uids, list):
        rejected_uids = []

    PRICE_CONFLICT_DECISIONS[conflict_id] = {
        "conflict_id": conflict_id,
        "selected_uid": selected_uid,
        "rejected_uids": rejected_uids,
        "selected_price": parse_money(payload.get("selected_price")),
        "selected_row": payload.get("selected_row") or {},
    }
    return jsonify({"ok": True, "saved": PRICE_CONFLICT_DECISIONS[conflict_id]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
