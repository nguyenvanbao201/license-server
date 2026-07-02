from flask import Flask, request, jsonify
import random
import string

app = Flask(__name__)


def generate_key():
    chars = string.ascii_uppercase + string.digits
    return "LOTTO-" + "-".join(
        "".join(random.choice(chars) for _ in range(4))
        for _ in range(4)
    )
    
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "online",
        "version": "1.0"
    })    

@app.route("/")
def home():
    return "VB TOOL KEY SERVER ONLINE"


@app.route("/verify-key", methods=["POST"])
def verify_key():
    data = request.get_json()

    if not data:
        return jsonify({
            "status": "error",
            "message": "No data"
        }), 400

    key = data.get("key", "").strip()

    # Key dùng để test
    if key == "TEST-KEY":
        return jsonify({
            "status": "success"
        }), 200

    return jsonify({
        "status": "invalid"
    }), 401


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)