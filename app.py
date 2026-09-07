from flask import Flask, jsonify, request
import requests
from datetime import datetime
from difflib import SequenceMatcher

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
    text = str(text or "").lower()

    replacements = {
        "&": " and ",
        "st.": "state",
        "st ": "state ",
        "florida st": "florida state",
        "miami fl": "miami",
        "university": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return "".join(
        c for c in text
        if c.isalnum()
    )


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0

    if a in b or b in a:
        return 1.0

    return SequenceMatcher(
        None,
        a,
        b
    ).ratio()


def event_text(event):
    return " ".join([
        str(event.get("title", "")),
        str(event.get("sub_title", "")),
        str(event.get("event_ticker", ""))
    ])


def matchup_score(event, team, opponent):
    text = event_text(event)

    team_score = similarity(
        team,
        text
    )

    opponent_score = similarity(
        opponent,
        text
    )

    return team_score + opponent_score


def discover_game_event(team, opponent, game_date):
    date_obj = datetime.strptime(
        game_date,
        "%Y-%m-%d"
    )

    date_code = date_obj.strftime(
        "%y%b%d"
    ).upper()

    cursor = None
    candidates = []

    for _ in range(20):
        params = {
            "series_ticker": "KXNCAAFGAME",
            "limit": 200
        }

        if cursor:
            params["cursor"] = cursor

        data = kalshi_get(
            "/events",
            params=params
        )

        for event in data.get("events", []):
            event_ticker = str(
                event.get("event_ticker", "")
            ).upper()

            if date_code not in event_ticker:
                continue

            score = matchup_score(
                event,
                team,
                opponent
            )

            candidates.append(
                (score, event)
            )

        cursor = data.get("cursor")

        if not cursor:
            break

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    best_score, best_event = candidates[0]

    if best_score < 1.0:
        return None

    return best_event.get(
        "event_ticker"
    )


def related_event_ticker(
    game_event,
    market_type
):
    if not game_event:
        return None

    prefix = "KXNCAAFGAME-"

    if not game_event.startswith(prefix):
        return None

    suffix = game_event.split(
        prefix,
        1
    )[1]

    if market_type == "spread":
        return (
            f"KXNCAAFSPREAD-{suffix}"
        )

    if market_type == "total":
        return (
            f"KXNCAAFTOTAL-{suffix}"
        )

    return game_event


@app.get("/market/<ticker>")
def market(ticker):
    try:
        return jsonify(
            kalshi_get(
                f"/markets/{ticker}"
            )
        )

    except requests.RequestException as e:
        return jsonify(
            error=str(e)
        ), 502


@app.get("/game")
def game():
    team = request.args.get(
        "team",
        ""
    ).strip()

    opponent = request.args.get(
        "opponent",
        ""
    ).strip()

    game_date = request.args.get(
        "date",
        ""
    ).strip()

    if not team or not opponent or not game_date:
        return jsonify(
            error=(
                "Use /game?team=SMU"
                "&opponent=Florida%20State"
                "&date=2026-09-07"
            )
        ), 400

    try:
        datetime.strptime(
            game_date,
            "%Y-%m-%d"
        )

    except ValueError:
        return jsonify(
            error="Date must use YYYY-MM-DD format"
        ), 400

    try:
        game_event = discover_game_event(
            team,
            opponent,
            game_date
        )

        spread_event = related_event_ticker(
            game_event,
            "spread"
        )

        total_event = related_event_ticker(
            game_event,
            "total"
        )

        game_winner = (
            get_event_markets(game_event)
            if game_event else []
        )

        spreads = (
            get_event_markets(spread_event)
            if spread_event else []
        )

        totals = (
            get_event_markets(total_event)
            if total_event else []
        )

        return jsonify(
            matchup=f"{team} vs {opponent}",
            date=game_date,
            event_tickers={
                "game": game_event,
                "spread": spread_event,
                "total": total_event
            },
            game_winner=[
                compact_market(m)
                for m in game_winner
            ],
            spread=[
                compact_market(m)
                for m in spreads
            ],
            total=[
                compact_market(m)
                for m in totals
            ]
        )

    except requests.RequestException as e:
        return jsonify(
            error=str(e)
        ), 502


@app.get("/smu-today")
def smu_today():
    try:
        game_event = (
            "KXNCAAFGAME-26SEP07SMUFSU"
        )

        spread_event = (
            "KXNCAAFSPREAD-26SEP07SMUFSU"
        )

        total_event = (
            "KXNCAAFTOTAL-26SEP07SMUFSU"
        )

        return jsonify(
            matchup="SMU vs Florida State",
            date="2026-09-07",
            game_winner=[
                compact_market(m)
                for m in get_event_markets(
                    game_event
                )
            ],
            spread=[
                compact_market(m)
                for m in get_event_markets(
                    spread_event
                )
            ],
            total=[
                compact_market(m)
                for m in get_event_markets(
                    total_event
                )
            ]
        )

    except requests.RequestException as e:
        return jsonify(
            error=str(e)
        ), 502
