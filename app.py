from flask import Flask, request, jsonify
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hmac
import secrets
import threading
import sqlite3

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

# MULTI_VIP_PER_TELEGRAM_ENABLED = True
app = Flask(__name__)

# Khóa sẽ được khởi tạo sau khi biết thư mục dữ liệu.
_KEY_ISSUE_LOCK = None
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

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


# Không dùng keys.json làm kho key. Key mới và trạng thái kích hoạt được lưu
# trong SQLite phía server để không thể bị đặt lại bằng cách xóa dữ liệu trên máy khách.
DATABASE_FILE = str(DATA_DIR / "vbtool_keys.sqlite3")
LOCK_FILE = str(DATA_DIR / ".vbtool_keys.lock")
REVOKED_FILE = _persistent_file("revoked_keys.json")
TELEGRAM_VIP_ACTIVATED_FILE = _persistent_file("telegram_vip_activated.json")  # Legacy

# Hiện đường dẫn kho key lên log Render để chẩn đoán trường hợp cấp và xác thực
# bị chuyển tới các instance/ổ đĩa khác nhau. Không ghi nội dung key vào log.
app.logger.info("[KEY_STORE] sqlite_path=%s data_dir=%s", DATABASE_FILE, DATA_DIR)
if os.getenv("RENDER") and not os.getenv("VBTOOL_DATA_DIR") and not os.path.isdir("/var/data"):
    app.logger.warning("[KEY_STORE] /var/data không tồn tại; SQLite có thể nằm trên ổ tạm. Hãy cấu hình Persistent Disk và VBTOOL_DATA_DIR.")


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
# KEY STORAGE (SQLite; keys.json is not used)
# =========================================================

def _db_connection():
    conn = sqlite3.connect(DATABASE_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS issued_keys ("
        "key TEXT PRIMARY KEY, info_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    return conn


def load_keys():
    """Read the server-side key registry from SQLite, never from keys.json."""
    conn = _db_connection()
    try:
        rows = conn.execute("SELECT key, info_json FROM issued_keys").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["info_json"])
            except (TypeError, json.JSONDecodeError):
                # Preserve bad records as non-dictionaries so normal validation rejects them.
                result[row["key"]] = None
        return result
    finally:
        conn.close()


def save_keys(data):
    """Upsert key records without ever resetting a key's first activation."""
    if not isinstance(data, dict):
        raise TypeError("save_keys expects a dictionary")

    timestamp = _now_iso()
    conn = _db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = []
        for key, candidate in data.items():
            key = str(key)
            info = dict(candidate) if isinstance(candidate, dict) else candidate

            # Make activation irreversible. If a concurrent/stale update tries to
            # restore an unused record, keep the server's original activation.
            current = conn.execute(
                "SELECT info_json FROM issued_keys WHERE key = ?", (key,)
            ).fetchone()
            if current is not None and isinstance(info, dict):
                try:
                    old_info = json.loads(current["info_json"])
                except (TypeError, json.JSONDecodeError):
                    old_info = None

                if isinstance(old_info, dict) and (
                    old_info.get("used")
                    or old_info.get("redeemed_once")
                    or old_info.get("activated_at")
                    or old_info.get("used_at")
                    or old_info.get("telegram_activated_at")
                ):
                    info["used"] = True
                    info["redeemed_once"] = True
                    for field in (
                        "activated_at", "used_at", "telegram_activated_at",
                        "device", "telegram_id", "expires_at",
                    ):
                        if old_info.get(field) is not None:
                            info[field] = old_info[field]

            rows.append((
                key,
                json.dumps(info, ensure_ascii=False, separators=(",", ":")),
                timestamp,
            ))

        conn.executemany(
            "INSERT INTO issued_keys (key, info_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "info_json=excluded.info_json, updated_at=excluded.updated_at",
            rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
# RANDOM KEY GENERATION / ISSUANCE
# =========================================================
# /api/issue_key always calls _generate_vip_key() for a new order. There is no
# pre-generated key stock and no keys.json source.

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

def _is_forever_package(package):
    """Nhận diện tên gói vĩnh viễn ngay cả khi bot cũ gửi thiếu cờ is_forever."""
    value = _normalize(package)
    compact = re.sub(r"[\s_\-]+", "", value)
    permanent_names = {
        "vĩnh viễn", "vinh vien", "vĩnh viễn vip", "vinh vien vip",
        "forever", "permanent", "lifelong", "lifetime", "lifetime vip",
        "trọn đời", "tron doi", "suốt đời", "suot doi", "vip forever",
    }
    permanent_compact = {
        re.sub(r"[\s_\-]+", "", _normalize(item))
        for item in permanent_names
    }
    if value in permanent_names or compact in permanent_compact:
        return True
    # Cho phép tên đầy đủ như "VIP Vĩnh Viễn" hoặc "KEY VIP Lifetime".
    permanent_markers = (
        "vĩnhviễn", "vinhvien", "forever", "permanent", "lifelong",
        "lifetime", "trọnđời", "trondoi", "suốtđời", "suotdoi",
    )
    return any(marker in compact for marker in permanent_markers)


def _duration_from_package(package):
    """Suy ra thời hạn từ tên gói nếu client cũ không gửi duration_hours."""
    value = _normalize(package)
    compact = re.sub(r"[\s_\-]+", "", value)
    aliases = {
        "3ngày": 72, "3ngay": 72, "3d": 72, "3day": 72, "3days": 72,
        "1tuần": 168, "1tuan": 168, "7ngày": 168, "7ngay": 168,
        "1week": 168, "1weeks": 168, "7d": 168,
        "2tuần": 336, "2tuan": 336, "14ngày": 336, "14ngay": 336,
        "2week": 336, "2weeks": 336, "14d": 336,
        "1tháng": 720, "1thang": 720, "30ngày": 720, "30ngay": 720,
        "1month": 720, "1months": 720, "30d": 720,
        "2tháng": 1440, "2thang": 1440, "60ngày": 1440, "60ngay": 1440,
        "2month": 1440, "2months": 1440, "60d": 1440,
        "4tháng": 2880, "4thang": 2880, "120ngày": 2880, "120ngay": 2880,
        "4month": 2880, "4months": 2880, "120d": 2880,
        "6tháng": 4320, "6thang": 4320, "180ngày": 4320, "180ngay": 4320,
        "6month": 4320, "6months": 4320, "180d": 4320,
    }
    return aliases.get(value, aliases.get(compact))


def _generate_vip_key(existing_keys, revoked_keys):
    """Tạo key ngẫu nhiên khó đoán, không phụ thuộc kho key dựng sẵn."""
    while True:
        groups = [
            "".join(secrets.choice(_KEY_ALPHABET) for _ in range(4))
            for _ in range(4)
        ]
        key = "VBVIP-" + "-".join(groups)
        if key not in existing_keys and key not in revoked_keys:
            return key


def _issue_key_response(key, info, message="Đã tạo key thành công"):
    return {
        "success": True,
        "message": message,
        "key": key,
        "duration": info.get("duration"),
        "key_type": info.get("type", "VIP"),
        "package": info.get("package") or info.get("type", "VIP"),
        "issued_at": info.get("issued_at"),
        "order_id": info.get("order_id"),
        "activated": _key_was_ever_activated(info),
        "expires_at": info.get("expires_at"),
        "is_forever": _is_forever_key(info),
    }


@app.route("/api/issue_key", methods=["GET", "POST"])
@app.route("/api/issue-key", methods=["GET", "POST"])
@app.route("/issue_key", methods=["GET", "POST"])
def issue_key():
    data = _request_data()

    # Chỉ bot/backend có server secret mới được tự tạo key, tránh người lạ
    # gọi API công khai để sinh key miễn phí. Bot hiện tại gửi header này.
    if not _telegram_admin_secret_ok(data):
        return jsonify({
            "success": False,
            "status": "UNAUTHORIZED",
            "message": "Unauthorized",
        }), 401

    requested_package = _get_field(
        data, "package", "duration", "type", "plan", "key_type", "tier"
    )
    requested_duration_hours = _coerce_int(_get_field(
        data,
        "duration_hours", "requested_duration_hours", "durationHours",
        "hours", "expire_hours",
    ))
    requested_is_forever = _coerce_bool(_get_field(
        data, "is_forever", "forever", "permanent"
    ))
    # Tên gói là nguồn dự phòng quan trọng: không để gói Vĩnh Viễn bị gán 24 giờ
    # chỉ vì bot/client cũ quên gửi is_forever=true hoặc gửi duration_hours=24.
    if _is_forever_package(requested_package) or _normalize(requested_package) == "vip":
        # Trong menu mua key Telegram hiện tại, package="vip" chỉ dùng cho gói
        # Vĩnh Viễn. Bỏ qua cả duration_hours=24 bị gửi nhầm từ bot cũ.
        requested_is_forever = True

    # Tương thích với client cũ gửi tên gói mà không gửi số giờ.
    if not requested_is_forever and requested_duration_hours is None:
        requested_duration_hours = _duration_from_package(requested_package)

    if requested_is_forever:
        # Với key vĩnh viễn, thời hạn không được tính theo số giờ.
        requested_duration_hours = None
    elif requested_duration_hours is None or requested_duration_hours <= 0:
        return jsonify({
            "success": False,
            "status": "INVALID_PACKAGE",
            "message": "Gói không hợp lệ: cần duration_hours dương hoặc is_forever=true",
            "requested_package": requested_package,
        }), 400

    order_id = _get_field(data, "order_id", "orderId", "id", "request_id", "requestId")
    user_id = _get_field(data, "user_id", "userId", "telegram_id", "chat_id", "chatId")
    username = _get_field(data, "username", "user_name", "name")
    note = _get_field(data, "note", "message", "desc", "description")

    now_iso = _now_iso()
    package_name = str(requested_package or (
        "VIP vĩnh viễn" if requested_is_forever
        else f"VIP {requested_duration_hours} giờ"
    )).strip()

    with _KEY_ISSUE_LOCK:
        keys = load_keys()
        revoked_keys = load_revoked_keys()

        # Idempotency: cùng order_id + cùng cấu hình thì trả lại key đã tạo.
        # Điều này tránh sinh hai key nếu bot gọi lại sau timeout/mất phản hồi.
        if order_id is not None:
            wanted_order_id = str(order_id).strip()
            for old_key, old_info in keys.items():
                if not isinstance(old_info, dict):
                    continue
                if str(old_info.get("order_id") or "").strip() != wanted_order_id:
                    continue

                same_forever = _is_forever_key(old_info) == requested_is_forever
                old_duration = _coerce_int(old_info.get("duration"))
                same_duration = (
                    requested_is_forever
                    or old_duration == requested_duration_hours
                )
                old_user_id = str(old_info.get("user_id") or "").strip()
                wanted_user_id = str(user_id or "").strip()
                same_user = not old_user_id or not wanted_user_id or old_user_id == wanted_user_id

                # Sửa dữ liệu của đơn Vĩnh Viễn đã lỡ được phát hành với 24 giờ.
                # Chỉ áp dụng khi chính yêu cầu hiện tại xác nhận đơn là Vĩnh Viễn,
                # và đơn hàng/tài khoản khớp với hồ sơ cũ.
                old_package = _normalize(old_info.get("package") or old_info.get("name"))
                old_package_is_forever = (
                    _is_forever_package(old_package) or old_package == "vip"
                )
                if requested_is_forever and old_package_is_forever and same_user:
                    old_info["duration"] = None
                    old_info["is_forever"] = True
                    old_info["expires_at"] = None
                    old_info["expired"] = False
                    old_info["package"] = package_name
                    old_info["name"] = package_name
                    _, revoked_changed = _reset_unactivated_vip_expiry(
                        old_key, old_info, revoked_keys
                    )
                    save_keys(keys)
                    if revoked_changed:
                        save_revoked_keys(revoked_keys)
                    return jsonify(_issue_key_response(
                        old_key, old_info,
                        message="Đã sửa đơn Vĩnh Viễn: key không giới hạn thời gian"
                    ))

                if not (same_forever and same_duration and same_user):
                    return jsonify({
                        "success": False,
                        "status": "ORDER_CONFLICT",
                        "message": "Mã đơn đã được dùng để tạo key với thông tin khác",
                        "order_id": wanted_order_id,
                    }), 409

                key_changed, revoked_changed = _reset_unactivated_vip_expiry(
                    old_key, old_info, revoked_keys
                )
                if key_changed:
                    save_keys(keys)
                if revoked_changed:
                    save_revoked_keys(revoked_keys)

                return jsonify(_issue_key_response(
                    old_key, old_info, message="Key của đơn hàng đã được tạo trước đó"
                ))

        # Không lấy key có sẵn trong kho. Mỗi đơn mới tự tạo một key VIP mới.
        key = _generate_vip_key(keys, revoked_keys)
        info = {
            "type": "VIP",
            "key_type": "VIP",
            "tier": "VIP",
            "package": package_name,
            "name": package_name,
            "duration": None if requested_is_forever else requested_duration_hours,
            "is_forever": bool(requested_is_forever),
            "issued": True,
            "issued_at": now_iso,
            "order_id": str(order_id).strip() if order_id is not None else None,
            "user_id": str(user_id).strip() if user_id is not None else None,
            "username": str(username).strip() if username is not None else None,
            "note": str(note).strip() if note is not None else None,
            "used": False,
            "redeemed_once": False,
            "activated_at": None,
            "used_at": None,
            "expires_at": None,
            "expired": False,
            "device": None,
            "source": "telegram_purchase_api",
        }
        keys[key] = info
        save_keys(keys)

    return jsonify(_issue_key_response(key, info))


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
