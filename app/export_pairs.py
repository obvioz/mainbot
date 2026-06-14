import json
from pathlib import Path

from app.market import make_exchange


OUT = "data/bybit_pairs.json"


def main():
    ex = make_exchange()

    print("Загружаю рынки...")

    ex.load_markets()

    result = {
        "BTC": [],
        "ETH": [],
        "USDT": [],
        "ALL": [],
    }

    for symbol in ex.symbols:

        result["ALL"].append(symbol)

        if symbol.endswith("/BTC"):
            result["BTC"].append(symbol)

        elif symbol.endswith("/ETH"):
            result["ETH"].append(symbol)

        elif symbol.endswith("/USDT"):
            result["USDT"].append(symbol)

    Path("data").mkdir(exist_ok=True)

    with open(
        OUT,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            result,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("Сохранено:", OUT)

    print()
    print("BTC:", len(result["BTC"]))
    print("ETH:", len(result["ETH"]))
    print("USDT:", len(result["USDT"]))
    print("ALL:", len(result["ALL"]))

    print()
    print("BTC пары:")

    for x in result["BTC"][:50]:
        print("-", x)


if __name__ == "__main__":
    main()