// timeline.js — scene card renderer for the Timeline tab
// Depends on: apiUrl() and escapeHtml() from app.js (loads after app.js)

let selectedTimelineScene = 0;

function sceneDescription(scene) {
  return (
    scene.description ||
    scene.starting_frame ||
    scene.image_prompt ||
    scene.prompt ||
    scene.title ||
    ""
  ).trim();
}

function renderTimelineDetail(scene, index) {
  const detail = document.getElementById("timeline-detail");
  if (!detail || !scene) return;

  const number = String(scene.number ?? index + 1);
  const duration = scene.duration != null ? `${Number(scene.duration).toFixed(1)}s` : "—";
  const motion = scene.motion ? ` · ${escapeHtml(scene.motion)}` : "";
  const vo = (scene.vo_text || "").trim() || "No voiceover text set.";
  const desc = sceneDescription(scene) || "No scene description set.";

  detail.classList.remove("empty");
  detail.innerHTML = `
    <div class="timeline-detail-head">
      <div class="timeline-detail-title">Scene ${escapeHtml(number)}</div>
      <div class="timeline-detail-meta">${escapeHtml(duration)}${motion}</div>
    </div>
    <div class="timeline-detail-block">
      <div class="timeline-detail-label">Voiceover</div>
      <div class="timeline-detail-body">${escapeHtml(vo)}</div>
    </div>
    <div class="timeline-detail-block">
      <div class="timeline-detail-label">Scene Description</div>
      <div class="timeline-detail-body">${escapeHtml(desc)}</div>
    </div>
  `;
}

async function refreshTimeline() {
  const container = document.getElementById("timeline-scenes");
  const detail = document.getElementById("timeline-detail");
  if (!container) return;

  let data;
  try {
    const r = await fetch(apiUrl("/api/manifest"));
    if (!r.ok) return;
    data = await r.json();
  } catch (_) {
    return;
  }

  if (!data.exists || !data.scenes || data.scenes.length === 0) {
    container.innerHTML = '<div class="timeline-empty">No scenes yet — brief the Head of Production to build the manifest.</div>';
    if (detail) {
      detail.classList.add("empty");
      detail.textContent = "Click a scene card to inspect voiceover and scene description.";
    }
    return;
  }

  // Build cumulative start times for click-to-seek
  const scenes = data.scenes;
  let t = 0;
  const startTimes = scenes.map((s) => {
    const ts = t;
    t += Number(s.duration) || 0;
    return ts;
  });

  const qs = (() => {
    const p = new URLSearchParams(window.location.search);
    const out = new URLSearchParams();
    for (const k of ["user", "project"]) {
      const v = p.get(k);
      if (v) out.set(k, v);
    }
    return out.toString();
  })();

  container.innerHTML = "";
  selectedTimelineScene = Math.max(0, Math.min(selectedTimelineScene, scenes.length - 1));

  scenes.forEach((s, i) => {
    const card = document.createElement("div");
    card.className = "scene-card";
    if (i === selectedTimelineScene) card.classList.add("active");

    const stillSrc = s.still_url ? (s.still_url + (qs ? "?" + qs : "")) : null;
    const thumb = stillSrc
      ? `<img class="scene-card-thumb" src="${escapeHtml(stillSrc)}" alt="scene ${s.number}" loading="lazy" />`
      : `<div class="scene-card-thumb scene-card-nothumb">no still</div>`;

    const dur = s.duration != null ? `${Number(s.duration).toFixed(1)}s` : "—";
    const motion = s.motion ? ` · ${escapeHtml(s.motion)}` : "";
    const vo = (s.vo_text || "").trim();
    const voHtml = vo
      ? `<div class="scene-card-vo">${escapeHtml(vo.slice(0, 100))}${vo.length > 100 ? "…" : ""}</div>`
      : "";

    card.innerHTML = `
      ${thumb}
      <div class="scene-card-body">
        <div class="scene-card-num">SCENE ${escapeHtml(String(s.number ?? i + 1))}</div>
        <div class="scene-card-meta">${escapeHtml(dur)}${motion}</div>
        ${voHtml}
      </div>`;

    card.title = `Click to seek to scene ${s.number ?? i + 1} (${dur})`;
    card.addEventListener("click", () => {
      selectedTimelineScene = i;
      container.querySelectorAll(".scene-card").forEach((el) => el.classList.remove("active"));
      card.classList.add("active");
      renderTimelineDetail(s, i);

      const video = document.querySelector("#player-wrap video");
      if (video) {
        video.currentTime = startTimes[i];
        video.play().catch(() => {});
      }
    });

    container.appendChild(card);
  });

  renderTimelineDetail(scenes[selectedTimelineScene], selectedTimelineScene);
}
