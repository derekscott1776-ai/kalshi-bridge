from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@app.get("/")
def home():
    return jsonify(
        status="ok",
        message="Kalshi bridge is running"
    )


@app.get("/market/<ticker>")
def market(ticker):
    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets/{ticker}",
            timeout=15
        )
        return (
            r.text,
            r.status_code,
            {"Content-Type": "application/json"}
        )
    except requests.RequestException as e:
        return jsonify(error=str(e)), 502


@app.get("/markets")
def markets():
    params = {}

    limit = request.args.get("limit", "100")
    cursor = request.args.get("cursor")
    status = request.args.get("status")

    params["limit"] = limit

    if cursor:
        params["cursor"] = cursor

    if status:
        params["status"] = status

    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params=params,
            timeout=15
        )
        return (
            r.text,
            r.status_code,
            {"Content-Type": "application/json"}
        )
    except requests.RequestException as e:
        return jsonify(error=str(e)), 502


@app.get("/search")
def search():
    query = request.args.get("q", "").strip().lower()

    if not query:
        return jsonify(
            error="Add a search term using ?q=example"
        ), 400

    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params={
                "limit": 1000,
                "status": "open"
            },
            timeout=20
        )
        r.raise_for_status()
        data = r.json()

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502

    markets_list = data.get("markets", [])
    matches = []

    for market in markets_list:
        searchable = " ".join([
            str(market.get("title", "")),
            str(market.get("subtitle", "")),
            str(market.get("ticker", "")),
            str(market.get("event_ticker", ""))
        ]).lower()

        if query in searchable:
            matches.append(market)

    return jsonify(
        query=query,
        count=len(matches),
        markets=matches
    )
