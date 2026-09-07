from flask import Flask, jsonify, request
import requests
from datetime import datetime

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

        "yes_bid_dollars": m.get("yes_bid_dollars"),
        "yes_ask_dollars": m.get("yes_ask_dollars"),
        "no_bid_dollars": m.get("no_bid_dollars"),
        "no_ask_dollars": m.get("no_ask_dollars"),
        "last_price_dollars": m.get("last_price_dollars"),

        "volume_fp": m.get("volume_fp"),
        "volume_24h_fp": m.get("volume_24h_fp"),
        "open_interest_fp": m.get("open_interest_fp"),

        "close_time": m.get("close_time")
    }


def normalize(text):
    return " ".join(
        str(text or "").lower().replace("-", " ").split()
    )


def market_text(m):
    return normalize(
        " ".join([
            str(m.get("ticker", "")),
            str(m.get("event_ticker", "")),
            str(m.get("title", "")),
            str(m.get("subtitle", "")),
            str(m.get("yes_sub_title", "")),
            str(m.get("no_sub_title", ""))
        ])
    )


def discover_game_events(team, opponent, game_date):
    date_obj = datetime.strptime(game_date, "%Y-%m-%d")
    date_code = date_obj.strftime("%y%b%d").upper()

    prefixes = [
        "KXNCAAFGAME",
        "KXNCAAFSPREAD",
        "KXNCAAFTOTAL"
    ]

    found = {
        "game": None,
        "spread": None,
        "total": None
    }

    team_q = normalize(team)
    opponent_q = normalize(opponent)

    for prefix in prefixes:
        cursor = None

        for _ in range(10):
            params = {
                "series_ticker": prefix,
                "limit": 1000
            }

            if cursor:
                params["cursor"] = cursor

            data = kalshi_get("/markets", params=params)

            for m in data.get("markets", []):
                text = market_text(m)
                ticker = str(m.get("ticker", "")).upper()

                if date_code not in ticker:
                    continue

                if team_q not in text:
                    continue

                if opponent_q not in text:
                    continue

                event_ticker = m.get("event_ticker")

                if prefix == "KXNCAAFGAME":
                    found["game"] = event_ticker
                elif prefix == "KXNCAAFSPREAD":
                    found["spread"] = event_ticker
                elif prefix == "KXNCAAFTOTAL":
                    found["total"] = event_ticker

                break

            if (
                (prefix == "KXNCAAFGAME" and found["game"]) or
                (prefix == "KXNCAAFSPREAD" and found["spread"]) or
                (prefix == "KXNCAAFTOTAL" and found["total"])
            ):
                break

            cursor = data.get("cursor")

            if not cursor:
                break

    return found


@app.get("/market/<ticker>")
def market(ticker):
    try:
        data = kalshi_get(f"/markets/{ticker}")
        return jsonify(data)

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502


@app.get("/game")
def game():
    team = request.args.get("team", "").strip()
    opponent = request.args.get("opponent", "").strip()
    game_date = request.args.get("date", "").strip()

    if not team or not opponent or not game_date:
        return jsonify(
            error="Use /game?team=SMU&opponent=Florida%20State&date=2026-09-07"
        ), 400

    try:
        datetime.strptime(game_date, "%Y-%m-%d")
    except ValueError:
        return jsonify(
            error="Date must use YYYY-MM-DD format"
        ), 400

    try:
        events = discover_game_events(
            team,
            opponent,
            game_date
        )

        game_winner = (
            get_event_markets(events["game"])
            if events["game"] else []
        )

        spreads = (
            get_event_markets(events["spread"])
            if events["spread"] else []
        )

        totals = (
            get_event_markets(events["total"])
            if events["total"] else []
        )

        return jsonify(
            matchup=f"{team} vs {opponent}",
            date=game_date,
            event_tickers=events,
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


@app.get("/smu-today")
def smu_today():
    try:
        game_event = "KXNCAAFGAME-26SEP07SMUFSU"
        spread_event = "KXNCAAFSPREAD-26SEP07SMUFSU"
        total_event = "KXNCAAFTOTAL-26SEP07SMUFSU"

        return jsonify(
            matchup="SMU vs Florida State",
            date="2026-09-07",
            game_winner=[
                compact_market(m)
                for m in get_event_markets(game_event)
            ],
            spread=[
                compact_market(m)
                for m in get_event_markets(spread_event)
            ],
            total=[
                compact_market(m)
                for m in get_event_markets(total_event)
            ]
        )

    except requests.RequestException as e:
        return jsonify(error=str(e)), 502
