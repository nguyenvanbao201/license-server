from flask import Flask, request, jsonify
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hmac

# MULTI_VIP_PER_TELEGRAM_ENABLED = True
app = Flask(__name__)

# =========================================================
# PERSISTENT DATA DIRECTORY
# =========================================================
# Trên Render, nếu có Persistent Disk tại /var/data thì toàn bộ key
# được lưu ở đó để restart/redeploy không làm mất dữ liệu. Có thể
# đổi bằng biến môi trường VBTOOL_DATA_DIR.
_DEFAULT_DATA_DIR = "/var/data" if os.path.isdir("/var/data") else "."
DATA_DIR = Path(os.getenv("VBTOOL_DATA_DIR", _DEFAULT_DATA_DIR)).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _persistent_file(name):
    target = DATA_DIR / name
    legacy = Path(name)

    # Di chuyển/copy dữ liệu cũ từ thư mục project vào nơi lưu bền vững
    # khi lần đầu bật Persistent Disk.
    if target != legacy and not target.exists() and legacy.exists():
        try:
            target.write_bytes(legacy.read_bytes())
        except Exception:
            pass
    return str(target)


KEY_FILE = _persistent_file("keys.json")
REVOKED_FILE = _persistent_file("revoked_keys.json")
TELEGRAM_VIP_ACTIVATED_FILE = _persistent_file("telegram_vip_activated.json")  # Legacy


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


def load_telegram_vip_activated():
    if not os.path.exists(TELEGRAM_VIP_ACTIVATED_FILE):
        with open(TELEGRAM_VIP_ACTIVATED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)
    try:
        with open(TELEGRAM_VIP_ACTIVATED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_telegram_vip_activated(data):
    tmp_file = TELEGRAM_VIP_ACTIVATED_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, TELEGRAM_VIP_ACTIVATED_FILE)


def _telegram_vip_already_activated(telegram_id):
    tid = str(telegram_id or "").strip()
    if not tid:
        return False
    return tid in load_telegram_vip_activated()


def _mark_telegram_vip_activated(telegram_id, key):
    tid = str(telegram_id or "").strip()
    if not tid:
        return
    data = load_telegram_vip_activated()
    data[tid] = {
        "key": str(key or "").strip(),
        "activated_at": _now_iso(),
    }
    save_telegram_vip_activated(data)


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


# =========================================================
# TELEGRAM ID KEY STORAGE / LOOKUP
# =========================================================

def _telegram_key_type(info):
    if not isinstance(info, dict):
        return ""
    return str(info.get("type") or info.get("key_type") or "").strip().upper()


def _cleanup_expired_telegram_keys(keys, revoked_keys):
    """Đánh dấu các key Telegram đã hết hạn nhưng không xóa lịch sử key."""
    changed_keys = False
    changed_revoked = False
    for key, info in keys.items():
        if not isinstance(info, dict):
            continue
        if not str(info.get("telegram_id") or "").strip():
            continue
        if _is_expired(info) and not info.get("expired", False):
            before = key in revoked_keys
            _mark_expired(key, info, revoked_keys)
            changed_keys = True
            changed_revoked = changed_revoked or (not before and key in revoked_keys)
    if changed_keys:
        save_keys(keys)
    if changed_revoked:
        save_revoked_keys(revoked_keys)
    return changed_keys or changed_revoked


def _find_active_telegram_key(telegram_id, key_type=None, keys=None, revoked_keys=None):
    tid = str(telegram_id or "").strip()
    if not tid:
        return None, None

    keys = keys if keys is not None else load_keys()
    revoked_keys = revoked_keys if revoked_keys is not None else load_revoked_keys()
    candidates = []

    for key, info in keys.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("telegram_id") or "").strip() != tid:
            continue
        current_type = _telegram_key_type(info)
        if key_type and current_type != str(key_type).upper():
            continue
        if not info.get("used", False):
            continue
        if _is_key_revoked(key, info, revoked_keys):
            continue
        if _is_expired(info):
            continue
        candidates.append((key, info))

    candidates.sort(
        key=lambda item: str(
            item[1].get("activated_at")
            or item[1].get("used_at")
            or item[1].get("issued_at")
            or ""
        ),
        reverse=True,
    )
    return candidates[0] if candidates else (None, None)


def _telegram_key_payload(key, info):
    return {
        "key": key,
        "key_type": _telegram_key_type(info),
        "duration": info.get("duration"),
        "telegram_id": str(info.get("telegram_id") or ""),
        "device": info.get("device"),
        "activated_at": info.get("activated_at") or info.get("used_at"),
        "expires_at": None if _is_forever_key(info) else info.get("expires_at"),
        "is_forever": _is_forever_key(info),
    }


def _telegram_admin_secret_ok(data):
    expected = str(os.getenv("KEY_SERVER_ADMIN_SECRET", "")).strip()
    if not expected:
        return False
    provided = request.headers.get("X-Server-Secret", "") or _get_field(
        data, "admin_secret", "server_secret", "secret"
    ) or ""
    return hmac.compare_digest(str(provided).strip(), expected)


def _telegram_auth_status_data(telegram_id):
    tid = str(telegram_id or "").strip()
    if not tid:
        return None, None

    keys = load_keys()
    revoked_keys = load_revoked_keys()
    _cleanup_expired_telegram_keys(keys, revoked_keys)
    # cleanup có thể lưu dữ liệu mới; nạp lại để bảo đảm đọc bản mới nhất.
    keys = load_keys()
    revoked_keys = load_revoked_keys()

    vip_key, vip_info = _find_active_telegram_key(tid, "VIP", keys, revoked_keys)
    free_key, free_info = _find_active_telegram_key(tid, "FREE", keys, revoked_keys)

    return (
        _telegram_key_payload(vip_key, vip_info) if vip_key else None,
        _telegram_key_payload(free_key, free_info) if free_key else None,
    )


# ---------------------------------------------------------
# TELEGRAM BOT VIP VERIFY ONLY
# ---------------------------------------------------------
# Endpoint riêng cho Telegram Bot.
# KHÔNG sửa hoặc thay đổi /api/verify_key và các chức năng key hiện tại.
# Telegram Bot gửi: {"key": "...", "telegram_id": "..."}
# ---------------------------------------------------------

@app.route("/api/telegram/verify_key", methods=["POST"])
def telegram_verify_key():
    data = _request_data()

    key = _get_field(
        data,
        "key",
        "license",
        "license_key",
    )

    telegram_id = _get_field(
        data,
        "telegram_id",
        "telegramId",
        "chat_id",
        "chatId",
    )

    if not key:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Thiếu key",
        }), 400

    if not telegram_id:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Thiếu telegram_id",
        }), 400

    key = str(key).strip()
    telegram_id = str(telegram_id).strip()

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

    # Chỉ cho key VIP dùng endpoint của Telegram Bot.
    key_type = str(
        info.get("type")
        or info.get("key_type")
        or ""
    ).strip().upper()

    if "VIP" not in key_type:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Key này không phải key VIP",
        })

    # Giữ nguyên cơ chế revoke hiện tại của server.
    if _is_key_revoked(key, info, revoked_keys):
        return jsonify({
            "success": False,
            "status": "REVOKED",
            "message": "Key đã bị khóa vĩnh viễn",
        })

    # Giữ nguyên cơ chế hết hạn hiện tại.
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

    # CHO PHÉP MỘT TELEGRAM ID CÓ NHIỀU KEY VIP CÒN HẠN.
    # Chỉ khóa theo từng KEY: nếu key đã gắn với Telegram ID khác thì không được dùng.
    # Vì vậy người dùng có thể mua và kích hoạt Key B trong khi Key A vẫn còn hạn.

    # Khóa key theo Telegram ID. Một key đã gắn ID khác thì không thể dùng lại.
    bound_telegram_id = str(info.get("telegram_id") or "").strip()

    if bound_telegram_id and bound_telegram_id != telegram_id:
        return jsonify({
            "success": False,
            "status": "TELEGRAM_MISMATCH",
            "message": "Key đã được liên kết với Telegram ID khác",
            "expires_at": info.get("expires_at"),
        })

    changed = False
    now = _now()

    if not bound_telegram_id:
        info["telegram_id"] = telegram_id
        info["telegram_activated_at"] = now.isoformat()
        changed = True

    # Nếu key chưa dùng thì kích hoạt. Nếu key đã dùng trên hệ thống khác,
    # endpoint Telegram chỉ chấp nhận khi nó đã có đúng Telegram ID.
    if not info.get("used", False):
        info["issued"] = True
        info["used"] = True
        info["redeemed_once"] = True
        info["activated_at"] = now.isoformat()
        info["used_at"] = info["activated_at"]
        info["expired"] = False

        expiry = _calculate_expiry(info, now)
        if expiry is not None:
            info["expires_at"] = expiry.isoformat()
        else:
            info["expires_at"] = None

        changed = True

    if changed:
        save_keys(keys)

    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Key VIP hợp lệ trên Telegram Bot",
        "key": key,
        "key_type": info.get("type"),
        "duration": info.get("duration"),
        "telegram_id": telegram_id,
        "device": info.get("device"),
        "activated_at": info.get("activated_at") or info.get("used_at"),
        "expires_at": (
            None
            if _is_forever_key(info)
            else info.get("expires_at")
        ),
        "is_forever": _is_forever_key(info),
    })


# ---------------------------------------------------------
# TELEGRAM FREE KEY REGISTER / VERIFY / STATUS / REVOKE
# ---------------------------------------------------------

@app.route("/api/telegram/register_free_key", methods=["POST"])
def telegram_register_free_key():
    data = _request_data()
    if not _telegram_admin_secret_ok(data):
        return jsonify({"success": False, "status": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    key = str(_get_field(data, "key", "license", "license_key") or "").strip()
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()
    device_id = str(_get_field(data, "device_id", "device", "hwid") or "").strip()
    if not key or not telegram_id:
        return jsonify({"success": False, "status": "INVALID", "message": "Thiếu key hoặc telegram_id"}), 400

    keys = load_keys()
    revoked_keys = load_revoked_keys()
    if key in keys:
        info = keys[key]
        if isinstance(info, dict) and _telegram_key_type(info) == "FREE" and str(info.get("telegram_id") or "").strip() == telegram_id and not _is_expired(info):
            return jsonify({"success": True, "status": "ACTIVE", "message": "Key FREE đã tồn tại", **_telegram_key_payload(key, info)})
        return jsonify({"success": False, "status": "KEY_EXISTS", "message": "Key đã tồn tại trên server"})

    active_free_key, active_free_info = _find_active_telegram_key(telegram_id, "FREE", keys, revoked_keys)
    if active_free_key:
        return jsonify({
            "success": False,
            "status": "TELEGRAM_ACTIVE_KEY",
            "message": "Telegram ID này đang có Key FREE còn hiệu lực",
            "telegram_id": telegram_id,
            "expires_at": active_free_info.get("expires_at"),
        })

    now = _now()
    info = {
        "type": "FREE",
        "key_type": "FREE",
        "package": "FREE",
        "duration": 24,
        "issued": True,
        "issued_at": now.isoformat(),
        "used": False,
        "redeemed_once": False,
        "activated_at": None,
        "used_at": None,
        "expires_at": None,
        "pending_expires_at": (now + timedelta(hours=24)).isoformat(),
        "expired": False,
        "telegram_id": telegram_id,
        "device": device_id or None,
        "source": "telegram_free",
    }
    keys[key] = info
    save_keys(keys)

    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Đã lưu Key FREE theo Telegram ID",
        **_telegram_key_payload(key, info),
    })


@app.route("/api/telegram/verify_free_key", methods=["POST"])
def telegram_verify_free_key():
    data = _request_data()
    key = str(_get_field(data, "key", "license", "license_key") or "").strip()
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()
    if not key or not telegram_id:
        return jsonify({"success": False, "status": "INVALID", "message": "Thiếu key hoặc telegram_id"}), 400

    keys = load_keys()
    revoked_keys = load_revoked_keys()
    if key not in keys or not isinstance(keys.get(key), dict):
        return jsonify({"success": False, "status": "INVALID", "message": "Key FREE không tồn tại"})

    info = keys[key]
    if _telegram_key_type(info) != "FREE":
        return jsonify({"success": False, "status": "INVALID", "message": "Key này không phải Key FREE"})
    if _is_key_revoked(key, info, revoked_keys):
        return jsonify({"success": False, "status": "REVOKED", "message": "Key FREE đã bị thu hồi"})

    bound_id = str(info.get("telegram_id") or "").strip()
    if bound_id and bound_id != telegram_id:
        return jsonify({"success": False, "status": "TELEGRAM_MISMATCH", "message": "Key FREE đã thuộc Telegram ID khác"})

    # Key FREE chưa kích hoạt sẽ hết hạn theo thời gian chờ của link.
    if not info.get("used", False):
        pending_expiry = _parse_iso(info.get("pending_expires_at"))
        if pending_expiry is not None and _now() >= pending_expiry:
            info["expired"] = True
            info["expired_at"] = pending_expiry.isoformat()
            revoked_keys[key] = {
                "reason": "expired",
                "revoked_at": pending_expiry.isoformat(),
                "device": info.get("device"),
                "key_type": info.get("type"),
                "duration": info.get("duration"),
                "activated_at": None,
                "expires_at": None,
                "telegram_id": telegram_id,
            }
            save_keys(keys)
            save_revoked_keys(revoked_keys)
            return jsonify({"success": False, "status": "EXPIRED", "message": "Key FREE đã hết thời gian chờ", **_telegram_key_payload(key, info)})

        now = _now()
        info["used"] = True
        info["redeemed_once"] = True
        info["activated_at"] = now.isoformat()
        info["used_at"] = now.isoformat()
        info["expires_at"] = (now + timedelta(hours=24)).isoformat()
        info["expired"] = False
        save_keys(keys)

    if _is_expired(info):
        _mark_expired(key, info, revoked_keys)
        save_keys(keys)
        save_revoked_keys(revoked_keys)
        return jsonify({"success": False, "status": "EXPIRED", "message": "Key FREE đã hết hạn", **_telegram_key_payload(key, info)})

    if not bound_id:
        info["telegram_id"] = telegram_id
        save_keys(keys)

    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Key FREE hợp lệ trên Telegram Bot",
        **_telegram_key_payload(key, info),
    })


@app.route("/api/telegram/auth_status", methods=["GET", "POST"])
def telegram_auth_status():
    data = _request_data()
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()
    if not telegram_id:
        return jsonify({"success": False, "status": "INVALID", "message": "Thiếu telegram_id"}), 400

    vip, free = _telegram_auth_status_data(telegram_id)
    preferred = vip or free
    if not preferred:
        return jsonify({
            "success": False,
            "status": "NO_ACTIVE_KEY",
            "message": "Telegram ID chưa có Key VIP/FREE đang hoạt động",
            "telegram_id": telegram_id,
            "vip": None,
            "free": None,
            "preferred": None,
        })

    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Telegram ID có Key đang hoạt động",
        "telegram_id": telegram_id,
        "vip": vip,
        "free": free,
        "preferred": preferred,
    })


def _telegram_revoke_common(data):
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()
    requested_type = str(_get_field(data, "key_type", "type") or "").strip().upper() or None
    if requested_type not in (None, "FREE", "VIP"):
        return None, None, jsonify({"success": False, "status": "INVALID", "message": "Loại key phải là FREE hoặc VIP"}), 400
    if not telegram_id:
        return None, None, jsonify({"success": False, "status": "INVALID", "message": "Thiếu telegram_id"}), 400

    keys = load_keys()
    revoked_keys = load_revoked_keys()
    _cleanup_expired_telegram_keys(keys, revoked_keys)
    keys = load_keys()
    revoked_keys = load_revoked_keys()

    targets = []
    types = [requested_type] if requested_type else ["VIP", "FREE"]
    for key_type in types:
        key, info = _find_active_telegram_key(telegram_id, key_type, keys, revoked_keys)
        if key and info:
            targets.append((key, info))

    if not targets:
        return None, None, jsonify({"success": False, "status": "NOT_FOUND", "message": "Không tìm thấy Key đang hoạt động cho Telegram ID"}), 200

    now_iso = _now_iso()
    result = []
    for key, info in targets:
        info["revoked"] = True
        info["revoked_at"] = now_iso
        info["revoked_reason"] = "telegram_admin_revoke"
        revoked_keys[key] = {
            "reason": "telegram_admin_revoke",
            "revoked_at": now_iso,
            "device": info.get("device"),
            "key_type": info.get("type"),
            "duration": info.get("duration"),
            "activated_at": info.get("activated_at") or info.get("used_at"),
            "expires_at": info.get("expires_at"),
            "telegram_id": telegram_id,
        }
        result.append(_telegram_key_payload(key, info))

    save_keys(keys)
    save_revoked_keys(revoked_keys)
    return telegram_id, result, None, 200


@app.route("/api/telegram/revoke", methods=["POST"])
def telegram_revoke():
    data = _request_data()
    if not _telegram_admin_secret_ok(data):
        return jsonify({"success": False, "status": "UNAUTHORIZED", "message": "Unauthorized"}), 401
    telegram_id, result, error_response, status_code = _telegram_revoke_common(data)
    if error_response is not None:
        return error_response, status_code
    return jsonify({
        "success": True,
        "status": "REVOKED",
        "message": "Đã thu hồi Key Telegram",
        "telegram_id": telegram_id,
        "keys": result,
        "count": len(result),
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
            "telegram_id": info.get("telegram_id"),
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
# TOOLXW DEVICE LOCK MANAGEMENT
# =========================================================
TOOLXW_DEVICE_FILE = "toolxw_devices.json"
TOOLXW_DEVICE_SECRET = os.getenv("TOOLXW_LOCK_SECRET", "ToolxwFileLock_2026_4Yp8N7vQ2mK6")


def _toolxw_auth_ok():
    provided = request.headers.get("X-Server-Secret", "")
    return bool(provided) and provided == TOOLXW_DEVICE_SECRET


def _load_toolxw_devices():
    if not os.path.exists(TOOLXW_DEVICE_FILE):
        return {}
    try:
        with open(TOOLXW_DEVICE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_toolxw_devices(data):
    tmp = TOOLXW_DEVICE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, TOOLXW_DEVICE_FILE)


def _toolxw_entry_key(file_id, device_id):
    return f"{str(file_id).strip().upper()}::{str(device_id).strip().upper()}"


@app.route("/api/toolxw_register", methods=["POST"])
def toolxw_register():
    if not _toolxw_auth_ok():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data = _request_data()
    file_id = str(_get_field(data, "file_id") or "").strip().upper()
    device_id = str(_get_field(data, "device_id", "device") or "").strip().upper()
    user_id = str(_get_field(data, "user_id", "userId") or "").strip()
    if not file_id or not device_id:
        return jsonify({"success": False, "message": "Thiếu file_id hoặc device_id"}), 400
    devices = _load_toolxw_devices()
    k = _toolxw_entry_key(file_id, device_id)
    entry = devices.get(k, {})
    entry.update({
        "file_id": file_id,
        "device_id": device_id,
        "user_id": user_id,
        "status": "ACTIVE" if entry.get("status") != "LOCKED" else "LOCKED",
        "last_seen": _now_iso(),
        "registered_at": entry.get("registered_at") or _now_iso(),
    })
    devices[k] = entry
    _save_toolxw_devices(devices)
    return jsonify({"success": True, "status": entry["status"], "file_id": file_id, "device_id": device_id})


@app.route("/api/toolxw_status", methods=["POST"])
def toolxw_status():
    if not _toolxw_auth_ok():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data = _request_data()
    file_id = str(_get_field(data, "file_id") or "").strip().upper()
    device_id = str(_get_field(data, "device_id", "device") or "").strip().upper()
    if not file_id or not device_id:
        return jsonify({"success": False, "message": "Thiếu file_id hoặc device_id"}), 400
    devices = _load_toolxw_devices()
    k = _toolxw_entry_key(file_id, device_id)
    entry = devices.get(k)
    if not entry:
        return jsonify({"success": False, "status": "UNKNOWN", "message": "Device chưa đăng ký"}), 404
    entry["last_seen"] = _now_iso()
    devices[k] = entry
    _save_toolxw_devices(devices)
    return jsonify({"success": True, **entry})


@app.route("/api/toolxw_devices", methods=["GET"])
def toolxw_devices():
    if not _toolxw_auth_ok():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    devices = list(_load_toolxw_devices().values())
    devices.sort(key=lambda x: str(x.get("last_seen") or ""), reverse=True)
    return jsonify({"success": True, "count": len(devices), "devices": devices})


@app.route("/api/toolxw_lock", methods=["POST"])
def toolxw_lock():
    if not _toolxw_auth_ok():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data = _request_data()
    file_id = str(_get_field(data, "file_id") or "").strip().upper()
    device_id = str(_get_field(data, "device_id", "device") or "").strip().upper()
    if not device_id:
        return jsonify({"success": False, "message": "Thiếu device_id"}), 400
    devices = _load_toolxw_devices()
    if file_id:
        keys = [_toolxw_entry_key(file_id, device_id)]
    else:
        keys = [k for k,v in devices.items() if str(v.get("device_id", "")).upper() == device_id]
    if not keys:
        return jsonify({"success": False, "message": "Không tìm thấy Device ID"})
    for k in keys:
        devices[k]["status"] = "LOCKED"
        devices[k]["locked_at"] = _now_iso()
    _save_toolxw_devices(devices)
    return jsonify({"success": True, "status": "LOCKED", "device_id": device_id})


@app.route("/api/toolxw_unlock", methods=["POST"])
def toolxw_unlock():
    if not _toolxw_auth_ok():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data = _request_data()
    file_id = str(_get_field(data, "file_id") or "").strip().upper()
    device_id = str(_get_field(data, "device_id", "device") or "").strip().upper()
    if not device_id:
        return jsonify({"success": False, "message": "Thiếu device_id"}), 400
    devices = _load_toolxw_devices()
    if file_id:
        keys = [_toolxw_entry_key(file_id, device_id)]
    else:
        keys = [k for k,v in devices.items() if str(v.get("device_id", "")).upper() == device_id]
    if not keys:
        return jsonify({"success": False, "message": "Không tìm thấy Device ID"})
    for k in keys:
        devices[k]["status"] = "ACTIVE"
        devices[k]["unlocked_at"] = _now_iso()
    _save_toolxw_devices(devices)
    return jsonify({"success": True, "status": "ACTIVE", "device_id": device_id})


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
