from flask import Flask, jsonify, request
import os
import math
import requests
from datetime import datetime
from difflib import SequenceMatcher
from decimal import Decimal, ROUND_CEILING

app = Flask(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "").strip()

# Calibrated from 3,611 completed, non-neutral,
# FBS-vs-FBS regular-season games from 2021-2025.
CALIBRATED_HOME_FIELD_ELO = 67.0

# ============================================================
# EXECUTION-LAYER CONFIGURATION
#
# These settings affect ONLY execution-cost analysis.
# They DO NOT change the probability model.
#
# Kalshi's general taker-fee schedule uses:
#   fee = round up(rate * contracts * price * (1 - price))
#
# The default rate is configurable because some Kalshi markets
# can use a different fee schedule.
# ============================================================

KALSHI_TAKER_FEE_RATE = float(
    os.environ.get(
        "KALSHI_TAKER_FEE_RATE",
        "0.07"
    )
)

EXECUTION_DEFAULT_CONTRACTS = int(
    os.environ.get(
        "EXECUTION_DEFAULT_CONTRACTS",
        "1"
    )
)

MAX_ALLOWED_SLIPPAGE_DOLLARS = float(
    os.environ.get(
        "MAX_ALLOWED_SLIPPAGE_DOLLARS",
        "0.02"
    )
)

MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS = float(
    os.environ.get(
        "MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS",
        "0.05"
    )
)


@app.get("/")
def home():
    return jsonify(
        status="ok",
        message="Kalshi bridge is running",
        cfbd_configured=bool(CFBD_API_KEY),
        calibrated_home_field_elo=CALIBRATED_HOME_FIELD_ELO,
        execution_layer={
            "enabled": True,
            "default_contracts": EXECUTION_DEFAULT_CONTRACTS,
            "taker_fee_rate": KALSHI_TAKER_FEE_RATE,
            "max_slippage_dollars":
                MAX_ALLOWED_SLIPPAGE_DOLLARS,
            "max_bid_ask_spread_dollars":
                MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS
        }
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
# KALSHI ORDER-BOOK / EXECUTION LAYER
# ============================================================

def ceil_to_cent(value):
    """
    Round a positive dollar amount UP to the next cent.

    Kalshi's published fee formula specifies round-up behavior.
    """
    value = Decimal(str(value))

    if value <= 0:
        return 0.0

    return float(
        value.quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING
        )
    )


def get_orderbook(ticker, depth=0):
    """
    Fetch the public Kalshi order book for one market.

    Kalshi returns YES bids and NO bids. Because binary
    contracts are complementary:
        YES ask = 1 - NO bid
        NO ask  = 1 - YES bid
    """
    return kalshi_get(
        f"/markets/{ticker}/orderbook",
        params={
            "depth": depth
        }
    )


def parse_orderbook_levels(levels):
    parsed = []

    for level in levels or []:
        if (
            not isinstance(level, (list, tuple))
            or len(level) < 2
        ):
            continue

        price = number(level[0])
        quantity = number(level[1])

        if (
            price is None
            or quantity is None
            or price <= 0
            or price >= 1
            or quantity <= 0
        ):
            continue

        parsed.append({
            "price": price,
            "quantity": quantity
        })

    return parsed


def normalize_orderbook(orderbook_response):
    """
    Convert Kalshi's bid-only order book into explicit
    YES/NO bid and ask ladders.
    """
    raw = (
        orderbook_response.get(
            "orderbook_fp",
            {}
        )
        if isinstance(
            orderbook_response,
            dict
        )
        else {}
    )

    yes_bids = parse_orderbook_levels(
        raw.get("yes_dollars")
    )

    no_bids = parse_orderbook_levels(
        raw.get("no_dollars")
    )

    yes_bids.sort(
        key=lambda level:
            level["price"],
        reverse=True
    )

    no_bids.sort(
        key=lambda level:
            level["price"],
        reverse=True
    )

    yes_asks = [
        {
            "price": round(
                1.0 - level["price"],
                4
            ),
            "quantity":
                level["quantity"]
        }
        for level in no_bids
    ]

    no_asks = [
        {
            "price": round(
                1.0 - level["price"],
                4
            ),
            "quantity":
                level["quantity"]
        }
        for level in yes_bids
    ]

    yes_asks.sort(
        key=lambda level:
            level["price"]
    )

    no_asks.sort(
        key=lambda level:
            level["price"]
    )

    return {
        "yes_bids": yes_bids,
        "yes_asks": yes_asks,
        "no_bids": no_bids,
        "no_asks": no_asks
    }


def best_price(levels):
    if not levels:
        return None

    return number(
        levels[0].get(
            "price"
        )
    )


def book_spread(
    bid_levels,
    ask_levels
):
    best_bid = best_price(
        bid_levels
    )

    best_ask = best_price(
        ask_levels
    )

    if (
        best_bid is None
        or best_ask is None
    ):
        return None

    return max(
        0.0,
        best_ask - best_bid
    )


def walk_orderbook(
    ask_levels,
    desired_contracts
):
    """
    Simulate an immediate taker BUY by consuming asks from
    cheapest to most expensive.

    Returns weighted-average fill price, slippage from the
    best ask, fill details, and whether the requested size
    can be filled completely.
    """
    desired_contracts = number(
        desired_contracts
    )

    if (
        desired_contracts is None
        or desired_contracts <= 0
    ):
        return {
            "requested_contracts": 0,
            "filled_contracts": 0,
            "unfilled_contracts": 0,
            "full_fill": False,
            "best_ask": None,
            "weighted_average_fill_price": None,
            "slippage_dollars": None,
            "gross_contract_cost": 0.0,
            "fills": []
        }

    desired_contracts = float(
        desired_contracts
    )

    remaining = desired_contracts
    filled = 0.0
    gross_cost = 0.0
    fills = []

    sorted_asks = sorted(
        ask_levels or [],
        key=lambda level:
            level["price"]
    )

    best_ask = (
        sorted_asks[0]["price"]
        if sorted_asks
        else None
    )

    for level in sorted_asks:
        if remaining <= 0:
            break

        price = number(
            level.get("price")
        )

        available = number(
            level.get("quantity")
        )

        if (
            price is None
            or available is None
            or available <= 0
        ):
            continue

        fill_quantity = min(
            remaining,
            available
        )

        level_cost = (
            fill_quantity
            * price
        )

        fills.append({
            "price": round(
                price,
                4
            ),
            "available_contracts":
                round(
                    available,
                    4
                ),
            "filled_contracts":
                round(
                    fill_quantity,
                    4
                ),
            "level_cost_dollars":
                round(
                    level_cost,
                    4
                )
        })

        filled += fill_quantity
        gross_cost += level_cost
        remaining -= fill_quantity

    full_fill = (
        remaining <= 0.0000001
    )

    average_fill_price = (
        gross_cost / filled
        if filled > 0
        else None
    )

    slippage = (
        average_fill_price - best_ask
        if (
            average_fill_price is not None
            and best_ask is not None
        )
        else None
    )

    return {
        "requested_contracts":
            round(
                desired_contracts,
                4
            ),
        "filled_contracts":
            round(
                filled,
                4
            ),
        "unfilled_contracts":
            round(
                max(
                    0.0,
                    desired_contracts - filled
                ),
                4
            ),
        "full_fill":
            full_fill,
        "best_ask":
            (
                round(
                    best_ask,
                    4
                )
                if best_ask is not None
                else None
            ),
        "weighted_average_fill_price":
            (
                round(
                    average_fill_price,
                    6
                )
                if average_fill_price is not None
                else None
            ),
        "slippage_dollars":
            (
                round(
                    max(
                        0.0,
                        slippage
                    ),
                    6
                )
                if slippage is not None
                else None
            ),
        "gross_contract_cost":
            round(
                gross_cost,
                4
            ),
        "fills":
            fills
    }


def estimated_kalshi_taker_fee(
    fills,
    fee_rate=None
):
    """
    Estimate taker fees conservatively.

    The default general Kalshi formula is:
        rate * contracts * price * (1 - price)

    Because a market order can sweep multiple price levels,
    this estimates the fee at each executed price level and
    rounds each level upward to the next cent. This is
    intentionally conservative.

    KALSHI_TAKER_FEE_RATE is configurable because particular
    Kalshi markets can use different fee schedules.
    """
    if fee_rate is None:
        fee_rate = (
            KALSHI_TAKER_FEE_RATE
        )

    fee_rate = number(
        fee_rate
    )

    if (
        fee_rate is None
        or fee_rate < 0
    ):
        return {
            "fee_rate": None,
            "estimated_fee_dollars": None,
            "fee_details": []
        }

    total_fee = 0.0
    fee_details = []

    for fill in fills or []:
        quantity = number(
            fill.get(
                "filled_contracts"
            )
        )

        price = number(
            fill.get(
                "price"
            )
        )

        if (
            quantity is None
            or price is None
            or quantity <= 0
            or price <= 0
            or price >= 1
        ):
            continue

        raw_fee = (
            fee_rate
            * quantity
            * price
            * (1.0 - price)
        )

        rounded_fee = (
            ceil_to_cent(
                raw_fee
            )
        )

        total_fee += rounded_fee

        fee_details.append({
            "price":
                round(
                    price,
                    4
                ),
            "contracts":
                round(
                    quantity,
                    4
                ),
            "raw_formula_fee_dollars":
                round(
                    raw_fee,
                    6
                ),
            "estimated_rounded_fee_dollars":
                round(
                    rounded_fee,
                    2
                )
        })

    return {
        "fee_rate":
            fee_rate,
        "fee_rounding":
            (
                "Conservative estimate: "
                "formula applied and rounded "
                "up separately at each price level."
            ),
        "estimated_fee_dollars":
            round(
                total_fee,
                2
            ),
        "fee_details":
            fee_details
    }


def execution_side_metrics(
    side,
    fair_probability,
    bid_levels,
    ask_levels,
    desired_contracts
):
    """
    Evaluate one BUY side (YES or NO) using actual order-book
    depth, weighted fill price, estimated taker fees, spread,
    slippage, and execution-adjusted net edge.
    """
    fair_probability = (
        clamp_probability(
            fair_probability
        )
    )

    walk = walk_orderbook(
        ask_levels,
        desired_contracts
    )

    spread = book_spread(
        bid_levels,
        ask_levels
    )

    fee = estimated_kalshi_taker_fee(
        walk.get(
            "fills",
            []
        )
    )

    filled_contracts = number(
        walk.get(
            "filled_contracts"
        )
    )

    gross_cost = number(
        walk.get(
            "gross_contract_cost"
        )
    )

    fee_total = number(
        fee.get(
            "estimated_fee_dollars"
        )
    )

    all_in_cost_per_contract = None
    net_edge = None
    net_roi = None
    expected_profit_total = None

    if (
        filled_contracts is not None
        and filled_contracts > 0
        and gross_cost is not None
        and fee_total is not None
    ):
        all_in_total = (
            gross_cost
            + fee_total
        )

        all_in_cost_per_contract = (
            all_in_total
            / filled_contracts
        )

        if (
            fair_probability is not None
            and walk.get(
                "full_fill"
            )
        ):
            net_edge = (
                fair_probability
                - all_in_cost_per_contract
            )

            if (
                all_in_cost_per_contract
                > 0
            ):
                net_roi = (
                    net_edge
                    / all_in_cost_per_contract
                )

            expected_profit_total = (
                net_edge
                * filled_contracts
            )

    slippage = number(
        walk.get(
            "slippage_dollars"
        )
    )

    spread_pass = (
        spread is not None
        and spread
        <= MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS
    )

    slippage_pass = (
        slippage is not None
        and slippage
        <= MAX_ALLOWED_SLIPPAGE_DOLLARS
    )

    full_fill_pass = bool(
        walk.get(
            "full_fill"
        )
    )

    liquidity_pass = (
        full_fill_pass
        and slippage_pass
        and spread_pass
    )

    best_ask = number(
        walk.get(
            "best_ask"
        )
    )

    gross_edge = (
        fair_probability - best_ask
        if (
            fair_probability is not None
            and best_ask is not None
        )
        else None
    )

    return {
        "side":
            side,
        "fair_probability":
            (
                round(
                    fair_probability,
                    6
                )
                if fair_probability is not None
                else None
            ),
        "requested_contracts":
            walk.get(
                "requested_contracts"
            ),
        "orderbook_walk":
            walk,
        "best_bid":
            (
                round(
                    best_price(
                        bid_levels
                    ),
                    4
                )
                if best_price(
                    bid_levels
                ) is not None
                else None
            ),
        "best_ask":
            walk.get(
                "best_ask"
            ),
        "bid_ask_spread":
            (
                round(
                    spread,
                    6
                )
                if spread is not None
                else None
            ),
        "fee":
            fee,
        "all_in_cost_per_contract":
            (
                round(
                    all_in_cost_per_contract,
                    6
                )
                if all_in_cost_per_contract
                is not None
                else None
            ),
        "gross_edge_before_fees_and_slippage":
            (
                round(
                    gross_edge,
                    6
                )
                if gross_edge is not None
                else None
            ),
        "gross_edge_percentage_points":
            (
                round(
                    gross_edge * 100,
                    2
                )
                if gross_edge is not None
                else None
            ),
        "net_execution_edge":
            (
                round(
                    net_edge,
                    6
                )
                if net_edge is not None
                else None
            ),
        "net_execution_edge_percentage_points":
            (
                round(
                    net_edge * 100,
                    2
                )
                if net_edge is not None
                else None
            ),
        "net_expected_roi":
            (
                round(
                    net_roi,
                    6
                )
                if net_roi is not None
                else None
            ),
        "net_expected_roi_percent":
            (
                round(
                    net_roi * 100,
                    2
                )
                if net_roi is not None
                else None
            ),
        "expected_profit_for_requested_size":
            (
                round(
                    expected_profit_total,
                    4
                )
                if expected_profit_total
                is not None
                else None
            ),
        "checks": {
            "full_fill":
                full_fill_pass,
            "spread_pass":
                spread_pass,
            "slippage_pass":
                slippage_pass,
            "liquidity_pass":
                liquidity_pass,
            "max_allowed_spread_dollars":
                MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS,
            "max_allowed_slippage_dollars":
                MAX_ALLOWED_SLIPPAGE_DOLLARS
        }
    }


def build_execution_analysis(
    ticker,
    fair_yes_probability,
    desired_contracts
):
    """
    Fetch one order book and evaluate BUY YES and BUY NO
    from the same snapshot.
    """
    fair_yes = clamp_probability(
        fair_yes_probability
    )

    if fair_yes is None:
        return {
            "available": False,
            "reason":
                "No independent fair probability is available."
        }

    fair_no = 1.0 - fair_yes

    try:
        raw_orderbook = (
            get_orderbook(
                ticker,
                depth=0
            )
        )

        book = normalize_orderbook(
            raw_orderbook
        )

        return {
            "available": True,
            "ticker":
                ticker,
            "desired_contracts":
                desired_contracts,
            "fee_configuration": {
                "taker_fee_rate":
                    KALSHI_TAKER_FEE_RATE,
                "rate_source":
                    (
                        "Configurable default. "
                        "Override with the "
                        "KALSHI_TAKER_FEE_RATE "
                        "environment variable when "
                        "a market uses a different "
                        "fee schedule."
                    )
            },
            "liquidity_configuration": {
                "max_allowed_slippage_dollars":
                    MAX_ALLOWED_SLIPPAGE_DOLLARS,
                "max_allowed_bid_ask_spread_dollars":
                    MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS
            },
            "yes": execution_side_metrics(
                "YES",
                fair_yes,
                book["yes_bids"],
                book["yes_asks"],
                desired_contracts
            ),
            "no": execution_side_metrics(
                "NO",
                fair_no,
                book["no_bids"],
                book["no_asks"],
                desired_contracts
            )
        }

    except requests.RequestException as e:
        return {
            "available": False,
            "ticker":
                ticker,
            "reason":
                (
                    "Kalshi order-book request failed."
                ),
            "error":
                str(e)
        }


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
    fair_yes_probability=None,
    desired_contracts=None
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

    if desired_contracts is None:
        desired_contracts = (
            EXECUTION_DEFAULT_CONTRACTS
        )

    execution = None

    if (
        market_type == "winner"
        and fair_yes is not None
        and m.get("ticker")
    ):
        execution = (
            build_execution_analysis(
                m.get("ticker"),
                fair_yes,
                desired_contracts
            )
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

        "execution_analysis":
            execution,

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
    fair_probabilities=None,
    desired_contracts=None
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
            ),
            desired_contracts
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


@app.get("/orderbook/<ticker>")
def orderbook(ticker):
    try:
        depth_text = request.args.get(
            "depth",
            "0"
        ).strip()

        try:
            depth = int(
                depth_text
            )
        except ValueError:
            return jsonify(
                error=(
                    "depth must be an integer "
                    "from 0 to 100."
                )
            ), 400

        if depth < 0 or depth > 100:
            return jsonify(
                error=(
                    "depth must be between "
                    "0 and 100."
                )
            ), 400

        raw = get_orderbook(
            ticker,
            depth=depth
        )

        normalized = (
            normalize_orderbook(
                raw
            )
        )

        return jsonify(
            ticker=ticker,
            depth=depth,
            raw_orderbook=raw,
            normalized_orderbook=(
                normalized
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

    contracts_text = request.args.get(
        "contracts",
        str(
            EXECUTION_DEFAULT_CONTRACTS
        )
    ).strip()

    try:
        desired_contracts = int(
            contracts_text
        )
    except ValueError:
        return jsonify(
            error=(
                "contracts must be a whole "
                "number from 1 to 10000."
            )
        ), 400

    if (
        desired_contracts < 1
        or desired_contracts > 10000
    ):
        return jsonify(
            error=(
                "contracts must be between "
                "1 and 10000."
            )
        ), 400

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
                winner_probabilities,
                desired_contracts
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
                        "Raw edge remains independent "
                        "fair probability minus the "
                        "displayed executable ask. "
                        "Winner markets also include "
                        "execution-adjusted net edge "
                        "after walking the order book "
                        "and estimating taker fees."
                    ),

                "expected_profit":
                    (
                        "Execution-adjusted expected "
                        "profit uses fair probability "
                        "minus all-in cost per contract."
                    ),

                "expected_roi":
                    (
                        "Execution-adjusted expected "
                        "ROI is net edge divided by "
                        "all-in cost per contract."
                    ),

                "execution_layer":
                    (
                        "For modeled winner contracts, "
                        "the bridge derives asks from "
                        "the reciprocal Kalshi bid book, "
                        "walks the requested size, "
                        "calculates a weighted-average "
                        "fill price, estimates taker "
                        "fees, measures slippage and "
                        "spread, and applies liquidity "
                        "checks."
                    ),

                "limitations":
                    (
                        "Injuries, weather, matchup "
                        "efficiency, roster changes and "
                        "model calibration beyond Elo "
                        "plus home field are not yet "
                        "included. Fee estimates use a "
                        "configurable default schedule "
                        "and should be changed for "
                        "markets with special fee terms."
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

            execution_request={
                "contracts":
                    desired_contracts,
                "taker_fee_rate":
                    KALSHI_TAKER_FEE_RATE,
                "max_allowed_slippage_dollars":
                    MAX_ALLOWED_SLIPPAGE_DOLLARS,
                "max_allowed_bid_ask_spread_dollars":
                    MAX_ALLOWED_BID_ASK_SPREAD_DOLLARS
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


# ============================================================
# OUT-OF-SAMPLE ELO CURVE VALIDATION
# Fits BOTH home-field advantage and the Elo probability
# curve scale on 2021-2024, then evaluates on unseen 2025.
# This endpoint DOES NOT change the live model automatically.
# ============================================================

def elo_probability_with_scale(
    rating_difference,
    scale
):
    return (
        1.0
        /
        (
            1.0
            + math.pow(
                10.0,
                -rating_difference
                / scale
            )
        )
    )


def elo_model_metrics(
    games,
    home_field_elo,
    scale
):
    if not games:
        return {
            "log_loss": None,
            "brier_score": None
        }

    total_log_loss = 0.0
    total_brier = 0.0

    for game in games:
        rating_difference = (
            game["home_elo"]
            - game["away_elo"]
            + home_field_elo
        )

        probability = (
            elo_probability_with_scale(
                rating_difference,
                scale
            )
        )

        probability = max(
            0.000001,
            min(
                0.999999,
                probability
            )
        )

        actual = game["home_win"]

        total_log_loss += -(
            actual * math.log(probability)
            +
            (1 - actual)
            * math.log(1 - probability)
        )

        total_brier += (
            probability - actual
        ) ** 2

    sample_size = len(games)

    return {
        "log_loss": (
            total_log_loss
            / sample_size
        ),
        "brier_score": (
            total_brier
            / sample_size
        )
    }


def fit_elo_curve(games):
    """
    Fast two-parameter Elo curve fit.

    The Elo probability model can be written as ordinary logistic
    regression:

        p = sigmoid(a + b * elo_difference)

    where:
        scale = ln(10) / b
        home_field_elo = a / b

    Newton-Raphson therefore fits HFA and scale directly in a few
    iterations instead of evaluating millions of grid-search
    combinations. The function keeps the same return values as the
    previous fitter: (best_hfa, best_scale, best_log_loss).
    """
    if not games:
        raise RuntimeError("Cannot fit Elo curve without games.")

    # Start from the current live shape: 67 HFA / 400 scale.
    b = math.log(10.0) / 400.0
    a = b * CALIBRATED_HOME_FIELD_ELO

    for _ in range(30):
        grad_a = 0.0
        grad_b = 0.0
        h_aa = 0.0
        h_ab = 0.0
        h_bb = 0.0

        for game in games:
            x = game["home_elo"] - game["away_elo"]
            y = game["home_win"]

            z = a + b * x

            # Numerically stable logistic probability.
            if z >= 0:
                exp_neg = math.exp(-z)
                probability = 1.0 / (1.0 + exp_neg)
            else:
                exp_pos = math.exp(z)
                probability = exp_pos / (1.0 + exp_pos)

            error = probability - y
            weight = probability * (1.0 - probability)

            grad_a += error
            grad_b += error * x

            h_aa += weight
            h_ab += weight * x
            h_bb += weight * x * x

        determinant = (h_aa * h_bb) - (h_ab * h_ab)

        if abs(determinant) < 1e-12:
            break

        delta_a = (
            (h_bb * grad_a)
            - (h_ab * grad_b)
        ) / determinant

        delta_b = (
            (-h_ab * grad_a)
            + (h_aa * grad_b)
        ) / determinant

        new_a = a - delta_a
        new_b = b - delta_b

        # Keep the fitted curve in a sensible positive-scale range.
        min_b = math.log(10.0) / 700.0
        max_b = math.log(10.0) / 150.0
        new_b = max(min_b, min(max_b, new_b))

        # Equivalent HFA range used by the prior validation search.
        new_hfa = new_a / new_b
        new_hfa = max(0.0, min(150.0, new_hfa))
        new_a = new_hfa * new_b

        if (
            abs(new_a - a) < 1e-10
            and abs(new_b - b) < 1e-12
        ):
            a = new_a
            b = new_b
            break

        a = new_a
        b = new_b

    best_scale = math.log(10.0) / b
    best_hfa = a / b

    metrics = elo_model_metrics(
        games,
        best_hfa,
        best_scale
    )

    return (
        round(best_hfa, 3),
        round(best_scale, 3),
        metrics["log_loss"]
    )


def elo_calibration_buckets(
    games,
    home_field_elo,
    scale
):
    buckets = {}

    for start_pct in range(
        0,
        100,
        10
    ):
        key = (
            f"{start_pct:02d}-"
            f"{start_pct + 10:02d}%"
        )

        buckets[key] = {
            "count": 0,
            "predicted_sum": 0.0,
            "actual_sum": 0
        }

    for game in games:
        rating_difference = (
            game["home_elo"]
            - game["away_elo"]
            + home_field_elo
        )

        probability = (
            elo_probability_with_scale(
                rating_difference,
                scale
            )
        )

        pct = max(
            0.0,
            min(
                99.999999,
                probability * 100.0
            )
        )

        start_pct = int(
            pct // 10
        ) * 10

        key = (
            f"{start_pct:02d}-"
            f"{start_pct + 10:02d}%"
        )

        buckets[key]["count"] += 1
        buckets[key]["predicted_sum"] += probability
        buckets[key]["actual_sum"] += (
            game["home_win"]
        )

    result = []

    for key, values in buckets.items():
        count = values["count"]

        if count == 0:
            continue

        avg_predicted = (
            values["predicted_sum"]
            / count
        )

        actual_rate = (
            values["actual_sum"]
            / count
        )

        result.append({
            "bucket": key,
            "count": count,
            "average_predicted_probability": round(
                avg_predicted,
                4
            ),
            "actual_home_win_rate": round(
                actual_rate,
                4
            ),
            "calibration_gap": round(
                actual_rate - avg_predicted,
                4
            )
        })

    return result


@app.get("/out-of-sample-elo-model")
def out_of_sample_elo_model():
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

        (
            fitted_hfa,
            fitted_scale,
            training_loss_fitted
        ) = fit_elo_curve(
            training_games
        )

        baseline_training = (
            elo_model_metrics(
                training_games,
                CALIBRATED_HOME_FIELD_ELO,
                400.0
            )
        )

        fitted_training = (
            elo_model_metrics(
                training_games,
                fitted_hfa,
                fitted_scale
            )
        )

        baseline_test = (
            elo_model_metrics(
                test_games,
                CALIBRATED_HOME_FIELD_ELO,
                400.0
            )
        )

        fitted_test = (
            elo_model_metrics(
                test_games,
                fitted_hfa,
                fitted_scale
            )
        )

        test_log_loss_difference = (
            fitted_test["log_loss"]
            - baseline_test["log_loss"]
        )

        test_brier_difference = (
            fitted_test["brier_score"]
            - baseline_test["brier_score"]
        )

        if (
            fitted_test["log_loss"]
            < baseline_test["log_loss"]
        ):
            better_log_loss = (
                "fitted_elo_curve"
            )
        elif (
            baseline_test["log_loss"]
            < fitted_test["log_loss"]
        ):
            better_log_loss = (
                "current_live_shape"
            )
        else:
            better_log_loss = "tie"

        if (
            fitted_test["brier_score"]
            < baseline_test["brier_score"]
        ):
            better_brier = (
                "fitted_elo_curve"
            )
        elif (
            baseline_test["brier_score"]
            < fitted_test["brier_score"]
        ):
            better_brier = (
                "current_live_shape"
            )
        else:
            better_brier = "tie"

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
                    "Fit both home-field Elo and "
                    "the Elo logistic scale on "
                    "2021-2024 only, then evaluate "
                    "the fitted probability curve "
                    "on unseen 2025 games."
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

            current_live_shape={
                "home_field_elo_points": (
                    CALIBRATED_HOME_FIELD_ELO
                ),
                "elo_scale": 400.0,
                "formula": (
                    "1 / (1 + 10^(-rating_difference/400))"
                )
            },

            fitted_2021_2024={
                "home_field_elo_points": (
                    fitted_hfa
                ),
                "elo_scale": (
                    fitted_scale
                ),
                "training_log_loss": round(
                    training_loss_fitted,
                    6
                ),
                "changed_live_model": False
            },

            training={
                **summarize_calibration_games(
                    training_games
                ),
                "games_by_year": training_counts,
                "current_live_shape": {
                    "log_loss": round(
                        baseline_training["log_loss"],
                        6
                    ),
                    "brier_score": round(
                        baseline_training["brier_score"],
                        6
                    )
                },
                "fitted_elo_curve": {
                    "log_loss": round(
                        fitted_training["log_loss"],
                        6
                    ),
                    "brier_score": round(
                        fitted_training["brier_score"],
                        6
                    )
                }
            },

            test_2025={
                **summarize_calibration_games(
                    test_games
                ),
                "games_by_year": test_counts,

                "current_live_shape": {
                    "home_field_elo_points": (
                        CALIBRATED_HOME_FIELD_ELO
                    ),
                    "elo_scale": 400.0,
                    "log_loss": round(
                        baseline_test["log_loss"],
                        6
                    ),
                    "brier_score": round(
                        baseline_test["brier_score"],
                        6
                    ),
                    "calibration_buckets": (
                        elo_calibration_buckets(
                            test_games,
                            CALIBRATED_HOME_FIELD_ELO,
                            400.0
                        )
                    )
                },

                "fitted_elo_curve": {
                    "home_field_elo_points": (
                        fitted_hfa
                    ),
                    "elo_scale": (
                        fitted_scale
                    ),
                    "log_loss": round(
                        fitted_test["log_loss"],
                        6
                    ),
                    "brier_score": round(
                        fitted_test["brier_score"],
                        6
                    ),
                    "calibration_buckets": (
                        elo_calibration_buckets(
                            test_games,
                            fitted_hfa,
                            fitted_scale
                        )
                    )
                },

                "fitted_minus_current_log_loss": round(
                    test_log_loss_difference,
                    6
                ),

                "fitted_minus_current_brier_score": round(
                    test_brier_difference,
                    6
                ),

                "better_on_log_loss": (
                    better_log_loss
                ),

                "better_on_brier_score": (
                    better_brier
                )
            },

            interpretation={
                "lower_log_loss_is_better": True,
                "lower_brier_score_is_better": True,
                "why_this_matters": (
                    "This tests whether the standard "
                    "400-point Elo probability curve "
                    "is too aggressive or too "
                    "conservative for college football."
                ),
                "decision_rule": (
                    "Do not change the live model "
                    "unless the fitted curve improves "
                    "unseen 2025 performance by a "
                    "meaningful amount and behaves "
                    "sensibly across probability buckets."
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


# ============================================================
# WALK-FORWARD ELO VALIDATION
# Re-fits HFA + Elo probability scale using ONLY seasons prior
# to each test year, then evaluates on that unseen test season.
# This endpoint DOES NOT change the live model automatically.
# ============================================================

@app.get("/walk-forward-elo-model")
def walk_forward_elo_model():
    try:
        # Fetch each historical season once, then reuse it for every fold.
        games_by_year = {}

        for year in range(2021, 2026):
            year_games, _ = calibration_games(year, year)
            games_by_year[year] = year_games

        test_years = [2024, 2025]
        yearly_results = []

        total_test_games = 0
        weighted_current_log_loss = 0.0
        weighted_fitted_log_loss = 0.0
        weighted_current_brier = 0.0
        weighted_fitted_brier = 0.0

        fitted_log_loss_wins = 0
        current_log_loss_wins = 0
        log_loss_ties = 0
        fitted_brier_wins = 0
        current_brier_wins = 0
        brier_ties = 0

        fitted_hfas = []
        fitted_scales = []

        for test_year in test_years:
            training_games = []
            training_counts = {}

            for year in range(2021, test_year):
                year_games = games_by_year.get(year, [])
                training_games.extend(year_games)
                training_counts[str(year)] = len(year_games)

            test_games = games_by_year.get(test_year, [])
            test_counts = {str(test_year): len(test_games)}

            if not training_games:
                return jsonify(
                    success=False,
                    error=(
                        f"No qualifying training games were available "
                        f"before {test_year}."
                    )
                ), 500

            if not test_games:
                return jsonify(
                    success=False,
                    error=(
                        f"No qualifying test games were available "
                        f"for {test_year}."
                    )
                ), 500

            fitted_hfa, fitted_scale, fitted_training_loss = (
                fit_elo_curve(training_games)
            )

            current_test = elo_model_metrics(
                test_games,
                CALIBRATED_HOME_FIELD_ELO,
                400.0
            )

            fitted_test = elo_model_metrics(
                test_games,
                fitted_hfa,
                fitted_scale
            )

            log_loss_difference = (
                fitted_test["log_loss"]
                - current_test["log_loss"]
            )

            brier_difference = (
                fitted_test["brier_score"]
                - current_test["brier_score"]
            )

            if log_loss_difference < 0:
                log_loss_winner = "fitted_elo_curve"
                fitted_log_loss_wins += 1
            elif log_loss_difference > 0:
                log_loss_winner = "current_live_shape"
                current_log_loss_wins += 1
            else:
                log_loss_winner = "tie"
                log_loss_ties += 1

            if brier_difference < 0:
                brier_winner = "fitted_elo_curve"
                fitted_brier_wins += 1
            elif brier_difference > 0:
                brier_winner = "current_live_shape"
                current_brier_wins += 1
            else:
                brier_winner = "tie"
                brier_ties += 1

            test_sample_size = len(test_games)
            total_test_games += test_sample_size

            weighted_current_log_loss += (
                current_test["log_loss"] * test_sample_size
            )
            weighted_fitted_log_loss += (
                fitted_test["log_loss"] * test_sample_size
            )
            weighted_current_brier += (
                current_test["brier_score"] * test_sample_size
            )
            weighted_fitted_brier += (
                fitted_test["brier_score"] * test_sample_size
            )

            fitted_hfas.append(fitted_hfa)
            fitted_scales.append(fitted_scale)

            yearly_results.append({
                "test_year": test_year,
                "training_period": {
                    "start_year": 2021,
                    "end_year": test_year - 1
                },
                "training_sample_size": len(training_games),
                "training_games_by_year": training_counts,
                "test_sample_size": test_sample_size,
                "test_games_by_year": test_counts,
                "fitted_on_prior_seasons_only": {
                    "home_field_elo_points": fitted_hfa,
                    "elo_scale": fitted_scale,
                    "training_log_loss": round(
                        fitted_training_loss, 6
                    )
                },
                "current_live_shape": {
                    "home_field_elo_points":
                        CALIBRATED_HOME_FIELD_ELO,
                    "elo_scale": 400.0,
                    "test_log_loss": round(
                        current_test["log_loss"], 6
                    ),
                    "test_brier_score": round(
                        current_test["brier_score"], 6
                    )
                },
                "fitted_elo_curve": {
                    "home_field_elo_points": fitted_hfa,
                    "elo_scale": fitted_scale,
                    "test_log_loss": round(
                        fitted_test["log_loss"], 6
                    ),
                    "test_brier_score": round(
                        fitted_test["brier_score"], 6
                    )
                },
                "fitted_minus_current_log_loss": round(
                    log_loss_difference, 6
                ),
                "fitted_minus_current_brier_score": round(
                    brier_difference, 6
                ),
                "better_on_log_loss": log_loss_winner,
                "better_on_brier_score": brier_winner
            })

        if total_test_games == 0:
            return jsonify(
                success=False,
                error="No unseen test games were available."
            ), 500

        current_log_loss = (
            weighted_current_log_loss / total_test_games
        )
        fitted_log_loss = (
            weighted_fitted_log_loss / total_test_games
        )
        current_brier = (
            weighted_current_brier / total_test_games
        )
        fitted_brier = (
            weighted_fitted_brier / total_test_games
        )

        return jsonify(
            success=True,
            design={
                "method": "expanding-window walk-forward validation",
                "fit_method": (
                    "two-parameter logistic Newton-Raphson fit "
                    "equivalent to the Elo probability curve"
                ),
                "test_years": test_years,
                "training_rule": (
                    "Each test season is predicted using a model "
                    "fitted only on earlier seasons."
                ),
                "future_data_used_in_fit": False,
                "historical_data_fetched_once": True,
                "live_model_changed": False
            },
            filters={
                "completed_only": True,
                "regular_season_only": True,
                "non_neutral_only": True,
                "fbs_vs_fbs_only": True,
                "ties_excluded": True,
                "pregame_elo_required": True
            },
            current_live_shape={
                "home_field_elo_points":
                    CALIBRATED_HOME_FIELD_ELO,
                "elo_scale": 400.0
            },
            yearly_results=yearly_results,
            aggregate={
                "total_unseen_test_games": total_test_games,
                "current_live_shape": {
                    "log_loss": round(current_log_loss, 6),
                    "brier_score": round(current_brier, 6)
                },
                "walk_forward_fitted_curve": {
                    "log_loss": round(fitted_log_loss, 6),
                    "brier_score": round(fitted_brier, 6)
                },
                "fitted_minus_current_log_loss": round(
                    fitted_log_loss - current_log_loss, 6
                ),
                "fitted_minus_current_brier_score": round(
                    fitted_brier - current_brier, 6
                ),
                "season_wins": {
                    "log_loss": {
                        "fitted_elo_curve": fitted_log_loss_wins,
                        "current_live_shape": current_log_loss_wins,
                        "ties": log_loss_ties
                    },
                    "brier_score": {
                        "fitted_elo_curve": fitted_brier_wins,
                        "current_live_shape": current_brier_wins,
                        "ties": brier_ties
                    }
                },
                "average_fitted_parameters": {
                    "home_field_elo_points": round(
                        sum(fitted_hfas) / len(fitted_hfas), 2
                    ),
                    "elo_scale": round(
                        sum(fitted_scales) / len(fitted_scales), 2
                    )
                }
            },
            interpretation={
                "lower_log_loss_is_better": True,
                "lower_brier_score_is_better": True,
                "negative_difference_means_fitted_model_improved":
                    True,
                "decision_rule": (
                    "Do not change the live model automatically. "
                    "Evaluate whether the fitted curve improves both "
                    "unseen seasons and whether its parameters remain "
                    "reasonably stable."
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
