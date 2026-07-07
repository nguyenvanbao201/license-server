from flask import Flask, request, jsonify
import json
import os

app = Flask(__name__)

KEY_FILE = "keys.json"


def load_keys():
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f:
            json.dump({}, f)

    with open(KEY_FILE, "r") as f:
        return json.load(f)


def save_keys(data):
    with open(KEY_FILE, "w") as f:
        json.dump(data, f, indent=4)


@app.route("/")
def home():
    return "VB TOOL KEY SERVER ONLINE"


@app.route("/api/verify_key", methods=["POST"])
def verify():
    data = request.json

    key = data.get("key")
    device = data.get("device_id")

    keys = load_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "message": "Key không tồn tại"
        })

    info = keys[key]

    if not info["used"]:
        info["used"] = True
        info["device"] = device
        save_keys(keys)

    elif info["device"] != device:
        return jsonify({
            "success": False,
            "message": "Key đã dùng trên thiết bị khác"
        })

    return jsonify({
        "success": True,
        "duration": info["duration"],
        "key_type": info["type"]
    })


if __name__ == "__main__":
    app.run()
