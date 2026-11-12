import httpx, pytest
from sourcing import oxylabs


def test_web_api_key_used_when_no_user_pass(monkeypatch):
    monkeypatch.setattr(oxylabs, "USER", None); monkeypatch.setattr(oxylabs, "PASS", None)
    monkeypatch.setattr(oxylabs, "WEB_API_KEY", "dummy-test-key")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, auth=headers["Authorization"])
        return httpx.Response(200, json={"results": [{"html": "<html>ok</html>"}]})

    monkeypatch.setattr(oxylabs.httpx, "post", fake_post)
    b = oxylabs.Budget(5)
    out = oxylabs.scrape({"source": "universal", "url": "https://x.test/p", "render": "html"}, b)
    assert out == "<html>ok</html>" and b.used == 1
    assert seen["url"] == oxylabs.WEB_API_ENDPOINT and seen["auth"] == "Bearer dummy-test-key"
    assert seen["body"] == {"url": "https://x.test/p", "output": ["html"], "location": "US", "run_js": True}


def test_web_api_401_stops_run(monkeypatch):
    monkeypatch.setattr(oxylabs, "USER", None); monkeypatch.setattr(oxylabs, "PASS", None)
    monkeypatch.setattr(oxylabs, "WEB_API_KEY", "dummy-test-key")
    monkeypatch.setattr(oxylabs.httpx, "post", lambda *a, **k: httpx.Response(401, json={}))
    with pytest.raises(oxylabs.OxylabsError):
        oxylabs.scrape({"source": "universal", "url": "https://x.test/p"}, oxylabs.Budget(5))
