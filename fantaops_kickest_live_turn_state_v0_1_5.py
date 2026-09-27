#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FantaOps — Kickest Live Turn State Collector v0.1 (DEVELOPMENT CANDIDATE)

Collects exact per-entry live roster state from the official Kickest/Fantaking API:
  /fantasy-teams/{fantasy_team_id}/matchdays/{matchday_id}/roster/preview
and the official matchday schedule:
  /schedules/{schedule_id}/matchdays/{matchday_id}
plus the complete paginated player market:
  /players-lists/{players_list_id}/matchdays/{matchday_id}/players

Purpose: produce a USER-LOCAL STAGING bundle for LIVE_TURN_STATE v1 plus a
full-market point-in-time Availability signal capture. Normal use requires only --gw: canonical A/B entries
and the Kickest matchday_id are resolved automatically. Canonical roster identity
is reconciled by player_id first; names are diagnostic. Optional --entries and
--matchday-id remain as explicit overrides. It has NO downstream/SETS authority by itself.

Secrets: reads KICKEST_BEARER or KICKEST_BEARER_TOKEN (or hidden prompt), never persists it.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import shutil
import sys
import time
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlparse

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as e:
    raise SystemExit("Missing dependency 'requests'. Run: python -m pip install requests") from e

VERSION = "0.1.5"
IMPLEMENTATION_ID = "fantaops_kickest_live_turn_state_v0_1"
API_ROOT = "https://fantaking-api.dunkest.com/api/v1"
ALLOWED_HOST = "fantaking-api.dunkest.com"
FINAL_MATCH_STATUSES = {"played", "completed", "finished", "final"}
LIVE_MATCH_STATUSES = {"live", "in_progress", "in-progress", "playing"}

# Full-market point-in-time acquisition used by the Availability fresh-signal challenger.
# This remains non-factual Kickest predictive evidence: canonical football identity/facts
# continue to be resolved downstream through the governed CommonDB/Opta path.
PLAYERS_LIST_IDS: Dict[str, int] = {
    "2026-27": 45,
}
COACH_POSITION_ID = 13

MARKET_PLAYER_FIELDS = [
    "season","gw","matchday_id","captured_at_utc","capture_page",
    "backend_id","kickest_id","first_name","last_name","player_name",
    "position_id","position_name","team_id","team_name","team_abbreviation",
    "opponent_id","opponent_name","opponent_abbreviation","round_id","round_number",
    "label","quotation","selectable","active",
    "probability_of_playing","is_injured","started_from_bench",
    "is_on_fire","avg_pts","popularity",
]

AVAILABILITY_SIGNAL_FIELDS = [
    "season","gw","matchday_id","captured_at_utc","capture_page",
    "kickest_id","player_name","team_id","position_id",
    "active","probability_of_playing","is_injured","started_from_bench",
]

# CURRENT LIVE_TURN_STATE v1 identity authority (D-111).
# Keep this guard narrow: only seasons/entries explicitly governed by the contract live here.
CANONICAL_ENTRY_MAP: Dict[str, Dict[str, int]] = {
    "2026-27": {
        "KICK-A-2627": 2656657,
        "KICK-B-2627": 2656733,
    }
}

# Canonical roster identity. Player IDs are the hard identity key; names are
# descriptive diagnostics only. This avoids false mismatches caused by accents,
# transliteration or display-name changes (for example Østigård/Ostigard).
CANONICAL_EXPECTED_PLAYER_IDS: Dict[str, Dict[str, List[int]]] = {
    "2026-27": {
        "KICK-A-2627": [1177, 9914, 834, 919, 4801, 3546, 1038, 3736, 1066, 4309, 3538, 9015, 8910, 840, 870, 3667],
        "KICK-B-2627": [1177, 9914, 834, 3546, 1150, 1107, 9934, 3736, 1066, 9015, 3687, 930, 8910, 3743, 1480, 1054],
    }
}

# Canonical display names retained for human-readable diagnostics and backward
# compatibility with explicit entry configs. They are not the primary identity key.
CANONICAL_EXPECTED_NAMES: Dict[str, Dict[str, List[str]]] = {
    "2026-27": {
        "KICK-A-2627": [
            "Wladimiro Falcone", "Lorenzo Palmisani", "Federico Dimarco",
            "Pierre Kalulu", "Tiago Gabriel", "Enrico Delprato", "Anthony Oyono",
            "Nico Paz", "Morten Frendrup", "Mandela Keita", "Gianluca Busio",
            "Darryl Bakola", "Donyell Malen", "Armand Laurienté",
            "El Bilal Touré", "Cesc Fàbregas",
        ],
        "KICK-B-2627": [
            "Wladimiro Falcone", "Lorenzo Palmisani", "Federico Dimarco",
            "Enrico Delprato", "Leo Østigård", "Mergim Vojvoda",
            "Gabriele Bracaglia", "Nico Paz", "Morten Frendrup", "Darryl Bakola",
            "Jurgen Ekkelenkamp", "Nemanja Matić", "Donyell Malen",
            "Francisco Conceição", "Keinan Davis", "Domenico Tedesco",
        ],
    }
}

# Known current-season anchor. Matchday IDs are resolved from GW and verified
# against the official schedule payload data.number before use. If the arithmetic
# candidate ever stops being contiguous, a bounded API scan finds the correct ID.
MATCHDAY_ANCHORS: Dict[str, Dict[str, int]] = {
    "2026-27": {"gw": 3, "matchday_id": 1431},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_api_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme != "https" or p.netloc != ALLOWED_HOST:
        raise RuntimeError(f"Unexpected/non-allowed API URL: {url}")
    return url


def bearer() -> str:
    token = os.getenv("KICKEST_BEARER", "").strip() or os.getenv("KICKEST_BEARER_TOKEN", "").strip()
    if not token:
        token = getpass.getpass("Kickest Bearer token: ").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError("Missing Kickest Bearer token")
    return token


def session(token: str) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8))
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"FantaOps/{IMPLEMENTATION_ID}-{VERSION}",
    })
    return s


def get_json(s: requests.Session, url: str, timeout: Tuple[int, int] = (10, 60)) -> Tuple[Dict[str, Any], int, str]:
    safe_api_url(url)
    captured = utc_now()
    r = s.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {url}: {r.text[:500]}")
    try:
        obj = r.json()
    except Exception as e:
        raise RuntimeError(f"Non-JSON response from {url}") from e
    if not isinstance(obj, dict):
        raise RuntimeError(f"Unexpected payload root from {url}: {type(obj).__name__}")
    return obj, r.status_code, captured


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def norm_name(first: Any, last: Any) -> str:
    return " ".join(x for x in [str(first or "").strip(), str(last or "").strip()] if x).strip()


def canonical_entry_map_for(cfg: Dict[str, Any]) -> Dict[str, int]:
    season = str(cfg.get("season") or "").strip()
    return CANONICAL_ENTRY_MAP.get(season, {})


def validate_canonical_entry_map(cfg: Dict[str, Any]) -> Dict[str, Any]:
    expected = canonical_entry_map_for(cfg)
    configured: Dict[str, int] = {}
    for entry in cfg.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("entry_id") or not entry.get("fantasy_team_id"):
            continue
        configured[str(entry["entry_id"])] = int(entry["fantasy_team_id"])

    relevant_expected = {entry_id: team_id for entry_id, team_id in expected.items() if entry_id in configured}
    mismatches = {
        entry_id: {"expected": team_id, "actual": configured.get(entry_id)}
        for entry_id, team_id in relevant_expected.items()
        if configured.get(entry_id) != team_id
    }
    return {
        "season": str(cfg.get("season") or "").strip() or None,
        "expected": relevant_expected,
        "actual": {entry_id: configured.get(entry_id) for entry_id in relevant_expected},
        "pass": not mismatches,
        "mismatches": mismatches,
    }


def default_entries_config(season: str, gw: int) -> Dict[str, Any]:
    mapping = CANONICAL_ENTRY_MAP.get(season)
    if not mapping:
        raise RuntimeError(
            f"No built-in canonical entry mapping for season {season}. "
            "Pass --entries with an explicit config file."
        )
    names = CANONICAL_EXPECTED_NAMES.get(season, {})
    player_ids = CANONICAL_EXPECTED_PLAYER_IDS.get(season, {})
    return {
        "season": season,
        "gw": int(gw),
        "entries": [
            {
                "entry_id": entry_id,
                "fantasy_team_id": int(team_id),
                "expected_player_ids": list(player_ids.get(entry_id, [])),
                "expected_names": list(names.get(entry_id, [])),
            }
            for entry_id, team_id in mapping.items()
        ],
    }


def load_entries(path: Path | None, season: str, gw: int) -> Dict[str, Any]:
    if path is None:
        obj = default_entries_config(season, gw)
    else:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or not isinstance(obj.get("entries"), list):
            raise RuntimeError("Entries JSON must contain an 'entries' list")
        # Command-line season/GW are runtime authority; the file may omit them.
        obj.setdefault("season", season)
        obj.setdefault("gw", int(gw))

        # For governed current-season entries, inject canonical player IDs as the
        # hard roster-identity authority unless the explicit file already provides
        # its own ID list. Display names remain diagnostic/backward-compatible.
        canonical_ids = CANONICAL_EXPECTED_PLAYER_IDS.get(str(obj.get("season") or season), {})
        for entry in obj.get("entries") or []:
            entry_id = str(entry.get("entry_id") or "")
            if entry_id in canonical_ids and not entry.get("expected_player_ids"):
                entry["expected_player_ids"] = list(canonical_ids[entry_id])

    if not isinstance(obj, dict) or not isinstance(obj.get("entries"), list):
        raise RuntimeError("Entries configuration must contain an 'entries' list")
    missing = [e.get("entry_id", "<unknown>") for e in obj["entries"] if not e.get("fantasy_team_id")]
    if missing:
        raise RuntimeError(
            "Missing fantasy_team_id for: " + ", ".join(missing) +
            ". Fill the backend IDs before running."
        )
    ids = [int(e["fantasy_team_id"]) for e in obj["entries"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate fantasy_team_id values in entries configuration")

    mapping_check = validate_canonical_entry_map(obj)
    if mapping_check["expected"] and not mapping_check["pass"]:
        details = "; ".join(
            f"{entry_id}: expected {vals['expected']}, got {vals['actual']}"
            for entry_id, vals in mapping_check["mismatches"].items()
        )
        raise RuntimeError(
            "Canonical entry/backend mapping mismatch for "
            f"season {mapping_check['season']}: {details}. "
            "Fix the explicit entries override; IDs are not auto-swapped."
        )
    return obj


def schedule_matchday_number(payload: Dict[str, Any]) -> int | None:
    d = payload.get("data")
    if not isinstance(d, dict):
        return None
    try:
        return int(d.get("number"))
    except Exception:
        return None


def fetch_schedule_candidate(
    s: requests.Session, schedule_id: int, matchday_id: int
) -> Tuple[Dict[str, Any] | None, int | None, str | None, int | None]:
    url = f"{API_ROOT}/schedules/{schedule_id}/matchdays/{matchday_id}"
    safe_api_url(url)
    captured = utc_now()
    r = s.get(url, timeout=(10, 60))
    if r.status_code != 200:
        return None, r.status_code, captured, None
    try:
        obj = r.json()
    except Exception:
        return None, r.status_code, captured, None
    if not isinstance(obj, dict):
        return None, r.status_code, captured, None
    return obj, r.status_code, captured, schedule_matchday_number(obj)


def resolve_matchday_id(
    s: requests.Session, season: str, gw: int, schedule_id: int, override: int | None
) -> Tuple[int, Dict[str, Any], int, str, str]:
    if override is not None:
        url = f"{API_ROOT}/schedules/{schedule_id}/matchdays/{override}"
        payload, http_status, captured = get_json(s, url)
        actual_gw = schedule_matchday_number(payload)
        if actual_gw != int(gw):
            raise RuntimeError(
                f"--matchday-id {override} resolves to schedule GW {actual_gw}, not requested GW {gw}."
            )
        return int(override), payload, http_status, captured, "explicit_override"

    anchor = MATCHDAY_ANCHORS.get(season)
    if not anchor:
        raise RuntimeError(
            f"No matchday discovery anchor for season {season}. Pass --matchday-id explicitly."
        )

    candidate = int(anchor["matchday_id"]) + (int(gw) - int(anchor["gw"]))
    payload, http_status, captured, actual_gw = fetch_schedule_candidate(s, schedule_id, candidate)
    if payload is not None and actual_gw == int(gw):
        return candidate, payload, int(http_status), str(captured), "anchor_verified"

    # Bounded fallback around the arithmetic candidate. The candidate is checked first;
    # then nearest IDs are tried symmetrically, so ordinary runs remain one API call.
    for delta in range(1, 41):
        for mid in (candidate - delta, candidate + delta):
            if mid <= 0:
                continue
            payload, http_status, captured, actual_gw = fetch_schedule_candidate(s, schedule_id, mid)
            if payload is not None and actual_gw == int(gw):
                return mid, payload, int(http_status), str(captured), f"fallback_scan_delta_{delta}"

    raise RuntimeError(
        f"Unable to auto-resolve matchday_id for season {season} GW{gw:02d} "
        f"around anchor {anchor['matchday_id']}. Pass --matchday-id explicitly."
    )


def schedule_rows(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    d = payload.get("data")
    if not isinstance(d, dict):
        raise RuntimeError("Schedule payload missing data object")
    rounds = d.get("rounds") or []
    rows: List[Dict[str, Any]] = []
    summary: Dict[int, Dict[str, Any]] = {}
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        rn = rnd.get("number")
        try:
            rn_int = int(rn)
        except Exception:
            continue
        matches = rnd.get("matches") or []
        statuses: List[str] = []
        starts: List[str] = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            status = str(m.get("status") or "").strip().lower()
            statuses.append(status)
            if m.get("started_at"):
                starts.append(str(m.get("started_at")))
            home = m.get("home_team") if isinstance(m.get("home_team"), dict) else {}
            away = m.get("away_team") if isinstance(m.get("away_team"), dict) else {}
            rows.append({
                "round_number": rn_int,
                "round_id": rnd.get("id"),
                "match_id": m.get("id"),
                "status": m.get("status"),
                "started_at": m.get("started_at"),
                "home_team_id": home.get("id"),
                "home_team_name": home.get("name"),
                "home_score": home.get("score"),
                "away_team_id": away.get("id"),
                "away_team_name": away.get("name"),
                "away_score": away.get("score"),
            })
        if matches and statuses and all(s in FINAL_MATCH_STATUSES for s in statuses):
            state = "COMPLETE"
        elif any(s in LIVE_MATCH_STATUSES for s in statuses):
            state = "LIVE"
        elif any(s in FINAL_MATCH_STATUSES for s in statuses):
            state = "IN_PROGRESS"
        else:
            state = "UPCOMING"
        summary[rn_int] = {
            "round_number": rn_int,
            "round_id": rnd.get("id"),
            "state": state,
            "match_count": len(matches),
            "statuses": statuses,
            "earliest_start": min(starts) if starts else None,
            "latest_start": max(starts) if starts else None,
        }
    return rows, summary


def derive_boundary(round_summary: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    nums = sorted(round_summary)
    complete = [n for n in nums if round_summary[n]["state"] == "COMPLETE"]
    last_completed = max(complete) if complete else 0
    next_round = next((n for n in nums if n > last_completed), None)
    # A valid Turn boundary exists only if every prior numbered round is COMPLETE.
    prefix_ok = all(round_summary[n]["state"] == "COMPLETE" for n in nums if n <= last_completed)
    boundary_ready = prefix_ok and last_completed > 0 and next_round is not None
    return {
        "last_completed_round": last_completed if last_completed > 0 else None,
        "next_round": next_round,
        "boundary_ready": boundary_ready,
        "rounds": [round_summary[n] for n in nums],
    }


def resolve_players_list_id(season: str, override: Optional[int]) -> int:
    if override is not None:
        return int(override)
    if season in PLAYERS_LIST_IDS:
        return int(PLAYERS_LIST_IDS[season])
    raise RuntimeError(
        f"No built-in players_list_id for season {season}. "
        "Pass --players-list-id explicitly."
    )


def extract_market_entities(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("players", "entities", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    for key in ("players", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    raise RuntimeError("Unable to find player entities in players-list payload")


def get_paginated_market_entities(
    s: requests.Session, first_url: str
) -> Tuple[List[Tuple[Dict[str, Any], str, int]], List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch the complete players-list collection following official pagination.

    Every entity retains the timestamp of the exact API page on which it was observed.
    This is important for point-in-time Availability freshness checks.
    """
    records: List[Tuple[Dict[str, Any], str, int]] = []
    pages: List[Dict[str, Any]] = []
    seen_urls = set()
    url: Optional[str] = first_url
    expected_total: Optional[int] = None
    expected_last_page: Optional[int] = None

    while url:
        url = urljoin(API_ROOT + "/", str(url))
        if url in seen_urls:
            raise RuntimeError(f"Players-list pagination loop detected: {url}")
        seen_urls.add(url)
        payload, http_status, captured = get_json(s, url, timeout=(10, 90))
        entities = extract_market_entities(payload)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
        page_no = int(meta.get("current_page") or len(pages) + 1)

        if expected_total is None and meta.get("total") is not None:
            expected_total = int(meta["total"])
        if expected_last_page is None and meta.get("last_page") is not None:
            expected_last_page = int(meta["last_page"])

        pages.append({
            "page": page_no,
            "captured_at_utc": captured,
            "request_url": url,
            "http_status": http_status,
            "entity_count": len(entities),
            "response": payload,
        })
        records.extend((dict(entity), captured, page_no) for entity in entities)

        nxt = links.get("next")
        url = str(nxt) if nxt else None
        if url:
            time.sleep(0.05)

    if expected_last_page is not None and len(pages) != expected_last_page:
        raise RuntimeError(
            f"Incomplete players-list pagination: pages={len(pages)} expected={expected_last_page}"
        )
    if expected_total is not None and len(records) != expected_total:
        raise RuntimeError(
            f"Incomplete players-list pagination: entities={len(records)} expected={expected_total}"
        )

    ids = [record[0].get("id") for record in records]
    duplicate_ids = len(ids) - len(set(ids))
    if duplicate_ids:
        raise RuntimeError(f"Players-list pagination contains {duplicate_ids} duplicate entity ids")

    qa = {
        "pages_fetched": len(pages),
        "expected_last_page": expected_last_page,
        "entities_fetched": len(records),
        "expected_total": expected_total,
        "duplicate_entity_ids": duplicate_ids,
        "complete": (expected_last_page is None or len(pages) == expected_last_page)
                    and (expected_total is None or len(records) == expected_total),
    }
    return records, pages, qa


def normalize_market_player(
    entity: Mapping[str, Any], season: str, gw: int, matchday_id: int,
    captured_at_utc: str, capture_page: int,
) -> Dict[str, Any]:
    pos = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    team = entity.get("team") if isinstance(entity.get("team"), dict) else {}
    opp = entity.get("opponent") if isinstance(entity.get("opponent"), dict) else {}
    rnd = entity.get("round") if isinstance(entity.get("round"), dict) else {}
    eid = entity.get("id")
    return {
        "season": season,
        "gw": int(gw),
        "matchday_id": int(matchday_id),
        "captured_at_utc": captured_at_utc,
        "capture_page": int(capture_page),
        "backend_id": eid,
        "kickest_id": eid,
        "first_name": entity.get("first_name"),
        "last_name": entity.get("last_name"),
        "player_name": norm_name(entity.get("first_name"), entity.get("last_name")),
        "position_id": pos.get("id"),
        "position_name": pos.get("name"),
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "team_abbreviation": team.get("abbreviation"),
        "opponent_id": opp.get("id"),
        "opponent_name": opp.get("name"),
        "opponent_abbreviation": opp.get("abbreviation"),
        "round_id": rnd.get("id"),
        "round_number": rnd.get("number"),
        "label": entity.get("label"),
        "quotation": entity.get("quotation"),
        "selectable": True,
        "active": entity.get("active"),
        # Fresh Availability signals: retain source values exactly as observed.
        "probability_of_playing": entity.get("probability_of_playing"),
        "is_injured": entity.get("is_injured"),
        "started_from_bench": entity.get("started_from_bench"),
        "is_on_fire": entity.get("is_on_fire"),
        "avg_pts": entity.get("avg_pts"),
        "popularity": entity.get("popularity"),
    }


def market_signal_qa(rows: List[Dict[str, Any]], pagination: Dict[str, Any]) -> Dict[str, Any]:
    ids = [r.get("kickest_id") for r in rows]
    duplicate_ids = len(ids) - len(set(ids))

    def is_true(v: Any) -> bool:
        if v is True:
            return True
        return str(v).strip().lower() in {"1", "true", "t", "yes", "y"}

    hard_out = 0
    bench = 0
    pplay_values: List[float] = []
    for r in rows:
        p = r.get("probability_of_playing")
        try:
            pf = float(p) if p is not None and str(p).strip() != "" else None
        except Exception:
            pf = None
        if pf is not None:
            pplay_values.append(pf)
        if is_true(r.get("is_injured")) and pf is not None and abs(pf) <= 1e-12:
            hard_out += 1
        if is_true(r.get("started_from_bench")):
            bench += 1

    hard_pass = bool(rows) and duplicate_ids == 0 and bool(pagination.get("complete"))
    return {
        "status": "PASS_STAGING" if hard_pass else "FAIL_STAGING",
        "hard_pass": hard_pass,
        "players": len(rows),
        "duplicate_player_ids": duplicate_ids,
        "pagination": pagination,
        "missing_probability_of_playing": sum(r.get("probability_of_playing") is None for r in rows),
        "missing_is_injured": sum(r.get("is_injured") is None for r in rows),
        "missing_started_from_bench": sum(r.get("started_from_bench") is None for r in rows),
        "injured_and_pplay_zero": hard_out,
        "started_from_bench_true": bench,
        "pplay_min": min(pplay_values) if pplay_values else None,
        "pplay_max": max(pplay_values) if pplay_values else None,
        "signal_semantics": {
            "probability_of_playing": "continuous fresh participation signal; primary challenger input",
            "is_injured": "strong fresh unavailability modifier; not an unconditional hard-zero by itself",
            "started_from_bench": "fresh Start/minutes risk signal; not equivalent to DNP",
        },
    }


def normalize_roster(
    entry: Dict[str, Any], payload: Dict[str, Any], capture_utc: str, round_summary: Dict[int, Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    d = payload.get("data")
    if not isinstance(d, dict):
        raise RuntimeError(f"Roster payload for {entry['entry_id']} missing data object")
    players = d.get("players") or []
    if not isinstance(players, list):
        raise RuntimeError(f"Roster players for {entry['entry_id']} is not a list")

    team_row = {
        "entry_id": entry["entry_id"],
        "fantasy_team_id": int(entry["fantasy_team_id"]),
        "fantasy_team_name": d.get("name") or d.get("fantasy_team_name"),
        "capture_utc": capture_utc,
        "formation_id": d.get("formation_id"),
        "roster_pts": d.get("pts"),
        "plus": d.get("plus"),
        "trades": d.get("trades"),
        "position": d.get("position"),
        "wildcard_used": d.get("wildcard_used"),
        "max_num_trades_per_matchday": d.get("max_num_trades_per_matchday"),
        "defense_modifier": d.get("defense_modifier"),
        "player_count": len(players),
    }

    prows: List[Dict[str, Any]] = []
    srows: List[Dict[str, Any]] = []
    for p in players:
        if not isinstance(p, dict):
            continue
        pos = p.get("position") if isinstance(p.get("position"), dict) else {}
        club = p.get("team") if isinstance(p.get("team"), dict) else {}
        opp = p.get("opponent") if isinstance(p.get("opponent"), dict) else {}
        rnd = p.get("round") if isinstance(p.get("round"), dict) else {}
        rn = rnd.get("number")
        try:
            rn_int = int(rn) if rn is not None else None
        except Exception:
            rn_int = None
        rstate = round_summary.get(rn_int, {}).get("state") if rn_int is not None else None
        match_live = p.get("match_live") is True
        court_position = p.get("court_position")
        position_name = pos.get("name")
        # Empirical API guard: historical roster/preview may expose match_played=false
        # even when the matchday is complete. Finality therefore comes from the
        # official round state, while court_position represents the current XI/bench slot.
        if position_name == "Coach":
            score_state = "COACH_NON_TURN"
        elif match_live:
            score_state = "LIVE_PARTIAL"
        elif rn_int is not None and rstate == "COMPLETE":
            if isinstance(court_position, int) and court_position <= 11:
                score_state = "LOCKED_CURRENT_XI"
            elif isinstance(court_position, int):
                score_state = "ROUND_CLOSED_BENCH"
            else:
                score_state = "ROUND_CLOSED_UNKNOWN_SLOT"
        else:
            score_state = "UNLOCKED"

        row = {
            "entry_id": entry["entry_id"],
            "fantasy_team_id": int(entry["fantasy_team_id"]),
            "capture_utc": capture_utc,
            "player_id": p.get("id"),
            "player_name": norm_name(p.get("first_name"), p.get("last_name")),
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "position_id": pos.get("id"),
            "position_name": pos.get("name"),
            "team_id": club.get("id"),
            "team_name": club.get("name"),
            "opponent_id": opp.get("id"),
            "opponent_name": opp.get("name"),
            "quotation_api": p.get("quotation"),
            "pts_api": p.get("pts"),
            "active": p.get("active"),
            "court_position": p.get("court_position"),
            "is_captain": p.get("is_captain"),
            "captain_multiplier": p.get("captain_multiplier"),
            "match_played": p.get("match_played"),
            "round_id": rnd.get("id"),
            "round_number": rn_int,
            "round_state": rstate,
            "match_live": p.get("match_live"),
            "has_scored": p.get("has_scored"),
            "score_state": score_state,
            "stats_items_count": len(p.get("stats_items") or []) if isinstance(p.get("stats_items"), list) else 0,
        }
        prows.append(row)
        stats = p.get("stats_items") or []
        if isinstance(stats, list):
            for st in stats:
                if not isinstance(st, dict):
                    continue
                srows.append({
                    "entry_id": entry["entry_id"],
                    "fantasy_team_id": int(entry["fantasy_team_id"]),
                    "capture_utc": capture_utc,
                    "player_id": p.get("id"),
                    "stat_id": st.get("id"),
                    "stat_name": st.get("name"),
                    "value": st.get("value"),
                    "fantasy_pts": st.get("fantasy_pts"),
                })
    return team_row, prows, srows


def norm_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "").casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ø", "o").replace("đ", "d").replace("ł", "l").replace("ß", "ss")
    return " ".join(s.split())


def qa(entries_cfg: Dict[str, Any], team_rows: List[Dict[str, Any]], player_rows: List[Dict[str, Any]], boundary: Dict[str, Any]) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    expected_by_entry = {e["entry_id"]: e for e in entries_cfg["entries"]}
    actual_entries = {r["entry_id"] for r in team_rows}
    exp_entries = set(expected_by_entry)
    checks.append({"check": "all_entries_fetched", "pass": actual_entries == exp_entries, "expected": sorted(exp_entries), "actual": sorted(actual_entries)})

    mapping_check = validate_canonical_entry_map(entries_cfg)
    if mapping_check["expected"]:
        checks.append({
            "check": "canonical_entry_backend_mapping",
            "pass": mapping_check["pass"],
            "season": mapping_check["season"],
            "expected": mapping_check["expected"],
            "actual": mapping_check["actual"],
            "mismatches": mapping_check["mismatches"],
        })

    for entry_id, cfg in expected_by_entry.items():
        trow = next((r for r in team_rows if r["entry_id"] == entry_id), None)
        erows = [r for r in player_rows if r["entry_id"] == entry_id]
        ids = [r.get("player_id") for r in erows]
        checks.append({"check": f"{entry_id}:roster_count_16", "pass": len(erows) == 16, "actual": len(erows)})
        checks.append({"check": f"{entry_id}:no_duplicate_player_id", "pass": len(ids) == len(set(ids)), "actual": len(ids) - len(set(ids))})
        roles = Counter(str(r.get("position_name") or "") for r in erows)
        # Role names are API labels; this is a hard check only when standard labels are present.
        standard = {"Goalkeeper": 2, "Defender": 5, "Midfielder": 5, "Attacker": 3, "Coach": 1}
        if any(k in roles for k in standard):
            checks.append({"check": f"{entry_id}:role_shape", "pass": all(roles.get(k, 0) == v for k, v in standard.items()), "actual": dict(roles)})
        expected_player_ids = cfg.get("expected_player_ids") or []
        if expected_player_ids:
            exp_ids = {int(x) for x in expected_player_ids}
            act_ids = {int(r.get("player_id")) for r in erows if r.get("player_id") is not None}
            checks.append({
                "check": f"{entry_id}:expected_roster_identity_by_player_id",
                "pass": exp_ids == act_ids,
                "missing_player_ids": sorted(exp_ids - act_ids),
                "unexpected_player_ids": sorted(act_ids - exp_ids),
            })

        # Names are deliberately diagnostic when player IDs are available. They
        # must not fail hard QA because the official API may transliterate accents.
        expected_names = cfg.get("expected_names") or []
        if expected_names:
            exp = {norm_key(x) for x in expected_names}
            act = {norm_key(r.get("player_name") or "") for r in erows}
            name_match = exp == act
            name_check = {
                "check": f"{entry_id}:expected_roster_display_names",
                "pass": True if expected_player_ids else name_match,
                "diagnostic_match": name_match,
                "identity_authority": "player_id" if expected_player_ids else "normalized_name_fallback",
                "missing": sorted(exp - act),
                "unexpected": sorted(act - exp),
            }
            checks.append(name_check)
        if trow is not None:
            checks.append({"check": f"{entry_id}:team_id_echo", "pass": int(trow["fantasy_team_id"]) == int(cfg["fantasy_team_id"])})

    score_states = Counter(str(r.get("score_state") or "") for r in player_rows)
    checks.append({"check": "no_live_partial_marked_locked", "pass": not any(r.get("match_live") is True and r.get("score_state") == "LOCKED_CURRENT_XI" for r in player_rows)})
    checks.append({"check": "boundary_semantics_present", "pass": "last_completed_round" in boundary and "next_round" in boundary})
    hard_pass = all(c.get("pass") is True for c in checks)
    return {
        "status": "PASS_STAGING" if hard_pass else "FAIL_STAGING",
        "hard_pass": hard_pass,
        "checks": checks,
        "score_state_counts": dict(score_states),
        "boundary": boundary,
        "consumer_authority": "NONE — DEVELOPMENT CANDIDATE / STAGING ONLY",
    }


def zip_tree(root: Path, out_zip: Path) -> None:
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file() and p != out_zip:
                z.write(p, p.relative_to(root))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gw", type=int, required=True)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--matchday-id", type=int, help="Optional explicit override; normally auto-resolved from --gw")
    ap.add_argument("--entries", type=Path, help="Optional override JSON; built-in canonical A/B mapping is used by default")
    ap.add_argument("--schedule-id", type=int, default=45)
    ap.add_argument("--players-list-id", type=int, help="Optional full-market players-list override; season default is used when omitted")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    if args.gw < 1 or args.gw > 38:
        raise RuntimeError("--gw must be between 1 and 38")
    cfg = load_entries(args.entries, args.season, args.gw)
    players_list_id = resolve_players_list_id(args.season, args.players_list_id)
    token = bearer()
    s = session(token)
    matchday_id, schedule_payload, schedule_http, schedule_cap, matchday_resolution = resolve_matchday_id(
        s, args.season, args.gw, args.schedule_id, args.matchday_id
    )
    ts = compact_utc()
    out = args.out or Path(f"kickest_live_turn_state_G{args.gw:02d}_{ts}_v0_1")
    raw = out / "raw"
    norm = out / "normalized"
    raw.mkdir(parents=True, exist_ok=True)
    norm.mkdir(parents=True, exist_ok=True)

    schedule_url = f"{API_ROOT}/schedules/{args.schedule_id}/matchdays/{matchday_id}"
    write_json(raw / f"G{args.gw:02d}_matchday_{matchday_id}_schedule.json", {
        "captured_at_utc": schedule_cap,
        "request_url": schedule_url,
        "http_status": schedule_http,
        "response": schedule_payload,
    })
    srows, rsummary = schedule_rows(schedule_payload)
    boundary = derive_boundary(rsummary)

    # Full-market players-list snapshot. This is deliberately separate from
    # roster/preview: roster state remains the D-111 Turn source, while the
    # full market snapshot supplies point-in-time Availability challenger signals.
    players_url = f"{API_ROOT}/players-lists/{players_list_id}/matchdays/{matchday_id}/players"
    market_records, market_pages, market_pagination = get_paginated_market_entities(s, players_url)
    for page in market_pages:
        page_no = int(page["page"])
        write_json(raw / f"G{args.gw:02d}_players_list_page_{page_no:03d}.json", page)
    write_json(raw / f"G{args.gw:02d}_players_list_index.json", {
        "request_url": players_url,
        "players_list_id": players_list_id,
        "matchday_id": matchday_id,
        "snapshot_started_at_utc": market_pages[0]["captured_at_utc"] if market_pages else None,
        "snapshot_completed_at_utc": market_pages[-1]["captured_at_utc"] if market_pages else None,
        "pagination": market_pagination,
        "raw_pages": [f"G{args.gw:02d}_players_list_page_{int(p['page']):03d}.json" for p in market_pages],
    })

    market_player_rows: List[Dict[str, Any]] = []
    market_coach_count = 0
    for entity, entity_cap, page_no in market_records:
        pos = entity.get("position") if isinstance(entity.get("position"), dict) else {}
        try:
            position_id = int(pos.get("id")) if pos.get("id") is not None else None
        except (TypeError, ValueError):
            position_id = None
        if position_id == COACH_POSITION_ID:
            market_coach_count += 1
            continue
        market_player_rows.append(normalize_market_player(
            entity, args.season, args.gw, matchday_id, entity_cap, page_no
        ))

    write_csv(norm / "market_players.csv", market_player_rows, MARKET_PLAYER_FIELDS)
    write_csv(norm / "availability_signals.csv", market_player_rows, AVAILABILITY_SIGNAL_FIELDS)
    market_q = market_signal_qa(market_player_rows, market_pagination)
    market_q["coaches_excluded"] = market_coach_count

    team_rows: List[Dict[str, Any]] = []
    player_rows: List[Dict[str, Any]] = []
    stat_rows: List[Dict[str, Any]] = []
    source_records: List[Dict[str, Any]] = []

    for entry in cfg["entries"]:
        tid = int(entry["fantasy_team_id"])
        url = f"{API_ROOT}/fantasy-teams/{tid}/matchdays/{matchday_id}/roster/preview"
        payload, http_status, cap = get_json(s, url)
        raw_path = raw / f"{entry['entry_id']}_fantasy_team_{tid}_roster_preview.json"
        write_json(raw_path, {
            "captured_at_utc": cap,
            "entry_id": entry["entry_id"],
            "fantasy_team_id": tid,
            "request_url": url,
            "http_status": http_status,
            "response": payload,
        })
        tr, pr, sr = normalize_roster(entry, payload, cap, rsummary)
        team_rows.append(tr); player_rows.extend(pr); stat_rows.extend(sr)
        source_records.append({"entry_id": entry["entry_id"], "fantasy_team_id": tid, "url": url, "captured_at_utc": cap, "raw_file": str(raw_path.relative_to(out))})
        time.sleep(0.1)

    write_csv(norm / "schedule_matches.csv", srows, [
        "round_number","round_id","match_id","status","started_at","home_team_id","home_team_name","home_score","away_team_id","away_team_name","away_score"
    ])
    write_csv(norm / "roster_teams.csv", team_rows, [
        "entry_id","fantasy_team_id","fantasy_team_name","capture_utc","formation_id","roster_pts","plus","trades","position","wildcard_used","max_num_trades_per_matchday","defense_modifier","player_count"
    ])
    write_csv(norm / "roster_players.csv", player_rows, [
        "entry_id","fantasy_team_id","capture_utc","player_id","player_name","first_name","last_name","position_id","position_name","team_id","team_name","opponent_id","opponent_name","quotation_api","pts_api","active","court_position","is_captain","captain_multiplier","match_played","round_id","round_number","round_state","match_live","has_scored","score_state","stats_items_count"
    ])
    write_csv(norm / "roster_player_stats.csv", stat_rows, [
        "entry_id","fantasy_team_id","capture_utc","player_id","stat_id","stat_name","value","fantasy_pts"
    ])
    write_json(out / "TURN_STATE.json", {
        "status": "STAGING / DEVELOPMENT CANDIDATE",
        "gw": args.gw,
        "matchday_id": matchday_id,
        "matchday_resolution": matchday_resolution,
        "captured_at_utc": utc_now(),
        "boundary": boundary,
        "rule": "Only completed prior Kickest rounds/Turns may be treated as locked state; LIVE_PARTIAL never becomes locked.",
        "consumer_authority": "NONE",
    })
    q = qa(cfg, team_rows, player_rows, boundary)
    q["market_capture"] = market_q
    q["hard_pass"] = bool(q.get("hard_pass")) and bool(market_q.get("hard_pass"))
    q["status"] = "PASS_STAGING" if q["hard_pass"] else "FAIL_STAGING"
    write_json(out / "QA.json", q)

    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name not in {"MANIFEST.json"}:
            files.append({"path": str(p.relative_to(out)), "sha256": sha256_file(p), "bytes": p.stat().st_size})
    manifest = {
        "artifact_id": f"KickestLiveTurnState_G{args.gw:02d}_{ts}_v0_1_STAGING",
        "artifact_version": "0.1",
        "producer": {"implementation_id": IMPLEMENTATION_ID, "implementation_version": VERSION},
        "game": "Kickest",
        "season": cfg.get("season", args.season),
        "gw": args.gw,
        "matchday_id": matchday_id,
        "matchday_resolution": matchday_resolution,
        "entries_config_source": "explicit_file" if args.entries is not None else "built_in_canonical",
        "generation_timestamp_utc": utc_now(),
        "data_class": {"raw": "RAW", "normalized_intended": "CANONICAL"},
        "lifecycle": "STAGING",
        "source_authority": "Official Kickest/Fantaking authenticated API",
        "schedule_endpoint": schedule_url,
        "players_list_id": players_list_id,
        "players_list_endpoint": players_url,
        "market_capture": {
            "players": len(market_player_rows),
            "coaches_excluded": market_coach_count,
            "pagination": market_pagination,
            "signal_fields": ["probability_of_playing", "is_injured", "started_from_bench"],
            "normalized_players_file": "normalized/market_players.csv",
            "availability_signals_file": "normalized/availability_signals.csv",
        },
        "roster_endpoint_template": f"{API_ROOT}/fantasy-teams/{{fantasy_team_id}}/matchdays/{{matchday_id}}/roster/preview",
        "entries": [{"entry_id": e["entry_id"], "fantasy_team_id": int(e["fantasy_team_id"])} for e in cfg["entries"]],
        "source_records": source_records,
        "qa_status": q["status"],
        "downstream_authority": "NONE — requires separate LIVE_TURN_STATE contract validation/promotion before SETS consumption",
        "secret_persistence": "Bearer token never written",
        "files": files,
    }
    write_json(out / "MANIFEST.json", manifest)

    if not args.no_zip:
        out_zip = out.with_suffix(".zip")
        zip_tree(out, out_zip)
        print(f"ZIP: {out_zip}")
    print(f"Output: {out}")
    print(f"Matchday ID: {matchday_id} ({matchday_resolution})")
    print(f"QA: {q['status']}")
    print(f"Full-market players: {len(market_player_rows)}; pages: {market_pagination.get('pages_fetched')}")
    print(f"Fresh signals — injured+pplay0: {market_q.get('injured_and_pplay_zero')}; from_bench: {market_q.get('started_from_bench_true')}")
    print(json.dumps(boundary, ensure_ascii=False, indent=2))
    return 0 if q["hard_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
