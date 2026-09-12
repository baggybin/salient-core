"use strict";

const $ = (id) => document.getElementById(id);

function showError(msg) {
  const el = $("error");
  el.textContent = msg;
  el.hidden = !msg;
}

async function loadCatalog() {
  const wrap = $("families");
  try {
    const res = await fetch("/api/catalog");
    const data = await res.json();
    wrap.innerHTML = "";
    for (const fam of data.families) {
      const row = document.createElement("label");
      row.className = "fam" + (fam.available ? "" : " off");

      const check = document.createElement("input");
      check.type = "checkbox";
      check.dataset.brain = fam.brain;
      check.disabled = !fam.available;

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = fam.brain;

      const right = document.createElement("span");
      if (fam.available) {
        const select = document.createElement("select");
        select.dataset.brain = fam.brain;
        const def = document.createElement("option");
        def.value = "";
        def.textContent = "default model";
        select.appendChild(def);
        for (const m of fam.models) {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = m.label || m.id;
          select.appendChild(opt);
        }
        right.appendChild(select);
      } else {
        right.className = "tag";
        right.textContent = "no API key set";
      }

      row.append(check, name, right);
      wrap.appendChild(row);
    }
  } catch (e) {
    wrap.textContent = "Could not load models: " + e;
  }
}

function collectSeats() {
  const seats = [];
  for (const check of document.querySelectorAll('.fam input[type="checkbox"]')) {
    if (!check.checked) continue;
    const brain = check.dataset.brain;
    const select = document.querySelector(`select[data-brain="${brain}"]`);
    seats.push({ brain, model: select ? select.value : "" });
  }
  return seats;
}

function renderResult(data) {
  const result = $("result");
  result.hidden = false;

  const conv = data.converged
    ? '<span class="ok">converged</span>'
    : '<span class="no">hit round cap</span>';
  $("summary").innerHTML =
    `Chair: <b>${data.chair}</b> · ${data.rounds} round(s) · ${conv} · score ` +
    `${data.score == null ? "n/a" : Number(data.score).toFixed(2)}`;

  const board = $("leaderboard");
  board.innerHTML = "";
  for (const idea of data.ideas) {
    const li = document.createElement("li");
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = idea.support;
    const label = document.createElement("span");
    label.textContent = idea.label;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = "(" + idea.supporters.join(", ") + ")";
    li.append(count, label, who);
    board.appendChild(li);
  }

  $("map").textContent = data.map_text || "(no summary)";

  const tr = $("transcript");
  tr.innerHTML = "";
  for (const post of data.transcript) {
    const div = document.createElement("div");
    div.className = "post";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `#${post.seq} · round ${post.round} · ${post.agent}`;
    const body = document.createElement("div");
    body.textContent = post.text;
    div.append(meta, body);
    tr.appendChild(div);
  }
}

async function run() {
  showError("");
  const topic = $("topic").value.trim();
  if (!topic) return showError("Enter a topic.");
  const seats = collectSeats();
  if (seats.length < 2) return showError("Pick at least two debaters.");

  const btn = $("run");
  btn.disabled = true;
  btn.textContent = "Running… (models are arguing)";
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        topic,
        seats,
        rounds: Number($("rounds").value) || 6,
        threshold: Number($("threshold").value) || 0.85,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.error + (data.skipped ? " — " + data.skipped.join("; ") : ""));
    } else {
      renderResult(data);
    }
  } catch (e) {
    showError("Request failed: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run conference";
  }
}

$("run").addEventListener("click", run);
loadCatalog();
