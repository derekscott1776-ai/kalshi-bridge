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
    r = requests.get(
        f"{KALSHI_BASE}/markets/{ticker}",
        timeout=10
    )

    return (
        r.text,
        r.status_code,
        {"Content-Type": "application/json"}
    )


@app.get("/markets")
def markets():
    params = {
        "limit": request.args.get("limit", 100),
        "status": request.args.get("status", "open")
    }

    cursor = request.args.get("cursor")
    if cursor:
        params["cursor"] = cursor

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


@app.get("/search")
def search():
    query = request.args.get("q", "").strip().lower()

    if not query:
        return jsonify(
            error="Use ?q= followed by a search term"
        ), 400

    r = requests.get(
        f"{KALSHI_BASE}/markets",
        params={
            "limit": 1000,
            "status": "open"
        },
        timeout=15
    )

    if not r.ok:
        return (
            r.text,
            r.status_code,
            {"Content-Type": "application/json"}
        )

    markets = r.json().get("markets", [])

    matches = []

    for market in markets:
        searchable = (
            market.get("title", "") + " " +
            market.get("subtitle", "") + " " +
            market.get("ticker", "") + " " +
            market.get("event_ticker", "")
        ).lower()

        if query in searchable:
            matches.append(market)

    return jsonify(
        query=query,
        count=len(matches),
        markets=matches
    )
