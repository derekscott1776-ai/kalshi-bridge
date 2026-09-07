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


def get_markets(max_pages=10):
    markets = []
    cursor = None

    for _ in range(max_pages):
        params = {
            "limit": 1000,
            "status": "open"
        }

        if cursor:
            params["cursor"] = cursor

        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params=params,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()
        markets.extend(data.get("markets", []))

        cursor = data.get("cursor")

        if not cursor:
            break

    return markets


def basic_text(market):
    fields = [
        market.get("ticker", ""),
        market.get("event_ticker", ""),
        market.get("title", ""),
        market.get("subtitle", ""),
        market.get("yes_sub_title", ""),
        market.get("no_sub_title", "")
    ]

    return " ".join(str(x) for x in fields).lower()


@app.get("/search")
def search():
    query = request.args.get("q", "").strip()

    if not query:
        return jsonify(error="Use /search?q=SMU"), 400

    try:
        markets = get_markets(max_pages=10)
        q = query.lower()

        matches = [
            m for m in markets
            if q in basic_text(m)
        ]

        return jsonify(
            query=query,
            count=len(matches),
            markets=matches[:100]
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502


@app.get("/smu-debug")
def smu_debug():
    try:
        markets = get_markets(max_pages=10)
        matches = []

        for m in markets:
            ticker = m.get("ticker") or ""
            event_ticker = m.get("event_ticker") or ""
            text = basic_text(m)

            if "smu" not in text:
                continue

            if ticker.startswith("KXMVE") or event_ticker.startswith("KXMVE"):
                continue

            matches.append({
                "ticker": ticker,
                "event_ticker": event_ticker,
                "title": m.get("title"),
                "subtitle": m.get("subtitle"),
                "yes_sub_title": m.get("yes_sub_title"),
                "no_sub_title": m.get("no_sub_title"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "no_bid": m.get("no_bid"),
                "no_ask": m.get("no_ask"),
                "last_price": m.get("last_price"),
                "open_time": m.get("open_time"),
                "close_time": m.get("close_time"),
                "expiration_time": m.get("expiration_time")
            })

        return jsonify(
            count=len(matches),
            markets=matches[:100]
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502
