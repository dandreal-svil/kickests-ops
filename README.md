# KickestOps automated acquisition — DEVELOPMENT

Entry point: `kickestops_auto_acquire.py`.

## Come si avvia

**Nessuno schedule. Nessun cron.**

L'acquisizione parte solo:

1. manualmente da GitHub Actions; oppure
2. quando KickestOps modifica `.kickestops/run-request.json` dopo una richiesta esplicita dell'utente.

Gli altri commit non avviano l'acquisizione.

## Modalità normale: FAST

Usare:

- `mode: auto`
- `gw: auto`
- `horizon_gws: 3`

Il percorso normale è intenzionalmente leggero:

- rileva la GW con poche chiamate Kickest;
- acquisisce schedule e mercato Kickest;
- acquisisce roster preview solo durante LIVE_TURN;
- per Opta aggiorna solo `opta_fixtures.parquet` e costruisce l'orizzonte;
- **non** ricostruisce l'H5 pesante.

## Modalità disponibili

- `auto` — percorso normale/rapido; sceglie PRE-GW o LIVE-TURN.
- `pre-gw` — Kickest + Opta fixture horizon rapido.
- `live-turn` — Kickest point-in-time + roster preview.
- `opta` — solo Opta fixture horizon rapido.
- `opta-full` — rebuild Opta/H5 completo; esplicito, pesante, non usato da `auto`.

## Secret richiesto

Repository secret:

`KICKEST_BEARER`

Il bearer non deve essere salvato nei file del repository.

## Affidabilità

- retry sui download;
- resume dei download parziali nello stesso run;
- timeout del workflow;
- manifest scritto anche in caso di errore;
- artifact di staging caricato a fine run;
- nessun fallback silenzioso;
- nessuna promozione automatica a runtime authority.

## Authority

Gli output restano `DEVELOP/STAGING`.

Passaggio successivo governato:

`STAGING -> CommonDB/boundary materialization -> QA/register -> Common Data Path`
