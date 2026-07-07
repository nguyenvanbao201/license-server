from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "VB TOOL KEY SERVER ONLINE"

if __name__ == "__main__":
    app.run(debug=True)
