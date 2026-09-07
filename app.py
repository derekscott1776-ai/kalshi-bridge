from flask import Flask, jsonify, request
import requests
from datetime import datetime, timezone

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
            "limit": 1000
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


def searchable_text(market):
    fields = [
        market.get("ticker", ""),
        market.get("event_ticker", ""),
        market.get("title", ""),
        market.get("subtitle", ""),
        market.get("yes_sub_title", ""),
        market.get("no_sub_title", ""),
        market.get("rules_primary", ""),
        market.get("custom_strike", {})
    ]

    return " ".join(str(x) for x in fields).lower()


@app.get("/search")
def search():
    query = request.args.get("q", "").strip()

    if not query:
        return jsonify(
            error="Use /search?q=SMU"
        ), 400

    try:
        markets = get_markets(max_pages=5)
        q = query.lower()

        matches = [
            market for market in markets
            if q in searchable_text(market)
        ]

        return jsonify(
            query=query,
            count=len(matches),
            pages_checked=5,
            markets=matches
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502


@app.get("/smu-debug")
def smu_debug():
    try:
        markets = get_markets(max_pages=10)
        matches = []

        for m in markets:
            text = searchable_text(m)

            if "smu" in text:
                matches.append({
                    "ticker": m.get("ticker"),
                    "event_ticker": m.get("event_ticker"),
                    "title": m.get("title"),
                    "subtitle": m.get("subtitle"),
                    "open_time": m.get("open_time"),
                    "close_time": m.get("close_time"),
                    "expiration_time": m.get("expiration_time")
                })

        return jsonify(
            count=len(matches),
            markets=matches[:50]
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502

@app.get("/smu-today")
def smu_today():
    try:
        markets = get_markets(max_pages=10)

        today = datetime.now(timezone.utc).date()

        smu = []

        for market in markets:
            text = searchable_text(market)

            if "smu" not in text:
                continue

            close_time = market.get("close_time", "")

            try:
                market_date = datetime.fromisoformat(
                    close_time.replace("Z", "+00:00")
                ).date()
            except (ValueError, TypeError):
                continue

            if market_date != today:
                continue

            smu.append(market)

        winner = []
        spread = []
        total = []

        for market in smu:
            text = searchable_text(market)

            if any(x in text for x in [
                "spread",
                "point spread",
                "margin"
            ]):
                spread.append(market)

            elif any(x in text for x in [
                "total",
                "over",
                "under",
                "points scored"
            ]):
                total.append(market)

            else:
                winner.append(market)

        def compact(m):
            return {
                "ticker": m.get("ticker"),
                "event_ticker": m.get("event_ticker"),
                "title": m.get("title"),
                "subtitle": m.get("subtitle"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "no_bid": m.get("no_bid"),
                "no_ask": m.get("no_ask"),
                "last_price": m.get("last_price"),
                "close_time": m.get("close_time")
            }

        return jsonify(
            date=str(today),
            team="SMU",
            game_winner=[compact(x) for x in winner],
            spread=[compact(x) for x in spread],
            total=[compact(x) for x in total]
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502
