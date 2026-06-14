from app.market import make_exchange

ex = make_exchange()

for p in ["ETH/BTC", "SOL/BTC", "BNB/BTC"]:
    t = ex.fetch_ticker(p)
    print(p)
    print("last:", t.get("last"))
    print("open:", t.get("open"))
    print("percentage:", t.get("percentage"))
    print("change:", t.get("change"))
    print()