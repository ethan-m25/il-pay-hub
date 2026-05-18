#!/usr/bin/env python3
"""
il-pay-hub/scripts/search-lever.py
Lever job board scraper — Illinois edition.

Illinois Equal Pay Act (820 ILCS 112/10): effective Jan 1, 2025.
Employers with 15+ employees must include pay scale/range + benefits description.

Strategy:
  1. Seed slugs (IL-based Lever companies) + Exa/Brave discovery
  2. Lever public postings JSON API → all jobs per company
  3. Salary: structured salaryRange field first, then regex fallback
  4. IL filter: location mentions IL / Chicago / remote-eligible

Run: python3 ~/il-pay-hub/scripts/search-lever.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, exa_search, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE, IL_TERMS, _NON_IL_LOC_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/il-pay-hub/scripts/lever.log")
LOCK_FILE = os.path.expanduser("~/il-pay-hub/scripts/.lever.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    # ── Chicago / Illinois Native ─────────────────────────────────────────────
    "milhouseinc",        # Milhouse Engineering — Chicago (engineering/utilities)
    "ppil",               # Planned Parenthood of Illinois — Chicago
    "mhnchicago",         # Medical Home Network — Chicago (healthcare)
    "bisnow",             # Bisnow — Chicago (commercial real estate media)
    "relativity",         # Relativity — Chicago (legal tech, eDiscovery)
    "clearcover",         # Clearcover Insurance — Chicago (auto insurtech)
    "vivid-seats",        # Vivid Seats — Chicago (ticketing marketplace)
    "uptake",             # Uptake — Chicago (industrial AI)
    "coyote-logistics",   # Coyote Logistics — Chicago (freight brokerage)
    "power-reviews",      # PowerReviews — Chicago (ratings SaaS)
    "solstice",           # Solstice — Chicago (clean energy)
    "fooda",              # Fooda — Chicago (corporate food delivery)
    "availity",           # Availity — Chicago (healthcare info network)
    "pathward",           # Pathward (MetaBank) — remote/IL
    "jumptrading",        # Jump Trading — Chicago (quantitative trading)
    "arcadiagroup",       # Arcadia Group — Chicago (clean energy data)
    "thinknear",          # ThinkNear — Chicago
    "textura",            # Textura — Deerfield, IL (construction software)
    "comed",              # ComEd — Chicago (utility, may have jobs)
    "bounteous",          # Bounteous — Chicago (digital agency)
    "navigant",           # Navigant — Chicago (consulting)
    "paylocity",          # Paylocity — Schaumburg, IL (payroll HCM SaaS)
    "echo",               # Echo Global Logistics — Chicago
    "springbig",          # SpringBig — Chicago (cannabis tech)
    "medialink",          # MediaLink — Chicago (media consulting)
    "outcome-health",     # Outcome Health — Chicago (point-of-care media)
    "thirdwayhealth",     # Third Way Health — Chicago (healthcare)
    "sproutsocial",       # Sprout Social — Chicago (also on GH, catches extras)
    "morningconsult",     # Morning Consult — Chicago/DC (polling data)
    "innerworkings",      # InnerWorkings — Chicago (marketing supply chain)

# Remove duplicates at runtime
]

# Remove duplicates
SEED_SLUGS = list(dict.fromkeys(SEED_SLUGS))


DISCOVERY_QUERIES = [
    'site:jobs.lever.co "Chicago" OR "Illinois" salary range 2025 2026',
    'site:jobs.lever.co "Chicago, IL" engineer OR analyst OR manager salary',
    'site:jobs.lever.co "Illinois" "pay range" 2025',
    'site:jobs.lever.co "Chicago" healthcare finance tech salary 2026',
    'site:jobs.lever.co "Illinois Equal Pay" OR "820 ILCS" salary 2025',
]


SALARY_RE = [
    re.compile(r'\$\s*([\d,]+)(?:\.\d+)?\s*(?:USD|usd)?\s*[-–—to]+\s*\$\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'\$([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    re.compile(r'(?:pay|salary|compensation|base|wage|range)[^$\n]{0,50}\$?([\d,]{5,})\s*[-–—to]+\s*\$?([\d,]{5,})', re.IGNORECASE),
]

LEVER_SLUG_RE = re.compile(r'https?://jobs\.lever\.co/([a-zA-Z0-9._-]+)', re.IGNORECASE)
_SKIP_SLUGS = {'jobs', 'search', 'home', 'usasurveyjob'}


def discover_slugs(seed_slugs):
    known = set(seed_slugs)
    discovered = set()
    for i, query in enumerate(DISCOVERY_QUERIES, 1):
        log(f"  Discovery Exa [{i}/{len(DISCOVERY_QUERIES)}]: {query[:60]}...")
        resp = exa_search(query, num_results=15, start_date=LOOKBACK_DATE, log=log)
        if not resp:
            continue
        new = 0
        for r in resp.get("results", []):
            m = LEVER_SLUG_RE.search(r.get("url", ""))
            if not m:
                continue
            slug = m.group(1).lower()
            if slug in _SKIP_SLUGS or slug in known or len(slug) < 2:
                continue
            discovered.add(slug)
            new += 1
        log(f"    → {new} new slugs")
        time.sleep(1.5)
    return discovered


def fetch_company_jobs(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        page = fetcher.get(url, timeout=20)
        if page.status != 200:
            return []
        return page.json() or []
    except Exception as e:
        log(f"  API error ({slug}): {e}")
        return []


def is_il_location(location_str: str, desc_text: str = "") -> bool:
    loc = (location_str or "").lower()

    # Deny explicit non-IL locations (when no IL term is present)
    if any(t in loc for t in _NON_IL_LOC_TERMS):
        if not any(t in loc for t in IL_TERMS):
            return False

    # Accept if explicitly mentions IL
    if any(t in loc for t in IL_TERMS):
        return True

    # Remote/unspecified → accept (IL-eligible from IL-seeded companies)
    if not loc or any(r in loc for r in ("remote", "distributed", "anywhere", "virtual", "work from")):
        return True

    return False


def parse_location(location_str: str) -> str:
    city_map = {
        "chicago": "Chicago, IL",
        "evanston": "Evanston, IL",
        "schaumburg": "Schaumburg, IL",
        "naperville": "Naperville, IL",
        "rockford": "Rockford, IL",
        "joliet": "Joliet, IL",
        "waukegan": "Waukegan, IL",
        "elgin": "Elgin, IL",
        "peoria": "Peoria, IL",
        "bloomington": "Bloomington, IL",
        "champaign": "Champaign, IL",
        "downers grove": "Downers Grove, IL",
        "oak brook": "Oak Brook, IL",
        "lincolnshire": "Lincolnshire, IL",
        "lake forest": "Lake Forest, IL",
        "buffalo grove": "Buffalo Grove, IL",
    }
    loc = (location_str or "").lower()
    for city, label in city_map.items():
        if city in loc:
            return label
    if "remote" in loc or not loc:
        return "Remote (IL)"
    return "Chicago, IL"


def extract_salary_from_range(sal_range):
    if not sal_range:
        return None
    currency = sal_range.get("currency", "").upper()
    if currency not in ("USD", ""):
        return None
    if sal_range.get("interval", "") != "per-year-salary":
        return None
    try:
        vmin = int(float(sal_range["min"]))
        vmax = int(float(sal_range["max"]))
        if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
            return vmin, vmax
    except (KeyError, ValueError, TypeError):
        pass
    return None


def extract_salary_from_text(text):
    if not text:
        return None
    clean = html_mod.unescape(re.sub(r'<[^>]+>', ' ', text))
    clean = html_mod.unescape(re.sub(r'\s+', ' ', clean).strip())
    for pat in SALARY_RE:
        m = pat.search(clean)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                if "k" in m.group(0).lower():
                    vmin = int(float(raw_min) * 1000)
                    vmax = int(float(raw_max) * 1000)
                else:
                    vmin = int(float(raw_min))
                    vmax = int(float(raw_max))
                if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                    return vmin, vmax
            except (ValueError, IndexError):
                continue
    return None


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== IL Lever scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    log(f"Running discovery ({len(DISCOVERY_QUERIES)} queries)...")
    extra_slugs = discover_slugs(SEED_SLUGS)
    log(f"  {len(SEED_SLUGS)} seed + {len(extra_slugs)} discovered = "
        f"{len(SEED_SLUGS) + len(extra_slugs)} total slugs")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    api_failures = 0
    discovered_slug_yield = {}
    all_slugs = list(SEED_SLUGS) + sorted(extra_slugs)

    for slug in all_slugs:
        jobs = fetch_company_jobs(slug)
        if not jobs:
            log(f"── {slug}: no jobs or API error")
            api_failures += 1
            time.sleep(1)
            continue

        company_name = slug.replace("-", " ").replace("_", " ").replace(".", " ").title()
        log(f"\n── {company_name} ({slug}): {len(jobs)} postings ──")
        il_count = 0
        found_this = 0

        for job in jobs:
            cats = job.get("categories") or {}
            loc_name = cats.get("location", "") or cats.get("allLocations", "")
            if isinstance(loc_name, list):
                loc_name = ", ".join(loc_name)

            desc_plain = job.get("descriptionPlain") or ""
            if not is_il_location(loc_name, desc_plain):
                continue
            il_count += 1

            title = (job.get("text") or "").strip()
            if not title:
                continue

            key = f"{title.lower()}|{company_name.lower()}"
            if key in seen_keys:
                continue

            salary = extract_salary_from_range(job.get("salaryRange"))
            if not salary:
                sal_desc = job.get("salaryDescriptionPlain") or job.get("salaryDescription") or ""
                salary = extract_salary_from_text(sal_desc) or extract_salary_from_text(desc_plain)

            if not salary:
                log(f"  [{title[:50]}] → no salary")
                continue

            vmin, vmax = salary
            job_id = job.get("id", "")
            abs_url = f"https://jobs.lever.co/{slug}/{job_id}" if job_id else ""

            posted = TODAY
            created_ms = job.get("createdAt")
            if created_ms:
                try:
                    posted = datetime.fromtimestamp(
                        int(created_ms) / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    pass

            job_out = {
                "role":            title,
                "company":         company_name,
                "min":             vmin,
                "max":             vmax,
                "location":        parse_location(loc_name),
                "source_url":      abs_url,
                "posted":          posted,
                "source_platform": "lever",
            }

            write_job(OUTPUT_FILE, job_out)
            seen_keys.add(key)
            total_found += 1
            found_this += 1
            log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{loc_name}]")

        log(f"  IL: {il_count} | New w/ salary: {found_this}")
        if slug in extra_slugs:
            discovered_slug_yield[slug] = found_this
        time.sleep(2)

    log(f"\n=== Lever scraper complete: {total_found} new jobs "
        f"(api_failures={api_failures}) ===")

    # Auto-inject high-yield discovered slugs
    seed_set = set(SEED_SLUGS)
    newly_qualified = {
        slug: count
        for slug, count in discovered_slug_yield.items()
        if slug not in seed_set and count >= 3
    }
    if newly_qualified:
        log(f"\nAuto-injecting {len(newly_qualified)} high-yield slug(s) into SEED_SLUGS:")
        script_path = os.path.abspath(__file__)
        try:
            source = open(script_path).read()
            new_lines = []
            for slug, count in sorted(newly_qualified.items(), key=lambda x: -x[1]):
                if f'"{slug}"' in source:
                    log(f"  skip {slug} — already in file")
                    continue
                log(f"  + {slug} ({count} IL+salary jobs)")
                new_lines.append(
                    f'    "{slug}",  # auto-discovered {TODAY} — {count} IL+salary'
                )
            if new_lines:
                insert_block = "\n".join(new_lines)
                marker = "\n# Remove duplicates"
                if marker in source:
                    source = source.replace(marker, f"\n{insert_block}{marker}")
                    open(script_path, "w").write(source)
                    log(f"  Persisted {len(new_lines)} slug(s) to SEED_SLUGS")
                else:
                    log("  Could not find SEED_SLUGS end marker — skipping persist")
        except Exception as e:
            log(f"  Auto-inject error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
