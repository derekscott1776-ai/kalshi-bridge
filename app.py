from flask import Flask, jsonify
import requests

app = Flask(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

@app.get("/")
def home():
    return jsonify(status="ok", message="Kalshi bridge is running")

@app.get("/market/<ticker>")
def market(ticker):
    r = requests.get(f"{KALSHI_BASE}/markets/{ticker}", timeout=10)
    return (r.text, r.status_code, {"Content-Type": "application/json"})
