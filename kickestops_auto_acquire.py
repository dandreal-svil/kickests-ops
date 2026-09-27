#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KickestOps Unified Acquisition v0.2.0-dev.

DEVELOP/STAGING acquisition only. One entrypoint for:
- official Kickest/Fantaking schedule, full market and saved-entry roster preview;
- public Pannadata Opta latest factual parquet acquisition;
- frozen-origin fixture horizon extraction;
- pre-GW H5 packaging of prior factual match-addressable data.

No CommonDB promotion/registration and no downstream runtime authority.
"""
from __future__ import annotations

import argparse, csv, hashlib, json, os, re, shutil, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VERSION = "0.3.0-dev"
API_ROOT = "https://fantaking-api.dunkest.com/api/v1"
ALLOWED_API_HOST = "fantaking-api.dunkest.com"
SCHEDULE_ID = 45
PLAYERS_LIST_ID = {"2026-27": 45}
MATCHDAY_ANCHOR = {"2026-27": (3, 1431)}
ENTRY_MAP = {"2026-27": {"KICK-A-2627": 2656657, "KICK-B-2627": 2656733}}
EXPECTED_PLAYER_IDS = {
    "2026-27": {
        "KICK-A-2627": [1177,9914,834,919,4801,3546,1038,3736,1066,4309,3538,9015,8910,840,870,3667],
        "KICK-B-2627": [1177,9914,834,3546,1150,1107,9934,3736,1066,9015,3687,930,8910,3743,1480,1054],
    }
}
FINAL = {"played","completed","finished","final","ft","fulltime","complete","ended"}
LIVE = {"live","in_progress","in-progress","playing"}
PANNA_REPO = "peteowen1/pannadata"
PANNA_TAG = "opta-latest"
PANNA_FULL_ASSETS = (
    "opta_fixtures.parquet", "opta_player_stats.parquet", "opta_lineups.parquet",
    "opta_match_stats.parquet", "opta_shots.parquet", "opta_shot_events.parquet", "opta_events.parquet",
)

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""): h.update(b)
    return h.hexdigest()

def write_json(path: Path, obj: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def write_csv(path: Path, rows: list[dict[str,Any]], fields: list[str] | None=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields=[]
        for r in rows:
            for k in r:
                if k not in fields: fields.append(k)
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows)

def token() -> str:
    t=(os.getenv("KICKEST_BEARER") or os.getenv("KICKEST_BEARER_TOKEN") or "").strip()
    if t.lower().startswith("bearer "): t=t[7:].strip()
    if not t: raise RuntimeError("Missing KICKEST_BEARER GitHub secret/environment variable")
    return t

def api_json(url: str, bearer: str, retries: int=4) -> tuple[dict[str,Any],str]:
    if not url.startswith(f"https://{ALLOWED_API_HOST}/"): raise RuntimeError(f"Blocked API URL: {url}")
    last=None
    for attempt in range(retries+1):
        req=urllib.request.Request(url,headers={"Authorization":f"Bearer {bearer}","Accept":"application/json","User-Agent":f"KickestOps/{VERSION}"})
        try:
            with urllib.request.urlopen(req,timeout=90) as r:
                if getattr(r,"status",200)!=200: raise RuntimeError(f"HTTP {r.status}")
                obj=json.load(r)
                if not isinstance(obj,dict): raise RuntimeError("Unexpected non-object JSON")
                return obj, now()
        except Exception as e:
            last=e
            if attempt>=retries: break
            time.sleep(min(8,0.8*(2**attempt)))
    raise RuntimeError(f"API request failed: {url}: {last}")

def schedule_number(obj: dict[str,Any]) -> int|None:
    try: return int((obj.get("data") or {}).get("number"))
    except Exception: return None

def resolve_matchday(bearer: str, season: str, gw: int) -> tuple[int,dict[str,Any],str]:
    if season not in MATCHDAY_ANCHOR: raise RuntimeError(f"No matchday anchor for {season}")
    agw, amid=MATCHDAY_ANCHOR[season]; candidate=amid+(gw-agw)
    # The season anchor is validated on every hit. Keep fallback bounded so a
    # bad/missing GW fails quickly instead of spraying the API with requests.
    for delta in [0]+[x for d in range(1,6) for x in (-d,d)]:
        mid=candidate+delta
        if mid<=0: continue
        try: obj,cap=api_json(f"{API_ROOT}/schedules/{SCHEDULE_ID}/matchdays/{mid}",bearer,1)
        except Exception: continue
        if schedule_number(obj)==gw: return mid,obj,cap
    raise RuntimeError(f"Cannot resolve matchday_id for {season} GW{gw}")

def schedule_rows(obj: dict[str,Any]) -> tuple[list[dict[str,Any]],dict[int,dict[str,Any]]]:
    rows=[]; summary={}
    for rnd in (obj.get("data") or {}).get("rounds") or []:
        if not isinstance(rnd,dict): continue
        try: rn=int(rnd.get("number"))
        except Exception: continue
        statuses=[]
        for m in rnd.get("matches") or []:
            if not isinstance(m,dict): continue
            st=str(m.get("status") or "").strip().lower(); statuses.append(st)
            h=m.get("home_team") or {}; a=m.get("away_team") or {}
            rows.append({"round_number":rn,"round_id":rnd.get("id"),"match_id":m.get("id"),"status":m.get("status"),"started_at":m.get("started_at"),"home_team_id":h.get("id"),"home_team_name":h.get("name"),"home_score":h.get("score"),"away_team_id":a.get("id"),"away_team_name":a.get("name"),"away_score":a.get("score")})
        state="COMPLETE" if statuses and all(s in FINAL for s in statuses) else "LIVE" if any(s in LIVE for s in statuses) else "IN_PROGRESS" if any(s in FINAL for s in statuses) else "UPCOMING"
        summary[rn]={"round_number":rn,"round_id":rnd.get("id"),"state":state,"statuses":statuses,"match_count":len(statuses)}
    return rows,summary

def boundary(summary: dict[int,dict[str,Any]]) -> dict[str,Any]:
    nums=sorted(summary); complete=[n for n in nums if summary[n]["state"]=="COMPLETE"]
    last=max(complete) if complete else None; nxt=next((n for n in nums if last is not None and n>last), nums[0] if nums else None)
    return {"last_completed_round":last,"next_round":nxt,"boundary_ready":bool(last and nxt),"rounds":[summary[n] for n in nums]}

def market_entities(bearer: str, players_list_id: int, matchday_id: int) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    url=f"{API_ROOT}/players-lists/{players_list_id}/matchdays/{matchday_id}/players"; out=[]; pages=0; expected=None
    while url:
        obj,cap=api_json(url,bearer); data=obj.get("data")
        if isinstance(data,dict): ents=next((data.get(k) for k in ("players","entities","items","data") if isinstance(data.get(k),list)),None)
        else: ents=data if isinstance(data,list) else None
        if ents is None: raise RuntimeError("Cannot find market player entities")
        pages+=1; meta=obj.get("meta") or {}; links=obj.get("links") or {}; expected=expected if expected is not None else meta.get("total")
        for e in ents:
            if not isinstance(e,dict): continue
            pos=e.get("position") or {}; team=e.get("team") or {}; opp=e.get("opponent") or {}; rnd=e.get("round") or {}
            out.append({"captured_at_utc":cap,"capture_page":pages,"kickest_id":e.get("id"),"first_name":e.get("first_name"),"last_name":e.get("last_name"),"player_name":" ".join(x for x in [str(e.get("first_name") or "").strip(),str(e.get("last_name") or "").strip()] if x),"position_id":pos.get("id"),"position_name":pos.get("name"),"team_id":team.get("id"),"team_name":team.get("name"),"opponent_id":opp.get("id"),"opponent_name":opp.get("name"),"round_id":rnd.get("id"),"round_number":rnd.get("number"),"quotation":e.get("quotation"),"active":e.get("active"),"probability_of_playing":e.get("probability_of_playing"),"is_injured":e.get("is_injured"),"started_from_bench":e.get("started_from_bench"),"avg_pts":e.get("avg_pts"),"popularity":e.get("popularity")})
        nxt=links.get("next"); url=str(nxt) if nxt else None
        if url and url.startswith("/"): url=API_ROOT.rstrip("/")+url
    ids=[x.get("kickest_id") for x in out]
    if len(ids)!=len(set(ids)): raise RuntimeError("Duplicate market player IDs")
    if expected is not None and len(out)!=int(expected): raise RuntimeError(f"Incomplete market: {len(out)} != {expected}")
    return out,{"pages_fetched":pages,"entities_fetched":len(out),"expected_total":expected,"complete":True}

def roster(bearer: str, entry_id: str, team_id: int, matchday_id: int, rsummary: dict[int,dict[str,Any]]) -> tuple[dict[str,Any],list[dict[str,Any]]]:
    obj,cap=api_json(f"{API_ROOT}/fantasy-teams/{team_id}/matchdays/{matchday_id}/roster/preview",bearer)
    d=obj.get("data") or {}; players=d.get("players") or []
    t={"entry_id":entry_id,"fantasy_team_id":team_id,"fantasy_team_name":d.get("name") or d.get("fantasy_team_name"),"capture_utc":cap,"roster_pts":d.get("pts"),"trades":d.get("trades"),"position":d.get("position"),"wildcard_used":d.get("wildcard_used"),"player_count":len(players)}
    rows=[]
    for p in players:
        pos=p.get("position") or {}; club=p.get("team") or {}; opp=p.get("opponent") or {}; rnd=p.get("round") or {}
        try: rn=int(rnd.get("number"))
        except Exception: rn=None
        rs=rsummary.get(rn,{}).get("state") if rn is not None else None; cp=p.get("court_position")
        score="COACH_NON_TURN" if pos.get("name")=="Coach" else "LIVE_PARTIAL" if p.get("match_live") is True else "LOCKED_CURRENT_XI" if rs=="COMPLETE" and isinstance(cp,int) and cp<=11 else "ROUND_CLOSED_BENCH" if rs=="COMPLETE" and isinstance(cp,int) else "UNLOCKED"
        rows.append({"entry_id":entry_id,"fantasy_team_id":team_id,"capture_utc":cap,"player_id":p.get("id"),"player_name":" ".join(x for x in [str(p.get("first_name") or "").strip(),str(p.get("last_name") or "").strip()] if x),"position_id":pos.get("id"),"position_name":pos.get("name"),"team_id":club.get("id"),"team_name":club.get("name"),"opponent_id":opp.get("id"),"opponent_name":opp.get("name"),"quotation_api":p.get("quotation"),"pts_api":p.get("pts"),"active":p.get("active"),"court_position":cp,"is_captain":p.get("is_captain"),"captain_multiplier":p.get("captain_multiplier"),"round_number":rn,"round_state":rs,"match_live":p.get("match_live"),"score_state":score})
    return t,rows

def acquire_kickest(out: Path, season: str, gw: int, horizon: int, include_roster: bool) -> dict[str,Any]:
    b=token(); mid,sobj,scap=resolve_matchday(b,season,gw); raw=out/"raw"; normdir=out/"normalized"; raw.mkdir(parents=True,exist_ok=True); normdir.mkdir(parents=True,exist_ok=True)
    write_json(raw/f"GW{gw:02d}_schedule.json",{"captured_at_utc":scap,"response":sobj})
    srows,rsummary=schedule_rows(sobj); write_csv(normdir/"schedule_matches.csv",srows); bnd=boundary(rsummary)
    horizon_rows=[]
    for tg in range(gw,min(38,gw+horizon-1)+1):
        tmid,tobj,tcap=(mid,sobj,scap) if tg==gw else resolve_matchday(b,season,tg)
        tr,_=schedule_rows(tobj)
        for r in tr: horizon_rows.append({"origin_gw":gw,"target_gw":tg,"matchday_id":tmid,"captured_at_utc":tcap,**r})
    write_csv(normdir/"schedule_horizon_matches.csv",horizon_rows)
    plist=PLAYERS_LIST_ID.get(season)
    if not plist: raise RuntimeError(f"No players-list id for {season}")
    market,pqa=market_entities(b,plist,mid); coaches=[r for r in market if int(r.get("position_id") or -1)==13]; players=[r for r in market if int(r.get("position_id") or -1)!=13]
    write_csv(normdir/"market_players.csv",players); write_csv(normdir/"market_coaches.csv",coaches); write_csv(normdir/"availability_signals.csv",players,["captured_at_utc","capture_page","kickest_id","player_name","team_id","position_id","active","probability_of_playing","is_injured","started_from_bench"])
    teams=[]; roster_rows=[]; qchecks=[]
    if include_roster:
        for eid,tid in ENTRY_MAP.get(season,{}).items():
            t,rr=roster(b,eid,tid,mid,rsummary); teams.append(t); roster_rows+=rr
            actual={int(x["player_id"]) for x in rr if x.get("player_id") is not None}; expected=set(EXPECTED_PLAYER_IDS.get(season,{}).get(eid,[]))
            qchecks.append({"check":f"{eid}:roster_count_16","pass":len(rr)==16,"actual":len(rr)})
            if expected: qchecks.append({"check":f"{eid}:roster_identity","pass":actual==expected,"missing":sorted(expected-actual),"unexpected":sorted(actual-expected)})
        write_csv(normdir/"roster_teams.csv",teams); write_csv(normdir/"roster_players.csv",roster_rows)
    else:
        qchecks.append({"check":"roster_preview_required_only_for_live_turn","pass":True})
    hard=bool(players) and pqa["complete"] and all(c["pass"] for c in qchecks)
    qa={"status":"PASS_STAGING" if hard else "FAIL_STAGING","hard_pass":hard,"checks":qchecks,"market":pqa,"boundary":bnd}
    write_json(out/"QA.json",qa); write_json(out/"TURN_STATE.json",{"status":"STAGING / DEVELOPMENT","season":season,"gw":gw,"matchday_id":mid,"captured_at_utc":now(),"boundary":bnd,"consumer_authority":"NONE"})
    if not hard: raise RuntimeError("Kickest staging QA failed")
    return {"matchday_id":mid,"players":len(players),"coaches":len(coaches),"roster_preview_captured":include_roster,"boundary":bnd}

def gh_json(url: str) -> Any:
    headers={"Accept":"application/vnd.github+json","User-Agent":f"KickestOps/{VERSION}","X-GitHub-Api-Version":"2022-11-28"}
    gh_token=(os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    if gh_token:
        headers["Authorization"]=f"Bearer {gh_token}"
    req=urllib.request.Request(url,headers=headers)
    with urllib.request.urlopen(req,timeout=120) as r:
        return json.load(r)

def release_assets() -> dict[str,dict[str,Any]]:
    rel=gh_json(f"https://api.github.com/repos/{PANNA_REPO}/releases/tags/{PANNA_TAG}")
    return {a["name"]:a for a in rel.get("assets",[]) if a.get("name") and a.get("browser_download_url")}

def asset_url(name: str) -> str:
    return f"https://github.com/{PANNA_REPO}/releases/download/{PANNA_TAG}/{name}"

def download(url: str, path: Path, expected_size: int|None=None, retries: int=3):
    """Download with retry and in-run resume; never discard a valid final file."""
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file() and (not expected_size or path.stat().st_size==expected_size):
        return
    tmp=path.with_suffix(path.suffix+".part")
    last=None
    for attempt in range(retries+1):
        try:
            have=tmp.stat().st_size if tmp.exists() else 0
            headers={"User-Agent":f"KickestOps/{VERSION}"}
            if have:
                headers["Range"]=f"bytes={have}-"
            req=urllib.request.Request(url,headers=headers)
            with urllib.request.urlopen(req,timeout=180) as r:
                status=getattr(r,"status",200)
                append=have>0 and status==206
                mode="ab" if append else "wb"
                with tmp.open(mode) as fh:
                    shutil.copyfileobj(r,fh,1024*1024)
            if expected_size and tmp.stat().st_size!=expected_size:
                raise RuntimeError(f"Size mismatch for {path.name}: {tmp.stat().st_size} != {expected_size}")
            tmp.replace(path)
            return
        except Exception as exc:
            last=exc
            if attempt>=retries:
                break
            time.sleep(min(5,0.75*(2**attempt)))
    raise RuntimeError(f"Download failed for {path.name}: {last}")

def norm(v: Any) -> str: return re.sub(r"[^a-z0-9]+","",str(v).lower())

def detect(cols: Iterable[str], names: Iterable[str]) -> str|None:
    by={norm(c):c for c in cols}
    for n in names:
        if norm(n) in by: return by[norm(n)]
    return None

def season_match(v: Any, season: str) -> bool:
    """Match the season encodings accepted by the original v1.0.6 bridge."""
    if v is None:
        return False
    t=re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})",str(season).strip())
    if not t:
        return norm(v)==norm(season)
    ts=int(t.group(1)); rhs=t.group(2)
    te=int(rhs) if len(rhs)==4 else (ts//100)*100+int(rhs)
    if te<=ts:
        te+=100

    s=str(v).strip()
    m=re.search(r"(\d{4})\D+(\d{4}|\d{2})",s)
    if m:
        vs=int(m.group(1)); vr=m.group(2)
        ve=int(vr) if len(vr)==4 else (vs//100)*100+int(vr)
        if ve<=vs:
            ve+=100
        return (vs,ve)==(ts,te)
    try:
        numeric=int(float(s))
    except (TypeError,ValueError):
        return norm(s)==norm(season)
    return numeric==te


def parse_match_dates(series):
    """Robust Pannadata fixture-date parser from the reviewed horizon collector."""
    import datetime as _dt
    import pandas as pd

    def unwrap(v):
        if v is None:
            return None
        as_py=getattr(v,"as_py",None)
        if callable(as_py):
            try:
                v=as_py()
            except Exception:
                pass
        if isinstance(v,(bytes,bytearray,memoryview)):
            try:
                return bytes(v).decode("utf-8")
            except Exception:
                return bytes(v).decode("utf-8",errors="replace")
        return v

    def one(v):
        v=unwrap(v)
        if v is None:
            return pd.NaT
        try:
            if pd.isna(v):
                return pd.NaT
        except Exception:
            pass
        if isinstance(v,pd.Timestamp):
            return v.tz_localize("UTC") if v.tzinfo is None else v.tz_convert("UTC")
        if isinstance(v,_dt.datetime):
            x=pd.Timestamp(v)
            return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")
        if isinstance(v,_dt.date):
            return pd.Timestamp(v,tz="UTC")
        if isinstance(v,(int,float)) and not isinstance(v,bool):
            n=int(v); a=abs(n)
            if 19000101<=a<=22001231:
                try:
                    return pd.Timestamp(_dt.datetime.strptime(str(n),"%Y%m%d"),tz="UTC")
                except Exception:
                    pass
            try:
                if a>=10**17: return pd.to_datetime(n,unit="ns",utc=True)
                if a>=10**14: return pd.to_datetime(n,unit="us",utc=True)
                if a>=10**11: return pd.to_datetime(n,unit="ms",utc=True)
                if a>=10**8: return pd.to_datetime(n,unit="s",utc=True)
                if 10000<=a<=100000:
                    return pd.Timestamp("1970-01-01",tz="UTC")+pd.to_timedelta(n,unit="D")
            except Exception:
                return pd.NaT
        s=str(v).strip()
        if not s:
            return pd.NaT
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}Z",s):
            s=s[:10]+"T00:00:00Z"
        if re.fullmatch(r"[+-]?\d+(?:\.0+)?",s):
            try:
                return one(int(float(s)))
            except Exception:
                pass
        iso=s.replace("Z","+00:00") if s.endswith(("Z","z")) else s
        try:
            x=pd.Timestamp(_dt.datetime.fromisoformat(iso))
            return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")
        except Exception:
            pass
        try:
            return pd.Timestamp(_dt.date.fromisoformat(s),tz="UTC")
        except Exception:
            pass
        try:
            return pd.to_datetime(s,utc=True,errors="coerce",format="mixed")
        except TypeError:
            return pd.to_datetime(s,utc=True,errors="coerce")
        except Exception:
            return pd.NaT

    return pd.to_datetime(series.map(one),utc=True,errors="coerce")

def opta_fixture_context(fixtures: Path, season: str, target_gw: int):
    import pandas as pd
    df=pd.read_parquet(fixtures); cols=df.columns
    sc=detect(cols,["season","season_name","season_end_year"]); lc=detect(cols,["competition","competition_name","league"]); mc=detect(cols,["match_id","opta_match_id","fixture_id"]); hc=detect(cols,["home_team_id","homeTeamId","home_team"]); ac=detect(cols,["away_team_id","awayTeamId","away_team"]); gc=detect(cols,["gw","gameweek","matchday","round_number"]); dc=detect(cols,["match_date","date","kickoff","start_time"]); stc=detect(cols,["status","match_status"])
    if not all([sc,lc,mc,hc,ac]): raise RuntimeError(f"Opta fixtures missing required columns; have={list(cols)}")
    df=df[df[sc].map(lambda x:season_match(x,season)) & df[lc].map(lambda x:norm(x) in {norm("Serie_A"),norm("ITA")})].copy()
    if df.empty:
        source=pd.read_parquet(fixtures,columns=list(dict.fromkeys([sc,lc])))
        season_samples=source[sc].dropna().astype(str).drop_duplicates().tail(30).tolist()
        league_samples=source[lc].dropna().astype(str).drop_duplicates().head(30).tolist()
        raise RuntimeError(
            f"No Serie A fixture rows for requested season={season}; "
            f"season_column={sc} samples={season_samples}; "
            f"league_column={lc} samples={league_samples}"
        )
    if gc:
        parsed=pd.to_numeric(df[gc],errors="coerce"); df["_gw"]=parsed
    else:
        if not dc: raise RuntimeError("No GW and no date column for chronology derivation")
        df["_dt"]=parse_match_dates(df[dc])
        if df["_dt"].isna().any(): raise RuntimeError("Unparseable Opta fixture dates")
        df=df.sort_values(["_dt",mc],kind="stable").reset_index(drop=True); teams=set(df[hc].astype(str))|set(df[ac].astype(str)); m=len(teams)//2
        if len(teams)!=20 or m!=10: raise RuntimeError(f"Expected 20 teams/10 matches, got {len(teams)}/{m}")
        gws=[]
        for i in range(0,len(df),m):
            block=df.iloc[i:i+m]; app=list(block[hc].astype(str))+list(block[ac].astype(str))
            if len(block)!=m or len(set(app))!=20 or any(app.count(x)!=1 for x in set(app)): raise RuntimeError("Chronology-derived GW violates one-match-per-team invariant")
            gws += [i//m+1]*len(block)
        df["_gw"]=gws
    target=df[df["_gw"]==target_gw].copy(); prior=df[df["_gw"]<target_gw].copy()
    if len(target)!=10: raise RuntimeError(f"Target GW{target_gw} has {len(target)} fixtures, expected 10")
    if stc and target[stc].astype(str).map(lambda x:norm(x) in {norm(s) for s in FINAL}).any(): raise RuntimeError("Target Opta GW already contains final fixtures")
    return df,target,prior,{"season":sc,"league":lc,"match":mc,"home":hc,"away":ac,"gw":gc,"date":dc,"status":stc}

def acquire_opta_fast(out: Path, cache: Path, season: str, gw: int, horizon: int) -> dict[str,Any]:
    """Fast PRE-GW Opta refresh: fixtures only (~10 MB), no heavyweight H5 rebuild."""
    fixture=cache/PANNA_TAG/"opta_fixtures.parquet"
    # The rolling opta-latest tag may replace the asset in place, so refresh this
    # small file every requested run. This avoids stale-cache ambiguity.
    fixture.unlink(missing_ok=True)
    download(asset_url("opta_fixtures.parquet"),fixture,retries=3)

    allfix,target,prior,col=opta_fixture_context(fixture,season,gw)
    horizon_df=allfix[(allfix["_gw"]>=gw)&(allfix["_gw"]<gw+horizon)].copy()
    if horizon_df["_gw"].nunique()!=horizon:
        raise RuntimeError("Incomplete Opta fixture horizon")

    hdir=out/"opta_horizon"
    hdir.mkdir(parents=True,exist_ok=True)
    csvp=hdir/f"opta_live_match_snapshot_{season}_GW{gw:02d}_H{horizon}_DEV.csv"
    keep=[col["match"],col["home"],col["away"]]+([col["date"]] if col["date"] else [])
    exp=horizon_df[["_gw"]+keep].rename(columns={
        "_gw":"gw",
        col["match"]:"opta_match_id",
        col["home"]:"opta_home_team_id",
        col["away"]:"opta_away_team_id",
    })
    exp.insert(0,"season",season)
    exp.to_csv(csvp,index=False)
    manifest={
        "status":"DEVELOPMENT_STAGING_NO_RUNTIME_AUTHORITY",
        "profile":"FAST_INCREMENTAL",
        "season":season,
        "origin_gw":gw,
        "target_gws":list(range(gw,gw+horizon)),
        "source_repo":PANNA_REPO,
        "source_tag":PANNA_TAG,
        "fixture_asset":{"path":str(fixture),"size":fixture.stat().st_size,"sha256":sha256(fixture)},
        "fixture_horizon":{"path":str(csvp),"rows":len(exp),"sha256":sha256(csvp)},
        "target_match_ids":len(set(target[col["match"]].astype(str))),
        "prior_match_ids":len(set(prior[col["match"]].astype(str))),
        "h5_rebuilt":False,
        "promotion_state":"STAGING_NOT_RUNTIME_ELIGIBLE",
    }
    write_json(hdir/"MANIFEST.json",manifest)
    return manifest

def acquire_opta_full(out: Path, cache: Path, season: str, gw: int, horizon: int) -> dict[str,Any]:
    """Explicit heavyweight rebuild. Never called by normal auto refresh."""
    import pandas as pd, h5py
    assets=release_assets()
    missing=[n for n in PANNA_FULL_ASSETS if n not in assets]
    if missing:
        raise RuntimeError(f"Missing Pannadata assets: {missing}")

    paths={}
    for name in PANNA_FULL_ASSETS:
        a=assets[name]
        p=cache/PANNA_TAG/name
        if not p.is_file() or (a.get("size") and p.stat().st_size!=int(a["size"])):
            download(a["browser_download_url"],p,int(a.get("size") or 0) or None)
        paths[name]=p

    allfix,target,prior,col=opta_fixture_context(paths["opta_fixtures.parquet"],season,gw)
    horizon_df=allfix[(allfix["_gw"]>=gw)&(allfix["_gw"]<gw+horizon)].copy()
    if horizon_df["_gw"].nunique()!=horizon:
        raise RuntimeError("Incomplete Opta fixture horizon")

    hdir=out/"opta_horizon"
    hdir.mkdir(parents=True,exist_ok=True)
    csvp=hdir/f"opta_live_match_snapshot_{season}_GW{gw:02d}_H{horizon}_DEV.csv"
    keep=[col["match"],col["home"],col["away"]]+([col["date"]] if col["date"] else [])
    exp=horizon_df[["_gw"]+keep].rename(columns={
        "_gw":"gw",
        col["match"]:"opta_match_id",
        col["home"]:"opta_home_team_id",
        col["away"]:"opta_away_team_id",
    })
    exp.insert(0,"season",season)
    exp.to_csv(csvp,index=False)

    prior_ids=set(prior[col["match"]].astype(str))
    target_ids=set(target[col["match"]].astype(str))
    h5dir=out/"opta_h5"
    h5dir.mkdir(parents=True,exist_ok=True)
    h5p=h5dir/f"kickestops_opta_raw_{season}_pre_GW{gw:02d}.h5"

    with h5py.File(h5p,"w") as h5:
        h5.attrs.update({
            "format_id":"KICKESTOPS_OPTA_RAW_H5_BRIDGE",
            "format_version":"1.0",
            "bridge_version":VERSION,
            "canonical_status":"LOCAL_RAW_NON_CANONICAL",
            "generated_at_utc":now(),
            "mode":"live",
            "league":"Serie_A",
            "season":season,
            "next_gw":gw,
        })
        tables=h5.create_group("tables")
        for name,p in paths.items():
            df=pd.read_parquet(p)
            mc=detect(df.columns,["match_id","opta_match_id","fixture_id"])
            if name=="opta_fixtures.parquet":
                selected=pd.concat([target.assign(_scope="TARGET_GW"),prior.assign(_scope="PRIOR")],ignore_index=True)
            elif mc:
                selected=df[df[mc].astype(str).isin(prior_ids)].copy()
            else:
                sc=detect(df.columns,["season","season_name","season_end_year"])
                lc=detect(df.columns,["league","competition","competition_name"])
                if not sc or not lc:
                    continue
                selected=df[
                    df[sc].map(lambda x:season_match(x,season))
                    & df[lc].map(lambda x:norm(x) in {norm("Serie_A"),norm("ITA")})
                ].copy()
            key=name.removeprefix("opta_").removesuffix(".parquet")
            grp=tables.create_group(key)
            payload=selected.to_json(orient="records",lines=True,date_format="iso").encode("utf-8")
            import numpy as np
            grp.create_dataset("jsonl_utf8",data=np.frombuffer(payload,dtype=np.uint8),compression="gzip")
            grp.attrs["source_asset"]=name
            grp.attrs["row_count"]=len(selected)
            grp.attrs["columns_json"]=json.dumps(list(selected.columns))

    manifest={
        "status":"DEVELOPMENT_STAGING_NO_RUNTIME_AUTHORITY",
        "profile":"FULL_H5_REBUILD",
        "season":season,
        "origin_gw":gw,
        "target_gws":list(range(gw,gw+horizon)),
        "source_repo":PANNA_REPO,
        "source_tag":PANNA_TAG,
        "fixture_horizon":{"path":str(csvp),"rows":len(exp),"sha256":sha256(csvp)},
        "h5":{"path":str(h5p),"sha256":sha256(h5p)},
        "target_match_ids":len(target_ids),
        "prior_match_ids":len(prior_ids),
        "h5_rebuilt":True,
        "promotion_state":"STAGING_NOT_RUNTIME_ELIGIBLE",
    }
    write_json(h5dir/(h5p.stem+".manifest.json"),manifest)
    return manifest

def _gw_state(bearer: str, season: str, gw: int) -> tuple[str,int,dict[str,Any]]:
    mid,obj,_=resolve_matchday(bearer,season,gw)
    _,rs=schedule_rows(obj)
    states=[x["state"] for x in rs.values()]
    state="LIVE_TURN" if "LIVE" in states else "COMPLETE" if states and all(s=="COMPLETE" for s in states) else "PRE_GW_FULL_MARKET"
    return state,mid,obj

def auto_gw(season: str) -> tuple[int,str]:
    """Find first non-complete GW with O(log N) schedule calls, then verify neighbours."""
    b=token()
    lo,hi=1,38
    cache={}
    def get(g):
        if g not in cache:
            cache[g]=_gw_state(b,season,g)
        return cache[g]

    while lo<hi:
        mid=(lo+hi)//2
        state,_,_=get(mid)
        if state=="COMPLETE":
            lo=mid+1
        else:
            hi=mid

    candidate=lo
    # A live neighbour takes precedence. Otherwise use the first non-complete GW.
    for g in range(max(1,candidate-1),min(38,candidate+1)+1):
        state,_,_=get(g)
        if state=="LIVE_TURN":
            return g,state
    state,_,_=get(candidate)
    if state!="COMPLETE":
        return candidate,state
    if candidate==38:
        return 38,state
    raise RuntimeError("Cannot resolve active GW")

def main() -> int:
    started=time.monotonic()
    ap=argparse.ArgumentParser()
    ap.add_argument("mode",choices=("auto","pre-gw","live-turn","opta","opta-full","inspect"))
    ap.add_argument("--gw",default="auto")
    ap.add_argument("--season",default="2026-27")
    ap.add_argument("--horizon-gws",type=int,default=3)
    ap.add_argument("--output-dir",default="artifacts")
    ap.add_argument("--cache-dir",default=".kickestops-cache")
    a=ap.parse_args()

    if a.mode=="inspect":
        print(json.dumps({
            "version":VERSION,
            "modes":["auto","pre-gw","live-turn","opta","opta-full"],
            "scheduled":False,
            "default_profile":"FAST_INCREMENTAL",
            "full_opta_is_explicit_only":True,
        },indent=2))
        return 0
    if not 1<=a.horizon_gws<=9:
        raise RuntimeError("--horizon-gws must be 1..9")

    out=Path(a.output_dir).resolve()
    cache=Path(a.cache_dir).resolve()
    out.mkdir(parents=True,exist_ok=True)
    cache.mkdir(parents=True,exist_ok=True)

    result={
        "orchestrator_version":VERSION,
        "generated_at_utc":now(),
        "mode_requested":a.mode,
        "season":a.season,
        "authority":"DEVELOP_STAGING_NO_RUNTIME_AUTHORITY",
        "status":"RUNNING",
    }
    try:
        if a.gw=="auto":
            gw,state=auto_gw(a.season)
        else:
            gw=int(a.gw)
            state="EXPLICIT_GW"

        mode=a.mode
        if mode=="auto":
            mode="live-turn" if state=="LIVE_TURN" else "pre-gw"

        result.update({"mode_executed":mode,"gw":gw,"detected_state":state})
        if mode in ("pre-gw","live-turn"):
            result["kickest"]=acquire_kickest(
                out/f"kickest_GW{gw:02d}",
                a.season,
                gw,
                a.horizon_gws,
                include_roster=(mode=="live-turn"),
            )
        if mode in ("pre-gw","opta"):
            result["opta"]=acquire_opta_fast(out,cache,a.season,gw,a.horizon_gws)
        elif mode=="opta-full":
            result["opta"]=acquire_opta_full(out,cache,a.season,gw,a.horizon_gws)

        result["status"]="SUCCESS_STAGING"
        result["next_boundary"]="governed CommonDB/boundary materialization -> QA/register"
        return_code=0
    except Exception as exc:
        result["status"]="FAILED_STAGING"
        result["error"]=f"{type(exc).__name__}: {exc}"
        return_code=2
    finally:
        result["elapsed_seconds"]=round(time.monotonic()-started,3)
        write_json(out/"AUTO_ACQUISITION_RUN.json",result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    return return_code

if __name__=="__main__":
    try: raise SystemExit(main())
    except KeyboardInterrupt: raise SystemExit(130)
    except Exception as e: print(f"ERROR: {e}",file=sys.stderr); raise SystemExit(2)
