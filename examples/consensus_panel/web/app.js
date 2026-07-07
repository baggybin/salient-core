// Consensus-panel UI: pick models, fan one question across them, stream each
// leg's trace into its own pane, allow per-pane abort, then show the semantic
// convergence meter + judge verdict. Vanilla JS + EventSource (SSE).

const SWATCHES = ["#4a9eff", "#a371f7", "#39c5cf", "#3fb950", "#d29922", "#f85149"];
let MODELS = [];
let legSeq = 0;
let currentRun = null; // { id, source, legs: {legId: {el, status}} }

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

async function loadModels() {
  try {
    const r = await fetch("/api/models");
    MODELS = (await r.json()).models || [];
  } catch {
    MODELS = [];
  }
  if (!MODELS.length) MODELS = [{ id: "claude-opus-4-8", display_name: "Claude Opus 4.8" }];
  // Seed a 3-leg panel (or as many distinct models as we have).
  $("#legs").innerHTML = "";
  const seed = Math.min(3, Math.max(2, MODELS.length));
  for (let i = 0; i < seed; i++) addLeg(MODELS[i % MODELS.length].id);
  fillJudge();
}

function modelOptions(selectedId) {
  return MODELS.map(
    (m) =>
      `<option value="${escapeHtml(m.id)}" ${m.id === selectedId ? "selected" : ""}>${escapeHtml(m.display_name)}</option>`
  ).join("");
}

function addLeg(selectedId) {
  const id = `leg${legSeq++}`;
  const swatch = SWATCHES[(legSeq - 1) % SWATCHES.length];
  const row = el("div", "leg-pick");
  row.dataset.leg = id;
  row.innerHTML = `<span class="swatch" style="background:${swatch}"></span>
    <select>${modelOptions(selectedId || MODELS[0].id)}</select>
    <button class="rm" title="remove" type="button">×</button>`;
  row.dataset.swatch = swatch;
  row.querySelector(".rm").onclick = () => {
    if ($("#legs").children.length > 2) row.remove();
  };
  $("#legs").appendChild(row);
}

function fillJudge() {
  $("#judge-model").innerHTML = modelOptions(MODELS[0].id);
}

function panelFromUI() {
  return [...$("#legs").children].map((row) => ({
    id: row.dataset.leg,
    model: row.querySelector("select").value,
    swatch: row.dataset.swatch,
  }));
}

async function run() {
  const question = $("#question").value.trim();
  const panel = panelFromUI();
  if (!question || panel.length < 2) return;

  $("#run").disabled = true;
  $("#convergence").classList.add("hidden");
  buildPanes(panel);

  let res;
  try {
    res = await fetch("/api/consensus", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, panel, judge_model: $("#judge-model").value }),
    }).then((r) => r.json());
  } catch (e) {
    $("#run").disabled = false;
    return;
  }
  if (res.error) {
    $("#run").disabled = false;
    return;
  }

  const source = new EventSource(`/api/consensus/${res.run_id}/events`);
  currentRun = { id: res.run_id, source, legs: {} };
  panel.forEach((p) => (currentRun.legs[p.id] = { swatch: p.swatch }));

  source.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
  source.addEventListener("end", () => {
    source.close();
    $("#run").disabled = false;
  });
}

function buildPanes(panel) {
  const panes = $("#panes");
  panes.innerHTML = "";
  panel.forEach((p) => {
    const model = MODELS.find((m) => m.id === p.model);
    const pane = el("div", "pane");
    pane.dataset.leg = p.id;
    pane.innerHTML = `
      <div class="pane-head">
        <div class="model"><span class="swatch" style="background:${escapeHtml(p.swatch)}"></span>${escapeHtml(
          model ? model.display_name : p.model
        )}</div>
        <div class="status running"><span class="pulse"></span>waiting…</div>
      </div>
      <div class="trace"></div>
      <div class="pane-foot"><button class="abort" type="button">Abort this model</button></div>`;
    pane.querySelector(".abort").onclick = () => abortLeg(p.id);
    panes.appendChild(pane);
  });
}

function paneFor(leg) {
  return $(`.pane[data-leg="${leg}"]`);
}

function setStatus(leg, cls, text) {
  const s = paneFor(leg)?.querySelector(".status");
  if (!s) return;
  s.className = `status ${cls}`;
  s.innerHTML = cls === "running" ? `<span class="pulse"></span>${text}` : text;
}

function handleEvent(e) {
  switch (e.kind) {
    case "leg_start":
      setStatus(e.leg, "running", "thinking…");
      break;
    case "trace": {
      const trace = paneFor(e.leg)?.querySelector(".trace");
      if (!trace) break;
      const step = el("div", "step");
      const tool = e.data.tool ? ` <span class="tool-name">${escapeHtml(e.data.tool)}</span>` : "";
      const kind = escapeHtml(e.data.step);
      step.innerHTML = `<span class="k ${kind}">${kind.replace("_", " ")}</span>
        <span class="txt">${tool}${escapeHtml(e.data.text)}</span>`;
      trace.appendChild(step);
      break;
    }
    case "answer": {
      const pane = paneFor(e.leg);
      if (!pane) break;
      const ans = el("div", "answer");
      ans.textContent = e.data.text;
      pane.querySelector(".trace").after(ans);
      setStatus(e.leg, "done", "✓ answered");
      disableAbort(e.leg);
      break;
    }
    case "aborted":
      setStatus(e.leg, "aborted", "⊘ aborted");
      disableAbort(e.leg);
      break;
    case "convergence":
      showConvergence(e.data);
      break;
    case "error":
      console.error("run error:", e.data.message);
      break;
  }
}

function disableAbort(leg) {
  const b = paneFor(leg)?.querySelector(".abort");
  if (b) b.disabled = true;
}

async function abortLeg(leg) {
  if (!currentRun) return;
  disableAbort(leg);
  try {
    await fetch(`/api/consensus/${currentRun.id}/abort`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ leg }),
    });
  } catch {
    /* leg finishes on its own if the abort didn't land */
  }
}

function showConvergence(d) {
  $("#convergence").classList.remove("hidden");
  const pct = d.semantic_score == null ? 0 : Math.round(d.semantic_score * 100);
  $("#score").textContent = d.semantic_score == null ? "n/a" : `${pct}%`;
  $("#meter-fill").style.width = `${pct}%`;
  const verdict = d.judge || "No judge verdict (fewer than two answers).";
  $("#verdict").textContent = verdict;
  const tag = $("#judge-tag");
  if (/^AGREE/.test(verdict)) {
    tag.className = "tag agree";
    tag.textContent = "converged";
  } else if (/^DIVERGE/.test(verdict)) {
    tag.className = "tag diverge";
    tag.textContent = "diverged";
  } else {
    tag.className = "tag";
    tag.textContent = "";
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

$("#add-leg").onclick = () => addLeg();
$("#run").onclick = run;
$("#mode").onchange = (e) => $("#composer").classList.toggle("hidden", !e.target.checked);

loadModels();
