#!/usr/bin/env python3
"""Spritpreis-Tracker: holt Super-E5-Preise über die Tankerkönig-API,
führt einen rollierenden Verlauf (Standard 14 Tage) und schreibt die
Daten fürs Dashboard nach docs/data/."""

import csv
import json
import os
import statistics
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import agent

TZ = ZoneInfo("Europe/Berlin")
API = "https://creativecommons.tankerkoenig.de/json/list.php"
RADIUS_KM = float(os.environ.get("RADIUS_KM", "15"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "14"))
TOP_N = 15

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
HISTORY = DATA / "history.csv"
FIELDS = ["ts", "min", "p25", "median", "count", "cheapest_id", "cheapest"]


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Umgebungsvariable {name} fehlt (GitHub-Secret anlegen).")
    return value


def fetch_stations():
    params = {
        "lat": env("HOME_LAT"),
        "lng": env("HOME_LNG"),
        "rad": RADIUS_KM,
        "sort": "price",
        "type": "e5",
        "apikey": env("TK_API_KEY"),
    }
    req = urllib.request.Request(
        f"{API}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "spritpreis-tracker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise SystemExit(f"Tankerkönig meldet: {payload.get('message', 'unbekannter Fehler')}")
    stations = [
        s for s in payload.get("stations", [])
        if s.get("isOpen") and isinstance(s.get("price"), (int, float)) and s["price"] > 0
    ]
    stations.sort(key=lambda s: s["price"])
    return stations


def tidy(text):
    text = (text or "").strip()
    return text.title() if text.isupper() else text


def label(s):
    return tidy(s.get("brand")) or tidy(s.get("name")) or "Tankstelle"


def address(s):
    street = " ".join(x for x in [tidy(s.get("street")), (s.get("houseNumber") or "").strip()] if x)
    return ", ".join(x for x in [street, tidy(s.get("place"))] if x)


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = (len(v) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def load_history(now):
    if not HISTORY.exists():
        return []
    cutoff = now - timedelta(days=KEEP_DAYS)
    with HISTORY.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if datetime.fromisoformat(r["ts"]) >= cutoff]


def build_dashboard(now, stations, rows):
    pts = [
        (datetime.fromisoformat(r["ts"]).astimezone(TZ), float(r["min"]), float(r["median"]), r["cheapest"])
        for r in rows
    ]

    # Tage mit Messungen in mindestens 18 verschiedenen Stunden gelten als vollständig.
    by_day = defaultdict(list)
    for t, p, _, _ in pts:
        by_day[t.date()].append((t.hour, p))
    full_days = {d for d, v in by_day.items() if len({h for h, _ in v}) >= 18}
    day_mean = {d: statistics.fmean(p for _, p in v) for d, v in by_day.items()}

    # Tagesprofil: Abweichung vom jeweiligen Tagesmittel (blendet Trends über mehrere Tage aus).
    dev, cell = defaultdict(list), defaultdict(list)
    for t, p, _, _ in pts:
        cell[(t.weekday(), t.hour)].append(p)
        if t.date() in full_days:
            dev[t.hour].append(p - day_mean[t.date()])
    hour_profile = [round(statistics.fmean(dev[h]), 4) if dev[h] else None for h in range(24)]
    heat = [
        [round(statistics.fmean(cell[(w, h)]), 3) if cell[(w, h)] else None for h in range(24)]
        for w in range(7)
    ]
    wd = defaultdict(list)
    for d in full_days:
        wd[d.weekday()].append(day_mean[d])
    weekday_profile = [round(statistics.fmean(wd[w]), 3) if wd[w] else None for w in range(7)]

    hourly = defaultdict(lambda: ([], []))
    for t, p, m, _ in pts:
        bucket = hourly[t.replace(minute=0)]
        bucket[0].append(p)
        bucket[1].append(m)
    series = [
        [k.isoformat(), round(min(a), 3), round(statistics.fmean(b), 3)]
        for k, (a, b) in sorted(hourly.items())
    ]

    best = min(pts, key=lambda x: x[1]) if pts else None
    counts = Counter(c for *_, c in pts)
    total = len(pts) or 1

    return {
        "updated": now.isoformat(),
        "radius_km": RADIUS_KM,
        "keep_days": KEEP_DAYS,
        "span_days": round((pts[-1][0] - pts[0][0]).total_seconds() / 86400, 2) if len(pts) > 1 else 0,
        "full_days": len(full_days),
        "samples": len(pts),
        "open_count": len(stations),
        "stations": [
            {"name": label(s), "address": address(s), "price": round(s["price"], 3)} for s in stations[:TOP_N]
        ],
        "hour_profile": hour_profile,
        "weekday_profile": weekday_profile,
        "heat": heat,
        "best_seen": {"ts": best[0].isoformat(), "price": best[1], "station": best[3]} if best else None,
        "frequent_cheapest": [
            {"station": s, "share": round(n / total, 3)} for s, n in counts.most_common(5)
        ],
        "series": series,
    }


def main():
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    stations = fetch_stations()
    rows = load_history(now)
    if stations:
        prices = [s["price"] for s in stations]
        top = stations[0]
        rows.append({
            "ts": now.isoformat(),
            "min": f"{prices[0]:.3f}",
            "p25": f"{quantile(prices, 0.25):.3f}",
            "median": f"{statistics.median(prices):.3f}",
            "count": len(prices),
            "cheapest_id": top["id"],
            "cheapest": f"{label(top)}, {address(top)}",
        })

    DATA.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    data = build_dashboard(now, stations, rows)
    enriched = [
        {"id": s["id"], "name": label(s), "address": address(s), "price": round(s["price"], 3),
         "dist": float(s.get("dist") or 0)}
        for s in stations
    ]
    data["agent"] = agent.run(now, enriched, rows, data)
    (DATA / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    cheapest = f"{stations[0]['price']:.3f} € ({label(stations[0])})" if stations else "keine offene Tankstelle"
    print(f"{now:%d.%m.%Y %H:%M}: {len(stations)} offen, günstigste {cheapest}, Verlauf {len(rows)} Messungen")


if __name__ == "__main__":
    main()
