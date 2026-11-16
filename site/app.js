/* Morgen-Briefing – App-Logik (ohne Fremdbibliotheken) */
(function () {
  "use strict";

  // ------------------------------------------------------------------ Zustand
  const LS = {
    get(k, d) { try { const v = localStorage.getItem("mb_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("mb_" + k, JSON.stringify(v)); } catch (e) { /* voll / privat */ } },
  };
  const S = {
    view: "briefing",
    news: null, markets: null, briefing: null, status: null,
    cat: LS.get("cat", "all"),
    sort: LS.get("sort", "top"),
    q: "",
    open: new Set(),
    read: new Set(LS.get("read", [])),
    saved: LS.get("saved", {}),
    hiddenCats: new Set(LS.get("hiddenCats", [])),
    lastVisit: LS.get("lastVisit", null),
    prevVisit: LS.get("lastVisit", null),
  };
  const $ = (s, r = document) => r.querySelector(s);
  const main = $("#main");

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");
  const nf = (v, d = 2) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + nf(v, 2) + " %");

  function ago(iso) {
    if (!iso) return "";
    const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (m < 1) return "gerade eben";
    if (m < 60) return "vor " + m + " Min.";
    const h = Math.round(m / 60);
    if (h < 24) return "vor " + h + " Std.";
    const d = new Date(iso);
    return d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" }) + ", " + d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  }
  function clock(iso) { return new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }); }
  function dayLabel(iso) {
    const d = new Date(iso), t = new Date();
    const y = new Date(); y.setDate(t.getDate() - 1);
    if (d.toDateString() === t.toDateString()) return "Heute";
    if (d.toDateString() === y.toDateString()) return "Gestern";
    return d.toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" });
  }
  function toast(msg, ms = 2600) {
    const t = $("#toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
  }
  const catName = (id) => ((S.news && S.news.categories) || []).find((c) => c.id === id)?.name || id;

  // ------------------------------------------------------------------ Einstellungen anwenden
  function applyPrefs() {
    const r = document.documentElement;
    r.dataset.theme = LS.get("theme", "auto");
    r.dataset.size = LS.get("size", "m");
  }
  applyPrefs();

  // ------------------------------------------------------------------ Daten laden
  async function getJSON(name) {
    const r = await fetch("data/" + name + "?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(name + ": HTTP " + r.status);
    return r.json();
  }
  async function loadAll(silent) {
    const btn = $("#refreshBtn");
    btn.classList.add("spin");
    try {
      const [n, m, b, s] = await Promise.allSettled([getJSON("news.json"), getJSON("markets.json"), getJSON("briefing.json"), getJSON("status.json")]);
      if (n.status === "fulfilled") S.news = n.value;
      if (m.status === "fulfilled") S.markets = m.value;
      if (b.status === "fulfilled") S.briefing = b.value;
      if (s.status === "fulfilled") S.status = s.value;
      if (n.status !== "fulfilled" && !silent) toast("Keine Verbindung – zeige gespeicherten Stand.");
    } finally {
      btn.classList.remove("spin");
    }
    updateStamp();
    render();
  }
  function updateStamp() {
    const u = S.news && S.news.updated;
    $("#stamp").textContent = u ? "Stand: " + ago(u) + " · " + clock(u) + " Uhr" : "Noch keine Daten";
  }
  setInterval(updateStamp, 60000);

  // ------------------------------------------------------------------ Aktualisieren
  function repoInfo() {
    const manual = LS.get("repo", "");
    if (manual && manual.includes("/")) return manual.trim();
    const h = location.hostname, p = location.pathname.split("/").filter(Boolean);
    if (h.endsWith(".github.io")) return h.split(".")[0] + "/" + (p[0] || h);
    return "";
  }
  async function triggerRemote() {
    const token = LS.get("token", ""), repo = repoInfo();
    if (!token || !repo) return false;
    const r = await fetch("https://api.github.com/repos/" + repo + "/actions/workflows/update.yml/dispatches", {
      method: "POST",
      headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
      body: JSON.stringify({ ref: LS.get("branch", "main") }),
    });
    if (r.status !== 204) {
      let t = ""; try { t = (await r.json()).message; } catch (e) { /* */ }
      throw new Error("GitHub antwortet " + r.status + (t ? ": " + t : ""));
    }
    return true;
  }
  async function refresh() {
    const before = S.news && S.news.updated;
    let remote = false;
    try { remote = await triggerRemote(); } catch (e) { toast(e.message, 5000); }
    if (!remote) {
      await loadAll();
      if (!LS.get("token", "")) toast("Neuester Stand geladen. Die Quellen werden automatisch alle 30 Min. abgerufen.");
      else toast("Neuester Stand geladen.");
      return;
    }
    const pr = $("#progress"), pt = $("#progressText");
    pr.hidden = false;
    const t0 = Date.now();
    const tick = () => { pt.textContent = "Neue Nachrichten werden abgerufen und ausgewertet … " + Math.round((Date.now() - t0) / 1000) + " s"; };
    tick();
    const iv = setInterval(tick, 1000);
    try {
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 15000));
        try {
          const s = await getJSON("status.json");
          if (s.updated && s.updated !== before && new Date(s.updated) > new Date(t0 - 60000)) { await loadAll(true); toast("Aktualisiert ✓"); return; }
        } catch (e) { /* weiter warten */ }
      }
      toast("Der Abruf dauert ungewöhnlich lange – bitte später erneut öffnen.", 5000);
    } finally { clearInterval(iv); pr.hidden = true; }
  }
  $("#refreshBtn").addEventListener("click", refresh);

  // ------------------------------------------------------------------ Navigation
  const titles = { briefing: "Briefing", news: "Nachrichten", markets: "Märkte", saved: "Merkliste", settings: "Mehr" };
  $("#tabbar").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]"); if (!b) return;
    S.view = b.dataset.view;
    document.querySelectorAll("#tabbar button").forEach((x) => x.classList.toggle("on", x === b));
    render(); window.scrollTo({ top: 0 });
  });

  function render() {
    $("#viewTitle").textContent = S.view === "briefing" ? greeting() : titles[S.view];
    ({ briefing: viewBriefing, news: viewNews, markets: viewMarkets, saved: viewSaved, settings: viewSettings }[S.view])();
  }
  function greeting() {
    const h = new Date().getHours();
    return h < 11 ? "Guten Morgen" : h < 17 ? "Dein Briefing" : "Guten Abend";
  }

  // ------------------------------------------------------------------ Artikel-Karte
  function items() { return (S.news && S.news.items) || []; }
  function visible(it) { return (it.cats || [it.cat]).some((c) => !S.hiddenCats.has(c)); }
  function byId(id) { return items().find((x) => x.id === id) || S.saved[id]; }

  function card(it) {
    const ai = it.ai, au = it.auto || {};
    const mood = (ai && ai.stimmung) || au.mood || "neutral";
    const isRead = S.read.has(it.id), isOpen = S.open.has(it.id);
    const short = ai ? ai.zusammenfassung : it.teaser || (au.keypoints || [])[0] || "";
    const tags = (ai && ai.themen && ai.themen.length ? ai.themen : au.tags || []).slice(0, 4);
    const isNew = S.prevVisit && new Date(it.published) > new Date(S.prevVisit) && !isRead;
    let h = `<article class="art${isRead ? " read" : ""}${isOpen ? " open" : ""}" data-id="${esc(it.id)}">
      <button class="art-head" data-act="toggle">
        <div class="meta">${isNew ? '<span class="dot" title="neu"></span>' : ""}<span class="src">${esc(it.source)}</span>
          <span>· ${esc(ago(it.published))}</span><span>· ${esc(catName((it.cats || [it.cat])[0]))}</span>
          <span class="mood ${esc(mood)}" title="Tendenz: ${esc(mood)}"></span>${ai ? '<span class="badge ai" style="padding:1px 6px">KI</span>' : ""}
          ${it.also && it.also.length ? `<span>· +${it.also.length} Quelle${it.also.length > 1 ? "n" : ""}</span>` : ""}</div>
        <h3>${esc(it.title)}</h3>
        ${short ? `<p class="sum">${esc(short)}</p>` : ""}
        ${tags.length ? `<div class="tags">${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
      </button>`;
    if (isOpen) h += `<div class="art-body">${detail(it)}</div>`;
    return h + "</article>";
  }

  function detail(it) {
    const ai = it.ai, au = it.auto || {};
    let h = "";
    if (ai) {
      h += `<div class="box ai"><h4><span>KI-Auswertung</span><span style="text-transform:none;letter-spacing:0;font-weight:500">Basis: ${esc(ai.basis || "Anriss")} · Relevanz ${"●".repeat(ai.relevanz || 3)}${"○".repeat(5 - (ai.relevanz || 3))}</span></h4>
        ${ai.zusammenfassung ? `<p>${esc(ai.zusammenfassung)}</p>` : ""}
        ${ai.kernpunkte && ai.kernpunkte.length ? `<ul>${ai.kernpunkte.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}
        ${ai.einordnung ? `<p class="einord"><b>Einordnung:</b> ${esc(ai.einordnung)}</p>` : ""}</div>`;
    }
    const hasAuto = (au.keypoints && au.keypoints.length) || (au.figures && au.figures.length) || (au.tags && au.tags.length);
    if (hasAuto) {
      h += `<div class="box auto"><h4><span>Automatische Auswertung</span><span style="text-transform:none;letter-spacing:0;font-weight:500">Tendenz: ${esc(au.mood)}</span></h4>
        ${!ai && au.keypoints && au.keypoints.length ? `<ul>${au.keypoints.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}
        ${au.figures && au.figures.length ? `<p><b>Kennzahlen:</b> ${au.figures.map(esc).join(" · ")}</p>` : ""}
        ${au.tags && au.tags.length ? `<p><b>Themen:</b> ${au.tags.map(esc).join(", ")}</p>` : ""}</div>`;
    }
    if (it.teaser) h += `<div class="teaser"><b>Anriss der Quelle${it.lang === "en" ? " (englisch)" : ""}</b>${esc(it.teaser)}</div>`;
    if (!ai && !it.teaser) h += `<p class="teaser muted">Die Quelle liefert nur die Schlagzeile. Den ganzen Artikel findest du über „Artikel lesen“.</p>`;
    if (it.also && it.also.length) h += `<div class="also">Auch berichtet von: ${it.also.map((a) => `<a href="${esc(safeUrl(a.link))}" target="_blank" rel="noopener">${esc(a.source)}</a>`).join(", ")}</div>`;
    const sv = !!S.saved[it.id];
    h += `<div class="actions">
      <a class="btn primary" href="${esc(safeUrl(it.link))}" target="_blank" rel="noopener">Artikel lesen ↗</a>
      <button class="btn${sv ? " saved" : ""}" data-act="save">${sv ? "Gemerkt ✓" : "Merken"}</button>
      ${navigator.share ? '<button class="btn" data-act="share">Teilen</button>' : ""}
    </div>
    <div class="also">${esc(new Date(it.published).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" }))} Uhr · ${esc((it.cats || []).map(catName).join(", "))}</div>`;
    return h;
  }

  main.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const art = b.closest(".art"), id = art && art.dataset.id;
    const act = b.dataset.act;
    if (act === "toggle" && id) {
      if (S.open.has(id)) S.open.delete(id); else { S.open.add(id); markRead(id); }
      art.outerHTML = card(byId(id));
    } else if (act === "save" && id) {
      if (S.saved[id]) { delete S.saved[id]; toast("Aus Merkliste entfernt"); }
      else { S.saved[id] = { ...byId(id), savedAt: new Date().toISOString() }; toast("Gemerkt – zu finden unter „Merkliste“"); }
      LS.set("saved", S.saved);
      const a2 = main.querySelector(`.art[data-id="${CSS.escape(id)}"]`); if (a2) a2.outerHTML = card(byId(id));
    } else if (act === "share" && id) {
      const it = byId(id); navigator.share({ title: it.title, url: it.link }).catch(() => {});
    } else if (act === "cat") {
      S.cat = b.dataset.cat; LS.set("cat", S.cat); viewNews();
    } else if (act === "sort") {
      S.sort = b.dataset.sort; LS.set("sort", S.sort); viewNews();
    } else if (act === "gocat") {
      S.cat = b.dataset.cat; LS.set("cat", S.cat); S.view = "news";
      document.querySelectorAll("#tabbar button").forEach((x) => x.classList.toggle("on", x.dataset.view === "news"));
      render(); window.scrollTo({ top: 0 });
    } else if (act === "allread") {
      listed().forEach((x) => S.read.add(x.id)); saveRead(); viewNews();
    }
  });
  function markRead(id) { S.read.add(id); saveRead(); }
  function saveRead() { LS.set("read", [...S.read].slice(-3000)); }

  // ------------------------------------------------------------------ Ansicht: Briefing
  function viewBriefing() {
    const b = S.briefing;
    if (!b && !S.news) { main.innerHTML = `<div class="empty">Noch keine Daten vorhanden.<br>Nach der Einrichtung erscheint hier jeden Morgen dein Bericht.</div>`; return; }
    let h = "";
    const shown = new Set();
    if (b) {
      const isAI = b.art === "ki";
      h += `<section class="lead">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <span class="eyebrow">${esc(new Date(b.at || Date.now()).toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" }))}</span>
          <span class="badge ${isAI ? "ai" : "auto"}">${isAI ? "KI-Bericht · " + esc(clock(b.at)) + " Uhr" : "automatisch zusammengestellt"}</span></div>
        <h2>${esc(b.titel)}</h2>
        ${isAI ? `<p>${esc(b.ueberblick)}</p>` : ""}
        ${b.maerkte ? `<p class="mk">${esc(b.maerkte)}</p>` : ""}
        ${tickStrip()}
      </section>`;
      if (b.beobachten && b.beobachten.length) {
        h += `<div class="section-h">Worauf heute zu achten ist</div><div class="sec"><ul class="watch">${b.beobachten.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
      }
      const secs = (b.abschnitte || []).filter((s) => !S.hiddenCats.has(s.kategorie));
      if (secs.length) h += `<div class="section-h">Die Bereiche im Überblick</div>`;
      secs.forEach((s) => {
        // jeden Artikel nur einmal im Briefing zeigen, auch wenn er zu mehreren Bereichen passt
        const arts = (s.ids || []).filter((id) => !shown.has(id)).map(byId).filter(Boolean);
        arts.forEach((a) => shown.add(a.id));
        h += `<div class="sec"><div class="eyebrow">${esc(catName(s.kategorie))}</div>
          ${isAI ? `<p>${esc(s.text)}</p>` : ""}</div>`;
        h += arts.map(card).join("");
        h += `<div style="text-align:right;margin:-2px 2px 16px"><button class="btn" data-act="gocat" data-cat="${esc(s.kategorie)}">Alle Meldungen: ${esc(catName(s.kategorie))} →</button></div>`;
      });
    }
    // Neu seit dem Bericht
    if (b && b.at) {
      const fresh = items().filter((x) => visible(x) && new Date(x.published) > new Date(b.at) && !shown.has(x.id)).sort((a, c) => c.score - a.score).slice(0, 8);
      if (fresh.length) h += `<div class="section-h">Neu seit dem Bericht <small>${fresh.length} wichtigste</small></div>` + fresh.map(card).join("");
    }
    main.innerHTML = h;
  }
  function tickStrip() {
    const ms = ((S.markets && S.markets.markets) || []).filter((m) => m.ok && m.price != null);
    if (!ms.length) return "";
    const want = ["DAX", "Euro Stoxx 50", "S&P 500", "Nasdaq 100", "Nikkei 225", "EUR/USD", "Gold (USD/oz)", "Brent-Öl (USD)", "Bitcoin (EUR)"];
    const pick = want.map((n) => ms.find((m) => m.name === n)).filter(Boolean);
    return `<div class="strip">${pick.map((m) => `<div class="tick"><b>${esc(m.name)}</b><span>${nf(m.price, m.price > 1000 ? 0 : 2)}</span> <span class="${m.chgPct >= 0 ? "up" : "down"}" style="font-size:.78em">${pct(m.chgPct)}</span></div>`).join("")}</div>`;
  }

  // ------------------------------------------------------------------ Ansicht: Nachrichten
  function listed() {
    let arr = items().filter(visible);
    if (S.cat === "unread") arr = arr.filter((x) => !S.read.has(x.id));
    else if (S.cat !== "all") arr = arr.filter((x) => (x.cats || [x.cat]).includes(S.cat));
    if (S.q) {
      const q = S.q.toLowerCase();
      arr = arr.filter((x) => (x.title + " " + (x.teaser || "") + " " + x.source + " " + ((x.ai && x.ai.zusammenfassung) || "") + " " + ((x.auto && x.auto.tags) || []).join(" ")).toLowerCase().includes(q));
    }
    if (S.sort === "top") arr.sort((a, b) => b.score - a.score);
    else arr.sort((a, b) => (a.published < b.published ? 1 : -1));
    return arr;
  }
  function viewNews() {
    const cats = ((S.news && S.news.categories) || []).filter((c) => !S.hiddenCats.has(c.id));
    const all = items().filter(visible);
    const unread = all.filter((x) => !S.read.has(x.id)).length;
    const count = (id) => all.filter((x) => (x.cats || [x.cat]).includes(id)).length;
    let h = `<div class="chips">
      <button class="chip${S.cat === "all" ? " on" : ""}" data-act="cat" data-cat="all">Alle<span class="n">${all.length}</span></button>
      <button class="chip${S.cat === "unread" ? " on" : ""}" data-act="cat" data-cat="unread">Ungelesen<span class="n">${unread}</span></button>
      ${cats.map((c) => `<button class="chip${S.cat === c.id ? " on" : ""}" data-act="cat" data-cat="${esc(c.id)}">${esc(c.name)}<span class="n">${count(c.id)}</span></button>`).join("")}
    </div>
    <div class="tools"><input class="search" id="q" type="search" placeholder="Suchen, z. B. EZB, Bodenrichtwert …" value="${esc(S.q)}">
      <div class="seg"><button class="${S.sort === "top" ? "on" : ""}" data-act="sort" data-sort="top">Wichtig</button><button class="${S.sort === "new" ? "on" : ""}" data-act="sort" data-sort="new">Neu</button></div></div>`;
    const arr = listed();
    if (!arr.length) h += `<div class="empty">${S.cat === "unread" ? "Alles gelesen 👍" : "Keine Meldungen gefunden."}</div>`;
    else {
      if (S.cat === "unread") h += `<div style="text-align:right;margin-bottom:8px"><button class="btn" data-act="allread">Alle als gelesen markieren</button></div>`;
      if (S.sort === "new") {
        let last = "";
        arr.slice(0, 200).forEach((x) => { const d = dayLabel(x.published); if (d !== last) { h += `<div class="dayh">${esc(d)}</div>`; last = d; } h += card(x); });
      } else h += arr.slice(0, 200).map(card).join("");
    }
    main.innerHTML = h;
    const q = $("#q");
    q.addEventListener("input", () => { S.q = q.value; clearTimeout(viewNews._t); viewNews._t = setTimeout(() => { const pos = q.selectionStart; viewNews(); const q2 = $("#q"); q2.focus(); q2.setSelectionRange(pos, pos); }, 250); });
  }

  // ------------------------------------------------------------------ Ansicht: Märkte
  function spark(arr, up) {
    if (!arr || arr.length < 2) return "";
    const min = Math.min(...arr), max = Math.max(...arr), w = 70, h = 26;
    const pts = arr.map((v, i) => `${((i / (arr.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / (max - min || 1)) * (h - 4)).toFixed(1)}`).join(" ");
    return `<svg class="spark" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="var(${up ? "--pos" : "--neg"})" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }
  function viewMarkets() {
    const ms = (S.markets && S.markets.markets) || [];
    if (!ms.length) { main.innerHTML = `<div class="empty">Noch keine Marktdaten.</div>`; return; }
    const groups = [...new Set(ms.map((m) => m.group))];
    let h = `<p class="muted" style="font-size:.78em;margin:0 2px 12px">Kurse von Yahoo Finance, i. d. R. ca. 15 Min. verzögert. Veränderung zum Vortagesschluss; Linie = letzter Monat.</p>`;
    groups.forEach((g) => {
      h += `<div class="section-h" style="margin-top:6px">${esc(g)}</div><div class="mgroup">`;
      ms.filter((m) => m.group === g).forEach((m) => {
        if (!m.ok || m.price == null) { h += `<div class="mrow"><div><div class="nm">${esc(m.name)}</div><div class="sub">derzeit nicht verfügbar</div></div><div></div><div></div></div>`; return; }
        const up = (m.chgPct || 0) >= 0;
        h += `<div class="mrow"><div><div class="nm">${esc(m.name)}</div><div class="sub">1 Monat: ${pct(m.chg1mPct)}${m.time ? " · " + esc(clock(m.time * 1000)) : ""}</div></div>
          ${spark(m.spark, (m.chg1mPct || 0) >= 0)}
          <div class="val"><b>${nf(m.price, m.price > 1000 ? 0 : 2)}</b><span class="pill ${up ? "up" : "down"}">${pct(m.chgPct)}</span></div></div>`;
      });
      h += `</div>`;
    });
    main.innerHTML = h;
  }

  // ------------------------------------------------------------------ Ansicht: Merkliste
  function viewSaved() {
    const arr = Object.values(S.saved).sort((a, b) => (a.savedAt < b.savedAt ? 1 : -1));
    main.innerHTML = arr.length
      ? `<p class="muted" style="font-size:.8em;margin:0 2px 12px">Gemerkte Artikel bleiben hier gespeichert, auch wenn sie aus den aktuellen Nachrichten verschwinden.</p>` + arr.map(card).join("")
      : `<div class="empty">Noch nichts gemerkt.<br>Öffne einen Artikel und tippe auf „Merken“, um ihn später in Ruhe zu lesen.</div>`;
  }

  // ------------------------------------------------------------------ Ansicht: Einstellungen
  function viewSettings() {
    const st = S.status, ai = (st && st.ai) || (S.news && S.news.ai) || {};
    const cats = (S.news && S.news.categories) || [];
    const theme = LS.get("theme", "auto"), size = LS.get("size", "m");
    const seg = (key, val, opts) => `<div class="seg">${opts.map(([v, l]) => `<button class="${val === v ? "on" : ""}" data-pref="${key}" data-val="${v}">${l}</button>`).join("")}</div>`;
    let h = `<div class="section-h" style="margin-top:4px">Lesen</div><div class="set">
      <div class="row"><label>Schriftgröße</label>${seg("size", size, [["s", "A−"], ["m", "A"], ["l", "A+"], ["xl", "A++"]])}</div>
      <div class="row"><label>Darstellung</label>${seg("theme", theme, [["auto", "Auto"], ["light", "Hell"], ["dark", "Dunkel"]])}</div>
    </div>
    <div class="section-h">Bereiche</div><div class="set">
      ${cats.map((c) => `<div class="row"><label for="c_${esc(c.id)}">${esc(c.name)}</label><input class="switch" type="checkbox" id="c_${esc(c.id)}" data-cat="${esc(c.id)}" ${S.hiddenCats.has(c.id) ? "" : "checked"}></div>`).join("") || '<p>Noch keine Daten.</p>'}
    </div>
    <div class="section-h">Sofort aktualisieren</div><div class="set">
      <p>Die App holt sich alle 30 Minuten (5–22 Uhr) automatisch die neuesten Meldungen. Damit der Knopf ↻ oben zusätzlich <b>sofort</b> einen neuen Abruf startet, trage hier einmalig deinen GitHub-Zugangsschlüssel ein (siehe Anleitung). Er wird nur auf diesem iPhone gespeichert.</p>
      <div class="row" style="display:block"><label>GitHub-Zugangsschlüssel (Token)</label><input type="password" id="token" autocomplete="off" placeholder="github_pat_…" value="${esc(LS.get("token", ""))}"></div>
      <div class="row" style="display:block"><label>Repository <span class="muted">(automatisch erkannt: ${esc(repoInfo() || "–")})</span></label><input type="text" id="repo" autocomplete="off" placeholder="benutzername/morgen-briefing" value="${esc(LS.get("repo", ""))}"></div>
    </div>
    <div class="section-h">Status</div><div class="set">
      <div class="row"><span>Letzter Abruf</span><span>${st ? esc(ago(st.updated)) : "–"}</span></div>
      <div class="row"><span>Meldungen</span><span>${st ? st.itemCount : "–"}</span></div>
      <div class="row"><span>KI-Auswertung</span><span class="${ai.enabled ? "ok" : "err"}">${ai.enabled ? "aktiv (" + esc(ai.model) + ")" : "aus" + (ai.reason ? " – " + esc(ai.reason) : "")}</span></div>
      <div class="row"><span>KI-Aufrufe heute</span><span>${ai.callsToday != null ? ai.callsToday + " / " + ai.maxPerDay : "–"}</span></div>
      <details><summary>Nachrichtenquellen (${st && st.feeds ? st.feeds.filter((f) => f.ok).length + " von " + st.feeds.length + " erreichbar" : "–"})</summary>
        ${((st && st.feeds) || []).map((f) => `<div class="feedrow"><span>${esc(f.feed)}<br><span class="muted">${esc(catName(f.cat))}</span></span><span class="${f.ok ? "ok" : "err"}">${f.ok ? f.count + " ✓" : "Fehler"}</span></div>`).join("")}
      </details>
    </div>
    <div class="section-h">Daten</div><div class="set">
      <div class="row"><span>Gelesen-Markierungen zurücksetzen</span><button class="btn" id="resetRead">Zurücksetzen</button></div>
      <div class="row"><span>Merkliste leeren</span><button class="btn" id="resetSaved">Leeren</button></div>
    </div>
    <p class="muted" style="font-size:.74em;text-align:center;margin:20px 10px">Nachrichten aus öffentlichen RSS-Quellen der jeweiligen Verlage; Auswertungen automatisch bzw. per KI erstellt und ohne Gewähr. Keine Anlageberatung.</p>`;
    main.innerHTML = h;
    main.querySelectorAll("[data-pref]").forEach((b) => b.addEventListener("click", () => { LS.set(b.dataset.pref, b.dataset.val); applyPrefs(); viewSettings(); }));
    main.querySelectorAll("input[data-cat]").forEach((i) => i.addEventListener("change", () => {
      if (i.checked) S.hiddenCats.delete(i.dataset.cat); else S.hiddenCats.add(i.dataset.cat);
      LS.set("hiddenCats", [...S.hiddenCats]);
    }));
    $("#token").addEventListener("change", (e) => { LS.set("token", e.target.value.trim()); toast("Gespeichert"); });
    $("#repo").addEventListener("change", (e) => { LS.set("repo", e.target.value.trim()); viewSettings(); });
    $("#resetRead").addEventListener("click", () => { S.read.clear(); saveRead(); toast("Zurückgesetzt"); });
    $("#resetSaved").addEventListener("click", () => { if (confirm("Merkliste wirklich leeren?")) { S.saved = {}; LS.set("saved", {}); toast("Geleert"); } });
  }

  // ------------------------------------------------------------------ Start
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && S.news && Date.now() - new Date(S.news.updated) > 5 * 60000) loadAll(true);
    if (document.visibilityState === "hidden") LS.set("lastVisit", new Date().toISOString());
  });
  window.addEventListener("pagehide", () => LS.set("lastVisit", new Date().toISOString()));
  if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("sw.js").catch(() => {});
  loadAll(true);
})();
