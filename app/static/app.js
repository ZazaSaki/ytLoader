(() => {
  const $ = (s) => document.querySelector(s);
  const jobs = new Map();
  const ACTIVE = new Set(["queued", "downloading", "processing"]);
  let formatLabels = {};

  // ---- API helpers -------------------------------------------------------
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return r.json();
  }

  // ---- Rendering ---------------------------------------------------------
  function encodePath(p) {
    return p.split("/").map(encodeURIComponent).join("/");
  }

  function render() {
    const active = [], history = [];
    for (const j of jobs.values()) (ACTIVE.has(j.status) ? active : history).push(j);
    active.sort((a, b) => a.created_at - b.created_at);
    history.sort((a, b) => (b.finished_at || b.created_at) - (a.finished_at || a.created_at));
    renderList($("#active"), active);
    renderList($("#history"), history);
    $("#active-empty").hidden = active.length > 0;
    $("#history-empty").hidden = history.length > 0;
    $("#active-count").textContent = active.length ? `(${active.length})` : "";
    $("#history-count").textContent = history.length ? `(${history.length})` : "";
    $("#clear").disabled = history.length === 0;
    renderBundles(history);
    document.title = active.length ? `(${active.length}) Video Downloader` : "Video Downloader";
  }

  // "Download all" + one ZIP link per playlist, counting only finished files
  function renderBundles(history) {
    const done = history.filter((j) => j.status === "done" && j.filename);
    const saveAll = $("#save-all");
    saveAll.classList.toggle("disabled", done.length === 0);
    saveAll.textContent = done.length ? `⬇ Download all (${done.length})` : "⬇ Download all";
    saveAll.title = done.length ? "Save every finished file as one ZIP" : "Nothing finished yet";

    const playlists = new Map();
    for (const j of done) {
      if (j.playlist_title) playlists.set(j.playlist_title, (playlists.get(j.playlist_title) || 0) + 1);
    }
    const box = $("#bundles");
    const key = JSON.stringify([...playlists].sort());
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.replaceChildren();
    for (const [title, n] of [...playlists].sort((a, b) => a[0].localeCompare(b[0]))) {
      const a = document.createElement("a");
      a.href = "/api/archive?playlist=" + encodeURIComponent(title);
      a.download = "";
      a.textContent = `📃 ${title} · ${n} file${n === 1 ? "" : "s"} ⬇`;
      a.title = "Download this playlist as a ZIP";
      box.append(a);
    }
    box.hidden = playlists.size === 0;
  }

  function renderList(container, list) {
    const seen = new Set();
    list.forEach((job, i) => {
      seen.add(job.id);
      let el = container.querySelector(`[data-id="${job.id}"]`);
      if (!el) {
        el = $("#job-tpl").content.firstElementChild.cloneNode(true);
        el.dataset.id = job.id;
      }
      fill(el, job);
      if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
    });
    for (const el of [...container.children]) if (!seen.has(el.dataset.id)) el.remove();
  }

  function fill(el, j) {
    el.className = "job st-" + j.status;
    const img = el.querySelector("img");
    if (img.getAttribute("src") !== (j.thumbnail || "")) img.src = j.thumbnail || "";
    el.querySelector(".title").textContent = j.title || j.url;
    el.querySelector(".title").title = j.url;
    const st = el.querySelector(".status");
    st.textContent = j.status;
    st.className = "badge status " + j.status;
    el.querySelector(".fmt").textContent = formatLabels[j.format_key] || j.format_key;
    const pl = el.querySelector(".playlist");
    pl.hidden = !j.playlist_title;
    pl.textContent = j.playlist_title ? "📃 " + j.playlist_title : "";

    const stats = [];
    if (j.status === "downloading") {
      stats.push(`${j.progress.toFixed(1)}%`);
      if (j.speed) stats.push(j.speed);
      if (j.eta) stats.push(`ETA ${j.eta}`);
    } else if (j.status === "processing") stats.push("converting with ffmpeg…");
    else if (j.status === "done" && j.filename) stats.push(j.filename);
    el.querySelector(".stats").textContent = stats.join(" · ");

    const bar = el.querySelector(".bar");
    bar.hidden = !(j.status === "downloading" || j.status === "processing");
    el.querySelector(".fill").style.width = (j.status === "processing" ? 100 : j.progress) + "%";

    const err = el.querySelector(".errmsg");
    err.hidden = !j.error;
    err.textContent = j.error || "";

    const actions = el.querySelector(".actions");
    actions.replaceChildren();
    if (ACTIVE.has(j.status)) {
      if (j.status !== "processing") {
        actions.append(btn("Cancel", "danger", () => api(`/api/downloads/${j.id}/cancel`, { method: "POST" })));
      }
    } else {
      if (j.status === "done" && j.filename) {
        const a = document.createElement("a");
        a.href = "/files/" + encodePath(j.filename);
        a.textContent = "Open file";
        a.target = "_blank";
        actions.append(a);
        const d = a.cloneNode(true);
        d.textContent = "Save";
        d.download = j.filename.split("/").pop();
        d.removeAttribute("target");
        actions.append(d);
      }
      if (j.status !== "done") {
        actions.append(btn("Retry", "", () => api(`/api/downloads/${j.id}/retry`, { method: "POST" })));
      }
      actions.append(btn("Remove", "ghost", () => api(`/api/downloads/${j.id}`, { method: "DELETE" })));
      if (j.status === "done" && j.filename) {
        actions.append(btn("Delete file", "ghost danger", () => {
          if (confirm("Delete the downloaded file from disk?")) api(`/api/downloads/${j.id}?delete_file=true`, { method: "DELETE" });
        }));
      }
    }
  }

  function btn(label, cls, onClick) {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = cls;
    b.onclick = () => onClick().catch((e) => showMsg(e.message, true));
    return b;
  }

  function showMsg(text, isErr = false) {
    const m = $("#msg");
    m.textContent = text;
    m.className = "msg" + (isErr ? " err" : "");
    m.hidden = !text;
  }

  // ---- Events ------------------------------------------------------------
  function connect() {
    const es = new EventSource("/api/events");
    es.onopen = () => $("#conn").classList.add("on");
    es.onerror = () => $("#conn").classList.remove("on");
    es.addEventListener("snapshot", (e) => {
      jobs.clear();
      for (const j of JSON.parse(e.data)) jobs.set(j.id, j);
      render();
    });
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "job") jobs.set(ev.job.id, ev.job);
      else if (ev.type === "removed") jobs.delete(ev.id);
      render();
    };
  }

  async function submit() {
    const urls = $("#urls").value.trim();
    if (!urls) return;
    const btn = $("#submit");
    btn.disabled = true;
    btn.textContent = "Adding…";
    showMsg("Resolving links (playlists may take a moment)…");
    try {
      const created = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({ urls, format: $("#format").value }),
      });
      $("#urls").value = "";
      showMsg(`Added ${created.length} item${created.length === 1 ? "" : "s"} to the queue.`);
      setTimeout(() => showMsg(""), 4000);
    } catch (e) {
      showMsg(e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Download";
    }
  }

  // ---- Init --------------------------------------------------------------
  async function init() {
    const formats = await api("/api/formats");
    const sel = $("#format");
    for (const f of formats) {
      formatLabels[f.key] = f.label;
      const o = document.createElement("option");
      o.value = f.key;
      o.textContent = f.label;
      sel.append(o);
    }
    try { const saved = localStorage.getItem("format"); if (saved && formatLabels[saved]) sel.value = saved; } catch {}
    sel.onchange = () => { try { localStorage.setItem("format", sel.value); } catch {} };

    $("#submit").onclick = submit;
    $("#urls").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
    });
    $("#clear").onclick = () => api("/api/history", { method: "DELETE" }).catch((e) => showMsg(e.message, true));

    connect();
  }

  init();
})();
