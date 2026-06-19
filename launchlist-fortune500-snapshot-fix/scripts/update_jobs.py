#!/usr/bin/env python3
"""LaunchList rolling Fortune 500 internship scanner.

Coverage model
--------------
1. Refresh the current Fortune 500 company directory from Fortune's ranking pages.
2. Enrich company records with corporate domains from a public company directory.
3. Scan every company in a rolling queue. The default hourly batch of 35 companies
   covers all 500 in roughly 15 hours.
4. Use direct ATS APIs when possible; otherwise discover official careers pages and
   inspect internship links found there.
5. Keep only target-role internships/pre-internships whose stated graduation rule
   includes 2029 or does not state a graduation year.

This deliberately favors official employer/ATS links and precision over recall.
Some career sites block automated access; the generated site exposes coverage and
failure counts instead of pretending every scan succeeded.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "opportunities.json"
DIRECTORY_PATH = ROOT / "data" / "fortune500_companies.json"
SCAN_STATE_PATH = ROOT / "data" / "company_scan_state.json"
OVERRIDES_PATH = ROOT / "config" / "companies.json"
SCANNER_CONFIG_PATH = ROOT / "config" / "scanner.json"
FEED_PATH = ROOT / "feed.xml"
USER_AGENT = "LaunchList/2.2 (+personal Fortune 500 internship tracker)"
NOW = datetime.now(timezone.utc)

SEARCH_TERMS = ["intern", "internship", "co-op", "early insight", "explore program"]
CAREER_WORDS = re.compile(r"\b(careers?|jobs?|students?|university|early careers?|campus)\b", re.I)
JOB_LINK_WORDS = re.compile(r"\b(intern(ship)?|co[- ]?op|extern(ship)?|apprentice(ship)?|early insight|explore|discovery)\b", re.I)

ROLE_RULES: dict[str, tuple[str, ...]] = {
    "Tech consulting": (
        "technology consultant", "tech consultant", "consulting intern", "solutions consultant",
        "solution engineering", "customer solutions", "advisory intern", "technology advisory",
    ),
    "Product management": (
        "product manager", "product management", "product strategy", "product development intern",
        "associate product", "product intern",
    ),
    "Wealth management": (
        "wealth management", "private wealth", "asset management", "investment management",
        "client analyst", "portfolio analytics", "financial advisor intern",
    ),
    "Software engineering": (
        "software engineer", "software development", "software developer", "sde intern",
        "developer intern", "engineering intern", "full stack intern", "application developer intern",
    ),
    "Business analyst": (
        "business analyst", "business analytics", "strategy analyst", "financial analyst intern",
        "finance intern", "business intelligence intern",
    ),
    "Data analyst": (
        "data analyst", "data analytics", "analytics intern", "business intelligence",
        "reporting analyst", "data science intern", "decision science intern",
    ),
    "Operations analyst": (
        "operations analyst", "operations intern", "operational excellence", "operations management intern",
        "supply chain intern", "process improvement intern", "strategy and operations",
    ),
    "IT analyst": (
        "it analyst", "information technology intern", "technology analyst", "systems analyst intern",
        "it intern", "technology program intern", "infrastructure intern",
    ),
    "Startup analytics": (
        "startup analytics", "growth analytics", "venture analytics", "innovation analytics",
        "corporate venture", "startup program",
    ),
    "Digital transformation": (
        "digital transformation", "technology transformation", "cloud transformation", "automation intern",
        "digital strategy", "modernization intern", "transformation analyst",
    ),
    "Product operations": (
        "product operations", "product ops", "program manager intern", "product delivery",
        "business operations intern", "product commercialization", "technical program manager intern",
    ),
}

INTERN_TITLE_RE = re.compile(
    r"\b(intern(ship)?|co[- ]?op|extern(ship)?|apprentice(ship)?|early insight|early identification|"
    r"sophomore program|freshman program|explore program|discovery program|pre[- ]?intern)\b",
    re.I,
)
GRAD_TRIGGER_RE = re.compile(
    r"graduat(?:e|es|ing|ion|ion date|ion year)?|conferral|degree completion|complete (?:a|the) degree|"
    r"return(?:ing)? to (?:school|university|college)|remaining (?:in|until) (?:school|graduation)",
    re.I,
)
YEAR_RE = re.compile(r"\b20(?:2[4-9]|3[0-2])\b")
US_RE = re.compile(r"\b(united states|usa|u\.s\.|us,|remote[- ]?us)\b", re.I)
REMOTE_RE = re.compile(r"\bremote\b", re.I)
ATS_HOST_HINTS = (
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "smartrecruiters.com",
    "ashbyhq.com", "icims.com", "successfactors.com", "oraclecloud.com",
    "phenompeople.com", "eightfold.ai", "jobvite.com", "careers-page.com",
)


@dataclass
class GradDecision:
    eligible: bool
    status: str
    evidence: str


@dataclass
class ScanResult:
    company: dict[str, Any]
    jobs: list[dict[str, Any]]
    status: str
    error: str | None = None
    careers_url: str | None = None
    adapter: str = "discovery"


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(html.unescape(value), "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def normalize_company_name(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace("&", "and")
    value = re.sub(r"\b(the|incorporated|inc|corp|corporation|company|companies|co|holdings|group|plc|llc)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def stable_id(company: str, external_id: str | None, url: str) -> str:
    seed = external_id or url
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{slugify(company)}-{digest}"


def grad_contexts(text: str) -> list[str]:
    contexts: list[str] = []
    for match in GRAD_TRIGGER_RE.finditer(text):
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 240)
        contexts.append(text[start:end])
    return contexts


def shorten_evidence(text: str, limit: int = 175) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def evaluate_graduation(text: str) -> GradDecision:
    contexts = grad_contexts(text)
    if not contexts:
        return GradDecision(True, "No graduation year listed", "The posting does not state a graduation-year requirement.")

    joined = " … ".join(contexts)
    years = sorted({int(year) for year in YEAR_RE.findall(joined)})
    if 2029 in years:
        excerpt = next((c for c in contexts if "2029" in c), joined)
        return GradDecision(True, "2029 eligible", shorten_evidence(excerpt))

    for context in contexts:
        context_years = [int(y) for y in YEAR_RE.findall(context)]
        lower = context.lower()
        if not context_years:
            continue
        if re.search(r"\b(or|and) later\b|\bor after\b|\bafter\b|\bno earlier than\b", lower):
            if min(context_years) <= 2029:
                return GradDecision(True, "2029 eligible", shorten_evidence(context))
        if len(context_years) >= 2 and min(context_years) <= 2029 <= max(context_years):
            return GradDecision(True, "2029 eligible", shorten_evidence(context))

    if years:
        return GradDecision(False, "Excluded graduation year", shorten_evidence(joined))
    return GradDecision(True, "No graduation year listed", "School enrollment is required, but no graduation year is stated.")


def classify_roles(title: str, description: str) -> list[str]:
    title_lower = title.lower()
    combined = f"{title} {description[:7000]}".lower()
    matches: list[tuple[int, str]] = []
    for role, phrases in ROLE_RULES.items():
        score = sum(5 if phrase in title_lower else 1 if phrase in combined else 0 for phrase in phrases)
        if score:
            matches.append((score, role))
    matches.sort(reverse=True)
    return [role for _, role in matches[:3]]


def derive_tags(text: str) -> list[str]:
    candidates = {
        "Python": r"\bpython\b", "SQL": r"\bsql\b", "Excel": r"\bexcel\b",
        "Tableau": r"\btableau\b", "Power BI": r"\bpower\s*bi\b", "Cloud": r"\bcloud\b",
        "AI / ML": r"\b(ai|machine learning|ml|generative ai|genai)\b", "Product": r"\bproduct\b",
        "Strategy": r"\bstrategy\b", "Operations": r"\boperations?\b", "Finance": r"\bfinance\b",
    }
    return [label for label, pattern in candidates.items() if re.search(pattern, text, re.I)][:5]


def opportunity_type(title: str) -> str:
    lower = title.lower()
    if "co-op" in lower or "coop" in lower:
        return "Co-op"
    if re.search(r"pre[- ]?intern|early insight|explore|discovery|sophomore|freshman|extern", lower):
        return "Pre-internship"
    return "Internship"


def freshman_friendly(text: str, grad: GradDecision) -> bool:
    lower = text.lower()
    positive = any(term in lower for term in ("freshman", "first-year", "first year", "sophomore", "second-year", "second year", "2029"))
    negative = any(term in lower for term in ("must be a junior", "junior standing required", "penultimate year", "rising senior"))
    return (positive and not negative) or (grad.status == "2029 eligible" and not negative)


def infer_region(location: str) -> str:
    if REMOTE_RE.search(location) and (US_RE.search(location) or location.strip().lower() == "remote"):
        return "Remote"
    state_re = r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b"
    if US_RE.search(location) or re.search(state_re, location):
        return "United States"
    return "Global"


def parse_flexible_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = clean_text(str(value))
    lower = raw.lower()
    if "today" in lower or "just posted" in lower:
        return NOW.date().isoformat()
    if "yesterday" in lower:
        return (NOW - timedelta(days=1)).date().isoformat()
    relative = re.search(r"(?:posted\s+)?(\d+)\s+days?\s+ago", lower)
    if relative:
        return (NOW - timedelta(days=int(relative.group(1)))).date().isoformat()
    relative_hours = re.search(r"(?:posted\s+)?(\d+)\s+hours?\s+ago", lower)
    if relative_hours:
        return (NOW - timedelta(hours=int(relative_hours.group(1)))).date().isoformat()
    try:
        dt = date_parser.parse(raw, fuzzy=True, default=datetime(NOW.year, 1, 1))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def extract_deadline(text: str) -> str | None:
    match = re.search(r"(?:application deadline|apply by|end date|last day to apply|applications? close[sd]?)\s*[:\-]?\s*([^.;\n]{4,50})", text, re.I)
    return parse_flexible_date(match.group(1)) if match else None


def deadline_passed(deadline: str | None) -> bool:
    if not deadline:
        return False
    try:
        return datetime.fromisoformat(deadline).date() < NOW.date()
    except ValueError:
        return False


def make_summary(description: str, categories: list[str]) -> str:
    text = clean_text(description)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    useful: list[str] = []
    for sentence in sentences:
        s = sentence.strip()
        if 35 <= len(s) <= 400:
            useful.append(s)
        if sum(len(x) for x in useful) > 230:
            break
    return shorten_evidence(" ".join(useful) or f"Hands-on internship work related to {', '.join(categories[:2]).lower()}.", 280)


def host_matches_domain(host: str, domain: str) -> bool:
    host = host.lower().split(":")[0]
    domain = domain.lower().lstrip("www.")
    return host == domain or host.endswith("." + domain)


def is_allowed_official_url(url: str, source: dict[str, Any]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.lower()
    allowed_hosts = set(source.get("allowed_hosts", []))
    if source.get("host"):
        allowed_hosts.add(source["host"])
    domain = source.get("domain", "")
    base_host = urlparse(source.get("base_url", "")).netloc.lower()
    if base_host:
        allowed_hosts.add(base_host)
    return (
        (domain and host_matches_domain(host, domain))
        or any(host == h or host.endswith("." + h) for h in allowed_hosts)
        or (any(hint in host for hint in ATS_HOST_HINTS) and bool(source.get("ats_verified")))
    )


def normalize_job(*, source: dict[str, Any], title: str, description: str, location: str,
                  url: str, external_id: str | None = None, posted_date: str | None = None,
                  deadline: str | None = None) -> dict[str, Any] | None:
    full_text = clean_text(f"{title} {description}")
    if not INTERN_TITLE_RE.search(title):
        return None
    categories = classify_roles(title, full_text)
    if not categories:
        return None
    grad = evaluate_graduation(full_text)
    if not grad.eligible:
        return None
    deadline = deadline or extract_deadline(full_text)
    if deadline_passed(deadline) or not is_allowed_official_url(url, source):
        return None
    return {
        "id": stable_id(source["company"], external_id, url),
        "source_key": source["key"],
        "fortune_rank": source.get("rank"),
        "company": source["company"],
        "title": clean_text(title),
        "location": clean_text(location) or "Location not listed",
        "region": infer_region(location),
        "opportunity_type": opportunity_type(title),
        "categories": categories,
        "tags": derive_tags(full_text),
        "summary": make_summary(description, categories),
        "grad_status": grad.status,
        "grad_evidence": grad.evidence,
        "freshman_friendly": freshman_friendly(full_text, grad),
        "posted_date": posted_date,
        "deadline": deadline,
        "url": url,
        "external_id": external_id,
        "verified_at": utc_iso(),
        "match_score": 85 + (5 if grad.status == "2029 eligible" else 0),
    }


def iter_json(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json(item)


def rank_from_dict(item: dict[str, Any]) -> int | None:
    for key in ("rank", "currentRank", "ranking", "rankValue", "position"):
        raw = item.get(key)
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("rank")
        try:
            rank = int(str(raw).replace("#", ""))
            if 1 <= rank <= 500:
                return rank
        except (TypeError, ValueError):
            pass
    return None


def name_from_dict(item: dict[str, Any]) -> str | None:
    for key in ("companyName", "organizationName", "company", "name", "title"):
        raw = item.get(key)
        if isinstance(raw, dict):
            raw = raw.get("name") or raw.get("value")
        if isinstance(raw, str) and 1 < len(raw) < 120 and not raw.lower().startswith(("fortune", "sector")):
            return clean_text(raw)
    return None


def iter_embedded_json_values(text: str) -> Iterator[Any]:
    """Yield JSON values embedded inside a larger text stream.

    Next.js React Server Component payloads are often valid JSON strings whose
    decoded contents contain many adjacent JSON objects rather than one outer
    JSON document. Scanning with ``JSONDecoder.raw_decode`` keeps rank/name
    fields inside the same object and avoids pairing a rank from one dashboard
    card with a company from the next card.
    """

    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        starts = [position for position in (text.find("{", index), text.find("[", index)) if position >= 0]
        if not starts:
            return
        start = min(starts)
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        yield value
        index = start + max(consumed, 1)


def decoded_next_payloads(script_text: str) -> Iterator[str]:
    """Decode string payloads passed to ``self.__next_f.push`` calls."""

    marker = "self.__next_f.push("
    search_at = 0
    decoder = json.JSONDecoder()
    while True:
        marker_at = script_text.find(marker, search_at)
        if marker_at < 0:
            return
        argument_at = marker_at + len(marker)
        try:
            argument, consumed = decoder.raw_decode(script_text[argument_at:])
        except json.JSONDecodeError:
            search_at = argument_at
            continue
        if isinstance(argument, list):
            for item in argument[1:]:
                if isinstance(item, str):
                    yield item
        search_at = argument_at + max(consumed, 1)


def extract_ranked_companies_from_html(page: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page, "html.parser")

    # A Fortune rank is not a unique company identifier: revenue ties can give
    # multiple companies the same rank. Keying this collection only by rank used
    # to overwrite tied companies and made a 500-company page look like 498 rows.
    candidates: dict[tuple[int, str], dict[str, Any]] = {}

    def add_candidate(rank: int, name: str) -> None:
        cleaned = clean_text(name)
        normalized = normalize_company_name(cleaned)
        if not (1 <= rank <= 500) or not normalized:
            return
        candidates.setdefault((rank, normalized), {"rank": rank, "company": cleaned})

    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or ("rank" not in raw.lower() and "company" not in raw.lower()):
            continue

        parsed_any = False
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            parsed_any = True
            for item in iter_json(parsed):
                if not isinstance(item, dict):
                    continue
                rank, name = rank_from_dict(item), name_from_dict(item)
                if rank and name:
                    add_candidate(rank, name)

        # Fortune uses Next.js flight payloads. Decode the strings passed to
        # self.__next_f.push and parse object-bounded JSON values. The previous
        # flat ``.{0,800}`` regex crossed object boundaries and produced exactly
        # the false 502-company/373-rank result reported in production.
        for payload in decoded_next_payloads(raw):
            parsed_any = True
            for embedded in iter_embedded_json_values(payload):
                for item in iter_json(embedded):
                    if not isinstance(item, dict):
                        continue
                    rank, name = rank_from_dict(item), name_from_dict(item)
                    if rank and name:
                        add_candidate(rank, name)

        # Some script tags contain decoded JSON fragments without a surrounding
        # Next.js push call. Parse those fragments object-by-object as a final,
        # object-safe fallback.
        if not parsed_any:
            for variant in (raw, html.unescape(raw)):
                for embedded in iter_embedded_json_values(variant):
                    for item in iter_json(embedded):
                        if not isinstance(item, dict):
                            continue
                        rank, name = rank_from_dict(item), name_from_dict(item)
                        if rank and name:
                            add_candidate(rank, name)

    # Visible ranking links are useful when the page server-renders rows.
    for anchor in soup.select('a[href*="/company/"]'):
        label = clean_text(anchor.get_text(" "))
        match = re.match(r"^(\d{1,3})\s+(.{2,100})$", label)
        if match:
            add_candidate(int(match.group(1)), match.group(2))

    return sorted(candidates.values(), key=lambda record: (record["rank"], record["company"].lower()))


def official_snapshot_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize whether one page contains a coherent Fortune 500 snapshot."""

    by_company: dict[str, set[int]] = {}
    rank_counts: dict[int, int] = {}
    for record in records:
        identity = normalize_company_name(record.get("company", ""))
        rank = record.get("rank")
        if not identity or not isinstance(rank, int) or not 1 <= rank <= 500:
            continue
        by_company.setdefault(identity, set()).add(rank)
        rank_counts[rank] = rank_counts.get(rank, 0) + 1

    multi_rank_companies = {
        company: sorted(ranks)
        for company, ranks in by_company.items()
        if len(ranks) > 1
    }
    tied_ranks = sorted(rank for rank, count in rank_counts.items() if count > 1)
    missing_ranks = sorted(set(range(1, 501)) - set(rank_counts))
    unique_company_count = len(by_company)
    distinct_rank_count = len(rank_counts)

    # Fortune can contain an occasional true tie, but a page yielding hundreds
    # of duplicate/missing ranks is a dashboard-data mix, not the ranked list.
    coherent = (
        unique_company_count == 500
        and len(records) == 500
        and not multi_rank_companies
        and distinct_rank_count >= 490
        and len(missing_ranks) <= 10
    )
    return {
        "coherent": coherent,
        "record_count": len(records),
        "unique_company_count": unique_company_count,
        "distinct_rank_count": distinct_rank_count,
        "tied_ranks": tied_ranks,
        "missing_ranks": missing_ranks,
        "multi_rank_companies": multi_rank_companies,
    }


def extract_directory_rows(page: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page, "html.parser")
    rows: dict[int, dict[str, Any]] = {}
    for tr in soup.select("tr"):
        cells = [clean_text(cell.get_text(" ")) for cell in tr.select("th,td")]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        rank = int(cells[0])
        if not 1 <= rank <= 500:
            continue
        company = cells[1]
        domain = ""
        for anchor in tr.select("a[href]"):
            host = urlparse(anchor.get("href", "")).netloc.lower().lstrip("www.")
            if host and not any(x in host for x in ("50pros.com", "linkedin.com")):
                domain = host
        if not domain:
            for cell in reversed(cells):
                match = re.search(r"\b([a-z0-9][a-z0-9.-]+\.[a-z]{2,})\b", cell, re.I)
                if match:
                    domain = match.group(1).lower().lstrip("www.")
                    break
        rows[rank] = {"rank": rank, "company": company, "domain": domain}

    # The directory currently renders one row per text line; support that layout too.
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        match = re.match(r"^(\d{1,3})\s+(.+?)\s+([a-z0-9][a-z0-9.-]+\.[a-z]{2,})$", line, re.I)
        if not match:
            continue
        rank = int(match.group(1))
        if not 1 <= rank <= 500 or rank in rows:
            continue
        domain = match.group(3).lower().lstrip("www.")
        middle = match.group(2)
        # Company is the leading text before common industry/revenue markers.
        company = re.split(r"\s+(?:Internet Services|General Merchandisers|Health Care|Computers|Wholesalers|Insurance|Petroleum|Commercial Banks|Telecommunications|Utilities|Aerospace|Pharmaceuticals|Food|Financial|Industrial|Motor Vehicles|Specialty Retailers|Transportation|Real Estate|Chemicals|Entertainment|Building|Engineering|Medical|Semiconductors|Securities|Airlines|Metals|Packaging|Apparel|Railroads|Pipelines|Energy|Homebuilders|Diversified|Network and Other|Scientific|Advertising|Waste Management)\b", middle, maxsplit=1)[0]
        rows[rank] = {"rank": rank, "company": company.strip(), "domain": domain}
    return [rows[r] for r in sorted(rows)]


class Fetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        timeout = kwargs.pop("timeout", 18)
        for attempt in range(2):
            try:
                response = self.session.request(method, url, timeout=timeout, allow_redirects=True, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(f"Failed request: {url}: {last_error}")

    def refresh_company_directory(self, config: dict[str, Any]) -> dict[str, Any]:
        official_url = config["official_list_url"]
        official_urls = list(dict.fromkeys([official_url, config.get("official_explorer_url", "")]))
        directory_url = config["domain_directory_url"]
        official: list[dict[str, Any]] = []
        official_source_url = ""
        official_source_diagnostics: dict[str, Any] = {}
        domains: list[dict[str, Any]] = []
        errors: list[str] = []

        # Treat each Fortune page as an independent snapshot. The main ranking
        # page and explorer contain overlapping company data plus dashboard-only
        # ranks (sector ranks, previous ranks, mover cards). Unioning them creates
        # a synthetic list that is not any real Fortune 500 edition.
        for candidate_url in filter(None, official_urls):
            try:
                records = extract_ranked_companies_from_html(self.request("GET", candidate_url, timeout=30).text)
                diagnostics = official_snapshot_diagnostics(records)
                if diagnostics["coherent"]:
                    official = records
                    official_source_url = candidate_url
                    official_source_diagnostics = diagnostics
                    break
                errors.append(
                    f"official list {candidate_url}: rejected incoherent snapshot "
                    f"({diagnostics['record_count']} records, "
                    f"{diagnostics['unique_company_count']} unique companies, "
                    f"{diagnostics['distinct_rank_count']} distinct ranks, "
                    f"{len(diagnostics['multi_rank_companies'])} companies with multiple ranks)"
                )
            except Exception as exc:
                errors.append(f"official list {candidate_url}: {exc}")

        try:
            domains = extract_directory_rows(self.request("GET", directory_url, timeout=30).text)
        except Exception as exc:
            errors.append(f"domain directory: {exc}")

        existing = read_json(DIRECTORY_PATH, {"companies": []})
        domain_by_name = {normalize_company_name(row["company"]): row.get("domain", "") for row in domains}
        existing_domains = {normalize_company_name(row.get("company", "")): row.get("domain", "") for row in existing.get("companies", [])}

        rank_counts: dict[int, int] = {}
        for record in official:
            rank_counts[record["rank"]] = rank_counts.get(record["rank"], 0) + 1
        tied_ranks = sorted(rank for rank, count in rank_counts.items() if count > 1)
        missing_ranks = sorted(set(range(1, 501)) - set(rank_counts))

        if official and official_source_diagnostics.get("coherent"):
            source = "Official Fortune 500 ranking page"
            members = official
            if tied_ranks:
                errors.append(
                    "Fortune contains tied ranks " + ", ".join(map(str, tied_ranks)) +
                    "; duplicate rank numbers are preserved as separate companies"
                )
        elif len(existing.get("companies", [])) == 500 and existing.get("membership_verified_from_fortune"):
            # Keep the last known official directory rather than substituting a
            # different revenue ranking that merely contains 500 companies.
            existing["refresh_error"] = "; ".join(errors) or f"official page yielded {len(official)} ranked records"
            existing["refresh_attempted_at"] = utc_iso()
            DIRECTORY_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return existing
        else:
            diagnostics = (
                f"official page yielded {len(official)} unique companies across "
                f"{len(rank_counts)} distinct rank values"
            )
            if tied_ranks:
                diagnostics += f"; tied ranks: {tied_ranks}"
            if missing_ranks:
                diagnostics += f"; absent rank values: {missing_ranks[:20]}"
                if len(missing_ranks) > 20:
                    diagnostics += "…"
            detail = "; ".join([diagnostics, *errors])
            raise RuntimeError(
                "Could not verify all 500 members from Fortune's official ranking page. "
                "The scanner will not silently replace it with a non-Fortune ranking. " + detail
            )

        companies = []
        used_keys: set[str] = set()
        for member in members:
            name = member["company"]
            normalized = normalize_company_name(name)
            domain = domain_by_name.get(normalized) or existing_domains.get(normalized) or ""
            key = slugify(name)
            if key in used_keys:
                key = f"{key}-{member['rank']}"
            used_keys.add(key)
            companies.append({"rank": member["rank"], "company": name, "key": key, "domain": domain})

        payload = {
            "year": config.get("fortune_year", 2026),
            "record_count": len(companies),
            "source": source,
            "membership_verified_from_fortune": True,
            "official_record_count": len(official),
            "official_distinct_rank_count": len(rank_counts),
            "official_tied_ranks": tied_ranks,
            "official_missing_rank_values": missing_ranks,
            "official_list_url": official_source_url or official_url,
            "official_candidate_urls": official_urls,
            "domain_directory_url": directory_url,
            "refreshed_at": utc_iso(),
            "refresh_warnings": errors,
            "companies": companies,
        }
        DIRECTORY_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return payload

    def fetch_workday(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        host, tenant, site = source["host"], source["tenant"], source["site"]
        endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        postings: dict[str, dict[str, Any]] = {}
        for query in SEARCH_TERMS:
            offset = 0
            while offset < 120:
                data = self.request("POST", endpoint, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query}).json()
                page = data.get("jobPostings", [])
                for posting in page:
                    path = posting.get("externalPath")
                    if path:
                        postings[path] = posting
                offset += len(page)
                if not page or offset >= int(data.get("total", 0)):
                    break
        jobs: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._workday_detail, source, path, posting): path for path, posting in postings.items()}
            for future in as_completed(futures):
                try:
                    job = future.result()
                    if job:
                        jobs.append(job)
                except Exception as exc:
                    logging.debug("Workday detail failed for %s: %s", futures[future], exc)
        return jobs

    def _workday_detail(self, source: dict[str, Any], path: str, listing: dict[str, Any]) -> dict[str, Any] | None:
        host, tenant, site = source["host"], source["tenant"], source["site"]
        data = self.request("GET", f"https://{host}/wday/cxs/{tenant}/{site}{path}").json().get("jobPostingInfo", {})
        external_url = data.get("externalUrl") or f"https://{host}/en-US/{site}{path}"
        return normalize_job(
            source=source,
            title=data.get("title") or listing.get("title") or "",
            description=data.get("jobDescription") or "",
            location=data.get("location") or listing.get("locationsText") or "",
            url=external_url,
            external_id=data.get("jobReqId"),
            posted_date=parse_flexible_date(data.get("postedOn") or listing.get("postedOn")),
            deadline=parse_flexible_date(data.get("endDate")),
        )

    def fetch_amazon_html(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        links: set[str] = set()
        for query in ("intern", "software engineer internship", "business analyst intern", "product manager intern", "data analyst intern", "operations intern"):
            url = f"{source['base_url']}/en/search?base_query={quote_plus(query)}&loc_query=United+States&sort=recent"
            soup = BeautifulSoup(self.request("GET", url).text, "html.parser")
            for anchor in soup.select('a[href*="/en/jobs/"]'):
                href = anchor.get("href", "")
                if re.search(r"/en/jobs/\d+", href):
                    links.add(urljoin(source["base_url"], href.split("?")[0]))
        return self.fetch_generic_job_pages(source, links)

    def fetch_greenhouse(self, source: dict[str, Any], board: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true").json()
        jobs = []
        source = {**source, "allowed_hosts": [*source.get("allowed_hosts", []), "greenhouse.io"], "ats_verified": True}
        for item in data.get("jobs", []):
            job = normalize_job(
                source=source,
                title=item.get("title", ""),
                description=item.get("content", ""),
                location=(item.get("location") or {}).get("name", ""),
                url=item.get("absolute_url", ""),
                external_id=str(item.get("id", "")),
                posted_date=parse_flexible_date(item.get("updated_at")),
            )
            if job:
                jobs.append(job)
        return jobs

    def fetch_lever(self, source: dict[str, Any], site: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"https://api.lever.co/v0/postings/{site}?mode=json").json()
        jobs = []
        source = {**source, "allowed_hosts": [*source.get("allowed_hosts", []), "lever.co"], "ats_verified": True}
        for item in data:
            description = " ".join(filter(None, [item.get("descriptionPlain"), item.get("additionalPlain")]))
            cats = item.get("categories") or {}
            job = normalize_job(
                source=source,
                title=item.get("text", ""),
                description=description,
                location=cats.get("location", ""),
                url=item.get("hostedUrl", ""),
                external_id=item.get("id"),
                posted_date=parse_flexible_date(item.get("createdAt") and datetime.fromtimestamp(item["createdAt"] / 1000, timezone.utc).isoformat()),
            )
            if job:
                jobs.append(job)
        return jobs

    def fetch_ashby(self, source: dict[str, Any], board: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true").json()
        jobs = []
        source = {**source, "allowed_hosts": [*source.get("allowed_hosts", []), "ashbyhq.com"], "ats_verified": True}
        for item in data.get("jobs", []):
            job = normalize_job(
                source=source,
                title=item.get("title", ""),
                description=item.get("descriptionPlain") or item.get("descriptionHtml") or "",
                location=item.get("location", ""),
                url=item.get("jobUrl") or item.get("applyUrl") or "",
                external_id=item.get("id"),
                posted_date=parse_flexible_date(item.get("publishedAt")),
            )
            if job:
                jobs.append(job)
        return jobs

    def fetch_smartrecruiters(self, source: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
        jobs = []
        offset = 0
        source = {**source, "allowed_hosts": [*source.get("allowed_hosts", []), "smartrecruiters.com"], "ats_verified": True}
        while offset < 300:
            data = self.request("GET", f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings?limit=100&offset={offset}").json()
            content = data.get("content", [])
            for item in content:
                title = item.get("name", "")
                if not INTERN_TITLE_RE.search(title):
                    continue
                detail = self.request("GET", f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings/{item['id']}").json()
                sections = detail.get("jobAd", {}).get("sections", {})
                description = " ".join(clean_text(section.get("text")) for section in sections.values() if isinstance(section, dict))
                location_obj = item.get("location") or {}
                location = ", ".join(filter(None, [location_obj.get("city"), location_obj.get("region"), location_obj.get("country")]))
                job = normalize_job(
                    source=source, title=title, description=description, location=location,
                    url=detail.get("applyUrl") or detail.get("ref") or "", external_id=item.get("id"),
                    posted_date=parse_flexible_date(item.get("releasedDate")),
                )
                if job:
                    jobs.append(job)
            offset += len(content)
            if not content or offset >= int(data.get("totalFound", 0)):
                break
        return jobs

    def fetch_generic_job_pages(self, source: dict[str, Any], urls: Iterable[str]) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(self._generic_job_detail, source, url): url for url in list(dict.fromkeys(urls))[:40]}
            for future in as_completed(futures):
                try:
                    job = future.result()
                    if job:
                        jobs.append(job)
                except Exception as exc:
                    logging.debug("Generic job page failed %s: %s", futures[future], exc)
        return jobs

    def _generic_job_detail(self, source: dict[str, Any], url: str) -> dict[str, Any] | None:
        response = self.request("GET", url, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
        title_node = soup.select_one("h1") or soup.select_one('[class*="job-title" i]') or soup.select_one("title")
        title = clean_text(title_node.get_text(" ") if title_node else "")
        body = soup.select_one("main") or soup.select_one('[class*="job-description" i]') or soup.body
        description = clean_text(body.get_text(" ", strip=True) if body else "")
        location = ""
        for selector in ('[class*="location" i]', '[data-automation-id="locations"]'):
            node = soup.select_one(selector)
            if node:
                location = clean_text(node.get_text(" "))
                break
        canonical = soup.select_one('link[rel="canonical"]')
        final_url = urljoin(response.url, canonical.get("href")) if canonical and canonical.get("href") else response.url
        return normalize_job(source=source, title=title, description=description, location=location, url=final_url)

    def discover_career_pages(self, company: dict[str, Any]) -> list[str]:
        domain = company.get("domain", "")
        if not domain:
            return []
        seeds = [f"https://{domain}", f"https://www.{domain}"]
        candidates: list[str] = []
        for seed in seeds:
            try:
                response = self.request("GET", seed, timeout=12)
                soup = BeautifulSoup(response.text, "html.parser")
                for anchor in soup.select("a[href]"):
                    label = clean_text(anchor.get_text(" "))
                    href = urljoin(response.url, anchor.get("href", ""))
                    if CAREER_WORDS.search(label) or CAREER_WORDS.search(urlparse(href).path):
                        candidates.append(href)
                if candidates:
                    break
            except Exception:
                continue
        candidates.extend(f"https://{domain}/{path}" for path in ("careers", "jobs", "careers/jobs"))
        return list(dict.fromkeys(candidates))[:8]

    def discover_ats(self, source: dict[str, Any], career_pages: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        ats: list[tuple[str, str]] = []
        generic_links: list[str] = []
        allowed_hosts: set[str] = set(source.get("allowed_hosts", []))
        for page_url in career_pages[:6]:
            try:
                response = self.request("GET", page_url, timeout=15)
            except Exception:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            page_host = urlparse(response.url).netloc.lower()
            if any(hint in page_host for hint in ATS_HOST_HINTS):
                allowed_hosts.add(page_host)
            for anchor in soup.select("a[href]"):
                href = urljoin(response.url, anchor.get("href", ""))
                label = clean_text(anchor.get_text(" "))
                host = urlparse(href).netloc.lower()
                if any(hint in host for hint in ATS_HOST_HINTS):
                    allowed_hosts.add(host)
                if JOB_LINK_WORDS.search(label) or JOB_LINK_WORDS.search(href):
                    generic_links.append(href)
                parsed = self.parse_ats_url(href)
                if parsed:
                    ats.append(parsed)
        source["allowed_hosts"] = sorted(allowed_hosts)
        source["ats_verified"] = bool(allowed_hosts)
        return list(dict.fromkeys(ats)), list(dict.fromkeys(generic_links))

    @staticmethod
    def parse_ats_url(url: str) -> tuple[str, str] | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        parts = [part for part in parsed.path.split("/") if part]
        if "boards.greenhouse.io" in host and parts:
            return ("greenhouse", parts[0])
        if "jobs.lever.co" in host and parts:
            return ("lever", parts[0])
        if "jobs.ashbyhq.com" in host and parts:
            return ("ashby", parts[0])
        if "smartrecruiters.com" in host and len(parts) >= 2 and parts[0].lower() in ("company", "careers"):
            return ("smartrecruiters", parts[1])
        if "myworkdayjobs.com" in host:
            locale_index = next((i for i, p in enumerate(parts) if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)), -1)
            site = parts[locale_index + 1] if locale_index >= 0 and len(parts) > locale_index + 1 else (parts[0] if parts else "")
            tenant = host.split(".")[0]
            if site:
                return ("workday", json.dumps({"host": host, "tenant": tenant, "site": site}))
        return None

    def public_search_links(self, company: dict[str, Any]) -> list[str]:
        if os.getenv("ENABLE_PUBLIC_SEARCH", "1") != "1":
            return []
        query = f'"{company["company"]}" (intern OR internship OR co-op) careers'
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            soup = BeautifulSoup(self.request("GET", url, timeout=15).text, "html.parser")
        except Exception:
            return []
        links: list[str] = []
        for anchor in soup.select("a.result__a[href], a[href]"):
            href = anchor.get("href", "")
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs:
                href = unquote(qs["uddg"][0])
            if href.startswith("http"):
                host = urlparse(href).netloc.lower()
                if (company.get("domain") and host_matches_domain(host, company["domain"])) or any(h in host for h in ATS_HOST_HINTS):
                    links.append(href)
        return list(dict.fromkeys(links))[:12]

    def scan_company(self, company: dict[str, Any], override: dict[str, Any] | None = None) -> ScanResult:
        source = {**company}
        source.setdefault("key", slugify(source["company"]))
        if override:
            source.update(override)
            source["rank"] = company.get("rank")
            source["domain"] = source.get("domain") or company.get("domain", "")
        started = time.time()
        try:
            if source.get("adapter"):
                jobs = getattr(self, f"fetch_{source['adapter']}")(source)
                return ScanResult(source, jobs, "ok", adapter=source["adapter"])

            career_pages = self.discover_career_pages(source)
            ats_sources, generic_links = self.discover_ats(source, career_pages)
            if not ats_sources and not generic_links:
                searched = self.public_search_links(source)
                ats_sources = [parsed for url in searched if (parsed := self.parse_ats_url(url))]
                generic_links.extend(searched)
                for url in searched:
                    host = urlparse(url).netloc.lower()
                    if any(hint in host for hint in ATS_HOST_HINTS):
                        source.setdefault("allowed_hosts", []).append(host)
                        source["ats_verified"] = True

            jobs: list[dict[str, Any]] = []
            for adapter, identifier in ats_sources[:4]:
                if adapter == "workday":
                    params = json.loads(identifier)
                    jobs.extend(self.fetch_workday({**source, **params}))
                else:
                    jobs.extend(getattr(self, f"fetch_{adapter}")(source, identifier))
            if generic_links:
                jobs.extend(self.fetch_generic_job_pages(source, generic_links))
            status = "ok" if career_pages or ats_sources or generic_links else "no-career-source"
            careers_url = career_pages[0] if career_pages else None
            logging.debug("%s scan %.1fs via %s", source["company"], time.time() - started, ats_sources or "discovery")
            return ScanResult(source, dedupe(jobs), status, careers_url=careers_url, adapter="discovery")
        except Exception as exc:
            return ScanResult(source, [], "error", error=str(exc)[:350], adapter=source.get("adapter", "discovery"))


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_existing() -> dict[str, Any]:
    return read_json(DATA_PATH, {"updated_at": None, "opportunities": []})


def load_overrides() -> dict[str, dict[str, Any]]:
    records = read_json(OVERRIDES_PATH, [])
    return {record["key"]: record for record in records if record.get("enabled", True)}


def choose_companies(directory: list[dict[str, Any]], state: dict[str, Any], batch_size: int, full_sweep: bool,
                     requested: set[str], priority_keys: set[str]) -> list[dict[str, Any]]:
    if requested:
        return [c for c in directory if c["key"] in requested or str(c.get("rank")) in requested]
    if full_sweep:
        return directory
    company_state = state.get("companies", {})
    def checked_time(company: dict[str, Any]) -> datetime:
        raw = company_state.get(company["key"], {}).get("checked_at")
        try:
            dt = date_parser.parse(raw) if raw else datetime(1970, 1, 1, tzinfo=timezone.utc)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return datetime(1970, 1, 1, tzinfo=timezone.utc)

    # Never-scanned companies come first. Direct adapters break ties only on the
    # first sweep; after that every company is ordered strictly by oldest check.
    ordered = sorted(
        directory,
        key=lambda c: (
            checked_time(c),
            0 if c["key"] in priority_keys else 1,
            c["rank"],
        ),
    )
    return ordered[:batch_size]


def retain_existing_jobs(existing: dict[str, Any], selected: set[str], successes: set[str], failures: set[str]) -> list[dict[str, Any]]:
    keep: list[dict[str, Any]] = []
    for job in existing.get("opportunities", []):
        key = job.get("source_key")
        if key in successes:
            continue
        try:
            verified = date_parser.parse(job.get("verified_at", ""))
            if verified.tzinfo is None:
                verified = verified.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        max_age = timedelta(hours=72 if key in failures else 40)
        if key not in selected or key in failures:
            if verified >= NOW - max_age:
                if key in failures:
                    job["source_stale"] = True
                keep.append(job)
    return keep


def dedupe(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for job in jobs:
        key = job.get("url") or job["id"]
        if key not in by_key or job.get("verified_at", "") > by_key[key].get("verified_at", ""):
            by_key[key] = job
    return sorted(by_key.values(), key=lambda item: (item.get("posted_date") or "", item["company"], item["title"]), reverse=True)


def write_feed(jobs: list[dict[str, Any]]) -> None:
    items = []
    for job in jobs[:100]:
        pub_date = parse_flexible_date(job.get("posted_date")) or NOW.date().isoformat()
        try:
            rss_date = date_parser.parse(pub_date).strftime("%a, %d %b %Y 12:00:00 +0000")
        except ValueError:
            rss_date = NOW.strftime("%a, %d %b %Y %H:%M:%S +0000")
        description = html.escape(f"{job['location']} — {job['grad_status']}. {job['summary']}")
        items.append(f"""    <item>
      <title>{html.escape(job['company'] + ' — ' + job['title'])}</title>
      <link>{html.escape(job['url'])}</link>
      <guid isPermaLink="false">{html.escape(job['id'])}</guid>
      <pubDate>{rss_date}</pubDate>
      <description>{description}</description>
    </item>""")
    FEED_PATH.write_text(f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel>
<title>LaunchList — 2029-Eligible Fortune 500 Internships</title>
<link>https://example.github.io/launchlist/</link>
<description>Official internship postings screened for 2029 graduation eligibility.</description>
<lastBuildDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>
{chr(10).join(items)}
</channel></rss>
""", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full-sweep", action="store_true", help="Scan all 500 companies in one run")
    parser.add_argument("--refresh-directory-only", action="store_true")
    parser.add_argument("--source", action="append", help="Company key or rank; may be repeated")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    config = read_json(SCANNER_CONFIG_PATH, {})
    fetcher = Fetcher()
    try:
        directory_payload = fetcher.refresh_company_directory(config)
    except Exception as exc:
        logging.error("Company directory refresh failed: %s", exc)
        return 2
    companies = directory_payload.get("companies", [])
    if len(companies) != 500:
        logging.error("Refusing to claim Fortune 500 coverage: directory contains %d records", len(companies))
        return 2
    if args.refresh_directory_only:
        print(json.dumps({k: v for k, v in directory_payload.items() if k != "companies"}, indent=2))
        return 0

    overrides = load_overrides()
    # Match overrides to refreshed company names when their historical key differs.
    override_by_name = {normalize_company_name(v.get("company", "")): v for v in overrides.values()}
    for company in companies:
        if company["key"] not in overrides:
            candidate = override_by_name.get(normalize_company_name(company["company"]))
            if candidate:
                overrides[company["key"]] = candidate

    scan_state = read_json(SCAN_STATE_PATH, {"companies": {}})
    batch_size = args.batch_size or int(config.get("hourly_batch_size", 35))
    selected = choose_companies(companies, scan_state, batch_size, args.full_sweep, set(args.source or []), set(overrides))
    logging.info("Scanning %d of 500 companies this run", len(selected))

    results: list[ScanResult] = []
    workers = min(int(config.get("company_workers", 6)), max(1, len(selected)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetcher.scan_company, company, overrides.get(company["key"])): company for company in selected}
        for future in as_completed(futures):
            company = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = ScanResult(company, [], "error", error=str(exc)[:350])
            results.append(result)
            logging.info("#%-3s %-28s %-16s %3d matches", company.get("rank", ""), company["company"][:28], result.status, len(result.jobs))

    existing = read_existing()
    fresh_jobs = [job for result in results for job in result.jobs]
    selected_keys = {r.company["key"] for r in results}
    success_keys = {r.company["key"] for r in results if r.status == "ok"}
    failure_keys = {r.company["key"] for r in results if r.status in ("error", "no-career-source")}
    jobs = dedupe([*fresh_jobs, *retain_existing_jobs(existing, selected_keys, success_keys, failure_keys)])

    company_state = scan_state.setdefault("companies", {})
    for result in results:
        company_state[result.company["key"]] = {
            "rank": result.company.get("rank"),
            "company": result.company["company"],
            "checked_at": utc_iso(),
            "status": result.status,
            "eligible_jobs": len(result.jobs),
            "adapter": result.adapter,
            "careers_url": result.careers_url,
            "error": result.error,
        }
    scan_state["updated_at"] = utc_iso()

    cutoff_24h = NOW - timedelta(hours=24)
    scanned_24h = 0
    failed_24h = 0
    for record in company_state.values():
        try:
            checked = date_parser.parse(record.get("checked_at", ""))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            if checked >= cutoff_24h:
                scanned_24h += 1
                failed_24h += record.get("status") in ("error", "no-career-source")
        except (ValueError, TypeError):
            pass

    payload = {
        "updated_at": utc_iso(),
        "eligibility_policy": "2029 explicitly allowed, an allowed range reaches 2029, 'or later' includes 2029, or no graduation year is listed",
        "fortune500_year": directory_payload.get("year"),
        "fortune500_company_count": 500,
        "companies_in_scan_queue": len(companies),
        "directory_source": directory_payload.get("source"),
        "directory_warnings": directory_payload.get("refresh_warnings", []),
        "directory_refreshed_at": directory_payload.get("refreshed_at"),
        "companies_scanned_this_run": len(results),
        "companies_scanned_last_24h": scanned_24h,
        "companies_failed_last_24h": failed_24h,
        "companies_with_eligible_jobs": len({job["source_key"] for job in jobs}),
        "estimated_full_cycle_hours": round(500 / max(1, batch_size), 1),
        "opportunity_count": len(jobs),
        "opportunities": jobs,
    }

    if args.dry_run:
        print(json.dumps({k: v for k, v in payload.items() if k != "opportunities"}, indent=2))
        return 0

    DATA_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    SCAN_STATE_PATH.write_text(json.dumps(scan_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_feed(jobs)
    logging.info("Wrote %d eligible opportunities; %d/500 companies scanned in the last 24h", len(jobs), scanned_24h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
