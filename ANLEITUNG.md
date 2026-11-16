# Morgen-Briefing – Einrichtung (ca. 20 Minuten, einmalig)

Die App läuft kostenlos auf GitHub. Ein Automatik-Job holt dort alle 30 Minuten (ca. 5–22 Uhr) die neuesten Meldungen und Marktdaten, wertet sie aus und stellt sie der App bereit. Ab ca. 5 Uhr schreibt die KI daraus den Morgenbericht. Du brauchst dafür **keinen Mac** und keine Programmierkenntnisse, nur einen Computer mit Browser für die Einrichtung.

---

## Schritt 1 – GitHub-Konto anlegen
1. Auf **github.com** auf „Sign up“ tippen und ein kostenloses Konto anlegen.
2. Merke dir deinen **Benutzernamen** (z. B. `annika-h`). Er steht später in der Adresse deiner App.

## Schritt 2 – Projekt hochladen
1. Oben rechts auf **+** und dann **New repository** klicken.
2. Name: `morgen-briefing` · **Public** auswählen (nötig für das kostenlose Hosting) · „Create repository“.
3. Auf der leeren Seite auf den Link **„uploading an existing file“** klicken.
4. Die ZIP-Datei auf dem Computer entpacken und **den gesamten Inhalt des Ordners `morgen-briefing`** (also `config.json`, `ANLEITUNG.md`, die Ordner `scripts`, `site` und `.github`) in das Browserfenster ziehen. Dann auf **Commit changes** klicken.
5. Prüfe, ob der Ordner `.github` mit hochgeladen wurde. Ordner mit Punkt am Anfang sind auf manchen Computern unsichtbar. Fehlt er:
   **Add file → Create new file**, als Namen `.github/workflows/update.yml` eintippen, den Inhalt der gleichnamigen Datei aus der ZIP hineinkopieren und speichern.

## Schritt 3 – Veröffentlichung einschalten
1. Im Repository: **Settings → Pages**.
2. Bei „Source“ die Option **GitHub Actions** auswählen.
   (Ein roter Fehler-Lauf direkt nach dem Hochladen ist normal: Er lief, bevor Pages eingeschaltet war.)
3. Dann **Actions** (obere Leiste) → links „Briefing aktualisieren“ → **Run workflow** → **Run workflow**.
   Falls GitHub fragt, ob Workflows aktiviert werden sollen: bestätigen.
4. Nach etwa 2–3 Minuten erscheint ein grüner Haken. Die App ist jetzt erreichbar unter:
   **`https://DEIN-BENUTZERNAME.github.io/morgen-briefing/`**

## Schritt 4 – KI-Auswertung einschalten (kostenlos, empfohlen)
Ohne diesen Schritt funktioniert die App auch. Sie wertet die Artikel dann automatisch ohne KI aus: Themen, Kennzahlen, Tendenz und Kernaussagen. Mit KI bekommst du zusätzlich deutsche Zusammenfassungen, Kernpunkte, eine Einordnung je Artikel und einen ausformulierten Morgenbericht.

1. Auf **aistudio.google.com** mit einem Google-Konto anmelden.
2. **Get API key → Create API key** wählen und den Schlüssel kopieren. Eine Kreditkarte ist nicht nötig, solange du keine Abrechnung aktivierst.
3. Zurück zu GitHub: im Repository **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `AI_API_KEY`
   - Secret: den kopierten Schlüssel einfügen und dann **Add secret** klicken.
4. Unter **Actions** den Workflow noch einmal von Hand starten (wie in Schritt 3). Ab jetzt läuft die KI automatisch mit.

> Hinweis: Google begrenzt die kostenlose Nutzung pro Tag. Die App ist darauf ausgelegt: höchstens 40 KI-Aufrufe täglich, die wichtigsten Artikel zuerst, und ein Aufruf bleibt immer für den Morgenbericht reserviert. Ist das Tageskontingent erreicht, läuft die automatische Auswertung ohne KI weiter. Wie es gerade aussieht, siehst du in der App unter **Mehr → Status**.

## Schritt 5 – Auf das iPhone holen
1. Die Adresse aus Schritt 3 in **Safari** öffnen (nicht in Chrome).
2. Unten auf das **Teilen-Symbol** (Quadrat mit Pfeil) tippen, dann **„Zum Home-Bildschirm“** und **Hinzufügen**.
3. Ab jetzt startet „Briefing“ wie eine normale App im Vollbild. Bereits geladene Artikel kannst du auch offline lesen.

## Schritt 6 (optional) – Knopf ↻ für sofortigen Abruf
Ohne diesen Schritt lädt ↻ den neuesten Stand, der höchstens 30 Minuten alt ist. Soll der Knopf **sofort** einen neuen Abruf starten (Dauer ca. 2–3 Minuten):
1. GitHub → Profilbild → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. Name: `Briefing-App` · Ablauf: z. B. 1 Jahr · **Repository access: Only select repositories → morgen-briefing**.
3. Unter **Permissions → Repository permissions → Actions** die Option **Read and write** wählen. Sonst nichts ändern.
4. **Generate token** tippen und den Schlüssel kopieren.
5. In der App unter **Mehr → Sofort aktualisieren** einfügen. Der Schlüssel bleibt nur auf deinem iPhone gespeichert.

---

## Was die App kann
- **Briefing:** Morgenbericht mit Überblick, „Was die Märkte bewegt“, Kursleiste, „Worauf heute zu achten ist“, Zusammenfassung je Bereich mit den passenden Artikeln und „Neu seit dem Bericht“.
- **Nachrichten:** alle Meldungen nach Bereichen (Märkte & Börse · Politik & Wirtschaftspolitik · Notenbanken, Zinsen & Konjunktur · Immobilienbewertung · Immobilienmarkt Deutschland · Immobilien international · Real Estate Asset Management). Sortierung nach „Wichtig“ oder „Neu“, Suche, Ungelesen-Filter. Tippe auf einen Artikel, um die Auswertung aufzuklappen. Mit „Artikel lesen ↗“ öffnet sich der Originalartikel beim Verlag.
- Meldungen, die in mehreren Quellen erscheinen, werden zu einem Eintrag zusammengefasst („+3 Quellen“) und weiter oben einsortiert.
- **Dossiers:** Jeden Morgen ab ca. 4 Uhr schreibt die KI zu jedem Thema einen eigenen Fachartikel mit 1.000 bis über 6.000 Wörtern, je nach Nachrichtenlage. Die Themen: Märkte & Börse · Indizes (Nasdaq & MSCI World) · Politik & Wirtschaftspolitik · Notenbanken, Zinsen & Konjunktur · Immobilienbewertung · Immobilienmarkt Deutschland · Immobilien international · Real Estate Asset Management. Grundlage sind die frei zugänglichen Volltexte der 25 wichtigsten Artikel, weitere Schlagzeilen und Hintergrundwissen aus Wikipedia. Jede Aussage ist mit einer Quellennummer belegt. Tippst du darauf, springt die App zur Quelle. Pro Lauf entstehen zwei Dossiers, nach etwa 1,5 Stunden sind alle fertig. Benötigt den KI-Schlüssel (Schritt 4).
- **Märkte:** DAX, MDAX, Euro Stoxx, FTSE, S&P 500, Nasdaq, Dow, VIX, US-Futures, Nikkei, Hang Seng, Shanghai, EUR/USD, Gold, Brent, US-Zinsen 10 J. und Bitcoin, jeweils mit Monatsverlauf.
- **Merkliste:** Artikel zum späteren Lesen speichern.
- **Mehr:** Schriftgröße, Hell/Dunkel, Bereiche ein- und ausblenden, Status aller Quellen.

## Anpassen
Alle Quellen, Suchbegriffe und Marktwerte stehen in **`config.json`**. Du kannst die Datei direkt auf GitHub bearbeiten (Datei öffnen → Stift-Symbol → „Commit changes“). Die Änderung wirkt beim nächsten Abruf.
- Neue Quelle mit RSS-Adresse: `{"cat": "immo_de", "name": "Name", "url": "https://…/rss"}`
- Neues Suchthema über Google News: `{"cat": "bewertung", "name": "Google News", "q": "Suchbegriffe", "lang": "de"}`
- Uhrzeit des KI-Morgenberichts: `"briefingHourBerlin": 5` unter `ai`.

## Gut zu wissen
- Die App zeigt Schlagzeilen und Anrisse aus den öffentlichen RSS-Angeboten der Verlage sowie eigene Auswertungen. Den vollständigen Text liest du über „Artikel lesen“ beim Verlag. Bei Bezahlangeboten wie FAZ+ oder Handelsblatt brauchst du dort ein Abo. Die KI liest frei zugängliche Artikeltexte, um sie zusammenzufassen. Veröffentlicht wird nur ihre Zusammenfassung, nicht der Originaltext.
- Die Adresse deiner App ist öffentlich, aber nicht verlinkt. Sie enthält keine persönlichen Daten: Merkliste, Gelesen-Status und Token liegen nur auf deinem iPhone.
- Kurse kommen von Yahoo Finance und sind meist ca. 15 Minuten verzögert.
- GitHub kann geplante Läufe bei hoher Auslastung ein paar Minuten verspäten.
- Keine Anlageberatung. Alle Auswertungen ohne Gewähr.
