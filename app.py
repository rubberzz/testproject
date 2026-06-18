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
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.setdefault("Access-Control-Expose-Headers", "Content-Disposition, Content-Type")
    return response

@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    # Always return JSON + CORS instead of letting the connection die silently.
    app.logger.exception("Unhandled backend error")
    return jsonify({"error": str(exc), "type": exc.__class__.__name__}), 500


MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Lightweight in-memory store for conflict choices from the frontend.
# For production you can replace this with Firestore/Postgres/S3/etc.
PRICE_CONFLICT_DECISIONS: Dict[str, Dict[str, Any]] = {}

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



def category_code_key(row: Dict[str, Any]) -> str:
    """Composite identity for supplier/manufacturing codes inside category blocks.

    The same manufacturing code can appear in multiple category sections, for example
    Palisade / SEA04005006 and Angle Iron / SEA04005006. Those are separate retail
    items even though the code is identical, so all collision/dedupe logic must use
    category + code rather than code alone.
    """
    category = normalise_text(
        row.get("active_category")
        or row.get("_active_category")
        or row.get("block_category")
        or row.get("category")
        or row.get("Category")
        or row.get("source_sheet")
        or row.get("Source Sheet")
    )
    code = clean_code(best_code(row)).lower()
    if not code:
        return ""
    return f"{slugify(category) or 'uncategorised'}_{code}"


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
    if normalise_for_compare(text) == normalise_for_compare(row_title):
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

def true_duplicate_key(row: Dict[str, Any]) -> Tuple[str, str, str, float, float]:
    return (
        category_code_key(row) or clean_code(best_code(row)).lower(),
        normalise_for_compare(title_value(row)),
        clean_code(best_code(row)).lower(),
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
        identity = category_code_key(row)
        if not identity:
            continue
        by_identity.setdefault(identity, []).append(row)

    conflict_uids = set()
    conflicts: List[Dict[str, Any]] = []
    for group_index, rows in enumerate(by_identity.values()):
        if len(rows) < 2:
            continue
        code = best_code(rows[0])
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


def preflight_products_payload(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Full frontend payload preflight: dedupe, enrich generic IDs, then group price conflicts."""
    before_count = len(products)
    cleaned = preflight_products_for_frontend(products)
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
        },
    }

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

        set_category_code_identity(row)

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
            new_title = f"{title} - {suffix}" if suffix and normalise_for_compare(suffix) not in normalise_for_compare(title) else title
            new_title = re.sub(r"\s+", " ", new_title).strip()
            set_title(row, new_title)
            base_pid = slugify(new_title)
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

    if ext in {"xlsx", "xls", "xl"}:
        engine = "openpyxl" if ext == "xlsx" else None
        sheets = pd.read_excel(file_storage, sheet_name=None, dtype=object, header=None, engine=engine)
        for sheet_name, raw_df in sheets.items():
            source_sheet = str(sheet_name)

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


@app.post("/process-retail-file-json")
def process_retail_file_json():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded. Use multipart field name 'file'."}), 400

    uploaded_file = request.files["file"]
    try:
        raw_products = parse_uploaded_file(uploaded_file)
        payload = preflight_products_payload(raw_products)
        products = payload["products"]
        issues = build_issues(products)
        summary = dict(payload.get("metadata") or {})
        summary.update({
            "errors": sum(1 for i in issues if i["level"] == "error"),
            "warnings": sum(1 for i in issues if i["level"] == "warning"),
        })
        return jsonify({
            "products": products,
            "price_conflicts": payload.get("price_conflicts", []),
            "issues": issues,
            "summary": summary,
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
        enrich_remaining_duplicate_product_ids(products)
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
