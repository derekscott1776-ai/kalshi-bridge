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


def related_event_ticker(game_event, market_type):
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
        return f"KXNCAAFSPREAD-{suffix}"

    if market_type == "total":
        return f"KXNCAAFTOTAL-{suffix}"

    return game_event


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def analysis_market(m, market_type):
    yes_bid = number(
        m.get("yes_bid_dollars")
    )

    yes_ask = number(
        m.get("yes_ask_dollars")
    )

    no_bid = number(
        m.get("no_bid_dollars")
    )

    no_ask = number(
        m.get("no_ask_dollars")
    )

    last_price = number(
        m.get("last_price_dollars")
    )

    volume = number(
        m.get("volume_fp")
    )

    volume_24h = number(
        m.get("volume_24h_fp")
    )

    open_interest = number(
        m.get("open_interest_fp")
    )

    yes_spread = None

    if (
        yes_bid is not None
        and yes_ask is not None
    ):
        yes_spread = round(
            yes_ask - yes_bid,
            4
        )

    return {
        "market_type": market_type,
        "ticker": m.get("ticker"),
        "title": m.get("title"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": last_price,
        "yes_bid_ask_spread": yes_spread,
        "volume": volume,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
        "close_time": m.get("close_time")
    }


def analysis_candidates(markets, market_type):
    results = []

    for m in markets:
        item = analysis_market(
            m,
            market_type
        )

        if (
            item["yes_ask"] is None
            and item["no_ask"] is None
        ):
            continue

        results.append(item)

    results.sort(
        key=lambda x: (
            -(
                x["volume_24h"]
                if x["volume_24h"] is not None
                else 0
            ),
            (
                x["yes_bid_ask_spread"]
                if x["yes_bid_ask_spread"] is not None
                else 999
            )
        )
    )

    return results


def build_game_data(team, opponent, game_date):
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

    return {
        "game_event": game_event,
        "spread_event": spread_event,
        "total_event": total_event,
        "game_winner": game_winner,
        "spread": spreads,
        "total": totals
    }


def validate_game_request():
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
        return (
            None,
            None,
            None,
            jsonify(
                error=(
                    "Provide team, opponent, "
                    "and date."
                )
            ),
            400
        )

    try:
        datetime.strptime(
            game_date,
            "%Y-%m-%d"
        )

    except ValueError:
        return (
            None,
            None,
            None,
            jsonify(
                error=(
                    "Date must use "
                    "YYYY-MM-DD format"
                )
            ),
            400
        )

    return (
        team,
        opponent,
        game_date,
        None,
        None
    )


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
    (
        team,
        opponent,
        game_date,
        error_response,
        error_code
    ) = validate_game_request()

    if error_response:
        return error_response, error_code

    try:
        data = build_game_data(
            team,
            opponent,
            game_date
        )

        return jsonify(
            matchup=f"{team} vs {opponent}",
            date=game_date,
            event_tickers={
                "game": data["game_event"],
                "spread": data["spread_event"],
                "total": data["total_event"]
            },
            game_winner=[
                compact_market(m)
                for m in data["game_winner"]
            ],
            spread=[
                compact_market(m)
                for m in data["spread"]
            ],
            total=[
                compact_market(m)
                for m in data["total"]
            ]
        )

    except requests.RequestException as e:
        return jsonify(
            error=str(e)
        ), 502


@app.get("/analyze")
def analyze():
    (
        team,
        opponent,
        game_date,
        error_response,
        error_code
    ) = validate_game_request()

    if error_response:
        return error_response, error_code

    try:
        data = build_game_data(
            team,
            opponent,
            game_date
        )

        if not data["game_event"]:
            return jsonify(
                matchup=f"{team} vs {opponent}",
                date=game_date,
                found=False,
                message=(
                    "No matching Kalshi "
                    "college football event found."
                )
            ), 404

        winner_candidates = analysis_candidates(
            data["game_winner"],
            "winner"
        )

        spread_candidates = analysis_candidates(
            data["spread"],
            "spread"
        )

        total_candidates = analysis_candidates(
            data["total"],
            "total"
        )

        return jsonify(
            found=True,
            matchup=f"{team} vs {opponent}",
            date=game_date,
            event_tickers={
                "game": data["game_event"],
                "spread": data["spread_event"],
                "total": data["total_event"]
            },
            summary={
                "winner_contracts": len(
                    winner_candidates
                ),
                "spread_contracts": len(
                    spread_candidates
                ),
                "total_contracts": len(
                    total_candidates
                )
            },
            winner=winner_candidates,
            spread=spread_candidates,
            total=total_candidates
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
