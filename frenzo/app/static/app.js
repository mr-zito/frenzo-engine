/* Frenzo app: talks to the local server (frenzo/app/server.py). */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(url, opts = {}) {
  const o = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
  const r = await fetch(url, o);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok || (data && data.ok === false)) throw new Error((data && data.error) || `Request failed (${r.status})`);
  return data;
}

const S = {
  state: null, streamers: [], clips: [], logs: [], logSeq: 0, page: "live", logJob: null,
  detail: null, past: [], pastSel: new Set(), jobStatus: {}, settings: null, offline: false,
};

/* ------------------------------------------------------------------ small UI helpers */
function toast(html, err = false, ms = 4200) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.innerHTML = html;
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 300); }, ms);
}
function confirmBox(title, text, yes = "Delete", danger = true) {
  return new Promise((res) => {
    $("#modalTitle").textContent = title;
    $("#modalText").textContent = text;
    const y = $("#modalYes");
    y.textContent = yes;
    y.className = danger ? "danger" : "primary";
    $("#modal").classList.remove("hidden");
    const done = (v) => { $("#modal").classList.add("hidden"); y.onclick = $("#modalNo").onclick = null; res(v); };
    y.onclick = () => done(true);
    $("#modalNo").onclick = () => done(false);
  });
}
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
};
const fmtT = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = (t % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
};
function ago(iso) {
  if (!iso) return "";
  const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
const VERSION_NAMES = { "clip_clean.mp4": "Clean (no handle)", "clip.mp4": "Clip", "clip_tiktok+instagram.mp4": "TikTok & Instagram",
  "clip_youtube.mp4": "YouTube", "clip_x.mp4": "X", "clip_tiktok.mp4": "TikTok", "clip_instagram.mp4": "Instagram" };
const verName = (f) => VERSION_NAMES[f] || f.replace(/^clip_|\.mp4$/g, "").replace(/\+/g, " & ");
const POST_PLATS = [["tiktok", "TikTok", "TT"], ["instagram", "Instagram", "IG"], ["youtube", "YouTube", "YT"], ["x", "X", "X"]];

/* ------------------------------------------------------------------ navigation */
function go(page) {
  S.page = page;
  $$(".nav").forEach((b) => b.classList.toggle("on", b.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("on", p.id === "page-" + page));
  if (page === "clips" || page === "live") loadClips();
  if (page === "settings") loadSettings();
  if (page === "past") { renderPastSelects(); loadYtCookies(); }
  if (page === "activity") renderLog(true);
  if (page === "recs") loadRecs();
}
$$(".nav").forEach((b) => (b.onclick = () => go(b.dataset.page)));
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) go(g.dataset.goto); });

/* ------------------------------------------------------------------ polling */
async function poll() {
  try {
    const st = await api("/api/state");
    if (S.offline) { S.offline = false; toast("Reconnected to Frenzo"); }
    S.state = st;
    handleJobTransitions(st.jobs);
    renderState();
    const lines = await api(`/api/logs?since=${S.logSeq}&limit=600`);
    if (lines.length) {
      S.logSeq = lines[lines.length - 1].seq;
      S.logs.push(...lines);
      if (S.logs.length > 4000) S.logs.splice(0, S.logs.length - 3000);
      onNewLines(lines);
    }
  } catch (e) {
    if (!S.offline) {
      S.offline = true;
      const p = $("#livePill"); p.className = "pill off"; $("span", p).textContent = "App server stopped";
    }
  }
  setTimeout(poll, 2000);
}

function handleJobTransitions(jobs) {
  let refresh = false;
  for (const j of jobs) {
    const prev = S.jobStatus[j.id];
    if (prev && prev !== j.status && ["done", "failed", "stopped"].includes(j.status)) {
      if (j.kind !== "live") {
        refresh = true;
        if (j.kind === "learn") learnFinished(j);
        if (j.status === "done" && j.kind !== "learn") toast(`<b>Done</b> · ${esc(j.label)}`);
        else if (j.status === "failed") toast(`<b>Failed</b> · ${esc(j.label)}<br><span class="muted small">${esc(j.last)}</span>`, true, 8000);
      } else toast("Live clipping stopped");
    }
    S.jobStatus[j.id] = j.status;
  }
  if (refresh) { loadClips(true); if (S.page === "recs") { loadRecs(); if (R.cur) openRec(R.cur, true); } }
}

function onNewLines(lines) {
  let saved = false;
  for (const l of lines) {
    if (l.level === "saved" && S.initialLogsLoaded) {
      saved = true;
      if (/SAVED clip/i.test(l.text)) toast(`<b>New clip</b> · ${esc(l.who)}<br><span class="muted small">${esc(l.text.replace(/^SAVED clip /, ""))}</span>`);
    }
  }
  S.initialLogsLoaded = true;
  if (saved) loadClips(true);
  renderFeed();
  renderLog(false, lines);
}

/* ------------------------------------------------------------------ live page */
function renderState() {
  const st = S.state, live = st.live;
  const pill = $("#livePill"), btn = $("#liveBtn");
  const nrec = Object.keys(live.recording || {}).length;
  if (live.status === "running") {
    pill.className = "pill on";
    $("span", pill).textContent = nrec ? `Live · recording ${nrec}` : "Live · waiting for streams";
    btn.textContent = "Stop live clipping"; btn.classList.add("stop"); btn.disabled = false;
    const flagged = Object.values(live.recording || {}).reduce((a, r) => a + (r.flagged || 0), 0);
    $("#liveSub").textContent = nrec
      ? `Recording ${nrec} stream${nrec > 1 ? "s" : ""} · ${flagged} moment${flagged === 1 ? "" : "s"} flagged${live.queued ? ` · ${live.queued} waiting to be clipped` : ""}`
      : "Watching your streamers. Recording starts as soon as one goes live on GTA 6.";
  } else if (live.status === "stopping") {
    pill.className = "pill on"; $("span", pill).textContent = "Stopping…";
    btn.textContent = "Stopping…"; btn.disabled = true;
  } else {
    pill.className = "pill off"; $("span", pill).textContent = "Live clipping off";
    btn.textContent = "Start live clipping"; btn.classList.remove("stop"); btn.disabled = false;
    $("#liveSub").textContent = "Records your streamers while they play GTA 6 and clips the best moments.";
  }
  $("#restartBanner").classList.toggle("hidden", !live.restart_needed);
  const active = st.jobs.filter((j) => j.kind !== "live" && ["queued", "running"].includes(j.status)).length;
  const nj = $("#navJobs"); nj.textContent = active ? active : ""; nj.classList.toggle("hot", !!active);
  if (S.page === "activity") renderJobs();
  if (S.page === "past") renderPastJobs();
  if (S.page === "clips") renderClips();
  if (S.detail) renderBusy();
  // keep streamer cards' recording status fresh
  const sig = JSON.stringify(Object.keys(live.recording || {}).sort()) + live.status;
  if (sig !== S.recSig) { S.recSig = sig; loadStreamers(); }
  else if (S.page === "live") renderStreamers();
  $("#outDir").textContent = st.output_dir;
}

$("#liveBtn").onclick = async () => {
  const live = S.state && S.state.live;
  try {
    if (live && live.status === "running") {
      if (!(await confirmBox("Stop live clipping?", "Recordings stop now. Moments still waiting to be clipped are skipped.", "Stop"))) return;
      await api("/api/live/stop", { method: "POST" });
      toast("Stopping live clipping…");
    } else {
      await api("/api/live/start", { method: "POST" });
      toast("<b>Live clipping started</b> · watching your streamers");
    }
    poll.now && poll.now();
  } catch (e) { toast(esc(e.message), true); }
};
$("#restartBtn").onclick = async () => {
  await api("/api/live/restart", { method: "POST" });
  toast("Restarting live clipping with your new settings…");
};

async function loadStreamers() {
  try { S.streamers = await api("/api/streamers"); } catch (e) { return; }
  renderStreamers();
  renderPastSelects();
  renderClipFilters();
}

function renderStreamers() {
  const box = $("#streamers");
  $("#streamerCount").textContent = S.streamers.length || "";
  if (!S.streamers.length) { box.innerHTML = `<div class="empty">No streamers yet. Paste a channel link above.</div>`; return; }
  const live = S.state ? S.state.live : { recording: {} };
  box.innerHTML = S.streamers.map((s) => {
    const r = (live.recording || {})[s.key];
    const status = r
      ? `<span class="dot"></span><b>Recording</b> · ${fmtDur(Date.now() / 1000 - r.since)} · ${r.flagged || 0} moment${r.flagged === 1 ? "" : "s"}<span class="t">${esc(r.title || r.game || "")}</span>`
      : live.status === "running" ? `Waiting for a GTA 6 stream<span class="t">checked every minute</span>`
      : `Not watching<span class="t">start live clipping to record</span>`;
    return `<div class="st ${r ? "rec" : ""}" data-key="${esc(s.key)}">
      <div class="st-top">
        <div class="av ${s.platform}">${esc(s.name[0] || "?").toUpperCase()}</div>
        <div style="min-width:0"><div class="st-name">${esc(s.name)}</div>
          <div class="st-plat"><span class="pdot ${s.platform}"></span>${esc(s.platform)} · <a href="${esc(s.url)}" target="_blank">open</a></div></div>
        <button class="st-x" title="Remove" data-act="del">×</button>
      </div>
      <div class="st-status">${status}</div>
      <div class="st-foot">
        <span class="chip ${s.hot_takes ? "on" : ""}" data-act="hot" title="Listens to what they say: hot takes + NPC moments (uses some PC power)">Speech listener</span>
        <span class="chip ${s.any_game ? "on" : ""}" data-act="any" title="Record every stream, not only GTA 6 (for streamers who play under Just Chatting)">Any game</span>
        <span class="chip ${s.has_campaign ? "camp-on" : ""}" data-act="camp" title="${s.has_campaign ? esc(s.campaign.name || "Campaign rules set") : "Add this streamer's campaign rules (editing and writing only)"}">Campaign rules</span>
        <span class="n">${s.clips} clip${s.clips === 1 ? "" : "s"}</span>
      </div></div>`;
  }).join("");
}

$("#streamers").addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]");
  if (!act) return;
  const card = e.target.closest(".st"), key = card.dataset.key, s = S.streamers.find((x) => x.key === key);
  try {
    if (act.dataset.act === "del") {
      if (!(await confirmBox(`Remove ${s.name}?`, "They won't be recorded anymore. Their clips stay in your clips folder.", "Remove"))) return;
      await api(`/api/streamers/${encodeURIComponent(key)}`, { method: "DELETE" });
      toast(`Removed <b>${esc(s.name)}</b>`);
    } else if (act.dataset.act === "hot") {
      await api(`/api/streamers/${encodeURIComponent(key)}`, { method: "PATCH", body: { hot_takes: !s.hot_takes } });
    } else if (act.dataset.act === "any") {
      await api(`/api/streamers/${encodeURIComponent(key)}`, { method: "PATCH", body: { any_game: !s.any_game } });
    } else if (act.dataset.act === "camp") {
      openCampaign(s);
      return;
    }
    loadStreamers();
  } catch (err) { toast(esc(err.message), true); }
});

/* ---- campaign rules (per streamer): editing and writing only ---- */
const CAMP_PLATS = [["tiktok", "TikTok"], ["instagram", "Instagram"], ["youtube", "YouTube"], ["x", "X"], ["facebook", "Facebook"]];
const splitTags = (t, p) => [...new Set(String(t || "").split(/[\s,]+/).map((x) => x.trim()).filter(Boolean)
  .map((x) => (x.startsWith(p) ? x : p + x.replace(/^[#@]+/, ""))).filter((x) => x.length > 1))];
function campOf(login) { const s = S.streamers.find((x) => x.name === login); return s && s.has_campaign ? s.campaign : null; }
function openCampaign(s) {
  const c = s.campaign || {};
  S.campKey = s.key;
  $("#campWho").textContent = "· " + s.name;
  $("#cpName").value = c.name || ""; $("#cpLink").value = c.link || ""; $("#cpInstr").value = c.instructions || "";
  $("#cpTags").value = (c.hashtags || []).join(" "); $("#cpMentions").value = (c.mentions || []).join(" ");
  S.campPlats = new Set(c.platforms || ["tiktok", "instagram", "youtube", "x"]);
  renderCampPlats();
  $("#cpMin").value = c.min_seconds || 0; $("#cpMax").value = c.max_seconds || 0;
  $("#cpWatermark").checked = c.watermark !== false; $("#cpCaptions").checked = c.captions !== false; $("#cpSfx").checked = c.sound_effects !== false;
  const ov = c.overlay || {};
  S.campOverlay = ov.file || "";
  $("#cpOvPos").value = ov.position || "top-right"; $("#cpOvSize").value = ov.size || 22; $("#cpOvOp").value = Math.round((ov.opacity || 1) * 100);
  $("#cpLook").value = c.look_for || ""; $("#cpChatKw").value = (c.chat_keywords || []).join(", "); $("#cpSpeechKw").value = (c.speech_keywords || []).join(", ");
  renderOverlay();
  $("#cpApply").disabled = !s.has_campaign; $("#cpClear").classList.toggle("hidden", !s.has_campaign);
  $("#campPanel").classList.remove("hidden");
  $("#cpInstr").focus();
}
function renderCampPlats() {
  $("#cpPlats").innerHTML = CAMP_PLATS.map(([k, n]) => `<span class="chip ${S.campPlats.has(k) ? "on" : "off"}" data-cplat="${k}">${S.campPlats.has(k) ? "✓ " : ""}${n}</span>`).join("");
}
$("#cpPlats").addEventListener("click", (e) => {
  const ch = e.target.closest("[data-cplat]"); if (!ch) return;
  const k = ch.dataset.cplat; S.campPlats.has(k) ? S.campPlats.delete(k) : S.campPlats.add(k); renderCampPlats();
});
function campForm() {
  return { name: $("#cpName").value.trim(), link: $("#cpLink").value.trim(), instructions: $("#cpInstr").value.trim(),
    hashtags: splitTags($("#cpTags").value, "#"), mentions: splitTags($("#cpMentions").value, "@"),
    platforms: CAMP_PLATS.map(([k]) => k).filter((k) => S.campPlats.has(k)),
    min_seconds: Math.max(0, +$("#cpMin").value || 0), max_seconds: Math.max(0, +$("#cpMax").value || 0),
    watermark: $("#cpWatermark").checked, captions: $("#cpCaptions").checked, sound_effects: $("#cpSfx").checked,
    overlay: { file: S.campOverlay || "", position: $("#cpOvPos").value, size: +$("#cpOvSize").value || 22, opacity: (+$("#cpOvOp").value || 100) / 100 },
    look_for: $("#cpLook").value.trim(),
    chat_keywords: $("#cpChatKw").value.split(",").map((x) => x.trim()).filter(Boolean),
    speech_keywords: $("#cpSpeechKw").value.split(",").map((x) => x.trim()).filter(Boolean) };
}
/* overlay preview: the phone frame is 135 px wide for a 1080 px video (x 0.125) */
function renderOverlay() {
  const f = S.campOverlay, img = $("#ovImg"), m = 40 * 135 / 1080;
  $("#cpOvSizeV").textContent = `${$("#cpOvSize").value}% of the width`;
  $("#cpOvOpV").textContent = `${$("#cpOvOp").value}%`;
  $("#cpOvName").textContent = f ? f.split("/").pop() : "No image";
  $("#cpOvRemove").classList.toggle("hidden", !f);
  img.classList.toggle("hidden", !f);
  if (!f) return;
  const want = `/overlay/${encodeURIComponent(f.split("/").pop())}`;
  if (img.getAttribute("src") !== want) img.setAttribute("src", want);
  img.style.width = (135 * (+$("#cpOvSize").value) / 100) + "px";
  img.style.opacity = (+$("#cpOvOp").value) / 100;
  img.style.left = img.style.right = img.style.top = img.style.bottom = "";
  const p = $("#cpOvPos").value, tx = [], ty = [];
  if (p.includes("left")) img.style.left = m + "px"; else if (p.includes("right")) img.style.right = m + "px"; else { img.style.left = "50%"; tx.push("-50%"); }
  if (p.startsWith("top")) img.style.top = m + "px"; else if (p.startsWith("bottom")) img.style.bottom = m + "px"; else { img.style.top = "50%"; ty.push("-50%"); }
  img.style.transform = `translate(${tx[0] || 0}, ${ty[0] || 0})`;
}
["#cpOvPos", "#cpOvSize", "#cpOvOp"].forEach((x) => $(x).addEventListener("input", renderOverlay));
$("#cpOvFile").onchange = async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await fetch(`/api/streamers/${encodeURIComponent(S.campKey)}/campaign-overlay`, { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok || d.ok === false) throw new Error(d.error || "Upload failed");
    S.campOverlay = d.file; renderOverlay();
    toast("Image ready · press <b>Save rules</b> to use it");
  } catch (err) { toast(esc(err.message), true); }
};
$("#cpOvRemove").onclick = () => { S.campOverlay = ""; renderOverlay(); };
const closeCampaign = () => $("#campPanel").classList.add("hidden");
$("#campClose").onclick = $("#cpCancel").onclick = closeCampaign;
$("#campPanel").addEventListener("mousedown", (e) => { if (e.target.id === "campPanel") closeCampaign(); });
$("#cpOpen").onclick = () => { const u = $("#cpLink").value.trim(); if (u) window.open(/^https?:\/\//i.test(u) ? u : "https://" + u, "_blank"); };
$("#cpSave").onclick = async () => {
  const body = campForm();
  if (body.max_seconds && body.min_seconds > body.max_seconds) { toast("The min length is longer than the max length", true); return; }
  try {
    await api(`/api/streamers/${encodeURIComponent(S.campKey)}`, { method: "PATCH", body: { campaign: body } });
    closeCampaign(); await loadStreamers();
    toast("<b>Campaign rules saved</b> · new clips follow them. Use <b>Apply to this streamer's clips</b> for the old ones' post text.", false, 7000);
  } catch (err) { toast(esc(err.message), true); }
};
$("#cpClear").onclick = async () => {
  if (!(await confirmBox("Remove the campaign rules?", "This streamer's clips will be edited and written without campaign rules again. Clips already made keep their text.", "Remove"))) return;
  try { await api(`/api/streamers/${encodeURIComponent(S.campKey)}`, { method: "PATCH", body: { campaign: null } }); closeCampaign(); loadStreamers(); toast("Campaign rules removed"); }
  catch (err) { toast(esc(err.message), true); }
};
$("#cpApply").onclick = async () => {
  try {
    const r = await api(`/api/streamers/${encodeURIComponent(S.campKey)}/apply-campaign`, { method: "POST" });
    toast(`<b>Post text updated</b> on ${r.changed} clip${r.changed === 1 ? "" : "s"}${r.skipped ? ` (${r.skipped} busy, skipped)` : ""}<br><span class="muted small">To re-cut a clip with the rules, open it and use Re-edit with Claude.</span>`, false, 7000);
    loadClips(true);
  } catch (err) { toast(esc(err.message), true); }
};
function campChecklist(camp, c) {
  const items = [];
  const lines = String(camp.instructions || "").split(/\n+|(?<=[.!?])\s+(?=[A-Z0-9"'•\-*])/).map((x) => x.replace(/^[\s•\-*\d.)]+/, "").trim()).filter((x) => x.length > 3);
  lines.slice(0, 6).forEach((l) => items.push(["", l.length > 140 ? l.slice(0, 137) + "…" : l]));
  const text = [c.post_title, c.caption, c.hashtags].join(" ").toLowerCase();
  (camp.hashtags || []).forEach((t) => items.push([text.includes(t.toLowerCase()) ? "ok" : "bad", `Hashtag ${t}`]));
  (camp.mentions || []).forEach((m) => items.push([text.includes(m.toLowerCase()) ? "ok" : "bad", `Credit ${m}`]));
  const d = c.duration || 0;
  if (camp.max_seconds) items.push([d <= camp.max_seconds + 0.5 ? "ok" : "bad", `At most ${camp.max_seconds} s (this clip: ${Math.round(d)} s)`]);
  if (camp.min_seconds) items.push([d >= camp.min_seconds - 0.5 ? "ok" : "bad", `At least ${camp.min_seconds} s (this clip: ${Math.round(d)} s)`]);
  if (camp.watermark === false) items.push(["ok", "No handle on the video: the clean version is posted"]);
  if (camp.overlay && camp.overlay.file) items.push(["", `Campaign image on the video (${camp.overlay.position.replace("-", " ")}): clips made before it was added need a re-edit`]);
  if (camp.look_for || (camp.chat_keywords || []).length || (camp.speech_keywords || []).length) {
    if (c.campaign_match === true) items.push(["ok", `Has the campaign's content${c.campaign_note ? ": " + c.campaign_note : ""}`]);
    else if (c.campaign_match === false) items.push(["bad", `Campaign's content not seen${c.campaign_note ? ": " + c.campaign_note : ""}`]);
    else items.push(["", "Campaign's content: not checked for this clip yet (Re-edit with Claude checks it)"]);
  }
  return items;
}

$("#addForm").onsubmit = async (e) => {
  e.preventDefault();
  const link = $("#addLink").value.trim();
  if (!link) return;
  try {
    const r = await api("/api/streamers", { method: "POST", body: { link, hot_takes: $("#addHot").checked } });
    $("#addLink").value = "";
    toast(`Added <b>${esc(r.key)}</b>`);
    loadStreamers();
  } catch (err) { toast(esc(err.message), true); }
};

function feedLine(l) {
  const t = (l.t || "").slice(0, 5);
  return `<div class="fl ${l.level}"><span class="t">${t}</span><span class="m">${l.who && l.who !== "engine" ? `<span class="w">${esc(l.who)}</span>` : ""}${esc(l.text)}</span></div>`;
}
function renderFeed() {
  const live = S.state && S.state.live;
  const box = $("#liveFeed");
  const jid = live && live.job;
  if (!jid) return;
  const lines = S.logs.filter((l) => l.job === jid && (l.level !== "info" || /score \d+\/10|what happens|webcam found|LIVE:/i.test(l.text))).slice(-70);
  if (!lines.length) { box.innerHTML = `<div class="empty">Starting up…</div>`; return; }
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = lines.map(feedLine).join("");
  if (stick) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------------ clips */
async function loadClips(force) {
  if (!force && S.clipsLoadedAt && Date.now() - S.clipsLoadedAt < 1500) return;
  S.clipsLoadedAt = Date.now();
  try { S.clips = await api("/api/clips"); } catch (e) { return; }
  $("#navClipCount").textContent = S.clips.length || "";
  renderClips();
  renderLatest();
  renderClipFilters();
  if (S.detail) {
    const c = S.clips.find((x) => x.id === S.detail.id);
    if (!c) closeDetail();
    else if (c.version !== S.detail.version) { S.detail = c; renderDetail(true); }
    else S.detail = c;
  }
}
setInterval(() => { if (["clips", "live"].includes(S.page)) loadClips(); }, 9000);

function renderLatest() {
  const box = $("#latestClips");
  const list = S.clips.slice(0, 6);
  box.innerHTML = list.length ? list.map((c) => `<div class="mc" data-open="${c.id}">
      ${c.has_thumb ? `<img src="/media/${c.id}/thumb.jpg?v=${c.version}" loading="lazy">` : ""}
      <span>${esc(c.title)} ${esc(c.emoji)}</span></div>`).join("")
    : `<div class="empty small" style="grid-column:1/-1">No clips yet.</div>`;
}

function renderClipFilters() {
  const sel = $("#clipStreamer"), cur = sel.value;
  const names = [...new Set(S.clips.map((c) => c.login))].sort();
  sel.innerHTML = `<option value="">All streamers</option>` + names.map((n) => `<option ${n === cur ? "selected" : ""}>${esc(n)}</option>`).join("");
}

function filteredClips() {
  const q = $("#clipSearch").value.trim().toLowerCase(), who = $("#clipStreamer").value, f = $("#clipFilter").value;
  let list = S.clips.filter((c) => (!who || c.login === who) && (!q || (c.title + " " + c.streamer + " " + c.what_happens).toLowerCase().includes(q)));
  const isPosted = (c) => Object.values(c.posted || {}).some(Boolean);
  if (f === "posted") list = list.filter(isPosted);
  if (f === "unposted") list = list.filter((c) => !isPosted(c));
  if ($("#clipSort").value === "score") list = [...list].sort((a, b) => (b.score || 0) - (a.score || 0));
  return list;
}

function renderClips() {
  const list = filteredClips(), busy = (S.state && S.state.busy) || {};
  $("#clipsSub").textContent = `${S.clips.length} clip${S.clips.length === 1 ? "" : "s"} · review, post, re-edit or delete.`;
  const grid = $("#clipGrid");
  const html = list.length ? list.map((c) => {
    const posted = POST_PLATS.filter(([k]) => c.posted && c.posted[k]).map(([, , s]) => `<i>${s}</i>`).join("");
    return `<div class="clip" data-open="${c.id}">
      <div class="th">${c.has_thumb ? `<img src="/media/${c.id}/thumb.jpg?v=${c.version}" loading="lazy">` : ""}
        ${c.category === "manual" ? `<span class="sc cut" title="Cut by you">✂</span>` : c.score != null ? `<span class="sc">${c.score}</span>` : ""}
        <span class="du">${Math.round(c.duration || 0)}s</span>
        ${posted ? `<span class="po">${posted}</span>` : ""}
        ${busy[c.id] ? `<div class="busy"><span class="spinner"></span>${busy[c.id] === "queued" ? "Waiting to re-edit…" : "Re-editing…"}</div>` : ""}
      </div>
      <div class="ti">${esc(c.title)} ${esc(c.emoji)}</div>
      <div class="me">${esc(c.streamer)} · ${ago(c.created)}</div></div>`;
  }).join("") : `<div class="empty" style="grid-column:1/-1">${S.clips.length ? "No clips match." : "No clips yet. Start live clipping or clip a past stream."}</div>`;
  if (html !== S.gridHtml) { grid.innerHTML = html; S.gridHtml = html; }
}
["#clipSearch", "#clipStreamer", "#clipFilter", "#clipSort"].forEach((s) => $(s).addEventListener("input", renderClips));
document.addEventListener("click", (e) => { const o = e.target.closest("[data-open]"); if (o) openClip(o.dataset.open); });
$("#openOutput").onclick = () => api("/api/open-output", { method: "POST" });
$("#fixAll").onclick = async () => {
  if (!(await confirmBox("Re-edit all old clips?", "Claude re-watches every clip made by an older version and re-cuts it with a correct title and more context. It stops at Claude's hourly limit; run it again later to continue.", "Start", false))) return;
  await api("/api/fix-all", { method: "POST" });
  toast("Re-editing old clips · follow it in Activity");
};

/* ------------------------------------------------------------------ clip detail */
const E = { start: 0, end: 0, dur: 0, focus: 0.5, camMode: "keep", box: null, loadedFor: null, stopAt: null };

function openClip(id) {
  const c = S.clips.find((x) => x.id === id);
  if (!c) return;
  S.detail = c;
  E.loadedFor = null;
  closeEditors();
  $("#detail").classList.remove("hidden");
  setDTab("info");
  renderDetail(false);
}
function closeDetail() {
  S.detail = null;
  $("#dVideo").pause(); $("#rawVideo").pause();
  $("#dVideo").removeAttribute("src"); $("#rawVideo").removeAttribute("src");
  $("#dVideo").load(); $("#rawVideo").load();
  $("#detail").classList.add("hidden");
}
$("#detailClose").onclick = closeDetail;
$("#detail").addEventListener("mousedown", (e) => { if (e.target.id === "detail") closeDetail(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#modal").classList.contains("hidden")) return; if (e.key === "Escape" && !$("#campPanel").classList.contains("hidden")) { closeCampaign(); return; } if (e.key === "Escape" && S.detail) closeDetail(); });

function titleHtml(c) {
  let t = esc(c.title);
  if (c.highlight) {
    const h = esc(c.highlight), i = t.toLowerCase().indexOf(h.toLowerCase());
    if (i >= 0) t = t.slice(0, i) + "<b>" + t.slice(i, i + h.length) + "</b>" + t.slice(i + h.length);
  }
  return `${t} ${esc(c.emoji)}`;
}
function closeEditors() {
  ["#titleEditor", "#cEditor", "#pEditor"].forEach((x) => $(x).classList.add("hidden"));
  $("#cShow").classList.remove("hidden");
  $("#cIdeasBox").innerHTML = "";
  $("#dPost").classList.remove("hidden");
}

function renderDetail(reloaded) {
  const c = S.detail;
  $("#dTitle").innerHTML = titleHtml(c);
  $("#dMeta").innerHTML = [
    `<span class="badge y">${esc(c.streamer)}</span>`,
    c.category === "manual" ? `<span class="badge y">Cut by you</span>` : c.score != null ? `<span class="badge">Score ${c.score}/10</span>` : "",
    c.category && c.category !== "manual" ? `<span class="badge">${esc(c.category)}</span>` : "",
    `<span class="badge">${Math.round(c.duration || 0)} s</span>`,
    `<span class="badge">${esc(ago(c.created))}</span>`,
    c.old_title ? `<span class="badge" title="Title before the re-edit">was: ${esc(c.old_title)}</span>` : "",
    c.censored ? `<span class="badge r" title="Nudity was pixelated in ${c.censored.spots} spot(s)">Blurred</span>`
      : c.nudity ? `<span class="badge r" title="Claude saw nudity in this clip and it was NOT blurred">Nudity</span>` : "",
  ].join("");
  // versions
  const cur = S.curVersion && c.videos.includes(S.curVersion) ? S.curVersion : c.main;
  S.curVersion = cur;
  $("#dVersions").innerHTML = c.videos.map((v) => `<span class="chip ${v === cur ? "on" : ""}" data-ver="${esc(v)}">${esc(verName(v))}</span>`).join("");
  setVideo(cur, c);
  const camp = campOf(c.login), cname = c.campaign || (camp && (camp.name || "Campaign rules"));
  $("#dCampBlock").classList.toggle("hidden", !cname);
  if (cname) {
    const link = camp && camp.link ? (/^https?:\/\//i.test(camp.link) ? camp.link : "https://" + camp.link) : "";
    $("#dCamp").innerHTML = (link ? `<a class="camp-link" href="${esc(link)}" target="_blank">${esc(cname)}</a>` : esc(cname))
      + (camp ? "" : ` <span class="muted small">(this streamer has no rules anymore)</span>`);
  }
  $("#dWhat").textContent = c.what_happens || "(not recorded for this clip)";
  const cs = $("#cShow");
  cs.textContent = c.user_context || "Not written yet. Watch the clip, then write what really happens: Claude uses it for titles and re-edits.";
  cs.classList.toggle("empty-ctx", !c.user_context);
  $("#cEdit").textContent = c.user_context ? "Edit" : "Write it";
  $("#dReason").textContent = [c.reason, c.flagged_by ? `Flagged by: ${c.flagged_by}` : ""].filter(Boolean).join("  ·  ");
  $("#dPost").textContent = [c.post_title, c.caption, c.hashtags].filter(Boolean).join("\n\n");
  $("#dPosted").innerHTML = POST_PLATS.map(([k, n]) => `<span class="chip ${c.posted && c.posted[k] ? "on" : ""}" data-post="${k}">${c.posted && c.posted[k] ? "✓ " : ""}${n}</span>`).join("");
  $("#claudeHint").textContent = S.state && !S.state.claude ? "Claude Code wasn't found on this PC, so this button won't work until it's installed." : "Uses one of your Claude checks.";
  if (reloaded) { E.loadedFor = null; if ($("#dtab-edit").classList.contains("on")) initEdit(); }
  renderBusy();
}
function setVideo(file, c) {
  const v = $("#dVideo"), src = `/media/${c.id}/${encodeURIComponent(file)}?v=${c.version}`;
  if (v.getAttribute("src") !== src) { v.setAttribute("src", src); v.load(); }
}
$("#dVersions").addEventListener("click", (e) => {
  const ch = e.target.closest("[data-ver]"); if (!ch) return;
  S.curVersion = ch.dataset.ver;
  $$("#dVersions .chip").forEach((x) => x.classList.toggle("on", x === ch));
  setVideo(S.curVersion, S.detail);
});
$("#dPosted").addEventListener("click", async (e) => {
  const ch = e.target.closest("[data-post]"); if (!ch) return;
  const k = ch.dataset.post, val = !(S.detail.posted && S.detail.posted[k]);
  try {
    await api(`/api/clips/${S.detail.id}/posted`, { method: "POST", body: { platform: k, value: val } });
    S.detail.posted = Object.assign({}, S.detail.posted, { [k]: val });
    renderDetail(false);
    loadClips(true);
  } catch (err) { toast(esc(err.message), true); }
});
$("#dCopy").onclick = async () => {
  try { await navigator.clipboard.writeText($("#dPost").textContent); toast("Post text copied"); }
  catch (e) { const r = document.createRange(); r.selectNodeContents($("#dPost")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Selected: press Ctrl+C"); }
};
$("#dOpen").onclick = () => api(`/api/clips/${S.detail.id}/open`, { method: "POST", body: {} });
$("#dDelete").onclick = async () => {
  const c = S.detail;
  if (!(await confirmBox("Delete this clip?", `"${c.title}" and its raw.mp4 are deleted from your PC. This can't be undone.`, "Delete"))) return;
  try {
    await api(`/api/clips/${c.id}`, { method: "DELETE" });
    closeDetail();
    toast("Clip deleted");
    loadClips(true);
  } catch (err) { toast(esc(err.message), true); }
};

function setDTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.dtab === name));
  $$(".dtab").forEach((t) => t.classList.toggle("on", t.id === "dtab-" + name));
  if (name === "edit") initEdit(); else $("#rawVideo").pause();
  if (name === "post") renderPost();
}
$$(".tab").forEach((t) => (t.onclick = () => setDTab(t.dataset.dtab)));

function renderBusy() {
  const c = S.detail, busy = (S.state && S.state.busy) || {}, st = busy[c.id];
  const b = $("#dBusy");
  if (st) {
    const job = S.state.jobs.find((j) => j.target === c.id && ["queued", "running"].includes(j.status));
    b.innerHTML = `<span class="spinner"></span> ${st === "queued" ? "Waiting for another job to finish…" : esc(job ? job.label : "Working…")}<span class="muted small" style="margin-left:auto">${esc(job && job.last || "")}</span>`;
    b.classList.remove("hidden");
  } else b.classList.add("hidden");
  $("#renderCut").disabled = $("#askClaude").disabled = $("#dDelete").disabled = $("#tSave").disabled = $("#cReedit").disabled = !!st;
  if (!c.has_raw) { $("#renderCut").disabled = $("#askClaude").disabled = true; }
}

/* ---- posting: open the upload page, show the right file, copy the text ---- */
const PLATS = [
  { k: "tiktok", n: "TikTok", fields: [["caption", "Caption", 2200, 5]], tip: "3-5 hashtags work best. Check the campaign's rules for tags or credits it requires." },
  { k: "instagram", n: "Instagram", fields: [["caption", "Caption", 2200, 4]], tip: "Upload it as a Reel (Create → Reel). Keep 3-8 hashtags." },
  { k: "youtube", n: "YouTube Shorts", fields: [["title", "Title", 100, 1], ["description", "Description", 5000, 4]], tip: "Keep #shorts in the title. Pick 'No, it's not made for kids'." },
  { k: "x", n: "X", fields: [["text", "Post text", 280, 3]], tip: "The text is already filled in on X: just add the video." },
  { k: "facebook", n: "Facebook", fields: [["caption", "Caption", 5000, 4]], tip: "Uses the clean version (no handle)." },
];
S.postPlat = "tiktok";
function defaultTexts(c) {
  const tags = (c.hashtags || "").split(/\s+/).filter((t) => t.startsWith("#"));
  const t = c.post_title || c.title || "", cap = c.caption || "";
  const yt = t.length > 88 ? t.slice(0, 88).trim() : t;
  return {
    tiktok: { caption: `${t}\n\n${cap} ${tags.slice(0, 5).join(" ")}`.trim() },
    instagram: { caption: `${t}\n\n${cap}\n.\n${tags.slice(0, 8).join(" ")}`.trim() },
    youtube: { title: `${yt} #shorts`, description: `${cap}\n\n${tags.join(" ")}`.trim() },
    x: { text: `${t} ${tags.slice(0, 2).join(" ")}`.trim().slice(0, 280) },
    facebook: { caption: `${t}\n\n${cap} ${tags.slice(0, 5).join(" ")}`.trim() },
  };
}
function postTexts(c) { const d = defaultTexts(c), saved = c.post_texts || {}; for (const k in saved) d[k] = Object.assign({}, d[k], saved[k]); return d; }
function renderPost() {
  const c = S.detail, P = PLATS.find((x) => x.k === S.postPlat), texts = postTexts(c)[P.k];
  const camp = campOf(c.login), paid = camp ? new Set(camp.platforms || []) : null;
  $("#postPlats").innerHTML = PLATS.map((x) => {
    const out = paid && !paid.has(x.k);
    return `<span class="chip ${x.k === S.postPlat ? "on" : ""} ${out ? "notcamp" : ""}" data-pplat="${x.k}" ${out ? 'title="not in this campaign"' : ""}>${c.posted && c.posted[x.k] ? '<span class="ok">✓</span> ' : ""}${x.n}${out ? "<small>not in this campaign</small>" : ""}</span>`;
  }).join("");
  const box = $("#postCamp");
  box.classList.toggle("hidden", !camp);
  if (camp) {
    box.innerHTML = `<h3>${esc(camp.name || "Campaign rules")}${paid.has(P.k) ? "" : ` <span class="badge r">not in this campaign</span>`}</h3>
      <ul>${campChecklist(camp, c).map(([st, t]) => `<li class="${st}"><i>${st === "ok" ? "✓" : st === "bad" ? "✗" : "•"}</i><span>${esc(t)}</span></li>`).join("")}</ul>`;
  }
  $("#postFields").innerHTML = P.fields.map(([f, label, max, rows]) => `<label class="fld">${label}<span class="counter" data-for="pf_${f}"></span>
      ${rows > 1 ? `<textarea id="pf_${f}" rows="${rows}" data-max="${max}">${esc(texts[f] || "")}</textarea>` : `<input id="pf_${f}" data-max="${max}" value="${esc(texts[f] || "")}">`}</label>`).join("");
  $$("#postFields [data-max]").forEach((el) => { el.addEventListener("input", () => { counters(); savePostTexts(); }); });
  counters();
  $("#postTip").textContent = (c.censored ? "This clip had nudity, now pixelated: watch it once before posting, reach may be limited and some campaigns reject it. "
    : c.nudity ? "Claude saw nudity in this clip and it is NOT blurred: platforms may remove it. " : "") + P.tip;
  const noMark = camp && camp.watermark === false;
  const file = (noMark ? (c.videos || []).find((f) => f === "clip_clean.mp4") : null)
    || (c.videos || []).find((f) => f.slice(5, -4).split("+").includes(P.k)) || (c.videos || []).find((f) => f === "clip_clean.mp4") || c.main;
  $("#postFile").innerHTML = `File: <b>${esc(file || "?")}</b>${noMark ? " · no handle (this campaign doesn't allow it)"
    : c.files && c.files[file] && c.files[file] !== "no handle" ? ` · with ${esc(c.files[file])}` : ""}`;
  $("#postOpen").textContent = P.k === "x" ? "Open X with the text" : P.k === "youtube" ? "Copy title & open YouTube" : `Copy caption & open ${P.n}`;
  $("#postCopy2").classList.toggle("hidden", P.k !== "youtube");
  $("#postStep3").innerHTML = P.k === "x" ? "Check the text, add the video, then post."
    : P.k === "youtube" ? "Press <b>Ctrl+V</b> in the title, then use <b>Copy description</b> for the description, then publish."
    : "Press <b>Ctrl+V</b> to paste the caption, then post.";
  const link = (c.links || {})[P.k];
  $("#postLink").value = link || "";
  $("#postDoneInfo").innerHTML = c.posted && c.posted[P.k] ? `Posted ✓ ${link ? `· <a href="${esc(link)}" target="_blank">open the post</a>` : ""}` : "";
}
function counters() {
  $$("#postFields [data-max]").forEach((el) => {
    const n = [...el.value].length, max = +el.dataset.max, c = $(`[data-for="${el.id}"]`);
    c.textContent = `${n} / ${max}`; c.classList.toggle("over", n > max);
  });
}
let postSaveT = null;
function savePostTexts() {
  clearTimeout(postSaveT);
  postSaveT = setTimeout(async () => {
    const c = S.detail, P = PLATS.find((x) => x.k === S.postPlat), val = {};
    P.fields.forEach(([f]) => (val[f] = $("#pf_" + f).value));
    c.post_texts = Object.assign({}, c.post_texts, { [P.k]: val });
    try { await api(`/api/clips/${c.id}/post-texts`, { method: "POST", body: { [P.k]: val } }); } catch (e) { /* ignore */ }
  }, 600);
}
$("#postPlats").addEventListener("click", (e) => { const ch = e.target.closest("[data-pplat]"); if (ch) { S.postPlat = ch.dataset.pplat; renderPost(); } });
$("#goPost").onclick = () => setDTab("post");
async function copyText(t) { try { await navigator.clipboard.writeText(t); } catch (e) { /* the app also copies it */ } }
$("#postOpen").onclick = async () => {
  const c = S.detail, P = PLATS.find((x) => x.k === S.postPlat), first = $("#pf_" + P.fields[0][0]).value;
  const over = $$("#postFields .counter.over").length;
  if (over) { toast("The text is too long for this platform: shorten it first", true); return; }
  await copyText(first);
  try {
    await api(`/api/clips/${c.id}/post-open`, { method: "POST", body: { platform: P.k, copy: first, x_text: P.k === "x" ? first : "" } });
    toast(`<b>${P.n} is opening</b> · drag the selected file onto the page${P.k === "x" ? "" : ", then Ctrl+V"}`);
  } catch (err) { toast(esc(err.message), true); }
};
$("#postCopy2").onclick = async () => {
  const t = $("#pf_description").value;
  await copyText(t);
  try { await api(`/api/clips/${S.detail.id}/post-open`, { method: "POST", body: { platform: "youtube", copy: t, reveal: false, open_page: false } }); } catch (e) { /* ignore */ }
  toast("Description copied: paste it with Ctrl+V");
};
$("#postDone").onclick = async () => {
  const c = S.detail, P = PLATS.find((x) => x.k === S.postPlat), link = $("#postLink").value.trim();
  try {
    await api(`/api/clips/${c.id}/post-done`, { method: "POST", body: { platform: P.k, link } });
    c.posted = Object.assign({}, c.posted, { [P.k]: true });
    if (link) c.links = Object.assign({}, c.links, { [P.k]: link });
    renderPost(); renderDetail(false); loadClips(true);
    toast(`Marked as posted on <b>${P.n}</b>`);
  } catch (err) { toast(esc(err.message), true); }
};

/* ---- title on the video ---- */
function tPreview() {
  $("#tPreview").innerHTML = titleHtml({ title: $("#tTitle").value || "…", highlight: $("#tHighlight").value.trim(), emoji: $("#tEmoji").value });
}
function setEmoji(sel, v) {
  if (v && ![...sel.options].some((o) => o.value === v)) sel.add(new Option(v, v));
  sel.value = v || "";
}
function openTitleEditor(idea) {
  const c = S.detail;
  if (!c.has_raw) { toast("This clip has no raw.mp4, so the title on the video can't be changed", true); return; }
  setDTab("info");
  const src = idea || { title: c.title === "(no title)" ? "" : c.title, highlight: c.highlight || "", emoji: c.emoji || "" };
  S.ideaTitle = idea ? idea.title : null;          // learning: was the saved title one of Claude's ideas?
  $("#tTitle").value = src.title; $("#tHighlight").value = src.highlight || ""; setEmoji($("#tEmoji"), src.emoji);
  $("#tOld").classList.toggle("hidden", !c.old_title || c.old_title === c.title);
  $("#titleEditor").classList.remove("hidden");
  tPreview();
  $("#tTitle").focus();
}
["#tTitle", "#tHighlight", "#tEmoji"].forEach((x) => $(x).addEventListener("input", tPreview));
$("#dTitle").onclick = () => openTitleEditor();
$("#dTitleEdit").onclick = () => openTitleEditor();
$("#tCancel").onclick = () => $("#titleEditor").classList.add("hidden");
$("#tOld").onclick = () => {
  const c = S.detail;
  $("#tTitle").value = c.old_title || "";
  $("#tHighlight").value = /gta 6/i.test(c.old_title || "") ? "GTA 6" : "";
  tPreview();
};
$("#tTitle").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#tSave").click(); });
$("#tSave").onclick = async () => {
  const title = $("#tTitle").value.trim(), hl = $("#tHighlight").value.trim();
  if (!title) { toast("Write a title first", true); return; }
  if (hl && !title.toLowerCase().includes(hl.toLowerCase())) { toast("The yellow word must be part of the title", true); return; }
  try {
    await api(`/api/clips/${S.detail.id}/retitle`, { method: "POST", body: { title, highlight: hl, emoji: $("#tEmoji").value, idea: !!S.ideaTitle && title === S.ideaTitle } });
    $("#titleEditor").classList.add("hidden");
    toast("<b>Saving the new title</b> · the clip is re-rendered with the same cut");
  } catch (err) { toast(esc(err.message), true); }
};

/* ---- your own context ---- */
$("#cEdit").onclick = () => {
  $("#cText").value = S.detail.user_context || "";
  $("#cEditor").classList.remove("hidden"); $("#cShow").classList.add("hidden");
  $("#cText").focus();
};
$("#cCancel").onclick = () => { $("#cEditor").classList.add("hidden"); $("#cShow").classList.remove("hidden"); $("#cIdeasBox").innerHTML = ""; };
async function saveContext(quiet) {
  const ctx = $("#cText").value.trim();
  await api(`/api/clips/${S.detail.id}/text`, { method: "POST", body: { user_context: ctx } });
  S.detail.user_context = ctx;
  const cs = $("#cShow");
  cs.textContent = ctx || "Not written yet.";
  cs.classList.toggle("empty-ctx", !ctx);
  $("#cEdit").textContent = ctx ? "Edit" : "Write it";
  if (!quiet) toast("Context saved");
  return ctx;
}
$("#cSave").onclick = async () => {
  try { await saveContext(); $("#cEditor").classList.add("hidden"); $("#cShow").classList.remove("hidden"); } catch (err) { toast(esc(err.message), true); }
};
$("#cIdeas").onclick = async () => {
  const ctx = $("#cText").value.trim();
  if (ctx.length < 5) { toast("Write what happens first", true); return; }
  const box = $("#cIdeasBox");
  box.innerHTML = `<div class="muted small"><span class="spinner" style="width:14px;height:14px"></span>&nbsp; Claude is writing title ideas…</div>`;
  $("#cIdeas").disabled = true;
  try {
    const r = await api(`/api/clips/${S.detail.id}/suggest-titles`, { method: "POST", body: { context: ctx } });
    S.detail.user_context = ctx;
    S.ideas = r.ideas;
    box.innerHTML = r.ideas.map((x, i) => `<button class="idea" data-idea="${i}">${titleHtml(x)}<small>Click to use this title</small></button>`).join("")
      || `<div class="muted small">No ideas came back, try again.</div>`;
    const cs = $("#cShow"); cs.textContent = ctx; cs.classList.remove("empty-ctx"); $("#cEdit").textContent = "Edit";
  } catch (err) { box.innerHTML = `<div class="small" style="color:var(--red)">${esc(err.message)}</div>`; }
  $("#cIdeas").disabled = false;
};
$("#cIdeasBox").addEventListener("click", (e) => {
  const b = e.target.closest("[data-idea]"); if (!b) return;
  openTitleEditor(S.ideas[+b.dataset.idea]);
  $("#titleEditor").scrollIntoView({ behavior: "smooth", block: "nearest" });
});
$("#cReedit").onclick = async () => {
  const ctx = $("#cText").value.trim();
  if (ctx.length < 5) { toast("Write what happens first", true); return; }
  if (!(await confirmBox("Re-edit with your context?", "Claude re-watches the moment in raw.mp4 and re-cuts the clip and its title based on what you wrote.", "Re-edit", false))) return;
  try {
    await api(`/api/clips/${S.detail.id}/reedit`, { method: "POST", body: { context: ctx } });
    S.detail.user_context = ctx;
    $("#cEditor").classList.add("hidden"); $("#cShow").classList.remove("hidden");
    toast("<b>Claude is re-editing this clip</b> with your context");
  } catch (err) { toast(esc(err.message), true); }
};

/* ---- post text ---- */
$("#pEdit").onclick = () => {
  const c = S.detail;
  $("#pTitle").value = c.post_title || ""; $("#pCaption").value = c.caption || ""; $("#pTags").value = c.hashtags || "";
  $("#pEditor").classList.remove("hidden"); $("#dPost").classList.add("hidden");
};
$("#pCancel").onclick = () => { $("#pEditor").classList.add("hidden"); $("#dPost").classList.remove("hidden"); };
$("#pSave").onclick = async () => {
  const body = { post_title: $("#pTitle").value.trim(), caption: $("#pCaption").value.trim(), hashtags: $("#pTags").value.trim() };
  try {
    const r = await api(`/api/clips/${S.detail.id}/text`, { method: "POST", body });
    const added = r && (r.caption !== body.caption || r.hashtags !== body.hashtags);
    if (r) { body.caption = r.caption ?? body.caption; body.hashtags = r.hashtags ?? body.hashtags; }
    Object.assign(S.detail, { post_title: body.post_title, caption: body.caption, hashtags: body.hashtags });
    $("#dPost").textContent = [body.post_title, body.caption, body.hashtags].filter(Boolean).join("\n\n");
    $("#pCancel").click();
    toast(added ? "Post text saved · the campaign's required hashtags / credit were added back" : "Post text saved");
  } catch (err) { toast(esc(err.message), true); }
};

/* ---- edit tab: raw player, timeline, webcam box, gameplay band ---- */
const rawV = $("#rawVideo");
function initEdit() {
  const c = S.detail;
  if (!c.has_raw) { $("#claudeHint").textContent = "This clip has no raw.mp4, so it can't be re-edited."; return; }
  if (E.loadedFor === c.id + ":" + c.version) return;
  E.loadedFor = c.id + ":" + c.version;
  E.camMode = "keep"; E.box = c.facecam ? { x: c.facecam.x, y: c.facecam.y, w: c.facecam.w, h: c.facecam.h } : null;
  E.focus = typeof c.focus_x === "number" ? c.focus_x : 0.5;
  $$("#camMode button").forEach((b) => b.classList.toggle("on", b.dataset.cam === "keep"));
  $("#camRememberWrap").classList.add("hidden"); $("#camRemember").checked = false;
  $("#eTitle").value = c.title === "(no title)" ? "" : c.title;
  $("#useOld").classList.toggle("hidden", !c.old_title || c.old_title === c.title);
  $("#useOld").title = c.old_title ? `Before the re-edit: ${c.old_title}` : "";
  $("#eHighlight").value = c.highlight || "";
  const em = $("#eEmoji");
  if (![...em.options].some((o) => o.value === (c.emoji || ""))) em.add(new Option(c.emoji, c.emoji));
  em.value = c.emoji || "";
  $("#eFocus").value = E.focus;
  $("#eNote").value = ""; $("#eUseRange").checked = false;
  rawV.src = `/media/${c.id}/raw.mp4?v=${c.version}`;
  rawV.load();
  rawV.onloadedmetadata = () => {
    E.dur = rawV.duration || 0;
    if (rawV.videoWidth) $("#rawWrap").style.aspectRatio = `${rawV.videoWidth} / ${rawV.videoHeight}`;
    E.origStart = Math.min(c.raw_offset, E.dur);
    E.origEnd = Math.min(E.dur, c.raw_offset + (c.span || c.duration || 20));
    E.start = E.origStart; E.end = E.origEnd;
    rawV.currentTime = E.start;
    drawEdit();
  };
}
function camLayoutFrac() {
  const vw = rawV.videoWidth || 16, vh = rawV.videoHeight || 9;
  const reel = E.camMode === "draw" ? !!E.box : E.camMode === "keep" ? !!(S.detail && S.detail.facecam) : false;
  return (vh / vw) * (reel ? 1080 / 1280 : 1080 / 1920);
}
function drawEdit() {
  const d = E.dur || 1;
  const pct = (t) => `${(Math.max(0, Math.min(d, t)) / d) * 100}%`;
  $("#tlOrig").style.left = pct(E.origStart); $("#tlOrig").style.width = `${((E.origEnd - E.origStart) / d) * 100}%`;
  $("#tlSel").style.left = pct(E.start); $("#tlSel").style.width = `${((E.end - E.start) / d) * 100}%`;
  $("#tlHead").style.left = pct(rawV.currentTime);
  $("#rawTime").textContent = fmtT(rawV.currentTime);
  $("#cutLen").textContent = `Your cut: ${fmtT(E.start)} → ${fmtT(E.end)} (${(E.end - E.start).toFixed(1)} s)`;
  const keep = keepCuts();
  $("#cutHint").textContent = keep
    ? "Start and end unchanged: the clip keeps its jump cuts and callouts (only what you changed is redone)."
    : "Your cut keeps everything between start and end (no jump cuts).";
  const w = Math.min(1, camLayoutFrac());
  const left = Math.max(0, Math.min(1 - w, E.focus - w / 2));
  const band = $("#focusBand");
  band.style.left = `${left * 100}%`; band.style.width = `${w * 100}%`;
  const box = $("#camBox");
  const showBox = E.camMode !== "none" && E.box;
  box.classList.toggle("hidden", !showBox);
  if (showBox) Object.assign(box.style, { left: `${E.box.x * 100}%`, top: `${E.box.y * 100}%`, width: `${E.box.w * 100}%`, height: `${E.box.h * 100}%` });
  $("#rawWrap").classList.toggle("draw", E.camMode === "draw");
}
function tick() {
  if (!rawV.paused) {
    if (E.stopAt != null && rawV.currentTime >= E.stopAt) { rawV.pause(); E.stopAt = null; }
    drawEdit();
  }
  $("#rawPlay").textContent = rawV.paused ? "Play" : "Pause";
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
rawV.addEventListener("seeked", drawEdit);
$("#rawPlay").onclick = () => { E.stopAt = null; rawV.paused ? rawV.play() : rawV.pause(); };
$("#setStart").onclick = () => { E.start = Math.min(rawV.currentTime, E.end - 3); drawEdit(); };
$("#setEnd").onclick = () => { E.end = Math.max(rawV.currentTime, E.start + 3); drawEdit(); };
$("#playCut").onclick = () => { rawV.currentTime = E.start; E.stopAt = E.end; rawV.play(); };

// timeline: click to seek, drag the yellow handles to change start / end
(function () {
  const tl = $("#timeline");
  let drag = null;
  const tAt = (e) => { const r = tl.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (E.dur || 0); };
  tl.addEventListener("mousedown", (e) => {
    if (!E.dur) return;
    if (e.target.classList.contains("l")) drag = "l";
    else if (e.target.classList.contains("r")) drag = "r";
    else { drag = "seek"; rawV.currentTime = tAt(e); }
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const t = tAt(e);
    if (drag === "l") { E.start = Math.min(t, E.end - 3); rawV.currentTime = E.start; }
    else if (drag === "r") { E.end = Math.max(t, E.start + 3); rawV.currentTime = E.end; }
    else rawV.currentTime = t;
    drawEdit();
  });
  window.addEventListener("mouseup", () => (drag = null));
})();

// video overlay: drag sideways = move the gameplay band; in "Draw box" mode = draw the webcam box
(function () {
  const ov = $("#rawOverlay");
  let st = null;
  const pos = (e) => { const r = ov.getBoundingClientRect(); return { x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)) }; };
  ov.addEventListener("mousedown", (e) => { st = { p: pos(e), moved: false }; e.preventDefault(); });
  window.addEventListener("mousemove", (e) => {
    if (!st) return;
    const p = pos(e);
    if (Math.abs(p.x - st.p.x) + Math.abs(p.y - st.p.y) > 0.006) st.moved = true;
    if (!st.moved) return;
    if (E.camMode === "draw") {
      E.box = { x: Math.min(p.x, st.p.x), y: Math.min(p.y, st.p.y), w: Math.abs(p.x - st.p.x), h: Math.abs(p.y - st.p.y) };
    } else {
      E.focus = p.x; $("#eFocus").value = E.focus;
    }
    drawEdit();
  });
  window.addEventListener("mouseup", () => {
    if (!st) return;
    if (!st.moved) { E.stopAt = null; rawV.paused ? rawV.play() : rawV.pause(); }
    else if (E.camMode === "draw" && E.box && (E.box.w < 0.03 || E.box.h < 0.03)) E.box = null;
    st = null; drawEdit();
  });
})();
$("#eFocus").oninput = (e) => { E.focus = parseFloat(e.target.value); drawEdit(); };
$("#camMode").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cam]"); if (!b) return;
  E.camMode = b.dataset.cam;
  $$("#camMode button").forEach((x) => x.classList.toggle("on", x === b));
  if (E.camMode === "keep") { const f = S.detail.facecam; E.box = f ? { ...f } : null; }
  if (E.camMode === "draw") { toast("Drag a box around the streamer's webcam on the video"); $("#rawWrap").scrollIntoView({ behavior: "smooth", block: "center" }); }
  $("#camRememberWrap").classList.toggle("hidden", E.camMode !== "draw");
  drawEdit();
});

function keepCuts() {
  const c = S.detail;
  return !!(c && (c.jump_cuts || []).length > 1 && Math.abs(E.start - E.origStart) < 0.05 && Math.abs(E.end - E.origEnd) < 0.05);
}
$("#useOld").onclick = () => {
  const c = S.detail;
  $("#eTitle").value = c.old_title || "";
  const hl = (c.highlight && (c.old_title || "").toLowerCase().includes(c.highlight.toLowerCase())) ? c.highlight
    : /gta 6/i.test(c.old_title || "") ? "GTA 6" : "";
  $("#eHighlight").value = hl;
  toast("Old title filled in: pick the yellow word, then Render my cut");
};
$("#renderCut").onclick = async () => {
  const c = S.detail;
  if (E.camMode === "draw" && !E.box) { toast("Draw the webcam box on the video first (or pick Keep / No webcam)", true); return; }
  const body = { start: E.start, end: E.end, title: $("#eTitle").value.trim(), highlight: $("#eHighlight").value.trim(),
    emoji: $("#eEmoji").value, focus: E.focus, keep_cuts: keepCuts() };
  if (E.camMode === "none") body.cam = "none";
  if (E.camMode === "draw") body.cam = E.box;
  try {
    await api(`/api/clips/${c.id}/recut`, { method: "POST", body });
    if (E.camMode === "draw" && $("#camRemember").checked && c.platform && c.login) {
      api(`/api/streamers/${encodeURIComponent(c.platform + ":" + c.login)}`, { method: "PATCH", body: { facecam: E.box } })
        .then(() => toast(`Webcam box saved for <b>${esc(c.login)}</b>`)).catch(() => toast("Couldn't save the webcam box for this streamer", true));
    }
    toast("<b>Rendering your cut</b> · it appears here when it's ready");
    rawV.pause();
  } catch (err) { toast(esc(err.message), true); }
};
$("#askClaude").onclick = async () => {
  const c = S.detail, body = { note: $("#eNote").value.trim() };
  if ($("#eUseRange").checked) { body.start = E.start; body.end = E.end; }
  try {
    await api(`/api/clips/${c.id}/reedit`, { method: "POST", body });
    toast("<b>Claude is re-editing this clip</b> · it appears here when it's ready");
    rawV.pause();
  } catch (err) { toast(esc(err.message), true); }
};

/* ------------------------------------------------------------------ past streams */
function renderPastSelects() {
  const sel = $("#pastStreamer"), cur = sel.value;
  sel.innerHTML = S.streamers.map((s) => `<option value="${esc(s.key)}" ${s.key === cur ? "selected" : ""}>${esc(s.name)} · ${esc(s.platform)}</option>`).join("")
    || `<option value="">Add streamers in Live clipping first</option>`;
  const ns = $("#pastName"), ncur = ns.value;
  ns.innerHTML = S.streamers.map((s) => `<option value="${esc(s.name)}" ${s.name === ncur ? "selected" : ""}>${esc(s.name)}</option>`).join("")
    + `<option value="__other" ${ncur === "__other" ? "selected" : ""}>Other…</option>`;
}
$("#pastName").onchange = () => $("#pastNameOther").classList.toggle("hidden", $("#pastName").value !== "__other");

$("#pastSearch").onclick = async () => {
  const key = $("#pastStreamer").value;
  if (!key) return;
  const box = $("#pastList");
  box.innerHTML = `<div class="empty"><span class="spinner"></span>&nbsp; Looking for past broadcasts…</div>`;
  $("#pastSearch").disabled = true;
  try {
    S.past = await api(`/api/past/list?key=${encodeURIComponent(key)}&hours=${$("#pastHours").value}`);
    S.pastFor = S.streamers.find((s) => s.key === key);
    S.pastSel = new Set(S.past.filter((v) => !v.done).map((v) => v.url));
    if (S.pastFor) { $("#pastName").value = S.pastFor.name; $("#pastNameOther").classList.add("hidden"); }
    renderPast();
  } catch (err) { box.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
  $("#pastSearch").disabled = false;
};
function renderPast() {
  const box = $("#pastList");
  if (!S.past.length) { box.innerHTML = `<div class="empty">No past broadcasts found in that time (or the platform could not be reached).</div>`; updatePastInfo(); return; }
  box.innerHTML = S.past.map((v) => {
    const d = v.timestamp ? new Date(v.timestamp * 1000) : null;
    return `<label class="vod ${S.pastSel.has(v.url) ? "sel" : ""}" data-url="${esc(v.url)}">
      <input type="checkbox" ${S.pastSel.has(v.url) ? "checked" : ""}>
      <div style="min-width:0"><div class="vod-t">${esc(v.title || "(untitled)")}</div>
        <div class="vod-m">${d ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + " · " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) + " · " : ""}${fmtDur(v.duration)}</div></div>
      ${v.done ? `<span class="badge g">Already clipped</span>` : `<span class="badge y">New</span>`}</label>`;
  }).join("");
  updatePastInfo();
}
$("#pastList").addEventListener("change", (e) => {
  const row = e.target.closest(".vod"); if (!row) return;
  e.target.checked ? S.pastSel.add(row.dataset.url) : S.pastSel.delete(row.dataset.url);
  row.classList.toggle("sel", e.target.checked);
  updatePastInfo();
});
$("#pastLinks").addEventListener("input", updatePastInfo);
function pastUrls() {
  const typed = $("#pastLinks").value.split(/\s+/).map((x) => x.trim()).filter((x) => /^https?:\/\//.test(x));
  return [...new Set([...S.pastSel, ...typed])];
}
function updatePastInfo() {
  const n = pastUrls().length;
  $("#pastSelInfo").textContent = `${n} broadcast${n === 1 ? "" : "s"} selected`;
  $("#pastGo").disabled = !n;
}
function pastTopNote() {
  const n = Math.max(0, parseInt($("#pastTop").value) || 0);
  $("#pastTopNote").textContent = n ? `At most ${n} clip${n === 1 ? "" : "s"} per stream` : "No limit: every good moment is clipped";
}
$$(".stepper button").forEach((b) => (b.onclick = () => {
  const i = $("#pastTop"); i.value = Math.max(0, (parseInt(i.value) || 0) + parseInt(b.dataset.step));   // 0 = no limit
  pastTopNote();
}));
$("#pastTop").addEventListener("input", pastTopNote);
$("#pastGo").onclick = async () => {
  const urls = pastUrls();
  let name = $("#pastName").value;
  if (name === "__other") name = $("#pastNameOther").value.trim();
  try {
    await api("/api/past/clip", { method: "POST", body: { urls, streamer: name, top: String(Math.max(0, parseInt($("#pastTop").value) || 0)), any_game: $("#pastAnyGame").checked } });
    toast(`<b>Clipping ${urls.length} past broadcast${urls.length === 1 ? "" : "s"}</b> · follow it here or in Activity`);
    S.pastSel.clear(); $("#pastLinks").value = ""; renderPast();
  } catch (err) { toast(esc(err.message), true); }
};
const STATUS_BADGE = { queued: ["", "Waiting"], running: ["y", "Running"], done: ["g", "Done"], failed: ["r", "Failed"], stopped: ["", "Stopped"], stopping: ["y", "Stopping"], cancelled: ["", "Cancelled"] };
function jobHtml(j, cancel = true) {
  const [cls, txt] = STATUS_BADGE[j.status] || ["", j.status];
  return `<div class="job ${S.logJob === j.id ? "sel" : ""}" data-job="${j.id}">
    <div class="job-h">${j.status === "running" ? `<span class="spinner" style="width:14px;height:14px"></span>` : ""}<span class="job-l">${esc(j.label)}</span><span class="badge ${cls}">${txt}</span></div>
    ${j.last ? `<div class="job-last">${esc(j.last)}</div>` : ""}
    ${cancel && ["queued", "running"].includes(j.status) ? `<button class="cx" data-cancel="${j.id}">${j.kind === "live" ? "Stop" : "Cancel"}</button>` : ""}</div>`;
}
function renderPastJobs() {
  const list = S.state.jobs.filter((j) => j.kind === "past").slice(0, 8);
  const html = list.length ? list.map((j) => jobHtml(j)).join("") : `<div class="empty small">No past-stream jobs yet.</div>`;
  if (html !== S.pastJobsHtml) { $("#pastJobs").innerHTML = html; S.pastJobsHtml = html; }
}

/* ------------------------------------------------------------------ activity */
function renderJobs() {
  const html = S.state.jobs.length ? S.state.jobs.map((j) => jobHtml(j)).join("") : `<div class="empty small">Nothing has run yet.</div>`;
  if (html !== S.jobsHtml) { $("#jobList").innerHTML = html; S.jobsHtml = html; }
}
document.addEventListener("click", async (e) => {
  const cx = e.target.closest("[data-cancel]");
  if (cx) {
    e.stopPropagation();
    await api(`/api/jobs/${cx.dataset.cancel}/cancel`, { method: "POST" });
    toast("Stopping…");
    return;
  }
  const j = e.target.closest("[data-job]");
  if (j) {
    S.logJob = S.logJob === j.dataset.job ? null : j.dataset.job;
    const job = S.state.jobs.find((x) => x.id === S.logJob);
    $("#logTitle").textContent = job ? job.label : "All activity";
    $("#logAll").classList.toggle("hidden", !S.logJob);
    S.jobsHtml = S.pastJobsHtml = null;
    if (S.page !== "activity") go("activity"); else { renderJobs(); renderLog(true); }
  }
});
$("#logAll").onclick = () => { S.logJob = null; $("#logTitle").textContent = "All activity"; $("#logAll").classList.add("hidden"); S.jobsHtml = null; renderJobs(); renderLog(true); };
function renderLog(full, newLines) {
  if (S.page !== "activity") return;
  const box = $("#logBox");
  const ok = (l) => !S.logJob || l.job === S.logJob;
  const stick = $("#autoScroll").checked;
  if (full || !newLines) box.innerHTML = S.logs.filter(ok).slice(-1500).map(feedLine).join("") || `<div class="empty">No activity yet.</div>`;
  else {
    const add = newLines.filter(ok);
    if (!add.length) return;
    $(".empty", box) && box.replaceChildren();
    box.insertAdjacentHTML("beforeend", add.map(feedLine).join(""));
    while (box.childElementCount > 2000) box.firstElementChild.remove();
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------------ settings */
async function loadSettings() {
  try { S.settings = await api("/api/settings"); } catch (e) { return; }
  $$("[data-set]").forEach((el) => {
    const v = S.settings[el.dataset.set];
    if (el.type === "checkbox") el.checked = el.dataset.kind === "captions" ? v === "mini" || v === true : !!v;
    else el.value = v ?? "";
  });
  $("#outDir").textContent = S.settings["storage.output_dir"];
  loadLearning();
}
$("#saveSettings").onclick = async () => {
  const body = {};
  $$("[data-set]").forEach((el) => {
    const k = el.dataset.set, kind = el.dataset.kind;
    body[k] = el.type === "checkbox" ? (kind === "captions" ? (el.checked ? "mini" : false) : el.checked) : el.value;
  });
  try { await api("/api/settings", { method: "POST", body }); toast("<b>Settings saved</b>"); }
  catch (err) { toast(esc(err.message), true); }
};

/* ------------------------------------------------------------------ learning */
const LEARN_LABELS = { spike_multiplier: "Chat spike ×", min_messages_in_window: "Min chat msgs", voice_sensitivity: "Voice",
  chaos_ratio: "Chaos ×", min_score: "Min score", hot_takes: "Speech listener" };
async function loadLearning() {
  let L;
  try { L = await api("/api/learning"); } catch (e) { $("#learnStats").innerHTML = `<div class="small" style="color:var(--red)">${esc(e.message)}</div>`; return; }
  S.learning = L;
  const c = L.counts || {};
  const stat = (n, label, hi) => `<div class="learn-stat${hi ? " hi" : ""}"><b>${n}</b><span>${label}</span></div>`;
  $("#learnStats").innerHTML = stat(c.missed || 0, "missed (cut by hand)") + stat(c.deleted || 0, "deleted") +
    stat(c.retitled || 0, "retitled") + stat(c.timing || 0, "re-timed") + stat(c.reedited || 0, "re-edited with Claude") +
    stat(L.since_last || 0, "new since last run", L.since_last > 0) +
    stat(L.decisions || 0, "engine decisions logged");
  $("#learnBadge").textContent = L.busy ? "learning…" : "";
  $("#learnAuto").checked = !!L.auto;
  $("#learnNow").disabled = !!L.busy;
  $("#learnUndo").disabled = !L.can_undo || !!L.busy;
  $("#learnLast").innerHTML = L.last_run
    ? `<div class="muted small" style="margin-bottom:6px">${esc(ago(L.last_run))}</div>${esc(L.summary || "No summary.")}`
    : `<span class="muted">Nothing learned yet. It needs at least ${L.min_examples} examples (${L.total} so far); it runs by itself after ${L.auto_every} new ones.</span>`;
  const rows = (L.streamers || []).map((s) => {
    const cells = Object.keys(LEARN_LABELS).filter((k) => s.learned[k] !== undefined).map((k) => {
      const v = s.learned[k], d = s.defaults[k];
      const show = (x) => (typeof x === "boolean" ? (x ? "on" : "off") : x);
      return `<tr><td>${esc(s.login)}</td><td>${LEARN_LABELS[k]}</td><td class="${v !== d ? "ch" : ""}">${v !== d ? `<s>${esc(show(d))} →</s>` : ""}${esc(show(v))}</td></tr>`;
    }).join("");
    return cells;
  }).join("");
  const margins = [["lead_in", "Seconds before the setup", L.default_lead_in], ["tail", "Seconds after the reaction", L.default_tail]]
    .filter(([k]) => L[k] !== null && L[k] !== undefined)
    .map(([k, label, d]) => `<tr><td>all</td><td>${label}</td><td class="${L[k] !== d ? "ch" : ""}">${L[k] !== d ? `<s>${esc(d)} →</s>` : ""}${esc(L[k])}</td></tr>`).join("");
  $("#learnStreamers").innerHTML = rows || margins
    ? `<table class="learn-table"><tr><th>Streamer</th><th>Setting</th><th>Default → learned</th></tr>${rows}${margins}</table>`
    : `<div class="muted small">Nothing tuned yet: every streamer uses your settings.</div>`;
  const notes = L.editor_notes || [];
  $("#learnNotesCount").textContent = notes.length || "";
  $("#learnNotes").innerHTML = notes.length
    ? notes.map((n, i) => `<li><span>${esc(n)}</span><button data-note="${i}" title="Remove this note">×</button></li>`).join("")
    : `<li class="muted"><span>No notes yet. They appear after a learning run.</span></li>`;
  const kw = (arr) => arr.map((k) => `<span class="kw">${esc(k)}</span>`).join("");
  $("#learnExtra").innerHTML =
    ((L.title_notes || []).length ? `<div><b>Title rules:</b> ${L.title_notes.map(esc).join(" · ")}</div>` : "") +
    ((L.chat_keywords || []).length ? `<div><b>Chat words:</b> ${kw(L.chat_keywords)}</div>` : "") +
    ((L.speech_keywords || []).length ? `<div><b>Speech phrases:</b> ${kw(L.speech_keywords)}</div>` : "") +
    `<div class="small" style="margin-top:8px">Files: <code>${esc(L.folder)}</code></div>`;
}
$("#learnNotes").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-note]"); if (!b) return;
  try { await api("/api/learning/note-remove", { method: "POST", body: { index: +b.dataset.note } }); toast("Note removed · live clipping uses it after a restart"); loadLearning(); }
  catch (err) { toast(esc(err.message), true); }
});
$("#learnNow").onclick = async () => {
  try { await api("/api/learning/run", { method: "POST" }); toast("<b>Learning from your clips</b> · Claude is looking at all your examples"); loadLearning(); }
  catch (err) { toast(esc(err.message), true); }
};
$("#learnUndo").onclick = async () => {
  if (!(await confirmBox("Undo the last learning?", "The engine goes back to what it had learned before the last run. Your examples are kept.", "Undo", false))) return;
  try { await api("/api/learning/undo", { method: "POST" }); toast("Last learning undone · live clipping uses it after a restart"); loadLearning(); }
  catch (err) { toast(esc(err.message), true); }
};
$("#learnReset").onclick = async () => {
  if (!(await confirmBox("Reset learning?", "Everything learned is cleared (you can still undo it). Your examples are kept.", "Reset"))) return;
  try { await api("/api/learning/reset", { method: "POST" }); toast("Learning reset · your examples are kept"); loadLearning(); }
  catch (err) { toast(esc(err.message), true); }
};
$("#learnAuto").onchange = async (e) => {
  try { await api("/api/settings", { method: "POST", body: { "learning.auto": e.target.checked } }); toast(e.target.checked ? "Learning automatically" : "Automatic learning off"); }
  catch (err) { toast(esc(err.message), true); e.target.checked = !e.target.checked; }
};
async function learnFinished(job) {
  if (job.status !== "done") return;
  try {
    const L = await api("/api/learning");
    toast(`<b>Learning finished</b><br><span class="small">${esc(L.summary || "Nothing changed.")}</span><br><span class="muted small">Restart live clipping to use it.</span>`, false, 15000);
  } catch (e) { /* ignore */ }
  if (S.page === "settings") loadLearning();
}

/* ------------------------------------------------------------------ recordings */
const R = { sessions: [], sel: new Set(), cur: null, data: null, start: null, end: null, stopAt: null, drag: null };
const gb = (b) => (b / 1e9).toFixed(b >= 10e9 ? 0 : 1) + " GB";
const clock = (t) => t ? new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "?";
const hms = (t) => { t = Math.max(0, t || 0); const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s2 = Math.floor(t % 60);
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s2).padStart(2, "0"); };
const PART_STATUS = { recording: ["y", "Recording"], converting: ["y", "Preparing…"], waiting: ["", "Waiting to prepare"], ready: ["g", "Ready"] };

async function loadRecs() {
  try {
    const r = await api("/api/recordings");
    R.sessions = r.sessions;
    renderDisk(r.disk);
    renderRecList();
    if (R.cur) {
      const p = findPart(R.cur);
      if (!p) closeRec();
      else if (p.status !== R.curStatus) openRec(R.cur, true);
    }
  } catch (e) { /* offline */ }
}
setInterval(() => { if (S.page === "recs") loadRecs(); }, 10000);

function renderDisk(d) {
  if (!d || !d.total) return;
  const low = d.free < 30e9;
  $("#disk").classList.toggle("low", low);
  $("#diskRec").innerHTML = `Recordings: <b>${gb(d.recordings || 0)}</b>`;
  $("#diskFree").innerHTML = `Free on disk: <b>${gb(d.free)}</b>`;
  $("#diskUsed").style.width = `${((d.total - d.free) / d.total) * 100}%`;
  $("#diskRecBar").style.left = `${Math.max(0, (d.total - d.free - (d.recordings || 0)) / d.total) * 100}%`;
  $("#diskRecBar").style.width = `${((d.recordings || 0) / d.total) * 100}%`;
  const b = $("#diskBanner");
  b.classList.toggle("hidden", !low);
  if (low) b.innerHTML = `Your disk is getting full (${gb(d.free)} left). Each streamer uses about 2-3 GB per hour: delete the hours you've already checked.`;
  $("#navRecs").textContent = d.recordings ? gb(d.recordings) : "";
}
function findPart(id) { for (const s of R.sessions) for (const p of s.parts) if (p.id === id) return Object.assign({ sess: s }, p); return null; }

function renderRecList() {
  const box = $("#recList"), busy = (S.state && S.state.busy) || {};
  if (!R.sessions.length) { box.innerHTML = `<div class="empty">No recordings yet. They appear here while live clipping records.</div>`; updateMerge(); return; }
  const trash = `<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3"/></svg>`;
  box.innerHTML = R.sessions.map((s) => {
    const d = s.t0 ? new Date(s.t0 * 1000) : null;
    return `<div class="sess" data-sess="${esc(s.session)}">
      <div class="sess-h"><div class="av">${esc((s.display || "?")[0].toUpperCase())}</div>
        <div style="min-width:0"><div class="sess-name">${esc(s.display || s.login)}</div>
          <div class="sess-sub">${d ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + " · " : ""}${esc(s.title || "")} · ${gb(s.size)}</div></div>
        <button class="link" data-delsess="${esc(s.session)}">Delete stream</button></div>
      ${s.parts.map((p, i) => {
        const [cls, txt] = PART_STATUS[p.status] || ["", p.status];
        const lock = busy["rec:" + p.stem];
        const name = p.parts.length > 1 ? `Parts ${Math.min(...p.parts) + 1}-${Math.max(...p.parts) + 1}` : `Part ${p.part + 1}`;
        return `<div class="prt ${R.cur === p.id ? "on" : ""}" data-part="${p.id}">
          <input type="checkbox" data-pick="${p.id}" ${R.sel.has(p.id) ? "checked" : ""} title="Tick two parts next to each other to merge them">
          <div style="min-width:0"><div class="prt-t">${name} · ${clock(p.t0)} → ${p.status === "recording" ? "now" : clock(p.ended)}</div>
            <div class="prt-m"><span>${Math.round((p.duration || 0) / 60)} min</span><span>${gb(p.size)}</span>
              ${p.clips ? `<span class="y">${p.clips} clip${p.clips > 1 ? "s" : ""}</span>` : ""}
              ${p.moments ? `<span>${p.moments} flagged</span>` : ""}
              <span class="badge ${cls}" style="padding:1px 7px">${lock ? (lock === "queued" ? "Waiting…" : "Working…") : txt}</span></div></div>
          <button class="del" data-delpart="${p.id}" title="Delete this part">${trash}</button></div>`;
      }).join("")}</div>`;
  }).join("");
  updateMerge();
}
function updateMerge() { $("#mergeBtn").disabled = R.sel.size !== 2; }

$("#recList").addEventListener("click", async (e) => {
  const pick = e.target.closest("[data-pick]");
  if (pick) {
    e.stopPropagation();
    pick.checked ? R.sel.add(pick.dataset.pick) : R.sel.delete(pick.dataset.pick);
    if (R.sel.size > 2) { const first = [...R.sel][0]; R.sel.delete(first); renderRecList(); }
    updateMerge();
    return;
  }
  const dp = e.target.closest("[data-delpart]");
  if (dp) {
    e.stopPropagation();
    const p = findPart(dp.dataset.delpart);
    if (!(await confirmBox("Delete this hour?", `${p.sess.display} · ${clock(p.t0)} → ${clock(p.ended)} (${gb(p.size)}). Clips already made from it are kept.`, "Delete"))) return;
    try { const r = await api(`/api/recordings/${p.id}`, { method: "DELETE" }); toast(`Deleted · ${gb(r.freed)} freed`); R.sel.delete(p.id); if (R.cur === p.id) closeRec(); loadRecs(); }
    catch (err) { toast(esc(err.message), true); }
    return;
  }
  const ds = e.target.closest("[data-delsess]");
  if (ds) {
    e.stopPropagation();
    const s = R.sessions.find((x) => x.session === ds.dataset.delsess);
    if (!(await confirmBox("Delete this whole stream?", `All ${s.parts.length} part${s.parts.length > 1 ? "s" : ""} of ${s.display} (${gb(s.size)}). Clips already made are kept.`, "Delete all"))) return;
    let freed = 0, failed = 0;
    for (const p of s.parts) {
      try { const r = await api(`/api/recordings/${p.id}`, { method: "DELETE" }); freed += r.freed; if (R.cur === p.id) closeRec(); }
      catch (err) { failed++; }
    }
    toast(`Deleted · ${gb(freed)} freed` + (failed ? ` · ${failed} part still recording, kept` : ""), !!failed);
    loadRecs();
    return;
  }
  const row = e.target.closest("[data-part]");
  if (row) openRec(row.dataset.part);
});
$("#mergeBtn").onclick = async () => {
  const [a, b] = [...R.sel], pa = findPart(a), pb = findPart(b);
  if (!pa || !pb) return;
  if (!(await confirmBox("Merge these two parts?", "They become one file (the few minutes they overlap are removed), so you can cut a clip across the hour change. The two original parts are replaced.", "Merge", false))) return;
  try { await api("/api/recordings/merge", { method: "POST", body: { a, b } }); toast("<b>Merging</b> · follow it in Activity"); R.sel.clear(); renderRecList(); }
  catch (err) { toast(esc(err.message), true); }
};

const rv = $("#recVideo");
function closeRec() { R.cur = null; R.data = null; rv.pause(); rv.removeAttribute("src"); rv.load(); $("#recBody").classList.add("hidden"); $("#recEmpty").classList.remove("hidden"); renderRecList(); }
async function openRec(id, refresh) {
  const p = findPart(id);
  if (!p) return;
  if (!refresh && R.cur !== id) { R.start = R.end = null; }
  R.cur = id; R.curStatus = p.status;
  renderRecList();
  $("#recEmpty").classList.add("hidden"); $("#recBody").classList.remove("hidden");
  const name = p.parts.length > 1 ? `Parts ${Math.min(...p.parts) + 1}-${Math.max(...p.parts) + 1}` : `Part ${p.part + 1}`;
  $("#recTitle").textContent = `${p.sess.display} · ${name}`;
  $("#recMeta").textContent = `${clock(p.t0)} → ${p.status === "recording" ? "now" : clock(p.ended)} · ${gb(p.size)}`;
  try { R.data = await api(`/api/recordings/${id}`); } catch (e) { return; }
  const nr = $("#recNotReady");
  if (!R.data.ready) {
    nr.classList.remove("hidden");
    nr.textContent = p.status === "recording" ? "This hour is still being recorded. It can be watched a few minutes after it ends."
      : "Preparing this hour for playback (a minute or two)…";
    rv.removeAttribute("src"); rv.load();
  } else {
    nr.classList.add("hidden");
    const src = `/media-rec/${id}`;
    if (rv.getAttribute("src") !== src) { rv.setAttribute("src", src); rv.load(); }
  }
  renderMoments();
  drawGraph();
}
function recDur() { return rv.duration || (R.data && R.data.duration) || Math.max(1, ((R.data && R.data.chat.counts.length) || 1) * 5); }

function drawGraph() {
  const cv = $("#recGraph"), d = R.data;
  if (!d) return;
  const W = cv.clientWidth, H = 120, dpr = window.devicePixelRatio || 1;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const dur = recDur(), x = (t) => (t / dur) * W;
  // chat bars
  const cc = d.chat.counts || [], bk = d.chat.bucket || 5, mx = Math.max(1, ...cc);
  g.fillStyle = "rgba(255,214,10,.55)";
  const bw = Math.max(1, (bk / dur) * W);
  cc.forEach((n, i) => { if (n) { const h = (n / mx) * (H - 34); g.fillRect(x(i * bk), H - 6 - h, bw, h); } });
  // loudness line
  const ld = d.loud || [];
  if (ld.length) {
    const lo = Math.min(...ld.filter((v) => v > -80)), hi = Math.max(...ld);
    g.strokeStyle = "rgba(200,200,200,.7)"; g.lineWidth = 1.2; g.beginPath();
    ld.forEach((v, i) => { const y = H - 6 - ((Math.max(v, lo) - lo) / Math.max(1, hi - lo)) * (H - 34); i ? g.lineTo(x(i * 5 + 2.5), y) : g.moveTo(x(2.5), y); });
    g.stroke();
  }
  // selection
  if (R.start != null && R.end != null) {
    g.fillStyle = "rgba(255,214,10,.16)"; g.fillRect(x(R.start), 0, x(R.end) - x(R.start), H);
    g.fillStyle = "#FFD60A"; g.fillRect(x(R.start) - 1, 0, 2, H); g.fillRect(x(R.end) - 1, 0, 2, H);
  }
  // clips made (yellow flags) and flagged moments (white dots)
  (d.clips || []).forEach((c) => { g.fillStyle = "#FFD60A"; g.fillRect(x(c.t), 4, Math.max(3, x(c.t + (c.dur || 20)) - x(c.t)), 8); });
  (d.moments || []).forEach((m) => { g.fillStyle = "#fff"; g.beginPath(); g.arc(x(m.t), 22, 3.5, 0, 7); g.fill(); });
  // playhead
  g.fillStyle = "#fff"; g.fillRect(x(rv.currentTime || 0) - 1, 0, 2, H);
}
window.addEventListener("resize", () => { if (S.page === "recs") drawGraph(); });

function renderMoments() {
  const d = R.data, box = $("#recMoments");
  const items = [...(d.moments || []).map((m) => ({ t: m.t, kind: "m", why: m.why })),
                 ...(d.clips || []).map((c) => ({ t: c.t, kind: "c", why: c.title + " " + (c.emoji || ""), id: c.id }))].sort((a, b) => a.t - b.t);
  box.innerHTML = items.length ? items.map((m) => {
    const clipped = m.kind === "m" && (d.clips || []).some((c) => Math.abs(c.t - m.t) < 60);
    return `<button class="mom" data-seek="${m.t}" ${m.id ? `data-clip="${m.id}"` : ""}><span class="t">${hms(m.t)}</span>
      <span class="w" title="${esc(m.why)}">${esc(m.why)}</span>
      ${m.kind === "c" ? `<span class="badge y">clip</span>` : clipped ? `<span class="badge g">clipped</span>` : `<span class="badge">skipped</span>`}</button>`;
  }).join("") : `<div class="empty small">Nothing flagged in this hour. Use the yellow chat bars to spot busy moments.</div>`;
}
$("#recMoments").addEventListener("click", (e) => {
  const b = e.target.closest("[data-seek]"); if (!b) return;
  if (b.dataset.clip && e.detail === 2) { openClip(b.dataset.clip); return; }
  if (!R.data.ready) return;
  rv.currentTime = Math.max(0, parseFloat(b.dataset.seek) - 15); rv.play();
});

// graph: click = jump there, drag = select
(function () {
  const cv = $("#recGraph"), tip = $("#recTip");
  const tAt = (e) => { const r = cv.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * recDur(); };
  cv.addEventListener("mousedown", (e) => { if (!R.data) return; R.drag = { t: tAt(e), moved: false }; e.preventDefault(); });
  window.addEventListener("mousemove", (e) => {
    if (R.drag) {
      const t = tAt(e);
      if (Math.abs(t - R.drag.t) > recDur() / 400) R.drag.moved = true;
      if (R.drag.moved) { R.start = Math.min(t, R.drag.t); R.end = Math.max(t, R.drag.t); updSel(); drawGraph(); }
    }
    if (e.target === cv && R.data) {
      const t = tAt(e), near = (arr, f) => arr.find((m) => Math.abs(f(m) - t) < recDur() / 120);
      const c = near(R.data.clips || [], (m) => m.t), m = near(R.data.moments || [], (m) => m.t);
      tip.innerHTML = `<b>${hms(t)}</b>${c ? ` · clip: ${esc(c.title)}` : ""}${m ? ` · ${esc(m.why)}` : ""}`;
      tip.style.left = e.clientX + 12 + "px"; tip.style.top = e.clientY - 34 + "px";
      tip.classList.remove("hidden");
    } else tip.classList.add("hidden");
  });
  window.addEventListener("mouseup", (e) => {
    if (!R.drag) return;
    if (!R.drag.moved && R.data && R.data.ready) { rv.currentTime = R.drag.t; }
    R.drag = null; drawGraph();
  });
})();
function updSel() {
  if (R.start == null || R.end == null) { $("#rSel").textContent = "No selection yet"; return; }
  const len = R.end - R.start;
  $("#rSel").innerHTML = `Selection <b style="color:var(--y)">${hms(R.start)} → ${hms(R.end)}</b> (${len.toFixed(1)} s)` + (len > 90 ? " · long for a short clip" : "");
}
function recTick() {
  if (S.page === "recs" && R.data) {
    if (!rv.paused && R.stopAt != null && rv.currentTime >= R.stopAt) { rv.pause(); R.stopAt = null; }
    $("#rTime").textContent = `${hms(rv.currentTime)} / ${hms(recDur())}`;
    $("#rPlay").textContent = rv.paused ? "Play" : "Pause";
    if (!rv.paused || R.lastT !== rv.currentTime) { R.lastT = rv.currentTime; drawGraph(); }
  }
  requestAnimationFrame(recTick);
}
requestAnimationFrame(recTick);
$("#rPlay").onclick = () => { R.stopAt = null; rv.paused ? rv.play() : rv.pause(); };
$("#rBack").onclick = () => (rv.currentTime = Math.max(0, rv.currentTime - 10));
$("#rFwd").onclick = () => (rv.currentTime = Math.min(recDur(), rv.currentTime + 10));
$("#rStart").onclick = () => { R.start = rv.currentTime; if (R.end == null || R.end <= R.start + 1) R.end = Math.min(recDur(), R.start + 20); updSel(); drawGraph(); };
$("#rEnd").onclick = () => { R.end = rv.currentTime; if (R.start == null || R.start >= R.end - 1) R.start = Math.max(0, R.end - 20); updSel(); drawGraph(); };
$("#rPlaySel").onclick = () => { if (R.start == null) return; rv.currentTime = R.start; R.stopAt = R.end; rv.play(); };
document.addEventListener("keydown", (e) => {
  if (S.page !== "recs" || !R.data || S.detail || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
  if (e.key === " ") { e.preventDefault(); $("#rPlay").click(); }
  else if (e.key === "ArrowLeft") rv.currentTime = Math.max(0, rv.currentTime - (e.shiftKey ? 1 : 5));
  else if (e.key === "ArrowRight") rv.currentTime = Math.min(recDur(), rv.currentTime + (e.shiftKey ? 1 : 5));
  else if (e.key.toLowerCase() === "i") $("#rStart").click();
  else if (e.key.toLowerCase() === "o") $("#rEnd").click();
});
async function makeRecClip(mode) {
  if (R.start == null || R.end == null) { toast("Select the moment first: drag on the graph, or use Set start / Set end", true); return; }
  if (!R.data.ready) { toast("This hour isn't ready to cut yet", true); return; }
  const title = $("#rcTitle").value.trim(), hl = $("#rcHighlight").value.trim();
  if (mode === "manual" && !title) { toast("Write a title (or use Make clip with Claude)", true); $("#rcTitle").focus(); return; }
  if (hl && title && !title.toLowerCase().includes(hl.toLowerCase())) { toast("The yellow word must be part of the title", true); return; }
  try {
    await api(`/api/recordings/${R.cur}/cut`, { method: "POST", body: { start: R.start, end: R.end, mode, title, highlight: hl,
      emoji: $("#rcEmoji").value, context: $("#rcContext").value.trim() } });
    toast(`<b>Making your clip</b> · it appears in Clips in a minute or two`);
    $("#rcTitle").value = $("#rcHighlight").value = $("#rcContext").value = "";
  } catch (err) { toast(esc(err.message), true); }
}
$("#rcManual").onclick = () => makeRecClip("manual");
$("#rcClaude").onclick = () => makeRecClip("claude");

/* ------------------------------------------------------------------ quit */
$("#quitBtn").onclick = async () => {
  const running = S.state && S.state.live.running;
  if (!(await confirmBox("Quit Frenzo?", running ? "Live clipping and any running jobs will stop." : "Any running jobs will stop.", "Quit"))) return;
  try { await api("/api/quit", { method: "POST" }); } catch (e) { /* server is closing */ }
  document.body.innerHTML = `<div style="margin:auto;text-align:center"><div class="brand" style="font-size:54px">frenzo<span>.</span></div><p class="muted">Frenzo is closed. You can close this window.</p></div>`;
  setTimeout(() => window.close(), 800);
};

/* ------------------------------------------------------------------ start */
(async function start() {
  await poll.call(null);
})();
loadStreamers();
loadClips(true);

/* ------------------------------------------------------------------ YouTube sign-in (cookies file) */
async function loadYtCookies() {
  let st;
  try { st = await api("/api/youtube-cookies"); } catch (e) { $("#ytStatus").textContent = "couldn't check"; return; }
  const el = $("#ytStatus");
  if (!st.present) { el.innerHTML = "not set · YouTube links may be blocked"; el.style.color = ""; }
  else {
    el.innerHTML = st.signed_in ? `<span style="color:var(--green)">✓ signed in</span> · file from ${esc(ago(new Date(st.updated * 1000).toISOString()))}`
      : `<span style="color:var(--red)">file has no YouTube login</span> · export it while signed in`;
  }
  $("#ytRemove").classList.toggle("hidden", !st.present);
}
$("#ytFile").onchange = async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await fetch("/api/youtube-cookies", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok || d.ok === false) throw new Error(d.error || "Upload failed");
    toast(d.signed_in ? "<b>YouTube sign-in saved</b> · YouTube links use it from now on"
      : "Saved, but this file has no YouTube login: export it again while signed in", !d.signed_in, 7000);
    loadYtCookies();
  } catch (err) { toast(esc(err.message), true); }
};
$("#ytRemove").onclick = async () => {
  if (!(await confirmBox("Remove the YouTube sign-in?", "The cookies file is deleted from the frenzo-engine folder. YouTube links may be blocked again.", "Remove"))) return;
  try { await api("/api/youtube-cookies", { method: "DELETE" }); toast("YouTube sign-in removed"); loadYtCookies(); }
  catch (err) { toast(esc(err.message), true); }
};
