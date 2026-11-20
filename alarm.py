"""Einbruch-Alarm Windberg
Liest die Polizeimeldungen aus dem Presseportal und schickt per ntfy eine
Push-Nachricht, wenn ein Stadtteil aus der Stichwortliste erwähnt wird.
Läuft automatisch über GitHub Actions (siehe .github/workflows/alarm.yml).
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

# --- Einstellungen ---------------------------------------------------------
FEEDS = {
    "Polizei Mönchengladbach": "https://www.presseportal.de/rss/dienststelle_30127.rss2",
    "Kreispolizei Viersen": "https://www.presseportal.de/rss/dienststelle_65857.rss2",
}

# Stadtteile, die eine Nachricht auslösen (Groß-/Kleinschreibung egal)
STADTTEILE = ["Windberg", "Großheide", "Grossheide", "Helenabrunn", "Venn", "Eicken"]

# Wörter, bei denen die Nachricht als dringend markiert wird
EINBRUCH_WOERTER = ["einbruch", "eingebrochen", "einbrecher", "einbrüche",
                    "aufgehebelt", "hebelspuren", "verdächtig"]

STATE_FILE = "state.json"
NTFY_SERVER = "https://ntfy.sh"
# ---------------------------------------------------------------------------

STADTTEIL_RE = re.compile(r"\b(" + "|".join(map(re.escape, STADTTEILE)) + r")", re.IGNORECASE)


def lade_status():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def speichere_status(status):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)


def hole_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Einbruch-Alarm-Windberg/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def lies_meldungen(xml_bytes):
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        text = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        kennung = (item.findtext("guid") or link or titel).strip()
        try:
            datum = parsedate_to_datetime(item.findtext("pubDate")).date().isoformat()
        except Exception:
            datum = date.today().isoformat()
        yield {"id": kennung, "titel": titel, "link": link, "text": text, "datum": datum}


def sende_push(topic, titel, nachricht, link=None, dringend=False):
    daten = {
        "topic": topic,
        "title": titel,
        "message": nachricht,
        "priority": 5 if dringend else 3,
        "tags": ["rotating_light"] if dringend else ["police_car"],
    }
    if link:
        daten["click"] = link
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(daten).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).read()


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("Fehler: Das Secret NTFY_TOPIC ist nicht gesetzt.")

    status = lade_status()
    erster_lauf = status is None
    if erster_lauf:
        status = {"seen": [], "heartbeat": ""}
    status.setdefault("einbrueche", [])
    gesehen = set(status["seen"])
    neu_gesehen = []
    treffer = []
    feeds_gelesen = 0

    for quelle, url in FEEDS.items():
        try:
            meldungen = list(lies_meldungen(hole_feed(url)))
        except Exception as e:  # ein Feed-Ausfall soll den anderen nicht stoppen
            print(f"Warnung: {quelle} nicht erreichbar: {e}")
            continue
        feeds_gelesen += 1
        print(f"{quelle}: {len(meldungen)} Meldungen gelesen")
        for m in meldungen:
            if m["id"] in gesehen:
                continue
            neu_gesehen.append(m["id"])
            gesehen.add(m["id"])
            gesamt = (m["titel"] + " " + m["text"]).lower()
            ist_einbruch = any(w in gesamt for w in ("einbruch", "eingebrochen", "einbrecher", "einbrüche"))
            fund = STADTTEIL_RE.search(m["titel"] + " " + m["text"])
            ort = next(s for s in STADTTEILE if s.lower() == fund.group(1).lower()) if fund else ""
            if ist_einbruch:  # für die Risikoampel merken
                status["einbrueche"].append({"datum": m["datum"], "lokal": bool(fund),
                                             "ort": ort, "titel": m["titel"][:120]})
            if fund:
                dringend = any(w in gesamt for w in EINBRUCH_WOERTER)
                treffer.append((quelle, ort, m, dringend))

    if erster_lauf and feeds_gelesen == 0:
        sys.exit("Erster Lauf: kein Feed erreichbar, versuche es beim nächsten Lauf erneut.")

    if erster_lauf:
        sende_push(topic, "Einbruch-Alarm aktiv",
                   "Das Frühwarnsystem läuft. Du bekommst ab jetzt eine Nachricht, "
                   "wenn die Polizei etwas aus Windberg und Umgebung meldet.")
        print("Erster Lauf: bestehende Meldungen gemerkt, keine alten Treffer gesendet.")
    else:
        for quelle, ort, m, dringend in treffer:
            kopf = ("EINBRUCH: " if dringend else "Polizeimeldung: ") + ort
            sende_push(topic, kopf, f"{m['titel']}\n({quelle})", m["link"], dringend)
            print(f"Gesendet: {kopf} – {m['titel']}")

    # Status speichern: max. 1000 Kennungen, plus wöchentliches Lebenszeichen,
    # damit GitHub den Zeitplan nicht nach 60 Tagen ohne Aktivität abschaltet.
    status["seen"] = (status["seen"] + neu_gesehen)[-1000:]
    grenze = (date.today() - timedelta(days=30)).isoformat()
    status["einbrueche"] = [e for e in status["einbrueche"] if e["datum"] >= grenze]
    heute = date.today().isoformat()
    letzter = status.get("heartbeat") or "1970-01-01"
    if (date.today() - date.fromisoformat(letzter)).days >= 7:
        status["heartbeat"] = heute
    speichere_status(status)


if __name__ == "__main__":
    main()
