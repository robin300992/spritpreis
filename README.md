# Spritpreis-Agent Super E5

Läuft vollautomatisch und kostenlos auf GitHub. Alle 15 Minuten durchläuft der Agent einen festen Kreislauf:

1. **Wahrnehmen:** Super-E5-Preise aller offenen Tankstellen im Umkreis von 15 km über die Tankerkönig-API abrufen, 14 Tage Verlauf speichern.
2. **Bewerten:** Frühere Empfehlungen mit dem tatsächlichen Preisverlauf vergleichen. War „Jetzt tanken“ richtig, oder wurde es danach noch deutlich günstiger? Hat sich „Warten“ gelohnt?
3. **Lernen:** Einmal täglich (ab 3 Uhr) passt er drei Regeln in festen Grenzen an: die Günstig-Schwelle, die Mindestersparnis fürs Warten und eine Korrektur der Prognose. Jede Änderung steht mit Begründung im Dashboard.
4. **Entscheiden:** In einer günstigen Phase oder bei einem neuen 7-Tage-Tiefstpreis entscheidet er, ob sich eine Nachricht lohnt und welche Tankstelle sich trotz Umweg rechnet. Das übernimmt Claude, wenn ein API-Key hinterlegt ist, sonst feste Regeln.
5. **Handeln:** Push-Nachricht aufs iPhone über die App ntfy.

Harte Grenzen gelten immer, auch wenn Claude entscheidet: Ruhezeit 22–7 Uhr, höchstens 2 Nachrichten pro Tag, mindestens 6 Stunden Abstand. Eine frühere Nachricht ist nur möglich, wenn der Preis seit der letzten Nachricht um mindestens 2 Cent gefallen ist.

## Einrichtung

### Grundsystem (falls noch nicht erledigt)

1. Tankerkönig-API-Key holen: https://creativecommons.tankerkoenig.de
2. Öffentliches Repository auf github.com anlegen und den Inhalt dieses Ordners hochladen, inkl. `.github/` und `config.json`.
3. Unter Settings › Secrets and variables › Actions diese Secrets anlegen: `TK_API_KEY`, `HOME_LAT`, `HOME_LNG`.
4. Unter Settings › Actions › General › Workflow permissions „Read and write permissions“ wählen.
5. Unter Settings › Pages „Deploy from a branch“, `main`, `/docs` einstellen.

### Push-Nachrichten aufs iPhone

6. App **ntfy** aus dem App Store installieren und Mitteilungen erlauben.
7. Einen schwer erratbaren Kanalnamen ausdenken, z. B. `sprit-robin-7f3k9q2x`. Wer den Namen kennt, kann mitlesen.
8. In der App auf „+“ tippen und den Kanalnamen abonnieren. Server bleibt ntfy.sh.
9. Auf GitHub das Secret `NTFY_TOPIC` mit genau diesem Namen anlegen.

### Claude als Entscheider (optional)

10. Auf https://console.anthropic.com ein Konto anlegen, Guthaben aufladen und einen API-Key erzeugen. Die API wird separat vom Claude-Abo nach Verbrauch abgerechnet. Der Agent fragt Claude nur, wenn ein Auslöser vorliegt, also nur wenige Male am Tag.
11. Auf GitHub das Secret `ANTHROPIC_API_KEY` anlegen.

Ohne diesen Key funktioniert alles genauso, nur entscheiden dann feste Regeln statt Claude.

### Start

12. Actions › Spritpreise › Run workflow. Das Dashboard läuft unter `https://<benutzername>.github.io/<repo>/`. Ein Tipp auf eine Push-Nachricht öffnet es direkt.

Eigene Prognosen gibt es ab 2 vollständigen Tagen. Ab dem dritten Tag beginnt das Lernen; die ersten Anpassungen erfolgen, sobald genug Empfehlungen bewertet sind (mind. 10 bzw. 6).

## Anpassen: `config.json`

| Feld | Bedeutung |
|---|---|
| `tank_liter` | typische Tankmenge in Litern, Basis für die Umweg-Rechnung |
| `verbrauch_l_100km` | Verbrauch im Stadtverkehr |
| `umwegfaktor` | Straßenweg im Verhältnis zur Luftlinie (1,3 passt für Berlin) |
| `zeitwert_eur_h` | Was dir eine Stunde Zeit wert ist (0 = nur Spritkosten zählen) |
| `max_umweg_km` | Tankstellen mit größerem Umweg werden ignoriert |
| `ruhezeit_von` / `ruhezeit_bis` | Keine Nachrichten in diesem Zeitraum (Stunden) |
| `max_push_pro_tag`, `push_abstand_h` | Grenzen gegen zu viele Nachrichten |
| `llm_modell` | Claude-Modell für Entscheidungen |
| `dashboard_url` | Nur nötig, wenn die Adresse vom Standard abweicht |

Die Umweg-Rechnung geht von der Fahrt ab der Wohnadresse aus und vergleicht mit der nächstgelegenen offenen Tankstelle.

## Dateien

- `scripts/update.py`: Preise abrufen, Verlauf führen, Dashboard-Daten schreiben
- `scripts/agent.py`: Bewerten, Lernen, Entscheiden, Benachrichtigen
- `state/agent.json`: Gedächtnis des Agenten (gelernte Regeln, bewertete Empfehlungen, Protokoll)
- `docs/`: Dashboard

## Hinweise

- API-Keys, Koordinaten und Kanalname liegen nur in den GitHub-Secrets.
- GitHub startet geplante Läufe manchmal einige Minuten verspätet.
- Daten: Tankerkönig-API / MTS-K, Lizenz CC BY 4.0.
