# Unterstütztes Bewerben lokal einrichten

Die vorhandene Funktion ist absichtlich **kein unbeaufsichtigtes Auto-Submit**.
Sie öffnet die Bewerbung, befüllt erkannte Textfelder, hängt CV und optionales
Cover Letter an und pausiert vor dem Absenden. Den finalen Submit führt immer
der Nutzer selbst aus.

## Benötigte Dateien

- `secrets/applicant_profile.json`: Kontakt- und Bewerbungsstammdaten
- CV als PDF am in `documents.base_dir`/`documents.cv` eingetragenen Pfad
- optional `secrets/profile_cv.txt`: CV als Text für Cover-Letter-Erzeugung
- optional `secrets/cover_letter_samples.txt`: eigene frühere Anschreiben als Stilvorlage
- optional `secrets/supporting_docs_context.txt`: kuratierte Fakten aus
  Zeugnissen und weiteren Nachweisen für das Cover-Letter-Drafting

Reference Letters sind nicht Teil des automatischen Uploads. Sie bleiben als
separate Nachweise verfügbar. Sie dürfen als Hintergrund für das Drafting
dienen, werden aber nur hochgeladen, wenn ihre Fassung final bestätigt ist und
eine konkrete Bewerbung sie verlangt.

## Nationalität und Arbeitserlaubnis

Die Antwort wird pro Stellenstandort aus der im privaten Profil hinterlegten
deutschen Nationalität abgeleitet:

- EU/EWR: arbeitsberechtigt, kein Arbeitgeber-Sponsoring erforderlich
- Schweiz: über die EU/EFTA-Freizügigkeit grundsätzlich berechtigt; die
  schweizerischen Melde- bzw. Bewilligungsformalitäten bleiben zu beachten
- UK und USA: deutsche Nationalität allein belegt dort keine Arbeitserlaubnis;
  ohne einen separaten Status wird Sponsoring angenommen
- alle anderen Länder: keine automatische Ja-Antwort, sondern eine
  länderspezifische Prüfung

Ein separater Aufenthalts- oder Arbeitsstatus muss ausdrücklich ins Profil
aufgenommen werden und wird niemals aus der Nationalität erfunden. Das
LinkedIn-Feld darf leer bleiben und wird erst nach Eintragung eines bestätigten
Profil-Links verwendet.

## Sichere Verwendung

Am bequemsten direkt auf der Website: Eine Rolle öffnen und
`Prepare application` wählen. Nach der Bestätigung öffnet sich ein separates
lokales Bewerbungsfenster. Dieses kann den CV an ein erkanntes Upload-Feld
übertragen, klickt aber niemals auf `Submit`. Nach dem tatsächlichen Absenden
den Status auf der Website auf `applied` setzen.

Die Terminalbefehle unten bleiben nur als Diagnose-/Fallback-Weg verfügbar.

Auf der Website interessante Rollen auf den Status `queued` setzen. Dann:

```bash
./apply_local.sh --queue --no-letter
```

Oder genau eine Rolle anhand ihrer ID öffnen:

```bash
./apply_local.sh --job-id JOB_ID --no-letter
```

`--no-letter` verwendet nur das CV und benötigt keine Claude CLI. Ohne diesen
Schalter versucht das Originalprojekt, ein individuelles Cover Letter über die
Claude CLI zu erzeugen.

Vor jeder Übertragung persönlicher Daten und vor jedem finalen Submit muss die
konkrete Bewerbung kontrolliert und bestätigt werden.
