---
name: trader-kandidaten
description: >-
  Kandidaten-Check. The whole morning routine for a small account in one
  command, with German output: runs the opening-range scan (or the daily
  pre-selection, or tickers you name), keeps breakouts (optionally under a price
  cap) that are at most 10 minutes old, drops stocks with their own earnings yesterday,
  today or coming up, then for each one runs the entry check, the exit plan
  (stop at the range middle, target 2R) and the Time Machine, sizes it to a
  euro budget and turns broker fees into a fee hurdle in R. One verdict per
  stock: "Test-Kandidat" or "raus (Grund)". Keeps a paper-trading journal
  (Tagebuch) that fills in each trade's result after 15:55 and exports CSV for
  Numbers or Excel. Writes nothing inside the repo except the journal. Use
  when the user says "Kandidaten", "Scan", "prüf diese Aktien", "Tagebuch",
  "wie lief der Test", "Test-Kandidat", or wants the pack's checks in one go.
argument-hint: "[scan | kandidaten | check TICKER ... | tagebuch show|update|export]"
---

# trader-kandidaten

Der Kandidaten-Check bündelt Scanner, News, Entry-Check, Exit-Plan und
Zeitmaschine zu einem Befehl und rechnet alles für ein kleines Konto in Euro
nach Gebühren. Er sagt pro Aktie „Test-Kandidat“ oder „raus“, mit Grund.
Ein Test-Kandidat ist für einen Test im Paper-Trading gedacht, nicht zum
Kaufen. Nothing here places an order: an order only ever goes through
`/trader-broker`, after `/trader-risk` passes it and the user types yes.

Tool in this folder (standard library Python 3.8+, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/kandidaten.py" --help
```

## Die Befehle

```bash
python3 trader-kandidaten/kandidaten.py scan                 # ORB-Scan, frische Ausbrüche prüfen
python3 trader-kandidaten/kandidaten.py scan --since 10:00   # nur Kerzen, die nach 10:00 geschlossen haben
python3 trader-kandidaten/kandidaten.py scan --from orb.json # einen gespeicherten Scan nehmen
python3 trader-kandidaten/kandidaten.py kandidaten           # Vorauswahl aus dem Daily-Scan
python3 trader-kandidaten/kandidaten.py check RIVN TTD       # bestimmte Ticker
python3 trader-kandidaten/kandidaten.py scan --journal       # Test-Kandidaten ins Tagebuch
python3 trader-kandidaten/kandidaten.py tagebuch update      # Ergebnisse nach 15:55 eintragen
python3 trader-kandidaten/kandidaten.py tagebuch show        # Übersicht und Summe in Euro
python3 trader-kandidaten/kandidaten.py tagebuch export      # CSV für Numbers / Excel
```

Exit code: 0 when at least one Test-Kandidat came out, 1 otherwise, 2 on an error.

## Das Urteil

„Test-Kandidat“ nur, wenn ALLES zutrifft:

1. Ausgelöst (eine 5-Minuten-Kerze hat über dem Range-Hoch geschlossen) und
   der Kurs liegt jetzt noch über dem Range-Hoch.
2. Das Ziel bei 2R liegt mindestens `min_target_pct` (2,2 %) über dem Einstieg.
3. Keine eigenen Quartalszahlen gestern oder heute, und keine angekündigten.
4. Nur bei `scan`: die Ausbruchskerze ist höchstens `fresh_minutes` (10) vor
   dem Scan geschlossen, sonst „raus (zu alt)“.
5. Zeitmaschine: Ø R der 10.000 Durchläufe UND Ø R der echten Fälle der
   letzten 60 Tage jeweils mindestens so groß wie die Gebühren-Hürde.

Gebühren-Hürde = Gebühren für Kauf und Verkauf in € ÷ Risiko bis zum Stop in €
+ `fee_buffer_r`. Mit 500 € und 1 € pro Order sind das etwa 0,46 R bei 1,1 %
Stop-Abstand, 0,30 R bei 2 % und 0,23 R bei 3 %. Mit mehr Budget sinkt sie
Richtung `fee_buffer_r`.

## Einstellungen

`trader/kandidaten.json` (Vorlage: `config.example.json` in diesem Ordner).
Ohne Datei gelten: 500 € Budget, 1 € pro Kauf und 1 € pro Verkauf,
1 € = 1,15 $, Puffer 0,10 R, Bruchstücke erlaubt (`fractional`, auf 3 Stellen) und
darum keine Preisgrenze (`max_price_usd: null`), Ziel ab 2,2 %, frisch = 10 Minuten,
höchstens 8 Aktien pro Lauf. Den Wechselkurs auf den des Brokers setzen.

## Was er schreibt

- Alles Zwischenergebnis nach `workdir` (Standard `/tmp/trader-kandidaten/<Datum>/`):
  Aktienliste, Scans, Pläne, `ergebnis-*.json`.
- Die Tools ruft er so auf, dass sie nichts ins Repo schreiben: News mit
  `--dry-run`, Entry ohne `--write`, Pläne in den Arbeitsordner.
- Das Tagebuch liegt unter `journal` (Standard `trader/tagebuch.csv`; `trader/`
  ist in `.gitignore`). In einer Cloud-Sitzung ist der Ordner nach dem Ende
  weg: das Tagebuch dann exportieren oder einen festen Pfad setzen.

## Das Tagebuch

`--journal` trägt jeden Test-Kandidaten ein (`--journal-all`: jeden Trade mit
Plan). `tagebuch update` holt nach 15:55 ET die 5-Minuten-Kerzen des Tages
und rechnet nach Regel aus, was passiert wäre: Stop, Ziel, oder Verkauf zum
Schluss der 15:50-Kerze. Berühren Stop und Ziel dieselbe Kerze, zählt der
Stop (die vorsichtige Lesart). Gewinn in € nach Gebühren, R pro Trade, Summe.
`tagebuch export` schreibt eine CSV mit Semikolon und Dezimalkomma, die
Numbers und Excel auf Deutsch direkt öffnen.

## Grenzen, ehrlich

- Die Zahlen-Erkennung liest Schlagzeilen, keinen Terminkalender. „nein“ heißt
  nur: keine Schlagzeile dazu. Sie irrt lieber Richtung „ja“.
- Der Scanner prüft nur die 200 meistgehandelten Kandidaten genau.
- Die Zeitmaschine kennt nur die letzten 60 Tage der Aktie. Ein Test-Kandidat
  ist eine Hypothese, kein Vorteil.
- Ein Stop ist ein Plan, keine Garantie: eine Lücke kann ihn überspringen.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing places an order except `/trader-broker`, and only after
  `/trader-risk` passes AND the user types an explicit yes for that order in
  the chat, AND the brokerage tool is set to "Needs approval" in Claude.
- Day trading loses most people money. A fee hurdle and a journal make the
  test honest; they do not make a trade win.

Nur ganze Aktien beim Broker? Dann `"fractional": false` und eine Preisgrenze
setzen, etwa `"max_price_usd": 190` (ein Drittel des Budgets, also mindestens
3 Aktien), sonst bleibt bei teuren Aktien zu viel Geld liegen.
