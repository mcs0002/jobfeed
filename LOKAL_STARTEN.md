# Jobfeed lokal benutzen

Die Python-Umgebung, alle festgeschriebenen Abhaengigkeiten und der von einigen
Quellen benoetigte Chromium-Browser sind bereits eingerichtet. Die lokale
Konfiguration liegt in `.env`; sie ist durch `.gitignore` vom Git-Repository
ausgeschlossen.

## 1. Stellenanzeigen abrufen

```bash
./scan_local.sh
```

Der erste vollstaendige Lauf prueft mehrere hundert oeffentliche Karriereboards
und kann entsprechend lange dauern. Ergebnisse werden lokal in `jobs.db`
gespeichert. Der lokale Starter verwendet die bereits angemeldete Codex CLI
fuer das strukturierte Tagging; ein separater API-Schluessel ist nicht noetig.
Mit `./scan_local.sh --no-tag` kann stattdessen jederzeit der lokale,
deterministische Fallback verwendet werden.
Die optionale Detail-Anreicherung ist im lokalen Starter auf 500 neue Anzeigen
pro Lauf begrenzt; alle Stellenlisten werden trotzdem gespeichert. So fordert
ein grosser Erstlauf nicht zehntausende Detailseiten am Stueck an. Der Wert kann
bei Bedarf mit `--max-enrich N` ueberschrieben werden.

Fuer einen schnellen ersten Test kann nur eine Firma abgerufen werden:

```bash
./scan_local.sh --company Optiver
```

Nuetzliche Varianten:

```bash
./scan_local.sh --dry-run
./scan_local.sh --workers 10
./scan_local.sh --all
./scan_local.sh --company Optiver --company BlackRock
```

## 2. Weboberflaeche starten

In einem zweiten Terminal:

```bash
./start_local.sh
```

Danach [http://127.0.0.1:8000](http://127.0.0.1:8000) im Browser oeffnen. Der
Starter bindet den Server nur an den eigenen Rechner. Beenden mit `Ctrl-C`.

## Codex-Tagging

In `.env` muss `TAG_PROVIDER=codex` stehen. Die CLI verwendet ihre vorhandene
ChatGPT-Anmeldung; den Status kann man mit `codex login status` pruefen. Das
Tagging laeuft nicht-interaktiv, ephemer und ohne Agenten-Tools. Ein konkretes
Modell kann optional mit `TAG_CODEX_MODEL` gesetzt werden; ohne Wert gilt die
CLI-Konfiguration.

```bash
./scan_local.sh
```

Die OpenAI-kompatible API und die Claude CLI bleiben fuer andere Installationen
als alternative Provider erhalten. API-Schluessel gehoeren ausschliesslich in
`.env`; die Datei wird nicht von Git erfasst.

## Spaeter aktualisieren

Lokale Konfigurations- und Datenbankdateien bleiben bei einem Fast-forward
Update erhalten:

```bash
git pull --ff-only
.venv/bin/python -m pip install -r requirements.lock
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  .venv/bin/python -m playwright install chromium
```
