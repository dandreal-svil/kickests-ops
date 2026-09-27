# KickestOps automated acquisition — DEVELOPMENT

Single operational entrypoint: `kickestops_auto_acquire.py`.

## Trigger policy

This repository has **no scheduled execution**. Acquisition starts only when a user explicitly runs the GitHub Actions workflow `KickestOps Auto Acquisition`.

## Required GitHub secret

Create repository secret `KICKEST_BEARER` containing the current Kickest bearer token.

## Manual run

GitHub → Actions → `KickestOps Auto Acquisition` → `Run workflow`.

Recommended defaults:

- `mode`: `auto`
- `gw`: `auto`
- `horizon_gws`: `3`

The active GW is resolved from the official Kickest schedule when `gw=auto`.

## Modes

- `auto`: detects the current Kickest state and chooses PRE-GW or LIVE-TURN acquisition.
- `pre-gw`: Kickest full-market capture plus Opta/Pannadata frozen-origin horizon and H5 package.
- `live-turn`: Kickest schedule/full-market plus roster-preview Turn state.
- `opta`: Opta/Pannadata acquisition only.

## Authority

Outputs remain `DEVELOP/STAGING`. This automation does not silently promote or register source data as runtime-authoritative.

Governed next boundary:

`STAGING -> CommonDB/boundary materialization -> QA/register -> Common Data Path`
