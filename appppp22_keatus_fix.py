from flask import Flask, request, jsonify
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hmac
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

# MULTI_VIP_PER_TELEGRAM_ENABLED = True
app = Flask(__name__)

# Khóa sẽ được khởi tạo sau khi biết thư mục dữ liệu.
_KEY_ISSUE_LOCK = None

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


# Kho key và trạng thái kích hoạt được lưu trong keys.json giống bản v1.
KEY_FILE = _persistent_file("keys.json")
LOCK_FILE = str(DATA_DIR / ".vbtool_keys.lock")
REVOKED_FILE = _persistent_file("revoked_keys.json")
TELEGRAM_VIP_ACTIVATED_FILE = _persistent_file("telegram_vip_activated.json")  # Legacy

# Hiện đường dẫn kho key lên log Render để chẩn đoán trường hợp cấp và xác thực
# bị chuyển tới các instance/ổ đĩa khác nhau. Không ghi nội dung key vào log.
app.logger.info("[KEY_STORE] keys_json=%s data_dir=%s", KEY_FILE, DATA_DIR)
if os.getenv("RENDER") and not os.getenv("VBTOOL_DATA_DIR") and not os.path.isdir("/var/data"):
    app.logger.warning("[KEY_STORE] /var/data không tồn tại; keys.json có thể nằm trên ổ tạm. Hãy cấu hình Persistent Disk và VBTOOL_DATA_DIR.")


class _CrossProcessLock:
    """Khóa đồng thời giữa thread và worker/process trên Render/Linux."""
    def __init__(self, lock_path):
        self.lock_path = lock_path
        self.thread_lock = threading.Lock()
        self._local = threading.local()

    def __enter__(self):
        self.thread_lock.acquire()
        try:
            handle = open(self.lock_path, "a+b")
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._local.handle = handle
            return self
        except Exception:
            self.thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        handle = getattr(self._local, "handle", None)
        try:
            if handle is not None:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        finally:
            self._local.handle = None
            self.thread_lock.release()


_KEY_ISSUE_LOCK = _CrossProcessLock(LOCK_FILE)


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


def _normalize_key(value):
    """Key codes are case-insensitive at input; generated keys are stored uppercase."""
    return str(value or "").strip().upper()


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
        "1 năm": {
            "1 năm", "1 nam", "12 tháng", "12 thang",
            "1 year", "1 years", "365 ngày", "365 ngay", "365d"
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
# KEY STORAGE (keys.json; theo cơ chế bản v1)
# =========================================================

def load_keys():
    """Đọc kho key từ keys.json giống bản v1."""
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Chuẩn hóa key để nhập chữ hoa/thường đều tìm được.
            return {_normalize_key(key): value for key, value in data.items()}
    except Exception:
        pass
    return {}


def save_keys(data):
    """Lưu trạng thái key vào keys.json bằng cách ghi file tạm rồi thay thế."""
    if not isinstance(data, dict):
        raise TypeError("save_keys expects a dictionary")
    normalized = {_normalize_key(key): value for key, value in data.items()}
    tmp_file = KEY_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, KEY_FILE)


def _load_telegram_binding_keys(telegram_id, key_type=None):
    """Tìm key đã kích hoạt của Telegram ID trực tiếp trong keys.json."""
    tid = str(telegram_id or "").strip()
    if not tid:
        return []
    keys = load_keys()
    candidates = []
    for key, info in keys.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("telegram_id") or "").strip() != tid:
            continue
        current_type = str(info.get("type") or info.get("key_type") or "").strip().upper()
        if key_type and current_type != str(key_type).upper():
            continue
        if _key_was_ever_activated(info):
            candidates.append((str(info.get("activated_at") or info.get("used_at") or ""), key))
    candidates.sort(reverse=True)
    return [key for _, key in candidates]


def load_revoked_keys():
    if not os.path.exists(REVOKED_FILE):
        with open(REVOKED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=4)

    try:
        with open(REVOKED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            # Normalize legacy lowercase/mixed-case revoked key entries too.
            return {_normalize_key(key): value for key, value in data.items()}

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

    # Key VIP chưa được kích hoạt thành công thì KHÔNG được hết hạn,
    # kể cả dữ liệu cũ có expires_at được ghi từ lúc tạo/cấp key.
    # Thời điểm hết hạn chỉ được tính tại endpoint kích hoạt thành công.
    if not _key_was_ever_activated(info):
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


def _key_was_ever_activated(info):
    """Trả về True nếu key đã từng được kích hoạt/redeem ít nhất 1 lần."""
    if not isinstance(info, dict):
        return False

    return bool(
        info.get("used", False)
        or info.get("redeemed_once", False)
        or info.get("activated_at")
        or info.get("used_at")
        or info.get("telegram_activated_at")
    )


def _reset_unactivated_vip_expiry(key, info, revoked_keys=None):
    """Xóa hạn dùng bị ghi nhầm trước kích hoạt ở dữ liệu VIP cũ.

    Chỉ khôi phục trường hợp chưa từng kích hoạt và chỉ bỏ bản ghi thu hồi
    tự động vì hết hạn; không mở lại key bị Admin khóa thủ công.
    Trả về (keys_changed, revoked_changed).
    """
    if not isinstance(info, dict) or _key_was_ever_activated(info):
        return False, False

    key_type = str(info.get("type") or info.get("key_type") or "").strip().upper()
    if "VIP" not in key_type:
        return False, False

    keys_changed = False
    if info.get("expires_at") is not None:
        info["expires_at"] = None
        keys_changed = True
    if info.get("expired"):
        info["expired"] = False
        keys_changed = True
    if "expired_at" in info:
        info.pop("expired_at", None)
        keys_changed = True

    revoked_changed = False
    if isinstance(revoked_keys, dict):
        revoked_info = revoked_keys.get(key)
        if (
            isinstance(revoked_info, dict)
            and _normalize(revoked_info.get("reason")) == "expired"
            and not info.get("revoked", False)
            and not info.get("revoked_reason")
        ):
            revoked_keys.pop(key, None)
            revoked_changed = True

    return keys_changed, revoked_changed


# =========================================================
# KEY ISSUANCE
# =========================================================
# /api/issue_key chỉ lấy key chưa cấp/chưa kích hoạt từ keys.json; không tự sinh key.

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
                or _key_was_ever_activated(info)
                or info.get("revoked", False)
                or _is_key_revoked(key, info, revoked_keys)
                or _is_forever_key(info)
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
                or _key_was_ever_activated(info)
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
            or _key_was_ever_activated(info)
            or info.get("revoked", False)
            or _is_key_revoked(key, info, revoked_keys)
            or _is_forever_key(info)
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
    if not _telegram_admin_secret_ok(data):
        return jsonify({
            "success": False,
            "status": "FORBIDDEN",
            "message": "Thiếu hoặc sai KEY_SERVER_ADMIN_SECRET",
        }), 403

    requested_package = _get_field(data, "package", "duration", "type", "plan", "key_type", "tier")
    requested_duration_hours = _coerce_int(_get_field(data, "duration_hours", "requested_duration_hours", "durationHours", "hours", "expire_hours"))
    requested_is_forever = _coerce_bool(_get_field(data, "is_forever", "forever", "permanent"))
    package_label = _normalize(requested_package)
    permanent_package_names = {"forever", "permanent", "permanently", "lifelong", "vĩnh viễn", "vinh vien", "vĩnh viễn vip", "vinh vien vip"}
    if requested_is_forever or (requested_duration_hours is None and package_label in permanent_package_names):
        return jsonify({
            "success": False,
            "status": "PACKAGE_REMOVED",
            "message": "Gói Key vĩnh viễn đã ngừng bán. Chỉ hỗ trợ các gói 3 ngày, 1 tuần, 2 tuần, 1 tháng, 2 tháng và 1 năm.",
            "requested_package": requested_package,
        }), 200
    order_id = _get_field(data, "order_id", "orderId", "id", "request_id", "requestId")
    user_id = _get_field(data, "user_id", "userId", "telegram_id", "chat_id", "chatId")
    username = _get_field(data, "username", "user_name", "name")
    note = _get_field(data, "note", "message", "desc", "description")

    with _KEY_ISSUE_LOCK:
        keys = load_keys()
        revoked_keys = load_revoked_keys()

        # Idempotent retry: if a previous request reserved a key for this exact
        # order before a network timeout, return that same reservation rather
        # than consuming a second key from keys.json.
        if order_id is not None:
            requested_order_id = str(order_id)
            for reserved_key, reserved_info in keys.items():
                if not isinstance(reserved_info, dict):
                    continue
                if (
                    reserved_info.get("issued", False)
                    and str(reserved_info.get("order_id") or "") == requested_order_id
                ):
                    if reserved_info.get("revoked", False) or _is_key_revoked(reserved_key, reserved_info, revoked_keys):
                        return jsonify({
                            "success": False,
                            "status": "REVOKED",
                            "message": "Key đã giữ cho đơn này bị thu hồi; cần Admin kiểm tra thủ công",
                            "order_id": requested_order_id,
                        }), 200
                    return jsonify({
                        "success": True,
                        "message": "Key đã được giữ cho đơn này; trả lại key đã cấp trước đó",
                        "key": reserved_key,
                        "duration": reserved_info.get("duration"),
                        "key_type": reserved_info.get("type") or reserved_info.get("key_type"),
                        "package": reserved_info.get("package") or reserved_info.get("duration") or reserved_info.get("type"),
                        "issued_at": reserved_info.get("issued_at"),
                        "order_id": requested_order_id,
                        "activated": _key_was_ever_activated(reserved_info),
                        "expires_at": reserved_info.get("expires_at"),
                        "is_forever": _is_forever_key(reserved_info),
                    })

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
                "message": "Không còn key phù hợp trong keys.json",
                "requested_package": requested_package,
                "requested_duration_hours": requested_duration_hours,
                "requested_is_forever": requested_is_forever,
            }), 200

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
            "message": "Đã lấy key thành công từ keys.json",
            "key": key,
            "duration": info.get("duration"),
            "key_type": info.get("type") or info.get("key_type"),
            "package": info.get("package") or info.get("duration") or info.get("type"),
            "issued_at": info.get("issued_at"),
            "order_id": info.get("order_id"),
            "activated": _key_was_ever_activated(info),
            "expires_at": info.get("expires_at"),
            "is_forever": _is_forever_key(info),
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

    key = _normalize_key(key)

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

    # A used key is invalid on every subsequent activation attempt, regardless
    # of device or Telegram account. Check this before expiry/revocation details.
    if _key_was_ever_activated(info):
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "KEY KHÔNG HỢP LỆ",
        })

    # Repair old VIP records whose countdown started at issue time instead of
    # activation time. Manual/Admin revocations are intentionally preserved.
    key_changed, revoked_changed = _reset_unactivated_vip_expiry(
        key, info, revoked_keys
    )
    if key_changed:
        save_keys(keys)
    if revoked_changed:
        save_revoked_keys(revoked_keys)

    # Permanently revoked before first activation.
    if _is_key_revoked(key, info, revoked_keys):
        return jsonify({
            "success": False,
            "status": "REVOKED",
            "message": "Key đã bị khóa vĩnh viễn",
        })

    # -----------------------------------------------------
    # FIRST ACTIVATION — ONE TIME ONLY
    # -----------------------------------------------------
    if not _key_was_ever_activated(info):
        # Lock + reload under the lock so two simultaneous requests cannot
        # both activate the same key using stale copies of keys.json.
        with _KEY_ISSUE_LOCK:
            keys = load_keys()
            info = keys.get(key)

            if not isinstance(info, dict):
                return jsonify({
                    "success": False,
                    "status": "INVALID",
                    "message": "Key không tồn tại",
                })

            # Another request/process may have activated it while this waited.
            if _key_was_ever_activated(info):
                return jsonify({
                    "success": False,
                    "status": "INVALID",
                    "message": "KEY KHÔNG HỢP LỆ",
                })

            fresh_revoked = load_revoked_keys()
            key_changed, revoked_changed = _reset_unactivated_vip_expiry(
                key, info, fresh_revoked
            )
            if key_changed:
                save_keys(keys)
            if revoked_changed:
                save_revoked_keys(fresh_revoked)

            if _is_key_revoked(key, info, fresh_revoked):
                return jsonify({
                    "success": False,
                    "status": "REVOKED",
                    "message": "Key đã bị khóa vĩnh viễn",
                })

            now = _now()
            info["used"] = True
            info["redeemed_once"] = True
            info["activated_at"] = now.isoformat()
            info["used_at"] = info["activated_at"]
            info["device"] = device
            info["expired"] = False

            expiry = _calculate_expiry(info, now)
            info["expires_at"] = expiry.isoformat() if expiry is not None else None

            save_keys(keys)

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
    # ALREADY ACTIVATED — RE-ENTRY IS ALWAYS REJECTED
    # -----------------------------------------------------
    return jsonify({
        "success": False,
        "status": "INVALID",
        "message": "KEY KHÔNG HỢP LỆ",
    })



# =========================================================
# TELEGRAM ID KEY STORAGE / LOOKUP
# =========================================================

def _telegram_key_type(info):
    """Chuẩn hóa loại Key Telegram, ưu tiên trường key_type rõ ràng.

    Dữ liệu cũ có thể lưu type là nhãn gói (ví dụ "VIP 1h") nhưng đồng thời
    lưu key_type="VIP". Trả về VIP/FREE chuẩn để auth_status vẫn khôi phục được.
    """
    if not isinstance(info, dict):
        return ""
    values = [info.get("key_type"), info.get("type"), info.get("package")]
    for value in values:
        text = str(value or "").strip().upper()
        if not text:
            continue
        if text == "VIP" or re.search(r"\bVIP\b", text):
            return "VIP"
        if text == "FREE" or re.search(r"\bFREE\b", text):
            return "FREE"
    return str(info.get("key_type") or info.get("type") or info.get("package") or "").strip().upper()


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
        owner_id = str(info.get("telegram_id") or "").strip()
        issued_user_id = str(info.get("user_id") or "").strip()
        # Các key đã cấp cho Telegram ID từ phiên bản cũ có thể chỉ còn user_id.
        # Chỉ dùng user_id làm fallback khi telegram_id đang trống hoặc trùng ID;
        # không ghi đè liên kết đã thuộc tài khoản khác.
        if owner_id and owner_id != tid:
            continue
        if not owner_id and issued_user_id != tid:
            # Bản ghi FREE cũ có thể chỉ nhận dạng bằng device TG-<telegram_id>.
            if str(info.get("device") or "").strip() != f"TG-{tid}":
                continue
        elif owner_id != tid and issued_user_id != tid:
            continue
        current_type = _telegram_key_type(info)
        if key_type and current_type != str(key_type).upper():
            continue
        if not info.get("used", False):
            continue
        if not str(info.get("telegram_id") or "").strip():
            issued_user_id = str(info.get("user_id") or "").strip()
            device_id = str(info.get("device") or "").strip()
            if issued_user_id == tid or device_id == f"TG-{tid}":
                info["telegram_id"] = tid
        if _is_key_revoked(key, info, revoked_keys):
            continue
        if _is_expired(info):
            continue
        candidates.append((key, info))

    # Ưu tiên liên kết Telegram ID đã ghi riêng trong SQLite. Đây là đường
    # khôi phục chính sau khi process bot restart hoặc file JSON cục bộ bị mất.
    # Vẫn kiểm tra trạng thái key thật để key hết hạn/thu hồi không được phục hồi.
    bound_candidates = []
    for bound_key in _load_telegram_binding_keys(tid, key_type):
        info = keys.get(bound_key)
        if not isinstance(info, dict):
            continue
        current_type = _telegram_key_type(info)
        if key_type and current_type != str(key_type).upper():
            continue
        owner_id = str(info.get("telegram_id") or "").strip()
        if owner_id and owner_id != tid:
            continue
        if not info.get("used", False) or _is_key_revoked(bound_key, info, revoked_keys) or _is_expired(info):
            continue
        # Hỗ trợ các bản ghi cũ: nếu binding SQLite còn mà metadata thiếu ID,
        # gắn ID lấy từ binding vào bản ghi đang dùng trong request này.
        if not owner_id:
            info["telegram_id"] = tid
        bound_candidates.append((bound_key, info))
    if bound_candidates:
        bound_candidates.sort(
            key=lambda item: str(item[1].get("activated_at") or item[1].get("used_at") or ""),
            reverse=True,
        )
        return bound_candidates[0]

    candidates.sort(
        key=lambda item: str(
            item[1].get("activated_at")
            or item[1].get("used_at")
            or item[1].get("issued_at")
            or ""
        ),
        reverse=True,
    )
    if candidates:
        return candidates[0]
    return None, None


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
    expected = str(os.getenv("KEY_SERVER_ADMIN_SECRET", "VBTOOL_VANBAO999")).strip()
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

    # Ghi lại liên kết Telegram ID được khôi phục từ user_id/device của dữ liệu cũ.
    if any(isinstance(info, dict) and str(info.get("telegram_id") or "").strip() == tid for info in keys.values()):
        save_keys(keys)

    return (
        _telegram_key_payload(vip_key, vip_info) if vip_key else None,
        _telegram_key_payload(free_key, free_info) if free_key else None,
    )


# ---------------------------------------------------------
# TELEGRAM BOT VIP VERIFY ONLY
# ---------------------------------------------------------
# Endpoint kích hoạt key VIP riêng cho Telegram Bot.
# Mọi endpoint kích hoạt dùng chung trạng thái server; mỗi key chỉ kích hoạt 1 lần.
# Telegram Bot gửi: {"key": "...", "telegram_id": "..."}
# ---------------------------------------------------------

@app.route("/api/telegram/verify_key", methods=["POST"])
def telegram_verify_key():
    data = _request_data()
    key = _normalize_key(_get_field(data, "key", "license", "license_key"))
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()

    if not key or not telegram_id:
        return jsonify({
            "success": False,
            "status": "INVALID",
            "message": "Thiếu key hoặc telegram_id",
        }), 400

    # Lock, re-read, validate and persist inside the same critical section. This
    # prevents parallel requests on separate Gunicorn workers from activating twice.
    with _KEY_ISSUE_LOCK:
        keys = load_keys()
        revoked_keys = load_revoked_keys()
        info = keys.get(key)

        if not isinstance(info, dict):
            app.logger.warning(
                "[KEY_VERIFY] status=INVALID telegram_id=%s reason=key_not_found",
                telegram_id,
            )
            return jsonify({
                "success": False,
                "status": "INVALID",
                "message": "KEY KHÔNG HỢP LỆ: không tìm thấy key trên máy chủ",
            })

        key_type = str(info.get("type") or info.get("key_type") or "").strip().upper()
        if "VIP" not in key_type:
            app.logger.warning(
                "[KEY_VERIFY] status=INVALID telegram_id=%s reason=not_vip_key",
                telegram_id,
            )
            return jsonify({
                "success": False,
                "status": "INVALID",
                "message": "KEY KHÔNG HỢP LỆ: key không phải gói VIP",
            })

        # Kiểm tra thu hồi/hết hạn trước khi cho phép cùng tài khoản khôi phục.
        # Nếu lần kích hoạt đầu đã lưu ở server nhưng bot chưa kịp lưu hồ sơ cục bộ,
        # đúng Telegram ID được phép khôi phục trạng thái mà KHÔNG đặt lại thời hạn.
        # Telegram ID khác vẫn bị từ chối.
        if _key_was_ever_activated(info):
            if _is_expired(info):
                _mark_expired(key, info, revoked_keys)
                save_keys(keys)
                save_revoked_keys(revoked_keys)
                app.logger.info(
                    "[KEY_VERIFY] status=EXPIRED telegram_id=%s reason=deadline_passed",
                    telegram_id,
                )
                return jsonify({
                    "success": False,
                    "status": "EXPIRED",
                    "message": "KEY VIP KHÔNG HỢP LỆ: key đã hết hạn",
                    "key": key,
                    "expires_at": info.get("expires_at"),
                })

            revoked_entry = revoked_keys.get(key)
            revoked_reason = _normalize(
                revoked_entry.get("reason") if isinstance(revoked_entry, dict) else ""
            )
            if _is_key_revoked(key, info, revoked_keys):
                if revoked_reason == "expired" or info.get("expired", False):
                    return jsonify({
                        "success": False,
                        "status": "EXPIRED",
                        "message": "KEY VIP KHÔNG HỢP LỆ: key đã hết hạn",
                        "key": key,
                        "expires_at": info.get("expires_at"),
                    })
                app.logger.info(
                    "[KEY_VERIFY] status=REVOKED telegram_id=%s reason=revoked",
                    telegram_id,
                )
                return jsonify({
                    "success": False,
                    "status": "REVOKED",
                    "message": "Key đã bị khóa vĩnh viễn",
                })

            bound_telegram_id = str(info.get("telegram_id") or "").strip()
            if bound_telegram_id and bound_telegram_id == telegram_id:
                return jsonify({
                    "success": True,
                    "status": "ACTIVE",
                    "message": "Key đã được kích hoạt trên tài khoản Telegram này",
                    "key": key,
                    "key_type": info.get("type") or info.get("key_type"),
                    "duration": info.get("duration"),
                    "telegram_id": bound_telegram_id,
                    "device": info.get("device"),
                    "activated_at": info.get("activated_at") or info.get("used_at"),
                    "expires_at": None if _is_forever_key(info) else info.get("expires_at"),
                    "is_forever": _is_forever_key(info),
                    "already_activated_same_account": True,
                })

            app.logger.info(
                "[KEY_VERIFY] status=ALREADY_ACTIVATED telegram_id=%s owner_match=false",
                telegram_id,
            )
            return jsonify({
                "success": False,
                "status": "ALREADY_ACTIVATED",
                "message": "KEY VIP ĐÃ ĐƯỢC KÍCH HOẠT TRÊN TÀI KHOẢN KHÁC",
                "key": key,
                "expires_at": info.get("expires_at"),
            })

        # Nếu key VIP cũ từng bị đánh dấu hết hạn khi chưa kích hoạt,
        # khôi phục trạng thái chờ kích hoạt và bắt đầu đếm từ lúc này.
        key_changed, revoked_changed = _reset_unactivated_vip_expiry(
            key, info, revoked_keys
        )
        if key_changed:
            save_keys(keys)
        if revoked_changed:
            save_revoked_keys(revoked_keys)

        if _is_key_revoked(key, info, revoked_keys):
            return jsonify({
                "success": False,
                "status": "REVOKED",
                "message": "Key đã bị khóa vĩnh viễn",
            })

        if _is_expired(info):
            _mark_expired(key, info, revoked_keys)
            save_keys(keys)
            save_revoked_keys(revoked_keys)
            return jsonify({
                "success": False,
                "status": "EXPIRED",
                "message": "Key đã hết hạn",
                "key": key,
                "expires_at": info.get("expires_at"),
            })

        bound_telegram_id = str(info.get("telegram_id") or "").strip()
        if bound_telegram_id and bound_telegram_id != telegram_id:
            return jsonify({
                "success": False,
                "status": "INVALID",
                "message": "KEY KHÔNG HỢP LỆ",
            })

        now = _now()
        info["issued"] = True
        info["used"] = True
        info["redeemed_once"] = True
        info["activated_at"] = now.isoformat()
        info["used_at"] = info["activated_at"]
        info["telegram_activated_at"] = info["activated_at"]
        info["telegram_id"] = telegram_id
        info["expired"] = False
        expiry = _calculate_expiry(info, now)
        info["expires_at"] = expiry.isoformat() if expiry is not None else None
        save_keys(keys)

    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Kích hoạt key thành công",
        "key": key,
        "key_type": info.get("type") or info.get("key_type"),
        "duration": info.get("duration"),
        "telegram_id": telegram_id,
        "device": info.get("device"),
        "activated_at": info.get("activated_at"),
        "expires_at": info.get("expires_at"),
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

    key = _normalize_key(_get_field(data, "key", "license", "license_key"))
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()
    device_id = str(_get_field(data, "device_id", "device", "hwid") or "").strip()
    if not key or not telegram_id:
        return jsonify({"success": False, "status": "INVALID", "message": "Thiếu key hoặc telegram_id"}), 400

    with _KEY_ISSUE_LOCK:
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
    key = _normalize_key(_get_field(data, "key", "license", "license_key"))
    telegram_id = str(_get_field(data, "telegram_id", "telegramId", "chat_id", "chatId") or "").strip()
    if not key or not telegram_id:
        return jsonify({"success": False, "status": "INVALID", "message": "Thiếu key hoặc telegram_id"}), 400

    with _KEY_ISSUE_LOCK:
        keys = load_keys()
        revoked_keys = load_revoked_keys()
        info = keys.get(key)

        if not isinstance(info, dict) or _telegram_key_type(info) != "FREE":
            return jsonify({"success": False, "status": "INVALID", "message": "KEY KHÔNG HỢP LỆ"})

        # Strictly one activation per key globally, including the original Telegram ID.
        if _key_was_ever_activated(info):
            return jsonify({"success": False, "status": "INVALID", "message": "KEY KHÔNG HỢP LỆ"})

        if _is_key_revoked(key, info, revoked_keys):
            return jsonify({"success": False, "status": "INVALID", "message": "KEY KHÔNG HỢP LỆ"})

        bound_id = str(info.get("telegram_id") or "").strip()
        if bound_id and bound_id != telegram_id:
            return jsonify({"success": False, "status": "INVALID", "message": "KEY KHÔNG HỢP LỆ"})

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
            return jsonify({"success": False, "status": "EXPIRED", "message": "Key FREE đã hết thời gian chờ"})

        now = _now()
        info["used"] = True
        info["redeemed_once"] = True
        info["activated_at"] = now.isoformat()
        info["used_at"] = now.isoformat()
        info["telegram_activated_at"] = now.isoformat()
        info["telegram_id"] = telegram_id
        info["expires_at"] = (now + timedelta(hours=24)).isoformat()
        info["expired"] = False
        save_keys(keys)

    return jsonify({
        "success": True,
        "status": "ACTIVE",
        "message": "Kích hoạt key thành công",
        **_telegram_key_payload(key, info),
    })


@app.route("/api/telegram/auth_status", methods=["GET", "POST"])
def telegram_auth_status():
    data = _request_data()
    # Endpoint trả về key đang hoạt động nên chỉ cho phép bot/backend có secret gọi.
    if not _telegram_admin_secret_ok(data):
        return jsonify({"success": False, "status": "UNAUTHORIZED", "message": "Unauthorized"}), 401
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
    """Thu hồi mọi Key còn hiệu lực gắn với Telegram ID theo loại yêu cầu.

    Hỗ trợ cả Key VIP đã kích hoạt (telegram_id/binding) và Key VIP đã cấp
    cho Telegram ID nhưng chưa kích hoạt (user_id). Không đụng tới loại Key
    khác khi caller chỉ yêu cầu thu hồi VIP.
    """
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

    types = [requested_type] if requested_type else ["VIP", "FREE"]
    targets = []
    seen_keys = set()
    for key_type in types:
        bound_keys = set(_load_telegram_binding_keys(telegram_id, key_type))
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue
            if _telegram_key_type(info) != str(key_type).upper():
                continue
            if _is_key_revoked(key, info, revoked_keys) or _is_expired(info):
                continue

            owner_telegram_id = str(info.get("telegram_id") or "").strip()
            issued_user_id = str(info.get("user_id") or "").strip()
            # Binding SQLite là bằng chứng ràng buộc cho các bản ghi đời cũ
            # bị thiếu telegram_id trong JSON/metadata.
            is_for_target = (
                owner_telegram_id == telegram_id
                or issued_user_id == telegram_id
                or key in bound_keys
            )
            if not is_for_target or key in seen_keys:
                continue
            seen_keys.add(key)
            targets.append((key, info))

    if not targets:
        return None, None, jsonify({"success": False, "status": "NOT_FOUND", "message": "Không tìm thấy Key còn hiệu lực cho Telegram ID"}), 200

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
            "key_type": _telegram_key_type(info),
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

    key = _normalize_key(key)

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

    # Migrate old unused VIP keys: issue-time expiry must never consume duration.
    key_changed, revoked_changed = _reset_unactivated_vip_expiry(
        key, info, revoked_keys
    )
    if key_changed:
        save_keys(keys)
    if revoked_changed:
        save_revoked_keys(revoked_keys)

    # Automatically mark expired keys (only after a successful activation).
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

    key = _normalize_key(key)

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

    key = _normalize_key(key)

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
