const SUGGESTIONS = ["renewable energy", "AI regulation", "interest rates", "Premier League", "space exploration"];
const SENTIMENTS = ["positive", "neutral", "negative"];
const PAGE_SIZE = 20;
const PARALLEL_ANALYSES = 3;

const state = {
  query: "",
  results: [],          // articles from the latest search; each has .analysis (or null)
  searchToken: 0,       // bumped per search so a slow, older response can't overwrite a newer one
  busy: new Set(),      // article URLs with an analysis request in flight
  errors: new Map(),    // article URL -> last error message for that card
  analyzingAll: false,
  progress: "",
  library: [],
  total: 0,
  counts: { positive: 0, neutral: 0, negative: 0 },
  filter: "",
  libraryToken: 0,
};

const $ = (id) => document.getElementById(id);

// ---------- helpers ----------

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  } catch {
    throw new Error("Can't reach the server. Check your connection and try again.");
  }
  if (response.status === 204) return null;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(body?.error?.message || "Something went wrong. Try again.");
    error.status = response.status;
    throw error;
  }
  return body;
}

// Builds DOM nodes. Text always goes in as text nodes, never as HTML, so article content can't inject markup.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "text") node.textContent = value;
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null && child !== false));
  return node;
}

const safeUrl = (url) => (/^https?:\/\//i.test(url || "") ? url : null);

const relativeTime = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
function timeAgo(iso) {
  if (!iso) return "";
  const seconds = (new Date(iso) - Date.now()) / 1000;
  for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (Math.abs(seconds) >= size) return relativeTime.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

const formatScore = (score) => (score > 0 ? "+" : "") + score.toFixed(2);

// ---------- actions ----------

async function search(rawQuery) {
  const query = rawQuery.trim();
  if (!query) {
    setStatus($("search-status"), "Type a topic to search for.");
    return;
  }
  const token = ++state.searchToken;
  Object.assign(state, { query, results: [], analyzingAll: false, progress: "" });
  state.errors.clear();
  $("search-button").disabled = true;
  setStatus($("search-status"), "Searching…");
  renderResults();
  try {
    const data = await api(`/api/articles?q=${encodeURIComponent(query)}`);
    if (token !== state.searchToken) return;
    state.results = data.articles;
    setStatus(
      $("search-status"),
      state.results.length
        ? `${state.results.length} recent articles for “${query}”`
        : `No articles found for “${query}”. Try broader keywords.`,
    );
  } catch (error) {
    if (token !== state.searchToken) return;
    setStatus($("search-status"), error.message, { error: true, retry: () => search(query) });
  } finally {
    if (token === state.searchToken) {
      $("search-button").disabled = false;
      renderResults();
    }
  }
}

// Runs one request for an article, tracking its busy/error state so its card can show progress.
async function withBusy(url, task) {
  state.busy.add(url);
  state.errors.delete(url);
  render();
  try {
    await task();
    return true;
  } catch (error) {
    state.errors.set(url, error.message);
    return false;
  } finally {
    state.busy.delete(url);
    render();
  }
}

// Puts a new or updated analysis on every card showing that article.
function applyAnalysis(analysis) {
  for (const article of state.results) if (article.url === analysis.url) article.analysis = analysis;
  state.library = state.library.map((item) => (item.id === analysis.id ? analysis : item));
}

function analyze(article) {
  return withBusy(article.url, async () => {
    applyAnalysis(await api("/api/analyses", { method: "POST", body: JSON.stringify(article) }));
  });
}

async function reanalyze(analysis) {
  await withBusy(analysis.url, async () => {
    applyAnalysis(await api(`/api/analyses/${analysis.id}/reanalyze`, { method: "POST" }));
  });
  loadLibrary();
}

async function remove(analysis) {
  if (!confirm(`Delete the analysis of “${analysis.title}”?`)) return;
  await withBusy(analysis.url, async () => {
    try {
      await api(`/api/analyses/${analysis.id}`, { method: "DELETE" });
    } catch (error) {
      if (error.status !== 404) throw error; // already gone is fine
    }
    for (const article of state.results) if (article.url === analysis.url) article.analysis = null;
  });
  loadLibrary();
}

async function analyzeAll() {
  const token = state.searchToken;
  const queue = state.results.filter((a) => !a.analysis && !state.busy.has(a.url));
  const total = queue.length;
  let done = 0;
  let failed = 0;
  state.analyzingAll = true;
  state.progress = `Analyzing 0 of ${total}…`;

  const worker = async () => {
    // Stop taking new articles if the user has started a different search.
    while (queue.length && token === state.searchToken) {
      const article = queue.shift();
      if (article.analysis || state.busy.has(article.url)) {
        done++; // the user analyzed this one by hand while the batch was running
        continue;
      }
      (await analyze(article)) ? done++ : failed++;
      if (token === state.searchToken) state.progress = `Analyzing ${done + failed} of ${total}…`;
    }
  };
  await Promise.all(Array.from({ length: PARALLEL_ANALYSES }, worker));

  loadLibrary();
  if (token !== state.searchToken) return;
  state.analyzingAll = false;
  state.progress = failed ? `${done} analyzed, ${failed} failed. Use Retry on the failed cards.` : `${done} analyzed.`;
  renderResults();
}

async function loadLibrary({ append = false } = {}) {
  const token = ++state.libraryToken;
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: append ? state.library.length : 0 });
  if (state.filter) params.set("sentiment", state.filter);
  try {
    const data = await api(`/api/analyses?${params}`);
    if (token !== state.libraryToken) return;
    state.library = append ? state.library.concat(data.items) : data.items;
    state.total = data.total;
    state.counts = data.counts;
    setStatus($("library-status"), "");
  } catch (error) {
    if (token !== state.libraryToken) return;
    setStatus($("library-status"), error.message, { error: true, retry: () => loadLibrary({ append }) });
  }
  renderLibrary();
}

// ---------- rendering ----------

function setStatus(node, message, { error = false, retry = null } = {}) {
  node.className = error ? "status error" : "status";
  node.replaceChildren(message, retry ? el("button", { onclick: retry, text: "Retry" }) : "");
}

function render() {
  renderResults();
  renderLibrary();
}

function analysisBlock(analysis, extraActions = []) {
  const busy = state.busy.has(analysis.url);
  const reanalyzeLabel = analysis.text_source === "full" ? "Re-analyze" : "Retry with full article";
  return el(
    "div",
    { class: "analysis" },
    el(
      "div",
      { class: "analysis-head" },
      el("span", {
        class: `badge ${analysis.sentiment}`,
        title: "Sentiment score from −1 (very negative) to +1 (very positive)",
        text: `${analysis.sentiment} ${formatScore(analysis.sentiment_score)}`,
      }),
      el("span", { text: analysis.text_source === "full" ? "Based on full article" : "Based on preview only" }),
      el("span", { text: `Analyzed ${timeAgo(analysis.analyzed_at)}` }),
    ),
    el("p", { text: analysis.summary }),
    el("p", { class: "reason", text: analysis.sentiment_reason }),
    el(
      "div",
      { class: "actions" },
      el("button", { class: "link", disabled: busy, onclick: () => reanalyze(analysis), text: busy ? "Working…" : reanalyzeLabel }),
      ...extraActions,
    ),
  );
}

function card(article, body) {
  const link = safeUrl(article.url);
  const image = safeUrl(article.image_url);
  const error = state.errors.get(article.url);
  return el(
    "article",
    { class: "card" },
    el(
      "div",
      {},
      el("p", { class: "meta", text: [article.source_name, timeAgo(article.published_at)].filter(Boolean).join(" · ") }),
      el("h3", {}, link ? el("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: article.title }) : article.title),
      article.description ? el("p", { class: "description", text: article.description }) : null,
      error ? el("p", { class: "card-error", role: "alert", text: error }) : null,
      body,
    ),
    image ? el("img", { src: image, alt: "", loading: "lazy", onerror: (event) => event.target.remove() }) : null,
  );
}

function searchCard(article) {
  if (article.analysis) return card(article, analysisBlock(article.analysis));
  const busy = state.busy.has(article.url);
  const label = busy ? "Analyzing…" : state.errors.has(article.url) ? "Retry" : "Analyze";
  const onclick = () => analyze(article).then(() => loadLibrary());
  return card(article, el("button", { class: "primary", disabled: busy, onclick, text: label }));
}

function topicSummary(analyses) {
  const counts = Object.fromEntries(SENTIMENTS.map((s) => [s, analyses.filter((a) => a.sentiment === s).length]));
  return el(
    "div",
    { class: "topic" },
    el("div", { class: "topic-label", text: `Sentiment so far: ${SENTIMENTS.map((s) => `${counts[s]} ${s}`).join(" · ")}` }),
    el("div", { class: "bar", "aria-hidden": "true" },
      ...SENTIMENTS.map((s) => el("span", { class: s, style: `flex: ${counts[s]}` }))),
  );
}

function renderResults() {
  const toolbar = $("results-toolbar");
  const fresh = state.results.filter((a) => !a.analysis).length;
  const analyzed = state.results.filter((a) => a.analysis).map((a) => a.analysis);
  toolbar.hidden = state.results.length === 0;
  toolbar.replaceChildren(
    analyzed.length ? topicSummary(analyzed) : el("div", { class: "topic topic-label", text: "Pick articles to analyze, or analyze them all at once." }),
    el("div", { class: "actions" },
      state.progress ? el("span", { class: "progress", role: "status", text: state.progress }) : null,
      el("button", { class: "primary", disabled: state.analyzingAll || fresh === 0, onclick: analyzeAll, text: `Analyze all (${fresh} new)` }),
    ),
  );
  $("results").replaceChildren(...state.results.map(searchCard));
}

function renderLibrary() {
  const all = SENTIMENTS.reduce((sum, s) => sum + state.counts[s], 0);
  $("tab-library").textContent = `Library (${all})`;
  $("filters").replaceChildren(
    ...[["", `All (${all})`], ...SENTIMENTS.map((s) => [s, `${s[0].toUpperCase()}${s.slice(1)} (${state.counts[s]})`])].map(
      ([value, label]) =>
        el("button", {
          class: "chip",
          "aria-pressed": String(state.filter === value),
          onclick: () => { state.filter = value; loadLibrary(); },
          text: label,
        }),
    ),
  );
  const empty = state.filter ? `No ${state.filter} analyses yet.` : "No analyses yet. Search for a topic and hit Analyze.";
  $("library").replaceChildren(
    ...(state.library.length
      ? state.library.map((item) =>
          card(item, analysisBlock(item, [
            el("button", { class: "link", disabled: state.busy.has(item.url), onclick: () => remove(item), text: "Delete" }),
          ])))
      : [el("p", { class: "empty", text: empty })]),
  );
  $("load-more").hidden = state.library.length >= state.total;
}

function showTab(name) {
  for (const tab of ["search", "library"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`${tab}-view`).hidden = tab !== name;
  }
  if (name === "library") loadLibrary();
}

// ---------- wiring ----------

$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  search($("search-input").value);
});
$("suggestions").replaceChildren(
  ...SUGGESTIONS.map((topic) =>
    el("button", { class: "chip", type: "button", text: topic, onclick: () => { $("search-input").value = topic; search(topic); } }),
  ),
);
$("tab-search").addEventListener("click", () => showTab("search"));
$("tab-library").addEventListener("click", () => showTab("library"));
$("load-more").addEventListener("click", () => loadLibrary({ append: true }));
loadLibrary();
