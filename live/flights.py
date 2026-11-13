"""Flugradar: holt taeglich um 06:00 die guenstigsten Fluege, schreibt data.js fuer dashboard.html
und stellt das Ergebnis auf GitHub Pages (publish.py).

Quellen:
  - Google Flights, Einzelstrecken (buendelt Airline-Seiten wie Eurowings, Austrian, Iberia, Vueling ...)
  - Google Flights, Hin- und Rueckflug-Tarife fuer die besten Kandidaten (oft guenstiger, z.B. Austrian)
  - Travelpayouts/Aviasales (optional, mit kostenlosem Token in config.json)

Aufruf:
  python flights.py               sofort abrufen
  python flights.py --if-stale    nur abrufen, wenn seit dem letzten 06:00-Termin noch nicht geschehen (Aufgabenplanung)
  python flights.py --background  wie --if-stale, aber im Hintergrund starten (Claude-Start-Hook)
"""
import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

import publish

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "update.log"
LOCK = ROOT / ".lock"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
NO_WINDOW = 0x08000000

AIRLINES = {  # IATA -> Name (fuer Travelpayouts)
    "EW": "Eurowings", "OS": "Austrian", "LH": "Lufthansa", "IB": "Iberia", "I2": "Iberia Express",
    "VY": "Vueling", "FR": "Ryanair", "U2": "easyJet", "W6": "Wizz Air", "UX": "Air Europa",
    "KL": "KLM", "AF": "Air France", "LX": "SWISS", "SN": "Brussels Airlines", "SK": "SAS",
    "DE": "Condor", "LO": "LOT", "TP": "TAP Air Portugal", "TK": "Turkish Airlines", "A3": "Aegean",
    "4U": "Eurowings", "X3": "TUIfly", "BT": "airBaltic", "EN": "Air Dolomiti", "LG": "Luxair",
}


class Blocked(Exception):
    """Google zeigt eine Sperr-/Captcha-Seite -> Abruf sofort abbrechen."""


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    if not os.environ.get("FLUGRADAR_QUIET"):  # bei GitHub sind Protokolle oeffentlich -> dort keine Reisedaten ausgeben
        print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def progress(kind, phase, n=0, total=0, **extra):
    """Fortschritt fuer die Anzeige im Dashboard (progress-refresh.json / progress-search.json)."""
    data = {"phase": phase, "n": n, "total": total, "time": datetime.now().isoformat(timespec="seconds"), **extra}
    try:
        (ROOT / f"progress-{kind}.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def notify(title, text, url=None):
    """Windows-Benachrichtigung; Klick oeffnet url (Standard: das lokale Dashboard)."""
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "notify.ps1"),
                        "-Title", title, "-Text", text, "-Url", url or (ROOT / "dashboard.html").as_uri()],
                       creationflags=NO_WINDOW, timeout=30, check=False)
    except Exception as e:  # noqa: BLE001
        log(f"Benachrichtigung fehlgeschlagen: {e}")


# ---------------------------------------------------------------- Google Flights
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num, payload):  # length-delimited protobuf field
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def _int(num, v):
    return _varint(num << 3) + _varint(v)


def _leg(frm, to, day, segs=()):
    """Eine Strecke; segs = bereits ausgewaehlte Fluege [(von, datum, nach, airline, nummer), ...]."""
    out = _field(2, day.encode())
    for s in segs:
        out += _field(4, _field(1, s[0].encode()) + _field(2, s[1].encode()) + _field(3, s[2].encode())
                      + _field(5, s[3].encode()) + _field(6, s[4].encode()))
    return out + _field(13, _field(2, frm.encode())) + _field(14, _field(2, to.encode()))


# Gepaeckfilter "1 Handgepaeck, 0 aufgegeben": Google rechnet die echten Handgepaeck-Gebuehren der Airlines ein
BAGS = _field(13, _int(2, 1) + _int(3, 0))


def google_url(frm, to, day, ret=None, hl="en", out_segs=(), ret_segs=(), bags=True):
    """One-Way (ret=None) oder Hin- und Rueckflug; 1 Erwachsener, Economy, inkl. 1 Handgepaeckstueck.
    Mit out_segs zeigt Google die Rueckfluege zu diesem Hinflug, mit beiden die Buchungsoptionen.
    bags=False: ohne Handgepaeck-Filter (Google blendet sonst bei Hin+Rueck die Light-Tarife z.B. von Austrian aus)."""
    legs = _field(3, _leg(frm, to, day, out_segs)) + (_field(3, _leg(to, frm, ret, ret_segs)) if ret else b"")
    info = legs + _int(8, 1) + _int(9, 1) + (BAGS if bags else b"") + _int(19, 1 if ret else 2)
    tfs = base64.b64encode(info).decode()
    return f"https://www.google.com/travel/flights?tfs={quote(tfs)}&hl={hl}&gl=DE&curr=EUR"


# Flugsegmente in den eingebetteten Seitendaten: Von, Nach, Abflug-/Ankunftszeit, Datum, Airline + Flugnummer
SEG = re.compile(
    r'\[null,null,null,"([A-Z0-9]{3})","[^"]*","[^"]*","([A-Z0-9]{3})",null,'
    r'\[(\d*|null)(?:,(\d+))?\],null,\[(\d*|null)(?:,(\d+))?\],\d+,.{0,600}?'
    r'\[(\d{4}),(\d+),(\d+)\],\[\d{4},\d+,\d+\],\["([A-Z0-9]{2})","(\d+)"'
)


def _hm(h, m):
    return f"{int(h) if h and h != 'null' else 0:02d}:{int(m or 0):02d}"


def itineraries(text, frm, to):
    """Verbindungen frm->to mit ihren Segmenten, z.B. {'dep': '07:35', 'arr': '09:05', 'segs': [('HAM', '2026-11-19', 'VIE', 'OS', '260')]}."""
    out, seen, cur = [], set(), []
    for m in SEG.finditer(text):
        s = {"frm": m[1], "to": m[2], "dep": _hm(m[3], m[4]), "arr": _hm(m[5], m[6]),
             "date": f"{int(m[7]):04d}-{int(m[8]):02d}-{int(m[9]):02d}", "seg": (m[1], f"{int(m[7]):04d}-{int(m[8]):02d}-{int(m[9]):02d}", m[2], m[10], m[11])}
        if s["frm"] == frm:
            cur = [s]
        elif cur and cur[-1]["to"] == s["frm"]:
            cur.append(s)
        else:
            cur = []
            continue
        if s["to"] == to:
            key = tuple(x["seg"] for x in cur)
            if key not in seen:
                seen.add(key)
                out.append({"dep": cur[0]["dep"], "arr": cur[-1]["arr"], "segs": [x["seg"] for x in cur]})
            cur = []
    return out


def match(itins, f):
    """Segmente zur Verbindung f (gleiche Abflug- und Ankunftszeit)."""
    return next((i["segs"] for i in itins if i["dep"] == f["dep"] and i["arr"] == f["arr"]), None)


LABEL = re.compile(
    r"^From (?P<p>[\d,.]+) euros(?: round trip total)?\.(?P<nobag>This price does not include overhead bin access\.)?.*?"
    r"(?P<stops>Nonstop|\d+ stops?) flight with (?P<al>.+?)\.(?: Operated by [^.]*\.)*\s*"
    r"Leaves .*? at (?P<dep>\d{1,2}:\d{2}\s?[AP]M) on (?P<dd>[^.]+?) and arrives at .*? at "
    r"(?P<arr>\d{1,2}:\d{2}\s?[AP]M) on (?P<ad>[^.]+?)\. Total duration (?P<dur>[^.]+)\.(?P<rest>.*)$"
)


def _to24(t):
    return datetime.strptime(re.sub(r"\s+", " ", t).strip(), "%I:%M %p").strftime("%H:%M")


def _minutes(dur):
    h = re.search(r"(\d+) hr", dur)
    m = re.search(r"(\d+) min", dur)
    return (int(h[1]) * 60 if h else 0) + (int(m[1]) if m else 0)


class Google:
    name = "Google Flights"

    def __init__(self, cfg):
        self.cfg = cfg
        self.local = threading.local()

    @property
    def session(self):
        if not hasattr(self.local, "s"):
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            # Cookie-Banner: nur notwendige Cookies (ohne SOCS leitet Google auf die Consent-Seite um)
            s.cookies.set("SOCS", "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg", domain=".google.com")
            self.local.s = s
        return self.local.s

    def fetch_rt(self, frm, to, day, ret, out_segs=(), nb=0):
        """Hin+Rueck-Seite als (Fluege, Seitentext). Ohne out_segs: mit UND ohne Handgepaeck-Filter; aus der
        Abfrage ohne Filter zaehlen nur Airlines mit bekanntem Aufpreis (bagCorrection), Eintrag mit nb=1.
        Mit out_segs: Rueckfluege zum Hinflug, ohne Filter wenn der Hinflug aus dieser Abfrage stammt (nb)."""
        if out_segs:
            res, text = self.fetch(frm, to, day, ret, out_segs=out_segs, raw=True, bags=not nb)
            return (corr_only(self.cfg, res) if nb and res is not None else res), text
        r1, t1 = self.fetch(frm, to, day, ret, raw=True)
        time.sleep(float(self.cfg.get("delaySeconds", 1.5)))
        r2, t2 = self.fetch(frm, to, day, ret, raw=True, bags=False)
        if r1 is None and r2 is None:
            return None, ""
        return (r1 or []) + corr_only(self.cfg, r2 or []), t1 + t2

    def fetch(self, frm, to, day, ret=None, out_segs=(), raw=False, bags=True):
        """Liste von Fluegen; None bei Netzwerkfehler; wirft Blocked bei Sperrseite.
        raw=True liefert (Fluege, Seitentext) fuer die Flugnummern. Mit out_segs: Rueckfluege zu diesem Hinflug."""
        url = google_url(frm, to, day, ret, out_segs=out_segs, bags=bags)
        text = ""
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=40)
                r.encoding = "utf-8"
                if r.status_code == 429 or "/sorry/" in r.url or "unusual traffic" in r.text[:200000]:
                    raise Blocked(f"Google-Sperrseite bei {frm}-{to} {day}")
                if "consent.google" in r.url:
                    raise Blocked("Google verlangt Cookie-Zustimmung (Consent-Seite)")
                text = r.text
                flights = self._parse(text, frm, to, day, ret, bags)
                if flights or attempt == 2:
                    return (flights, text) if raw else flights
            except requests.RequestException as e:
                if attempt == 2:
                    log(f"FEHLER Google {frm}-{to} {day}: {e}")
                    return (None, "") if raw else None
            time.sleep(4 * (attempt + 1))
        return ([], text) if raw else []

    def _parse(self, text, frm, to, day, ret, bags=True):
        seen, out = set(), []
        link = google_url(frm, to, day, ret, hl="de", bags=bags)
        for m in re.finditer(r'aria-label="(From [^"]+)"', text):
            label = html.unescape(m.group(1)).replace(" ", " ").replace(" ", " ")
            if label in seen:
                continue
            seen.add(label)
            x = LABEL.match(label)
            if not x:
                continue
            stops = 0 if x["stops"] == "Nonstop" else int(re.sub(r"\D", "", x["stops"]))
            out.append(flight(
                self.cfg, price=float(x["p"].replace(",", "")), airline=x["al"], stops=stops,
                bag_known=True, bag_included=not x["nobag"], legs=2 if ret else 1,
                dep=_to24(x["dep"]), arr=_to24(x["arr"]), next_day=x["dd"] != x["ad"],
                minutes=_minutes(x["dur"]), via=", ".join(re.findall(r"layover at .*? in ([^.]+)\.", x["rest"])),
                src="Google Flights", link=link, airport=to,
            ))
        return out


# ---------------------------------------------------------------- Travelpayouts
class Travelpayouts:
    """Aviasales-Preisdaten (Cache der letzten 48 h aus vielen Buchungsportalen). Ein Aufruf pro Monat und Richtung."""
    name = "Aviasales"

    def __init__(self, cfg, token):
        self.cfg, self.token = cfg, token
        self.cache, self.lock = {}, threading.Lock()

    def _month(self, frm, to, month):
        key = (frm, to, month)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            res = {}
            try:
                r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", timeout=40, params={
                    "origin": frm, "destination": to, "departure_at": month, "one_way": "true",
                    "sorting": "price", "currency": "eur", "limit": 1000, "token": self.token})
                r.raise_for_status()
                for it in r.json().get("data", []):
                    res.setdefault(it["departure_at"][:10], []).append(it)
            except Exception as e:  # noqa: BLE001 - Quelle ist optional
                log(f"FEHLER Aviasales {frm}-{to} {month}: {e}")
            self.cache[key] = res
            return res

    def fetch(self, frm, to, day, ret=None):
        out = []
        for it in self._month(frm, to, day[:7]).get(day, []):
            dep = datetime.fromisoformat(it["departure_at"])
            mins = int(it.get("duration_to") or it.get("duration") or 0)
            arr = dep + timedelta(minutes=mins)
            code = it.get("airline", "")
            out.append(flight(
                self.cfg, price=float(it["price"]), airline=AIRLINES.get(code, code), stops=int(it.get("transfers", 0)),
                bag_known=False, bag_included=False, legs=1, dep=dep.strftime("%H:%M"),
                arr=arr.strftime("%H:%M") if mins else "", next_day=bool(mins) and arr.date() > dep.date(),
                minutes=mins, via="", src="Aviasales", link="https://www.aviasales.com" + it.get("link", ""), airport=to,
            ))
        return out


# ---------------------------------------------------------------- Rechnen
def bag_fee(cfg, airline_text):
    fees = cfg["bagFees"]
    parts = [a.strip() for a in re.split(r", | and ", airline_text) if a.strip()]
    return max((fees.get(a, fees["_default"]) for a in parts), default=fees["_default"])


def bag_correction(cfg, airline_text):
    """Handgepaeck-Aufpreis pro Flug, den Google als 'inklusive' meldet, die Airline aber verlangt
    (config.json -> bagCorrection, z.B. Austrian 20 EUR). 0, wenn keine der Airlines betroffen ist."""
    corr = {k: v for k, v in cfg.get("bagCorrection", {}).items() if not k.startswith("_")}
    parts = [a.strip() for a in re.split(r", | and ", airline_text) if a.strip()]
    return max((corr.get(a, 0) for a in parts), default=0)


def corr_only(cfg, res):
    """Ergebnisse ohne Handgepaeck-Filter: nur Fluege, bei denen alle Airlines einen bekannten Aufpreis haben
    (bagCorrection); Preis = Ticket + Aufpreis je Flug (Hin+Rueck = 2 Fluege). nb=1 merkt die Herkunft."""
    corr = {k for k in cfg.get("bagCorrection", {}) if not k.startswith("_")}
    out = []
    for f in res:
        parts = [a.strip() for a in re.split(r", | and ", f["a"]) if a.strip()]
        if parts and all(a in corr for a in parts):
            fee = bag_correction(cfg, f["a"]) * 2
            out.append(dict(f, f=fee, b=f["p"] + fee, fc=1, nb=1))
    return out


def flight(cfg, *, price, airline, stops, bag_known, bag_included, legs, dep, arr, next_day, minutes, via, src, link, airport):
    corr = bag_correction(cfg, airline) * legs if bag_included else 0
    fee = corr if bag_included else bag_fee(cfg, airline) * legs
    return {"p": price, "f": fee, "b": price + fee, "bk": bag_known, "fc": int(bool(corr)), "a": airline, "s": stops,
            "dep": dep, "arr": arr, "nd": int(bool(next_day)), "m": minutes,
            "dur": f"{minutes // 60}h {minutes % 60:02d}m" if minutes else "", "via": via,
            "src": src, "url": link, "ap": airport}


MAX_MINUTES = 600  # niemand fliegt laenger als 10 h pro Strecke


def acceptable(f, max_stops):
    """Grundregel fuer jede Verbindung: keine Nachtverbindung (Ankunft am Folgetag), hoechstens 10 h."""
    return f["s"] <= max_stops and not f["nd"] and f["m"] <= MAX_MINUTES


def keep_best(flights, max_stops, n=6):
    fl = sorted((f for f in flights if acceptable(f, max_stops)), key=lambda f: (f["b"], f["s"], f["m"]))
    uniq, seen = [], set()
    for f in fl:  # gleiche Verbindung aus 2 Quellen -> nur die guenstigere
        k = (f["a"], f["dep"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    keep = uniq[:n]
    for pick in (lambda f: f["s"] == 0, lambda f: f["m"] and f["m"] <= 300):
        extra = next((f for f in uniq if pick(f)), None)  # damit Filter im Dashboard immer etwas finden
        if extra and extra not in keep:
            keep.append(extra)
    return keep


DEFAULT_FILTER = {"direct": False}  # = Voreinstellung im Dashboard


def ok(f, flt):
    """Filter "Nur Direktfluege" - bei Hin+Rueck-Tickets mit bekanntem Rueckflug auch fuer diesen."""
    return not flt["direct"] or (f["s"] == 0 and f.get("r", {}).get("s", 0) == 0)


def best(lst, flt):
    c = [f for f in lst if ok(f, flt)]
    return min(c, key=lambda f: f["b"]) if c else None


def add(d, n):
    return (date.fromisoformat(d) + timedelta(n)).isoformat()


def trip_price(cfg, trip, legs, rts, d, n, flt=DEFAULT_FILTER, rt=True):
    """Guenstigste Reise (Hin am Tag d, n Naechte): 2 Einzeltickets oder 1 Hin+Rueck-Ticket."""
    rd = add(d, n)
    home = trip["home"]
    outs = [f for a in trip["dest"] for f in legs.get(f"{home}-{a}", {}).get(d, [])]
    rets = [f for a in trip["dest"] for f in legs.get(f"{a}-{home}", {}).get(rd, [])]
    o, r = best(outs, flt), best(rets, flt)
    cands = []
    if o and r:
        cands.append(o["b"] + r["b"])
    if rt:
        for a in trip["dest"]:
            x = rts.get(trip["id"], {}).get(f"{a}|{d}|{rd}")
            if x and ok(x, flt):
                cands.append(x["b"])
    return min(cands) if cands else None


def rt_entry(res, text, frm, to, d, n, max_stops):
    """Bestes Hin+Rueck-Ticket einer Google-Ergebnisseite (+ Direktflug-Variante als "alt"), mit Flugnummern."""
    good = [f for f in res if acceptable(f, max_stops)]
    b = min(good, key=lambda f: f["b"], default=None)
    if not b:
        return None
    direct = min((f for f in good if f["s"] == 0), key=lambda f: f["b"], default=None)
    itins = itineraries(text, frm, to)
    b = dict(b, rt=1, ret=add(d, n), segs=match(itins, b))
    if direct and b["s"]:  # fuer den Filter "Nur Direktfluege"
        b["alt"] = dict(direct, rt=1, ret=add(d, n), segs=match(itins, direct))
    return b


def apply_return(cfg, y, res, text, frm, to, d, n, segs, max_stops):
    """Rueckflug-Seite zu Ticket y auswerten: guenstigster erlaubter Rueckflug, Preis inkl. Gepaeck-Korrektur.
    False, wenn es nur Nacht-/Langstrecken-Rueckfluege gibt (Ticket kommt dann nicht in Frage)."""
    r = min((f for f in res if acceptable(f, max_stops)), key=lambda f: f["b"], default=None)
    if not r:
        return False
    ret_segs = match(itineraries(text, to, frm), r)
    if r["f"] == 0 or r["fc"]:  # Handgepaeck laut Google inklusive -> Korrektur je Flug (Hin- + Rueckflug-Airline)
        fee = bag_correction(cfg, y["a"]) + bag_correction(cfg, r["a"])
        r = dict(r, f=fee, b=r["p"] + fee, fc=int(bool(fee)))
    y.update(p=r["p"], f=r["f"], b=r["b"], fc=r["fc"],
             r={k: r[k] for k in ("a", "s", "dep", "arr", "nd", "m", "dur", "via")},
             url=google_url(frm, to, d, add(d, n), hl="de", out_segs=segs, ret_segs=ret_segs or (), bags=not y.get("nb")))
    return True


def trip_option(trip, legs, rts, d, n, direct=False):
    """Wie option() im Dashboard: guenstigste Reise und welches Hin+Rueck-Ticket dabei verwendet wird.
    rt = (Schluessel, "x" | "alt") oder None bei 2 Einzeltickets."""
    rd = add(d, n)
    flt = {"direct": direct}
    home = trip["home"]
    o = best([f for a in trip["dest"] for f in legs.get(f"{home}-{a}", {}).get(d, [])], flt)
    r = best([f for a in trip["dest"] for f in legs.get(f"{a}-{home}", {}).get(rd, [])], flt)
    res = {"dep": d, "ret": rd, "n": n, "total": o["b"] + r["b"], "rt": None} if o and r else None
    for a in trip["dest"]:
        key = f"{a}|{d}|{rd}"
        x, which = rts.get(trip["id"], {}).get(key), "x"
        if x and not ok(x, flt):
            x, which = (x["alt"], "alt") if x.get("alt") and ok(x["alt"], flt) else (None, None)
        if x and (res is None or x["b"] < res["total"]):
            res = {"dep": d, "ret": rd, "n": n, "total": x["b"], "rt": (key, which)}
    return res


def visible_rts(cfg, trip, legs, rts, today, horizon, max_alts=4, per_day=2):
    """Hin+Rueck-Tickets, die das Dashboard in der Standardansicht zeigt: beste Reise und Alternativen
    (nur direkt, max. 2 Abfluege pro Tag) jeder Woche, Top 3 und die besten 6 jeder Sonderabfrage."""
    main_days, extra_days = trip_days(cfg, trip, today, horizon)
    nights = range(trip["minNights"], trip["maxNights"] + 1)
    days = sorted({d for d, _ in main_days})
    need = set()
    opts = [o for d, n in main_days if (o := trip_option(trip, legs, rts, d, n))]
    by_week = {}
    for o in opts:
        by_week.setdefault(monday(o["dep"]), []).append(o)
    for w, lst in by_week.items():
        top = min(lst, key=lambda o: o["total"])
        need.add(top["rt"])
        used, count = {top["dep"]: 1}, 0
        direct = sorted((o for d in days if monday(d) == w for n in nights
                         if (o := trip_option(trip, legs, rts, d, n, direct=True))), key=lambda o: o["total"])
        for a in direct:
            if count == max_alts:
                break
            if (a["dep"], a["ret"]) == (top["dep"], top["ret"]) or used.get(a["dep"], 0) >= per_day:
                continue
            used[a["dep"]] = used.get(a["dep"], 0) + 1
            count += 1
            need.add(a["rt"])
    picks = []  # Top 3: nur Direktfluege, keine Ueberschneidung, moeglichst verschiedene Naechte
    direct_opts = [o for d, n in main_days if (o := trip_option(trip, legs, rts, d, n, direct=True))]
    for strict in (True, False):
        for o in sorted(direct_opts, key=lambda o: o["total"]):
            if len(picks) == 3:
                break
            if o in picks or any(o["dep"] < p["ret"] and p["dep"] < o["ret"] for p in picks):
                continue
            if strict and any(p["n"] == o["n"] for p in picks):
                continue
            picks.append(o)
    need.update(p["rt"] for p in picks)
    if extra_days:
        extra = sorted((o for d, n in extra_days if (o := trip_option(trip, legs, rts, d, n))), key=lambda o: o["total"])
        need.update(o["rt"] for o in extra[:6])
    need.discard(None)
    return need


# ---------------------------------------------------------------- Ablauf
DAILY_HOUR = 6  # taeglicher Abruf um 06:00 (Aufgabenplanung)


def is_stale():
    """Seit dem letzten 06:00-Termin noch nicht abgerufen? (manuelle Abrufe davor zaehlen nicht)"""
    now = datetime.now()
    due = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if now < due:
        due -= timedelta(1)
    try:
        d = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
        return datetime.fromisoformat(d.get("lastAttempt") or d["generated"]) < due
    except Exception:  # noqa: BLE001
        return True


def pid_alive(pid):
    """Laeuft der Prozess noch? (Windows, ohne ihn zu beeinflussen)"""
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok_ = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok_) and code.value == 259  # STILL_ACTIVE


def keep_awake(on):
    """Windows waehrend des Abrufs nicht in den Ruhezustand schicken (Bildschirm darf aus).
    Sonst friert ein kurzes Aufwachen, z.B. um 06:47, den Abruf nach Sekunden wieder ein."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:  # noqa: BLE001 - ohne Wachhalten laeuft der Abruf trotzdem
        pass


def locked():
    """Ein Abruf laeuft gerade (Sperrdatei eines noch lebenden Prozesses)."""
    try:
        return pid_alive(int(LOCK.read_text().strip()))
    except (OSError, ValueError):
        return False


def plan(cfg, today, horizon):
    """Welche Einzelstrecken an welchen Tagen abgefragt werden muessen."""
    need = {}

    def want(frm, to, d0, d1):
        s = need.setdefault(f"{frm}-{to}", set())
        d = d0
        while d <= d1:
            if d > today:
                s.add(d)
            d += timedelta(1)

    end = today + timedelta(horizon)
    for t in cfg["trips"]:
        for a in t["dest"]:
            want(t["home"], a, today + timedelta(1), end)
            want(a, t["home"], today + timedelta(1 + t["minNights"]), end + timedelta(t["maxNights"]))
    for x in cfg.get("extraSearches", []):
        t = next(t for t in cfg["trips"] if t["id"] == x["trip"])
        d0, d1 = date.fromisoformat(x["depFrom"]), date.fromisoformat(x["depTo"])
        for a in t["dest"]:
            want(t["home"], a, d0, d1)
            want(a, t["home"], d0 + timedelta(x["minNights"]), d1 + timedelta(x["maxNights"]))
    return {k: sorted(d.isoformat() for d in v) for k, v in need.items()}


def trip_days(cfg, trip, today, horizon):
    """(Abflugtag, Naechte) fuer die Hauptansicht und die Sonderabfragen."""
    main = [((today + timedelta(i)).isoformat(), n) for i in range(1, horizon + 1)
            for n in range(trip["minNights"], trip["maxNights"] + 1)]
    extra = []
    for x in cfg.get("extraSearches", []):
        if x["trip"] != trip["id"]:
            continue
        d = date.fromisoformat(x["depFrom"])
        while d <= date.fromisoformat(x["depTo"]):
            extra += [(d.isoformat(), n) for n in range(x["minNights"], x["maxNights"] + 1)]
            d += timedelta(1)
    return main, extra


def monday(d):
    x = date.fromisoformat(d)
    return (x - timedelta(x.weekday())).isoformat()


LIVE_LOCK = ROOT / ".live.lock"
LIVE_FILE = ROOT / "rtcheck.json"


def rt_check(trip_id, a, b, flex):
    """Live-Pruefung aus der eigenen Suche (Knopf im Dashboard): Hin+Rueck-Tickets fuer alle Kombinationen
    Hinflug a +-flex / Rueckflug b +-flex, Rueckflug-Details fuer die guenstigsten. Ergebnis: rtcheck.json"""
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    trip = next(t for t in cfg["trips"] if t["id"] == trip_id)
    flex = max(0, min(3, int(flex)))
    max_stops, delay = int(cfg["maxStops"]), float(cfg.get("delaySeconds", 1.5))
    first = date.today() + timedelta(1)
    combos = []
    for i in range(-flex, flex + 1):
        d = date.fromisoformat(a) + timedelta(i)
        for j in range(-flex, flex + 1):
            n = (date.fromisoformat(b) + timedelta(j) - d).days
            if d >= first and n >= 1:
                combos.append((d.isoformat(), n))
    home, to = trip["home"], trip["dest"][0]
    out = {"trip": trip_id, "a": a, "b": b, "f": flex, "status": "error", "rt": {}}
    state = {"phase": "rt", "n": 0, "total": len(combos)}
    guard = threading.Lock()
    google = Google(cfg)

    def fetch(job):
        d, n, segs, nb = job
        res = google.fetch_rt(home, to, d, add(d, n), out_segs=segs, nb=nb)
        time.sleep(delay)
        with guard:
            state["n"] += 1
            progress("live", state["phase"], state["n"], state["total"])
        return job, res

    LIVE_LOCK.write_text(str(os.getpid()))
    keep_awake(True)
    try:
        for _ in range(270):  # laeuft der taegliche Abruf, erst warten (max. 45 Min.) - nicht doppelt bei Google anfragen
            if not locked():
                break
            progress("live", "wait")
            time.sleep(10)
        log(f"Live-Pruefung {trip_id} {a} -> {b} (+-{flex} Tage): {len(combos)} Hin+Rueck-Abfragen")
        rts = {}
        with ThreadPoolExecutor(max_workers=int(cfg.get("workers", 2))) as ex:
            progress("live", "rt", 0, len(combos))
            for (d, n, _, _), (res, text) in ex.map(fetch, [(d, n, (), 0) for d, n in combos]):
                x = rt_entry(res, text, home, to, d, n, max_stops) if res else None
                if x:
                    rts[f"{to}|{d}|{add(d, n)}"] = x
            # Rueckflug fuer die 8 guenstigsten Tickets und die 4 guenstigsten Direktflug-Varianten
            jobs = [(k, "x", x) for k, x in sorted(rts.items(), key=lambda kv: kv[1]["b"])[:8]]
            jobs += [(k, "alt", x["alt"]) for k, x in sorted(((k, x) for k, x in rts.items() if x.get("alt")),
                                                              key=lambda kv: kv[1]["alt"]["b"])[:4]]
            det = []
            for key, which, y in jobs:
                if y.get("segs"):
                    _, d, rd = key.split("|")
                    det.append((key, which, y, d, (date.fromisoformat(rd) - date.fromisoformat(d)).days))
            state.update(phase="ret", n=0, total=len(det))
            progress("live", "ret", 0, len(det))
            results = ex.map(fetch, [(d, n, tuple(y["segs"]), y.get("nb", 0)) for _, _, y, d, n in det])
            for (key, which, y, d, n), (_, (res, text)) in zip(det, results):
                if res is None:
                    continue
                if not apply_return(cfg, y, res, text, home, to, d, n, tuple(y["segs"]), max_stops):
                    x = rts.get(key)
                    if not x:
                        continue
                    if which == "alt":
                        x.pop("alt", None)
                    elif x.get("alt"):
                        rts[key] = x["alt"]
                    else:
                        rts.pop(key)
        for x in rts.values():
            x.pop("segs", None)
            x.pop("nb", None)
            if "alt" in x:
                x["alt"].pop("segs", None)
                x["alt"].pop("nb", None)
        out.update(status="ok", rt=rts)
        log(f"Live-Pruefung fertig: {len(rts)} Hin+Rueck-Tickets")
        return 0
    except Blocked as e:
        out["status"] = "blocked"
        log(f"Live-Pruefung abgebrochen: {e}")
        return 1
    finally:
        out["generated"] = datetime.now().isoformat(timespec="seconds")
        LIVE_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        keep_awake(False)
        progress("live", "done" if out["status"] == "ok" else "error")
        LIVE_LOCK.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="Vorschau-Zeitraum ueberschreiben (Test)")
    ap.add_argument("--if-stale", action="store_true", help="nur abrufen, wenn heute noch nicht versucht")
    ap.add_argument("--background", action="store_true", help="wie --if-stale, startet aber losgeloest im Hintergrund")
    ap.add_argument("--rt-check", nargs=4, metavar=("REISE", "HIN", "RUECK", "FLEX"),
                    help="Live-Pruefung der Hin+Rueck-Tickets fuer die eigene Suche (Dashboard-Knopf)")
    args = ap.parse_args()

    if args.rt_check:
        return rt_check(*args.rt_check)

    if (args.if_stale or args.background) and (not is_stale() or locked()):
        return 0
    if args.background:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(ROOT / "flights.py"), "--if-stale"],
                         cwd=ROOT, creationflags=0x00000008 | 0x00000200 | NO_WINDOW, close_fds=True)
        return 0
    if locked():  # z.B. Klick im Dashboard, waehrend der taegliche Abruf laeuft
        log("Abruf laeuft bereits - uebersprungen")
        return 0
    LOCK.write_text(str(os.getpid()))
    progress("refresh", "start")
    keep_awake(True)
    rc = 1
    try:
        rc = run(args)
        if args.days:  # Testlauf mit verkuerzter Vorschau nie online stellen
            log("Testlauf (--days): nicht online gestellt")
            return rc
        progress("refresh", "publish")
        ok, info = publish.publish(log)  # Online-Ansicht (GitHub Pages), auch bei Fehler -> dort erscheint der Hinweis
        if rc == 0:  # Fehlschlag des Abrufs wurde schon in run() gemeldet
            if ok:
                notify("Flugradar: Neue Preise online ✈", summary(), info)
            else:
                notify("Flugradar: Neue Preise abgerufen", f"{summary()}\nOnline-Ansicht: {info}")
        return rc
    finally:
        keep_awake(False)
        progress("refresh", "done" if rc == 0 else "error")
        LOCK.unlink(missing_ok=True)


def run(args):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    horizon = args.days or int(cfg["horizonDays"])
    max_stops = int(cfg["maxStops"])
    today = date.today()
    tk = today.isoformat()
    need = plan(cfg, today, horizon)

    google = Google(cfg)
    extra_sources = [Travelpayouts(cfg, cfg["travelpayoutsToken"])] if cfg.get("travelpayoutsToken") else []

    old = {}
    if (ROOT / "data.json").exists():
        try:
            old = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    old_legs = old.get("legs", {})
    if not old_legs and old.get("routes"):  # altes Format (v1) uebernehmen
        for r in old["routes"]:
            old_legs[f'{r["from"]}-{r["to"]}'] = r.get("out", {})
            old_legs[f'{r["to"]}-{r["from"]}'] = r.get("ret", {})

    status = {"level": "ok", "messages": []}
    stop = threading.Event()
    counter = {"n": 0, "total": 0, "phase": "probe"}
    delay = float(cfg.get("delaySeconds", 1.5))

    def fetch_leg(job):
        key, d = job
        if stop.is_set():
            return job, None
        frm, to = key.split("-")
        try:
            res = google.fetch(frm, to, d)
        except Blocked as e:
            stop.set()
            status["blocked"] = str(e)
            return job, None
        fl = None if res is None else list(res)
        for s in extra_sources:
            x = s.fetch(frm, to, d)
            fl = (fl or []) + x
        time.sleep(delay)
        counter["n"] += 1
        if counter["n"] % 5 == 0:
            progress("refresh", counter["phase"], counter["n"], counter["total"])
        if counter["n"] % 50 == 0:
            log(f"  {counter['n']} / {counter['total']}")
        return job, (None if fl is None else keep_best(fl, max_stops))

    workers = int(cfg.get("workers", 2))
    legs = {k: {} for k in need}
    failed = 0
    log(f"Start: {sum(len(v) for v in need.values())} Einzelstrecken-Tage auf {len(need)} Richtungen")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        # 1) Stichprobe je Richtung: gibt es dort ueberhaupt Fluege? (z.B. Bratislava)
        probe = {k: [v[i] for i in sorted({len(v) // 6, len(v) // 2, (5 * len(v)) // 6})] for k, v in need.items()}
        jobs = [(k, d) for k, ds in probe.items() for d in ds]
        counter.update(n=0, total=len(jobs), phase="probe")
        progress("refresh", "probe", 0, len(jobs))
        no_service = set()
        found = {}
        for (k, d), fl in ex.map(fetch_leg, jobs):
            if fl is not None:
                legs[k][d] = fl
                found[k] = found.get(k, 0) + len(fl)
        for k in need:
            if not found.get(k) and not stop.is_set():
                no_service.add(k)
        if no_service:
            log("Keine Fluege gefunden, Richtung uebersprungen: " + ", ".join(sorted(no_service)))

        # 2) alle Tage der Richtungen mit Flugangebot
        jobs = [(k, d) for k, ds in need.items() if k not in no_service for d in ds if d not in legs[k]]
        counter.update(n=0, total=len(jobs), phase="legs")
        for (k, d), fl in ex.map(fetch_leg, jobs):
            if fl is None and not stop.is_set():
                failed += 1
            if fl is None:
                fl = old_legs.get(k, {}).get(d)  # Abruf fehlgeschlagen -> Vortageswerte behalten
            if fl is not None:
                legs[k][d] = fl

        # 3) Hin- und Rueckflug-Tarife fuer die besten Kandidaten je Woche (+ Sonderabfragen)
        rts = {t["id"]: {} for t in cfg["trips"]}
        rt_jobs = []
        if not stop.is_set():
            for t in cfg["trips"]:
                if not t.get("roundTrip"):
                    continue
                main_days, extra_days = trip_days(cfg, t, today, horizon)
                a = next((a for a in t["dest"] if f'{t["home"]}-{a}' not in no_service), None)
                if not a:
                    continue
                groups = t.get("nightGroups") or [[t["minNights"], t["maxNights"]]]
                scored = []
                for d, n in main_days + extra_days:
                    p = trip_price(cfg, t, legs, {}, d, n, rt=False)
                    if p is not None:
                        scored.append((p, d, n))
                picked, main_set = set(), set(main_days)
                for g in groups:
                    by_week = {}
                    for p, d, n in sorted(scored):
                        if g[0] <= n <= g[1] and (d, n) in main_set:
                            lst = by_week.setdefault(monday(d), [])
                            if len(lst) < (2 if t["maxNights"] - t["minNights"] > 5 else 3):
                                lst.append((d, n))
                    for lst in by_week.values():
                        picked.update(lst)
                extra_set = set(extra_days)
                picked.update([(d, n) for p, d, n in sorted(scored) if (d, n) in extra_set][:4])
                rt_jobs += [(t["id"], t["home"], a, d, n) for d, n in sorted(picked)]

        def fetch_rt(job):
            """Hin+Rueck-Suche; mit Segmenten (job[5]) die Rueckfluege zu genau diesem Hinflug."""
            tid, frm, to, d, n, segs = job[:6]
            if stop.is_set():
                return job, (None, "")
            try:
                res = google.fetch_rt(frm, to, d, add(d, n), out_segs=segs, nb=job[7] if len(job) > 7 else 0)
            except Blocked as e:
                stop.set()
                status["blocked"] = str(e)
                return job, (None, "")
            time.sleep(delay)
            counter["n"] += 1
            if counter["n"] % 5 == 0:
                progress("refresh", counter["phase"], counter["n"], counter["total"])
            if counter["n"] % 50 == 0:
                log(f"  {counter['phase']} {counter['n']} / {counter['total']}")
            return job, res

        rt_jobs = [j + ((),) for j in rt_jobs]
        counter.update(n=0, total=len(rt_jobs), phase="rt")
        if rt_jobs:
            log(f"Hin- und Rueckflug-Tarife: {len(rt_jobs)} Abfragen")
        for (tid, frm, to, d, n, _), (res, text) in ex.map(fetch_rt, rt_jobs):
            b = rt_entry(res, text, frm, to, d, n, max_stops) if res else None
            if b:
                rts[tid][f"{to}|{d}|{add(d, n)}"] = b

        # 4) Rueckflug zu genau den Hin+Rueck-Tickets abfragen, die das Dashboard zeigt (beste Reise und
        #    Alternativen jeder Woche, Top 3, Sonderabfragen): echter Rueckflug statt "im Ticket", und die Regeln
        #    (keine Nachtverbindung, max. 10 h, bei Alternativen direkt) gelten auch fuer ihn.
        #    Zweite Runde, weil sich durch verworfene/teurere Tickets die Auswahl verschieben kann.
        asked, dropped = set(), 0
        counter.update(n=0, total=0, phase="ret")
        for round_no in (1, 2):
            if stop.is_set():
                break
            det_jobs = []
            for t in cfg["trips"]:
                for key, which in visible_rts(cfg, t, legs, rts, today, horizon):
                    x = rts[t["id"]].get(key)
                    y = (x or {}).get("alt") if which == "alt" else x
                    if not y or "r" in y or not y.get("segs") or (t["id"], key, which) in asked:
                        continue
                    asked.add((t["id"], key, which))
                    to, d, rd = key.split("|")
                    n = (date.fromisoformat(rd) - date.fromisoformat(d)).days
                    det_jobs.append((t["id"], t["home"], to, d, n, tuple(y["segs"]), which, y.get("nb", 0)))
            if not det_jobs:
                break
            counter["total"] += len(det_jobs)
            log(f"Rueckflug-Details (Runde {round_no}): {len(det_jobs)} Abfragen")
            for (tid, frm, to, d, n, segs, which, _), (res, text) in ex.map(fetch_rt, det_jobs):
                if res is None:
                    continue  # Abruf fehlgeschlagen -> Ticket bleibt ohne Rueckflug-Details
                key = f"{to}|{d}|{add(d, n)}"
                x = rts[tid].get(key)
                if not x:
                    continue
                y = x.get("alt") if which == "alt" else x
                if not apply_return(cfg, y, res, text, frm, to, d, n, segs, max_stops):  # nur Nacht-/Langstrecken-Rueckfluege
                    dropped += 1
                    if which == "alt":
                        x.pop("alt", None)
                    elif x.get("alt"):
                        rts[tid][key] = x["alt"]  # die Direktflug-Variante bleibt als Ticket uebrig
                    else:
                        rts[tid].pop(key)
        if dropped:
            log(f"  {dropped} Hin+Rueck-Tickets verworfen (nur Nachtverbindungen oder > 10 h beim Rueckflug)")
        for per_trip in rts.values():  # Flugnummern werden nur waehrend des Abrufs gebraucht
            for x in per_trip.values():
                x.pop("segs", None)
                x.pop("nb", None)
                if "alt" in x:
                    x["alt"].pop("segs", None)
                    x["alt"].pop("nb", None)

    total_found = sum(len(v) for v in legs.values())

    # ---- Gesundheits-Checks
    if status.get("blocked"):
        status["level"] = "error"
        status["messages"].append("Google hat die Abfrage blockiert (Sperr-/Captcha-Seite). Abruf abgebrochen, "
                                  "es werden die letzten gueltigen Preise angezeigt. Neuer Versuch morgen.")
        log("ABBRUCH: " + status["blocked"])
    if total_found == 0 or (not status.get("blocked") and len(no_service) == len(need)):
        status["level"] = "error"
        status["messages"].append("Es wurden keine Fluege erkannt - vermutlich hat Google die Seite umgebaut. "
                                  "Das Abrufskript muss angepasst werden.")
    for k in need:
        if k in no_service and any(old_legs.get(k, {}).values()):
            status["level"] = "warn" if status["level"] == "ok" else status["level"]
            status["messages"].append(f"{k}: frueher gab es Fluege, heute keine erkannt - bitte pruefen.")
    if failed:
        status["messages"].append(f"{failed} Tagesabfragen fehlgeschlagen, dort werden Vortagespreise angezeigt.")

    if status["level"] == "error":
        # letzte gueltige Daten behalten, nur Status aktualisieren
        if old:
            old["status"], old["lastAttempt"] = status, datetime.now().isoformat(timespec="seconds")
            write(old)
        notify("Flugradar: Abruf fehlgeschlagen", status["messages"][0])
        return 1

    # ---- Reisen fuer Verlauf, Vortagsvergleich und Preisalarm
    trips_now, daily, alerts = {}, {}, []
    for t in cfg["trips"]:
        main_days, _ = trip_days(cfg, t, today, horizon)
        trips, mn, mnd = {}, None, None
        for d, n in main_days:
            p = trip_price(cfg, t, legs, rts, d, n)
            if p is not None:
                trips[f"{d}|{n}"] = round(p)
                mn = p if mn is None else min(mn, p)
            pd = trip_price(cfg, t, legs, rts, d, n, dict(DEFAULT_FILTER, direct=True))
            if pd is not None:
                mnd = pd if mnd is None else min(mnd, pd)
        trips_now[t["id"]] = trips
        daily[t["id"]] = {"min": mn, "minDirect": mnd}
        lim = t.get("alertBelow")
        if lim:
            for k, p in trips.items():
                if p <= lim:
                    alerts.append({"trip": t["id"], "key": k, "price": p})

    hist_file = ROOT / "history.json"
    hist = {"daily": {}, "last": None, "prev": None, "alerted": {}}
    if hist_file.exists():
        try:
            hist.update(json.loads(hist_file.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            log("history.json nicht lesbar, beginne neu")
    rename = {"HAM-VIE": "wien", "HAM-MAD": "madrid"}  # altes Format (v1)
    for day_ in hist["daily"].values():
        for o, nw in rename.items():
            if o in day_:
                day_[nw] = day_.pop(o)
    for snap in (hist.get("last"), hist.get("prev")):
        if snap:
            for o, nw in rename.items():
                if o in snap.get("trips", {}):
                    snap["trips"][nw] = snap["trips"].pop(o)
    if hist.get("last") and hist["last"]["date"] != tk:
        hist["prev"] = hist["last"]
    hist["last"] = {"date": tk, "trips": trips_now}
    hist["daily"][tk] = daily

    # Preisalarm: nur neue Treffer oder deutlich gesunkene Preise melden
    alerted = {k: v for k, v in hist.get("alerted", {}).items() if k.split("|")[1] > tk}
    fresh = [a for a in alerts if f'{a["trip"]}|{a["key"]}' not in alerted or a["price"] <= alerted[f'{a["trip"]}|{a["key"]}'] - 5]
    for a in alerts:
        alerted[f'{a["trip"]}|{a["key"]}'] = min(a["price"], alerted.get(f'{a["trip"]}|{a["key"]}', 1e9))
    hist["alerted"] = alerted
    hist_file.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")

    if fresh:
        lines = []
        for t in cfg["trips"]:
            mine = sorted((a for a in fresh if a["trip"] == t["id"]), key=lambda a: a["price"])
            if mine:
                d, n = mine[0]["key"].split("|")
                lines.append(f'{t["label"]}: {len(mine)}x unter {t["alertBelow"]} €, ab {mine[0]["price"]} € '
                             f'({date.fromisoformat(d):%d.%m.}, {n} Nächte)')
        notify("Flugradar: Preisalarm ✈", "\n".join(lines))
        log("Preisalarm: " + " | ".join(lines))

    data = {
        "version": 2, "generated": datetime.now().isoformat(timespec="seconds"),
        "lastAttempt": datetime.now().isoformat(timespec="seconds"), "today": tk, "horizonDays": horizon,
        "maxStops": max_stops, "status": status, "failed": failed, "noService": sorted(no_service),
        "sources": ["Google Flights"] + [s.name for s in extra_sources],
        "bagFees": cfg["bagFees"],
        "trips": cfg["trips"], "extraSearches": cfg.get("extraSearches", []),
        "legs": legs, "rt": rts, "history": hist["daily"], "prevTrips": hist.get("prev"),
        "alerts": alerts,
    }
    write(data)
    log(f"Fertig. {failed} Fehler. Guenstigste Reise: " + ", ".join(f"{k} {v['min']:.0f} EUR" for k, v in daily.items() if v["min"]))
    return 0


def summary():
    """Guenstigste Reise je Reiseart aus dem letzten Abruf, fuer die Benachrichtigung."""
    try:
        d = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
        day = d["history"].get(d["today"], {})
        return "\n".join(f'{t["label"]}: ab {day[t["id"]]["min"]:.0f} €' for t in d["trips"]
                         if day.get(t["id"], {}).get("min") is not None)
    except Exception:  # noqa: BLE001
        return ""


def write(data):
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (ROOT / "data.json").write_text(js, encoding="utf-8")
    (ROOT / "data.js").write_text(f"window.FLIGHT_DATA = {js};", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
