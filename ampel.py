"""Einbruch-Risikoampel Windberg
Stufen: ROT = Einbruch/Einbruchsversuch in der Nähe (7 Tage), GELB-ROT = verdächtige
Beobachtung (3 Tage), sonst GELB oder GRÜN nach Punkten für Dunkelheit, Wetter, Kalender, Trend.
- sonntags gegen 18:30 Uhr: Wochenausblick für die kommende Woche
- alle 15 Minuten (python3 ampel.py pruefen, aus dem Alarm-Lauf): Nachricht nur, wenn die
  Ampel durch ein neues Ereignis auf Gelb-Rot oder Rot steigt
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

GELB_AB = 5   # Punkte aus Rahmenbedingungen für Gelb (Rot/Gelb-Rot nur durch Ereignisse)

PUNKTE = {
    "dunkel_frueh": 2,      # Sonnenuntergang vor 17:30
    "dunkel": 1,            # Sonnenuntergang vor 18:30
    "werktag": 1,           # Mo–Fr, kein Feiertag
    "feiertag": 1,
    "ferien": 1,            # Schulferien NRW
    "anlass": 1,            # St. Martin, Halloween, Weihnachten, Silvester, Karneval
    "regen": 1,             # mind. 3 Stunden Regen
    "einbrueche_stadt": 1,  # Übergang: mind. 3 Einbruchsmeldungen anderswo in 7 Tagen
    "trend_hoch": 1,        # Einbrüche in MG/Viersen mind. 1,5-mal so viele wie üblich
    "trend_sehr_hoch": 2,   # Einbrüche in MG/Viersen mind. doppelt so viele wie üblich
    "veranstaltung": 1,     # Heimspiel Borussia oder eingetragene Großveranstaltung
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
# Stufen, von niedrig nach hoch. ROT und GELB-ROT hängen nur an Ereignissen,
# GRÜN und GELB an Rahmenbedingungen (Dunkelheit, Wetter, Kalender, Trend).
RANG = {"GRÜN": 0, "GELB": 1, "GELB-ROT": 2, "ROT": 3}
SYMBOL = {"GRÜN": "🟢", "GELB": "🟡", "GELB-ROT": "🟡🔴", "ROT": "🔴"}
EINBRUCH_TAGE = 7   # so lange hält ein Einbruch in der Nähe die Ampel auf Rot
VERDACHT_TAGE = 3   # so lange hält eine verdächtige Beobachtung die Ampel auf Gelb-Rot


def zeitpunkt(eintrag):
    """Zeitpunkt eines Ereignisses; ohne Uhrzeit gilt 12:00 Uhr des Tages."""
    try:
        z = datetime.fromisoformat(eintrag["zeit"])
        return z if z.tzinfo else z.replace(tzinfo=ZONE)
    except (KeyError, TypeError, ValueError):
        return datetime.fromisoformat(eintrag["datum"] + "T12:00").replace(tzinfo=ZONE)


def alter_text(eintrag, jetzt):
    stunden = (jetzt - zeitpunkt(eintrag)).total_seconds() / 3600
    if stunden <= 24:
        return "neues Ereignis (letzte 24 Stunden)"
    tage = (jetzt.date() - date.fromisoformat(eintrag["datum"])).days
    return "gestern" if tage <= 1 else f"vor {tage} Tagen"


def aktive_ereignisse(tag, einbrueche, beobachtungen):
    nah = [e for e in einbrueche if e.get("lokal")
           and 0 <= (tag - date.fromisoformat(e["datum"])).days <= EINBRUCH_TAGE]
    verdacht = [b for b in beobachtungen
                if 0 <= (tag - date.fromisoformat(b["datum"])).days <= VERDACHT_TAGE]
    nah.sort(key=zeitpunkt, reverse=True)
    verdacht.sort(key=zeitpunkt, reverse=True)
    return nah, verdacht


def ereignis_stufe(tag, einbrueche, beobachtungen):
    nah, verdacht = aktive_ereignisse(tag, einbrueche, beobachtungen)
    return "ROT" if nah else "GELB-ROT" if verdacht else None


def ereignis_gruende(tag, einbrueche, beobachtungen, jetzt=None):
    """Ereignisse zuerst, das neueste vorn. Mit jetzt: Altersangabe, sonst Datum."""
    nah, verdacht = aktive_ereignisse(tag, einbrueche, beobachtungen)
    gruende = []
    for e in nah[:3]:
        wann = alter_text(e, jetzt) if jetzt else f"vom {date.fromisoformat(e['datum']):%d.%m.}"
        gruende.append(f"Einbruch in der Nähe{': ' + e['ort'] if e.get('ort') else ''} – {wann}")
    if len(nah) > 3:
        gruende.append(f"und {len(nah) - 3} weitere Einbrüche in der Nähe")
    for b in verdacht[:2]:
        wann = alter_text(b, jetzt) if jetzt else f"vom {date.fromisoformat(b['datum']):%d.%m.}"
        gruende.append(f"verdächtige Beobachtung – {wann}")
    return gruende


def bewerte(tag, ferien, regen, einbrueche, beobachtungen=(), veranstaltungen=None, seit=None,
            jetzt=None):
    """Liefert (stufe, gruende, sonnenuntergang). Gründe: Ereignisse zuerst."""
    gruende, punkte = [], 0

    def plus(schluessel, text):
        nonlocal punkte
        punkte += PUNKTE[schluessel]
        gruende.append(text)

    fenster = [e for e in einbrueche if 0 <= (tag - date.fromisoformat(e["datum"])).days <= 7]
    region = [e for e in fenster if not e.get("lokal")]
    trend = regional_trend(tag, einbrueche, seit)
    if trend is None:  # noch zu wenig Daten: alte Regel
        if len(region) >= 3:
            plus("einbrueche_stadt", f"{len(region)} Einbruchsmeldungen in der Region")
    else:
        faktor, aktuell, ueblich = trend
        text = f"mehr Einbrüche in MG/Viersen als üblich ({aktuell} statt ca. {ueblich:.0f} pro Woche)"
        if faktor >= 2 and aktuell >= 4:
            plus("trend_sehr_hoch", text)
        elif faktor >= 1.5 and aktuell >= 3:
            plus("trend_hoch", text)

    su = sonnenuntergang(tag)
    if su.hour * 60 + su.minute < 17 * 60 + 30:
        plus("dunkel_frueh", f"früh dunkel ({su:%H:%M})")
    elif su.hour * 60 + su.minute < 18 * 60 + 30:
        plus("dunkel", f"Dämmerung am frühen Abend ({su:%H:%M})")

    if regen.get(tag, 0) >= 3:
        plus("regen", f"Regen ({regen[tag]:.0f} Std.)")

    event = (veranstaltungen or {}).get(tag) or VERANSTALTUNGEN.get(tag)
    if event:
        plus("veranstaltung", event)

    feiertag = feiertage_nrw(tag.year).get(tag)
    anlass = besondere_abende(tag.year).get(tag)
    if anlass and anlass != feiertag:
        plus("anlass", anlass)
    if feiertag:
        plus("feiertag", feiertag)
    elif tag.weekday() < 5:
        plus("werktag", "Werktag")

    ferienname = next((n for s, e, n in ferien if s <= tag <= e), None)
    if ferienname:
        plus("ferien", ferienname)

    # Rahmenbedingungen allein ergeben höchstens Gelb; Rot und Gelb-Rot nur durch Ereignisse.
    stufe = ereignis_stufe(tag, einbrueche, beobachtungen) or ("GELB" if punkte >= GELB_AB else "GRÜN")
    return stufe, ereignis_gruende(tag, einbrueche, beobachtungen, jetzt) + gruende, su


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
        stufe, gruende, _ = bewerte(tag, ferien, regen, einbrueche, beobachtungen, veranstaltungen, seit)
        zeile = f"{SYMBOL[stufe]} {WOCHENTAGE[tag.weekday()]} {tag:%d.%m.}"
        if stufe != "GRÜN":
            zeile += ": " + ", ".join(gruende)
        zeilen.append(zeile)
    zeilen.append("\nRegen ist nur für die nächsten Tage verlässlich, neue Ereignisse ändern die Ampel kurzfristig.")
    return "\n".join(zeilen)


def statusnachricht(stufe, gruende, su):
    return ("Gründe: " + ", ".join(gruende) +
            f"\n\nTipp: Ab {su - timedelta(minutes=30):%H:%M} Licht an oder Zeitschaltuhr, "
            "Fenster und Terrassentüren schließen, auf Fremde in der Siedlung achten.")


def lade_ereignisse(status):
    einbrueche = list(status.get("einbrueche", []))
    beobachtungen = list(status.get("beobachtungen", []))
    for m in lade_manuelle_meldungen():
        if m["art"] == "einbruch":
            einbrueche.append({"datum": m["datum"], "lokal": True, "ort": m["ort"]})
        elif m["art"] == "region":  # Einbruch anderswo in der Stadt, zählt nur für Trend/Region
            einbrueche.append({"datum": m["datum"], "lokal": False, "ort": m["ort"]})
        else:
            beobachtungen.append({"datum": m["datum"], "art": m["ort"]})
    return einbrueche, beobachtungen


def pruefe_wechsel(topic, status, jetzt):
    """Läuft alle 15 Minuten nach alarm.py. Meldet nur, wenn die Ampel durch ein neues
    Ereignis auf Gelb-Rot oder Rot steigt. Speichert die Ereignisstufe in state.json."""
    heute = jetzt.date()
    einbrueche, beobachtungen = lade_ereignisse(status)
    neu = ereignis_stufe(heute, einbrueche, beobachtungen) or "KEINE"
    alt = status.get("ampel_ereignisstufe")
    rang = lambda s: RANG.get(s, 0)
    print(f"Ereignisstufe: vorher {alt}, jetzt {neu}")
    if alt is not None and neu != "KEINE" and rang(neu) > rang(alt):
        gruende = ereignis_gruende(heute, einbrueche, beobachtungen, jetzt)
        su = sonnenuntergang(heute)
        sende_push(topic, f"{SYMBOL[neu]} Einbruch-Ampel jetzt {neu}",
                   statusnachricht(neu, gruende, su), dringend=(neu == "ROT"),
                   tags=["rotating_light"])
        print(f"Wechsel auf {neu} gemeldet")
    status["ampel_ereignisstufe"] = neu
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("Fehler: Das Secret NTFY_TOPIC ist nicht gesetzt.")
    modus = sys.argv[1] if len(sys.argv) > 1 else ""
    jetzt = datetime.now(ZONE)
    heute = jetzt.date()
    status = lade_status()

    if modus == "pruefen":
        pruefe_wechsel(topic, status, jetzt)
        return

    test = modus == ""                         # manueller Start
    sonntag = modus.strip().endswith("* * 0")  # Sonntags-Zeitplan
    if not (test or sonntag):
        print(f"Unbekannter Modus '{modus}', nichts zu tun.")
        return
    # GitHub plant in UTC: sonntags laufen zwei Starts, gesendet wird nur der
    # um ca. 18:30 Uhr deutscher Zeit (funktioniert so in Winter- und Sommerzeit).
    if sonntag and jetzt.hour != 18:
        print(f"Sonntagslauf um {jetzt:%H:%M} Uhr übersprungen, der andere Start sendet.")
        return

    ferien = lade_schulferien(heute, heute + timedelta(days=8))
    regen = lade_regen(heute, 8)
    einbrueche, beobachtungen = lade_ereignisse(status)
    seit = status.get("seit")
    veranstaltungen = lade_heimspiele(heute.year)
    print(f"Im Speicher: {len(einbrueche)} Einbruchsmeldungen, {len(beobachtungen)} Beobachtungen")

    sende_push(topic, ("TEST: " if test else "") + "Wochenausblick Einbruchsrisiko",
               wochenausblick(heute + timedelta(days=1), ferien, regen, einbrueche, beobachtungen,
                              veranstaltungen, seit),
               tags=["calendar"])
    print("Wochenausblick gesendet")

    if test:  # beim manuellen Test zusätzlich den heutigen Stand zeigen
        stufe, gruende, su = bewerte(heute, ferien, regen, einbrueche, beobachtungen,
                                     veranstaltungen, seit, jetzt)
        sende_push(topic, f"TEST: {SYMBOL[stufe]} Einbruch-Ampel heute {stufe}",
                   statusnachricht(stufe, gruende, su), tags=[])
        print(f"Heute: {stufe} – Teststatus gesendet")


if __name__ == "__main__":
    main()
