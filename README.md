# KickestOps automated acquisition — DEVELOPMENT

Single operational entrypoint: `kickestops_auto_acquire.py`.

## Trigger policy

There is **no scheduled execution**.

Acquisition starts only in one of two explicit ways:

1. GitHub → Actions → `KickestOps Auto Acquisition` → `Run workflow`;
2. when you tell ChatGPT/KickestOps to refresh the data, it updates `.kickestops/run-request.json`; that commit is the trigger.

Ordinary commits do not run acquisition. The workflow listens only to the explicit run-request file.

## Required GitHub secret

Create repository secret `KICKEST_BEARER` containing the current Kickest bearer token.

This is intentionally not stored in the repository.

## Recommended request

- `mode`: `auto`
- `gw`: `auto`
- `horizon_gws`: `3`

The active GW is resolved from the official Kickest schedule when `gw=auto`.

## Modes

- `auto`: detects the current Kickest state and chooses PRE-GW or LIVE-TURN acquisition.
- `pre-gw`: Kickest full-market capture plus Opta/Pannadata frozen-origin horizon and H5 package.
- `live-turn`: Kickest schedule/full-market plus roster-preview Turn state.
- `opta`: Opta/Pannadata acquisition only.

## Outputs and authority

Each run uploads an immutable GitHub Actions artifact.

Outputs remain `DEVELOP/STAGING`. Acquisition does not silently promote or register source data as runtime-authoritative.

Governed next boundary:

`STAGING -> CommonDB/boundary materialization -> QA/register -> Common Data Path`
