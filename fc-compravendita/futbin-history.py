#!/usr/bin/env python3
"""FUTBIN FC27 market-history collector for John Brain.

Sources:
  - FUTBIN /27/playerGraph for daily/hourly history
  - FUTBIN /27/sales page as fallback for embedded Highcharts data

The script stores raw responses first, then writes normalized CSVs and derives
Thursday/Sunday panic -> rebound gaps with EA 5% tax included.

Use responsibly: rate-limited requests, no parallel scraping.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.futbin.com/27"
YEAR = "27"
DEFAULT_PLATFORM = "ps"  # FUTBIN FC27 shared console market
EA_TAX = 0.05
REQUEST_DELAY_SECONDS = 2.0

DEFAULT_SHORTLIST = [
    {"name": "Bradley Barcola", "rating": 85},
    {"name": "Nico Williams", "rating": 84},
    {"name": "Aurelien Tchouameni", "rating": 84},
    {"name": "Marcus Rashford", "rating": 82},
    {"name": "Karim Adeyemi", "rating": 82},
    {"name": "Marcos Llorente", "rating": 85},
    {"name": "Frenkie de Jong", "rating": 86},
    {"name": "Selma Bacha", "rating": 86},
]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")


@dataclass
class CardRef:
    name: str
    rating: int | None
    page_id: int
    slug: str
    resource_id: int
    url: str


class FutbinClient:
    def __init__(self, platform: str = DEFAULT_PLATFORM, delay: float = REQUEST_DELAY_SECONDS):
        self.platform = platform
        self.delay = delay
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def get(self, url: str, *, params: dict[str, Any] | None = None, timeout: float = 25.0) -> requests.Response:
        elapsed = time.time() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        r = self.session.get(url, params=params, timeout=timeout)
        self.last_request = time.time()
        r.raise_for_status()
        return r

    def find_card(self, name: str, rating: int | None = None) -> CardRef:
        """Find the FC27 card page and extract FUTBIN resource id.

        Search-result markup changes over time, so candidates are discovered from
        every /27/player/<id>/<slug> link and validated by fetching the card page.
        """
        search_url = f"{BASE}/players"
        r = self.get(search_url, params={"search": name})
        soup = BeautifulSoup(r.text, "lxml")

        candidates: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            m = re.search(r"/27/player/(\d+)/([^/?#]+)", href)
            if not m:
                continue
            page_id = int(m.group(1))
            if page_id in seen:
                continue
            seen.add(page_id)
            candidates.append((page_id, m.group(2), urljoin("https://www.futbin.com", href)))

        # Search engines/site markup may return no card links. Try direct player-history
        # discovery only as a diagnostic; we never invent a card id.
        if not candidates:
            raise RuntimeError(f"No FC27 FUTBIN card candidates found for {name!r}")

        wanted = norm(name)
        scored: list[tuple[int, CardRef]] = []
        for page_id, slug, url in candidates[:15]:
            try:
                pr = self.get(url)
            except requests.RequestException:
                continue
            ps = BeautifulSoup(pr.text, "lxml")
            text = " ".join(ps.stripped_strings)

            page_name = ""
            h1 = ps.find("h1")
            if h1:
                page_name = h1.get_text(" ", strip=True)
            if not page_name:
                page_name = slug.replace("-", " ")

            page_rating = None
            for sel in (".pcdisplay-rat", "[class*='rating']"):
                el = ps.select_one(sel)
                if el:
                    mm = re.search(r"\b(\d{2})\b", el.get_text(" ", strip=True))
                    if mm:
                        page_rating = int(mm.group(1))
                        break
            if page_rating is None and rating is not None:
                # Conservative fallback: look for the expected rating near Gold/Rare text.
                if re.search(rf"\b{rating}\b", text):
                    page_rating = rating

            resource_id = None
            page_info = ps.select_one("#page-info")
            if page_info:
                for attr in ("data-player-resource", "data-player-id", "data-resource-id"):
                    raw = page_info.get(attr)
                    if raw and str(raw).isdigit():
                        resource_id = int(raw)
                        break
            if resource_id is None:
                # Search raw HTML for current/legacy attributes.
                mm = re.search(r'data-player-resource=["\'](\d+)["\']', pr.text)
                if mm:
                    resource_id = int(mm.group(1))
            if resource_id is None:
                continue

            score = 0
            pnorm = norm(page_name)
            if wanted in pnorm or pnorm in wanted:
                score += 100
            if norm(slug).replace(" ", "") == wanted.replace(" ", ""):
                score += 50
            if rating is not None and page_rating == rating:
                score += 40
            if re.search(r"Gold Rare|Rare Gold|Gold", text, re.I):
                score += 10

            scored.append((score, CardRef(name=name, rating=page_rating, page_id=page_id,
                                          slug=slug, resource_id=resource_id, url=url)))

        if not scored:
            raise RuntimeError(f"Found FUTBIN links but could not validate card/resource id for {name!r}")
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best = scored[0]
        if best_score < 100:
            raise RuntimeError(f"Ambiguous FUTBIN match for {name!r}: best={best.url} score={best_score}")
        return best

    def graph(self, card: CardRef, graph_type: str) -> tuple[dict[str, Any], str]:
        url = f"{BASE}/playerGraph"
        r = self.get(url, params={
            "type": graph_type,
            "year": YEAR,
            "player": card.resource_id,
            "set_id": "",
        })
        try:
            return r.json(), r.url
        except ValueError as e:
            raise RuntimeError(f"FUTBIN graph returned non-JSON for {card.name}: {r.url}") from e

    def sales_fallback(self, card: CardRef) -> tuple[list[tuple[int, int]], str, str]:
        url = f"{BASE}/sales/{card.page_id}/{card.slug}"
        r = self.get(url, params={"platform": self.platform})

        # Current/legacy Highcharts payloads. Prefer explicit {x,y} objects; then [[ts,price]].
        points: list[tuple[int, int]] = []
        for mm in re.finditer(r'"x"\s*:\s*(1\d{12})\s*,\s*"y"\s*:\s*(\d+)', r.text):
            points.append((int(mm.group(1)), int(mm.group(2))))
        if not points:
            arrays = re.findall(r'\[\[(1\d{12},\d+)\](?:,\[(?:1\d{12},\d+)\])+\]', r.text)
            # Regex above only proves such arrays exist; scan all timestamp-price pairs globally.
            if arrays:
                points = [(int(a), int(b)) for a, b in re.findall(r'\[(1\d{12}),(\d+)\]', r.text)]

        # De-duplicate while preserving chronological order.
        points = sorted(set(points))
        return points, r.url, r.text


def extract_series(payload: dict[str, Any], platform: str) -> list[tuple[int, int]]:
    """Normalize current/legacy FUTBIN graph JSON into (timestamp_ms, price)."""
    keys = [platform]
    if platform == "ps":
        keys += ["xbox", "console", "cross"]
    raw = None
    for k in keys:
        if k in payload and isinstance(payload[k], list):
            raw = payload[k]
            break
    if raw is None:
        # Some wrappers nest graph series.
        for v in payload.values():
            if isinstance(v, dict):
                for k in keys:
                    if isinstance(v.get(k), list):
                        raw = v[k]
                        break
            if raw is not None:
                break
    if raw is None:
        return []

    out: list[tuple[int, int]] = []
    for p in raw:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            ts, price = p[0], p[1]
        elif isinstance(p, dict):
            ts = p.get("timestamp", p.get("x"))
            price = p.get("price", p.get("y"))
        else:
            continue
        try:
            ts_i = int(ts)
            price_i = int(float(price))
        except (TypeError, ValueError):
            continue
        if ts_i < 10_000_000_000:  # seconds -> ms
            ts_i *= 1000
        if price_i > 0:
            out.append((ts_i, price_i))
    return sorted(set(out))


def write_series_csv(path: Path, card: CardRef, series_name: str, points: Iterable[tuple[int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["player", "rating", "series", "timestamp_ms", "timestamp_utc", "price"])
        for ts, price in points:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            w.writerow([card.name, card.rating or "", series_name, ts, dt.isoformat(), price])


def read_points_csv(path: Path) -> list[tuple[datetime, int]]:
    rows: list[tuple[datetime, int]] = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            dt = datetime.fromisoformat(r["timestamp_utc"])
            rows.append((dt, int(r["price"])))
    return rows


def pct(a: float, b: float) -> float:
    return ((b / a) - 1.0) * 100.0 if a else 0.0


def analyze_event(points: list[tuple[datetime, int]], event_day: datetime, kind: str) -> dict[str, Any] | None:
    """Derive a John-style pre-drop -> low -> rebound cycle.

    Thursday:
      pre = last observation in prior 18h
      low = minimum from Thu 00:00 to Thu 18:00 UTC
      rebound = maximum after low through Fri 23:59 UTC

    Sunday:
      pre = last observation in prior 18h
      low = minimum during Sunday
      rebound = maximum after low through Monday 23:59 UTC

    Windows are intentionally explicit and easy to tune as FC27 reward/content timing evolves.
    """
    day0 = event_day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    pre_start = day0 - timedelta(hours=18)
    low_end = day0 + (timedelta(hours=18) if kind == "thursday" else timedelta(days=1))
    rebound_end = day0 + timedelta(days=2)

    pre_pts = [(d, p) for d, p in points if pre_start <= d < day0]
    low_pts = [(d, p) for d, p in points if day0 <= d < low_end]
    if not pre_pts or not low_pts:
        return None
    pre_dt, pre_price = pre_pts[-1]
    low_dt, low_price = min(low_pts, key=lambda x: x[1])
    rebound_pts = [(d, p) for d, p in points if low_dt <= d < rebound_end]
    if not rebound_pts:
        return None
    reb_dt, reb_price = max(rebound_pts, key=lambda x: x[1])

    return {
        "event": kind,
        "event_date": day0.date().isoformat(),
        "pre_time_utc": pre_dt.isoformat(),
        "pre_price": pre_price,
        "low_time_utc": low_dt.isoformat(),
        "low_price": low_price,
        "drop_pct": round(pct(pre_price, low_price), 2),
        "rebound_time_utc": reb_dt.isoformat(),
        "rebound_price": reb_price,
        "rebound_gross_pct": round(pct(low_price, reb_price), 2),
        "rebound_net_roi_pct": round(((reb_price * (1 - EA_TAX) / low_price) - 1) * 100, 2),
        "hours_to_rebound": round((reb_dt - low_dt).total_seconds() / 3600, 1),
    }


def derive_gaps(hourly_csv: Path, out_csv: Path) -> None:
    points = read_points_csv(hourly_csv)
    if not points:
        return
    start = points[0][0].date()
    end = points[-1][0].date()
    cur = start
    rows: list[dict[str, Any]] = []
    while cur <= end:
        wd = cur.weekday()  # Mon=0
        if wd in (3, 6):  # Thursday, Sunday
            dt = datetime(cur.year, cur.month, cur.day, tzinfo=timezone.utc)
            r = analyze_event(points, dt, "thursday" if wd == 3 else "sunday")
            if r:
                rows.append(r)
        cur += timedelta(days=1)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "event", "event_date", "pre_time_utc", "pre_price", "low_time_utc", "low_price",
        "drop_pct", "rebound_time_utc", "rebound_price", "rebound_gross_pct",
        "rebound_net_roi_pct", "hours_to_rebound",
    ]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def collect_player(client: FutbinClient, player: dict[str, Any], root: Path) -> dict[str, Any]:
    card = client.find_card(player["name"], player.get("rating"))
    key = slugify(card.name)
    raw_dir = root / "raw" / key
    norm_dir = root / "normalized"
    derived_dir = root / "derived"
    raw_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "name": card.name,
        "rating": card.rating,
        "page_id": card.page_id,
        "resource_id": card.resource_id,
        "slug": card.slug,
        "url": card.url,
        "platform": client.platform,
    }
    (raw_dir / "card.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    hourly_points: list[tuple[int, int]] = []
    daily_points: list[tuple[int, int]] = []
    sources: dict[str, str] = {}

    for graph_type in ("hourly_graph", "daily_graph"):
        try:
            payload, url = client.graph(card, graph_type)
            sources[graph_type] = url
            (raw_dir / f"{graph_type}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            pts = extract_series(payload, client.platform)
            if graph_type == "hourly_graph":
                hourly_points = pts
            else:
                daily_points = pts
        except Exception as e:
            (raw_dir / f"{graph_type}.error.txt").write_text(str(e), encoding="utf-8")

    # Fallback for recent history when hourly graph is unavailable/empty.
    if not hourly_points:
        try:
            pts, url, html = client.sales_fallback(card)
            sources["sales_fallback"] = url
            (raw_dir / "sales.html").write_text(html, encoding="utf-8")
            hourly_points = pts
        except Exception as e:
            (raw_dir / "sales.error.txt").write_text(str(e), encoding="utf-8")

    meta["sources"] = sources
    meta["hourly_points"] = len(hourly_points)
    meta["daily_points"] = len(daily_points)
    (raw_dir / "card.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    hourly_csv = norm_dir / f"{key}-hourly.csv"
    daily_csv = norm_dir / f"{key}-daily.csv"
    if hourly_points:
        write_series_csv(hourly_csv, card, "hourly", hourly_points)
        derive_gaps(hourly_csv, derived_dir / f"{key}-weekly-gaps.csv")
    if daily_points:
        write_series_csv(daily_csv, card, "daily", daily_points)
    return meta


def load_shortlist(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return DEFAULT_SHORTLIST
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("shortlist JSON must be an array")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="history", help="History output directory")
    ap.add_argument("--shortlist", type=Path, help="JSON shortlist override")
    ap.add_argument("--player", help="Collect only one player name")
    ap.add_argument("--rating", type=int, help="Expected rating with --player")
    ap.add_argument("--platform", choices=["ps", "pc"], default=DEFAULT_PLATFORM)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS)
    args = ap.parse_args()

    players = [{"name": args.player, "rating": args.rating}] if args.player else load_shortlist(args.shortlist)
    root = Path(args.out)
    client = FutbinClient(platform=args.platform, delay=args.delay)

    manifest: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for i, player in enumerate(players, 1):
        print(f"[{i}/{len(players)}] {player['name']}", flush=True)
        try:
            meta = collect_player(client, player, root)
            manifest.append(meta)
            print(f"  OK hourly={meta['hourly_points']} daily={meta['daily_points']} {meta['url']}")
        except Exception as e:
            failures.append({"name": player["name"], "error": str(e)})
            print(f"  FAIL {e}", file=sys.stderr)

    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": args.platform,
        "successes": manifest,
        "failures": failures,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Done: {len(manifest)} OK, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())