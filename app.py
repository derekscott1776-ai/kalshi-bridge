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
    params = {
        "limit": request.args.get("limit", "100")
    }

    cursor = request.args.get("cursor")
    status = request.args.get("status")

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

    matches = []
    cursor = None
    pages_checked = 0
    max_pages = 50

    try:
        while pages_checked < max_pages:
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
            markets_list = data.get("markets", [])

            for market in markets_list:
                searchable = " ".join([
                    str(market.get("title", "")),
                    str(market.get("subtitle", "")),
                    str(market.get("ticker", "")),
                    str(market.get("event_ticker", "")),
                    str(market.get("yes_sub_title", "")),
                    str(market.get("no_sub_title", ""))
                ]).lower()

                if query in searchable:
                    matches.append(market)

            pages_checked += 1
            cursor = data.get("cursor")

            if not cursor:
                break

        return jsonify(
            query=query,
            count=len(matches),
            pages_checked=pages_checked,
            markets=matches
        )

    except requests.RequestException as e:
        return jsonify(
            error=str(e),
            pages_checked=pages_checked
        ), 502
