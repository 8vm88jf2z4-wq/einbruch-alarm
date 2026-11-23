"""Einbruch-Alarm Windberg
Liest die Polizeimeldungen aus dem Presseportal und schickt per ntfy eine
Push-Nachricht, wenn ein Stadtteil aus der Stichwortliste erwähnt wird.
Läuft automatisch über GitHub Actions (siehe .github/workflows/alarm.yml).
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

# --- Einstellungen ---------------------------------------------------------
FEEDS = {
    "Polizei Mönchengladbach": "https://www.presseportal.de/rss/dienststelle_30127.rss2",
    "Kreispolizei Viersen": "https://www.presseportal.de/rss/dienststelle_65857.rss2",
}

# Stadtteile, die eine Nachricht auslösen (Groß-/Kleinschreibung egal)
STADTTEILE = ["Windberg", "Großheide", "Grossheide", "Helenabrunn", "Venn", "Eicken"]

# Wörter, bei denen die Nachricht als dringend markiert wird
# Nur Meldungen mit einem dieser Wörter werden verschickt (Einbruch, Diebstahl, Verdächtiges).
# Unfälle, Brände, Vermisste usw. werden ignoriert.
RELEVANTE_WOERTER = ["einbruch", "eingebrochen", "einbrecher", "einbrüche", "aufgehebelt",
                     "hebelspuren", "aufgebrochen", "diebstahl", "diebe", "gestohlen",
                     "entwendet", "verdächtig", "trickbetrug", "trickdieb"]

EINBRUCH_WOERTER = ["einbruch", "eingebrochen", "einbrecher", "einbrüche",
                    "aufgehebelt", "hebelspuren", "verdächtig"]

STATE_FILE = "state.json"
# Link zur veröffentlichten CSV der Formular-Antworten, kommt aus dem Secret FORMULAR_CSV
FORMULAR_CSV = os.environ.get("FORMULAR_CSV", "").strip()
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


# --- Meldeformular der Nachbarn ---------------------------------------------
def spalte(kopf, *anfaenge):
    """Findet die Spalte, deren Überschrift mit einem der Anfänge beginnt."""
    for i, k in enumerate(kopf):
        if k.strip().lower().startswith(anfaenge):
            return i
    return None


def lies_datum(text, ersatz):
    text = (text or "").strip().split(" ")[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return ersatz


def lies_formular():
    """Liefert die Formular-Einträge als Liste von Dicts (leer, falls nicht eingerichtet)."""
    if not FORMULAR_CSV:
        return []
    roh = hole_feed(FORMULAR_CSV).decode("utf-8-sig")
    zeilen = list(csv.reader(io.StringIO(roh)))
    if len(zeilen) < 2:
        return []
    kopf = zeilen[0]
    s_zeit = 0
    s_was = spalte(kopf, "was")
    s_datum = spalte(kopf, "datum")
    s_uhr = spalte(kopf, "uhrzeit")
    s_wo = spalte(kopf, "wo")
    s_text = spalte(kopf, "beschreibung")
    s_pol = spalte(kopf, "polizei")
    feld = lambda z, i: z[i].strip() if i is not None and i < len(z) else ""
    eintraege = []
    for z in zeilen[1:]:
        if not any(z):
            continue
        eingang = lies_datum(feld(z, s_zeit), date.today())
        eintraege.append({
            "id": "f-" + hashlib.sha1("|".join(z).encode("utf-8")).hexdigest()[:16],
            "was": feld(z, s_was) or "Sonstiges",
            "datum": lies_datum(feld(z, s_datum), eingang),
            "uhr": feld(z, s_uhr)[:5],
            "wo": feld(z, s_wo),
            "text": feld(z, s_text),
            "polizei": feld(z, s_pol),
        })
    return eintraege


def sende_push(topic, titel, nachricht, link=None, dringend=False, prioritaet=None):
    daten = {
        "topic": topic,
        "title": titel,
        "message": nachricht,
        "priority": prioritaet or (5 if dringend else 3),
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
    status.setdefault("beobachtungen", [])
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
            if fund and not any(w in gesamt for w in RELEVANTE_WOERTER):
                print(f"Übersprungen (nicht relevant): {m['titel']}")
            elif fund:
                dringend = any(w in gesamt for w in EINBRUCH_WOERTER)
                treffer.append((quelle, ort, m, dringend))

    # Meldeformular: neue Einträge sofort weitermelden und für die Ampel merken.
    # In state.json (öffentlich) landen nur Datum und Art, keine Orte oder Beschreibungen.
    formular_neu = []
    try:
        eintraege = lies_formular()
        if FORMULAR_CSV:
            print(f"Meldeformular: {len(eintraege)} Einträge gelesen")
        formular_start = FORMULAR_CSV and not status.get("formular_init")
        for e in eintraege:
            if e["id"] in gesehen:
                continue
            neu_gesehen.append(e["id"])
            gesehen.add(e["id"])
            if formular_start:
                continue  # beim ersten Einlesen alte Testeinträge nicht melden
            formular_neu.append(e)
            art = e["was"].lower()
            if "einbruch" in art:
                status["einbrueche"].append({"datum": e["datum"].isoformat(), "lokal": True,
                                             "ort": "Nachbarschaft", "titel": e["was"]})
            elif "verdächtig" in art:
                status["beobachtungen"].append({"datum": e["datum"].isoformat(), "art": e["was"]})
        if formular_start:
            status["formular_init"] = True
            print("Meldeformular: erstes Einlesen, vorhandene Einträge gemerkt.")
    except Exception as ex:
        print(f"Warnung: Meldeformular nicht lesbar: {ex}")

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
        for e in formular_neu:
            einbruch = "einbruch" in e["was"].lower()
            zeilen = [f"Wo: {e['wo'] or 'k. A.'}",
                      f"Wann: {e['datum']:%d.%m.}" + (f" {e['uhr']} Uhr" if e["uhr"] else "")]
            if e["text"]:
                zeilen.append(e["text"])
            if e["polizei"]:
                zeilen.append(f"Polizei informiert: {e['polizei']}")
            sende_push(topic, "Nachbarschaft meldet: " + e["was"], "\n".join(zeilen),
                       prioritaet=5 if einbruch else 4)
            print(f"Gesendet: Formular-Eintrag {e['was']}")

    # Status speichern: max. 1000 Kennungen, plus wöchentliches Lebenszeichen,
    # damit GitHub den Zeitplan nicht nach 60 Tagen ohne Aktivität abschaltet.
    status["seen"] = (status["seen"] + neu_gesehen)[-1000:]
    status.setdefault("seit", date.today().isoformat())  # Start der Aufzeichnung für den Trendvergleich
    grenze = (date.today() - timedelta(days=90)).isoformat()
    status["einbrueche"] = [e for e in status["einbrueche"] if e["datum"] >= grenze]
    status["beobachtungen"] = [b for b in status["beobachtungen"] if b["datum"] >= grenze]
    heute = date.today().isoformat()
    letzter = status.get("heartbeat") or "1970-01-01"
    if (date.today() - date.fromisoformat(letzter)).days >= 7:
        status["heartbeat"] = heute
    speichere_status(status)


if __name__ == "__main__":
    main()
