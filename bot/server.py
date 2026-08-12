import pathlib

from aiohttp import web

from bot import db
from bot.roulette import color_of, payout_multiplier, spin
from bot.webapp_auth import validate_init_data

WEBAPP_DIR = pathlib.Path(__file__).parent / "webapp"


def _auth(request: web.Request) -> dict | None:
    init_data = request.headers.get("X-Init-Data", "")
    return validate_init_data(init_data)


async def handle_state(request: web.Request) -> web.Response:
    user = _auth(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    balance = db.get_or_create_player(user["id"], user.get("username"))
    return web.json_response({"balance": balance})


async def handle_spin(request: web.Request) -> web.Response:
    user = _auth(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
        amount = int(body["amount"])
        bet_type = str(body["bet_type"])
        bet_value = str(body["bet_value"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)

    if bet_type not in ("color", "parity", "range", "dozen", "number"):
        return web.json_response({"error": "bad_bet_type"}, status=400)

    if bet_type == "number":
        if not bet_value.isdigit() or not (0 <= int(bet_value) <= 36):
            return web.json_response({"error": "bad_bet_value"}, status=400)
    elif bet_type == "dozen" and bet_value not in ("1", "2", "3"):
        return web.json_response({"error": "bad_bet_value"}, status=400)
    elif bet_type == "color" and bet_value not in ("red", "black"):
        return web.json_response({"error": "bad_bet_value"}, status=400)
    elif bet_type == "parity" and bet_value not in ("even", "odd"):
        return web.json_response({"error": "bad_bet_value"}, status=400)
    elif bet_type == "range" and bet_value not in ("low", "high"):
        return web.json_response({"error": "bad_bet_value"}, status=400)

    user_id = user["id"]
    balance = db.get_or_create_player(user_id, user.get("username"))

    if amount <= 0 or amount > balance:
        return web.json_response({"error": "bad_amount", "balance": balance}, status=400)

    balance -= amount
    db.set_balance(user_id, balance)

    number = spin()
    color = color_of(number)
    multiplier = payout_multiplier(bet_type, bet_value, number)

    profit = 0
    if multiplier > 0:
        profit = amount * multiplier
        balance += amount + profit
        db.set_balance(user_id, balance)

    return web.json_response(
        {
            "number": number,
            "color": color,
            "win": multiplier > 0,
            "profit": profit,
            "balance": balance,
        }
    )


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/spin", handle_spin)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", WEBAPP_DIR, show_index=False)
    return app
