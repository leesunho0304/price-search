
import os
import json
import re
import io
from datetime import datetime, date
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, send_file, request

PRICE_SPREADSHEET_ID = "1l1qub-I2zuLKLDP2RJFGiDNTIBuGEAxI7PTxIDmfYi4"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DATA_FILE = "search_data.json"
EXCEPTION_FILE = "exceptions.json"
AUTO_SYNC_MINUTES = 10
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



def extract_price_amounts(text):
    """
    가격 셀 안의 금액 후보를 추출한다.
    사이즈 150, 160 같은 숫자는 제외하고 1,000원 이상 금액만 사용한다.
    """
    text = clean_text(text)
    if not text:
        return []

    found = re.findall(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{4,6})(?!\d)", text)
    amounts = []
    for raw in found:
        try:
            value = int(raw.replace(",", ""))
        except Exception:
            continue
        if 1000 <= value <= 999999:
            amounts.append(value)

    # 원문 순서를 유지하며 중복 제거
    unique = []
    for value in amounts:
        if value not in unique:
            unique.append(value)
    return unique


def build_price_info(price_raw, note=""):
    """
    가격을 숫자 하나로 억지 변환하지 않고 원문과 표시 방식을 함께 반환한다.

    반환:
    - price: 단일가격일 때만 숫자, 그 외 0
    - price_type: single / range / discretion / multiple / attached / text / missing
    - price_display: 카드에 표시할 짧은 문구
    - price_raw: 시트의 가격 원문
    - price_amounts: 원문에서 추출한 금액 목록
    - show_detail: 가격 상세 펼침 표시 여부
    """
    raw = clean_text(price_raw)
    note_text = clean_text(note)
    combined = f"{raw}\n{note_text}".lower()
    amounts = extract_price_amounts(raw)

    if not raw:
        return {
            "price": 0,
            "price_type": "missing",
            "price_display": "가격 정보 없음",
            "price_raw": "",
            "price_amounts": [],
            "show_detail": False,
        }

    # 숫자 하나만 적힌 단일 가격
    numeric_text = raw.replace(",", "").replace("원", "").strip()
    if re.fullmatch(r"\d+(?:\.0+)?", numeric_text):
        try:
            value = int(float(numeric_text))
        except Exception:
            value = 0
        return {
            "price": value,
            "price_type": "single",
            "price_display": f"{value:,}원" if value else raw,
            "price_raw": raw,
            "price_amounts": [value] if value else [],
            "show_detail": False,
        }

    # 상품에 부착된 가격표를 확인해야 하는 경우
    if "부착된 가격" in raw or "부착 가격" in raw:
        return {
            "price": 0,
            "price_type": "attached",
            "price_display": "상품 부착가 확인",
            "price_raw": raw,
            "price_amounts": amounts,
            "show_detail": False,
        }

    # 매장/권역 재량 가격
    if "재량" in combined:
        if len(amounts) >= 2:
            price_display = f"매장·권역 재량 · {min(amounts):,}~{max(amounts):,}원"
        elif len(amounts) == 1:
            price_display = f"매장·권역 재량 · {amounts[0]:,}원"
        else:
            price_display = "매장·권역 재량 가격"
        return {
            "price": 0,
            "price_type": "discretion",
            "price_display": price_display,
            "price_raw": raw,
            "price_amounts": amounts,
            "show_detail": True,
        }

    # 줄바꿈과 상품별 설명이 있는 복합 가격
    category_words = [
        "슬리퍼", "샌들", "운동화", "구두", "워커", "로퍼", "가방",
        "의류", "잡화", "아동", "성인", "티셔츠", "바지", "셔츠",
        "블라우스", "원피스", "자켓", "점퍼", "사이즈", "size"
    ]
    has_category_lines = "\n" in raw and any(word in raw.lower() for word in category_words)

    if has_category_lines or ("\n" in raw and len(amounts) >= 2):
        return {
            "price": 0,
            "price_type": "multiple",
            "price_display": "종류별 가격 · 상세보기",
            "price_raw": raw,
            "price_amounts": amounts,
            "show_detail": True,
        }

    # 1,000 ~ 3,000 형태의 범위 가격
    if len(amounts) >= 2 and any(mark in raw for mark in ["~", "～", "-", "–", "—"]):
        return {
            "price": 0,
            "price_type": "range",
            "price_display": f"{min(amounts):,}~{max(amounts):,}원",
            "price_raw": raw,
            "price_amounts": amounts,
            "show_detail": True,
        }

    # 3,000 / 5,000 / 7,000 형태
    if len(amounts) >= 2:
        if len(amounts) <= 4:
            display = " / ".join(f"{value:,}" for value in amounts) + "원"
        else:
            display = f"복수가격 · {min(amounts):,}~{max(amounts):,}원"
        return {
            "price": 0,
            "price_type": "multiple",
            "price_display": display,
            "price_raw": raw,
            "price_amounts": amounts,
            "show_detail": True,
        }

    # 숫자 추출은 안 되지만 유효한 가격 안내 문구가 있는 경우
    first_line = raw.splitlines()[0].strip()
    short_text = first_line if len(first_line) <= 30 else first_line[:30] + "…"
    return {
        "price": 0,
        "price_type": "text",
        "price_display": short_text or "가격 상세 확인",
        "price_raw": raw,
        "price_amounts": amounts,
        "show_detail": len(raw.splitlines()) > 1 or len(raw) > 30,
    }

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
    elif days <= 90:
        sec = "90일이내"
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



def make_item_key(store, inbound, box_no, product, sheet, row):
    """
    같은 상품이라도 매장/입고일/박스/행이 다르면 다른 물건으로 봄.
    매장명이 없으면 시트+행까지 포함해 충돌을 줄임.
    """
    parts = [
        clean_text(store) or "전체",
        clean_text(inbound),
        clean_text(box_no),
        clean_text(product),
        clean_text(sheet),
        str(row or "")
    ]
    return "|".join(parts)


def find_item_override(item_key, product_name, exceptions):
    """
    1순위: item_key 정확히 일치
    2순위: 기존 상품명 예외 부분일치
    """
    if item_key and item_key in exceptions:
        return item_key, exceptions[item_key]

    name = clean_text(product_name)
    for key in sorted(exceptions.keys(), key=len, reverse=True):
        key_clean = clean_text(key)
        if key_clean and key_clean in name:
            return key, exceptions.get(key, {})

    return "", {}


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

        price_info = build_price_info(price_raw, note)
        price = price_info["price"]
        price_type = price_info["price_type"]
        price_display = price_info["price_display"]
        price_amounts = price_info["price_amounts"]
        show_price_detail = price_info["show_detail"]

        band = extract_price_band(price_raw) or extract_price_band(product) or extract_price_band(note)
        if not band and price:
            band = f"{round(price / 1000, 1):.1f}"

        days, sec = section_by_expiry(expiry)

        if category == "식품":
            manage = "유통기한관리" if expiry else "기한확인필요"
        else:
            manage = "유통기한관리" if expiry else "비관리대상"

        exception_mode = get_exception_mode(product, exceptions)
        # 예외/수정 설정 적용
        item_key = make_item_key(store, inbound, box_no, product, title, r_idx)
        matched_key, override = find_item_override(item_key, product, exceptions)
        exception_mode = clean_text(override.get("mode", "")) if override else ""

        override_expiry = clean_text(override.get("expiryDate", "")) if override else ""
        override_note = clean_text(override.get("memo", "")) if override else ""

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

        search = f"{title} {inbound} {category_raw} {category} {product} {price} {band} {price_raw} {price_display} {expiry} {note} {barcode} {box_no} {store} {item_type} {exception_mode} {override_note}".lower()

        rows.append({
            "manage": manage,
            "section": sec,
            "category": category,
            "majorCategory": category_raw,
            "product": product,
            "price": price,
            "priceBand": band,
            "priceRaw": price_raw,
            "priceType": price_type,
            "priceDisplay": price_display,
            "priceAmounts": price_amounts,
            "showPriceDetail": show_price_detail,
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
            "itemKey": item_key,
            "overrideKey": matched_key,
            "overrideMemo": override_note,
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
    should_sync = not Path(DATA_FILE).exists()

    if not should_sync:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                current = json.load(f)
            updated_at = current.get("updatedAt", "")
            dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
            age_minutes = (datetime.now() - dt).total_seconds() / 60
            should_sync = age_minutes >= AUTO_SYNC_MINUTES
        except Exception:
            should_sync = True

    if should_sync:
        try:
            sync_data()
        except Exception:
            pass

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



@app.route("/set_item_expiry/<path:item_key>/<expiry_date>")
def set_item_expiry(item_key, expiry_date):
    item_key = clean_text(item_key)
    expiry_date = clean_text(expiry_date)

    if not item_key:
        return jsonify({"ok": False, "message": "item_key가 없습니다."})

    try:
        datetime.strptime(expiry_date, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "message": "유통기한 형식이 올바르지 않습니다. YYYY-MM-DD 형식이어야 합니다."})

    data = load_exceptions()
    existing = data.get(item_key, {})
    data[item_key] = {
        **existing,
        "mode": "expiry",
        "expiryDate": expiry_date,
        "scope": "item",
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_exceptions(data)

    try:
        sync_data()
    except Exception:
        pass

    return jsonify({"ok": True, "itemKey": item_key, "expiryDate": expiry_date})


@app.route("/set_item_exception/<path:item_key>/<mode>")
def set_item_exception(item_key, mode):
    if mode not in ["manufacture", "exclude", "expiry", "clear"]:
        return jsonify({"ok": False, "message": "잘못된 mode입니다."})

    item_key = clean_text(item_key)
    if not item_key:
        return jsonify({"ok": False, "message": "item_key가 없습니다."})

    data = load_exceptions()
    if mode == "clear":
        data.pop(item_key, None)
    else:
        existing = data.get(item_key, {})
        data[item_key] = {
            **existing,
            "mode": mode,
            "scope": "item",
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    save_exceptions(data)

    try:
        sync_data()
    except Exception:
        pass

    return jsonify({"ok": True, "itemKey": item_key, "mode": mode})


@app.route("/status")
def status():
    need_sync = True
    updated_at = ""
    count = 0

    if Path(DATA_FILE).exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            updated_at = data.get("updatedAt", "")
            count = data.get("count", 0)
            dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
            age_minutes = (datetime.now() - dt).total_seconds() / 60
            need_sync = age_minutes >= AUTO_SYNC_MINUTES
        except Exception:
            need_sync = True

    return jsonify({
        "ok": True,
        "needSync": need_sync,
        "updatedAt": updated_at,
        "count": count,
        "autoSyncMinutes": AUTO_SYNC_MINUTES
    })



@app.route("/set_store_item_expiry/<path:item_key>/<store_name>/<expiry_date>")
def set_store_item_expiry(item_key, store_name, expiry_date):
    item_key = clean_text(item_key)
    store_name = clean_text(store_name)
    expiry_date = clean_text(expiry_date)

    if not item_key:
        return jsonify({"ok": False, "message": "item_key가 없습니다."})
    if not store_name:
        return jsonify({"ok": False, "message": "매장명이 없습니다."})

    try:
        datetime.strptime(expiry_date, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "message": "유통기한 형식이 올바르지 않습니다. YYYY-MM-DD 형식이어야 합니다."})

    # 같은 원본 행이라도 매장별로 다른 유통기한을 저장할 수 있게 매장명을 앞에 붙임
    store_item_key = f"STORE|{store_name}|{item_key}"

    data = load_exceptions()
    existing = data.get(store_item_key, {})
    data[store_item_key] = {
        **existing,
        "mode": "expiry",
        "expiryDate": expiry_date,
        "scope": "store_item",
        "store": store_name,
        "baseItemKey": item_key,
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_exceptions(data)

    try:
        sync_data()
    except Exception:
        pass

    return jsonify({"ok": True, "itemKey": store_item_key, "store": store_name, "expiryDate": expiry_date})


@app.route("/store_overrides/<store_name>")
def get_store_overrides(store_name):
    store_name = clean_text(store_name)
    data = load_exceptions()
    prefix = f"STORE|{store_name}|"
    result = {}
    for key, value in data.items():
        if key.startswith(prefix):
            base_key = key[len(prefix):]
            result[base_key] = value
    return jsonify({"ok": True, "store": store_name, "overrides": result})



def apply_store_override_to_export(item, store_name, exceptions):
    """엑셀 다운로드 시 선택 매장의 수정 유통기한을 반영한다."""
    row = dict(item)
    store_name = clean_text(store_name)
    if not store_name:
        return row

    override_key = f"STORE|{store_name}|{row.get('itemKey', '')}"
    override = exceptions.get(override_key, {})
    expiry_date = clean_text(override.get("expiryDate", ""))

    if expiry_date:
        row["expiryDate"] = expiry_date
        days, section = section_by_expiry(expiry_date)
        row["daysLeft"] = days
        row["section"] = section
        row["manage"] = "유통기한관리"
        row["storeOverride"] = True

    mode = clean_text(override.get("mode", ""))
    if mode == "manufacture" and not expiry_date:
        row["manage"] = "기한확인필요"
        row["section"] = "기한확인필요"
        row["daysLeft"] = ""
        row["storeOverride"] = True
    elif mode == "exclude" and not expiry_date:
        row["manage"] = "비관리대상"
        row["section"] = "기한관리제외"
        row["daysLeft"] = ""
        row["storeOverride"] = True

    return row


def export_menu_match(item, menu):
    if not menu or menu == "전체상품":
        return True
    if menu == "유통기한관리":
        return item.get("manage") == "유통기한관리"
    if menu == "기한확인필요":
        return item.get("manage") == "기한확인필요"
    if menu == "비관리대상":
        return item.get("manage") == "비관리대상"
    return item.get("section") == menu


@app.route("/download_excel")
def download_excel():
    """
    현재 화면의 검색어·날짜·분류·선택 매장을 반영한 취합 리스트를 엑셀로 내려준다.
    필터가 없으면 전체 가격리스트가 다운로드된다.
    """
    try:
        if not Path(DATA_FILE).exists():
            sync_data()

        with open(DATA_FILE, "r", encoding="utf-8") as f:
            source = json.load(f)

        items = source.get("items", [])
        exceptions = load_exceptions()

        search_mode = clean_text(request.args.get("mode", "product"))
        query = clean_text(request.args.get("q", ""))
        date_start = clean_text(request.args.get("start", ""))
        date_end = clean_text(request.args.get("end", ""))
        menu = clean_text(request.args.get("menu", "전체상품"))
        store_name = clean_text(request.args.get("store", ""))

        filtered = []
        tokens = [x for x in query.lower().split() if x]

        for original in items:
            item = apply_store_override_to_export(original, store_name, exceptions)

            inbound = clean_text(item.get("inboundDate", ""))
            if date_start and inbound < date_start:
                continue
            if date_end and inbound > date_end:
                continue
            if not export_menu_match(item, menu):
                continue

            if query:
                if search_mode == "box":
                    if clean_text(item.get("boxNo", "")) != query:
                        continue
                else:
                    search_text = clean_text(item.get("search", "")).lower()
                    if not all(token in search_text for token in tokens):
                        continue

            filtered.append(item)

        import xlsxwriter

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("취합리스트")

        title_fmt = workbook.add_format({
            "bold": True,
            "font_size": 16,
            "font_color": "#FFFFFF",
            "bg_color": "#147D73",
            "align": "center",
            "valign": "vcenter",
        })
        info_fmt = workbook.add_format({
            "font_color": "#334155",
            "bg_color": "#F1F5F9",
            "align": "left",
            "valign": "vcenter",
        })
        header_fmt = workbook.add_format({
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#111827",
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
        })
        text_fmt = workbook.add_format({
            "border": 1,
            "valign": "top",
            "text_wrap": True,
        })
        center_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "top",
            "text_wrap": True,
        })
        discount_input_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "top",
            "text_wrap": True,
        })
        expiry_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "top",
            "bg_color": "#FFF2CC",
            "font_color": "#C65911",
            "num_format": "yyyy-mm-dd",
        })
        days_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "top",
            "bg_color": "#FFF2CC",
            "font_color": "#C65911",
        })

        headers = [
            "형태",
            "대분류",
            "상품명",
            "가격 원문",
            "인하 가격",
            "유통기한",
            "남은 일수",
        ]

        sheet.merge_range(
            0, 0, 0, len(headers) - 1,
            "가격인하 취합",
            title_fmt
        )

        store_label = store_name or "전체"
        menu_label = menu if menu and menu != "전체상품" else "전체"
        info_text = (
            f"매장: {store_label} | "
            f"분류: {menu_label} | "
            f"총 {len(filtered):,}건 | "
            f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        sheet.merge_range(
            1, 0, 1, len(headers) - 1,
            info_text,
            info_fmt
        )

        for col, header in enumerate(headers):
            sheet.write(2, col, header, header_fmt)

        for row_idx, item in enumerate(filtered, start=3):
            price_raw = clean_text(item.get("priceRaw", ""))
            if not price_raw:
                price_raw = clean_text(item.get("priceDisplay", ""))

            values = [
                clean_text(item.get("type", "")),
                clean_text(item.get("category", "")),
                clean_text(item.get("product", "")),
                price_raw,
                "",  # 인하 가격은 매장에서 직접 입력
                clean_text(item.get("expiryDate", "")),
                item.get("daysLeft", ""),
            ]

            sheet.write(row_idx, 0, values[0], center_fmt)
            sheet.write(row_idx, 1, values[1], center_fmt)
            sheet.write(row_idx, 2, values[2], text_fmt)
            sheet.write(row_idx, 3, values[3], text_fmt)
            sheet.write_blank(row_idx, 4, None, discount_input_fmt)
            sheet.write(row_idx, 5, values[5], expiry_fmt)
            sheet.write(row_idx, 6, values[6], days_fmt)

            # 복합 가격·긴 상품명이 잘리지 않도록 행 높이 조절
            line_count = max(
                1,
                len(str(values[2]).splitlines()),
                len(str(values[3]).splitlines()),
            )
            estimated_lines = max(
                line_count,
                (len(str(values[2])) // 24) + 1,
                (len(str(values[3])) // 32) + 1,
            )
            sheet.set_row(row_idx, max(28, min(90, 18 * estimated_lines)))

        widths = [11, 12, 34, 44, 17, 14, 12]
        for idx, width in enumerate(widths):
            sheet.set_column(idx, idx, width)

        sheet.set_row(0, 30)
        sheet.set_row(1, 24)
        sheet.set_row(2, 30)
        sheet.freeze_panes(3, 0)
        sheet.autofilter(2, 0, max(2, len(filtered) + 2), len(headers) - 1)

        # 인쇄 설정: A4 가로, 한 페이지 너비
        sheet.set_landscape()
        sheet.set_paper(9)
        sheet.fit_to_pages(1, 0)
        sheet.set_margins(left=0.25, right=0.25, top=0.4, bottom=0.4)
        sheet.repeat_rows(0, 2)
        sheet.hide_gridlines(2)

        workbook.close()
        output.seek(0)

        filename = f"가격인하_취합_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
