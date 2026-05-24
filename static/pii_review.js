// PII Review UI — v2 (viz_data-driven)
//
// Click behaviours on the PDF overlay:
//   • click                → select the whole xml_text at cursor (replaces selection)
//   • Alt+click            → select a single word                (replaces selection)
//   • Ctrl+click           → ADD the whole xml_text to selection
//   • Ctrl+Alt+click       → ADD a single word      to selection
//
// Multi-selection allows any combination of words / xml_texts on the same
// page. The combined text is the selected words joined in reading order.
//
// LIMITATION (documented): exact string matching only. "Mr K Adams" and
// "K Adams" are separate tokens. Semantic aliasing must be handled outside
// this tool.

// DEBUG flag: enabled by ?debug=1 in URL. Gates noisy per-interaction logs.
const PR_DEBUG = new URLSearchParams(location.search).has('debug');

console.log("%c[pii_review] script loaded — " + new Date().toLocaleTimeString(),
            "background:#0d6efd;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold;");

// Document-level diagnostic: log EVERY click on the page so we can prove
// whether clicks are reaching JS at all, even if they miss the overlay.
// Off by default; enable with ?debug=1.
if (PR_DEBUG) {
  window.addEventListener("click", (e) => {
    const t = e.target;
    console.log(`[doc click] ${t.tagName}.${t.className||'-'} (id=${t.id||'-'}) @ (${e.clientX},${e.clientY})`);
  }, true);
}

let pdfjsLib = null;

const CATEGORIES = [
  "PERSON", "ACCOUNT_NUMBER", "PHONE_NUMBER",
  "EMAIL", "ADDRESS", "SG_NRIC", "SG_UEN",
  "CREDIT_CARD", "IBAN_CODE",
  "OTHER", "REJECT",
];

const CAT_PREFIX = {
  PERSON:         "NAME",
  ACCOUNT_NUMBER: "ACCOUNT",
  PHONE_NUMBER:   "PHONE",
  EMAIL:          "EMAIL",
  ADDRESS:        "ADDRESS",
  SG_NRIC:        "NRIC",
  SG_UEN:         "UEN",
  CREDIT_CARD:    "CARD",
  IBAN_CODE:      "IBAN",
  OTHER:          "OTHER",
};

const state = {
  clientId: null,             // active client (gates Upload + File select)
  stem: null,
  data: null,                 // <stem>_pii.json
  vizData: null,              // <stem>_viz_data.json
  pdfDoc: null,
  pageCanvases: {},
  activeEntityId: null,
  redactedView: false,        // toolbar toggle: preview redacted+tokenized output
  useBboxD: true,             // default ON: pdf.js renders text aligned to D (poppler) coords; A (pdfplumber/fitz) coords can be ~18pt left of glyphs on some footers, e.g. JPM "Account E40591001 Page X of Y" (Bug 13)

  // reading-order indices per page (built from viz_data)
  flatWordsByPage: {},        // page → [{wid, xid, word, xt}]
  wordKeyToIdx:   {},         // "page:wid" → index in flatWordsByPage[page]

  // selection
  hoverText: null,            // {page, xml_text_id, word_id?, left,top,right,bottom}
  selectedKeys: [],           // list of "page:wid" on one page
  selectedPage: null,
  extraTwins: [],
};

// All backend calls are client-scoped. Helper to append client_id to fetch URLs.
function _withClient(url) {
  if (!state.clientId) return url;
  const sep = url.includes("?") ? "&" : "?";
  return url + sep + "client_id=" + encodeURIComponent(state.clientId);
}

// ── Init ──────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  try {
    pdfjsLib = await import("https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.min.mjs");
    pdfjsLib.GlobalWorkerOptions.workerSrc = window.location.origin + "/static/pdf.worker.min.mjs";
  } catch (e) { console.error("[pii_review] pdf.js load failed:", e); }
  initResize();
  initKeyboard();
  initSelectionHud();
  await loadClients();
  startStatusPoll();
});

function initKeyboard() {
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target || {}).tagName)) return;
    if (e.key === "Escape") {
      clearSelection();
    } else if (e.key === "Enter" && state.selectedKeys.length > 0) {
      e.preventDefault();
      commitSelectionAsManual();
    }
  });
}

function initSelectionHud() {
  const hud = document.createElement("div");
  hud.id = "pr-sel-hud";
  hud.style.cssText = "position:fixed;bottom:20px;left:20px;z-index:150;"
    + "background:#fff;border:2px solid var(--accent);border-radius:6px;"
    + "padding:10px 14px;display:none;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.15);"
    + "max-width:480px;";
  document.body.appendChild(hud);
}

function updateSelectionHud() {
  const hud = document.getElementById("pr-sel-hud");
  if (!hud) return;
  if (!state.selectedKeys.length) { hud.style.display = "none"; return; }
  hud.style.display = "block";
  const combined = selectionText();
  const matchCount = findOccurrencesInPdf(combined).length;
  hud.innerHTML = `
    <div style="font-weight:600;margin-bottom:4px;color:var(--accent);">
      <i class="fas fa-check-square"></i> Selection (${state.selectedKeys.length} word${state.selectedKeys.length !== 1 ? "s" : ""})
    </div>
    <div style="font-family:monospace;word-break:break-word;margin-bottom:4px;">${
      combined.replace(/</g, "&lt;")
    }</div>
    <div style="font-size:11px;color:var(--text-mute);margin-bottom:8px;">
      <i class="fas fa-magnifying-glass"></i> ${matchCount} match${matchCount !== 1 ? "es" : ""} in this PDF
      (exact string, will tokenize every occurrence)
    </div>
    <div style="font-size:11px;color:var(--text-mute);">
      Category: <select id="pr-sel-cat" style="font-size:11px;padding:2px 4px;">
        <option>PERSON</option>
        <option>ACCOUNT_NUMBER</option>
        <option>PHONE_NUMBER</option>
        <option>EMAIL</option>
        <option>ADDRESS</option>
        <option>DATE_OF_BIRTH</option>
        <option selected>OTHER</option>
      </select>
      <button class="pr-btn pr-btn-primary" onclick="prCommitSel()" style="margin-left:6px;">
        <i class="fas fa-plus"></i> Add as PII (Enter)
      </button>
      <button class="pr-btn" onclick="prClearSel()" style="margin-left:4px;">Clear (Esc)</button>
    </div>
    <div style="font-size:10px;color:var(--text-mute);margin-top:6px;">
      click = xml_text · Alt+click = word · Ctrl+click = add · Ctrl+Alt+click = add word
    </div>
  `;
}

function clearSelection() {
  state.selectedKeys = [];
  state.selectedPage = null;
  clearExtraTwins();
  updateSelectionHud();
}

// Sort words within ONE xml_text by reading order, respecting rotation.
//   rotation=0   : left → right
//   rotation=90  : bottom → top of strip (high y first), so on-screen
//                  vertical text reads correctly when tilted upright
//   rotation=270 : top → bottom of strip
//   rotation=180 : right → left
function _sortWordsByReadingOrder(words, xt) {
  const rot = (xt && xt.rotation) | 0;
  const a = [...words];
  if (rot === 90)       a.sort((x, y) => y.top - x.top);
  else if (rot === 270) a.sort((x, y) => x.top - y.top);
  else if (rot === 180) a.sort((x, y) => y.left - x.left);
  else                  a.sort((x, y) => x.left - y.left);
  return a;
}

function selectionText() {
  if (!state.selectedKeys.length) return "";
  const page = state.selectedPage;
  const flat = state.flatWordsByPage[page] || [];
  // Group selected words by parent xml_text so we can sort each group
  // by its own rotation. Cross-xml_text order = bbox top → left.
  const groups = new Map();   // xt → [word]
  for (const k of state.selectedKeys) {
    const i = state.wordKeyToIdx[k];
    if (i === undefined) continue;
    const f = flat[i];
    if (!groups.has(f.xt)) groups.set(f.xt, []);
    groups.get(f.xt).push(f.word);
  }
  const xts = [...groups.keys()].sort((p, q) =>
    Math.round(p.top) - Math.round(q.top) || p.left - q.left
  );
  return xts.map(xt =>
    _sortWordsByReadingOrder(groups.get(xt), xt).map(w => w.text).join(" ")
  ).join(" ");
}

// Re-add absorber. Without this, adding "John Doe" → reject → add again
// pushes a SECOND manual entry with the same entity string. Both share the
// same decisions[entity] (keyed by string), so reject/restore on one
// flips both. Symptoms: duplicated card in rejected pool, restore-both,
// delete-both.
//
// Behaviour on re-add:
//   - clear any stale `rejected: true` / `category: "REJECT"` (re-adding
//     an entity = explicit un-reject)
//   - if a recommendation with the same string exists, that wins —
//     drop any manual duplicates and skip the new push (return true)
//   - otherwise drop prior manual entries with the same string before
//     the caller pushes the fresh one (return false)
function _absorbReAdd(entity) {
  if (!state.data) return false;
  const dec = (state.data.decisions || {})[entity];
  if (dec) {
    if (dec.rejected) dec.rejected = false;
    if (dec.category === "REJECT") delete dec.category;
  }
  const inRec = (state.data.recommendations || []).some(r => r.entity === entity);
  if (inRec) {
    state.data.manual_additions = (state.data.manual_additions || [])
      .filter(m => m.entity !== entity);
    return true;
  }
  if (state.data.manual_additions) {
    state.data.manual_additions = state.data.manual_additions
      .filter(m => m.entity !== entity);
  }
  return false;
}

// #0 fix (ARCH v1.1 §2.1 / ADR-3): pure, deterministic, no state mutation.
// Distinguishes "an entity record with this name exists" (what _absorbReAdd
// answers) from "are THESE selected regions already captured for this
// entity" (what the caller actually needs). Shared by BOTH call-sites
// (commitSelectionAsManual + prSubmitManual) so the fix cannot drift apart
// in a future edit (PM 0732 / IG binding — both sites mandatory).
// Occurrence equality key = (page, xml_text_id) per pack ID-2 / KI:39.
// NOT full-bbox-float equality, NOT word_ids.
function _diffNewOccurrences(combined, builtOccurrences) {
  const existing = (state.data && state.data.recommendations || [])
    .find(r => r.entity === combined);
  if (!existing) return { existing: null, newOccs: builtOccurrences };
  const key = o => `${o.page}:${o.xml_text_id}`;
  const haveKeys = new Set((existing.occurrences || []).map(key));
  const newOccs = (builtOccurrences || []).filter(o => !haveKeys.has(key(o)));
  return { existing, newOccs };
}

// #0 fix (ARCH v1.1 §2.1 steps 4-7 / UX §2 + §4): IDENTICAL post-diff
// control flow shared by BOTH call-sites. `wasRejected` = did step 1's
// _absorbReAdd un-reject the entity (for the NO-OP "restored and already
// fully covered" copy). `occurrences` = the per-site builder output.
function _applyDiffedOccurrences(combined, occurrences, wasRejected) {
  const { existing, newOccs } = _diffNewOccurrences(combined, occurrences || []);
  if (existing == null) {
    // Brand-new entity — caller's PRESERVED fresh-entity path handles it
    // byte-for-byte, INCLUDING the legitimate zero-match case
    // (prSubmitManual originally pushed + toasted "0 matches"). The ERROR
    // state is scoped to an EXISTING entity whose non-empty selection
    // resolved to zero new boxes — never the brand-new path (ARCH §2.1
    // step 4 PRESERVED vs step 7 ERROR; UX §2c).
    return { brandNew: true, occurrences };
  }
  // ERROR (L-6 / UX §2c / ARCH §2.1 step 7): entity EXISTS but the
  // (non-empty-selection) build yielded zero occurrences to diff. Never a
  // silent swallow; selection left intact; NO push.
  if (!occurrences || !occurrences.length) {
    renderEntityList();
    redrawAllBboxes();
    toast(`Couldn't add "${combined}" — selection could not be resolved to a box. Try reselecting.`, "error");
    return { brandNew: false };
  }
  if (newOccs.length > 0) {
    // SUCCESS — push genuinely-new occs ONLY into existing.occurrences
    // (binding data-location contract: pack 04 SC-0; UX §3; ADR-3).
    if (!existing.occurrences) existing.occurrences = [];
    for (const o of newOccs) existing.occurrences.push(o);
    const M = existing.occurrences.length;
    const N = newOccs.length;
    renderEntityList();
    redrawAllBboxes();
    toast(`Added ${N} new box${N !== 1 ? "es" : ""} to "${combined}" — now ${M} total`, "success");
    return { brandNew: false };
  }
  // NO-OP (UX §2b): nothing genuinely new. Success-kind (nothing went
  // wrong). Overlay unchanged (idempotent redraw). _absorbReAdd's
  // un-reject + manual-dedup side-effects were already applied by step 1.
  renderEntityList();
  redrawAllBboxes();
  if (wasRejected) {
    toast(`"${combined}" restored and already fully covered`, "success");
  } else {
    toast(`"${combined}" is already fully covered — nothing new to add`, "success");
  }
  return { brandNew: false };
}

function commitSelectionAsManual() {
  if (!state.selectedKeys.length || !state.data) return;
  const combined = selectionText();
  const cat = (document.getElementById("pr-sel-cat") || {}).value || "OTHER";
  if (!state.data.manual_additions) state.data.manual_additions = [];

  // #0 fix (ARCH v1.1 §2.1 / ADR-3 — BOTH call-sites, PM 0732 / IG binding).
  // Step 1: capture pre-call reject state, then run _absorbReAdd ONLY for its
  // PRESERVED side-effects (un-reject + manual-dedup, I-P3). Its boolean is
  // NO LONGER used to early-return — the occurrence builder below now runs on
  // the name-match path too (the #0 defect was that it was skipped).
  const _decBefore = (state.data.decisions || {})[combined];
  const wasRejected = !!(_decBefore
    && (_decBefore.rejected === true || _decBefore.category === "REJECT"));
  _absorbReAdd(combined);

  // Build occurrences from the user's actual selection — grouped by parent
  // xml_text. One occurrence per xml_text the selection touched. This is
  // always correct regardless of reading-order interleaving. top/bottom
  // inherit from the xml_text (matches backend + pdf_qc Text Band layer).
  const page = state.selectedPage;
  const flat = state.flatWordsByPage[page] || [];
  const byXt = new Map();
  for (const key of state.selectedKeys) {
    const i = state.wordKeyToIdx[key];
    if (i === undefined) continue;
    const { word, xt } = flat[i];
    if (!byXt.has(xt)) byXt.set(xt, []);
    byXt.get(xt).push(word);
  }
  const occurrences = [];
  for (const [xt, words] of byXt.entries()) {
    // Sort by rotation-aware reading order (left→right normally,
    // bottom→top for 90°-rotated vertical text, etc.)
    const ordered = _sortWordsByReadingOrder(words, xt);
    const xtWords = xt.words || [];
    const firstId = xtWords.length ? xtWords[0].id : null;
    const lastId  = xtWords.length ? xtWords[xtWords.length - 1].id : null;
    const matchedIds = ordered.map(w => w.id);
    const left  = matchedIds.includes(firstId) ? xt.left  : Math.min(...ordered.map(w => w.left));
    const right = matchedIds.includes(lastId)  ? xt.right : Math.max(...ordered.map(w => w.right));
    occurrences.push({
      page,
      left,
      top:         xt.top,
      right,
      bottom:      xt.bottom,
      xml_text_id: xt.id,
      word_ids:    matchedIds,
      partial:     false,
      full_text:   ordered.map(w => w.text).join(" "),
    });
  }
  // Also merge any extra matches findOccurrencesInPdf finds (other pages /
  // other instances of the same string), deduped by (page, xml_text_id).
  const seen = new Set(occurrences.map(o => `${o.page}:${o.xml_text_id}`));
  for (const occ of findOccurrencesInPdf(combined)) {
    const k = `${occ.page}:${occ.xml_text_id}`;
    if (!seen.has(k)) { occurrences.push(occ); seen.add(k); }
  }

  // #0 fix: route through the shared post-diff flow. existing==null ⇒
  // brand-new entity ⇒ fall through to the PRESERVED fresh-entity path
  // (manual_additions push + "Added: … (N bbox…)" toast, byte-for-byte).
  // existing!=null ⇒ SUCCESS/NO-OP/ERROR handled inside the helper.
  const _r = _applyDiffedOccurrences(combined, occurrences, wasRejected);
  if (!_r || !_r.brandNew) {
    clearSelection();
    return;
  }

  state.data.manual_additions.push({
    entity: combined, category: cat, tokenize: cat === "ACCOUNT_NUMBER",
    suggested_category: cat, models_voted: ["manual"], vote_count: 1,
    occurrences,
  });
  clearSelection();
  renderEntityList();
  redrawAllBboxes();
  toast(`Added: ${combined} (${occurrences.length} bbox${occurrences.length !== 1 ? "es" : ""})`, "success");
}

window.prCommitSel = commitSelectionAsManual;
window.prClearSel = () => clearSelection();

// ── Clients ───────────────────────────────────────────────────────────────

async function loadClients() {
  try {
    const res = await fetch("/api/pii-review/clients");
    const data = await res.json();
    const sel = document.getElementById("pr-client-select");
    const prev = state.clientId;
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = ""; placeholder.textContent = "— Select client —";
    sel.appendChild(placeholder);
    for (const c of (data.clients || [])) {
      const opt = document.createElement("option");
      opt.value = c.client_id;
      opt.textContent = `${c.client_name}  (${c.pdf_count} pdf, ${c.literal_count} lit)`;
      if (c.client_id === prev) opt.selected = true;
      sel.appendChild(opt);
    }
    // If we had a selection that still exists, keep it; otherwise reset
    if (prev && Array.from(sel.options).some(o => o.value === prev)) {
      sel.value = prev;
    } else {
      state.clientId = null;
      _setClientGatedUi();
    }
  } catch (e) {
    console.error("loadClients failed", e);
  }
}

function _setClientGatedUi() {
  const upload = document.getElementById("pr-btn-upload");
  const fileSel = document.getElementById("pr-file-select");
  const navClient = document.getElementById("pr-nav-client");
  if (state.clientId) {
    upload.disabled = false;
    fileSel.disabled = false;
    const sel = document.getElementById("pr-client-select");
    const label = sel.options[sel.selectedIndex]?.textContent || state.clientId;
    navClient.innerHTML = `<i class="fas fa-user-tag"></i> ${label}`;
    navClient.classList.add("active");
  } else {
    upload.disabled = true;
    fileSel.disabled = true;
    fileSel.innerHTML = "";
    navClient.innerHTML = `<i class="fas fa-user-tag"></i> <em>no client selected</em>`;
    navClient.classList.remove("active");
  }
}

async function prOnClientChange() {
  const sel = document.getElementById("pr-client-select");
  state.clientId = sel.value || null;
  state.stem = null;
  state.data = null;
  state.vizData = null;
  document.getElementById("pr-entity-list").innerHTML = "";
  document.getElementById("pr-pdf-container").innerHTML = "";
  document.getElementById("pr-entity-list").style.display = "none";
  document.getElementById("pr-pdf-container").style.display = "none";
  document.getElementById("pr-left-empty").style.display = "flex";
  document.getElementById("pr-right-empty").style.display = "flex";
  document.getElementById("pr-stat-info").textContent = "";
  _setClientGatedUi();
  if (state.clientId) {
    await loadFiles();
  }
}

function prOpenNewClient() {
  document.getElementById("pr-new-client-modal").style.display = "flex";
  document.getElementById("pr-new-client-name").value = "";
  document.getElementById("pr-new-client-custodian").value = "";
  document.getElementById("pr-new-client-name").focus();
}
function prCancelNewClient() {
  document.getElementById("pr-new-client-modal").style.display = "none";
}
async function prSubmitNewClient() {
  let name = document.getElementById("pr-new-client-name").value.trim();
  const custodian = document.getElementById("pr-new-client-custodian").value.trim();
  if (!name) { toast("Client name required", "error"); return; }
  // Bug 10 fix: normalize NEW client names to UPPERCASE so the on-disk
  // folder name is predictable. Pre-check against existing client_ids
  // case-insensitively so the user sees a clear error instead of the
  // backend silently creating a `_2`-suffixed sibling folder (which
  // produced the TEST_20260519 vs Test_20260519 → Test_20260519_2 bug
  // class). Backend has the same check as defense-in-depth.
  name = name.toUpperCase();
  const sel = document.getElementById("pr-client-select");
  const existingIds = [...sel.options].map(o => o.value).filter(Boolean);
  const collision = existingIds.find(id => id.toUpperCase() === name);
  if (collision) {
    toast(`Client already exists: "${collision}" (case-insensitive match for "${name}")`, "error");
    return;
  }
  try {
    const res = await fetch("/api/pii-review/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, custodian }),
    });
    const data = await res.json();
    if (!data.ok) {
      toast("Create failed: " + (data.error || "?"), "error");
      return;
    }
    toast(`Client created: ${data.client.client_id}`, "success");
    state.clientId = data.client.client_id;
    prCancelNewClient();
    await loadClients();
    document.getElementById("pr-client-select").value = state.clientId;
    _setClientGatedUi();
    await loadFiles();
  } catch (e) {
    toast("Create failed: " + e, "error");
  }
}
window.prOnClientChange   = prOnClientChange;
window.prOpenNewClient    = prOpenNewClient;
window.prCancelNewClient  = prCancelNewClient;
window.prSubmitNewClient  = prSubmitNewClient;

// ── Files ─────────────────────────────────────────────────────────────────

async function loadFiles() {
  if (!state.clientId) return;
  try {
    const res = await fetch(_withClient("/api/pii-review/files"));
    const data = await res.json();
    const sel = document.getElementById("pr-file-select");
    const curValue = sel.value;
    const prevStatus = {};
    for (const opt of sel.options) {
      if (opt.value && opt.dataset.status) prevStatus[opt.value] = opt.dataset.status;
    }
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = ""; placeholder.textContent = "— Select a file —";
    sel.appendChild(placeholder);
    // Track per-stem messages in the OPTION dataset (cheap, survives across
    // poll iterations because options are rebuilt with same dataset.status
    // semantics).
    if (!state._prevMessages) state._prevMessages = {};
    const prevMessages = state._prevMessages;
    const newMessages = {};
    for (const f of (data.files || [])) {
      const opt = document.createElement("option");
      opt.value = f.stem;
      opt.dataset.status = f.status;
      const icon = f.status === "analyzed" ? "✓"
                 : f.status === "analyzing" ? "⟳"
                 : f.status === "applied" ? "✅"
                 : f.status === "error" ? "✗" : "○";
      opt.textContent = `${icon} ${f.filename}` +
        (f.candidates ? ` [${f.candidates} cand.]` : "");
      sel.appendChild(opt);

      // Notify on background-apply transitions. The Apply endpoint returns
      // immediately (work runs in a thread pool), so the user never hears
      // back unless we watch the status poll for the result.
      //
      // Gotcha: the 5s poll can race past the 'applying' state on a fast
      // apply, so checking "prev === 'applying'" misses the transition.
      // Instead, fire on ANY prev → applied/error flip, but suppress the
      // very first render (prev === undefined) so we don't toast stale
      // states on page load.
      const prev = prevStatus[f.stem];
      if (prev !== undefined && prev !== f.status) {
        if (f.status === "applied") {
          toast(`Tokenized PDF ready: ${f.filename}`, "success");
        } else if (f.status === "error") {
          toast(`Apply failed: ${f.message || "see server log"}`, "error");
        }
      }
      // Sibling auto-refresh: backend writes status_message like
      // "+2 new candidate(s) [Example Org, AC-12345]"
      // when diff-propagation adds new literals. Toast on message change.
      const prevMsg = prevMessages[f.stem];
      newMessages[f.stem] = f.message || "";
      if (prevMsg !== undefined
          && prevMsg !== (f.message || "")
          && /new candidate|updated/i.test(f.message || "")) {
        toast(`${f.filename}: ${f.message}`, "success");
      }
    }
    state._prevMessages = newMessages;
    if (curValue) sel.value = curValue;
    if (curValue && prevStatus[curValue] === "analyzing") {
      const nowStatus = (data.files.find(f => f.stem === curValue) || {}).status;
      if (nowStatus === "analyzed" && state.stem === curValue && !state.data) {
        prLoadFile();
      }
    }
  } catch (e) { console.error(e); }
}

function startStatusPoll() {
  if (state._pollTimer) return;
  state._pollTimer = setInterval(() => {
    if (state.clientId) loadFiles();
  }, 5000);
}

// ── Load a file's data ───────────────────────────────────────────────────

// Long-term cleanup Goal 2 (2026-05-19): re-fetch only the per-PDF state
// (pii.json content) from backend and replace state.data + re-render entity
// list / bbox overlay. Does NOT re-fetch viz_data or re-render the PDF —
// those are immutable across Saves and expensive to re-load. Called by
// prSaveDecisions after a successful POST so the FE never shows stale state.
async function refreshStateFromDisk() {
  if (!state.clientId || !state.stem) return;
  try {
    const res = await fetch(_withClient(`/api/pii-review/data?stem=${encodeURIComponent(state.stem)}`));
    if (!res.ok) return;
    const parsed = await res.json();
    if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.recommendations)) return;
    state.data = parsed;
    if (!state.data.recommendations)  state.data.recommendations = [];
    if (!state.data.manual_additions) state.data.manual_additions = [];
    if (!state.data.decisions)        state.data.decisions = {};
    renderEntityList();
    redrawAllBboxes();
  } catch (e) {
    console.warn("[refreshStateFromDisk] failed:", e);
  }
}

async function prLoadFile() {
  const sel = document.getElementById("pr-file-select");
  const stem = sel.value;
  if (!stem) return;

  state.stem = stem;
  state.data = null;
  state.vizData = null;
  state.pageCanvases = {};
  state.activeEntityId = null;
  state.flatWordsByPage = {};
  state.wordKeyToIdx = {};
  clearSelection();

  document.getElementById("pr-left-empty").style.display = "none";
  document.getElementById("pr-right-empty").style.display = "none";
  document.getElementById("pr-entity-list").style.display = "block";
  document.getElementById("pr-pdf-container").style.display = "block";
  // Wipe old DOM immediately. Cards from a previous file carry closure
  // references to that file's state.data; if this file's fetch fails and
  // we return early, leaving those cards in place, any click fires
  // handlers that dereference state.data (now null) and crash with
  // "Cannot read properties of null".
  // Then immediately render a loading state so the user isn't staring at
  // a blank panel while viz_data + PDF (often 50-200MB combined) load.
  const loadingLeft = `
    <div class="pr-loading">
      <div class="pr-spinner"></div>
      <div class="pr-loading-title">Loading entities…</div>
      <div class="pr-loading-sub">${stem}</div>
    </div>`;
  const loadingRight = `
    <div class="pr-loading">
      <div class="pr-spinner"></div>
      <div class="pr-loading-title">Loading PDF…</div>
      <div class="pr-loading-sub">fetching viz_data + rendering pages</div>
    </div>`;
  document.getElementById("pr-entity-list").innerHTML  = loadingLeft;
  document.getElementById("pr-pdf-container").innerHTML = loadingRight;
  document.getElementById("pr-stat-info").textContent = `Loading ${stem}…`;

  try {
    const [piiRes, vizRes] = await Promise.all([
      fetch(_withClient(`/api/pii-review/data?stem=${encodeURIComponent(stem)}`)),
      fetch(_withClient(`/api/pii-review/viz-data?stem=${encodeURIComponent(stem)}`)),
    ]);
    if (!piiRes.ok) { alert("Analysis not ready yet"); return; }
    const parsed = await piiRes.json();
    if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.recommendations)) {
      alert("Analysis data malformed or still being written — try again in a few seconds");
      state.data = null;
      return;
    }
    state.data = parsed;
    // Defensive defaults so downstream never dereferences undefined
    if (!state.data.recommendations)     state.data.recommendations = [];
    if (!state.data.manual_additions)    state.data.manual_additions = [];
    if (!state.data.decisions)           state.data.decisions = {};

    if (vizRes.ok) {
      state.vizData = await vizRes.json();
      // Synthesise a virtual word for xml_texts where the v4 extractor
      // populated `content` but left `words[]` empty. Without this the
      // overlay hover finds the bbox (uses xt.left/top/right/bottom) but
      // click fails (xmlTextWordKeys filters words[].text → empty array).
      // See pdf_qc bug: same row + same content extracts cleanly on most
      // pages but drops words[] on a few. Workaround until the extractor
      // is fixed at source.
      let _synth = 0;
      for (const pdata of Object.values(state.vizData.pages || {})) {
        for (const xt of (pdata.xml_texts || [])) {
          if ((!xt.words || xt.words.length === 0) && xt.content) {
            xt.words = [{
              id:     `${xt.id}.0`,
              text:   xt.content,
              left:   xt.left,
              top:    xt.top,
              right:  xt.right,
              bottom: xt.bottom,
              _synth: true,
            }];
            _synth++;
          }
        }
      }
      if (_synth) console.log(`[viz_data] synthesised ${_synth} virtual word(s) from content for empty-words xml_texts`);
    } else {
      console.warn("viz_data not available — click-to-select disabled");
    }

    buildFlatWordIndex();

    const info = document.getElementById("pr-stat-info");
    info.textContent = `${state.data.recommendations.length} candidates, `
      + `${state.data.total_pages || 0} pages`;

    renderEntityList();
    await renderPdf();
  } catch (e) { console.error(e); alert("Load failed: " + e); }
}

// Build reading-order flat word list per page from viz_data xml_texts.
// Skips words whose text strips to empty (lone punctuation like "." between
// "P.O" and "BOX" — viz_data tokenises "P.O." as 3 separate words including
// a bare "."). Without this skip, selectionText() joins the bare "." into
// the saved entity ("P.O . BOX...") AND findOccurrencesInPdf can't align
// target ['p.o','box'] against flat ['p.o','','box'] on other pages.
function buildFlatWordIndex() {
  state.flatWordsByPage = {};
  state.wordKeyToIdx = {};
  if (!state.vizData || !state.vizData.pages) return;
  for (const [pStr, pdata] of Object.entries(state.vizData.pages)) {
    const page = Number(pStr);
    const flat = [];
    for (const xt of (pdata.xml_texts || [])) {
      for (const w of (xt.words || [])) {
        if (!w.text) continue;
        if (!stripWord(w.text)) continue;   // lone punctuation
        flat.push({ word: w, xt, wid: w.id, xid: xt.id });
      }
    }
    flat.sort((a, b) =>
      Math.round(a.word.top) - Math.round(b.word.top) ||
      a.word.left - b.word.left
    );
    state.flatWordsByPage[page] = flat;
    flat.forEach((f, i) => {
      state.wordKeyToIdx[`${page}:${f.wid}`] = i;
    });
  }
}

// ── Render entity list grouped by page ───────────────────────────────────

// "Rejected" = user clicked X → card lives in the rejected pool.
// A card that's only unticked (tokenize=false) STAYS in the active list,
// just dimmed and not tokenized on Apply.
function isRejected(entity) {
  const dec = (state.data?.decisions || {})[entity];
  if (dec) {
    // User decision wins in both directions — including explicit un-reject
    // (rejected:false) so the restore arrow works on auto-rejected items.
    if (dec.rejected === true) return true;
    if (dec.category === "REJECT") return true;
    if (dec.rejected === false) return false;
  }
  // Default auto-reject: below-threshold PERSON candidates land in the
  // Rejected pool on initial load. User can restore them with the ↻ arrow
  // which writes decisions[entity].rejected = false and re-enters above.
  const rec = (state.data?.recommendations || []).find(r => r.entity === entity);
  if (rec && rec.suggested_category === "PERSON" && rec.consensus_passed === false) {
    return true;
  }
  return false;
}

// ── Token assignment ─────────────────────────────────────────────────────
//
// Each tokenized candidate gets a per-category token (NAME_1, NAME_2, ...).
// Rules:
//   • Only entities that are tokenize=true AND not rejected consume a K
//   • User can override via the token dropdown on the card (= aliasing)
//   • Unless overridden, default = smallest unused K in the category
//   • Toggling tokenize off / rejecting frees the K for reuse
//   • Processing order = first-occurrence (page, top, left) per category
//
// Returns { assignments: Map<entity, "NAME_3">, usedByCat: Map<"NAME", Set<int>> }

function firstOccurrenceKey(e) {
  // Topmost-leftmost occurrence across pages — must be a true min over the
  // array, not occurrences[0] which is whatever the matcher emitted first.
  // Manual entities pushed mid-session can have occurrences in arbitrary
  // order (frontend captures user selection occs first, then appends
  // findOccurrencesInPdf hits). Backend uses min() too, so match it.
  const occs = e.occurrences || [];
  if (!occs.length) return [999, 1e9, 1e9];
  let best = occs[0];
  for (const o of occs) {
    const a = [o.page ?? 999, o.top ?? 1e9, o.left ?? 1e9];
    const b = [best.page ?? 999, best.top ?? 1e9, best.left ?? 1e9];
    if (a[0] < b[0] || (a[0] === b[0] && (a[1] < b[1] || (a[1] === b[1] && a[2] < b[2])))) best = o;
  }
  return [best.page ?? 999, best.top ?? 1e9, best.left ?? 1e9];
}

function entityDecision(e) {
  return (state.data?.decisions || {})[e.entity] || {};
}

// Default: only ACCOUNT_NUMBER (and user manual additions) auto-tokenize.
// Every other detected category starts with the toggle OFF — user opts in.
function defaultTokenizeFor(r) {
  // Manual additions carry their own tokenize flag (set at add time per
  // category). Use it verbatim — do NOT force true. Only ACCOUNT_NUMBER
  // manual adds default to tokenize; everything else defaults to redact-only.
  if (r._manual) return r.tokenize === true;
  return r.suggested_category === "ACCOUNT_NUMBER";
}

function isTokenizeOn(e) {
  const dec = entityDecision(e);
  if (isRejected(e.entity)) return false;
  return (dec.tokenize !== undefined) ? dec.tokenize : defaultTokenizeFor(e);
}

function effectiveCategory(e) {
  const dec = entityDecision(e);
  return dec.category || e.suggested_category;
}

function entityOverride(e) {
  // Long-term cleanup (2026-05-19): single source of truth for tokens.
  // Reads only from state.data.decisions[entity].token. The legacy
  // `e._manual && e.token` fallback was removed — manual_additions[].token
  // is no longer written by FE. Backend reads of m.token still work for
  // backward compat with pre-cleanup pii.json files, but the authoritative
  // override is decisions.token (pii_review.py:_merge_into_client_config).
  const dec = entityDecision(e);
  return dec.token || null;
}

function parseTokenIndex(tok, cat) {
  const prefix = CAT_PREFIX[cat] || "OTHER";
  const m = (tok || "").match(new RegExp(`^${prefix}_(\\d+)$`));
  return m ? parseInt(m[1], 10) : null;
}

function computeTokenAssignments() {
  if (!state.data) return { assignments: new Map(), usedByCat: new Map() };
  // Build entity → preferred-source map, deduped. Recommendations win over
  // manual_additions (mirrors backend frontend_occs precedence). Without
  // dedup, the same entity from BOTH arrays each consume a K via Pass 1
  // and clobber each other's assignment — visible symptom: a 3-account PDF
  // shows tokens [ACCOUNT_2] [ACCOUNT_4] [ACCOUNT_6] instead of _1/_2/_3
  // because each entity is processed twice.
  const byEntity = new Map();
  for (const r of (state.data.recommendations || [])) {
    if (!r.entity) continue;
    if (!byEntity.has(r.entity)) byEntity.set(r.entity, { ...r, _manual: false });
  }
  for (const m of (state.data.manual_additions || [])) {
    if (!m.entity) continue;
    if (!byEntity.has(m.entity)) byEntity.set(m.entity, { ...m, _manual: true });
  }
  const all = [...byEntity.values()];

  // Filter to entities that actually appear on this PDF.
  const active = all.filter(e => isTokenizeOn(e) && (e.occurrences || []).length > 0);
  // Sort by first-occurrence reading order. K NUMBERS ARE PURE FUNCTION OF
  // POSITION — no locks, no overrides. Adding a new entity at an earlier
  // position renumbers everyone after it. UI is fluid at all times.
  active.sort((a, b) => {
    const [ap, at, al] = firstOccurrenceKey(a);
    const [bp, bt, bl] = firstOccurrenceKey(b);
    return ap - bp || at - bt || al - bl;
  });

  // Aliasing is preserved via "shared stored token" grouping. When the
  // user picks K from the token dropdown for entity Y matching entity X's
  // current K, both end up with the same decisions[].token string — that
  // string is treated as a GROUP ID, not as a fixed K. On render, the
  // canonical (first in first-occ order) gets the next sequential K, and
  // the others in the same group inherit it. Result: alias survives, K
  // stays fluid.
  const groupOf = new Map();   // entity → group key (their stored token string)
  for (const e of active) {
    const stored = entityOverride(e);   // decisions[ent].token or m.token
    if (stored) groupOf.set(e.entity, stored);
  }
  // For each group, the FIRST entity (in first-occ order) is canonical;
  // others alias to it. Singleton groups (only one entity) are no-ops.
  const groupCanonical = new Map();   // group key → canonical entity
  for (const e of active) {
    const g = groupOf.get(e.entity);
    if (!g) continue;
    if (!groupCanonical.has(g)) groupCanonical.set(g, e.entity);
  }
  const aliasTarget = new Map();   // entity → canonical entity (within group)
  for (const e of active) {
    const g = groupOf.get(e.entity);
    if (!g) continue;
    const canon = groupCanonical.get(g);
    if (canon && canon !== e.entity) aliasTarget.set(e.entity, canon);
  }

  // Sequential K per category, in first-occ order. Aliases skip and inherit.
  const usedByCat = new Map();
  const assignments = new Map();
  function ensureSet(prefix) {
    if (!usedByCat.has(prefix)) usedByCat.set(prefix, new Set());
    return usedByCat.get(prefix);
  }
  function nextK(prefix) {
    const used = ensureSet(prefix);
    let k = 1;
    while (used.has(k)) k++;
    used.add(k);
    return k;
  }

  // Pass 1: non-alias entities (incl. canonical of each alias group)
  for (const e of active) {
    if (aliasTarget.has(e.entity)) continue;
    const cat = effectiveCategory(e);
    const prefix = CAT_PREFIX[cat] || "OTHER";
    const k = nextK(prefix);
    assignments.set(e.entity, `${prefix}_${k}`);
  }
  // Pass 2: aliases inherit canonical's assignment
  for (const e of active) {
    if (assignments.has(e.entity)) continue;
    const canon = aliasTarget.get(e.entity);
    if (canon && assignments.has(canon)) {
      assignments.set(e.entity, assignments.get(canon));
    } else {
      // Fallback (canonical not active for some reason) — give own K
      const cat = effectiveCategory(e);
      const prefix = CAT_PREFIX[cat] || "OTHER";
      const k = nextK(prefix);
      assignments.set(e.entity, `${prefix}_${k}`);
    }
  }

  return { assignments, usedByCat };
}

// Called when the user picks a different token from the dropdown
function setEntityToken(entity, token, isManual) {
  if (!state.data) return;
  // Long-term cleanup (2026-05-19): single source of truth.
  // Writes go to state.data.decisions[entity].token only. No more
  // m.token writes (the asymmetric storage that caused Bug 3 has been
  // eliminated; entityOverride reads from decisions only). isManual
  // parameter is retained for backwards compat with the dropdown's
  // onchange call site but no longer affects behavior.
  const writeStored = (ent, tok) => {
    if (!state.data.decisions) state.data.decisions = {};
    const prev = state.data.decisions[ent] || {};
    if (tok) state.data.decisions[ent] = { ...prev, token: tok };
    else     { delete prev.token; state.data.decisions[ent] = prev; }
  };
  // Canonical-write: when aliasing to an existing K, also persist the
  // canonical's stored token so computeTokenAssignments sees both
  // entities in the same group on the next render (otherwise the canonical
  // becomes a singleton and prSaveDecisions overwrites the alias with
  // fresh K's). Fires unconditionally — provenance no longer matters.
  if (token) {
    const { assignments } = computeTokenAssignments();
    let canonical = null;
    for (const [ent, tok] of assignments.entries()) {
      if (ent !== entity && tok === token) { canonical = ent; break; }
    }
    if (canonical) writeStored(canonical, token);
  }
  writeStored(entity, token);
  renderEntityList();
  redrawAllBboxes();
}

function renderEntityList() {
  const container = document.getElementById("pr-entity-list");
  container.innerHTML = "";
  if (!state.data) return;

  // Dedupe: when a manual_addition entity has already been promoted to a
  // CRM rec (entity exists in both arrays), only show the CRM rec — that's
  // the source of truth post-promotion. Avoids 2 cards per entity on files
  // where the user added it AND backfill/propagation re-added it as a rec.
  const all = [];
  const seenEntities = new Set();
  for (const r of (state.data.recommendations || [])) {
    if (!(r.occurrences || []).length) continue;
    seenEntities.add(r.entity);
    all.push({ ...r, _manual: false });
  }
  for (const m of (state.data.manual_additions || [])) {
    if (seenEntities.has(m.entity)) continue;  // already shown as CRM rec
    all.push({ ...m, _manual: true });
  }

  // Split into active vs rejected pool
  const active = all.filter(r => !isRejected(r.entity));
  const rejected = all.filter(r => isRejected(r.entity));

  // Compute token assignments once — shared across all cards this render.
  const { assignments, usedByCat } = computeTokenAssignments();

  // ── Active entities — grouped by first-occurrence page, in order ──
  // Sorted by (page, top, left). Entities with no occurrence use a high
  // page sentinel so they land AFTER real pages rather than at "Page 0".
  active.sort((a, b) => {
    const [ap, at, al] = firstOccurrenceKey(a);
    const [bp, bt, bl] = firstOccurrenceKey(b);
    return ap - bp || at - bt || al - bl;
  });
  const byPage = new Map();
  for (const r of active) {
    const p = r.occurrences?.[0]?.page;
    const key = (p === undefined || p === null) ? "—" : p;
    if (!byPage.has(key)) byPage.set(key, []);
    byPage.get(key).push(r);
  }
  for (const [p, list] of byPage.entries()) {
    const grp = document.createElement("div");
    grp.className = "pr-page-group";
    grp.id = `pr-pgroup-${p}`;
    const hdr = document.createElement("div");
    hdr.className = "pr-page-header";
    hdr.textContent = p === "—"
      ? `(no page match) — ${list.length} candidate(s)`
      : `Page ${p} — ${list.length} candidate(s)`;
    grp.appendChild(hdr);
    for (const r of list) grp.appendChild(renderEntityCard(r, false, assignments, usedByCat));
    container.appendChild(grp);
  }

  const addBtn = document.createElement("button");
  addBtn.className = "pr-btn pr-btn-primary";
  addBtn.style.cssText = "margin: 12px 6px; width: calc(100% - 12px); justify-content: center;";
  addBtn.innerHTML = `<i class="fas fa-plus"></i> Add manual PII entity`;
  addBtn.onclick = () => openManualModal();
  container.appendChild(addBtn);

  // ── Rejected pool at the bottom ──
  if (rejected.length) {
    const pool = document.createElement("div");
    pool.className = "pr-page-group pr-rejected-pool";
    pool.style.marginTop = "18px";
    const phdr = document.createElement("div");
    phdr.className = "pr-page-header";
    phdr.style.cssText = "background:#f0f0f0;color:#888;";
    phdr.innerHTML = `<i class="fas fa-trash-alt"></i> Rejected (${rejected.length}) — click ↻ to restore`;
    pool.appendChild(phdr);
    for (const r of rejected) pool.appendChild(renderEntityCard(r, true, assignments, usedByCat));
    container.appendChild(pool);
  }

  // Limitation footer
  const note = document.createElement("div");
  note.style.cssText = "margin: 8px 10px; font-size: 10px; color: #888; line-height: 1.4;";
  note.innerHTML = `<i class="fas fa-info-circle"></i> Exact-string matching only.
    "Mr K Adams" ≠ "K Adams". Aliasing is done outside this tool.`;
  container.appendChild(note);
}

function renderEntityCard(r, rejected, assignments, usedByCat) {
  const card = document.createElement("div");
  card.className = "pr-entity" + (rejected ? " rejected" : "");
  card.dataset.entityId = r.id || `m_${r.entity}`;
  card.dataset.entity = r.entity;
  card.style.position = "relative";

  const dec = (state.data.decisions || {})[r.entity] || {};
  const userCat = dec.category || r.suggested_category;

  // ── Corner action buttons ──
  // Right-most:  X (reject → moves to rejected pool, restorable)  /
  //              ↻ (restore from rejected pool)
  // Next to it:  🗑 (REMOVE — hard delete, no undo). Use for entries
  //              typed wrongly that you want gone, not just hidden.
  const action = document.createElement("button");
  action.className = "pr-card-action";
  action.title = rejected ? "Restore" : "Reject (move to rejected pool)";
  action.innerHTML = rejected
    ? '<i class="fas fa-rotate-left"></i>'
    : '<i class="fas fa-xmark"></i>';
  action.style.cssText = "position:absolute;top:6px;right:6px;width:22px;height:22px;"
    + "border:none;border-radius:50%;background:" + (rejected ? "#d0d0d0" : "#f5c6cb") + ";"
    + "color:" + (rejected ? "#555" : "#842029") + ";font-size:11px;cursor:pointer;"
    + "display:flex;align-items:center;justify-content:center;padding:0;";
  action.onclick = (e) => {
    e.stopPropagation();
    if (rejected) restoreEntity(r.entity);
    else          rejectEntity(r.entity, userCat);
  };
  card.appendChild(action);

  const removeAction = document.createElement("button");
  removeAction.className = "pr-card-action";
  removeAction.title = "Remove permanently (cannot be restored — for entries added by mistake)";
  removeAction.innerHTML = '<i class="fas fa-trash"></i>';
  removeAction.style.cssText = "position:absolute;top:6px;right:32px;width:22px;height:22px;"
    + "border:none;border-radius:50%;background:#e2e3e5;"
    + "color:#495057;font-size:10px;cursor:pointer;"
    + "display:flex;align-items:center;justify-content:center;padding:0;";
  removeAction.onclick = async (e) => {
    e.stopPropagation();
    const ok = await prConfirm(
      `Remove "${r.entity}" from this PDF's UI?\n\n` +
      `Save commits the removal to pii.json and the client CRM. ` +
      `Apply additionally regenerates the tokenized PDF without it.`,
      { confirmText: "Remove", cancelText: "Cancel", danger: true }
    );
    if (ok) removeEntity(r.entity);
  };
  card.appendChild(removeAction);

  const txt = document.createElement("div");
  txt.className = "pr-ent-text";
  txt.style.paddingRight = "28px";
  txt.textContent = r.entity;
  card.appendChild(txt);

  const meta = document.createElement("div");
  meta.className = "pr-ent-meta";

  const catBadge = document.createElement("span");
  catBadge.className = "pr-badge pr-badge-cat";
  catBadge.textContent = r.suggested_category;
  meta.appendChild(catBadge);

  const voters = r.models_voted || [];
  for (const v of voters) {
    const b = document.createElement("span");
    b.className = v.startsWith("regex") ? "pr-badge pr-badge-regex" : "pr-badge pr-badge-model";
    b.textContent = v;
    meta.appendChild(b);
  }
  if (r._manual) {
    const b = document.createElement("span");
    b.className = "pr-badge pr-badge-regex";
    b.textContent = "manual";
    meta.appendChild(b);
  }
  if (r.alias_of) {
    const b = document.createElement("span");
    b.className = "pr-badge pr-badge-regex";
    b.style.background = "#d1ecf1"; b.style.color = "#0c5460";
    b.innerHTML = `<i class="fas fa-link"></i> alias of ${r.alias_of}`;
    meta.appendChild(b);
  }
  if (voters.length) {
    const vb = document.createElement("span");
    vb.className = "pr-badge pr-badge-votes";
    vb.textContent = `${voters.length} vote${voters.length > 1 ? "s" : ""}`;
    meta.appendChild(vb);
  }
  if (r.consensus_passed !== undefined) {
    const cb = document.createElement("span");
    cb.className = "pr-badge " + (r.consensus_passed ? "pr-badge-pass" : "pr-badge-fail");
    cb.textContent = r.consensus_passed ? "consensus ✓" : "below threshold";
    meta.appendChild(cb);
  }
  const occ = r.occurrences || [];
  const occBadge = document.createElement("span");
  occBadge.className = "pr-badge pr-badge-occ";
  occBadge.textContent = `${occ.length} occurrence${occ.length !== 1 ? "s" : ""}`;
  meta.appendChild(occBadge);

  card.appendChild(meta);

  // ── Controls — only on ACTIVE cards ──
  if (!rejected) {
    const userTokenize = (dec.tokenize !== undefined)
      ? dec.tokenize
      : defaultTokenizeFor(r);

    const ctrl = document.createElement("div");
    ctrl.className = "pr-ent-meta";
    ctrl.style.marginTop = "6px";

    const catSel = document.createElement("select");
    catSel.className = "pr-cat-select";
    for (const c of CATEGORIES) {
      if (c === "REJECT") continue;
      const opt = document.createElement("option");
      opt.value = c; opt.textContent = c;
      if (c === userCat) opt.selected = true;
      catSel.appendChild(opt);
    }
    catSel.onchange = (e) => {
      e.stopPropagation();
      updateDecision(r.entity, { category: catSel.value, tokenize: tokChk.checked });
    };
    ctrl.appendChild(catSel);

    const tokWrap = document.createElement("span");
    tokWrap.className = "pr-tokenize-wrap";
    tokWrap.innerHTML = `
      <label class="pr-toggle" title="Tokenize on Apply">
        <input type="checkbox" ${userTokenize ? "checked" : ""}>
        <span class="pr-toggle-slider"></span>
      </label>
      <span class="pr-toggle-label">tokenize</span>
    `;
    const tokChk = tokWrap.querySelector("input");
    tokChk.onclick = (e) => e.stopPropagation();
    tokChk.onchange = (e) => {
      e.stopPropagation();
      updateDecision(r.entity, { category: catSel.value, tokenize: tokChk.checked });
    };
    ctrl.appendChild(tokWrap);

    // ── Token dropdown (only when tokenize is ON) ──
    if (userTokenize && assignments) {
      const currentTok = assignments.get(r.entity);
      const prefix = CAT_PREFIX[userCat] || "OTHER";
      const used = new Set((usedByCat && usedByCat.get(prefix)) || []);
      // Options = all currently-used K's in this category + next-fresh K
      const opts = new Set();
      for (const k of used) opts.add(`${prefix}_${k}`);
      let fresh = 1;
      while (used.has(fresh)) fresh++;
      opts.add(`${prefix}_${fresh}`);

      const tokSel = document.createElement("select");
      tokSel.className = "pr-token-select";
      tokSel.title = "Change = alias to another entity's token, or pick next fresh";
      const sortedOpts = [...opts].sort((a, b) => {
        const ka = parseInt(a.split("_").pop(), 10);
        const kb = parseInt(b.split("_").pop(), 10);
        return ka - kb;
      });
      for (const tokName of sortedOpts) {
        const opt = document.createElement("option");
        opt.value = `[${tokName}]`;
        opt.textContent = `[${tokName}]`;
        if (`[${tokName}]` === `[${currentTok}]`) opt.selected = true;
        tokSel.appendChild(opt);
      }
      tokSel.onchange = (e) => {
        e.stopPropagation();
        const picked = tokSel.value.replace(/^\[|\]$/g, "");
        setEntityToken(r.entity, picked, r._manual);
      };
      ctrl.appendChild(tokSel);
    }

    card.appendChild(ctrl);
  }

  if (occ.length) {
    const occDiv = document.createElement("div");
    occDiv.className = "pr-occ-list";
    occDiv.appendChild(document.createTextNode("pages: "));
    const pages = [...new Set(occ.map(o => o.page))].sort((a, b) => a - b);
    for (const p of pages) {
      const lnk = document.createElement("span");
      lnk.className = "pr-occ-link";
      lnk.textContent = p;
      lnk.onclick = (e) => { e.stopPropagation(); focusEntity(r, p); };
      occDiv.appendChild(lnk);
    }
    card.appendChild(occDiv);
  }

  card.onclick = () => focusEntity(r, (occ[0] || {}).page || 1);

  return card;
}

function rejectEntity(entity, currentCat) {
  if (!state.data) return;
  if (!state.data.decisions) state.data.decisions = {};
  state.data.decisions[entity] = {
    category: currentCat || "OTHER",
    tokenize: false,
    rejected: true,
  };
  renderEntityList();
  redrawAllBboxes();
}

// Custom HTML confirm — replaces native confirm() which blocks the JS thread
// and forces the browser to flush pending paints (24 PDF canvases + overlays
// + smooth-scroll) before showing the modal, causing a noticeable hang on
// click. This one is async, non-blocking, and pops instantly.
//
// Returns a promise that resolves to true (confirm) or false (cancel/Esc).
function prConfirm(message, opts = {}) {
  const { confirmText = "Confirm", cancelText = "Cancel", danger = false } = opts;
  return new Promise(resolve => {
    const modal = document.createElement("div");
    modal.className = "pr-modal";
    modal.style.cssText = "display:flex;";
    const okStyle = danger
      ? "background:var(--danger);color:#fff;border-color:var(--danger);font-weight:600;"
      : "";
    modal.innerHTML = `
      <div class="pr-modal-content" style="min-width:360px;max-width:480px;">
        <div style="font-size:13px;line-height:1.5;margin-bottom:16px;white-space:pre-wrap;">${
          message.replace(/</g, "&lt;")
        }</div>
        <div class="pr-modal-actions">
          <button class="pr-btn" data-act="cancel">${cancelText}</button>
          <button class="pr-btn" data-act="ok" style="${okStyle}">${confirmText}</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    const onKey = (e) => {
      if (e.key === "Escape") { cleanup(false); }
      else if (e.key === "Enter") { cleanup(true); }
    };
    const cleanup = (val) => {
      document.removeEventListener("keydown", onKey);
      modal.remove();
      resolve(val);
    };
    modal.querySelector('[data-act="cancel"]').onclick = () => cleanup(false);
    modal.querySelector('[data-act="ok"]').onclick     = () => cleanup(true);
    // Click outside the content card → cancel
    modal.onclick = (e) => { if (e.target === modal) cleanup(false); };
    document.addEventListener("keydown", onKey);
    modal.querySelector('[data-act="ok"]').focus();
  });
}

// Hard remove from in-memory UI state. Pure UI — no CRM / no backend
// touch. Strips the entity from manual_additions[], recommendations[],
// and decisions{}. Save/Apply later commits the in-memory state to disk
// (CRM gets updated then, not now). Re-adding the same string later
// starts completely fresh.
function removeEntity(entity /* isManual ignored — UI is one source */) {
  if (!state.data) return;
  // Bug 6: track explicit user-driven deletions so the backend doesn't
  // mistake state drift for intentional removal. Save sends this list as
  // `deleted_entities`; backend strips ONLY these from the CRM and
  // carries forward everything else from the prior client_config.
  if (!state.deletedEntities) state.deletedEntities = new Set();
  state.deletedEntities.add(entity);
  if (state.data.manual_additions) {
    state.data.manual_additions = state.data.manual_additions
      .filter(m => m.entity !== entity);
  }
  if (state.data.recommendations) {
    state.data.recommendations = state.data.recommendations
      .filter(r => r.entity !== entity);
  }
  if (state.data.decisions && state.data.decisions[entity]) {
    delete state.data.decisions[entity];
  }
  renderEntityList();
  redrawAllBboxes();
}

function restoreEntity(entity) {
  if (!state.data) return;
  if (!state.data.decisions) state.data.decisions = {};
  // Write rejected:false explicitly, don't delete the decision. Auto-rejected
  // items (below-threshold PERSONs) have no decision entry at all — if we
  // deleted here, isRejected's fallback rule would keep them in the pool.
  // Also reset tokenize to category default — rejectEntity forces
  // tokenize:false so the entity stops being drawn while rejected. On
  // restore, we must un-force it or the entity comes back invisible
  // (empty token, no overlay) instead of fully active.
  const prev = state.data.decisions[entity] || {};
  // Recover the entity record to read its category for default tokenize
  const recOrManual =
    (state.data.recommendations || []).find(r => r.entity === entity) ||
    (state.data.manual_additions || []).find(m => m.entity === entity);
  const cat = prev.category || recOrManual?.suggested_category || recOrManual?.category || 'OTHER';
  state.data.decisions[entity] = {
    ...prev,
    rejected: false,
    tokenize: defaultTokenizeFor(recOrManual || { suggested_category: cat }),
  };
  renderEntityList();
  redrawAllBboxes();
}

function updateDecision(entity, dec) {
  if (!state.data) return;
  if (!state.data.decisions) state.data.decisions = {};
  const prev = state.data.decisions[entity] || {};
  // If the category changed, the old token override (tied to the old
  // category's prefix) no longer makes sense — drop it so the algorithm
  // picks a fresh default in the new category.
  const prevCat = prev.category;
  const catChanged = prevCat && prevCat !== dec.category;
  const keepToken = !catChanged && prev.token;
  state.data.decisions[entity] = {
    category: dec.category,
    tokenize: dec.tokenize,
    ...(prev.rejected ? { rejected: true } : {}),
    ...(keepToken ? { token: prev.token } : {}),
  };
  // FE-2 fix: mirror category + tokenize back into manual_additions[ent]
  // if this entity was added manually. Without this, _merge_into_client_config
  // absorbs the stale manual_additions row on Save and the literal lands in
  // _client.json with the OLD tokenize/category, ignoring user's card-side edit.
  if (state.data.manual_additions) {
    for (const m of state.data.manual_additions) {
      if (m.entity === entity) {
        m.category = dec.category;
        m.tokenize = dec.tokenize;
      }
    }
  }
  // Long-term cleanup (2026-05-19): m.token writes removed — single source
  // of truth is now decisions[entity].token. The previous `delete m.token`
  // on category change is dead code in the new model and has been removed.
  renderEntityList();
  redrawAllBboxes();
}

// ── Focus / highlight on PDF ─────────────────────────────────────────────

function focusEntity(r, pageNum) {
  state.activeEntityId = r.id || `m_${r.entity}`;
  document.querySelectorAll(".pr-entity").forEach(el => el.classList.remove("active"));
  const card = document.querySelector(`.pr-entity[data-entity="${CSS.escape(r.entity)}"]`);
  if (card) card.classList.add("active");

  const wrapper = document.getElementById(`pr-pdf-page-${pageNum}`);
  if (wrapper) wrapper.scrollIntoView({ behavior: "smooth", block: "start" });

  redrawAllBboxes();
}

// ── PDF rendering ────────────────────────────────────────────────────────

async function renderPdf() {
  const container = document.getElementById("pr-pdf-container");
  container.innerHTML = "";
  if (!pdfjsLib) { container.textContent = "pdf.js not loaded"; return; }

  const url = _withClient(`/api/pii-review/pdf/${encodeURIComponent(state.stem + ".pdf")}`);
  try { state.pdfDoc = await pdfjsLib.getDocument(url).promise; }
  catch (e) { container.textContent = "PDF load failed: " + e; return; }

  const panelW = document.getElementById("pr-right").clientWidth - 20;
  const dpr = window.devicePixelRatio || 1;

  for (let p = 1; p <= state.pdfDoc.numPages; p++) {
    // Per-page label, same pattern as pdf_qc's right panel header.
    const label = document.createElement("div");
    label.className = "pr-pdf-page-label";
    label.textContent = `PAGE ${p} / ${state.pdfDoc.numPages}`;
    container.appendChild(label);

    const wrapper = document.createElement("div");
    wrapper.className = "pr-pdf-page-wrapper";
    wrapper.id = `pr-pdf-page-${p}`;
    container.appendChild(wrapper);

    const page = await state.pdfDoc.getPage(p);
    const natural = page.getViewport({ scale: 1 });
    const scale = Math.min(panelW / natural.width, 3.0);
    const vp = page.getViewport({ scale: scale * dpr });
    const canvas = document.createElement("canvas");
    // Only set the INTRINSIC pixel buffer size. Display size comes from
    // CSS (.pr-pdf-page-wrapper canvas { width: 100%; height: auto }). The
    // browser scales the high-DPR buffer to whatever the container is —
    // GPU-accelerated, no re-render needed when the resize divider moves.
    canvas.width = vp.width; canvas.height = vp.height;
    wrapper.appendChild(canvas);
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;

    const overlay = document.createElement("canvas");
    overlay.className = "pr-bbox-overlay";
    overlay.width = vp.width; overlay.height = vp.height;
    wrapper.appendChild(overlay);

    const vizPage = state.vizData?.pages?.[p] || {};
    const layout  = vizPage.page_layout || {};
    state.pageCanvases[p] = {
      overlay,
      pdfW: layout.width  || natural.width,
      pdfH: layout.height || natural.height,
      scale: scale * dpr, dpr,
      xml_texts: vizPage.xml_texts || [],
    };

    overlay.style.pointerEvents = "auto";
    overlay.style.cursor = "default";
    overlay.addEventListener("mousemove", e => onOverlayMove(e, p));
    overlay.addEventListener("mouseleave", () => { state.hoverText = null; hideTwin(); });
    overlay.addEventListener("click", e => {
      try {
        onOverlayClick(e, p);
      } catch (err) {
        console.error(`[click p${p}] HANDLER THREW`, err);
      }
    });
    console.log(`[overlay attach] page ${p}: canvas ${overlay.width}x${overlay.height}, listeners bound`);
  }
  redrawAllBboxes();
}

// ── Hit-testing ──────────────────────────────────────────────────────────

// xml_text hit-test (whole line)
function hitXmlTextAt(pageNum, pdfX, pdfY) {
  const info = state.pageCanvases[pageNum];
  if (!info) return null;
  let best = null, bestArea = Infinity;
  for (const t of info.xml_texts) {
    if (pdfX >= t.left && pdfX <= t.right
        && pdfY >= t.top && pdfY <= t.bottom) {
      const area = (t.right - t.left) * (t.bottom - t.top);
      if (area < bestArea) { bestArea = area; best = t; }
    }
  }
  return best;
}

// single-word hit-test inside an xml_text
function hitWordAt(pageNum, pdfX, pdfY) {
  const xt = hitXmlTextAt(pageNum, pdfX, pdfY);
  if (!xt) return null;
  let best = null, bestArea = Infinity;
  for (const w of (xt.words || [])) {
    if (pdfX >= w.left && pdfX <= w.right
        && pdfY >= w.top && pdfY <= w.bottom) {
      const area = (w.right - w.left) * (w.bottom - w.top);
      if (area < bestArea) { bestArea = area; best = { word: w, xt }; }
    }
  }
  return best;
}

function overlayCoords(e, pageNum) {
  const info = state.pageCanvases[pageNum];
  if (!info) return null;
  const rect = info.overlay.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const pdfX = (e.clientX - rect.left) / rect.width  * info.pdfW;
  const pdfY = (e.clientY - rect.top)  / rect.height * info.pdfH;
  return { pdfX, pdfY };
}

// ── Selection logic (word-keyed) ─────────────────────────────────────────

// All selection is stored as "page:word_id". An xml_text click expands to
// all its word keys. Multi-select allows any combination on the same page
// (no contiguity requirement).

function xmlTextWordKeys(page, xt) {
  return (xt.words || [])
    .filter(w => w.text)
    .map(w => `${page}:${w.id}`);
}

function trySetSelection(page, keys) {
  if (!keys.length) return false;
  state.selectedPage = page;
  state.selectedKeys = keys.slice();
  rebuildExtraTwins();
  updateSelectionHud();
  return true;
}

function tryAddToSelection(page, keys) {
  if (state.selectedPage !== null && state.selectedPage !== page) {
    toast("Multi-select must stay on one page", "error");
    return false;
  }
  // Toggle: if every key already selected, remove them
  const allIn = keys.every(k => state.selectedKeys.includes(k));
  let merged;
  if (allIn) {
    merged = state.selectedKeys.filter(k => !keys.includes(k));
  } else {
    const set = new Set(state.selectedKeys);
    for (const k of keys) set.add(k);
    merged = [...set];
  }
  if (!merged.length) { clearSelection(); return true; }
  state.selectedPage = page;
  state.selectedKeys = merged;
  rebuildExtraTwins();
  updateSelectionHud();
  return true;
}

// ── Twin highlight canvases ──────────────────────────────────────────────

function showTwin(pageNum, occ, color) {
  const info = state.pageCanvases[pageNum];
  if (!info) return;
  const wrapper = document.getElementById(`pr-pdf-page-${pageNum}`);
  if (!wrapper) return;
  const wrapRect = wrapper.getBoundingClientRect();
  const sx = wrapRect.width  / info.pdfW;
  const sy = wrapRect.height / info.pdfH;
  const twin = document.getElementById("pr-twin");
  if (!twin) return;
  if (twin.parentElement !== wrapper) wrapper.appendChild(twin);
  const w   = (occ.right  - occ.left) * sx;
  const h   = (occ.bottom - occ.top)  * sy;
  const dpr = 2;
  twin.width  = Math.max(1, Math.ceil(w * dpr));
  twin.height = Math.max(1, Math.ceil(h * dpr));
  twin.style.left   = (occ.left * sx) + "px";
  twin.style.top    = (occ.top  * sy) + "px";
  twin.style.width  = w + "px";
  twin.style.height = h + "px";
  twin.style.display = "block";
  const ctx = twin.getContext("2d");
  ctx.clearRect(0, 0, twin.width, twin.height);
  ctx.scale(dpr, dpr);
  ctx.fillStyle = color || "rgba(255, 235, 59, 0.45)";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "rgba(255, 193, 7, 1)";
  ctx.lineWidth = 2;
  ctx.strokeRect(1, 1, w - 2, h - 2);
}
function hideTwin() {
  const twin = document.getElementById("pr-twin");
  if (twin) twin.style.display = "none";
}

function addExtraTwinForBox(pageNum, box, color) {
  const info = state.pageCanvases[pageNum];
  if (!info) return;
  const wrapper = document.getElementById(`pr-pdf-page-${pageNum}`);
  if (!wrapper) return;
  const wrapRect = wrapper.getBoundingClientRect();
  const sx = wrapRect.width  / info.pdfW;
  const sy = wrapRect.height / info.pdfH;
  const el = document.createElement("canvas");
  el.className = "pr-extra-twin";
  el.style.cssText = "position:absolute;pointer-events:none;z-index:2;";
  const w = (box.right - box.left) * sx;
  const h = (box.bottom - box.top) * sy;
  const dpr = 2;
  el.width  = Math.max(1, Math.ceil(w * dpr));
  el.height = Math.max(1, Math.ceil(h * dpr));
  el.style.left = (box.left * sx) + "px";
  el.style.top  = (box.top  * sy) + "px";
  el.style.width  = w + "px";
  el.style.height = h + "px";
  const ctx = el.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = color || "rgba(0, 200, 83, 0.3)";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "#00c853";
  ctx.lineWidth = 2;
  ctx.strokeRect(1, 1, w - 2, h - 2);
  wrapper.appendChild(el);
  state.extraTwins.push(el);
}

function clearExtraTwins() {
  (state.extraTwins || []).forEach(e => e.remove());
  state.extraTwins = [];
}

// Selection is stored as word keys, but highlights are drawn per xml_text:
// if every word of an xml_text is selected, one full-line highlight is drawn;
// otherwise a tight union box around the selected words in that xml_text.
function rebuildExtraTwins() {
  clearExtraTwins();
  if (!state.selectedKeys.length || state.selectedPage === null) return;
  const page = state.selectedPage;
  const flat = state.flatWordsByPage[page] || [];
  const selSet = new Set(state.selectedKeys);
  const groupsByXt = new Map();
  for (const key of state.selectedKeys) {
    const i = state.wordKeyToIdx[key];
    if (i === undefined) continue;
    const f = flat[i];
    if (!groupsByXt.has(f.xt)) groupsByXt.set(f.xt, []);
    groupsByXt.get(f.xt).push(f.word);
  }
  for (const [xt, words] of groupsByXt.entries()) {
    // Edge rule (same as backend Strategy 1):
    //   - selection includes the LEFTMOST word of xml_text → use xml_text.left
    //   - selection includes the RIGHTMOST word of xml_text → use xml_text.right
    //   - middle-only selection → word-tight both sides
    // xml_text bbox (from pdftohtml) is the authoritative render extent;
    // pdfplumber word bboxes undershoot on the trailing side.
    const xtWords = xt.words || [];
    const firstId = xtWords.length ? xtWords[0].id : null;
    const lastId  = xtWords.length ? xtWords[xtWords.length - 1].id : null;
    const matchedIds = words.map(w => w.id);
    const left  = matchedIds.includes(firstId) ? xt.left  : Math.min(...words.map(w => w.left));
    const right = matchedIds.includes(lastId)  ? xt.right : Math.max(...words.map(w => w.right));
    addExtraTwinForBox(page, { left, top: xt.top, right, bottom: xt.bottom },
                       "rgba(0, 200, 83, 0.3)");
  }
}

// ── Overlay event handlers ───────────────────────────────────────────────

function _hoverKey(h) {
  return h ? `${h.page}:${h.left}:${h.top}:${h.right}:${h.bottom}` : null;
}

function onOverlayMove(e, pageNum) {
  const c = overlayCoords(e, pageNum);
  if (!c) return;
  const info = state.pageCanvases[pageNum];
  const altMode = e.altKey;
  let box = null;
  if (altMode) {
    const hit = hitWordAt(pageNum, c.pdfX, c.pdfY);
    // Word hit: left/right from word, top/bottom from parent xml_text
    if (hit) box = {
      left:   hit.word.left,
      top:    hit.xt.top,
      right:  hit.word.right,
      bottom: hit.xt.bottom,
    };
  } else {
    box = hitXmlTextAt(pageNum, c.pdfX, c.pdfY);
  }
  const newHover = box ? { page: pageNum, left: box.left, top: box.top, right: box.right, bottom: box.bottom } : null;
  if (_hoverKey(state.hoverText) !== _hoverKey(newHover)) {
    state.hoverText = newHover;
    info.overlay.style.cursor = box ? "pointer" : "default";
    if (box) showTwin(pageNum, box);
    else hideTwin();
    // Hover state transition log — only on change, not every mouse move.
    // Gated behind ?debug=1 URL flag (PR_DEBUG); previously `window.__PR_DEBUG !== false`
    // which was on-by-default.
    if (PR_DEBUG) {
      console.log(`[hover p${pageNum} ${altMode ? 'alt(word)' : 'xt'}] (${c.pdfX.toFixed(1)},${c.pdfY.toFixed(1)})`,
        box ? `→ HIT bbox=${JSON.stringify(box)}` : "→ MISS (no xml_text covers this point)");
    }
  }
}

function onOverlayClick(e, pageNum) {
  const c = overlayCoords(e, pageNum);
  if (!c) {
    console.warn(`[click p${pageNum}] overlayCoords returned null — overlay rect zero?`);
    return;
  }
  const ctrl = e.ctrlKey || e.metaKey;
  const alt  = e.altKey;
  const page = pageNum;
  const mode = `${ctrl ? 'ctrl+' : ''}${alt ? 'alt+' : ''}click`;

  // Resolve what was clicked
  let keys = null;
  let hitDescr = null;
  if (alt) {
    const hit = hitWordAt(page, c.pdfX, c.pdfY);
    if (hit) {
      keys = [`${page}:${hit.word.id}`];
      hitDescr = `WORD id=${hit.word.id} text=${JSON.stringify(hit.word.text)} xt=${hit.xt.id}`;
    }
  } else {
    const xt = hitXmlTextAt(page, c.pdfX, c.pdfY);
    if (xt) {
      keys = xmlTextWordKeys(page, xt);
      const txt = (xt.words || []).map(w => w.text).join(" ");
      hitDescr = `XT id=${xt.id} bbox=[${xt.left},${xt.top},${xt.right},${xt.bottom}] words=${keys.length} text=${JSON.stringify(txt)}`;
    }
  }

  if (!keys || !keys.length) {
    // Diagnostic: how many xml_texts on this page total, how many overlap the
    // click row (within ±2pt of click Y), and the closest one's bbox.
    const info = state.pageCanvases[page];
    const xts = info?.xml_texts || [];
    const sameRow = xts.filter(t => Math.abs(((t.top + t.bottom)/2) - c.pdfY) < 6);
    let closest = null, closestDist = Infinity;
    for (const t of xts) {
      const cx = (t.left + t.right)/2, cy = (t.top + t.bottom)/2;
      const d = Math.abs(cx - c.pdfX) + Math.abs(cy - c.pdfY);
      if (d < closestDist) { closestDist = d; closest = t; }
    }
    console.warn(`[click p${page} ${mode}] MISS at (${c.pdfX.toFixed(1)},${c.pdfY.toFixed(1)})`,
      `\n  page has ${xts.length} xml_text(s), ${sameRow.length} on this row (±6pt of click Y)`,
      closest ? `\n  closest xt id=${closest.id} bbox=[${closest.left},${closest.top},${closest.right},${closest.bottom}] dist=${closestDist.toFixed(1)} text=${JSON.stringify((closest.words||[]).map(w=>w.text).join(' '))}` : '\n  page has NO xml_texts at all',
      `\n  → If you can SEE text here but no xml_text covers it, that text is likely rendered as path/image (not Tj operators) and pdftohtml couldn't extract it. Use the manual-add modal to type the literal.`);
    if (!ctrl) clearSelection();
    return;
  }

  if (PR_DEBUG) console.log(`[click p${page} ${mode}] HIT ${hitDescr}`);
  if (ctrl) tryAddToSelection(page, keys);
  else      trySetSelection(page, keys);
}

// ── Whole-PDF occurrence finder (exact string, whole-word sequence) ──────
// Mirrors the Python _find_entity_occurrences Strategy 1.

const WORD_STRIP_RE = /^[.,;:!?()[\]"'\u2019]+|[.,;:!?()[\]"'\u2019]+$/g;
function stripWord(s) { return s.replace(WORD_STRIP_RE, ""); }

function findOccurrencesInPdf(entity) {
  const out = [];
  if (!entity || !state.vizData) return out;
  const baseTarget = entity.trim().split(/\s+/).map(w => stripWord(w).toLowerCase()).filter(Boolean);
  if (!baseTarget.length) return out;
  // Two target variants to try in order:
  //   1. Un-expanded — preserves internal hyphens. Matches PDFs where
  //      '97031-0711' is a single xml_texts word (the common case;
  //      backend _find_entity_occurrences uses ONLY this form).
  //   2. Dash-expanded — splits hyphenated tokens. Fallback for PDFs
  //      where '316 - 116276 - 052' renders as 3 separate tokens with
  //      dashes between them (e.g. some MS footers).
  // Only run variant 2 if variant 1 finds nothing AND a hyphen exists.
  const variants = [baseTarget];
  const expanded = [];
  let didExpand = false;
  for (const tw of baseTarget) {
    if (tw.includes("-")) {
      const parts = tw.split("-");
      if (parts.length > 1 && parts.every(p => p)) {
        expanded.push(...parts);
        didExpand = true;
        continue;
      }
    }
    expanded.push(tw);
  }
  if (didExpand) variants.push(expanded);

  let target = baseTarget;
  for (const variant of variants) {
    target = variant;
    const variantStart = out.length;
    for (const [pStr, pdata] of Object.entries(state.vizData.pages || {})) {
    const page = Number(pStr);
    const flat = state.flatWordsByPage[page] || [];
    const lower = flat.map(f => stripWord(f.word.text).toLowerCase());
    const n = target.length;
    for (let i = 0; i <= flat.length - n; i++) {
      let ok = true;
      for (let j = 0; j < n; j++) {
        if (lower[i + j] !== target[j]) { ok = false; break; }
      }
      if (!ok) continue;
      // Group matched words by parent xml_text so each occurrence is one line.
      // top/bottom inherit from parent xml_text (match backend + pdf_qc Text
      // Band). left/right stay word-tight.
      let bucket = [];
      let curParent = null;
      const flush = () => {
        if (!bucket.length) return;
        const xt = bucket[0].xt;
        const xtWords = xt.words || [];
        const firstId = xtWords.length ? xtWords[0].id : null;
        const lastId  = xtWords.length ? xtWords[xtWords.length - 1].id : null;
        const matchedIds = bucket.map(f => f.wid);
        const left  = matchedIds.includes(firstId) ? xt.left  : Math.min(...bucket.map(f => f.word.left));
        const right = matchedIds.includes(lastId)  ? xt.right : Math.max(...bucket.map(f => f.word.right));
        out.push({
          page,
          left, top: xt.top, right, bottom: xt.bottom,
          xml_text_id: xt.id,
          word_ids: matchedIds,
          partial: false,
          full_text: bucket.map(f => f.word.text).join(" "),
        });
      };
      for (let j = 0; j < n; j++) {
        const f = flat[i + j];
        if (curParent !== null && f.xt !== curParent) { flush(); bucket = []; }
        curParent = f.xt;
        bucket.push(f);
      }
      flush();
      i += n - 1;
    }
    }
    if (out.length > variantStart) break;
  }

  // Strategy 1c: whitespace-variant match (mirrors backend Strategy 1c).
  // Same logical identifier may render with different whitespace across
  // the PDF — e.g. 'LL 51161 96' in body tables vs 'LL5116196' /
  // 'LL51161960' in footers. Build the whitespace-free join of needle
  // and of each consecutive-word window per xml_text; emit when (a) joins
  // are equal, or (b) needle is a prefix of join (single-word case, with
  // char-tight bbox). Either direction of the variant (spaced ↔ joined)
  // is recovered from a single user input. ADDITIVE — runs even if
  // Strategy 1 found body hits, so footer-renderings still get matched;
  // skips xml_texts already hit on the same page to avoid duplicates.
  const _strat1cHit = new Set();
  for (const o of out) _strat1cHit.add(o.page + ":" + o.xml_text_id);
  const needleStripped = baseTarget.join("");
  if (needleStripped && needleStripped.length >= 4) {
    for (const [pStr, pdata] of Object.entries(state.vizData.pages || {})) {
      const page = Number(pStr);
      for (const xt of (pdata.xml_texts || [])) {
        if (_strat1cHit.has(page + ":" + xt.id)) continue;
        const words = xt.words || [];
        if (!words.length) continue;
        const norm = words.map(w => [w, stripWord(w.text || "").toLowerCase()]);
        for (let startI = 0; startI < norm.length; startI++) {
          if (!norm[startI][1]) continue;
          let joinAcc = "";
          const seg = [];
          for (let endI = startI; endI < norm.length; endI++) {
            const tok = norm[endI][1];
            if (!tok) continue;
            joinAcc += tok;
            seg.push(norm[endI][0]);
            if (joinAcc === needleStripped) {
              // Skip if equivalent to a Strategy 1 single-token hit
              if (seg.length === 1 && baseTarget.length === 1) break;
              const matchedIds = seg.map(s => s.id);
              const left  = Math.min(...seg.map(s => s.left));
              const right = Math.max(...seg.map(s => s.right));
              const useXtBox = seg.length === words.length;
              out.push({
                page,
                left:  useXtBox ? xt.left  : left,
                top:   xt.top,
                right: useXtBox ? xt.right : right,
                bottom: xt.bottom,
                xml_text_id: xt.id,
                word_ids: matchedIds,
                partial: false,
                full_text: seg.map(s => s.text).join(" "),
              });
              break;
            }
            if (joinAcc.length > needleStripped.length) {
              // Overshot. Prefix-of-single-word case → char-tight prefix bbox.
              if (seg.length === 1 && joinAcc.startsWith(needleStripped)) {
                const w = seg[0];
                const wtext = w.text || "";
                const chars = w.chars || [];
                let L, R;
                if (chars.length === wtext.length && needleStripped.length <= chars.length) {
                  const cseg = chars.slice(0, needleStripped.length);
                  L = Math.min(...cseg.map(c => (state.useBboxD && c.left_d  != null) ? c.left_d  : c.left));
                  R = Math.max(...cseg.map(c => (state.useBboxD && c.right_d != null) ? c.right_d : c.right));
                } else {
                  L = w.left; R = w.right;
                }
                out.push({
                  page, left: L, top: xt.top, right: R, bottom: xt.bottom,
                  xml_text_id: xt.id,
                  word_ids: [],
                  partial: true,
                  full_text: wtext,
                });
              }
              break;
            }
          }
        }
      }
    }
  }

  // Strategy 2: if whole-word (+ subsequence implicit via single-word
  // target) found nothing, fall through to partial substring match
  // inside each word. Char-tight bboxes via chars[] when available.
  // Mirrors backend _find_entity_occurrences Strategy 2. Uses baseTarget
  // (un-expanded) so single-token hyphenated needles still try substring.
  if (out.length === 0 && baseTarget.length === 1) {
    const needle = baseTarget[0];
    for (const [pStr, pdata] of Object.entries(state.vizData.pages || {})) {
      const page = Number(pStr);
      for (const xt of (pdata.xml_texts || [])) {
        for (const w of (xt.words || [])) {
          const wtext = w.text || "";
          if (!wtext) continue;
          const wlower = wtext.toLowerCase();
          const idx = wlower.indexOf(needle);
          if (idx < 0 || needle === wlower) continue;
          // Bug 7: boundary anchor. Refuse alphanumeric-prefix/suffix
          // substring matches (e.g. 'LL5116196' inside 'LL51161960')
          // which almost always mean two different identifiers sharing
          // a prefix. Mirrors backend Strategy 2 boundary rule.
          const endIdx = idx + needle.length;
          const beforeOk = idx === 0 || !/[a-z0-9]/i.test(wlower[idx - 1]);
          const afterOk  = endIdx >= wlower.length || !/[a-z0-9]/i.test(wlower[endIdx]);
          if (!beforeOk || !afterOk) continue;
          // Char-tight left/right if chars[] aligns with text length
          const chars = w.chars || [];
          let L, R;
          if (chars.length === wtext.length && idx + needle.length <= chars.length) {
            const seg = chars.slice(idx, idx + needle.length);
            L = Math.min(...seg.map(c => (state.useBboxD && c.left_d  != null) ? c.left_d  : c.left));
            R = Math.max(...seg.map(c => (state.useBboxD && c.right_d != null) ? c.right_d : c.right));
          } else {
            L = w.left;
            R = w.right;
          }
          out.push({
            page, left: L, top: xt.top, right: R, bottom: xt.bottom,
            xml_text_id: xt.id,
            word_ids: [],
            partial: true,
            full_text: wtext,
          });
        }
      }
    }
  }

  // Strategy 3: substring of xml_text content. Mirrors backend
  // _find_entity_occurrences Strategy 3. Runs ADDITIVELY so xml_texts
  // whose words[] are sorted by storage-Y rather than logical reading
  // order (vertical / rotated text) still get matched — Strategies 1
  // and 1c above assume left-to-right word order and miss these.
  // Reads ONLY xt.content (the authoritative logical-reading string
  // viz_data records for every xml_text) — orientation-agnostic.
  // Duplicate-guard skips xml_texts already produced by Strategies 1/1c/2.
  const _strat3Hit = new Set();
  for (const o of out) _strat3Hit.add(o.page + ":" + o.xml_text_id);
  const needleLower = baseTarget.join(" ");
  if (needleLower) {
    for (const [pStr, pdata] of Object.entries(state.vizData.pages || {})) {
      const page = Number(pStr);
      for (const xt of (pdata.xml_texts || [])) {
        if (_strat3Hit.has(page + ":" + xt.id)) continue;
        const content = (xt.content || "").toLowerCase();
        if (content.includes(needleLower)) {
          out.push({
            page,
            left:  xt.left,
            top:   xt.top,
            right: xt.right,
            bottom: xt.bottom,
            xml_text_id: xt.id,
            word_ids: [],
            partial: true,
            full_text: xt.content || "",
          });
        }
      }
    }
  }

  return out;
}

// ── Redraw ───────────────────────────────────────────────────────────────

function redrawAllBboxes() {
  if (!state.data) return;
  for (const p of Object.keys(state.pageCanvases)) {
    const info = state.pageCanvases[p];
    const ctx = info.overlay.getContext("2d");
    ctx.clearRect(0, 0, info.overlay.width, info.overlay.height);
  }
  // Same dedup + 0-occ filter as renderEntityList. Skip manual entries
  // already promoted to CRM recs to avoid double-drawing bboxes.
  const seenEntities = new Set();
  const recs = [];
  for (const r of (state.data.recommendations || [])) {
    if (!(r.occurrences || []).length) continue;
    seenEntities.add(r.entity);
    recs.push(r);
  }
  for (const m of (state.data.manual_additions || [])) {
    if (seenEntities.has(m.entity)) continue;
    recs.push({ ...m, _manual: true });
  }
  // Token map for redacted preview (entity → "NAME_2")
  const { assignments } = computeTokenAssignments();

  for (const r of recs) {
    if (isRejected(r.entity)) continue;  // hide rejected on right panel
    const isActive = (r.id || `m_${r.entity}`) === state.activeEntityId;
    const dec = (state.data.decisions || {})[r.entity] || {};
    const cat = dec.category || r.suggested_category;
    const tokOn = (dec.tokenize !== undefined) ? dec.tokenize : true;

    if (state.redactedView) {
      // Redacted preview mode:
      //   tokenize ON  → solid black box labelled with its token "[NAME_2]"
      //   tokenize OFF → solid black box, NO label (pure redaction)
      const token = tokOn ? assignments.get(r.entity) : null;
      for (const occ of (r.occurrences || [])) {
        drawRedactedBox(occ, token);
      }
    } else {
      // Normal review mode: category-coloured boxes
      for (const occ of (r.occurrences || [])) {
        drawBox(occ, isActive, false, cat);
      }
    }
  }
}

function drawRedactedBox(occ, token /* string or null */) {
  const info = state.pageCanvases[occ.page];
  if (!info) return;
  const ctx = info.overlay.getContext("2d");
  const s = info.scale;
  const oL = bboxLeft(occ), oR = bboxRight(occ);
  const x = oL * s, y = occ.top * s;
  const w = (oR - oL) * s, h = (occ.bottom - occ.top) * s;
  // Solid black fill
  ctx.fillStyle = "#000";
  ctx.fillRect(x, y, w, h);
  // Token label sizing + positioning mirrors pdf_tokenizer_v3.apply_decisions
  // exactly, so the on-screen preview matches the tokenized PDF that Apply
  // produces. Apply works in PDF user-space points; we compute fontsize in
  // points (rectH_pt × 0.85, capped [3,10]) and scale to canvas pixels at
  // draw time. Font is Helvetica (fitz "helv"). Anchoring is left-edge +
  // baseline near rect bottom, not center — matches apply's insert_text call.
  if (token) {
    // Frontend assignments map stores BARE token (e.g. "ACCOUNT_1"); apply's
    // backend stores already-bracketed (`[ACCOUNT_1]`). Wrap here to match.
    const label = `[${token}]`;
    const rectW_pt = oR - oL;
    const rectH_pt = occ.bottom - occ.top;
    let fontsize_pt = Math.min(10.0, Math.max(3.0, rectH_pt * 0.85));
    const setFont = (pt) => { ctx.font = `${pt * s}px Helvetica, Arial, sans-serif`; };
    setFont(fontsize_pt);
    // Shrink width-wise — same loop apply runs (decrement 0.5pt, floor 3pt).
    // Compare in canvas px against rectW_pt × s; equivalent to comparing
    // rendered text length against rect.width in points (apply's check).
    while (fontsize_pt > 3.0 && ctx.measureText(label).width > rectW_pt * s) {
      fontsize_pt -= 0.5;
      setFont(fontsize_pt);
    }
    const baselineY_pt = occ.bottom - Math.max(0.5, rectH_pt * 0.15);
    ctx.fillStyle = "#fff";
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "left";
    ctx.fillText(label, (oL + 0.5) * s, baselineY_pt * s);
  }
}

function drawBox(occ, active, rejected, cat) {
  const info = state.pageCanvases[occ.page];
  if (!info) return;
  const ctx = info.overlay.getContext("2d");
  const s = info.scale;
  const oL = bboxLeft(occ), oR = bboxRight(occ);
  const x = oL * s, y = occ.top * s;
  const w = (oR - oL) * s, h = (occ.bottom - occ.top) * s;

  const colors = {
    PERSON: "rgba(220, 53, 69, ",
    ACCOUNT_NUMBER: "rgba(13, 110, 253, ",
    PHONE_NUMBER: "rgba(255, 193, 7, ",
    EMAIL: "rgba(25, 135, 84, ",
    ADDRESS: "rgba(111, 66, 193, ",
    DATE_OF_BIRTH: "rgba(13, 202, 240, ",
    OTHER: "rgba(108, 117, 125, ",
    REJECT: "rgba(150, 150, 150, ",
  };
  const rgba = colors[cat] || colors.OTHER;
  const fillA = active ? 0.35 : (rejected ? 0.06 : 0.15);
  const strokeA = active ? 1.0 : (rejected ? 0.3 : 0.6);
  ctx.fillStyle = rgba + fillA + ")";
  ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = rgba + strokeA + ")";
  ctx.lineWidth = active ? 2 : 1;
  ctx.strokeRect(x, y, w, h);
}

// ── Save / Apply ─────────────────────────────────────────────────────────

async function prSaveDecisions() {
  if (!state.data || !state.clientId) return;
  // Snapshot the UI's current token assignments into decisions[entity].token
  // so the persisted pii.json AND any subsequent Apply mirror EXACTLY what
  // the user saw at the moment of click. Without this, only entities the
  // user manually picked from the dropdown have stored tokens; auto-assigned
  // ones don't, and backend re-derivation can diverge from UI display.
  const { assignments } = computeTokenAssignments();
  if (!state.data.decisions) state.data.decisions = {};
  for (const [entity, tokenName] of assignments.entries()) {
    const prev = state.data.decisions[entity] || {};
    state.data.decisions[entity] = { ...prev, token: tokenName };
  }
  try {
    const res = await fetch("/api/pii-review/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_id: state.clientId,
        stem: state.stem,
        decisions: state.data.decisions || {},
        manual_additions: state.data.manual_additions || [],
        // Send the full UI snapshot of recommendations too. UI is fluid:
        // anything the user removed/rejected/restored mid-session is in
        // state.data exactly as displayed. Save commits all of it. Without
        // this, backend keeps stale recs in pii.json.
        recommendations: state.data.recommendations || [],
        // Explicit-deletion channel (Bug 6): backend removes ONLY these
        // from the CRM. Entities absent from `recommendations` for any
        // other reason are carried forward, preventing silent state-drift
        // wipes that previously zeroed entity occurrences on save.
        deleted_entities: [...(state.deletedEntities || [])],
      }),
    });
    const data = await res.json();
    if (data.ok) {
      const merged   = data.client_merged ?? 0;
      const siblings = data.siblings_refreshing ?? 0;
      let msg = "Decisions saved";
      if (merged)   msg += ` · ${merged} merged into CRM`;
      if (siblings) msg += ` · auto-refreshing ${siblings} sibling PDF(s)`;
      toast(msg, "success");
      // Bug 6: explicit deletions just got committed — clear the pending set
      // so a follow-up Save doesn't re-send stale deletions for entities the
      // backend already removed (and possibly the user has since re-added).
      state.deletedEntities = new Set();
      // Refresh client list so literal_count badge is current
      loadClients();
      // Long-term cleanup Goal 2 (2026-05-19): auto-sync FE state from BE.
      // The backend just rewrote pii.json (via _merge_into_client_config →
      // _populate_recommendations_from_client_config → M-MERGE). Without this
      // refresh, state.data continues to show the pre-save snapshot — UI
      // lies about disk state until the user manually reloads. Refresh
      // pulls the authoritative post-save state and re-renders.
      await refreshStateFromDisk();
    } else {
      toast("Save failed: " + (data.error || "?"), "error");
    }
  } catch (e) { toast("Save error: " + e, "error"); }
}

async function prReanalyze() {
  if (!state.data || !state.clientId || !state.stem) return;
  const ok = await prConfirm(
    "Re-analyze this PDF against the latest client config?\n\n" +
    "This re-resolves all client literals against this PDF's viz_data and " +
    "overwrites recommendations + decisions. Your manual_additions are preserved.",
    { confirmText: "Re-analyze", cancelText: "Cancel" }
  );
  if (!ok) return;
  try {
    const res = await fetch("/api/pii-review/reanalyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: state.clientId, stem: state.stem }),
    });
    const data = await res.json();
    if (data.ok) {
      toast(`Re-analyzed · ${data.hits} literal(s) matched · ${data.preserved_manual} manual entr(ies) kept`, "success");
      // Reload the file so UI reflects the fresh recs/decisions
      await prLoadFile();
    } else {
      toast("Re-analyze failed: " + (data.error || "?"), "error");
    }
  } catch (e) { toast("Re-analyze error: " + e, "error"); }
}
window.prReanalyze = prReanalyze;

async function prApply() {
  if (!state.data) return;
  const ok = await prConfirm(
    "Apply decisions, tokenize PII, and sanitize hidden metadata?\n\n" +
    "Output lands in safe/ subdir under an opaque filename. " +
    "Trace back to original filename is logged in _client.json.",
    { confirmText: "Apply", cancelText: "Cancel" }
  );
  if (!ok) return;
  // prSaveDecisions already snapshots UI token assignments into decisions
  // before POSTing, so backend's apply_decisions sees the explicit tokens
  // and writes them verbatim — no divergence from UI.
  await prSaveDecisions();
  try {
    const res = await fetch("/api/pii-review/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: state.clientId, stem: state.stem }),
    });
    const data = await res.json();
    if (data.ok) toast("Applying… tokenized PDF will appear in output/pii_review/", "success");
    else toast("Apply failed: " + (data.error || "?"), "error");
  } catch (e) { toast("Apply error: " + e, "error"); }
}

// ── Upload ───────────────────────────────────────────────────────────────

async function prUpload(input) {
  if (!state.clientId) {
    toast("Pick a client first", "error");
    input.value = "";
    return;
  }
  const files = Array.from(input.files || []);
  if (!files.length) return;
  for (const f of files) {
    if (!f.name.toLowerCase().endsWith(".pdf")) continue;
    const fd = new FormData(); fd.append("file", f);
    try {
      const res = await fetch(_withClient("/api/pii-review/upload"), {
        method: "POST", body: fd,
      });
      const data = await res.json();
      if (data.ok) toast(`Uploaded ${data.filename}`, "success");
    } catch (e) { toast("Upload failed: " + e, "error"); }
  }
  input.value = "";
  await loadFiles();
}

// ── Manual entity modal ──────────────────────────────────────────────────

function openManualModal() {
  document.getElementById("pr-manual-modal").style.display = "flex";
  document.getElementById("pr-manual-text").value = "";
  // FE-1 fix: reset Category to default OTHER on every open so the dropdown
  // doesn't carry the previous Add's choice (e.g. ACCOUNT_NUMBER) across.
  document.getElementById("pr-manual-cat").value = "OTHER";
}
function prCancelManual() {
  document.getElementById("pr-manual-modal").style.display = "none";
}
function prSubmitManual() {
  const txt = document.getElementById("pr-manual-text").value.trim();
  const cat = document.getElementById("pr-manual-cat").value;
  if (!txt) return;
  if (!state.data) return;
  if (!state.data.manual_additions) state.data.manual_additions = [];

  // #0 fix (ARCH v1.1 §2.1 / ADR-3 — BOTH call-sites, PM 0732 / IG binding).
  // IDENTICAL post-diff flow to commitSelectionAsManual (shared helper).
  // Step 1: capture pre-call reject state, then run _absorbReAdd ONLY for its
  // PRESERVED side-effects (un-reject + manual-dedup, I-P3). Boolean NO LONGER
  // early-returns — findOccurrencesInPdf now runs on the name-match path too.
  const _decBefore = (state.data.decisions || {})[txt];
  const wasRejected = !!(_decBefore
    && (_decBefore.rejected === true || _decBefore.category === "REJECT"));
  _absorbReAdd(txt);

  const occurrences = findOccurrencesInPdf(txt);

  // existing==null ⇒ brand-new ⇒ PRESERVED fresh-entity path (manual_additions
  // push + "Manual entity added (N match…)" toast, byte-for-byte).
  // existing!=null ⇒ SUCCESS/NO-OP/ERROR handled inside the shared helper.
  const _r = _applyDiffedOccurrences(txt, occurrences, wasRejected);
  if (!_r || !_r.brandNew) {
    prCancelManual();
    return;
  }

  state.data.manual_additions.push({
    entity: txt, category: cat, tokenize: cat === "ACCOUNT_NUMBER",
    suggested_category: cat, models_voted: ["manual"], vote_count: 1,
    occurrences,
  });
  prCancelManual();
  renderEntityList();
  redrawAllBboxes();
  toast(`Manual entity added (${occurrences.length} match${occurrences.length !== 1 ? "es" : ""})`, "success");
}

// ── Resize handle ────────────────────────────────────────────────────────

function initResize() {
  const handle = document.getElementById("pr-resize");
  const left = document.getElementById("pr-left");
  const main = document.getElementById("pr-main");
  let dragging = false, startX = 0, startW = 0;
  handle.addEventListener("mousedown", e => {
    dragging = true; startX = e.clientX; startW = left.getBoundingClientRect().width;
    document.body.style.cursor = "col-resize";
  });
  document.addEventListener("mousemove", e => {
    if (!dragging) return;
    const totalW = main.getBoundingClientRect().width - 5;
    const newW = Math.max(260, Math.min(totalW - 260, startW + (e.clientX - startX)));
    left.style.flex = "none"; left.style.width = newW + "px";
  });
  document.addEventListener("mouseup", () => {
    dragging = false; document.body.style.cursor = "";
  });
}

// ── Toast ────────────────────────────────────────────────────────────────

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "pr-toast " + (kind || "");
  // #0 fix a11y (UX §5.2 / ARCH §2.1): ADDITIVE only — derive ARIA from the
  // existing `kind` arg (no signature change, no visual change, no copy/
  // timing change). error ⇒ assertive/alert; else polite/status. Net
  // non-regression for ALL toasts (Preservation Matrix KEEP — additive).
  if (kind === "error") {
    el.setAttribute("role", "alert");
    el.setAttribute("aria-live", "assertive");
  } else {
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
  }
  el.setAttribute("aria-atomic", "true");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── Expose for inline handlers ───────────────────────────────────────────

window.prToggleRedactedView = (checked) => {
  state.redactedView = !!checked;
  redrawAllBboxes();
};
window.prToggleBboxSource = (checked) => {
  state.useBboxD = !!checked;
  redrawAllBboxes();
};

// Helper: read left/right respecting the active bbox source toggle.
// Works on any object with {left, right, left_d?, right_d?}.
function bboxLeft(o)  { return state.useBboxD && o.left_d  != null ? o.left_d  : o.left;  }
function bboxRight(o) { return state.useBboxD && o.right_d != null ? o.right_d : o.right; }
window.prLoadFile = prLoadFile;
window.prSaveDecisions = prSaveDecisions;
window.prApply = prApply;
window.prUpload = prUpload;
window.prCancelManual = prCancelManual;
window.prSubmitManual = prSubmitManual;
