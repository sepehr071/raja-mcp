from datetime import date, timedelta

import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_stations(client):
    r = (await client.call_tool("rj_stations", {"query": "مشهد"})).structured_content
    assert {"code": 191, "name": "مشهد", "english": "MASHAD", "car_transport": True} in r["stations"]


async def test_live_search_and_stops(client):
    day = (date.today() + timedelta(days=5)).isoformat()
    r = (
        await client.call_tool(
            "rj_search_trains", {"origin": "تهران", "destination": "مشهد", "date": day, "include_sold_out": True}
        )
    ).structured_content
    assert r["trains_found"] > 0
    t = r["trains"][0]
    assert t["price_toman"] > 100_000 and t["departs"] and t["company"]
    s = (await client.call_tool("rj_train_stops", {"ref": t["ref"]})).structured_content
    assert len(s["stops"]) >= 2 and all(x["time"] for x in s["stops"])


async def test_live_news_and_notices(client):
    r = (await client.call_tool("rj_news", {"limit": 3})).structured_content
    assert len(r["news"]) == 3
    item = (await client.call_tool("rj_news", {"news_id": r["news"][0]["id"]})).structured_content
    assert item["title"] and item["text"]
    assert (await client.call_tool("rj_announcements", {})).structured_content["notices"]
