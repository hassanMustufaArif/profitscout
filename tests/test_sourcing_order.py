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


# ─── audit 2026-11-13 ────────────────────────────────────────────────────────
import json as _json

import pytest

from sourcing import http_sources

ITS = [  # shape of alibaba.com/search/api/imageTextSearch model.offers (captured 2026-11-13)
    {"productId": "1601996400710", "title": "LOZEE Thick Curling Mascara", "price": "$0.60",
     "productUrl": "//www.alibaba.com/product-detail/LOZEE_1601996400710.html?priceId=x",
     "moq": "Min. order: 10000 pieces", "companyName": "Guangzhou Xixue", "multiImage": ["//s.alicdn.com/a.jpg"],
     "goldSupplierYears": "3"},
    {"productId": "1601800994510", "title": "OEM ODM Extensionist Cream for 24HR Curl &amp; Lengthening",
     "price": "$1.40", "productUrl": "//www.alibaba.com/product-detail/OEM_1601800994510.html", "moq": "Min. order: 1 piece",
     "companyName": "Jiemei"},
    {"productId": "10000046724975", "title": "OEM 4D Silk Fiber Mascara", "price": "$0.45-2.65",
     "productUrl": "//www.alibaba.com/product-detail/OEM-4D_10000046724975.html", "moq": "Min. order: 12 pieces"},
]


def test_alibaba_site_list_parsed_in_page_order():
    rows = parse.alibaba_its_offers(ITS)
    assert [r["product_id"] for r in rows] == ["1601996400710", "1601800994510", "10000046724975"]
    assert rows[1]["title"].startswith("OEM ODM Extensionist Cream for 24HR Curl & Lengthening")
    assert rows[2]["price_min"] == 0.45 and rows[2]["price_max"] == 2.65 and rows[2]["moq"] == 12
    assert rows[0]["url"] == "https://www.alibaba.com/product-detail/LOZEE_1601996400710.html"
    assert rows[0]["image_url"] == "https://s.alicdn.com/a.jpg" and rows[0]["moq"] == 10000


def _ae_item(i, sale, orig, disc):
    return {"productId": str(1005000000000 + i), "title": {"displayTitle": f"AE {i}"},
            "prices": {"salePrice": {"minPrice": sale, "discount": disc}, "originalPrice": {"minPrice": orig}},
            "image": {"imgUrl": f"//ae/{i}.jpg"}}


def _ae_page(items):
    return ("<html><script>window._dida_config_._init_data_= { data: {\"root\":{\"fields\":{\"mods\":{\"itemList\":"
            + _json.dumps({"content": items}) + "}}}} }</script></html>")


def test_aliexpress_welcome_deals_are_kept_at_normal_price():
    offers = http_sources._ae_page_offers(_ae_page([_ae_item(0, 1.09, 7.79, 86), _ae_item(1, 5.97, 12.84, 53)]))
    kept = match.top_raw(offers, 28.99, 3)
    assert [o["title"] for o in kept] == ["AE 0", "AE 1"]
    assert kept[0]["price_min"] == 7.79          # not the $1.09 new-user price
    assert kept[1]["price_min"] == 5.97


def test_aliexpress_challenge_or_shell_page_is_blocked_not_no_match():
    with pytest.raises(http_sources.MarketplaceBlocked):
        http_sources._ae_page_offers('<html>{"rgv587_flag":"sm","url":"/_____tmd_____/punish"}</html>')
    with pytest.raises(http_sources.MarketplaceBlocked):
        http_sources._ae_page_offers("<html><title>AliExpress</title><div id=root></div></html>")
