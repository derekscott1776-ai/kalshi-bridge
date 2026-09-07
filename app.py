from flask import Flask, jsonify, request
import os
import math
import requests
from datetime import datetime
from difflib import SequenceMatcher

app = Flask(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "").strip()

# Calibrated from 3,611 completed, non-neutral,
# FBS-vs-FBS regular-season games from 2021-2025.
CALIBRATED_HOME_FIELD_ELO = 67.0


@app.get("/")
def home():
    return jsonify(
        status="ok",
        message="Kalshi bridge is running",
        cfbd_configured=bool(CFBD_API_KEY),
        calibrated_home_field_elo=CALIBRATED_HOME_FIELD_ELO
    )


# ============================================================
# KALSHI
# ============================================================

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


# ============================================================
# GENERAL HELPERS
# ============================================================

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


def normalize(text):
    text = str(text or "").lower()

    replacements = {
        "&": " and ",
        "st.": "state",
        "florida st": "florida state",
        "miami fl": "miami",
        "miami (fl)": "miami",
        "university": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return "".join(
        c
        for c in text
        if c.isalnum()
    )


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    if a in b or b in a:
        return 1.0

    return SequenceMatcher(
        None,
        a,
        b
    ).ratio()


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00"
            )
        )
    except ValueError:
        return None


# ============================================================
# KALSHI EVENT DISCOVERY
# ============================================================

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


def discover_game_event(
    team,
    opponent,
    game_date
):
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

        for event in data.get(
            "events",
            []
        ):
            event_ticker = str(
                event.get(
                    "event_ticker",
                    ""
                )
            ).upper()

            if date_code not in event_ticker:
                continue

            score = matchup_score(
                event,
                team,
                opponent
            )

            candidates.append(
                (
                    score,
                    event
                )
            )

        cursor = data.get(
            "cursor"
        )

        if not cursor:
            break

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    best_score, best_event = (
        candidates[0]
    )

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

    if not game_event.startswith(
        prefix
    ):
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

    spread_event = (
        related_event_ticker(
            game_event,
            "spread"
        )
    )

    total_event = (
        related_event_ticker(
            game_event,
            "total"
        )
    )

    game_winner = (
        get_event_markets(
            game_event
        )
        if game_event
        else []
    )

    spread = (
        get_event_markets(
            spread_event
        )
        if spread_event
        else []
    )

    total = (
        get_event_markets(
            total_event
        )
        if total_event
        else []
    )

    return {
        "game_event": game_event,
        "spread_event": spread_event,
        "total_event": total_event,
        "game_winner": game_winner,
        "spread": spread,
        "total": total
    }


# ============================================================
# CFBD
# ============================================================

def cfbd_get(path, params=None):
    if not CFBD_API_KEY:
        raise RuntimeError(
            "CFBD_API_KEY is not configured"
        )

    r = requests.get(
        f"{CFBD_BASE}{path}",
        params=params,
        headers={
            "Authorization":
                f"Bearer {CFBD_API_KEY}"
        },
        timeout=20
    )

    r.raise_for_status()

    return r.json()


def game_pair_score(
    game,
    team,
    opponent
):
    home_team = str(
        game.get(
            "homeTeam",
            ""
        )
    )

    away_team = str(
        game.get(
            "awayTeam",
            ""
        )
    )

    team_home = similarity(
        team,
        home_team
    )

    team_away = similarity(
        team,
        away_team
    )

    opponent_home = similarity(
        opponent,
        home_team
    )

    opponent_away = similarity(
        opponent,
        away_team
    )

    orientation_one = (
        team_home
        + opponent_away
    )

    orientation_two = (
        team_away
        + opponent_home
    )

    best_orientation = max(
        orientation_one,
        orientation_two
    )

    best_team_match = max(
        team_home,
        team_away
    )

    best_opponent_match = max(
        opponent_home,
        opponent_away
    )

    return {
        "score": best_orientation,
        "team_match": best_team_match,
        "opponent_match":
            best_opponent_match
    }


def discover_cfbd_game(
    team,
    opponent,
    game_date
):
    requested_date = (
        datetime.strptime(
            game_date,
            "%Y-%m-%d"
        ).date()
    )

    year = requested_date.year

    searches = [
        {
            "year": year,
            "team": team,
            "seasonType": "both"
        },
        {
            "year": year,
            "team": opponent,
            "seasonType": "both"
        }
    ]

    seen_ids = set()
    games = []

    for params in searches:
        data = cfbd_get(
            "/games",
            params=params
        )

        for game in data:
            game_id = game.get(
                "id"
            )

            if game_id in seen_ids:
                continue

            seen_ids.add(
                game_id
            )

            games.append(
                game
            )

    # Fallback for a naming mismatch
    if not games:
        games = cfbd_get(
            "/games",
            params={
                "year": year,
                "seasonType": "both"
            }
        )

    candidates = []

    for game in games:
        pair = game_pair_score(
            game,
            team,
            opponent
        )

        if (
            pair["team_match"] < 0.65
            or
            pair["opponent_match"] < 0.65
        ):
            continue

        start_dt = (
            parse_iso_datetime(
                game.get(
                    "startDate"
                )
            )
        )

        if start_dt is None:
            continue

        date_difference = abs(
            (
                start_dt.date()
                - requested_date
            ).days
        )

        # UTC date can differ from
        # the US local game date by one day.
        if date_difference > 1:
            continue

        adjusted_score = (
            pair["score"]
            - (
                date_difference
                * 0.05
            )
        )

        candidates.append({
            "score":
                adjusted_score,
            "game":
                game
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    return candidates[0][
        "game"
    ]


def build_cfbd_game_context(
    team,
    opponent,
    game_date
):
    game = discover_cfbd_game(
        team,
        opponent,
        game_date
    )

    if not game:
        return {
            "available": False,
            "reason":
                "No matching CFBD game found."
        }

    home_team = str(
        game.get(
            "homeTeam",
            ""
        )
    )

    away_team = str(
        game.get(
            "awayTeam",
            ""
        )
    )

    team_home_score = similarity(
        team,
        home_team
    )

    team_away_score = similarity(
        team,
        away_team
    )

    if (
        team_home_score
        >= team_away_score
    ):
        team_side = "home"
        opponent_side = "away"

        team_cfbd_name = (
            home_team
        )

        opponent_cfbd_name = (
            away_team
        )

        team_pregame_elo = number(
            game.get(
                "homePregameElo"
            )
        )

        opponent_pregame_elo = number(
            game.get(
                "awayPregameElo"
            )
        )

    else:
        team_side = "away"
        opponent_side = "home"

        team_cfbd_name = (
            away_team
        )

        opponent_cfbd_name = (
            home_team
        )

        team_pregame_elo = number(
            game.get(
                "awayPregameElo"
            )
        )

        opponent_pregame_elo = number(
            game.get(
                "homePregameElo"
            )
        )

    return {
        "available": True,

        "game_id":
            game.get("id"),

        "season":
            game.get("season"),

        "week":
            game.get("week"),

        "season_type":
            game.get(
                "seasonType"
            ),

        "start_date":
            game.get(
                "startDate"
            ),

        "neutral_site":
            bool(
                game.get(
                    "neutralSite"
                )
            ),

        "venue_id":
            game.get(
                "venueId"
            ),

        "venue":
            game.get(
                "venue"
            ),

        "home_team":
            home_team,

        "away_team":
            away_team,

        "home_conference":
            game.get(
                "homeConference"
            ),

        "away_conference":
            game.get(
                "awayConference"
            ),

        "team_requested":
            team,

        "team_cfbd_name":
            team_cfbd_name,

        "team_home_away":
            team_side,

        "team_pregame_elo":
            team_pregame_elo,

        "opponent_requested":
            opponent,

        "opponent_cfbd_name":
            opponent_cfbd_name,

        "opponent_home_away":
            opponent_side,

        "opponent_pregame_elo":
            opponent_pregame_elo,

        "raw_home_pregame_elo":
            number(
                game.get(
                    "homePregameElo"
                )
            ),

        "raw_away_pregame_elo":
            number(
                game.get(
                    "awayPregameElo"
                )
            )
    }


# ============================================================
# ELO MODEL
# ============================================================

def elo_probability(
    rating_difference
):
    return (
        1.0
        /
        (
            1.0
            + math.pow(
                10.0,
                -rating_difference
                / 400.0
            )
        )
    )


def build_probability_model(
    team,
    opponent,
    game_date
):
    context = (
        build_cfbd_game_context(
            team,
            opponent,
            game_date
        )
    )

    if not context.get(
        "available"
    ):
        return {
            "available": False,
            "model":
                "CFBD pregame Elo + calibrated home field",
            "reason":
                context.get(
                    "reason"
                )
        }

    team_elo = (
        context.get(
            "team_pregame_elo"
        )
    )

    opponent_elo = (
        context.get(
            "opponent_pregame_elo"
        )
    )

    if (
        team_elo is None
        or
        opponent_elo is None
    ):
        return {
            "available": False,

            "model":
                "CFBD pregame Elo + calibrated home field",

            "reason":
                "The matching game does not "
                "currently contain both "
                "pregame Elo ratings.",

            "game_context":
                context
        }

    raw_rating_difference = (
        team_elo
        - opponent_elo
    )

    neutral_site = context.get(
        "neutral_site",
        False
    )

    team_home_away = context.get(
        "team_home_away"
    )

    if neutral_site:
        home_field_adjustment = 0.0
        location_reason = (
            "Neutral-site game: no "
            "home-field adjustment applied."
        )

    elif team_home_away == "home":
        home_field_adjustment = (
            CALIBRATED_HOME_FIELD_ELO
        )

        location_reason = (
            "Selected team is home: "
            "+67 Elo applied."
        )

    elif team_home_away == "away":
        home_field_adjustment = (
            -CALIBRATED_HOME_FIELD_ELO
        )

        location_reason = (
            "Selected team is away: "
            "-67 Elo applied from the "
            "selected team's perspective."
        )

    else:
        home_field_adjustment = 0.0

        location_reason = (
            "Home/away status unavailable: "
            "no location adjustment applied."
        )

    adjusted_rating_difference = (
        raw_rating_difference
        + home_field_adjustment
    )

    team_probability = (
        elo_probability(
            adjusted_rating_difference
        )
    )

    opponent_probability = (
        1.0
        - team_probability
    )

    return {
        "available": True,

        "model":
            "CFBD pregame Elo + calibrated home field",

        "model_version":
            "cfbd-pregame-elo-hfa-v3",

        "game_context":
            context,

        "team_pregame_elo":
            team_elo,

        "opponent_pregame_elo":
            opponent_elo,

        "raw_rating_difference":
            round(
                raw_rating_difference,
                2
            ),

        "calibrated_home_field_elo":
            CALIBRATED_HOME_FIELD_ELO,

        "neutral_site":
            neutral_site,

        "team_home_away":
            team_home_away,

        "home_field_adjustment_elo_points":
            home_field_adjustment,

        "home_field_adjustment_calibrated":
            True,

        "home_field_adjustment_status":
            location_reason,

        "adjusted_rating_difference":
            round(
                adjusted_rating_difference,
                2
            ),

        "team_fair_probability":
            round(
                team_probability,
                6
            ),

        "team_fair_probability_percent":
            round(
                team_probability
                * 100,
                2
            ),

        "opponent_fair_probability":
            round(
                opponent_probability,
                6
            ),

        "opponent_fair_probability_percent":
            round(
                opponent_probability
                * 100,
                2
            ),

        "notes":
            (
                "Fair probability uses the "
                "matching CFBD game's pregame "
                "Elo ratings plus the historically "
                "calibrated home-field adjustment. "
                "Kalshi prices are not inputs "
                "to the probability model."
            )
    }


# ============================================================
# EDGE / EV / ROI
# ============================================================

def expected_metrics(
    fair_probability,
    entry_price
):
    fair_probability = (
        clamp_probability(
            fair_probability
        )
    )

    entry_price = number(
        entry_price
    )

    if fair_probability is None:
        return None

    if entry_price is None:
        return None

    if (
        entry_price <= 0
        or
        entry_price >= 1
    ):
        return None

    edge = (
        fair_probability
        - entry_price
    )

    expected_profit = edge

    expected_roi = (
        expected_profit
        / entry_price
    )

    return {
        "fair_probability":
            round(
                fair_probability,
                4
            ),

        "entry_price":
            round(
                entry_price,
                4
            ),

        "edge":
            round(
                edge,
                4
            ),

        "edge_percentage_points":
            round(
                edge * 100,
                2
            ),

        "expected_profit_per_contract":
            round(
                expected_profit,
                4
            ),

        "expected_roi":
            round(
                expected_roi,
                4
            ),

        "expected_roi_percent":
            round(
                expected_roi
                * 100,
                2
            )
    }


def analysis_market(
    m,
    market_type,
    fair_yes_probability=None
):
    yes_bid = number(
        m.get(
            "yes_bid_dollars"
        )
    )

    yes_ask = number(
        m.get(
            "yes_ask_dollars"
        )
    )

    no_bid = number(
        m.get(
            "no_bid_dollars"
        )
    )

    no_ask = number(
        m.get(
            "no_ask_dollars"
        )
    )

    last_price = number(
        m.get(
            "last_price_dollars"
        )
    )

    volume = number(
        m.get(
            "volume_fp"
        )
    )

    volume_24h = number(
        m.get(
            "volume_24h_fp"
        )
    )

    open_interest = number(
        m.get(
            "open_interest_fp"
        )
    )

    yes_spread = None

    if (
        yes_bid is not None
        and
        yes_ask is not None
    ):
        yes_spread = round(
            yes_ask
            - yes_bid,
            4
        )

    fair_yes = (
        clamp_probability(
            fair_yes_probability
        )
    )

    fair_no = (
        1.0 - fair_yes
        if fair_yes is not None
        else None
    )

    return {
        "market_type":
            market_type,

        "ticker":
            m.get("ticker"),

        "title":
            m.get("title"),

        "yes_bid":
            yes_bid,

        "yes_ask":
            yes_ask,

        "no_bid":
            no_bid,

        "no_ask":
            no_ask,

        "last_price":
            last_price,

        "yes_bid_ask_spread":
            yes_spread,

        "volume":
            volume,

        "volume_24h":
            volume_24h,

        "open_interest":
            open_interest,

        "close_time":
            m.get(
                "close_time"
            ),

        "fair_probability_supplied":
            fair_yes is not None,

        "yes_evaluation":
            expected_metrics(
                fair_yes,
                yes_ask
            ),

        "no_evaluation":
            expected_metrics(
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
        fair_probabilities
        or {}
    )

    results = []

    for m in markets:
        ticker = m.get(
            "ticker"
        )

        item = analysis_market(
            m,
            market_type,
            fair_probabilities.get(
                ticker
            )
        )

        if (
            item["yes_ask"] is None
            and
            item["no_ask"] is None
        ):
            continue

        results.append(
            item
        )

    results.sort(
        key=lambda x: (
            -(
                x["volume_24h"]
                if
                x["volume_24h"]
                is not None
                else 0
            ),
            (
                x[
                    "yes_bid_ask_spread"
                ]
                if
                x[
                    "yes_bid_ask_spread"
                ]
                is not None
                else 999
            )
        )
    )

    return results


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
        ticker = market.get(
            "ticker"
        )

        title = str(
            market.get(
                "title",
                ""
            )
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
            and
            team_score
            > opponent_score
        ):
            probabilities[
                ticker
            ] = team_probability

        elif (
            opponent_score >= 0.75
            and
            opponent_score
            > team_score
        ):
            probabilities[
                ticker
            ] = opponent_probability

    return probabilities


# ============================================================
# VALIDATION
# ============================================================

def validate_values(
    team,
    opponent,
    game_date
):
    if (
        not team
        or
        not opponent
        or
        not game_date
    ):
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


# ============================================================
# ROUTES
# ============================================================

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
            error=(
                "CFBD_API_KEY is missing"
            )
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
                if data
                else None
            )
        )

    except requests.RequestException as e:
        return jsonify(
            configured=True,
            success=False,
            error=str(e)
        ), 502


@app.get("/cfbd-game")
def cfbd_game():
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

    valid, message = (
        validate_values(
            team,
            opponent,
            game_date
        )
    )

    if not valid:
        return jsonify(
            error=message
        ), 400

    try:
        context = (
            build_cfbd_game_context(
                team,
                opponent,
                game_date
            )
        )

        return jsonify(
            matchup=(
                f"{team} vs {opponent}"
            ),
            date=game_date,
            cfbd_game=context
        )

    except RuntimeError as e:
        return jsonify(
            error=str(e)
        ), 500

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

    valid, message = (
        validate_values(
            team,
            opponent,
            game_date
        )
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
            matchup=(
                f"{team} vs {opponent}"
            ),

            date=game_date,

            event_tickers={
                "game":
                    data[
                        "game_event"
                    ],

                "spread":
                    data[
                        "spread_event"
                    ],

                "total":
                    data[
                        "total_event"
                    ]
            },

            game_winner=[
                compact_market(m)
                for m
                in data[
                    "game_winner"
                ]
            ],

            spread=[
                compact_market(m)
                for m
                in data[
                    "spread"
                ]
            ],

            total=[
                compact_market(m)
                for m
                in data[
                    "total"
                ]
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

    valid, message = (
        validate_values(
            team,
            opponent,
            game_date
        )
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

        if not data[
            "game_event"
        ]:
            return jsonify(
                found=False,
                matchup=(
                    f"{team} vs "
                    f"{opponent}"
                ),
                date=game_date,
                message=(
                    "No matching Kalshi "
                    "college football "
                    "event found."
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
                data[
                    "game_winner"
                ],
                team,
                opponent,
                probability_model
            )
        )

        winner_candidates = (
            analysis_candidates(
                data[
                    "game_winner"
                ],
                "winner",
                winner_probabilities
            )
        )

        spread_candidates = (
            analysis_candidates(
                data[
                    "spread"
                ],
                "spread"
            )
        )

        total_candidates = (
            analysis_candidates(
                data[
                    "total"
                ],
                "total"
            )
        )

        return jsonify(
            found=True,

            matchup=(
                f"{team} vs {opponent}"
            ),

            date=game_date,

            probability_model=(
                probability_model
            ),

            methodology={
                "probability_source":
                    (
                        "Exact CFBD game "
                        "pregame Elo ratings."
                    ),

                "game_context":
                    (
                        "CFBD supplies week, "
                        "home team, away team, "
                        "venue and neutral-site "
                        "status."
                    ),

                "home_field":
                    (
                        "A +67 Elo home-field "
                        "adjustment is applied to "
                        "the home team. From the "
                        "selected team's perspective "
                        "this is +67 when home, "
                        "-67 when away, and 0 on "
                        "neutral sites."
                    ),

                "home_field_calibration":
                    (
                        "67 Elo points was fitted "
                        "on 3,611 completed "
                        "non-neutral FBS-vs-FBS "
                        "regular-season games from "
                        "2021-2025 by minimizing "
                        "binary log loss."
                    ),

                "kalshi_role":
                    (
                        "Kalshi prices are used "
                        "only after independent "
                        "fair probability is "
                        "calculated."
                    ),

                "edge":
                    (
                        "Independent fair "
                        "probability minus "
                        "executable ask price."
                    ),

                "expected_profit":
                    (
                        "Fair probability minus "
                        "contract cost."
                    ),

                "expected_roi":
                    (
                        "Expected profit divided "
                        "by contract cost."
                    ),

                "limitations":
                    (
                        "Fees, slippage, injuries, "
                        "weather, matchup efficiency, "
                        "roster changes and model "
                        "calibration beyond Elo plus "
                        "home field are not yet included."
                    )
            },

            event_tickers={
                "game":
                    data[
                        "game_event"
                    ],

                "spread":
                    data[
                        "spread_event"
                    ],

                "total":
                    data[
                        "total_event"
                    ]
            },

            summary={
                "winner_contracts":
                    len(
                        winner_candidates
                    ),

                "spread_contracts":
                    len(
                        spread_candidates
                    ),

                "total_contracts":
                    len(
                        total_candidates
                    ),

                "winner_contracts_with_model":
                    len(
                        winner_probabilities
                    )
            },

            winner=
                winner_candidates,

            spread=
                spread_candidates,

            total=
                total_candidates
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
            matchup=(
                "SMU vs Florida State"
            ),

            date="2026-09-07",

            game_winner=[
                compact_market(m)
                for m
                in get_event_markets(
                    game_event
                )
            ],

            spread=[
                compact_market(m)
                for m
                in get_event_markets(
                    spread_event
                )
            ],

            total=[
                compact_market(m)
                for m
                in get_event_markets(
                    total_event
                )
            ]
        )

    except requests.RequestException as e:
        return jsonify(
            error=str(e)
        ), 502

# ============================================================
# HISTORICAL HOME-FIELD ELO CALIBRATION
# ============================================================

def calibration_games(start_year=2021, end_year=2025):
    games_used = []
    yearly_counts = {}

    for year in range(start_year, end_year + 1):
        games = cfbd_get(
            "/games",
            params={
                "year": year,
                "seasonType": "regular"
            }
        )

        count = 0

        for game in games:
            # Completed games only
            if not game.get("completed"):
                continue

            # Exclude neutral-site games
            if game.get("neutralSite"):
                continue

            # FBS vs FBS only
            if game.get("homeClassification") != "fbs":
                continue

            if game.get("awayClassification") != "fbs":
                continue

            home_elo = number(
                game.get("homePregameElo")
            )

            away_elo = number(
                game.get("awayPregameElo")
            )

            home_points = number(
                game.get("homePoints")
            )

            away_points = number(
                game.get("awayPoints")
            )

            if (
                home_elo is None
                or away_elo is None
                or home_points is None
                or away_points is None
            ):
                continue

            # Exclude ties
            if home_points == away_points:
                continue

            games_used.append({
                "year": year,
                "home_elo": home_elo,
                "away_elo": away_elo,
                "home_win": (
                    1
                    if home_points > away_points
                    else 0
                )
            })

            count += 1

        yearly_counts[str(year)] = count

    return games_used, yearly_counts


def calibration_log_loss(games, home_field_elo):
    total_loss = 0.0

    for game in games:
        rating_difference = (
            game["home_elo"]
            - game["away_elo"]
            + home_field_elo
        )

        probability = elo_probability(
            rating_difference
        )

        # Protect log() from 0 or 1
        probability = max(
            0.000001,
            min(
                0.999999,
                probability
            )
        )

        actual = game["home_win"]

        total_loss += -(
            actual * math.log(probability)
            +
            (1 - actual)
            * math.log(1 - probability)
        )

    return total_loss / len(games)


def fit_home_field_elo(games):
    best_hfa = None
    best_loss = None

    # Search from -100 to +200 Elo
    # in 0.5-point increments.
    step = 0.5

    value = -100.0

    while value <= 200.0:
        loss = calibration_log_loss(
            games,
            value
        )

        if (
            best_loss is None
            or loss < best_loss
        ):
            best_loss = loss
            best_hfa = value

        value += step

    return best_hfa, best_loss


@app.get("/calibrate-home-field")
def calibrate_home_field():
    try:
        games, yearly_counts = (
            calibration_games(
                2021,
                2025
            )
        )

        if not games:
            return jsonify(
                success=False,
                error=(
                    "No qualifying historical "
                    "games were returned."
                )
            ), 500

        best_hfa, best_loss = (
            fit_home_field_elo(
                games
            )
        )

        zero_hfa_loss = (
            calibration_log_loss(
                games,
                0.0
            )
        )

        home_wins = sum(
            game["home_win"]
            for game in games
        )

        home_win_rate = (
            home_wins
            / len(games)
        )

        improvement = (
            zero_hfa_loss
            - best_loss
        )

        return jsonify(
            success=True,

            calibration_period={
                "start_year": 2021,
                "end_year": 2025
            },

            filters={
                "completed_only": True,
                "regular_season_only": True,
                "non_neutral_only": True,
                "fbs_vs_fbs_only": True,
                "ties_excluded": True,
                "pregame_elo_required": True
            },

            sample_size=len(games),

            games_by_year=yearly_counts,

            home_wins=home_wins,

            home_win_rate=round(
                home_win_rate,
                6
            ),

            home_win_rate_percent=round(
                home_win_rate * 100,
                2
            ),

            fitted_home_field_elo_points=(
                best_hfa
            ),

            currently_used_home_field_elo_points=(
                CALIBRATED_HOME_FIELD_ELO
            ),

            matches_live_model=(
                best_hfa
                == CALIBRATED_HOME_FIELD_ELO
            ),

            log_loss_without_home_field=round(
                zero_hfa_loss,
                6
            ),

            log_loss_with_home_field=round(
                best_loss,
                6
            ),

            log_loss_improvement=round(
                improvement,
                6
            ),

            methodology={
                "model": (
                    "P(home win) = "
                    "1 / (1 + 10^("
                    "-((home Elo - away Elo "
                    "+ HFA) / 400)))"
                ),

                "objective": (
                    "Choose the HFA Elo value "
                    "that minimizes average "
                    "binary log loss on "
                    "historical winners."
                ),

                "search_range": (
                    "-100 to +200 Elo points"
                ),

                "search_increment": (
                    "0.5 Elo points"
                ),

                "kalshi_used": False
            }
        )

    except RuntimeError as e:
        return jsonify(
            success=False,
            error=str(e)
        ), 500

    except requests.RequestException as e:
        return jsonify(
            success=False,
            error=str(e)
        ), 502

# ============================================================
# OUT-OF-SAMPLE HOME-FIELD VALIDATION
# ============================================================

def summarize_calibration_games(games):
    if not games:
        return {
            "sample_size": 0,
            "home_wins": 0,
            "home_win_rate": None,
            "home_win_rate_percent": None
        }

    home_wins = sum(
        game["home_win"]
        for game in games
    )

    home_win_rate = (
        home_wins
        / len(games)
    )

    return {
        "sample_size": len(games),
        "home_wins": home_wins,
        "home_win_rate": round(
            home_win_rate,
            6
        ),
        "home_win_rate_percent": round(
            home_win_rate * 100,
            2
        )
    }


@app.get("/out-of-sample-home-field")
def out_of_sample_home_field():
    try:
        training_games, training_counts = (
            calibration_games(
                2021,
                2024
            )
        )

        test_games, test_counts = (
            calibration_games(
                2025,
                2025
            )
        )

        if not training_games:
            return jsonify(
                success=False,
                error=(
                    "No qualifying 2021-2024 "
                    "training games were returned."
                )
            ), 500

        if not test_games:
            return jsonify(
                success=False,
                error=(
                    "No qualifying 2025 test "
                    "games were returned."
                )
            ), 500

        fitted_hfa, training_loss_fitted = (
            fit_home_field_elo(
                training_games
            )
        )

        training_loss_zero = (
            calibration_log_loss(
                training_games,
                0.0
            )
        )

        training_loss_67 = (
            calibration_log_loss(
                training_games,
                CALIBRATED_HOME_FIELD_ELO
            )
        )

        test_loss_zero = (
            calibration_log_loss(
                test_games,
                0.0
            )
        )

        test_loss_67 = (
            calibration_log_loss(
                test_games,
                CALIBRATED_HOME_FIELD_ELO
            )
        )

        test_loss_fitted = (
            calibration_log_loss(
                test_games,
                fitted_hfa
            )
        )

        fitted_vs_67_difference = (
            test_loss_fitted
            - test_loss_67
        )

        if test_loss_fitted < test_loss_67:
            better_2025 = "fitted_2021_2024"
        elif test_loss_67 < test_loss_fitted:
            better_2025 = "current_67"
        else:
            better_2025 = "tie"

        return jsonify(
            success=True,

            design={
                "training_period": {
                    "start_year": 2021,
                    "end_year": 2024
                },
                "test_period": {
                    "start_year": 2025,
                    "end_year": 2025
                },
                "test_year_excluded_from_fit": True,
                "objective": (
                    "Fit home-field Elo on "
                    "2021-2024 only, then evaluate "
                    "binary log loss on unseen "
                    "2025 games."
                )
            },

            filters={
                "completed_only": True,
                "regular_season_only": True,
                "non_neutral_only": True,
                "fbs_vs_fbs_only": True,
                "ties_excluded": True,
                "pregame_elo_required": True
            },

            training={
                **summarize_calibration_games(
                    training_games
                ),
                "games_by_year": training_counts,
                "fitted_home_field_elo_points": (
                    fitted_hfa
                ),
                "log_loss_at_fitted_hfa": round(
                    training_loss_fitted,
                    6
                ),
                "log_loss_at_zero_hfa": round(
                    training_loss_zero,
                    6
                ),
                "log_loss_at_67_hfa": round(
                    training_loss_67,
                    6
                )
            },

            test_2025={
                **summarize_calibration_games(
                    test_games
                ),
                "games_by_year": test_counts,
                "log_loss_at_fitted_hfa": round(
                    test_loss_fitted,
                    6
                ),
                "log_loss_at_67_hfa": round(
                    test_loss_67,
                    6
                ),
                "log_loss_at_zero_hfa": round(
                    test_loss_zero,
                    6
                ),
                "fitted_minus_67_log_loss": round(
                    fitted_vs_67_difference,
                    6
                ),
                "better_on_unseen_2025": (
                    better_2025
                )
            },

            live_model={
                "home_field_elo_points": (
                    CALIBRATED_HOME_FIELD_ELO
                ),
                "changed_by_this_endpoint": False
            },

            interpretation={
                "lower_log_loss_is_better": True,
                "comparison": (
                    "The 2021-2024 fitted HFA and "
                    "the current 67-point HFA are "
                    "both evaluated on the same "
                    "unseen 2025 test games."
                ),
                "decision_rule": (
                    "Do not change the live 67-point "
                    "setting automatically. Review "
                    "the out-of-sample result first."
                )
            }
        )

    except RuntimeError as e:
        return jsonify(
            success=False,
            error=str(e)
        ), 500

    except requests.RequestException as e:
        return jsonify(
            success=False,
            error=str(e)
        ), 502

