
import os
import json
import re
from datetime import datetime, date
from pathlib import Path
from flask import Flask, jsonify, send_from_directory

PRICE_SPREADSHEET_ID = "1l1qub-I2zuLKLDP2RJFGiDNTIBuGEAxI7PTxIDmfYi4"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DATA_FILE = "search_data.json"
EXCEPTION_FILE = "exceptions.json"
VALID_CATEGORIES = ["식품", "뷰티", "생활", "의류", "패션", "잡화", "신발", "도서", "아동", "가전", "미분류"]

app = Flask(__name__, static_folder=".")


def clean_text(v):
    if v is None:
        return ""
    t = str(v).strip()
    if t.lower() in ["nan", "none", "nat"]:
        return ""
    return t


def to_number(v):
    t = clean_text(v).replace(",", "").replace("원", "")
    try:
        return int(float(t))
    except Exception:
        return 0


def sheet_year(title):
    m = re.search(r"(20\d{2})", str(title))
    return int(m.group(1)) if m else date.today().year


def normalize_date_text(text, default_year=None):
    text = clean_text(text)
    if not text:
        return ""

    m = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d).strftime("%Y-%m-%d")
        except Exception:
            return ""

    m = re.search(r"(20\d{2})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d).strftime("%Y-%m-%d")
        except Exception:
            return ""

    m = re.search(r"(?<!\d)(\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)", text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y + 2000, mo, d).strftime("%Y-%m-%d")
        except Exception:
            return ""

    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m and default_year:
        mo, d = map(int, m.groups())
        try:
            return date(int(default_year), mo, d).strftime("%Y-%m-%d")
        except Exception:
            return ""

    return ""


def detect_inbound_date_row(row, default_year):
    for c in row:
        t = clean_text(c)
        if not t:
            continue
        if re.search(r"\d{1,2}\s*월\s*\d{1,2}\s*일", t):
            return normalize_date_text(t, default_year=default_year)
        if re.search(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", t):
            return normalize_date_text(t, default_year=default_year)
    return ""


def normalize_header(t):
    return clean_text(t).replace("\n", "").replace(" ", "")


def is_header_row(row):
    joined = "|".join([normalize_header(c) for c in row])
    has_product = ("상품명" in joined) or ("품명" in joined)
    has_structure = any(k in joined for k in ["박스번호", "박스", "형태", "대분류", "가격", "금액", "판매가"])
    return has_product and has_structure


def find_col(headers, names):
    h = [normalize_header(x) for x in headers]
    for name in names:
        n = name.replace(" ", "").replace("\n", "")
        for i, v in enumerate(h):
            if n in v:
                return i
    return None


def cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return clean_text(row[idx])


def extract_category(text):
    text = str(text or "")
    for c in VALID_CATEGORIES:
        if c != "미분류" and c in text:
            return c
    return "미분류"


def extract_price_band(text):
    text = clean_text(text)
    if not text:
        return ""
    m = re.search(r"(?<!\d)(\d{1,3})[\.,](0)(?!\d)", text)
    if m:
        return f"{int(m.group(1))}.0"
    amounts = re.findall(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{4,6})(?!\d)", text)
    for a in amounts:
        n = to_number(a)
        if 1000 <= n <= 300000:
            return f"{round(n / 1000, 1):.1f}"
    return ""


def section_by_expiry(expiry):
    if not expiry:
        return "", "기한확인필요"
    try:
        exp = datetime.strptime(expiry, "%Y-%m-%d").date()
    except Exception:
        return "", "기한확인필요"

    days = (exp - date.today()).days
    if days < 0:
        sec = "만료"
    elif days <= 7:
        sec = "7일이내"
    elif days <= 30:
        sec = "30일이내"
    elif days <= 60:
        sec = "60일이내"
    elif days >= 180:
        sec = "장기재고"
    else:
        sec = "전체"
    return days, sec



def load_exceptions():
    path = Path(EXCEPTION_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_exceptions(data):
    with open(EXCEPTION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_exception_mode(product_name, exceptions):
    name = clean_text(product_name)
    if not name:
        return ""
    for key in sorted(exceptions.keys(), key=len, reverse=True):
        key_clean = clean_text(key)
        if key_clean and key_clean in name:
            return exceptions.get(key, {}).get("mode", "")
    return ""


def parse_sheet(values, title, exceptions=None):
    y = sheet_year(title)
    exceptions = exceptions or {}
    rows = []
    current_inbound = ""
    headers = None
    col = {}

    for r_idx, row in enumerate(values, start=1):
        if not any(clean_text(c) for c in row):
            continue

        if is_header_row(row):
            headers = row
            col = {
                "box": find_col(row, ["박스번호", "박스"]),
                "type": find_col(row, ["형태", "구분"]),
                "category": find_col(row, ["대분류", "분류", "카테고리"]),
                "product": find_col(row, ["상품명", "품명"]),
                "barcode": find_col(row, ["바코드"]),
                "price": find_col(row, ["가격", "금액", "판매가", "단가"]),
                "note": find_col(row, ["비고", "메모", "참고"]),
                "store": find_col(row, ["매장"]),
                "expiry": find_col(row, ["유통기한", "소비기한", "기한"]),
                "inbound": find_col(row, ["입고일", "입고일자"]),
            }
            continue

        date_row = detect_inbound_date_row(row, y)
        if date_row:
            current_inbound = date_row
            continue

        if not headers or not col:
            continue

        product = cell(row, col.get("product"))
        if not product:
            continue

        if "가격 이외의 사항은" in product or "바코드 사용" in product or "문의 부탁" in product:
            continue

        category_raw = cell(row, col.get("category"))
        category = category_raw if category_raw in VALID_CATEGORIES else extract_category(product)
        item_type = cell(row, col.get("type"))
        box_no = cell(row, col.get("box"))
        barcode = cell(row, col.get("barcode"))
        price_raw = cell(row, col.get("price"))
        note = cell(row, col.get("note"))
        store = cell(row, col.get("store"))

        inbound = normalize_date_text(cell(row, col.get("inbound")), y) or current_inbound
        expiry = normalize_date_text(cell(row, col.get("expiry")), y)
        if not expiry:
            expiry = normalize_date_text(f"{product} {note}", y)

        price = to_number(price_raw)
        band = extract_price_band(price_raw) or extract_price_band(product) or extract_price_band(note)
        if not band and price:
            band = f"{round(price / 1000, 1):.1f}"

        days, sec = section_by_expiry(expiry)

        if category == "식품":
            manage = "유통기한관리" if expiry else "기한확인필요"
        else:
            manage = "유통기한관리" if expiry else "비관리대상"

        exception_mode = get_exception_mode(product, exceptions)
        # 예외상품 설정 적용
        override_expiry = ""
        if exception_mode:
            for key in sorted(exceptions.keys(), key=len, reverse=True):
                if clean_text(key) and clean_text(key) in product:
                    override_expiry = clean_text(exceptions.get(key, {}).get("expiryDate", ""))
                    break

        if override_expiry:
            expiry = override_expiry
            days, sec = section_by_expiry(expiry)
            manage = "유통기한관리"
            exception_mode = "expiry_override"
        elif exception_mode == "manufacture":
            manage = "기한확인필요"
            sec = "기한확인필요"
            days = ""
        elif exception_mode == "exclude":
            manage = "비관리대상"
            sec = "기한관리제외"
            days = ""
        elif exception_mode == "expiry":
            pass

        search = f"{title} {inbound} {category_raw} {category} {product} {price} {band} {expiry} {note} {barcode} {box_no} {store} {item_type} {exception_mode}".lower()

        rows.append({
            "manage": manage,
            "section": sec,
            "category": category,
            "majorCategory": category_raw,
            "product": product,
            "price": price,
            "priceBand": band,
            "inboundDate": inbound,
            "expiryDate": expiry,
            "daysLeft": days,
            "note": note,
            "barcode": barcode,
            "boxNo": box_no,
            "type": item_type,
            "store": store,
            "sheet": title,
            "row": r_idx,
            "search": search,
            "exceptionMode": exception_mode,
        })

    return rows


def get_credentials():
    import gspread
    from google.oauth2.service_account import Credentials

    json_text = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if json_text:
        info = json.loads(json_text)
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(credentials)

    # 로컬 테스트용
    local_path = Path(r"C:\GPAM\gpam.json")
    if local_path.exists():
        credentials = Credentials.from_service_account_file(str(local_path), scopes=SCOPES)
        return gspread.authorize(credentials)

    raise FileNotFoundError("GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 없고, C:\\GPAM\\gpam.json도 없습니다.")


def sync_data():
    client = get_credentials()
    spreadsheet = client.open_by_key(PRICE_SPREADSHEET_ID)

    all_rows = []
    loaded = []
    failed = []
    exceptions = load_exceptions()

    for ws in spreadsheet.worksheets():
        title = ws.title
        if not ("★2026년★" in title or "★2025년★" in title):
            continue

        try:
            parsed = parse_sheet(ws.get_all_values(), title, exceptions)
            if parsed:
                all_rows.extend(parsed)
                loaded.append(title)
            else:
                failed.append(title)
        except Exception as e:
            failed.append(f"{title}: {e}")

    all_rows.sort(key=lambda x: (x.get("inboundDate") or "", x.get("sheet") or "", x.get("row") or 0), reverse=True)

    output = {
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(all_rows),
        "loadedSheets": loaded,
        "failedSheets": failed,
        "items": all_rows,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    return output


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/search_data.json")
def data():
    if not Path(DATA_FILE).exists():
        sync_data()
    return send_from_directory(".", DATA_FILE)


@app.route("/sync")
def sync():
    try:
        result = sync_data()
        return jsonify({
            "ok": True,
            "message": "동기화 완료",
            "data": {
                "updatedAt": result["updatedAt"],
                "count": result["count"],
                "loadedSheets": result["loadedSheets"],
                "failedSheets": result["failedSheets"],
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})



@app.route("/exceptions")
def get_exceptions():
    return jsonify(load_exceptions())


@app.route("/set_exception/<path:product>/<mode>")
def set_exception(product, mode):
    if mode not in ["manufacture", "exclude", "expiry", "clear"]:
        return jsonify({"ok": False, "message": "잘못된 mode입니다."})

    product = clean_text(product)
    if not product:
        return jsonify({"ok": False, "message": "상품명이 없습니다."})

    data = load_exceptions()
    if mode == "clear":
        data.pop(product, None)
    else:
        data[product] = {
            "mode": mode,
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    save_exceptions(data)

    try:
        sync_data()
    except Exception:
        pass

    return jsonify({"ok": True, "product": product, "mode": mode})



@app.route("/set_expiry/<path:product>/<expiry_date>")
def set_expiry(product, expiry_date):
    product = clean_text(product)
    expiry_date = clean_text(expiry_date)

    if not product:
        return jsonify({"ok": False, "message": "상품명이 없습니다."})

    try:
        datetime.strptime(expiry_date, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "message": "유통기한 형식이 올바르지 않습니다. YYYY-MM-DD 형식이어야 합니다."})

    data = load_exceptions()
    data[product] = {
        "mode": "expiry",
        "expiryDate": expiry_date,
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_exceptions(data)

    try:
        sync_data()
    except Exception:
        pass

    return jsonify({"ok": True, "product": product, "expiryDate": expiry_date})


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
