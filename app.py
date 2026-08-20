from flask import Flask, request, jsonify
import json
import os
import re
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

KEY_FILE = "keys.json"
REVOKED_FILE = "revoked_keys.json"


# =========================================================
# TIME / NORMALIZATION HELPERS
# =========================================================

def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _parse_iso(value):
    if not value:
        return None

    try:
        text = str(value).strip()

        # Support timestamps ending with Z.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)
    except Exception:
        return None


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

    mapping = {
        "3 ngay": {
            "3 ngày", "3 ngay", "3d", "3 day", "3 days"
        },
        "1 tuan": {
            "1 tuần", "1 tuan", "7 ngày", "7 ngay",
            "1 week", "7d"
        },
        "2 tuan": {
            "2 tuần", "2 tuan", "14 ngày", "14 ngay",
            "2 weeks", "14d"
        },
        "1 thang": {
            "1 tháng", "1 thang", "30 ngày", "30 ngay",
            "1 month", "30d"
        },
        "2 thang": {
            "2 tháng", "2 thang", "60 ngày", "60 ngay",
            "2 months", "60d"
        },
        "4 thang": {
            "4 tháng", "4 thang", "120 ngày", "120 ngay",
            "4 months", "120d"
        },
        "6 thang": {
            "6 tháng", "6 thang", "180 ngày", "180 ngay",
            "6 months", "180d"
        },
        "vip": {
            "vip", "vĩnh viễn", "vinh vien", "forever",
            "permanent", "lifelong", "vĩnh viễn vip"
        },
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

    return text in {
        "1", "true", "yes", "y", "on",
        "forever", "vip",
        "vĩnh viễn", "vinh vien"
    }


# =========================================================
# FILE STORAGE
# =========================================================

def load_keys():
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_keys(data):
    tmp_file = KEY_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    os.replace(tmp_file, KEY_FILE)


def load_revoked_keys():
    if not os.path.exists(REVOKED_FILE):
        with open(REVOKED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    try:
        with open(REVOKED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_revoked_keys(data):
    tmp_file = REVOKED_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    os.replace(tmp_file, REVOKED_FILE)


# =========================================================
# REQUEST HELPERS
# =========================================================

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


# =========================================================
# PACKAGE MATCHING
# =========================================================

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

    req = _normalize(requested)

    for field in package_fields:
        s = _normalize(field)

        if s and (req in s or s in req):
            return True

    return False


# =========================================================
# KEY TYPE / EXPIRATION
# =========================================================

def _is_forever_key(info):
    if not isinstance(info, dict):
        return False

    if _coerce_bool(info.get("is_forever")):
        return True

    key_type = _normalize(info.get("type"))
    if key_type in {"forever", "permanent", "lifelong"}:
        return True

    duration = _coerce_int(info.get("duration"))

    # Preserve the old server's convention for very large durations.
    if key_type == "vip" and duration is not None and duration >= 100000:
        return True

    return False


def _calculate_expiry(info, activated_at):
    """
    duration is treated as HOURS, matching the existing server/client design.

    Permanent keys have expires_at = None.
    """
    if _is_forever_key(info):
        return None

    duration_hours = _coerce_int(info.get("duration"))

    if duration_hours is None or duration_hours <= 0:
        return None

    return activated_at + timedelta(hours=duration_hours)


def _get_expiry(info):
    expires_at = _parse_iso(info.get("expires_at"))

    if expires_at is not None:
        return expires_at

    # Backward compatibility:
    # If an older key was already activated but has no expires_at,
    # calculate it from activated_at / used_at.
    activated_at = _parse_iso(
        info.get("activated_at") or info.get("used_at")
    )

    if activated_at is None:
        return None

    calculated = _calculate_expiry(info, activated_at)

    if calculated is not None:
        info["expires_at"] = calculated.isoformat()

    return calculated


def _is_expired(info, now=None):
    if not isinstance(info, dict):
        return False

    if _is_forever_key(info):
        return False

    now = now or _now()

    expires_at = _get_expiry(info)

    # Invalid/missing duration for a non-forever activated key:
    # treat it as expired instead of accidentally allowing unlimited use.
    if expires_at is None:
        activated_at = _parse_iso(
            info.get("activated_at") or info.get("used_at")
        )
        duration = _coerce_int(info.get("duration"))

        if activated_at is not None and (duration is None or duration <= 0):
            return True

        # If it is not activated yet, it is not expired.
        if activated_at is None:
            return False

        return False

    return now >= expires_at


def _is_key_revoked(key, info, revoked_keys):
    if key in revoked_keys:
        return True

    if isinstance(info, dict):
        # revoked=True is reserved for permanent manual/system revocation.
        if info.get("revoked", False):
            return True

    return False


# =========================================================
# KEY SEARCH / ISSUANCE
# =========================================================

def _find_available_key(
    keys,
    requested_package=None,
    requested_duration_hours=None,
    requested_is_forever=False,
    revoked_keys=None,
):
    revoked_keys = revoked_keys if revoked_keys is not None else load_revoked_keys()

    # Exact duration matching.
    if requested_duration_hours is not None:
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue

            if (
                info.get("issued", False)
                or info.get("used", False)
                or info.get("revoked", False)
                or _is_key_revoked(key, info, revoked_keys)
            ):
                continue

            duration = _coerce_int(info.get("duration"))

            if duration is None:
                continue

            if duration == requested_duration_hours:
                return key, info

        return None, None

    # Explicit forever request.
    if requested_is_forever:
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue

            if (
                info.get("issued", False)
                or info.get("used", False)
                or info.get("revoked", False)
                or _is_key_revoked(key, info, revoked_keys)
            ):
                continue

            if _is_forever_key(info):
                return key, info

        return None, None

    # Package/type matching.
    for key, info in keys.items():
        if not isinstance(info, dict):
            continue

        if (
            info.get("issued", False)
            or info.get("used", False)
            or info.get("revoked", False)
            or _is_key_revoked(key, info, revoked_keys)
        ):
            continue

        if not _matches_requested_package(info, requested_package):
            continue

        return key, info

    return None, None


# =========================================================
# RESPONSE / STATUS HELPERS
# =========================================================

def _key_status(key, info, revoked_keys=None):
    revoked_keys = revoked_keys if revoked_keys is not None else load_revoked_keys()

    if not isinstance(info, dict):
        return {
            "status": "INVALID",
            "message": "Dữ liệu key không hợp lệ",
        }

    if _is_key_revoked(key, info, revoked_keys):
        return {
            "status": "REVOKED",
            "message": "Key đã bị khóa vĩnh viễn",
        }

    # Not activated yet.
    if not info.get("used", False):
        return {
            "status": "AVAILABLE",
            "message": "Key chưa được kích hoạt",
        }

    expires_at = _get_expiry(info)

    if _is_expired(info):
        return {
            "status": "EXPIRED",
            "message": "Key đã hết hạn",
            "activated_at": info.get("activated_at") or info.get("used_at"),
            "expires_at": info.get("expires_at"),
        }

    return {
        "status": "ACTIVE",
        "message": "Key đang hoạt động",
        "activated_at": info.get("activated_at") or info.get("used_at"),
        "expires_at": (
            info.get("expires_at")
            if not _is_forever_key(info)
            else None
        ),
        "device": info.get("device"),
    }


def _mark_expired(key, info, revoked_keys):
    """
    Mark an expired key permanently unusable.

    This does NOT make an active key expire early.
    It only records the expiration after the deadline has passed.
    """
    if not isinstance(info, dict):
        return

    if _is_expired(info):
        info["expired"] = True
        info["expired_at"] = info.get("expires_at") or _now_iso()

        # Keep it unusable for issuance forever.
        revoked_keys[key] = {
            "reason": "expired",
            "revoked_at": info["expired_at"],
            "device": info.get("device"),
            "key_type": info.get("type"),
            "duration": info.get("duration"),
            "activated_at": info.get("activated_at") or info.get("used_at"),
            "expires_at": info.get("expires_at"),
        }


# =========================================================
# ROUTES
# =========================================================

@app.route("/")
def home():
    return "VB TOOL KEY SERVER ONLINE"


# ---------------------------------------------------------
# ISSUE / RESERVE KEY
# ---------------------------------------------------------

@app.route("/api/issue_key", methods=["GET", "POST"])
@app.route("/api/issue-key", methods=["GET", "POST"])
@app.route("/issue_key", methods=["GET", "POST"])
def issue_key():
    data = _request_data()

    requested_package = _get_field(
        data,
        "package",
        "duration",
        "type",
        "plan",
        "key_type",
        "tier",
    )

    requested_duration_hours = _coerce_int(
        _get_field(
            data,
            "duration_hours",
            "requested_duration_hours",
            "durationHours",
            "hours",
            "expire_hours",
        )
    )

    requested_is_forever = _coerce_bool(
        _get_field(
            data,
            "is_forever",
            "forever",
            "permanent",
        )
    )

    order_id = _get_field(
        data,
        "order_id",
        "orderId",
        "id",
        "request_id",
        "requestId",
    )

    user_id = _get_field(
        data,
        "user_id",
        "userId",
        "telegram_id",
        "chat_id",
        "chatId",
    )

    username = _get_field(
        data,
        "username",
        "user_name",
        "name",
    )

    note = _get_field(
        data,
        "note",
        "message",
        "desc",
        "description",
    )

    keys = load_keys()
    revoked_keys = load_revoked_keys()

    key, info = _find_available_key(
        keys,
        requested_package=requested_package,
        requested_duration_hours=requested_duration_hours,
        requested_is_forever=requested_is_forever,
        revoked_keys=revoked_keys,
    )

    if not key:
        return jsonify({
            "success": False,
            "message": "Không còn key phù hợp trong server",
            "requested_package": requested_package,
            "requested_duration_hours": requested_duration_hours,
            "requested_is_forever": requested_is_forever,
        }), 200

    # Issue/reserve the key for the order.
    info["issued"] = True
    info["issued_at"] = info.get("issued_at") or _now_iso()

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
        "package": (
            info.get("package")
            or info.get("duration")
            or info.get("type")
        ),
        "issued_at": info.get("issued_at"),
        "order_id": info.get("order_id"),
        "activated": bool(info.get("used", False)),
        "expires_at": info.get("expires_at"),
    })


# ---------------------------------------------------------
# VERIFY / ACTIVATE / RECHECK KEY
# ---------------------------------------------------------

@app.route("/api/verify_key", methods=["POST"])
@app.route("/api/verify-key", methods=["POST"])
def verify():
    data = _request_data()

    key = _get_field(
        data,
        "key",
        "license",
        "license_key",
    )

    device = _get_field(
        data,
        "device_id",
        "device",
        "hwid",
    )

    if not key:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Thiếu key",
        }), 400

    key = str(key).strip()

    if not device:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Thiếu device_id",
        }), 400

    device = str(device).strip()

    keys = load_keys()
    revoked_keys = load_revoked_keys()

    if key not in keys:
        if key in revoked_keys:
            return jsonify({
                "success": False,
                "status": "REVOKED",
                "message": "Key đã bị khóa vĩnh viễn",
            })

        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Key không tồn tại",
        })

    info = keys[key]

    if not isinstance(info, dict):
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Dữ liệu key không hợp lệ",
        })

    # Permanently revoked.
    if _is_key_revoked(key, info, revoked_keys):
        return jsonify({
            "success": False,
            "status": "REVOKED",
            "message": "Key đã bị khóa vĩnh viễn",
        })

    # -----------------------------------------------------
    # FIRST ACTIVATION
    # -----------------------------------------------------
    if not info.get("used", False):
        now = _now()

        info["used"] = True
        info["redeemed_once"] = True
        info["activated_at"] = now.isoformat()

        # Keep used_at for compatibility with older clients.
        info["used_at"] = info["activated_at"]

        info["device"] = device
        info["expired"] = False

        expiry = _calculate_expiry(info, now)

        # Không có duration hợp lệ thì không trả lỗi riêng về thời hạn.
        # Chỉ key được cấu hình là vĩnh viễn mới thực sự không hết hạn.
        if expiry is not None:
            info["expires_at"] = expiry.isoformat()
        else:
            info["expires_at"] = None

        save_keys(keys)

        # A non-positive/invalid duration is rejected above.
        return jsonify({
            "success": True,
            "status": "ACTIVE",
            "message": "Kích hoạt key thành công",
            "key": key,
            "duration": info.get("duration"),
            "key_type": info.get("type"),
            "device": info.get("device"),
            "activated_at": info.get("activated_at"),
            "expires_at": info.get("expires_at"),
            "is_forever": _is_forever_key(info),
        })

    # -----------------------------------------------------
    # ALREADY ACTIVATED
    # -----------------------------------------------------

    # First check expiration before device check.
    # Once expired, even the correct device/key is rejected.
    if _is_expired(info):
        _mark_expired(key, info, revoked_keys)
        save_keys(keys)
        save_revoked_keys(revoked_keys)

        return jsonify({
            "success": False,
            "status": "EXPIRED",
            "message": "Key đã hết hạn",
            "key": key,
            "duration": info.get("duration"),
            "key_type": info.get("type"),
            "activated_at": info.get("activated_at") or info.get("used_at"),
            "expires_at": info.get("expires_at"),
        })

    # Device is locked after first activation.
    saved_device = str(info.get("device") or "").strip()

    if not saved_device:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Key đã kích hoạt nhưng thiếu device_id đã liên kết",
        })

    if saved_device != device:
        return jsonify({
            "success": False,
            "status": "DEVICE_MISMATCH",
            "message": "Key đã được kích hoạt trên thiết bị khác",
            "expires_at": info.get("expires_at"),
        })

    # Same device + still active = allow re-check/re-login.
    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Key hợp lệ",
        "key": key,
        "duration": info.get("duration"),
        "key_type": info.get("type"),
        "device": saved_device,
        "activated_at": info.get("activated_at") or info.get("used_at"),
        "expires_at": (
            None
            if _is_forever_key(info)
            else info.get("expires_at")
        ),
        "is_forever": _is_forever_key(info),
    })


# ---------------------------------------------------------
# KEY STATUS
# ---------------------------------------------------------

@app.route("/api/key_status", methods=["GET", "POST"])
@app.route("/api/key-status", methods=["GET", "POST"])
def key_status():
    data = _request_data()

    key = _get_field(
        data,
        "key",
        "license",
        "license_key",
    )

    device = _get_field(
        data,
        "device_id",
        "device",
        "hwid",
    )

    if not key:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Thiếu key",
        }), 400

    key = str(key).strip()

    keys = load_keys()
    revoked_keys = load_revoked_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Key không tồn tại",
        })

    info = keys[key]

    if not isinstance(info, dict):
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Dữ liệu key không hợp lệ",
        })

    # Automatically mark expired keys.
    status = _key_status(key, info, revoked_keys)

    if status["status"] == "EXPIRED":
        _mark_expired(key, info, revoked_keys)
        save_keys(keys)
        save_revoked_keys(revoked_keys)

    # Optional device check for active keys.
    if (
        status["status"] == "ACTIVE"
        and device is not None
        and str(info.get("device") or "").strip() != str(device).strip()
    ):
        return jsonify({
            "success": False,
            "status": "DEVICE_MISMATCH",
            "message": "Key đã được kích hoạt trên thiết bị khác",
            "expires_at": info.get("expires_at"),
        })

    return jsonify({
        "success": status["status"] in {"AVAILABLE", "ACTIVE"},
        **status,
        "key": key,
        "duration": info.get("duration"),
        "key_type": info.get("type"),
        "is_forever": _is_forever_key(info),
        "issued": bool(info.get("issued", False)),
        "used": bool(info.get("used", False)),
    })


# ---------------------------------------------------------
# ACTIVATED KEYS
# ---------------------------------------------------------

@app.route("/api/activated_keys", methods=["GET", "POST"])
@app.route("/api/activated-keys", methods=["GET", "POST"])
def activated_keys():
    keys = load_keys()
    revoked_keys = load_revoked_keys()
    result = []

    for key, info in keys.items():
        if not isinstance(info, dict) or not info.get("used", False):
            continue

        status_info = _key_status(key, info, revoked_keys)
        result.append({
            "key": key,
            "device": info.get("device"),
            "device_id": info.get("device"),
            "key_type": info.get("type"),
            "package": info.get("package") or info.get("type") or info.get("duration"),
            "duration": info.get("duration"),
            "status": status_info.get("status"),
            "message": status_info.get("message"),
            "issued_at": info.get("issued_at"),
            "activated_at": info.get("activated_at") or info.get("used_at"),
            "expires_at": info.get("expires_at"),
            "is_forever": _is_forever_key(info),
            "revoked": _is_key_revoked(key, info, revoked_keys),
        })

    result.sort(key=lambda x: str(x.get("activated_at") or ""), reverse=True)

    return jsonify({
        "success": True,
        "count": len(result),
        "keys": result,
    })



# ---------------------------------------------------------
# MANUAL REVOKE
# ---------------------------------------------------------

@app.route("/api/revoke_key", methods=["POST"])
@app.route("/api/revoke-key", methods=["POST"])
def revoke_key():
    data = _request_data()

    key = _get_field(
        data,
        "key",
        "license",
        "license_key",
    )

    reason = _get_field(
        data,
        "reason",
        "message",
        "note",
    ) or "manual_revoke"

    if not key:
        return jsonify({
            "success": False,
            "message": "Thiếu key",
        }), 400

    key = str(key).strip()

    keys = load_keys()
    revoked_keys = load_revoked_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "message": "Key không tồn tại",
        })

    info = keys[key]

    if not isinstance(info, dict):
        return jsonify({
            "success": False,
            "message": "Dữ liệu key không hợp lệ",
        })

    info["revoked"] = True
    info["revoked_at"] = _now_iso()
    info["revoked_reason"] = str(reason)

    revoked_keys[key] = {
        "reason": str(reason),
        "revoked_at": info["revoked_at"],
        "device": info.get("device"),
        "key_type": info.get("type"),
        "duration": info.get("duration"),
        "activated_at": info.get("activated_at") or info.get("used_at"),
        "expires_at": info.get("expires_at"),
    }

    save_keys(keys)
    save_revoked_keys(revoked_keys)

    return jsonify({
        "success": True,
        "message": "Đã khóa key vĩnh viễn",
        "key": key,
    })


# ---------------------------------------------------------
# MANUAL UNREVOKE
# ---------------------------------------------------------

@app.route("/api/unrevoke_key", methods=["POST"])
@app.route("/api/unrevoke-key", methods=["POST"])
def unrevoke_key():
    data = _request_data()

    key = _get_field(
        data,
        "key",
        "license",
        "license_key",
    )

    if not key:
        return jsonify({
            "success": False,
            "message": "Thiếu key",
        }), 400

    key = str(key).strip()

    keys = load_keys()
    revoked_keys = load_revoked_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "message": "Key không tồn tại",
        })

    info = keys[key]

    if not isinstance(info, dict):
        return jsonify({
            "success": False,
            "message": "Dữ liệu key không hợp lệ",
        })

    # Không mở lại key đã hết hạn.
    if _is_expired(info):
        info["expired"] = True
        save_keys(keys)

        return jsonify({
            "success": False,
            "status": "EXPIRED",
            "message": "Key đã hết hạn, không thể mở khóa",
            "key": key,
            "expires_at": info.get("expires_at"),
        })

    # Bỏ trạng thái thu hồi thủ công.
    info["revoked"] = False
    info.pop("revoked_at", None)
    info.pop("revoked_reason", None)
    revoked_keys.pop(key, None)

    save_keys(keys)
    save_revoked_keys(revoked_keys)

    return jsonify({
        "success": True,
        "status": "ACTIVE" if info.get("used", False) else "AVAILABLE",
        "message": "Đã mở khóa key",
        "key": key,
        "device": info.get("device"),
        "duration": info.get("duration"),
        "key_type": info.get("type"),
        "activated_at": info.get(
            "activated_at"
        ) or info.get(
            "used_at"
        ),
        "expires_at": info.get("expires_at"),
        "is_forever": _is_forever_key(info),
    })



# ---------------------------------------------------------
# KEY COUNTS
# ---------------------------------------------------------

def _count_available_keys_by_duration(keys, revoked_keys=None):
    revoked_keys = revoked_keys or {}

    counts = {}

    for key, info in keys.items():
        if not isinstance(info, dict):
            continue

        if (
            info.get("used", False)
            or info.get("issued", False)
            or info.get("revoked", False)
            or key in revoked_keys
        ):
            continue

        duration = _coerce_int(info.get("duration"))

        if duration is None:
            continue

        counts[duration] = counts.get(duration, 0) + 1

    return counts


@app.route("/api/key_counts", methods=["GET"])
@app.route("/api/key-counts", methods=["GET"])
def key_counts():
    keys = load_keys()
    revoked_keys = load_revoked_keys()

    counts = _count_available_keys_by_duration(
        keys,
        revoked_keys
    )

    return jsonify({
        "success": True,
        "counts": counts,
        "total": sum(counts.values()),
    })


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "success": True,
        "message": "ok",
        "time": _now_iso(),
    })


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
