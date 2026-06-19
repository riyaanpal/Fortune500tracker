const state = {
  jobs: [],
  role: "All roles",
  search: "",
  type: "",
  region: "",
  grad: "",
  sort: "newest",
  freshman: false,
  savedOnly: false,
  saved: new Set(JSON.parse(localStorage.getItem("launchlist:saved") || "[]")),
  seen: new Set(JSON.parse(localStorage.getItem("launchlist:seen") || "[]")),
};

const roles = [
  "All roles", "Tech consulting", "Product management", "Wealth management",
  "Software engineering", "Business analyst", "Data analyst", "Operations analyst",
  "IT analyst", "Startup analytics", "Digital transformation", "Product operations"
];

const els = {
  grid: document.querySelector("#jobGrid"),
  template: document.querySelector("#jobCardTemplate"),
  search: document.querySelector("#searchInput"),
  roleStrip: document.querySelector("#roleStrip"),
  resultCount: document.querySelector("#resultCount"),
  newCount: document.querySelector("#newCount"),
  empty: document.querySelector("#emptyState"),
  filterPanel: document.querySelector("#filterPanel"),
  filterToggle: document.querySelector("#filterToggle"),
  activeFilterCount: document.querySelector("#activeFilterCount"),
  type: document.querySelector("#typeFilter"),
  region: document.querySelector("#regionFilter"),
  grad: document.querySelector("#gradFilter"),
  sort: document.querySelector("#sortFilter"),
  freshman: document.querySelector("#freshmanFilter"),
  savedOnly: document.querySelector("#savedOnlyFilter"),
  savedCount: document.querySelector("#savedCount"),
  heroJobCount: document.querySelector("#heroJobCount"),
  heroCompanyCount: document.querySelector("#heroCompanyCount"),
  lastUpdated: document.querySelector("#lastUpdated"),
  coverageQueued: document.querySelector("#coverageQueued"),
  coverageScanned: document.querySelector("#coverageScanned"),
  coverageFailed: document.querySelector("#coverageFailed"),
  coverageMatched: document.querySelector("#coverageMatched"),
  coverageCycle: document.querySelector("#coverageCycle"),
  directorySource: document.querySelector("#directorySource"),
  coverageDisclosure: document.querySelector("#coverageDisclosure"),
};

function titleCaseInitials(name) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0]).join("").toUpperCase();
}

function parseDate(value) {
  if (!value) return null;
  const date = new Date(value.includes("T") ? value : `${value}T12:00:00`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value, fallback = "Not listed") {
  const date = parseDate(value);
  return date ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date) : fallback;
}

function relativeTime(value) {
  const date = parseDate(value);
  if (!date) return "recently";
  const hours = Math.max(0, Math.round((Date.now() - date.getTime()) / 36e5));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(value);
}

function matchesRegion(job) {
  if (!state.region) return true;
  const location = `${job.location || ""} ${job.region || ""}`.toLowerCase();
  if (state.region === "United States") return job.region === "United States" || /\b(usa|united states|remote - us)\b/i.test(location);
  if (state.region === "Remote") return /remote/i.test(location);
  return job.region && job.region !== "United States";
}

function matchScore(job) {
  let score = Number(job.match_score || 0);
  if (job.freshman_friendly) score += 8;
  if (job.grad_status === "2029 eligible") score += 4;
  if ((job.categories || []).includes(state.role)) score += 15;
  const posted = parseDate(job.posted_date);
  if (posted) score += Math.max(0, 10 - Math.floor((Date.now() - posted) / 864e5));
  return score;
}

function filteredJobs() {
  const q = state.search.trim().toLowerCase();
  let result = state.jobs.filter(job => {
    const haystack = [job.company, job.title, job.location, job.summary, ...(job.categories || []), ...(job.tags || [])].join(" ").toLowerCase();
    return (!q || haystack.includes(q))
      && (state.role === "All roles" || (job.categories || []).includes(state.role))
      && (!state.type || job.opportunity_type === state.type)
      && matchesRegion(job)
      && (!state.grad || job.grad_status === state.grad)
      && (!state.freshman || job.freshman_friendly)
      && (!state.savedOnly || state.saved.has(job.id));
  });

  result.sort((a, b) => {
    if (state.sort === "company") return a.company.localeCompare(b.company);
    if (state.sort === "deadline") {
      const ad = parseDate(a.deadline)?.getTime() || Number.MAX_SAFE_INTEGER;
      const bd = parseDate(b.deadline)?.getTime() || Number.MAX_SAFE_INTEGER;
      return ad - bd;
    }
    if (state.sort === "match") return matchScore(b) - matchScore(a);
    return (parseDate(b.posted_date)?.getTime() || 0) - (parseDate(a.posted_date)?.getTime() || 0);
  });
  return result;
}

function toggleSave(id) {
  state.saved.has(id) ? state.saved.delete(id) : state.saved.add(id);
  localStorage.setItem("launchlist:saved", JSON.stringify([...state.saved]));
  render();
}

function renderRoles() {
  els.roleStrip.innerHTML = "";
  roles.forEach(role => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `role-chip${state.role === role ? " active" : ""}`;
    button.textContent = role;
    button.addEventListener("click", () => { state.role = role; renderRoles(); render(); });
    els.roleStrip.append(button);
  });
}

function renderCard(job) {
  const card = els.template.content.firstElementChild.cloneNode(true);
  const isNew = !state.seen.has(job.id) && parseDate(job.posted_date) && ((Date.now() - parseDate(job.posted_date)) / 864e5 <= 7);
  if (isNew) card.classList.add("is-new");

  card.querySelector(".company-logo").textContent = titleCaseInitials(job.company);
  card.querySelector(".company-name").textContent = job.company;
  card.querySelector(".job-title").textContent = job.title;
  card.querySelector(".job-meta").textContent = `${job.location || "Location not listed"} · ${job.opportunity_type || "Internship"}`;
  card.querySelector(".job-summary").textContent = job.summary || "Review the official posting for full responsibilities and qualifications.";
  card.querySelector(".eligibility-title").textContent = job.grad_status;
  card.querySelector(".eligibility-detail").textContent = job.grad_evidence || "No conflicting graduation year found.";

  const tags = card.querySelector(".tag-row");
  [...new Set([...(job.categories || []).slice(0, 2), ...(job.tags || []).slice(0, 2)])].slice(0, 4).forEach(tag => {
    const span = document.createElement("span");
    span.className = "tag";
    span.textContent = tag;
    tags.append(span);
  });

  const dateLabel = card.querySelector(".date-label");
  const dateValue = card.querySelector(".date-value");
  if (job.deadline) {
    dateLabel.textContent = "Deadline";
    dateValue.textContent = formatDate(job.deadline);
  } else {
    dateLabel.textContent = "Posted";
    dateValue.textContent = relativeTime(job.posted_date);
  }

  const link = card.querySelector(".apply-button");
  link.href = job.url;
  link.addEventListener("click", () => {
    const applied = new Set(JSON.parse(localStorage.getItem("launchlist:opened") || "[]"));
    applied.add(job.id);
    localStorage.setItem("launchlist:opened", JSON.stringify([...applied]));
  });

  const save = card.querySelector(".save-button");
  save.classList.toggle("saved", state.saved.has(job.id));
  save.setAttribute("aria-label", state.saved.has(job.id) ? "Remove saved opportunity" : "Save opportunity");
  save.addEventListener("click", () => toggleSave(job.id));
  return card;
}

function activeFilterTotal() {
  return [state.role !== "All roles", state.search, state.type, state.region, state.grad, state.sort !== "newest", state.freshman, state.savedOnly].filter(Boolean).length;
}

function render() {
  const jobs = filteredJobs();
  els.grid.innerHTML = "";
  jobs.forEach(job => els.grid.append(renderCard(job)));
  els.resultCount.textContent = jobs.length;
  const newJobs = jobs.filter(job => !state.seen.has(job.id) && parseDate(job.posted_date) && ((Date.now() - parseDate(job.posted_date)) / 864e5 <= 7)).length;
  els.newCount.textContent = newJobs ? `· ${newJobs} new this week` : "";
  els.empty.hidden = jobs.length > 0;
  els.grid.hidden = jobs.length === 0;
  els.savedCount.textContent = state.saved.size;
  els.activeFilterCount.textContent = activeFilterTotal();
}

function resetFilters() {
  Object.assign(state, { role: "All roles", search: "", type: "", region: "", grad: "", sort: "newest", freshman: false, savedOnly: false });
  els.search.value = ""; els.type.value = ""; els.region.value = ""; els.grad.value = ""; els.sort.value = "newest"; els.freshman.checked = false; els.savedOnly.checked = false;
  renderRoles(); render();
}

async function loadJobs() {
  try {
    const response = await fetch(`data/opportunities.json?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.jobs = payload.opportunities || [];
    els.heroJobCount.textContent = state.jobs.length;
    const queued = Number(payload.companies_in_scan_queue ?? payload.fortune500_company_count ?? 0);
    const scanned = Number(payload.companies_scanned_last_24h ?? 0);
    const failed = Number(payload.companies_failed_last_24h ?? 0);
    const matched = Number(payload.companies_with_eligible_jobs ?? new Set(state.jobs.map(job => job.company)).size);
    const cycle = Number(payload.estimated_full_cycle_hours ?? 0);
    els.heroCompanyCount.textContent = queued || "—";
    els.coverageQueued.textContent = queued || "—";
    els.coverageScanned.textContent = scanned;
    els.coverageFailed.textContent = failed;
    els.coverageMatched.textContent = matched;
    els.coverageCycle.textContent = cycle ? `${cycle}h` : "—";
    els.directorySource.textContent = payload.directory_source || "Awaiting first automated directory refresh";
    const warningCount = Array.isArray(payload.directory_warnings) ? payload.directory_warnings.length : 0;
    els.coverageDisclosure.textContent = scanned
      ? `${scanned} of ${queued || 500} companies were actually checked during the last 24 hours.${warningCount ? ` ${warningCount} directory warning${warningCount === 1 ? "" : "s"} recorded.` : ""}`
      : "The packaged sample has not run the live 500-company scanner yet. Deploy the included GitHub Action to populate real scan coverage.";
    els.lastUpdated.textContent = relativeTime(payload.updated_at);
    document.title = `${state.jobs.length} eligible openings — LaunchList`;
    render();
    state.jobs.forEach(job => state.seen.add(job.id));
    localStorage.setItem("launchlist:seen", JSON.stringify([...state.seen].slice(-1000)));
  } catch (error) {
    els.grid.innerHTML = `<div class="empty-state"><div class="empty-icon">!</div><h2>Could not load opportunity data</h2><p>${error.message}. Open the site through a local web server or deploy it with GitHub Pages.</p></div>`;
    els.heroJobCount.textContent = "0";
    els.heroCompanyCount.textContent = "0";
    els.coverageQueued.textContent = "0";
    els.coverageScanned.textContent = "0";
    els.coverageFailed.textContent = "0";
    els.coverageMatched.textContent = "0";
    els.coverageCycle.textContent = "—";
    els.directorySource.textContent = "Unavailable";
    els.lastUpdated.textContent = "Unavailable";
  }
}

function bindEvents() {
  els.search.addEventListener("input", event => { state.search = event.target.value; render(); });
  els.type.addEventListener("change", event => { state.type = event.target.value; render(); });
  els.region.addEventListener("change", event => { state.region = event.target.value; render(); });
  els.grad.addEventListener("change", event => { state.grad = event.target.value; render(); });
  els.sort.addEventListener("change", event => { state.sort = event.target.value; render(); });
  els.freshman.addEventListener("change", event => { state.freshman = event.target.checked; render(); });
  els.savedOnly.addEventListener("change", event => { state.savedOnly = event.target.checked; render(); });
  els.filterToggle.addEventListener("click", () => {
    const willOpen = els.filterPanel.hidden;
    els.filterPanel.hidden = !willOpen;
    els.filterToggle.setAttribute("aria-expanded", String(willOpen));
  });
  document.querySelector("#clearFilters").addEventListener("click", resetFilters);
  document.querySelector("#emptyClearButton").addEventListener("click", resetFilters);
  document.querySelector("#savedViewButton").addEventListener("click", () => { state.savedOnly = !state.savedOnly; els.savedOnly.checked = state.savedOnly; render(); document.querySelector(".tracker-shell").scrollIntoView({ behavior: "smooth" }); });
  document.addEventListener("keydown", event => {
    if (event.key === "/" && document.activeElement !== els.search) { event.preventDefault(); els.search.focus(); }
    if (event.key === "Escape" && document.activeElement === els.search) { els.search.blur(); }
  });
}

renderRoles();
bindEvents();
loadJobs();
