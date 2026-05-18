#!/usr/bin/env python3
"""
il-pay-hub/scripts/search-greenhouse.py
Greenhouse job board scraper — Illinois edition.

Illinois Equal Pay Act (820 ILCS 112/10): effective Jan 1, 2025.
Employers with 15+ employees must include pay scale/range + benefits description.

Run: python3 ~/il-pay-hub/scripts/search-greenhouse.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE, IL_TERMS, _NON_IL_LOC_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/il-pay-hub/scripts/greenhouse.log")
LOCK_FILE = os.path.expanduser("~/il-pay-hub/scripts/.greenhouse.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    # ── Chicago HQ / Native ───────────────────────────────────────────────────
    ("sproutsocialprospects", "Sprout Social"),    # Sprout Social — Chicago HQ (social media SaaS)
    ("tempus", None),                              # Tempus — Chicago HQ (AI healthcare)
    ("enova", "Enova International"),              # Enova International — Chicago HQ (fintech)
    ("cameo", None),                               # Cameo — Chicago HQ (celebrity video)
    ("gohealth", "GoHealth"),                      # GoHealth — Chicago (insurance marketplace)
    ("greenthumbindustries", "Green Thumb"),       # Green Thumb Industries — Chicago (cannabis)
    ("waltzhealth", "Waltz Health"),               # Waltz Health — Chicago (pharmacy tech)
    ("honeycombinsurance", "Honeycomb Insurance"), # Honeycomb Insurance — Chicago (insurtech)
    ("cpm", "Chicago Public Media"),               # Chicago Public Media — WBEZ
    ("suntimes", "Chicago Sun-Times"),             # Chicago Sun-Times
    ("chompscareers", "Chomps"),                   # Chomps — Chicago (healthy snacks)
    ("metropolis", "Metropolis"),                  # Metropolis — Chicago (parking AI)
    ("kellerpostman", "Keller Postman"),           # Keller Postman — Chicago (law firm)
    ("climatecabinet", "Climate Cabinet"),         # Climate Cabinet — Chicago (civic tech)
    ("civisanalytics", "Civis Analytics"),         # Civis Analytics — Chicago (data science)
    ("morningstar", None),                         # Morningstar — Chicago HQ (investment research)
    ("grubhub", None),                             # Grubhub — Chicago HQ (food delivery)
    ("avant", None),                               # Avant — Chicago HQ (online lending)
    ("clearcover", "Clearcover"),                  # Clearcover — Chicago (auto insurance)
    ("vividseatsinc", "Vivid Seats"),              # Vivid Seats — Chicago (ticketing)
    ("gogoair", "Gogo"),                           # Gogo Business Aviation — Chicago
    ("progrexion", None),                          # Progrexion — Chicago (credit repair)
    ("harrisons", "Harrison Street"),              # Harrison Street — Chicago (real estate)
    ("gravitypayments", "Gravity Payments"),       # Gravity Payments — Chicago office
    ("zebra", "Zebra Technologies"),               # Zebra Technologies — Lincolnshire, IL
    ("catalina", "Catalina Marketing"),            # Catalina — Chicago (retail media)
    ("grainger", "W.W. Grainger"),                 # W.W. Grainger — Lake Forest, IL
    ("zoro", "Zoro Tools"),                        # Zoro Tools — Buffalo Grove, IL (subsidiary of Grainger)
    ("unlock-health", "Unlock Health"),            # Unlock Health — Chicago (healthcare marketing)
    ("outcomesforlife", "Outcomes"),               # Outcomes — Chicago (pharmacy software)
    ("builtinintegrationsandbox", "BenchPrep"),    # BenchPrep — Chicago (edtech)
    ("propublica", "ProPublica"),                  # ProPublica — Chicago office, IL salary shown
    # ── National companies posting IL-compliant salary ────────────────────────
    ("doordashusa", "DoorDash"),                   # DoorDash — posts IL salary
    ("groupon", "Groupon"),                        # Groupon — Chicago, uses eu.greenhouse.io
    ("imc", "IMC Trading"),                        # IMC Trading — Chicago (trading firm)
    ("dvtrading", "DV Trading"),                   # DV Trading — Chicago (prop trading)
    ("aquaticcapitalmanagement", "Aquatic Capital"), # Aquatic Capital — Chicago (quantitative trading)
    ("88fourthward", "88 Fourth Ward"),            # Political/civic tech, Chicago
    ("benchprep", "BenchPrep"),                    # BenchPrep — Chicago edtech
    ("manyawards", "Many Awards"),                 # check
]


SALARY_PATTERNS = [
    r'\$\s*([\d,]+)\s*[-–—]\s*\$\s*([\d,]+)',
    r'([\d,]+)\s*[-–—]\s*([\d,]+)\s*(?:USD|usd)',
    r'salary[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'compensation[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'pay range[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'"salary_min":\s*(\d+).*?"salary_max":\s*(\d+)',
    r'"min_salary":\s*(\d+).*?"max_salary":\s*(\d+)',
    # Illinois EPEA often labels range before city
    r'illinois[^$\n]{0,80}\$\s*([\d,]+)\s*[-–—]\s*\$\s*([\d,]+)',
    r'chicago[^$\n]{0,80}\$\s*([\d,]+)\s*[-–—]\s*\$\s*([\d,]+)',
]


def parse_salary_from_text(text: str):
    if not text:
        return None, None
    text = html_mod.unescape(html_mod.unescape(text))
    for pat in SALARY_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                val_min = int(float(raw_min))
                val_max = int(float(raw_max))
                if val_min < 1000:
                    val_min *= 1000
                if val_max < 1000:
                    val_max *= 1000
                if 30_000 <= val_min < val_max <= 1_500_000:
                    return val_min, val_max
            except (ValueError, IndexError):
                continue
    return None, None


def is_il_job(title: str, location: str, content: str) -> bool:
    loc_low = location.lower()
    content_low = (content or "").lower()

    # Deny explicit non-IL locations
    if any(t in loc_low for t in _NON_IL_LOC_TERMS):
        return False

    # Accept if explicit IL location
    if any(t in loc_low for t in IL_TERMS):
        return True

    # Accept if content specifically mentions Illinois salary range (EPEA compliance)
    if ("illinois" in content_low or "chicago" in content_low) and (
        "salary range" in content_low or "pay range" in content_low or "compensation range" in content_low
    ):
        return True

    # Accept remote/unspecified from IL-seeded companies
    if not loc_low or any(r in loc_low for r in ("remote", "distributed", "virtual", "anywhere", "work from", "wfh")):
        return True

    return False


def parse_location(location: str) -> str:
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
        "decatur": "Decatur, IL",
        "downers grove": "Downers Grove, IL",
        "oak brook": "Oak Brook, IL",
        "oak park": "Oak Park, IL",
        "lisle": "Lisle, IL",
        "rosemont": "Rosemont, IL",
        "lincolnshire": "Lincolnshire, IL",
        "lake forest": "Lake Forest, IL",
        "buffalo grove": "Buffalo Grove, IL",
        "skokie": "Skokie, IL",
        "arlington heights": "Arlington Heights, IL",
        "wheaton": "Wheaton, IL",
    }
    loc = (location or "").lower()
    for key, label in city_map.items():
        if key in loc:
            return label
    if "remote" in loc:
        return "Remote (IL)"
    return "Chicago, IL"


def fetch_company_jobs(slug: str, company_name_override=None):
    # Try standard US board only (EU board API unavailable without VPN)
    for board_base in [
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    ]:
        try:
            resp = fetcher.get(board_base, timeout=20)
            data = resp.json()
            if data.get("jobs"):
                break
        except Exception as e:
            log(f"  [{slug}] API error ({board_base[:50]}...): {e}")
            data = {}

    jobs_raw = data.get("jobs", [])
    if not jobs_raw:
        return []

    company_name = company_name_override or data.get("company", {}).get("name") or slug.title()
    results = []

    for j in jobs_raw:
        updated_at = j.get("updated_at", "")
        if updated_at and updated_at < LOOKBACK_DATE:
            continue

        title = j.get("title", "").strip()
        location_obj = j.get("location", {})
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else str(location_obj)
        content_html = j.get("content", "")
        content_text = re.sub(r'<[^>]+>', ' ', content_html)
        content_text = html_mod.unescape(content_text)

        if not is_il_job(title, location, content_text):
            continue

        val_min, val_max = parse_salary_from_text(content_html + " " + content_text)
        if val_min is None:
            val_min, val_max = parse_salary_from_text(str(j))

        if val_min is None:
            continue

        posted_date = updated_at[:10] if updated_at else TODAY
        job_url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j.get('id','')}"

        results.append({
            "role": title,
            "company": company_name,
            "min": val_min,
            "max": val_max,
            "location": parse_location(location),
            "source_url": job_url,
            "posted": posted_date,
            "source_platform": "greenhouse",
        })

    return results


def main():
    if not acquire_lock(LOCK_FILE, log):
        return

    log("=== IL Greenhouse scraper started ===")
    existing = load_existing_keys()
    log(f"Existing dedup keys: {len(existing)}")

    new_count = 0
    for slug, name_override in SEED_SLUGS:
        log(f"[{slug}] fetching...")
        jobs = fetch_company_jobs(slug, name_override)
        for job in jobs:
            key = f"{job['role'].lower().strip()}|{job['company'].lower().strip()}"
            if key in existing:
                continue
            write_job(OUTPUT_FILE, job)
            existing.add(key)
            new_count += 1
            log(f"  + {job['role']} @ {job['company']} | ${job['min']:,}–${job['max']:,} | {job['location']}")
        time.sleep(0.5)

    log(f"=== Done. {new_count} new IL jobs written to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
