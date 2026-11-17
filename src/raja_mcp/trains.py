"""Raja tools: stations, train search with prices and free seats, stops of a train, announcements, news."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import API, SITE, ApiError, encrypt, fetch
from .registry import tool

TICKET_TYPE = {"family": "Family", "male": "Male", "female": "Famale"}  # sic: the site's own spelling
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
LETTERS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "‌": " "})


def _norm(s: str) -> str:
    return " ".join(s.translate(LETTERS).lower().split())


def _jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Gregorian to Jalali date (the common 33-year-cycle arithmetic)."""
    g = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm, jd = (1 + days // 31, 1 + days % 31) if days < 186 else (7 + (days - 186) // 30, 1 + (days - 186) % 30)
    return jy, jm, jd


def _parse_date(s: str) -> tuple[str, str, date]:
    """Jalali (1405/08/30) or Gregorian (2026-11-21) text to ('14050830', '1405/08/30', gregorian date)."""
    parts = re.split(r"[/\-.]", s.translate(DIGITS).strip())
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ApiError("Date must look like 1405/08/30 (Jalali) or 2026-11-21 (Gregorian).")
    y, m, d = map(int, parts)
    try:
        if y >= 1700:
            g = date(y, m, d)
            y, m, d = _jalali(y, m, d)
        elif not (1 <= m <= 12 and 1 <= d <= 31):
            raise ValueError
        else:
            g = _gregorian(y, m, d)
    except ValueError:
        raise ApiError(f"{s!r} is not a valid date.") from None
    return f"{y:04d}{m:02d}{d:02d}", f"{y:04d}/{m:02d}/{d:02d}", g


def _gregorian(jy: int, jm: int, jd: int) -> date:
    """Jalali to Gregorian: walk forward from a Gregorian guess (the Jalali year starts around 21 March)."""
    g = date(jy + 621, 3, 18)
    for step in range(0, 400):
        t = date.fromordinal(g.toordinal() + step)
        if _jalali(t.year, t.month, t.day) == (jy, jm, jd):
            return t
    raise ValueError("no such date")


async def _stations() -> list[dict[str, Any]]:
    return await fetch(f"{SITE}/assets/File/station.json", ttl=3600)


def _resolve(stations: list[dict[str, Any]], q: str | int) -> dict[str, Any]:
    """A station by code, exact name (Persian or English), or a unique partial name."""
    text = str(q).translate(DIGITS).strip()
    if text.isdigit():
        for s in stations:
            if s["Code"] == int(text):
                return s
    n = _norm(text)
    names = [(s, _norm(s["Name"]), _norm(s["EnglishName"])) for s in stations]
    exact = [s for s, fa, en in names if n in (fa, en)]
    near = exact or [s for s, fa, en in names if n and (n in fa or n in en)]
    if len(near) == 1 or (near and len({s["Name"] for s in near}) == 1):
        return near[0]
    if not near:
        raise ApiError(f"No station matches {q!r}. Call rj_stations with part of the name.")
    raise ApiError(
        f"{q!r} matches several stations: "
        + ", ".join(f"{s['Name']} ({s['Code']})" for s in near[:8])
        + ". Use the code."
    )


def _tomans(rial: float | None) -> int | None:
    return None if rial is None else int(round(rial / 10))


def _hhmm(t: str | None) -> str | None:
    return t[:5] if t else None


@tool("Raja stations")
async def rj_stations(
    query: Annotated[
        str | None,
        Field(description="Part of a station name in Persian or English, or a code. Empty lists the major stations."),
    ] = None,
) -> dict[str, Any]:
    """Find Raja railway stations and their codes by Persian or English name.

    Use the returned `code` (or the name) as origin or destination in rj_search_trains. With no query it lists
    only the major stations (the site's "main stations" group); pass a query to search all of them.
    """
    stations = await _stations()
    if query and query.strip():
        n = _norm(query.translate(DIGITS))
        hits = [s for s in stations if n in _norm(s["Name"]) or n in _norm(s["EnglishName"]) or n == str(s["Code"])]
    else:
        hits = [s for s in stations if s.get("Group")]
    return {
        "count": len(hits),
        "stations": [
            {"code": s["Code"], "name": s["Name"], "english": s["EnglishName"], "car_transport": s["IsCarTicket"]}
            for s in hits[:60]
        ],
    }


@tool("Search trains")
async def rj_search_trains(
    origin: Annotated[
        str, Field(description="Origin station: Persian name, English name or code, e.g. تهران, Tehran or 1.")
    ],
    destination: Annotated[str, Field(description="Destination station, same forms as origin, e.g. مشهد or 191.")],
    date: Annotated[str, Field(description="Travel date, Jalali like 1405/08/30 or Gregorian like 2026-11-21.")],
    passengers: Annotated[int, Field(description="Number of passengers.", ge=1, le=9)] = 1,
    ticket_type: Annotated[
        Literal["family", "male", "female"],
        Field(description="Seat group: family (mixed, default) or male / female for same-sex compartments."),
    ] = "family",
    include_sold_out: Annotated[bool, Field(description="Also list trains with no free seats.")] = False,
    limit: Annotated[int, Field(description="Maximum rows.", ge=1, le=100)] = 40,
) -> dict[str, Any]:
    """Search one-way trains between two stations on a date, with prices in Toman and free seats.

    Each row is one train and wagon class. `free_seats` is what the site shows (it shows "20+" above 20 and
    sells at most 10 per booking). Pass a row's `ref` to rj_train_stops for its stops. Prices are per passenger.
    Search only: it never reserves or pays. Raja may answer only from an Iranian IP.
    """
    stations = await _stations()
    a, b = _resolve(stations, origin), _resolve(stations, destination)
    if a["Code"] == b["Code"]:
        raise ApiError("Origin and destination are the same station.")
    day, shown, _ = _parse_date(date)
    key = (await fetch(f"{SITE}/assets/File/xrs.json", ttl=3600))["Key"]
    q = f"{a['Code']}-{b['Code']}-{TICKET_TYPE[ticket_type]}-1-{day}-{day}-{passengers}-false-0-0-L1"
    res = await fetch(f"{API}/TrainListEq", {"q": encrypt(q, key)})
    rows = []
    for t in res.get("GoTrains") or []:
        free = int(t.get("Counting") or 0)
        if free <= 0 and not include_sold_out:
            continue
        cost, full = _tomans(t.get("Cost")), _tomans(t.get("FullPrice"))
        rows.append(
            {
                "train": t["TrainNumber"],
                "company": t.get("CompanyName", "").strip(),
                "wagon": (t.get("WagonName") or "").strip(),
                "compartment": t.get("WagonInfo"),
                "departs": _hhmm(t.get("ExitTime")),
                "arrives": _hhmm(t.get("TimeOfArrival")),
                "price_toman": cost,
                "full_price_toman": full if full != cost else None,
                "discount": t.get("DiscountPercent") or None,
                "free_seats": free,
                "air_conditioned": t.get("AirConditioning"),
                "ref": f"{t['CircularPeriod']}-{t['CircularNumberSerial']}-{t['TrainNumber']}-{str(t['ExitDate'])[:10]}",
            }
        )
    rows.sort(key=lambda r: (r["departs"] or "", r["train"]))
    return {
        "from": a["Name"],
        "to": b["Name"],
        "date": shown,
        "passengers": passengers,
        "trains_found": len(rows),
        "trains": rows[:limit],
    }


@tool("Train stops")
async def rj_train_stops(
    ref: Annotated[str, Field(description="The `ref` of a row from rj_search_trains.")],
) -> dict[str, Any]:
    """List every stop of a train with its time, from a row's `ref` of rj_search_trains.

    This is the whole route, including stations before your origin. Times are Tehran time; a time earlier than
    the one before it is after midnight.
    """
    m = re.fullmatch(r"(\d+)-(\d+)-(\d+)-(\d{4})-(\d\d)-(\d\d)", ref.strip())
    if not m:
        raise ApiError("ref must come from rj_search_trains (like 14050401-283-172-2026-11-21).")
    period, serial, train, y, mo, d = m.groups()
    start = date(int(y), int(mo), int(d))
    for back in (0, 1):  # a train that leaves after midnight started the day before: keyed by its first station
        move = (start - timedelta(days=back)).strftime("%a %b %d %Y")  # JS Date.toDateString, as the site sends it
        rows = await fetch(
            f"{API}/StationTimeLine",
            body={
                "CircularPeriod": int(period),
                "CircularNumberSerial": int(serial),
                "pTrainNumber": int(train),
                "MoveDate": move,
            },
        )
        if rows:
            break
    return {
        "train": int(train),
        "stops": [{"station": r["StationName"], "code": r["StationCode"], "time": r.get("EnterTimeF")} for r in rows],
    }


def _text(s: str | None) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


@tool("Raja notices")
async def rj_announcements() -> dict[str, Any]:
    """Read the notices Raja shows at the top of its site: presale dates, refund rules, boarding rules.

    Persian site text; the presale notice says when tickets for the next period go on sale.
    """
    s = await fetch(f"{SITE}/assets/File/xrs.json", ttl=600)
    notices = [_text(s.get(k)) for k in ("Message1", "Message2", "Message3")]
    return {"notices": [n for n in notices if n]}


@tool("Raja news")
async def rj_news(
    news_id: Annotated[
        int | None, Field(description="An id from the list to read that story's text. Omit to list the latest.")
    ] = None,
    limit: Annotated[int, Field(description="How many latest stories to list.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """List the latest Raja company news (title, Jalali date, id) or read one story's text by id."""
    if news_id is not None:
        n = await fetch(f"{API}/NewsShow", {"Id": news_id})
        return {
            "id": n["ID"],
            "title": _text(n.get("Title")),
            "date": n.get("DateTime"),
            "text": _text(n.get("Description"))[:6000],
        }
    items = await fetch(f"{API}/ListAllNews", ttl=300)
    return {"news": [{"id": n["ID"], "title": _text(n["Title"]), "date": n["DateTime"]} for n in items[:limit]]}
