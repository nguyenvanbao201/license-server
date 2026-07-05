from flask import Flask, request, jsonify
from datetime import datetime
import json
import os

app = Flask(__name__)

KEY_FILE = "keys.json"


def load_keys():
    if not os.path.exists(KEY_FILE):
        return {}

    with open(KEY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_keys(data):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


@app.route("/api/verify_key", methods=["POST"])
def verify_key():
    data = request.json

    device = data.get("device_id", "")
    key = data.get("key", "").upper().strip()

    keys = load_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "message": "Key không tồn tại"
        })

    info = keys[key]

    expire = datetime.fromisoformat(info["expire"])

    if datetime.now() > expire:
        return jsonify({
            "success": False,
            "message": "Key hết hạn"
        })

    if info["used"]:
        return jsonify({
            "success": False,
            "message": "Key đã sử dụng"
        })

    info["used"] = True
    info["device"] = device
    info["time"] = datetime.now().isoformat()

    save_keys(keys)

    duration = 24

    if info["type"] == "VIP":
        duration = 240

    return jsonify({
        "success": True,
        "key_type": info["type"],
        "duration": duration,
        "is_forever": False
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
