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

## Reihenfolge beim Scan

1. Der Scanner findet die Ausbrüche (die 200 meistgehandelten Kandidaten mit genauen Kursen).
2. Frische-Regel: nur Ausbrüche, deren Kerze höchstens `fresh_minutes` vor dem Scan geschlossen hat.
3. Vorfilter für alle: Ziel bei 2R unter `min_target_pct` fliegt sofort raus (Stop in der Range-Mitte, das
   lässt sich aus dem Scan rechnen). Diese Ausbrüche kommen trotzdem mit Plan ins Tagebuch.
4. Der Rest wird nach Handelsvolumen in Dollar sortiert (die meistgehandelten zuerst), nicht nach Alphabet.
5. Zahlen-Check in Gruppen von 10, nur so weit, bis `max_checks` saubere Aktien zusammen sind. Eine Gruppe,
   die nicht antwortet, wird einmal in halber Größe wiederholt.
6. Gründliche Prüfung (Entry, Plan, Zeitmaschine) für bis zu `max_checks` Aktien, 8 gleichzeitig.
7. Was wegen des Limits ungeprüft bleibt, steht in der Ausgabe unter „Nicht geprüft“.

## Das Urteil

„Test-Kandidat“ nur, wenn ALLES zutrifft:

1. Ausgelöst (eine 5-Minuten-Kerze hat über dem Range-Hoch geschlossen) und
   der Kurs liegt jetzt noch über dem Range-Hoch.
2. Das Ziel bei 2R liegt mindestens `min_target_pct` (2,2 %) über dem Einstieg.
3. Keine eigenen Quartalszahlen gestern oder heute, und keine angekündigten.
4. Nur bei `scan`: die Ausbruchskerze ist höchstens `fresh_minutes` (10) vor
   dem Scan geschlossen, sonst „raus (zu alt)“.
5. Zeitmaschine: Ø R der echten Fälle der letzten 60 Tage mindestens so groß wie die
   Gebühren-Hürde. (Die 10.000 Simulations-Durchläufe zeigen nur die Chancen auf Ziel und Stop:
   Sie würfeln vergangene Bewegungen neu zusammen, ihr Ø R liegt darum immer nahe 0.)
6. Regel-Check (`--regel`, von `labor.py`): Die Regel muss über alle Aktien der Liste in den
   letzten 60 Tagen nach Gebühren im Plus sein. Ohne Regel-Check gibt es keinen Test-Kandidaten.

Gebühren-Hürde = Gebühren für Kauf und Verkauf in € ÷ Risiko bis zum Stop in €
+ `fee_buffer_r`. Mit 500 € und 1 € pro Order sind das etwa 0,46 R bei 1,1 %
Stop-Abstand, 0,30 R bei 2 % und 0,23 R bei 3 %. Mit mehr Budget sinkt sie
Richtung `fee_buffer_r`.

## Einstellungen

`trader/kandidaten.json` (Vorlage: `config.example.json` in diesem Ordner).
Ohne Datei gelten: 500 € Budget, 1 € pro Kauf und 1 € pro Verkauf,
1 € = 1,15 $, Puffer 0,10 R, Bruchstücke erlaubt (`fractional`, auf 3 Stellen) und
darum keine Preisgrenze (`max_price_usd: null`), Ziel ab 2,2 %, frisch = 10 Minuten,
beim Scan bis zu 100 gründliche Prüfungen pro Lauf (`max_checks`, praktisch jeder frische Ausbruch),
bei der Vorauswahl 8 (`max_checks_vorauswahl`). Den Wechselkurs auf den des Brokers setzen.

## Was er schreibt

- Alles Zwischenergebnis nach `workdir` (Standard `/tmp/trader-kandidaten/<Datum>/`):
  Aktienliste, Scans, Pläne, `ergebnis-*.json`.
- Die Tools ruft er so auf, dass sie nichts ins Repo schreiben: News mit
  `--dry-run`, Entry ohne `--write`, Pläne in den Arbeitsordner.
- Das Tagebuch liegt unter `journal` (Standard `trader/tagebuch.csv`; `trader/`
  ist in `.gitignore`). In einer Cloud-Sitzung ist der Ordner nach dem Ende
  weg: das Tagebuch dann exportieren oder einen festen Pfad setzen.

## Regel-Check (labor.py)

```bash
python3 trader-kandidaten/labor.py alle --out regel.json   # beide Regeln, etwa 30 Sekunden
python3 trader-kandidaten/kandidaten.py scan --regel regel.json
python3 trader-kandidaten/swing.py scan --regel regel.json
```

Rechnet die Regeln über die Aktien in `labor-aktien.txt` (271 große, liquide US-Werte) durch:
Tages-Ausbruch auf 60 Tagen 5-Minuten-Kerzen mit Einstieg 10 Minuten nach dem Ausbruch (so lange
brauchen Scan und Push), Swing auf 2 Jahren Tageskursen. Größe, 2 % Risiko und Gebühren wie im Tool.
„OK“ nur, wenn der Durchschnitt pro Trade nach Gebühren über 0 liegt (ab 300 bzw. 150 Fällen).
Eine einzelne Aktie hat zu wenige Fälle, um Können von Glück zu trennen; die Regel über viele Aktien
schon eher. Die Liste enthält nur heute gelistete Firmen, das schmeichelt den Ergebnissen etwas.

## Momentum-Blöcke (bloecke.py)

```bash
python3 trader-kandidaten/bloecke.py rangliste              # die stärksten Aktien der letzten 6 Monate
python3 trader-kandidaten/bloecke.py tick --from b.json --out b.json   # einmal pro Tag nach Börsenschluss
```

Drei Paper-Konten mit je 500 €: **A** hält die eine stärkste Aktie, **B** die drei stärksten
(Geld gleich verteilt), **C** den Gesamtmarkt (SPY) als Maßstab. „Stärkste“ = größter Anstieg über
126 Handelstage unter den Aktien in `labor-aktien.txt`, die über ihrem 200-Tage-Schnitt schließen.
Ein Block läuft 25 Handelstage (etwa 5 Wochen), gekauft und verkauft wird zum Schlusskurs, dann
kommt der nächste Block mit der neuen Rangliste. Gebühren wie im Tool. Kein Stop: ein schlechter
Block kann 20 % und mehr kosten. In der Rückrechnung (2022 bis 2026) lag diese Art von Rotation meist
vor dem Halten derselben Aktien, aber mit stark schwankenden Ergebnissen und einer Aktienliste, die
diese Strategie begünstigt. Deshalb läuft sie hier als Test nach vorn, nicht als Versprechen.

## Swing (bis 10 Handelstage)

```bash
python3 trader-kandidaten/swing.py scan                      # heutige 20-Tage-Hochs, nach Börsenschluss
python3 trader-kandidaten/swing.py scan --symbols AMD,EOG    # bestimmte Ticker
python3 trader-kandidaten/swing.py update --from offen.json --out neu.json   # offene Swing-Trades fortschreiben
```

- Signal: Schlusskurs über allen Schlusskursen der 20 Tage davor.
- Einstieg: am nächsten Handelstag, sobald der Kurs über dem Hoch des Signaltags handelt (oder zur
  Eröffnung, wenn er darüber öffnet). Kommt der Kurs dort nicht hin, gibt es keinen Trade.
- Stop: Hoch des Signaltags minus 1,5 × ATR(14). Ziel: 2R. Ausstieg: Ziel, Stop oder Schluss des
  10. Handelstags. Eine Kurslücke durch den Stop wird zur Eröffnung abgerechnet.
- Keine Swing-Trades, wenn die Firma vor höchstens 2 Tagen Zahlen gemeldet hat oder die nächsten Zahlen
  in die Haltezeit fallen könnten (letzter 10-Q/10-K bei der SEC + 91 Tage, ±10 Tage).
- Geprüft wird jede Aktie mit ihren Tageskursen der letzten 2 Jahre (ein Trade nach dem anderen).
  Swing-Kandidat ab 8 Fällen und Ø R nach Gebühren mindestens `fee_buffer_r`, und nur, wenn der
  Regel-Check für Swing OK ist.

Für alle Trades gilt: Verlust am Stop höchstens `max_risk_pct` (2 %) des Budgets. Ist der Stop weit
weg, kauft das Tool weniger Stück.

## Markt-Lage und Radar

```bash
python3 trader-kandidaten/markt.py status      # Ampel grün / gelb / rot, SPY, QQQ, VIX, starke und schwache Branchen
python3 trader-kandidaten/markt.py radar       # wichtige 8-K-Meldungen der letzten 3 Stunden + Nasdaq-Handelsstopps
python3 trader-kandidaten/markt.py termine --from termine.json   # Fed, Inflation, Arbeitsmarkt in den nächsten Tagen
```

- Ampel: rot bei VIX ab 25, SPY heute -1 % oder schlechter, oder SPY unter dem 20-Tage-Schnitt und im Minus;
  grün bei SPY über dem 20-Tage-Schnitt, höchstens -0,2 % heute und VIX unter 20; sonst gelb.
- Radar: das SEC-Feed aller neuen 8-K-Meldungen (jede Minute aktuell), nur börsennotierte Firmen und nur
  Punkte, die Kurse bewegen (Zahlen, Deals, Chefwechsel, Insolvenz, Bilanzprobleme ...), dazu die
  Handelsstopps der Nasdaq-Liste mit Grund (z. B. T1: Nachricht kommt gleich). Dazu, wenn erreichbar:
  Pressemitteilungen mit Ticker (Business Wire, PR Newswire, ohne Anwalts-Werbung), Schlagzeilen
  (MarketWatch, Google News), was gerade besprochen wird (StockTwits trending, r/wallstreetbets der
  letzten 24 Stunden) und die Pressemitteilungen der Fed. `--nur-sec` lässt diese Quellen weg. Fällt eine
  Quelle aus, fehlt nur ihr Teil.
- Betroffene Aktien (`betroffen` im Radar): jede Aktie, die eine Meldung nennt, mit einer Tendenz von -3 bis +3
  nach Stichworten (z. B. „to be acquired“ +3, „raises guidance“ +2, „offering“ -2, „bankruptcy“ -3) und den
  8-K-Punkten (Insolvenz, unzuverlässige Bilanz ...). Quartalszahlen ohne Zahlen im Titel bleiben „Richtung
  offen“. Nennt eine Schlagzeile mehrere Firmen, zählt sie für keine. Eine Einschätzung, keine Prognose.
- Jedes Signal bekommt die Ampel (`markt`) und das relative Volumen der Ausbruchskerze (`vol_x`) mit ins
  Tagebuch. Beides ist eine Information, kein Filter, bis das Tagebuch zeigt, dass es einen Unterschied macht.

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
