import os
import sys
import time
import re
import signal
import queue
import threading
import json
import base64
import subprocess
import secrets
import traceback
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Lịch tạm dừng dùng giờ Việt Nam (UTC+7), không phụ thuộc múi giờ máy chủ Render.
_VIETNAM_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")
_SCHEDULE_STATE_LOCK = threading.Lock()
_SCHEDULE_PAUSED_STATE = {"VTH": None, "CDTD": None}


def _normalize_daily_pause_window(window):
    """Chuẩn hóa một hoặc nhiều khung nghỉ thành tuple các cặp (phút bắt đầu, phút chạy lại)."""
    if window is None or window == "" or window == 0 or window == "0":
        return None

    def _valid_pair(start, end):
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            return None
        if 0 <= start < 1440 and 0 <= end < 1440 and start != end:
            return start, end
        return None

    # Tương thích với cấu hình cũ chỉ có một cặp (start, end).
    if isinstance(window, (tuple, list)):
        if len(window) == 2 and not isinstance(window[0], (tuple, list)) and not isinstance(window[1], (tuple, list)):
            pair = _valid_pair(window[0], window[1])
            return (pair,) if pair else None
        pairs = []
        for item in window:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                pair = _valid_pair(item[0], item[1])
                if pair and pair not in pairs:
                    pairs.append(pair)
        return tuple(sorted(pairs)) or None

    if not isinstance(window, str):
        return None

    raw = window.strip()
    if not raw or raw == "0":
        return None

    # Cho phép nhập nhiều khung cách nhau bằng dấu cách, dấu phẩy hoặc dấu chấm phẩy.
    # Cũng chấp nhận khoảng trắng quanh dấu gạch ngang, ví dụ: 17:30 - 20:30.
    raw = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", raw)
    tokens = [token for token in re.split(r"[\s,;]+", raw) if token]
    pairs = []
    pattern = re.compile(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})")
    for token in tokens:
        match = pattern.fullmatch(token)
        if not match:
            return None
        start_h, start_m, end_h, end_m = map(int, match.groups())
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            return None
        pair = _valid_pair(start_h * 60 + start_m, end_h * 60 + end_m)
        if not pair:
            return None
        if pair not in pairs:
            pairs.append(pair)

    return tuple(sorted(pairs)) or None


def _format_daily_pause_window(window):
    windows = _normalize_daily_pause_window(window)
    if windows is None:
        return "0 (chạy liên tục)"
    return " ".join(
        f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
        for start, end in windows
    )


def _prompt_daily_pause_window(tool_name, step_number):
    """Hỏi một hoặc nhiều khung giờ tạm dừng hằng ngày; nhập 0 để chạy liên tục."""
    while True:
        print(
            f"{step_number}. ⏰ Khung giờ chạy {tool_name} (nhập nhiều khung cách nhau bằng dấu cách, "
            "ví dụ 17:30-20:30 22:10-23:59; nhập 0 = chạy liên tục):",
            flush=True,
        )
        raw = input("").strip()
        if raw == "0":
            return None

        windows = _normalize_daily_pause_window(raw)
        if windows is not None:
            return windows

        print(
            "❌ Khung giờ không hợp lệ. Ví dụ: 17:30-20:30 22:10-23:59 hoặc 0. "
            "Có thể nhập nhiều khung, cách nhau bằng dấu cách, dấu phẩy hoặc dấu chấm phẩy; "
            "mỗi khung phải có giờ hợp lệ và giờ bắt đầu khác giờ kết thúc.",
            flush=True,
        )


def _daily_pause_is_active(window, now=None):
    windows = _normalize_daily_pause_window(window)
    if windows is None:
        return False
    if now is None:
        now = datetime.now(_VIETNAM_TZ)
    current_minute = now.hour * 60 + now.minute

    for start, end in windows:
        # Hỗ trợ khung nghỉ qua nửa đêm, ví dụ 23:00-06:00.
        if start < end and start <= current_minute < end:
            return True
        if start > end and (current_minute >= start or current_minute < end):
            return True
    return False

def _check_daily_pause_window(window, tool_name):
    """Trả về True trong khung nghỉ và chỉ thông báo lúc bắt đầu/kết thúc nghỉ."""
    paused = _daily_pause_is_active(window)
    notify = None
    with _SCHEDULE_STATE_LOCK:
        previous = _SCHEDULE_PAUSED_STATE.get(tool_name)
        if paused and previous is not True:
            notify = "pause"
            _SCHEDULE_PAUSED_STATE[tool_name] = True
        elif not paused and previous is True:
            notify = "resume"
            _SCHEDULE_PAUSED_STATE[tool_name] = False
        elif previous is None:
            _SCHEDULE_PAUSED_STATE[tool_name] = paused

    if notify == "pause":
        print(
            f"⏸️ {tool_name}: ĐANG TẠM DỪNG theo lịch "
            f"{_format_daily_pause_window(window)} "
            "Không phân tích và không đặt cược.",
            flush=True,
        )
    elif notify == "resume":
        print(
            f"▶️ {tool_name}: HẾT GIỜ NGHỈ, tự động chạy phân tích và đặt cược lại.",
            flush=True,
        )
    return paused


def _reset_daily_pause_state(tool_name):
    with _SCHEDULE_STATE_LOCK:
        _SCHEDULE_PAUSED_STATE[tool_name] = None

_ORIGINAL_INPUT = input

_RICH_MARKUP_RE = re.compile(r"\[(?:/?[a-zA-Z][^\]]*)\]")

def _strip_rich_markup(value):
    text = str(value or "")
    text = _RICH_MARKUP_RE.sub("", text)
    text = text.replace("\x00", "").replace("\r", "")
    return text

def _telegram_input(prompt=""):
    """Input cố định cho worker Telegram qua stdin PIPE của ToolSession."""
    clean_prompt = _strip_rich_markup(prompt)
    if clean_prompt:
        try:
            print(clean_prompt, flush=True)
        except Exception:
            pass
    try:
        return _ORIGINAL_INPUT()
    except EOFError:
        return ""
    except KeyboardInterrupt:
        return ""


class _WorkerSafeOutput:

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        try:
            return self._stream.write(data)
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise SystemExit(0) from exc

    def flush(self):
        try:
            return self._stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise SystemExit(0) from exc

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _protect_worker_output_streams():
    if not isinstance(sys.stdout, _WorkerSafeOutput):
        sys.stdout = _WorkerSafeOutput(sys.stdout)
    if not isinstance(sys.stderr, _WorkerSafeOutput):
        sys.stderr = _WorkerSafeOutput(sys.stderr)


input = _telegram_input

# Worker context is passed explicitly on the command line.
# No Telegram mode environment variable is used anywhere.
_WORKER_CHAT_ID = None


try:
    import requests
except ImportError as exc:
    raise SystemExit("Thiếu requests. Cài: pip install requests") from exc


class _AnsiFore:
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

class _AnsiStyle:
    RESET_ALL = "\033[0m"

Fore = _AnsiFore()
Style = _AnsiStyle()

BOT_TOKEN = os.getenv("BOT_TOKEN", '8344831081:AAHiHX38GXgKvtTKuqtsAdtY0_W2TaE00rk').strip()
POLL_TIMEOUT = 25
MAX_TEXT = 3900
FLUSH_INTERVAL = 0.40

def _parse_chat_ids(value):
    ids = set()
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except (TypeError, ValueError):
            pass
    return ids
    
_ALLOWED = os.getenv("ALLOWED_CHAT_IDS", "801844480").strip()
ALLOWED_CHAT_IDS = _parse_chat_ids(_ALLOWED)

# Danh sach ADMIN rieng. Telegram ID 8801844480 luon la Admin chinh.
# Van cho phep them Admin khac qua bien moi truong ADMIN_CHAT_IDS.
_ADMIN = os.getenv("ADMIN_CHAT_IDS", "801844480").strip()
ADMIN_CHAT_IDS = _parse_chat_ids(_ADMIN)
ADMIN_CHAT_IDS.add(8801844480)

TOOLS = {
    "vth": ("⚡ VTH", "main_vth"),
    "lotto": ("🎲 LOTTO", "main_lotto"),
    "cdtd": ("🏎️ CDTD", "main_cdtd"),
    "xworld": ("🎯 CANH CODE", "xw_main"),
    "hilo": ("🃏 HILO", "hilo_main"),
}

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
STOP_ALL = threading.Event()
_RENDER_HTTP_SERVER = None

_BOT_POLL_ACTIVITY_LOCK = threading.Lock()
_BOT_POLL_ACTIVITY_TS = time.time()
_BOT_WATCHDOG_THREAD = None
_BOT_WATCHDOG_STOP = threading.Event()


def _mark_bot_poll_activity():
    global _BOT_POLL_ACTIVITY_TS
    with _BOT_POLL_ACTIVITY_LOCK:
        _BOT_POLL_ACTIVITY_TS = time.time()


def _bot_poll_watchdog():

    stale_limit = max(180.0, float(POLL_TIMEOUT) * 6.0)
    while not STOP_ALL.is_set() and not _BOT_WATCHDOG_STOP.wait(15.0):
        with _BOT_POLL_ACTIVITY_LOCK:
            age = time.time() - _BOT_POLL_ACTIVITY_TS
        if age > stale_limit:
            try:
                print(
                    f"[Watchdog] Telegram polling không hoạt động {int(age)}s; thoát process để Render tự khởi động lại.",
                    flush=True,
                )
            except Exception:
                pass
            # Render sẽ khởi động lại service/worker theo restart policy.
            os._exit(70)


def _start_render_health_server():

    global _RENDER_HTTP_SERVER

    port_raw = os.getenv("PORT", "").strip() or "10000"
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError("PORT ngoài phạm vi")
    except (TypeError, ValueError):
        print(f"[Render] PORT không hợp lệ: {port_raw!r}", flush=True)
        return None

    try:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _HealthHandler(BaseHTTPRequestHandler):
            def _send_ok(self):
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return body

            def do_GET(self):
                body = self._send_ok()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_HEAD(self):
                self._send_ok()

            def log_message(self, format, *args):
                return

        class _ReusableHealthServer(ThreadingHTTPServer):
            allow_reuse_address = True

        server = _ReusableHealthServer(("0.0.0.0", port), _HealthHandler)
        server.daemon_threads = True
        _RENDER_HTTP_SERVER = server
        threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name="render-health-server",
        ).start()
        print(f"[Render] Health server listening on 0.0.0.0:{port}", flush=True)
        return server
    except OSError as exc:
        print(f"[Render] Không mở được PORT {port}: {type(exc).__name__}: {exc}", flush=True)
        return None
    except Exception as exc:
        print(f"[Render] Health server lỗi: {type(exc).__name__}: {exc}", flush=True)
        return None


def _stop_render_health_server():
    global _RENDER_HTTP_SERVER
    server = _RENDER_HTTP_SERVER
    _RENDER_HTTP_SERVER = None
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

if Path("/var/data").is_dir():
    BOT_DATA_DIR = Path(os.getenv("VBTOOL_DATA_DIR", "/var/data")).expanduser()
else:
    BOT_DATA_DIR = Path(os.getenv("VBTOOL_DATA_DIR", ".")).expanduser()
BOT_DATA_DIR.mkdir(parents=True, exist_ok=True)


# =========================================================
# THANH TOÁN MUA KEY
# =========================================================
PAYMENT_WALLET = "MOMO"
PAYMENT_ACCOUNT_NAME = "TRAN TUAN HUNG"
PAYMENT_ACCOUNT_NUMBER = "PSP2611614000000227"
PAYMENT_QR_FILE = str(BOT_DATA_DIR / "vbtool_payment_qr.jpg")
_PAYMENT_QR_B64 = """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"""


def _emv_tlv(tag: str, value: str) -> str:
    """Tạo trường TLV theo chuẩn EMVCo/VietQR."""
    value = str(value or "")
    return f"{tag}{len(value):02d}{value}"


def _crc16_ccitt(payload: str) -> str:
    """CRC-16/CCITT-FALSE cho trường 63 của EMVCo QR."""
    crc = 0xFFFF
    for byte in payload.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def _build_dynamic_momo_qr_payload(order) -> str:
    """Tạo VietQR/NAPAS động với số tiền và nội dung CK của đúng đơn."""
    if not isinstance(order, dict):
        raise ValueError("Đơn hàng không hợp lệ.")

    amount = int(order.get("price") or 0)
    payment_content = str(order.get("payment_content") or "").strip()
    if amount <= 0:
        raise ValueError("Số tiền QR không hợp lệ.")
    if not payment_content:
        raise ValueError("Thiếu nội dung chuyển khoản.")
    if len(payment_content) > 25:
        raise ValueError("Nội dung chuyển khoản vượt quá 25 ký tự.")

    # Giữ nguyên định danh MoMo/NAPAS đã có trong QR gốc của bot.
    acquirer_id = "971025"
    account_number = str(PAYMENT_ACCOUNT_NUMBER).strip()
    momo_reference = "MOMOW2W125320472"

    merchant_account = (
        _emv_tlv("00", "A000000727")
        + _emv_tlv("01", _emv_tlv("00", acquirer_id) + _emv_tlv("01", account_number))
        + _emv_tlv("02", "QRIBFTTA")
    )

    # 05 = Reference Label của QR MoMo gốc.
    # 08 = Purpose of Transaction -> nội dung chuyển khoản.
    additional_data = (
        _emv_tlv("05", momo_reference)
        + _emv_tlv("08", payment_content)
    )

    payload_without_crc = (
        _emv_tlv("00", "01")
        + _emv_tlv("01", "12")
        + _emv_tlv("38", merchant_account)
        + _emv_tlv("53", "704")
        + _emv_tlv("54", str(amount))
        + _emv_tlv("58", "VN")
        + _emv_tlv("62", additional_data)
        + "6304"
    )
    return payload_without_crc + _crc16_ccitt(payload_without_crc)


def _build_dynamic_payment_qr_file(order):
    """Tạo ảnh QR riêng cho từng đơn hàng."""
    order_id = str(order.get("order_id") or secrets.token_hex(4)).strip().upper()
    qr_path = Path(BOT_DATA_DIR) / f"vbtool_payment_qr_{order_id}.png"

    try:
        import qrcode

        payload = _build_dynamic_momo_qr_payload(order)
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        qr.make_image().save(str(qr_path))
        return str(qr_path)
    except Exception as exc:
        try:
            log_file = BOT_DATA_DIR / "bot_runtime_errors.log"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[DYNAMIC QR] {type(exc).__name__}: {exc}\n"
                )
        except Exception:
            pass
        try:
            qr_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _ensure_payment_qr_file():
    try:
        if Path(PAYMENT_QR_FILE).is_file() and Path(PAYMENT_QR_FILE).stat().st_size > 1000:
            return PAYMENT_QR_FILE
        raw = base64.b64decode(_PAYMENT_QR_B64)
        with open(PAYMENT_QR_FILE, "wb") as f:
            f.write(raw)
        return PAYMENT_QR_FILE
    except Exception as exc:
        try:
            log_file = BOT_DATA_DIR / "bot_runtime_errors.log"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[QR PAYMENT] {type(exc).__name__}: {exc}\n"
                )
        except Exception:
            pass
        return None


_ensure_payment_qr_file()

BOT_AUTH_FILE = str(BOT_DATA_DIR / "telegram_auth.json")
BOT_PENDING_FILE = str(BOT_DATA_DIR / "telegram_pending.json")
BOT_FREE_USED_FILE = str(BOT_DATA_DIR / "telegram_free_used.json")
BOT_EXPIRED_USERS_FILE = str(BOT_DATA_DIR / "telegram_expired_users.json")
BOT_VIP_ACTIVATED_FILE = str(BOT_DATA_DIR / "telegram_vip_activated.json")  # Legacy
BOT_BAN_FILE = str(BOT_DATA_DIR / "telegram_bans.json")
BOT_CHAT_USERS_FILE = str(BOT_DATA_DIR / "telegram_chat_users.json")
BOT_SPAM_WINDOW = 600  # 10 phút
BOT_MAX_AUTH_ATTEMPTS = 3
BOT_BAN_MIN_HOURS = 12
BOT_BAN_MAX_HOURS = 48
BOT_AUTH = {}          # chat_id -> {key, key_type, expires_at, device_id}
BOT_EXPIRED_PENDING = {}  # chat_id -> key_type, giữ loại key khi record vừa hết hạn bị worker chạm trước watcher
BOT_EXPIRED_USERS = {}  # chat_id -> key_type, giữ dấu tài khoản hết hạn để nút CHỌN TOOL yêu cầu xác thực lại
BOT_VIP_ACTIVATED = {} # Legacy data; không dùng để giới hạn Telegram ID
_CURRENT_CHAT_ID = None
BOT_SPAM = {}          # Legacy/free activity counter; does NOT trigger VIP BAN
BOT_VIP_SPAM = {}      # chat_id -> [timestamps] chỉ dùng cho Key VIP
BOT_AUTH_LOCK = threading.Lock()
BOT_KEY_EXPIRY_WATCHER = None
BOT_KEY_EXPIRY_STOP = threading.Event()

def _load_json_file(path, default):
    try:
        fp = Path(path)
        if not fp.exists():
            return default
        with fp.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default

def _save_json_file(path, data):
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(target) + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        tmp.replace(target)
        return True
    except Exception:
        return False

BOT_BANS = _load_json_file(BOT_BAN_FILE, {})
BOT_AUTH = _load_json_file(BOT_AUTH_FILE, {})
BOT_EXPIRED_USERS = _load_json_file(BOT_EXPIRED_USERS_FILE, {})
BOT_PENDING = _load_json_file(BOT_PENDING_FILE, {})
BOT_CHAT_USERS = _load_json_file(BOT_CHAT_USERS_FILE, [])
if not isinstance(BOT_CHAT_USERS, list):
    BOT_CHAT_USERS = []
BOT_CHAT_USERS = list(dict.fromkeys(str(x) for x in BOT_CHAT_USERS if str(x).strip()))
BOT_CHAT_USERS_LOCK = threading.Lock()
# Trạng thái CHAT chỉ giữ trong RAM để tránh tự phát tin sau khi bot restart.
# BOT_CHAT_WAIT_STOP: Admin đang trả lời có dừng tất cả tool hay không.
# BOT_CHAT_COMPOSING: Admin đã chọn xong và đang soạn tin nhắn phát đi.
BOT_CHAT_WAIT_STOP = set()
BOT_CHAT_COMPOSING = set()


def _save_bot_pending():
    return _save_json_file(BOT_PENDING_FILE, BOT_PENDING)


def _register_chat_user(chat_id):
    """Lưu ID chat riêng đã tương tác với bot để Admin có thể gửi CHAT."""
    try:
        chat_key = str(int(chat_id))
    except (TypeError, ValueError):
        return False
    with BOT_CHAT_USERS_LOCK:
        if chat_key in BOT_CHAT_USERS:
            return True
        BOT_CHAT_USERS.append(chat_key)
        if not _save_json_file(BOT_CHAT_USERS_FILE, BOT_CHAT_USERS):
            BOT_CHAT_USERS.remove(chat_key)
            return False
    return True


def _chat_broadcast_recipients():
    """Kết hợp danh sách chat đã lưu với user cũ có trong dữ liệu bot."""
    recipients = set()
    with BOT_CHAT_USERS_LOCK:
        recipients.update(str(x) for x in BOT_CHAT_USERS if str(x).strip())
    recipients.update(str(x) for x in BOT_AUTH.keys())
    recipients.update(str(x) for x in BOT_EXPIRED_USERS.keys())
    recipients.update(str(x) for x in BOT_PENDING.keys())
    try:
        for record in BOT_FREE_USED.values():
            if isinstance(record, dict) and record.get("telegram_id") is not None:
                recipients.add(str(record.get("telegram_id")))
    except (NameError, AttributeError):
        pass
    try:
        for order in BOT_ORDERS.values():
            if isinstance(order, dict) and order.get("telegram_id") is not None:
                recipients.add(str(order.get("telegram_id")))
    except (NameError, AttributeError):
        pass
    recipients.update(str(x) for x in ADMIN_CHAT_IDS)
    return recipients


def _broadcast_admin_chat(admin_chat_id, message):
    """Phát tin nhắn Admin đồng thời theo nhóm nhỏ để giảm thời gian chờ."""
    message = str(message or "").strip()
    if not message:
        send_message(admin_chat_id, "⚠️ Nội dung CHAT không được để trống.", home_markup(admin_chat_id))
        return

    recipients = _chat_broadcast_recipients()
    sender_id = str(admin_chat_id)
    targets = []
    for recipient in sorted(recipients):
        if recipient == sender_id:
            continue
        try:
            targets.append(int(recipient))
        except (TypeError, ValueError):
            continue

    def _send_one(target_id):
        try:
            result = send_message(target_id, message, home_markup(target_id))
            return result is not None
        except Exception:
            return False

    sent = 0
    failed = 0
    if targets:
        # Gửi đồng thời theo nhóm nhỏ: một tài khoản lỗi/timeout không làm
        # toàn bộ danh sách phải chờ gửi xong từng người trước đó.
        worker_count = min(5, len(targets))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_send_one, target_id) for target_id in targets]
            for future in as_completed(futures):
                try:
                    if future.result():
                        sent += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1

    send_message(
        admin_chat_id,
        "✅ ĐÃ GỬI CHAT\n\n"
        f"👥 Gửi thành công: {sent}\n"
        f"⚠️ Không gửi được: {failed}",
        home_markup(admin_chat_id),
    )


def _admin_stop_all_running_tools():
    """Yêu cầu dừng mọi phiên ToolSession đang chạy, không ảnh hưởng tiến trình bot."""
    with SESSIONS_LOCK:
        active_sessions = list(SESSIONS.items())

    requested = 0
    for target_chat_id, session in active_sessions:
        try:
            if session is None or not session.running:
                continue

            # ToolSession sẽ gửi thông báo này sau khi tiến trình thực sự kết thúc.
            session.set_end_message(
                "🛑 TOOL ĐÃ ĐƯỢC DỪNG THEO YÊU CẦU CỦA ADMIN.\n\n"
                "Bạn có thể chọn tool và chạy lại khi được phép.",
                home_markup(target_chat_id),
            )
            threading.Thread(
                target=session.stop,
                daemon=True,
                name=f"admin-stop-tool-{target_chat_id}",
            ).start()
            requested += 1
        except Exception as exc:
            _admin_only_exception_log(target_chat_id, "Admin dừng toàn bộ tool", exc)

    return requested

BOT_FREE_USED = _load_json_file(BOT_FREE_USED_FILE, {})
BOT_VIP_ACTIVATED = _load_json_file(BOT_VIP_ACTIVATED_FILE, {})


def _now_ts():
    return time.time()

def _parse_iso_ts(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.timestamp()
        return dt.timestamp()
    except Exception:
        return None

def _ban_remaining(chat_id):
    rec = BOT_BANS.get(str(chat_id))
    if not isinstance(rec, dict):
        return 0
    if rec.get("permanent") is True:
        return -1
    until = float(rec.get("until", 0) or 0)
    remaining = until - _now_ts()
    if remaining <= 0:
        BOT_BANS.pop(str(chat_id), None)
        _save_json_file(BOT_BAN_FILE, BOT_BANS)
        return 0
    return remaining

def _format_duration(seconds):
    total_minutes = max(1, int(seconds // 60))
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days} ngày {hours} giờ" if hours else f"{days} ngày"
    return f"{hours} giờ {minutes} phút" if hours else f"{minutes} phút"

def _register_vip_spam(chat_id, reason="Nhập Key VIP không hợp lệ 3 lần liên tiếp"):

    key = str(chat_id)
    now = _now_ts()
    with BOT_AUTH_LOCK:
        arr = [t for t in BOT_VIP_SPAM.get(key, []) if now - t <= BOT_SPAM_WINDOW]
        arr.append(now)
        BOT_VIP_SPAM[key] = arr
        count = len(arr)
        if count < BOT_MAX_AUTH_ATTEMPTS:
            return False, count
        BOT_BANS[key] = {
            "permanent": True,
            "reason": reason,
            "created_at": now,
            "attempts": count,
            "source": "vip_key",
        }
        BOT_VIP_SPAM.pop(key, None)
        BOT_SPAM.pop(key, None)
        _save_json_file(BOT_BAN_FILE, BOT_BANS)
        return True, count

def _reset_vip_spam(chat_id):
    BOT_VIP_SPAM.pop(str(chat_id), None)

_ADMIN_ERROR_LOCK = threading.Lock()
_ADMIN_ERROR_LAST_SENT = {}
_ADMIN_ERROR_COOLDOWN = 15.0

def _notify_admin_code_error(context, exc=None, *, chat_id=None, tool_key=None, traceback_text=None):
    """Gửi lỗi kỹ thuật chi tiết CHỈ cho Admin, tuyệt đối không đưa traceback vào stdout."""
    try:
        if traceback_text is None:
            if exc is not None:
                traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            else:
                traceback_text = ""

        tool_name = TOOLS.get(str(tool_key).strip().lower(), (str(tool_key or "SYSTEM"), ""))[0]
        source_chat = str(chat_id or _WORKER_CHAT_ID or "").strip()
        key = f"{tool_name}|{source_chat}|{context}|{type(exc).__name__ if exc else 'ERROR'}"
        now = time.time()
        with _ADMIN_ERROR_LOCK:
            last = _ADMIN_ERROR_LAST_SENT.get(key, 0.0)
            if now - last < _ADMIN_ERROR_COOLDOWN:
                return False
            _ADMIN_ERROR_LAST_SENT[key] = now

        header = f"🛑 LỖI CODE / RUNTIME\n\n🧰 Tool: {tool_name}\n"
        if source_chat:
            header += f"👤 Telegram ID: {source_chat}\n"
        header += f"📍 Vị trí: {context}\n"
        body = str(traceback_text or str(exc) or "Không có chi tiết lỗi").strip()
        text = (header + "\n" + body)[-3900:]

        sent = False
        for admin_id in sorted(ADMIN_CHAT_IDS):
            try:
                if send_message(admin_id, text, home_markup(admin_id)) is not None:
                    sent = True
            except Exception:
                pass

        # Ghi file nội bộ để vẫn có dấu vết ngay cả khi Telegram tạm lỗi.
        try:
            log_file = BOT_DATA_DIR / "bot_runtime_errors.log"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {header}\n{body}\n")
        except Exception:
            pass
        return sent
    except Exception:
        return False

def _admin_only_exception_log(chat_id, context, exc):
    """Tương thích code cũ: chi tiết lỗi chỉ đi tới Admin, không stdout/user."""
    return _notify_admin_code_error(context, exc, chat_id=chat_id)


def _bot_banned_message(chat_id):
    remaining = _ban_remaining(chat_id)
    if remaining == -1:
        return "⛔ TÀI KHOẢN ĐÃ BỊ BAN VĨNH VIỄN.\n\n🔒 Lý do: Nhập Key VIP không hợp lệ 3 lần liên tiếp."
    if remaining > 0:
        return (
            "⛔ TÀI KHOẢN ĐÃ BỊ BAN DO SPAM.\n\n"
            f"⏰ Thời gian còn lại: {_format_duration(remaining)}"
        )
    return None

def _bot_device_id(chat_id):
    return f"TG-{int(chat_id)}"

def _bot_pending_record(chat_id):
    rec = BOT_PENDING.get(str(chat_id))
    if not isinstance(rec, dict):
        return {}
    expires = float(rec.get("expires_at", 0) or 0)
    created = float(rec.get("created_at", 0) or 0)
    if expires and _now_ts() >= expires:
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        return {}
    if created and _now_ts() - created > 86400:
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        return {}
    return dict(rec)


_BOT_AUTH_RESTORE_LAST = {}
_BOT_AUTH_RESTORE_LOCK = threading.Lock()


def _server_datetime_to_timestamp(value):
    if not value:
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _restore_telegram_auth_from_server(chat_id, force=False):
    """Khôi phục quyền VIP/FREE từ Key Server theo Telegram ID sau khi bot restart."""
    telegram_id = str(chat_id or "").strip()
    if not telegram_id:
        return False
    now = time.time()
    with _BOT_AUTH_RESTORE_LOCK:
        last_check = float(_BOT_AUTH_RESTORE_LAST.get(telegram_id, 0) or 0)
        if not force and now - last_check < 20:
            return False
        _BOT_AUTH_RESTORE_LAST[telegram_id] = now

    try:
        headers = {}
        payload = {"telegram_id": telegram_id}
        if KEY_SERVER_ADMIN_SECRET:
            headers["X-Server-Secret"] = KEY_SERVER_ADMIN_SECRET
            payload["admin_secret"] = KEY_SERVER_ADMIN_SECRET
        response = requests.post(
            f"{KEY_SERVER_URL}/api/telegram/auth_status",
            json=payload,
            headers=headers,
            timeout=8,
        )
        data = response.json() if response.content else {}
        if response.status_code != 200 or not isinstance(data, dict):
            return False
    except Exception as exc:
        log_debug(f"Telegram auth restore failed for {telegram_id}: {type(exc).__name__}: {exc}")
        return False

    if not data.get("success"):
        status = str(data.get("status") or "").upper()
        if status == "NO_ACTIVE_KEY":
            # Server xác nhận không còn key hoạt động (hết hạn/thu hồi): không giữ
            # bản cache cũ để tránh mở quyền trái với trạng thái trên server.
            with BOT_AUTH_LOCK:
                if BOT_AUTH.pop(telegram_id, None) is not None:
                    _save_json_file(BOT_AUTH_FILE, BOT_AUTH)
        return False

    # VIP được ưu tiên, nếu không có VIP còn hạn thì khôi phục FREE còn hạn.
    server_rec = data.get("vip") or data.get("free") or data.get("preferred")
    if not isinstance(server_rec, dict):
        return False
    key = str(server_rec.get("key") or "").strip().upper()
    key_type = str(server_rec.get("key_type") or "").strip().upper()
    if not key or key_type not in {"VIP", "FREE"}:
        return False

    duration_hours = server_rec.get("duration")
    try:
        duration_hours = float(duration_hours) if duration_hours is not None else None
    except (TypeError, ValueError):
        duration_hours = None
    if not duration_hours or duration_hours <= 0:
        duration_hours = 8760.0 if key_type == "VIP" else 24.0
    duration_hours = min(duration_hours, 8760.0) if key_type == "VIP" else duration_hours
    activated_ts = _server_datetime_to_timestamp(server_rec.get("activated_at")) or now
    expiry_ts = _server_datetime_to_timestamp(server_rec.get("expires_at"))
    if not expiry_ts:
        expiry_ts = activated_ts + duration_hours * 3600
    if expiry_ts <= now:
        return False
    restored = {
        "key": key,
        "key_type": key_type,
        "expires_at": float(expiry_ts),
        "device_id": server_rec.get("device") or _bot_device_id(chat_id),
        "duration_hours": duration_hours,
        "is_forever": False,
        "activated_at": float(activated_ts),
        "restored_from_server": True,
    }
    with BOT_AUTH_LOCK:
        BOT_AUTH[telegram_id] = restored
        BOT_EXPIRED_PENDING.pop(telegram_id, None)
        BOT_EXPIRED_USERS.pop(telegram_id, None)
        _save_json_file(BOT_AUTH_FILE, BOT_AUTH)
        _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)
    return True


def _bot_auth_record(chat_id):
    key = str(chat_id)
    with BOT_AUTH_LOCK:
        rec = BOT_AUTH.get(key)
    if not isinstance(rec, dict):
        _restore_telegram_auth_from_server(chat_id)
        with BOT_AUTH_LOCK:
            rec = BOT_AUTH.get(key)
    if not isinstance(rec, dict):
        return None

    with BOT_AUTH_LOCK:
        rec = BOT_AUTH.get(key)
        if not isinstance(rec, dict):
            return None

        # Migra hồ sơ cache cũ từng được lưu là VIP vĩnh viễn sang thời hạn 1 năm.
        # Tính từ timestamp kích hoạt gốc, không reset thời hạn khi bot restart.
        is_vip_record = str(rec.get("key_type", "FREE")).upper() == "VIP"
        try:
            current_expiry = float(rec.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            current_expiry = 0.0
        legacy_unlimited = bool(rec.get("is_forever")) or (
            is_vip_record and rec.get("duration_hours") is None
        ) or current_expiry <= 0
        if legacy_unlimited:
            try:
                activated_ts = float(rec.get("activated_at", 0) or 0)
            except (TypeError, ValueError):
                activated_ts = 0.0
            finite_expiry = activated_ts + 8760 * 3600 if activated_ts > 0 else _now_ts()
            rec["is_forever"] = False
            rec["duration_hours"] = 8760.0 if is_vip_record else rec.get("duration_hours", 24.0)
            rec["expires_at"] = finite_expiry
            BOT_AUTH[key] = rec
            _save_json_file(BOT_AUTH_FILE, BOT_AUTH)

        # QUAN TRỌNG:
        # Worker Telegram chạy ở process riêng. Worker không được tự xóa
        # BOT_AUTH khỏi file khi key hết hạn, vì watcher ở process chính
        # phải còn thấy expires_at + key_type để dừng ToolSession trước
        # rồi mới gửi đúng thông báo KEY VIP/FREE ĐÃ HẾT HẠN.
        try:
            expiry = float(rec.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            expiry = 0.0

        if expiry and _now_ts() >= expiry:
            expired_rec = dict(rec)
            expired_rec["__expired__"] = True

            # Giữ lại thông tin loại key cho watcher trung tâm.
            expired_type = str(rec.get("key_type", "FREE")).upper()
            BOT_EXPIRED_PENDING[key] = expired_type
            if BOT_EXPIRED_USERS.get(key) != expired_type:
                BOT_EXPIRED_USERS[key] = expired_type
                _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)

            # KHÔNG pop/save BOT_AUTH ở đây.
            return expired_rec

        return dict(rec)

def _bot_auth_valid(chat_id, require_vip=False):
    rec = _bot_auth_record(chat_id)
    if not rec:
        return False, None

    if rec.get("__expired__"):
        return False, rec

    is_vip = str(rec.get("key_type", "FREE")).upper() == "VIP"
    if require_vip and not is_vip:
        return False, rec
    return True, rec

def _set_bot_auth(chat_id, key, key_type, expires_at, server_device_id=None, duration_hours=None, is_forever=False):
    # Hệ thống không còn hỗ trợ key không thời hạn; chuyển mọi dữ liệu cũ sang thời hạn hữu hạn.
    is_forever = False
    try:
        duration_value = float(duration_hours) if duration_hours is not None else (8760.0 if str(key_type).upper() == "VIP" else 24.0)
    except (TypeError, ValueError):
        duration_value = 8760.0 if str(key_type).upper() == "VIP" else 24.0
    duration_value = min(duration_value, 8760.0) if str(key_type).upper() == "VIP" else duration_value
    expiry_value = float(expires_at or 0)
    if expiry_value <= 0:
        expiry_value = _now_ts() + duration_value * 3600
    with BOT_AUTH_LOCK:
        BOT_EXPIRED_PENDING.pop(str(chat_id), None)
        # Xóa dấu hết hạn khi người dùng xác thực key mới thành công.
        if BOT_EXPIRED_USERS.pop(str(chat_id), None) is not None:
            _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)
    rec = {
        "key": str(key).strip().upper(),
        "key_type": str(key_type).upper(),
        "expires_at": expiry_value,
        "device_id": server_device_id or _bot_device_id(chat_id),
        "duration_hours": duration_value,
        "is_forever": False,
        "activated_at": _now_ts(),
    }
    with BOT_AUTH_LOCK:
        BOT_AUTH[str(chat_id)] = rec
        _save_json_file(BOT_AUTH_FILE, BOT_AUTH)
    if str(key_type).upper() == "VIP":
        _reset_vip_spam(chat_id)
    return rec

def _free_key_used(key):
    return str(key or "").strip() in BOT_FREE_USED


def _mark_free_key_used(key, chat_id, link=None):
    key = str(key or "").strip()
    if not key:
        return False
    BOT_FREE_USED[key] = {
        "telegram_id": str(chat_id),
        "activated_at": _now_ts(),
        "link": str(link or "").strip(),
    }
    return _save_json_file(BOT_FREE_USED_FILE, BOT_FREE_USED)


def _revoke_vip_on_server(telegram_id):
    """Thu hồi DUY NHẤT Key VIP thật trên Render theo Telegram ID."""
    if not KEY_SERVER_ADMIN_SECRET:
        return False, "KEY_SERVER_ADMIN_SECRET chưa được cấu hình"
    try:
        resp = requests.post(
            f"{KEY_SERVER_URL}/api/telegram/revoke",
            json={
                "telegram_id": str(telegram_id),
                "key_type": "VIP",
                "admin_secret": KEY_SERVER_ADMIN_SECRET,
            },
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or not isinstance(data, dict):
            return False, f"HTTP {resp.status_code}"
        return bool(data.get("success")), str(data.get("message", ""))
    except Exception as exc:
        return False, str(exc)


def _revoke_bot_auth(target_chat_id, key_type=None):

    target = str(target_chat_id).strip()
    with BOT_AUTH_LOCK:
        rec = BOT_AUTH.get(target)
        if not isinstance(rec, dict):
            return False, None
        if key_type and str(rec.get("key_type", "")).upper() != str(key_type).upper():
            return False, rec
        BOT_AUTH.pop(target, None)
        _save_json_file(BOT_AUTH_FILE, BOT_AUTH)
    BOT_PENDING.pop(target, None)
    _save_bot_pending()
    return True, rec

def _clear_bot_auth_for_reauth(target_chat_id):
    """Xóa cache xác thực cục bộ để người dùng trở lại menu nhập Key."""
    target = str(target_chat_id or "").strip()
    if not target:
        return None
    with BOT_AUTH_LOCK:
        rec = BOT_AUTH.pop(target, None)
        if rec is not None:
            _save_json_file(BOT_AUTH_FILE, BOT_AUTH)
    if target in BOT_PENDING:
        BOT_PENDING.pop(target, None)
        _save_bot_pending()
    BOT_EXPIRED_PENDING.pop(target, None)
    if BOT_EXPIRED_USERS.pop(target, None) is not None:
        _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)
    return rec


def _stop_tool_silently(target_chat_id):
    """Dừng tool sau khi thu hồi key mà không gửi log/thông báo dừng cho user."""
    try:
        target = int(target_chat_id)
    except (TypeError, ValueError):
        target = target_chat_id
    session = get_session(target)
    if session is None:
        return False
    try:
        session.suppress_output = True
        # Bỏ các nội dung tool đang chờ gửi để không đẩy thông báo cũ sau khi thu hồi.
        for q in (getattr(session, "send_queue", None),):
            if q is not None:
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
        session.stop()
    except Exception as exc:
        log_debug(f"Silent tool stop failed for {target}: {type(exc).__name__}: {exc}")
    finally:
        with SESSIONS_LOCK:
            if SESSIONS.get(target) is session:
                SESSIONS.pop(target, None)
    return True


def _send_key_entry_menu_after_revoke(target_chat_id):
    """Quay về menu nhập key bằng thông báo chung, không tiết lộ lý do thu hồi."""
    try:
        target = int(target_chat_id)
    except (TypeError, ValueError):
        target = target_chat_id
    send_message(
        target,
        "🔑 HỆ THỐNG KEY\n\n"
        "Bạn chưa có Key đang hoạt động.\n"
        "Vui lòng chọn FREE hoặc VIP để xác thực.",
        _auth_menu_markup(target),
    )


def _is_bot_admin(chat_id):
    try:
        return int(chat_id) in ADMIN_CHAT_IDS
    except (TypeError, ValueError):
        return False


def _is_admin_only_message(text):
    """Nhận diện lệnh/nút chỉ dành cho Admin để bỏ qua âm thầm với user thường."""
    command = str(text or "").strip()
    if not command:
        return False

    # Các nút Admin trong Reply Keyboard.
    if command.upper() in {"💬 CHAT", "🔓 MỞ BAN", "🔑 THU HỒI KEY", "📋 DANH SÁCH ĐƠN"}:
        return True

    lower = command.lower()
    upper = command.upper()
    # Giữ nhất quán với các nhánh xử lý lệnh hiện có (một số nhánh dùng startswith).
    if upper.startswith(("/DUYET", "/TUCHOI", "/MO_BAN")) or upper == "/VIP":
        return True
    if lower.startswith(("/revoke", "/revoke_key", "/unban")):
        return True
    if lower in {"/orders", "/donhang", "/danhsachdon"}:
        return True
    if lower == "/banlist":
        return True
    if lower.startswith("/ban") and not lower.startswith(("/banlist", "/unban")):
        return True
    return False

def _clear_user_auth_pending(chat_id):
    """Xóa trạng thái chờ nhập Key FREE/VIP của người dùng."""
    key = str(chat_id)
    pending = BOT_PENDING.get(key)
    if not isinstance(pending, dict):
        return False
    if pending.get("mode") in {"vip", "free", "free_login", "free_select"}:
        BOT_PENDING.pop(key, None)
        _save_bot_pending()
        return True
    return False

def _expire_running_tool_for_key(chat_id, key_type):
    """Hết hạn key: dừng process tool trước, sau đó mới báo đúng loại key."""
    key_type = "VIP" if str(key_type).upper() == "VIP" else "FREE"
    # Ghi nhớ tài khoản vừa hết hạn để lần bấm CHỌN TOOL tiếp theo
    # luôn hiện link xác thực, kể cả sau khi BOT_AUTH đã được xóa.
    with BOT_AUTH_LOCK:
        BOT_EXPIRED_USERS[str(chat_id)] = key_type
        _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)

    # BOT_AUTH dùng key chuỗi, còn SESSIONS thường dùng Telegram int.
    try:
        session_chat_id = int(chat_id)
    except (TypeError, ValueError):
        session_chat_id = chat_id

    session = get_session(session_chat_id)
    message = f"KEY {key_type} ĐÃ HẾT HẠN"
    target_chat_id = getattr(session, "chat_id", session_chat_id)

    if not session or not session.running:
        # Không có tool chạy: vẫn báo hết hạn và đưa thẳng về màn hình nhập key.
        send_message(target_chat_id, message, _auth_menu_markup(target_chat_id))
        return False

    # Đặt end-message TRƯỚC khi stop để _flusher biết đây là kết thúc do hết hạn.
    session.set_end_message(message, _auth_menu_markup(target_chat_id))
    session.stop()
    return True


def _bot_key_expiry_watcher():
    """Watcher trung tâm xử lý hết hạn key cho mọi ToolSession."""
    while not STOP_ALL.is_set() and not BOT_KEY_EXPIRY_STOP.wait(0.5):
        now = _now_ts()
        expired = {}

        with BOT_AUTH_LOCK:
            # Process chính là nơi duy nhất quyết định key đã hết hạn.
            # Worker chỉ được đánh dấu __expired__ để báo ngược về đây.
            for chat_id, rec in list(BOT_AUTH.items()):
                if not isinstance(rec, dict):
                    continue
                try:
                    expires_at = float(rec.get("expires_at", 0) or 0)
                except (TypeError, ValueError):
                    expires_at = 0.0

                if expires_at and now >= expires_at:
                    expired[str(chat_id)] = str(
                        rec.get("key_type", "FREE")
                    ).upper()

            # Hỗ trợ cả trường hợp worker đã chạm vào record trước watcher.
            for chat_id, key_type in list(BOT_EXPIRED_PENDING.items()):
                expired.setdefault(
                    str(chat_id),
                    str(key_type).upper(),
                )
                BOT_EXPIRED_PENDING.pop(chat_id, None)

            if expired:
                # Lưu dấu hết hạn trước khi xóa quyền xác thực, để không mất trạng thái
                # nếu tiến trình khởi động lại ngay lúc key vừa hết hạn.
                for chat_id, key_type in expired.items():
                    BOT_EXPIRED_USERS[str(chat_id)] = (
                        "VIP" if str(key_type).upper() == "VIP" else "FREE"
                    )
                _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)

                # Xóa quyền xác thực CHỈ SAU khi đã lấy đủ key_type.
                for chat_id in expired:
                    BOT_AUTH.pop(chat_id, None)
                try:
                    _save_json_file(BOT_AUTH_FILE, BOT_AUTH)
                except Exception:
                    pass

        for chat_id, key_type in expired.items():
            try:
                _expire_running_tool_for_key(chat_id, key_type)
            except Exception as exc:
                _notify_admin_code_error(
                    "Watcher hết hạn key lỗi",
                    exc,
                    chat_id=chat_id,
                    tool_key="SYSTEM",
                )


def start_bot_key_expiry_watcher():
    global BOT_KEY_EXPIRY_WATCHER
    if BOT_KEY_EXPIRY_WATCHER is not None and BOT_KEY_EXPIRY_WATCHER.is_alive():
        return
    BOT_KEY_EXPIRY_STOP.clear()
    BOT_KEY_EXPIRY_WATCHER = threading.Thread(
        target=_bot_key_expiry_watcher,
        daemon=True,
        name="bot-key-expiry-watcher",
    )
    BOT_KEY_EXPIRY_WATCHER.start()


def stop_bot_key_expiry_watcher():
    BOT_KEY_EXPIRY_STOP.set()


def _auth_menu_markup(chat_id=None):
    """Menu nhập Key; nút CHAT chỉ được hiển thị với Admin."""
    keyboard = [
        [{"text": "🛒 MUA KEY VIP"}],
        [{"text": "🆓 NHẬP KEY FREE"}, {"text": "👑 NHẬP KEY VIP"}],
    ]
    if chat_id is not None and _is_bot_admin(chat_id):
        keyboard.append([{"text": "💬 CHAT"}])
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Chọn nhập Key FREE hoặc VIP...",
    }

def _auth_menu_text(chat_id):
    ban = _bot_banned_message(chat_id)
    if ban:
        return ban
    valid, rec = _bot_auth_valid(chat_id)
    if valid and rec:
        remaining = max(0, float(rec.get("expires_at", 0) or 0) - _now_ts())
        expiry_text = f"⏰ Còn: {_format_duration(remaining)}\n"
        return (
            "✅ ĐÃ XÁC THỰC KEY\n\n"
            f"🔑 Loại Key: {rec.get('key_type', 'FREE')}\n"
            f"{expiry_text}\n"
            "Chọn tool để sử dụng."
        )
    return (
        "🔑 HỆ THỐNG KEY\n\n"
        "Bạn chưa có Key đang hoạt động.\n"
        "Vui lòng chọn FREE hoặc VIP để xác thực.\n"
    )


def tg(method, payload=None, timeout=35):
    """Gọi Telegram API với retry an toàn cho lỗi mạng tạm thời.

    Chỉ retry getUpdates vì đây là long-polling và việc gọi lại không tạo
    tác dụng phụ như sendMessage/sendPhoto. Các API khác vẫn fail-fast để
    tránh gửi/trùng hành động ngoài ý muốn.
    """
    if not BOT_TOKEN:
        raise RuntimeError("Chưa đặt BOT_TOKEN.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    attempts = 4 if method == "getUpdates" else 1
    last_exc = None
    for attempt in range(attempts):
        try:
            r = requests.post(url, json=payload or {}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(str(data))
            return data.get("result")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                raise
            # Kết nối TLS/Socket trên Android/Render đôi lúc bị peer đóng.
            # Chờ ngắn rồi tạo lại kết nối ở lần gọi tiếp theo.
            time.sleep(min(2 * (attempt + 1), 6))
    if last_exc is not None:
        raise last_exc
    return None


def send_message(chat_id, text, markup=None, parse_mode=None):
    text = text or " "
    payload = {"chat_id": chat_id, "text": text[:4090],
               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if markup:
        payload["reply_markup"] = markup
    try:
        return tg("sendMessage", payload, timeout=12)
    except Exception as exc:
        # Khi chạy worker, stderr bị nối vào stdout và sẽ được chuyển về user.
        # Không in exception ra stdout/stderr trong Telegram mode; chỉ lưu nội bộ.
        if True:
            try:
                log_file = BOT_DATA_DIR / "bot_runtime_errors.log"
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(
                        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"[Telegram sendMessage] {type(exc).__name__}: {exc}\n"
                    )
            except Exception:
                pass
        else:
            print("[Telegram]", exc, file=sys.stderr)
        return None


def delete_message(chat_id, message_id):
    """Xóa tin nhắn Telegram cũ, dùng cho luồng thanh toán mua Key."""
    try:
        if message_id is None:
            return False
        tg(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
            timeout=12,
        )
        return True
    except Exception as exc:
        try:
            log_file = BOT_DATA_DIR / "bot_runtime_errors.log"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[Telegram deleteMessage] {type(exc).__name__}: {exc}\n"
                )
        except Exception:
            pass
        return False


def send_photo(chat_id, photo_path, caption="", markup=None, parse_mode=None):
    """Gửi ảnh cho Telegram bằng multipart/form-data (sendPhoto)."""
    if not photo_path or not Path(photo_path).is_file():
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data = {
        "chat_id": chat_id,
        "caption": str(caption or "")[:1024],
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if markup:
        data["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data=data,
                files={"photo": (Path(photo_path).name, f, "image/jpeg")},
                timeout=20,
            )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return payload.get("result")
    except Exception as exc:
        try:
            log_file = BOT_DATA_DIR / "bot_runtime_errors.log"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(
                    f"\\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[Telegram sendPhoto] {type(exc).__name__}: {exc}\\n"
                )
        except Exception:
            pass
        return None


def answer_callback(callback_id, text=""):
    try:
        tg("answerCallbackQuery",
           {"callback_query_id": callback_id, "text": text[:190]},
           timeout=15)
    except Exception:
        pass


def updates(offset=None):
    payload = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    return tg("getUpdates", payload, timeout=POLL_TIMEOUT + 10)


def home_markup(chat_id=None):
    """Menu chính. Chưa xác thực Key thì CHỈ hiện FREE/VIP, không hiện menu tool."""
    global _CURRENT_CHAT_ID
    # Never infer the recipient from _CURRENT_CHAT_ID: concurrent updates could
    # accidentally expose Admin-only buttons in another user's menu.
    is_admin = _is_bot_admin(chat_id) if chat_id is not None else False
    valid, rec = _bot_auth_valid(chat_id) if chat_id is not None else (False, None)

    # Admin vẫn được phép thấy menu quản trị/tool.
    if is_admin:
        keyboard = [
            [{"text": "🧰 CHỌN TOOL"}],
            [{"text": "🛒 MUA KEY VIP"}],
            [{"text": "🆓 NHẬP KEY FREE"}, {"text": "👑 NHẬP KEY VIP"}],
            [{"text": "📊 Trạng thái"}, {"text": "⏹️ Dừng"}],
            [{"text": "📋 DANH SÁCH ĐƠN"}],
            [{"text": "💬 CHAT"}],
            [{"text": "🔓 MỞ BAN"}, {"text": "🔑 THU HỒI KEY"}],
        ]
    elif valid and rec:
        keyboard = [
            [{"text": "🧰 CHỌN TOOL"}],
            [{"text": "🛒 MUA KEY VIP"}],
            [{"text": "🆓 NHẬP KEY FREE"}, {"text": "👑 NHẬP KEY VIP"}],
            [{"text": "📊 Trạng thái"}, {"text": "⏹️ Dừng"}],
        ]
    else:
        # CHƯA CÓ KEY: không hiện CHỌN TOOL / Trạng thái / Dừng.
        return _auth_menu_markup(chat_id)

    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Chọn chức năng...",
    }


def tool_menu_markup():
    # Menu 5 tool chỉ xuất hiện sau khi người dùng bấm CHỌN TOOL.
    # Không còn nút "↩️ Quay lại" ở menu chọn tool.
    # Sau khi chọn tool, start_tool() sẽ tự đổi bàn phím về menu chính.
    return {
        "keyboard": [
            [{"text": "⚡ VTH"}, {"text": "🎲 LOTTO"}],
            [{"text": "🏎️ CDTD"}, {"text": "🎯 CANH CODE"}],
            [{"text": "🃏 HILO"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Chọn tool muốn sử dụng...",
    }


def clean_output(text):
    text = ANSI_RE.sub("", text or "")
    text = _strip_rich_markup(text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)


_INTERNAL_ERROR_PATTERNS = (
    "Traceback (most recent call last):",
    "NameError:", "TypeError:", "ValueError:", "KeyError:",
    "AttributeError:", "IndexError:", "RuntimeError:",
    "ZeroDivisionError:", "JSONDecodeError:", "ModuleNotFoundError:",
    "ImportError:", "SyntaxError:", "AssertionError:",
    "ConnectionError:", "TimeoutError:", "FileNotFoundError:",
    "requests.exceptions.", "websocket._exceptions.",
)

def _looks_like_internal_error(text):
    """Nhận diện lỗi kỹ thuật để không chuyển traceback/exception cho user."""
    raw = str(text or "")
    return any(pattern in raw for pattern in _INTERNAL_ERROR_PATTERNS)

def chunks(text, limit=MAX_TEXT):
    text = text.strip()
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < 200:
            cut = limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


class ToolSession:
    def __init__(self, chat_id, key):
        self.chat_id = chat_id
        self.key = key
        self.name, self.fn_name = TOOLS[key]
        self.proc = None
        # Giới hạn stdout queue để tool in quá nhiều log không làm RAM tăng vô hạn.
        self.q = queue.Queue(maxsize=600)
        self.reader_thread = None
        self.stderr_thread = None
        self.flush_thread = None
        self.sender_thread = None
        self.stop_event = threading.Event()
        self.sender_stop_event = threading.Event()
        self.send_queue = queue.Queue(maxsize=200)
        self._last_sent_text = ""
        self._last_sent_ts = 0.0
        self._stdin_lock = threading.Lock()
        self._expected_stop = False
        self.suppress_output = False
        self._end_message = None
        self._end_markup = None
        self._stderr_lines = []
        self.awaiting_cdtd_coin = False
        self.awaiting_cdtd_bet_type = False
        self.awaiting_cdtd_logic = False
        self.started = time.time()
        # Telegram CDTD: đang chờ nút chọn loại tiền (USDT/BUILD/XWORLD).
        self.awaiting_cdtd_coin = False
        self.input_file = os.path.join(
            "/tmp", f"vbtool_tg_input_{chat_id}_{self.started:.6f}.queue"
        )
        try:
            Path(self.input_file).write_text("", encoding="utf-8")
        except Exception:
            pass

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        worker_script = os.path.abspath(__file__)
        self.proc = subprocess.Popen(
            [sys.executable, "-u", worker_script, "--worker", self.key, str(self.chat_id)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        self.reader_thread = threading.Thread(
            target=self._reader, daemon=True, name=f"tg-stdout-{self.chat_id}-{self.key}")
        self.reader_thread.start()
        self.stderr_thread = threading.Thread(
            target=self._stderr_reader, daemon=True, name=f"tg-stderr-{self.chat_id}-{self.key}")
        self.stderr_thread.start()
        self.sender_thread = threading.Thread(
            target=self._sender_loop, daemon=True, name=f"tg-send-{self.chat_id}-{self.key}")
        self.sender_thread.start()
        self.flush_thread = threading.Thread(
            target=self._flusher, daemon=True, name=f"tg-flush-{self.chat_id}-{self.key}")
        self.flush_thread.start()

    def _reader(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if line == "":
                    break
                try:
                    self.q.put_nowait(line)
                except queue.Full:
                    
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.q.put_nowait(line)
                    except queue.Full:
                        pass
        except Exception as exc:
            _notify_admin_code_error(
                "ToolSession reader lỗi output",
                exc,
                chat_id=self.chat_id,
                tool_key=self.key,
            )
        finally:
            self.stop_event.set()

    def _stderr_reader(self):
        try:
            while True:
                line = self.proc.stderr.readline()
                if line == "":
                    break
                self._stderr_lines.append(line.rstrip("\n"))
                if len(self._stderr_lines) > 120:
                    self._stderr_lines = self._stderr_lines[-120:]
        except Exception as exc:
            _notify_admin_code_error(
                "ToolSession stderr reader lỗi",
                exc,
                chat_id=self.chat_id,
                tool_key=self.key,
            )

    def _enqueue_message(self, text, markup=None):
        if self.suppress_output:
            return
        text = str(text or " ").strip() or " "
        item = (text, markup)
        try:
            self.send_queue.put_nowait(item)
        except queue.Full:

            try:
                self.send_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.send_queue.put_nowait(item)
            except queue.Full:
                pass

    def _sender_loop(self):

        min_interval = 0.95
        while not self.sender_stop_event.is_set() or not self.send_queue.empty():
            try:
                text, markup = self.send_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            now = time.time()
            wait_for = min_interval - (now - self._last_sent_ts)
            if wait_for > 0:
                time.sleep(wait_for)

            now = time.time()
            if text == self._last_sent_text and now - self._last_sent_ts < 2.0:
                continue

            if self.suppress_output:
                continue
            try:
                send_message(self.chat_id, text, markup)
                self._last_sent_text = text
                self._last_sent_ts = time.time()
            except Exception as exc:
                _notify_admin_code_error(
                    "Telegram output sender lỗi",
                    exc,
                    chat_id=self.chat_id,
                    tool_key=self.key,
                )

    def _flusher(self):
        buf = []
        first_line_ts = None
        last_data_ts = None

        def _send_part(part):
            # Tách lỗi lịch không hợp lệ và câu hỏi nhập lịch thành 2 tin Telegram.
            # stdout của worker được gom trong FLUSH_INTERVAL, nên các print liền nhau
            # có thể về chung một `part` nếu không tách ở đây.
            lines = str(part or "").splitlines()
            invalid_i = next(
                (i for i, line in enumerate(lines)
                 if line.strip().startswith("❌ Khung giờ không hợp lệ.")),
                None,
            )
            prompt_i = next(
                (i for i, line in enumerate(lines)
                 if re.match(r"^\d+\.\s*⏰\s*Khung giờ (?:tạm dừng|chạy)", line.strip())),
                None,
            )
            if invalid_i is not None and prompt_i is not None and invalid_i < prompt_i:
                before = "\n".join(lines[:invalid_i]).strip()
                error_msg = "\n".join(lines[invalid_i:prompt_i]).strip()
                prompt_msg = lines[prompt_i].strip()
                after_prompt = "\n".join(lines[prompt_i + 1:]).strip()
                if before:
                    _send_part(before)
                if error_msg:
                    _send_part(error_msg)
                if prompt_msg:
                    _send_part(prompt_msg)
                if after_prompt:
                    _send_part(after_prompt)
                return

            # Giữ thông báo lịch nghỉ/chạy lại thành tin Telegram độc lập cho CẢ VTH và CDTD.
            # Nếu các print xảy ra sát nhau và bị FLUSH_INTERVAL gom chung, tách dòng
            # trạng thái khỏi nội dung ván/phòng/thuật toán/tiền cược.
            schedule_prefixes = (
                f"⏸️ {self.key.upper()}: ĐANG TẠM DỪNG",
                f"▶️ {self.key.upper()}: HẾT GIỜ NGHỈ",
            )
            schedule_i = next(
                (i for i, line in enumerate(lines)
                 if line.strip().startswith(schedule_prefixes)),
                None,
            )
            if schedule_i is not None and len(lines) > 1:
                before = "\n".join(lines[:schedule_i]).strip()
                status_msg = lines[schedule_i].strip()
                following = "\n".join(lines[schedule_i + 1:]).strip()
                if before:
                    _send_part(before)
                _send_part(status_msg)
                if following:
                    _send_part(following)
                return

            if self.key == "cdtd" and "✅ Đã lưu cấu hình." in part and "9.CHỌN LOGIC" in part:
                left, right = part.split("9.CHỌN LOGIC", 1)
                _send_part(left.rstrip())
                _send_part("9.CHỌN LOGIC" + right)
                return

            if _looks_like_internal_error(part):
                _notify_admin_code_error(
                    "lỗi kỹ thuật",
                    chat_id=self.chat_id,
                    tool_key=self.key,
                    traceback_text=part,
                )
                self._enqueue_message(
                    "BOT LỖI"
                )
                return

            payload_text = part if self.key in ("vth", "cdtd", "lotto") else f"{self.name}\n\n{part}"
            markup = None

            if self.key == "cdtd":
                if "9.CHỌN LOGIC" in part:
                    self.awaiting_cdtd_coin = False
                    self.awaiting_cdtd_bet_type = False
                    self.awaiting_cdtd_logic = True
                    markup = {
                        "keyboard": [
                            [{"text": "🆓 LOGIC FREE"}, {"text": "👑 LOGIC VIP"}]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                        "input_field_placeholder": "Chọn Logic...",
                    }

                elif "👉 Nhập loại tiền bạn muốn chơi" in part or "CHỌN LOẠI TIỀN CƯỢC" in part:
                    self.awaiting_cdtd_coin = True
                    self.awaiting_cdtd_bet_type = False
                    markup = {
                        "keyboard": [
                            [{"text": "USDT"}, {"text": "BUILD"}],
                            [{"text": "XWORLD"}]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                        "input_field_placeholder": "Chọn loại tiền...",
                    }

                elif "CHỌN LOẠI ĐẶT" in part or "3. Chọn loại đặt" in part:
                    self.awaiting_cdtd_coin = False
                    self.awaiting_cdtd_bet_type = True
                    markup = {
                        "keyboard": [
                            [{"text": "Ai là quán quân"}],
                            [{"text": "Ai không là quán quân"}]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                        "input_field_placeholder": "Chọn loại đặt...",
                    }

                elif "4. Đặt vào bao nhiêu người:" in part:
                    self.awaiting_cdtd_coin = False
                    self.awaiting_cdtd_bet_type = False
                    self.awaiting_cdtd_logic = False
                    markup = {
                        "keyboard": [
                            [{"text": "1"}, {"text": "2"}, {"text": "3"}],
                            [{"text": "4"}, {"text": "5"}, {"text": "6"}]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                        "input_field_placeholder": "Chọn số người (1-6)...",
                    }

                elif "✅ Đã chọn Logic" in part:
                    self.awaiting_cdtd_coin = False
                    self.awaiting_cdtd_bet_type = False
                    self.awaiting_cdtd_logic = False
                    # Sau khi chọn Logic CDTD xong, trả Reply Keyboard về Menu chính.
                    markup = home_markup(self.chat_id)

                elif "Đặt vào bao nhiêu người" in part or ("Nhập số" in part and "cược mỗi ván" in part):
                    self.awaiting_cdtd_coin = False
                    self.awaiting_cdtd_bet_type = False
                    self.awaiting_cdtd_logic = False
                    markup = {"remove_keyboard": True}

            self._enqueue_message(payload_text, markup)

        while not self.stop_event.is_set() or not self.q.empty():
            try:
                line = self.q.get(timeout=0.10)
                buf.append(line)
                now = time.time()

                if first_line_ts is None:
                    first_line_ts = now

                last_data_ts = now

            except queue.Empty:
                pass

            now = time.time()

            if buf and (
                (last_data_ts is not None and now - last_data_ts >= FLUSH_INTERVAL)
                or (first_line_ts is not None and now - first_line_ts >= 1.5)
                or (self.stop_event.is_set() and self.q.empty())
            ):
                text = clean_output("".join(buf))
                buf = []
                first_line_ts = None
                last_data_ts = None

                for part in chunks(text):
                    if self.key == "vth" and "🚀 ĐANG KHỞI ĐỘNG VTH..." in part:
                        before, _ = part.split("🚀 ĐANG KHỞI ĐỘNG VTH...", 1)
                        before = before.strip()

                        if before:
                            _send_part(before)

                        _send_part("🚀 ĐANG KHỞI ĐỘNG VTH...")

                    elif self.key == "cdtd" and "🚀 ĐANG KHỞI ĐỘNG CDTD..." in part:
                        before, _ = part.split("🚀 ĐANG KHỞI ĐỘNG CDTD...", 1)
                        before = before.strip()

                        if before:
                            _send_part(before)

                        _send_part("🚀 ĐANG KHỞI ĐỘNG CDTD...")

                    elif self.key == "cdtd" and "4. Đặt vào bao nhiêu người:" in part:
                        before, _ = part.split("4. Đặt vào bao nhiêu người:", 1)
                        before = before.strip()

                        if before:
                            _send_part(before)

                        _send_part("4. Đặt vào bao nhiêu người:")

                    else:
                        _send_part(part)

        if buf:
            text = clean_output("".join(buf))

            for part in chunks(text):
                if self.key == "vth" and "🚀 ĐANG KHỞI ĐỘNG VTH..." in part:
                    before, _ = part.split("🚀 ĐANG KHỞI ĐỘNG VTH...", 1)
                    before = before.strip()

                    if before:
                        _send_part(before)

                    _send_part("🚀 ĐANG KHỞI ĐỘNG VTH...")

                elif self.key == "cdtd" and "🚀 ĐANG KHỞI ĐỘNG CDTD..." in part:
                    before, _ = part.split("🚀 ĐANG KHỞI ĐỘNG CDTD...", 1)
                    before = before.strip()

                    if before:
                        _send_part(before)

                    _send_part("🚀 ĐANG KHỞI ĐỘNG CDTD...")

                elif self.key == "cdtd" and "4. Đặt vào bao nhiêu người:" in part:
                    before, _ = part.split("4. Đặt vào bao nhiêu người:", 1)
                    before = before.strip()

                    if before:
                        _send_part(before)

                    _send_part("4. Đặt vào bao nhiêu người:")

                else:
                    _send_part(part)

        try:
            code = self.proc.wait(timeout=3)
        except Exception:
            code = self.proc.poll()

        stderr_text = "\n".join(self._stderr_lines[-80:]).strip()
        abnormal = (not self._expected_stop) and (code not in (0, None))
        if abnormal:
            _notify_admin_code_error(
                f"Worker {self.name} kết thúc bất thường, exit_code={code}",
                chat_id=self.chat_id,
                tool_key=self.key,
                traceback_text=stderr_text or f"Worker kết thúc với exit code {code}, không có stderr.",
            )

        self.sender_stop_event.set()
        if self.sender_thread is not None and self.sender_thread is not threading.current_thread():
            try:
                self.sender_thread.join(timeout=2)
            except Exception:
                pass

        with SESSIONS_LOCK:
            if SESSIONS.get(self.chat_id) is self:
                SESSIONS.pop(self.chat_id, None)

        try:
            Path(self.input_file).unlink(missing_ok=True)
        except Exception:
            pass

        if not STOP_ALL.is_set():

            try:
                if self._end_message:
                    send_message(self.chat_id, self._end_message, self._end_markup)
                elif abnormal:
                    send_message(self.chat_id, ("⚠️ BOT ĐANG LỖI." if self.key == "lotto" else f"⚠️lỗi."))
                else:
                    send_message(self.chat_id, ("✅ Công cụ đã kết thúc." if self.key == "lotto" else f"✅ {self.name} đã kết thúc."), home_markup(self.chat_id))
            except Exception as exc:
                _notify_admin_code_error(
                    "Gửi thông báo kết thúc worker lỗi",
                    exc,
                    chat_id=self.chat_id,
                    tool_key=self.key,
                )

    def set_end_message(self, text, markup=None):
        self._end_message = str(text or "").strip() or None
        self._end_markup = markup

    def send_input(self, text):
        if not self.running:
            return False
        try:
            if self.proc is None or self.proc.stdin is None:
                return False
            value = str(text).replace("\x00", "").replace("\r", "").strip()

            payload = value + "\n"
            with self._stdin_lock:
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def stop(self):

        self._expected_stop = True
        self.sender_stop_event.set()
        proc = self.proc
        if not proc:
            self.stop_event.set()
            return

        if proc.poll() is not None:
            self.stop_event.set()
            return

        self.stop_event.set()

        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        end = time.time() + 3.0
        while time.time() < end and proc.poll() is None:
            time.sleep(0.1)

        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass


def get_session(chat_id):
    with SESSIONS_LOCK:
        return SESSIONS.get(chat_id)


def start_tool(chat_id, key):
    # Đã chọn tool thì không còn ở chế độ nhập Key.
    # Xóa pending cũ để link game/tài khoản không bị bắt nhầm bởi VIP handler.
    _clear_user_auth_pending(chat_id)

    ban = _bot_banned_message(chat_id)
    if ban:
        send_message(chat_id, ban)
        return

    # Tất cả tài khoản, kể cả admin, đều phải có key còn hiệu lực để chạy tool.
    # Không cho phép bỏ qua kiểm tra key bằng quyền admin.
    valid, rec = _bot_auth_valid(chat_id)
    if not valid or not rec:
        if str(chat_id) in BOT_EXPIRED_USERS:
            send_message(
                chat_id,
                "⚠️ VUI LÒNG XÁC THỰC KEY:\n\n"
                "https://vuotnhanh.com/Zdg7\n\n"
                "📌 Sau khi có Key, gõ:\n"
                "/login KEY_CUA_BAN\n\n"
                "⏰ Key có hiệu lực trong 24h",
                _auth_menu_markup(chat_id),
            )
        else:
            send_message(
                chat_id,
                "🔐 Bạn chưa có key còn hiệu lực.\n\n"
                "Vui lòng xác thực Key FREE hoặc Key VIP trước khi chạy tool.",
                _auth_menu_markup(chat_id),
            )
        return

    if key == "xworld" and str(rec.get("key_type", "FREE")).upper() != "VIP":
        send_message(chat_id, "🔒 CANH CODE chỉ dành cho KEY VIP.\n\nGõ /KEY_VIP để nhập key VIP.", _auth_menu_markup(chat_id))
        return

    if key not in TOOLS:
        send_message(chat_id, "❌ Tool không hợp lệ.", home_markup(chat_id))
        return

    old = get_session(chat_id)
    if old and old.running:
        send_message(chat_id,
                     f"⚠️ Bạn đang chạy {old.name}.\n🛑 Hãy Dừng trước.",
                     home_markup(chat_id))
        return

    session = ToolSession(chat_id, key)
    with SESSIONS_LOCK:
        SESSIONS[chat_id] = session
    try:
        session.start()
        # Chọn tool xong -> tự động trả Reply Keyboard về menu chính.
        # Nút "Quay lại" không còn nằm trong menu chọn tool.
        send_message(
            chat_id,
            f"✅ Đã chọn {session.name}.",
            home_markup(chat_id),
        )
    except Exception as exc:
        with SESSIONS_LOCK:
            if SESSIONS.get(chat_id) is session:
                SESSIONS.pop(chat_id, None)
        _admin_only_exception_log(chat_id, "Lỗi khởi động tool", exc)
        send_message(chat_id, "❌ Không thể khởi động tool. Vui lòng thử lại.", home_markup(chat_id))
        return



def stop_menu_markup():
    return {
        "keyboard": [
            [{"text": "⚡ Dừng VTH"}, {"text": "🎲 Dừng LOTTO"}],
            [{"text": "🏎️ Dừng CDTD"}, {"text": "🎯 Dừng CANH CODE"}],
            [{"text": "🃏 Dừng HILO"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Chọn tool muốn dừng...",
    }


def stop_tool(chat_id, key=None):
    session = get_session(chat_id)
    if not session or not session.running:
        send_message(chat_id, "ℹ️ Không có tool đang chạy.", home_markup(chat_id))
        return

    if key is None:
        send_message(
            chat_id,
            "🛑 CHỌN TOOL MUỐN DỪNG\n\n"
            f"🟢 Đang chạy: {session.name}\n\n"
            "Hãy chọn tool muốn dừng:",
            stop_menu_markup(),
        )
        return

    if key != session.key:
        send_message(
            chat_id,
            f"ℹ️ {TOOLS.get(key, (key, ''))[0]} hiện không chạy.\n\n"
            f"🟢 Tool đang chạy: {session.name}",
            home_markup(chat_id),
        )
        return

    send_message(chat_id, f"⏳ ĐANG DỪNG {session.name}...", None)
    session.stop()

    # Chỉ thông báo đã dừng sau khi tiến trình worker thực sự kết thúc.
    with SESSIONS_LOCK:
        if SESSIONS.get(chat_id) is session:
            SESSIONS.pop(chat_id, None)
    send_message(chat_id, f"✅ ĐÃ DỪNG {session.name}.", home_markup(chat_id))


def show_status(chat_id):
    session = get_session(chat_id)
    if not session or not session.running:
        send_message(chat_id, "⚪ Không có tool nào đang chạy.", home_markup(chat_id))
        return
    send_message(
        chat_id,
        f"🟢 Đang chạy\n"
        f"{session.name}",
        None,
    )


def handle_message(msg):
    global _CURRENT_CHAT_ID
    chat_id = (msg.get("chat") or {}).get("id")
    _CURRENT_CHAT_ID = chat_id
    text = msg.get("text")
    if chat_id is None or text is None:
        return

    is_admin = _is_bot_admin(chat_id)
    cmd = text.strip()
    upper_cmd = cmd.upper()

    # Lệnh/nút liên quan quyền Admin: nếu người gửi không phải Admin thì
    # bỏ qua hoàn toàn, không gửi tin nhắn báo lỗi hay thông báo từ chối.
    if not is_admin and _is_admin_only_message(cmd):
        return

    ban = _bot_banned_message(chat_id)
    if ban and not is_admin:
        send_message(chat_id, ban)
        return

    # Chỉ ghi nhận chat riêng; tin nhắn CHAT Admin sẽ được phát qua DM.
    chat_info = msg.get("chat") or {}
    if str(chat_info.get("type", "private")).lower() == "private":
        _register_chat_user(chat_id)

    # ================= CHAT CHỈ ADMIN =================
    # Bước 1: Admin phải chọn 0 (không dừng) hoặc 1 (dừng tất cả tool).
    if is_admin and str(chat_id) in BOT_CHAT_WAIT_STOP:
        if cmd.lower() in {"/cancel", "/huy", "hủy", "↩️ quay lại"}:
            BOT_CHAT_WAIT_STOP.discard(str(chat_id))
            BOT_CHAT_COMPOSING.discard(str(chat_id))
            send_message(chat_id, "✅ Đã hủy CHAT.", home_markup(chat_id))
            return

        if cmd == "0":
            BOT_CHAT_WAIT_STOP.discard(str(chat_id))
            BOT_CHAT_COMPOSING.add(str(chat_id))
            requested = 0
            choice_text = "✅ Giữ nguyên: không dừng các tool đang chạy."
        elif cmd == "1":
            BOT_CHAT_WAIT_STOP.discard(str(chat_id))
            requested = _admin_stop_all_running_tools()
            BOT_CHAT_COMPOSING.add(str(chat_id))
            choice_text = (
                "🛑 Đã gửi lệnh dừng tới "
                f"{requested} phiên tool đang chạy."
            )
        else:
            send_message(
                chat_id,
                "⚠️ Vui lòng chỉ nhập 0 hoặc 1.\n\n"
                "0 = Không dừng tool\n"
                "1 = Dừng tất cả tool đang chạy\n\n"
                "Gõ /cancel để hủy CHAT.",
                home_markup(chat_id),
            )
            return

        recipients_count = len(_chat_broadcast_recipients() - {str(chat_id)})
        send_message(
            chat_id,
            f"{choice_text}\n\n"
            "💬 Bây giờ hãy soạn và gửi tin nhắn bất kỳ để phát tới các tài khoản đã tương tác với bot.\n"
            f"👥 Số tài khoản trong danh sách: {recipients_count}\n\n"
            "❌ Gõ /cancel để hủy soạn tin.",
            home_markup(chat_id),
        )
        return

    # Bước 2: Nhận nội dung Admin soạn và phát tới người dùng.
    if is_admin and str(chat_id) in BOT_CHAT_COMPOSING:
        if cmd.lower() in {"/cancel", "/huy", "hủy", "↩️ quay lại"}:
            BOT_CHAT_COMPOSING.discard(str(chat_id))
            send_message(chat_id, "✅ Đã hủy soạn CHAT.", home_markup(chat_id))
            return
        # Khi đang ở chế độ soạn CHAT, mọi nội dung đều được xem là tin nhắn
        # cần phát đi (kể cả nội dung bắt đầu bằng dấu /), trừ lệnh hủy ở trên.
        BOT_CHAT_COMPOSING.discard(str(chat_id))
        _broadcast_admin_chat(chat_id, cmd)
        return

    if cmd == "📋 DANH SÁCH ĐƠN":
        if not is_admin:
            # Chặn ở phía máy chủ, kể cả khi người dùng tự gõ tên nút.
            return
        _show_admin_orders(chat_id, page=0)
        return

    if cmd == "💬 CHAT":
        if not is_admin:
            send_message(
                chat_id,
                "⛔ CHAT là chức năng riêng của Admin.\n\n"
                "Bạn vẫn có thể xem thông báo CHAT do Admin gửi tới.",
                home_markup(chat_id),
            )
            return
        BOT_CHAT_COMPOSING.discard(str(chat_id))
        BOT_CHAT_WAIT_STOP.add(str(chat_id))
        send_message(
            chat_id,
            "💬 CHAT ADMIN\n\n"
            "Admin có muốn dừng tất cả tool mà người dùng đang chạy không?\n\n"
            "Nhập 0️⃣ để KHÔNG dừng tool.\n"
            "Nhập 1️⃣ để DỪNG tất cả tool đang chạy.\n\n"
            "Sau khi chọn, Admin sẽ soạn tin nhắn để gửi tới các tài khoản đã tương tác với bot.\n"
            "❌ Gõ /cancel để hủy.",
            home_markup(chat_id),
        )
        return

    # ================= MUA KEY =================
    if cmd == "🛒 MUA KEY VIP":
        _show_buy_menu(chat_id)
        return

    if cmd.lower() in {"/mua", "/muakey", "/buy"}:
        _show_buy_menu(chat_id)
        return

    if cmd in {"↩️ QUAY LẠI", "/huy_mua", "/cancel"}:
        pending_purchase = BOT_PENDING.get(str(chat_id), {})
        if isinstance(pending_purchase, dict) and pending_purchase.get("mode") == "purchase_quantity":
            BOT_PENDING.pop(str(chat_id), None)
            _save_bot_pending()
        if cmd == "↩️ QUAY LẠI":
            send_message(chat_id, _auth_menu_text(chat_id), home_markup(chat_id))
        else:
            send_message(chat_id, "✅ Đã hủy thao tác mua key.", home_markup(chat_id))
        return

    # Nút sản phẩm chỉ chọn gói; bot sẽ hỏi số lượng ở tin nhắn kế tiếp.
    package_code_from_button = _purchase_code_from_button(cmd)
    if package_code_from_button:
        _start_purchase_quantity(chat_id, package_code_from_button)
        return

    # Sau khi chọn gói, chờ người dùng nhập số lượng key cần mua.
    pending_purchase = BOT_PENDING.get(str(chat_id), {})
    if isinstance(pending_purchase, dict) and pending_purchase.get("mode") == "purchase_quantity" and not cmd.startswith("/"):
        if not re.fullmatch(r"[0-9]+", cmd):
            send_message(chat_id, "⚠️ Vui lòng chỉ nhập số lượng bằng chữ số. Ví dụ: 3")
            return
        quantity = int(cmd)
        if quantity < 1 or quantity > MAX_PURCHASE_QUANTITY:
            send_message(chat_id, f"⚠️ Số lượng phải từ 1 đến {MAX_PURCHASE_QUANTITY} key. Vui lòng nhập lại.")
            return

        package_code = str(pending_purchase.get("package_code") or "")
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        order_id, order = _create_key_purchase_order(chat_id, package_code, quantity)
        user = msg.get("from") if isinstance(msg, dict) else {}
        _handle_purchase_order_result(chat_id, order_id, order, user or {})
        return

    # ================= ADMIN DUYỆT ĐƠN =================
    if upper_cmd.startswith("/DUYET"):
        if not is_admin:
            send_message(chat_id, "⛔ Bạn không có quyền duyệt đơn.", home_markup(chat_id))
            return
        parts = cmd.split()
        if len(parts) < 2:
            send_message(chat_id, "🛑 Cú pháp: /duyet MA_DON")
            return
        order_id = parts[1].strip().upper()
        ok, result = _issue_purchase_order(order_id, chat_id)
        if not ok:
            order_fail = BOT_ORDERS.get(order_id, {})
            target_fail = str(order_fail.get("telegram_id", ""))
            if str(result).startswith("SOLD_OUT|"):
                sold_msg = str(result).split("|", 1)[1].strip()
                send_message(
                    chat_id,
                    f"❌ DUYỆT ĐƠN THẤT BẠI\n\n📦 {sold_msg}\n\n⏳ Đơn vẫn được giữ ở trạng thái chờ duyệt để Admin xử lý khi có thêm Key.",
                    home_markup(chat_id),
                )
                try:
                    send_message(
                        int(target_fail),
                        "⚠️ ĐƠN HÀNG CHƯA THỂ CẤP KEY\n\n"
                        f"📦 {sold_msg}\n"
                        f"🧾 Đơn: {order_id}\n\n"
                        "⏳ Hiện chưa có Key phù hợp trong kho. Đơn của bạn vẫn được giữ lại và sẽ được xử lý khi có Key.",
                        home_markup(int(target_fail)),
                    )
                except Exception:
                    pass
            else:
                send_message(chat_id, f"❌ DUYỆT ĐƠN THẤT BẠI\n\n{result}", home_markup(chat_id))
            return
        order = BOT_ORDERS.get(order_id, {})
        target_id = str(order.get("telegram_id", ""))
        order_keys = _get_order_keys(order)
        keys_plain = "\n".join(order_keys) if order_keys else str(result)
        keys_html = "\n".join(f"<code>{html.escape(key)}</code>" for key in order_keys) if order_keys else f"<code>{html.escape(str(result))}</code>"
        send_message(
            chat_id,
            "✅ DUYỆT ĐƠN THÀNH CÔNG\n\n"
            f"• Đơn: {order_id}\n"
            f"• User ID: {target_id}\n"
            f"• Gói: {order.get('label', '-') }\n"
            f"• Số lượng: {int(order.get('quantity') or 1)}\n"
            f"• Danh sách Key:\n{keys_plain}",
            home_markup(chat_id),
        )
        try:
            send_message(
                int(target_id),
                "🎉 THANH TOÁN / ĐƠN HÀNG ĐÃ ĐƯỢC DUYỆT!\n\n"
                f"🔑 KEY VIP:\n{keys_html}\n"
                f"📦 Gói: {order.get('label', '-') }\n"
                f"🧾 Đơn: {order_id}\n\n"
                "⚠️ Key chưa được kích hoạt.\n"
                "👉 Gõ /KEY_VIP rồi nhập Key muốn kích hoạt.",
                None,
                "HTML",
            )
        except Exception:
            pass
        return

    if upper_cmd.startswith("/TUCHOI"):
        if not is_admin:
            send_message(chat_id, "⛔ Bạn không có quyền từ chối đơn.", home_markup(chat_id))
            return
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "🛑 Cú pháp: /tuchoi MA_DON")
            return
        order_id = parts[1].strip().upper()
        with BOT_ORDER_LOCK:
            order = BOT_ORDERS.get(order_id)
            if not isinstance(order, dict):
                send_message(chat_id, "❌ Không tìm thấy đơn.")
                return
            if str(order.get("status", "")).upper() == "COMPLETED":
                send_message(chat_id, "❌ Đơn đã hoàn tất, không thể từ chối.")
                return
            order["status"] = "REJECTED"
            order["rejected_by"] = str(chat_id)
            order["rejected_at"] = datetime.now().isoformat()
            _save_bot_orders()
        send_message(chat_id, f"✅ Đã từ chối đơn {order_id}.", home_markup(chat_id))
        try:
            send_message(int(order["telegram_id"]),
                         f"❌ Đơn {order_id} đã bị từ chối.")
        except Exception:
            pass
        return

    if upper_cmd.startswith("/DONHANG"):
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "🛑 Cú pháp: /donhang MA_DON")
            return
        order_id = parts[1].strip().upper()
        order = BOT_ORDERS.get(order_id)
        if not isinstance(order, dict):
            send_message(chat_id, "❌ Không tìm thấy đơn.")
        elif not is_admin and str(order.get("telegram_id")) != str(chat_id):
            return
        else:
            status = str(order.get("status", "")).upper()
            if not is_admin and str(order.get("telegram_id")) == str(chat_id) and status == "WAITING_PAYMENT":
                _send_payment_instructions(chat_id, order)
            else:
                send_message(chat_id, _format_order(order, show_key=is_admin or status == "COMPLETED"), home_markup(chat_id))
        return

    # ================= ADMIN COMMANDS MỚI =================
    if upper_cmd.startswith("/MO_BAN"):
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền MỞ BAN.", home_markup(chat_id))
            return
        parts = cmd.split(maxsplit=1)
        if len(parts) == 1 or not parts[1].strip():
            BOT_PENDING[str(chat_id)] = {"mode": "admin_mo_ban", "created_at": _now_ts()}
            _save_bot_pending()
            send_message(chat_id, "🔓 Gõ TELEGRAM_ID cần mở BAN.", home_markup(chat_id))
            return
        target_id = parts[1].strip()
        if not re.fullmatch(r"-?\d+", target_id):
            send_message(chat_id, "❌ TELEGRAM_ID không hợp lệ.", home_markup(chat_id))
            return
        existed = str(target_id) in BOT_BANS
        BOT_BANS.pop(str(target_id), None)
        _save_json_file(BOT_BAN_FILE, BOT_BANS)
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        send_message(chat_id, (f"✅ ĐÃ MỞ BAN Telegram ID: {target_id}" if existed else f"ℹ️ Telegram ID {target_id} hiện không bị BAN."), home_markup(chat_id))
        try:
            send_message(int(target_id), "✅ Bạn đã được MỞ BAN.", home_markup(int(target_id)))
        except Exception:
            pass
        return

    if upper_cmd == "/VIP":
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền THU HỒI KEY.", home_markup(chat_id))
            return
        BOT_PENDING[str(chat_id)] = {"mode": "admin_revoke_vip", "created_at": _now_ts()}
        _save_bot_pending()
        send_message(chat_id, "🔑 THU HỒI KEY VIP\n\n👉 Nhập TELEGRAM_ID cần thu hồi key VIP.", home_markup(chat_id))
        return

    pending_admin = BOT_PENDING.get(str(chat_id), {})
    admin_mode = pending_admin.get("mode")
    if _is_bot_admin(chat_id) and admin_mode in ("admin_mo_ban", "admin_revoke_vip") and not cmd.startswith("/"):
        target_id = cmd.strip()
        if not re.fullmatch(r"-?\d+", target_id):
            send_message(chat_id, "❌ TELEGRAM_ID không hợp lệ. Hãy nhập lại.", home_markup(chat_id))
            return
        if admin_mode == "admin_mo_ban":
            existed = str(target_id) in BOT_BANS
            BOT_BANS.pop(str(target_id), None)
            _save_json_file(BOT_BAN_FILE, BOT_BANS)
            BOT_PENDING.pop(str(chat_id), None)
            _save_bot_pending()
            send_message(chat_id, (f"✅ ĐÃ MỞ BAN Telegram ID: {target_id}" if existed else f"ℹ️ Telegram ID {target_id} hiện không bị BAN."), home_markup(chat_id))
            try:
                send_message(int(target_id), "✅ Bạn đã được MỞ BAN.", home_markup(int(target_id)))
            except Exception:
                pass
            return
        key_type = "VIP"
        server_ok, server_msg = _revoke_vip_on_server(target_id)
        if not server_ok:
            send_message(chat_id, f"❌ Không thể thu hồi VIP.\n{server_msg}", home_markup(chat_id))
            return
        # Server xác nhận đã tìm và thu hồi VIP; không phụ thuộc cache BOT_AUTH
        # còn hay mất sau khi bot restart. FREE ở server không bị thu hồi.
        _clear_bot_auth_for_reauth(target_id)
        _stop_tool_silently(target_id)
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        send_message(chat_id, f"✅ ĐÃ THU HỒI KEY {key_type} THÀNH CÔNG\n\n👤 Telegram ID: {target_id}", home_markup(chat_id))
        _send_key_entry_menu_after_revoke(target_id)
        return

    # Reply Keyboard: bấm CHỌN TOOL mới mở danh sách 5 tool sau khi đã xác thực Key.
    if cmd == "🧰 CHỌN TOOL":
        valid, rec = _bot_auth_valid(chat_id)

        # Nếu key vừa hết hạn, yêu cầu xác thực lại bằng đúng link người dùng cung cấp.
        # Kiểm tra này đứng trước nhánh admin để key hết hạn không thể bỏ qua xác thực.
        if rec and rec.get("__expired__"):
            with BOT_AUTH_LOCK:
                BOT_EXPIRED_USERS[str(chat_id)] = str(rec.get("key_type", "FREE")).upper()
                _save_json_file(BOT_EXPIRED_USERS_FILE, BOT_EXPIRED_USERS)

        if str(chat_id) in BOT_EXPIRED_USERS:
            send_message(
                chat_id,
                "⚠️ VUI LÒNG XÁC THỰC KEY:\n\n"
                "https://vuotnhanh.com/Zdg7\n\n"
                "📌 Sau khi có Key, gõ:\n"
                "/login KEY_CUA_BAN\n\n"
                "⏰ Key có hiệu lực trong 24h",
                _auth_menu_markup(chat_id),
            )
            return

        if not valid or not rec:
            send_message(
                chat_id,
                "🔐 Bạn chưa có key còn hiệu lực.\n\n"
                "Vui lòng xác thực Key FREE hoặc Key VIP trước khi chọn tool.",
                _auth_menu_markup(chat_id),
            )
            return
        send_message(chat_id, "Hãy chọn tool muốn sử dụng:", tool_menu_markup())
        return

    if cmd == "↩️ Quay lại":
        send_message(chat_id, _auth_menu_text(chat_id), home_markup(chat_id))
        return

    # Các nút 5 tool trong Reply Keyboard.
    tool_buttons = {
        "⚡ VTH": "vth",
        "🎲 LOTTO": "lotto",
        "🏎️ CDTD": "cdtd",
        "🎯 CANH CODE": "xworld",
        "🃏 HILO": "hilo",
    }
    if cmd in tool_buttons:
        start_tool(chat_id, tool_buttons[cmd])
        return

    if cmd == "📊 Trạng thái":
        show_status(chat_id)
        return

    if cmd == "⏹️ Dừng":
        stop_tool(chat_id)
        return

    # Các nút dừng tool trong Reply Keyboard.
    stop_buttons = {
        "⚡ Dừng VTH": "vth",
        "🎲 Dừng LOTTO": "lotto",
        "🏎️ Dừng CDTD": "cdtd",
        "🎯 Dừng CANH CODE": "xworld",
        "🃏 Dừng HILO": "hilo",
    }
    if cmd in stop_buttons:
        stop_tool(chat_id, stop_buttons[cmd])
        return

    if cmd == "🆓 NHẬP KEY FREE":
        # Chuyển dứt điểm VIP -> FREE: hủy pending VIP và xóa bộ đếm VIP cũ
        # để các tin nhắn/Key FREE sau đó tuyệt đối không bị VIP handler nhận.
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        _reset_vip_spam(chat_id)
        BOT_PENDING[str(chat_id)] = {"mode": "free_select", "created_at": _now_ts()}
        _save_bot_pending()
        _handle_free_key_request(chat_id)
        return

    if cmd == "👑 NHẬP KEY VIP":
        # Chuyển thẳng FREE -> VIP: pending FREE bị hủy, chỉ nhận Key VIP.
        BOT_PENDING[str(chat_id)] = {"mode": "vip", "created_at": _now_ts()}
        _save_bot_pending()
        send_message(chat_id,
                     "👑 NHẬP KEY VIP\n\n"
                     "Hãy gửi Key VIP ở tin nhắn tiếp theo.\n"
                     "🔐 Key sẽ được xác thực.", home_markup(chat_id))
        return

    # ================= ADMIN REPLY KEYBOARD =================
    if cmd == "🔓 MỞ BAN":
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền MỞ BAN.", home_markup(chat_id))
            return
        BOT_PENDING[str(chat_id)] = {"mode": "admin_mo_ban", "created_at": _now_ts()}
        send_message(
            chat_id,
            "🔓 MỞ BAN\n\n"
            "👉 Gõ /mo_ban rồi nhập TELEGRAM_ID.\n"
            "Ví dụ: /mo_ban 1301501405",
            home_markup(chat_id),
        )
        return

    if cmd == "🔑 THU HỒI KEY":
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền THU HỒI KEY.", home_markup(chat_id))
            return
        BOT_PENDING[str(chat_id)] = {"mode": "admin_revoke_menu", "created_at": _now_ts()}
        send_message(
            chat_id,
            "🔑 THU HỒI KEY VIP\n\n"
            "• /vip → Thu hồi KEY VIP\n\n"
            "Sau đó gửi TELEGRAM_ID của người cần thu hồi.",
            home_markup(chat_id),
        )
        return

    # Admin luôn được mở menu quản trị, kể cả khi chưa kích hoạt Key VIP.
    # Trước đây /start ở trạng thái chưa có Key lại gửi _auth_menu_markup(),
    # khiến nút 📋 DANH SÁCH ĐƠN không xuất hiện dù tài khoản là Admin.
    if cmd in ("/start", "/menu", "/tools"):
        # Server là nguồn sự thật; tự khôi phục key theo Telegram ID nếu bot vừa restart.
        _restore_telegram_auth_from_server(chat_id, force=True)
        valid, rec = _bot_auth_valid(chat_id)
        if is_admin:
            _clear_user_auth_pending(chat_id)
            send_message(chat_id, _auth_menu_text(chat_id), home_markup(chat_id))
        elif valid and rec:
            # Đã có Key hợp lệ thì /start chỉ mở menu, không giữ mode nhập Key cũ.
            _clear_user_auth_pending(chat_id)
            send_message(chat_id, _auth_menu_text(chat_id), home_markup(chat_id))
        else:
            send_message(chat_id, _auth_menu_text(chat_id), _auth_menu_markup(chat_id))
        return

    # Lệnh dự phòng để Admin mở danh sách đơn ngay cả khi bàn phím Telegram cũ.
    if cmd.lower() in {"/orders", "/donhang", "/danhsachdon"}:
        if not is_admin:
            return
        _show_admin_orders(chat_id, page=0)
        return

    if cmd.lower().startswith(("/revoke", "/revoke_key")):
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền thu hồi Key.")
            return
        parts = cmd.split()
        if len(parts) < 2:
            send_message(chat_id,
                         "🛑 THU HỒI KEY VIP\n\n"
                         "Cú pháp:\n"
                         "/revoke TELEGRAM_ID\n"
                         "/revoke TELEGRAM_ID VIP")
            return
        target_id = parts[1].strip()
        if not re.fullmatch(r"-?\d+", target_id):
            send_message(chat_id, "❌ Telegram ID không hợp lệ.")
            return
        requested_type = parts[2].upper() if len(parts) >= 3 else "VIP"
        if requested_type != "VIP":
            send_message(chat_id, "❌ Chức năng này chỉ hỗ trợ THU HỒI KEY VIP.")
            return
        server_ok, server_msg = _revoke_vip_on_server(target_id)
        if not server_ok:
            send_message(chat_id, f"❌ Không thể thu hồi VIP.\n{server_msg}")
            return

        # Server là nguồn sự thật; không đòi BOT_AUTH local phải còn VIP.
        _clear_bot_auth_for_reauth(target_id)
        _stop_tool_silently(target_id)
        send_message(chat_id,
                     "✅ ĐÃ THU HỒI KEY VIP THÀNH CÔNG\n\n"
                     f"👤 Telegram ID: {target_id}")
        _send_key_entry_menu_after_revoke(target_id)
        return

    if cmd.lower().startswith('/ban') and not cmd.lower().startswith(('/banlist', '/unban')):
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền BAN người dùng.")
            return
        parts = cmd.split()
        if len(parts) < 2 or not re.fullmatch(r"-?\d+", parts[1].strip()):
            send_message(
                chat_id,
                "🛑 BAN NGƯỜI DÙNG\n\n"
                "Cú pháp:\n"
                "/ban TELEGRAM_ID\n"
                "/ban TELEGRAM_ID 24\n\n"
                "Nếu không nhập số giờ, mặc định 24 giờ."
            )
            return
        target_id = parts[1].strip()
        try:
            ban_hours = float(parts[2]) if len(parts) >= 3 else 24.0
        except ValueError:
            send_message(chat_id, "❌ Số giờ BAN không hợp lệ.")
            return
        if ban_hours <= 0:
            send_message(chat_id, "❌ Số giờ BAN phải lớn hơn 0.")
            return
        if ban_hours > 720:
            send_message(chat_id, "❌ Thời gian BAN tối đa là 720 giờ.")
            return

        target_key = str(target_id)
        now = _now_ts()
        until = now + ban_hours * 3600
        BOT_BANS[target_key] = {
            "until": until,
            "reason": f"Admin BAN bởi {chat_id}",
            "created_at": now,
            "admin_id": str(chat_id),
            "manual": True,
        }
        _save_json_file(BOT_BAN_FILE, BOT_BANS)

        # Xóa auth local và dừng tool đang chạy.
        try:
            _revoke_bot_auth(target_key)
        except Exception:
            pass
        try:
            running = get_session(int(target_id))
            if running and running.running:
                stop_tool(int(target_id), running.key)
        except Exception:
            pass

        duration_text = _format_duration(ban_hours * 3600)
        send_message(
            chat_id,
            "✅ ĐÃ BAN THÀNH CÔNG\n\n"
            f"👤 Telegram ID: {target_id}\n"
            f"⏰ Thời gian: {duration_text}\n"
            f"🔒 Người dùng sẽ không thể sử dụng bot trong thời gian này."
        )
        try:
            send_message(
                int(target_id),
                "⛔ BẠN ĐÃ BỊ ADMIN BAN\n\n"
                f"⏰ Thời gian: {duration_text}\n"
                "🔒 Bạn không thể sử dụng bot trong thời gian bị khóa."
            )
        except Exception:
            pass
        return

    # ADMIN: /unban <telegram_id>
    if cmd.lower().startswith('/unban'):
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền GỠ BAN người dùng.")
            return
        parts = cmd.split()
        if len(parts) < 2 or not re.fullmatch(r"-?\d+", parts[1].strip()):
            send_message(chat_id, "🛑 Cú pháp: /unban TELEGRAM_ID")
            return
        target_id = parts[1].strip()
        existed = str(target_id) in BOT_BANS
        BOT_BANS.pop(str(target_id), None)
        _save_json_file(BOT_BAN_FILE, BOT_BANS)
        if existed:
            send_message(chat_id, f"✅ ĐÃ GỠ BAN Telegram ID: {target_id}")
            try:
                send_message(int(target_id), "✅ Bạn đã được GỠ BAN.\n🔓 Có thể sử dụng bot trở lại.", home_markup(int(target_id)))
            except Exception:
                pass
        else:
            send_message(chat_id, f"ℹ️ Telegram ID {target_id} hiện không bị BAN.")
        return

    # ADMIN: /banlist — xem danh sách người đang bị BAN
    if cmd.lower() == '/banlist':
        if not _is_bot_admin(chat_id):
            send_message(chat_id, "⛔ Bạn không có quyền xem danh sách BAN.")
            return
        active = []
        now = _now_ts()
        for tid, rec in list(BOT_BANS.items()):
            if not isinstance(rec, dict):
                continue
            remaining = float(rec.get('until', 0) or 0) - now
            if remaining > 0:
                active.append((tid, remaining, rec.get('reason', '')))
        if not active:
            send_message(chat_id, "📋 DANH SÁCH BAN\n\n✅ Hiện không có người dùng nào đang bị BAN.")
            return
        lines = ["📋 DANH SÁCH BAN", ""]
        for tid, remaining, reason in active:
            lines.append(f"👤 {tid}\n⏰ Còn: {_format_duration(remaining)}\n📝 {reason}\n")
        send_message(chat_id, "\n".join(lines).strip())
        return

    if cmd.lower().startswith("/login"):
      
        _reset_vip_spam(chat_id)
        parts = cmd.split(maxsplit=1)
        pending = _bot_pending_record(chat_id)

        if len(parts) < 2 or not parts[1].strip():
            if pending.get("mode") == "free" and pending.get("key"):
                pending["mode"] = "free_login"
                BOT_PENDING[str(chat_id)] = pending
                _save_bot_pending()
            else:
                BOT_PENDING.pop(str(chat_id), None)
                BOT_PENDING[str(chat_id)] = {"mode": "free_login", "created_at": _now_ts()}
                _save_bot_pending()
            send_message(chat_id, "🔑 Hãy gửi key FREE ngay tin nhắn tiếp theo để xác thực.")
            return

        user_key = parts[1].strip()
        expected_key = str(pending.get("key", "")).strip()

        if pending.get("mode") not in ("free", "free_login") or not expected_key:
            if user_key and _free_key_used(user_key):
                send_message(chat_id, "❌ KEY FREE KHÔNG HỢP LỆ.", _auth_menu_markup(chat_id))
            else:
                send_message(chat_id, "⚠️ Bạn chưa tạo link FREE. Hãy bấm 🆓 NHẬP KEY FREE trước.", _auth_menu_markup(chat_id))
            return

        if _free_key_used(user_key):
            send_message(chat_id, "❌ KEY FREE KHÔNG HỢP LỆ.", _auth_menu_markup(chat_id))
            return

        if user_key != expected_key:
            send_message(
                chat_id,
                "❌ Key FREE không đúng.\n\n"
                "🔐 Hãy lấy đúng Key FREE từ link đã tạo rồi gửi lại."
            )
            return

        _mark_free_key_used(user_key, chat_id, pending.get("link"))
        expires = _now_ts() + 24 * 3600
        _set_bot_auth(chat_id, user_key, "FREE", expires, _bot_device_id(chat_id), 24)
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        send_message(chat_id,
                     "✅ XÁC THỰC KEY FREE THÀNH CÔNG!\n\n"
                     "🆓 Quyền: LOGIC FREE\n"
                     "⏰ Hiệu lực: 24 giờ\n\n"
                     "Bạn có thể chọn tool trong Menu.",
                     home_markup(chat_id))
        return

    # /KEY_VIP hoặc /KEY_VIP ABC123
    if upper_cmd.startswith("/KEY_VIP"):
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2:
            # /KEY_VIP chuyển trạng thái nhập sang VIP, hủy luồng FREE đang chờ.
            BOT_PENDING[str(chat_id)] = {"mode": "vip", "created_at": _now_ts()}
            send_message(chat_id,
                         "👑 NHẬP KEY VIP\n\n"
                         "Gõ /KEY_VIP rồi nhập key VIP.\n"
                         "🔐 Key sẽ được xác thực.")
            return
        user_key = parts[1].strip()
        _verify_bot_vip_key(chat_id, user_key, msg.get("from") if isinstance(msg, dict) else None)
        return

    # Khi bot đang chờ key VIP.
    pending = _bot_pending_record(chat_id)
    if pending.get("mode") == "vip":
        _verify_bot_vip_key(chat_id, cmd, msg.get("from") if isinstance(msg, dict) else None)
        return

    if cmd == "/stop":
        stop_tool(chat_id)
        return
    if cmd == "/status":
        show_status(chat_id)
        return

    session = get_session(chat_id)
    if session and session.running:
        if not session.send_input(text):
            send_message(chat_id,
                         "⚠️ Tool đã đóng hoặc không nhận được dữ liệu.",
                         home_markup(chat_id))
    else:
        send_message(chat_id,
                     "🔐 Bạn chưa chọn hoặc chưa xác thực key.\n\nGõ /start để mở hệ thống key.",
                     _auth_menu_markup(chat_id))


def _notify_admin_vip_activated(chat_id, user_key, user_info=None, auth_record=None):
    """Gửi ID Telegram và key VIP vừa kích hoạt tới các admin đã cấu hình."""
    user_info = user_info if isinstance(user_info, dict) else {}
    username = str(user_info.get("username") or "").strip()
    first_name = str(user_info.get("first_name") or "").strip()
    last_name = str(user_info.get("last_name") or "").strip()
    display_name = " ".join(part for part in (first_name, last_name) if part).strip()

    try:
        expiry = float((auth_record or {}).get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        expiry = 0.0
    duration_text = (
        _format_duration(max(0, expiry - _now_ts())) if expiry else "Không xác định"
    )

    lines = [
        "✅ NGƯỜI DÙNG VỪA KÍCH HOẠT KEY VIP",
        "",
        f"🆔 Telegram ID: {chat_id}",
    ]
    if username:
        lines.append(f"👤 Username: @{username}")
    if display_name:
        lines.append(f"📛 Tên tài khoản: {display_name}")
    lines.extend([
        f"🔑 Key VIP: {user_key}",
        f"⏰ Hiệu lực: {duration_text}",
    ])
    notice = "\n".join(lines)

    for admin_id in sorted(ADMIN_CHAT_IDS):
        try:
            send_message(admin_id, notice)
        except Exception:
            # Lỗi gửi tin cho một admin không được làm hỏng việc kích hoạt key.
            pass


def _find_active_vip_key_owner(user_key, current_chat_id):
    """Tìm Telegram khác đang giữ cùng Key VIP còn hiệu lực trong dữ liệu bot."""
    key_to_check = str(user_key or "").strip().upper()
    current_id = str(current_chat_id)
    now = _now_ts()
    with BOT_AUTH_LOCK:
        snapshot = [
            (str(owner_id), dict(record))
            for owner_id, record in BOT_AUTH.items()
            if isinstance(record, dict)
        ]

    for owner_id, record in snapshot:
        if owner_id == current_id:
            continue
        if str(record.get("key", "")).strip().upper() != key_to_check:
            continue
        if str(record.get("key_type", "")).upper() != "VIP":
            continue

        try:
            expires_at = float(record.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0.0

        if expires_at > 0 and now < expires_at:
            return owner_id
    return None


def _verify_bot_vip_key(chat_id, user_key, user_info=None):
    # Mã key không phân biệt chữ hoa/thường; lưu và đối chiếu theo dạng chuẩn của server.
    user_key = str(user_key or "").strip().upper()
    if not user_key:
        send_message(chat_id, "❌ Key VIP không được để trống.", _auth_menu_markup(chat_id))
        return

    # Nếu đúng key đã kích hoạt trên tài khoản này và còn hiệu lực,
    # báo rõ key đã được kích hoạt thay vì coi đó là key không hợp lệ.
    # Hồ sơ này được lưu bền nên vẫn hoạt động sau khi bot khởi động lại.
    local_auth = _bot_auth_record(chat_id)
    if isinstance(local_auth, dict):
        saved_key = str(local_auth.get("key", "")).strip().upper()
        saved_type = str(local_auth.get("key_type", "")).upper()
        is_expired = bool(local_auth.get("__expired__"))
        if saved_key == user_key and saved_type == "VIP" and not is_expired:
            banned, vip_attempts = _register_vip_spam(
                chat_id, "Nhập lại Key VIP đã được kích hoạt 3 lần liên tiếp"
            )
            if banned:
                BOT_PENDING.pop(str(chat_id), None)
                _save_bot_pending()
                send_message(
                    chat_id,
                    "⛔ BẠN ĐÃ BỊ BAN VĨNH VIỄN.\n\n"
                    "🔒 Bạn đã nhập lại Key VIP đã được kích hoạt 3 lần liên tiếp.",
                    None,
                )
            else:
                remain = max(0, BOT_MAX_AUTH_ATTEMPTS - vip_attempts)
                send_message(
                    chat_id,
                    "❌ KEY VIP ĐÃ ĐƯỢC KÍCH HOẠT.\n\n"
                    f"⚠️ BẠN CÒN {remain}/3 LẦN ĐỂ BAN VĨNH VIỄN.\n\n"
                    "🔐 Hãy nhập lại Key VIP.",
                    None,
                )
            return

    send_message(chat_id, "🔎 ĐANG XÁC THỰC KEY VIP...")
    try:
        device_id = _bot_device_id(chat_id)
        result = verify_vip_key_from_server(
            device_id,
            user_key,
            quiet=True,
            telegram_id=str(chat_id),
        )

        # Lỗi mạng/server không đồng nghĩa với key sai: không trừ lượt nếu
        # chưa nhận được một kết quả xác thực đáng tin cậy.
        if not isinstance(result, dict):
            send_message(
                chat_id,
                "⚠️ CHƯA THỂ XÁC THỰC KEY VIP DO LỖI KẾT NỐI.\n\n"
                "🔄 Vui lòng thử lại sau.",
                None,
            )
            return

        status = str(result.get("status", "INVALID")).upper()

        # Kiểm tra hạn ngay tại bot để phòng trường hợp server trả success=True
        # nhưng expiry đã qua. Key hết hạn luôn được xem là không hợp lệ.
        result["is_forever"] = False
        expiry_raw = result.get("expires_at")
        expiry_ts = None
        if expiry_raw not in (None, "", 0, "0", False):
            if isinstance(expiry_raw, (int, float)):
                expiry_ts = float(expiry_raw)
            else:
                try:
                    expiry_ts = float(str(expiry_raw).strip())
                except (TypeError, ValueError):
                    expiry_ts = _parse_iso_ts(expiry_raw)
        if not expiry_ts or expiry_ts <= 0:
            try:
                fallback_hours = float(result.get("duration_hours") or 8760)
            except (TypeError, ValueError):
                fallback_hours = 8760.0
            fallback_hours = min(fallback_hours, 8760.0)
            expiry_ts = _now_ts() + fallback_hours * 3600
            result["expires_at"] = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).isoformat()
        if expiry_ts <= _now_ts():
            status = "EXPIRED"
            result["status"] = status

        # Lớp bảo vệ bổ sung: nếu server trả thành công nhưng dữ liệu bot cho
        # thấy Telegram khác đang dùng cùng key VIP còn hạn, không cấp lại key.
        # Trạng thái EXPIRED/REVOKED của server luôn được ưu tiên.
        if result.get("_server_success") and status not in {"EXPIRED", "REVOKED"}:
            server_owner = str(result.get("telegram_id") or "").strip()
            # Nếu chính server xác nhận key thuộc Telegram hiện tại, kết quả
            # server có thẩm quyền hơn bản ghi bot cũ có thể đã bị sót/stale.
            server_confirms_same_owner = bool(server_owner and server_owner == str(chat_id))
            if not server_confirms_same_owner:
                existing_owner = _find_active_vip_key_owner(user_key, chat_id)
                if existing_owner is not None:
                    status = "TELEGRAM_MISMATCH"
                    result["status"] = status

        invalid_statuses = {
            "REVOKED", "EXPIRED", "DEVICE_MISMATCH", "INVALID",
            "TELEGRAM_MISMATCH", "TELEGRAM_VIP_ALREADY_ACTIVATED",
            "ALREADY_ACTIVATED", "KEY_ALREADY_ACTIVATED", "USED",
        }
        if status in invalid_statuses:
            # Log status/reason (never the key) so Admin can distinguish a
            # missing key, expiration, revocation, or activation on another ID.
            try:
                print(
                    f"[KEY_VERIFY] telegram_id={chat_id} status={status} "
                    f"message={str(result.get('message') or '')[:240]}",
                    flush=True,
                )
            except Exception:
                pass
            banned, vip_attempts = _register_vip_spam(
                chat_id, "Nhập Key VIP không hợp lệ 3 lần liên tiếp"
            )
            if banned:
                BOT_PENDING.pop(str(chat_id), None)
                _save_bot_pending()
                send_message(
                    chat_id,
                    "⛔ BẠN ĐÃ BỊ BAN VĨNH VIỄN.\n\n"
                    "🔒 Bạn đã nhập Key VIP không hợp lệ 3 lần liên tiếp.",
                    None,
                )
            else:
                if status == "REVOKED":
                    msg = "❌ KEY VIP ĐÃ BỊ THU HỒI."
                elif status == "EXPIRED":
                    # Theo yêu cầu: khi key đã dùng hết thời hạn, chuyển về
                    # thông báo KEY VIP KHÔNG HỢP LỆ ở bước nhập lại key.
                    msg = "❌ KEY VIP KHÔNG HỢP LỆ."
                elif status in {
                    "DEVICE_MISMATCH", "TELEGRAM_MISMATCH",
                    "TELEGRAM_VIP_ALREADY_ACTIVATED", "ALREADY_ACTIVATED",
                    "KEY_ALREADY_ACTIVATED", "USED",
                }:
                    # Các trạng thái này cho biết key đã được dùng/liên kết trước đó.
                    # Nếu hồ sơ cùng key trên tài khoản hiện tại đã hết hạn thì ưu
                    # tiên báo không hợp lệ, không coi key hết hạn là key đang dùng.
                    same_saved_key_expired = (
                        isinstance(local_auth, dict)
                        and str(local_auth.get("key", "")).strip() == user_key
                        and str(local_auth.get("key_type", "")).upper() == "VIP"
                        and bool(local_auth.get("__expired__"))
                    )
                    msg = (
                        "❌ KEY VIP KHÔNG HỢP LỆ."
                        if same_saved_key_expired or status == "EXPIRED"
                        else "❌ KEY VIP ĐÃ ĐƯỢC KÍCH HOẠT."
                    )
                else:
                    msg = "❌ KEY VIP KHÔNG HỢP LỆ."
                remain = max(0, BOT_MAX_AUTH_ATTEMPTS - vip_attempts)
                send_message(
                    chat_id,
                    f"{msg}\n\n⚠️ BẠN CÒN {remain}/3 LẦN ĐỂ BAN VĨNH VIỄN.\n\n🔐 Hãy nhập lại Key VIP.",
                    None,
                )
            return

        duration_hours = result.get("duration_hours")
        try:
            duration_hours = float(duration_hours) if duration_hours is not None else 8760.0
        except (TypeError, ValueError):
            duration_hours = 8760.0
        duration_hours = min(duration_hours, 8760.0)
        already_activated_same_account = bool(result.get("already_activated_same_account", False))
        is_forever = False
        expires = _parse_iso_ts(result.get("expires_at"))
        if not expires or expires <= 0:
            expires = _now_ts() + duration_hours * 3600

        # CỘNG DỒN thời hạn của key VIP mới vào thời gian VIP còn lại.
        current_auth = _bot_auth_record(chat_id)
        if not already_activated_same_account:
            try:
                new_duration_hours = float(duration_hours)
            except (TypeError, ValueError):
                new_duration_hours = 0.0

            now_ts = _now_ts()
            if (
                new_duration_hours > 0
                and isinstance(current_auth, dict)
                and not current_auth.get("__expired__")
                and str(current_auth.get("key_type", "")).upper() == "VIP"
            ):
                try:
                    current_expiry = float(current_auth.get("expires_at", 0) or 0)
                except (TypeError, ValueError):
                    current_expiry = 0.0

                if current_expiry > now_ts:
                    expires = current_expiry + new_duration_hours * 3600
                    duration_hours = (expires - now_ts) / 3600

        rec = _set_bot_auth(
            chat_id, user_key, "VIP", expires, device_id,
            duration_hours, is_forever=False
        )
        # Phải xóa và LƯU pending sau khi VIP xác thực thành công.
        # Tránh mode=vip cũ sống lại sau restart và bắt nhầm link game.
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        expiry_message = f"⏰ Còn: {_format_duration(max(0, rec['expires_at'] - _now_ts()))}"

        # Chỉ gửi thông báo sau khi server xác thực thành công và hồ sơ đã lưu.
        _notify_admin_vip_activated(chat_id, user_key, user_info, rec)

        send_message(
            chat_id,
            "✅ XÁC THỰC KEY VIP THÀNH CÔNG!\n\n"
            "💎 Quyền: LOGIC VIP + LOGIC FREE\n"
            f"{expiry_message}\n\n"
            "Bạn có thể dùng các tool và logic VIP được cấp quyền.",
            home_markup(chat_id),
        )
    except Exception as exc:
        _admin_only_exception_log(chat_id, "Lỗi xác thực Key VIP", exc)
        send_message(chat_id, "❌ Xác thực Key VIP thất bại. Vui lòng thử lại.")



# =========================================================
# MUA KEY TELEGRAM - KHỚP KEY SERVER app_keys.py
# =========================================================
# app_keys.py hỗ trợ /api/issue_key với SECRET quản trị:
# package, duration_hours, is_forever, order_id, user_id, username, note.
# Gói VIP mới là 1 năm (8760 giờ), 100.000đ; bot chỉ cấp key SAU khi Admin duyệt đơn.

KEY_PURCHASE_PACKAGES = {
    "3d": {
        "label": "3 NGÀY",
        "package": "3 ngày",
        "duration_hours": 72,
        "price": 5000,
        "is_forever": False,
    },
    "1w": {
        "label": "1 TUẦN",
        "package": "1 tuần",
        "duration_hours": 168,
        "price": 10000,
        "is_forever": False,
    },
    "2w": {
        "label": "2 TUẦN",
        "package": "2 tuần",
        "duration_hours": 336,
        "price": 20000,
        "is_forever": False,
    },
    "1m": {
        "label": "1 THÁNG",
        "package": "1 tháng",
        "duration_hours": 720,
        "price": 30000,
        "is_forever": False,
    },
    "2m": {
        "label": "2 THÁNG",
        "package": "2 tháng",
        "duration_hours": 1440,
        "price": 50000,
        "is_forever": False,
    },
    "1y": {
        "label": "1 NĂM",
        "package": "1 năm",
        "duration_hours": 8760,
        "price": 100000,
        "is_forever": False,
    },
}

MAX_PURCHASE_QUANTITY = 50

BOT_ORDERS_FILE = str(BOT_DATA_DIR / "telegram_key_orders.json")
BOT_ORDERS = _load_json_file(BOT_ORDERS_FILE, {})
BOT_ORDER_LOCK = threading.Lock()


def _save_bot_orders():
    return _save_json_file(BOT_ORDERS_FILE, BOT_ORDERS)


def _money_vnd(value):
    try:
        return f"{int(value):,}".replace(",", ".") + " VNĐ"
    except Exception:
        return f"{value} VNĐ"


def _purchase_button_text(package_code):
    pkg = KEY_PURCHASE_PACKAGES.get(package_code, {})
    label = str(pkg.get("label", package_code)).upper()
    price = _money_vnd(pkg.get("price", 0)).replace(" VNĐ", "đ")
    icon = "👑" if pkg.get("is_forever") else "📦"
    return f"{icon} KEY {label} | {price}"


def _purchase_markup():
    return {
        "inline_keyboard": [
            [{"text": "📦 KEY 3 NGÀY | 5.000đ", "callback_data": "buy:3d"}],
            [{"text": "📦 KEY 1 TUẦN | 10.000đ", "callback_data": "buy:1w"}],
            [{"text": "📦 KEY 2 TUẦN | 20.000đ", "callback_data": "buy:2w"}],
            [{"text": "📦 KEY 1 THÁNG | 30.000đ", "callback_data": "buy:1m"}],
            [{"text": "📦 KEY 2 THÁNG | 50.000đ", "callback_data": "buy:2m"}],
            [{"text": "📦 KEY 1 NĂM | 100.000đ", "callback_data": "buy:1y"}],
        ]
    }


def _purchase_text():
    return (
        "🛒 SHOP KEY VIP VBXW-BOT\n"
        "Bấm sản phẩm muốn mua bên dưới để tạo đơn."
    )


def _purchase_code_from_button(button_text):
    text = str(button_text or "").strip()
    for code in KEY_PURCHASE_PACKAGES:
        if text == _purchase_button_text(code):
            return code
    return None


def _new_order_id():
    return secrets.token_hex(4).upper()


def _new_payment_content():
    # Dạng giống ví dụ DKOOARPWS: 9 ký tự, dễ đọc, không có khoảng trắng.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "DK" + "".join(secrets.choice(alphabet) for _ in range(7))


def _get_user_open_purchase_order(chat_id):
    """Lấy đơn đang khóa việc tạo đơn mới của từng Telegram user."""
    tid = str(chat_id)
    for oid, order in BOT_ORDERS.items():
        if not isinstance(order, dict):
            continue
        if str(order.get("telegram_id")) != tid:
            continue

        _expire_purchase_order_if_needed(order)
        status = str(order.get("status", "")).upper()

        # Khi người dùng đã bấm "ĐÃ THANH TOÁN • GỬI YÊU CẦU DUYỆT",
        # approval_requested_at được lưu lại. Từ thời điểm này chỉ được
        # tạo đơn mới sau khi Admin DUYỆT hoàn tất hoặc TỪ CHỐI đơn.
        if order.get("approval_requested_at") and status not in {
            "COMPLETED",
            "REJECTED",
            "CANCELLED",
        }:
            return oid, order

        # Các trạng thái đang giao dịch cũng khóa tạo đơn mới.
        if status in {
            "PENDING",
            "WAITING_PAYMENT",
            "WAITING_APPROVAL",
            "PROCESSING",
        }:
            return oid, order

    return None, None


def _cancel_unpaid_purchase_order(chat_id, order_id=None):
    """Hủy đơn còn chờ thanh toán khi người dùng chọn mua gói khác.

    Đơn đã gửi yêu cầu Admin duyệt không thể tự hủy từ nút này.
    Trả về (đã_hủy, mã_đơn).
    """
    tid = str(chat_id)
    with BOT_ORDER_LOCK:
        candidates = []
        if order_id:
            item = BOT_ORDERS.get(str(order_id).strip().upper())
            if isinstance(item, dict):
                candidates.append((str(order_id).strip().upper(), item))
        else:
            # Tương thích với nút "MUA GÓI KHÁC" được gửi từ phiên bản bot cũ,
            # vốn chỉ mang callback_data="buy" mà không kèm mã đơn.
            candidates = list(BOT_ORDERS.items())[::-1]

        for oid, order in candidates:
            if not isinstance(order, dict):
                continue
            if str(order.get("telegram_id")) != tid:
                continue
            if str(order.get("status", "")).upper() not in {"WAITING_PAYMENT", "PENDING"}:
                continue
            if order.get("approval_requested_at"):
                continue

            order["status"] = "CANCELLED"
            order["cancelled_at"] = datetime.now().isoformat()
            order["cancelled_by"] = "user_changed_package"
            _save_bot_orders()
            return True, str(oid)

    return False, None


def _create_key_purchase_order(chat_id, package_code, quantity=1):
    pkg = KEY_PURCHASE_PACKAGES.get(str(package_code))
    if not pkg:
        return None, "Gói key không hợp lệ."
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None, "Số lượng key không hợp lệ."
    if quantity < 1 or quantity > MAX_PURCHASE_QUANTITY:
        return None, f"Số lượng phải từ 1 đến {MAX_PURCHASE_QUANTITY} key."

    tid = str(chat_id)
    with BOT_ORDER_LOCK:
        # Mỗi Telegram user chỉ được có 1 đơn chưa kết thúc.
        existing_id, existing_order = _get_user_open_purchase_order(chat_id)
        if existing_id:
            return existing_id, "EXISTING"

        order_id = _new_order_id()
        while order_id in BOT_ORDERS:
            order_id = _new_order_id()

        payment_content = _new_payment_content()
        while any(
            isinstance(item, dict)
            and str(item.get("payment_content", "")).upper() == payment_content
            and str(item.get("status", "")).upper() not in {"REJECTED", "CANCELLED"}
            for item in BOT_ORDERS.values()
        ):
            payment_content = _new_payment_content()

        unit_price = int(pkg["price"])
        order = {
            "order_id": order_id,
            "telegram_id": tid,
            "username": "",
            "package_code": package_code,
            "package": pkg["package"],
            "label": pkg["label"],
            "duration_hours": pkg["duration_hours"],
            "is_forever": pkg["is_forever"],
            "quantity": quantity,
            "unit_price": unit_price,
            "price": unit_price * quantity,
            "status": "WAITING_PAYMENT",
            "created_at": datetime.now().isoformat(),
            "payment_wallet": PAYMENT_WALLET,
            "payment_account_name": PAYMENT_ACCOUNT_NAME,
            "payment_account_number": PAYMENT_ACCOUNT_NUMBER,
            "payment_content": payment_content,
            "key": None,
            "keys": [],
        }
        BOT_ORDERS[order_id] = order
        _save_bot_orders()

    return order_id, order


def _start_purchase_quantity(chat_id, package_code, source_message=None):
    if str(package_code) not in KEY_PURCHASE_PACKAGES:
        send_message(chat_id, "❌ Gói key không hợp lệ.")
        return

    # Lưu trạng thái chờ số lượng theo từng Telegram user.
    BOT_PENDING[str(chat_id)] = {
        "mode": "purchase_quantity",
        "package_code": str(package_code),
        "created_at": _now_ts(),
    }
    _save_bot_pending()

    # Khi chọn sản phẩm từ shop inline, xóa luôn tin nhắn tiêu đề/shop.
    if isinstance(source_message, dict):
        message_id = source_message.get("message_id")
        if message_id is not None:
            try:
                delete_message(chat_id, message_id)
            except Exception:
                pass

    send_message(chat_id, "🔢 Nhập số lượng key muốn mua:\n⚠️ Lưu ý: chỉ nhập số")


def _handle_purchase_order_result(chat_id, order_id, order, user=None):
    if order == "EXISTING":
        old = BOT_ORDERS.get(str(order_id), {})
        status = str(old.get("status", "")).upper()
        if old.get("approval_requested_at") and status not in {"COMPLETED", "REJECTED", "CANCELLED"}:
            msg = (
                "⛔ BẠN ĐANG CÓ ĐƠN CHỜ ADMIN DUYỆT\n\n"
                "Bạn không thể tạo đơn mới cho tới khi Admin xử lý xong đơn hiện tại.\n\n"
                + _format_order(old, show_key=False)
            )
        else:
            msg = "⚠️ Bạn đang có một đơn hàng chưa hoàn tất.\n\n" + _format_order(old, show_key=False)
        # Don't attach package-selection buttons while an unfinished order exists.
        send_message(chat_id, msg)
        if status == "WAITING_PAYMENT":
            _send_payment_instructions(chat_id, old)
        return

    if isinstance(order, dict):
        user = user if isinstance(user, dict) else {}
        order["username"] = user.get("username") or user.get("first_name") or ""
        with BOT_ORDER_LOCK:
            BOT_ORDERS[str(order_id)] = order
            _save_bot_orders()
        _send_payment_instructions(chat_id, order)
        return

    send_message(chat_id, f"❌ {order}", _purchase_markup())


def _format_order_created_at(value):
    """Hiển thị giờ Việt Nam theo dạng HH:MM:SS | DD-MM-YYYY."""
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        text = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        # Render thường lưu giờ hệ thống theo UTC; dữ liệu cũ không có múi giờ
        # được xem là UTC rồi chuyển sang giờ Việt Nam (UTC+7).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        vietnam_tz = timezone(timedelta(hours=7))
        dt = dt.astimezone(vietnam_tz)
        return dt.strftime("%H:%M:%S | %d-%m-%Y")
    except Exception:
        return raw


def _format_order(order, show_key=True):
    if not isinstance(order, dict):
        return "❌ Đơn hàng không hợp lệ."
    status = str(order.get("status", "-")).upper()
    status_text = {
        "WAITING_PAYMENT": "CHỜ THANH TOÁN",
        "PENDING": "CHỜ THANH TOÁN",
        "WAITING_APPROVAL": "CHỜ ADMIN DUYỆT",
        "PROCESSING": "ĐANG XỬ LÝ",
        "COMPLETED": "ĐÃ DUYỆT / HOÀN TẤT",
        "REJECTED": "ĐÃ TỪ CHỐI",
        "CANCELLED": "ĐÃ HỦY / HẾT HẠN",
    }.get(status, status)
    text = (
        "🧾 THÔNG TIN ĐƠN HÀNG\n\n"
        f"• Đơn: {order.get('order_id', '-')}\n"
        f"• User ID: {order.get('telegram_id', '-')}\n"
        f"• Gói: {order.get('label', order.get('package', '-'))}\n"
        f"• Số lượng: {int(order.get('quantity') or 1)}\n"
        f"• Tổng số tiền: {_money_vnd(order.get('price', order.get('unit_price', 0)))}\n"
        f"• Trạng thái: {status_text}\n"
        f"• Nội dung CK: {order.get('payment_content', '-')}\n"
        f"• Tạo lúc: {_format_order_created_at(order.get('created_at'))}\n"
    )
    if show_key and status == "COMPLETED":
        keys = _get_order_keys(order)
        if len(keys) == 1:
            text += f"• Key: {keys[0]}\n"
        elif keys:
            text += "• Danh sách Key:\n" + "\n".join(f"  {key}" for key in keys) + "\n"
    return text


# ================= DANH SÁCH ĐƠN CHỈ DÀNH CHO ADMIN =================
_ADMIN_ORDER_PAGE_SIZE = 6


def _admin_order_status_label(status):
    status = str(status or "-").upper()
    return {
        "WAITING_PAYMENT": "CHỜ THANH TOÁN",
        "PENDING": "CHỜ THANH TOÁN",
        "WAITING_APPROVAL": "CHỜ ADMIN DUYỆT",
        "PROCESSING": "ĐANG XỬ LÝ",
        "COMPLETED": "ĐÃ DUYỆT / HOÀN TẤT",
        "REJECTED": "ĐÃ TỪ CHỐI",
        "CANCELLED": "ĐÃ HỦY / HẾT HẠN",
    }.get(status, status)


def _admin_order_status_icon(status):
    status = str(status or "-").upper()
    return {
        "WAITING_PAYMENT": "💳",
        "PENDING": "💳",
        "WAITING_APPROVAL": "⏳",
        "PROCESSING": "🛠️",
        "COMPLETED": "✅",
        "REJECTED": "❌",
        "CANCELLED": "🚫",
    }.get(status, "📄")


def _admin_orders_markup(page, total_pages, page_orders):
    keyboard = []
    for order_id, order in page_orders:
        status = str(order.get("status", "-")).upper()
        label = _admin_order_status_label(status)
        icon = _admin_order_status_icon(status)
        # Callback ngắn gọn, nằm trong giới hạn callback_data của Telegram.
        keyboard.append([{
            "text": f"{icon} {order_id} · {label}"[:60],
            "callback_data": f"orders:view:{order_id}:{page}",
        }])

    navigation = []
    if page > 0:
        navigation.append({"text": "⬅️ TRƯỚC", "callback_data": f"orders:list:{page - 1}"})
    navigation.append({"text": f"📄 {page + 1}/{max(1, total_pages)}", "callback_data": f"orders:list:{page}"})
    if page + 1 < total_pages:
        navigation.append({"text": "TIẾP ➡️", "callback_data": f"orders:list:{page + 1}"})
    keyboard.append(navigation)
    keyboard.append([{"text": "🔄 LÀM MỚI DANH SÁCH", "callback_data": f"orders:list:{page}"}])
    return {"inline_keyboard": keyboard}


def _admin_order_detail_markup(order_id, page, status):
    keyboard = []
    status = str(status or "-").upper()
    # Chỉ hiện nút duyệt cho đơn đã gửi yêu cầu duyệt, tránh duyệt nhầm đơn
    # mới tạo/chưa xác nhận thanh toán.
    if status == "WAITING_APPROVAL":
        keyboard.append([
            {"text": "✅ DUYỆT ĐƠN", "callback_data": f"buy:approve:{order_id}"},
            {"text": "❌ TỪ CHỐI ĐƠN", "callback_data": f"buy:reject:{order_id}"},
        ])
    keyboard.append([{"text": "↩️ QUAY LẠI DANH SÁCH ĐƠN", "callback_data": f"orders:list:{page}"}])
    keyboard.append([{"text": "🔄 LÀM MỚI CHI TIẾT", "callback_data": f"orders:view:{order_id}:{page}"}])
    return {"inline_keyboard": keyboard}


def _show_admin_orders(chat_id, page=0):
    """Hiển thị tất cả đơn theo trang; chỉ Admin được phép gọi hàm này."""
    if not _is_bot_admin(chat_id):
        return

    with BOT_ORDER_LOCK:
        all_orders = []
        for order_id, order in BOT_ORDERS.items():
            if isinstance(order, dict):
                all_orders.append((str(order_id), dict(order)))

    all_orders.sort(
        key=lambda item: _parse_iso_ts(item[1].get("created_at")) or 0,
        reverse=True,
    )
    total_orders = len(all_orders)
    total_pages = max(1, (total_orders + _ADMIN_ORDER_PAGE_SIZE - 1) // _ADMIN_ORDER_PAGE_SIZE)
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0
    page = min(page, total_pages - 1)
    start = page * _ADMIN_ORDER_PAGE_SIZE
    page_orders = all_orders[start:start + _ADMIN_ORDER_PAGE_SIZE]

    status_counts = {}
    for _, order in all_orders:
        status = str(order.get("status", "-")).upper()
        status_counts[status] = status_counts.get(status, 0) + 1

    waiting_approval = status_counts.get("WAITING_APPROVAL", 0)
    waiting_payment = status_counts.get("WAITING_PAYMENT", 0) + status_counts.get("PENDING", 0)
    completed = status_counts.get("COMPLETED", 0)
    rejected = status_counts.get("REJECTED", 0)
    cancelled = status_counts.get("CANCELLED", 0)
    processing = status_counts.get("PROCESSING", 0)

    lines = [
        "📋 DANH SÁCH ĐƠN HÀNG (ADMIN)",
        "",
        f"📚 Tổng số đơn: {total_orders}",
        f"✅ Đã duyệt / hoàn tất: {completed}",
        f"⏳ Chờ Admin duyệt: {waiting_approval}",
        f"💳 Chờ thanh toán: {waiting_payment}",
        f"🛠️ Đang xử lý: {processing}",
        f"❌ Đã từ chối: {rejected}",
        f"🚫 Đã hủy / hết hạn: {cancelled}",
        "",
    ]

    if not all_orders:
        lines.append("Chưa có đơn hàng nào trong hệ thống.")
    else:
        lines.append(f"📄 Trang {page + 1}/{total_pages} — bấm một đơn bên dưới để xem chi tiết:")
        lines.append("")
        for index, (order_id, order) in enumerate(page_orders, start=start + 1):
            status = str(order.get("status", "-")).upper()
            lines.append(
                f"{index}. {_admin_order_status_icon(status)} Đơn {order_id} — {_admin_order_status_label(status)}\n"
                f"   👤 User ID: {order.get('telegram_id', '-')}\n"
                f"   📦 {order.get('label', order.get('package', '-'))} × {int(order.get('quantity') or 1)}"
                f" | 💰 {_money_vnd(order.get('price', order.get('unit_price', 0)))}\n"
                f"   🕒 {_format_order_created_at(order.get('created_at'))}"
            )
            lines.append("")

    send_message(
        chat_id,
        "\n".join(lines).rstrip(),
        _admin_orders_markup(page, total_pages, page_orders),
    )


def _key_copy_markup(key):
    """Nút sao chép chính xác toàn bộ Key bằng Telegram CopyTextButton."""
    key_text = str(key or "").strip()
    if not key_text:
        return None
    return {
        "inline_keyboard": [
            [{
                "text": "📋 SAO CHÉP KEY",
                "copy_text": {"text": key_text[:256]},
            }]
        ]
    }


def _payment_markup(order_id):
    return {
        "inline_keyboard": [
            [{
                "text": "✅ ĐÃ THANH TOÁN • GỬI YÊU CẦU DUYỆT",
                "callback_data": f"buy:request:{order_id}",
            }],
            [{"text": "↩️ MUA GÓI KHÁC", "callback_data": f"buy:other:{order_id}"}],
        ]
    }


def _admin_purchase_markup(order_id):
    return {
        "inline_keyboard": [
            [{
                "text": "✅ XÁC NHẬN ĐÃ NHẬN TIỀN • DUYỆT",
                "callback_data": f"buy:approve:{order_id}",
            }],
            [{
                "text": "❌ TỪ CHỐI ĐƠN",
                "callback_data": f"buy:reject:{order_id}",
            }],
            [{
                "text": "🧾 XEM ĐƠN",
                "callback_data": f"buy:order:{order_id}",
            }],
        ]
    }


def _payment_text(order):
    # Dùng HTML <code>...</code> cho nội dung chuyển khoản để Telegram hiển thị
    # dạng mã có thể nhấn/giữ để sao chép trên các ứng dụng Telegram hỗ trợ.
    payment_content = html.escape(str(order.get("payment_content") or "-"))
    return (
        "💳 <b>THANH TOÁN MUA KEY VIP</b>\n\n"
        f"🧾 Mã đơn: {html.escape(str(order.get('order_id') or '-'))}\n"
        f"📦 Gói: {html.escape(str(order.get('label') or '-'))}\n"
        f"🔢 Số lượng: {int(order.get('quantity') or 1)}\n"
        f"💰 Tổng số tiền: {_money_vnd(order.get('price', order.get('unit_price', 0)))}\n\n"
        f"💳 VÍ: {html.escape(PAYMENT_WALLET)}\n"
        f"👤 CHỦ TÀI KHOẢN: {html.escape(PAYMENT_ACCOUNT_NAME)}\n"
        f"🔢 SỐ TÀI KHOẢN: <code>{html.escape(PAYMENT_ACCOUNT_NUMBER)}</code>\n"
        f"📝 NỘI DUNG: <code>{payment_content}</code>\n\n"
        "⚠️ <b>Lưu ý:</b> Chuyển đúng số tiền và đúng nội dung trên.\n"
        "Sau khi chuyển khoản thành công, bấm nút bên dưới để gửi yêu cầu Admin kiểm tra và duyệt."
    )


def _send_payment_instructions(chat_id, order):
    # Ưu tiên QR động theo đúng đơn hàng: tự điền số tiền + nội dung CK khi quét.
    dynamic_qr_path = _build_dynamic_payment_qr_file(order)
    qr_path = dynamic_qr_path or _ensure_payment_qr_file()
    caption = _payment_text(order)
    if qr_path:
        result = send_photo(
            chat_id,
            qr_path,
            caption,
            _payment_markup(order["order_id"]),
            parse_mode="HTML",
        )
        if result is not None:
            if dynamic_qr_path:
                try:
                    Path(dynamic_qr_path).unlink(missing_ok=True)
                except Exception:
                    pass
            return result
    send_message(chat_id, caption, _payment_markup(order["order_id"]), parse_mode="HTML")
    return None


def _send_purchase_admin_notice(order):
    text = (
        "📥 YÊU CẦU DUYỆT ĐƠN MUA KEY\n\n"
        f"• Đơn: {order['order_id']}\n"
        f"• User ID: {order['telegram_id']}\n"
        f"• Username: {order.get('username') or 'Không có'}\n"
        f"• Gói: {order['label']}\n"
        f"• Số lượng: {int(order.get('quantity') or 1)}\n"
        f"• Tổng số tiền: {_money_vnd(order.get('price', order.get('unit_price', 0)))}\n"
        f"• VÍ: {order.get('payment_wallet', PAYMENT_WALLET)}\n"
        f"• STK: {order.get('payment_account_number', PAYMENT_ACCOUNT_NUMBER)}\n"
        f"• Nội dung CK: {order.get('payment_content', '-')}\n"
        f"• Trạng thái: {order.get('status')}\n"
        f"• Tạo lúc: {order.get('created_at', '-')}\n"
        f"• Yêu cầu lúc: {order.get('approval_requested_at', '-')}\n\n"
        "⚠️ Admin kiểm tra giao dịch MoMo thực tế trước khi bấm DUYỆT."
    )
    for admin_id in sorted(ADMIN_CHAT_IDS):
        try:
            send_message(admin_id, text, _admin_purchase_markup(order["order_id"]))
        except Exception:
            pass


def _expire_purchase_order_if_needed(order):
    """Tự hủy đơn chờ thanh toán sau 10 phút kể từ lúc tạo."""
    if not isinstance(order, dict):
        return False
    status = str(order.get("status", "")).upper()
    if status not in {"WAITING_PAYMENT", "PENDING"} or order.get("approval_requested_at"):
        return False
    created_ts = _parse_iso_ts(order.get("created_at"))
    if created_ts is None or time.time() - created_ts < 600:
        return False
    order["status"] = "CANCELLED"
    order["cancelled_at"] = datetime.now().isoformat()
    order["cancelled_by"] = "payment_timeout_10_minutes"
    order["cancel_reason"] = "Quá hạn thanh toán 10 phút"
    _save_bot_orders()
    return True


def _request_purchase_approval(order_id, user_chat_id):
    with BOT_ORDER_LOCK:
        order = BOT_ORDERS.get(str(order_id))
        if not isinstance(order, dict):
            return False, "Không tìm thấy đơn hàng."
        if str(order.get("telegram_id")) != str(user_chat_id):
            return False, "Bạn không có quyền gửi duyệt đơn này."

        # Hết hạn sau 10 phút thì hủy đơn trước khi xử lý nút "Đã thanh toán".
        if _expire_purchase_order_if_needed(order):
            return False, "Đơn đã bị hủy vì quá 10 phút chưa gửi yêu cầu thanh toán. Vui lòng tạo đơn mới; QR cũ đã hết hạn trong hệ thống bot."

        status = str(order.get("status", "")).upper()
        if status == "CANCELLED":
            return False, "Đơn đã bị hủy hoặc hết hạn. Vui lòng tạo đơn mới."
        if status == "WAITING_APPROVAL":
            return True, "EXISTING"
        if status == "COMPLETED":
            return False, "Đơn này đã hoàn tất."
        if status == "PROCESSING":
            return False, "Đơn đang được Admin xử lý."
        if status == "REJECTED":
            return False, "Đơn này đã bị từ chối. Bạn hãy tạo đơn mới."
        if status not in {"WAITING_PAYMENT", "PENDING"}:
            return False, f"Trạng thái đơn hiện tại: {status}."

        order["status"] = "WAITING_APPROVAL"
        order["approval_requested_at"] = datetime.now().isoformat()
        order.pop("error", None)
        _save_bot_orders()
        snap = dict(order)

    _send_purchase_admin_notice(snap)
    return True, snap

def _get_order_keys(order):
    if not isinstance(order, dict):
        return []
    keys = order.get("keys")
    if isinstance(keys, list):
        normalized = [str(key).strip() for key in keys if str(key or "").strip()]
        if normalized:
            return normalized
    key = str(order.get("key") or "").strip()
    return [key] if key else []


def _issue_purchase_order(order_id, admin_chat_id):
    with BOT_ORDER_LOCK:
        order = BOT_ORDERS.get(str(order_id))
        if not isinstance(order, dict):
            return False, "Không tìm thấy đơn hàng."
        status = str(order.get("status", "")).upper()
        if status == "COMPLETED" and (order.get("keys") or order.get("key")):
            return False, (
                "Đơn này đã được duyệt và cấp Key trước đó. Bot không gửi lại Key "
                "để tránh phát hành lại key có thể đã kích hoạt. Admin hãy xem "
                "chi tiết trong DANH SÁCH ĐƠN nếu cần tra cứu."
            )
        if status not in {"PENDING", "WAITING_APPROVAL", "PROCESSING"}:
            return False, f"Đơn đang ở trạng thái {status}."
        try:
            quantity = int(order.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1
        if quantity < 1 or quantity > MAX_PURCHASE_QUANTITY:
            return False, f"Số lượng trong đơn không hợp lệ (1-{MAX_PURCHASE_QUANTITY})."

        order["status"] = "PROCESSING"
        order["approved_by"] = str(admin_chat_id)
        order["approved_at"] = datetime.now().isoformat()
        # Giữ danh sách key đã cấp để lần thử lại chỉ cấp phần còn thiếu.
        order.setdefault("keys", _get_order_keys(order))
        if not isinstance(order.get("keys"), list):
            order["keys"] = _get_order_keys(order)
        issued_keys = [str(key).strip() for key in order["keys"] if str(key or "").strip()]
        order["keys"] = issued_keys
        _save_bot_orders()
        order = dict(order)

    package_code = str(order.get("package_code") or "")
    pkg = KEY_PURCHASE_PACKAGES.get(package_code)
    legacy_forever_order = package_code == "forever"
    if legacy_forever_order:
        # Đơn cũ đang chờ duyệt được chuyển sang gói 1 năm; không còn phát hành key vô hạn.
        pkg = dict(KEY_PURCHASE_PACKAGES["1y"])
        order["package_code"] = "1y"
        order["label"] = pkg["label"]
        order["duration_hours"] = pkg["duration_hours"]
        order["is_forever"] = False
        with BOT_ORDER_LOCK:
            if order_id in BOT_ORDERS:
                BOT_ORDERS[order_id]["package_code"] = "1y"
                BOT_ORDERS[order_id]["label"] = pkg["label"]
                BOT_ORDERS[order_id]["duration_hours"] = pkg["duration_hours"]
                BOT_ORDERS[order_id]["is_forever"] = False
                _save_bot_orders()
    if not pkg:
        with BOT_ORDER_LOCK:
            if order_id in BOT_ORDERS:
                BOT_ORDERS[order_id]["status"] = "WAITING_APPROVAL"
                _save_bot_orders()
        return False, "Không tìm thấy cấu hình gói."

    issued_keys = list(order.get("keys") or [])
    for key_index in range(len(issued_keys), quantity):
        # Đơn mua nhiều key cần order_id riêng cho từng lần gọi API,
        # nhưng vẫn giữ mã đơn chính để người dùng/Admin tra cứu.
        api_order_id = order["order_id"] if quantity == 1 else f"{order['order_id']}-{key_index + 1:03d}"
        payload = {
            "package": pkg["package"],
            "order_id": api_order_id,
            "user_id": order["telegram_id"],
            "username": order.get("username", ""),
            "note": "telegram_purchase",
        }
        payload["duration_hours"] = min(int(pkg.get("duration_hours") or 8760), 8760)

        try:
            headers = {}
            if KEY_SERVER_ADMIN_SECRET:
                headers["X-Server-Secret"] = KEY_SERVER_ADMIN_SECRET
            resp = requests.post(
                f"{KEY_SERVER_URL}/api/issue_key",
                json=payload,
                headers=headers,
                timeout=20,
            )
            data = resp.json() if resp.content else {}
        except Exception as exc:
            with BOT_ORDER_LOCK:
                if order_id in BOT_ORDERS:
                    BOT_ORDERS[order_id]["status"] = "WAITING_APPROVAL"
                    BOT_ORDERS[order_id]["error"] = str(exc)
                    _save_bot_orders()
            return False, f"Lỗi kết nối Key Server khi cấp key {key_index + 1}/{quantity}: {exc}"

        if resp.status_code != 200 or not isinstance(data, dict) or not data.get("success"):
            server_status = str(data.get("status", "")).strip().upper() if isinstance(data, dict) else ""
            msg = str(data.get("message", f"HTTP {resp.status_code}"))
            sold_out = (
                server_status == "SOLD_OUT"
                or "không còn key" in msg.lower()
                or "bán hết" in msg.lower()
                or "het key" in msg.lower()
                or "sold out" in msg.lower()
            )
            with BOT_ORDER_LOCK:
                if order_id in BOT_ORDERS:
                    BOT_ORDERS[order_id]["status"] = "WAITING_APPROVAL"
                    BOT_ORDERS[order_id]["error"] = msg
                    if sold_out:
                        BOT_ORDERS[order_id]["stock_status"] = "SOLD_OUT"
                    else:
                        BOT_ORDERS[order_id].pop("stock_status", None)
                    _save_bot_orders()
            if sold_out:
                return False, f"SOLD_OUT|Gói {order.get('label', pkg.get('package', ''))} hiện đã hết key sau khi cấp {len(issued_keys)}/{quantity} key."
            return False, f"Cấp key {key_index + 1}/{quantity} thất bại: {msg}"

        already_activated = any([
            data.get("activated") is True,
            data.get("used") is True,
            data.get("redeemed_once") is True,
            bool(data.get("activated_at")),
            bool(data.get("used_at")),
        ])
        if already_activated:
            with BOT_ORDER_LOCK:
                if order_id in BOT_ORDERS:
                    BOT_ORDERS[order_id]["status"] = "WAITING_APPROVAL"
                    BOT_ORDERS[order_id]["error"] = f"Server trả về Key đã kích hoạt ở vị trí {key_index + 1}."
                    BOT_ORDERS[order_id]["stock_status"] = "INVALID_ISSUED_KEY"
                    _save_bot_orders()
            return False, f"Server trả về Key đã từng được kích hoạt ở vị trí {key_index + 1}. Không cấp Key này cho người dùng."

        key = str(data.get("key") or "").strip()
        if not key:
            with BOT_ORDER_LOCK:
                if order_id in BOT_ORDERS:
                    BOT_ORDERS[order_id]["status"] = "WAITING_APPROVAL"
                    BOT_ORDERS[order_id]["error"] = f"Server không trả key ở vị trí {key_index + 1}."
                    _save_bot_orders()
            return False, f"Server không trả về Key ở vị trí {key_index + 1}/{quantity}."

        issued_keys.append(key)
        with BOT_ORDER_LOCK:
            if order_id in BOT_ORDERS:
                BOT_ORDERS[order_id]["keys"] = list(issued_keys)
                BOT_ORDERS[order_id]["key"] = issued_keys[0]
                BOT_ORDERS[order_id]["issued_count"] = len(issued_keys)
                BOT_ORDERS[order_id].pop("error", None)
                BOT_ORDERS[order_id].pop("stock_status", None)
                _save_bot_orders()

    # Tất cả key đã được cấp thành công.
    with BOT_ORDER_LOCK:
        if order_id in BOT_ORDERS:
            BOT_ORDERS[order_id]["status"] = "COMPLETED"
            BOT_ORDERS[order_id]["keys"] = list(issued_keys)
            BOT_ORDERS[order_id]["key"] = issued_keys[0] if issued_keys else None
            BOT_ORDERS[order_id]["key_type"] = "VIP"
            BOT_ORDERS[order_id].pop("expires_at", None)
            BOT_ORDERS[order_id]["completed_at"] = datetime.now().isoformat()
            BOT_ORDERS[order_id]["activation_status"] = "NOT_ACTIVATED"
            _save_bot_orders()

    return True, "\n".join(issued_keys)


def _show_buy_menu(chat_id):
    send_message(chat_id, _purchase_text(), _purchase_markup())


def handle_callback(query):
    global _CURRENT_CHAT_ID
    callback_id = query.get("id", "")
    data = str(query.get("data", ""))
    chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
    _CURRENT_CHAT_ID = chat_id
    if chat_id is None:
        answer_callback(callback_id, "Không xác định được chat.")
        return

    if not _is_bot_admin(chat_id) and (
        data.startswith(("buy:approve:", "buy:reject:")) or data.startswith("orders:")
    ):
        # Dismiss callback silently without a visible toast or message.
        answer_callback(callback_id)
        return

    ban = _bot_banned_message(chat_id)
    if ban:
        answer_callback(callback_id, "Bạn đang bị khóa do spam.")
        send_message(chat_id, ban)
        return

    callback_chat = (query.get("message") or {}).get("chat") or {}
    if str(callback_chat.get("type", "private")).lower() == "private":
        _register_chat_user(chat_id)

    answer_callback(callback_id)

    if data == "home":
        valid, rec = _bot_auth_valid(chat_id)
        if valid and rec:
            send_message(chat_id, _auth_menu_text(chat_id), home_markup(chat_id))
        else:
            send_message(chat_id, _auth_menu_text(chat_id), _auth_menu_markup(chat_id))
    elif data == "buy":
        # Tương thích với tin nhắn thanh toán được gửi bởi phiên bản bot cũ.
        # Khi người dùng chọn mua gói khác, hủy đơn chưa thanh toán, xóa màn
        # hình thanh toán cũ (kể cả ảnh QR), rồi mới hiện lại shop.
        source_message = query.get("message") or {}
        cancelled, _ = _cancel_unpaid_purchase_order(chat_id)
        if cancelled:
            delete_message(chat_id, source_message.get("message_id"))
        _show_buy_menu(chat_id)
    elif data.startswith("buy:other:"):
        order_id = data.split(":", 2)[2].strip().upper()
        source_message = query.get("message") or {}
        cancelled, _ = _cancel_unpaid_purchase_order(chat_id, order_id)
        if not cancelled:
            send_message(
                chat_id,
                "⚠️ Không thể đổi gói vì đơn này không còn ở trạng thái chờ thanh toán. "
                "Nếu bạn đã gửi yêu cầu Admin duyệt, vui lòng chờ Admin xử lý đơn hiện tại.",
            )
            return
        # Xóa tin nhắn thanh toán cũ trước khi gửi lại menu shop.
        delete_message(chat_id, source_message.get("message_id"))
        _show_buy_menu(chat_id)
    elif data.startswith("buy:request:"):
        order_id = data.split(":", 2)[2].strip().upper()
        ok, result = _request_purchase_approval(order_id, chat_id)
        if not ok:
            send_message(chat_id, f"❌ {result}", home_markup(chat_id))
        else:
            # Xóa màn hình thanh toán cũ (kể cả tin nhắn ảnh QR) sau khi
            # yêu cầu duyệt đã được ghi nhận thành công.
            source_message = query.get("message") or {}
            delete_message(chat_id, source_message.get("message_id"))
            # Chỉ gửi đúng 1 thông báo, không gửi thêm chi tiết đơn hàng.
            send_message(
                chat_id,
                "⏳ Yêu cầu đã được chuyển tới Admin.\nVui lòng chờ Admin kiểm tra giao dịch",
            )
    elif data.startswith("buy:approve:"):
        if not _is_bot_admin(chat_id):
            return
        order_id = data.split(":", 2)[2].strip().upper()
        ok, result = _issue_purchase_order(order_id, chat_id)
        if not ok:
            order_fail = BOT_ORDERS.get(order_id, {})
            target_fail = str(order_fail.get("telegram_id", ""))
            if str(result).startswith("SOLD_OUT|"):
                sold_msg = str(result).split("|", 1)[1].strip()
                send_message(
                    chat_id,
                    f"❌ DUYỆT ĐƠN THẤT BẠI\n\n📦 {sold_msg}\n\n⏳ Đơn vẫn được giữ ở trạng thái chờ duyệt để Admin xử lý khi có thêm Key.",
                    home_markup(chat_id),
                )
                try:
                    send_message(
                        int(target_fail),
                        "⚠️ ĐƠN HÀNG CHƯA THỂ CẤP KEY\n\n"
                        f"📦 {sold_msg}\n"
                        f"🧾 Đơn: {order_id}\n\n"
                        "⏳ Hiện chưa có Key phù hợp trong kho. Đơn của bạn vẫn được giữ lại và sẽ được xử lý khi có Key.",
                        home_markup(int(target_fail)),
                    )
                except Exception:
                    pass
            else:
                send_message(chat_id, f"❌ DUYỆT ĐƠN THẤT BẠI\n\n{result}", home_markup(chat_id))
            return
        order = BOT_ORDERS.get(order_id, {})
        target_id = str(order.get("telegram_id", ""))
        answer_callback(callback_id, "✅ Đã duyệt đơn.")
        order_keys = _get_order_keys(order)
        keys_plain = "\n".join(order_keys) if order_keys else str(result)
        keys_html = "\n".join(f"<code>{html.escape(key)}</code>" for key in order_keys) if order_keys else f"<code>{html.escape(str(result))}</code>"
        send_message(
            chat_id,
            "✅ DUYỆT ĐƠN THÀNH CÔNG\n\n"
            f"• Đơn: {order_id}\n"
            f"• User ID: {target_id}\n"
            f"• Gói: {order.get('label', '-')}\n"
            f"• Số lượng: {int(order.get('quantity') or 1)}\n"
            f"• Danh sách Key:\n{keys_plain}",
            home_markup(chat_id),
        )
        try:
            send_message(
                int(target_id),
                "🎉 THANH TOÁN / ĐƠN HÀNG ĐÃ ĐƯỢC DUYỆT!\n\n"
                f"🔑 KEY VIP:\n{keys_html}\n"
                f"📦 Gói: {order.get('label', '-') }\n"
                f"🧾 Đơn: {order_id}\n"
                "👉 Gõ /KEY_VIP rồi nhập Key muốn kích hoạt.",
                None,
                "HTML",
            )
        except Exception:
            pass
    elif data.startswith("buy:reject:"):
        if not _is_bot_admin(chat_id):
            return
        order_id = data.split(":", 2)[2].strip().upper()
        with BOT_ORDER_LOCK:
            order = BOT_ORDERS.get(order_id)
            if not isinstance(order, dict):
                send_message(chat_id, "❌ Không tìm thấy đơn.")
                return
            status = str(order.get("status", "")).upper()
            if status == "COMPLETED":
                send_message(chat_id, "❌ Đơn đã hoàn tất, không thể từ chối.")
                return
            order["status"] = "REJECTED"
            order["rejected_by"] = str(chat_id)
            order["rejected_at"] = datetime.now().isoformat()
            _save_bot_orders()
            snap = dict(order)
        answer_callback(callback_id, "❌ Đã từ chối đơn.")
        send_message(chat_id, f"✅ Đã từ chối đơn {order_id}.", home_markup(chat_id))
        try:
            send_message(
                int(snap["telegram_id"]),
                f"❌ Đơn {order_id} đã bị Admin từ chối.",
                home_markup(int(snap["telegram_id"])),
            )
        except Exception:
            pass
    elif data.startswith("buy:order:"):
        order_id = data.split(":", 2)[2].strip().upper()
        order = BOT_ORDERS.get(order_id)
        if not isinstance(order, dict):
            send_message(chat_id, "❌ Không tìm thấy đơn.")
        elif not _is_bot_admin(chat_id) and str(order.get("telegram_id")) != str(chat_id):
            return
        else:
            markup = _admin_purchase_markup(order_id) if _is_bot_admin(chat_id) else (
                _payment_markup(order_id) if str(order.get("status", "")).upper() == "WAITING_PAYMENT" else None
            )
            send_message(chat_id, _format_order(order, show_key=_is_bot_admin(chat_id) or str(order.get("status", "")).upper() == "COMPLETED"), markup)
    elif data.startswith("orders:list:"):
        if not _is_bot_admin(chat_id):
            return
        try:
            page = int(data.split(":", 2)[2])
        except (IndexError, TypeError, ValueError):
            page = 0
        _show_admin_orders(chat_id, page=page)
    elif data.startswith("orders:view:"):
        if not _is_bot_admin(chat_id):
            return
        parts = data.split(":")
        if len(parts) < 4:
            send_message(chat_id, "❌ Dữ liệu xem đơn không hợp lệ.", home_markup(chat_id))
            return
        order_id = parts[2].strip().upper()
        try:
            page = max(0, int(parts[3]))
        except (TypeError, ValueError):
            page = 0
        with BOT_ORDER_LOCK:
            order_raw = BOT_ORDERS.get(order_id)
            order = dict(order_raw) if isinstance(order_raw, dict) else None
        if not isinstance(order, dict):
            send_message(chat_id, "❌ Không tìm thấy đơn hàng.", home_markup(chat_id))
        else:
            status = str(order.get("status", "-")).upper()
            send_message(
                chat_id,
                _format_order(order, show_key=True),
                _admin_order_detail_markup(order_id, page, status),
            )
    elif data.startswith("buy:"):
        package_code = data.split(":", 1)[1]
        if package_code not in KEY_PURCHASE_PACKAGES:
            send_message(chat_id, "❌ Gói key không hợp lệ.")
        else:
            _start_purchase_quantity(
                chat_id,
                package_code,
                source_message=query.get("message") or {},
            )
    elif data == "auth:free":
        # Callback FREE cũng phải hủy hoàn toàn chế độ VIP trước khi tạo/nhận Key FREE.
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        _reset_vip_spam(chat_id)
        BOT_PENDING[str(chat_id)] = {"mode": "free_select", "created_at": _now_ts()}
        _save_bot_pending()
        _handle_free_key_request(chat_id)
    elif data == "auth:vip":
        # Callback VIP cũng hủy pending FREE ngay lập tức.
        BOT_PENDING[str(chat_id)] = {"mode": "vip", "created_at": _now_ts()}
        _save_bot_pending()
        send_message(chat_id,
                     "👑 NHẬP KEY VIP\n\n"
                     "Gõ /KEY_VIP rồi nhập key VIP.\n"
                     "🔐 Key sẽ được xác thực.")
    elif data == "status":
        show_status(chat_id)
    elif data == "stop":
        stop_tool(chat_id)
    elif data.startswith("stop:"):
        stop_tool(chat_id, data.split(":", 1)[1])
    elif data.startswith("tool:"):
        start_tool(chat_id, data.split(":", 1)[1])


def _handle_free_key_request(chat_id):
    # Hàm FREE luôn nhận quyền xử lý tin nhắn FREE; nếu còn pending VIP thì hủy ngay.
    pending_current = _bot_pending_record(chat_id)
    if pending_current.get("mode") == "vip":
        BOT_PENDING.pop(str(chat_id), None)
        _save_bot_pending()
        _reset_vip_spam(chat_id)

    ban = _bot_banned_message(chat_id)
    if ban:
        send_message(chat_id, ban)
        return

    valid, rec = _bot_auth_valid(chat_id)
    if valid and rec and str(rec.get("key_type", "FREE")).upper() == "FREE":
        remaining = max(0, rec["expires_at"] - _now_ts())
        send_message(chat_id,
                     "✅ Bạn đang có Key FREE còn hiệu lực.\n"
                     f"⏰ Còn: {_format_duration(remaining)}",
                     home_markup(chat_id))
        return

    # Chống yêu cầu tạo link liên tục.
    now = _now_ts()
    pending = _bot_pending_record(chat_id)
    if pending.get("mode") == "free" and pending.get("link") and now - float(pending.get("created_at", 0)) < 300:
        send_message(chat_id,
                     "🔗 Bạn đã có một link FREE đang chờ.\n\n"
                     f"{pending['link']}\n\n"
                     "📌 Sau khi có Key, gõ:\n/login KEY_CUA_BAN\n\n"
                     "⏰ Key có hiệu lực trong 24h")
        return

    # Spam nút/tạo link FREE không được tính vào bộ đếm BAN VIP.
    # Nếu đã có link chờ trong 5 phút thì trả lại link cũ thay vì tạo thêm.
    device_id = _bot_device_id(chat_id)
    send_message(chat_id, "⏳ ĐANG TẠO LINK LẤY KEY FREE...")
    try:
        key = secrets.token_hex(4)
        telegraph_url = create_telegraph_page(key, device_id)
        if not telegraph_url:
            send_message(chat_id, "❌ Không tạo được trang lấy Key. Vui lòng thử lại sau.")
            return

        params_4m = {"api": LINK4M_TOKEN, "url": telegraph_url}
        r1 = requests.get(LINK4M_API, params=params_4m, timeout=15)
        if r1.status_code != 200:
            raise RuntimeError(f"Link4m HTTP {r1.status_code}")
        data1 = r1.json()
        if data1.get("status") != "success":
            raise RuntimeError("Link4m API không trả về success")
        link4m_url = data1.get("shortenedUrl")
        if not link4m_url:
            raise RuntimeError("Không nhận được shortenedUrl")

        params_vn = {"api": VUOTNHANH_TOKEN, "url": link4m_url}
        r2 = requests.get(VUOTNHANH_API, params=params_vn, timeout=15)
        if r2.status_code != 200:
            raise RuntimeError(f"Vuotnhanh HTTP {r2.status_code}")
        data2 = r2.json()
        final_url = data2.get("shortenedUrl") if data2.get("status") == "success" else None
        final_url = final_url or link4m_url

        BOT_PENDING[str(chat_id)] = {
            "mode": "free",
            "created_at": now,
            "key": key,
            "link": final_url,
            "device_id": device_id,
            "expires_at": now + 24 * 3600,
        }
        _save_bot_pending()

        send_message(chat_id,
                     "🔑 ĐÃ TẠO LINK VƯỢT THÀNH CÔNG:\n\n"
                     f"{final_url}\n\n"
                     "📌 Sau khi có Key, gõ:\n"
                     "/login KEY_CUA_BAN\n\n"
                     "⏰ Key có hiệu lực trong 24h")
    except Exception as exc:
        _admin_only_exception_log(chat_id, "Lỗi tạo link FREE", exc)
        send_message(chat_id, "❌ Không tạo được link FREE. Vui lòng thử lại sau.")


_BOT_HANDLER_LOCKS = {}
_BOT_HANDLER_LOCKS_LOCK = threading.Lock()
_BOT_HANDLER_EXECUTOR = None


def _get_bot_handler_lock(chat_id):
    key = str(chat_id)
    with _BOT_HANDLER_LOCKS_LOCK:
        lock = _BOT_HANDLER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BOT_HANDLER_LOCKS[key] = lock
        return lock


def _handle_update_async(update):
    """Xử lý update trong worker thread để Telegram polling không bị chặn bởi API/tool."""
    try:
        if "callback_query" in update:
            query = update["callback_query"]
            chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
            chat_id = chat_id if chat_id is not None else ((query.get("from") or {}).get("id"))
            lock = _get_bot_handler_lock(chat_id) if chat_id is not None else threading.Lock()
            with lock:
                handle_callback(query)
        elif "message" in update:
            msg = update["message"]
            chat_id = (msg.get("chat") or {}).get("id")
            lock = _get_bot_handler_lock(chat_id) if chat_id is not None else threading.Lock()
            with lock:
                handle_message(msg)
    except Exception as exc:
        chat_id = None
        try:
            if "message" in update:
                chat_id = (update["message"].get("chat") or {}).get("id")
            elif "callback_query" in update:
                chat_id = ((update["callback_query"].get("message") or {}).get("chat") or {}).get("id")
        except Exception:
            pass
        _notify_admin_code_error("Telegram update handler lỗi", exc, chat_id=chat_id, tool_key="SYSTEM")


def bot_main():
    global _BOT_HANDLER_EXECUTOR, _BOT_WATCHDOG_THREAD
    if not BOT_TOKEN:
        raise SystemExit(
            'Chưa đặt BOT_TOKEN.\n'
            'Hãy cấu hình BOT_TOKEN trong Environment Variables của Render.'
        )
    try:
        tg("deleteWebhook", {"drop_pending_updates": False}, timeout=20)
    except Exception:
        pass

    print("BOT ĐANG CHẠY...", flush=True)
    start_bot_key_expiry_watcher()
    _mark_bot_poll_activity()
    _BOT_WATCHDOG_STOP.clear()
    if _BOT_WATCHDOG_THREAD is None or not _BOT_WATCHDOG_THREAD.is_alive():
        _BOT_WATCHDOG_THREAD = threading.Thread(
            target=_bot_poll_watchdog, daemon=True, name="telegram-poll-watchdog"
        )
        _BOT_WATCHDOG_THREAD.start()
    offset = None
    if _BOT_HANDLER_EXECUTOR is None:
        from concurrent.futures import ThreadPoolExecutor
        _BOT_HANDLER_EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="tg-handler")
    try:
        while not STOP_ALL.is_set():
            try:
                _mark_bot_poll_activity()
                result = updates(offset)
                _mark_bot_poll_activity()
                for update in result or []:
                    offset = int(update["update_id"]) + 1
                    _BOT_HANDLER_EXECUTOR.submit(_handle_update_async, update)
            except KeyboardInterrupt:
                break
            except requests.exceptions.HTTPError as exc:
                _mark_bot_poll_activity()
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code == 409:
                    # Một instance Telegram khác đang giữ getUpdates.
                    # Không gửi traceback cho Admin liên tục; chờ rồi thử lại.
                    print("[Telegram] 409 Conflict: đang chờ poller khác nhả kết nối...", flush=True)
                    time.sleep(7)
                    continue
                _notify_admin_code_error("Telegram polling HTTP lỗi", exc, tool_key="SYSTEM")
                time.sleep(3)
            except Exception as exc:
                _mark_bot_poll_activity()
                _notify_admin_code_error("Telegram polling lỗi", exc, tool_key="SYSTEM")
                time.sleep(3)
    finally:
        STOP_ALL.set()
        _BOT_WATCHDOG_STOP.set()
        stop_bot_key_expiry_watcher()
        _stop_render_health_server()
        with SESSIONS_LOCK:
            sessions = list(SESSIONS.values())
            SESSIONS.clear()
        for session in sessions:
            try:
                session.stop()
            except Exception:
                pass
        if _BOT_HANDLER_EXECUTOR is not None:
            try:
                _BOT_HANDLER_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                _BOT_HANDLER_EXECUTOR.shutdown(wait=False)
            except Exception:
                pass
            _BOT_HANDLER_EXECUTOR = None


def run_worker(tool_key, chat_id=None):
    mapping = {
        "vth": "main_vth",
        "lotto": "main_lotto",
        "cdtd": "main_cdtd",
        "xworld": "xw_main",
        "hilo": "hilo_main",
    }
    fn_name = mapping.get(str(tool_key).strip().lower())
    if not fn_name:
        raise SystemExit(f"Tool không hợp lệ: {tool_key}")
    fn = globals().get(fn_name)
    if not callable(fn):
        raise SystemExit(f"Không tìm thấy {fn_name} trong engine.")

    global _WORKER_CHAT_ID
    _WORKER_CHAT_ID = str(chat_id or "").strip() or None
    _protect_worker_output_streams()

    # Mỗi worker có vùng dữ liệu riêng theo Telegram chat ID.
    chat_id = _WORKER_CHAT_ID
    if chat_id:
        safe_chat = re.sub(r"[^0-9A-Za-z_-]+", "_", chat_id)
        worker_dir = BOT_DATA_DIR / "telegram_workers" / f"tg_{safe_chat}"
        try:
            worker_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            worker_dir = BOT_DATA_DIR

        global ACCOUNTS_FILE, CONFIG_FILE, XW_ACCOUNT_FILE, XW_LOG_FILE, STRATEGY_CONFIG_FILE
        ACCOUNTS_FILE = str(worker_dir / "accounts.json")
        CONFIG_FILE = str(worker_dir / "wh_config.json")
        XW_ACCOUNT_FILE = str(worker_dir / "data_xw_confirm_code.txt")
        XW_LOG_FILE = str(worker_dir / "history_nhap_code.log")
        STRATEGY_CONFIG_FILE = str(worker_dir / "strategy.json")

    # Supervisor cho TẤT CẢ tool Telegram.
    # Một exception runtime bất ngờ chỉ làm hỏng lần chạy hiện tại; worker sẽ
    # tự khởi động lại tool thay vì kết thúc ngay, đồng thời vẫn báo chi tiết
    # cho Admin. SystemExit(0)/KeyboardInterrupt vẫn được coi là dừng bình thường.
    consecutive_failures = 0
    last_failure_ts = 0.0
    max_consecutive_failures = 10

    while True:
        try:
            fn()
            return
        except KeyboardInterrupt:
            return
        except SystemExit as exc:
            code = exc.code
            if code in (None, 0, "0"):
                return

            now = time.time()
            if now - last_failure_ts > 90:
                consecutive_failures = 0
            consecutive_failures += 1
            last_failure_ts = now

            _notify_admin_code_error(
                f"Worker {tool_key.upper()} kết thúc bằng SystemExit, auto-restart lần {consecutive_failures}",
                exc,
                chat_id=chat_id,
                tool_key=tool_key,
            )

            if consecutive_failures > max_consecutive_failures:
                _notify_admin_code_error(
                    f"Worker {tool_key.upper()} vượt quá {max_consecutive_failures} lần auto-restart liên tiếp",
                    exc,
                    chat_id=chat_id,
                    tool_key=tool_key,
                )
                raise SystemExit(1)

            try:
                print(
                    f"🔄 Tool gặp lỗi tạm thời, đang tự khởi động lại... ({consecutive_failures}/{max_consecutive_failures})",
                    flush=True,
                )
            except Exception:
                pass
            time.sleep(min(2.0 * consecutive_failures, 10.0))
        except BrokenPipeError:
            # stdout/stderr của worker không còn ở phía parent -> dừng worker bình thường.
            # Không auto-restart vì restart trong tình huống này sẽ lặp vô hạn.
            return
        except Exception as exc:
            now = time.time()
            # Nếu lần lỗi trước cách đủ lâu, coi đây là một đợt lỗi mới.
            if now - last_failure_ts > 90:
                consecutive_failures = 0
            consecutive_failures += 1
            last_failure_ts = now

            _notify_admin_code_error(
                f"Worker {tool_key.upper()} gặp lỗi runtime, auto-restart lần {consecutive_failures}",
                exc,
                chat_id=chat_id,
                tool_key=tool_key,
            )

            if consecutive_failures > max_consecutive_failures:
                _notify_admin_code_error(
                    f"Worker {tool_key.upper()} vượt quá {max_consecutive_failures} lần auto-restart liên tiếp",
                    exc,
                    chat_id=chat_id,
                    tool_key=tool_key,
                )
                raise SystemExit(1)

            try:
                print(
                    f"🔄 Tool gặp lỗi tạm thời, đang tự khởi động lại... ({consecutive_failures}/{max_consecutive_failures})",
                    flush=True,
                )
            except Exception:
                pass

            # Backoff ngắn để tránh vòng lặp restart quá nhanh khi API đang lỗi.
            time.sleep(min(2.0 * consecutive_failures, 10.0))


# ==================== EMBEDDED ENGINE ====================

import sys
import subprocess
import importlib

REQUIRED_PACKAGES = {
    "requests": "requests",
    "pytz": "pytz",
    "websocket": "websocket-client",
    "colorama": "colorama",
}


def install_missing_packages():
    missing = []

    for module, package in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if not missing:
        return

    print("")

    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                *missing,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


install_missing_packages()

import base64
import hashlib
import io
import json
import logging
import math
import os
import platform
import random
import re
import secrets
import threading
import json
import subprocess
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import uuid
import webbrowser

from collections import Counter, defaultdict, deque
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    wait,
    FIRST_COMPLETED,
)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


import pytz
import requests
import urllib3
import websocket

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class _BotConsole:
    width = 80
    def print(self, *objects, sep=" ", end="\n", **kwargs):

        return None
    def clear(self):
        return None

class Text(str):
    def __new__(cls, text="", *args, **kwargs):
        obj = super().__new__(cls, str(text))
        obj._parts = [str(text)] if str(text) else []
        return obj

    def append(self, value, style=None, *args, **kwargs):
        # Tương thích tối thiểu với rich.text.Text trong BOT-ONLY mode.
        # Text ở chế độ này chỉ cần giữ nội dung để tránh lỗi runtime.
        self._parts.append(str(value))
        return None

    @classmethod
    def assemble(cls, *parts):
        out=[]
        for part in parts:
            if isinstance(part, (tuple,list)) and part:
                out.append(str(part[0]))
            else:
                out.append(str(part))
        return cls("".join(out))

    def __str__(self):
        parts = getattr(self, "_parts", None)
        return "".join(parts) if parts is not None else super().__str__()

class _Box:
    ROUNDED = HEAVY = DOUBLE = SIMPLE_HEAVY = None
box = _Box()

class Align:
    @staticmethod
    def center(value, *args, **kwargs): return str(value)
    @staticmethod
    def left(value, *args, **kwargs): return str(value)

class Group:
    def __init__(self, *items): self.items = list(items)
    def __str__(self): return "\n".join(str(x) for x in self.items if str(x))

class Columns:
    def __init__(self, items, *args, **kwargs): self.items=list(items)
    def __str__(self): return "\n".join(str(x) for x in self.items)

class Table:
    def __init__(self, *args, **kwargs): self.rows=[]; self.columns=[]
    @classmethod
    def grid(cls, *args, **kwargs): return cls()
    def add_column(self, *args, **kwargs): self.columns.append(kwargs.get("header", args[0] if args else ""))
    def add_row(self, *values, **kwargs): self.rows.append([str(v) for v in values])
    def __str__(self): return "\n".join(" | ".join(row) for row in self.rows)

class Layout:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name")
        self.rows = []
        self.value = ""

    def split_row(self, *args):
        self.rows.extend(args)
        return self

    def split_column(self, *args):
        self.rows.extend(args)
        return self

    def update(self, value):
        self.value = value
        return self

    def __getitem__(self, name):
        name = str(name)
        if self.name == name:
            return self
        for child in self.rows:
            if isinstance(child, Layout):
                try:
                    return child[name]
                except KeyError:
                    pass
        raise KeyError(name)

    def __str__(self):
        parts = []
        if getattr(self, "value", None):
            parts.append(str(self.value))
        parts.extend(str(x) for x in getattr(self, "rows", []))
        return "\n".join(p for p in parts if p)

class Panel:
    def __init__(self, renderable="", *args, **kwargs): self.renderable=renderable; self.title=kwargs.get("title")
    def __str__(self): return str(self.renderable)

class Rule:
    def __init__(self, title="", *args, **kwargs): self.title=str(title)
    def __str__(self): return ""

class Live:
    def __init__(self, renderable=None, *args, **kwargs): self.renderable=renderable
    def __enter__(self): return self
    def update(self, renderable, *args, **kwargs): self.renderable=renderable
    def __exit__(self, *exc): return False


class _HiloBotLive:
    
    def __init__(self, state):
        self.state = state
        self.renderable = None
        self._last_signature = None
        self._last_emit = 0.0

    def __enter__(self):
        self._emit(force=True)
        return self

    def update(self, renderable=None, *args, **kwargs):
        self.renderable = renderable
        self._emit(force=False)

    def _emit(self, force=False):
        try:
            state = self.state or {}
            balance = state.get("balance", 0)
            current_bet = state.get("current_bet", 0)
            original = state.get("original_balance", 0)
            try:
                profit = float(balance) - float(original)
            except Exception:
                profit = 0.0

            stats = state.get("model_stats") or {}
            current_card = state.get("current_card")
            card_text = hilo__hilo_rank_label(current_card) if current_card is not None else "—"
            last_issue = state.get("last_issue", "—")
            result = state.get("last_result", "—")
            prediction = state.get("next_prediction", "—")
            log_text = str(state.get("log_text", "—"))
            history = state.get("history") or []
            last_hist = history[-1] if history else {}

            signature = (
                str(last_issue), result, card_text, str(prediction), log_text,
                round(float(balance or 0), 8), round(float(current_bet or 0), 8),
                int(stats.get("wins", 0) or 0), int(stats.get("losses", 0) or 0),
                str(last_hist.get("issue_id", "")), str(last_hist.get("result", "")),
            )
            now = time.time()
            if not force and signature == self._last_signature and now - self._last_emit < 5.0:
                return

            if not force and signature == self._last_signature:
                return

            self._last_signature = signature
            self._last_emit = now
            print(
                "🃏 HILO\n"
                f"👤 User: {state.get('account_id', '—')}\n"
                f"💰 Số dư: {float(balance or 0):.2f} | Lãi/Lỗ: {profit:+.2f}\n"
                f"🎴 Lá hiện tại: {card_text}\n"
                f"🎯 Dự đoán: {prediction}\n"
                f"💵 Cược: {float(current_bet or 0):.2f}\n"
                f"📊 Kết quả: {result} | Win {stats.get('wins', 0)} - Lose {stats.get('losses', 0)}\n"
                f"📝 {log_text}",
                flush=True,
            )
        except Exception as exc:
            try:
                _notify_admin_code_error(
                    "HILO Telegram Live output lỗi",
                    exc,
                    chat_id=str(_WORKER_CHAT_ID or ""),
                    tool_key="hilo",
                )
            except Exception:
                pass

    def __exit__(self, *exc):
        return False

class Prompt:
    @staticmethod
    def ask(prompt="", default=None, **kwargs):
        suffix = "" if default is None else f" [{default}]"
        value=input(str(prompt)+suffix+": ")
        return default if value == "" and default is not None else value

class IntPrompt:
    @staticmethod
    def ask(prompt="", default=None, **kwargs):
        while True:
            value=Prompt.ask(prompt, default=default)
            try: return int(value)
            except (TypeError,ValueError): continue

class FloatPrompt:
    @staticmethod
    def ask(prompt="", default=None, **kwargs):
        while True:
            value=Prompt.ask(prompt, default=default)
            try: return float(value)
            except (TypeError,ValueError): continue

console = _BotConsole()

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)



tool_start_time = time.time()

processed_issue_ids = set()

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass


def force_exit_vbtool(code: int = 0):
    try:
        if threading.current_thread() is threading.main_thread():
            raise SystemExit(code)
        os._exit(code)
    except SystemExit:
        raise
    except Exception:
        try:
            os._exit(code)
        except Exception:
            raise

def show_key_expired_message_and_exit(delay: float = 2.0):
    try:
        console.print("\n❌ KEY ĐÃ HẾT HẠN!")
        console.print("⚠️ Vui lòng nhập key khác để được sử dụng.")

    except Exception:
        pass

    try:
        time.sleep(delay)
    except Exception:
        pass

    force_exit_vbtool(0)

tz = pytz.timezone("Asia/Ho_Chi_Minh")

VBTOOL_COLORS = {k: "" for k in (
    "gold","gold_dark","platinum","diamond","ruby","emerald","sapphire",
    "amethyst","onyx","rose","history_blue","neon_pink","neon_green",
    "neon_orange","crimson","turquoise","lavender","bright_blue","dark_blue",
    "bright_cyan","bold_cyan","neon_blue","white","bright_green","yellow",
    "red","green","bold_green") }
ICONS = {
    "user": "👤",
    "phone": "📱",
    "chart": "📊",
    "check": "✅",
    "clock": "⏱️",
    "diamond": "💎",
    "dice": "🎲",
    "fire": "🔥",
    "money": "💰",
    "target": "🎯",
    "trophy": "🏆",
    "brain": "🧠",
}

LOGO_VBTOOL = ""
LOGO_ULTIMATE = ""

# ================== HỆ THỐNG KEY ==================

DEVICE_ID_FILE = "device_id.txt"
SALT = "VBTOOLKEY"
TEMP_KEY_FILE = str(BOT_DATA_DIR / ".temp_key.txt")
ACTIVATION_FILE = str(BOT_DATA_DIR / "activation.dat")
LINK_HISTORY_FILE = str(BOT_DATA_DIR / "link_history.json")

VUOTNHANH_API = "https://vuotnhanh.com/api"
LINK4M_API = "https://link4m.co/api-shorten/v2"
LINK4M_TOKEN = os.getenv("LINK4M_TOKEN", "6a2391ae3f35952ad60cb3cf").strip()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "fd9a7822bac3990bd0e7e86de2317df0ded0c419ba6a4588baffbedecd06").strip()
VUOTNHANH_TOKEN = os.getenv("VUOTNHANH_TOKEN", "2e82a116-da12-4344-8eb0-936c9274a2f1").strip()

ENCRYPTION_KEY = hashlib.sha256(b"VBTool_VIP").digest()

#========Telegaph================
def create_telegraph_page(key, device_id):
    url = "https://api.telegra.ph/createPage"

    content = json.dumps([
        {
            "tag": "p",
            "children": [
                "🔑 VBTOOL KEY"
            ]
        },
        {
            "tag": "p",
            "children": [
                f"📱 Device ID: {device_id}"
            ]
        },
        {
            "tag": "p",
            "children": [
                f"🔑 Key: {key}"
            ]
        },
        {
            "tag": "p",
            "children": [
                "⚠️ Key chỉ sử dụng trên tài khoản này."
            ]
        },
        {
            "tag": "p",
            "children": [
                "⚠️ Key chỉ được kích hoạt 1 lần, 1 tài khoản duy nhất."
            ]
        }
    ])

    data = {
        "access_token": ACCESS_TOKEN,
        "title": "KEY 24H",
        "author_name": "VBTOOL",
        "content": content,
        "return_content": False
    }

    r = requests.post(url, data=data)

    if r.status_code == 200:
        result = r.json()
        if result["ok"]:
            return result["result"]["url"]

    return None
    
# ================== KEY SERVER ==================
KEY_SERVER_URL = os.getenv("KEY_SERVER_URL", "https://keyvip-sever1.onrender.com").rstrip("/")
KEY_SERVER_ADMIN_SECRET = os.getenv("KEY_SERVER_ADMIN_SECRET", 'VBTOOL_VANBAO999').strip()
# ================== HÀM MÃ HÓA ==================


def simple_decrypt(encrypted_str: str) -> Optional[dict]:
    try:
        if not encrypted_str:
            return None
        parts = encrypted_str.split(':', 1)
        if len(parts) != 2:
            return None
        stored_checksum, encrypted_data = parts
        key = ENCRYPTION_KEY
        encrypted = base64.b64decode(encrypted_data.encode())
        decrypted = bytearray()
        for i, char in enumerate(encrypted):
            decrypted.append(char ^ key[i % len(key)])
        json_str = decrypted.decode('utf-8')
        if hashlib.sha256(json_str.encode()).hexdigest() != stored_checksum:
            return None
        return json.loads(json_str)
    except Exception:
        return None

# ================== QUẢN LÝ KEY ==================

def get_device_id() -> str:
    if Path(DEVICE_ID_FILE).exists():
        try:
            with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    new_id = str(uuid.uuid4()).replace("-", "")[:32]
    try:
        with open(DEVICE_ID_FILE, "w", encoding="utf-8") as f:
            f.write(new_id)
    except Exception:
        pass
    return new_id


def check_internet_connection() -> bool:
    try:
        requests.get("https://www.google.com", timeout=3)
        return True
    except Exception:
        return False
def is_vip_activated() -> bool:
    """Kiểm tra quyền VIP theo đúng hồ sơ kích hoạt hiện tại.

    Trong Telegram worker, check_activation_valid() lấy quyền trực tiếp
    từ _WORKER_CHAT_ID/BOT_AUTH của từng người dùng, nên VTH phải dùng
    kết quả này thay vì đọc một biến môi trường không tồn tại.
    """
    try:
        is_valid, _, is_vip = check_activation_valid(exit_on_expired=False)
        return bool(is_valid and is_vip)
    except Exception:
        return False

def verify_vip_key_from_server(device_id: str, key: str, quiet: bool = False, telegram_id=None) -> Optional[Dict]:
    """Kiểm tra key VIP trên server.

    quiet=True dùng cho background monitor để không chen thông báo vào
    Prompt/Setting của người dùng. Chỉ luồng nhập key trực tiếp mới in lỗi.
    """
    if not check_internet_connection():
        if not quiet:
            console.print("[bold red]❌ Không có kết nối internet![/bold red]")
        return None

    try:
        effective_device_id = str(device_id or "").strip()
        telegram_id = str(telegram_id or _WORKER_CHAT_ID or "").strip()
        key = str(key or "").strip().upper()
        if not key:
            return {"status": "INVALID", "message": "Thiếu key", "_server_success": False}
        if telegram_id:
            payload = {"telegram_id": telegram_id, "key": key}
            url = f"{KEY_SERVER_URL}/api/telegram/verify_key"
        else:
            payload = {"device_id": effective_device_id, "key": key}
            url = f"{KEY_SERVER_URL}/api/verify_key"
        resp = requests.post(url, json=payload, timeout=15)

        if resp.status_code != 200:
            if not quiet:
                console.print(f"[bold red]❌ HTTP Lỗi: {resp.status_code}[/bold red]")
            return None

        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            return None

        status = str(data.get("status", "")).strip().upper()
        if data.get("success"):
            return {
                "status": status or "ACTIVE",
                "_server_success": True,
                "duration_hours": data.get("duration", 24),
                "key_type": data.get("key_type", "VIP"),
                "server_time": data.get("server_time", datetime.now().isoformat()),
                "is_vip": True,
                "is_forever": bool(data.get("is_forever", False)),
                "expires_at": data.get("expires_at"),
                "telegram_id": data.get("telegram_id"),
                "already_activated_same_account": bool(data.get("already_activated_same_account", False)),
            }

        result = {
            "status": status or "INVALID",
            "_server_success": False,
            "message": data.get("message"),
            "duration_hours": data.get("duration"),
            "key_type": data.get("key_type"),
            "expires_at": data.get("expires_at"),
            "is_forever": bool(data.get("is_forever", False)),
        }

        # Chỉ in các trạng thái do người dùng chủ động kiểm tra.
        # Background monitor gọi quiet=True nên tuyệt đối không in "Key không tồn tại".
        if not quiet:
            if status == "REVOKED":
                console.print("[bold red]❌ KEY ĐÃ BỊ THU HỒI![/bold red]")
            elif status == "EXPIRED":
                console.print("[bold yellow]❌ KEY ĐÃ HẾT HẠN![/bold yellow]")
            elif status == "DEVICE_MISMATCH":
                console.print("[bold red]❌ KEY ĐÃ ĐƯỢC KÍCH HOẠT TRÊN THIẾT BỊ KHÁC![/bold red]")
            else:
                console.print(f"[bold red]❌ {data.get('message', 'Key không hợp lệ')}[/bold red]")
        return result

    except requests.exceptions.Timeout:
        if not quiet:
            console.print("[bold red]❌ Hết thời gian kiểm tra key.[/bold red]")
        return None
    except Exception as e:
        if not quiet:
            console.print(f"[bold red]❌ Lỗi kiểm tra key: {e}[/bold red]")
        else:
            log_debug(f"VIP verify background error: {e}")
        return None




def remove_local_activation(revoked: bool = False):
    """Xóa thông tin kích hoạt cục bộ khi key hết hạn/thu hồi."""
    try:
        if Path(ACTIVATION_FILE).exists():
            os.remove(ACTIVATION_FILE)
    except Exception:
        pass

    # Chỉ dọn dữ liệu link tạm nếu đó là key FREE.
    if not revoked:
        try:
            clear_link_history()
            if Path(TEMP_KEY_FILE).exists():
                os.remove(TEMP_KEY_FILE)
        except Exception:
            pass
        


def clear_link_history():
    try:
        if Path(LINK_HISTORY_FILE).exists():
            os.remove(LINK_HISTORY_FILE)
    except Exception:
        pass


# ================== HÀM ACTIVATION ==================


def load_activation() -> Optional[Dict]:
    # Telegram worker phải chỉ dùng key của đúng Telegram ID đang chạy.
    # Nếu hồ sơ Telegram không còn/hết hạn, tuyệt đối không fallback sang
    # activation.dat cục bộ dùng chung vì có thể vô tình cấp quyền cho worker.
    if _WORKER_CHAT_ID:
        try:
            return _load_worker_activation()
        except Exception:
            return None

    if not Path(ACTIVATION_FILE).exists():
        return None
    try:
        with open(ACTIVATION_FILE, "r", encoding="utf-8") as f:
            encrypted_data = f.read().strip()
        if not encrypted_data:
            return None
        data = simple_decrypt(encrypted_data)
        if not data:
            try:
                os.remove(ACTIVATION_FILE)
            except Exception:
                pass
            return None
        expected_checksum = hashlib.sha256(
            f"{data.get('device_id', '')}:{data.get('key', '')}:{data.get('activation_time', '')}:{SALT}".encode()
        ).hexdigest()
        if data.get("checksum") != expected_checksum:
            try:
                os.remove(ACTIVATION_FILE)
            except Exception:
                pass
            return None
        return data
    except Exception:
        return None

def _load_worker_activation() -> Optional[Dict]:
    try:
        if not _WORKER_CHAT_ID:
            return None
        rec = _bot_auth_record(_WORKER_CHAT_ID)
        if not isinstance(rec, dict):
            return None
        if rec.get("__expired__"):
            return None
        expires_at = float(rec.get("expires_at", 0) or 0)
        if expires_at and time.time() >= expires_at:
            return None
        is_forever = bool(rec.get("is_forever"))
        return {
            "device_id": get_device_id(),
            "key": rec.get("key", ""),
            "key_type": str(rec.get("key_type", "FREE")).upper(),
            "activation_time": datetime.now().isoformat(),
            "expiry_time": datetime.fromtimestamp(expires_at).isoformat() if expires_at else None,
            "duration_hours": rec.get("duration_hours"),
            "is_forever": is_forever,
            "is_vip": str(rec.get("key_type", "FREE")).upper() == "VIP",
            "_telegram_bot_auth": True,
            "server_device_id": rec.get("device_id"),
        }
    except Exception:
        return None

def check_activation_valid(exit_on_expired: bool = True) -> Tuple[bool, Optional[str], bool]:
    # Worker luôn lấy quyền trực tiếp từ hồ sơ Telegram theo chat ID.
    if _WORKER_CHAT_ID:
        rec = _bot_auth_record(_WORKER_CHAT_ID)
        if not rec:
            # Không có hồ sơ key nghĩa là chưa xác thực, key đã bị thu hồi,
            # hoặc watcher vừa xóa key hết hạn. Không được mặc định cấp FREE.
            return False, "CHƯA CÓ KEY HỢP LỆ — VUI LÒNG NHẬP KEY FREE HOẶC VIP", False
        try:
            if rec.get("__expired__"):
                return False, "KEY ĐÃ HẾT HẠN", False

            key_type = str(rec.get("key_type", "FREE")).strip().upper()
            expires_at = float(rec.get("expires_at", 0) or 0)
            if expires_at and time.time() >= expires_at:
                return False, "KEY ĐÃ HẾT HẠN", False
            is_vip = key_type == "VIP"
            remaining = max(0.0, expires_at - time.time()) if expires_at else 0.0
            if remaining:
                return True, f"Key {key_type} còn {remaining / 3600.0:.1f} giờ", is_vip
            return True, f"Key {key_type} đang hoạt động", is_vip
        except Exception as exc:
            return False, f"Key Telegram không hợp lệ: {exc}", False

    activation = load_activation()
    if not activation:
        return False, "Chưa kích hoạt", False
    try:
        current_device = get_device_id()
        if not activation.get("_telegram_bot_auth") and activation.get("device_id") != current_device:
            try:
                os.remove(ACTIVATION_FILE)
                console.print("[bold red]❌ Device ID không khớp! Key đã dùng trên máy khác.[/bold red]")
            except Exception:
                pass
            return False, "Device ID không khớp", False
        key_type = activation.get("key_type", "FREE")
        # Mọi key VIP cũ dạng vĩnh viễn/thiếu hạn đều bị chuyển sang tối đa 1 năm.
        expiry_raw = activation.get("expiry_time")
        expiry_time = datetime.fromisoformat(expiry_raw) if expiry_raw else None
        if key_type == "VIP" and (activation.get("is_forever") or expiry_time is None):
            activation_raw = activation.get("activation_time")
            try:
                activation_time = datetime.fromisoformat(str(activation_raw)) if activation_raw else None
            except Exception:
                activation_time = None
            if activation_time is None:
                expiry_time = datetime.now() - timedelta(seconds=1)
            else:
                expiry_time = activation_time + timedelta(hours=8760)
            activation["is_forever"] = False
            activation["expiry_time"] = expiry_time.isoformat()
            activation["duration_hours"] = 8760
        is_forever = False
        now = datetime.now()
        if key_type == "FREE" and not activation.get("_telegram_bot_auth"):
            tomorrow = now + timedelta(days=1)
            next_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
            if now >= next_midnight:
                try:
                    os.remove(ACTIVATION_FILE)
                    clear_link_history()
                    if Path(TEMP_KEY_FILE).exists():
                        os.remove(TEMP_KEY_FILE)
                    console.print("[bold yellow] Key free đã reset lúc 00h! Nhập key mới.[/bold yellow]")
                except Exception:
                    pass
                return False, "Key free đã reset", False
        if not is_forever and expiry_time is not None and now > expiry_time:
            try:
                os.remove(ACTIVATION_FILE)

                if key_type == "FREE":
                    clear_link_history()
                    if Path(TEMP_KEY_FILE).exists():
                        os.remove(TEMP_KEY_FILE)
            except Exception:
                pass

            if exit_on_expired:
                show_key_expired_message_and_exit(2)
            return False, "KEY ĐÃ HẾT HẠN", False
        is_vip = activation.get("is_vip", False)
        if expiry_time is None:
            return False, "Thiếu thông tin thời hạn key", False
        remaining = expiry_time - now
        days = remaining.total_seconds() / 86400
        hours = remaining.total_seconds() / 3600

        if days >= 1:
            return True, f"Còn {days:.1f} ngày", is_vip
        else:
            return True, f"Còn {hours:.1f} giờ", is_vip
    except Exception as e:
        try:
            os.remove(ACTIVATION_FILE)
        except Exception:
            pass
        return False, f"Lỗi: {e}", False

_key_monitor_started = False

def start_key_expiry_monitor():
    global _key_monitor_started
    # Telegram đã có watcher trung tâm của BotSession để xử lý đồng nhất
    # cho mọi tool; tránh worker tự dừng trước khi watcher gửi thông báo đúng loại key.
    if _WORKER_CHAT_ID:
        return
    if _key_monitor_started:
        return
    _key_monitor_started = True
    threading.Thread(target=key_expiry_monitor, daemon=True).start()

def _activation_is_expired_or_reset() -> bool:
    activation = load_activation()
    if not activation:
        return False
    try:
        expiry_raw = activation.get("expiry_time")
        if not expiry_raw:
            if str(activation.get("key_type", "")).upper() == "VIP":
                activation_raw = activation.get("activation_time")
                try:
                    activation_time = datetime.fromisoformat(str(activation_raw)) if activation_raw else None
                except Exception:
                    activation_time = None
                if activation_time is None:
                    return True
                expiry_time = activation_time + timedelta(hours=8760)
            else:
                return False
        else:
            expiry_time = datetime.fromisoformat(expiry_raw)
        now = datetime.now()
        if now > expiry_time:
            return True
        if activation.get("key_type", "FREE") == "FREE":
            tomorrow = now + timedelta(days=1)
            next_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
            if now >= next_midnight:
                return True
    except Exception:
        return False
    return False

def key_expiry_monitor():
    """Theo dõi key nền nhưng không làm nhiễu giao diện.

    Chỉ xử lý khi server xác nhận rõ trạng thái terminal liên tiếp 2 lần.
    Lỗi mạng/HTTP/INVALID tạm thời sẽ bị bỏ qua, không tự xóa key.
    """
    server_check_interval = 10.0
    last_server_check = 0.0
    terminal_seen = {"REVOKED": 0, "EXPIRED": 0, "DEVICE_MISMATCH": 0}

    while True:
        try:
            if _activation_is_expired_or_reset():
                try:
                    check_activation_valid(exit_on_expired=False)
                except Exception:
                    pass
                try:
                    _shutdown_vth_runtime("[bold yellow]Key đã hết hạn[/bold yellow]")
                except Exception:
                    pass
                return

            now = time.time()
            if now - last_server_check >= server_check_interval:
                last_server_check = now
                activation = load_activation()
                if activation and activation.get("key_type") == "VIP":
                    key = str(activation.get("key", "")).strip()
                    device_id = get_device_id()
                    result = verify_vip_key_from_server(device_id, key, quiet=True) if key else None
                    status = str((result or {}).get("status", "")).upper()

                    if status in terminal_seen:
                        for other in terminal_seen:
                            if other != status:
                                terminal_seen[other] = 0
                        terminal_seen[status] += 1
                    else:
                        for other in terminal_seen:
                            terminal_seen[other] = 0

                    if status == "REVOKED" and terminal_seen[status] >= 2:
                        remove_local_activation(revoked=True)
                        _shutdown_vth_runtime("[bold red]❌ KEY ĐÃ BỊ THU HỒI![/bold red]")
                        return

                    if status == "EXPIRED" and terminal_seen[status] >= 2:
                        remove_local_activation(revoked=False)
                        _shutdown_vth_runtime("[bold yellow]❌ KEY ĐÃ HẾT HẠN![/bold yellow]")
                        return

                    if status == "DEVICE_MISMATCH" and terminal_seen[status] >= 2:
                        remove_local_activation(revoked=True)
                        _shutdown_vth_runtime("[bold red]❌ KEY KHÔNG THUỘC THIẾT BỊ NÀY![/bold red]")
                        return
        except Exception as e:
            log_debug(f"key_expiry_monitor error: {e}")

        time.sleep(1)


# ================== TOOL 1: VUA THOÁT HIỂM ==================

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

BET_API_URL = "https://api.escapemaster.net/escape_game/bet"
WS_URL = "wss://api.escapemaster.net/escape_master/ws"
WALLET_API_URL = "https://wallet.3games.io/api/wallet/user_asset"

HTTP = requests.Session()
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=Retry(total=3, backoff_factor=0.2, status_forcelist=(500, 502, 503, 504)))
    HTTP.mount("https://", adapter)
    HTTP.mount("http://", adapter)
except Exception:
    pass

ROOM_NAMES = {1: "📦 Nhà kho", 2: "🪑 Phòng họp", 3: "👔 Phòng giám đốc", 4: "💬 Phòng trò chuyện", 5: "🎥 Phòng giám sát", 6: "🏢 Văn phòng", 7: "💰 Phòng tài vụ", 8: "👥 Phòng nhân sự"}
ROOM_ORDER = [1, 2, 3, 4, 5, 6, 7, 8]

USER_ID: Optional[int] = None
SECRET_KEY: Optional[str] = None
IS_VIP_USER: bool = False
issue_id: Optional[int] = None
issue_start_ts: Optional[float] = None
issue_end_ts: Optional[float] = None
count_down: Optional[int] = None
killed_room: Optional[int] = None
round_index: int = 0

room_state: Dict[int, Dict[str, Any]] = {r: {"players": 0, "bet": 0} for r in ROOM_ORDER}
room_stats: Dict[int, Dict[str, Any]] = {r: {"kills": 0, "survives": 0, "last_kill_round": None, "last_players": 0, "last_bet": 0} for r in ROOM_ORDER}

predicted_room: Optional[int] = None
last_killed_room: Optional[int] = None
last_killed_room_delayed: Optional[int] = None
prediction_locked: bool = False

current_build: Optional[float] = None
current_usdt: Optional[float] = None
current_world: Optional[float] = None
last_balance_ts: Optional[float] = None
last_balance_val: Optional[float] = None
starting_balance: Optional[float] = None
cumulative_profit: Optional[float] = None
# PNL Telegram = BUILD lãi/lỗ thực tế đã chốt so với số dư gốc.

win_streak: int = 0
lose_streak: int = 0
max_win_streak: int = 0
max_lose_streak: int = 0

base_bet: float = 1.0
multiplier: float = 2.0
current_bet: Optional[float] = None
last_display_bet: float = 0.0
run_mode: str = "AUTO"
bet_rounds_before_skip: int = 0
_rounds_placed_since_skip: int = 0
skip_next_round_flag: bool = False

bet_history: deque = deque(maxlen=200)
bet_sent_for_issue: set = set()

pause_after_losses: int = 0
_skip_rounds_remaining: int = 0
profit_target: Optional[float] = None
stop_when_profit_reached: bool = False
stop_loss_target: Optional[float] = None
stop_when_loss_reached: bool = False
stop_flag: bool = False
_vth_daily_pause_window = None

ui_state: str = "IDLE"
analysis_duration: float = 45.0
analysis_initial_delay: float = 1.5
analysis_ready_ts: Optional[float] = None
analysis_start_ts: Optional[float] = None

def _vth_get_remaining_seconds() -> Optional[float]:
    """
    UI chỉ chạy theo countdown server vừa gửi.
    Không tự trôi theo đồng hồ nội bộ giữa các lần cập nhật.
    """
    try:
        if count_down is not None:
            return max(0.0, float(count_down))
    except Exception:
        pass
    return None

last_msg_ts: float = time.time()
last_balance_fetch_ts: float = 0.0
BALANCE_POLL_INTERVAL: float = 4.0
_ws: Dict[str, Any] = {"ws": None}
active_vth_poller: Optional[Any] = None
# Hoãn kết thúc VTH sau khi vừa chạm mục tiêu/cắt lỗ để Telegram
# kịp gửi bản tin WIN/LOSE của chính ván cuối cùng.
capital_stop_pending: bool = False

def _shutdown_vth_runtime(message: Optional[str] = None):
    global stop_flag, active_vth_poller, capital_stop_pending
    stop_flag = True
    capital_stop_pending = False

    if message:
        try:
            console.print(message)
        except Exception:
            pass

    poller = active_vth_poller
    active_vth_poller = None
    if poller is not None:
        try:
            poller.stop()
        except Exception:
            pass

    try:
        wsobj = _ws.get("ws")
        if wsobj:
            wsobj.close()
    except Exception:
        pass

_sequential_bet_index = 0
killer_history = deque(maxlen=20)
game_kill_log = deque(maxlen=100)
processed_result_issues = set()
history_recorded_issue = set()
bet_selection_by_issue: Dict[int, Dict[str, Any]] = {}

FREE_AI_LIST = [
    "RANDOM",
    "MIN_PLAYER_BET",
    "PROBABILITY",
    "FOLLOW_KILLER",
    "SEQUENTIAL",
    "KILLER_PERSONALITY",
    "SMART_SAFE",
    "FOLLOW_KILLER_DELAYED",
    "HIDE_SEEK_MASTER",
    "BALANCE",
    "MOST_PLAYERS",
    "LEAST_PLAYERS",
    "RICHEST",
    "POOREST",
    "AVOID_RESULT",
    "MEDIAN",
    "PATTERN",
    "ALL",
]

VIP_AI_LIST = [
    "KILLER_WAVE", "PSYCHO_ANALYSIS", "MARKOV_CHAIN", "DEEP_LEARNING",
    "REINFORCEMENT", "BAYESIAN", "K_MEANS", "NEURAL", "FUZZY", "GENETIC",
    "ANT_COLONY", "PARTICLE_SWARM", "KNN", "DECISION_TREE", "RANDOM_FOREST",
    "GRADIENT_BOOST", "LSTM", "TRANSFORMER", "ENSEMBLE", "CYCLE_ANALYSIS",
    "TREND_ANALYSIS", "SAFE_RISK", "TONG_HOP",
]

SELECTION_MODES = {
    "RANDOM": "1. RANDOM (FREE)",
    "MIN_PLAYER_BET": "2. AN TOÀN (FREE)",
    "PROBABILITY": "3. XÁC SUẤT (FREE)",
    "FOLLOW_KILLER": "4. THEO SÁT THỦ (FREE)",
    "SEQUENTIAL": "5. TUẦN TỰ 1→8 (FREE)",
    "KILLER_PERSONALITY": "6. TÍNH CÁCH SÁT THỦ (FREE)",
    "SMART_SAFE": "7. NÉ SÁT THỦ THÔNG MINH (FREE)",
    "FOLLOW_KILLER_DELAYED": "8. THEO VẾT SÁT THỦ (FREE)",
    "HIDE_SEEK_MASTER": "9. BẬC THẦY TRỐN TÌM (FREE)",
    "BALANCE": "10. CÂN BẰNG (FREE)",
    "MOST_PLAYERS": "11. NHIỀU NGƯỜI (FREE)",
    "LEAST_PLAYERS": "12. ÍT NGƯỜI (FREE)",
    "RICHEST": "13. NHIỀU CƯỢC (FREE)",
    "POOREST": "14. ÍT CƯỢC (FREE)",

    "AVOID_RESULT": "15. NÉ KẾT QUẢ (FREE)",
    "MEDIAN": "16. TRUNG VỊ (FREE)",
    "PATTERN": "17. MẪU LẶP (FREE)",
    "ALL": "18. ALL IN (V1 FREE / V2 VIP)",
    "KILLER_WAVE": "19. HOÁ THẦN (VIP)",
    "PSYCHO_ANALYSIS": "20. PHÂN TÍCH TÂM LÝ (VIP)",
    "MARKOV_CHAIN": "21. ADAPTIVE PATTERN (VIP)",
    "DEEP_LEARNING": "22. ENSEMBLE LEARNING (VIP)",
    "REINFORCEMENT": "23. PHÂN TÍCH CHUYÊN SÂU (VIP)",
    "BAYESIAN": "24. MARKOV PROBABILITY (VIP)",
    "K_MEANS": "25. PHÂN TÍCH DỮ LIỆU (VIP)",
    "NEURAL": "26. DATA FUSION (VIP)",
    "FUZZY": "27. X-PMLP (VIP)",
    "GENETIC": "28. H-AITTH (VIP)",
    "ANT_COLONY": "29. TỐI THƯỢNG (VIP)",
    "PARTICLE_SWARM": "30. FREQUENCY ANALYSIS (VIP)",
    "KNN": "31. THỐNG KÊ (VIP)",
    "DECISION_TREE": "32. THDZ-AI (VIP)",
    "RANDOM_FOREST": "33. LMT-AI (VIP)",
    "GRADIENT_BOOST": "34. THẦN THÁNH (VIP)",
    "LSTM": "35. THỢ SĂN (VIP)",
    "TRANSFORMER": "36. VBTTTH (VIP)",
    "ENSEMBLE": "37. SIÊU PHÂN TÍCH V1 (VIP)",
    "CYCLE_ANALYSIS": "38. CHU KỲ (VIP)",
    "TREND_ANALYSIS": "39. BẮT XU HƯỚNG (VIP)",
    "SAFE_RISK": "40. SIÊU PHÂN TÍCH V2 (VIP)",
    "TONG_HOP": "41.TỔNG HỢP (VIP)",
}

settings = {"algo": "RANDOM"}
selected_vth_algo = "RANDOM"
STRATEGY_CONFIG_FILE = "strategy.json"


def get_available_ai_list(key_type: str = "free") -> List[str]:
    """FREE được dùng Logic 1-18; VIP được dùng toàn bộ Logic 1-41."""
    if str(key_type or "free").strip().lower() == "vip":
        return list(FREE_AI_LIST) + list(VIP_AI_LIST)
    return list(FREE_AI_LIST)


def is_ai_available(ai_key: str, key_type: str = "free") -> bool:
    return str(ai_key or "").upper() in get_available_ai_list(key_type)


def get_vth_algo_display_name(algo_key=None, is_vip=None):
    key = str(algo_key or selected_vth_algo or "RANDOM").upper()
    if key == "ALL":
        if is_vip is None:
            try:
                valid, _reason, detected_vip = check_activation_valid(exit_on_expired=False)
                is_vip = bool(valid and detected_vip)
            except Exception:
                try:
                    is_vip = bool(is_vip_activated())
                except Exception:
                    is_vip = False
        version = "V2" if bool(is_vip) else "V1"
        return f"ALL IN {version} (Cược hết số dư)"
    label = str(SELECTION_MODES.get(key, key))
    # Các nhãn gốc TST có số thứ tự; menu Telegram tự đánh số theo quyền hiện tại.
    return re.sub(r"^\s*\d+\.\s*", "", label).strip()

_spinner = ["📦", "🪑", "👔", "💬", "🎥", "🏢", "💰", "👥"]
_num_re = re.compile(r"-?\d+[\d,]*\.?\d*")
VIP_COLORS = ["#FF00FF", "#D700FF", "#AF00FF", "#8700FF", "#5F00FF", "#0000FF", "#005FFF", "#0087FF", "#00AFFF", "#00D7FF", "#00FFFF"]

# ================== CÁC HÀM HỖ TRỢ VTH ==================


def log_debug(msg: str):
    try:
        logger.debug(msg)
    except Exception:
        pass

def _parse_number(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x)
    m = _num_re.search(s)
    if not m:
        return None
    token = m.group(0).replace(",", "")
    try:
        return float(token)
    except Exception:
        return None

def human_ts() -> str:
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")



def balance_headers_for(uid: Optional[int] = None, secret: Optional[str] = None) -> Dict[str, str]:
    h = {"accept": "*/*", "accept-language": "vi,en;q=0.9", "cache-control": "no-cache", "country-code": "vn", "origin": "https://xworld.info", "pragma": "no-cache", "referer": "https://xworld.info/", "user-agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36", "user-login": "login_v2", "xb-language": "vi-VN"}
    if uid is not None:
        h["user-id"] = str(uid)
    if secret:
        h["user-secret-key"] = str(secret)
    return h

def fetch_balances_3games(retries=3, timeout=8, params=None, uid=None, secret=None):
    global current_build, current_usdt, current_world, last_balance_ts, starting_balance, last_balance_val, cumulative_profit
    uid = uid or USER_ID
    secret = secret or SECRET_KEY
    payload = {"user_id": int(uid) if uid is not None else None, "source": "home"}
    attempt = 0
    while attempt <= retries:
        attempt += 1
        try:
            r = HTTP.post(WALLET_API_URL, json=payload, headers=balance_headers_for(uid, secret), timeout=timeout)
            r.raise_for_status()
            j = r.json()
            data = j.get("data", {}) if isinstance(j, dict) else {}
            ua = data.get("user_asset", {}) if isinstance(data, dict) else {}
            build = _parse_number(ua.get("BUILD"))
            world = _parse_number(ua.get("WORLD"))
            usdt = _parse_number(ua.get("USDT"))
            if build is not None:
                if last_balance_val is None:
                    starting_balance = build
                    last_balance_val = build
                else:
                    last_balance_val = build
                current_build = build
                # Không cập nhật PNL ở đây.
                # Wallet có thể trả về số dư tạm thời sau khi trừ tiền cược;
                # chỉ ván đã được chốt kết quả mới được phép cập nhật cumulative_profit.
            if usdt is not None:
                current_usdt = usdt
            if world is not None:
                current_world = world
            last_balance_ts = time.time()
            return current_build, current_world, current_usdt
        except Exception as e:
            log_debug(f"wallet fetch attempt {attempt} error: {e}")
            time.sleep(min(1.5 * attempt, 4))
    return current_build, current_world, current_usdt


def _vth_total_build_pnl() -> Optional[float]:
    try:
        if starting_balance is None or current_build is None:
            return None
        return float(current_build) - float(starting_balance)
    except Exception:
        return None

# ================== LOGIC PHÂN TÍCH CHỌN PHÒNG ==================
VTH_SKIP_IF_SAFEST_ABOVE = 78.0
VTH_DROP_ROOM_ABOVE = 70.0
AI_PERFORMANCE = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})

def compute_vth_room_risk() -> List[Dict[str, Any]]:
    """
    Rủi ro VTH v4 — Bayesian + realtime + volatility.

    P_base   = Dirichlet-smoothed kill rate (ổn định khi ít mẫu)
    P_ewma   = EWMA kill gần (20 ván, decay 0.86)
    P_markov = Markov 1 + 2 bậc
    crowd/money/gap/streak như v3
    vol      = độ biến thiên kill gần → tăng risk (không ổn định)

    conf cao → tin base; conf thấp → tin realtime.
    """
    rows = []
    n_rooms = max(1, len(ROOM_ORDER))
    max_players = max((int(room_state[r].get("players", 0) or 0) for r in ROOM_ORDER), default=0) or 1
    max_bet = max((float(room_state[r].get("bet", 0) or 0) for r in ROOM_ORDER), default=0.0) or 1.0
    log = list(game_kill_log) if game_kill_log else []
    n_log = len(log)

    # EWMA 20
    ewma = {r: 0.0 for r in ROOM_ORDER}
    if log:
        window = log[-20:]
        decay = 0.86
        w = 1.0
        wsum = 0.0
        for k in reversed(window):
            if k in ewma:
                ewma[k] += w
            wsum += w
            w *= decay
        if wsum > 0:
            for r in ewma:
                ewma[r] /= wsum

    # Markov 1 + 2
    markov = {r: 1.0 / n_rooms for r in ROOM_ORDER}
    if len(log) >= 2:
        last = log[-1]
        cnt = defaultdict(float)
        tot = 0.0
        for a, b in zip(log, log[1:]):
            if a == last:
                cnt[b] += 1.0
                tot += 1.0
        alpha = 0.85
        for r in ROOM_ORDER:
            markov[r] = (cnt.get(r, 0.0) + alpha) / (tot + alpha * n_rooms)
    if len(log) >= 3:
        a2, b2 = log[-2], log[-1]
        cnt2 = defaultdict(float)
        tot2 = 0.0
        for i in range(len(log) - 2):
            if log[i] == a2 and log[i + 1] == b2:
                cnt2[log[i + 2]] += 1.0
                tot2 += 1.0
        if tot2 > 0:
            alpha2 = 0.55
            for r in ROOM_ORDER:
                p2 = (cnt2.get(r, 0.0) + alpha2) / (tot2 + alpha2 * n_rooms)
                markov[r] = 0.60 * markov[r] + 0.40 * p2

    recent8 = log[-8:] if log else []
    streak_map = {r: recent8.count(r) for r in ROOM_ORDER}

    # volatility: biến thiên tần suất kill theo nửa log
    vol_map = {r: 0.0 for r in ROOM_ORDER}
    if n_log >= 8:
        half = n_log // 2
        first, second = log[:half], log[half:]
        for r in ROOM_ORDER:
            f1 = first.count(r) / float(len(first) or 1)
            f2 = second.count(r) / float(len(second) or 1)
            vol_map[r] = abs(f2 - f1)

    for r in ROOM_ORDER:
        kills = int(room_stats[r].get("kills", 0) or 0)
        survives = int(room_stats[r].get("survives", 0) or 0)
        samples = kills + survives
        k_log = log.count(r) if log else 0

        # Dirichlet prior alpha=1 (đều)
        if samples >= 2:
            p_base = (kills + 1.0) / (samples + float(n_rooms))
            conf = min(1.0, samples / 24.0)
        elif n_log >= 4:
            p_base = (k_log + 1.0) / (n_log + float(n_rooms))
            conf = min(1.0, n_log / 20.0) * 0.75
        else:
            p_base = 1.0 / n_rooms
            conf = 0.12

        p_ewma = float(ewma.get(r, 0.0))
        p_mk = float(markov.get(r, 1.0 / n_rooms))
        players = int(room_state[r].get("players", 0) or 0)
        bet = float(room_state[r].get("bet", 0) or 0)
        crowd = players / max_players
        money = bet / max_bet

        gap = float(n_rooms + 3)
        if log:
            for i in range(len(log) - 1, -1, -1):
                if log[i] == r:
                    gap = float(len(log) - 1 - i)
                    break
        gap_factor = min(1.0, gap / 12.0)
        streak_n = min(1.0, float(streak_map.get(r, 0)) / 3.0)
        vol = float(vol_map.get(r, 0.0))

        w_base = 0.24 + 0.26 * conf
        w_ewma = 0.18 + 0.10 * (1.0 - conf)
        w_mk = 0.14 + 0.06 * (1.0 - conf)
        w_crowd = 0.09
        w_money = 0.07
        w_gap = 0.09
        w_streak = 0.08
        w_vol = 0.05
        wsum = w_base + w_ewma + w_mk + w_crowd + w_money + w_gap + w_streak + w_vol
        p_lose = (
            w_base * p_base
            + w_ewma * p_ewma
            + w_mk * p_mk
            + w_crowd * crowd
            + w_money * money
            + w_gap * gap_factor * (1.0 / n_rooms) * 2.2
            + w_streak * streak_n * 0.48
            + w_vol * min(1.0, vol * 4.0) * 0.35
        ) / max(wsum, 1e-6)
        p_lose = max(0.02, min(0.95, p_lose))
        p_win = 1.0 - p_lose

        rows.append({
            "id": r,
            "name": ROOM_NAMES.get(r, f"Phòng {r}"),
            "kills": kills,
            "survives": survives,
            "samples": samples,
            "kill_rate": p_base,
            "players": players,
            "bet": bet,
            "risk_pct": p_lose * 100.0,
            "win_prob": p_win * 100.0,
            "lose_prob": p_lose * 100.0,
            "ewma": p_ewma,
            "markov": p_mk,
            "gap": gap,
            "streak": streak_map.get(r, 0),
            "volatility": vol,
            "confidence": conf,
            "last_kill_round": room_stats[r].get("last_kill_round"),
        })

    if rows:
        mx = max(x["risk_pct"] for x in rows) or 1.0
        mn = min(x["risk_pct"] for x in rows)
        span = max(1e-6, mx - mn)
        for x in rows:
            rel = (x["risk_pct"] - mn) / span * 100.0
            x["risk_pct"] = 0.78 * x["risk_pct"] + 0.22 * rel
            x["risk_pct"] = max(1.0, min(99.0, x["risk_pct"]))
            x["lose_prob"] = x["risk_pct"]
            x["win_prob"] = 100.0 - x["risk_pct"]
        s = sum(float(x["win_prob"]) for x in rows) or 1.0
        for x in rows:
            x["win_prob_rel"] = float(x["win_prob"]) / s * 100.0
    rows.sort(key=lambda x: x["risk_pct"], reverse=True)
    return rows

def _vth_kill_gap(room: int) -> float:
    """Số ván từ lần kill gần nhất (cao = an toàn hơn)."""
    if not game_kill_log:
        return 8.0
    log = list(game_kill_log)
    for i in range(len(log) - 1, -1, -1):
        if log[i] == room:
            return float(len(log) - 1 - i)
    return float(min(12, len(log) + 2))

def _vth_transition_safe(room: int) -> float:
    """P(không bị kill ngay sau last) — điểm an toàn Markov."""
    if len(game_kill_log) < 2:
        return 0.5
    last = game_kill_log[-1]
    trans = defaultdict(int)
    total = 0
    for a, b in zip(game_kill_log, list(game_kill_log)[1:]):
        if a == last:
            trans[b] += 1
            total += 1
    if total == 0:
        return 0.5
    # xác suất room bị kill tiếp
    p_kill = (trans.get(room, 0) + 0.35) / (total + 0.35 * len(ROOM_ORDER))
    return 1.0 - p_kill

def _vth_unified_scores(noise: float = 0.04) -> Dict[int, float]:
    """
    Điểm an toàn hợp nhất (cao = nên cược):
    survival + gap + markov + anti-crowd + anti-money + risk% (nếu có).
    """
    max_p = max((room_state[r].get("players", 0) or 0) for r in ROOM_ORDER) or 1
    max_b = max((room_state[r].get("bet", 0) or 0) for r in ROOM_ORDER) or 1.0
    risk_by = {}
    try:
        for row in compute_vth_room_risk() or []:
            rid = int(row.get("id") or row.get("room") or 0)
            if rid:
                risk_by[rid] = float(row.get("risk_pct", 50) or 50) / 100.0
    except Exception:
        pass

    scores = {}
    for r in ROOM_ORDER:
        kills = int(room_stats[r].get("kills", 0) or 0)
        survives = int(room_stats[r].get("survives", 0) or 0)
        survival = (survives + 1.2) / (kills + survives + 2.4)
        gap = min(1.0, _vth_kill_gap(r) / 8.0)
        markov_safe = _vth_transition_safe(r)
        crowd = 1.0 - (float(room_state[r].get("players", 0) or 0) / max_p)
        money = 1.0 - (float(room_state[r].get("bet", 0) or 0) / max_b)
        risk_safe = 1.0 - risk_by.get(r, 0.5)

        scores[r] = (
            0.24 * survival
            + 0.18 * gap
            + 0.18 * markov_safe
            + 0.14 * crowd
            + 0.12 * money
            + 0.14 * risk_safe
        )
        if noise:
            scores[r] += random.uniform(-noise, noise)
    return scores

def _vth_pick_max(scores: Dict[int, float]) -> int:
    if not scores:
        return random.choice(ROOM_ORDER)
    best = max(scores.values())
    tops = [r for r, v in scores.items() if abs(v - best) < 1e-9]
    return random.choice(tops)

def _vth_risk_map() -> Dict[int, float]:
    try:
        return {int(r["id"]): float(r.get("risk_pct", 50) or 50) for r in (compute_vth_room_risk() or [])}
    except Exception:
        return {}

def _vth_conservative_pick(candidates: List[int], n: int = 1) -> List[int]:
    """Ưu tiên risk thấp — luôn trả đủ n phòng (không bỏ ván)."""
    n = max(1, min(4, int(n or 1)))
    rm = _vth_risk_map()
    pool = list(candidates) if candidates else list(ROOM_ORDER)
    pool = sorted(set(pool), key=lambda r: (rm.get(r, 55.0), r))
    out = list(pool)
    if len(out) < n:
        for r in sorted(ROOM_ORDER, key=lambda x: (rm.get(x, 55.0), x)):
            if r not in out:
                out.append(r)
            if len(out) >= n:
                break
    if not out:
        out = list(ROOM_ORDER)[:n]
    return out[:n]

def _vth_round_danger_check() -> tuple:
    """
    Phân tích độ ổn định kỳ hiện tại.
    Returns: (is_danger: bool, message: str)
    """
    try:
        rows = compute_vth_room_risk() or []
    except Exception:
        rows = []
    if not rows:
        return True, "Chưa đủ dữ liệu — phân tích không ổn"
    risks = [float(r.get("risk_pct", 50) or 50) for r in rows]
    safest = min(risks)
    avg = sum(risks) / max(1, len(risks))
    high_n = sum(1 for x in risks if x >= 70)
    # mẫu kill quá ít → kém tin cậy
    samples = sum(int(r.get("samples", 0) or 0) for r in rows)
    if samples < 8 and len(list(game_kill_log or [])) < 5:
        return True, f"Dữ liệu mỏng (mẫu {samples}) — chưa ổn để cược"
    if safest >= VTH_SKIP_IF_SAFEST_ABOVE:
        return True, f"Nguy hiểm: phòng an toàn nhất vẫn {safest:.0f}% risk"
    if avg >= 66.0:
        return True, f"Nguy hiểm: risk trung bình {avg:.0f}% quá cao"
    if high_n >= 6:
        return True, f"Nguy hiểm: {high_n}/8 phòng risk CAO"
    # chênh lệch quá nhỏ + avg cao → không có chỗ trú
    if (max(risks) - min(risks)) < 8 and avg >= 58:
        return True, "Nguy hiểm: mọi phòng risk gần nhau, không có lựa chọn ổn"
    return False, ""

# LOGIC 43 — RỦI RO AN TOÀN (VIP / SAFE RISK)
def choose_safe_risk() -> int:
    """VIP: tối ưu giảm thua — risk thấp là trọng số chính."""
    scores = _vth_unified_scores(noise=0.015)
    try:
        for row in compute_vth_room_risk() or []:
            rid = int(row.get("id") or 0)
            if rid in scores:
                rp = float(row.get("risk_pct", 50) or 50) / 100.0
                scores[rid] += 0.55 * (1.0 - rp)
                if rp > 0.70:
                    scores[rid] -= 0.45
    except Exception:
        pass
    # conservative: chỉ xét top risk thấp
    picks = _vth_conservative_pick(list(scores.keys()), 1)
    if picks:
        return picks[0]
    return _vth_pick_max(scores)

# LOGIC 1 — PHẬT ĐỘ (RANDOM)
def choose_random() -> int:
    return random.choice(ROOM_ORDER)

# LOGIC 2 — AN TOÀN (MIN PLAYERS & BET)
def choose_min_player_bet() -> int:
    if not any(rs.get('players', 0) > 0 or rs.get('bet', 0) > 0 for rs in room_state.values()):
        return choose_random()
    player_ranks = sorted(ROOM_ORDER, key=lambda r: room_state[r]['players'])
    bet_ranks = sorted(ROOM_ORDER, key=lambda r: room_state[r]['bet'])
    scores = defaultdict(int)
    for i, r in enumerate(player_ranks):
        scores[r] += i
    for i, r in enumerate(bet_ranks):
        scores[r] += i
    return min(scores, key=scores.get)

# LOGIC 3 — XÁC SUẤT (PROBABILITY)
def choose_probability() -> int:
    scores = {}
    for r in ROOM_ORDER:
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        survival_rate = (survives + 1) / (kills + survives + 2)
        scores[r] = survival_rate
    return max(scores, key=scores.get)

# LOGIC 4 — THEO SÁT THỦ (FOLLOW KILLER)
def choose_follow_killer() -> int:
    if last_killed_room is not None and last_killed_room in ROOM_ORDER:
        return last_killed_room
    return random.choice(ROOM_ORDER)

# LOGIC 5 — TUẦN TỰ
def choose_sequential() -> int:
    global _sequential_bet_index
    room_to_bet = ROOM_ORDER[_sequential_bet_index]
    _sequential_bet_index = (_sequential_bet_index + 1) % len(ROOM_ORDER)
    return room_to_bet

# LOGIC 6 — TÍNH CÁCH SÁT THỦ
def choose_killer_personality_enhanced() -> int:
    if len(killer_history) < 3:
        return choose_random()
    
    recent_killers = list(killer_history)[-15:]
    avg_players = sum(h['players'] for h in recent_killers) / len(recent_killers)
    avg_bet = sum(h['bet'] for h in recent_killers) / len(recent_killers)
    player_var = sum((h['players'] - avg_players) ** 2 for h in recent_killers) / len(recent_killers)
    bet_var = sum((h['bet'] - avg_bet) ** 2 for h in recent_killers) / len(recent_killers)
    stability_factor = 1.5 if player_var < 2 and bet_var < 100 else 0.8
    
    avoidance_scores = {}
    for r in ROOM_ORDER:
        current_players = room_state[r]['players']
        current_bet = room_state[r]['bet']
        player_dist = abs(current_players - avg_players) / (avg_players + 1)
        bet_dist = abs(current_bet - avg_bet) / (avg_bet + 1)
        base_score = (player_dist + bet_dist) * stability_factor
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        survival_rate = (survives + 1) / (kills + survives + 2)
        base_score += survival_rate * 0.3
        avoidance_scores[r] = base_score
    
    return max(avoidance_scores, key=avoidance_scores.get)

# LOGIC 7 — THÔNG MINH (SMART SAFE)
def choose_smart_safe_enhanced() -> int:
    """Smart Safe v2 — unified score + lệch pattern sát thủ."""
    scores = _vth_unified_scores(noise=0.05)
    if killer_history:
        avg_p = sum(h.get("players", 0) for h in killer_history) / len(killer_history)
        avg_b = sum(h.get("bet", 0) for h in killer_history) / len(killer_history)
        for r in ROOM_ORDER:
            # càng khác profile sát thủ càng an toàn
            pd = abs((room_state[r].get("players", 0) or 0) - avg_p) / (avg_p + 1)
            bd = abs((room_state[r].get("bet", 0) or 0) - avg_b) / (avg_b + 1)
            scores[r] += 0.12 * (pd + bd)
    return _vth_pick_max(scores)

# LOGIC 8 — THEO VẾT SÁT THỦ (TRỄ 1 VÁN)
def choose_follow_killer_delayed() -> int:
    global last_killed_room_delayed
    if last_killed_room_delayed is not None and last_killed_room_delayed in ROOM_ORDER:
        return last_killed_room_delayed
    return random.choice(ROOM_ORDER)

# LOGIC 9 — THÁNH TRỐN TÌM (MASTER AI)
def choose_hide_seek_master() -> int:
    danger_scores = {}
    max_players = max(rs['players'] for rs in room_state.values()) or 1
    max_bet = max(rs['bet'] for rs in room_state.values()) or 1
    avg_players_killed = 0
    avg_bet_killed = 0
    if killer_history:
        avg_players_killed = sum(h['players'] for h in killer_history) / len(killer_history)
        avg_bet_killed = sum(h['bet'] for h in killer_history) / len(killer_history)
    for r in ROOM_ORDER:
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        hist_danger = (kills + 1) / (kills + survives + 2)
        crowd_danger = room_state[r]['players'] / max_players
        money_danger = room_state[r]['bet'] / max_bet
        personality_danger = 0
        if killer_history:
            player_sim = 1 - (abs(room_state[r]['players'] - avg_players_killed) / (avg_players_killed + max_players + 1))
            bet_sim = 1 - (abs(room_state[r]['bet'] - avg_bet_killed) / (avg_bet_killed + max_bet + 1))
            personality_danger = (player_sim + bet_sim) / 2
        recency_penalty = 0.0  # không chặn phòng vừa kill
        total_danger = (0.3 * hist_danger) + (0.2 * crowd_danger) + (0.2 * money_danger) + (0.3 * personality_danger) + recency_penalty
        danger_scores[r] = total_danger
    return min(danger_scores, key=danger_scores.get)

# LOGIC 10 — CÂN BẰNG (BALANCE)
def choose_balance() -> int:
    scores = {}
    total_players = sum(rs['players'] for rs in room_state.values())
    total_bet = sum(rs['bet'] for rs in room_state.values())
    avg_players = total_players / len(ROOM_ORDER) if total_players > 0 else 0
    avg_bet = total_bet / len(ROOM_ORDER) if total_bet > 0 else 0
    for r in ROOM_ORDER:
        players = room_state[r]['players']
        bet = room_state[r]['bet']
        score = abs(players - avg_players) / (avg_players + 1) + abs(bet - avg_bet) / (avg_bet + 1)
        scores[r] = score
    return min(scores, key=scores.get)

# LOGIC 11 — ĐÔNG NHẤT (MOST PLAYERS)
def choose_most_players() -> int:
    return max(ROOM_ORDER, key=lambda r: room_state[r]['players'])

# LOGIC 12 — ÍT NHẤT (LEAST PLAYERS)
def choose_least_players() -> int:
    return min(ROOM_ORDER, key=lambda r: room_state[r]['players'])

# LOGIC 13 — GIÀU NHẤT (RICHEST)
def choose_richest() -> int:
    return max(ROOM_ORDER, key=lambda r: room_state[r]['bet'])

# LOGIC 14 — NGHÈO NHẤT (POOREST)
def choose_poorest() -> int:
    return min(ROOM_ORDER, key=lambda r: room_state[r]['bet'])



# LOGIC 16 — TRÁNH KẾT QUẢ (AVOID RESULT)
def choose_avoid_result() -> int:
    if last_killed_room is None:
        return random.choice(ROOM_ORDER)
    candidates = [r for r in ROOM_ORDER if r != last_killed_room]
    if not candidates:
        return random.choice(ROOM_ORDER)
    return random.choice(candidates)

# LOGIC 19 — TRUNG VỊ (MEDIAN)
def choose_median() -> int:
    if not any(rs['players'] > 0 for rs in room_state.values()):
        return random.choice(ROOM_ORDER)
    players_list = sorted(ROOM_ORDER, key=lambda r: room_state[r]['players'])
    bet_list = sorted(ROOM_ORDER, key=lambda r: room_state[r]['bet'])
    median_players = players_list[len(players_list) // 2]
    median_bet = bet_list[len(bet_list) // 2]
    if median_players == median_bet:
        return median_players
    scores = {}
    for r in ROOM_ORDER:
        dist_players = abs(room_state[r]['players'] - room_state[median_players]['players'])
        dist_bet = abs(room_state[r]['bet'] - room_state[median_bet]['bet'])
        scores[r] = dist_players + dist_bet
    return min(scores, key=scores.get)

# LOGIC 20 — MẪU LẶP (PATTERN)
def choose_pattern() -> int:
    if len(game_kill_log) < 3:
        return random.choice(ROOM_ORDER)
    last_3 = list(game_kill_log)[-3:]
    if len(last_3) == 3 and last_3[0] == last_3[2]:
        return last_3[1]
    return random.choice(ROOM_ORDER)

# LOGIC 41 — PHÂN TÍCH CHU KỲ (CYCLE ANALYSIS)
def choose_cycle_analysis() -> int:
    if len(game_kill_log) < 6:
        return choose_random()
    
    history = list(game_kill_log)
    best_cycle = None
    best_score = -1
    
    for cycle_len in range(2, 11):
        if len(history) >= cycle_len * 2:
            matches = 0
            total = len(history) - cycle_len
            for i in range(total):
                if history[i] == history[i + cycle_len]:
                    matches += 1
            score = matches / max(1, total)
            if score > best_score:
                best_score = score
                best_cycle = cycle_len
    
    if best_cycle and best_score > 0.6:
        last_cycle_start = len(history) - best_cycle
        if last_cycle_start >= 0:
            predicted = history[last_cycle_start]
            if predicted in ROOM_ORDER:
                if predicted == last_killed_room:
                    alternatives = [r for r in ROOM_ORDER if r != predicted]
                    if alternatives:
                        return random.choice(alternatives)
                return predicted
    
    return choose_smart_safe_enhanced()

# LOGIC 42 — PHÂN TÍCH XU HƯỚNG (TREND ANALYSIS)
def choose_trend_analysis() -> int:
    if len(killer_history) < 5:
        return choose_random()
    
    player_trends = []
    bet_trends = []
    for h in list(killer_history)[-10:]:
        player_trends.append(h['players'])
        bet_trends.append(h['bet'])
    
    if len(player_trends) >= 3:
        player_slope = (player_trends[-1] - player_trends[0]) / max(1, len(player_trends))
        bet_slope = (bet_trends[-1] - bet_trends[0]) / max(1, len(bet_trends))
        next_players = player_trends[-1] + player_slope * 1.5
        next_bet = bet_trends[-1] + bet_slope * 1.5
        
        scores = {}
        for r in ROOM_ORDER:
            if r == last_killed_room:
                scores[r] = -999999
                continue
            players = room_state[r]['players']
            bet = room_state[r]['bet']
            player_dist = abs(players - next_players) / (next_players + 1)
            bet_dist = abs(bet - next_bet) / (next_bet + 1)
            scores[r] = player_dist + bet_dist
        
        return max(scores, key=scores.get)
    
    return choose_random()

def choose_vip_random() -> int:
    logic_list = [
        choose_random, choose_min_player_bet, choose_probability,
        choose_follow_killer, choose_sequential, choose_killer_personality_enhanced,
        choose_smart_safe_enhanced, choose_follow_killer_delayed, choose_hide_seek_master,
        choose_balance, choose_most_players, choose_least_players,
        choose_richest, choose_poorest, choose_alternate,
        choose_avoid_result, choose_cold, choose_hot, choose_median, choose_pattern,
        choose_cycle_analysis, choose_trend_analysis
    ]
    sys_random = random.SystemRandom()
    chosen_func = sys_random.choice(logic_list)
    return chosen_func()

# LOGIC 22 — BẮT SÓNG SÁT THỦ (KILLER WAVE)
def choose_killer_wave() -> int:
    if len(game_kill_log) < 4:
        return choose_random()
    last_4 = list(game_kill_log)[-4:]
    for i in range(1, 4):
        if len(last_4) >= i*2 and last_4[-i:] == last_4[-i*2:-i]:
            predicted = last_4[-i-1] if len(last_4) > i else last_4[-1]
            return predicted
    return choose_smart_safe_enhanced()

# LOGIC 23 — PHÂN TÍCH TÂM LÝ (PSYCHO ANALYSIS)
def choose_psycho_analysis() -> int:
    max_players_room = max(ROOM_ORDER, key=lambda r: room_state[r]['players'])
    max_bet_room = max(ROOM_ORDER, key=lambda r: room_state[r]['bet'])
    crowd_favorite = max_players_room if room_state[max_players_room]['players'] > room_state[max_bet_room]['players'] else max_bet_room
    candidates = [r for r in ROOM_ORDER if r != crowd_favorite]
    if candidates:
        return min(candidates, key=lambda r: room_state[r]['players'] + room_state[r]['bet'] * 0.01)
    return choose_random()

# LOGIC 24 — CHUỖI MARKOV (MARKOV CHAIN)
def choose_markov_chain() -> int:
    """Markov v2: dự đoán phòng kill kế → cược phòng KHÁC (an toàn)."""
    if len(game_kill_log) < 3:
        return _vth_pick_max(_vth_unified_scores())
    # bậc 1
    t1 = defaultdict(lambda: defaultdict(float))
    for a, b in zip(game_kill_log, list(game_kill_log)[1:]):
        t1[a][b] += 1.0
    last = game_kill_log[-1]
    # bậc 2 nếu đủ dữ liệu
    pred_kill_scores = {r: 0.15 for r in ROOM_ORDER}
    if len(game_kill_log) >= 3:
        t2 = defaultdict(lambda: defaultdict(float))
        log = list(game_kill_log)
        for i in range(len(log) - 2):
            t2[(log[i], log[i + 1])][log[i + 2]] += 1.0
        key = (log[-2], log[-1])
        if t2[key]:
            for r, c in t2[key].items():
                pred_kill_scores[r] += c
    if t1[last]:
        for r, c in t1[last].items():
            pred_kill_scores[r] += c * 0.85
    # chọn phòng ít khả năng bị kill nhất
    return min(pred_kill_scores, key=pred_kill_scores.get)

# LOGIC 25 — HỌC SÂU (DEEP LEARNING)
def choose_deep_learning_enhanced() -> int:
    if len(killer_history) < 5:
        return choose_random()
    
    weights = {}
    for r in ROOM_ORDER:
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        survival_rate = (survives + 1) / (kills + survives + 2)
        
        recent_boost = 0
        if r in game_kill_log:
            recent_count = list(game_kill_log).count(r)
            recent_boost = -0.3 * min(1, recent_count / 3)
        
        trend_boost = 0
        if len(game_kill_log) >= 5:
            last_5 = list(game_kill_log)[-5:]
            if r in last_5:
                recent_appear = last_5.count(r)
                trend_boost = -0.2 * recent_appear
            else:
                trend_boost = 0.1
        
        max_players = max(rs['players'] for rs in room_state.values()) or 1
        max_bet = max(rs['bet'] for rs in room_state.values()) or 1
        crowd_boost = 1 - (room_state[r]['players'] / max_players)
        money_boost = 1 - (room_state[r]['bet'] / max_bet)
        
        killer_pattern_boost = 0
        if len(game_kill_log) >= 3:
            last_3 = list(game_kill_log)[-3:]
            if len(last_3) == 3 and last_3[0] == last_3[2] and last_3[0] == r:
                killer_pattern_boost = -0.4
        
        weights[r] = (0.25 * survival_rate + 0.20 * recent_boost + 0.15 * trend_boost + 0.15 * crowd_boost + 0.10 * money_boost + 0.15 * killer_pattern_boost)
        weights[r] += random.uniform(-0.08, 0.08)
    
    return max(weights, key=weights.get)

# LOGIC 26 — HỌC TĂNG CƯỜNG (REINFORCEMENT)
def choose_reinforcement() -> int:
    if len(bet_history) < 3:
        return choose_random()
    action_scores = {r: 0 for r in ROOM_ORDER}
    for b in list(bet_history)[-10:]:
        room = b.get('room')
        result = b.get('result')
        if room in ROOM_ORDER and result:
            if result == "Thắng":
                action_scores[room] += 1
            else:
                action_scores[room] -= 0.5
    max_score = max(action_scores.values())
    if max_score <= 0:
        return choose_random()
    best_rooms = [r for r, s in action_scores.items() if s == max_score]
    return random.choice(best_rooms)

# LOGIC 27 — XÁC SUẤT BAYES (BAYESIAN)
def choose_bayesian() -> int:
    """Bayes v2: posterior P(kill) kết hợp form realtime → chọn min."""
    if len(game_kill_log) < 2:
        return _vth_pick_max(_vth_unified_scores())
    room_counts = Counter(game_kill_log)
    total_kills = len(game_kill_log)
    posterior = {}
    for r in ROOM_ORDER:
        prior = 1.0 / len(ROOM_ORDER)
        like = (room_counts.get(r, 0) + 0.8) / (total_kills + 0.8 * len(ROOM_ORDER))
        # evidence: đông / nhiều tiền → sát thủ hay chọn
        max_p = max(room_state[x].get("players", 0) or 0 for x in ROOM_ORDER) or 1
        max_b = max(room_state[x].get("bet", 0) or 0 for x in ROOM_ORDER) or 1
        crowd = (room_state[r].get("players", 0) or 0) / max_p
        money = (room_state[r].get("bet", 0) or 0) / max_b
        evidence = 0.55 + 0.25 * crowd + 0.20 * money
        posterior[r] = prior * like * evidence
    s = sum(posterior.values()) or 1
    for r in posterior:
        posterior[r] /= s
    return min(posterior, key=posterior.get)

# LOGIC 28 — PHÂN CỤM K-MEANS
def choose_k_means() -> int:
    if len(game_kill_log) < 6:
        return choose_random()
    from collections import defaultdict
    room_features = defaultdict(lambda: [0, 0])
    for i, room in enumerate(list(game_kill_log)[-10:]):
        room_features[room][0] += 1
        room_features[room][1] = i
    cluster_1 = set()
    cluster_2 = set()
    for room, features in room_features.items():
        if features[0] < 2:
            cluster_1.add(room)
        else:
            cluster_2.add(room)
    if cluster_1:
        return random.choice(list(cluster_1))
    return choose_random()

# LOGIC 29 — MẠNG NƠ-RON (NEURAL)
def choose_neural() -> int:
    if len(killer_history) < 3:
        return choose_random()
    scores = {}
    for r in ROOM_ORDER:
        players = room_state[r]['players']
        bet = room_state[r]['bet']
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        layer1 = (0.3 * survives) - (0.5 * kills) + (0.2 * players) - (0.3 * bet)
        layer2 = (0.4 * layer1) + (0.2 * (survives - kills))
        layer3 = (0.5 * layer2) + (0.3 * (1 - players/max(1, max(rs['players'] for rs in room_state.values()))))
        output = 1 / (1 + math.exp(-layer3))
        scores[r] = output
    return max(scores, key=scores.get)

# LOGIC 30 — LOGIC MỜ (FUZZY)
def choose_fuzzy() -> int:
    if len(killer_history) < 2:
        return choose_random()
    scores = {}
    for r in ROOM_ORDER:
        players = room_state[r]['players']
        bet = room_state[r]['bet']
        players_young = max(0, 1 - players/2) if players < 2 else 0
        players_mid = max(0, 1 - abs(players-3)/2) if 1 < players < 5 else 0
        players_old = max(0, (players-4)/2) if players > 4 else 0
        bet_low = max(0, 1 - bet/100) if bet < 100 else 0
        bet_mid = max(0, 1 - abs(bet-300)/200) if 100 < bet < 500 else 0
        bet_high = max(0, (bet-400)/200) if bet > 400 else 0
        rule1 = min(players_young, bet_low)
        rule2 = min(players_old, bet_high)
        rule3 = min(players_mid, bet_mid)
        safety_score = (rule1 * 1.0 + rule2 * 0.0 + rule3 * 0.5) / (rule1 + rule2 + rule3 + 0.01)
        scores[r] = safety_score
    return max(scores, key=scores.get)

# LOGIC 31 — THUẬT TOÁN DI TRUYỀN (GENETIC)
def choose_genetic() -> int:
    if len(killer_history) < 5:
        return choose_random()
    population = list(game_kill_log)[-10:]
    if not population:
        return choose_random()
    fitness = {}
    for r in ROOM_ORDER:
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        fitness[r] = (survives + 1) / (kills + survives + 2)
    return max(fitness, key=fitness.get)

# LOGIC 32 — KIẾN BÒ (ANT COLONY)
def choose_ant_colony() -> int:
    if len(game_kill_log) < 3:
        return choose_random()
    pheromone = {}
    for r in ROOM_ORDER:
        count = list(game_kill_log).count(r)
        pheromone[r] = count / len(game_kill_log) if game_kill_log else 0
    return min(pheromone, key=pheromone.get)

# LOGIC 33 — BẦY ĐÀN (PARTICLE SWARM)
def choose_particle_swarm() -> int:
    if len(killer_history) < 3:
        return choose_random()
    scores = {}
    for r in ROOM_ORDER:
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        survival_rate = (survives + 1) / (kills + survives + 2)
        recent_trend = 0.3 if r in list(game_kill_log)[-3:] else 0
        scores[r] = survival_rate + recent_trend
    return max(scores, key=scores.get)

# LOGIC 34 — K-NEAREST NEIGHBORS (KNN)
def choose_knn() -> int:
    if len(game_kill_log) < 3:
        return choose_random()
    k = min(3, len(game_kill_log))
    nearest = list(game_kill_log)[-k:]
    counts = Counter(nearest)
    min_count = min(counts.values())
    candidates = [r for r, c in counts.items() if c == min_count]
    if candidates:
        return random.choice(candidates)
    return choose_random()

# LOGIC 35 — CÂY QUYẾT ĐỊNH (DECISION TREE)
def choose_decision_tree() -> int:
    if len(killer_history) < 5:
        return choose_random()
    return choose_probability() if killer_history else choose_random()

# LOGIC 36 — RỪNG NGẪU NHIÊN (RANDOM FOREST)
def choose_random_forest() -> int:
    if len(killer_history) < 3:
        return choose_random()
    predictions = []
    for _ in range(5):
        if random.random() > 0.5:
            predictions.append(choose_probability())
        else:
            predictions.append(choose_min_player_bet())
    counts = Counter(predictions)
    return max(counts, key=counts.get)

# LOGIC 37 — TĂNG CƯỜNG GRADIENT (GRADIENT BOOST)
def choose_gradient_boost() -> int:
    if len(killer_history) < 3:
        return choose_random()
    scores = {}
    for r in ROOM_ORDER:
        base_score = 0.5
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        survival_rate = (survives + 1) / (kills + survives + 2)
        base_score += 0.3 * survival_rate
        base_score -= 0.1 * (room_state[r]['players'] / max(1, max(rs['players'] for rs in room_state.values())))
        base_score -= 0.1 * (room_state[r]['bet'] / max(1, max(rs['bet'] for rs in room_state.values())))
        scores[r] = base_score
    return max(scores, key=scores.get)

# LOGIC 38 — LSTM
def choose_lstm() -> int:
    if len(game_kill_log) < 4:
        return choose_random()
    last_5 = list(game_kill_log)[-5:]
    if len(last_5) == 5 and last_5[0] == last_5[3] and last_5[1] == last_5[4]:
        return last_5[2]
    return choose_markov_chain()

# LOGIC 39 — TRANSFORMER
def choose_transformer() -> int:
    if len(game_kill_log) < 4:
        return choose_random()
    attention_scores = {}
    for r in ROOM_ORDER:
        kills = room_stats[r].get('kills', 0)
        survives = room_stats[r].get('survives', 0)
        recency = 1 - (list(game_kill_log).count(r) / max(1, len(game_kill_log)))
        attention_scores[r] = (0.4 * recency) + (0.3 * (survives / max(1, kills + survives))) + (0.3 * (1 - room_state[r]['players'] / max(1, max(rs['players'] for rs in room_state.values()))))
    return max(attention_scores, key=attention_scores.get)

# Logic 8: ALL IN
# LOGIC 21 — ALL IN (FREE: V1 / VIP: V2)
def choose_all():
    if not ROOM_ORDER:
        return 1

    if len(ROOM_ORDER) == 1:
        return ROOM_ORDER[0]

    max_players = max(
        (
            room_state.get(r, {}).get("players", 0)
            for r in ROOM_ORDER
        ),
        default=1
    ) or 1

    max_bet = max(
        (
            room_state.get(r, {}).get("bet", 0)
            for r in ROOM_ORDER
        ),
        default=1
    ) or 1

    full_history = list(game_kill_log)
    history100 = full_history[-100:]
    history10 = history100[-10:]

    scores = {}
    current_gaps = {}

    for room in ROOM_ORDER:
        state = room_state.get(room, {})

        players = max(
            0.0,
            float(state.get("players", 0))
        )

        bet = max(
            0.0,
            float(state.get("bet", 0))
        )

        bet_score = 1.0 - (
            bet / max_bet
        )

        bet_score = max(
            0.0,
            min(
                1.0,
                bet_score
            )
        )

        player_score = (
            players / max_players
        )

        player_score = max(
            0.0,
            min(
                1.0,
                player_score
            )
        )

        hits100 = history100.count(room)

        if history100:
            kill_safety100 = 1.0 - (
                hits100 / len(history100)
            )
        else:
            kill_safety100 = 0.5

        kill_safety100 = max(
            0.0,
            min(
                1.0,
                kill_safety100
            )
        )

        hits10 = history10.count(room)

        if history10:
            kill_safety10 = 1.0 - (
                hits10 / len(history10)
            )
        else:
            kill_safety10 = 0.5

        kill_safety10 = max(
            0.0,
            min(
                1.0,
                kill_safety10
            )
        )

        current_gap = len(history100)

        for i in range(
            len(history100) - 1,
            -1,
            -1
        ):
            if history100[i] == room:
                current_gap = (
                    len(history100) - 1 - i
                )
                break

        current_gaps[room] = current_gap

        if history100:
            delay_score = min(
                current_gap / len(history100),
                1.0
            )
        else:
            delay_score = 0.5

        delay_score = max(
            0.0,
            min(
                1.0,
                delay_score
            )
        )

        score = (
            bet_score * 0.55 +
            player_score * 0.05 +
            kill_safety100 * 0.15 +
            kill_safety10 * 0.10 +
            delay_score * 0.15
        )

        if hits10 == 0 and len(history10) >= 5:
            score += 0.05

        if hits100 <= 2 and len(history100) >= 30:
            score += 0.04

        if (
            current_gap >= 10
            and
            len(history100) >= 20
        ):
            score += 0.04

        if (
            current_gap >= 20
            and
            len(history100) >= 30
        ):
            score += 0.03

        scores[room] = score

    available_rooms = [
        room
        for room in ROOM_ORDER
        if room != last_killed_room
    ]

    if not available_rooms:
        available_rooms = list(ROOM_ORDER)

    best_score = max(
        scores.get(
            room,
            float("-inf")
        )
        for room in available_rooms
    )

    candidates = [
        room
        for room in available_rooms
        if scores.get(
            room,
            float("-inf")
        ) >= best_score - 0.02
    ]

    if not candidates:
        candidates = available_rooms

    candidates.sort(
        key=lambda room: (
            -scores.get(
                room,
                float("-inf")
            ),
            current_gaps.get(
                room,
                0
            ),
            -history100.count(room),
            -history10.count(room),
            room_state.get(
                room,
                {}
            ).get(
                "bet",
                float("inf")
            )
        )
    )

    return candidates[0]

# LOGIC 40 — TỔNG HỢP ENHANCED (ENSEMBLE)
def choose_ensemble_enhanced() -> int:
    """Ensemble v3: multi-AI + unified score + adaptive performance + anti-kill."""
    base = _vth_unified_scores(noise=0.02)
    votes = defaultdict(float)
    for r, sc in base.items():
        votes[r] += sc * 1.15

    ai_weights = {
        "choose_safe_risk": 1.45,
        "choose_smart_safe_enhanced": 1.15,
        "choose_probability": 0.9,
        "choose_markov_chain": 1.05,
        "choose_bayesian": 1.0,
        "choose_killer_personality_enhanced": 0.7,
        "choose_deep_learning_enhanced": 0.7,
        "choose_hide_seek_master": 0.5,
        "choose_neural": 0.65,
        "choose_cycle_analysis": 0.6,
        "choose_trend_analysis": 0.55,
        "choose_transformer": 0.55,
    }
    for ai_name, weight in ai_weights.items():
        try:
            func = globals().get(ai_name)
            if not func:
                continue
            room = func()
            w = float(weight)
            perf = AI_PERFORMANCE.get(ai_name, {}) or AI_PERFORMANCE.get(ai_name.replace("choose_", "").upper(), {})
            total_p = int(perf.get("total", 0) or 0)
            if total_p >= 4:
                wr = float(perf.get("wins", 0)) / total_p
                w *= 0.5 + 1.0 * wr
            votes[room] += w
        except Exception:
            continue

    if not votes:
        picks = _vth_conservative_pick(list(ROOM_ORDER), 1)
        return picks[0] if picks else random.choice(ROOM_ORDER)
    top = _vth_pick_max(dict(votes))
    picks = _vth_conservative_pick([top] + sorted(votes.keys(), key=lambda r: votes[r], reverse=True), 1)
    return picks[0] if picks else top

ENHANCED_LOGIC_MAP = {
    "RANDOM": choose_random,
    "MIN_PLAYER_BET": choose_min_player_bet,
    "PROBABILITY": choose_probability,
    "FOLLOW_KILLER": choose_follow_killer,
    "SEQUENTIAL": choose_sequential,
    "KILLER_PERSONALITY": choose_killer_personality_enhanced,
    "SMART_SAFE": choose_smart_safe_enhanced,
    "FOLLOW_KILLER_DELAYED": choose_follow_killer_delayed,
    "HIDE_SEEK_MASTER": choose_hide_seek_master,
    "BALANCE": choose_balance,
    "MOST_PLAYERS": choose_most_players,
    "LEAST_PLAYERS": choose_least_players,
    "RICHEST": choose_richest,
    "POOREST": choose_poorest,
    "AVOID_RESULT": choose_avoid_result,
    "MEDIAN": choose_median,
    "PATTERN": choose_pattern,
    "VIP_RANDOM": choose_vip_random,
    "KILLER_WAVE": choose_killer_wave,
    "PSYCHO_ANALYSIS": choose_psycho_analysis,
    "MARKOV_CHAIN": choose_markov_chain,
    "DEEP_LEARNING": choose_deep_learning_enhanced,
    "REINFORCEMENT": choose_reinforcement,
    "BAYESIAN": choose_bayesian,
    "K_MEANS": choose_k_means,
    "NEURAL": choose_neural,
    "FUZZY": choose_fuzzy,
    "GENETIC": choose_genetic,
    "ANT_COLONY": choose_ant_colony,
    "PARTICLE_SWARM": choose_particle_swarm,
    "KNN": choose_knn,
    "DECISION_TREE": choose_decision_tree,
    "RANDOM_FOREST": choose_random_forest,
    "GRADIENT_BOOST": choose_gradient_boost,
    "LSTM": choose_lstm,
    "TRANSFORMER": choose_transformer,
    "ENSEMBLE": choose_ensemble_enhanced,
    "SAFE_RISK": choose_safe_risk,
    "CYCLE_ANALYSIS": choose_cycle_analysis,
    "TREND_ANALYSIS": choose_trend_analysis,
    "ALL": choose_all,
}


# LOGIC 44 — TỔNG HỢP VIP (ĐẶT CUỐI MENU)
def choose_tong_hop() -> int:
    """Logic 44 — Tổng Hợp VIP: bỏ phiếu từ logic VIP 22-43 và cộng điểm rủi ro.

    Đây là lựa chọn tổng hợp ở cuối menu VIP; ALL IN V2 cũng dùng hàm này
    để chọn phòng khi key VIP đã được xác thực.
    """
    rooms = [int(r) for r in ROOM_ORDER]
    if not rooms:
        return 1
    candidates = [r for r in rooms if r != last_killed_room]
    if not candidates:
        candidates = list(rooms)

    vote_scores = {r: 0.0 for r in candidates}
    vote_counts = {r: 0 for r in candidates}

    # Tổng hợp các logic VIP 22-43. ENSEMBLE vẫn là một phiếu với trọng số thấp hơn.
    # Bỏ TONG_HOP/ALL để tránh tự gọi đệ quy khi chạy Logic 44 hoặc ALL IN V2.
    for ai_key in VIP_AI_LIST:
        if ai_key in {"TONG_HOP", "ALL"}:
            continue
        func = ENHANCED_LOGIC_MAP.get(ai_key)
        if not callable(func):
            continue
        try:
            room = int(func())
            if room not in vote_scores:
                continue
            weight = 0.55 if ai_key == "ENSEMBLE" else 1.0
            try:
                perf = AI_PERFORMANCE.get(func.__name__, {}) or AI_PERFORMANCE.get(ai_key, {}) or {}
                total = int(perf.get("total", 0) or 0)
                if total >= 4:
                    win_rate = float(perf.get("wins", 0) or 0) / max(1, total)
                    weight *= 0.5 + win_rate
            except Exception:
                pass
            vote_scores[room] += weight
            vote_counts[room] += 1
        except Exception as exc:
            log_debug(f"TONG_HOP VIP: lỗi ở {ai_key}: {exc}")

    # Bổ sung điểm an toàn tổng quát và ước lượng risk realtime.
    try:
        unified = _vth_unified_scores(noise=0.0)
        vals = [float(unified.get(r, 0.0) or 0.0) for r in candidates]
        lo = min(vals) if vals else 0.0
        hi = max(vals) if vals else 1.0
        span = hi - lo
        for room in candidates:
            raw = float(unified.get(room, 0.0) or 0.0)
            normalized = ((raw - lo) / span) if span > 1e-12 else 0.5
            vote_scores[room] += 0.35 * normalized
    except Exception as exc:
        log_debug(f"TONG_HOP VIP: unified score lỗi: {exc}")

    try:
        for row in compute_vth_room_risk() or []:
            rid = int(row.get("id") or row.get("room") or 0)
            if rid not in vote_scores:
                continue
            risk_pct = max(0.0, min(100.0, float(row.get("risk_pct", 50) or 50)))
            vote_scores[rid] += 0.75 * (1.0 - risk_pct / 100.0)
    except Exception as exc:
        log_debug(f"TONG_HOP VIP: risk score lỗi: {exc}")

    # Phạt nhẹ phòng vừa bị hạ gần đây và phòng tập trung quá đông/tiền.
    try:
        recent = list(game_kill_log)[-10:]
        max_players = max((float(room_state.get(r, {}).get("players", 0) or 0) for r in candidates), default=1.0) or 1.0
        max_bet = max((float(room_state.get(r, {}).get("bet", 0) or 0) for r in candidates), default=1.0) or 1.0
        for room in candidates:
            state = room_state.get(room, {})
            players = max(0.0, float(state.get("players", 0) or 0))
            bet = max(0.0, float(state.get("bet", 0) or 0))
            vote_scores[room] -= 0.15 * (players / max_players)
            vote_scores[room] -= 0.15 * (bet / max_bet)
            vote_scores[room] -= 0.10 * recent.count(room)
    except Exception as exc:
        log_debug(f"TONG_HOP VIP: điều chỉnh phòng lỗi: {exc}")

    best_score = max(vote_scores.values())
    best_rooms = [r for r, score in vote_scores.items() if abs(score - best_score) < 1e-9]
    if len(best_rooms) > 1:
        # Khi điểm bằng nhau ưu tiên phòng có nhiều phiếu, rồi chọn ngẫu nhiên nếu vẫn hòa.
        max_votes = max(vote_counts.get(r, 0) for r in best_rooms)
        best_rooms = [r for r in best_rooms if vote_counts.get(r, 0) == max_votes]
    return int(random.choice(best_rooms or candidates))


ENHANCED_LOGIC_MAP["TONG_HOP"] = choose_tong_hop


def _vth_current_key_type(is_vip=None) -> str:
    """Lấy quyền FREE/VIP hiện tại từ trạng thái xác thực của bot."""
    if is_vip is None:
        try:
            valid, _reason, detected_vip = check_activation_valid(exit_on_expired=False)
            if valid:
                is_vip = bool(detected_vip)
            else:
                is_vip = bool(IS_VIP_USER or is_vip_activated())
        except Exception:
            try:
                is_vip = bool(IS_VIP_USER or is_vip_activated())
            except Exception:
                is_vip = bool(IS_VIP_USER)
    return "vip" if bool(is_vip) else "free"


def choose_rooms_multi(mode: str, n: int = 1, is_vip=None) -> Tuple[List[int], str]:
  
    n = max(1, min(4, int(n or 1)))
    mode = str(mode or "ENSEMBLE").upper()
    key_type = _vth_current_key_type(is_vip)
    if not is_ai_available(mode, key_type):
        mode = "RANDOM"
    try:
        scores = _vth_unified_scores(noise=0.02)
    except Exception:
        scores = {r: random.random() for r in ROOM_ORDER}
    try:
        for row in compute_vth_room_risk() or []:
            rid = int(row.get("id") or 0)
            if rid in scores:
                scores[rid] += 0.40 * (1.0 - float(row.get("risk_pct", 50) or 50) / 100.0)
    except Exception:
        pass
    try:
        if mode != "SAFE_RISK":
            primary, _ = choose_room_tn(mode, is_vip=(key_type == "vip"))
            if primary in scores:
                scores[primary] += 0.25
    except Exception:
        pass
    ranked = sorted(scores.keys(), key=lambda r: scores[r], reverse=True)
    filtered = _vth_conservative_pick(ranked, n)
    return filtered, mode


def choose_room_tn(mode: str, is_vip: Optional[bool] = None) -> Tuple[int, str]:
    """Chạy logic VTH, khóa VIP cho FREE và giữ ALL IN V1/V2 đúng quyền."""
    mode = str(mode or "RANDOM").upper()
    key_type = _vth_current_key_type(is_vip)

    # ALL IN không thay đổi cách cược: V1 dùng chọn phòng FREE, V2 dùng Tổng Hợp VIP.
    # Nhãn trả về vẫn là ALL để phần đặt cược lấy toàn bộ số dư tài khoản.
    if mode == "ALL":
        try:
            chosen_room = choose_tong_hop() if key_type == "vip" else choose_all()
            chosen_room = int(chosen_room)
            if chosen_room not in ROOM_ORDER:
                raise ValueError(f"ALL IN trả về phòng không hợp lệ: {chosen_room!r}")
            return chosen_room, "ALL"
        except Exception as exc:
            log_debug(f"Lỗi phân tích ALL IN {key_type.upper()}: {exc}")
            try:
                fallback_room = int(choose_safe_risk() if key_type == "vip" else choose_random())
            except Exception:
                fallback_room = ROOM_ORDER[0] if ROOM_ORDER else 1
            if ROOM_ORDER and fallback_room not in ROOM_ORDER:
                fallback_room = ROOM_ORDER[0]
            return fallback_room, "ALL"

    if not is_ai_available(mode, key_type):
        print(f"⚠️ AI {mode} không khả dụng với key {key_type.upper()}. Chuyển sang RANDOM.", flush=True)
        mode = "RANDOM"
    func = ENHANCED_LOGIC_MAP.get(mode, choose_random)
    try:
        chosen_room = int(func())
        if chosen_room not in ROOM_ORDER:
            raise ValueError(f"{mode} trả về phòng không hợp lệ: {chosen_room!r}")
        return chosen_room, mode
    except Exception as exc:
        log_debug(f"Lỗi thuật toán VTH {mode}: {exc}")
        try:
            fallback_room = int(choose_random())
        except Exception:
            fallback_room = ROOM_ORDER[0] if ROOM_ORDER else 1
        if ROOM_ORDER and fallback_room not in ROOM_ORDER:
            fallback_room = ROOM_ORDER[0]
        return fallback_room, "RANDOM"


def update_ai_performance(algo_name: str, is_win: bool):
    if is_win:
        AI_PERFORMANCE[algo_name]["wins"] += 1
    else:
        AI_PERFORMANCE[algo_name]["losses"] += 1
    AI_PERFORMANCE[algo_name]["total"] += 1
    try:
        with open('ai_performance.json', 'w', encoding='utf-8') as f:
            json.dump(dict(AI_PERFORMANCE), f, indent=2)
    except:
        pass

def get_best_ai() -> str:
    best_ai = "RANDOM"
    best_rate = 0
    for ai_name, stats in AI_PERFORMANCE.items():
        if stats["total"] >= 5:
            rate = stats["wins"] / stats["total"]
            if rate > best_rate:
                best_rate = rate
                best_ai = ai_name
    return best_ai


# ================== API VÀ ĐẶT CƯỢC VTH ==================

def api_headers() -> Dict[str, str]:
    return {"content-type": "application/json", "user-agent": "Mozilla/5.0", "user-id": str(USER_ID) if USER_ID else "", "user-secret-key": SECRET_KEY if SECRET_KEY else ""}

def place_bet_http(issue: int, room_id: int, amount: float) -> dict:
    payload = {"asset_type": "BUILD", "user_id": USER_ID, "room_id": int(room_id), "bet_amount": float(amount)}
    try:
        r = requests.post(BET_API_URL, headers=api_headers(), json=payload, timeout=8)
        try:
            return r.json()
        except Exception:
            return {"raw": r.text, "http_status": r.status_code}
    except Exception as e:
        return {"error": str(e)}

def record_bet(issue: int, room_id: int, amount: float, resp: dict, algo_used: Optional[str] = None) -> dict:
    """Ghi lịch sử bằng đúng snapshot phòng đã chốt cho ván.

    Không lấy lại predicted_room hiện tại vì sau khi sang ván mới biến này
    có thể đã đổi sang phòng khác. Snapshot theo issue giúp tránh tình trạng
    UI hiển thị P6 nhưng lịch sử lại ghi P7.
    """
    try:
        issue = int(issue)
    except (TypeError, ValueError):
        return {}

    if issue in history_recorded_issue:
        return next((r for r in bet_history if r.get("issue") == issue), {})

    snapshot = bet_selection_by_issue.get(issue) or {}
    selected_room = snapshot.get("room", room_id)
    try:
        selected_room = int(selected_room)
    except (TypeError, ValueError):
        selected_room = None

    # Tuyệt đối không ghi ID ngoài P1-P8. Nếu snapshot lỗi thì dùng room_id
    # truyền vào nhưng vẫn validate lại trước khi đưa vào lịch sử.
    if selected_room not in ROOM_ORDER:
        try:
            selected_room = int(room_id)
        except (TypeError, ValueError):
            selected_room = None
    if selected_room not in ROOM_ORDER:
        log_debug(f"Bỏ ghi lịch sử: room không hợp lệ issue={issue}, room={room_id!r}")
        return {}

    if room_id != selected_room:
        log_debug(
            f"ROOM SNAPSHOT mismatch issue={issue}: request_room={room_id!r}, "
            f"selected_room={selected_room}. Dùng selected_room."
        )

    history_recorded_issue.add(issue)
    now = datetime.now(tz).strftime("%H:%M:%S")
    display_algo = algo_used or snapshot.get("algo") or selected_vth_algo or settings.get("algo")
    if display_algo == "RANDOM" and selected_vth_algo and selected_vth_algo != "RANDOM":
        display_algo = selected_vth_algo

    rec = {
        "issue": issue,
        "room": selected_room,
        "amount": float(amount),
        "time": now,
        "resp": resp,
        "result": "Đang",
        "algo": display_algo,
        "delta": None,
        "balance_before_bet": None,
        "win_streak": win_streak,
        "lose_streak": lose_streak,
    }
    bet_history.append(rec)
    return rec

def place_bet_async(issue: int, room_id: int, amount: float, algo_used: Optional[str] = None):
    if _check_daily_pause_window(_vth_daily_pause_window, "VTH"):
        return
    try:
        issue = int(issue)
    except (TypeError, ValueError):
        return

    # Luôn ưu tiên snapshot của đúng ván.
    snapshot_room = (bet_selection_by_issue.get(issue) or {}).get("room")
    if snapshot_room in ROOM_ORDER:
        room_id = int(snapshot_room)
    else:
        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            return
        if room_id not in ROOM_ORDER:
            log_debug(f"place_bet_async bỏ qua room không hợp lệ issue={issue}: {room_id!r}")
            return

    if issue in bet_sent_for_issue:
        return
    bet_sent_for_issue.add(issue)
    def worker():

        # Kiểm tra lại trong thread để không gửi cược nếu khung nghỉ vừa bắt đầu.
        if _check_daily_pause_window(_vth_daily_pause_window, "VTH"):
            bet_sent_for_issue.discard(issue)
            return

        # Chụp số dư ngay trước khi gửi lệnh để tính lãi/lỗ BUILD thực tế.
        try:
            balance_before_bet = float(current_build) if current_build is not None else None
        except Exception:
            balance_before_bet = None

        res = place_bet_http(issue, room_id, amount)
        rec = record_bet(issue, room_id, amount, res, algo_used=algo_used)
        if rec is not None:
            rec["balance_before_bet"] = balance_before_bet
        if isinstance(res, dict) and (res.get("msg") == "ok" or res.get("code") == 0 or res.get("status") in ("ok", 1)):
            bet_sent_for_issue.add(issue)

        else:
            bet_sent_for_issue.discard(issue)
            history_recorded_issue.discard(issue)
            console.print(f"[red]❌ Đặt lỗi v{issue}: {res}[/]")
    threading.Thread(target=worker, daemon=True).start()

def lock_prediction_if_needed(force: bool = False):
    global prediction_locked, predicted_room, ui_state
    global current_bet, _rounds_placed_since_skip
    global skip_next_round_flag, _skip_rounds_remaining
    global stop_flag
    global last_display_bet
    global count_down
    global analysis_ready_ts

    if stop_flag or capital_stop_pending:
        return

    # Trong khung giờ đã cài, VTH không phân tích/chốt dự đoán.
    if _check_daily_pause_window(_vth_daily_pause_window, "VTH"):
        return

    if prediction_locked and not force:
        return

    if issue_id is None:
        return

    if not force and analysis_ready_ts is not None and time.time() < analysis_ready_ts:
        return

    if not force:
        remaining = _vth_get_remaining_seconds()
        # Tự động chốt dự đoán và đặt cược khi countdown còn 10 giây.
        if remaining is not None and remaining > 10:
            return

    mode = settings.get("algo", "RANDOM").upper()
    is_vip = _vth_current_key_type() == "vip"

    if not is_ai_available(mode, "vip" if is_vip else "free"):
        print("❌ Logic này chỉ dành cho Key VIP! Chuyển về RANDOM.", flush=True)
        mode = "RANDOM"
        settings["algo"] = "RANDOM"
   
    try:
        chosen, algo_used = choose_room_tn(mode, is_vip=is_vip)
        if chosen not in ROOM_ORDER:
            raise ValueError(f"Phòng không hợp lệ: {chosen!r}")
    except Exception as e:
        log_debug(f"Lỗi chọn phòng ({mode}): {e}")
        chosen = choose_random()
        if chosen not in ROOM_ORDER:
            chosen = ROOM_ORDER[0]
        algo_used = "RANDOM"
    predicted_room = int(chosen)
    prediction_locked = True

    all_in_bet_amount = None
    if mode == "ALL":
        try:
            all_in_balance, _, _ = fetch_balances_3games(retries=1, timeout=3)
            if all_in_balance is not None:
                all_in_bet_amount = float(all_in_balance)
        except Exception as exc:
            log_debug(f"ALL IN balance snapshot error: {exc}")

    try:
        room_label = ROOM_NAMES.get(predicted_room, f"Phòng {predicted_room}")
        algo_label = get_vth_algo_display_name(algo_used, is_vip)

        if mode == "ALL":
            current_bet_display = all_in_bet_amount if all_in_bet_amount is not None else 0.0
        else:
            current_bet_display = current_bet if current_bet not in (None, 0) else base_bet
        try:
            last_display_bet = float(current_bet_display)
        except Exception:
            pass
        if True:
            prediction_message = (
                f"📌 Ván: {issue_id or '-'}\n"
                f"🏠 Đánh: {room_label} (P{predicted_room})\n"
                f"🧠 Thuật toán: {algo_label}\n"
                f"💰 Cược: {float(current_bet_display or 0):,.2f} BUILD"
            )
            print(prediction_message, flush=True)
    except Exception as exc:
        log_debug(f"Telegram prediction display error: {exc}")

    try:
        current_issue = int(issue_id)
        bet_selection_by_issue[current_issue] = {
            "room": int(predicted_room),
            "algo": str(algo_used),
            "created_at": time.time(),
        }

        if len(bet_selection_by_issue) > 100:
            for old_issue in sorted(bet_selection_by_issue)[:-60]:
                bet_selection_by_issue.pop(old_issue, None)
    except (TypeError, ValueError):
        pass

    if _skip_rounds_remaining > 0:
        console.print(f"[yellow]⏸️ Đang nghỉ {_skip_rounds_remaining} ván theo cấu hình sau khi thua.[/]")
        _skip_rounds_remaining -= 1
        return
    if skip_next_round_flag:
        console.print("[yellow]⏸️ TẠM DỪNG THEO DÕI SÁT THỦ[/]")
        skip_next_round_flag = False
        return
    if run_mode == "AUTO":
        bld = current_build
        if bld is None:
            bld, _, _ = fetch_balances_3games(retries=1, timeout=3)
            if bld is None:
                console.print("[yellow]⚠️ Không lấy được số dư, không thể đặt cược. Sẽ thử lại...[/]")
                prediction_locked = False
                predicted_room = None
                ui_state = "ANALYZING"
                return
        if current_bet is None:
            current_bet = base_bet
        if mode == "ALL":
            if all_in_bet_amount is None:
                bld_fresh, _, _ = fetch_balances_3games(retries=1, timeout=3)
                if bld_fresh is None:
                    console.print("[yellow]⚠️ Không lấy được số dư BUILD cho ALL IN.[/]")
                    prediction_locked = False
                    predicted_room = None
                    ui_state = "ANALYZING"
                    return
                all_in_bet_amount = float(bld_fresh)
            amt = float(all_in_bet_amount)
            last_display_bet = amt
        else:
            amt = float(current_bet)
            last_display_bet = amt
        if amt <= 0:
            console.print("[yellow]⚠️ Số tiền đặt không hợp lệ. Bỏ qua.[/]")
            return
        if mode != "ALL" and amt > bld:
            console.print(f"[red]🔥 VỐN KHÔNG ĐỦ ĐỂ GẤP THẾP! Cần {amt:,.2f} nhưng chỉ có {bld:,.2f}. Reset về cược gốc.[/red]")
            current_bet = float(base_bet)
            amt = float(current_bet)
            last_display_bet = amt
            if amt > bld:
                console.print(f"[red]💀 Vốn không đủ để đặt cược gốc ({amt:,.2f}). Dừng tool.[/red]")
                stop_flag = True
                return
                
        place_bet_async(issue_id, predicted_room, amt, algo_used=algo_used)
        ui_state = "PREDICTED"
        _rounds_placed_since_skip += 1

        if (
            bet_rounds_before_skip > 0
            and _rounds_placed_since_skip >= bet_rounds_before_skip
        ):
            completed_rounds = _rounds_placed_since_skip
            skip_next_round_flag = True
            _rounds_placed_since_skip = 0
            try:
                snapshot = bet_selection_by_issue.setdefault(int(issue_id), {})
                snapshot["anti_soi_notice"] = (
                    f"🛡️ CHỐNG SOI KÍCH HOẠT\n"
                    f"✅ Đã cược đủ {completed_rounds} ván theo cấu hình.\n"
                    f"⏸️ Ván kế tiếp sẽ nghỉ 1 ván."
                )
            except Exception:
                pass

# ================== WEBSOCKET VTH ==================

def safe_send_enter_game(ws):
    if not ws:
        log_debug("safe_send_enter_game: ws None")
        return
    try:
        payload = {"msg_type": "handle_enter_game", "asset_type": "BUILD", "user_id": USER_ID, "user_secret_key": SECRET_KEY}
        ws.send(json.dumps(payload))
        log_debug("Sent enter_game")
    except Exception as e:
        log_debug(f"safe_send_enter_game err: {e}")

def _extract_issue_id(d: Dict[str, Any]) -> Optional[int]:
    if not isinstance(d, dict):
        return None
    possible = []
    for key in ("issue_id", "issueId", "issue", "id"):
        v = d.get(key)
        if v is not None:
            possible.append(v)
    if isinstance(d.get("data"), dict):
        for key in ("issue_id", "issueId", "issue", "id"):
            v = d["data"].get(key)
            if v is not None:
                possible.append(v)
    for p in possible:
        try:
            return int(p)
        except Exception:
            try:
                return int(str(p))
            except Exception:
                continue
    return None

def on_open(ws):
    _ws["ws"] = ws
    # Không gửi thông báo kết nối lặp lại qua Telegram mỗi lần websocket reconnect.
    safe_send_enter_game(ws)
def on_message(ws, message):
    global issue_id, count_down, killed_room, round_index, ui_state, analysis_start_ts, issue_start_ts, issue_end_ts
    global prediction_locked, predicted_room, last_killed_room, last_killed_room_delayed, last_msg_ts, current_bet
    global win_streak, lose_streak, max_win_streak, max_lose_streak, cumulative_profit, _skip_rounds_remaining, stop_flag

    last_msg_ts = time.time()

    try:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8", errors="replace")
            except Exception:
                message = str(message)

        data = None
        try:
            data = json.loads(message)
        except Exception:
            try:
                data = json.loads(message.replace("'", '"'))
            except Exception:
                log_debug(f"on_message non-json: {str(message)[:200]}")
                return

        if isinstance(data, dict) and isinstance(data.get("data"), str):
            try:
                inner = json.loads(data.get("data"))
                merged = dict(data)
                merged.update(inner)
                data = merged
            except Exception:
                pass

        msg_type = str(data.get("msg_type") or data.get("type") or "")
        new_issue = _extract_issue_id(data)

        if msg_type == "notify_enter_game":
            info = data.get("info", {})
            if isinstance(info, dict):
                if info.get("start_time"):
                    st = float(info.get("start_time"))
                    if st > time.time() * 500:
                        st /= 1000.0
                    issue_start_ts = st

                if info.get("end_time"):
                    et = float(info.get("end_time"))
                    if et > time.time() * 500:
                        et /= 1000.0
                    issue_end_ts = et

            if data.get("last_killed_room_id") is not None:
                try:
                    candidate_kill = int(data["last_killed_room_id"])
                except (TypeError, ValueError):
                    candidate_kill = None
                last_killed_room = candidate_kill if candidate_kill in ROOM_ORDER else None

            room_stat = data.get("room_stat", [])
            if isinstance(room_stat, list):
                for rm in room_stat:
                    _process_room_update(rm)

        elif msg_type == "notify_issue_stat" or "issue_stat" in msg_type:
            rooms = data.get("rooms") or []
            if not rooms and isinstance(data.get("data"), dict):
                rooms = data["data"].get("rooms", [])

            for rm in (rooms or []):
                _process_room_update(rm)
                try:
                    rid = int(rm.get("room_id") or rm.get("roomId") or rm.get("id"))
                except Exception:
                    continue

                # _process_room_update đã parse và validate dữ liệu.
                state = room_state.get(rid)
                if state is None:
                    continue
                room_stats[rid]["last_players"] = state.get("players", 0)
                room_stats[rid]["last_bet"] = state.get("bet", 0)

            if new_issue is not None and new_issue != issue_id:
                log_debug(f"New issue: {issue_id} -> {new_issue}")
                issue_id = new_issue
                processed_result_issues.clear()
                history_recorded_issue.clear()

                if data.get("start_time"):
                    st = float(data.get("start_time"))
                    if st > time.time() * 500:
                        st /= 1000.0
                    issue_start_ts = st
                else:
                    issue_start_ts = time.time()

                if data.get("end_time"):
                    et = float(data.get("end_time"))
                    if et > time.time() * 500:
                        et /= 1000.0
                    issue_end_ts = et
                else:
                    issue_end_ts = time.time() + 60

                count_down = 60
                round_index += 1
                killed_room = None
                prediction_locked = False
                predicted_room = None
                ui_state = "ANALYZING"
                analysis_start_ts = time.time()
                analysis_ready_ts = analysis_start_ts + analysis_initial_delay

        elif msg_type == "notify_count_down" or "count_down" in msg_type:
            try:
                new_count = (
                    data.get("count_down")
                    or data.get("countDown")
                    or data.get("count")
                    or data.get("data", {}).get("count_down")
                    or data.get("data", {}).get("countDown")
                )

                if new_count is not None:
                    # Đồng bộ countdown ngay khi server gửi
                    count_down = max(0, int(float(new_count)))
                    last_msg_ts = time.time()

                    # Không để countdown tự trôi giữa các lần update
                    issue_end_ts = None

                    # Khóa dự đoán khi countdown còn 10 giây
                    if count_down <= 10 and not prediction_locked:
                        lock_prediction_if_needed()

            except Exception as e:
                log_debug(f"notify_count_down error: {e}")

        elif msg_type == "notify_result":
            if issue_id in processed_result_issues:
                return

            processed_result_issues.add(issue_id)
            kr = None
            possible_keys = ["killed_room", "killed_room_id", "killedRoom", "killedRoomId", "kill_room"]

            for key in possible_keys:
                if data.get(key) is not None:
                    kr = data.get(key)
                    break

            if kr is None and isinstance(data.get("data"), dict):
                for key in possible_keys:
                    if data["data"].get(key) is not None:
                        kr = data["data"].get(key)
                        break

            if kr is not None:
                try:
                    krid = int(kr)
                except Exception:
                    krid = kr

                if krid not in ROOM_ORDER:
                    log_debug(f"Bỏ qua killed_room_id không hợp lệ: {krid!r}")
                    return

                killed_room = krid
                game_kill_log.append(krid)
                update_killer_history(krid)

                last_killed_room = krid
                last_killed_room_delayed = krid

                for rid in ROOM_ORDER:
                    if rid == krid:
                        room_stats[rid]["kills"] += 1
                        room_stats[rid]["last_kill_round"] = round_index
                    else:
                        room_stats[rid]["survives"] += 1

                balance_before_payout = current_build
                rec = None
                for b in reversed(bet_history):
                    if b.get("issue") == issue_id:
                        rec = b
                        break

                if rec is not None:
                    try:
                        placed_room = int(rec.get("room"))
                        if placed_room != int(killed_room):
                            rec["result"] = "Thắng"
                            current_bet = float(base_bet)
                            last_display_bet = float(base_bet)
                            win_streak += 1
                            lose_streak = 0
                            if win_streak > max_win_streak:
                                max_win_streak = win_streak
                        else:
                            rec["result"] = "Thua"

                            try:
                                previous_bet = float(rec.get("amount", base_bet) or base_bet)

                                factor = float(multiplier or 1.0)
                                if factor <= 0:
                                    factor = 1.0
                                next_bet = previous_bet * factor
                                if next_bet <= 0:
                                    next_bet = float(base_bet)
                                current_bet = next_bet

                                last_display_bet = next_bet
                            except Exception:
                                current_bet = float(base_bet)
                                last_display_bet = float(base_bet)

                            lose_streak += 1
                            win_streak = 0
                            if lose_streak > max_lose_streak:
                                max_lose_streak = lose_streak

                            if pause_after_losses > 0:
                                _skip_rounds_remaining = pause_after_losses
                                rec["pause_after_loss_notice"] = (
                                    f"⏸️ NGHỈ SAU THUA KÍCH HOẠT\n"
                                    f"❌ Ván vừa rồi: THUA.\n"
                                    f"⏸️ Sẽ nghỉ {pause_after_losses} ván theo cấu hình."
                                )

                        threading.Thread(
                            target=_background_update_balance_after_result,
                            args=(rec, balance_before_payout),
                            daemon=True
                        ).start()

                        rec["win_streak"] = win_streak
                        rec["lose_streak"] = lose_streak

                    except Exception as e:
                        log_debug(f"result handle err: {e}")

                    try:
                        if True:
                            _placed_room_snapshot = int(placed_room)
                            _killed_room_snapshot = int(killed_room)
                            _issue_snapshot = issue_id
                            _result_label_snapshot = rec.get("result", "Đang")
                            _win_streak_snapshot = int(win_streak)
                            _lose_streak_snapshot = int(lose_streak)
                            _max_win_streak_snapshot = int(max_win_streak)
                            _max_lose_streak_snapshot = int(max_lose_streak)
                            _tool_start_snapshot = tool_start_time
                            try:
                                _algo_snapshot = (
                                    (bet_selection_by_issue.get(int(_issue_snapshot)) or {}).get("algo")
                                    or rec.get("algo")
                                    or selected_vth_algo
                                    or settings.get("algo")
                                    or "RANDOM"
                                )
                                _algo_snapshot = get_vth_algo_display_name(_algo_snapshot)
                            except Exception:
                                _algo_snapshot = "RANDOM"

                            def _emit_vth_result_telegram():
                                try:
                                    for _wait in range(32):
                                        if rec.get("delta") is not None:
                                            break
                                        time.sleep(0.25)

                                    placed_label = ROOM_NAMES.get(
                                        _placed_room_snapshot,
                                        f"Phòng {_placed_room_snapshot}"
                                    )
                                    killed_label = ROOM_NAMES.get(
                                        _killed_room_snapshot,
                                        f"Phòng {_killed_room_snapshot}"
                                    )
                                    result_word = "WIN" if _result_label_snapshot == "Thắng" else "LOSE"

                                    anti_soi_notice = ""
                                    try:
                                        anti_soi_notice = str(
                                            (bet_selection_by_issue.get(int(_issue_snapshot)) or {}).get("anti_soi_notice") or ""
                                        ).strip()
                                    except Exception:
                                        anti_soi_notice = ""

                                    pause_after_loss_notice = str(
                                        rec.get("pause_after_loss_notice") or ""
                                    ).strip()

                                    capital_limit_notice = str(
                                        rec.get("capital_limit_notice") or ""
                                    ).strip()

                                    try:
                                        start_balance_display = (
                                            float(starting_balance)
                                            if starting_balance is not None else None
                                        )
                                    except Exception:
                                        start_balance_display = None

                                    try:
                                        current_balance_display = (
                                            float(current_build)
                                            if current_build is not None else None
                                        )
                                    except Exception:
                                        current_balance_display = None

                                    def _fmt_balance_display(value):
                                        if value is None:
                                            return "--"
                                        try:
                                            return f"{value:,.2f}"
                                        except Exception:
                                            return str(value)

                                    try:
                                        cumulative_build_pnl = _vth_total_build_pnl()
                                    except Exception:
                                        cumulative_build_pnl = None

                                    pnl_text = (
                                        f"{cumulative_build_pnl:+,.4f}"
                                        if cumulative_build_pnl is not None else "--"
                                    )

                                    try:
                                        elapsed = max(
                                            0,
                                            int(time.time() - _tool_start_snapshot)
                                        )
                                    except Exception:
                                        elapsed = 0
                                    run_h, rem = divmod(elapsed, 3600)
                                    run_m, _ = divmod(rem, 60)

                                    result_message = (
                                        f"👤 User:{USER_ID}\n"
                                        f"🧠 Thuật toán : {_algo_snapshot}\n"
                                        f"📌 Ván: {_issue_snapshot or '-'}\n"
                                        f"🎯 ĐÁNH:{placed_label} (P{_placed_room_snapshot})\n"
                                        f"☠️ PHÒNG VỪA BỊ KILL:{killed_label} (P{_killed_room_snapshot})\n"
                                        f"🏆 WIN:{_win_streak_snapshot}(MAX:{_max_win_streak_snapshot}) | 💀 LOSE:{_lose_streak_snapshot}(MAX:{_max_lose_streak_snapshot})\n"
                                        f"{'✅ WIN' if result_word == 'WIN' else '❌ LOSE'}\n"
                                        f"💸 LÃI VÁN: {float(rec.get('delta') or 0):.4f}\n"
                                        f"📊 LÃI/LỖ: {pnl_text}\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"💳 SỐ DƯ: {_fmt_balance_display(start_balance_display)} -> {_fmt_balance_display(current_balance_display)}\n"
                                        f"⏳ CHẠY BOT: {run_h} Giờ {run_m} Phút"
                                    )

                                    notices = [x for x in (
                                        anti_soi_notice,
                                        pause_after_loss_notice,
                                        capital_limit_notice,
                                    ) if x]
                                    capital_limit_reached = bool(rec.get("capital_limit_reached"))

                                    print(result_message, flush=True)
                                    if notices:

                                        time.sleep(max(FLUSH_INTERVAL + 0.15, 0.55))
                                        print("\n\n".join(notices), flush=True)

                                    if capital_limit_reached:
                                        time.sleep(max(1.20, FLUSH_INTERVAL + 0.70))
                                        _stop_for_capital_limit()
                                except Exception as exc:
                                    log_debug(f"Telegram result display error: {exc}")

                            threading.Thread(
                                target=_emit_vth_result_telegram,
                                daemon=True
                            ).start()
                    except Exception as exc:
                        log_debug(f"Telegram result display setup error: {exc}")

            ui_state = "RESULT"

    except Exception as e:
        log_debug(f"on_message err: {e}")

def _stop_for_capital_limit(message: str = None):
    if stop_flag:
        return
    _shutdown_vth_runtime(message)


def _check_capital_limit_now(rec: Optional[dict] = None) -> bool:
    global capital_stop_pending

    if (
        stop_when_profit_reached
        and profit_target is not None
        and isinstance(current_build, (int, float))
        and current_build >= profit_target
    ):
        notice = (
            f"🎯 ĐẠT MỤC TIÊU LÃI\n"
            f"💳 Số dư hiện tại: {current_build:,.2f} BUILD\n"
            f"🏁 Mục tiêu: {profit_target:,.2f} BUILD\n"
            f"✅ ĐỪNG VTH."
        )
        if isinstance(rec, dict):

            rec["capital_limit_notice"] = notice
            rec["capital_limit_reached"] = True
            capital_stop_pending = True
        else:
            _stop_for_capital_limit(notice)
        return True

    if (
        stop_when_loss_reached
        and stop_loss_target is not None
        and isinstance(current_build, (int, float))
        and current_build <= stop_loss_target
    ):
        notice = (
            f"🛑 ĐẠT MỨC CẮT LỖ\n"
            f"💀 Số dư hiện tại: {current_build:,.2f} BUILD\n"
            f"📉 Mức cắt lỗ: {stop_loss_target:,.2f} BUILD\n"
            f"🛑 Tool đã dừng."
        )
        if isinstance(rec, dict):

            rec["capital_limit_notice"] = notice
            rec["capital_limit_reached"] = True
            capital_stop_pending = True
        else:
            _stop_for_capital_limit(notice)
        return True

    return False

def _background_update_balance_after_result(rec: dict, balance_before: Optional[float]):

    global cumulative_profit
    global current_build, current_usdt, current_world
    global stop_flag

    try:

        if rec and rec.get("balance_before_bet") is not None:
            balance_before = rec.get("balance_before_bet")
        elif balance_before is None and rec:
            balance_before = rec.get("balance_before_bet")

        result_status = str(rec.get("result", "") if rec else "").strip().lower()
        is_win = result_status in ("thắng", "win")
        is_lose = result_status in ("thua", "lose")

        before = float(balance_before) if isinstance(balance_before, (int, float)) else None
        accepted_balance = None
        consecutive_same = 0
        last_value = None


        time.sleep(0.20)

        for _ in range(24):
            new_balance, usdt, world = fetch_balances_3games(
                retries=0,
                timeout=2
            )

            if isinstance(new_balance, (int, float)):
                new_balance = float(new_balance)
                current_usdt = usdt
                current_world = world

                if before is None:
                    accepted_balance = new_balance
                    break

                diff = new_balance - before

                if is_win:
                    valid_direction = diff > 1e-9
                elif is_lose:
                    valid_direction = diff < -1e-9
                else:
                    valid_direction = abs(diff) <= 1e-9

                if valid_direction:

                    if last_value is not None and abs(new_balance - last_value) <= 1e-9:
                        consecutive_same += 1
                    else:
                        consecutive_same = 1
                    last_value = new_balance

                    if consecutive_same >= 2:
                        accepted_balance = new_balance
                        break
                else:
                    consecutive_same = 0
                    last_value = new_balance

            time.sleep(0.30)

        if accepted_balance is None:
            if is_win:
                if before is not None:
                    current_build = before
                rec["delta"] = None
                log_debug(
                    f"WIN issue={rec.get('issue')} chưa thấy payout; không chốt PNL từ số dư tạm thời."
                )
                _check_capital_limit_now(rec)
                return
            accepted_balance = last_value

        if isinstance(accepted_balance, (int, float)):
            current_build = float(accepted_balance)

            if starting_balance is not None:
                cumulative_profit = _vth_total_build_pnl()

            capital_reached = _check_capital_limit_now(rec)

            if before is not None:
                rec["delta"] = float(accepted_balance) - before
            else:
                rec["delta"] = None

            if capital_reached:
                return

    except Exception as e:
        log_debug(f"background balance update error: {e}")

def update_killer_history(killed_room_id):
    if killed_room_id in room_state:
        killer_history.append({'players': room_state[killed_room_id].get('players', 0), 'bet': room_state[killed_room_id].get('bet', 0)})
def _process_room_update(room_data: dict):
    if not isinstance(room_data, dict):
        return
    try:
        rid = int(room_data.get("room_id") or room_data.get("roomId") or room_data.get("id"))
        if rid not in ROOM_ORDER:
            return

        players = int(_parse_number(
            room_data.get("user_cnt")
            if room_data.get("user_cnt") is not None
            else room_data.get("userCount", 0)
        ) or 0)
        bet = _parse_number(
            room_data.get("total_bet_amount")
            if room_data.get("total_bet_amount") is not None
            else (room_data.get("totalBet") if room_data.get("totalBet") is not None else room_data.get("bet", 0))
        ) or 0.0

        room_state[rid] = {"players": players, "bet": float(bet)}
        if rid not in room_stats:
            room_stats[rid] = {"kills": 0, "survives": 0, "last_kill_round": None, "last_players": 0, "last_bet": 0}
        room_stats[rid]["last_players"] = players
        room_stats[rid]["last_bet"] = float(bet)
    except (ValueError, TypeError):
        pass

def on_close(ws, code, reason):
    log_debug(f"WS closed: {code} {reason}")
def on_error(ws, err):
    log_debug(f"WS error: {err}")
def start_ws():
    backoff = 1.0
    while not stop_flag:
        try:
            ws_app = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_close=on_close, on_error=on_error)
            _ws["ws"] = ws_app
            ws_app.run_forever(ping_interval=15, ping_timeout=6)
        except Exception as e:
            log_debug(f"start_ws exception: {e}")
        t = min(backoff + random.random() * 0.8, 30)
        log_debug(f"Reconnect WS after {t}s")
        time.sleep(t)
        backoff = min(backoff * 1.8, 30)

class BalancePoller(threading.Thread):
    def __init__(self, uid: Optional[int], secret: Optional[str], poll_seconds: int = 2, on_balance=None, on_error=None, on_status=None):
        super().__init__(daemon=True)
        self.uid = uid
        self.secret = secret
        self.poll_seconds = max(1, int(poll_seconds))
        self._running = True
        self._last_balance_local: Optional[float] = None
        self.on_balance = on_balance
        self.on_error = on_error
        self.on_status = on_status

    def stop(self):
        self._running = False

    def run(self):
        if self.on_status:
            self.on_status("Kết nối...")
        while self._running and not stop_flag:
            try:
                build, world, usdt = fetch_balances_3games(params={"userId": str(self.uid)} if self.uid else None, uid=self.uid, secret=self.secret)
                if build is None:
                    raise RuntimeError("Không đọc được balance từ response")
                delta = 0.0 if self._last_balance_local is None else (build - self._last_balance_local)
                first_time = (self._last_balance_local is None)
                if first_time or abs(delta) > 0:
                    self._last_balance_local = build
                    if self.on_balance:
                        self.on_balance(float(build), float(delta), {"ts": human_ts()})
                    if self.on_status:
                        self.on_status("Đang theo dõi")
                else:
                    if self.on_status:
                        self.on_status("Đang theo dõi (không đổi)")
            except Exception as e:
                if self.on_error:
                    self.on_error(str(e))
                if self.on_status:
                    self.on_status("Lỗi kết nối vui lòng thử lại...")
            for _ in range(max(1, int(self.poll_seconds * 5))):
                if not self._running or stop_flag:
                    break
                time.sleep(0.2)
        if self.on_status:
            self.on_status("Đã dừng")

def monitor_loop():
    global last_balance_fetch_ts, last_msg_ts, stop_flag

    while not stop_flag:
        # Theo dõi chuyển trạng thái theo lịch để thông báo lúc nghỉ/chạy lại.
        _check_daily_pause_window(_vth_daily_pause_window, "VTH")
        now = time.time()

        if now - last_balance_fetch_ts >= BALANCE_POLL_INTERVAL:
            last_balance_fetch_ts = now
            try:
                fetch_balances_3games(
                    params={"userId": str(USER_ID)} if USER_ID else None
                )
            except Exception as e:
                log_debug(f"monitor fetch err: {e}")

        if now - last_msg_ts > 12:
            log_debug("No ws msg >12s, send enter_game")
            try:
                safe_send_enter_game(_ws.get("ws"))
            except Exception as e:
                log_debug(f"monitor send err: {e}")

        if now - last_msg_ts > 45:
            log_debug("No ws msg >45s, force reconnect")
            try:
                wsobj = _ws.get("ws")
                if wsobj:
                    try:
                        wsobj.close()
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            # Luôn dùng countdown / end_time từ server
            remaining = _vth_get_remaining_seconds()

            if (
                analysis_start_ts
                and (time.time() - analysis_start_ts >= analysis_duration)
                and not prediction_locked
                and remaining is not None
                and remaining <= 10
            ):
                lock_prediction_if_needed()

        except Exception as e:
            log_debug(f"monitor loop err: {e}")

        time.sleep(1)

def build_config_header():
    return Text("")
def build_step_indicator(current_step: int, total_steps: int):
    return ""
def prompt_settings() -> bool:
    global base_bet, multiplier, run_mode
    global selected_vth_algo
    global bet_rounds_before_skip, current_bet
    global pause_after_losses
    global profit_target, stop_when_profit_reached
    global stop_loss_target, stop_when_loss_reached
    global _vth_daily_pause_window

    # ================== MENU THUẬT TOÁN VTH ==================

    key_valid, key_reason, is_vip = check_activation_valid(
        exit_on_expired=False
    )

    if not key_valid:
        print(
            f"🔒 {key_reason}. Vui lòng nhập lại Key FREE hoặc Key VIP.",
            flush=True,
        )
        return False

    modes = list(SELECTION_MODES.items())
    vip_keys = set(VIP_AI_LIST)

    if is_vip:
        permission_line = "🔑 Quyền hiện tại: VIP — Logic 1-41"
    else:
        permission_line = "🔑 Quyền hiện tại: FREE — Dùng được Logic 1-18"

    lines = [
        "🧠 CHỌN LOGIC VUA THOÁT HIỂM",
        permission_line,
        "",
    ]

    for i, (key, _label) in enumerate(modes, 1):
        display_name = get_vth_algo_display_name(key, is_vip)

        if key in vip_keys and not is_vip:
            lines.append(
                f"{i}. {display_name} (🔒 CHỈ KEY VIP)"
            )
        else:
            lines.append(f"{i}. {display_name}")

    lines.extend([
        "",
        f">> Chọn số thứ tự (1-{len(modes)}):",
    ])

    print("\n".join(lines), flush=True)

    while True:
        raw = input("").strip()

        key_valid, key_reason, current_is_vip = check_activation_valid(
            exit_on_expired=False
        )

        if not key_valid:
            print(
                f"🔒 {key_reason}. Vui lòng nhập lại Key FREE hoặc Key VIP.",
                flush=True,
            )
            return False

        current_key_type = "vip" if current_is_vip else "free"
        current_available = get_available_ai_list(current_key_type)

        try:
            choice = int(raw)
        except ValueError:
            print(
                f"❌ Vui lòng nhập số từ 1 đến {len(modes)}.",
                flush=True,
            )
            continue

        if not 1 <= choice <= len(modes):
            print(
                f"❌ Vui lòng nhập từ 1 đến {len(modes)}.",
                flush=True,
            )
            continue

        selected_key = modes[choice - 1][0]

        if selected_key not in current_available:
            print("❌ Logic này chỉ dành cho Key VIP!", flush=True)
            continue

        is_vip = current_is_vip
        break

    selected_vth_algo = selected_key
    settings["algo"] = selected_key

    selected_name = get_vth_algo_display_name(
        selected_key,
        is_vip,
    )

    print(
        f"✅ Đã chọn thuật toán: {selected_name}",
        flush=True,
    )

    if selected_key == "ALL":
        pt_str = Prompt.ask("🎯 Mục tiêu lãi (0 = tắt)").strip()

        if pt_str and pt_str != "0":
            try:
                profit_target = float(pt_str)
                stop_when_profit_reached = profit_target > 0

                print(
                    f"✅ Đã đặt mục tiêu lãi: dừng khi số dư đạt "
                    f"{profit_target:,.2f} BUILD",
                    flush=True,
                )
            except ValueError:
                profit_target = None
                stop_when_profit_reached = False
                print(
                    "⚠️ Mục tiêu lãi không hợp lệ, đã tắt.",
                    flush=True,
                )
        else:
            profit_target = None
            stop_when_profit_reached = False

        stop_loss_target = None
        stop_when_loss_reached = False
        _vth_daily_pause_window = _prompt_daily_pause_window("VTH", "2")
        _reset_daily_pause_state("VTH")
        base_bet = 0.0
        multiplier = 1.0
        current_bet = 0.0
        bet_rounds_before_skip = 0
        pause_after_losses = 0
        run_mode = "AUTO"

        return True

    # ================== QUẢN LÝ VỐN ==================
    print("\n💰 QUẢN LÝ VỐN", flush=True)

    while True:
        try:
            base_bet = FloatPrompt.ask(
                "1. Cược gốc (BUILD)"
            )

            if base_bet >= 1:
                break

            print(
                "❌ Cược gốc tối thiểu là 1.0 BUILD.",
                flush=True,
            )

        except Exception:
            print(
                "❌ Vui lòng nhập số hợp lệ.",
                flush=True,
            )

    while True:
        try:
            multiplier = FloatPrompt.ask(
                "2. Hệ số nhân sau khi thua",
                default=10,
            )

            if multiplier > 0:
                break

            print(
                "❌ Hệ số phải lớn hơn 0.",
                flush=True,
            )

        except Exception:
            print(
                "❌ Vui lòng nhập số hợp lệ.",
                flush=True,
            )

    current_bet = base_bet

    # ================== QUẢN LÝ RỦI RO ==================
    print("\n🛡️ QUẢN LÝ RỦI RO", flush=True)

    bet_rounds_before_skip = IntPrompt.ask(
        "3. Chống soi: nghỉ 1 ván sau bao nhiêu ván cược? "
        "(0 = Tắt)",
        default=0,
    )

    pause_after_losses = IntPrompt.ask(
        "4. Nghỉ sau khi thua: nghỉ bao nhiêu ván? "
        "(0 = Tắt)",
        default=0,
    )

    # ================== ĐẶT MỤC TIÊU ==================
    print("\n🎯 ĐẶT MỤC TIÊU", flush=True)

    pt_str = Prompt.ask(
        "5. Mục tiêu lãi: nhập số BUILD muốn đạt (0 = Tắt)"
    ).strip()

    if pt_str and pt_str != "0":
        try:
            profit_target = float(pt_str)
            stop_when_profit_reached = profit_target > 0
        except ValueError:
            profit_target = None
            stop_when_profit_reached = False

            print(
                "⚠️ Mục tiêu lãi không hợp lệ, đã tắt.",
                flush=True,
            )
    else:
        profit_target = None
        stop_when_profit_reached = False

    sl_str = Prompt.ask(
        "6. Cắt lỗ: số BUILD tối thiểu còn lại để dừng "
        "(0 = Tắt)"
    ).strip()

    if sl_str and sl_str != "0":
        try:
            stop_loss_target = float(sl_str)
            stop_when_loss_reached = stop_loss_target > 0
        except ValueError:
            stop_loss_target = None
            stop_when_loss_reached = False

            print(
                "⚠️ Cắt lỗ không hợp lệ, đã tắt.",
                flush=True,
            )
    else:
        stop_loss_target = None
        stop_when_loss_reached = False

    _vth_daily_pause_window = _prompt_daily_pause_window("VTH", "7")
    _reset_daily_pause_state("VTH")

    run_mode = "AUTO"

    return True

def load_accounts() -> list:
    acc_file = Path(ACCOUNTS_FILE)
    if not acc_file.exists():
        return []
    try:
        return json.loads(acc_file.read_text())
    except (json.JSONDecodeError, IOError):
        return []

def save_accounts(accounts: list):
    acc_file = Path(ACCOUNTS_FILE)
    with acc_file.open("w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)

def add_new_account(accounts: list) -> bool:
    while True:
        console.clear()

        print(
            "📌 DÁN LINK TÀI KHOẢN VUA THOÁT HIỂM\n\n"
            "Gửi link game có dạng chứa userId và secretKey.",
            flush=True,
        )

        link = Prompt.ask(
            "👉 Dán link game vào đây (Nhập 0 để huỷ)"
        ).strip()

        if link == "0":
            print("🛑 Đã huỷ.", flush=True)
            return False

        if not link:
            print(
                "⚠️ Vui lòng dán link hoặc nhập 0 để huỷ.",
                flush=True,
            )
            continue

        try:
            params = parse_qs(urlparse(link).query)

            uid_raw = params.get("userId", [""])[0]
            skey = params.get("secretKey", [""])[0].strip()

            if not uid_raw or not skey:
                raise ValueError

            uid = int(uid_raw)

            accounts.append({
                "userId": uid,
                "secretKey": skey,
            })

            save_accounts(accounts)

            print(
                f"✅ TÀI KHOẢN {uid} ĐÃ ĐƯỢC THÊM THÀNH CÔNG!\n"
                "🧠 Bây giờ hãy chọn thuật toán để cài cấu hình.",
                flush=True,
            )

            time.sleep(0.5)
            return True

        except (KeyError, IndexError):
            print(
                "❌ Link game không hợp lệ!",
                flush=True,
            )

        except ValueError:
            print(
                "❌ UserID hoặc SecretKey không hợp lệ!",
                flush=True,
            )

        except Exception as e:
            print(
                f"❌ Lỗi: {e}",
                flush=True,
            )

        time.sleep(0.5)

def start_threads():
    threading.Thread(target=start_ws, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

def _send_vth_start_config(balance=None):
    """Gửi một lần cấu hình VTH vừa cài kèm số dư BUILD thực tế."""
    try:
        algo_name = get_vth_algo_display_name(selected_vth_algo, is_vip_activated())
    except Exception:
        algo_name = get_vth_algo_display_name(selected_vth_algo, False)

    def _fmt_num(value):
        try:
            return f"{float(value):,.2f}"
        except Exception:
            return "0.00"

    balance_text = _fmt_num(balance) if balance is not None else "Không lấy được"
    config_lines = [
        "📜 TỔNG KẾT CẤU HÌNH VTH",
        "",
        f"👤 Tài khoản:{USER_ID}",
        f"💳 Số dư tài khoản: {balance_text} BUILD",
        f"🧠 Thuật toán: {algo_name}",
        ("💰 Cược mỗi ván: ALL IN — toàn bộ số dư BUILD" if str(selected_vth_algo).upper() == "ALL" else f"💰 Cược mỗi ván: {_fmt_num(base_bet)} BUILD"),
        f"📈 Hệ số sau thua: x{_fmt_num(multiplier)}",
        f"🛡️ Chống soi: {int(bet_rounds_before_skip or 0)} ván rồi nghỉ 1 ván" if int(bet_rounds_before_skip or 0) > 0 else "🛡️ Chống soi: Tắt",
        f"⏸️ Nghỉ sau thua: {int(pause_after_losses or 0)} ván" if int(pause_after_losses or 0) > 0 else "⏸️ Nghỉ sau thua: Tắt",
        f"🎯 Mục tiêu lãi: Dừng khi số dư đạt {_fmt_num(profit_target)} BUILD" if stop_when_profit_reached and profit_target else "🎯 Mục tiêu lãi: Tắt",
        f"🛑 Cắt lỗ: Dừng khi số dư còn {_fmt_num(stop_loss_target)} BUILD" if stop_when_loss_reached and stop_loss_target else "🛑 Cắt lỗ: Tắt",
        f"⏰ Khung giờ chạy: {_format_daily_pause_window(_vth_daily_pause_window)}",
    ]
    print("\n".join(config_lines), flush=True)
    print("🚀 ĐANG KHỞI ĐỘNG VTH...", flush=True)


def start_game_flow():
    global stop_flag, IS_VIP_USER, active_vth_poller, capital_stop_pending
    global starting_balance, cumulative_profit, last_balance_val
    if USER_ID is None or SECRET_KEY is None:
        print("❌ Chưa chọn tài khoản.")
        return
    _, _, IS_VIP_USER = check_activation_valid()
    capital_stop_pending = False

    global _sequential_bet_index, killed_room, round_index
    global predicted_room, last_killed_room, last_killed_room_delayed
    global prediction_locked, issue_id, count_down
    global win_streak, lose_streak, max_win_streak, max_lose_streak
    global _rounds_placed_since_skip, skip_next_round_flag, _skip_rounds_remaining
    global analysis_start_ts, analysis_ready_ts, issue_start_ts, issue_end_ts
    _sequential_bet_index = 0
    killer_history.clear()
    game_kill_log.clear()
    bet_history.clear()
    bet_sent_for_issue.clear()
    processed_result_issues.clear()
    history_recorded_issue.clear()
    bet_selection_by_issue.clear()
    killed_room = None
    round_index = 0
    predicted_room = None
    last_killed_room = None
    last_killed_room_delayed = None
    prediction_locked = False
    issue_id = None
    count_down = None
    win_streak = lose_streak = 0
    max_win_streak = max_lose_streak = 0
    _rounds_placed_since_skip = 0
    skip_next_round_flag = False
    _skip_rounds_remaining = 0
    analysis_start_ts = None
    analysis_ready_ts = None
    issue_start_ts = None
    issue_end_ts = None

    starting_balance = None
    cumulative_profit = 0.0
    last_balance_val = None

    try:
        initial_balance, _, _ = fetch_balances_3games(
            retries=2,
            timeout=5,
            params={"userId": str(USER_ID)},
            uid=USER_ID,
            secret=SECRET_KEY,
        )
        if initial_balance is not None:
            initial_balance = float(initial_balance)
            globals()["current_build"] = initial_balance

            starting_balance = initial_balance
            cumulative_profit = 0.0
    except Exception as e:
        initial_balance = None
        log_debug(f"Lỗi lấy số dư BUILD lúc khởi động VTH: {e}")

    try:
        globals()["current_bet"] = float(base_bet)
        globals()["last_display_bet"] = float(base_bet)
    except Exception:
        pass

    _send_vth_start_config(initial_balance)

    start_threads()
    poller = BalancePoller(USER_ID, SECRET_KEY, poll_seconds=max(1, int(BALANCE_POLL_INTERVAL)), on_balance=None, on_error=None, on_status=None)
    active_vth_poller = poller
    poller.start()
    last_status_issue = None
    try:
        while not stop_flag:
            try:

                current_issue = issue_id
                if current_issue is not None and current_issue != last_status_issue:
                    b = current_build if isinstance(current_build, (int, float)) else 0.0
                    bet = last_display_bet if isinstance(last_display_bet, (int, float)) else current_bet
                    last_status_issue = current_issue
            except Exception as e:
                log_debug(f"telegram text loop: {e}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_vth_runtime("Đã dừng tool.")
        try: poller.stop()
        except Exception: pass
        if active_vth_poller is poller:
            active_vth_poller = None

def main_vth():
    start_key_expiry_monitor()

    if not check_activation_valid()[0]:
        console.print("[bold red]❌ Key của bạn không còn hiệu lực.[/bold red]")
        time.sleep(2)
        return

    global stop_flag, USER_ID, SECRET_KEY
    stop_flag = False

    console.clear()
    save_accounts([])
    if not add_new_account([]):
        return

    accounts = load_accounts()
    if not accounts:
        return

    USER_ID = accounts[0]["userId"]
    SECRET_KEY = accounts[0]["secretKey"]

    console.clear()
    if not prompt_settings():
        return

    console.clear()
    start_game_flow()

# ================== TOOL 2: LOTTO ==================

ACCOUNTS_FILE = str(BOT_DATA_DIR / "accounts.json")
CONFIG_FILE = "wh_config.json"
API_URL = "https://api.winhash.net/lucky_game/hourly_issue_list"
HOME_URL = "https://api.winhash.net/lucky_game/home"
API_URL_GET_BALANCE = "https://wallet.3games.io/api/wallet/user_asset"
HOME_POLL_INTERVAL = 3
UI_REFRESH = 6
DECIMALS = 4

ICON_LOTTO = {"nho": "🔵", "lon": "🔴", "hoa": "⚖️", 1:"1", 2:"2", 3:"3", 4:"4", 5:"5", 6:"6"}
NUMBER_COLORS = {1:"bright_blue", 2:"bright_cyan", 3:"bright_green", 4:"yellow", 5:"bright_magenta", 6:"bright_red"}

_num_re_local = re.compile(r"-?\d+[\d,]*\.?\d*")

# ================== CLASS PHÂN TÍCH LOTTO ==================

class ThreeNumberAnalyzer:
    def __init__(self):
        self.position_stats = {0: defaultdict(lambda: Counter()), 1: defaultdict(lambda: Counter()), 2: defaultdict(lambda: Counter())}
        self.position_names = {0: "ĐẦU", 1: "GIỮA", 2: "CUỐI"}
        self.pattern_history = []
        self.last_seen_index = {0: {}, 1: {}, 2: {}}

    def _build_position_stats(self, history: List[Dict], decay: float = 0.06):
        self.pattern_history = []
        for p in [0,1,2]:
            self.position_stats[p] = defaultdict(lambda: Counter())
            self.last_seen_index[p] = {}
        n = max(1, len(history) - 1)
        for i in range(len(history) - 1):
            current = history[i].get('lucky_codes', [])
            next_codes = history[i + 1].get('lucky_codes', [])
            if len(current) == 3 and len(next_codes) == 3:
                distance = (n - 1) - i
                weight = float(math.exp(-decay * max(0, distance)))
                for pos in [0, 1, 2]:
                    trigger_num = current[pos]
                    result_num = next_codes[pos]
                    self.position_stats[pos][trigger_num][result_num] += weight
                self.pattern_history.append({'current': tuple(current), 'next': tuple(next_codes), 'current_sum': sum(current), 'next_sum': sum(next_codes)})
        for pos in [0,1,2]:
            seen = {}
            for dist, rec in enumerate(reversed(history)):
                codes = rec.get('lucky_codes', [])
                if not isinstance(codes, list) or len(codes) != 3:
                    continue
                num = codes[pos]
                if num not in seen:
                    seen[num] = dist
            self.last_seen_index[pos] = seen

    def _predict_position(self, position: int, current_num: int, context: Optional[Tuple[int,int,int]] = None) -> Dict:
        stats = self.position_stats[position][current_num]
        total = float(sum(stats.values()))
        if total == 0:
            return {i: 1/6 for i in range(1, 7)}
        probs = {i: stats.get(i, 0) / total for i in range(1, 7)}
        alpha = 0.5
        last_seen = self.last_seen_index.get(position, {})
        recency_boost = {}
        for i in range(1,7):
            dist = last_seen.get(i)
            if dist is None:
                recency_boost[i] = 1.0
            else:
                recency_boost[i] = 1.0 + max(0.0, (6.0 - float(dist)) / 12.0)
        smoothed = {}
        denom = total + 6 * alpha
        for i in range(1,7):
            base = (stats.get(i, 0) + alpha) / denom
            smoothed[i] = base * recency_boost.get(i, 1.0)
        s_sum = sum(smoothed.values())
        if s_sum <= 0:
            return {i: 1/6 for i in range(1, 7)}
        base_probs = {i: smoothed[i] / s_sum for i in range(1,7)}
        if context and self.pattern_history:
            boost = defaultdict(float)
            total_ctx = 0.0
            for idx, ph in enumerate(reversed(self.pattern_history)):
                cur = ph.get('current')
                nxt = ph.get('next')
                if not cur or not nxt: continue
                match = 0
                for p in range(3):
                    if p == position: continue
                    if context[p] == cur[p]:
                        match += 1
                if match >= 1:
                    recency_w = math.exp(-0.06 * idx)
                    observed = nxt[position]
                    boost[observed] += recency_w * (1.0 + match * 0.25)
                    total_ctx += recency_w
            if total_ctx > 0:
                ctx_probs = {i: (boost.get(i, 0.0) / total_ctx) for i in range(1,7)}
                mix = 0.25
                blended = {i: base_probs.get(i,0.0) * (1.0 - mix) + ctx_probs.get(i,0.0) * mix for i in range(1,7)}
                s = sum(blended.values())
                if s > 0:
                    return {i: blended[i]/s for i in range(1,7)}
        return base_probs

    def analyze(self, history: List[Dict]) -> Dict:
        if len(history) < 2:
            return {"last_three": [], "position_probs": [{}, {}, {}], "predicted_numbers": [], "predicted_sum": 0, "prediction": None, "confidence": 0.0, "bet_action": "small"}
        self._build_position_stats(history)
        last_record = history[-1]
        last_three = last_record.get('lucky_codes', [])
        if len(last_three) != 3:
            return {"last_three": last_three, "position_probs": [{}, {}, {}], "predicted_numbers": [], "predicted_sum": 0, "prediction": None, "confidence": 0.0, "bet_action": "small"}
        position_probs = []
        predicted_numbers = []
        total_confidence = 0.0
        per_number_details = []
        for pos in [0, 1, 2]:
            current_num = last_three[pos]
            probs = self._predict_position(pos, current_num, context=last_three)
            details = {}
            counts = self.position_stats[pos]
            sample_size = float(sum(counts.get(current_num, Counter()).values())) if counts.get(current_num) else 0.0
            for num in range(1,7):
                prob = probs.get(num, 0.0)
                dist = self.last_seen_index.get(pos, {}).get(num)
                recency_score = 0.0 if dist is None else max(0.0, (12.0 - float(dist)) / 12.0)
                freq = 0
                for k,v in self.position_stats[pos].items():
                    freq += v.get(num, 0)
                freq_score = float(freq) / max(1.0, sum(sum(c.values()) for c in self.position_stats[pos].values()))
                score = prob * 0.6 + recency_score * 0.25 + freq_score * 0.15
                details[num] = {'prob': prob, 'recency_score': recency_score, 'freq': freq, 'freq_score': freq_score, 'score': score}
            position_probs.append(probs)
            best_num = max(details.items(), key=lambda x: x[1]['score'])[0]
            predicted_numbers.append(best_num)
            total_confidence += details[best_num]['prob']
            per_number_details.append({'position': pos, 'current': current_num, 'sample_size': sample_size, 'details': details})
        predicted_sum = sum(predicted_numbers)
        avg_confidence = total_confidence / 3
        if 3 <= predicted_sum <= 9:
            bet_action = "nho"
            prediction = "Nho"
        elif 10 <= predicted_sum <= 11:
            bet_action = "hoa"
            prediction = "Hoa"
        elif 12 <= predicted_sum <= 18:
            bet_action = "lon"
            prediction = "Lon"
        else:
            bet_action = "nho"
            prediction = "Nho"
        sample_sizes = []
        for pos in [0, 1, 2]:
            current_num = last_three[pos]
            stats = self.position_stats[pos][current_num]
            sample_sizes.append(sum(stats.values()))
        avg_sample = sum(sample_sizes) / 3 if sample_sizes else 0
        try:
            ent = 0.0
            for probs in position_probs:
                s = 0.0
                for p in probs.values():
                    if p > 0: s -= p * math.log(p)
                ent += s
            avg_entropy = ent / 3.0 if position_probs else 0.0
            entropy_penalty = avg_entropy / max(1.0, math.log(6))
            avg_confidence *= max(0.25, 1.0 - 0.5 * entropy_penalty)
        except Exception:
            pass
        if avg_sample < 3:
            avg_confidence *= 0.55
        elif avg_sample < 6:
            avg_confidence *= 0.8
        return {"last_three": last_three, "position_probs": position_probs, "predicted_numbers": predicted_numbers, "predicted_sum": predicted_sum, "prediction": prediction, "confidence": avg_confidence, "sample_sizes": sample_sizes, "bet_action": bet_action, "per_position_details": per_number_details}

class AccountManager:
    def __init__(self, path: Optional[str] = None):
        # Resolve path at runtime: Telegram worker dùng file riêng theo chat_id.
        self.path = path or ACCOUNTS_FILE
        self.accounts = self._load()

    def _load(self) -> List[Dict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)

    def parse_link_to_account(self, link: str) -> Optional[Dict]:
        try:
            p = urlparse(link)
            qs = parse_qs(p.query)

            user_id = qs.get("userId", [None])[0]
            secret = qs.get("secretKey", [None])[0]

            if not user_id or not secret:
                return None

            return {
                "user-id": user_id,
                "user-secret-key": secret,
                "country-code": "vn",
                "user-agent": "Mozilla/5.0",
                "asset": "BUILD",
            }

        except Exception:
            return None

    def add_account(self) -> Dict:
        while True:
            raw = Prompt.ask("👉 Dán link game")

            if raw.strip() == "":
                raise KeyboardInterrupt

            # Chỉ chấp nhận link có userId và secretKey
            if (
                raw.startswith("http")
                and "userId=" in raw
                and "secretKey=" in raw
            ):
                acc = self.parse_link_to_account(raw)

                if acc:
                    self.accounts.clear()
                    self.accounts.append(acc)
                    self.save()

                    if True:
                        print(f"✅ Đăng nhập thành công\n👤 userId={acc['user-id']}", flush=True)
                    else:
                        console.print(
                            Panel(
                                f"✅ Đăng nhập thành công\nUser ID: {acc['user-id']}",
                                style="bold green",
                                border_style="green",
                            )
                        )

                    return acc

            if True:
                print("❌ Link không hợp lệ! Hãy gửi lại link game có userId và secretKey.", flush=True)
            else:
                console.print("[bold red]❌ Link không hợp lệ![/bold red]")

    def remove_account(self, index: int) -> bool:
        if 0 <= index < len(self.accounts):
            self.accounts.pop(index)
            self.save()
            return True
        return False

class MartingaleBetting:

    def __init__(self, initial_bet: float, multiplier: float, asset: str):
        self.initial_bet = float(initial_bet)
        self.multiplier = float(multiplier)
        self.asset = asset
        self.current_bet = float(initial_bet)
        self.total_wagered = 0.0
        self.total_won = 0.0
        self.win_count = 0
        self.loss_count = 0
        self.current_win_streak = 0
        self.current_loss_streak = 0
        self.max_win_streak = 0
        self.max_loss_streak = 0
        self.net_profit = 0.0

    def get_bet_amount(self) -> float:
        return float(self.current_bet)

    def on_win(self, payout: float):
        try:
            self.total_won += float(payout)
        except Exception:
            pass
        self.win_count += 1
        self.current_bet = float(self.initial_bet)
        self.current_win_streak += 1
        self.current_loss_streak = 0
        if self.current_win_streak > self.max_win_streak:
            self.max_win_streak = self.current_win_streak

    def on_loss(self):
        self.loss_count += 1
        self.current_bet = float(self.current_bet) * float(self.multiplier)
        self.current_loss_streak += 1
        self.current_win_streak = 0
        if self.current_loss_streak > self.max_loss_streak:
            self.max_loss_streak = self.current_loss_streak

    def record_wager(self, amount: float):
        try:
            self.total_wagered += float(amount)
        except Exception:
            pass

    def get_stats(self) -> Dict:
        net_profit = self.total_won - self.total_wagered
        total_bets = self.win_count + self.loss_count
        return {"total_bets": total_bets, "wins": self.win_count, "losses": self.loss_count, "total_wagered": self.total_wagered, "total_won": self.total_won, "net_profit": net_profit, "current_bet": self.current_bet, "asset": self.asset, "current_win_streak": self.current_win_streak, "current_loss_streak": self.current_loss_streak, "max_win_streak": self.max_win_streak, "max_loss_streak": self.max_loss_streak}

def build_headers(config: Dict[str, str]) -> Dict[str, str]:
    return {'accept': '*/*', 'accept-language': 'en-US,en;q=0.9', 'country-code': config.get('country-code', 'vn'), 'origin': 'https://winhash.io', 'referer': 'https://winhash.io/', 'user-agent': config.get('user-agent', 'Mozilla/5.0'), 'user-id': config['user-id'], 'user-login': 'login_v2', 'user-secret-key': config['user-secret-key'], 'xb-language': config.get('xb-language', 'en-US')}

def fetch_issue_list(session: requests.Session, headers: Dict[str, str], ts: int) -> List[Dict]:
    params = {'ts': str(ts)}
    try:
        resp = session.get(API_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get('code') is not None and data.get('code') != 0:
            return []
        return data.get('data', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception:
        return []

def fetch_home(session: requests.Session, headers: Dict[str, str], asset: str) -> Optional[Dict]:
    params = {'game_id': '1', 'asset': asset}
    try:
        resp = session.get(HOME_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get('code') is not None and data.get('code') != 0:
            return None
        return data.get('data') if isinstance(data, dict) else None
    except Exception:
        return None

def get_balance(s: requests.Session, headers: Dict[str, str], uid: str, key: str) -> Dict[str, Dict[str, float]]:
    h = headers.copy()
    h["user-id"] = uid
    h["user-secret-key"] = key

    try:
        payload = {
            "user_id": int(uid) if str(uid).isdigit() else uid,
            "source": "home"
        }

        r = s.post(
            API_URL_GET_BALANCE,
            headers=h,
            json=payload,
            timeout=10
        )
        r.raise_for_status()

        d = r.json()

        if isinstance(d, dict) and d.get("code") == 0:

            data = d.get("data") or {}
            ua = data.get("user_asset") or data.get("assets") or data

            build = world = usdt = None

            if isinstance(ua, list):
                for item in ua:
                    asset = str(item.get("asset", "")).upper()
                    bal = _safe_round(
                        item.get("balance")
                        or item.get("amount")
                        or 0
                    )

                    if asset == "BUILD":
                        build = bal
                    elif asset in ("WORLD", "XWORLD"):
                        world = bal
                    elif asset == "USDT":
                        usdt = bal

            elif isinstance(ua, dict):
                for k, v in ua.items():

                    name = str(k).upper()

                    if isinstance(v, dict):
                        bal = _safe_round(
                            v.get("balance")
                            or v.get("amount")
                            or 0
                        )
                    else:
                        bal = _safe_round(v)

                    if name == "BUILD":
                        build = bal
                    elif name in ("WORLD", "XWORLD"):
                        world = bal
                    elif name == "USDT":
                        usdt = bal

            build = 0.0 if build is None else build
            world = 0.0 if world is None else world
            usdt = 0.0 if usdt is None else usdt

            return {
                "BUILD": {"balance": build},
                "XWORLD": {"balance": world},
                "USDT": {"balance": usdt},
            }

    except Exception as e:
        logger.debug(f"get_balance error: {e}")

    return {
        "BUILD": {"balance": 0.0},
        "XWORLD": {"balance": 0.0},
        "USDT": {"balance": 0.0},
    }

def _safe_round(val: Any, ndigits: int = DECIMALS) -> float:
    try:
        return round(float(val), ndigits)
    except Exception:
        return 0.0

def _parse_number_local(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x)
    m = _num_re_local.search(s)
    if not m:
        return None
    token = m.group(0).replace(",", "")
    try:
        return float(token)
    except Exception:
        return None

def sum_to_label(total: int) -> str:
    try:
        total = int(total)
    except Exception:
        return "unknown"
    if 3 <= total <= 9:
        return "nho"
    if 10 <= total <= 11:
        return "hoa"
    if 12 <= total <= 18:
        return "lon"
    return "unknown"

def get_balances_from_home(home: Dict) -> Dict[str, Optional[float]]:
    build = None
    world = None
    usdt = None
    if not isinstance(home, dict):
        return {"BUILD": None, "XWORLD": None, "USDT": None}
    ua = home.get('user_asset') or home.get('wallet') or home.get('assets') or {}
    if isinstance(ua, dict):
        if 'BUILD' in ua:
            build = _parse_number_local(ua.get('BUILD'))
        if 'WORLD' in ua:
            world = _parse_number_local(ua.get('WORLD'))
        if 'USDT' in ua:
            usdt = _parse_number_local(ua.get('USDT'))
    for k in ('build','ctoken','ctoken_contribute','balance','amount'):
        if build is None and k in home:
            build = _parse_number_local(home.get(k))
    for k in ('world','xworld','WORLD','XWORLD'):
        if world is None and k in home:
            world = _parse_number_local(home.get(k))
    for k in ('usdt','kusdt','USDT'):
        if usdt is None and k in home:
            usdt = _parse_number_local(home.get(k))
    if build is None or world is None or usdt is None:
        try:
            s = json.dumps(home)
            if build is None:
                m = re.search(r'(?:BUILD|build)[:\"\']*\s*([0-9\.,]+)', s, re.I)
                if m: build = _parse_number_local(m.group(1))
            if world is None:
                m = re.search(r'(?:WORLD|world|xworld)[:\"\']*\s*([0-9\.,]+)', s, re.I)
                if m: world = _parse_number_local(m.group(1))
            if usdt is None:
                m = re.search(r'(?:USDT|usdt|kusdt)[:\"\']*\s*([0-9\.,]+)', s, re.I)
                if m: usdt = _parse_number_local(m.group(1))
        except Exception:
            pass
    return {"BUILD": build, "XWORLD": world, "USDT": usdt}

def get_hour_start_timestamp(hour_offset: int = 0):
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_start - timedelta(hours=hour_offset)
    return int(hour_start.timestamp())

def compute_number_counts(history: List[Dict]) -> Dict[str, Any]:
    overall = Counter()
    per_pos = {0: Counter(), 1: Counter(), 2: Counter()}
    for rec in history:
        codes = rec.get('lucky_codes') or []
        if not isinstance(codes, list) or len(codes) != 3:
            continue
        for pos, num in enumerate(codes):
            per_pos[pos][num] += 1
            overall[num] += 1
    return {'overall': overall, 'per_pos': per_pos}

def issue_to_record(issue: Dict) -> Optional[Dict]:
    lucky_codes = issue.get('lucky_codes') or []
    if len(lucky_codes) != 3:
        return None
    total = sum(lucky_codes)
    return {'issue_id': issue.get('issue_id'), 'lucky_codes': lucky_codes, 'sum': total, 'result': sum_to_label(total)}

def home_to_record(home: Dict) -> Optional[Dict]:
    if not isinstance(home, dict):
        return None
    raw_issue_id = home.get('last_issue_id')
    try:
        last_issue_id = int(raw_issue_id) if raw_issue_id is not None else None
    except (TypeError, ValueError):
        return None

    raw_codes = home.get('last_issue_lucky_code') or []
    if last_issue_id is None or not isinstance(raw_codes, (list, tuple)) or len(raw_codes) != 3:
        return None
    try:
        lucky_codes = [int(x) for x in raw_codes]
    except (TypeError, ValueError):
        return None
    if any(x < 1 or x > 6 for x in lucky_codes):
        return None

    total = sum(lucky_codes)
    return {'issue_id': last_issue_id, 'lucky_codes': lucky_codes, 'sum': total, 'result': sum_to_label(total)}

def place_bet(session: requests.Session, headers: Dict[str, str], issue_id: int, bet_type: str, amount: float, asset: str) -> tuple:
    bet_ids = {'nho': 70309, 'lon': 71218, 'hoa': 71011}
    if bet_type not in bet_ids:
        return False, "Invalid bet type"
    payload = {"game_id": 1, "issue_id": issue_id, "items": [{"id": bet_ids[bet_type], "amount": str(amount), "asset": asset}]}
    try:
        resp = session.post('https://api.winhash.net/lucky_game/v2/create_order', headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            success = data.get('code') == 0
            error_msg = data.get('msg', 'Unknown error')
            return success, error_msg if not success else "Success"
        else:
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def make_vbtool_compact_panel(state: Dict, tick: int = 0) -> Panel:
    balances = state.get('balances') or {}
    def _fmt_bal(b):
        try:
            if b is None: return '—'
            if isinstance(b, dict): return f"{_safe_round(b.get('balance',0)):.2f}"
            return f"{_safe_round(b):.2f}"
        except: return '—'
    b_build = _fmt_bal(balances.get('BUILD'))
    b_x = _fmt_bal(balances.get('XWORLD'))
    last_issue = state.get('last_issue', '—')
    last_result = state.get('last_result', '—')
    next_pred = state.get('next_prediction', '—')
    conf = state.get('next_confidence', 0.0)
    analysis = state.get('three_number_analysis', {})
    last_three = analysis.get('last_three', [])
    predicted_numbers = analysis.get('predicted_numbers', [])
    predicted_sum = analysis.get('predicted_sum', 0)
    start_bal = None
    if isinstance(state.get('start_balances'), dict):
        try:
            start_bal = float(state['start_balances'].get('BUILD', {}).get('balance'))
        except: pass
    cur_bal = None
    try:
        b = balances.get('BUILD')
        if isinstance(b, dict): cur_bal = b.get('balance')
        else: cur_bal = float(b) if b else None
    except: pass
    model_stats = state.get('model_stats', {})
    wins = model_stats.get('wins', 0)
    losses = model_stats.get('losses', 0)
    total_pred = model_stats.get('predictions', 0)
    win_rate = (wins / total_pred * 100) if total_pred > 0 else 0.0

    t = Text()
    t.append(f"{ICONS['user']} User: {state.get('account_id', '—')}", style=VBTOOL_COLORS["history_blue"])
    t.append("  |  ", style="dim")
    t.append(f"{ICONS['diamond']} BUILD: {b_build}", style=f"bold {VBTOOL_COLORS['emerald']}")
    t.append("  |  ", style="dim")
    t.append(f"🌍 XW: {b_x}", style=VBTOOL_COLORS["history_blue"])
    t.append("\n" + "━" * 68 + "\n", style=VBTOOL_COLORS["gold"])
    t.append(f"📊 Kết quả #{last_issue}: {last_result}", style=VBTOOL_COLORS["platinum"])
    t.append(f"  |  Tỷ lệ: ", style="dim")
    t.append(f"{win_rate:.0f}%", style=f"bold {VBTOOL_COLORS['history_blue']}")
    if start_bal is not None and cur_bal is not None:
        diff = _safe_round(cur_bal - start_bal, DECIMALS)
        pct = (diff / start_bal * 100.0) if (start_bal and start_bal != 0) else 0.0
        sign = "+" if diff > 0 else ""
        if diff > 0: arrow, style = "🚀", f"bold {VBTOOL_COLORS['emerald']}"
        elif diff < 0: arrow, style = "💔", f"bold {VBTOOL_COLORS['ruby']}"
        else: arrow, style = "➡️", VBTOOL_COLORS["gold"]
        t.append("  |  ", style="dim")
        t.append(f"{arrow} {sign}{diff:.2f} ({sign}{pct:.1f}%)", style=style)
    t.append("\n" + "─" * 68 + "\n", style="dim")
    t.append(f"{ICONS['dice']} Hiện tại: ", style="dim")
    for num in last_three:
        icon = ICON_LOTTO.get(num, str(num))
        color = NUMBER_COLORS.get(num, "white")
        t.append(f"{icon} ", style=f"bold {color}")
    t.append("  →  ", style="dim")
    t.append("Dự đoán: ", style="dim")
    for num in predicted_numbers:
        icon = ICON_LOTTO.get(num, str(num))
        color = NUMBER_COLORS.get(num, "white")
        t.append(f"{icon} ", style=f"bold {color}")
    t.append(f"(Σ={predicted_sum})", style=VBTOOL_COLORS["gold"])
    t.append("\n")
    t.append(f"{ICONS['target']} Cược tiếp: ", style="dim")
    t.append(f"{next_pred}", style=f"bold {VBTOOL_COLORS['neon_pink']}")
    if conf:
        conf_bar = int(conf * 12)
        conf_style = VBTOOL_COLORS["emerald"] if conf >= 0.7 else (VBTOOL_COLORS["gold"] if conf >= 0.5 else VBTOOL_COLORS["ruby"])
        t.append("  [", style="dim")
        t.append("█" * conf_bar, style=conf_style)
        t.append("░" * (12 - conf_bar), style="dim")
        t.append(f" {conf*100:.0f}%]", style=conf_style)
    hist = state.get('history', []) or []
    counts = compute_number_counts(hist) if hist else {'overall': Counter(), 'per_pos': {0: Counter(), 1: Counter(), 2: Counter()}}
    overall = counts.get('overall', {})
    if overall:
        t.append("\n", "")
        t.append("🔥❄️ Hot/Cold: ", style="dim")
        maxc = max(overall.values()) if overall else 1
        for n in range(1,7):
            c = overall.get(n, 0)
            color = NUMBER_COLORS.get(n, 'white')
            bl = int((c / maxc) * 6) if maxc else 0
            bars = '█' * bl + ' ' * (6 - bl)
            t.append(f" {ICON_LOTTO.get(n)}", style=f"bold {color}")
            t.append(bars, style=color)
        t.append("\n", "")
    t.append(f"{ICONS['chart']} Tổng: ", style="dim")
    t.append(f"{total_pred} ván", style=VBTOOL_COLORS["platinum"])
    t.append("  |  🏆 Win ", style="dim")
    t.append(f"{wins}", style=VBTOOL_COLORS["emerald"])
    t.append("  💀 Lose ", style="dim")
    t.append(f"{losses}", style=VBTOOL_COLORS["ruby"])
    bet_stats = state.get('bet_stats')
    if bet_stats:
        win_streak = bet_stats.get('current_win_streak', 0)
        loss_streak = bet_stats.get('current_loss_streak', 0)
        if win_streak > 0:
            t.append("\n", "")
            t.append(f"{ICONS['fire']} Chuỗi thắng: ", style=VBTOOL_COLORS["emerald"])
            t.append(f"{win_streak}", style=f"bold {VBTOOL_COLORS['emerald']}")
            if win_streak >= 3:
                t.append(" 🏆", style=VBTOOL_COLORS["gold"])
        if loss_streak > 0:
            t.append("\n", "")
            t.append(f"💀 Chuỗi thua: ", style=VBTOOL_COLORS["ruby"])
            t.append(f"{loss_streak}", style=f"bold {VBTOOL_COLORS['ruby']}")
            if loss_streak >= 3:
                t.append(" ⚠️", style=VBTOOL_COLORS["gold"])
        t.append("\n", "")
        total_wagered = bet_stats.get('total_wagered', 0)
        if start_bal is not None and cur_bal is not None:
            net_profit = _safe_round(cur_bal - start_bal, DECIMALS)
        else:
            net_profit = bet_stats.get('net_profit', 0)
        asset = bet_stats.get('asset', 'BUILD')
        t.append(f"{ICONS['money']} Đã cược: ", style="dim")
        t.append(f"{total_wagered:.2f} {asset}", style=VBTOOL_COLORS["gold"])
        profit_style = f"bold {VBTOOL_COLORS['emerald']}" if net_profit > 0 else (f"bold {VBTOOL_COLORS['ruby']}" if net_profit < 0 else VBTOOL_COLORS["gold"])
        t.append(f"  |  {ICONS['chart']} Lãi/Lỗ: ", style="dim")
        t.append(f"{net_profit:+.2f} {asset}", style=profit_style)
    log_text = state.get('log_text', '—')
    t.append("\n" + "─" * 68 + "\n", style="dim")
    t.append(log_text, style=VBTOOL_COLORS["platinum"])
    t.append("\n", "")
    return Panel(t, border_style=VBTOOL_COLORS["bright_cyan"], box=box.SIMPLE_HEAVY, padding=(0, 1))

def make_vbtool_history_cards(history: List[Dict], limit: int = 5, cols: int = 1, tick: int = 0, highlight_issue: Optional[int] = None) -> Panel:
    recent = list(reversed(history[-limit:]))
    cards = []
    for rec in recent:
        issue = rec.get('issue_id')
        ts = rec.get('timestamp', '')
        short_ts = ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else ''
        codes = rec.get('lucky_codes', [])
        code_line = Text()
        for i, n in enumerate(codes):
            color = NUMBER_COLORS.get(n, 'white')
            code_line.append(f" {ICON_LOTTO.get(n)} ", style=f"bold {color}")
            if i < len(codes)-1:
                code_line.append(' ', style='dim')
        kq = rec.get('result', '—')
        if kq == 'nho':
            kq_style = f"bold white on {VBTOOL_COLORS['sapphire']}"
            kq_icon = '🔵'
        elif kq == 'lon':
            kq_style = f"bold white on {VBTOOL_COLORS['ruby']}"
            kq_icon = '🔴'
        else:
            kq_style = f"bold black on {VBTOOL_COLORS['gold']}"
            kq_icon = '⚖️'
        bet = rec.get('bet') or {}
        pl = None
        try:
            bb = bet.get('balance_before')
            ba = bet.get('balance_after')
            if bb is not None and ba is not None:
                pl = float(ba) - float(bb)
            else:
                pl = bet.get('profit')
        except Exception:
            pl = bet.get('profit') if bet else None
        if pl is None:
            pl_text = Text('—', style='dim')
        elif pl > 0:
            pl_text = Text(f'+{pl:.2f}', style=f'bold {VBTOOL_COLORS["emerald"]}')
        elif pl < 0:
            pl_text = Text(f'{pl:.2f}', style=f'bold {VBTOOL_COLORS["ruby"]}')
        else:
            pl_text = Text(f'{pl:.2f}', style=VBTOOL_COLORS["gold"])
        bs = VBTOOL_COLORS["onyx"]
        if highlight_issue is not None and issue == highlight_issue:
            bs = VBTOOL_COLORS["gold"]
        card = Panel(Align.left(Text.assemble(Text(f"#{str(issue)[-4:]} ", style=f'bold {VBTOOL_COLORS["history_blue"]}'), Text(short_ts + '\n', style='dim'), code_line, Text('\n', ''), Text.assemble(Text(kq_icon + ' ', style=kq_style), Text(' ', ''), pl_text))), box=box.ROUNDED, padding=(0, 1), border_style=bs)
        cards.append(card)
    if not cards:
        return Panel(Text('Không có lịch sử', style='dim'), border_style=VBTOOL_COLORS["onyx"])
    return Panel(Columns(cards, equal=True, expand=True), title=f"LỊCH SỬ GẦN ĐÂY", border_style=VBTOOL_COLORS["history_blue"], box=box.ROUNDED)

def make_vbtool_layout(state: Dict) -> Layout:
    tick = state.get("tick", 0)

    layout = Layout(name="root")
    layout.split_column(
        Layout(name="main", ratio=1),
        Layout(name="history", size=12),
    )

    layout["main"].update(
        make_vbtool_compact_panel(state, tick)
    )

    layout["history"].update(
        make_vbtool_history_cards(
            state.get("history", []),
            limit=5,
            cols=1,
            tick=tick,
            highlight_issue=state.get("last_issue"),
        )
    )

    return layout   

def main_lotto():
    start_key_expiry_monitor()

    print(
        "📋 Gửi link game có userId và secretKey.\n"
        "Ví dụ: link game chứa ?userId=...&secretKey=...",
        flush=True,
    )

    if not check_activation_valid()[0]:
        print("❌ Key của bạn không còn hiệu lực.", flush=True)
        time.sleep(2)
        return

    console.clear()
    am = AccountManager()

    console.print(
        Panel(
            "📋 Vui lòng dán link game để đăng nhập",
            title="LOTTO",
            border_style=VBTOOL_COLORS["history_blue"],
            box=box.ROUNDED,
        )
    )

    try:
        selected_account = am.add_account()
    except KeyboardInterrupt:
        return

    if not selected_account:
        print("❌ Không thêm được tài khoản!", flush=True)
        return

    uid = (
        selected_account.get("user-id")
        or selected_account.get("userId")
        or selected_account.get("uid")
    )

    secret = (
        selected_account.get("user-secret-key")
        or selected_account.get("secretKey")
        or selected_account.get("user_secret_key")
    )

    if not uid:
        print("❌ Không tìm thấy User ID!", flush=True)
        return

    if not secret:
        print("❌ Không tìm thấy Secret Key!", flush=True)
        return

    config = {
        "user-id": uid,
        "user-secret-key": secret,
        "country-code": selected_account.get("country-code", "vn"),
        "user-agent": selected_account.get("user-agent", "Mozilla/5.0"),
        "asset": selected_account.get("asset", "BUILD"),
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    headers = build_headers(config)
    session = requests.Session()
    asset = config.get("asset", "BUILD")

    while True:
        try:
            bet_per_round = float(
                Prompt.ask(
                    f"[bold {VBTOOL_COLORS['white']}]1. Cược mỗi ván bao nhiêu BUILD?[/bold {VBTOOL_COLORS['white']}]"
                )
            )
            break
        except ValueError:
            print("❌ Vui lòng nhập số hợp lệ!", flush=True)

    while True:
        try:
            multiplier = float(
                Prompt.ask(
                    f"[bold {VBTOOL_COLORS['white']}]2. Nhập số nhân sau khi thua[/bold {VBTOOL_COLORS['white']}]"
                )
            )
            break
        except ValueError:
            print("❌ Vui lòng nhập số hợp lệ!", flush=True)

    while True:
        answer = Prompt.ask(
            f"[bold {VBTOOL_COLORS['white']}]3. Bật auto cược? (y/n)[/bold {VBTOOL_COLORS['white']}]"
        ).strip().lower()

        if answer in ("y", "yes"):
            enable_betting = True
            break

        if answer in ("n", "no"):
            enable_betting = False
            break

        print("❌ Vui lòng nhập y hoặc n!", flush=True)

    betting_system = (
        MartingaleBetting(bet_per_round, multiplier, asset)
        if enable_betting
        else None
    )

    current_ts = get_hour_start_timestamp(0)
    previous_ts = get_hour_start_timestamp(1)
    issues = {}

    for issue in (
        fetch_issue_list(session, headers, previous_ts)
        + fetch_issue_list(session, headers, current_ts)
    ):
        if issue.get("issue_id"):
            issues[issue["issue_id"]] = issue

    history = [
        issue_to_record(i)
        for i in sorted(
            issues.values(),
            key=lambda x: x["issue_id"],
        )
        if issue_to_record(i)
    ]

    if not history:
        print(
            "❌ Không có lịch sử. Kiểm tra kết nối/config.",
            flush=True,
        )
        return

    three_number_analyzer = ThreeNumberAnalyzer()
    last_processed = max(h["issue_id"] for h in history)
    pending = {}
    pending_bets = {}
    processed_issue_ids = set()
    stop = False

    state = {
        "account_id": selected_account.get("user-id") if selected_account else None,
        "balances": {
            "BUILD": None,
            "XWORLD": None,
            "USDT": None,
        },
        "start_balances": {},
        "last_result": "—",
        "last_issue": "—",
        "next_prediction": "—",
        "next_confidence": None,
        "planned_bet": 0.0,
        "history": history,
        "three_number_analysis": three_number_analyzer.analyze(history),
        "bet_stats": None,
        "model_stats": {
            "predictions": 0,
            "wins": 0,
            "losses": 0,
            "current_streak": 0,
        },
        "log_text": "Sẵn sàng ✅",
        "tick": 0,
        "asset": asset,
    }

    console.clear()

    print(
        "🚀 LOTTO ĐANG CHẠY\n"
        f"👤 userId={uid}\n"
        f"💳 Loại tiền: {asset}\n"
        f"💰 Cược mỗi ván: {bet_per_round:.4f} {asset}\n"
        f"📈 Hệ số sau thua: x{multiplier:.2f}\n"
        f"🤖 Auto cược: {'BẬT' if enable_betting else 'TẮT'}\n"
        "⏳ Đang chờ dữ liệu ván mới...",
        flush=True,
    )

    with Live(
        make_vbtool_layout(state),
        console=console,
        refresh_per_second=UI_REFRESH,
    ) as live:
        loop_error_count = 0

        while not stop:
            try:
                if not check_activation_valid()[0]:
                    stop = True
                    break

                state["tick"] = (
                    state.get("tick", 0) + 1
                ) % 1000000

                try:
                    uid = selected_account.get("user-id")
                    key = selected_account.get("user-secret-key")

                    if uid and key:
                        bmap = get_balance(
                            session,
                            headers,
                            uid,
                            key,
                        )

                        if isinstance(bmap, dict):
                            state["balances"] = bmap

                            if not state.get("start_balances"):
                                state["start_balances"] = {
                                    k: {
                                        "balance": (
                                            float(v.get("balance", 0))
                                            if isinstance(v, dict)
                                            else _safe_round(v)
                                        )
                                    }
                                    for k, v in bmap.items()
                                }
                                state["log_text"] = "Đã lưu số dư khởi điểm"

                except Exception:
                    logger.debug("Wallet API read failed")

                home_data = fetch_home(
                    session,
                    headers,
                    asset,
                )

                if not home_data:
                    state["log_text"] = "Chờ dữ liệu..."

                    try:
                        live.update(make_vbtool_layout(state))
                    except Exception:
                        pass

                    try:
                        time.sleep(HOME_POLL_INTERVAL)
                    except KeyboardInterrupt:
                        return

                    continue

                try:
                    if state["balances"] and all(
                        _safe_round(
                            state["balances"]
                            .get(k, {})
                            .get("balance", 0)
                        ) == 0.0
                        for k in (
                            "BUILD",
                            "XWORLD",
                            "USDT",
                        )
                    ):
                        hb = get_balances_from_home(home_data)

                        for k in (
                            "BUILD",
                            "XWORLD",
                            "USDT",
                        ):
                            v = hb.get(k)

                            if v is not None:
                                state["balances"][k] = {
                                    "balance": _safe_round(v)
                                }

                        if (
                            state["balances"]
                            and not state.get("start_balances")
                        ):
                            state["start_balances"] = {
                                k: {
                                    "balance": (
                                        float(v.get("balance", 0))
                                        if isinstance(v, dict)
                                        else _safe_round(v)
                                    )
                                }
                                for k, v in state["balances"].items()
                            }

                except Exception:
                    pass

                last_id = home_data.get("last_issue_id")

                if last_id and last_id > last_processed:
                    record = home_to_record(home_data)

                    if record:
                        last_processed = record["issue_id"]
                        actual = record["result"]
                        decision = pending.pop(
                            record["issue_id"],
                            None,
                        )
                        bet_info = pending_bets.pop(
                            record["issue_id"],
                            None,
                        )

                        state["last_issue"] = record["issue_id"]
                        state["last_result"] = (
                            f"{actual.upper()} ({record['sum']})"
                        )
                        if decision:
                            predicted_bet = decision.get(
                                "bet_action",
                                "small",
                            )
                            correct = actual == predicted_bet
                            result_emoji = "✅" if correct else "❌"

                            state["log_text"] = (
                                f"{result_emoji} #{record['issue_id']}: "
                                f"{predicted_bet.upper()} → {actual.upper()}"
                            )

                            if betting_system and bet_info:
                                if isinstance(bet_info, dict):
                                    bet_type = bet_info.get("type")
                                    bet_amount = float(
                                        bet_info.get("amount", 0.0)
                                    )
                                    b_before = bet_info.get(
                                        "balance_before"
                                    )
                                else:
                                    try:
                                        bet_type, bet_amount = bet_info
                                        b_before = None
                                    except Exception:
                                        bet_type = None
                                        bet_amount = 0.0
                                        b_before = None

                                try:
                                    b_after = None
                                    bmap = state.get("balances") or {}
                                    bval = bmap.get("BUILD")

                                    if isinstance(bval, dict):
                                        b_after = float(
                                            bval.get("balance", 0.0)
                                        )
                                    else:
                                        b_after = _safe_round(bval)

                                except Exception:
                                    b_after = None

                                if correct:
                                    payout = bet_amount * 2
                                    betting_system.on_win(payout)
                                else:
                                    betting_system.on_loss()

                                try:
                                    if (
                                        b_before is not None
                                        and b_after is not None
                                    ):
                                        profit_value = (
                                            float(b_after)
                                            - float(b_before)
                                        )
                                    elif correct:
                                        profit_value = (
                                            payout - bet_amount
                                        )
                                    else:
                                        profit_value = -bet_amount

                                except Exception:
                                    profit_value = (
                                        payout - bet_amount
                                        if correct
                                        else -bet_amount
                                    )

                                record["bet"] = {
                                    "type": bet_type,
                                    "amount": float(bet_amount),
                                    "profit": _safe_round(
                                        profit_value,
                                        DECIMALS,
                                    ),
                                    "balance_before": b_before,
                                    "balance_after": b_after,
                                }

                                state["bet_stats"] = (
                                    betting_system.get_stats()
                                )

                            ms = state.get("model_stats") or {}
                            ms["predictions"] = (
                                ms.get("predictions", 0) + 1
                            )

                            if correct:
                                ms["wins"] = (
                                    ms.get("wins", 0) + 1
                                )
                                ms["current_streak"] = 0
                            else:
                                ms["losses"] = (
                                    ms.get("losses", 0) + 1
                                )
                                ms["current_streak"] = (
                                    ms.get("current_streak", 0) + 1
                                )

                            state["model_stats"] = ms

                        try:
                            record["timestamp"] = (
                                datetime.utcnow().isoformat() + "Z"
                            )
                        except Exception:
                            record["timestamp"] = None

                        if (
                            record.get("issue_id")
                            in processed_issue_ids
                        ):
                            continue

                        processed_issue_ids.add(
                            record.get("issue_id")
                        )

                        existing = next(
                            (
                                i
                                for i, h in enumerate(history)
                                if h.get("issue_id")
                                == record.get("issue_id")
                            ),
                            None,
                        )

                        if existing is None:
                            history.append(record)
                        else:
                            history[existing] = record

                        if len(history) > 500:
                            history = history[-500:]

                        state["history"] = history
                        state["three_number_analysis"] = (
                            three_number_analyzer.analyze(history)
                        )

                        ms_now = state.get("model_stats") or {}
                        bet_desc = "Không cược"

                        if record.get("bet"):
                            rb = record.get("bet") or {}
                            bet_desc = (
                                f"{str(rb.get('type', '—')).upper()} "
                                f"{float(rb.get('amount', 0.0)):.4f} "
                                f"{asset}"
                            )

                        print(
                            f"📊 KẾT QUẢ #{record.get('issue_id')}: "
                            f"{str(actual).upper()} "
                            f"({record.get('sum', '—')})\n"
                            f"🎯 Cược: {bet_desc}\n"
                            f"📈 Win: {ms_now.get('wins', 0)} "
                            f"| Lose: {ms_now.get('losses', 0)}",
                            flush=True,
                        )

                        try:
                            live.update(
                                make_vbtool_layout(state)
                            )
                        except Exception:
                            pass

                next_id = (last_id or 0) + 1

                if next_id not in pending:
                    analysis = three_number_analyzer.analyze(history)
                    state["three_number_analysis"] = analysis

                    predicted_outcome = analysis.get(
                        "bet_action",
                        "nho",
                    )
                    prediction_label = analysis.get(
                        "prediction",
                        "1-3",
                    )
                    conf = analysis.get(
                        "confidence",
                        0.5,
                    )

                    decision = {
                        "action": "PREDICT",
                        "bet_action": predicted_outcome,
                        "prediction": prediction_label,
                        "confidence": conf,
                    }

                    pending[next_id] = decision

                    icon = ICON_LOTTO.get(
                        predicted_outcome,
                        "",
                    )
                    state["next_prediction"] = (
                        f"{icon} {predicted_outcome.upper()}"
                    )
                    state["next_confidence"] = conf
                    state["log_text"] = (
                        f"Dự #{next_id}: "
                        f"{predicted_outcome.upper()} "
                        f"(tin cậy {conf * 100:.1f}%)"
                    )

                    if betting_system:
                        bet_amount = (
                            betting_system.get_bet_amount()
                        )
                        state["planned_bet"] = bet_amount

                        success, message = place_bet(
                            session,
                            headers,
                            next_id,
                            predicted_outcome,
                            bet_amount,
                            asset,
                        )

                        if success:
                            betting_system.record_wager(bet_amount)

                            try:
                                b_before = None
                                bmap = state.get("balances") or {}
                                bval = bmap.get("BUILD")

                                if isinstance(bval, dict):
                                    b_before = float(
                                        bval.get("balance", 0.0)
                                    )
                                else:
                                    b_before = _safe_round(bval)

                            except Exception:
                                b_before = None

                            pending_bets[next_id] = {
                                "type": predicted_outcome,
                                "amount": bet_amount,
                                "balance_before": b_before,
                            }

                            state["bet_stats"] = (
                                betting_system.get_stats()
                            )
                            state["log_text"] = (
                                f"💰Cược #{next_id}: "
                                f"{bet_amount:.2f} {asset} → "
                                f"{predicted_outcome.upper()}"
                            )

                        else:
                            state["log_text"] = (
                                f"⚠️ Lỗi: {message[:30]}"
                            )

                    else:
                        state["log_text"] = (
                            f"🎯 Dự đoán #{next_id}: "
                            f"{predicted_outcome.upper()}"
                        )
                        state["planned_bet"] = 0.0

                    pred_nums = ", ".join(
                        str(n)
                        for n in analysis.get(
                            "predicted_numbers",
                            [],
                        )
                    )

                    print(
                        f"🎯 DỰ ĐOÁN #{next_id}: "
                        f"{predicted_outcome.upper()}\n"
                        f"🔢 Bộ số: {pred_nums or '—'} "
                        f"| Σ={analysis.get('predicted_sum', '—')}\n"
                        f"🧠 Độ tin cậy: "
                        f"{float(conf) * 100:.1f}%",
                        flush=True,
                    )

                try:
                    live.update(
                        make_vbtool_layout(state)
                    )
                except Exception:
                    pass

                try:
                    time.sleep(HOME_POLL_INTERVAL)
                except KeyboardInterrupt:
                    return

            except KeyboardInterrupt:
                return

            except SystemExit:
                raise

            except Exception as exc:
                loop_error_count += 1
                log_debug(
                    f"LOTTO polling loop error #{loop_error_count}: "
                    f"{type(exc).__name__}: {exc}"
                )

                try:
                    _notify_admin_code_error(
                        "LOTTO polling loop lỗi — worker sẽ tự tiếp tục",
                        exc,
                        chat_id=str(_WORKER_CHAT_ID or ""),
                        tool_key="lotto",
                    )
                except Exception:
                    pass

                state["log_text"] = (
                    "⚠️ Kết nối/API gặp lỗi tạm thời, "
                    "đang tự chạy lại... "
                    f"({type(exc).__name__})"
                )

                try:
                    live.update(
                        make_vbtool_layout(state)
                    )
                except Exception:
                    pass

                if loop_error_count >= 3:
                    try:
                        session.close()
                    except Exception:
                        pass

                    session = requests.Session()
                    loop_error_count = 0

                try:
                    time.sleep(
                        min(
                            10.0,
                            2.0 + loop_error_count,
                        )
                    )
                except KeyboardInterrupt:
                    return

    console.clear()
    return

# ================== TOOL 3: CHẠY ĐUA TỐC ĐỘ ==================

def prints(r, g, b, text="text", end="\n"):
    if True:
        print(_strip_rich_markup(text), end=end, flush=True)
        return
    print(f"\033[38;2;{r};{g};{b}m{text}\033[0m", end=end)

def cprints(color_name, text="", end="\n"):

    try:
        h = str(VBTOOL_COLORS.get(color_name, "") or "").lstrip("#")
        if len(h) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
            print(text, end=end)
            return
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        prints(r, g, b, text=text, end=end)
    except (TypeError, ValueError):
        print(text, end=end)

NV = {
    1: "Bậc Thầy Tấn Công",
    2: "Quyền Sắt",
    3: "Thợ Lặn Sâu",
    4: "Cơn Lốc Sân Cỏ",
    5: "Hiệp Sĩ Phi Nhanh",
    6: "Vua Home Run",
}

def _sprint_history_sequence(history):
    seq = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        top1 = item.get("top1")
        if isinstance(top1, int):
            seq.append(top1)
        elif isinstance(top1, str) and top1.isdigit():
            seq.append(int(top1))
    return seq

def _sprint_delay(seq, nv):
    delay = 0
    for x in seq:
        if x == nv:
            break
        delay += 1
    return delay

def _sprint_current_streak(seq, nv):
    streak = 0
    for x in seq:
        if x == nv:
            streak += 1
        else:
            break
    return streak

def _sprint_weighted_recent_hits(seq, nv, limit=30, decay=0.92):
    score = 0.0
    weight = 1.0
    for x in seq[:limit]:
        if x == nv:
            score += weight
        weight *= decay
    return score

def _sprint_average_gap(seq, nv):
    positions = [i for i, x in enumerate(seq) if x == nv]
    if len(positions) < 2:
        return None
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    return sum(gaps) / len(gaps)

def _sprint_follow_probability(seq, prev_nv, nv):
    after = []
    for i in range(len(seq) - 1):
        if seq[i + 1] == prev_nv:
            after.append(seq[i])
    if not after:
        return 0.0
    return after.count(nv) / len(after)

def choose_free_cdtd(history, so_nv):
    seq = _sprint_history_sequence(history)
    if not seq:
        return random.sample(range(1, 7), so_nv)

    scores = {}
    total = len(seq)

    for nv in range(1, 7):
        score = 56.0

        # 1) Tần suất tổng thể
        wins = seq.count(nv)
        rate = wins / total
        score += (0.50 - rate) * 34

        # 2) Độ trễ kể từ lần xuất hiện gần nhất
        delay = _sprint_delay(seq, nv)
        if delay >= 18:
            score += 16
        elif delay >= 12:
            score += 12
        elif delay >= 8:
            score += 8
        elif delay >= 5:
            score += 4
        elif delay <= 1:
            score -= 7

        score += min(delay * 0.45, 8.5)

        # 3) Chuỗi hiện tại
        streak = _sprint_current_streak(seq, nv)
        if streak:
            score += min(streak * 6.5, 22)

        # 4) Xu hướng 30 ván gần nhất
        recent_score = _sprint_weighted_recent_hits(seq, nv, limit=30, decay=0.90)
        score += recent_score * 4.0

        # 5) Cửa sổ 10 ván gần
        last10 = seq[:10]
        count10 = sum(1 for x in last10 if x == nv)
        if count10 == 0:
            score += 5
        elif count10 == 1:
            score += 3
        elif count10 >= 4:
            score -= 9

        # 6) Mô hình sau người đang dẫn đầu gần nhất
        if len(seq) >= 2:
            prev_top = seq[0]
            score += _sprint_follow_probability(seq, prev_top, nv) * 32

        # 7) Chu kỳ xuất hiện
        avg_gap = _sprint_average_gap(seq, nv)
        if avg_gap:
            if delay >= avg_gap * 1.4:
                score += 8
            elif delay >= avg_gap:
                score += 4
            elif delay <= avg_gap * 0.55:
                score -= 4

        # 8) Tránh chọn mã quá nóng trong lịch sử dài
        history50 = seq[:50]
        if history50:
            wins50 = history50.count(nv)
            if wins50 >= 14:
                score -= 10
            elif wins50 == 0:
                score += 5

        # 9) Chống lặp gần nhất
        if len(seq) >= 2 and seq[0] == seq[1] == nv:
            score += 5

        scores[nv] = score

    min_score = min(scores.values())
    max_score = max(scores.values())
    if max_score != min_score:
        for nv in scores:
            scores[nv] = ((scores[nv] - min_score) / (max_score - min_score)) * 100

    # Lớp CDTD-AI bổ sung cho logic FREE.
    try:
        ai = analyze_cdtd_ai(history, "winner", "free")
        for nv in scores:
            scores[nv] += float(ai["scores"].get(nv, 50.0)) * 0.22
    except Exception:
        pass

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]

#=================NOT WINNER FREE=================

# 1
def choose_not_winner_frequency(history, so_nv):

    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    total = len(seq)

    recent10 = seq[:10]
    recent20 = seq[:20]
    recent50 = seq[:50]

    scores = {}

    for nv in range(1, 7):

        wins_all = seq.count(nv)

        not_win_all = (
            (total - wins_all) / total
            if total > 0 else 0.0
        )

        n10 = len(recent10)
        wins10 = recent10.count(nv)

        not_win10 = (
            (n10 - wins10) / n10
            if n10 > 0 else 0.0
        )

        n20 = len(recent20)
        wins20 = recent20.count(nv)

        not_win20 = (
            (n20 - wins20) / n20
            if n20 > 0 else 0.0
        )

        n50 = len(recent50)
        wins50 = recent50.count(nv)

        not_win50 = (
            (n50 - wins50) / n50
            if n50 > 0 else 0.0
        )


        delay = 0

        for x in seq:
            if x == nv:
                break
            delay += 1

        delay_score = min(
            delay / max(total, 1),
            1.0
        )

        score = (
            not_win_all * 0.30
            + not_win10 * 0.25
            + not_win20 * 0.20
            + not_win50 * 0.15
            + delay_score * 0.10
        )

        scores[nv] = score

    ranking = sorted(
        scores,
        key=lambda nv: scores[nv],
        reverse=True
    )

    return ranking[:so_nv]
    
# 2

def choose_xac_suat_free(history, so_nv):

    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    total = len(seq)

    recent30 = seq[:30]
    recent10 = seq[:10]

    scores = {}

    last_winner = seq[0] if seq else None

    for nv in range(1, 7):

        wins_all = seq.count(nv)

        p_all = (
            (total - wins_all + 1)
            / (total + 2)
        )

        n30 = len(recent30)
        wins30 = recent30.count(nv)

        p30 = (
            (n30 - wins30 + 1)
            / (n30 + 2)
        )

        n10 = len(recent10)
        wins10 = recent10.count(nv)

        p10 = (
            (n10 - wins10 + 1)
            / (n10 + 2)
        )

        transition_total = 0
        transition_wins = 0

        if len(seq) >= 2:
            for i in range(len(seq) - 1):
                previous_winner = seq[i + 1]
                current_winner = seq[i]

                if previous_winner == last_winner:
                    transition_total += 1

                    if current_winner == nv:
                        transition_wins += 1

        if transition_total > 0:
            p_transition = (
                (transition_total - transition_wins + 1)
                / (transition_total + 2)
            )
        else:
            p_transition = p_all

        probability = (
            p_all * 0.40
            + p30 * 0.30
            + p10 * 0.20
            + p_transition * 0.10
        )

        scores[nv] = probability

    result = sorted(
        scores,
        key=lambda nv: scores[nv],
        reverse=True
    )

    return result[:so_nv]
    
# 3
def choose_not_winner_transition(history, so_nv):

    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    current_winner = seq[0]

    scores = {}

    for nv in range(1, 7):

        transition_total = 0
        transition_win = 0

        for i in range(len(seq) - 1):

            newer = seq[i]
            older = seq[i + 1]

            if older == current_winner:
                transition_total += 1

                if newer == nv:
                    transition_win += 1

        if transition_total > 0:
            not_win_probability = (
                transition_total - transition_win + 1
            ) / (
                transition_total + 2
            )
        else:
            not_win_probability = 0.5

        recent_transitions = 0
        recent_not_win = 0

        for i in range(
            min(len(seq) - 1, 5)
        ):
            newer = seq[i]
            older = seq[i + 1]

            if older == current_winner:
                recent_transitions += 1

                if newer != nv:
                    recent_not_win += 1

        if recent_transitions > 0:
            recent_probability = (
                recent_not_win
                / recent_transitions
            )
        else:
            recent_probability = not_win_probability

        if current_winner == nv:
            current_penalty = 0.15
        else:
            current_penalty = 0.0

        score = (
            not_win_probability * 0.65
            + recent_probability * 0.35
            - current_penalty
        )

        scores[nv] = max(0.0, score)

    ranking = sorted(
        scores,
        key=lambda nv: scores[nv],
        reverse=True
    )

    return ranking[:so_nv]
    
#4
def choose_not_winner_markov(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    current = seq[0]
    scores = {}

    for nv in range(1, 7):
        total = 0
        win = 0

        for i in range(len(seq) - 1):
            if seq[i + 1] == current:
                total += 1
                if seq[i] == nv:
                    win += 1

        p_not = (total - win + 1) / (total + 2) if total else 0.5

        scores[nv] = p_not

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#5
def choose_not_winner_bayesian(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    scores = {}

    for nv in range(1, 7):
        wins = seq.count(nv)
        losses = len(seq) - wins

        probability = (losses + 1) / (wins + losses + 2)

        scores[nv] = probability

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#6
def choose_not_winner_momentum(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    recent = seq[:15]
    scores = {}

    for nv in range(1, 7):
        score = 0.0

        for i, winner in enumerate(recent):
            weight = 1 / (i + 1)

            if winner == nv:
                score -= weight
            else:
                score += weight / 5

        scores[nv] = score

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#7
def choose_not_winner_cycle(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    scores = {}

    for nv in range(1, 7):
        positions = [
            i for i, x in enumerate(seq)
            if x == nv
        ]

        if len(positions) < 2:
            scores[nv] = 0.5
            continue

        gaps = [
            positions[i + 1] - positions[i]
            for i in range(len(positions) - 1)
        ]

        avg_gap = sum(gaps) / len(gaps)
        current_gap = positions[0]

        distance = abs(current_gap - avg_gap)

        scores[nv] = min(
            distance / max(avg_gap, 1),
            1.0
        )

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#8
def choose_not_winner_balance(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    total = len(seq)
    expected = total / 6
    scores = {}

    for nv in range(1, 7):
        wins = seq.count(nv)

        deviation = abs(wins - expected)

        scores[nv] = (
            deviation / max(expected, 1)
        )

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#9
def choose_not_winner_multilevel(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    windows = [5, 10, 20, 40]
    scores = {}

    for nv in range(1, 7):
        score = 0.0
        weight_total = 0.0

        for size in windows:
            data = seq[:size]

            if not data:
                continue

            weight = 1 / size
            not_win = 1 - (
                data.count(nv) / len(data)
            )

            score += not_win * weight
            weight_total += weight

        scores[nv] = (
            score / weight_total
            if weight_total else 0.0
        )

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
 
#10
def choose_not_winner_stability(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    windows = [
        seq[:10],
        seq[:20],
        seq[:30]
    ]

    scores = {}

    for nv in range(1, 7):
        values = []

        for data in windows:
            if data:
                values.append(
                    1 - data.count(nv) / len(data)
                )

        if not values:
            scores[nv] = 0
            continue

        average = sum(values) / len(values)

        variance = sum(
            (x - average) ** 2
            for x in values
        ) / len(values)

        scores[nv] = average - variance

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#11
def choose_not_winner_bayes_multi(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    windows = [10, 20, 30, 50]
    scores = {}

    for nv in range(1, 7):
        total_score = 0.0
        total_weight = 0.0

        for size in windows:
            data = seq[:size]

            if not data:
                continue

            wins = data.count(nv)
            losses = len(data) - wins

            # Beta posterior
            probability = (
                losses + 1
            ) / (
                len(data) + 2
            )

            weight = 1 / size

            total_score += probability * weight
            total_weight += weight

        scores[nv] = (
            total_score / total_weight
            if total_weight
            else 0.5
        )

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#12
def choose_not_winner_entropy(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    recent = seq[:30]
    scores = {}

    for nv in range(1, 7):
        p = recent.count(nv) / len(recent)

        if p <= 0:
            scores[nv] = 1.0
            continue
        scores[nv] = -p * math.log(p)
    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
    
#13
def choose_not_winner_opponent(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    seq = _sprint_history_sequence(history)

    if not seq:
        return random.sample(range(1, 7), so_nv)

    current = seq[0]
    scores = {}

    for nv in range(1, 7):
        if nv == current:
            scores[nv] = 0.25
            continue

        occurrences = 0
        non_winner = 0

        for i in range(len(seq) - 1):
            if seq[i] == current:
                occurrences += 1

                if seq[i + 1] != nv:
                    non_winner += 1

        scores[nv] = (
            non_winner / occurrences
            if occurrences
            else 0.5
        )

    return sorted(scores, key=scores.get, reverse=True)[:so_nv]
#================TỔNG HỢP NOT_WINNER_FREE ================================
def choose_not_winner_free(history, so_nv):
    try:
        so_nv = max(1, min(int(so_nv), 6))
    except Exception:
        so_nv = 1

    if not history:
        return random.sample(range(1, 7), so_nv)

    logic_funcs = [
        choose_not_winner_frequency,
        choose_xac_suat_free,
        choose_not_winner_transition,
        choose_not_winner_markov,
        choose_not_winner_bayesian,
        choose_not_winner_momentum,
        choose_not_winner_cycle,
        choose_not_winner_balance,
        choose_not_winner_multilevel,
        choose_not_winner_stability,
        choose_not_winner_bayes_multi,
        choose_not_winner_entropy,
        choose_not_winner_opponent,
    ]

    # Điểm tổng hợp từ kết quả của các logic
    scores = {nv: 0.0 for nv in range(1, 7)}
    valid_results = 0

    for func in logic_funcs:
        try:
            result = func(history, so_nv)

            if not result:
                continue

            # Chấp nhận kết quả dạng danh sách số
            ranking = []
            for item in result:
                try:
                    nv = int(item)
                    if 1 <= nv <= 6 and nv not in ranking:
                        ranking.append(nv)
                except (TypeError, ValueError):
                    continue

            if not ranking:
                continue

            valid_results += 1

            # Xếp hạng cao hơn được điểm cao hơn,
            # giảm dần theo vị trí trong danh sách
            for rank, nv in enumerate(ranking):
                rank_score = (len(ranking) - rank) / len(ranking)
                scores[nv] += rank_score

        except Exception:
            continue

    if valid_results == 0:
        return random.sample(range(1, 7), so_nv)

    # Chuẩn hóa điểm theo số logic chạy thành công
    for nv in scores:
        scores[nv] /= valid_results

    # Xếp hạng theo điểm tổng hợp của 13 logic
    ranking = sorted(
        scores,
        key=lambda nv: scores[nv],
        reverse=True
    )

    return ranking[:so_nv]

# ================== LOGIC VIP ==================

def choose_vip_cdtd(history, so_nv):
    seq = _sprint_history_sequence(history)
    if not seq:
        return random.sample(range(1, 7), so_nv)
    try:
        so_nv = max(1, min(6, int(so_nv)))
    except Exception:
        so_nv = 1

    total = len(seq)
    fs = {
        "frequency": {nv: 0.0 for nv in range(1, 7)},
        "delay": {nv: 0.0 for nv in range(1, 7)},
        "streak": {nv: 0.0 for nv in range(1, 7)},
        "recent": {nv: 0.0 for nv in range(1, 7)},
        "markov1": {nv: 0.0 for nv in range(1, 7)},
        "markov2": {nv: 0.0 for nv in range(1, 7)},
        "last10": {nv: 0.0 for nv in range(1, 7)},
        "long_history": {nv: 0.0 for nv in range(1, 7)},
        "cycle": {nv: 0.0 for nv in range(1, 7)},
        "repeat": {nv: 0.0 for nv in range(1, 7)},
        "ai_module": {nv: 0.0 for nv in range(1, 7)},
    }

    for nv in range(1, 7):
        wins = seq.count(nv)
        rate = wins / total
        fs["frequency"][nv] = (0.50 - rate) * 46

        delay = _sprint_delay(seq, nv)
        d = min(delay * 1.0, 16)
        if delay >= 25:
            d += 18
        elif delay >= 18:
            d += 14
        elif delay >= 12:
            d += 10
        elif delay >= 6:
            d += 5
        elif delay <= 1:
            d -= 6
        fs["delay"][nv] = d

        streak = _sprint_current_streak(seq, nv)
        fs["streak"][nv] = min(streak * 8.0, 28) if streak else 0.0
        fs["recent"][nv] = _sprint_weighted_recent_hits(seq, nv, limit=50, decay=0.92) * 4.2

        if len(seq) >= 2:
            fs["markov1"][nv] = _sprint_follow_probability(seq, seq[0], nv) * 42

        if len(seq) >= 3:
            a, b = seq[1], seq[0]
            total_pair = hit = 0
            for i in range(len(seq) - 2):
                if seq[i + 2] == a and seq[i + 1] == b:
                    total_pair += 1
                    if seq[i] == nv:
                        hit += 1
            if total_pair:
                fs["markov2"][nv] = (hit / total_pair) * 58

        count10 = sum(1 for x in seq[:10] if x == nv)
        fs["last10"][nv] = 8 if count10 == 0 else 4 if count10 == 1 else -12 if count10 >= 4 else 0

        long_score = 0.0
        h100 = seq[:100]
        if h100:
            w100 = h100.count(nv)
            r100 = w100 / len(h100)
            long_score += r100 * 20
            if w100 >= 30:
                long_score += 10
            elif w100 >= 20:
                long_score += 6
            elif w100 == 0:
                long_score -= 8

        h200 = seq[:200]
        if h200:
            w200 = h200.count(nv)
            r200 = w200 / len(h200)
            long_score += r200 * 14
            if w200 >= 50:
                long_score += 12
            elif w200 >= 35:
                long_score += 8
        fs["long_history"][nv] = long_score

        avg_gap = _sprint_average_gap(seq, nv)
        if avg_gap:
            fs["cycle"][nv] = (
                10 if delay >= avg_gap * 1.45
                else 5 if delay >= avg_gap
                else -5 if delay <= avg_gap * 0.6
                else 0
            )

        if len(seq) >= 2 and seq[0] == seq[1] == nv:
            fs["repeat"][nv] = 6

    try:
        ai = analyze_cdtd_ai(history, "winner", "vip")
        fs["ai_module"] = {
            nv: (float(ai["scores"].get(nv, 50.0)) - 50.0) * 1.15
            for nv in range(1, 7)
        }
    except Exception:
        pass

    scores = {nv: 96.0 for nv in range(1, 7)}
    for feature_scores in fs.values():
        for nv, value in feature_scores.items():
            scores[nv] += float(value)

    items = list(scores.items())
    random.shuffle(items)
    items.sort(key=lambda item: item[1], reverse=True)
    return [nv for nv, _ in items[:so_nv]]


def choose_not_winner_vip(history, so_nv):
    seq = _sprint_history_sequence(history)
    if not seq:
        return random.sample(range(1, 7), so_nv)
    try:
        so_nv = max(1, min(6, int(so_nv)))
    except Exception:
        so_nv = 1

    total = len(seq)
    fs = {
        "frequency": {nv: 0.0 for nv in range(1, 7)},
        "delay": {nv: 0.0 for nv in range(1, 7)},
        "streak": {nv: 0.0 for nv in range(1, 7)},
        "recent": {nv: 0.0 for nv in range(1, 7)},
        "markov1": {nv: 0.0 for nv in range(1, 7)},
        "markov2": {nv: 0.0 for nv in range(1, 7)},
        "last10": {nv: 0.0 for nv in range(1, 7)},
        "long_history": {nv: 0.0 for nv in range(1, 7)},
        "cycle": {nv: 0.0 for nv in range(1, 7)},
        "repeat": {nv: 0.0 for nv in range(1, 7)},
        "ai_module": {nv: 0.0 for nv in range(1, 7)},
    }

    for nv in range(1, 7):
        wins = seq.count(nv)
        rate = wins / total
        fs["frequency"][nv] = (1 - rate) * 42

        delay = _sprint_delay(seq, nv)
        d = min(delay * 0.45, 8)
        if delay >= 25:
            d += 20
        elif delay >= 18:
            d += 16
        elif delay >= 12:
            d += 11
        elif delay >= 6:
            d += 6
        elif delay <= 1:
            d -= 12
        fs["delay"][nv] = d

        streak = _sprint_current_streak(seq, nv)
        if streak:
            fs["streak"][nv] = -min(streak * 9.0, 30)
        fs["recent"][nv] = -_sprint_weighted_recent_hits(seq, nv, limit=50, decay=0.92) * 5.5

        if len(seq) >= 2:
            fs["markov1"][nv] = -_sprint_follow_probability(seq, seq[0], nv) * 40

        if len(seq) >= 3:
            a, b = seq[1], seq[0]
            total_pair = hit = 0
            for i in range(len(seq) - 2):
                if seq[i + 2] == a and seq[i + 1] == b:
                    total_pair += 1
                    if seq[i] == nv:
                        hit += 1
            if total_pair:
                fs["markov2"][nv] = -(hit / total_pair) * 60

        count10 = sum(1 for x in seq[:10] if x == nv)
        fs["last10"][nv] = 10 if count10 == 0 else 5 if count10 == 1 else -15 if count10 >= 4 else 0

        long_score = 0.0
        h100 = seq[:100]
        if h100:
            w100 = h100.count(nv)
            r100 = w100 / len(h100)
            long_score += (1 - r100) * 26
            if w100 == 0:
                long_score += 12
            elif w100 <= 3:
                long_score += 7
            elif w100 >= 20:
                long_score -= 12

        h200 = seq[:200]
        if h200:
            w200 = h200.count(nv)
            r200 = w200 / len(h200)
            long_score += (1 - r200) * 16
            if w200 >= 45:
                long_score -= 14
        fs["long_history"][nv] = long_score

        avg_gap = _sprint_average_gap(seq, nv)
        if avg_gap:
            fs["cycle"][nv] = (
                8 if delay >= avg_gap * 1.5
                else -6 if delay <= avg_gap * 0.7
                else 0
            )

        if len(seq) >= 2 and seq[0] == seq[1] == nv:
            fs["repeat"][nv] = -8

    try:
        ai = analyze_cdtd_ai(history, "not_winner", "vip")
        fs["ai_module"] = {
            nv: (float(ai["scores"].get(nv, 50.0)) - 50.0) * 1.15
            for nv in range(1, 7)
        }
    except Exception:
        pass

    scores = {nv: 104.0 for nv in range(1, 7)}
    for feature_scores in fs.values():
        for nv, value in feature_scores.items():
            scores[nv] += float(value)

    items = list(scores.items())
    random.shuffle(items)
    items.sort(key=lambda item: item[1], reverse=True)
    return [nv for nv, _ in items[:so_nv]]


class SprintGame:

    def __init__(self):
        self.session = requests.Session()
        self.headers = {}
        self.coin = ""
        self.bet_amount = 0
        self.he_so = 0
        self.type_bet = ""
        self.so_nv = 0
        self.logic_type = "free"
        self.target_profit = 0.0
        self.stop_loss = 0.0
        self.rest_after_lose = 0
        self.anti_soi_after = 0
        self.auto_bet = True
        self.daily_pause_window = None

    def home(self, coin):
        params = {
            "asset": coin
        }

        while True:
            try:
                response = self.session.get(
                    "https://api.sprintrun.win/sprint/home",
                    params=params,
                    headers=self.headers,
                    timeout=10
                )

                return response.text

            except requests.exceptions.RequestException as e:
                cprints("ruby", f"⚠️ Lỗi kết nối API: {e}")
                cprints("yellow", "🔄 Đang kết nối lại...")
                time.sleep(2)

    def info(self, ki, coin):
        while True:
            try:
                params = {
                    "issue": str(ki),
                    "asset": coin,
                }

                response = self.session.get(
                    "https://api.sprintrun.win/sprint/issue_result",
                    params=params,
                    headers=self.headers,
                    timeout=10
                )

                return json.loads(response.text)

            except Exception as e:
                cprints("ruby", f"⚠️ Lỗi lấy kết quả: {e}")
                time.sleep(2)

    def bet(self, ki, kq, coin, bet_amount, type_bet):
        # Lớp bảo vệ cuối cùng: không gửi lệnh cược trong giờ nghỉ đã cài.
        if _check_daily_pause_window(
            getattr(self, "daily_pause_window", None), "CDTD"
        ):
            return False
        try:
            json_data = {
                "issue_id": int(ki),
                "bet_group": type_bet,
                "asset_type": coin,
                "athlete_id": kq,
                "bet_amount": bet_amount,
            }

            response = self.session.post(
                "https://api.sprintrun.win/sprint/bet",
                headers=self.headers,
                json=json_data,
                timeout=10
            )

            res = json.loads(response.text)

            if res.get("code") == 0:
                return True

        except Exception as e:
            cprints("ruby", f"❌ Lỗi đặt cược {coin}: {e}")

        return False

    def random_result(self, so_nguoi):
        li = [1, 2, 3, 4, 5, 6]
        kq = []

        for _ in range(int(so_nguoi)):
            x = random.choice(li)
            kq.append(x)
            li.remove(x)

        return kq

    def check_result(self, coin, bot_chon, type_game):
        try:
            res = json.loads(self.home(coin))
        except Exception as e:
            cprints("ruby", f"❌ Lỗi lấy dữ liệu trận đấu: {e}")
            return None

        try:
            ki = res["data"]["issue_id"]
        except Exception:
            cprints("ruby", "❌ Không lấy được mã ván đấu")
            return None

        while True:
            try:
                res = json.loads(self.home(coin))
                raw_sec = int(res.get("data", {}).get("expire_seconds", 0))
                expire_sec = max(0, raw_sec - 1)
                if expire_sec <= 1:
                    break
                time.sleep(1)
            except Exception as e:
                cprints("ruby", f"⚠️ Lỗi đếm thời gian: {e}")
                time.sleep(2)

        time.sleep(8)
        re_end = self.info(ki, coin)

        try:
            rank = [int(x) for x in re_end["data"]["athlete_rank"]]
            top1 = rank[0]
            award = re_end["data"]["my_total_award"]
            won = top1 in bot_chon if type_game == "winner" else top1 not in bot_chon

            return {
                "issue_id": re_end["data"]["issue_id"],
                "top1": top1,
                "my_total_bet": re_end["data"]["my_total_bet"],
                "my_total_award": re_end["data"]["my_total_award"],
                "kq": top1 in bot_chon if type_game == "winner" else top1 not in bot_chon,
                "bot_chon": bot_chon,
            }

        except Exception as e:
            cprints("ruby", f"❌ Lỗi xử lý kết quả: {e}")
            return None

    def vanchoi(self, coin, history, he_so, bet_amount0, type_bet, so_nv):

        if _check_daily_pause_window(
            getattr(self, "daily_pause_window", None), "CDTD"
        ):
            return None

        if self.logic_type == "vip":
            try:
                is_valid, msg, is_vip = check_activation_valid(exit_on_expired=False)
                if not is_valid:
                    cprints("ruby", f"❌ {msg or 'Key không còn hiệu lực.'}")
                    self.logic_type = "free"
                elif not is_vip:
                    cprints("ruby", "❌ Key FREE không được sử dụng Logic VIP!")
                    self.logic_type = "free"
            except Exception as exc:
                log_debug(f"CDTD VIP permission check error: {exc}")
                self.logic_type = "free"

        try:
            res = json.loads(self.home(coin))
            ki = res["data"]["issue_id"]

        except Exception as e:
            cprints("ruby", f"❌ Lỗi lấy ván hiện tại: {e}")
            return None

        analysis_time = 10

        while True:
            try:
                if _check_daily_pause_window(
                    getattr(self, "daily_pause_window", None), "CDTD"
                ):
                    return None
                timer = json.loads(self.home(coin))
                expire = int(timer.get("data", {}).get("expire_seconds", 0))
                if expire <= analysis_time:
                    break
                time.sleep(1)
            except Exception as e:
                cprints("ruby", f"⚠️ Lỗi đếm giờ: {e}")
                time.sleep(2)

        if _check_daily_pause_window(
            getattr(self, "daily_pause_window", None), "CDTD"
        ):
            return None

        if type_bet == "winner":
            if self.logic_type == "free":
                bot_chon = choose_free_cdtd(history, int(so_nv))
            elif self.logic_type == "vip":
                bot_chon = choose_vip_cdtd(history, int(so_nv))
            else:
                bot_chon = self.random_result(int(so_nv))

        elif type_bet == "not_winner":
            if self.logic_type == "free":
                bot_chon = choose_not_winner_free(history, int(so_nv))
            elif self.logic_type == "vip":
                bot_chon = choose_not_winner_vip(history, int(so_nv))
            else:
                bot_chon = self.random_result(int(so_nv))
        else:
            bot_chon = self.random_result(int(so_nv))

        ten_nv = [NV.get(i, str(i)) for i in bot_chon]

        bet_amount = bet_amount0 / len(bot_chon)

        if history and history[0].get("kq") is False:
            previous_total_bet = float(history[0].get("my_total_bet") or 0)
            if previous_total_bet > 0:
                bet_amount = (previous_total_bet * he_so) / len(bot_chon)

        total_round_bet = float((bet_amount * max(1, len(bot_chon))) or 0)
        self._telegram_current_bet = total_round_bet
        if _check_daily_pause_window(
            getattr(self, "daily_pause_window", None), "CDTD"
        ):
            return None
        _cdtd_telegram_round_bet(self, ki, total_round_bet, bot_chon, self.auto_bet)

        if self.auto_bet:

            try:
                pending_record = {
                    "issue_id": int(ki),
                    "bot_chon": list(bot_chon),
                    "selected": list(bot_chon),
                    "top1": None,
                    "my_total_bet": float(bet_amount * len(bot_chon)),
                    "my_total_award": 0.0,
                    "kq": None,
                    "status": "ĐANG CƯỢC",
                    "logic_type": self.logic_type,
                    "_pending_cdtd": True,
                }
                history[:] = [h for h in history if not (isinstance(h, dict) and h.get("_pending_cdtd") and str(h.get("issue_id")) == str(ki))]
                history.insert(0, pending_record)
            except Exception as e:
                log_debug(f"CDTD pending history error: {e}")

            while True:
                try:
                    if _check_daily_pause_window(
                        getattr(self, "daily_pause_window", None), "CDTD"
                    ):
                        history[:] = [
                            item for item in history
                            if not (
                                isinstance(item, dict)
                                and item.get("_pending_cdtd")
                            )
                        ]
                        return None
                    timer = json.loads(self.home(coin))
                    expire = int(timer.get("data", {}).get("expire_seconds", 0))
                    if expire <= 10:
                        break
                    time.sleep(1)
                except Exception as e:
                    cprints("ruby", f"⚠️ Lỗi đếm giờ: {e}")
                    time.sleep(2)

            try:
                res = json.loads(self.home(coin))
                ki = res["data"]["issue_id"]
            except Exception:
                ki = ki

            placed_count = 0
            for i in bot_chon:
                try:
                    if _check_daily_pause_window(
                        getattr(self, "daily_pause_window", None), "CDTD"
                    ):
                        history[:] = [
                            item for item in history
                            if not (
                                isinstance(item, dict)
                                and item.get("_pending_cdtd")
                            )
                        ]
                        return None
                    if self.bet(ki, i, coin, bet_amount, type_bet):
                        placed_count += 1
                except Exception as e:
                    log_debug(f"CDTD place bet error P{i}: {e}")

            # Cập nhật chính dòng lịch sử vừa tạo, không tạo thêm một dòng mới.
            try:
                for item in history:
                    if isinstance(item, dict) and item.get("_pending_cdtd") and str(item.get("issue_id")) == str(ki):
                        item["status"] = "ĐÃ ĐẶT CƯỢC" if placed_count > 0 else "CƯỢC THẤT BẠI"
                        item["my_total_bet"] = float(bet_amount * max(1, placed_count if placed_count else len(bot_chon)))
                        break
            except Exception as e:
                log_debug(f"CDTD pending history update error: {e}")


        return self.check_result(
            coin,
            bot_chon,
            type_bet,
        )
        
    def user_asset(self):
        while True:
            try:
                json_data = {
                    "user_id": int(self.headers["user-id"]),
                    "source": "home",
                }

                response = self.session.post(
                    "https://wallet.3games.io/api/wallet/user_asset",
                    headers=self.headers,
                    json=json_data,
                    timeout=10,
                )

                res = json.loads(response.text)

                asset = {
                    "USDT": res["data"]["user_asset"]["USDT"],
                    "WORLD": res["data"]["user_asset"]["WORLD"],
                    "BUILD": res["data"]["user_asset"]["BUILD"],
                }

                return asset

            except requests.exceptions.RequestException as e:
                cprints("ruby", f"⚠️ Lỗi kết nối lấy số dư: {e}")
                cprints("yellow", "🔄 Đang thử lấy lại số dư...")
                time.sleep(2)

            except Exception as e:
                cprints("ruby", f"❌ Lỗi khi lấy số dư: {e}")
                time.sleep(2)

def _read_float_input(prompt: str, default: float = 0.0) -> float:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            cprints("ruby", "❌ Giá trị không hợp lệ, vui lòng nhập số.")

def _read_int_input(prompt: str, default: int = 0):
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            cprints("ruby", "❌ Giá trị không hợp lệ, vui lòng nhập số nguyên.")

def _fmt_cdtd_number(value: Any, decimals: int = 6) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "0"
    text = f"{num:.{decimals}f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"

def _cdtd_capital_stop_reason(game, current_balance) -> tuple[bool, str]:
    try:
        current_balance = float(current_balance)
    except (TypeError, ValueError):
        return False, ""

    target_balance = float(getattr(game, "target_profit", 0.0) or 0.0)
    stop_balance = float(getattr(game, "stop_loss", 0.0) or 0.0)
    coin = str(getattr(game, "coin", "BUILD") or "BUILD")

    if target_balance > 0 and current_balance >= target_balance:
        return True, (
            f"🎉Đã đạt mục tiêu lãi ({_fmt_cdtd_number(current_balance)} {coin}). Dừng tool."
        )

    if stop_balance > 0 and current_balance <= stop_balance:
        return True, (
            f"💀Đã chạm cắt lỗ ({_fmt_cdtd_number(current_balance)} {coin}). Dừng tool."
        )

    return False, ""

def _cdtd_telegram_round_bet(game, issue_id, total_bet, bot_chon, auto_bet=True):

    coin = str(getattr(game, "coin", "BUILD") or "BUILD").upper()
    total = float(total_bet or 0)
    game._telegram_current_bet = total
    game._telegram_selected_names = ", ".join(NV.get(int(x), str(x)) for x in (bot_chon or [])) or "-"
    cprints(
        "gold",
        f"📌 VÁN #{issue_id}\n"
        f"🎯 Đánh : {game._telegram_selected_names}\n"
        f"💰 Cược: {_fmt_cdtd_number(total)} {coin}"
    )
def _cdtd_telegram_round_result(game, res, user_id, balance_before=None, balance_after=None, stats=None, run_start=None, start_balance=None):

    coin = str(getattr(game, "coin", "BUILD") or "BUILD").upper()
    res = res if isinstance(res, dict) else {}
    issue = res.get("issue_id", "-")
    top1 = res.get("top1")
    top1_name = NV.get(int(top1), str(top1)) if top1 is not None else "-"
    bet = float(res.get("my_total_bet") or 0)
    award = float(res.get("my_total_award") or 0)
    won = bool(res.get("kq"))

    if start_balance is not None and balance_after is not None:
        try:
            profit = float(balance_after) - float(start_balance)
        except (TypeError, ValueError):
            profit = 0.0
    else:

        profit = award - bet
    stats = stats or {}
    win_streak = int(stats.get("win_streak", 0) or 0)
    max_win_streak = int(stats.get("max_win_streak", 0) or 0)
    lose_streak = int(stats.get("lose_streak", 0) or 0)
    max_lose_streak = int(stats.get("max_lose_streak", 0) or 0)
    if run_start is not None:
        elapsed = max(0, int(time.time() - float(run_start)))
    else:
        elapsed = 0
    run_h, rem = divmod(elapsed, 3600)
    run_m = rem // 60

    balance_from = start_balance if start_balance is not None else (balance_before if balance_before is not None else balance_after)
    balance_to = balance_after if balance_after is not None else balance_before
    selected_names = getattr(game, "_telegram_selected_names", "-") or "-"
    result_line = "✅ WIN" if won else "❌ LOSE"
    logic_label = str(getattr(game, "logic_type", "free") or "free").upper()
    lines = [
        f"👤 User:{user_id}",
        f"🧠 LOGIC {logic_label}",
        f"📌 Ván: #{issue}",
        f"🎯 Đánh: {selected_names}",
        f"👑 Quán Quân: {top1_name}",
        f"🏆 WIN:{win_streak}(MAX:{max_win_streak}) | 💀 LOSE:{lose_streak}(MAX:{max_lose_streak})",
        result_line,
        f"📊 Lãi/lỗ {profit:+,.6f} {coin}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💳 SỐ DƯ: {_fmt_cdtd_number(balance_from)} -> {_fmt_cdtd_number(balance_to)}",
        f"⏳ CHẠY BOT: {run_h} Giờ {run_m} Phút",
    ]
    cprints("bright_green" if won else "ruby", "\n".join(lines))

def cauhinh_game(game):
    time.sleep(0.6)
    cprints("bright_green", "💰 CHỌN LOẠI TIỀN CƯỢC")

    while True:
        coin_key = input().strip().upper()
        coin_map = {
            "USDT": "USDT",
            "BUILD": "BUILD",
            "XWORLD": "WORLD",
        }

        if coin_key in coin_map:
            game.coin = coin_map[coin_key]
            break

        cprints(
            "ruby",
            "❌ Lựa chọn không hợp lệ, vui lòng chọn USDT, BUILD hoặc XWORLD.",
        )

    while True:
        game.bet_amount = _read_float_input(
            f"1. Nhập số {game.coin} cược mỗi ván: ",
            0.0,
        )
        if game.bet_amount > 0:
            break
        cprints("ruby", "❌ Tiền cược phải lớn hơn 0.")

    while True:
        game.he_so = _read_float_input(
            "2. Nhập hệ số cược sau thua: ",
            1.0,
        )
        if game.he_so >= 1:
            break
        cprints("ruby", "❌ Hệ số cược phải lớn hơn hoặc bằng 1.")

    cprints("bright_green", "3. Chọn loại đặt:")

    while True:
        type_bet = input().strip().casefold()

        if type_bet == "ai là quán quân":
            game.type_bet = "winner"
            print(
                "✅ Đã chọn: Ai là quán quân\n\n"
                "4. Đặt vào bao nhiêu người:",
                flush=True,
            )
            break

        if type_bet == "ai không là quán quân":
            game.type_bet = "not_winner"
            print(
                "✅ Đã chọn: Ai không là quán quân\n\n"
                "4. Đặt vào bao nhiêu người:",
                flush=True,
            )
            break

        cprints(
            "ruby",
            "❌ Vui lòng chọn: Ai là quán quân hoặc Ai không là quán quân.",
        )

    while True:
        try:
            game.so_nv = int(input().strip())
        except (ValueError, TypeError):
            cprints("ruby", "❌ Vui lòng chọn số người từ 1 đến 6")
            continue

        if 1 <= game.so_nv <= 6:
            break

        cprints("ruby", "❌ Vui lòng chọn số người từ 1 đến 6")

    cprints(
        "bright_green",
        "5. Mục tiêu lãi: Nhập số BUILD muốn đặt để dừng (Nhập 0 = tắt):",
    )

    game.target_profit = _read_float_input("", 0.0)
    game.stop_loss = _read_float_input(
        "6. Cắt lỗ: Nhập số BUILD còn lại để dừng (Nhập 0 = tắt): ",
        0.0,
    )
    game.daily_pause_window = _prompt_daily_pause_window("CDTD", "7")
    _reset_daily_pause_state("CDTD")
    game.rest_after_lose = _read_int_input(
        "8. Nghỉ sau khi thua: Nghỉ bao nhiêu ván? (0 = Tắt): ",
        0,
    )
    game.anti_soi_after = _read_int_input(
        "9. Chống soi: Nghỉ 1 ván sau bao nhiêu ván cược? (0 = Tắt): ",
        0,
    )

    game.target_profit = max(0.0, float(game.target_profit))
    game.stop_loss = max(0.0, float(game.stop_loss))
    game.rest_after_lose = max(0, int(game.rest_after_lose))
    game.anti_soi_after = max(0, int(game.anti_soi_after))

    json_config = {
        "Coin": game.coin,
        "he_so": game.he_so,
        "bet_amount0": game.bet_amount,
        "type_bet": game.type_bet,
        "So_nv": game.so_nv,
        "target_profit": game.target_profit,
        "stop_loss": game.stop_loss,
        "daily_pause_window": _format_daily_pause_window(game.daily_pause_window),
        "rest_after_lose": game.rest_after_lose,
        "anti_soi_after": game.anti_soi_after,
        "auto_bet": bool(getattr(game, "auto_bet", True)),
    }

    try:
        with open("config-cdtd.txt", "w", encoding="utf-8") as f:
            json.dump(
                json_config,
                f,
                indent=4,
                ensure_ascii=False,
            )

        cprints("bright_green", "✅ Đã lưu cấu hình.")

    except OSError as e:
        cprints("ruby", f"❌ Không thể lưu cấu hình: {e}")

    return game

def _cdtd_make_game(account, base_game):
    game = SprintGame()
    game.headers = {
        "accept": "*/*",
        "accept-language": "vi,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://sprintrun.win",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Mobile Safari/537.36"
        ),
        "user-id": str(account.get("user-id") or account.get("userId") or ""),
        "user-login": "login_v2",
        "user-secret-key": str(account.get("user-secret-key") or account.get("secretKey") or ""),
    }
    for attr in (
        "coin", "bet_amount", "he_so", "type_bet", "so_nv", "logic_type",
        "target_profit", "stop_loss", "daily_pause_window", "rest_after_lose", "anti_soi_after", "auto_bet",
    ):
        if hasattr(base_game, attr):
            setattr(game, attr, getattr(base_game, attr))
    game._telegram_current_bet = float(getattr(game, "bet_amount", 0) or 0)
    return game


def _cdtd_run_accounts(accounts, base_game, is_vip):
    """Chạy CDTD cho danh sách tài khoản đã chọn.

    Mỗi tài khoản được chạy độc lập trong một thread; cấu hình cược/logic dùng chung.
    """
    def worker(index, account):
        game = _cdtd_make_game(account, base_game)
        user_id = str(account["user-id"])
        stats = {
            "win": 0, "lose": 0, "win_streak": 0, "max_win_streak": 0,
            "lose_streak": 0, "max_lose_streak": 0, "earn": 0.0,
        }
        history = []
        skip_rounds = 0

        start_balance = None
        current_balance = 0.0
        cumulative_round_profit = 0.0
        run_start = time.time()

        if False:
            cprints("bright_green", f"✅ CDTD tài khoản {index}: userId={user_id} đã bắt đầu chạy.")
        while True:
            try:
                if _check_daily_pause_window(
                    getattr(game, "daily_pause_window", None), "CDTD"
                ):
                    time.sleep(1)
                    continue
                if not check_activation_valid(exit_on_expired=False)[0]:
                    cprints("ruby", f"⛔ Key hết hạn — dừng tài khoản {user_id}.")
                    return
                if skip_rounds > 0:
                    skip_rounds -= 1
                    time.sleep(2)
                    continue
                asset = game.user_asset()
                if not isinstance(asset, dict):
                    time.sleep(3)
                    continue
                current_balance = float(asset.get(game.coin, 0) or 0)
                if start_balance is None:
                    start_balance = current_balance
                should_stop, stop_reason = _cdtd_capital_stop_reason(game, current_balance)
                if should_stop:
                    cprints("gold", f"🛑 {user_id}: {stop_reason}")
                    return
                balance_before_round = current_balance
                res = game.vanchoi(
                    game.coin, history, game.he_so, game.bet_amount,
                    game.type_bet, game.so_nv,
                )
                if res is None:
                    time.sleep(2)
                    continue
                won = bool(res.get("kq"))
                bet = float(res.get("my_total_bet") or 0)
                award = float(res.get("my_total_award") or 0)


                try:
                    result_issue = str(res.get("issue_id", ""))
                    for item in history:
                        if isinstance(item, dict) and str(item.get("issue_id", "")) == result_issue:
                            item["kq"] = won
                            item["top1"] = res.get("top1")
                            item["my_total_bet"] = bet
                            item["my_total_award"] = award
                            item["status"] = "THẮNG" if won else "THUA"
                            item.pop("_pending_cdtd", None)
                            break

                    updated = next((item for item in history if isinstance(item, dict) and str(item.get("issue_id", "")) == result_issue), None)
                    if updated is not None:
                        history.remove(updated)
                        history.insert(0, updated)
                except Exception as e:
                    log_debug(f"CDTD result history update error: {e}")

                if won:
                    stats["win"] += 1
                    stats["win_streak"] += 1
                    stats["lose_streak"] = 0
                    stats["max_win_streak"] = max(stats["max_win_streak"], stats["win_streak"])
                else:
                    stats["lose"] += 1
                    stats["lose_streak"] += 1
                    stats["win_streak"] = 0
                    stats["max_lose_streak"] = max(stats["max_lose_streak"], stats["lose_streak"])
                    if game.rest_after_lose > 0:
                        skip_rounds = game.rest_after_lose
                balance_after_round = balance_before_round
                try:

                    post_asset = game.user_asset()
                    if isinstance(post_asset, dict):
                        live_after = post_asset.get(game.coin)
                        if live_after is not None:
                            balance_after_round = float(live_after)
                            current_balance = balance_after_round
                except Exception as e:
                    log_debug(f"CDTD post-round balance error: {e}")

                if start_balance is not None and balance_after_round is not None:
                    try:
                        cumulative_round_profit = float(balance_after_round) - float(start_balance)
                    except (TypeError, ValueError):
                        cumulative_round_profit = award - bet
                else:
                    cumulative_round_profit = award - bet
                stats["earn"] = cumulative_round_profit

                _cdtd_telegram_round_result(
                    game,
                    res,
                    user_id=user_id,
                    balance_before=balance_before_round,
                    balance_after=balance_after_round,
                    stats=stats,
                    run_start=run_start,
                    start_balance=start_balance,
                )
                if game.anti_soi_after > 0 and len(history) % game.anti_soi_after == 0:
                    cprints("gold", "🛡️ Chống soi: nghỉ 1 ván theo cấu hình.")
                    skip_rounds = max(skip_rounds, 1)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                cprints("ruby", f"❌ CDTD userId={user_id} lỗi: {type(exc).__name__}: {exc}")
                time.sleep(3)

    # CDTD hiện chạy đúng 1 tài khoản theo luồng mới.
    # Không tạo thread nền riêng để tránh luồng CDTD cũ còn treo/chạy song song.
    if not accounts:
        cprints("ruby", "❌ Chưa có tài khoản CDTD để chạy.")
        return
    try:
        worker(1, accounts[0])
    except KeyboardInterrupt:
        cprints("yellow", "🛑 Đang dừng CDTD...")


def main_cdtd():
    start_key_expiry_monitor()
    if not check_activation_valid()[0]:
        cprints("ruby", "❌ Key của bạn không còn hiệu lực.")
        return

    console.clear()
    am = AccountManager()
    cprints("gold", "📋 CHẠY ĐUA TỐC ĐỘ\n")
    cprints("white", "Gửi link game CDTD có userId và secretKey để thêm tài khoản.")
    cprints("dim" if "dim" in VBTOOL_COLORS else "white", "")

    try:
        raw_link = input("🔗 Dán link game: ").strip()
        if raw_link == "0":
            cprints("yellow", "🛑 Đã hủy.")
            return

        while True:
            acc = am.parse_link_to_account(raw_link)
            if acc:
                am.accounts.append(acc)
                am.save()
                break
            cprints("ruby", "❌ Link không hợp lệ! Link phải chứa userId và secretKey.")
            raw_link = input("🔗 Dán lại link game: ").strip()
            if raw_link == "0":
                return

        cprints("bright_green", f"✅ Đã thêm tài khoản: {am.accounts[-1].get('user-id') or am.accounts[-1].get('userId')}")
        selected_accounts = [am.accounts[-1]]

        game = cauhinh_game(SprintGame())
        is_valid, msg, is_vip = check_activation_valid(exit_on_expired=False)
        if not is_valid:
            cprints(
                "ruby",
                f"🔒 {msg}. Vui lòng nhập lại Key FREE hoặc Key VIP.",
            )
            return

        cprints("gold", "9.CHỌN LOGIC")
        while True:
            choice = input().strip().upper()

            # Xác thực lại sau khi người dùng nhập lựa chọn LOGIC.
            # Key hết hạn thì không được chọn tiếp kể cả LOGIC FREE.
            is_valid, msg, is_vip = check_activation_valid(exit_on_expired=False)
            if not is_valid:
                cprints(
                    "ruby",
                    f"🔒 {msg}. Vui lòng nhập lại Key FREE hoặc Key VIP.",
                )
                return

            if choice in ("1", "🆓 LOGIC FREE", "LOGIC FREE", "FREE"):
                game.logic_type = "free"
                cprints("bright_green", "✅ Đã chọn Logic FREE")
                break
            if choice in ("2", "👑 LOGIC VIP", "LOGIC VIP", "VIP"):
                if is_vip:
                    game.logic_type = "vip"
                    cprints("bright_green", "✅ Đã chọn Logic VIP")
                    break
                cprints("ruby", "❌ Key FREE không được sử dụng Logic VIP!")
                continue
            cprints("ruby", "❌ Lựa chọn không hợp lệ!")

        acc = selected_accounts[0]
        user_id = acc.get("user-id") or acc.get("userId") or "—"
        secret = acc.get("user-secret-key") or acc.get("secretKey") or ""
        balance = None

        try:
            uid_int = int(str(user_id))
            build, world, usdt = fetch_balances_3games(
                retries=2,
                timeout=6,
                params={"userId": str(user_id)},
                uid=uid_int,
                secret=secret,
            )
            coin = str(getattr(game, "coin", "BUILD") or "BUILD").upper()
            balance = (
                build if coin == "BUILD"
                else world if coin in ("XWORLD", "WORLD")
                else usdt if coin == "USDT"
                else None
            )
        except Exception as e:
            log_debug(f"CDTD config balance error: {e}")

        coin = str(getattr(game, "coin", "BUILD") or "BUILD").upper()
        base = float(getattr(game, "bet_amount", 0) or 0)
        mult = float(getattr(game, "he_so", 1) or 1)
        target = float(getattr(game, "target_profit", 0) or 0)
        stop = float(getattr(game, "stop_loss", 0) or 0)
        anti = int(getattr(game, "anti_soi_after", 0) or 0)
        pause = int(getattr(game, "rest_after_lose", 0) or 0)

        type_label = (
            "Ai là quán quân"
            if getattr(game, "type_bet", "") == "winner"
            else "Ai không là quán quân"
        )

        balance_text = _fmt_cdtd_number(balance, 2) if balance is not None else "Không lấy được"
        target_text = f"Dừng khi số dư đạt {_fmt_cdtd_number(target, 2)} {coin}" if target > 0 else "Tắt"
        stop_text = f"Dừng khi số dư còn {_fmt_cdtd_number(stop, 2)} {coin}" if stop > 0 else "Tắt"
        schedule_text = _format_daily_pause_window(
            getattr(game, "daily_pause_window", None)
        )
        anti_text = f"{anti} ván rồi nghỉ 1 ván" if anti > 0 else "Tắt"
        pause_text = f"{pause} ván" if pause > 0 else "Tắt"

        config_lines = [
            "✅ CẤU HÌNH CDTD CỦA BẠN",
            "",
            f"👤 Tài khoản: {user_id}",
            f"💳 Số dư tài khoản: {balance_text} {coin}",
            f"🧠 LOGIC {game.logic_type.upper()}",
            f"💰 Cược mỗi ván: {_fmt_cdtd_number(base, 2)} {coin}",
            f"📈 Hệ số sau thua: x{mult:.2f}",
            f"🎯 Loại đặt: {type_label}",
            f"🛡️ Chống soi: {anti_text}",
            f"⏸️ Nghỉ sau thua: {pause_text}",
            f"🎯 Mục tiêu lãi: {target_text}",
            f"🛑 Cắt lỗ: {stop_text}",
            f"⏰ Khung giờ chạy: {schedule_text}",
        ]

        cprints("bright_green", "\n".join(config_lines))
        time.sleep(0.15)
        cprints("gold", "🚀 ĐANG KHỞI ĐỘNG CDTD...")
        _cdtd_run_accounts(selected_accounts, game, is_vip)

    except KeyboardInterrupt:
        cprints("yellow", "🛑 Đã hủy thao tác CDTD.")
    except Exception as exc:
        cprints("ruby", f"❌ Lỗi khởi tạo CDTD: {type(exc).__name__}: {exc}")

# ================== TOOL 4: CANH CODE XWORLD ==================

XW_API_DETAIL = "https://web3task.3games.io/v1/task/redcode/detail"
XW_API_EXCHANGE = "https://web3task.3games.io/v1/task/redcode/exchange"
XW_API_ASSET = "https://wallet.3games.io/api/wallet/user_asset"

XW_HEADERS = {
    "accept": "*/*",
    "accept-language": "vi,en;q=0.9",
    "content-type": "application/json",
    "country-code": "vn",
    "origin": "https://xworld.info",
    "referer": "https://xworld.info/",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 "
        "Mobile Safari/537.36"
    ),
    "language": "vi-VN",
}

XW_MAX_RETRIES = 3
XW_LOG_FILE = "history_nhap_code.log"
XW_ACCOUNT_FILE = "data_xw_confirm_code.txt"

def xw_clear_screen() -> None:
    None if True else os.system("cls" if platform.system() == "Windows" else "clear")

def xw_rgb_print(r: int, g: int, b: int, text: object, end: str = "\n") -> None:
    if True:
        print(_strip_rich_markup(text), end=end, flush=True)
        return
    print(f"\033[38;2;{r};{g};{b}m{text}\033[0m", end=end)

def xw_translate_message(message: object) -> str:

    text = str(message or "").strip()
    if not text:
        return "Không xác định"

    lower = text.lower()

    rules = [
        (("success", "successful", "ok", "成功", "succeeded"), "Thành công"),
        (("already received", "already claimed", "already", "received", "đã nhận", "领取", "已领取", "已領取"), "Mã code này đã được nhận rồi"),
        (("out of", "not exist", "no exist", "not found", "empty", "hết", "không còn", "不存在", "无效", "已失效"), "Mã code đã hết hoặc không còn khả dụng"),
        (("limit", "max", "exceed", "giới hạn", "上限", "达到上限", "已达上限"), "Đã đạt giới hạn tài khoản"),
        (("expired", "hết hạn", "过期", "已过期"), "Đã hết hạn"),
        (("invalid", "không hợp lệ", "无效"), "Không hợp lệ"),
        (("network", "connect", "timeout", "timed out", "connection", "socket", "mạng", "网络", "连接"), "Lỗi kết nối mạng"),
        (("forbidden", "unauthorized", "unauth", "permission", "权限", "被拒绝"), "Không có quyền truy cập"),
        (("error", "failed", "failure", "lỗi", "失败"), "Đã xảy ra lỗi"),
    ]

    for keywords, vn in rules:
        if any(k in lower for k in keywords):
            return vn

    if re.search(r"[A-Za-z一-鿿]", text):
        return "Thông báo không xác định từ máy chủ"

    return text

def xw_get_real_len(text: object):
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return len(ansi_escape.sub("", str(text)))

def xw_print_banner() -> None:
    
    xw_clear_screen()
def xw_print_square_table(title: str, headers: list[str], rows: list[list[object]]) -> None:
    normalized = [[str(cell) for cell in row] for row in rows]
    widths = []

    for i, header in enumerate(headers):
        values = [xw_get_real_len(row[i]) for row in normalized if i < len(row)]
        widths.append(max([xw_get_real_len(header), *values]) + 2)

    def border(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (w + 2) for w in widths) + right

    total_width = sum(widths) + 3 * len(widths) + 1
    title_pad = max(0, total_width - xw_get_real_len(title) - 4)
    print(f"┌─ {title} {'─' * title_pad}┐")
    print(border("├", "┬", "┤"))

    header_str = "│ " + " │ ".join(
        str(h).center(widths[i]) for i, h in enumerate(headers)
    ) + " │"
    print(Fore.CYAN + header_str + Style.RESET_ALL)
    print(border("├", "┼", "┤"))

    for row in normalized:
        padded = []
        for i, cell in enumerate(row):
            pad = widths[i] - xw_get_real_len(cell)
            padded.append(cell + " " * max(0, pad))
        print("│ " + " │ ".join(padded) + " │")

    print(border("└", "┴", "┘"))


def xw_make_session(uid: str, secret: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(XW_HEADERS)
    session.headers.update({
        "user-id": str(uid),
        "user-secret-key": str(secret),
    })
    return session

def xw_parse_account_link(link: str) -> tuple[str, str]:
    uid_match = re.search(r"[?&]userId=([^&]+)", link)
    secret_match = re.search(r"[?&]secretKey=([^&]+)", link)
    if not uid_match or not secret_match:
        raise ValueError("Link thiếu userId hoặc secretKey")
    return uid_match.group(1), secret_match.group(1)

def xw_load_accounts() -> list[dict]:
    xw_rgb_print(255, 215, 0, "🔐 QUẢN LÝ TÀI KHOẢN XWORLD")
    raw_accounts: list[tuple[str, str]] = []

    if os.path.exists(XW_ACCOUNT_FILE):
        choice = input(
            f"📁 Phát hiện {XW_ACCOUNT_FILE}. Dùng lại acc cũ? (y/n): "
        ).strip().lower()

        if choice == "y":
            with open(XW_ACCOUNT_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("|", 1)
                    if len(parts) == 2:
                        raw_accounts.append((parts[0], parts[1]))

    if not raw_accounts:
        while True:
            try:
                num = int(input("👉 Nhập số lượng acc cần chạy: ").strip())
                if num <= 0:
                    print("❌ Số lượng phải lớn hơn 0!")
                    continue
                break
            except ValueError:
                print("❌ Vui lòng nhập số hợp lệ!")

        for i in range(num):
            while True:
                xw_rgb_print(
                    255,
                    0,
                    0,
                    f"\n📌 Lấy link tài khoản thứ {i + 1} "
                    "(Vào https://xworld.info/vi-VN -> Vua thoát hiểm -> Copy link):",
                )

                link = input("👉 Dán Link vào đây: ").strip()

                try:
                    uid, secret = xw_parse_account_link(link)
                    raw_accounts.append((uid, secret))
                    print(f"✅ Thành công UID: {uid}")
                    break
                except (ValueError, IndexError):
                    print("❌ Link không hợp lệ, vui lòng nhập lại!")

        with open(XW_ACCOUNT_FILE, "w", encoding="utf-8") as f:
            for uid, secret in raw_accounts:
                f.write(f"{uid}|{secret}\n")

    accounts = []
    for uid, secret in raw_accounts:
        accounts.append({
            "uid": uid,
            "secret": secret,
            "session": xw_make_session(uid, secret),
            "status": "ACTIVE",
            "error_count": 0,
            "done_this_code": False,
        })

    return accounts

def xw_show_account_assets(accounts: list[dict]) -> None:
    xw_rgb_print(0, 255, 255, "💎 ĐANG KIỂM TRA TÀI KHOẢN...")

    def get_asset(acc: dict) -> dict:
        try:
            resp = acc["session"].post(
                XW_API_ASSET,
                json={"user_id": int(acc["uid"])},
                timeout=10,
            )
            data = resp.json()

            if data.get("code") != 0:
                return {
                    "uid": acc["uid"],
                    "BUILD": 0.0,
                    "status": "LIMIT",
                }

            asset = data.get("data", {}).get("user_asset", {})

            return {
                "uid": acc["uid"],
                "BUILD": float(asset.get("BUILD", 0.0)),
                "status": "ACTIVE",
            }

        except Exception:
            return {
                "uid": acc["uid"],
                "BUILD": 0.0,
                "status": "LIMIT",
            }

    results = []
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(accounts)))) as executor:
        futures = [executor.submit(get_asset, acc) for acc in accounts]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda x: int(x["uid"]))

    rows = []
    total_build = 0.0

    for i, item in enumerate(results, 1):
        total_build += item["BUILD"]

        status = (
            Fore.GREEN + "🟢 Hoạt động" + Style.RESET_ALL
            if item["status"] == "ACTIVE"
            else Fore.RED + "🔴 Giới hạn" + Style.RESET_ALL
        )

        rows.append([
            i,
            str(item["uid"]),      
            f"{item['BUILD']:.2f}",
            status,
        ])

    rows.append([
        "",
        "TỔNG",
        f"{total_build:.2f}",
        f"{len(results)} ACC",
    ])

    xw_print_square_table(
        "DANH SÁCH TÀI KHOẢN",
        ["STT", "UID", "BUILD", "TRẠNG THÁI"],
        rows,
    )

def xw_get_code_info(code: str, session: requests.Session) -> dict:
    payload = {"platform": "android", "channel": "h5", "app": 3, "code": code}
    for attempt in range(XW_MAX_RETRIES):
        try:
            resp = session.post(XW_API_DETAIL, json=payload, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                d = data.get("data", {})
                total = int(d.get("user_cnt", 0) or 0)
                progress = int(d.get("progress", 0) or 0)
                return {
                    "code": code,
                    "value": float(d.get("value", 0) or 0),
                    "total": total,
                    "remaining": max(0, total - progress),
                    "progress": progress,
                }
            return {"error": xw_translate_message(data.get("message", "Lỗi không xác định"))}
        except Exception:
            if attempt + 1 < XW_MAX_RETRIES:
                time.sleep(0.1)
    return {"error": "Lỗi kết nối mạng, không thể kết nối được"}

def xw_redeem_code_fast(acc: dict, code: str) -> tuple[str, str, float, str]:

    payload = {"platform": "android", "channel": "h5", "app": 3, "code": code}
    try:
        resp = acc["session"].post(XW_API_EXCHANGE, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            value = float(data.get("data", {}).get("value", 0) or 0)
            return acc["uid"], "SUCCESS", value, "OK"

        raw_msg = str(data.get("message", "")).lower()
        msg = xw_translate_message(data.get("message", ""))
        if any(x in raw_msg for x in ("received", "already", "đã nhận", "领取", "已领取", "已領取")):
            status = "ALREADY_RECEIVED"
        elif any(x in raw_msg for x in ("out of", "not exist", "hết", "empty", "不存在", "已失效")):
            status = "CODE_EMPTY"
        elif any(x in raw_msg for x in ("limit", "max", "giới hạn", "exceed", "上限", "达到上限", "已达上限")):
            status = "ACCOUNT_LIMIT"
        else:
            status = "ERROR"
        return acc["uid"], status, 0.0, msg
    except Exception as exc:
        return acc["uid"], "ERROR", 0.0, xw_translate_message(str(exc))

def xw_log_result(uid: str, code: str, threshold: int,
               build_received: float, success: bool) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "SUCCESS" if success else "FAILED"
    with open(XW_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"{timestamp}|{uid}|{code}|{threshold}|{build_received}|{status}\n"
        )

def xw_key_still_valid_or_exit() -> bool:

    is_valid, _, _ = check_activation_valid(exit_on_expired=False)
    return is_valid

def xw_watch_code(code: str, accounts: list[dict], threshold: int) -> str:
    code = str(code).strip()
    if not code:
        xw_rgb_print(255, 0, 0, "❌ Mã code không hợp lệ.")
        return "empty"

    if not accounts:
        xw_rgb_print(255, 0, 0, "❌ Không có tài khoản để canh code.")
        return "no_accounts"

    try:
        threshold = int(threshold)
    except Exception:
        threshold = 0
    threshold = max(0, threshold)

    xw_rgb_print(0, 255, 0, f"\n🚀 ĐANG CANH CODE: {code} -- NGƯỠNG: {threshold}")
    print(Fore.WHITE + "Nhấn Ctrl+C để hủy canh code này.\n" + Style.RESET_ALL)

    if not xw_key_still_valid_or_exit():
        xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
        time.sleep(1)
        return "expired"

    scan_session = requests.Session()
    scan_session.headers.update(XW_HEADERS)
    results_history: list[list[object]] = []
    bot_mode = True
    last_bot_status_ts = 0.0

    for acc in accounts:
        acc["done_this_code"] = False
        acc["error_count"] = 0

    code_empty = False
    stop_reason = "normal"

    try:
        while True:
            if not xw_key_still_valid_or_exit():
                xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
                stop_reason = "expired"
                break

            try:
                info = xw_get_code_info(code, scan_session)
            except KeyboardInterrupt:
                stop_reason = "interrupt"
                break

            if "error" in info:
                if bot_mode:
                    now_ts = time.time()
                    if now_ts - last_bot_status_ts >= 5.0:
                        last_bot_status_ts = now_ts
                        xw_rgb_print(255, 80, 80, f"⚠️ Lỗi kiểm tra code: {xw_translate_message(info['error'])} — đang thử lại...")
                else:
                    sys.stdout.write(
                        f"\r{Fore.RED}⚠️ Lỗi kiểm tra code: {xw_translate_message(info['error'])} - Thử lại..."
                        f"{Style.RESET_ALL}        "
                    )
                    sys.stdout.flush()
                time.sleep(1)
                continue

            remaining = int(info.get("remaining", 0))
            current_time = datetime.now().strftime("%H:%M:%S")

            if bot_mode:
                now_ts = time.time()
                if now_ts - last_bot_status_ts >= 5.0 or remaining <= threshold:
                    last_bot_status_ts = now_ts
                    xw_rgb_print(255, 215, 0, f"🕒 [{current_time}] Lượt còn lại: {remaining} / {info.get('total', 0)} (Đang chờ mốc {threshold})")
            else:
                sys.stdout.write(
                    f"\r🕒 [{current_time}] Lượt còn lại: {remaining} / {info.get('total', 0)} "
                    f"(Đang chờ mốc {threshold})        "
                )
                sys.stdout.flush()

            if remaining > threshold:
                time.sleep(1)
                continue

            if not xw_key_still_valid_or_exit():
                stop_reason = "expired"
                break

            print(
                f"\n\n⚡ ĐÃ ĐẠT NGƯỠNG {threshold}! "
                "ĐỔI CODE LIÊN TỤC ĐẾN KHI HẾT CODE HOẶC ĐẠT GIỚI HẠN..."
            )

            active_accs = [
                acc for acc in accounts
                if acc.get("status") == "ACTIVE"
                and not acc.get("done_this_code")
                and acc.get("error_count", 0) < 3
            ]

            if not active_accs:
                print("\n⚠️ Tất cả tài khoản đã nhận, đạt giới hạn hoặc lỗi quá nhiều.")
                stop_reason = "empty"
                break

            executor = ThreadPoolExecutor(max_workers=min(10, len(active_accs)))
            try:
                futures = {
                    executor.submit(xw_redeem_code_fast, acc, code): acc
                    for acc in active_accs
                }

                pending = set(futures.keys())
                stop_now = False

                while pending:
                    if not xw_key_still_valid_or_exit():
                        stop_reason = "expired"
                        stop_now = True
                        break

                    try:
                        done, pending = wait(
                            pending,
                            timeout=0.2,
                            return_when=FIRST_COMPLETED,
                        )
                    except KeyboardInterrupt:
                        stop_reason = "interrupt"
                        stop_now = True
                        break

                    if not done:
                        continue

                    for future in done:
                        acc = futures[future]
                        try:
                            uid, status, build, msg = future.result()
                        except KeyboardInterrupt:
                            stop_reason = "interrupt"
                            stop_now = True
                            break
                        except Exception as exc:
                            uid, status, build, msg = "UNKNOWN", "ERROR", 0.0, str(exc)

                        if status == "SUCCESS":
                            acc["done_this_code"] = True
                            status_text = Fore.GREEN + "THÀNH CÔNG" + Style.RESET_ALL
                            xw_log_result(uid, code, threshold, build, True)

                        elif status == "ALREADY_RECEIVED":
                            acc["done_this_code"] = True
                            status_text = Fore.YELLOW + "ĐÃ NHẬN" + Style.RESET_ALL

                        elif status == "ACCOUNT_LIMIT":
                            acc["status"] = "LIMIT"
                            status_text = Fore.MAGENTA + "GIỚI HẠN" + Style.RESET_ALL
                            xw_log_result(uid, code, threshold, 0.0, False)

                        elif status == "CODE_EMPTY":
                            code_empty = True
                            stop_reason = "empty"
                            status_text = Fore.RED + "CODE ĐÃ HẾT" + Style.RESET_ALL
                            stop_now = True

                        else:
                            acc["error_count"] = acc.get("error_count", 0) + 1
                            status_text = Fore.RED + "LỖI" + Style.RESET_ALL

                        results_history.append([
                            str(uid),
                            status_text,
                            f"{build:.2f}",
                            msg,
                        ])

                        if stop_now:
                            break

                    if stop_now:
                        break

            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if results_history:
                xw_print_square_table(
                    "KẾT QUẢ NHẬP CODE",
                    ["UID", "Trạng thái", "BUILD", "Thông báo"],
                    results_history[-20:],
                )

            if code_empty:
                print("\n❌ Code đã hết lượt hoặc không còn khả dụng.")
                break

            if stop_reason in ("interrupt", "expired"):
                break

            time.sleep(0.1)

    finally:
        try:
            scan_session.close()
        except Exception:
            pass

    if stop_reason == "interrupt":
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(Fore.YELLOW + "🛑 Đã hủy canh code." + Style.RESET_ALL)
    elif stop_reason == "expired":
        print(Fore.YELLOW + "\n🔙 Đã quay về menu." + Style.RESET_ALL)
    else:
        print(Fore.GREEN + "\n👋 Đã thoát canh code." + Style.RESET_ALL)

    return stop_reason

def xw_main() -> None:
    start_key_expiry_monitor()
    bot_mode = True
    is_valid, reason, is_vip = check_activation_valid(exit_on_expired=False)

    if not is_valid:
        if "HẾT HẠN" in str(reason).upper() or "RESET" in str(reason).upper():
            xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
        else:
            xw_rgb_print(255, 0, 0, f"❌ Không thể vào XWORLD: {xw_translate_message(reason)}")
        time.sleep(1.5)
        return

    if not is_vip:
        xw_rgb_print(255, 0, 0, "❌ CANH CODE chỉ dành cho key VIP.")
        xw_rgb_print(255, 215, 0, "🔑 Hãy kích hoạt key VIP để sử dụng chức năng này.")
        time.sleep(2)
        return

    xw_print_banner()

    accounts = xw_load_accounts()
    if not accounts:
        xw_rgb_print(255, 0, 0, "❌ Không có tài khoản nào để chạy.")
        return

    if bot_mode:
        xw_rgb_print(0, 255, 0, f"✅ Đã tải {len(accounts)} tài khoản XWORLD.")

    temp_session = requests.Session()
    temp_session.headers.update(XW_HEADERS)
    xw_show_account_assets(accounts)

    while True:
        is_valid, reason, _ = check_activation_valid(exit_on_expired=False)
        if not is_valid:
            if "HẾT HẠN" in str(reason).upper() or "RESET" in str(reason).upper():
                xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
            else:
                xw_rgb_print(255, 0, 0, f"❌ Không thể tiếp tục: {xw_translate_message(reason)}")
            time.sleep(1)
            return

        try:
            print()
            code = input(
                Fore.CYAN + "🎁 Nhập mã code cần canh (Nhập 0 để thoát tool): "
                + Style.RESET_ALL
            ).strip()
        except KeyboardInterrupt:
            print(Fore.RED + "🛑 Đã hủy nhập mã code." + Style.RESET_ALL)
            return

        is_valid, reason, _ = check_activation_valid(exit_on_expired=False)
        if not is_valid:
            if "HẾT HẠN" in str(reason).upper() or "RESET" in str(reason).upper():
                xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
            else:
                xw_rgb_print(255, 0, 0, f"❌ Không thể tiếp tục: {xw_translate_message(reason)}")
            time.sleep(1)
            return

        if code == "0":
            xw_rgb_print(0, 255, 0, "👋 Đã dừng tool. Hẹn gặp lại!")
            return

        if not code:
            continue

        xw_rgb_print(255, 215, 0, f">> Đang kiểm tra mã code: {code} ...")
        info = xw_get_code_info(code, temp_session)

        if "error" in info:
            print(
                Fore.RED
                + f"❌ Lỗi kiểm tra mã code: {xw_translate_message(info['error'])}"
                + Style.RESET_ALL
            )
            continue

        xw_print_square_table(
            "THÔNG TIN MÃ CODE",
            ["Mã Code", "Giá trị (BUILD)", "Tổng lượt", "Lượt còn lại"],
            [[
                info.get("code", code),
                f"{info.get('value', 0):.2f}",
                info.get("total", 0),
                info.get("remaining", 0)
            ]],
        )

        try:
            choice = input(
                ">> Bạn có muốn canh mã code này không? (y/n): "
            ).strip().lower()
        except KeyboardInterrupt:
            print(Fore.RED + "🛑 Đã hủy nhập mã code." + Style.RESET_ALL)
            return

        if not check_activation_valid(exit_on_expired=False)[0]:
            xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
            time.sleep(1)
            return

        if choice != "y":
            continue

        try:
            threshold = int(input(
                "🎯 Nhập ngưỡng (số lượt còn lại để tự động kích hoạt nhập): "
            ).strip())
        except KeyboardInterrupt:
            print(Fore.RED + "🛑 Đã hủy nhập ngưỡng." + Style.RESET_ALL)
            continue
        except ValueError:
            print("❌ Ngưỡng phải là số nguyên!")
            continue

        if not check_activation_valid(exit_on_expired=False)[0]:
            xw_rgb_print(255, 215, 0, "🔙 Key đã hết hạn, quay về menu chính.")
            time.sleep(1)
            return

        try:
            result = xw_watch_code(code, accounts, threshold)
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            sys.stdout.flush()
            print(Fore.YELLOW + "🛑 Đã hủy canh code." + Style.RESET_ALL)
            continue

        if result == "interrupt":
            continue

        if result == "empty":
            xw_rgb_print(0, 255, 0, "👋 Mã code đã hết hoặc không còn khả dụng.")
            continue

        if result == "expired":
            return

        continue

# ================== HILO TOOL  ==================

C_GREEN = '\033[92m'
C_RED = '\033[91m'
C_YELLOW = '\033[93m'
C_CYAN = '\033[96m'
C_WHITE = '\033[97m'
C_NEON_PINK = '\033[95m'
C_RESET = '\033[0m'
C_DIM = '\033[2m'

def hilo_show_temp_error(message, seconds=3):

    if True:
        print(_strip_rich_markup(message), flush=True)
        return

    lines = str(message).splitlines() or [""]
    print("\n".join(lines), flush=True)
    time.sleep(seconds)
    for index in range(len(lines)):
        print("\033[2K\r", end="")
        if index < len(lines) - 1:
            print("\033[1A", end="")
    print("\r", end="", flush=True)
def hilo_get_device_fingerprint(identifier):
    hash_id = int(hashlib.md5(str(identifier).encode()).hexdigest(), 16)
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
    ]
    return uas[hash_id % len(uas)]

def hilo_build_secure_session(user_agent, proxy_url=None, token=None, apikey=None):
    session = requests.Session()
    is_mobile = "Mobile" in user_agent
    
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": user_agent,
        "Origin": "https://hilo.turbogg4u.online",
        "Referer": "https://hilo.turbogg4u.online/",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua-Mobile": "?1" if is_mobile else "?0",
        "Sec-Ch-Ua-Platform": "\"Android\"" if "Android" in user_agent else ("\"iOS\"" if "iPhone" in user_agent else "\"Windows\"")
    }
    
    if token:
        headers["authorization"] = token
    if apikey:
        headers["apikey"] = apikey
        headers["metadata"] = json.dumps({"device": "mobile" if is_mobile else "desktop", "manual": True})
        headers["subpartnerid"] = ""
        
    session.headers.update(headers)
    
    retry_strategy = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=50, pool_maxsize=50)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
        
    return session

def hilo__response_json(response, endpoint="API"):
    """Kiểm tra HTTP và parse JSON an toàn."""
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        preview = (response.text or "")[:300].replace("\n", " ")
        raise RuntimeError(f"{endpoint} trả về JSON không hợp lệ: {preview}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{endpoint} trả về kiểu dữ liệu không hợp lệ: {type(data).__name__}")
    return data


def hilo__extract_card(value, fallback=None):

    if value is None:
        return fallback

    if isinstance(value, dict):

        for key in (
            "card", "value", "rank", "number", "point",
            "cardValue", "cardRank", "currentCard",
            "drawnCard", "newCard", "resultCard", "initialCard",
        ):
            if value.get(key) is not None:
                found = hilo__extract_card(value.get(key), None)
                if found is not None:
                    return found

        for key in ("data", "result", "game", "round", "state", "payload"):
            if value.get(key) is not None:
                found = hilo__extract_card(value.get(key), None)
                if found is not None:
                    return found

        return fallback

    if isinstance(value, list):
        for item in value:
            found = hilo__extract_card(item, None)
            if found is not None:
                return found
        return fallback

    if isinstance(value, bool):
        return fallback

    if isinstance(value, (int, float)):
        value = int(value)
        return value if 1 <= value <= 13 else fallback

    text = str(value).strip()
    if not text:
        return fallback

    upper = text.upper()
    face_to_number = {"A": 1, "J": 11, "Q": 12, "K": 13}
    if upper in face_to_number:
        return face_to_number[upper]

    try:
        number = int(text)
        return number if 1 <= number <= 13 else fallback
    except ValueError:
        return fallback


def hilo__get_game_card(payload, fallback=None):

    card = hilo__extract_card(payload, None)
    if card is not None:
        return card
    return hilo__extract_card(fallback, None)


def hilo_get_card_value(card):

    normalized = hilo__extract_card(card)
    try:
        value = int(normalized)
    except (TypeError, ValueError):
        return 7

    return value if 1 <= value <= 13 else 7


def hilo_action_label(action):

    labels = {
        "hi": "HI",
        "lo": "LO",
    }
    normalized = str(action).lower()
    return labels.get(normalized, normalized.upper())


def hilo_compare_card_result(previous_card, drawn_card):

    if previous_card is None or drawn_card is None:
        return None
    try:
        previous_value = hilo_get_card_value(previous_card)
        drawn_value = hilo_get_card_value(drawn_card)
    except Exception:
        return None

    if drawn_value > previous_value:
        return "CAO"
    if drawn_value < previous_value:
        return "NHỎ"
    return "BẰNG"


# =========================
# LOGIC FREE / VIP
# =========================

def hilo__clean_history(history, limit=60):

    if not history:
        return []

    values = []
    for card in history[-limit:]:
        try:
            value = hilo_get_card_value(card)
        except Exception:
            continue

        if not 1 <= value <= 13:
            continue

        # API/display dùng 1 cho A, nhưng A là rank cao nhất khi phân tích.
        values.append(14 if value == 1 else value)

    return values


def hilo__outcome_between(previous_value, next_value):
    """Quan hệ giữa 2 lá: hi / lo / draw."""
    if next_value > previous_value:
        return "hi"
    if next_value < previous_value:
        return "lo"
    return "draw"


def hilo__direction_counts(values):
    """Đếm nhẹ hướng CAO/NHỎ/BẰNG trong chuỗi lịch sử."""
    hi = lo = draw = 0
    for previous_value, next_value in zip(values, values[1:]):
        outcome = hilo__outcome_between(previous_value, next_value)
        if outcome == "hi":
            hi += 1
        elif outcome == "lo":
            lo += 1
        else:
            draw += 1
    return hi, lo, draw


def hilo__weighted_direction_score(values, decay=0.82):
    """
    Điểm xu hướng theo thời gian.
    Mẫu càng gần hiện tại có trọng số càng lớn.
    Trả về >0 thiên CAO, <0 thiên NHỎ.
    """
    pairs = list(zip(values, values[1:]))
    if not pairs:
        return 0.0

    score = 0.0
    weight = 1.0
    total_weight = 0.0

    for previous_value, next_value in reversed(pairs):
        outcome = hilo__outcome_between(previous_value, next_value)
        if outcome == "hi":
            score += weight
        elif outcome == "lo":
            score -= weight
        # BẰNG = 0, không ép hướng.
        total_weight += weight
        weight *= decay

    if total_weight <= 0:
        return 0.0
    return score / total_weight


def hilo__streak_score(values, max_streak=5):
    """
    Tạo tín hiệu rất nhỏ từ chuỗi liên tiếp gần nhất.
    Không coi streak là quy luật chắc chắn; chỉ dùng như tín hiệu phụ.
    """
    pairs = list(zip(values, values[1:]))
    if not pairs:
        return 0.0

    last_outcome = hilo__outcome_between(*pairs[-1])
    if last_outcome == "draw":
        return 0.0

    streak = 0
    for previous_value, next_value in reversed(pairs):
        outcome = hilo__outcome_between(previous_value, next_value)
        if outcome != last_outcome:
            break
        streak += 1
        if streak >= max_streak:
            break

    # Streak quá dài được giảm trọng số để tránh đuổi cầu.
    strength = min(streak / max_streak, 1.0)
    return strength if last_outcome == "hi" else -strength


def hilo__markov_score(values):
    """
    Ước lượng chuyển trạng thái CAO/NHỎ từ lịch sử bằng Laplace smoothing.
    Chỉ dùng khi có đủ dữ liệu; tránh phản ứng mạnh với vài ván đầu.
    """
    pairs = list(zip(values, values[1:]))
    if len(pairs) < 5:
        return 0.0

    transitions = {
        "hi": {"hi": 1.0, "lo": 1.0},
        "lo": {"hi": 1.0, "lo": 1.0},
    }

    last_direction = None
    for previous_value, next_value in pairs:
        current_direction = hilo__outcome_between(previous_value, next_value)
        if current_direction not in ("hi", "lo"):
            continue
        if last_direction in transitions:
            transitions[last_direction][current_direction] += 1.0
        last_direction = current_direction

    if last_direction not in transitions:
        return 0.0

    row = transitions[last_direction]
    total = row["hi"] + row["lo"]
    p_hi = row["hi"] / total
    return (p_hi - 0.5) * 2.0


def hilo__current_card_edge(current_card):
    """Lợi thế cơ bản theo đúng thứ tự Hilo: 2 < ... < K < A.

    Có 13 hạng; bỏ trường hợp BẰNG khi chuẩn hóa xác suất hướng.
    Với rank v (2..14):
      cao hơn = 14-v
      thấp hơn = v-2
    """
    value = hilo_get_card_value(current_card)
    rank_value = 14 if value == 1 else value
    hi = 14 - rank_value
    lo = rank_value - 2
    total_non_draw = hi + lo

    if total_non_draw <= 0:
        return 0.0

    return (hi - lo) / total_non_draw


def hilo__recent_balance_score(values, window=10):
    """
    Tín hiệu cân bằng rất nhẹ.
    Không giả định rằng 'thiếu CAO' bắt buộc phải xuất hiện tiếp theo.
    Chỉ phản ánh mức lệch ngắn hạn để giảm quyết định quá cực đoan.
    """
    recent = values[-window:]
    if not recent:
        return 0.0

    hi = sum(1 for value in recent if value >= 8)
    lo = sum(1 for value in recent if value <= 6)
    active = hi + lo

    if active == 0:
        return 0.0

    imbalance = (hi - lo) / active
    return -imbalance * 0.25


def hilo__resolve_direction(hi_score, lo_score, fallback="hi"):
    """Chọn hướng ổn định, tránh trả None."""
    if hi_score > lo_score:
        return "hi"
    if lo_score > hi_score:
        return "lo"
    return fallback


def hilo_predict_action_basic(current_card):
    """Điểm nền theo đúng thứ tự Hilo: 2 < ... < K < A."""
    value = hilo_get_card_value(current_card)
    rank_value = 14 if value == 1 else value
    if rank_value >= 14:   # A lớn nhất
        return "lo"
    if rank_value <= 2:    # 2 nhỏ nhất
        return "hi"
    if rank_value <= 6:
        return "hi"
    if rank_value >= 8:
        return "lo"
    # 7 là vùng cân bằng; fallback CAO.
    return "hi"


def hilo_predict_action_free(current_card, history=None):
    """
    LOGIC FREE - đa tín hiệu, nhẹ và ổn định.

    Trọng số:
      60% lợi thế từ lá hiện tại
      25% xu hướng có trọng số theo thời gian
      10% cân bằng gần đây
       5% streak

    Mục tiêu là tốt hơn logic một điều kiện nhưng vẫn tránh "đuổi cầu".
    """
    values = hilo__clean_history(history, limit=30)

    current_edge = hilo__current_card_edge(current_card)
    trend = hilo__weighted_direction_score(values, decay=0.78)
    balance = hilo__recent_balance_score(values, window=8)
    streak = hilo__streak_score(values, max_streak=4)

    total_score = (
        0.60 * current_edge
        + 0.25 * trend
        + 0.10 * balance
        + 0.05 * streak
    )

    # Lá 8 là tâm đối xứng của dải 2..A khi xét 2 hướng CAO/NHỎ.
    # Gần vùng cân bằng thì dùng lịch sử làm tie-break.
    if abs(current_edge) < 0.01 and abs(total_score) < 0.08:
        if values:
            hi_count, lo_count, _ = hilo__direction_counts(values[-12:])
            if hi_count != lo_count:
                return "lo" if hi_count < lo_count else "hi"
        return "hi"

    return "hi" if total_score >= 0 else "lo"


def hilo_predict_action_vip(current_card, history=None):

    values = hilo__clean_history(history, limit=60)

    current_edge = hilo__current_card_edge(current_card)
    weighted_trend = hilo__weighted_direction_score(values, decay=0.88)
    short_trend = hilo__weighted_direction_score(values[-12:], decay=0.72)
    markov = hilo__markov_score(values)
    balance = hilo__recent_balance_score(values, window=12)
    streak = hilo__streak_score(values, max_streak=5)

    medium_shift = 0.0
    if len(values) >= 12:
        first = values[-12:-6]
        second = values[-6:]
        first_score = hilo__weighted_direction_score(first, decay=0.85)
        second_score = hilo__weighted_direction_score(second, decay=0.85)
        medium_shift = max(-1.0, min(1.0, second_score - first_score))

    n = len(values)
    if n < 6:

        hi_score = max(current_edge, 0.0)
        lo_score = max(-current_edge, 0.0)
        return hilo__resolve_direction(hi_score, lo_score, hilo_predict_action_basic(current_card))

    history_factor = min(1.0, n / 20.0)

    history_score = (
        0.28 * markov
        + 0.26 * short_trend
        + 0.16 * weighted_trend
        + 0.12 * balance
        + 0.10 * streak
        + 0.08 * medium_shift
    )

    combined_score = (
        (0.40 * current_edge)
        + (0.60 * history_factor * history_score)
    )

    # Không để lịch sử lấn át hoàn toàn lợi thế của lá hiện tại.
    current_weight = 0.55 if abs(current_edge) >= 0.50 else 0.45
    combined_score = (
        current_weight * current_edge
        + (1.0 - current_weight) * history_factor * history_score
    )

    # Vùng quá cân bằng: dùng current-card edge làm quyết định cuối,
    # giúp VIP không trả về None và không đảo hướng liên tục.
    if abs(combined_score) < 0.035:
        return hilo_predict_action_basic(current_card)

    return "hi" if combined_score > 0 else "lo"

def hilo_select_analysis_logic():
    """Chọn logic HILO theo loại key đang kích hoạt.

    KEY FREE: chỉ được phép chọn LOGIC FREE.
    KEY VIP: được phép chọn LOGIC FREE hoặc LOGIC VIP.
    """
    try:
        is_valid, reason, is_vip = check_activation_valid(exit_on_expired=False)
    except Exception as exc:
        is_valid, reason, is_vip = False, str(exc), False

    if not is_valid:
        console.print(f"[bold red]❌ Key không hợp lệ: {reason}[/bold red]")
        return None

    print(f"\n{C_NEON_PINK}========== CHỌN LOGIC HILO ================{C_RESET}")
    print(f"{C_GREEN}1. LOGIC FREE{C_RESET}")
    if is_vip:
        print(f"{C_RED}2. LOGIC VIP{C_RESET}")
    else:
        print(f"{C_DIM}2. LOGIC VIP (chỉ Key VIP){C_RESET}")
    print(f"{C_NEON_PINK}============================================{C_RESET}")

    while True:
        choice = input(f"{C_WHITE}👉 Chọn Logic (1/2): {C_RESET}").strip()

        # Kiểm tra lại sau lúc nhập: không cho chọn LOGIC FREE nếu key vừa hết hạn.
        try:
            is_valid, reason, is_vip = check_activation_valid(exit_on_expired=False)
        except Exception as exc:
            is_valid, reason, is_vip = False, str(exc), False
        if not is_valid:
            console.print(
                f"[bold red]🔒 {reason}. Vui lòng nhập lại Key FREE hoặc Key VIP.[/bold red]"
            )
            return None

        if choice == "1":
            print(f"{C_GREEN}✅ Đã chọn HILO LOGIC FREE{C_RESET}")
            return "free"

        if choice == "2":
            if is_vip:
                print(f"{C_RED}✅ Đã chọn HILO LOGIC VIP{C_RESET}")
                return "vip"

            hilo_show_temp_error(f"{C_RED}❌ LOGIC VIP chỉ dành cho Key VIP.{C_RESET}")
            continue

        hilo_show_temp_error(f"{C_RED}❌ Chỉ được chọn 1 hoặc 2.{C_RESET}")

def hilo_get_multiline_json():
    print(f"{C_WHITE}👉 Dán Payload JSON vào đây:{C_RESET}")

    while True:
        raw = input().strip()

        if not raw:
            hilo_show_temp_error(f"{C_YELLOW}⚠️ JSON đang trống. Vui lòng dán lại.{C_RESET}")
            continue

        try:
            payload = json.loads(raw)

            if not isinstance(payload, dict):
                hilo_show_temp_error(f"{C_RED}JSON hợp lệ nhưng phải là một object/dict.{C_RESET}")
                continue

            return payload

        except json.JSONDecodeError as e:
            hilo_show_temp_error(
                f"{C_RED}❌ JSON không hợp lệ. "
                f"Vui lòng dán lại và bấm Enter.{C_RESET}"
            )

        except Exception as e:
            hilo_show_temp_error(f"{C_RED}❌ Lỗi khi đọc JSON. Vui lòng kiểm tra lại dữ liệu.{C_RESET}")

def hilo__prompt_number(prompt, cast=float, min_value=None, allow_zero=True):

    while True:
        raw = input(prompt).strip()
        try:
            value = cast(raw)
            if isinstance(value, float) and not __import__('math').isfinite(value):
                raise ValueError("giá trị phải là số hữu hạn")
            if min_value is not None and value < min_value:
                hilo_show_temp_error(f"{C_RED}❌ Giá trị phải >= {min_value}. Vui lòng nhập lại.{C_RESET}")
                continue
            if not allow_zero and value == 0:
                hilo_show_temp_error(f"{C_RED}❌ Giá trị không được bằng 0. Vui lòng nhập lại.{C_RESET}")
                continue
            return value
        except (ValueError, TypeError):
            hilo_show_temp_error(f"{C_RED}❌ Nhập sai định dạng. Vui lòng nhập lại.{C_RESET}")


def hilo_setup_config():
    None if True else os.system('clear' if os.name == 'posix' else 'cls')

    while True:
        use_proxy = input(f"{C_WHITE}Bạn có muốn sử dụng Proxy không? (y/n): {C_RESET}").strip().lower()
        if use_proxy in ('y', 'n'):
            break
        hilo_show_temp_error(f"{C_RED}❌ Chỉ được nhập y hoặc n. Vui lòng nhập lại.{C_RESET}")

    proxy_url = None
    if use_proxy == 'y':
        while True:
            proxy_url = input(f"{C_WHITE} Nhập Proxy (VD: http://user:pass@ip:port): {C_RESET}").strip()
            if not proxy_url:
                hilo_show_temp_error(f"{C_RED}❌ Proxy đang trống. Vui lòng nhập lại.{C_RESET}")
                continue

            print(f"{C_DIM}>> Đang kiểm tra Proxy...{C_RESET}")
            try:
                test_sess = requests.Session()
                test_sess.proxies.update({"http": proxy_url, "https": proxy_url})
                test_sess.get("https://hilo.turbogg4u.online", timeout=10)
                print(f"{C_GREEN}>> Proxy hoạt động tốt.{C_RESET}")
                break
            except Exception:
                hilo_show_temp_error(f"{C_RED}❌ Proxy lỗi. Vui lòng kiểm tra lại Proxy.{C_RESET}")
                while True:
                    retry_proxy = input(f"{C_WHITE} Bạn có muốn thử lại Proxy không? (y/n): {C_RESET}").strip().lower()
                    if retry_proxy in ('y', 'n'):
                        break
                    hilo_show_temp_error(f"{C_RED}❌ Chỉ được nhập y hoặc n. Vui lòng nhập lại.{C_RESET}")
                if retry_proxy == 'n':
                    proxy_url = None
                    print(f"{C_YELLOW} Tiếp tục không sử dụng Proxy.{C_RESET}")
                    break

    login_payload = hilo_get_multiline_json()
    identifier = "termux_user"
    user_agent = hilo_get_device_fingerprint(identifier)

    print(f"{C_DIM}>> Đang xác thực tài khoản...{C_RESET}")
    auth_session = hilo_build_secure_session(user_agent, proxy_url)

    while True:
        try:
            response = auth_session.post(
                "https://hilo.turbogg4u.online/api/common/profile",
                json=login_payload,
                timeout=(10, 15)
            )
            res = hilo__response_json(response, "PROFILE")

            if 'id' in res and 'token' in res:
                apikey = res['id']
                token = res['token']
                currency = res.get('currency', 'bld')
                balance = float(res.get('balance', 0))
                name = res.get('playerName', 'Unknown')
                print(f"{C_GREEN}✅ Đăng nhập thành công!{C_RESET}")
                print(f"{C_WHITE}👤 Tài khoản: {name}{C_RESET}")
                print(f"{C_WHITE}💰 Số dư: {balance} {currency}{C_RESET}\n")
                break
            else:
                hilo_show_temp_error(f"{C_RED}❌ Không thể xác thực. Vui lòng kiểm tra lại Payload JSON.{C_RESET}")

        except Exception as e:
            hilo_show_temp_error(f"{C_RED}❌ Lỗi kết nối/xác thực. Vui lòng thử lại.{C_RESET}")

        login_payload = hilo_get_multiline_json()
        print(f"{C_DIM}>> Đang xác thực lại tài khoản...{C_RESET}")

    # Chọn LOGIC theo menu HILO cũ; KEY FREE bị khóa LOGIC VIP.
    analysis_logic = hilo_select_analysis_logic()
    if analysis_logic not in ("free", "vip"):
        raise RuntimeError("Không xác định được quyền LOGIC từ KEY hiện tại.")

    base_bet = hilo__prompt_number(f"{C_WHITE}1. Số BUILD cược mỗi ván: {C_RESET}", float, min_value=0, allow_zero=False)
    target_profit = hilo__prompt_number(f"{C_WHITE}2. Nhập số BUILD muốn lãi: {C_RESET}", float, min_value=0)
    stop_loss = hilo__prompt_number(f"{C_WHITE}3. Ngưỡng cắt lỗ: {C_RESET}", float, min_value=0)
    loss_multiplier = hilo__prompt_number(f"{C_WHITE}4. Hệ số gấp thếp khi thua: {C_RESET}", float, min_value=0, allow_zero=False)
    target_opens = hilo__prompt_number(f"{C_WHITE}5. Số lá mở an toàn tối đa 1 ván: {C_RESET}", int, min_value=1)
    delay_bet = hilo__prompt_number(f"{C_WHITE}6. Delay đánh bài (giây): {C_RESET}", float, min_value=0)
    delay_win = hilo__prompt_number(f"{C_WHITE}7. Delay sau khi thắng (giây): {C_RESET}", float, min_value=0)
    delay_loss = hilo__prompt_number(f"{C_WHITE}8. Delay sau khi thua (giây): {C_RESET}", float, min_value=0)

    return {
        'proxy_url': proxy_url,
        'login_payload': login_payload,
        'analysis_logic': analysis_logic,
        'apikey': apikey,
        'token': token,
        'currency': currency,
        'original_balance': balance,
        'current_balance': balance,
        'player_name': name,
        'base_bet': base_bet,
        'target_profit': target_profit,
        'stop_loss': stop_loss,
        'loss_multiplier': loss_multiplier,
        'target_opens': target_opens,
        'delay_bet': delay_bet,
        'delay_win': delay_win,
        'delay_loss': delay_loss,
        'user_agent': user_agent
    }

# ================== GIAO DIỆN TOOL HILO ==================

HILO_COLORS = VBTOOL_COLORS
HILO_ICONS = ICONS
HILO_LOGO = LOGO_VBTOOL


def hilo_make_vbtool_header() -> Group:
    return Group()


def hilo_make_vbtool_compact_panel(state: Dict) -> Panel:
    """Panel HILO tương thích với bộ renderer bot-only hiện tại."""
    adapted = dict(state or {})
    balance = adapted.get("balance", 0.0)
    original_balance = adapted.get("original_balance", balance)
    adapted["balances"] = {"BUILD": {"balance": balance}, "XWORLD": {"balance": 0.0}}
    adapted["start_balances"] = {"BUILD": {"balance": original_balance}}
    adapted["next_prediction"] = adapted.get("next_prediction", "—")
    adapted["next_confidence"] = adapted.get("next_confidence", 0.0)
    adapted["three_number_analysis"] = adapted.get("three_number_analysis", {})
    adapted["bet_stats"] = adapted.get("bet_stats")
    return make_vbtool_compact_panel(adapted)


def hilo_make_vbtool_history_cards(history, limit=5, highlight_issue=None) -> Panel:
    """Lịch sử HILO riêng, không phụ thuộc cấu trúc lịch sử LOTTO."""
    recent = list(reversed((history or [])[-limit:]))
    if not recent:
        return Panel(Text("Chưa có lịch sử", style="dim"), border_style=VBTOOL_COLORS["history_blue"])

    cards = []
    for rec in recent:
        issue = rec.get("issue_id", "—")
        ts = str(rec.get("timestamp", ""))
        short_ts = ts[11:19] if len(ts) >= 19 else ts
        current_card = hilo__hilo_rank_label(rec.get("current_card"))
        drawn_card = hilo__hilo_rank_label(rec.get("drawn_card"))
        result = str(rec.get("result", "—"))
        profit = rec.get("profit")
        try:
            profit_text = f"{float(profit):+.2f}" if profit is not None else "—"
        except (TypeError, ValueError):
            profit_text = str(profit) if profit is not None else "—"

        result_style = f"bold {VBTOOL_COLORS['emerald']}" if result.upper() in ("THẮNG", "WIN") else (
            f"bold {VBTOOL_COLORS['ruby']}" if result.upper() in ("THUA", "LOSS") else VBTOOL_COLORS["gold"])
        line = Text()
        line.append(f"#{str(issue)[-8:]} ", style=f"bold {VBTOOL_COLORS['history_blue']}")
        if short_ts:
            line.append(f"{short_ts}  ", style="dim")
        line.append(f"{current_card} → {drawn_card}  ", style=VBTOOL_COLORS["platinum"])
        line.append(result, style=result_style)
        line.append(f"  {profit_text}", style=result_style if profit is not None else "dim")
        cards.append(Panel(line, border_style=VBTOOL_COLORS["gold"] if issue == highlight_issue else VBTOOL_COLORS["onyx"], padding=(0, 1)))

    return Panel(Columns(cards, equal=True, expand=True), title="LỊCH SỬ HILO", border_style=VBTOOL_COLORS["history_blue"], box=box.ROUNDED)


def hilo__hilo_rank_label(card):

    try:
        normalized = hilo__extract_card(card, None)
        if normalized is None:
            return "—"

        if isinstance(normalized, str):
            upper = normalized.strip().upper()
            face_to_number = {"A": 1, "J": 11, "Q": 12, "K": 13}
            if upper in face_to_number:
                return str(face_to_number[upper])
            return str(int(upper)) if upper.isdigit() else upper
        value = int(normalized)
        return str(value)
    except Exception:
        return str(card) if card is not None else "—"

def hilo_make_vbtool_layout(state: Dict) -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=8),
        Layout(name="hilo_main", ratio=1),
        Layout(name="history", size=12),
    )
    layout["header"].update(hilo_make_vbtool_header())
    layout["hilo_main"].update(hilo_make_vbtool_compact_panel(state))
    layout["history"].update(hilo_make_vbtool_history_cards(
        state.get("history", []), limit=5, highlight_issue=state.get("last_issue")
    ))
    return layout


def hilo__history_append(state, issue, current_card, drawn_card, result, profit):
    state["history"].append({
        "issue_id": issue,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "current_card": current_card,
        "drawn_card": drawn_card,
        "result": result,
        "profit": profit,
    })
    if len(state["history"]) > 30:
        del state["history"][:-30]


def hilo_main():
    cfg = hilo_setup_config()
    session = hilo_build_secure_session(
        cfg['user_agent'],
        cfg['proxy_url'],
        cfg['token'],
        cfg['apikey']
    )

    current_bet = cfg['base_bet']
    last_result = 'none'
    analysis_history = []
    stats = {"predictions": 0, "wins": 0, "losses": 0}

    state = {
        "account_id": cfg.get("player_name") or cfg.get("login_payload", {}).get("username", "—"),
        "balance": cfg.get("current_balance", 0.0),
        "original_balance": cfg.get("original_balance", 0.0),
        "last_issue": "—",
        "last_result": "CHƯA CÓ",
        "current_card": None,
        "next_prediction": "—",
        "current_bet": current_bet,
        "history": [],
        "model_stats": stats,
        "log_text": "Sẵn sàng ✅",
    }

    stop_reason = None

    live_context = (
        _HiloBotLive(state)
        if True
        else Live(hilo_make_vbtool_layout(state), console=console, refresh_per_second=8, screen=True)
    )

    with live_context as live:
        while True:
            if last_result == 'win':
                time.sleep(cfg['delay_win'])
            elif last_result == 'loss':
                time.sleep(cfg['delay_loss'])
            else:
                time.sleep(0.5)

            state["current_bet"] = current_bet
            state["balance"] = cfg["current_balance"]
            state["log_text"] = "Đang chuẩn bị ván mới..."
            live.update(hilo_make_vbtool_layout(state))

            if current_bet > cfg['current_balance']:
                if cfg['current_balance'] >= cfg['base_bet']:
                    current_bet = cfg['base_bet']
                else:
                    state["log_text"] = "Số dư không đủ để cược. Dừng tool!"
                    live.update(hilo_make_vbtool_layout(state))
                    if True:
                        print("⚠️ Số dư không đủ để đặt cược. HILO đã dừng.", flush=True)
                    break

            initial_card = random.randint(1, 13)
            create_payload = {
                "clientSeed": str(uuid.uuid4()),
                "nonce": int(time.time() * 1000) % 100000,
                "initialCard": initial_card,
            }

            try:
                response = session.post(
                    "https://hilo.turbogg4u.online/api/games/create",
                    json=create_payload,
                    timeout=(10, 30)
                )
                res_create = hilo__response_json(response, "CREATE")
                if 'roundId' not in res_create:
                    state["log_text"] = f"Đang kết nối lại"
                    live.update(hilo_make_vbtool_layout(state))
                    time.sleep(2)
                    continue

                round_id = res_create['roundId']
                actual_card = hilo__get_game_card(
                    res_create,
                    create_payload.get('initialCard')
                )
                current_play_card = actual_card
                state["last_issue"] = round_id
                state["current_card"] = actual_card
                state["next_prediction"] = "—"
                state["last_result"] = "ĐANG CHẠY"
                state["log_text"] = f"Ván #{round_id} | Bài gốc: {hilo__hilo_rank_label(actual_card)}"
                live.update(hilo_make_vbtool_layout(state))

            except Exception as exc:
                _notify_admin_code_error("HILO CREATE", exc, chat_id=str(_WORKER_CHAT_ID or ""), tool_key="hilo")
                state["log_text"] = "Lỗi tạo ván. Vui lòng thử lại!"
                live.update(hilo_make_vbtool_layout(state))
                time.sleep(3)
                continue

            game_active = True
            opened_cards = 0
            while game_active:
                time.sleep(cfg['delay_bet'])

                # Kiểm tra key hiện tại để bảo đảm LOGIC VIP không bị dùng bởi KEY FREE.
                try:
                    key_valid, key_reason, key_is_vip = check_activation_valid(exit_on_expired=False)
                except Exception as exc:
                    key_valid, key_reason, key_is_vip = False, str(exc), False

                if not key_valid:
                    state["log_text"] = f"KEY không hợp lệ: {key_reason}"
                    live.update(hilo_make_vbtool_layout(state))
                    game_active = False
                    break

                # Giữ nguyên logic mà người dùng đã chọn trong menu HILO cũ.
                runtime_logic = str(cfg.get("analysis_logic", "free")).lower()
                if runtime_logic not in ("free", "vip"):
                    runtime_logic = "free"

                # KEY FREE tuyệt đối không được chạy LOGIC VIP.
                if runtime_logic == "vip" and not key_is_vip:
                    state["log_text"] = "❌ LOGIC VIP chỉ dành cho Key VIP."
                    live.update(hilo_make_vbtool_layout(state))
                    game_active = False
                    break

                if runtime_logic == "vip":
                    action = hilo_predict_action_vip(current_play_card, analysis_history)
                else:
                    action = hilo_predict_action_free(current_play_card, analysis_history)
                action_text = hilo_action_label(action)

                stats["predictions"] += 1
                state["model_stats"] = stats
                state["next_prediction"] = action_text
                state["current_bet"] = current_bet
                state["log_text"] = f"Phân tích: {hilo__hilo_rank_label(current_play_card)} → {action_text}"
                live.update(hilo_make_vbtool_layout(state))

                place_payload = {
                    "action": action,
                    "roundId": round_id,
                    "theme": "default",
                    "currency": cfg['currency'],
                    "amount": current_bet
                }

                try:
                    response = session.post(
                        "https://hilo.turbogg4u.online/api/bets/place",
                        json=place_payload,
                        timeout=(10, 15)
                    )
                    res_place = hilo__response_json(response, "PLACE")
                    if 'status' not in res_place:
                        state["log_text"] = "Ván kết thúc."
                        live.update(hilo_make_vbtool_layout(state))
                        game_active = False
                        continue
                except requests.exceptions.ReadTimeout:
                    state["log_text"] = "Kết nối không ổn định."
                    live.update(hilo_make_vbtool_layout(state))
                    game_active = False
                    last_result = 'none'
                    time.sleep(3)
                    continue
                except requests.exceptions.RequestException:
                    state["log_text"] = "Lỗi mạng khi đặt cược. Ván kết thúc."
                    live.update(hilo_make_vbtool_layout(state))
                    game_active = False
                    last_result = 'none'
                    time.sleep(3)
                    continue
                except Exception as exc:
                    _notify_admin_code_error("HILO PLACE", exc, chat_id=str(_WORKER_CHAT_ID or ""), tool_key="hilo")
                    state["log_text"] = "Lỗi đặt cược. Ván kết thúc."
                    live.update(hilo_make_vbtool_layout(state))
                    game_active = False
                    last_result = 'none'
                    time.sleep(3)
                    continue

                status = res_place.get('status')
                drawn_card = hilo__get_game_card(res_place)

                try:
                    if res_place.get('balance') is not None:
                        cfg['current_balance'] = float(res_place['balance'])
                        state['balance'] = cfg['current_balance']
                except (TypeError, ValueError):
                    pass

                if status == 0:

                    relation = hilo_compare_card_result(current_play_card, drawn_card)
                    state["last_result"] = f"THUA ({relation})" if relation else "THUA"
                    stats["losses"] += 1
                    stats["predictions"] = max(stats["predictions"], stats["wins"] + stats["losses"])
                    state["model_stats"] = stats
                    hilo__history_append(state, round_id, current_play_card, drawn_card, "THUA", -float(current_bet))
                    state["balance"] = cfg['current_balance']
                    drawn_label = hilo__hilo_rank_label(drawn_card) if drawn_card is not None else "—"
                    if relation:
                        state["log_text"] = f"💀 THUA | {hilo__hilo_rank_label(current_play_card)} → {drawn_label} ({relation})"
                    else:
                        state["log_text"] = f"💀 THUA | {hilo__hilo_rank_label(current_play_card)} → {drawn_label}"
                    live.update(hilo_make_vbtool_layout(state))
                    last_result = 'loss'
                    try:
                        profile_response = session.post(
                            "https://hilo.turbogg4u.online/api/common/profile",
                            json=cfg['login_payload'], timeout=(10, 10)
                        )
                        profile = hilo__response_json(profile_response, "PROFILE")
                        cfg['current_balance'] = float(profile.get('balance', cfg['current_balance']))
                        state['balance'] = cfg['current_balance']
                    except Exception:
                        pass
                    current_bet *= cfg['loss_multiplier']
                    state["current_bet"] = current_bet
                    game_active = False

                elif status == 1:
                    opened_cards += 1
                    next_coeffs = res_place.get('coefficients', {}).get('next', {})
                    previous_card = current_play_card
                    relation = hilo_compare_card_result(previous_card, drawn_card)
                    if drawn_card is not None:
                        current_play_card = drawn_card
                        state["current_card"] = drawn_card
                        analysis_history.append(drawn_card)
                        if len(analysis_history) > 100:
                            analysis_history = analysis_history[-100:]
                    state["last_result"] = f"MỞ {opened_cards}"
                    state["log_text"] = f"TIẾP TỤC | {action_text} → {hilo__hilo_rank_label(drawn_card)} ({relation or '—'})"
                    live.update(hilo_make_vbtool_layout(state))

                    if not next_coeffs or opened_cards >= cfg['target_opens']:
                        cashout_payload = {"roundId": round_id}
                        try:
                            response = session.post(
                                "https://hilo.turbogg4u.online/api/bets/cashout",
                                json=cashout_payload,
                                timeout=(10, 15)
                            )
                            res_cashout = hilo__response_json(response, "CASHOUT")
                            payout = float(res_cashout.get('payout', 0.0))

                            raw_multiplier = (
                                res_cashout.get('multiplier')
                                or res_cashout.get('coefficient')
                                or res_cashout.get('coefficients', {}).get('current')
                                if isinstance(res_cashout.get('coefficients', {}), dict)
                                else None
                            )
                            try:
                                win_multiplier = float(raw_multiplier) if raw_multiplier is not None else (payout / current_bet if current_bet else 0.0)
                            except (TypeError, ValueError, ZeroDivisionError):
                                win_multiplier = payout / current_bet if current_bet else 0.0
                            last_result = 'win'
                            stats["wins"] += 1
                            stats["predictions"] = max(stats["predictions"], stats["wins"] + stats["losses"])
                            state["model_stats"] = stats
                            try:
                                profile_response = session.post(
                                    "https://hilo.turbogg4u.online/api/common/profile",
                                    json=cfg['login_payload'], timeout=(10, 10)
                                )
                                profile = hilo__response_json(profile_response, "PROFILE")
                                cfg['current_balance'] = float(profile.get('balance', cfg['current_balance']))
                            except Exception:
                                pass
                            state['balance'] = cfg['current_balance']
                            profit_now = cfg['current_balance'] - cfg['original_balance']
                            hilo__history_append(state, round_id, actual_card, current_play_card, "THẮNG", payout - current_bet)
                            state["last_result"] = "THẮNG"
                            state["log_text"] = f"✅ CHỐT LỜI | Nhân: x{win_multiplier:.2f} | Nhận: {payout:.2f} | Lãi/Lỗ: {profit_now:+.2f}"
                            current_bet = cfg['base_bet']
                            state["current_bet"] = current_bet
                            live.update(hilo_make_vbtool_layout(state))
                            game_active = False
                        except Exception as exc:
                            _notify_admin_code_error("HILO CASHOUT", exc, chat_id=str(_WORKER_CHAT_ID or ""), tool_key="hilo")
                            state["log_text"] = "Lỗi chốt lời. Ván kết thúc."
                            live.update(hilo_make_vbtool_layout(state))
                            game_active = False
                else:
                    state["last_result"] = "KẾT THÚC"
                    state["log_text"] = f"không thể đặt cược"
                    live.update(hilo_make_vbtool_layout(state))
                    last_result = 'none'
                    game_active = False

            state["balance"] = cfg['current_balance']
            state["current_bet"] = current_bet
            current_profit = cfg['current_balance'] - cfg['original_balance']

            if cfg['target_profit'] > 0 and current_profit >= cfg['target_profit']:
                stop_reason = f"🎉 ĐÃ ĐẠT CHỈ TIÊU CHỐT LỜI (+{current_profit:.2f}). DỪNG TOOL!"
                state["log_text"] = stop_reason
                state["last_result"] = "CHỐT LỜI"
                live.update(hilo_make_vbtool_layout(state))
                break

            if cfg['stop_loss'] > 0 and current_profit <= -cfg['stop_loss']:
                stop_reason = f"💀 ĐÃ ĐẠT NGƯỠNG CẮT LỖ ({current_profit:.2f}). DỪNG TOOL AN TOÀN!"
                state["log_text"] = stop_reason
                state["last_result"] = "CẮT LỖ"
                live.update(hilo_make_vbtool_layout(state))
                break

    if stop_reason:
        if True:
            print(stop_reason, flush=True)
        else:
            console.print("\n")
            if "CHỐT LỜI" in stop_reason:
                console.print(
                    Panel(
                        Text(stop_reason, justify="center"),
                        title="[bold green]🎉 CHỐT LỜI[/bold green]",
                        border_style=VBTOOL_COLORS["emerald"],
                        box=box.DOUBLE,
                        padding=(1, 3),
                        expand=False,
                    )
                )
            else:
                console.print(
                    Panel(
                        Text(stop_reason, justify="center"),
                        title="[bold red]💀 CẮT LỖ[/bold red]",
                        border_style=VBTOOL_COLORS["ruby"],
                        box=box.DOUBLE,
                        padding=(1, 3),
                        expand=False,
                    )
                )

        time.sleep(3)
        return

# ================== PHẦN 5: MAIN ==================
def main():
    
    return bot_main()

from collections import defaultdict
from typing import Dict, List, Sequence, Any

ATHLETES = tuple(range(1, 7))

def _sequence(history: Sequence[dict]) -> List[int]:
    out: List[int] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        value = item.get("top1")
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value in ATHLETES:
            out.append(value)
    return out


def _delay(seq: List[int], athlete: int) -> int:
    for i, value in enumerate(seq):
        if value == athlete:
            return i
    return len(seq) + 3


def _streak(seq: List[int], athlete: int) -> int:
    count = 0
    for value in seq:
        if value == athlete:
            count += 1
        else:
            break
    return count


def _weighted_recent(seq: List[int], athlete: int, limit: int = 40, decay: float = 0.92) -> float:
    score = 0.0
    weight = 1.0
    for value in seq[:limit]:
        if value == athlete:
            score += weight
        weight *= decay
    return score


def _transition(seq: List[int], previous: int, athlete: int, order: int = 1) -> float:
    if len(seq) <= order:
        return 0.0
    hits = total = 0
    if order == 1:
        for i in range(len(seq) - 1):
            if seq[i] == previous:
                total += 1
                if seq[i + 1] == athlete:
                    hits += 1
    else:
        for i in range(len(seq) - 2):
            if seq[i] == previous and seq[i + 1] == seq[0]:
                total += 1
                if seq[i + 2] == athlete:
                    hits += 1
    return hits / total if total else 0.0


def _avg_gap(seq: List[int], athlete: int):
    positions = [i for i, value in enumerate(seq) if value == athlete]
    if len(positions) < 2:
        return None
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    return sum(gaps) / len(gaps)


def _base_score(seq: List[int], athlete: int, type_bet: str, mode: str) -> float:
    total = len(seq)
    if not total:
        return 50.0

    rate = seq.count(athlete) / total
    delay = _delay(seq, athlete)
    streak = _streak(seq, athlete)
    recent = _weighted_recent(seq, athlete)
    count10 = sum(1 for value in seq[:10] if value == athlete)
    score = 50.0

    # Tần suất: winner thích mức xuất hiện quanh trung bình; not_winner ưu tiên thấp.
    if type_bet == "winner":
        score += (1.0 / 6.0 - rate) * 35.0
        score += min(delay * 0.55, 14.0)
        if delay >= 12:
            score += 7.0
        if streak:
            score += min(streak * 2.5, 8.0)
        score += recent * 2.0
        if count10 == 0:
            score += 5.0
        elif count10 >= 4:
            score -= 8.0
    else:
        score += (1.0 - rate) * 24.0
        score += min(delay * 0.65, 18.0)
        if delay <= 1:
            score -= 10.0
        if streak:
            score -= min(streak * 6.0, 18.0)
        score -= recent * 2.8
        if count10 == 0:
            score += 8.0
        elif count10 >= 4:
            score -= 10.0

    if len(seq) >= 2:
        previous = seq[0]
        trans = _transition(seq, previous, athlete, 1)
        score += trans * (24.0 if type_bet == "winner" else -20.0)

    if len(seq) >= 3:
        trans2 = _transition(seq, seq[1], athlete, 2)
        score += trans2 * (14.0 if type_bet == "winner" else -10.0)

    gap = _avg_gap(seq, athlete)
    if gap:
        if delay >= gap * 1.35:
            score += 7.0
        elif delay <= gap * 0.55:
            score -= 4.0

    # VIP dùng cửa sổ dài hơn, nhưng vẫn chỉ là điểm bổ sung.
    if mode == "vip":
        recent20 = sum(1 for value in seq[:20] if value == athlete)
        recent60 = sum(1 for value in seq[:60] if value == athlete)
        if type_bet == "winner":
            score += (recent20 * 0.8) + (recent60 * 0.25)
        else:
            score -= (recent20 * 0.55) + (recent60 * 0.18)

    return max(0.0, min(100.0, score))


def analyze_cdtd_ai(history: Sequence[dict], type_bet: str = "winner", mode: str = "free") -> Dict[str, Any]:
    """Trả về điểm P1..P6 và danh sách xếp hạng.

    type_bet: winner / not_winner
    mode: free / vip
    """
    type_bet = "not_winner" if str(type_bet).lower() == "not_winner" else "winner"
    mode = "vip" if str(mode).lower() == "vip" else "free"
    seq = _sequence(history)

    scores = {athlete: _base_score(seq, athlete, type_bet, mode) for athlete in ATHLETES}
    ranked = sorted(ATHLETES, key=lambda athlete: (-scores[athlete], athlete))

    return {
        "module": "CDTD-AI",
        "mode": mode,
        "type_bet": type_bet,
        "history_size": len(seq),
        "scores": scores,
        "ranking": ranked,
        "selected_1": ranked[0] if ranked else None,
    }
# ===== END CDTD AI MODULE (EMBEDDED) =====

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":

        run_worker(str(sys.argv[2]).strip().lower(), sys.argv[3] if len(sys.argv) >= 4 else None)
    else:
        _start_render_health_server()
        bot_main()