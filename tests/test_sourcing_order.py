"""Supplier results must keep the marketplace's own order (2026-11-12 bug: lists came back reversed)."""
from sourcing import match, parse


def _offer(i, price=1.0):
    return {"offerId": str(1000 + i), "subject": f"Item {i}", "price": {"productLadderPrices": [{"dollarPrice": price}]},
            "mainImage": f"//img/{i}.jpg", "companyName": f"Co {i}"}


def test_alibaba_offers_keep_page_order():
    offers = parse.alibaba_offers([[_offer(i) for i in range(40)]])
    assert [o["title"] for o in offers[:3]] == ["Item 0", "Item 1", "Item 2"]


def test_top_raw_keeps_order_and_only_drops_no_margin():
    offers = parse.alibaba_offers([[_offer(0, 20.0), _offer(1, 12.0), _offer(2, 1.0), _offer(3, 2.0)]])
    kept = match.top_raw(offers, 15.0, 3)
    assert [o["title"] for o in kept] == ["Item 1", "Item 2", "Item 3"]   # $20 >= $15 dropped, $12 kept
