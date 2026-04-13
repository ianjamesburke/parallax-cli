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

function appendDispatchEvent(ev) {
  const el = document.createElement("div");
  el.className = "dispatch-strip";
  if (ev.phase === "done") el.classList.add("done");
  if (ev.phase === "error") el.classList.add("error");

  // Clean label: no [parallax] prefix, and a single leading dot character
  // for the active state, a check for done, X for error.
  let marker = "·";
  if (ev.phase === "done") marker = "✓";
  if (ev.phase === "error") marker = "!";

  const markerEl = document.createElement("span");
  markerEl.className = "dispatch-marker";
  markerEl.textContent = marker;
  el.appendChild(markerEl);

  const textEl = document.createElement("span");
  textEl.className = "dispatch-text";
  textEl.textContent = ev.text || ev.phase || "";
  el.appendChild(textEl);

  messagesEl().appendChild(el);
  scrollToBottom();
  if (ev.phase === "done" || ev.phase === "error") {
    state.dispatchActive = false;
    refreshGallery();
  } else {
    state.dispatchActive = true;
    setStatus("dispatching");
  }
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
    setStatus("thinking");
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
    if (!state.dispatchActive) setStatus("");
    state.thinking = false;
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

  try {
    const r = await fetch("/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, text }),
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
        del.textContent = "🗑";
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
        thumb.addEventListener("click", () => openLightbox(img.src));
        thumb.addEventListener("dblclick", () => openInFinder(s.path));
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
      item.addEventListener("click", () => {
        state.activeVideo = v.path;
        renderGallery({ project_dir: data.project_dir, stills, videos });
      });
      item.addEventListener("dblclick", () => openInFinder(v.path));
      vList.appendChild(item);
    }
  }
}

async function openInFinder(path) {
  try {
    await fetch("/api/open_in_finder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path || "" }),
    });
  } catch (e) {
    console.error("open_in_finder failed", e);
  }
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

// ----- history drawer ------------------------------------------------------

function relativeTime(ts) {
  const diff = Math.floor((Date.now() / 1000) - ts);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 172800) return "yesterday";
  return `${Math.floor(diff / 86400)}d ago`;
}

async function toggleHistoryDrawer() {
  const drawer = $("history-drawer");
  if (!drawer.classList.contains("hidden")) {
    drawer.classList.add("hidden");
    return;
  }
  drawer.classList.remove("hidden");
  await loadHistoryList();
}

async function loadHistoryList() {
  const list = $("history-list");
  list.innerHTML = '<div class="history-empty">Loading...</div>';
  try {
    const r = await fetch("/api/sessions");
    if (!r.ok) throw new Error(r.statusText);
    const data = await r.json();
    const sessions = data.sessions || [];
    if (sessions.length === 0) {
      list.innerHTML = '<div class="history-empty">No previous sessions.</div>';
      return;
    }
    list.innerHTML = "";
    for (const s of sessions) {
      const item = document.createElement("div");
      item.className = "history-item";
      item.innerHTML = `
        <div class="history-item-meta">${relativeTime(s.last_activity_at)} · ${s.event_count} events</div>
        <div class="history-item-preview">${escapeHtml(s.preview || "(no messages)")}</div>
      `;
      item.addEventListener("click", () => loadSessionHistory(s.id));
      list.appendChild(item);
    }
  } catch (e) {
    list.innerHTML = `<div class="history-empty">Error: ${escapeHtml(String(e))}</div>`;
  }
}

async function loadSessionHistory(sessionId) {
  $("history-drawer").classList.add("hidden");
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

function openNewProject() {
  const raw = window.prompt("New project name? (letters, numbers, hyphens, underscores)");
  if (!raw) return;
  const name = raw.trim().replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 32);
  if (!name) {
    alert("Invalid project name — use letters, numbers, hyphens, or underscores.");
    return;
  }
  // Preserve current user if present in URL or cookie
  const params = new URLSearchParams(window.location.search);
  let user = params.get("user");
  if (!user) {
    const m = document.cookie.match(/parallax_user=([^;]+)/);
    if (m) user = decodeURIComponent(m[1]);
  }
  const query = new URLSearchParams();
  query.set("project", name);
  if (user) query.set("user", user);
  const url = `${window.location.origin}/?${query.toString()}`;
  window.open(url, "_blank", "noopener");
}

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
    // Surface as a system-style message in the chat
    appendDispatchEvent({
      phase: "done",
      text: `uploaded ${data.path} (${(data.size / 1024).toFixed(1)} KB)`,
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
  $("open-finder-btn").addEventListener("click", () => openInFinder(""));
  $("history-btn").addEventListener("click", () => toggleHistoryDrawer());
  $("new-project-btn").addEventListener("click", () => openNewProject());

  $("lightbox").addEventListener("click", closeLightbox);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });
}

function init() {
  initComposer();
  initDropZone();
  refreshProjectBadge();
  refreshGallery();
  refreshUsage();
  setInterval(() => {
    if (!state.thinking && !state.dispatchActive) refreshGallery();
  }, 2000);
  setInterval(refreshUsage, 30000);
}

init();
