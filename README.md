# Einbruch-Alarm Windberg

Automatisches Frühwarnsystem für die Siedlung in Mönchengladbach-Windberg.

- `alarm.py` prüft alle 15 Minuten die Polizeimeldungen (Mönchengladbach, Viersen) und das Meldeformular.
- `ampel.py` berechnet die Risikoampel: Wochenausblick sonntags ca. 18:30 Uhr, Warnung an Gelb-/Rot-Tagen ca. 12:50 Uhr.
- `meldungen.csv` enthält von Hand nachgetragene Meldungen aus der Nachbarschaft (Datum;Art;Ort).
- `state.json` ist der automatisch gepflegte Speicher (nur Datum und Art, keine Orte aus dem Formular).

Benachrichtigungen kommen über ntfy. Kanal-Name und Formular-Link sind als GitHub-Secrets hinterlegt.
