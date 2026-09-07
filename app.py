from flask import Flask, jsonify, request
import os
import math
import requests
from datetime import datetime
from difflib import SequenceMatcher

app = Flask(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_API_KEY = os.environ.get("CFBD_API_KEY")


@app.get("/")
def home():
    return jsonify(
        status="ok",
        message="Kalshi bridge is running",
        cfbd_configured=bool(CFBD_API_KEY)
    )


# -------------------------
# KALSHI
# -------------------------

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


# -------------------------
# TEXT MATCHING
# -------------------------

def normalize(text):
    text = str(text or "").lower()

    replacements = {
        "&": " and ",
        "st.": "state",
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

    return (
        similarity(team, text)
        + similarity(opponent, text)
    )


# -------------------------
# KALSHI EVENT DISCOVERY
# -------------------------

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


# -------------------------
# CFBD PROBABILITY ENGINE
# -------------------------

def cfbd_get(path, params=None):
    if not CFBD_API_KEY:
        raise RuntimeError(
            "CFBD_API_KEY is not configured"
        )

    r = requests.get(
        f"{CFBD_BASE}{path}",
        params=params,
        headers={
            "Authorization": (
                f"Bearer {CFBD_API_KEY}"
            )
        },
        timeout=20
    )

    r.raise_for_status()
    return r.json()


def get_team_elo(team, year):
    data = cfbd_get(
        "/ratings/elo",
        params={
            "year": year,
            "team": team
        }
    )

    if not data:
        return None

    best = None
    best_score = -1

    for row in data:
        score = similarity(
            team,
            row.get("team")
        )

        if score > best_score:
            best_score = score
            best = row

    if not best:
        return None

    elo = best.get("elo")

    if elo is None:
        return None

    return {
        "requested_team": team,
        "matched_team": best.get("team"),
        "conference": best.get("conference"),
        "elo": float(elo)
    }


def elo_win_probability(
    team_elo,
    opponent_elo
):
    rating_difference = (
        team_elo - opponent_elo
    )

    probability = (
        1.0
        / (
            1.0
            + math.pow(
                10.0,
                -rating_difference / 400.0
            )
        )
    )

    return probability


def build_probability_model(
    team,
    opponent,
    game_date
):
    year = datetime.strptime(
        game_date,
        "%Y-%m-%d"
    ).year

    team_rating = get_team_elo(
        team,
        year
    )

    opponent_rating = get_team_elo(
        opponent,
        year
    )

    if (
        team_rating is None
        or opponent_rating is None
    ):
        return {
            "available": False,
            "model": "CFBD Elo baseline",
            "reason": (
                "Could not retrieve an Elo "
                "rating for both teams."
            )
        }

    team_probability = elo_win_probability(
        team_rating["elo"],
        opponent_rating["elo"]
    )

    opponent_probability = (
        1.0 - team_probability
    )

    return {
        "available": True,
        "model": "CFBD Elo baseline",
        "season": year,
        "team": team_rating,
        "opponent": opponent_rating,
        "rating_difference": round(
            team_rating["elo"]
            - opponent_rating["elo"],
            2
        ),
        "team_fair_probability": round(
            team_probability,
            6
        ),
        "opponent_fair_probability": round(
            opponent_probability,
            6
        ),
        "team_fair_probability_percent": round(
            team_probability * 100,
            2
        ),
        "opponent_fair_probability_percent": round(
            opponent_probability * 100,
            2
        ),
        "notes": (
            "Baseline independent probability "
            "derived only from CFBD Elo ratings. "
            "Kalshi prices are not inputs."
        )
    }


# -------------------------
# VALUE / EV MATH
# -------------------------

def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp_probability(value):
    value = number(value)

    if value is None:
        return None

    if value < 0 or value > 1:
        return None

    return value


def expected_metrics(
    fair_probability,
    entry_price
):
    fair_probability = clamp_probability(
        fair_probability
    )

    entry_price = number(
        entry_price
    )

    if fair_probability is None:
        return None

    if entry_price is None:
        return None

    if entry_price <= 0 or entry_price >= 1:
        return None

    edge = (
        fair_probability
        - entry_price
    )

    expected_roi = (
        edge / entry_price
    )

    return {
        "fair_probability": round(
            fair_probability,
            4
        ),
        "entry_price": round(
            entry_price,
            4
        ),
        "edge": round(
            edge,
            4
        ),
        "edge_percentage_points": round(
            edge * 100,
            2
        ),
        "expected_profit_per_contract": round(
            edge,
            4
        ),
        "expected_roi": round(
            expected_roi,
            4
        ),
        "expected_roi_percent": round(
            expected_roi * 100,
            2
        )
    }


def analysis_market(
    m,
    market_type,
    fair_yes_probability=None
):
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

    fair_yes = clamp_probability(
        fair_yes_probability
    )

    fair_no = (
        1.0 - fair_yes
        if fair_yes is not None
        else None
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
        "close_time": m.get("close_time"),
        "fair_probability_supplied": (
            fair_yes is not None
        ),
        "yes_evaluation": expected_metrics(
            fair_yes,
            yes_ask
        ),
        "no_evaluation": expected_metrics(
            fair_no,
            no_ask
        )
    }


def analysis_candidates(
    markets,
    market_type,
    fair_probabilities=None
):
    fair_probabilities = (
        fair_probabilities or {}
    )

    results = []

    for m in markets:
        ticker = m.get("ticker")

        item = analysis_market(
            m,
            market_type,
            fair_probabilities.get(ticker)
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


# -------------------------
# GAME DATA
# -------------------------

def build_game_data(
    team,
    opponent,
    game_date
):
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


def validate_values(
    team,
    opponent,
    game_date
):
    if not team or not opponent or not game_date:
        return (
            False,
            "Provide team, opponent, and date."
        )

    try:
        datetime.strptime(
            game_date,
            "%Y-%m-%d"
        )

    except ValueError:
        return (
            False,
            "Date must use YYYY-MM-DD format"
        )

    return True, None


def find_winner_fair_probabilities(
    markets,
    team,
    opponent,
    probability_model
):
    probabilities = {}

    if not probability_model.get(
        "available"
    ):
        return probabilities

    team_probability = (
        probability_model[
            "team_fair_probability"
        ]
    )

    opponent_probability = (
        probability_model[
            "opponent_fair_probability"
        ]
    )

    for market in markets:
        ticker = market.get("ticker")
        title = str(
            market.get("title", "")
        )

        team_score = similarity(
            team,
            title
        )

        opponent_score = similarity(
            opponent,
            title
        )

        if (
            team_score >= 0.75
            and team_score > opponent_score
        ):
            probabilities[ticker] = (
                team_probability
            )

        elif (
            opponent_score >= 0.75
            and opponent_score > team_score
        ):
            probabilities[ticker] = (
                opponent_probability
            )

    return probabilities


# -------------------------
# ROUTES
# -------------------------

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


@app.get("/cfbd-test")
def cfbd_test():
    if not CFBD_API_KEY:
        return jsonify(
            configured=False,
            error="CFBD_API_KEY is missing"
        ), 500

    try:
        data = cfbd_get(
            "/ratings/elo",
            params={
                "year": 2026,
                "team": "Rutgers"
            }
        )

        return jsonify(
            configured=True,
            success=True,
            records=len(data),
            sample=(
                data[0]
                if data else None
            )
        )

    except requests.RequestException as e:
        return jsonify(
            configured=True,
            success=False,
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

    valid, message = validate_values(
        team,
        opponent,
        game_date
    )

    if not valid:
        return jsonify(
            error=message
        ), 400

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

    valid, message = validate_values(
        team,
        opponent,
        game_date
    )

    if not valid:
        return jsonify(
            error=message
        ), 400

    try:
        data = build_game_data(
            team,
            opponent,
            game_date
        )

        if not data["game_event"]:
            return jsonify(
                found=False,
                matchup=f"{team} vs {opponent}",
                date=game_date,
                message=(
                    "No matching Kalshi "
                    "college football event found."
                )
            ), 404

        probability_model = (
            build_probability_model(
                team,
                opponent,
                game_date
            )
        )

        winner_probabilities = (
            find_winner_fair_probabilities(
                data["game_winner"],
                team,
                opponent,
                probability_model
            )
        )

        winner_candidates = (
            analysis_candidates(
                data["game_winner"],
                "winner",
                winner_probabilities
            )
        )

        spread_candidates = (
            analysis_candidates(
                data["spread"],
                "spread"
            )
        )

        total_candidates = (
            analysis_candidates(
                data["total"],
                "total"
            )
        )

        return jsonify(
            found=True,
            matchup=f"{team} vs {opponent}",
            date=game_date,
            probability_model=(
                probability_model
            ),
            methodology={
                "probability_source": (
                    "Independent CFBD Elo "
                    "ratings."
                ),
                "kalshi_role": (
                    "Kalshi prices are used "
                    "only after fair probability "
                    "is calculated."
                ),
                "edge": (
                    "Independent fair probability "
                    "minus executable ask price."
                ),
                "expected_profit": (
                    "Fair probability minus "
                    "contract cost."
                ),
                "expected_roi": (
                    "Expected profit divided "
                    "by contract cost."
                ),
                "limitations": (
                    "Version 1 is an Elo-only "
                    "baseline. It does not yet "
                    "adjust for venue, injuries, "
                    "weather, matchup efficiency, "
                    "roster changes, fees, or "
                    "slippage."
                )
            },
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
                ),
                "winner_contracts_with_model": (
                    len(winner_probabilities)
                )
            },
            winner=winner_candidates,
            spread=spread_candidates,
            total=total_candidates
        )

    except RuntimeError as e:
        return jsonify(
            error=str(e)
        ), 500

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
