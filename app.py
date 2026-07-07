from flask import Flask, request, jsonify
import json
import os
import secrets
import requests
from datetime import datetime, timedelta

ACCESS_TOKEN = "0279df46764ce5c01c748d86ca2e46d0a11c3e01a21d7bbc597b6b06e346"

def create_telegraph_page(title, html):
    r = requests.post(
        "https://api.telegra.ph/createPage",
        data={
            "access_token": ACCESS_TOKEN,
            "title": title,
            "author_name": "VB TOOL",
            "content": f'[{{"tag":"p","children":["{html}"]}}]',
            "return_content": False
        }
    )

    data = r.json()

    if data.get("ok"):
        return "https://telegra.ph/" + data["result"]["path"]

    return None

app = Flask(__name__)

DB_FILE = "keys.json"


def load_keys():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_keys(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        
@app.route("/")
def home():
    return "VB TOOL KEY SERVER ONLINE"

@app.route("/api/create_free_key", methods=["POST"])
def create_free_key():
    data = request.get_json()

    device_id = data.get("device_id")

    if not device_id:
        return jsonify({
            "success": False,
            "message": "Missing device_id"
        })

    keys = load_keys()

    # Nếu thiết bị đã có key FREE thì trả lại key cũ
    for key, info in keys.items():
        if info.get("device_id") == device_id and info.get("key_type") == "FREE":
            telegraph = create_telegraph_page(
            "VB TOOL FREE KEY",
            f"Device ID: {device_id}\n\nKey: {key}"
        )

            return jsonify({
                "success": True,
                "link": telegraph,
                "duration": info["duration"],
                "message": "Key already exists"
            })

    # Sinh key mới
    while True:
        key = secrets.token_hex(4).upper()
        if key not in keys:
            break

    expire = datetime.now() + timedelta(days=1)

    keys[key] = {
        "device_id": device_id,
        "key_type": "FREE",
        "duration": 24,
        "expire": expire.isoformat(),
        "used": False
    }

    save_keys(keys)

    telegraph = create_telegraph_page(
        "VB TOOL FREE KEY",
        f"Device ID: {device_id}\n\nKey: {key}"
    )

    return jsonify({
        "success": True,
        "link": telegraph,
        "duration": 24
    })
    
@app.route("/api/verify_key", methods=["POST"])
def verify_key():

    data = request.get_json()

    key = data.get("key", "").strip().upper()
    device_id = data.get("device_id", "").strip()

    keys = load_keys()

    if key not in keys:
        return jsonify({
            "success": False,
            "message": "Invalid key"
        })

    info = keys[key]

    # Kiểm tra Device ID
    if info["device_id"] != device_id:
        return jsonify({
            "success": False,
            "message": "Wrong device"
        })

    # Kiểm tra hết hạn
    expire = datetime.fromisoformat(info["expire"])

    if datetime.now() > expire:
        return jsonify({
            "success": False,
            "message": "Key expired"
        })

    return jsonify({
        "success": True,
        "duration": info["duration"],
        "key_type": info["key_type"],
        "is_forever": False
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
