import random

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def spin() -> int:
    return random.randint(0, 36)


def color_of(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in RED_NUMBERS else "black"


def color_emoji(color: str) -> str:
    return {"red": "🔴", "black": "⚫", "green": "🟢"}[color]


def payout_multiplier(bet_type: str, bet_value: str, number: int) -> int:
    """Returns the profit multiplier if the bet wins, otherwise 0.
    A win of multiplier M on stake S returns S * (M + 1) total (stake + profit).
    """
    color = color_of(number)

    if bet_type == "number":
        return 35 if int(bet_value) == number else 0

    if number == 0:
        # zero loses all outside bets (standard European roulette rule)
        return 0

    if bet_type == "color":
        return 1 if color == bet_value else 0

    if bet_type == "parity":
        is_even = number % 2 == 0
        return 1 if (bet_value == "even") == is_even else 0

    if bet_type == "range":
        if bet_value == "low":
            return 1 if 1 <= number <= 18 else 0
        return 1 if 19 <= number <= 36 else 0

    if bet_type == "dozen":
        dozen = int(bet_value)
        low = (dozen - 1) * 12 + 1
        high = dozen * 12
        return 2 if low <= number <= high else 0

    return 0


BET_LABELS = {
    ("color", "red"): "🔴 Красное",
    ("color", "black"): "⚫ Чёрное",
    ("parity", "even"): "Чёт",
    ("parity", "odd"): "Нечет",
    ("range", "low"): "1-18",
    ("range", "high"): "19-36",
    ("dozen", "1"): "Дюжина 1 (1-12)",
    ("dozen", "2"): "Дюжина 2 (13-24)",
    ("dozen", "3"): "Дюжина 3 (25-36)",
}


def bet_label(bet_type: str, bet_value: str) -> str:
    if bet_type == "number":
        return f"Число {bet_value}"
    return BET_LABELS.get((bet_type, bet_value), f"{bet_type}:{bet_value}")
