"""Agent-Schicht des Spritpreis-Trackers.

Jeder Lauf durchläuft einen Kreislauf:
  1. Wahrnehmen   – aktuelle Preise und Verlauf (kommen aus update.py)
  2. Bewerten     – frühere Empfehlungen mit dem tatsächlichen Preisverlauf vergleichen
  3. Lernen       – einmal täglich die eigenen Regeln in festen Grenzen nachjustieren
  4. Entscheiden  – lohnt sich eine Benachrichtigung, und welche Tankstelle trotz Umweg?
                    (mit Claude, falls ANTHROPIC_API_KEY gesetzt; sonst regelbasiert)
  5. Handeln      – Push-Nachricht über ntfy aufs iPhone
"""

import json
import os
import statistics
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "agent.json"
CONFIG = ROOT / "config.json"

HORIZON_H = 12
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
DEFAULT_PARAMS = {"percentile": 0.20, "min_saving": 0.015, "calib": 1.0}
BOUNDS = {"percentile": (0.10, 0.35), "min_saving": (0.010, 0.040), "calib": (0.3, 1.5)}
MIN_STEP = {"percentile": 0.005, "min_saving": 0.001, "calib": 0.02}
LABELS = {"percentile": "Günstig-Schwelle", "min_saving": "Mindestersparnis fürs Warten",
          "calib": "Prognose-Korrektur"}
DEFAULT_CONFIG = {
    "tank_liter": 40,
    "verbrauch_l_100km": 7.5,
    "umwegfaktor": 1.3,
    "stadt_kmh": 25,
    "zeitwert_eur_h": 0,
    "max_umweg_km": 10,
    "ruhezeit_von": 22,
    "ruhezeit_bis": 7,
    "max_push_pro_tag": 2,
    "push_abstand_h": 6,
    "llm_modell": "claude-haiku-5-5",
    "ntfy_server": "https://ntfy.sh",
    "dashboard_url": "",
}


# ---------------------------------------------------------------- Hilfsfunktionen

def clamp(name, value):
    lo, hi = BOUNDS[name]
    return round(min(hi, max(lo, value)), 4)


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = (len(v) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def fmt_param(name, v):
    if name == "min_saving":
        return f"{v * 100:.1f} ct/l".replace(".", ",")
    return f"{v * 100:.0f} %"


def eur(p):
    return f"{p:.3f}".replace(".", ",")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


def load_state():
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("params", dict(DEFAULT_PARAMS))
    state.setdefault("decisions", [])
    state.setdefault("journal", [])
    state.setdefault("last_learn", "")
    return state


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def log(state, now, kind, title, text, **extra):
    state["journal"].append({"ts": now.isoformat(), "kind": kind, "title": title, "text": text, **extra})
    state["journal"] = state["journal"][-40:]


def dashboard_url(cfg):
    if cfg.get("dashboard_url"):
        return cfg["dashboard_url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


# ---------------------------------------------------------------- Prognose

def signal(now, price, data, params, threshold):
    """Gleiche Logik wie im Dashboard: jetzt tanken oder warten?"""
    hp = data["hour_profile"]
    if data["full_days"] < 2 or sum(v is not None for v in hp) < 18:
        return {"kind": "info"}
    h = now.hour
    now_dev = hp[h] if hp[h] is not None else 0.0
    best = None
    for i in range(1, HORIZON_H + 1):
        k = (h + i) % 24
        if hp[k] is None:
            continue
        if best is None or hp[k] < hp[best]:
            best = k
    saving = (now_dev - hp[best]) * params["calib"] if best is not None else 0.0
    cheap = threshold is not None and price <= threshold
    if cheap or saving < params["min_saving"]:
        return {"kind": "now", "cheap": cheap, "saving": round(saving, 4)}
    target = now.replace(minute=0) + timedelta(hours=(best - h) % 24)
    return {"kind": "wait", "target": target.isoformat(), "target_hour": best, "saving": round(saving, 4)}


# ---------------------------------------------------------------- Bewerten & Lernen

def record_decision(state, now, price, sig):
    """Eine Empfehlung pro Stunde wird zur späteren Bewertung protokolliert."""
    if sig["kind"] == "info":
        return
    bucket = now.strftime("%Y-%m-%dT%H")
    if any(d["ts"].startswith(bucket) for d in state["decisions"]):
        return
    entry = {"ts": now.isoformat(), "price": price, "kind": sig["kind"], "saving": sig["saving"]}
    if sig["kind"] == "wait":
        entry["target"] = sig["target"]
    state["decisions"].append(entry)
    cutoff = now - timedelta(days=14)
    state["decisions"] = [d for d in state["decisions"] if datetime.fromisoformat(d["ts"]) >= cutoff]


def evaluate(state, pts, now):
    for d in state["decisions"]:
        if d.get("evaluated"):
            continue
        t = datetime.fromisoformat(d["ts"])
        if now - t < timedelta(hours=HORIZON_H):
            continue
        future = [p for tt, p in pts if t < tt <= t + timedelta(hours=HORIZON_H)]
        if len(future) < 12:
            d["evaluated"] = "zu wenige Messungen"
            continue
        if d["kind"] == "now":
            d["outcome"] = {"regret": round(d["price"] - min(future), 4)}
        else:
            target = datetime.fromisoformat(d["target"])
            window = [p for tt, p in pts if target - timedelta(minutes=30) <= tt <= target + timedelta(minutes=90)]
            if not window:
                d["evaluated"] = "zu wenige Messungen"
                continue
            d["outcome"] = {"realized": round(d["price"] - min(window), 4)}
        d["evaluated"] = now.isoformat()


def stats(state, now):
    recent = [d for d in state["decisions"] if d.get("outcome")]
    nows = [d["outcome"]["regret"] for d in recent if d["kind"] == "now"]
    waits = [(d["outcome"]["realized"], d["saving"]) for d in recent if d["kind"] == "wait"]
    return {
        "now_n": len(nows),
        "now_hit": round(sum(r < 0.02 for r in nows) / len(nows), 3) if nows else None,
        "wait_n": len(waits),
        "wait_hit": round(sum(r >= 0.5 * s for r, s in waits) / len(waits), 3) if waits else None,
        "_waits": waits,
    }


def learn(state, now):
    """Einmal täglich (ab 3 Uhr) die Regeln anhand der bewerteten Empfehlungen anpassen."""
    today = now.date().isoformat()
    if state["last_learn"] == today or now.hour < 3:
        return
    state["last_learn"] = today
    p = state["params"]
    st = stats(state, now)
    changes = []

    def change(name, new, why):
        new = clamp(name, new)
        if abs(new - p[name]) >= MIN_STEP[name]:
            changes.append(f"{why}: {LABELS[name]} {fmt_param(name, p[name])} → {fmt_param(name, new)}.")
            p[name] = new

    if st["now_n"] >= 10:
        miss = 1 - st["now_hit"]
        if miss > 0.30:
            change("percentile", p["percentile"] - 0.02,
                   f"„Jetzt tanken“ war in {miss:.0%} der Fälle zu früh, Schwelle wird strenger")
        elif miss < 0.10:
            change("percentile", p["percentile"] + 0.02,
                   f"„Jetzt tanken“ lag zu {1 - miss:.0%} richtig, Schwelle wird großzügiger")
    if st["wait_n"] >= 6:
        ratios = [r / s for r, s in st["_waits"] if s > 0]
        if ratios:
            target = p["calib"] * statistics.median(ratios)
            change("calib", 0.7 * p["calib"] + 0.3 * target,
                   f"Tatsächliche Ersparnis beim Warten lag bei {statistics.median(ratios):.0%} der Prognose")
        if st["wait_hit"] < 0.5:
            change("min_saving", p["min_saving"] + 0.002,
                   f"Warten hat sich nur in {st['wait_hit']:.0%} der Fälle gelohnt, Warten erst ab höherer Ersparnis")
        elif st["wait_hit"] > 0.75:
            change("min_saving", p["min_saving"] - 0.002,
                   f"Warten hat sich in {st['wait_hit']:.0%} der Fälle gelohnt, Warten schon ab kleinerer Ersparnis")
    if changes:
        log(state, now, "gelernt", "Regeln angepasst", " ".join(changes))


# ---------------------------------------------------------------- Umweg-Rechnung

def detour_options(stations, cfg):
    """Nettoersparnis je Tankstelle gegenüber der nächstgelegenen, inkl. Umwegkosten."""
    if not stations:
        return []
    liters, cons, f = cfg["tank_liter"], cfg["verbrauch_l_100km"] / 100, cfg["umwegfaktor"]
    near = min(stations, key=lambda s: s["dist"])
    base = near["price"] * liters
    out = []
    for s in stations:
        extra_km = max(0.0, 2 * (s["dist"] - near["dist"]) * f)
        if extra_km > cfg["max_umweg_km"]:
            continue
        cost = s["price"] * liters + extra_km * cons * s["price"]
        if cfg["zeitwert_eur_h"]:
            cost += extra_km / cfg["stadt_kmh"] * cfg["zeitwert_eur_h"]
        out.append({
            "id": s["id"], "name": s["name"], "address": s["address"], "price": s["price"],
            "extra_km": round(extra_km, 1), "net_saving": round(base - cost, 2),
            "is_nearest": s["id"] == near["id"],
        })
    out.sort(key=lambda o: -o["net_saving"])
    return out[:5]


# ---------------------------------------------------------------- Entscheiden

def triggers(now, price, sig, pts):
    reasons = []
    if sig["kind"] == "now" and sig.get("cheap"):
        reasons.append("günstige Phase: Preis unter der gelernten Schwelle")
    before = [p for t, p in pts if now - timedelta(days=7) <= t < now - timedelta(minutes=20)]
    if len(before) > 50 and price < min(before) - 0.004:
        reasons.append("neuer 7-Tage-Tiefstpreis")
    return reasons


def gate(now, state, cfg, reasons, price):
    """Harte Grenzen gegen zu viele Nachrichten – gelten auch, wenn Claude entscheidet."""
    von, bis = cfg["ruhezeit_von"], cfg["ruhezeit_bis"]
    quiet = (now.hour >= von or now.hour < bis) if von > bis else (von <= now.hour < bis)
    if quiet:
        return "Ruhezeit"
    pushes = [j for j in state["journal"] if j["kind"] in ("push", "push-entwurf")]
    today = [j for j in pushes if j["ts"][:10] == now.date().isoformat()]
    if len(today) >= cfg["max_push_pro_tag"]:
        return "Tageslimit erreicht"
    if pushes:
        last = pushes[-1]
        gap = now - datetime.fromisoformat(last["ts"])
        much_lower = last.get("price") is not None and price <= last["price"] - 0.02
        if gap < timedelta(hours=cfg["push_abstand_h"]) and not much_lower:
            return "Mindestabstand zur letzten Nachricht"
    skipped = [j for j in state["journal"] if j["kind"] == "verzichtet"]
    if skipped:
        last = skipped[-1]
        recent = now - datetime.fromisoformat(last["ts"]) < timedelta(hours=1)
        if recent and price > last.get("price", 99) - 0.01:
            return "vor kurzem bereits geprüft und verzichtet"
    return None


SYSTEM_PROMPT = """Du bist ein Tank-Agent für eine Person in Berlin. Du entscheidest, ob sie JETZT
eine Push-Nachricht zum Tanken von Super E5 bekommen soll, und welche Tankstelle du empfiehlst.

Regeln:
- Nutze ausschließlich die Zahlen aus dem Kontext, erfinde nichts.
- Eine Nachricht ist nur sinnvoll, wenn sie einen echten Vorteil bringt. Lieber einmal zu wenig
  als zu oft benachrichtigen; die Person soll den Nachrichten vertrauen können.
- Berücksichtige den Umweg: net_saving ist die Ersparnis in Euro für eine Tankfüllung gegenüber der
  nächstgelegenen Tankstelle, Umwegkosten bereits abgezogen. Unter 0,50 € lohnt sich kein Umweg;
  dann die nächstgelegene oder eine ohne Umweg empfehlen.
- Beachte die Prognose: Wenn sie für die nächsten Stunden deutlich niedrigere Preise erwarten lässt,
  benachrichtige nicht.
- Schreibe auf Deutsch, knapp und konkret. Preise mit Komma, z. B. 1,689 €.

Antworte NUR mit einem JSON-Objekt, ohne Markdown, in diesem Format:
{"notify": true|false, "station_id": "<id aus options oder leer>", "title": "<max. 60 Zeichen>",
 "message": "<max. 280 Zeichen, nennt Tankstelle, Preis und ggf. Ersparnis>",
 "reasoning": "<max. 280 Zeichen: warum diese Entscheidung>"}"""


def ask_claude(context, cfg):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    body = {
        "model": cfg["llm_modell"],
        "max_tokens": 600,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    return parse_decision(text, {o["id"] for o in context["options"]})


def parse_decision(text, valid_ids):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    out = json.loads(text[start:end + 1])
    if not isinstance(out.get("notify"), bool):
        raise ValueError("notify fehlt")
    if out.get("station_id") and out["station_id"] not in valid_ids:
        out["station_id"] = ""
    for k, n in (("title", 60), ("message", 280), ("reasoning", 280)):
        out[k] = str(out.get(k, ""))[:n]
    return out


def rule_decision(context):
    best = context["options"][0] if context["options"] else None
    if not best:
        return {"notify": False, "reasoning": "keine Tankstelle verfügbar"}
    pick = best if best["net_saving"] >= 0.5 else next(o for o in context["options"] if o["is_nearest"])
    saved = f"{pick['net_saving']:.2f}".replace(".", ",")
    detour = (f", Umweg ca. {max(1, round(pick['extra_km']))} km, spart netto {saved} € pro Tankfüllung"
              if not pick["is_nearest"] else ", nächstgelegene Tankstelle")
    return {
        "notify": True,
        "station_id": pick["id"],
        "title": f"Super E5 günstig: {eur(pick['price'])} €",
        "message": f"{pick['name']}, {pick['address']}{detour}.",
        "reasoning": "Regelbasiert: " + "; ".join(context["triggers"]) + ".",
    }


# ---------------------------------------------------------------- Handeln

def push(title, message, cfg):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    body = {"topic": topic, "title": title, "message": message, "tags": ["fuelpump"], "priority": 4}
    url = dashboard_url(cfg)
    if url:
        body["click"] = url
    req = urllib.request.Request(
        cfg["ntfy_server"].rstrip("/"),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20):
        return True


# ---------------------------------------------------------------- Hauptablauf

def run(now, stations, rows, data):
    cfg, state = load_config(), load_state()
    pts = [(datetime.fromisoformat(r["ts"]), float(r["min"])) for r in rows]
    params = state["params"]
    week = [p for t, p in pts if t >= now - timedelta(days=7)]
    threshold = round(quantile(week, params["percentile"]), 3) if week else None

    options = detour_options(stations, cfg)
    price = stations[0]["price"] if stations else None

    if price is not None:
        sig = signal(now, price, data, params, threshold)
        record_decision(state, now, price, sig)
        evaluate(state, pts, now)
        learn(state, now)

        reasons = triggers(now, price, sig, pts)
        blocked = gate(now, state, cfg, reasons, price) if reasons else None
        if reasons and not blocked:
            context = {
                "jetzt": f"{WOCHENTAGE[now.weekday()]}, {now:%d.%m.%Y %H:%M} Uhr",
                "triggers": reasons,
                "bestpreis_umkreis": price,
                "schwelle_guenstig": threshold,
                "tiefst_7_tage": min(week) if week else None,
                "median_7_tage": round(statistics.median(week), 3) if week else None,
                "prognose": sig,
                "tankfuellung_liter": cfg["tank_liter"],
                "options": options,
                "letzte_nachrichten": [j for j in state["journal"] if j["kind"] == "push"][-3:],
            }
            source = "Claude"
            try:
                decision = ask_claude(context, cfg)
            except Exception as exc:  # Netzwerk, Kontingent, ungültiges JSON …
                print(f"Claude nicht verfügbar ({exc}), entscheide regelbasiert.")
                decision = None
            if decision is None:
                decision, source = rule_decision(context), "Regeln"

            if decision["notify"]:
                sent = False
                try:
                    sent = push(decision["title"], decision["message"], cfg)
                except Exception as exc:
                    print(f"Push fehlgeschlagen: {exc}")
                log(state, now, "push" if sent else "push-entwurf", decision["title"], decision["message"],
                    reasoning=decision.get("reasoning", ""), source=source, price=price)
            else:
                log(state, now, "verzichtet", "Keine Nachricht gesendet", decision.get("reasoning", ""),
                    source=source, price=price)

    save_state(state)
    st = stats(state, now)
    st.pop("_waits")
    return {
        "params": params,
        "threshold_price": threshold,
        "push_enabled": bool(os.environ.get("NTFY_TOPIC", "").strip()),
        "llm_enabled": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "tank_liter": cfg["tank_liter"],
        "stats": st,
        "options": options,
        "journal": list(reversed(state["journal"][-15:])),
    }
