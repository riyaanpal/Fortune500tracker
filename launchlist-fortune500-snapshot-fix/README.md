# LaunchList

LaunchList is a deployable internship tracker built for a **2029 graduate**. It searches Fortune 500 employer career sites for:

- Tech consulting and digital transformation
- Product management and product operations
- Wealth management
- Software engineering
- Business, data, operations, and IT analysis
- Startup and growth analytics

## What changed in the full-coverage version

The original starter build had direct adapters for only 11 employers. This version uses a **500-company rolling queue** instead:

- The current Fortune 500 directory is refreshed at runtime.
- All 500 companies are placed in the scan queue.
- GitHub Actions runs hourly and checks 50 companies per run.
- A normal full rotation is therefore about 10 hours.
- Direct ATS adapters are used for known boards; other companies go through career-page and ATS discovery.
- The site separately displays companies queued, actually checked in the last 24 hours, failed or blocked, and companies with eligible matches.
- A company is never counted as checked merely because it exists in the directory.

The directory refresh requires one coherent 500-company snapshot extracted from Fortune's current ranking pages. The main ranking page is tried first and the explorer is used only as a fallback. Their records are never unioned. The secondary filing-based directory is used only to enrich verified members with corporate domains; it is never substituted for Fortune membership. Fortune may assign the same rank number to more than one company, so the parser verifies 500 unique companies while also rejecting pages with implausibly large numbers of rank collisions.

## Fix for “official page yielded 498 ranked records”

Earlier builds used the Fortune rank number as a unique dictionary key. When two companies shared a rank, one overwrote the other, reducing 500 companies to 498 rank-keyed records. The fixed parser:

- keeps each company as a separate record even when its rank is tied;
- validates each Fortune page as a separate snapshot instead of merging pages;
- verifies exactly 500 unique companies, not 500 unique rank numbers; and
- records tied and skipped rank values in directory metadata for diagnostics.

After replacing the project files, rerun **Actions → Refresh internships and deploy → Run workflow**. The directory-only command can be used to confirm the repair before a scan:

```bash
python scripts/update_jobs.py --refresh-directory-only
```

A successful response can report `official_record_count: 500` while `official_distinct_rank_count` is lower because tied ranks are valid.

## Fix for “502 unique companies across 373 distinct rank values”

The explorer page contains many rank-like values that are not the full-list rank, including sector positions, previous ranks, and biggest-mover cards. An earlier fallback regex could pair one of those values with a nearby company name, and the refresh then unioned results from the main ranking page and explorer. That created a synthetic 502-company list with hundreds of false rank conflicts.

The current parser:

- decodes Next.js `self.__next_f.push` payloads;
- parses JSON objects without crossing object boundaries;
- validates the main ranking page and explorer independently;
- chooses the first coherent 500-company snapshot; and
- rejects snapshots with companies assigned multiple ranks or with excessive missing and duplicate rank values.

After replacing the patched files, rerun:

```bash
python scripts/update_jobs.py --refresh-directory-only
```

Do not combine an older `update_jobs.py` with the new tests; replace both files from the same build.

## Eligibility logic

A job is published only when one of these is true:

1. The graduation requirement explicitly includes **2029**.
2. A stated graduation range reaches **2029**.
3. The requirement says a qualifying year **“or later”** and therefore includes 2029.
4. The posting states no graduation year.

A posting with an explicit graduation range that excludes 2029 is rejected. The script evaluates text near graduation or degree-conferral language so a phrase such as “Summer 2027 internship” is not mistaken for a graduation requirement.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests
python scripts/update_jobs.py
python -m http.server 8000
```

Then open `http://localhost:8000`.

Useful scanner commands:

```bash
# Check the next 50 companies in the rolling queue
python scripts/update_jobs.py

# Check a specific rank or normalized company key
python scripts/update_jobs.py --source 1 --dry-run --verbose
python scripts/update_jobs.py --source amazon --dry-run --verbose

# Force one long scan of the entire directory
python scripts/update_jobs.py --full-sweep

# Refresh only the 500-company directory
python scripts/update_jobs.py --refresh-directory-only
```

## Deploy with automatic updates

1. Create a GitHub repository and upload this folder to its `main` branch.
2. In **Settings → Pages**, set **Source** to **GitHub Actions**.
3. Open **Actions** and run **Refresh internships and deploy** once.
4. Leave `full_sweep` off for the normal 50-company rolling scan, or enable it for an initial long sweep.
5. The scheduled workflow runs hourly, commits the directory, scan state, postings, and RSS feed, then redeploys the website.

Scheduled GitHub workflows can run later than their exact cron minute during high demand. Career sites can also throttle or block automation, which is why failures are shown rather than hidden.

## Architecture

- `data/fortune500_companies.json` — refreshed 500-company directory
- `data/company_scan_state.json` — per-company last check, result, adapter, and error
- `data/opportunities.json` — eligible listings plus coverage metadata
- `config/companies.json` — direct employer/ATS overrides
- `config/scanner.json` — directory sources, batch size, and worker count
- `scripts/update_jobs.py` — directory refresh, rotating scanner, role matching, graduation screening, and RSS generation
- `.github/workflows/update-and-deploy.yml` — hourly automation and GitHub Pages deployment

## Important limitations

- The packaged postings are a manually verified sample. Live 500-company coverage begins only after the updater runs in an internet-connected deployment.
- Some employer sites rely on JavaScript, CAPTCHAs, or anti-bot controls. Those companies remain in the queue and are reported as blocked or failed rather than silently treated as covered.
- “No graduation year listed” does not guarantee freshman eligibility; class standing, degree, work authorization, and location requirements can still disqualify an applicant.
- Employers can revise requirements after a scan. Re-check the official posting immediately before applying.
