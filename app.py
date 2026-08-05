from flask import Flask, request, jsonify
import json
import os
import re
from datetime import datetime, timezone

app = Flask(__name__)

KEY_FILE = "keys.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _normalize(text):
    if text is None:
        return ""
    return str(text).strip().lower()


def _aliases(value):
    """Build a small set of normalized aliases for package/type matching."""
    v = _normalize(value)
    if not v:
        return set()

    aliases = {v}
    compact = re.sub(r"[\s_\-]+", "", v)
    aliases.add(compact)

    # Common Vietnamese duration aliases
    mapping = {
        "3 ngay": {"3 ngày", "3 ngay", "3d", "3 day", "3 days"},
        "1 tuan": {"1 tuần", "1 tuan", "7 ngày", "7 ngay", "1 week", "7d"},
        "2 tuan": {"2 tuần", "2 tuan", "14 ngày", "14 ngay", "2 weeks", "14d"},
        "1 thang": {"1 tháng", "1 thang", "30 ngày", "30 ngay", "1 month", "30d"},
        "2 thang": {"2 tháng", "2 thang", "60 ngày", "60 ngay", "2 months", "60d"},
        "4 thang": {"4 tháng", "4 thang", "120 ngày", "120 ngay", "4 months", "120d"},
        "6 thang": {"6 tháng", "6 thang", "180 ngày", "180 ngay", "6 months", "180d"},
        "vip": {"vip", "vĩnh viễn", "vinh vien", "forever", "permanent", "lifelong", "vĩnh viễn vip"},
        "free": {"free", "miễn phí", "mien phi"},
    }

    for key, vals in mapping.items():
        if v == key or compact == key.replace(" ", ""):
            aliases.update({_normalize(x) for x in vals})
            break

    return aliases


def _coerce_int(value):
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except Exception:
        return None


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = _normalize(value)
    return text in {"1", "true", "yes", "y", "on", "forever", "vip", "vĩnh viễn", "vinh vien"}

def load_keys():
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    with open(KEY_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_keys(data):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _request_data():
    data = {}
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict() or {}
        if not data:
            data = request.args.to_dict() or {}
    return data


def _get_field(data, *names):
    for name in names:
        if name in data and data.get(name) not in (None, ""):
            return data.get(name)
    return None


def _matches_requested_package(info, requested):
    if not requested:
        return True

    requested_aliases = _aliases(requested)
    package_fields = [
        info.get("package"),
        info.get("duration"),
        info.get("type"),
        info.get("name"),
        info.get("plan"),
        info.get("key_type"),
        info.get("tier"),
    ]

    for field in package_fields:
        if field is None:
            continue
        field_aliases = _aliases(field)
        if requested_aliases & field_aliases:
            return True

    # fallback: loose substring check
    req = _normalize(requested)
    for field in package_fields:
        s = _normalize(field)
        if s and (req in s or s in req):
            return True
    return False


def _find_available_key(keys, requested_package=None, requested_duration_hours=None, requested_is_forever=False):
    # Ưu tiên khớp chính xác theo thời hạn. Đây là cách đảm bảo user mua gói nào thì
    # server chỉ phát đúng key của gói đó, không rơi sang key khác chỉ vì cùng loại VIP.
    if requested_duration_hours is not None:
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue

            if info.get("used", False) or info.get("issued", False):
                continue

            duration = _coerce_int(info.get("duration"))
            if duration is None:
                continue

            if duration == requested_duration_hours:
                return key, info

        # Có duration_hours nhưng không có key khớp => không fallback sang tên gói.
        return None, None

    if requested_is_forever:
        # Fallback cho client cũ: tìm key VIP vĩnh viễn nếu có.
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue

            if info.get("used", False) or info.get("issued", False):
                continue

            duration = _coerce_int(info.get("duration"))
            key_type = _normalize(info.get("type"))
            if key_type == "vip" and duration is not None and duration >= 100000:
                return key, info

        return None, None

    for key, info in keys.items():
        if not isinstance(info, dict):
            continue

        if info.get("used", False):
            continue

        if info.get("issued", False):
            continue

        if not _matches_requested_package(info, requested_package):
            continue

        return key, info

    return None, None


@app.route("/")
def home():
    return "VB TOOL KEY SERVER ONLINE"


@app.route("/api/issue_key", methods=["GET", "POST"])
@app.route("/api/issue-key", methods=["GET", "POST"])
@app.route("/issue_key", methods=["GET", "POST"])
def issue_key():
    data = _request_data()

    requested_package = _get_field(
        data,
        "package", "duration", "type", "plan", "key_type", "tier"
    )
    requested_duration_hours = _coerce_int(
        _get_field(data, "duration_hours", "requested_duration_hours", "durationHours", "hours", "expire_hours")
    )
    requested_is_forever = _coerce_bool(
        _get_field(data, "is_forever", "forever", "permanent")
    )

    order_id = _get_field(data, "order_id", "orderId", "id", "request_id", "requestId")
    user_id = _get_field(data, "user_id", "userId", "telegram_id", "chat_id", "chatId")
    username = _get_field(data, "username", "user_name", "name")
    note = _get_field(data, "note", "message", "desc", "description")

    keys = load_keys()
    key, info = _find_available_key(keys, requested_package, requested_duration_hours, requested_is_forever)

    if not key:
        return jsonify({
            "success": False,
            "message": "Không còn key phù hợp trong server",
            "requested_package": requested_package,
        }), 200

    info["issued"] = True
    info["issued_at"] = _now_iso()
    if order_id is not None:
        info["order_id"] = str(order_id)
    if user_id is not None:
        info["user_id"] = str(user_id)
    if username is not None:
        info["username"] = str(username)
    if note is not None:
        info["note"] = str(note)

    save_keys(keys)

    return jsonify({
        "success": True,
        "message": "Đã lấy key thành công",
        "key": key,
        "duration": info.get("duration"),
        "key_type": info.get("type"),
        "package": info.get("package") or info.get("duration") or info.get("type"),
        "issued_at": info.get("issued_at"),
        "order_id": info.get("order_id"),
    })


@app.route("/api/verify_key", methods=["POST"])
def verify():
    data = _request_data()

    key = _get_field(data, "key", "license", "license_key")
    device = _get_field(data, "device_id", "device", "hwid")

    if not key:
        return jsonify({
            "success": False,
            "message": "Thiếu key"
        })

    keys = load_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "message": "Key không tồn tại"
        })

    info = keys[key]
    if not isinstance(info, dict):
        return jsonify({
            "success": False,
            "message": "Dữ liệu key không hợp lệ"
        })

    if info.get("used", False):
        return jsonify({
            "success": False,
            "message": "Key đã được sử dụng"
        })

    info["used"] = True
    info["used_at"] = _now_iso()
    info["device"] = device
    save_keys(keys)

    return jsonify({
        "success": True,
        "duration": info.get("duration"),
        "key_type": info.get("type"),
        "key": key
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"success": True, "message": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
