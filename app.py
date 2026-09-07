from flask import Flask, jsonify
import requests

app = Flask(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@app.get("/")
def home():
    return jsonify(
        status="ok",
        message="Kalshi bridge is running"
    )


def kalshi_get(path, params=None):
    r = requests.get(
        f"{KALSHI_BASE}{path}",
        params=params,
        timeout=20
    )
    r.raise_for_status()
    return r.json()


def get_event_markets(event_ticker):
    data = kalshi_get(
        "/markets",
        params={
            "event_ticker": event_ticker,
            "limit": 1000
        }
    )
    return data.get("markets", [])


def compact_market(m):
    return {
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "title": m.get("title"),
        "subtitle": m.get("subtitle"),
        "yes_sub_title": m.get("yes_sub_title"),
        "no_sub_title": m.get("no_sub_title"),
        "yes_bid": m.get("yes_bid"),
        "yes_ask": m.get("yes_ask"),
        "no_bid": m.get("no_bid"),
        "no_ask": m.get("no_ask"),
        "last_price": m.get("last_price"),
        "volume": m.get("volume"),
        "open_interest": m.get("open_interest"),
        "close_time": m.get("close_time")
    }


@app.get("/market/<ticker>")
def market(ticker):
    try:
        data = kalshi_get(f"/markets/{ticker}")
        return jsonify(data)

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502


@app.get("/smu-today")
def smu_today():
    try:
        game_event = "KXNCAAFGAME-26SEP07SMUFSU"
        spread_event = "KXNCAAFSPREAD-26SEP07SMUFSU"
        total_event = "KXNCAAFTOTAL-26SEP07SMUFSU"

        game_winner = get_event_markets(game_event)
        spreads = get_event_markets(spread_event)
        totals = get_event_markets(total_event)

        return jsonify(
            matchup="SMU vs Florida State",
            date="2026-09-07",
            game_winner=[
                compact_market(m) for m in game_winner
            ],
            spread=[
                compact_market(m) for m in spreads
            ],
            total=[
                compact_market(m) for m in totals
            ]
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502
