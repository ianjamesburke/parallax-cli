/*
 * app.js — parallax-web frontend
 *
 * Talks to the backend at / via:
 *   POST /api/message         { session_id?, text } -> { session_id }
 *   POST /api/cancel          { session_id }
 *   POST /api/open_in_finder  { path? }
 *   GET  /api/gallery
 *   GET  /api/stream/<sid>    (SSE)
 *
 * SSE event kinds: hello, assistant_delta, tool_use, tool_result,
 *                  dispatch_event, agent_done, error
 */

const state = {
  sessionId: null,
  eventSource: null,
  currentAssistantEl: null,
  thinking: false,
  dispatchActive: false,
  activeVideo: null,
  refImages: new Set(), // paths selected as reference images
};

const $ = (id) => document.getElementById(id);

// ----- status dot ----------------------------------------------------------

const STATUS_LABELS = {
  thinking: "Thinking",
  streaming: "Writing",
  dispatching: "Rendering",
  error: "Error",
};

function setStatus(kind) {
  const dot = $("status-dot");
  dot.className = "status-dot";
  if (kind) dot.classList.add(kind);
  dot.title = kind || "idle";

  const bar = $("thinking-bar");
  if (kind && kind !== "error") {
    $("thinking-label").textContent = STATUS_LABELS[kind] || kind;
    bar.classList.remove("hidden");
  } else {
    bar.classList.add("hidden");
  }
}

// ----- message rendering ---------------------------------------------------

function messagesEl() {
  return $("messages");
}

function scrollToBottom() {
  const m = messagesEl();
  m.scrollTop = m.scrollHeight;
}

function appendUserMsg(text) {
  hideWelcome();
  const el = document.createElement("div");
  el.className = "msg user";
  el.textContent = text;
  messagesEl().appendChild(el);
  scrollToBottom();
}

function newAssistantMsg() {
  const el = document.createElement("div");
  el.className = "msg assistant streaming";
  el.textContent = "";
  messagesEl().appendChild(el);
  state.currentAssistantEl = el;
  scrollToBottom();
  return el;
}

function appendAssistantDelta(text) {
  if (!state.currentAssistantEl) newAssistantMsg();
  state.currentAssistantEl.textContent += text;
  scrollToBottom();
}

function finalizeAssistantMsg() {
  if (state.currentAssistantEl) {
    const el = state.currentAssistantEl;
    const raw = el.textContent;
    if (raw) {
      el.innerHTML = marked.parse(raw);
    }
    el.classList.remove("streaming");
    state.currentAssistantEl = null;
  }
}

function appendToolUse(ev) {
  finalizeAssistantMsg();
  const el = document.createElement("div");
  el.className = "tool-card";
  el.dataset.toolId = ev.id || "";
  const args =
    ev.input && typeof ev.input === "object"
      ? Object.entries(ev.input)
          .map(([k, v]) => `${k}=${JSON.stringify(v).slice(0, 80)}`)
          .join(" ")
      : "";
  el.innerHTML = `<span class="tool-name">${escapeHtml(ev.name || "tool")}</span><span class="tool-arrow">→</span><span>${escapeHtml(args)}</span>`;
  messagesEl().appendChild(el);
  scrollToBottom();
}

function appendToolResult(ev) {
  const card = messagesEl().querySelector(
    `.tool-card[data-tool-id="${CSS.escape(ev.id || "")}"]`,
  );
  if (card) {
    const summary = document.createElement("div");
    summary.className = "tool-summary";
    summary.textContent = ev.summary || "(no result)";
    card.appendChild(summary);
  }
  scrollToBottom();
}

// Event-kind noise filter: by default, only terminal phases (done / error)
// land in the chat as a strip. Everything in between (starting, run_started,
// request_intended, cost_estimated, still_generated, etc.) updates the
// status bar at the top but doesn't spam the chat. Toggle verbose mode via
// localStorage.parallax_debug = "1" in devtools when you actually want to
// see every event.
function _debugDispatchEnabled() {
  try {
    return localStorage.getItem("parallax_debug") === "1";
  } catch (_) { return false; }
}

// Phases we'll always surface as a chat strip — the rest are treated as
// status-only updates.
const DISPATCH_CHAT_PHASES = new Set(["done", "error", "starting"]);

function appendDispatchEvent(ev) {
  const phase = ev.phase || "";
  const isTerminal = phase === "done" || phase === "error";

  // Always keep the status side-effects — the top status bar should still
  // reflect that something is dispatching.
  if (isTerminal) {
    state.dispatchActive = false;
    refreshGallery();
  } else {
    state.dispatchActive = true;
    setStatus("dispatching");
  }

  // Decide whether to render the strip into the chat.
  const shouldRender = _debugDispatchEnabled() || DISPATCH_CHAT_PHASES.has(phase);
  if (!shouldRender) return;

  hideWelcome();
  const el = document.createElement("div");
  el.className = "dispatch-strip";
  if (phase === "done") el.classList.add("done");
  if (phase === "error") el.classList.add("error");

  let marker = "·";
  if (phase === "done") marker = "✓";
  if (phase === "error") marker = "!";

  const markerEl = document.createElement("span");
  markerEl.className = "dispatch-marker";
  markerEl.textContent = marker;
  el.appendChild(markerEl);

  const textEl = document.createElement("span");
  textEl.className = "dispatch-text";
  textEl.textContent = ev.text || phase;
  el.appendChild(textEl);

  messagesEl().appendChild(el);
  scrollToBottom();
}

function appendError(message) {
  finalizeAssistantMsg();
  const el = document.createElement("div");
  el.className = "error-msg";
  el.textContent = `error: ${message}`;
  messagesEl().appendChild(el);
  scrollToBottom();
}

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );
}

// ----- SSE -----------------------------------------------------------------

function openStream(sessionId) {
  if (state.eventSource) {
    try {
      state.eventSource.close();
    } catch (_) {
      // already closed — nothing to do
    }
  }
  const es = new EventSource(`/api/stream/${sessionId}`);
  state.eventSource = es;

  es.addEventListener("hello", () => {
    // Only show thinking bar if we actually just sent a message.
    // When loading history, state.thinking is false — don't surface the bar.
    if (state.thinking) setStatus("thinking");
  });

  es.addEventListener("assistant_delta", (e) => {
    const data = JSON.parse(e.data);
    setStatus("streaming");
    appendAssistantDelta(data.text || "");
  });

  es.addEventListener("tool_use", (e) => {
    const data = JSON.parse(e.data);
    appendToolUse(data);
  });

  es.addEventListener("tool_result", (e) => {
    const data = JSON.parse(e.data);
    appendToolResult(data);
  });

  es.addEventListener("dispatch_event", (e) => {
    const data = JSON.parse(e.data);
    appendDispatchEvent(data);
  });

  es.addEventListener("agent_done", (e) => {
    finalizeAssistantMsg();
    // Hard clear every "something is running" flag. Previously we'd leave
    // the thinking bar on whenever dispatchActive was still true from a
    // stale prior run — that's the bug the user caught.
    state.thinking = false;
    state.dispatchActive = false;
    setStatus("");
    updateButtons();
    refreshGallery();
    refreshUsage();
    try {
      JSON.parse(e.data);
    } catch (_) {
      // ignore — just updating UI state
    }
  });

  es.addEventListener("error", (e) => {
    try {
      const data = JSON.parse(e.data);
      appendError(data.message || "stream error");
    } catch (_) {
      // SSE dropped with no payload — surface generic message
      appendError("stream disconnected");
    }
    setStatus("error");
  });
}

// ----- sending -------------------------------------------------------------

async function sendMessage(text) {
  if (!text.trim()) return;
  appendUserMsg(text);
  state.thinking = true;
  setStatus("thinking");
  updateButtons();

  const refImages = state.refImages.size > 0 ? Array.from(state.refImages) : undefined;
  // Clear reference selection immediately after send
  if (state.refImages.size > 0) {
    state.refImages.clear();
    updateRefBar();
    // Remove selected styling from all thumbs
    document.querySelectorAll(".thumb.ref-selected").forEach((t) => t.classList.remove("ref-selected"));
  }

  try {
    const r = await fetch("/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, text, reference_images: refImages }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ error: r.statusText }));
      appendError(err.error || r.statusText);
      setStatus("error");
      state.thinking = false;
      updateButtons();
      return;
    }
    const body = await r.json();
    if (!state.sessionId) {
      state.sessionId = body.session_id;
      openStream(state.sessionId);
      refreshSidebar();
    }
  } catch (e) {
    appendError(`POST /api/message failed: ${e}`);
    setStatus("error");
    state.thinking = false;
    updateButtons();
  }
}

async function cancel() {
  if (!state.sessionId) return;
  try {
    await fetch("/api/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId }),
    });
  } catch (e) {
    console.error("cancel failed", e);
  }
}

function updateButtons() {
  $("cancel-btn").disabled = !state.thinking && !state.dispatchActive;
  $("send-btn").disabled = false;
}

// ----- gallery -------------------------------------------------------------

async function refreshGallery() {
  try {
    const r = await fetch("/api/gallery");
    if (!r.ok) return;
    const data = await r.json();
    renderGallery(data);
  } catch (e) {
    console.error("gallery fetch failed", e);
  }
}

function renderGallery(data) {
  $("project-label").textContent = data.project_dir || "";
  $("project-label").title = data.project_dir || "";

  const stills = data.stills || [];
  const videos = data.videos || [];

  $("gallery-count").textContent = String(stills.length);
  $("video-count").textContent = String(videos.length);

  const gallery = $("gallery");

  // Signature-check: if the exact sequence of paths is unchanged, skip DOM
  // work entirely. This is the common case between polls and eliminates the
  // flashing caused by re-downloading every <img> on every tick.
  const sig = stills.map((s) => s.path).join("\u0001");
  if (gallery.dataset.sig !== sig) {
    gallery.dataset.sig = sig;

    // Diff the existing thumbs keyed by path. Keep matching DOM nodes
    // mounted (so their <img> never reloads), add new ones, remove gone.
    const existing = new Map();
    for (const node of Array.from(gallery.children)) {
      if (node.dataset.path) existing.set(node.dataset.path, node);
    }

    // Remove the empty state if there are stills now
    const emptyEl = gallery.querySelector(".gallery-empty");
    if (stills.length > 0 && emptyEl) emptyEl.remove();
    if (stills.length === 0 && !emptyEl) {
      const empty = document.createElement("div");
      empty.className = "gallery-empty";
      empty.innerHTML = 'Drag images here or click <strong>+ Upload</strong>';
      gallery.appendChild(empty);
    }

    const frag = document.createDocumentFragment();
    for (const s of stills) {
      let thumb = existing.get(s.path);
      if (thumb) {
        existing.delete(s.path);
        // Sync ref-selected class in case state changed
        if (state.refImages.has(s.path)) {
          thumb.classList.add("ref-selected");
        } else {
          thumb.classList.remove("ref-selected");
        }
      } else {
        thumb = document.createElement("div");
        thumb.className = "thumb";
        thumb.dataset.path = s.path;
        const img = document.createElement("img");
        img.loading = "lazy";
        img.decoding = "async";
        img.src = `/media/${encodeURI(s.path)}`;
        img.alt = s.name;
        thumb.appendChild(img);
        const del = document.createElement("button");
        del.className = "thumb-delete";
        del.title = "Move to Trash";
        del.textContent = "×";
        del.addEventListener("click", async (e) => {
          e.stopPropagation();
          try {
            const r = await fetch("/api/image", {
              method: "DELETE",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: s.path }),
            });
            if (r.ok) {
              thumb.remove();
              const count = parseInt($("gallery-count").textContent || "0") - 1;
              $("gallery-count").textContent = String(Math.max(0, count));
            } else {
              const err = await r.json().catch(() => ({}));
              console.error("delete failed", err);
            }
          } catch (e2) {
            console.error("delete failed", e2);
          }
        });
        thumb.appendChild(del);

        // Download button
        const dl = document.createElement("a");
        dl.className = "thumb-download";
        dl.title = "Download";
        dl.textContent = "↓";
        dl.href = `/media/${encodeURI(s.path)}`;
        dl.download = s.name;
        dl.addEventListener("click", (e) => e.stopPropagation());
        thumb.appendChild(dl);

        // Zoom button (opens lightbox)
        const zoom = document.createElement("button");
        zoom.className = "thumb-zoom";
        zoom.title = "View full size";
        zoom.textContent = "⤢";
        zoom.addEventListener("click", (e) => {
          e.stopPropagation();
          openLightbox(img.src);
        });
        thumb.appendChild(zoom);

        // Click anywhere on thumb toggles reference selection
        thumb.addEventListener("click", () => {
          if (state.refImages.has(s.path)) {
            state.refImages.delete(s.path);
            thumb.classList.remove("ref-selected");
          } else {
            state.refImages.add(s.path);
            thumb.classList.add("ref-selected");
          }
          updateRefBar();
        });
      }
      frag.appendChild(thumb);
    }
    for (const stale of existing.values()) {
      stale.remove();
    }
    gallery.appendChild(frag);
  }

  // ----- video gallery + player -----
  const player = $("player-wrap");
  const vList = $("video-gallery");

  // Track which video is currently shown in the player
  if (!state.activeVideo && videos.length > 0) {
    state.activeVideo = videos[0].path;
  }
  if (state.activeVideo && !videos.find((v) => v.path === state.activeVideo)) {
    state.activeVideo = videos[0]?.path || null;
  }

  // Player
  if (videos.length === 0) {
    player.innerHTML = '<div class="video-empty">No videos yet.</div>';
  } else {
    const active = videos.find((v) => v.path === state.activeVideo) || videos[0];
    const expected = `/media/${encodeURI(active.path)}`;
    const existing = player.querySelector("video");
    if (!existing || existing.dataset.src !== expected) {
      player.innerHTML = "";
      const el = document.createElement("video");
      el.controls = true;
      el.src = expected;
      el.dataset.src = expected;
      player.appendChild(el);
    }
  }

  // Video list (signature-checked like stills)
  const vsig = videos.map((v) => v.path).join("\u0001") + "|" + (state.activeVideo || "");
  if (vList.dataset.sig !== vsig) {
    vList.dataset.sig = vsig;
    vList.innerHTML = "";
    for (const v of videos) {
      const item = document.createElement("div");
      item.className = "video-item" + (v.path === state.activeVideo ? " active" : "");
      item.innerHTML = `<span class="video-item-icon">▶</span><span class="video-item-name">${escapeHtml(v.name)}</span>`;
      const vDl = document.createElement("a");
      vDl.className = "video-item-download";
      vDl.title = "Download";
      vDl.textContent = "↓";
      vDl.href = `/media/${encodeURI(v.path)}`;
      vDl.download = v.name;
      vDl.addEventListener("click", (e) => e.stopPropagation());
      item.appendChild(vDl);

      const vDel = document.createElement("button");
      vDel.className = "video-item-delete";
      vDel.title = "Move to Trash";
      vDel.textContent = "×";
      vDel.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          const r = await fetch("/api/image", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: v.path }),
          });
          if (r.ok) {
            if (state.activeVideo === v.path) state.activeVideo = null;
            refreshGallery();
          } else {
            const err = await r.json().catch(() => ({}));
            console.error("video delete failed", err);
          }
        } catch (e2) {
          console.error("video delete failed", e2);
        }
      });
      item.appendChild(vDel);
      item.addEventListener("click", () => {
        state.activeVideo = v.path;
        renderGallery({ project_dir: data.project_dir, stills, videos });
      });
      vList.appendChild(item);
    }
  }
}

// ----- reference image bar -------------------------------------------------

function updateRefBar() {
  const bar = $("ref-bar");
  const label = $("ref-bar-label");
  if (!bar || !label) return;
  const count = state.refImages.size;
  if (count === 0) {
    bar.classList.add("hidden");
  } else {
    label.textContent = `${count} reference image${count === 1 ? "" : "s"} selected`;
    bar.classList.remove("hidden");
  }
}

// ----- welcome card --------------------------------------------------------

const WELCOME_HTML = `
  <div class="welcome" id="welcome">
    <div class="welcome-title">Welcome to Parallax</div>
    <div class="welcome-sub">A short-form video studio driven by natural language.</div>
    <ul class="welcome-list">
      <li><b>Brief the Head of Production.</b> Describe what you want — a 3-scene Ken Burns ad, a character study, a mood piece — and it handles stills, voiceover, pacing, and final render.</li>
      <li><b>Upload reference images</b> via the gallery on the right. They're selected as references by default, so the next generation uses them image-to-image via Gemini.</li>
      <li><b>Type "TEST MODE" anywhere in your brief</b> to run the full pipeline without spending API credits — placeholder stills + macOS voice.</li>
      <li><b>Projects</b> live under <code>parallax/&lt;project&gt;/</code>. Create one via the <b>+</b> in the sidebar; files at the launch-dir root are shared across every project.</li>
    </ul>
  </div>`;

function hideWelcome() {
  const w = $("welcome");
  if (w) w.remove();
}

function showWelcomeIfEmpty() {
  const m = messagesEl();
  // Only show if there's no prior content and no welcome already there.
  if (m.children.length > 0 && $("welcome")) return;
  if (m.children.length > 0) return;
  m.insertAdjacentHTML("afterbegin", WELCOME_HTML);
}

// ----- lightbox ------------------------------------------------------------

function openLightbox(src) {
  $("lightbox-img").src = src;
  $("lightbox").classList.remove("hidden");
}
function closeLightbox() {
  $("lightbox").classList.add("hidden");
  $("lightbox-img").src = "";
}

// ----- sidebar -------------------------------------------------------------

// Projects come from the filesystem (Drive folders on disk).
// Sessions are nested under the active project.
const sidebarState = {
  projects: [],       // [{name, path}] from /api/projects
  sessions: [],       // from /api/sessions
  activeProject: null, // currently selected project name
};

async function refreshSidebar() {
  try {
    const [projRes, sessRes] = await Promise.all([
      fetch("/api/projects"),
      fetch("/api/sessions"),
    ]);
    if (projRes.ok) {
      const data = await projRes.json();
      sidebarState.projects = data.projects || [];
    }
    if (sessRes.ok) {
      const data = await sessRes.json();
      sidebarState.sessions = data.sessions || [];
    }
    renderSidebar();
  } catch (e) {
    console.error("sidebar fetch failed", e);
  }
}

function renderSidebar() {
  const list = $("sidebar-list");
  const { projects, sessions, activeProject } = sidebarState;

  if (projects.length === 0 && sessions.length === 0) {
    list.innerHTML = '<div class="sidebar-empty">No projects yet. Hit + to create one.</div>';
    return;
  }

  // Build session lookup by project name
  const sessionsByProject = {};
  for (const s of sessions) {
    const proj = s.project || "main";
    if (!sessionsByProject[proj]) sessionsByProject[proj] = [];
    sessionsByProject[proj].push(s);
  }

  // Merge: projects from disk + any orphan session projects not on disk.
  // Work on a local copy — never mutate sidebarState.projects here.
  const merged = [...projects];
  const projectNames = new Set(merged.map((p) => p.name));
  for (const proj of Object.keys(sessionsByProject)) {
    if (!projectNames.has(proj)) {
      merged.push({ name: proj, path: null });
      projectNames.add(proj);
    }
  }

  list.innerHTML = "";
  const currentProject = new URL(window.location.href).searchParams.get("project") || "main";

  for (const proj of merged) {
    const isActive = proj.name === currentProject;

    // Flat project row — no nested sessions. One chat per project, loaded
    // from <workspace>/chat.jsonl on click.
    const row = document.createElement("div");
    row.className = "sidebar-project-row" + (isActive ? " active" : "");

    const labelText = document.createElement("span");
    labelText.className = "sidebar-project-name";
    labelText.textContent = proj.name;
    row.appendChild(labelText);

    // Delete button — refuses to delete the active project or `main`.
    if (proj.name !== "main") {
      const del = document.createElement("button");
      del.className = "sidebar-project-del";
      del.title = "Delete project";
      del.textContent = "×";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (proj.name === currentProject) {
          appendError(
            `can't delete the active project "${proj.name}". Switch to another project first.`
          );
          return;
        }
        if (!window.confirm(
          `Delete project "${proj.name}"?\n\n` +
          `This removes parallax/${proj.name}/ and every still, voiceover, ` +
          `draft, output, log, and chat transcript inside it. ` +
          `Raw media at the master dir is not touched.`
        )) return;
        try {
          const r = await fetch(`/api/projects/${encodeURIComponent(proj.name)}`, {
            method: "DELETE",
          });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            appendError(`failed to delete project: ${err.error || r.statusText}`);
            return;
          }
          refreshSidebar();
        } catch (err) {
          appendError(`failed to delete project: ${err}`);
        }
      });
      row.appendChild(del);
    }

    // Click the row → switch project, update URL, reload chat.
    row.addEventListener("click", async () => {
      if (proj.name === currentProject) return;
      const url = new URL(window.location.href);
      if (proj.name === "main") {
        url.searchParams.delete("project");
      } else {
        url.searchParams.set("project", proj.name);
      }
      window.history.pushState({}, "", url);
      refreshProjectBadge();
      // Drop the in-memory session ID so the next message opens a fresh
      // server-side session that hydrates from the new project's chat.jsonl.
      state.sessionId = null;
      state.thinking = false;
      state.dispatchActive = false;
      if (state.eventSource) {
        try { state.eventSource.close(); } catch (_) {}
        state.eventSource = null;
      }
      setStatus("");
      updateButtons();
      await loadProjectChat();
      refreshSidebar();
      refreshGallery();
    });

    list.appendChild(row);
  }
}

async function loadProjectChat() {
  // Fetch the current workspace's chat.jsonl and replay it. Called on
  // page load and on every sidebar project switch.
  state.thinking = false;
  state.dispatchActive = false;
  setStatus("");
  updateButtons();
  messagesEl().innerHTML = "";
  state.currentAssistantEl = null;

  const params = new URLSearchParams(window.location.search);
  const qs = new URLSearchParams();
  for (const k of ["user", "project"]) {
    const v = params.get(k);
    if (v) qs.set(k, v);
  }
  const url = "/api/chat" + (qs.toString() ? "?" + qs.toString() : "");
  let data = { turns: [] };
  try {
    const r = await fetch(url);
    if (r.ok) data = await r.json();
  } catch (e) {
    console.error("loadProjectChat failed", e);
  }

  const turns = data.turns || [];
  if (turns.length === 0) {
    // Fresh / empty project — show the welcome card.
    showWelcomeIfEmpty();
    return;
  }
  for (const t of turns) {
    if (t.role === "user") {
      appendUserMsg(t.text || "");
    } else if (t.role === "assistant") {
      appendAssistantDelta(t.text || "");
      finalizeAssistantMsg();
    }
  }
}

async function loadSessionHistory(sessionId) {
  // Reset active session state so the thinking bar and buttons don't carry over
  state.thinking = false;
  state.dispatchActive = false;
  setStatus("");
  updateButtons();
  try {
    const r = await fetch(`/api/session/${sessionId}/history`);
    if (!r.ok) throw new Error(r.statusText);
    const data = await r.json();
    const msgs = data.messages || [];

    // Clear current messages
    messagesEl().innerHTML = "";
    state.currentAssistantEl = null;

    // Replay each message using existing render functions
    for (const msg of msgs) {
      if (msg.role === "user") {
        appendUserMsg(msg.text || "");
      } else if (msg.role === "assistant") {
        appendAssistantDelta(msg.text || "");
        finalizeAssistantMsg();
      } else if (msg.role === "tool_use") {
        appendToolUse({ id: msg.tool_id, name: msg.name, input: msg.args || {} });
      } else if (msg.role === "tool_result") {
        appendToolResult({ id: msg.tool_id, summary: msg.summary });
      } else if (msg.role === "dispatch") {
        appendDispatchEvent({ phase: msg.phase, text: msg.text });
      } else if (msg.role === "error") {
        appendError(msg.text || "error");
      }
    }

    // Switch session and open stream so the user can continue
    state.sessionId = sessionId;
    openStream(sessionId);

    // Reset any status that got set during history replay — history is done,
    // nothing is actually running right now.
    state.dispatchActive = false;
    state.thinking = false;
    setStatus("");
    updateButtons();

    refreshSidebar();
  } catch (e) {
    appendError(`failed to load history: ${e}`);
  }
}

// ----- usage badge ---------------------------------------------------------

// Cost is logged server-side in SQLite (~/.parallax/usage.db) for audit
// and per-user/per-project attribution, but intentionally NOT shown in the
// frontend. The agents shouldn't see money and the user shouldn't have to
// watch a counter tick up while they work.
function refreshUsage() {}


function refreshProjectBadge() {
  // Read project from URL ?project= or cookie or default to "main"
  const params = new URLSearchParams(window.location.search);
  let project = params.get("project");
  if (!project) {
    const m = document.cookie.match(/parallax_project=([^;]+)/);
    if (m) project = decodeURIComponent(m[1]);
  }
  project = project || "main";
  const badge = $("project-badge");
  if (badge) {
    badge.textContent = project;
    badge.title = (
      `Workspace: ${project}\n\n` +
      `To open a parallel project, add ?project=<name> to the URL ` +
      `or open a new tab with a different project name. Each project has its ` +
      `own stills, manifest, audio, and outputs — jobs in different projects ` +
      `run completely in parallel.`
    );
  }

  // Forward ?user= / ?project= to the header links so opening /manifest
  // or /costs from the chat lands in the same workspace the user is in.
  const forwarded = new URLSearchParams();
  for (const k of ["user", "project"]) {
    const v = params.get(k);
    if (v) forwarded.set(k, v);
  }
  const suffix = forwarded.toString() ? "?" + forwarded.toString() : "";
  for (const id of ["manifest-link", "costs-link"]) {
    const el = document.getElementById(id);
    if (el) {
      const base = el.getAttribute("href").split("?")[0];
      el.href = base + suffix;
    }
  }
}

// ----- file uploads --------------------------------------------------------

async function uploadFile(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: form });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ error: r.statusText }));
      appendError(`upload failed: ${err.error || r.statusText}`);
      return;
    }
    const data = await r.json();
    // Auto-select the upload as a reference image. The user can still
    // deselect it from the gallery — this just removes the dead click step.
    if (data.path) {
      state.refImages.add(data.path);
      renderRefBar();
    }
    appendDispatchEvent({
      phase: "done",
      text: `uploaded ${data.path} (${(data.size / 1024).toFixed(1)} KB) — selected as reference`,
    });
    refreshGallery();
  } catch (e) {
    appendError(`upload failed: ${e}`);
  }
}

function initDropZone() {
  const gallery = $("gallery");
  let dragCounter = 0;

  // Page-wide: prevent the browser from navigating away if user misses the zone.
  document.addEventListener("dragover", (e) => {
    if (e.dataTransfer && e.dataTransfer.types.includes("Files")) e.preventDefault();
  });
  document.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.types.includes("Files")) e.preventDefault();
  });

  // Gallery-targeted dropzone with visual highlight.
  gallery.addEventListener("dragenter", (e) => {
    if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    dragCounter++;
    gallery.classList.add("dropzone-active");
  });
  gallery.addEventListener("dragleave", (e) => {
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      gallery.classList.remove("dropzone-active");
    }
  });
  gallery.addEventListener("dragover", (e) => {
    if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
  });
  gallery.addEventListener("drop", (e) => {
    e.preventDefault();
    dragCounter = 0;
    gallery.classList.remove("dropzone-active");
    if (!e.dataTransfer || !e.dataTransfer.files) return;
    for (const file of e.dataTransfer.files) {
      uploadFile(file);
    }
  });

  // Upload button + hidden file input
  const btn = $("upload-btn");
  const input = $("upload-input");
  if (btn && input) {
    btn.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      for (const file of input.files) uploadFile(file);
      input.value = "";
    });
  }
}

// ----- wiring --------------------------------------------------------------

function initComposer() {
  const form = $("composer");
  const input = $("composer-input");

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    input.focus();
    sendMessage(text);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  $("cancel-btn").addEventListener("click", () => cancel());
  $("new-session-btn").addEventListener("click", () => startNewSession());
  $("ref-bar-clear").addEventListener("click", () => {
    state.refImages.clear();
    updateRefBar();
    document.querySelectorAll(".thumb.ref-selected").forEach((t) => t.classList.remove("ref-selected"));
  });

  $("lightbox").addEventListener("click", closeLightbox);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });
}

async function startNewSession() {
  // + button creates a brand new project folder on disk and switches into
  // it. The new project starts empty so the welcome card is visible on
  // first load.
  const raw = window.prompt("Project name:", "");
  if (raw === null) return; // user cancelled

  const name = raw.trim().replace(/[^a-zA-Z0-9_-]/g, "-").replace(/^-+|-+$/g, "") || "main";

  // URL query the server wants for ?user=/?project=.
  const curParams = new URLSearchParams(window.location.search);
  const qs = new URLSearchParams({ project: name });
  const curUser = curParams.get("user");
  if (curUser) qs.set("user", curUser);

  // Create the folder on disk.
  try {
    const r = await fetch("/api/projects?" + qs.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      appendError(`failed to create project: ${err.error || r.statusText}`);
      return;
    }
  } catch (e) {
    appendError(`failed to create project: ${e}`);
    return;
  }

  // Update the URL so subsequent messages land in this project.
  const url = new URL(window.location.href);
  if (name === "main") {
    url.searchParams.delete("project");
  } else {
    url.searchParams.set("project", name);
  }
  window.history.pushState({}, "", url);
  refreshProjectBadge();

  // Clear the chat panel and drop the session ID — next send creates a fresh one.
  state.sessionId = null;
  state.thinking = false;
  state.dispatchActive = false;
  if (state.eventSource) {
    try { state.eventSource.close(); } catch (_) {}
    state.eventSource = null;
  }
  setStatus("");
  updateButtons();
  messagesEl().innerHTML = "";
  // Fresh project → show the welcome card in the new chat.
  showWelcomeIfEmpty();
  $("composer-input").focus();

  // Refresh sidebar so the new folder appears
  refreshSidebar();
  refreshGallery();
}

async function openProjectInFinder() {
  try {
    const r = await fetch("/api/open_in_finder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      appendError(`finder open failed: ${err.error || r.statusText}`);
    }
  } catch (e) {
    appendError(`finder open failed: ${e}`);
  }
}

function init() {
  initComposer();
  initDropZone();
  refreshProjectBadge();
  refreshGallery();
  refreshSidebar();
  refreshUsage();
  // Load this project's on-disk chat transcript on page load so reload
  // keeps the conversation. Empty transcript → welcome card.
  loadProjectChat();
  // Wire the new Finder button in the header.
  const finderBtn = document.getElementById("finder-btn");
  if (finderBtn) finderBtn.addEventListener("click", openProjectInFinder);
  setInterval(() => {
    if (!state.thinking && !state.dispatchActive) refreshGallery();
  }, 2000);
  setInterval(refreshSidebar, 5000);
  setInterval(refreshUsage, 30000);
}

init();
