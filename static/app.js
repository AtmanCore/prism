"use strict";

const SEVS = ["critical", "high", "medium", "low"];
const RUNNING = ["queued", "preparing", "kb-refresh", "reviewing"];
const STATUS_LOZ = {
  new: ["gray", "Not reviewed"],
  queued: ["gray", "Queued"],
  preparing: ["blue", "Checking out"],
  "kb-refresh": ["blue", "Refreshing KB"],
  reviewing: ["blue", "In review"],
  done: ["green", "Reviewed"],
  failed: ["red", "Failed"],
  interrupted: ["yellow", "Interrupted"],
};
const VERDICT = {
  block: ["red", "Block merge", "Critical issues must be fixed before this can merge."],
  "fix-before-merge": ["yellow", "Fix before merge", "Real issues to fix, none catastrophic."],
  "approve-with-nits": ["blue", "Approve with nits", "Mergeable once the minor comments are handled."],
  clean: ["green", "Clean", "No findings from any pass."],
};
const SOURCE = {
  code: ["purple", "Code"],
  consistency: ["teal", "Ticket / PR consistency"],
  "kb-regression": ["yellow", "KB regression"],
};
const RESP = {
  resolved: ["green", "Resolved"],
  expected: ["teal", "Expected · skip"],
  "still-open": ["yellow", "Still open"],
  "awaiting-reply": ["gray", "Awaiting reply"],
};
const MY = {
  "to-review": ["blue", "To review"],
  updated: ["yellow", "Ready again"],
  reviewed: ["green", "Reviewed"],
  authored: ["purple", "Your PR"],
};
const CLOSED = { MERGED: ["purple", "Merged"], CLOSED: ["red", "Closed"], DRAFT: ["gray", "Back to draft"], OPEN: ["gray", "Not requested"], UNKNOWN: ["gray", "Gone"] };
function myState(p) { return p.my_state || "to-review"; }
function myLoz(p, long = false) {
  if (!p.active) { const [c, l] = CLOSED[p.closed_state] || ["gray", "No longer open"]; return `<span class="loz ${c}">${l}</span>`; }
  const st = myState(p);
  const [c, l] = MY[st] || MY["to-review"];
  const since = (p.my_activity || []).length ? ` · since: ${p.my_activity.join(", ")}` : "";
  const why = st === "updated" ? `Author ${(p.my_changes || []).join(" and ")}${since}`
    : st === "reviewed" ? `You ${String(p.my_verdict || "reviewed").toLowerCase().replace("_", " ")} ${ago(p.my_last_at)}${since}` : "";
  const icon = st === "reviewed" ? "✓ " : st === "updated" ? "● " : "";
  const hint = !long && st === "reviewed" && (p.my_activity || []).length ? `<div class="my-hint">since: ${esc(p.my_activity.join(", "))}</div>` : "";
  return `<span class="loz ${c} my-loz" title="${esc(why)}">${icon}${l}</span>${long && why ? ` <span class="muted my-why">${esc(why)}</span>` : hint}`;
}
const AC_LOZ = { covered: "green", partial: "yellow", missing: "red", contradicts: "red" };

const state = {
  prs: [], meta: {}, view: "todo", layout: localGet("layout") || "list", sort: localGet("sort") || "severity",
  q: "", detail: null, run: null, tab: "findings", sevFilter: new Set(), srcFilter: null, runId: null, pinned: false, liveLog: null,
  selected: new Set(), modal: null, chat: null, chatBusy: false, chatDraft: "",
};

function localGet(k) { try { return localStorage.getItem("prr." + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("prr." + k, v); } catch { /* private mode */ } }

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    const text = await res.text();
    let msg = text;
    try { msg = JSON.parse(text).error || text; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function until(iso) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function dur(sec) {
  if (sec == null) return "n/a";
  return sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`;
}

/* -------------------------------------------------------------- icons */
function prioIcon(sev) {
  const c = `var(--sev-${sev})`;
  const paths = {
    critical: `<path d="M3 9.5 8 5l5 4.5M3 13.5 8 9l5 4.5" stroke="${c}" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
    high: `<path d="M3 10.5 8 6l5 4.5" stroke="${c}" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
    medium: `<path d="M3 6.5h10M3 10.5h10" stroke="${c}" stroke-width="2" stroke-linecap="round"/>`,
    low: `<path d="M3 5.5 8 10l5-4.5" stroke="${c}" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
  };
  return `<svg class="prio" viewBox="0 0 16 16" role="img" aria-label="${sev}">${paths[sev] || ""}</svg>`;
}
const prIcon = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><rect width="16" height="16" rx="3" fill="#4bce97"/><path d="M5.5 4v8M5.5 4a1.2 1.2 0 1 0 0 .01M5.5 12a1.2 1.2 0 1 0 0 .01M10.5 12V7.5A2 2 0 0 0 8.5 5.5H8" stroke="#fff" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg>`;
const chev = `<svg class="chev" viewBox="0 0 16 16" width="16" height="16"><path d="m6 4 4 4-4 4" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>`;

function avatar(login, url, size = 24) {
  const initials = esc((login || "?").slice(0, 2).toUpperCase());
  const img = url ? `<img src="${esc(url)}&s=${size * 2}" alt="" onerror="this.remove()">` : "";
  return `<span class="avatar" style="width:${size}px;height:${size}px" title="${esc(login)}">${img || initials}</span>`;
}

function statusLoz(pr) {
  const [c, label] = STATUS_LOZ[pr.review_status] || ["gray", pr.review_status || "?"];
  const spin = RUNNING.includes(pr.review_status) && pr.review_status !== "queued" ? '<span class="spin"></span>' : "";
  const out = pr.outdated && !RUNNING.includes(pr.review_status) ? ' <span class="loz yellow">Outdated</span>' : "";
  return `<span class="loz ${c}">${spin}${label}</span>${out}`;
}

function sevPills(counts) {
  if (!counts) return '<span class="muted">n/a</span>';
  return `<span class="sevs">${SEVS.map((s) => {
    const n = counts[s] || 0;
    return `<span class="sev-pill ${n ? s : "zero"}" title="${n} ${s}">${n ? prioIcon(s) : ""}${n}</span>`;
  }).join("")}</span>`;
}

function topSeverity(pr) {
  const c = pr.counts || {};
  return SEVS.find((s) => c[s] > 0) || null;
}
function sevScore(pr) {
  const c = pr.counts || {};
  return (c.critical || 0) * 1e6 + (c.high || 0) * 1e4 + (c.medium || 0) * 100 + (c.low || 0);
}
function repoShort(pr) { return pr.repo.split("/").pop(); }
function isNew(pr) { return pr.review_status === "done" && pr.last_run && pr.seen_run !== pr.last_run; }

/* -------------------------------------------------------------- tiny markdown */
function md(src) {
  if (!src) return "";
  const blocks = [];
  let text = String(src).replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  for (const para of text.split(/\n{2,}/)) {
    const lines = para.split("\n");
    if (/^\u0000\d+\u0000$/.test(para.trim())) { out.push(blocks[+para.trim().slice(1, -1)]); continue; }
    if (lines.every((l) => /^\s*([-*]|\d+\.)\s+/.test(l) || !l.trim())) {
      out.push(`<ul>${lines.filter((l) => l.trim()).map((l) => `<li>${inline(l.replace(/^\s*([-*]|\d+\.)\s+/, ""))}</li>`).join("")}</ul>`);
      continue;
    }
    out.push(`<p>${lines.map(inline).join("<br>")}</p>`);
  }
  return `<div class="md">${out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i])}</div>`;
}

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* -------------------------------------------------------------- filtering */
function viewFilter(view) {
  return {
    todo: (p) => p.active && !p.authored && myState(p) !== "reviewed",
    authored: (p) => p.active && p.authored,
    ready: (p) => p.active && myState(p) === "updated",
    mine: (p) => p.active && myState(p) === "reviewed",
    all: (p) => p.active,
    attention: (p) => p.active && !p.authored && ((p.counts?.critical || 0) + (p.counts?.high || 0) > 0),
    running: (p) => p.active && RUNNING.includes(p.review_status),
    failed: (p) => p.active && ["failed", "interrupted"].includes(p.review_status),
    archived: (p) => !p.active,
  }[view] || (view.startsWith("kb:")
    ? (p) => p.active && (p.kb_matches || []).some((k) => k.id === view.slice(3))
    : (p) => p.active && view.startsWith("repo:") && p.repo === view.slice(5));
}
function viewTitle(view) {
  if (VIEW_TITLE[view]) return VIEW_TITLE[view];
  if (view.startsWith("kb:")) return `${(state.meta.kbs || []).find((k) => k.id === view.slice(3))?.name || view.slice(3)}: PRs`;
  return view.replace(/^repo:/, "");
}
const VIEW_TITLE = {
  todo: "To review", authored: "My PRs", ready: "Ready again (author handed it back)", mine: "Reviewed by you", all: "All open PRs", attention: "Needs attention", running: "In review", failed: "Failed reviews",
  archived: "Merged / closed",
};

function visiblePrs() {
  const q = state.q.trim().toLowerCase();
  let list = state.prs.filter(viewFilter(state.view));
  if (q) {
    list = list.filter((p) => [p.title, p.repo, p.author, p.head_ref, p.ticket, `#${p.number}`, ...(p.labels || [])]
      .join(" ").toLowerCase().includes(q));
  }
  const sorters = {
    severity: (a, b) => sevScore(b) - sevScore(a) || new Date(b.updated_at) - new Date(a.updated_at),
    updated: (a, b) => new Date(b.updated_at) - new Date(a.updated_at),
    created: (a, b) => new Date(a.created_at) - new Date(b.created_at),
    repo: (a, b) => a.repo.localeCompare(b.repo) || a.number - b.number,
  };
  return list.sort(sorters[state.sort] || sorters.severity);
}

/* -------------------------------------------------------------- sidebar */
function renderSidebar() {
  const counts = {};
  $("#kbNav").innerHTML = (state.meta.kbs || []).map((k) => `<a class="nav-item" data-view="kb:${esc(k.id)}" href="#/view/kb:${encodeURIComponent(k.id)}"><span class="nav-ico">◆</span><span class="repo-name">${esc(k.name)}</span><span class="count" data-count="kb:${esc(k.id)}"></span></a>`).join("");
  for (const v of ["todo", "authored", "ready", "mine", "all", "attention", "running", "failed", "archived", ...(state.meta.kbs || []).map((k) => `kb:${k.id}`)]) {
    counts[v] = state.prs.filter(viewFilter(v)).length;
  }
  document.querySelectorAll("[data-count]").forEach((el) => {
    const n = counts[el.dataset.count];
    el.textContent = n || "";
    el.classList.toggle("hot", el.dataset.count === "attention" && n > 0);
  });
  const repos = {};
  state.prs.filter((p) => p.active).forEach((p) => { repos[p.repo] = (repos[p.repo] || 0) + 1; });
  $("#repoNav").innerHTML = Object.entries(repos).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([r, n]) => `<a class="nav-item" data-view="repo:${esc(r)}" href="#/view/repo:${encodeURIComponent(r)}"><span class="nav-ico">▪</span><span class="repo-name">${esc(r.split("/").pop())}</span><span class="count">${n}</span></a>`).join("");
  const active = state.detail ? null : state.view;
  document.querySelectorAll(".nav-item").forEach((a) => a.classList.toggle("active", a.dataset.view === active));
  $("#orgName").textContent = state.meta.org || (state.meta.viewer ? `@${state.meta.viewer}` : "GitHub");
  if (state.meta.viewer) {
    const v = $("#viewer");
    v.title = state.meta.viewer;
    v.innerHTML = `<img src="https://github.com/${encodeURIComponent(state.meta.viewer)}.png?size=48" alt="" onerror="this.remove()">`;
  }
}

function renderSync() {
  const el = $("#syncStatus");
  const m = state.meta;
  if (m.last_poll_error) {
    el.className = "sync err";
    el.textContent = `Sync failed ${ago(m.last_poll)}`;
    el.title = m.last_poll_error;
    return;
  }
  el.className = "sync";
  el.title = "";
  const running = (m.running || []).length;
  el.textContent = `Synced ${ago(m.last_poll)} · next in ${until(m.next_poll)}${running ? ` · ${running} reviewing` : ""}`;
  const every = $("#syncEvery");
  if (every && m.poll_interval_sec && document.activeElement !== every) every.value = String(Math.round(m.poll_interval_sec / 60));
}

/* -------------------------------------------------------------- list page */
function renderList() {
  const list = visiblePrs();
  const active = state.prs.filter((p) => p.active);
  const totals = SEVS.reduce((acc, s) => ({ ...acc, [s]: active.reduce((n, p) => n + (p.counts?.[s] || 0), 0) }), {});
  const reviewing = active.filter((p) => RUNNING.includes(p.review_status)).length;
  const title = viewTitle(state.view);

  const stats = `
    <div class="stats">
      <button class="stat" data-go="todo"><div class="stat-label">To review</div><div class="stat-value">${active.filter(viewFilter("todo")).length}</div></button>
      <button class="stat ready" data-go="ready"><div class="stat-label">● Ready again</div><div class="stat-value">${active.filter((p) => myState(p) === "updated").length}</div></button>
      <button class="stat mine" data-go="mine"><div class="stat-label">✓ Reviewed by you</div><div class="stat-value">${active.filter((p) => myState(p) === "reviewed").length}</div></button>
      ${SEVS.map((s) => `<button class="stat ${s}" data-go="attention"><div class="stat-label">${prioIcon(s)}${s[0].toUpperCase() + s.slice(1)} findings</div><div class="stat-value">${totals[s]}</div></button>`).join("")}
      <button class="stat" data-go="running"><div class="stat-label">In review now</div><div class="stat-value">${reviewing}</div></button>
    </div>`;

  const toolbar = `
    <div class="toolbar">
      <div class="seg" role="group" aria-label="Layout">
        <button data-layout="list" class="${state.layout === "list" ? "on" : ""}">List</button>
        <button data-layout="board" class="${state.layout === "board" ? "on" : ""}">Board</button>
      </div>
      <span class="spacer"></span>
      <label class="muted" for="sortSel">Sort</label>
      <div class="seg">
        ${[["severity", "Severity"], ["updated", "Updated"], ["created", "Oldest"], ["repo", "Repo"]]
          .map(([k, l]) => `<button data-sort="${k}" class="${state.sort === k ? "on" : ""}">${l}</button>`).join("")}
      </div>
    </div>`;

  const mp = state.meta.my_prs || {};
  const othersAuto = state.meta.auto_review !== false;
  const myBar = state.view === "authored" ? `
    <div class="myprs-bar">
      <label class="switch"><input type="checkbox" id="myPrsAuto" ${mp.auto_review ? "checked" : ""}><span class="track"></span><b>Auto-review my PRs</b></label>
      <span class="muted">${mp.auto_review ? "On: each of your PRs (drafts too) is reviewed automatically, and again after new commits."
        : "Off: your open PRs are listed here, and reviewed only when you click <b>Review</b>."} Findings, the agent chat and draft comments work the same as for other PRs.</span>
    </div>` : state.view === "todo" ? `
    <div class="myprs-bar">
      <label class="switch"><input type="checkbox" id="othersAuto" ${othersAuto ? "checked" : ""}><span class="track"></span><b>Auto-review PRs I'm asked to review</b></label>
      <span class="muted">${othersAuto ? "On: every PR that requests your review is reviewed automatically, and again after new commits."
        : "Off: PRs are still listed, and reviewed only when you click <b>Review</b> (new commits don't trigger a re-review either)."}</span>
    </div>` : "";

  let body;
  if (!list.length) {
    const m = state.meta || {};
    let title = "Nothing here", hint = "No PRs match this view.";
    if (state.view === "authored") {
      title = mp.enabled === false ? "My PRs is turned off" : "You have no open PRs";
      hint = mp.enabled === false ? 'Set <span class="mono">my_prs.enabled</span> to true in <span class="mono">config.json</span>.'
        : "Open PRs you wrote show up here after the next sync (drafts included).";
    } else if (!state.prs.length && m.last_poll_error) {
      title = "Couldn't reach GitHub";
      hint = `${esc(m.last_poll_error)}<br>Check that <span class="mono">gh auth status</span> works in a terminal, or set <span class="mono">GITHUB_TOKEN</span> in <span class="mono">.env</span>.`;
    } else if (!state.prs.length && m.last_poll) {
      title = "No PRs are waiting for your review";
      hint = `The last sync found nothing. If that's wrong, check <span class="mono">github.search_query</span> and <span class="mono">github.orgs</span> in <span class="mono">config.json</span>, or try <span class="mono">gh search prs --review-requested=@me --state=open</span>.`;
    } else if (!state.prs.length) {
      title = "Waiting for the first sync"; hint = "Pulling your review requests from GitHub…";
    }
    body = `<div class="empty"><h3>${title}</h3>${hint}</div>`;
  } else if (state.layout === "board") {
    body = renderBoard(list);
  } else {
    body = `<div class="table-wrap"><table class="issues">
      <thead><tr><th>Key</th><th>Summary</th><th>You</th><th>Bot review</th><th>Findings</th><th>Verdict</th><th>Author</th><th>Updated</th></tr></thead>
      <tbody>${list.map(rowHtml).join("")}</tbody></table></div>`;
  }

  $("#main").innerHTML = `
    <div class="breadcrumbs"><a href="#/">Projects</a><span>/</span><a href="#/">PRism</a><span>/</span><span>${esc(title)}</span></div>
    <h1 class="page-title">${esc(title)}</h1>
    ${stats}${myBar}${toolbar}${body}`;
}

function rowHtml(p) {
  const v = VERDICT[p.verdict];
  const kb = (p.kb_matches || []).map((k) => `<span class="loz yellow" title="${esc(k.name)}: ${esc(k.services.join(", "))}">KB ${esc(k.id)}</span>`).join("");
  const labels = (p.labels || []).slice(0, 3).map((l) => `<span class="loz plain">${esc(l)}</span>`).join("");
  const rs = p.responses || {};
  const replies = Object.entries(RESP).filter(([k]) => rs[k]).map(([k, [c, l]]) => `<span class="loz ${c}" title="Replies to your comments">↩ ${rs[k]} ${esc(l.split(" ")[0].toLowerCase())}</span>`).join("");
  const progress = RUNNING.includes(p.review_status) && p.progress ? `<span class="muted" style="font-size:12px">${esc(p.progress)}</span>` : "";
  const draft = p.draft ? '<span class="loz plain">Draft</span>' : "";
  const reviewBtn = p.active && p.review_status === "new" && !RUNNING.includes(p.review_status) ? `<button class="btn tint-blue btn-sm" data-review-pr="${esc(p.key)}">Review</button>` : "";
  return `<tr class="row my-${esc(p.active ? myState(p) : "closed")}" data-key="${esc(p.key)}">
    <td><span class="key">${isNew(p) ? '<span class="new-dot" title="New report"></span>' : ""}${prIcon}${esc(repoShort(p))}#${p.number}</span></td>
    <td class="summary-cell"><span class="t" title="${esc(p.title)}">${esc(p.title)}</span>
      <div class="labels">${draft}${replies}${kb}${labels}${progress}</div></td>
    <td>${myLoz(p)}</td>
    <td>${reviewBtn || statusLoz(p)}</td>
    <td>${p.review_status === "done" || p.counts ? sevPills(p.counts) : '<span class="muted">n/a</span>'}</td>
    <td>${v ? `<span class="loz ${v[0]}">${v[1]}</span>` : '<span class="muted">n/a</span>'}</td>
    <td><span class="who">${avatar(p.author, p.author_avatar)}<span>${esc(p.author)}</span></span></td>
    <td class="muted" title="${esc(p.updated_at)}">${ago(p.updated_at)}</td>
  </tr>`;
}

function renderBoard(list) {
  const cols = [
    ["To review", (p) => p.active && myState(p) === "to-review"],
    ["✎ My PRs", (p) => p.active && p.authored],
    ["● Ready again", (p) => p.active && myState(p) === "updated"],
    ["✓ Reviewed by you", (p) => p.active && myState(p) === "reviewed"],
    ["Merged / closed", (p) => !p.active],
  ];
  return `<div class="board">${cols.map(([name, fn]) => {
    const items = list.filter(fn);
    return `<div class="col"><div class="col-head">${name}<span class="muted">${items.length}</span></div>
      ${items.map((p) => {
        const top = topSeverity(p);
        return `<a class="card my-${esc(p.active ? myState(p) : "closed")}" href="#/pr/${encodeURIComponent(p.key)}">
          ${RUNNING.includes(p.review_status) && p.progress ? `<div class="progress">${esc(p.progress)}</div>` : ""}
          <div class="card-title">${esc(p.title)}</div>
          <div style="margin-bottom:6px">${myLoz(p)} ${statusLoz(p)}</div>
          <div style="margin-bottom:8px">${p.counts ? sevPills(p.counts) : ""}</div>
          <div class="card-foot">${prIcon}<span class="key">${esc(repoShort(p))}#${p.number}</span>
            ${top ? prioIcon(top) : ""}${isNew(p) ? '<span class="new-dot"></span>' : ""}${avatar(p.author, p.author_avatar, 22)}</div>
        </a>`;
      }).join("")}</div>`;
  }).join("")}</div>`;
}

/* -------------------------------------------------------------- detail page */
// Whether to show "Approve": asked separately so a slow GitHub check never delays the page.
async function loadApproveCheck(key) {
  try {
    const a = await api(`/api/prs/${encodeURIComponent(key)}/approve-check`, { method: "POST", body: "{}" });
    const before = JSON.stringify(state.approve);
    state.approve = { key, ...a };
    if (state.detail?.pr.key === key && before !== JSON.stringify(state.approve)) renderDetail();
  } catch { /* no button */ }
}

async function loadDetail(key, runId) {
  const [d, run, chat] = await Promise.all([
    api(`/api/prs/${encodeURIComponent(key)}`),
    api(`/api/prs/${encodeURIComponent(key)}/runs/${runId || "latest"}`).catch(() => null),
    api(`/api/prs/${encodeURIComponent(key)}/chat`).catch(() => null),
  ]);
  state.detail = d;
  if (state.approve?.key !== key) state.approve = null;
  loadApproveCheck(key);
  state.chat = chat || { messages: [], decisions: [] };
  if (chat?.busy) state.chatBusy = true;
  state.run = run;
  state.runId = run?.run_id || null;
  state.pinned = Boolean(runId);
  if (d.pr.review_status === "done" && d.pr.last_run && d.pr.seen_run !== d.pr.last_run) {
    api(`/api/prs/${encodeURIComponent(key)}/seen`, { method: "POST" }).catch(() => {});
  }
}

function approveButton(pr) {
  const a = state.approve;
  if (!a || a.key !== pr.key) return "";
  if (a.already_approved) return '<span class="btn tint-blue" aria-disabled="true" style="cursor:default">✓ Approved</span>';
  if (!a.can) return "";
  return `<button class="btn tint-blue" data-act="approve" title="Approve this PR on GitHub (asks you to confirm)">
    <svg viewBox="0 0 24 24" width="14" height="14"><path d="m5 12.5 4.5 4.5L19 7.5" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
    Approve…</button>`;
}

function openApprove() {
  const pr = state.detail.pr, a = state.approve || {};
  const f = state.run?.findings;
  const open = (f?.findings || []).filter((x) => !x.dismissed && ["critical", "high"].includes(x.severity)).length;
  typedConfirm({
    title: "Approve this pull request",
    word: "approve",
    action: "Approve on GitHub",
    tone: "tint-blue",
    html: `<p style="margin-top:0">This approves <b>${esc(pr.repo)}#${pr.number}</b> on GitHub as <b>${esc(state.meta.viewer || "you")}</b>, at commit <span class="mono">${esc(pr.head_sha.slice(0, 8))}</span>. Everyone on the PR will see it, and it may count towards merging.</p>
      ${a.new_commits_since_review ? `<div class="banner warn" style="margin:0 0 12px"><div class="b-body"><b>New commits since PRism's review</b>The report is for <span class="mono">${esc((a.reviewed_sha || "").slice(0, 8))}</span>; you'd approve <span class="mono">${esc(pr.head_sha.slice(0, 8))}</span>.</div></div>` : ""}
      ${open ? `<div class="banner warn" style="margin:0 0 12px"><div class="b-body"><b>${open} critical/high finding${open === 1 ? "" : "s"} still open</b>Dismiss or downgrade them in the report if they don't apply.</div></div>` : ""}
      <label for="approveBody">Comment (optional)</label>
      <textarea id="approveBody" data-tc-field rows="2" style="width:100%;margin-bottom:12px" placeholder="e.g. LGTM, thanks!"></textarea>`,
    run: async (fields) => {
      const body = fields.approveBody || "";
      const r = await api(`/api/prs/${encodeURIComponent(pr.key)}/approve`, {
        method: "POST", body: JSON.stringify({ confirm: "approve", confirmed_by_user: true, body, expected_sha: pr.head_sha }),
      });
      state.approve = { ...(state.approve || {}), key: pr.key, already_approved: true };
      toast("Approved on GitHub.");
      renderDetail();
      if (r.url) window.open(r.url, "_blank", "noopener");
    },
  });
}

function renderDetail() {
  const { pr, runs } = state.detail;
  const run = state.run;
  const f = run?.findings;
  const findings = f?.findings || [];
  const running = RUNNING.includes(pr.review_status);
  const ticketUrl = (k) => state.meta.ticket_url_template ? state.meta.ticket_url_template.replace("{key}", encodeURIComponent(k)) : null;
  const ticket = f?.ticket?.key || pr.ticket;

  let banner = "";
  if (running) {
    banner = `<div class="banner info"><span class="spin" style="margin-top:4px"></span><div class="b-body"><b>${esc(STATUS_LOZ[pr.review_status]?.[1] || "Working")}${pr.queued_reason ? ` · ${esc(pr.queued_reason)}` : ""}</b>${esc(pr.progress || "")}${runs.length ? '<div style="margin-top:4px;font-size:12px">The previous report is shown below until this run finishes.</div>' : ""}</div></div>`;
  } else if (pr.review_status === "failed") {
    banner = `<div class="banner error"><div class="b-body"><b>The last review failed</b><span class="mono">${esc((pr.error || run?.error || "").slice(0, 600))}</span></div></div>`;
  } else if (pr.outdated) {
    banner = `<div class="banner warn"><div class="b-body"><b>New commits since this review</b>This report is for <span class="mono">${esc((pr.reviewed_sha || "").slice(0, 8))}</span>. The head is now <span class="mono">${esc(pr.head_sha.slice(0, 8))}</span>.</div></div>`;
  }
  if (run && state.pinned) {
    banner += `<div class="banner warn"><div class="b-body"><b>Viewing an older run (${esc(state.runId)})</b><a href="#" data-latest>Go to latest</a></div></div>`;
  }

  const v = VERDICT[f?.verdict];
  const verdict = f ? `<div class="verdict ${esc(f.verdict || "")}">
      <div class="verdict-top">${v ? `<span class="loz ${v[0]}">${v[1]}</span>` : ""}${sevPills(f.counts)}</div>
      ${md(f.summary)}
    </div>${mustFixHtml(f, findings)}${flowHtml(f)}` : (running ? "" : `<div class="empty"><h3>No report yet</h3>Click <b>Review again</b> to run the review.</div>`);

  const kbRan = f?.kb_regression?.ran;
  const tabs = [
    ["findings", "Findings", findings.filter((x) => !x.dismissed).length],
    ["discuss", "Ask the agent", (state.chat?.messages || []).filter((m) => m.role === "you").length || null],
    ["ac", "Acceptance criteria", (f?.ac_coverage || []).length],
    ["kb", "KB regression", kbRan ? findings.filter((x) => x.source === "kb-regression").length : null],
    ["responses", "Replies", (run?.responses?.threads || []).length || null],
    ["report", "Full report", null],
    ["log", "Run log", null],
    ["history", "History", runs.length],
  ];

  let tabBody = "";
  if (state.tab === "findings") tabBody = findingsHtml(findings, pr);
  else if (state.tab === "discuss") tabBody = discussHtml();
  else if (state.tab === "ac") tabBody = acHtml(f);
  else if (state.tab === "kb") tabBody = kbHtml(f, pr);
  else if (state.tab === "responses") tabBody = responsesHtml(run?.responses, pr);
  else if (state.tab === "report") tabBody = run?.report ? `<pre class="report">${esc(run.report)}</pre>` : '<div class="empty">No report text for this run.</div>';
  else if (state.tab === "log") tabBody = `<pre class="report" id="logPre">${esc((running && !state.pinned && state.liveLog) || run?.log || run?.error || "No log yet.")}</pre>`;
  else if (state.tab === "history") tabBody = historyHtml(runs);

  const meta = f?.meta || {};
  const box = $("#chatInput");
  const boxFocused = box && document.activeElement === box;
  if (box && !box.disabled) state.chatDraft = box.value;  // disabled while sending: the draft was set on purpose
  $("#main").innerHTML = `
    <div class="breadcrumbs"><a href="#/">PRism</a><span>/</span><a href="#/view/repo:${encodeURIComponent(pr.repo)}">${esc(repoShort(pr))}</a><span>/</span><span>${prIcon}</span><a href="${esc(pr.url)}" target="_blank" rel="noopener">#${pr.number}</a></div>
    <div class="issue-grid">
      <div>
        <div class="issue-head"><h1>${esc(pr.title)}</h1></div>
        <div class="my-line">${myLoz(pr, true)}</div>
        <div class="actions">
          <button class="btn primary" data-act="review" ${running ? "disabled" : ""}>
            <svg viewBox="0 0 24 24" width="14" height="14"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
            ${running ? "Reviewing…" : runs.length ? "Review again" : "Review this PR"}</button>
          ${pr.review_status === "reviewing" || pr.review_status === "kb-refresh" ? '<button class="btn danger" data-act="cancel">Cancel run</button>' : ""}
          ${approveButton(pr)}
          <button class="btn" data-act="check-replies" ${pr.responses_checking ? "disabled" : ""} title="Re-check the author's replies to your comments">${pr.responses_checking ? "Checking replies…" : "Check replies"}</button>
          <a class="btn" href="${esc(pr.url)}" target="_blank" rel="noopener">Open in GitHub ↗</a>
          ${ticket && ticketUrl(ticket) ? `<a class="btn" href="${esc(ticketUrl(ticket))}" target="_blank" rel="noopener">${esc(ticket)} ↗</a>` : ""}
        </div>
        ${banner}${verdict}
        <div class="tabs" role="tablist">${tabs.map(([k, l, n]) => `<button class="tab ${state.tab === k ? "on" : ""}" data-tab="${k}">${l}${n != null ? `<span class="n">${n}</span>` : ""}</button>`).join("")}</div>
        <div id="tabBody">${tabBody}</div>
      </div>
      <aside>
        <div class="panel"><div class="panel-head">Details</div>
          <dl class="dl">
            <dt>Status</dt><dd>${statusLoz(pr)}</dd>
            <dt>Verdict</dt><dd>${v ? `<span class="loz ${v[0]}">${v[1]}</span>` : '<span class="muted">n/a</span>'}</dd>
            <dt>Ticket</dt><dd>${ticket ? (ticketUrl(ticket) ? `<a href="${esc(ticketUrl(ticket))}" target="_blank" rel="noopener">${esc(ticket)}</a>` : esc(ticket)) : '<span class="muted">none linked</span>'}${f?.ticket?.summary ? `<div class="muted" style="font-size:12px">${esc(f.ticket.summary)}</div>` : ""}</dd>
            <dt>Author</dt><dd><span class="who">${avatar(pr.author, pr.author_avatar)}<span>${esc(pr.author)}</span></span></dd>
            <dt>Repository</dt><dd>${esc(pr.repo)}</dd>
            <dt>Branch</dt><dd class="mono">${esc(pr.head_ref)} → ${esc(pr.base_ref)}</dd>
            <dt>Size</dt><dd><span style="color:var(--loz-green)">+${pr.additions}</span> <span style="color:var(--loz-red)">−${pr.deletions}</span> · ${pr.changed_files} files</dd>
            <dt>Labels</dt><dd>${(pr.labels || []).map((l) => `<span class="loz plain">${esc(l)}</span>`).join(" ") || '<span class="muted">None</span>'}</dd>
            <dt>Knowledge base</dt><dd>${(pr.kb_matches || []).map((k) => `<div><span class="loz yellow">${esc(k.id)}</span> ${esc(k.services.join(", ") || "whole KB")}</div>`).join("") || '<span class="muted">None</span>'}
              <div><a href="#/settings/kb?repo=${encodeURIComponent(pr.repo)}" style="font-size:12px">${(pr.kb_matches || []).length ? "Manage" : "Attach a knowledge base…"}</a></div></dd>
            <dt>Head</dt><dd class="mono">${esc(pr.head_sha.slice(0, 10))}</dd>
          </dl>
        </div>
        <div class="panel"><div class="panel-head">This review</div>
          <dl class="dl">
            <dt>Run</dt><dd class="mono">${esc(state.runId || "n/a")}</dd>
            <dt>Reviewed SHA</dt><dd class="mono">${esc((meta.head_sha || "").slice(0, 10) || "n/a")}</dd>
            <dt>Finished</dt><dd>${meta.finished_at ? `${ago(meta.finished_at)}` : "n/a"}</dd>
            <dt>Duration</dt><dd>${dur(meta.duration_sec)}</dd>
            <dt>Trigger</dt><dd>${esc(meta.reason || "n/a")}</dd>
            <dt>Lint</dt><dd>${esc(f?.lint || "n/a")}</dd>
            <dt title="What this run would cost at API prices. Not billed when claude runs on your subscription.">API-price est.</dt><dd title="Not billed when claude runs on your subscription">${meta.cost_usd != null ? `~$${meta.cost_usd.toFixed(2)}` : "n/a"}</dd>
          </dl>
        </div>
        <div class="panel"><div class="panel-head">Created</div>
          <dl class="dl"><dt>Opened</dt><dd>${ago(pr.created_at)}</dd><dt>Updated</dt><dd>${ago(pr.updated_at)}</dd></dl>
        </div>
      </aside>
    </div>`;
  if (state.tab === "findings") linkRefs(findings, pr);
  if (state.tab === "discuss") {
    const input = $("#chatInput");
    if (input) { input.value = state.chatDraft; if (boxFocused) input.focus(); }
    const log = $("#chatLog");
    if (log) log.scrollTop = log.scrollHeight;
  }
  renderBulkBar();
  if (state.tab === "log") { const l = $("#logPre"); if (l) l.scrollTop = l.scrollHeight; }
}

function findingsHtml(findings, pr) {
  if (!state.run) return RUNNING.includes(pr.review_status) ? '<div class="empty"><h3>Review in progress</h3>Findings show up here when the run finishes.</div>' : '<div class="empty">No review yet.</div>';
  if (!state.run.findings && !state.run.error) return '<div class="empty"><h3>Run in progress</h3>This run has not finished yet.</div>';
  if (state.run.findings?.parse_error) return `<div class="banner warn"><div class="b-body"><b>The structured findings could not be parsed</b>Open the Full report tab to read the review.</div></div>`;
  if (!findings.length) return '<div class="empty"><h3>No findings</h3>None of the review passes flagged anything.</div>';
  const dismissed = findings.filter((x) => x.dismissed);
  findings = findings.filter((x) => !x.dismissed);
  const counts = SEVS.reduce((a, s) => ({ ...a, [s]: findings.filter((x) => x.severity === s).length }), {});
  const srcCounts = Object.keys(SOURCE).map((k) => [k, findings.filter((x) => x.source === k).length]).filter(([, n]) => n);
  const chips = `<div class="toolbar">
    ${SEVS.map((s) => `<button class="chip ${state.sevFilter.has(s) ? "on" : ""}" data-sev="${s}">${prioIcon(s)}${s[0].toUpperCase() + s.slice(1)} <b>${counts[s]}</b></button>`).join("")}
    <span class="spacer"></span>
    ${srcCounts.map(([k, n]) => `<button class="chip ${state.srcFilter === k ? "on" : ""}" data-src="${k}">${SOURCE[k][1]} <b>${n}</b></button>`).join("")}
    <button class="btn subtle" data-expand>Expand all</button>
  </div>
  <div class="toolbar post-bar">
    <span class="muted">Post to PR:</span>
    <button class="btn subtle" data-selall>${state.selected.size ? "Clear selection" : "Select all shown"}</button>
    <button class="btn" data-post-selected ${state.selected.size ? "" : "disabled"}>Post selected${state.selected.size ? ` (${state.selected.size})` : ""}</button>
    <button class="btn" data-post-all>Post all (${findings.filter((x) => !posted(x.id)).length})</button>
    <button class="btn tint-blue" data-post-summary title="Post the findings not posted yet as ONE comment, grouped by severity" ${findings.some((x) => !posted(x.id) && !x.dismissed) ? "" : "disabled"}>Post as one comment (${findings.filter((x) => !posted(x.id) && !x.dismissed).length})</button>
    <span class="muted" style="font-size:12px">You preview and confirm before anything goes to GitHub.</span>
  </div>`;
  const shown = findings.filter((x) => (!state.sevFilter.size || state.sevFilter.has(x.severity)) && (!state.srcFilter || x.source === state.srcFilter));
  const groups = SEVS.map((s) => [s, shown.filter((x) => x.severity === s)]).filter(([, xs]) => xs.length);
  return chips + (groups.length ? groups.map(([s, xs]) => `
    <div class="sev-group"><div class="sev-group-head">${prioIcon(s)}${s} <span class="muted">(${xs.length})</span></div>
    ${xs.map((x) => findingHtml(x, s === "critical" || s === "high")).join("")}</div>`).join("") : '<div class="empty">No findings match these filters.</div>')
    + (dismissed.length ? `<div class="sev-group"><div class="sev-group-head">Dismissed by you <span class="muted">(${dismissed.length})</span></div>
    ${dismissed.map((x) => findingHtml(x, false)).join("")}</div>` : "");
}

function findingHtml(x, open) {
  const src = SOURCE[x.source] || ["gray", x.source || "Other"];
  const ref = x.ref || (x.file ? `${x.file}${x.line ? ":" + x.line : ""}` : "");
  const p = posted(x.id);
  const r = respFor(x.id);
  if (r && ["resolved", "expected"].includes(r.status)) open = false;
  const settled = x.dismissed || (r && ["resolved", "expected"].includes(r.status));
  return `<details class="finding ${settled ? "settled" : ""}" id="finding-${esc(x.id)}" ${open ? "open" : ""}>
    <summary>${chev}<input type="checkbox" class="sel" data-sel="${esc(x.id)}" aria-label="Select for posting" ${state.selected.has(x.id) ? "checked" : ""}>${prioIcon(x.severity)}<span class="f-title">${esc(x.title)}</span>
      <span class="f-meta"><span class="f-ref" title="${esc(ref)}">${esc(ref)}</span>${decisionLoz(x)}${respLoz(r)}${p && !r ? `<a class="loz green" href="${esc(p.url || "#")}" target="_blank" rel="noopener" title="Posted ${esc(p.posted_at)}">Posted</a>` : ""}<span class="loz ${src[0]}">${esc(src[1])}</span>${x.scope ? `<span class="loz" title="Scope">${esc(x.scope)}</span>` : ""}${x.category ? `<span class="loz">${esc(x.category)}</span>` : ""}</span></summary>
    <div class="f-body">
      ${decisionBlock(x)}${respBlock(r)}
      ${ref ? `<h4>Where</h4><p><a class="mono ref-link" data-file="${esc(x.file || "")}" data-line="${esc(x.line || "")}" href="#" onclick="return !!this.dataset.href">${esc(ref)}</a></p>` : ""}
      ${x.why ? `<h4>Why this is a problem</h4>${md(x.why)}` : ""}
      ${x.impact ? `<h4>Impact</h4>${md(x.impact)}` : ""}
      ${x.fix ? `<h4>Recommended fix</h4>${md(x.fix)}` : ""}
      <div class="f-actions"><button class="btn" data-post="${esc(x.id)}">${p ? "Post again…" : "Post to PR…"}</button>
        <button class="btn subtle" data-discuss="${esc(x.id)}" title="Private chat with the review agent. The PR author never sees it.">Ask the agent about ${esc(x.id)}</button></div>
    </div></details>`;
}



/* --------------------------------------------- private discussion with the review agent */
const ACTION_LOZ = { severity: "purple", dismiss: "gray", keep: "green", context: "teal" };
function decisionById(id) { return (state.chat?.decisions || []).find((d) => d.id === id) || null; }
function decisionText(d) {
  return { dismiss: "Dismissed", keep: `Kept as ${d.from}`, severity: `${d.from} → ${d.to}`, context: "PR context" }[d.action] || d.action;
}
function decisionLoz(x) {
  if (x.reopened) return `<span class="loz yellow" title="${esc(x.reopened.reason)}">Reopened · ${esc(x.reopened.decision_id)}</span>`;
  const d = decisionById(x.decision_id);
  return d ? `<span class="loz ${ACTION_LOZ[d.action]}" title="${esc(d.reason)}">You: ${esc(decisionText(d))}</span>` : "";
}
function decisionBlock(x) {
  if (x.reopened) return `<div class="resp resp-still-open"><div class="resp-top">${decisionLoz(x)}<span>You decided on this earlier (${esc(x.reopened.decision_id)}), but the code changed: ${esc(x.reopened.reason)}</span></div></div>`;
  const d = decisionById(x.decision_id);
  if (!d) return "";
  return `<div class="resp resp-expected"><div class="resp-top">${decisionLoz(x)}<span>${esc(d.reason)}</span>
    <button class="btn subtle" data-undo-dec="${esc(d.id)}">Undo</button></div></div>`;
}
function linkFindingIds(html, runId) {
  if (runId !== state.runId) return html;
  const ids = new Set((state.run?.findings?.findings || []).map((x) => x.id));
  return html.replace(/(<[^>]+>)|\b(F\d+)\b/g, (all, tag, id) => (tag || !ids.has(id) ? all : `<a href="#" data-jump="${id}">${id}</a>`));
}
function proposalText(p) {
  return { severity: `change severity ${p.from} → ${p.to}`, dismiss: "dismiss (not a real issue)", keep: `keep as ${p.from}`,
    context: "save as PR context for future reviews", rereview: "run the whole review again" }[p.action] || p.action;
}
function proposalHtml(m, mi, p, pi) {
  const current = m.run_id === state.runId;
  let status;
  if (p.applied === "declined") status = '<span class="loz">Not now</span>';
  else if (p.applied === "queued") status = '<span class="loz blue">Review queued</span>';
  else if (p.applied && decisionById(p.applied)) status = `<span class="loz green">Applied · ${esc(p.applied)}</span>`;
  else if (p.action !== "context" && p.action !== "rereview" && !current) status = '<span class="muted">From an older run</span>';
  else if (p.action === "rereview") status = `<button class="btn primary" data-rereview="${mi}:${pi}">Re-run review</button><button class="btn subtle" data-decline="${mi}:${pi}">Not now</button>`;
  else status = `<button class="btn primary" data-apply="${mi}:${pi}">Apply</button><button class="btn subtle" data-decline="${mi}:${pi}">No</button>`;
  const what = p.finding_id ? `<a href="#" data-jump="${current ? esc(p.finding_id) : ""}"><b>${esc(p.finding_id)}</b></a> ${esc(p.title || "")}: ` : "";
  return `<div class="proposal"><div class="proposal-text"><div>${what}<b>${esc(proposalText(p))}</b></div>
    ${p.reason ? `<div class="muted">${esc(p.reason)}</div>` : ""}</div><div class="proposal-act">${status}</div></div>`;
}
function discussHtml() {
  const c = state.chat || { messages: [], decisions: [] };
  const intro = `<div class="banner info chat-banner"><div class="b-body"><b>Private chat with the review agent</b>
    This is between you and the agent that wrote this review. It is <b>not</b> a conversation with the PR author: they never see it,
    and nothing here is posted to GitHub. Ask why a finding was raised, push back, or give context. Mention findings by ID, e.g. <span class="mono">F3</span>.
    The agent proposes changes; nothing changes until you click Apply.</div></div>`;
  const decs = c.decisions.length ? `<div class="section-title">Your decisions on this PR <span class="muted">(${c.decisions.length})</span></div>
    <ul class="dec-list">${c.decisions.map((d) => `<li>
      <span class="loz ${ACTION_LOZ[d.action] || ""}">${esc(decisionText(d))}</span>
      ${d.finding_id ? `<a href="#" data-jump="${d.run_id === state.runId ? esc(d.finding_id) : ""}">${esc(d.finding_id)}</a> <b>${esc(d.title || "")}</b>` : ""}
      <span class="muted">${esc(d.reason)}</span><button class="btn subtle" data-undo-dec="${esc(d.id)}">Undo</button></li>`).join("")}</ul>
    <p class="muted small-note">Future reviews of this PR get these one-line decisions, not the chat, so they cost almost nothing.</p>` : "";
  let lastRun = null;
  const msgs = c.messages.map((m, mi) => {
    const sep = m.run_id !== lastRun ? `<div class="chat-sep">Review run ${esc(m.run_id)}${m.run_id === state.runId ? " (shown above)" : ""}</div>` : "";
    lastRun = m.run_id;
    const who = m.role === "you" ? "You" : "Review agent";
    return `${sep}<div class="msg msg-${m.role === "you" ? "you" : "agent"}"><div class="msg-who">${who} · ${ago(m.at)}${m.pending ? " · sending…" : ""}</div>
      ${linkFindingIds(md(m.text), m.run_id)}${(m.proposals || []).map((p, pi) => proposalHtml(m, mi, p, pi)).join("")}</div>`;
  }).join("");
  const canChat = state.runId && state.run?.findings && !state.run.findings.parse_error;
  const thinking = state.chatBusy ? '<div class="msg msg-agent"><div class="msg-who">Review agent</div><span class="spin"></span> <span class="muted">Thinking (it may read the code first)…</span></div>' : "";
  const box = canChat ? `<div class="chat-box">
      <textarea id="chatInput" rows="3" placeholder="e.g. “Why is F3 high? The input is validated in the caller.”" ${state.chatBusy ? "disabled" : ""}></textarea>
      <div class="chat-actions"><span class="muted">Enter to send · Shift+Enter for a new line · only you and the agent see this</span>
        <button class="btn primary" data-chat-send ${state.chatBusy ? "disabled" : ""}>Send to agent</button></div></div>`
    : '<div class="empty">There is no finished report to discuss yet.</div>';
  return `${intro}${decs}<div class="chat-log" id="chatLog">${msgs || '<p class="muted">No messages yet.</p>'}${thinking}</div>${box}`;
}
async function sendChat() {
  const input = $("#chatInput");
  const text = input?.value.trim();
  if (!text || state.chatBusy) return;
  const key = state.detail.pr.key;
  state.chatBusy = true;
  state.chatDraft = "";
  input.value = "";  // renderDetail() saves the box's current text as the draft
  state.chat.messages.push({ role: "you", text, at: new Date().toISOString(), run_id: state.runId, pending: true });
  renderDetail();
  try {
    const r = await api(`/api/prs/${encodeURIComponent(key)}/chat`, { method: "POST", body: JSON.stringify({ run_id: state.runId, message: text }) });
    if (state.detail?.pr.key === key) state.chat = r;
  } catch (err) {
    if (state.detail?.pr.key === key) { state.chat.messages.pop(); state.chatDraft = text; }
    toast(`The agent couldn't answer: ${err.message}`);
  }
  state.chatBusy = false;
  if (state.detail?.pr.key === key) renderDetail();
}
async function chatAction(path, body, okMsg) {
  const key = state.detail.pr.key;
  try {
    state.chat = await api(`/api/prs/${encodeURIComponent(key)}/${path}`, { method: "POST", body: JSON.stringify(body) });
    if (state.runId) state.run = await api(`/api/prs/${encodeURIComponent(key)}/runs/${state.runId}`);
    if (okMsg) toast(okMsg);
  } catch (err) { toast(err.message); }
  renderDetail();
}
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-discuss],[data-chat-send],[data-apply],[data-decline],[data-rereview],[data-undo-dec]");
  if (!t || !state.detail) return;
  e.preventDefault();
  if (t.dataset.discuss) {
    const box = $("#chatInput");
    const draft = (box ? box.value : state.chatDraft).trim();
    state.chatDraft = `${draft ? draft + " " : ""}${t.dataset.discuss}: `;
    state.tab = "discuss";
    renderDetail();
    const input = $("#chatInput");
    if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    return;
  }
  if ("chatSend" in t.dataset) { sendChat(); return; }
  const [mi, pi] = (t.dataset.apply || t.dataset.decline || t.dataset.rereview || "").split(":").map(Number);
  if (t.dataset.apply) {
    const m = state.chat.messages[mi]; const p = m.proposals[pi];
    chatAction("decisions", { run_id: m.run_id, message_index: mi, proposal_index: pi, ...p }, "Applied. Future reviews of this PR will keep it.");
  } else if (t.dataset.decline) {
    chatAction("proposal", { message_index: mi, proposal_index: pi, decision: "decline" });
  } else if (t.dataset.rereview) {
    chatAction("proposal", { message_index: mi, proposal_index: pi, decision: "rereview" }, "Review queued. It will use your decisions.");
  } else if (t.dataset.undoDec) {
    chatAction(`decisions/${encodeURIComponent(t.dataset.undoDec)}/undo`, {}, "Decision undone.");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.target.id === "chatInput" && e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
});

function posted(id) { return state.run?.posted?.[id] || null; }
function respFor(id) {
  return (state.run?.responses?.threads || []).find((t) => t.finding && t.finding.run_id === state.runId && t.finding.finding_id === id) || null;
}
function respLoz(r) {
  if (!r) return "";
  const [c, l] = RESP[r.status] || ["gray", r.status];
  return `<a class="loz ${c}" href="${esc(r.url)}" target="_blank" rel="noopener" title="${esc(r.note || "")}">↩ ${l}</a>`;
}
function respBlock(r) {
  if (!r) return "";
  const last = (r.replies || []).slice(-1)[0];
  return `<div class="resp resp-${esc(r.status)}"><div class="resp-top">${respLoz(r)}<span>${esc(r.note || "")}</span></div>
    ${last ? `<div class="resp-quote"><b>${esc(last.author)}:</b> ${esc(last.body.slice(0, 400))}${last.body.length > 400 ? "…" : ""}</div>` : ""}</div>`;
}

function mustFixHtml(f, findings) {
  if (!f || f.parse_error) return "";
  let items = f.must_fix;
  if (!Array.isArray(items)) {  // reports from before must_fix existed
    items = findings.filter((x) => !x.dismissed && ["critical", "high", "medium"].includes(x.severity)).map((x) => ({
      finding_id: x.id, severity: x.severity, title: x.title, ref: x.ref, cause: x.why, why: x.impact, fix: x.fix,
      importance: x.severity === "medium" ? "fix before merge" : "blocks merge",
    }));
  }
  if (!items.length) {
    return `<div class="mustfix ok"><div class="mustfix-head">${prioIcon("low")}Nothing needs fixing</div><p class="muted" style="margin:0">No Critical, High or Medium issues in the changed code.</p></div>`;
  }
  const one = (t) => esc(String(t || "").split(/\n/)[0].slice(0, 400));
  return `<div class="mustfix">
    <div class="mustfix-head">What needs fixing <span class="muted">(${items.length})</span></div>
    <ol class="mf-list">${items.map((m) => `
      <li class="mf ${["resolved", "expected"].includes(respFor(m.finding_id)?.status) ? "settled" : ""}">
        <div class="mf-top">${respLoz(respFor(m.finding_id))}${prioIcon(m.severity)}<a href="#" class="mf-title" data-jump="${esc(m.finding_id || "")}">${esc(m.title)}</a>
          <span class="loz ${m.severity === "medium" ? "yellow" : "red"}">${esc(m.importance || (m.severity === "medium" ? "fix before merge" : "blocks merge"))}</span>
          ${m.ref ? `<span class="f-ref">${esc(m.ref)}</span>` : ""}</div>
        <dl class="mf-dl">
          <dt>Cause</dt><dd>${one(m.cause)}</dd>
          <dt>Why it matters</dt><dd>${one(m.why)}</dd>
          <dt>Fix</dt><dd>${one(m.fix)}</dd>
        </dl>
      </li>`).join("")}</ol></div>`;
}

function flowHtml(f) {
  const fc = f?.flow_context;
  if (!fc || (!fc.summary && !(fc.steps || []).length)) return "";
  const steps = fc.steps || [];
  return `<div class="flow">
    <div class="mustfix-head">Flow context</div>
    ${steps.length ? `<div class="flow-chain">${steps.map((st, i) => `
      ${i ? '<span class="flow-arrow" aria-hidden="true">→</span>' : ""}
      <div class="flow-node ${st.changed ? "changed" : ""}" title="${esc(st.note || "")}">
        <div class="flow-kind">${esc(st.kind || "")}${st.changed ? " · changed" : ""}</div>
        <div class="flow-name">${esc(st.name)}</div>
        ${st.note ? `<div class="flow-note">${esc(st.note)}</div>` : ""}
      </div>`).join("")}</div>` : ""}
    ${fc.summary ? md(fc.summary) : ""}
    ${fc.source ? `<div class="muted" style="font-size:12px">Source: ${esc(fc.source)}</div>` : ""}
  </div>`;
}

/* -------------------------------------------------------------- posting (user-initiated only) */
async function startSummary() {
  state.modal = { kind: "summary", stage: "drafting", drafts: [], body: "", findingIds: [], error: null, confirmText: "" };
  renderModal();
  try {
    const r = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/summary-draft`, {
      method: "POST", body: JSON.stringify({ run_id: state.runId }),
    });
    if (!state.modal) return;
    Object.assign(state.modal, { body: r.body, findingIds: r.finding_ids, stage: "review" });
  } catch (err) {
    if (!state.modal) return;
    Object.assign(state.modal, { error: `Couldn't build the comment: ${err.message}`, stage: "review" });
  }
  renderModal();
}

async function startPost(ids) {
  ids = ids.filter(Boolean);
  if (!ids.length) { toast("Nothing selected."); return; }
  state.modal = { stage: "drafting", drafts: [], error: null, confirmText: "" };
  renderModal();
  try {
    const r = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/draft-comments`, {
      method: "POST", body: JSON.stringify({ run_id: state.runId, finding_ids: ids }),
    });
    if (!state.modal) return;
    state.modal.drafts = r.drafts.map((d) => ({ ...d, include: true }));
    state.modal.stage = "review";
  } catch (err) {
    if (!state.modal) return;
    state.modal.error = `Couldn't draft the comments: ${err.message}`;
    state.modal.stage = "review";
  }
  renderModal();
}

function closeModal() { state.modal = null; renderModal(); }

function renderModal() {
  let root = $("#modal");
  if (!root) { root = document.createElement("div"); root.id = "modal"; document.body.appendChild(root); }
  const m = state.modal;
  renderBulkBar();
  if (!m) { root.innerHTML = ""; return; }
  const pr = state.detail.pr;
  const chosen = m.drafts.filter((d) => d.include && d.body.trim());
  const summary = m.kind === "summary";
  let inner;
  if (summary && m.stage === "drafting") {
    inner = `<div class="modal-body center"><span class="spin big"></span><p>Putting the findings into one comment…</p></div>
      <div class="modal-foot"><button class="btn" data-modal-close>Cancel</button></div>`;
  } else if (summary && m.stage === "review") {
    inner = `<div class="modal-body">
        ${m.error ? `<div class="banner error"><div class="b-body">${esc(m.error)}</div></div>` : ""}
        <p class="muted" style="margin-top:0">One comment with ${m.findingIds.length} finding${m.findingIds.length === 1 ? "" : "s"}, grouped by severity. Findings you dismissed or already posted are left out, like <b>Post all</b>. Edit anything you like; nothing is posted until you confirm on the next step, and all ${m.findingIds.length} will then show as posted.</p>
        <textarea id="summaryBody" class="summary-body" rows="18">${esc(m.body)}</textarea>
      </div>
      <div class="modal-foot"><button class="btn" data-modal-close>Cancel</button>
        <button class="btn primary" data-modal-next ${m.body.trim() ? "" : "disabled"}>Post as one comment…</button></div>`;
  } else if (summary) {
    const ok = m.confirmText.trim().toLowerCase() === "comment";
    inner = `<div class="modal-body">
        <p style="margin-top:0">This posts <b>one comment</b> covering <b>${m.findingIds.length} finding${m.findingIds.length === 1 ? "" : "s"}</b> on <b>${esc(pr.repo)}#${pr.number}</b> as <b>${esc(state.meta.viewer || "you")}</b>, with no approve or request-changes. Everyone on the PR will see it.</p>
        <label for="confirmInput">Type <span class="mono"><b>comment</b></span> to confirm</label>
        <input id="confirmInput" class="confirm-input" autocomplete="off" value="${esc(m.confirmText)}" ${m.stage === "posting" ? "disabled" : ""}>
        ${m.error ? `<div class="banner error" style="margin-top:12px"><div class="b-body">${esc(m.error)}</div></div>` : ""}
      </div>
      <div class="modal-foot"><button class="btn" data-modal-back ${m.stage === "posting" ? "disabled" : ""}>Back</button>
        <button class="btn primary" data-modal-post ${ok && m.stage !== "posting" ? "" : "disabled"}>${m.stage === "posting" ? "Posting…" : "OK, post"}</button></div>`;
  } else if (m.stage === "drafting") {
    inner = `<div class="modal-body center"><span class="spin big"></span><p>Drafting short review comments…</p><p class="muted">Nothing is posted yet.</p></div>
      <div class="modal-foot"><button class="btn" data-modal-close>Cancel</button></div>`;
  } else if (m.stage === "review") {
    inner = `<div class="modal-body">
        ${m.error ? `<div class="banner error"><div class="b-body">${esc(m.error)}</div></div>` : ""}
        <p class="muted" style="margin-top:0">Edit anything you like. Nothing is posted until you confirm on the next step.</p>
        ${m.drafts.map((d, i) => `
          <div class="draft">
            <label class="draft-top"><input type="checkbox" data-draft-inc="${i}" ${d.include ? "checked" : ""}>${prioIcon(d.severity)}<b>${esc(d.title)}</b></label>
            <div class="draft-where">${d.inline ? `Inline on <span class="mono">${esc(d.path)}:${esc(d.line)}</span>` : `In the review summary${d.path ? ` (<span class="mono">${esc(d.path)}${d.line ? ":" + esc(d.line) : ""}</span> isn't in the diff)` : ""}`}</div>
            <textarea data-draft-body="${i}" rows="3">${esc(d.body)}</textarea>
          </div>`).join("")}
      </div>
      <div class="modal-foot"><button class="btn" data-modal-close>Cancel</button>
        <button class="btn primary" data-modal-next ${chosen.length ? "" : "disabled"}>Post ${chosen.length} comment${chosen.length === 1 ? "" : "s"}…</button></div>`;
  } else if (m.stage === "confirm" || m.stage === "posting") {
    const ok = m.confirmText.trim().toLowerCase() === "comment";
    inner = `<div class="modal-body">
        <p style="margin-top:0">This posts <b>${chosen.length} comment${chosen.length === 1 ? "" : "s"}</b> on <b>${esc(pr.repo)}#${pr.number}</b> as <b>${esc(state.meta.viewer || "you")}</b>, as one review with no approve or request-changes. Everyone on the PR will see them.</p>
        <label for="confirmInput">Type <span class="mono"><b>comment</b></span> to confirm</label>
        <input id="confirmInput" class="confirm-input" autocomplete="off" value="${esc(m.confirmText)}" ${m.stage === "posting" ? "disabled" : ""}>
        ${m.error ? `<div class="banner error" style="margin-top:12px"><div class="b-body">${esc(m.error)}</div></div>` : ""}
      </div>
      <div class="modal-foot"><button class="btn" data-modal-back ${m.stage === "posting" ? "disabled" : ""}>Back</button>
        <button class="btn primary" data-modal-post ${ok && m.stage !== "posting" ? "" : "disabled"}>${m.stage === "posting" ? "Posting…" : "OK, post"}</button></div>`;
  }
  root.innerHTML = `<div class="modal-backdrop" data-modal-close></div>
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <div class="modal-head"><h2 id="modalTitle">${m.stage === "confirm" || m.stage === "posting" ? "Confirm posting" : summary ? "Post all findings as one comment" : "Review comments before posting"}</h2></div>
      ${inner}
    </div>`;
  const ci = $("#confirmInput");
  if (ci && m.stage === "confirm") { ci.focus(); ci.setSelectionRange(ci.value.length, ci.value.length); }
}

async function doPostSummary() {
  const m = state.modal;
  m.stage = "posting";
  m.error = null;
  renderModal();
  try {
    const r = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/post-summary`, {
      method: "POST",
      body: JSON.stringify({ run_id: state.runId, confirmed_by_user: true, body: m.body, finding_ids: m.findingIds }),
    });
    closeModal();
    toast(`Posted one comment covering ${r.findings} finding${r.findings === 1 ? "" : "s"}.`);
    state.run = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/runs/${state.runId}`);
    renderDetail();
    if (r.url) window.open(r.url, "_blank", "noopener");
  } catch (err) {
    m.stage = "confirm";
    m.error = `GitHub rejected the post: ${err.message}`;
    renderModal();
  }
}

async function doPost() {
  const m = state.modal;
  if (m.kind === "summary") { doPostSummary(); return; }
  const chosen = m.drafts.filter((d) => d.include && d.body.trim());
  m.stage = "posting";
  m.error = null;
  renderModal();
  try {
    const r = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/post-comments`, {
      method: "POST",
      body: JSON.stringify({ run_id: state.runId, confirmed_by_user: true, comments: chosen.map(({ finding_id, path, line, inline, body }) => ({ finding_id, path, line, inline, body })) }),
    });
    closeModal();
    state.selected.clear();
    toast(`Posted ${chosen.length} comment${chosen.length === 1 ? "" : "s"} to GitHub.`);
    state.run = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/runs/${state.runId}`);
    renderDetail();
    if (r.url) window.open(r.url, "_blank", "noopener");
  } catch (err) {
    m.stage = "confirm";
    m.error = `GitHub rejected the post: ${err.message}`;
    renderModal();
  }
}

document.addEventListener("input", (e) => {
  const m = state.modal;
  if (!m) return;
  if (e.target.dataset.draftBody != null) { m.drafts[+e.target.dataset.draftBody].body = e.target.value; updateNextBtn(); }
  if (e.target.id === "summaryBody") {
    m.body = e.target.value;
    const btn = document.querySelector("[data-modal-next]");
    if (btn) btn.disabled = !m.body.trim();
  }
  if (e.target.id === "confirmInput") {
    m.confirmText = e.target.value;
    const btn = document.querySelector("[data-modal-post]");
    if (btn) btn.disabled = m.confirmText.trim().toLowerCase() !== "comment";
  }
});
function updateNextBtn() {
  const btn = document.querySelector("[data-modal-next]");
  if (!btn) return;
  const n = state.modal.drafts.filter((d) => d.include && d.body.trim()).length;
  btn.disabled = !n;
  btn.textContent = `Post ${n} comment${n === 1 ? "" : "s"}…`;
}
document.addEventListener("keydown", (e) => {
  if (!state.modal) return;
  if (e.key === "Escape" && state.modal.stage !== "posting") closeModal();
  if (e.key === "Enter" && e.target.id === "confirmInput" && !document.querySelector("[data-modal-post]").disabled) doPost();
});
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-modal-close],[data-modal-next],[data-modal-back],[data-modal-post],[data-draft-inc],[data-sel],[data-post],[data-post-selected],[data-post-all],[data-post-summary],[data-selall],[data-jump]");
  if (!t) return;
  const m = state.modal;
  if ("modalClose" in t.dataset) { if (m?.stage !== "posting") closeModal(); return; }
  if ("modalNext" in t.dataset) { m.stage = "confirm"; m.confirmText = ""; m.error = null; renderModal(); return; }
  if ("modalBack" in t.dataset) { m.stage = "review"; m.error = null; renderModal(); return; }
  if ("modalPost" in t.dataset) { doPost(); return; }
  if (t.dataset.draftInc != null) { m.drafts[+t.dataset.draftInc].include = t.checked; updateNextBtn(); return; }
  if (t.dataset.sel) {
    e.preventDefault();  // keep the <details> from toggling
    const id = t.dataset.sel;
    state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    setTimeout(() => { t.checked = state.selected.has(id); refreshPostBar(); });
    return;
  }
  if (t.dataset.post) { startPost([t.dataset.post]); return; }
  if ("postSelected" in t.dataset) { startPost([...state.selected]); return; }
  if ("postSummary" in t.dataset) { startSummary(); return; }
  if ("postAll" in t.dataset) { startPost((state.run?.findings?.findings || []).filter((x) => !posted(x.id) && !x.dismissed).map((x) => x.id)); return; }
  if ("selall" in t.dataset) {
    if (state.selected.size) state.selected.clear();
    else document.querySelectorAll("[data-sel]").forEach((c) => state.selected.add(c.dataset.sel));
    renderDetail();
    return;
  }
  if (t.dataset.jump) {
    e.preventDefault();
    if (state.tab !== "findings") { state.tab = "findings"; state.sevFilter.clear(); state.srcFilter = null; renderDetail(); }
    const d = document.getElementById(`finding-${t.dataset.jump}`);
    if (d) { d.open = true; d.scrollIntoView({ behavior: "smooth", block: "start" }); d.classList.add("flash"); setTimeout(() => d.classList.remove("flash"), 1500); }
  }
});
function renderBulkBar() {
  let bar = $("#bulkBar");
  if (!bar) { bar = document.createElement("div"); bar.id = "bulkBar"; bar.className = "bulk-bar"; document.body.appendChild(bar); }
  const n = state.detail && state.tab === "findings" ? state.selected.size : 0;
  bar.classList.toggle("show", n > 0 && !state.modal);
  bar.innerHTML = n ? `<span><b>${n}</b> selected</span>
    <button class="btn primary" data-post-selected>Post ${n} comment${n === 1 ? "" : "s"} to PR…</button>
    <button class="btn subtle" data-selall>Clear</button>` : "";
}

function refreshPostBar() {
  renderBulkBar();
  const n = state.selected.size;
  const b = document.querySelector(".post-bar [data-post-selected]");
  if (b) { b.disabled = !n; b.textContent = `Post selected${n ? ` (${n})` : ""}`; }
  const a = document.querySelector(".post-bar [data-selall]");
  if (a) a.textContent = n ? "Clear selection" : "Select all shown";
}

async function linkRefs(findings, pr) {
  for (const a of document.querySelectorAll(".ref-link")) {
    const file = a.dataset.file;
    if (!file) { a.removeAttribute("href"); continue; }
    try {
      const h = await sha256(file);
      a.href = `${pr.url}/files#diff-${h}${a.dataset.line ? "R" + a.dataset.line : ""}`;
    } catch {
      a.href = `${pr.url}/files`;
    }
    a.dataset.href = a.href;
    a.target = "_blank";
    a.rel = "noopener";
  }
}

function acHtml(f) {
  const ac = f?.ac_coverage || [];
  if (!ac.length) return `<div class="empty"><h3>No acceptance criteria</h3>${f?.ticket?.key ? "The linked ticket has no parseable acceptance criteria." : "The PR links no ticket, and its description makes no concrete claims to check."}</div>`;
  const tally = ["covered", "partial", "missing", "contradicts"].map((s) => `${ac.filter((a) => a.status === s).length} ${s}`).join(" · ");
  return `<p class="muted">${tally}</p><div class="panel" style="padding:0 16px">${ac.map((a) => `
    <div class="ac"><span class="loz ${AC_LOZ[a.status] || ""}">${esc(a.status)}</span>
      <div class="ac-body"><div>${esc(a.criterion)}</div>${a.ref ? `<div class="ac-ref">${esc(a.ref)}</div>` : ""}${a.note ? `<div class="muted" style="font-size:13px">${esc(a.note)}</div>` : ""}</div></div>`).join("")}</div>`;
}

function kbHtml(f, pr) {
  const kb = f?.kb_regression || {};
  const matches = pr.kb_matches || [];
  if (!matches.length && !kb.ran) return `<div class="empty"><h3>No knowledge base attached</h3>No knowledge base is attached to <b>${esc(pr.repo)}</b>, so no regression pass ran.<br><a class="btn" style="margin-top:12px" href="#/settings/kb?repo=${encodeURIComponent(pr.repo)}">Attach a knowledge base…</a></div>`;
  const kbFindings = (f?.findings || []).filter((x) => x.source === "kb-regression");
  return `
    <div class="section-title">Services checked</div>
    ${matches.map((m) => `<div style="margin-bottom:6px"><span class="loz yellow">${esc(m.id)}</span> <b>${esc(m.name)}</b>: ${esc(m.services.join(", ") || "whole knowledge base")}</div>`).join("")}
    ${kb.summary ? `<div class="section-title">Outcome</div>${md(kb.summary)}` : ""}
    ${(kb.checked || []).length ? `<div class="section-title">What was verified against the KB</div><ul class="list-plain">${kb.checked.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    <div class="section-title">Regression findings (${kbFindings.length})</div>
    ${kbFindings.length ? kbFindings.map((x) => findingHtml(x, true)).join("") : '<p class="muted">No regressions found against the knowledge base.</p>'}
    ${(kb.notes || []).length ? `<div class="section-title">KB refresh</div><ul class="list-plain muted">${kb.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`;
}

function responsesHtml(resp, pr) {
  const threads = resp?.threads || [];
  const head = `<div class="toolbar"><span class="muted">Threads you started on this PR, checked against the code at <span class="mono">${esc((resp?.head_sha || pr.head_sha).slice(0, 8))}</span>${resp?.checked_at ? `, ${ago(resp.checked_at)}` : ""}. Read-only: nothing is ever replied or resolved.</span>
    <span class="spacer"></span><button class="btn" data-act="check-replies" ${pr.responses_checking ? "disabled" : ""}>${pr.responses_checking ? "Checking…" : "Check replies now"}</button></div>`;
  if (!threads.length) return head + '<div class="empty"><h3>No threads yet</h3>Once you post comments on this PR, replies to them are checked here on every sync.</div>';
  const order = ["still-open", "awaiting-reply", "resolved", "expected"];
  return head + threads.slice().sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status)).map((t) => `
    <div class="thread">
      <div class="thread-top">${respLoz(t)}<span class="mono">${esc(t.path)}:${esc(t.line ?? "")}</span>
        ${t.finding ? `<a href="#" data-jump="${esc(t.finding.run_id === state.runId ? t.finding.finding_id : "")}" class="muted">${prioIcon(t.finding.severity || "low")}${esc(t.finding.title || "")}</a>` : '<span class="muted">(your own comment)</span>'}
        ${t.lesson_id ? `<a class="loz purple" href="#/learning" title="Became a lesson candidate">Lesson ${esc(t.lesson_id)}</a>` : ""}</div>
      <div class="thread-note">${esc(t.note || "")}</div>
      <div class="resp-quote"><b>You:</b> ${esc(t.original.slice(0, 300))}${t.original.length > 300 ? "…" : ""}</div>
      ${(t.replies || []).map((r) => `<div class="resp-quote"><b>${esc(r.author)}:</b> ${esc(r.body.slice(0, 500))}${r.body.length > 500 ? "…" : ""}</div>`).join("")}
    </div>`).join("");
}

function historyHtml(runs) {
  if (!runs.length) return '<div class="empty">No runs yet.</div>';
  return `<div class="panel">${runs.map((r) => `
    <div class="history-row ${r.run_id === state.runId ? "on" : ""}" data-run="${esc(r.run_id)}">
      <span class="mono">${esc(r.run_id)}</span>
      ${r.error ? '<span class="loz red">Failed</span>' : r.verdict && VERDICT[r.verdict] ? `<span class="loz ${VERDICT[r.verdict][0]}">${VERDICT[r.verdict][1]}</span>` : ""}
      ${r.counts ? sevPills(r.counts) : ""}
      <span class="muted" style="margin-left:auto;font-size:12px">${esc(r.reason || "")} · ${esc((r.head_sha || "").slice(0, 8))} · ${dur(r.duration_sec)}</span>
    </div>`).join("")}</div>`;
}

/* -------------------------------------------------------------- routing + events */

/* -------------------------------------------------------------- knowledge base settings */
const kbState = { list: [], repos: [], editing: null, draft: null, attachRepo: null, inspect: null, error: null, browser: null, detecting: false };
const lines = (v) => (v || "").split("\n").map((x) => x.trim()).filter(Boolean);
const argv = (v) => (v || "").match(/"[^"]*"|'[^']*'|\S+/g)?.map((x) => x.replace(/^["']|["']$/g, "")) || [];
const quoteArgs = (a) => (a || []).map((x) => (/\s/.test(x) ? `"${x}"` : x)).join(" ");

async function renderKbSettings(attachRepo, fromPoll = false) {
  const d = await api("/api/knowledge-bases");
  kbState.list = d.knowledge_bases;
  kbState.repos = d.known_repos;
  if (attachRepo !== undefined) kbState.attachRepo = attachRepo;
  // A background status poll mustn't redraw over an open form or folder picker.
  if (!fromPoll || (kbState.editing == null && !kbState.browser)) drawKbSettings();
  // While a refresh runs, keep its status live (only on this page).
  clearTimeout(kbState.pollTimer);
  if (kbState.list.some((kb) => kb.status?.refresh?.state === "running") && location.hash.startsWith("#/settings/kb")) {
    kbState.pollTimer = setTimeout(() => renderKbSettings(undefined, true).catch(() => {}), 3000);
  }
}

function kbRefreshHtml(kb) {
  const r = kb.status?.refresh || {};
  const when = r.last_refreshed ? `last refreshed ${ago(r.last_refreshed)}` : "not refreshed by PRism yet";
  if (r.state === "running") {
    return `<span class="spin"></span> Refreshing…${r.log?.length ? `<pre class="kb-log">${esc(r.log.join("\n"))}</pre>` : ""}`;
  }
  const how = r.method === "command" ? `<span class="mono">${esc(quoteArgs(kb.refresh_command))}</span>${kb.refresh_max_age_hours ? `, also before reviews at most every ${kb.refresh_max_age_hours}h` : ""}`
    : r.method === "skill" ? "prism-kb-builder skill, from fresh checkouts in <span class=\"mono\">.cache/repos</span>"
    : `<span class="muted">${esc(r.explain || "Not available")}</span>`;
  const outcome = r.state === "failed" ? `<div class="kb-refresh-err">Last refresh failed: ${esc(r.error || "")}</div>`
    : r.state === "done" ? `<div class="muted" style="font-size:12px">Last refresh finished ${ago(r.finished_at)}.</div>` : "";
  return `${how}<div class="muted" style="font-size:12px">${esc(when)}</div>${outcome}`;
}

function drawKbSettings() {
  const k = kbState;
  const banner = k.attachRepo ? `<div class="banner info"><div class="b-body"><b>Attach ${esc(k.attachRepo)} to a knowledge base</b>
      ${k.list.length ? `Click <b>Attach here</b> on the right card below, or add a new knowledge base.` : "Add a knowledge base first."}</div>
      <button class="btn subtle" data-kb-attach-cancel>Dismiss</button></div>` : "";
  const cards = k.list.map((kb, i) => {
    const st = kb.status || {};
    const attachRows = (kb.attach || []).map((a, j) => `<span class="chip on">${esc(a.repo)}${a.service ? ` → ${esc(a.service)}` : ""}<button class="chip-x" data-kb-detach="${i}:${j}" aria-label="Detach">×</button></span>`).join("");
    return `<div class="panel kb-card">
      <div class="panel-head kb-head"><span>◆ ${esc(kb.name)}</span><span class="mono muted">${esc(kb.id)}</span>
        <span class="loz ${st.exists ? "green" : "red"}">${st.exists ? "Folder found" : "Folder missing"}</span>
        <span class="spacer"></span>
        ${k.attachRepo ? `<button class="btn primary" data-kb-attach-here="${i}">Attach here</button>` : ""}
        <button class="btn tint-blue" data-kb-refresh="${i}" ${st.refresh?.method && st.refresh?.state !== "running" ? "" : "disabled"} title="${esc(st.refresh?.explain || "")}">${st.refresh?.state === "running" ? "Refreshing…" : "Refresh now"}</button>
        <button class="btn" data-kb-edit="${i}">Edit</button><button class="btn danger" data-kb-delete="${i}">Delete</button></div>
      <dl class="dl">
        <dt>Folder</dt><dd class="mono">${esc(st.resolved_path || kb.path)}</dd>
        <dt>Services</dt><dd>${st.services?.length ? `${st.services.length} from the registry` : '<span class="muted">No registry</span>'}${kb.service_dir ? ` · folder <span class="mono">${esc(kb.service_dir)}</span>` : ""}</dd>
        <dt>Context files</dt><dd>${st.context_files || 0} matched${(kb.context_files || []).length ? ` (<span class="mono">${esc(kb.context_files.join(", "))}</span>)` : ""}</dd>
        <dt>Monorepos</dt><dd>${(kb.monorepos || []).map((m) => `<span class="mono">${esc(m)}</span>`).join(", ") || '<span class="muted">None</span>'}</dd>
        <dt>Refresh</dt><dd>${kbRefreshHtml(kb)}</dd>
        <dt>Open PRs using it</dt><dd>${(st.active_prs || []).length}</dd>
        <dt>Attached repos</dt><dd><div class="chips">${attachRows || '<span class="muted">None. Registry matches still apply.</span>'}</div>
          <div class="attach-row">
            <input list="knownRepos" placeholder="owner/repo or owner/*" data-kb-attach-repo="${i}" value="${k.attachRepo && k.editing == null ? "" : ""}">
            <input list="svc-${i}" placeholder="service (optional)" data-kb-attach-svc="${i}">
            <datalist id="svc-${i}">${(st.services || []).map((x) => `<option value="${esc(x)}">`).join("")}</datalist>
            <button class="btn" data-kb-attach="${i}">Attach</button>
          </div></dd>
      </dl>
    </div>`;
  }).join("");
  $("#main").innerHTML = `
    <div class="breadcrumbs"><a href="#/">PRism</a><span>/</span><span>Settings</span><span>/</span><span>Knowledge bases</span></div>
    <div class="issue-head"><h1 class="page-title" style="flex:1">Knowledge bases</h1>
      <a class="btn" href="#/settings/kb/guide" style="margin-top:8px">How to build one</a>
      <button class="btn primary" data-kb-new style="margin-top:8px">Add knowledge base</button></div>
    <p class="muted" style="max-width:780px;margin-top:-8px">A knowledge base is a local folder of markdown about your services: who calls them, what they read and write, and which contracts others rely on. Reviews read it (never run it) to catch regressions the diff alone can't show. Don't have one yet? <a href="#/settings/kb/guide">Build one with Claude in a few minutes.</a></p>
    ${k.error ? `<div class="banner error"><div class="b-body">${esc(k.error)}</div></div>` : ""}
    ${banner}
    ${k.editing != null ? kbFormHtml() : ""}
    ${cards || (k.editing == null ? '<div class="empty"><h3>No knowledge bases yet</h3>Add a folder of docs to give reviews system context.</div>' : "")}
    <datalist id="knownRepos">${k.repos.map((r) => `<option value="${esc(r)}">`).join("")}</datalist>
    ${folderBrowserHtml()}`;
}

function kbFormHtml() {
  const d = kbState.draft;
  const ins = kbState.inspect;
  let detected = "<small>Pick the folder with <b>Browse…</b>. PRism reads it and fills in the settings below.</small>";
  if (kbState.detecting) detected = '<small><span class="spin" style="display:inline-block;vertical-align:middle"></span> Reading the folder…</small>';
  else if (ins && !ins.exists) detected = '<small class="err">Folder not found. Use <b>Browse…</b> to pick it.</small>';
  else if (ins) detected = `<div class="kb-detected">
      <span class="loz green">Folder found</span>
      <span>${ins.markdown_files} markdown files</span>
      <span>${ins.suggested?.registry ? `registry <span class="mono">${esc(ins.suggested.registry.file)}</span>` : '<span class="muted">no registry file</span>'}</span>
      <span>${ins.services?.length ? `${ins.services.length} services: ${esc(ins.services.slice(0, 6).join(", "))}${ins.services.length > 6 ? "…" : ""}` : '<span class="muted">no services matched yet</span>'}</span>
      ${ins.suggested?.registry || ins.suggested?.service_dir ? "" : '<a href="#/settings/kb/guide">How should the folder look?</a>'}
    </div>`;
  const open = kbState.editing !== -1 || (ins && !ins.suggested?.registry);
  return `<div class="panel kb-form"><div class="panel-head">${kbState.editing === -1 ? "Add knowledge base" : `Edit ${esc(d.name || d.id)}`}</div>
    <div class="form">
      <label class="wide">Folder<span class="row"><input data-f="path" id="kbPath" value="${esc(d.path || "")}" placeholder="~/kb/my-platform">
        <button class="btn primary" data-kb-browse>Browse…</button><button class="btn" data-kb-inspect title="Re-read the folder and refresh the detected settings">Detect again</button></span>
        ${detected}</label>
      <label>Name<input data-f="name" value="${esc(d.name || "")}" placeholder="Payments platform docs"></label>
      <label>ID<input data-f="id" value="${esc(d.id || "")}" placeholder="payments" ${kbState.editing !== -1 ? "readonly" : ""}><small>Lowercase letters, digits, - or _.</small></label>
    </div>
    <details class="kb-adv" ${open ? "open" : ""}><summary>Settings <span class="muted">(filled in from the folder; change only if needed)</span></summary>
    <div class="form">
      <label>Service folder<input data-f="service_dir" value="${esc(d.service_dir || "")}" placeholder="kb/{service}"><small><span class="mono">{service}</span> is replaced by the service name.</small></label>
      <label>Registry file<input data-f="registry.file" value="${esc(d.registry?.file || "")}" placeholder="registry.yaml"><small>Maps services to repos, so PRs match automatically.</small></label>
      <label>Context files<textarea data-f="context_files" rows="3" placeholder="README.md&#10;architecture.md&#10;docs/**/*.md">${esc((d.context_files || []).join("\n"))}</textarea><small>Globs, one per line: system-level docs read for every PR that uses this KB.</small></label>
      <label>Registry fields<span class="row"><input data-f="registry.name_field" value="${esc(d.registry?.name_field || "name")}" placeholder="name"><input data-f="registry.repo_field" value="${esc(d.registry?.repo_field || "repo")}" placeholder="repo"></span><small>Service-name field, repo field (<span class="mono">owner/repo</span>).</small></label>
      <label>Monorepos<textarea data-f="monorepos" rows="2" placeholder="my-org/deploy-manifests">${esc((d.monorepos || []).join("\n"))}</textarea><small>Repos where a service is matched by the changed paths.</small></label>
      <label>Refresh command<input data-f="refresh_command" value="${esc(quoteArgs(d.refresh_command))}" placeholder="optional, e.g. ./bin/kb update {service}"><small>Runs in the KB folder before a review. Leave empty to read it as-is.</small></label>
      <label>Refresh at most every (hours)<input data-f="refresh_max_age_hours" type="number" min="0" value="${esc(d.refresh_max_age_hours ?? 24)}"></label>
    </div></details>
    <div class="modal-foot"><button class="btn" data-kb-cancel>Cancel</button><button class="btn primary" data-kb-save>Save</button></div></div>`;
}

/* Folder picker: the local server lists folders (browsers can't give a page a real folder path). */
const FILE_ICON = { markdown: "M↓", yaml: "Y", json: "{}" };
function folderBrowserHtml() {
  const b = kbState.browser;
  if (!b) return "";
  const data = b.data;
  const crumbs = data ? data.path.split("/").map((part, i, all) => `<a href="#" data-fb-go="${esc(all.slice(0, i + 1).join("/"))}">${esc(part)}</a>`).join('<span class="muted">/</span>') : "";
  const body = !data ? '<div class="modal-body center"><span class="spin big"></span></div>' : `<div class="modal-body">
      ${data.docker ? `<div class="banner info" style="margin:0 0 10px"><div class="b-body">PRism runs in Docker, so it only sees folders mounted into it (under <span class="mono">~/kb</span>). To add your knowledge-base folders, run <span class="mono">./setup.sh kb</span> on your computer, then <span class="mono">docker compose up -d</span>.</div></div>` : ""}
      <div class="fb-crumbs">${crumbs}${data.looks_like_kb ? '<span class="loz green">Looks like a knowledge base</span>' : ""}</div>
      <ul class="fb-list">
        ${data.parent ? `<li><a href="#" data-fb-go="${esc(data.parent)}"><span class="fb-ico">↰</span>..</a></li>` : ""}
        ${data.dirs.map((d) => `<li><a href="#" data-fb-go="${esc(d.path)}"><span class="fb-ico">▸</span>${esc(d.name)}</a></li>`).join("")}
        ${data.files.map((f) => `<li class="fb-file"><span class="fb-ico ${esc(f.type)}">${FILE_ICON[f.type] || "·"}</span>${esc(f.name)}</li>`).join("")}
        ${!data.dirs.length && !data.files.length ? '<li class="muted">Empty folder</li>' : ""}
      </ul>
      <div class="fb-foot muted">${data.files.length ? `${data.files.length} markdown / YAML / JSON files here` : "No markdown, YAML or JSON files directly in this folder"}${data.other_files ? ` · ${data.other_files} other files hidden` : ""}${data.truncated ? " · list truncated" : ""}
        <label class="fb-hidden"><input type="checkbox" data-fb-hidden ${b.showHidden ? "checked" : ""}> Show hidden folders</label></div>
    </div>`;
  return `<div class="modal-backdrop" data-fb-close></div>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Choose a knowledge base folder">
      <div class="modal-head"><h2>Choose the knowledge base folder</h2></div>
      ${body}
      <div class="modal-foot"><span class="muted fb-pick">${data ? `Selected: <span class="mono">${esc(data.path)}</span>` : ""}</span>
        <button class="btn" data-fb-close>Cancel</button><button class="btn primary" data-fb-select ${data ? "" : "disabled"}>Use this folder</button></div>
    </div>`;
}
async function browseTo(path) {
  const b = kbState.browser;
  try { b.data = await api("/api/fs/browse", { method: "POST", body: JSON.stringify({ path, show_hidden: b.showHidden }) }); }
  catch (err) { toast(err.message); }
  drawKbSettings();
}
function slugId(name) { return String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32); }
async function detectKb() {
  const k = kbState;
  k.draft = readKbForm();
  k.detecting = true; drawKbSettings();
  try { k.inspect = await api("/api/knowledge-bases/inspect", { method: "POST", body: JSON.stringify({ path: k.draft.path }) }); }
  catch (err) { toast(err.message); }
  k.detecting = false;
  const ins = k.inspect, sug = ins?.suggested;
  if (ins?.exists && sug) {
    k.draft = { ...k.draft,
      ...(sug.service_dir && !k.draft.service_dir ? { service_dir: sug.service_dir } : {}),
      ...(sug.registry && !k.draft.registry ? { registry: sug.registry } : {}),
      context_files: [...new Set([...(k.draft.context_files || []), ...(sug.context_files || [])])] };
    if (!k.draft.name) k.draft.name = ins.suggested_name;
    if (!k.draft.id && k.editing === -1) k.draft.id = slugId(ins.suggested_name);
  }
  drawKbSettings();
}

function readKbForm() {
  const d = { ...kbState.draft, registry: { ...(kbState.draft.registry || {}) } };
  document.querySelectorAll(".kb-form [data-f]").forEach((el) => {
    const f = el.dataset.f, v = el.value;
    if (f.startsWith("registry.")) d.registry[f.slice(9)] = v.trim();
    else if (f === "context_files" || f === "monorepos") d[f] = lines(v);
    else if (f === "refresh_command") d[f] = argv(v).length ? argv(v) : null;
    else if (f === "refresh_max_age_hours") d[f] = Number(v || 24);
    else d[f] = v.trim();
  });
  if (!d.registry.file) d.registry = null;
  if (!d.service_dir) d.service_dir = null;
  return d;
}

async function saveKbs(list, okMsg) {
  kbState.error = null;
  try {
    await api("/api/knowledge-bases/save", { method: "POST", body: JSON.stringify({ knowledge_bases: list.map(({ status, ...kb }) => kb) }) });
    toast(okMsg);
    kbState.editing = null; kbState.draft = null; kbState.inspect = null;
    await refresh();
    await renderKbSettings();
  } catch (err) {
    kbState.error = err.message;
    drawKbSettings();
  }
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-kb-refresh],[data-kb-new],[data-kb-edit],[data-kb-delete],[data-kb-cancel],[data-kb-save],[data-kb-inspect],[data-kb-browse],[data-fb-go],[data-fb-close],[data-fb-select],[data-fb-hidden],[data-kb-attach],[data-kb-detach],[data-kb-attach-here],[data-kb-attach-cancel]");
  if (!t) return;
  const k = kbState;
  if ("fbHidden" in t.dataset) { k.browser.showHidden = t.checked; await browseTo(k.browser.data?.path || "~"); return; }
  e.preventDefault();
  if ("kbBrowse" in t.dataset) {
    k.draft = readKbForm();
    k.browser = { data: null, showHidden: false };
    drawKbSettings();
    await browseTo(k.draft.path || "~");
    return;
  }
  if (t.dataset.fbGo) { await browseTo(t.dataset.fbGo); return; }
  if ("fbClose" in t.dataset) { k.browser = null; drawKbSettings(); return; }
  if ("fbSelect" in t.dataset) {
    const path = k.browser.data.path;
    k.browser = null;
    if (k.draft.path !== path) { k.draft = { ...k.draft, path, service_dir: null, registry: null, context_files: [] }; }
    drawKbSettings();
    await detectKb();
    return;
  }
  if ("kbNew" in t.dataset) { k.editing = -1; k.draft = { context_files: [], monorepos: [], attach: [], refresh_max_age_hours: 24 }; k.inspect = null; drawKbSettings(); return; }
  if (t.dataset.kbEdit) { k.editing = +t.dataset.kbEdit; k.draft = JSON.parse(JSON.stringify(k.list[k.editing])); k.inspect = null; drawKbSettings(); return; }
  if ("kbCancel" in t.dataset) { k.editing = null; k.draft = null; drawKbSettings(); return; }
  if ("kbInspect" in t.dataset) { await detectKb(); return; }
  if ("kbSave" in t.dataset) {
    const d = readKbForm();
    const list = k.list.slice();
    if (k.editing === -1) list.push({ attach: k.attachRepo ? [{ repo: k.attachRepo }] : [], ...d }); else list[k.editing] = d;
    const attached = k.editing === -1 && k.attachRepo;
    if (attached) k.attachRepo = null;
    await saveKbs(list, attached ? "Knowledge base added and attached." : "Saved.");
    return;
  }
  if (t.dataset.kbRefresh) {
    const kb = k.list[+t.dataset.kbRefresh];
    try {
      await api(`/api/knowledge-bases/${encodeURIComponent(kb.id)}/refresh`, { method: "POST", body: "{}" });
      toast(`Refreshing ${kb.name}…`);
    } catch (err) { toast(`Couldn't refresh: ${err.message}`); }
    await renderKbSettings();
    return;
  }
  if (t.dataset.kbDelete) {
    const kb = k.list[+t.dataset.kbDelete];
    if (!confirm(`Remove "${kb.name}" from PRism? This doesn't touch the folder on disk.`)) return;
    await saveKbs(k.list.filter((_, i) => i !== +t.dataset.kbDelete), "Knowledge base removed."); return;
  }
  if (t.dataset.kbAttach) {
    const i = +t.dataset.kbAttach;
    const repo = document.querySelector(`[data-kb-attach-repo="${i}"]`).value.trim();
    const service = document.querySelector(`[data-kb-attach-svc="${i}"]`).value.trim();
    if (!repo) { toast("Enter a repo like owner/name, or owner/* for a whole org."); return; }
    const list = JSON.parse(JSON.stringify(k.list));
    list[i].attach = [...(list[i].attach || []), { repo, ...(service ? { service } : {}) }];
    await saveKbs(list, `Attached ${repo}.`); return;
  }
  if (t.dataset.kbAttachHere) {
    const i = +t.dataset.kbAttachHere;
    const list = JSON.parse(JSON.stringify(k.list));
    list[i].attach = [...(list[i].attach || []), { repo: k.attachRepo }];
    const repo = k.attachRepo; k.attachRepo = null;
    await saveKbs(list, `Attached ${repo} to ${list[i].name}. It applies from the next review.`); return;
  }
  if ("kbAttachCancel" in t.dataset) { k.attachRepo = null; drawKbSettings(); return; }
  if (t.dataset.kbDetach) {
    const [i, j] = t.dataset.kbDetach.split(":").map(Number);
    const list = JSON.parse(JSON.stringify(k.list));
    list[i].attach.splice(j, 1);
    await saveKbs(list, "Detached."); return;
  }
});


/* ------------------------------------------------ read-only "How to build a knowledge base" */
function docSlug(t) { return String(t).toLowerCase().replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-"); }
function mdDoc(src) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:]|$)/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((#[^)\s]+)\)/g, '<a href="$2" data-doc-anchor>$1</a>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, "$1");
  const lines = String(src).split("\n");
  const isBlock = (l) => /^\s*```/.test(l) || /^#{1,4}\s/.test(l) || /^\|/.test(l) || /^\s*([-*]|\d+\.)\s+/.test(l) || /^---+\s*$/.test(l);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      const buf = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) buf.push(lines[i].replace(/^ {2}/, ""));
      i++;
      out.push(`<div class="doc-code"><pre><code>${esc(buf.join("\n"))}</code></pre></div>`);
    } else if (/^#{1,4}\s/.test(l)) {
      const [, hashes, text] = l.match(/^(#{1,4})\s+(.*)$/);
      const n = Math.min(hashes.length + 1, 5);
      out.push(`<h${n} id="doc-${docSlug(text)}">${inline(text)}</h${n}>`);
      i++;
    } else if (/^---+\s*$/.test(l)) { out.push("<hr>"); i++; }
    else if (/^\|/.test(l)) {
      const rows = [];
      for (; i < lines.length && /^\|/.test(lines[i]); i++) {
        if (/^\|[\s:|-]+\|\s*$/.test(lines[i])) continue;
        rows.push(lines[i].replace(/^\||\|\s*$/g, "").split("|").map((c) => inline(c.trim())));
      }
      const [head, ...body] = rows;
      out.push(`<div class="doc-table"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead>
        <tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    } else if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
      const ordered = /^\s*\d+\./.test(l);
      const items = [];
      for (; i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i]); i++) {
        const m = lines[i].match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
        if (m) items.push(m[1]); else if (items.length) items[items.length - 1] += " " + lines[i].trim();
      }
      const li = (t) => { const c = t.match(/^\[( |x)\]\s+(.*)$/i); return c ? `<li class="check"><span class="box">${c[1].trim() ? "☑" : "☐"}</span>${inline(c[2])}</li>` : `<li>${inline(t)}</li>`; };
      out.push(`<${ordered ? "ol" : "ul"}>${items.map(li).join("")}</${ordered ? "ol" : "ul"}>`);
    } else if (!l.trim()) { i++; }
    else {
      const buf = [];
      for (; i < lines.length && lines[i].trim() && !isBlock(lines[i]); i++) buf.push(lines[i]);
      out.push(`<p>${inline(buf.join(" "))}</p>`);
    }
  }
  return `<div class="doc">${out.join("\n")}</div>`;
}
function copyBlock(text, label = "Copy") {
  return `<div class="copy-block"><pre><code>${esc(text)}</code></pre><button class="btn" data-copy="${esc(text)}">${label}</button></div>`;
}
async function renderKbGuide() {
  let g;
  try { g = await api("/api/knowledge-bases/guide"); } catch (err) { $("#main").innerHTML = `<div class="banner error"><div class="b-body">${esc(err.message)}</div></div>`; return; }
  const usage = "/prism-kb-builder Build a PRism knowledge base from ~/code/orders-api and ~/code/billing-worker into ~/kb/my-platform";
  $("#main").innerHTML = `
    <div class="breadcrumbs"><a href="#/">PRism</a><span>/</span><a href="#/settings/kb">Knowledge bases</a><span>/</span><span>How to build one</span></div>
    <div class="issue-head"><h1 class="page-title" style="flex:1">How to build a knowledge base</h1>
      <a class="btn primary" href="#/settings/kb" style="margin-top:8px">Add a knowledge base</a></div>
    <p class="muted guide-lede">Read-only reference. A knowledge base is a folder of markdown on your machine, in a layout PRism matches to PRs automatically. You can generate one with Claude Code, start from the example, or write it by hand.</p>

    <div class="guide-steps">
      <section class="panel guide-step">
        <div class="panel-head"><span class="step-n">1</span>Generate it with Claude Code <span class="loz green">Recommended</span></div>
        <div class="guide-step-body">
          <p>The <b>prism-kb-builder</b> skill reads your repos (read-only) and writes the whole folder: <span class="mono">registry.yaml</span>, one folder per service, and <span class="mono">architecture.md</span> with the cross-service contracts. It never writes secret values. Run it again later to refresh.</p>
          <p class="guide-label">Install the skill ${g.skill_installed ? '<span class="loz green">Installed</span>' : ""}</p>
          ${copyBlock(g.install_command)}
          <p class="muted small-note">Or <a href="/downloads/prism-kb-builder/SKILL.md" download>download SKILL.md</a> and put it in <span class="mono">~/.claude/skills/prism-kb-builder/</span>. Teammates without PRism can use the same file.</p>
          <p class="guide-label">Then, in Claude Code</p>
          ${copyBlock(usage)}
          <details class="guide-skill"><summary>Read the skill's instructions</summary>${copyBlock(g.skill, "Copy all")}</details>
        </div>
      </section>
      <section class="panel guide-step">
        <div class="panel-head"><span class="step-n">2</span>Or start from the example</div>
        <div class="guide-step-body">
          <p>A small, working knowledge base with a registry, two services and an architecture file. Copy it and replace the content.</p>
          ${copyBlock(`cp -R ${g.example_path} ~/kb/my-platform`)}
        </div>
      </section>
      <section class="panel guide-step">
        <div class="panel-head"><span class="step-n">3</span>Add it to PRism</div>
        <div class="guide-step-body">
          <p>Go to <a href="#/settings/kb">Knowledge bases</a>, click <b>Add knowledge base</b>, then <b>Browse…</b> and pick the folder. PRism detects the registry, the service folders and the context files for you.</p>
        </div>
      </section>
    </div>

    <h2 class="guide-ref-title">Reference: the layout PRism expects</h2>
    ${mdDoc(g.guide.replace(/^# .*\n/, ""))}`;
}
document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-doc-anchor]");
  if (a) {
    e.preventDefault();
    document.getElementById(`doc-${a.getAttribute("href").slice(1)}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const c = e.target.closest("[data-copy]");
  if (!c) return;
  try { await navigator.clipboard.writeText(c.dataset.copy); toast("Copied."); }
  catch { toast("Couldn't copy. Select the text and copy it instead."); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && kbState.browser) { kbState.browser = null; drawKbSettings(); }
});

async function renderLearning() {
  const d = await api("/api/learning");
  const lozFor = { candidate: ["gray", "Candidate"], promoted: ["green", "In lessons file"], "needs-prune": ["yellow", "Lessons file full"], removed: ["red", "Removed"] };
  const prs = (c) => [...new Set(c.occurrences.map((o) => o.pr))];
  $("#main").innerHTML = `
    <div class="breadcrumbs"><a href="#/">PRism</a><span>/</span><span>Learning</span></div>
    <h1 class="page-title">Learned lessons</h1>
    <p class="muted" style="max-width:760px">When an author's reply to your comment is valid <i>and</i> general, it becomes a candidate rule. Once the same rule shows up on ${d.promote_after_prs} different PRs, it's added to the top of the matching lessons file, and the reviewers apply it from then on. Nothing is added if a file already has ${d.cap} entries; prune it first. Remove anything you disagree with.</p>
    <div class="toolbar">${Object.entries(d.active_counts).map(([k, n]) => `<span class="loz ${n >= d.cap ? "yellow" : "plain"}">${esc(k)}.lessons.md: ${n}/${d.cap}</span>`).join(" ")}</div>
    ${d.candidates.length ? `<div class="panel">${d.candidates.map((c) => `
      <div class="lesson">
        <div class="lesson-top"><span class="loz ${lozFor[c.status]?.[0] || ""}">${lozFor[c.status]?.[1] || esc(c.status)}</span><span class="loz plain">${esc(c.target)}</span><span class="mono muted">${esc(c.id)}</span>
          <span class="spacer"></span>
          ${c.status === "promoted" ? `<button class="btn danger" data-lesson-remove="${esc(c.id)}">Remove from lessons</button>` : ""}
          ${["candidate", "removed", "needs-prune"].includes(c.status) ? `<button class="btn" data-lesson-promote="${esc(c.id)}">Add to lessons now</button>` : ""}</div>
        <div class="lesson-rule">${esc(c.rule)}</div>
        <div class="muted" style="font-size:12px">Seen on ${prs(c).length} PR${prs(c).length === 1 ? "" : "s"}: ${c.occurrences.map((o) => `<a href="${esc(o.thread)}" target="_blank" rel="noopener">${esc(o.pr.split("__").slice(1).join("#"))}</a>`).join(", ")}</div>
      </div>`).join("")}</div>` : '<div class="empty"><h3>No lessons yet</h3>They appear here when authors give valid, general reasons in reply to your comments.</div>'}`;
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-lesson-remove],[data-lesson-promote]");
  if (!t) return;
  t.disabled = true;
  const id = t.dataset.lessonRemove || t.dataset.lessonPromote;
  const r = await api(`/api/learning/${encodeURIComponent(id)}/${t.dataset.lessonRemove ? "remove" : "promote"}`, { method: "POST" });
  toast(r.status === "promoted" ? "Added to the lessons file." : r.status === "needs-prune" ? "Lessons file is full. Prune it first." : "Removed from the lessons file.");
  renderLearning();
});

async function route() {
  const h = location.hash.slice(1) || "/";
  if (h === "/settings/kb/guide") {
    state.detail = null; state.run = null; state.view = "settings-kb";
    renderSidebar(); renderBulkBar();
    await renderKbGuide();
    window.scrollTo(0, 0);
    return;
  }
  if (h.startsWith("/settings/kb")) {
    state.detail = null; state.run = null; state.view = "settings-kb";
    renderSidebar(); renderBulkBar();
    const q = new URLSearchParams(h.split("?")[1] || "");
    await renderKbSettings(q.get("repo"));
    return;
  }
  if (h === "/learning") {
    state.detail = null; state.run = null; state.view = "learning";
    renderSidebar(); renderBulkBar();
    await renderLearning();
    return;
  }
  const m = h.match(/^\/pr\/(.+)$/);
  if (m) {
    const key = decodeURIComponent(m[1]);
    if (state.detail?.pr.key !== key) { state.tab = "findings"; state.sevFilter.clear(); state.srcFilter = null; state.selected.clear(); state.modal = null; state.chatBusy = false; state.chatDraft = ""; }
    try { await loadDetail(key); } catch { location.hash = "#/"; return; }
    renderSidebar();
    renderDetail();
    window.scrollTo(0, 0);
    return;
  }
  state.detail = null;
  state.run = null;
  renderBulkBar();
  const v = h.match(/^\/view\/(.+)$/);
  state.view = v ? decodeURIComponent(v[1]) : "todo";
  renderSidebar();
  renderList();
  $("#sidebar").classList.remove("open");
}

document.addEventListener("click", async (e) => {
  const rb = e.target.closest("[data-review-pr]");
  if (rb) {
    e.stopPropagation();
    rb.disabled = true;
    await api(`/api/prs/${encodeURIComponent(rb.dataset.reviewPr)}/review`, { method: "POST", body: "{}" });
    toast("Review queued.");
    await refresh();
    return;
  }
  const t = e.target.closest("[data-key],[data-layout],[data-sort],[data-go],[data-tab],[data-act],[data-sev],[data-src],[data-run],[data-expand],[data-latest]");
  if (!t) return;
  if (t.dataset.key) { location.hash = `#/pr/${encodeURIComponent(t.dataset.key)}`; return; }
  if (t.dataset.layout) { state.layout = t.dataset.layout; localSet("layout", state.layout); renderList(); return; }
  if (t.dataset.sort) { state.sort = t.dataset.sort; localSet("sort", state.sort); renderList(); return; }
  if (t.dataset.go) { location.hash = `#/view/${t.dataset.go}`; return; }
  if (t.dataset.tab) { state.tab = t.dataset.tab; renderDetail(); return; }
  if (t.dataset.sev) { const s = t.dataset.sev; state.sevFilter.has(s) ? state.sevFilter.delete(s) : state.sevFilter.add(s); renderDetail(); return; }
  if (t.dataset.src) { state.srcFilter = state.srcFilter === t.dataset.src ? null : t.dataset.src; renderDetail(); return; }
  if ("expand" in t.dataset) { const all = document.querySelectorAll(".finding"); const open = [...all].some((d) => !d.open); all.forEach((d) => { d.open = open; }); t.textContent = open ? "Collapse all" : "Expand all"; return; }
  if ("latest" in t.dataset) { e.preventDefault(); await loadDetail(state.detail.pr.key); renderDetail(); return; }
  if (t.dataset.run) { const run = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/runs/${t.dataset.run}`); state.run = run; state.runId = run.run_id; state.pinned = true; state.tab = "findings"; renderDetail(); return; }
  if (t.dataset.act === "review") {
    t.disabled = true;
    const r = await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/review`, { method: "POST" });
    toast(r.queued ? "Review queued. The report updates here when it finishes." : "A review is already queued for this PR.");
    await refresh();
    return;
  }
  if (t.dataset.act === "check-replies") {
    t.disabled = true;
    await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/check-responses`, { method: "POST" });
    toast("Checking replies to your comments…");
    return;
  }
  if (t.dataset.act === "approve") { openApprove(); return; }
  if (t.dataset.act === "cancel") {
    await api(`/api/prs/${encodeURIComponent(state.detail.pr.key)}/cancel`, { method: "POST" });
    toast("Cancelling the run…");
  }
});

/* ------------------------------------------- type-to-confirm dialog
   For anything that can't be undone: the action button stays disabled until the exact word is typed. */
function typedConfirm({ title, html, word, action, tone = "tint-orange", run }) {
  let root = $("#typedConfirm");
  if (!root) { root = document.createElement("div"); root.id = "typedConfirm"; document.body.appendChild(root); }
  let typed = "", busy = false, error = null, fields = {};
  const close = () => { if (!busy) { root.innerHTML = ""; document.removeEventListener("keydown", onKey); } };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const draw = () => {
    root.innerHTML = `<div class="modal-backdrop" data-tc-close></div>
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="tcTitle">
        <div class="modal-head"><h2 id="tcTitle">${esc(title)}</h2></div>
        <div class="modal-body">${html}
          <label for="tcInput">Type <span class="mono"><b>${esc(word)}</b></span> to confirm</label>
          <input id="tcInput" class="confirm-input" autocomplete="off" spellcheck="false" value="${esc(typed)}" ${busy ? "disabled" : ""}>
          ${error ? `<div class="banner error" style="margin-top:12px"><div class="b-body">${esc(error)}</div></div>` : ""}
        </div>
        <div class="modal-foot"><button class="btn" data-tc-close ${busy ? "disabled" : ""}>Cancel</button>
          <button class="btn ${tone}" data-tc-ok disabled>${busy ? "Working…" : esc(action)}</button></div>
      </div>`;
    root.querySelectorAll("[data-tc-field]").forEach((el) => { if (el.id in fields) el.value = fields[el.id]; if (busy) el.disabled = true; });
    const input = root.querySelector("#tcInput"), ok = root.querySelector("[data-tc-ok]");
    const sync = () => { ok.disabled = busy || typed.trim().toLowerCase() !== word; };
    input.addEventListener("input", () => { typed = input.value; sync(); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !ok.disabled) ok.click(); });
    root.querySelectorAll("[data-tc-close]").forEach((b) => b.addEventListener("click", close));
    ok.addEventListener("click", async () => {
      root.querySelectorAll("[data-tc-field]").forEach((el) => { fields[el.id] = el.value; });
      busy = true; error = null; draw();
      try { await run(fields); busy = false; close(); } catch (err) { busy = false; error = err.message; draw(); }
    });
    sync();
    if (!busy) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  };
  document.addEventListener("keydown", onKey);
  draw();
}

function fmtSize(n) {
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${Math.round(n / 1e6)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;
}

$("#clearBtn").addEventListener("click", async () => {
  let s;
  try { s = await api("/api/data/summary"); } catch (err) { toast(`Couldn't read local data: ${err.message}`); return; }
  typedConfirm({
    title: "Clear local data",
    word: "clear",
    action: "Clear local data",
    html: `<p style="margin-top:0">This permanently deletes everything PRism has stored on this computer (${fmtSize(s.bytes)}):</p>
      <ul class="confirm-list">
        <li>${s.reports} review report${s.reports === 1 ? "" : "s"} for ${s.prs} PR${s.prs === 1 ? "" : "s"}</li>
        <li>your chats with the review agent, and your severity decisions</li>
        <li>learned lessons and posted-comment history</li>
        <li>git clones and checkouts (re-created when needed)</li>
      </ul>
      <p class="confirm-note">Kept: your settings (<span class="mono">config.json</span>), tokens (<span class="mono">.env</span>) and knowledge-base folders. Nothing on GitHub changes. PRs still waiting for your review are reviewed again on the next sync.</p>
      ${s.running ? `<div class="banner warn" style="margin:12px 0"><div class="b-body">${s.running} review${s.running === 1 ? " is" : "s are"} running and will be cancelled first.</div></div>` : ""}`,
    run: async () => {
      const r = await api("/api/data/clear", { method: "POST", body: JSON.stringify({ confirm: "clear", confirmed_by_user: true }) });
      toast(r.cancelled ? `Cancelled ${r.cancelled} running review${r.cancelled === 1 ? "" : "s"} and cleared local data.` : "Local data cleared.");
      location.hash = "#/";
      await refresh();
    },
  });
});

document.addEventListener("change", async (e) => {
  if (e.target.id !== "othersAuto") return;
  const on = e.target.checked;
  try {
    const r = await api("/api/settings/auto-review", { method: "POST", body: JSON.stringify({ auto_review: on }) });
    state.meta.auto_review = r.auto_review;
    toast(on ? "Auto-review is on. PRs waiting for a review are queued on this sync." : "Auto-review is off. Click Review on a PR to review it.");
    renderList();
  } catch (err) { e.target.checked = !on; toast(`Couldn't change it: ${err.message}`); }
});

document.addEventListener("change", async (e) => {
  if (e.target.id !== "myPrsAuto") return;
  const on = e.target.checked;
  try {
    const r = await api("/api/settings/my-prs", { method: "POST", body: JSON.stringify({ auto_review: on }) });
    state.meta.my_prs = r.my_prs;
    toast(on ? "Auto-review is on for your PRs. Unreviewed ones are queued on this sync." : "Auto-review is off for your PRs.");
    renderList();
  } catch (err) { e.target.checked = !on; toast(`Couldn't change it: ${err.message}`); }
});

$("#syncEvery").addEventListener("change", async (e) => {
  const minutes = Number(e.target.value);
  try {
    const r = await api("/api/settings/sync", { method: "POST", body: JSON.stringify({ minutes }) });
    state.meta.poll_interval_sec = r.poll_interval_sec;
    if (r.next_poll) state.meta.next_poll = r.next_poll;
    renderSync();
    toast(`PRism now checks GitHub every ${minutes} minute${minutes === 1 ? "" : "s"}.`);
  } catch (err) { toast(`Couldn't change the sync interval: ${err.message}`); }
  e.target.blur();
});

$("#pollBtn").addEventListener("click", async () => {
  await api("/api/poll", { method: "POST" });
  toast("Checking GitHub for review requests…");
  setTimeout(refresh, 4000);
});
$("#menuBtn").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#search").addEventListener("input", (e) => {
  state.q = e.target.value;
  if (state.detail) location.hash = `#/view/${state.view}`;
  else renderList();
});
window.addEventListener("hashchange", route);

let lastSig = "";
async function refresh() {
  try {
    const data = await api("/api/state");
    state.prs = data.prs;
    state.meta = data.meta;
    if (data.meta.ui_version) {
      const changed = refresh.uiVersion && refresh.uiVersion !== data.meta.ui_version;
      const dialogOpen = state.modal || $("#typedConfirm")?.innerHTML;
      if (changed && !dialogOpen) { location.reload(); return; }
      if (!changed) refresh.uiVersion = data.meta.ui_version;  // while a dialog is open, reload on a later refresh
    }
  } catch {
    $("#syncStatus").className = "sync err";
    $("#syncStatus").textContent = "Service unreachable. Run ./start.sh";
    return;
  }
  renderSync();
  const sig = JSON.stringify(state.prs.map((p) => [p.key, p.review_status, p.progress, p.last_run, p.counts, p.active, p.outdated, p.seen_run, p.updated_at, p.responses, p.responses_checking]));
  if (sig === lastSig) return;
  lastSig = sig;
  renderSidebar();
  if (state.detail) {
    const pr = state.prs.find((p) => p.key === state.detail.pr.key);
    if (!pr) return;
    const finished = pr.last_run !== state.detail.pr.last_run;
    if (finished && !state.pinned) {
      await loadDetail(pr.key);
      if (pr.review_status === "done") toast("New review report ready.");
    } else {
      const before = JSON.stringify([state.detail.pr.responses, state.detail.pr.responses_checking]);
      state.detail = await api(`/api/prs/${encodeURIComponent(pr.key)}`);
      if (before !== JSON.stringify([pr.responses, pr.responses_checking]) && state.runId) {
        const fresh = await api(`/api/prs/${encodeURIComponent(pr.key)}/runs/${state.runId}`).catch(() => null);
        if (fresh) state.run = fresh;
      }
    }
    state.liveLog = null;
    if (state.tab === "log" && RUNNING.includes(pr.review_status) && pr.current_run) {
      const live = await api(`/api/prs/${encodeURIComponent(pr.key)}/runs/${pr.current_run}`).catch(() => null);
      state.liveLog = live?.log || null;
    }
    if (state.modal) return;
    const openIds = [...document.querySelectorAll(".finding")].map((d) => d.open);
    renderDetail();
    document.querySelectorAll(".finding").forEach((d, i) => { if (openIds[i] !== undefined) d.open = openIds[i]; });
  } else if (!["learning", "settings-kb"].includes(state.view)) {
    renderList();
  }
}

refresh().then(route);
setInterval(refresh, 5000);
setInterval(renderSync, 1000);
