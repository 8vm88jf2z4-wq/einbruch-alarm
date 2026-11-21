"""Einbruch-Risikoampel Windberg
Berechnet für jeden Tag ein Punkte-Risiko aus Dunkelheit, Wochentag, Feiertagen,
Schulferien, besonderen Abenden, Regen und aktuellen Einbruchsmeldungen.
- sonntags gegen 18:30 Uhr: Wochenausblick für die kommende Woche
- täglich mittags: Warnung, aber nur bei GELB oder ROT
Läuft über GitHub Actions (siehe .github/workflows/ampel.yml).
"""
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# --- Einstellungen ---------------------------------------------------------
BREITE, LAENGE = 51.188, 6.415          # Mönchengladbach-Windberg
ZONE = ZoneInfo("Europe/Berlin")
NTFY_SERVER = "https://ntfy.sh"
STATE_FILE = "state.json"

GELB_AB = 5   # Punkte
ROT_AB = 7    # Punkte

PUNKTE = {
    "dunkel_frueh": 2,      # Sonnenuntergang vor 17:30
    "dunkel": 1,            # Sonnenuntergang vor 18:30
    "werktag": 1,           # Mo–Fr, kein Feiertag
    "feiertag": 1,
    "ferien": 1,            # Schulferien NRW
    "anlass": 1,            # St. Martin, Halloween, Weihnachten, Silvester, Karneval
    "regen": 1,             # mind. 3 Stunden Regen
    "einbruch_nah": 4,      # Einbruch in euren Stadtteilen in den letzten 7 Tagen
    "einbrueche_stadt": 1,  # Übergang: mind. 3 Einbruchsmeldungen anderswo in 7 Tagen
    "trend_hoch": 1,        # Einbrüche in MG/Viersen mind. 1,5-mal so viele wie üblich
    "trend_sehr_hoch": 2,   # Einbrüche in MG/Viersen mind. doppelt so viele wie üblich
    "veranstaltung": 1,     # Heimspiel Borussia oder eingetragene Großveranstaltung
    "verdaechtig": 2,       # verdächtige Beobachtung aus dem Meldeformular, letzte 3 Tage
}
# Weitere Großveranstaltungen von Hand eintragen: date(Jahr, Monat, Tag): "Name"
VERANSTALTUNGEN = {
    date(2026, 11, 21): "Heimspiel Borussia – Dortmund",
}
# Von Hand nachgetragene Meldungen (z. B. aus der WhatsApp-Gruppe) stehen in meldungen.csv:
# eine Zeile pro Meldung: Datum;Art;Ort  (Art: einbruch = in unserer Nähe, 4 Punkte für 7 Tage;
# verdaechtig = 2 Punkte für 3 Tage; region = Einbruch anderswo in der Stadt, zählt für den
# Trendvergleich). Zeilen mit # am Anfang werden ignoriert.
MELDUNGEN_DATEI = "meldungen.csv"


def lade_manuelle_meldungen():
    meldungen = []
    try:
        with open(MELDUNGEN_DATEI, encoding="utf-8") as f:
            for nr, zeile in enumerate(f, 1):
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#"):
                    continue
                teile = [t.strip() for t in zeile.split(";")]
                try:
                    datum = date.fromisoformat(teile[0]).isoformat()
                except (ValueError, IndexError):
                    print(f"Warnung: meldungen.csv Zeile {nr} unlesbar: {zeile}")
                    continue
                art = teile[1].lower() if len(teile) > 1 else "verdaechtig"
                ort = teile[2] if len(teile) > 2 else ""
                if "region" in art:
                    art = "region"
                elif "einbruch" in art:
                    art = "einbruch"
                else:
                    art = "verdaechtig"
                meldungen.append({"datum": datum, "art": art, "ort": ort})
    except FileNotFoundError:
        pass
    print(f"Nachgetragene Meldungen: {len(meldungen)}")
    return meldungen
TREND_MIN_WOCHEN = 4   # so viele Wochen Daten braucht der Trendvergleich mindestens
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
# ---------------------------------------------------------------------------


def hole_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Einbruch-Ampel-Windberg/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --- Sonnenuntergang (NOAA-Näherung, genau auf ca. 1-2 Minuten) ------------
def sonnenuntergang(tag):
    n = tag.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + 0.5)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(BREITE)
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl))
                                - math.tan(lat) * math.tan(decl)))
    minuten_utc = 720 - 4 * (LAENGE - ha) - eqtime
    utc = datetime(tag.year, tag.month, tag.day, tzinfo=timezone.utc) + timedelta(minutes=minuten_utc)
    return utc.astimezone(ZONE)


# --- Feiertage NRW und besondere Abende ------------------------------------
def ostersonntag(jahr):
    a, b, c = jahr % 19, jahr // 100, jahr % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    monat = (h + l - 7 * m + 114) // 31
    tag = ((h + l - 7 * m + 114) % 31) + 1
    return date(jahr, monat, tag)


def feiertage_nrw(jahr):
    o = ostersonntag(jahr)
    return {
        date(jahr, 1, 1): "Neujahr", o - timedelta(2): "Karfreitag",
        o + timedelta(1): "Ostermontag", date(jahr, 5, 1): "Tag der Arbeit",
        o + timedelta(39): "Christi Himmelfahrt", o + timedelta(50): "Pfingstmontag",
        o + timedelta(60): "Fronleichnam", date(jahr, 10, 3): "Tag der Deutschen Einheit",
        date(jahr, 11, 1): "Allerheiligen", date(jahr, 12, 25): "1. Weihnachtstag",
        date(jahr, 12, 26): "2. Weihnachtstag",
    }


def besondere_abende(jahr):
    o = ostersonntag(jahr)
    abende = {
        o - timedelta(52): "Weiberfastnacht", o - timedelta(48): "Rosenmontag",
        o - timedelta(47): "Veilchendienstag", date(jahr, 10, 31): "Halloween",
        date(jahr, 12, 24): "Heiligabend", date(jahr, 12, 25): "Weihnachten",
        date(jahr, 12, 26): "Weihnachten", date(jahr, 12, 31): "Silvester",
    }
    for t in range(8, 13):  # Hauptzeit der Martinszüge
        abende[date(jahr, 11, t)] = "St.-Martins-Zeit"
    return abende


# --- Externe Daten (fallen sie aus, rechnet die Ampel ohne sie weiter) -----
def lade_schulferien(von, bis):
    url = ("https://openholidaysapi.org/SchoolHolidays?" + urllib.parse.urlencode({
        "countryIsoCode": "DE", "subdivisionCode": "DE-NW", "languageIsoCode": "DE",
        "validFrom": (von - timedelta(days=40)).isoformat(), "validTo": bis.isoformat()}))
    try:
        zeitraeume = []
        for f in hole_json(url):
            name = next((n["text"] for n in f.get("name", []) if n.get("text")), "Schulferien")
            zeitraeume.append((date.fromisoformat(f["startDate"]), date.fromisoformat(f["endDate"]), name))
        print(f"Schulferien: {len(zeitraeume)} Zeiträume geladen")
        return zeitraeume
    except Exception as e:
        print(f"Warnung: Schulferien nicht abrufbar: {e}")
        return []


def lade_regen(von, tage):
    url = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": BREITE, "longitude": LAENGE, "daily": "precipitation_hours,precipitation_sum",
        "timezone": "Europe/Berlin", "forecast_days": min(tage + 1, 16)}))
    try:
        d = hole_json(url)["daily"]
        regen = {date.fromisoformat(t): (h or 0) for t, h in zip(d["time"], d["precipitation_hours"])}
        print(f"Wetter: {len(regen)} Tage geladen")
        return regen
    except Exception as e:
        print(f"Warnung: Wetter nicht abrufbar: {e}")
        return {}


def lade_heimspiele(jahr):
    """Heimspiele von Borussia Mönchengladbach über die kostenlose OpenLigaDB."""
    spiele = {}
    for saison in (jahr - 1, jahr):
        try:
            for m in hole_json(f"https://api.openligadb.de/getmatchdata/bl1/{saison}"):
                heim = (m.get("team1") or {}).get("teamName", "")
                if "gladbach" in heim.lower():
                    tag = date.fromisoformat(m["matchDateTime"][:10])
                    gast = (m.get("team2") or {}).get("shortName") or (m.get("team2") or {}).get("teamName", "")
                    spiele[tag] = f"Heimspiel Borussia – {gast}"
        except Exception as e:
            print(f"Warnung: Spielplan {saison} nicht abrufbar: {e}")
    print(f"Heimspiele Borussia: {len(spiele)} geladen")
    return spiele


def regional_trend(tag, einbrueche, seit):
    """Vergleicht Einbrüche der letzten 7 Tage mit dem Wochenschnitt davor.
    Liefert (faktor, aktuell, ueblich) oder None, solange zu wenig Daten da sind."""
    try:
        start = date.fromisoformat(seit)
    except (TypeError, ValueError):
        return None
    wochen = min((tag - start).days - 7, 83) / 7
    if wochen < TREND_MIN_WOCHEN:
        return None
    aktuell = sum(1 for e in einbrueche if 0 <= (tag - date.fromisoformat(e["datum"])).days < 7)
    vorher = sum(1 for e in einbrueche if 7 <= (tag - date.fromisoformat(e["datum"])).days < 7 + wochen * 7)
    ueblich = vorher / wochen
    return (aktuell / max(ueblich, 0.5)), aktuell, ueblich


def lade_status():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


# --- Bewertung -------------------------------------------------------------
def bewerte(tag, ferien, regen, einbrueche, beobachtungen=(), veranstaltungen=None, seit=None):
    gruende, punkte = [], 0

    def plus(schluessel, text):
        nonlocal punkte
        punkte += PUNKTE[schluessel]
        gruende.append(text)

    su = sonnenuntergang(tag)
    if su.hour * 60 + su.minute < 17 * 60 + 30:
        plus("dunkel_frueh", f"früh dunkel ({su:%H:%M})")
    elif su.hour * 60 + su.minute < 18 * 60 + 30:
        plus("dunkel", f"Dämmerung am frühen Abend ({su:%H:%M})")

    feiertag = feiertage_nrw(tag.year).get(tag)
    if feiertag:
        plus("feiertag", feiertag)
    elif tag.weekday() < 5:
        plus("werktag", "Werktag")

    ferienname = next((n for s, e, n in ferien if s <= tag <= e), None)
    if ferienname:
        plus("ferien", ferienname)

    anlass = besondere_abende(tag.year).get(tag)
    if anlass and anlass != feiertag:
        plus("anlass", anlass)

    event = (veranstaltungen or {}).get(tag) or VERANSTALTUNGEN.get(tag)
    if event:
        plus("veranstaltung", event)

    if regen.get(tag, 0) >= 3:
        plus("regen", f"Regen ({regen[tag]:.0f} Std.)")

    fenster = [e for e in einbrueche
               if 0 <= (tag - date.fromisoformat(e["datum"])).days <= 7]
    nah = [e for e in fenster if e.get("lokal")]
    if nah:
        orte = ", ".join(sorted({e.get("ort", "") for e in nah if e.get("ort")}))
        plus("einbruch_nah", f"Einbruch in der Nähe ({orte})" if orte else "Einbruch in der Nähe")
    trend = regional_trend(tag, einbrueche, seit)
    if trend is None:  # noch zu wenig Daten: alte Regel
        if len(fenster) - len(nah) >= 3:
            plus("einbrueche_stadt", f"{len(fenster) - len(nah)} Einbruchsmeldungen in der Region")
    else:
        faktor, aktuell, ueblich = trend
        text = f"mehr Einbrüche in MG/Viersen als üblich ({aktuell} statt ca. {ueblich:.0f} pro Woche)"
        if faktor >= 2 and aktuell >= 4:
            plus("trend_sehr_hoch", text)
        elif faktor >= 1.5 and aktuell >= 3:
            plus("trend_hoch", text)

    verdacht = [b for b in beobachtungen
                if 0 <= (tag - date.fromisoformat(b["datum"])).days <= 3]
    if verdacht:
        plus("verdaechtig", "verdächtige Beobachtung in der Siedlung")

    farbe = "ROT" if punkte >= ROT_AB else "GELB" if punkte >= GELB_AB else "GRÜN"
    return farbe, punkte, gruende, su


SYMBOL = {"GRÜN": "🟢", "GELB": "🟡", "ROT": "🔴"}


def sende_push(topic, titel, nachricht, dringend=False, tags=None):
    daten = {"topic": topic, "title": titel, "message": nachricht,
             "priority": 5 if dringend else 3, "tags": tags or []}
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(daten).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def wochenausblick(start, ferien, regen, einbrueche, beobachtungen=(), veranstaltungen=None, seit=None):
    zeilen = []
    for i in range(7):
        tag = start + timedelta(days=i)
        farbe, _, gruende, _ = bewerte(tag, ferien, regen, einbrueche, beobachtungen, veranstaltungen, seit)
        zeile = f"{SYMBOL[farbe]} {WOCHENTAGE[tag.weekday()]} {tag:%d.%m.}"
        if farbe != "GRÜN":
            zeile += ": " + ", ".join(gruende)
        zeilen.append(zeile)
    zeilen.append("\nRegen ist nur für die nächsten Tage verlässlich, neue Einbrüche ändern die Ampel kurzfristig.")
    return "\n".join(zeilen)


def tageswarnung(heute, ferien, regen, einbrueche, beobachtungen=(), veranstaltungen=None, seit=None):
    farbe, punkte, gruende, su = bewerte(heute, ferien, regen, einbrueche, beobachtungen, veranstaltungen, seit)
    text = ("Gründe: " + ", ".join(gruende) +
            f"\n\nTipp: Ab {su - timedelta(minutes=30):%H:%M} Licht an oder Zeitschaltuhr, "
            "Fenster und Terrassentüren schließen, auf Fremde in der Siedlung achten.")
    return farbe, punkte, text


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("Fehler: Das Secret NTFY_TOPIC ist nicht gesetzt.")
    zeitplan = sys.argv[1] if len(sys.argv) > 1 else ""
    test = zeitplan == ""                       # manueller Start
    sonntag = zeitplan.strip().endswith("* * 0")  # Sonntags-Zeitplan
    jetzt = datetime.now(ZONE)
    heute = jetzt.date()

    # GitHub plant in UTC: sonntags laufen zwei Starts, gesendet wird nur der
    # um ca. 18:30 Uhr deutscher Zeit (funktioniert so in Winter- und Sommerzeit).
    if sonntag and jetzt.hour != 18:
        print(f"Sonntagslauf um {jetzt:%H:%M} Uhr übersprungen, der andere Start sendet.")
        return
    ferien = lade_schulferien(heute, heute + timedelta(days=8))
    regen = lade_regen(heute, 8)
    status = lade_status()
    einbrueche = list(status.get("einbrueche", []))
    beobachtungen = list(status.get("beobachtungen", []))
    for m in lade_manuelle_meldungen():
        if m["art"] == "einbruch":
            einbrueche.append({"datum": m["datum"], "lokal": True, "ort": m["ort"]})
        elif m["art"] == "region":  # Einbruch anderswo in der Stadt, zählt nur für Trend/Region
            einbrueche.append({"datum": m["datum"], "lokal": False, "ort": m["ort"]})
        else:
            beobachtungen.append({"datum": m["datum"], "art": m["ort"]})
    seit = status.get("seit")
    veranstaltungen = lade_heimspiele(heute.year)
    print(f"Im Speicher: {len(einbrueche)} Einbruchsmeldungen, {len(beobachtungen)} Beobachtungen")

    if sonntag or test:
        sende_push(topic, ("TEST: " if test else "") + "Wochenausblick Einbruchsrisiko",
                   wochenausblick(heute + timedelta(days=1), ferien, regen, einbrueche, beobachtungen,
                                  veranstaltungen, seit),
                   tags=["calendar"])
        print("Wochenausblick gesendet")

    if not sonntag:
        farbe, punkte, text = tageswarnung(heute, ferien, regen, einbrueche, beobachtungen,
                                           veranstaltungen, seit)
        print(f"Heute: {farbe} ({punkte} Punkte)")
        if farbe != "GRÜN" or test:
            sende_push(topic, ("TEST: " if test else "") + f"Einbruchsrisiko heute: {farbe}",
                       text, dringend=(farbe == "ROT"),
                       tags=["red_circle" if farbe == "ROT" else "yellow_circle" if farbe == "GELB"
                             else "green_circle"])
            print("Tageswarnung gesendet")


if __name__ == "__main__":
    main()
