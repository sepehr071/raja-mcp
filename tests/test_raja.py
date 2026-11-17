import base64
import json

import httpx
import pytest
from conftest import fixture
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from raja_mcp import http, trains

pytestmark = pytest.mark.anyio

TOOLS = {"rj_stations", "rj_search_trains", "rj_train_stops", "rj_announcements", "rj_news"}
ST = "/assets/File/station.json"
KEY = "/assets/File/xrs.json"
SEARCH = "/Api/ServiceProvider/TrainListEq"


def decrypt(blob: str, key: str) -> str:
    raw = base64.b64decode(blob)
    salt, iv, ct = raw[:16], raw[16:32], raw[32:]
    k = PBKDF2HMAC(algorithm=hashes.SHA1(), length=32, salt=salt, iterations=100).derive(key.encode())
    d = Cipher(algorithms.AES(k), modes.CBC(iv)).decryptor()
    u = padding.PKCS7(128).unpadder()
    return (u.update(d.update(ct) + d.finalize()) + u.finalize()).decode()


@pytest.fixture
def site(api):
    api[ST] = fixture("stations.json")
    api[KEY] = fixture("xrs.json")
    api[SEARCH] = fixture("search.json")
    return api


def test_encrypt_roundtrip():
    assert decrypt(http.encrypt("1-191-Family-1-14050830-14050830-1-false-0-0-L1", "k"), "k").startswith("1-191-")
    assert http.encrypt("x", "k") != http.encrypt("x", "k")  # random salt and iv


def test_dates():
    assert trains._parse_date("2026-11-21")[:2] == ("14050830", "1405/08/30")
    assert trains._parse_date("۱۴۰۵/۰۸/۳۰")[2].isoformat() == "2026-11-21"
    assert trains._parse_date("1405-01-01")[2].isoformat() == "2026-03-21"
    with pytest.raises(http.ApiError):
        trains._parse_date("tomorrow")


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name


async def test_stations(client, site):
    r = (await client.call_tool("rj_stations", {"query": "isfahan"})).structured_content
    assert r["stations"] == [{"code": 21, "name": "اصفهان", "english": "ISFAHAN", "car_transport": True}]
    r = (await client.call_tool("rj_stations", {})).structured_content
    assert {s["code"] for s in r["stations"]} >= {1, 191}  # only the grouped main stations


async def test_search_sends_encrypted_query_and_trims_rows(client, site):
    r = (
        await client.call_tool(
            "rj_search_trains", {"origin": "تهران", "destination": "MASHAD", "date": "2026-11-21", "passengers": 2}
        )
    ).structured_content
    q = site.calls[-1].url.params["q"]
    assert decrypt(q, "TESTKEY") == "1-191-Family-1-14050830-14050830-2-false-0-0-L1"
    assert site.calls[-1].headers["api-key"] == http.API_KEY
    assert (r["from"], r["to"], r["date"]) == ("تهران", "مشهد", "1405/08/30")
    assert [t["train"] for t in r["trains"]] == [198, 318, 172]  # sold out train 192 hidden, sorted by departure
    t = r["trains"][2]
    assert t["price_toman"] == 3590000 and t["full_price_toman"] == 4160000 and t["free_seats"] == 26
    assert t["departs"] == "18:45" and t["arrives"] == "05:10"
    assert t["ref"] == "14050401-283-172-2026-11-21"


async def test_search_include_sold_out_and_errors(client, site):
    r = (
        await client.call_tool(
            "rj_search_trains",
            {"origin": "1", "destination": "191", "date": "1405/08/30", "include_sold_out": True},
        )
    ).structured_content
    assert r["trains_found"] == 4
    bad = await client.call_tool("rj_search_trains", {"origin": "تهران", "destination": "تهران", "date": "1405/08/30"})
    assert bad.is_error is True


async def test_station_resolve_ambiguity():
    st = fixture("stations.json")
    assert trains._resolve(st, "اصفهان")["Code"] == 21
    assert trains._resolve(st, "Tehran")["Code"] == 1
    with pytest.raises(http.ApiError, match="several"):
        trains._resolve(st, "ا")
    with pytest.raises(http.ApiError, match="No station"):
        trains._resolve(st, "xyz")


async def test_train_stops_retries_previous_day_for_after_midnight(client, api):
    def handler(request):
        body = json.loads(request.content)
        rows = fixture("timeline.json") if body["MoveDate"] == "Fri Nov 20 2026" else []
        return httpx.Response(200, json=rows)

    api["/Api/ServiceProvider/StationTimeLine"] = handler
    r = (await client.call_tool("rj_train_stops", {"ref": "14050401-283-172-2026-11-21"})).structured_content
    assert [json.loads(c.content)["MoveDate"] for c in api.calls] == ["Sat Nov 21 2026", "Fri Nov 20 2026"]
    assert r["stops"][0] == {"station": "قم", "code": 161, "time": "16:15"} and len(r["stops"]) == 8
    assert (await client.call_tool("rj_train_stops", {"ref": "nope"})).is_error is True


async def test_news_and_announcements(client, api):
    api["/Api/ServiceProvider/ListAllNews"] = fixture("news.json")
    api["/Api/ServiceProvider/NewsShow"] = fixture("newsshow.json")
    api[KEY] = fixture("xrs.json")
    r = (await client.call_tool("rj_news", {"limit": 2})).structured_content
    assert [n["id"] for n in r["news"]] == [5425, 5421]
    r = (await client.call_tool("rj_news", {"news_id": 5417})).structured_content
    assert api.calls[-1].url.params["Id"] == "5417" and "<" not in r["text"] and r["text"]
    r = (await client.call_tool("rj_announcements", {})).structured_content
    assert len(r["notices"]) == 3 and "پیش فروش" in r["notices"][0]


async def test_http_403_message(client, api):
    api[ST] = lambda request: httpx.Response(403, json="no")
    r = await client.call_tool("rj_stations", {})
    assert r.is_error is True and "403" in r.content[0].text


async def test_refuses_unknown_host(api):
    with pytest.raises(http.ApiError, match="unexpected host"):
        await http.fetch("https://example.com/x")
