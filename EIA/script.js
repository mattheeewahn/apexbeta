/* ============================================================
   EIA — ELECTRON INTERNATIONAL AIRPORT
   Atomic Terminal Operations — application logic
   ============================================================ */
(function () {
  "use strict";

  /* ---------- constants ---------- */
  const SUP = { 1: "\u2081", 2: "\u2082", 3: "\u2083" };
  const UP = "\u2191";     // ↑
  const DOWN = "\u2193";   // ↓
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- canonical electron records (derived from central atom) ----------
     `electrons` is NOT a fixed Oxygen dataset. It is rebuilt from atom.records
     every time the atom/ion changes (see rebuildLegacyElectrons()). Legacy
     modules read this array; the new simulator reads atom.records. Both derive
     from the same central configuration engine. */
  let electrons = [];

  /* ---------- global state ---------- */
  const state = {
    selectedElectron: null,
    energyState: "ground", // ground | excited
    hundStep: 0,
    quizProgress: 0,
    quizScore: 0
  };

  /* track whether a photon has been emitted at least once (for boards) */
  let photonEmitted = false;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /* ============================================================
     STARTUP SEQUENCE (<= ~1s)
     ============================================================ */
  function initStartup() {
    const el = $("#startup");
    const bar = $("#startupProgress");
    const status = $("#startupStatus");
    if (!el) return;
    const steps = ["INITIALIZING TERMINAL SYSTEMS", "LOADING GATE ASSIGNMENTS", "SYNCING ELECTRON MANIFEST", "TERMINAL OPERATIONS ONLINE"];
    let p = 0, si = 0;
    const total = reduceMotion ? 60 : 900;
    const t0 = performance.now();
    function tick(now) {
      p = Math.min(1, (now - t0) / total);
      bar.style.width = (p * 100) + "%";
      const ns = Math.min(steps.length - 1, Math.floor(p * steps.length));
      if (ns !== si) { si = ns; status.textContent = steps[si]; }
      if (p < 1) requestAnimationFrame(tick);
      else setTimeout(() => el.classList.add("done"), 120);
    }
    requestAnimationFrame(tick);
  }

  /* ============================================================
     NAVIGATION
     ============================================================ */
  function initNavigation() {
    const links = $$(".mainnav a");
    const toggle = $("#navtoggle");
    const nav = $("#mainnav");

    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    });
    links.forEach((a) => a.addEventListener("click", () => {
      nav.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
      $$(".nav-group", nav).forEach((g) => g.classList.remove("open"));
    }));
    // mobile: tap group button toggles its dropdown
    $$(".nav-group-btn", nav).forEach((btn) => btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      const group = btn.closest(".nav-group");
      const isOpen = group.classList.toggle("open");
      btn.setAttribute("aria-expanded", String(isOpen));
    }));

    // scroll-based active highlight
    const sections = $$("main .section[data-section]");
    const map = {};
    links.forEach((a) => (map[a.dataset.nav] = a));
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          const id = e.target.dataset.section;
          links.forEach((l) => l.classList.remove("active"));
          if (map[id]) map[id].classList.add("active");
        }
      });
    }, { rootMargin: "-45% 0px -50% 0px", threshold: 0 });
    sections.forEach((s) => obs.observe(s));

    // scroll buttons
    $$("[data-scroll]").forEach((btn) => btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.scroll);
      if (target) target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
    }));
  }

  /* ============================================================
     ENTRANCE FLIP TEXT (subtle FIDS-style)
     ============================================================ */
  function initFlip() {
    if (reduceMotion) return;
    $$(".flip").forEach((el) => {
      const text = el.dataset.flip;
      let i = 0;
      const glyphs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/ ";
      const interval = setInterval(() => {
        el.textContent = text.split("").map((ch, idx) => {
          if (idx < i) return ch;
          return glyphs[Math.floor(Math.random() * glyphs.length)];
        }).join("");
        i += 0.5;
        if (i >= text.length) { el.textContent = text; clearInterval(interval); }
      }, 40);
    });
  }

  /* ============================================================
     ELECTRON OPERATIONS TABLE (FIDS)
     ============================================================ */
  function stateLabel() {
    return state.energyState === "excited" ? "EXCITED" : "BOARDED";
  }

  function renderElectronTable() {
    const tbody = $("#electronTable tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    electrons.forEach((e) => {
      const tr = document.createElement("tr");
      tr.dataset.id = e.id;
      tr.innerHTML = rowHTML(e);
      tr.addEventListener("click", () => selectElectron(e.id, true));
      if (state.selectedElectron === e.id) tr.classList.add("selected");
      tbody.appendChild(tr);
    });
  }
  function initElectronTable() { renderElectronTable(); }

  function rowHTML(e) {
    const d = getElectronDisplayState(e);
    const spinClass = d.spin === UP ? "spin-up" : "spin-down";
    const eClass = d.energy === "LOW" ? "energy-low" : (d.energy === "HIGH" ? "energy-med" : "energy-med");
    const excited = d.stateKey === "excited";
    const stLabel = excited ? "EXCITED" : "BOARDED";
    const stClass = excited ? "excited" : "boarded";
    return `
      <td>${d.id}</td>
      <td>T${d.terminal}</td>
      <td>${d.section}</td>
      <td>${d.orbital}</td>
      <td class="${spinClass}">${d.spin}</td>
      <td class="${eClass}">${d.energy}</td>
      <td class="badge-state ${stClass}">${stLabel}</td>`;
  }

  function refreshTableRow(id) {
    const e = electrons.find((x) => x.id === id);
    const tr = $(`#electronTable tr[data-id="${id}"]`);
    if (e && tr) {
      tr.innerHTML = rowHTML(e);
      tr.addEventListener("click", () => selectElectron(e.id, true));
      if (state.selectedElectron === id) tr.classList.add("selected");
    }
  }

  /* ============================================================
     PASSENGER MANIFEST
     ============================================================ */
  function renderManifest() {
    const wrap = $("#manifest");
    if (!wrap) return;
    wrap.innerHTML = "";
    electrons.forEach((e) => {
      const d = getElectronDisplayState(e);
      const row = document.createElement("div");
      row.className = "manifest-row";
      row.dataset.id = e.id;
      const spinClass = d.spin === UP ? "spin-up" : "spin-down";
      row.innerHTML = `<span class="m-id">${d.id}</span>
        <span class="m-orb">${d.orbital}</span>
        <span class="m-spin ${spinClass}">${d.spin}</span>`;
      row.addEventListener("click", () => selectElectron(e.id, false));
      if (state.selectedElectron === e.id) row.classList.add("selected");
      wrap.appendChild(row);
    });
    // update manifest count label if present
    const head = wrap.previousElementSibling;
    if (head && head.classList && head.classList.contains("mini-head")) {
      head.textContent = "PASSENGER MANIFEST · " + atom.symbol + " / " + String(atom.electronCount).padStart(2, "0");
    }
  }
  function initManifest() { renderManifest(); }

  /* ============================================================
     SELECTION -> record, boarding pass, drawer, sync
     ============================================================ */
  function selectElectron(id, openDrawer) {
    const e = electrons.find((x) => x.id === id);
    if (!e) { state.selectedElectron = null; return; }
    state.selectedElectron = id;

    // table highlight + sweep
    $$("#electronTable tbody tr, #electronTable tr[data-id]").forEach((tr) => {
      tr.classList.toggle("selected", tr.dataset.id === id);
    });
    const selRow = $(`#electronTable tr[data-id="${id}"]`);
    if (selRow && !reduceMotion) {
      selRow.classList.remove("sweep"); void selRow.offsetWidth; selRow.classList.add("sweep");
    }
    $$(".manifest-row").forEach((r) => r.classList.toggle("selected", r.dataset.id === id));

    renderRecord(e);
    renderBoardingPass(e);
    if (openDrawer) openOpsDrawer(e);
  }

  /* ============================================================
     CANONICAL DISPLAY STATE — single source of truth.
     Every UI module derives an electron's displayed assignment
     from this function. Handles excited E-08 (2p → 3s).
     ============================================================ */
  function getElectronDisplayState(electron) {
    const e = typeof electron === "string"
      ? electrons.find((x) => x.id === electron)
      : electron;
    if (!e) return null;
    // The 2p→3s excitation is an Oxygen-specific educational demonstration.
    // Only apply it when Oxygen is the loaded atom, so other atoms are unaffected.
    const excited = e.id === "E-08" && state.energyState === "excited" && atom.symbol === "O";
    if (excited) {
      return {
        id: e.id,
        terminal: 3,
        section: "s",
        subshell: "3s",
        orbital: "3s",
        gate: "S01",
        spin: e.spin,
        energy: "HIGH",
        atomState: "excited",
        stateText: "Excited State",
        stateKey: "excited"
      };
    }
    return {
      id: e.id,
      terminal: e.terminal,
      section: e.section,
      subshell: e.terminal + e.section,
      orbital: e.orbital,
      gate: e.section === "p" ? e.gate : e.section.toUpperCase() + "01",
      spin: e.spin,
      energy: e.energy,
      atomState: "ground",
      stateText: "Ground State",
      stateKey: "ground"
    };
  }

  /* registry of update callbacks for global state sync */
  const syncHandlers = [];
  function onSync(fn) { syncHandlers.push(fn); }
  function syncAll() { syncHandlers.forEach((fn) => { try { fn(); } catch (err) { /* isolate */ } }); }

  function renderRecord(e) {
    const panel = $("#recordPanel");
    const d = getElectronDisplayState(e);
    panel.innerHTML = `
      <div class="record-grid">
        <div class="record-cell"><span class="rc-k">PASSENGER</span><span class="rc-v">${d.id}</span></div>
        <div class="record-cell"><span class="rc-k">PARTICLE</span><span class="rc-v">e⁻</span></div>
        <div class="record-cell"><span class="rc-k">CHARGE</span><span class="rc-v">−1</span></div>
        <div class="record-cell"><span class="rc-k">TERMINAL</span><span class="rc-v">${d.terminal}</span></div>
        <div class="record-cell"><span class="rc-k">SUBSHELL</span><span class="rc-v">${d.subshell}</span></div>
        <div class="record-cell"><span class="rc-k">ORBITAL</span><span class="rc-v">${d.orbital}</span></div>
        <div class="record-cell"><span class="rc-k">SPIN</span><span class="rc-v">${d.spin}</span></div>
        <div class="record-cell"><span class="rc-k">STATE</span><span class="rc-v" style="color:${d.stateKey === 'excited' ? 'var(--amber)' : 'var(--green)'}">${d.stateText.toUpperCase()}</span></div>
        <div class="record-cell"><span class="rc-k">CODE</span><span class="rc-v">T${d.terminal}-${d.gate}</span></div>
      </div>
      <p class="record-note">Electrons are negatively charged subatomic particles. Their arrangement around the nucleus determines an atom's electron configuration.</p>`;
  }

  function renderBoardingPass(e) {
    const wrap = $("#boardingPassWrap");
    wrap.hidden = false;
    const d = getElectronDisplayState(e);
    $("#bpId").textContent = d.id;
    $("#bpTerminal").textContent = String(d.terminal).padStart(2, "0");
    $("#bpSection").textContent = d.section.toUpperCase();
    $("#bpGate").textContent = d.gate;
    $("#bpOrbital").textContent = d.orbital;
    $("#bpSpin").textContent = d.spin;
    const st = d.stateKey === "excited" ? "EXCITED" : "BOARDED";
    $("#bpStatus").textContent = st;
    $("#bpStatus").style.color = d.stateKey === "excited" ? "var(--amber)" : "var(--green)";
    $("#bpOpCode").textContent = "T" + d.terminal + "-" + d.gate;
    // dynamic atom identity on the pass
    const el = currentElement();
    const bpAtomName = $("#bpAtomName"); if (bpAtomName) bpAtomName.textContent = el.name.toUpperCase();
    const bpAtom = $("#bpAtom"); if (bpAtom) bpAtom.textContent = el.sym + " / " + String(atom.protons).padStart(2, "0");
    const bpFac = $("#bpFacility"); if (bpFac) bpFac.textContent = facilityCode();
    // re-trigger slide
    if (!reduceMotion) {
      const bp = $("#boardingPass");
      bp.style.animation = "none"; void bp.offsetWidth; bp.style.animation = "";
    }
  }

  /* ---------- ops drawer ---------- */
  function openOpsDrawer(e) {
    const drawer = $("#opsDrawer");
    const scrim = $("#drawerScrim");
    const body = $("#drawerBody");
    const d = getElectronDisplayState(e);
    body.innerHTML = `
      <h3>${d.id}</h3>
      <div class="db-sub">${currentElement().name.toUpperCase()} / ${currentElement().sym} · ${String(atom.protons).padStart(2, "0")} · ${d.stateText.toUpperCase()}</div>
      <div class="db-grid">
        <div class="db-cell"><span class="db-k">PARTICLE</span><span class="db-v">Electron</span></div>
        <div class="db-cell"><span class="db-k">CHARGE</span><span class="db-v">−1</span></div>
        <div class="db-cell"><span class="db-k">TERMINAL</span><span class="db-v">${d.terminal}</span></div>
        <div class="db-cell"><span class="db-k">SUBSHELL</span><span class="db-v">${d.subshell}</span></div>
        <div class="db-cell"><span class="db-k">ORBITAL</span><span class="db-v">${d.orbital}</span></div>
        <div class="db-cell"><span class="db-k">SPIN</span><span class="db-v">${d.spin}</span></div>
      </div>
      <p class="db-note">Electrons are negatively charged subatomic particles. Their arrangement around the nucleus determines an atom's electron configuration.</p>
      <button class="btn btn-primary db-open-pass" id="drawerToPass">VIEW BOARDING PASS</button>`;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    scrim.hidden = false;
    $("#drawerToPass").addEventListener("click", () => {
      closeOpsDrawer();
      $("#passengers").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
    });
  }
  function closeOpsDrawer() {
    $("#opsDrawer").classList.remove("open");
    $("#opsDrawer").setAttribute("aria-hidden", "true");
    const gs = $("#gateScreen");
    if (!gs || !gs.classList.contains("open")) $("#drawerScrim").hidden = true;
  }
  function initDrawer() {
    $("#drawerClose").addEventListener("click", closeOpsDrawer);
    $("#drawerScrim").addEventListener("click", () => {
      closeOpsDrawer();
      const gs = $("#gateScreen");
      if (gs) { gs.classList.remove("open"); gs.setAttribute("aria-hidden", "true"); }
      $("#drawerScrim").hidden = true;
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        closeOpsDrawer();
        const gs = $("#gateScreen");
        if (gs && gs.classList.contains("open")) { gs.classList.remove("open"); gs.setAttribute("aria-hidden", "true"); $("#drawerScrim").hidden = true; }
      }
    });
  }

  /* ============================================================
     TERMINAL MAP interactions
     ============================================================ */
  function initTerminalMap() {
    const readout = $("#mapReadoutBody");

    function clearHighlights() {
      $$(".terminal, .concourse, .gate").forEach((el) => el.classList.remove("highlight"));
    }

    function setReadout(title, code, body, limit) {
      readout.innerHTML = `<h4>${title}</h4>
        <div class="r-code">${code}</div>
        <p>${body}</p>
        ${limit ? `<div class="r-limit">${limit}</div>` : ""}`;
    }

    // populate gate occupancy for filled 2p/2s/1s gates
    updateMapOccupancy();

    $$(".terminal").forEach((t) => {
      t.addEventListener("click", (ev) => {
        if (ev.target.closest(".gate") || ev.target.closest(".concourse")) return;
        clearHighlights();
        t.classList.add("highlight");
        const n = t.dataset.terminal;
        setReadout("TERMINAL " + n, "SHELL n = " + n,
          "This terminal represents principal energy level n = " + n + ". A shell is a principal energy region; higher n means higher average energy and greater distance from the nucleus.",
          "A shell is not a physical building — it is a quantized energy level.");
      });
    });

    $$(".concourse").forEach((c) => {
      c.addEventListener("click", (ev) => {
        if (ev.target.closest(".gate")) return;
        clearHighlights();
        c.classList.add("highlight");
        c.closest(".terminal").classList.add("highlight");
        const sub = c.dataset.subshell;
        const type = sub.slice(-1);
        const caps = { s: "1 orbital, 2 electrons", p: "3 orbitals, 6 electrons", d: "5 orbitals, 10 electrons", f: "7 orbitals, 14 electrons" };
        setReadout(type.toUpperCase() + " CONCOURSE", "SUBSHELL " + sub,
          "A subshell is a division within an energy level, labelled s, p, d or f. The " + sub + " subshell holds " + caps[type] + ".",
          "Concourses group orbitals of equal energy; they are an organisational analogy, not physical corridors.");
      });
    });

    $$(".gate").forEach((g) => {
      g.addEventListener("click", () => {
        clearHighlights();
        g.classList.add("highlight");
        g.closest(".concourse").classList.add("highlight");
        g.closest(".terminal").classList.add("highlight");
        const orb = g.dataset.orbital;
        const occ = mapGateOccupants(orb);
        setReadout("GATE " + orb, "ORBITAL " + orb,
          "An orbital is a region of space, described by the quantum mechanical model, where an electron is likely to be found. Each orbital holds up to two electrons with opposite spins. " +
          (occ.length ? "Currently assigned: " + occ.map((x) => x.id + " (" + x.spin + ")").join(", ") + "." : "This orbital is currently unoccupied in oxygen's ground state."),
          "An orbital is not a physical room or fixed location — it is a probability distribution.");
      });
    });
  }

  function mapGateOccupants(orb) {
    // Map map-gate orbital ids (1s, 2s, 2p1..) to electron records.
    // electron.orbital uses subscripts (2p₁); normalise to ascii (2p1).
    const oxExcited = atom.symbol === "O" && state.energyState === "excited";
    return electrons.filter((e) => {
      // Oxygen-only excitation demo: E-08 promoted to 3s
      if (oxExcited && e.id === "E-08") return orb === "3s";
      const orbNorm = e.orbital
        .replace(SUP[1], "1").replace(SUP[2], "2").replace(SUP[3], "3");
      return orbNorm === orb;
    });
  }

  function updateMapOccupancy() {
    $$(".gate").forEach((g) => {
      const orb = g.dataset.orbital;
      const occ = mapGateOccupants(orb);
      let tag = g.querySelector(".gate-occ");
      if (!tag) { tag = document.createElement("span"); tag.className = "gate-occ"; g.appendChild(tag); }
      if (occ.length) {
        g.dataset.empty = "false";
        tag.textContent = occ.map((o) => o.spin).join("");
      } else {
        g.dataset.empty = "true";
        tag.textContent = "—";
      }
    });
    const nuc = $("#nucleusLabel");
    if (nuc) nuc.textContent = "NUCLEUS / CORE · +" + atom.protons;
  }

  /* ============================================================
     AUFBAU SIMULATION
     ============================================================ */
  const aufbauOrder = [
    { name: "1s", cap: 2 },
    { name: "2s", cap: 2 },
    { name: "2p", cap: 6 },
    { name: "3s", cap: 2 },
    { name: "3p", cap: 6 }
  ];
  let aufbauTimer = null;

  function initAufbauSimulation() {
    const gatesWrap = $("#aufbauGates");
    gatesWrap.innerHTML = aufbauOrder.map((o) =>
      `<div class="au-gate" data-name="${o.name}">
        <div class="au-name">${o.name}</div>
        <div class="au-slots">${Array.from({ length: o.cap }).map(() => `<span class="au-slot"></span>`).join("")}</div>
      </div>`).join("");

    const log = $("#aufbauLog");

    function reset() {
      clearInterval(aufbauTimer);
      $$("#aufbauGates .au-slot").forEach((s) => s.classList.remove("filled"));
      log.innerHTML = "";
      $("#aufbauRun").disabled = false;
    }

    $("#aufbauReset").addEventListener("click", reset);

    $("#aufbauRun").addEventListener("click", () => {
      reset();
      $("#aufbauRun").disabled = true;
      // 8 electrons in oxygen fill order
      const fillSeq = [];
      let idx = 1;
      for (const o of aufbauOrder) {
        for (let i = 0; i < o.cap && idx <= 8; i++, idx++) {
          fillSeq.push({ eid: "E-" + String(idx).padStart(2, "0"), gate: o.name });
        }
      }
      let step = 0;
      const gap = reduceMotion ? 60 : 320;
      aufbauTimer = setInterval(() => {
        if (step >= fillSeq.length) {
          clearInterval(aufbauTimer);
          addLog(log, "CONFIGURATION COMPLETE · 1s² 2s² 2p⁴");
          return;
        }
        const item = fillSeq[step];
        const gateEl = $(`#aufbauGates .au-gate[data-name="${item.gate}"]`);
        const openSlot = gateEl.querySelector(".au-slot:not(.filled)");
        if (openSlot) openSlot.classList.add("filled");
        addLog(log, item.eid + " \u2192 " + item.gate);
        step++;
      }, gap);
    });
  }

  function addLog(list, text) {
    const li = document.createElement("li");
    li.className = "new";
    li.textContent = text;
    list.appendChild(li);
    list.scrollTop = list.scrollHeight;
  }

  /* ============================================================
     HUND SIMULATION (2p subshell, 4 electrons)
     ============================================================ */
  function initHundSimulation() {
    const gates = $$("#hundGates .hund-gate");
    const status = $("#hundStatus");
    // sequence of (gateIndex, spin) for 4 electrons obeying Hund
    const seq = [
      { g: 0, spin: UP }, { g: 1, spin: UP }, { g: 2, spin: UP }, { g: 0, spin: DOWN }
    ];
    const messages = [
      "1 / 4 · single electron, parallel spin",
      "2 / 4 · empty gates filled first",
      "3 / 4 · each 2p orbital singly occupied",
      "4 / 4 · pairing begins with opposite spin"
    ];

    function render() {
      gates.forEach((g) => (g.querySelector(".hg-slot").innerHTML = ""));
      for (let i = 0; i < state.hundStep; i++) {
        const s = seq[i];
        const slot = gates[s.g].querySelector(".hg-slot");
        const arr = document.createElement("span");
        arr.className = "arr" + (s.spin === DOWN ? " down" : "") + (!reduceMotion && i === state.hundStep - 1 ? " pop" : "");
        arr.textContent = s.spin;
        slot.appendChild(arr);
      }
      status.textContent = state.hundStep === 0
        ? "0 / 4 electrons assigned · empty gates first"
        : messages[state.hundStep - 1];
      $("#hundStep").disabled = state.hundStep >= seq.length;
    }

    $("#hundStep").addEventListener("click", () => {
      if (state.hundStep < seq.length) { state.hundStep++; render(); }
    });
    $("#hundReset").addEventListener("click", () => { state.hundStep = 0; render(); });
    render();
  }

  /* ============================================================
     PAULI SECURITY CONTROL
     ============================================================ */
  function initPauliSimulator() {
    const slots = $$("#pauliSlots .pauli-slot");
    const alert = $("#pauliAlert");
    const alertText = $("#pauliAlertText");

    function setAlert(stateKey, html) {
      alert.dataset.state = stateKey;
      if (!reduceMotion && stateKey === "denied") {
        alert.style.animation = "none"; void alert.offsetWidth; alert.style.animation = "";
      }
      alertText.innerHTML = html;
    }
    function clearSlots() {
      slots.forEach((s) => { s.textContent = ""; s.className = "pauli-slot"; });
    }

    $$("#pauliCombos .btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        clearSlots();
        const combo = btn.dataset.combo;
        const parts = combo.split("-"); // e.g. up, down
        const arrows = parts.map((p) => (p === "up" ? UP : DOWN));

        if (arrows.length > 2) {
          // too many electrons
          slots[0].textContent = arrows[0]; slots[0].classList.add("filled", arrows[0] === DOWN ? "down" : "");
          slots[1].textContent = arrows[1]; slots[1].classList.add("filled", arrows[1] === DOWN ? "down" : "");
          slots.forEach((s) => s.classList.add("invalid"));
          setAlert("denied", "<strong>ACCESS DENIED</strong>An orbital holds a maximum of two electrons. A third cannot board 2p₁.");
          return;
        }

        slots[0].textContent = arrows[0]; slots[0].classList.add("filled", arrows[0] === DOWN ? "down" : "");
        slots[1].textContent = arrows[1]; slots[1].classList.add("filled", arrows[1] === DOWN ? "down" : "");

        if (arrows[0] === arrows[1]) {
          slots.forEach((s) => s.classList.add("invalid"));
          setAlert("denied", "<strong>ACCESS DENIED</strong>Two electrons in one orbital must have opposite spins. Parallel spins violate the Pauli exclusion principle.");
        } else {
          setAlert("valid", "<strong>BOARDING APPROVED</strong>Opposite spins accepted. Gate 2p₁ is at capacity with a valid configuration.");
        }
      });
    });

    $("#pauliReset").addEventListener("click", () => {
      clearSlots();
      setAlert("idle", "Awaiting spin combination.");
    });
  }

  /* ============================================================
     ENERGY TRANSFER LAB  (E-08 absorb / emit)
     ============================================================ */
  let simClock = 15 * 3600 + 42 * 60 + 10; // simulated seconds -> 15:42:10
  function stamp(inc) {
    simClock += inc;
    const h = Math.floor(simClock / 3600) % 24;
    const m = Math.floor(simClock / 60) % 60;
    const s = simClock % 60;
    return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
  }

  function setEnergyState(next) {
    state.energyState = next;
    const excited = next === "excited";
    // markers
    const low = $("#markerLow"), high = $("#markerHigh");
    low.hidden = excited;
    high.hidden = !excited;
    high.classList.toggle("excited", excited);
    low.classList.toggle("excited", false);
    // status panel
    $("#esCurrent").textContent = excited ? "3s" : "2p";
    $("#esEnergy").textContent = excited ? "INCREASED" : "BASELINE";
    const stEl = $("#esStatus");
    stEl.textContent = excited ? "EXCITED STATE" : "GROUND STATE";
    stEl.dataset.state = next;
    // global indicators
    $("#navState").textContent = excited ? "EXCITED STATE" : "GROUND STATE";
    $("#navState").dataset.state = next;
    $("#entranceState").textContent = excited ? "EXCITED STATE" : "GROUND STATE";
    $("#entranceState").dataset.state = next;
    // buttons
    $("#absorbBtn").disabled = excited;
    $("#emitBtn").disabled = !excited;
    // sync table + map + selected record
    refreshTableRow("E-08");
    updateMapOccupancy();
    if (state.selectedElectron === "E-08") {
      const e = electrons.find((x) => x.id === "E-08");
      if (e) { renderRecord(e); renderBoardingPass(e); }
    }
    // footer + entrance reflect current atom and state
    renderFooter();
    renderEntrance();
    // sync all upgraded modules from canonical state
    syncAll();
  }

  /* shared: log an operations-timeline event (simulated clock) */
  const timelineEvents = [];
  function pushTimeline(event) {
    timelineEvents.push({ time: stamp(0), event });
    renderTimeline();
  }
  function renderTimeline() {
    const list = $("#opsTimeline");
    if (!list) return;
    if (!timelineEvents.length) {
      list.innerHTML = `<li><span class="tl-time mono">—</span><span class="tl-event muted">No events yet. Request an energy upgrade to begin.</span></li>`;
      return;
    }
    list.innerHTML = timelineEvents.map((t, i) =>
      `<li class="${i === timelineEvents.length - 1 ? "new" : ""}"><span class="tl-time mono">${t.time}</span><span class="tl-event">${t.event}</span></li>`).join("");
    list.scrollTop = list.scrollHeight;
  }

  function initEnergyLab() {
    const log = $("#energyLog");
    const pulseLayer = $("#pulseLayer");

    $("#absorbBtn").addEventListener("click", () => {
      if (state.energyState !== "ground") return;
      $("#absorbBtn").disabled = true;
      pushTimeline("Energy request received");
      addLog(log, stamp(0) + " ENERGY INPUT DETECTED");
      // energy pulse
      if (!reduceMotion) {
        const pulse = document.createElement("span");
        pulse.className = "energy-pulse absorb";
        pulse.style.left = "42%";
        pulseLayer.appendChild(pulse);
        setTimeout(() => pulse.remove(), 950);
      }
      const delay = reduceMotion ? 30 : 900;
      setTimeout(() => {
        setEnergyState("excited");
        addLog(log, stamp(1) + " ENERGY ABSORBED");
        addLog(log, stamp(0) + " E-08 TRANSITION 2p \u2192 3s");
        pushTimeline("Energy absorbed");
        pushTimeline("Transition 2p \u2192 3s");
        pushTimeline("Atom state changed to EXCITED");
      }, delay);
    });

    $("#emitBtn").addEventListener("click", () => {
      if (state.energyState !== "excited") return;
      $("#emitBtn").disabled = true;
      pushTimeline("Return initiated");
      // photon leaves
      if (!reduceMotion) {
        const ph = document.createElement("span");
        ph.className = "photon-particle";
        pulseLayer.appendChild(ph);
        setTimeout(() => ph.remove(), 1000);
      }
      const delay = reduceMotion ? 30 : 700;
      setTimeout(() => {
        photonEmitted = true;
        setEnergyState("ground");
        addLog(log, stamp(4) + " E-08 TRANSITION 3s \u2192 2p");
        addLog(log, stamp(0) + " PHOTON EMITTED · PH-01");
        pushTimeline("Transition 3s \u2192 2p");
        pushTimeline("Photon PH-001 emitted");
        pushTimeline("Ground state restored");
        showPhotonFids();
      }, delay);
    });
  }

  function showPhotonFids() {
    const fids = $("#photonFids");
    fids.hidden = false;
    if (!reduceMotion) { fids.style.animation = "none"; void fids.offsetWidth; fids.style.animation = ""; }
  }

  /* ============================================================
     SPECTRUM  (wavelength -> frequency -> energy)
     ============================================================ */
  function initSpectrum() {
    const slider = $("#spectrumSlider");
    const strip = $("#spectrumStrip");
    const h = 6.62607015e-34; // J·s
    const c = 299792458;      // m/s

    // marker line over strip
    const marker = document.createElement("div");
    marker.style.cssText = "position:absolute;top:0;bottom:0;width:2px;background:#fff;box-shadow:0 0 6px #000;pointer-events:none;";
    strip.style.position = "relative";
    strip.appendChild(marker);

    function band(nm) {
      if (nm < 450) return "VIOLET";
      if (nm < 495) return "BLUE";
      if (nm < 570) return "GREEN";
      if (nm < 590) return "YELLOW";
      if (nm < 620) return "ORANGE";
      return "RED";
    }

    function update() {
      const nm = parseInt(slider.value, 10);
      const lambda = nm * 1e-9;
      const freq = c / lambda;                 // Hz
      const energyJ = (h * c) / lambda;         // J
      const energyEV = energyJ / 1.602176634e-19;

      $("#srBand").textContent = band(nm);
      $("#srWave").textContent = nm + " nm";
      $("#srFreq").textContent = (freq / 1e12).toFixed(1) + " THz";
      $("#srEnergy").textContent = energyEV.toFixed(2) + " eV";

      const pct = ((nm - 380) / (750 - 380)) * 100;
      marker.style.left = pct + "%";
    }
    slider.addEventListener("input", update);
    update();
  }

  /* ============================================================
     BOHR MODEL electrons + RADAR orbital switch
     ============================================================ */
  function initModelComparison() {
    const g = $("#bohrElectrons");
    const shells = [{ r: 55, n: 2, cls: "" }, { r: 95, n: 6, cls: "shell2" }];
    let html = "";
    shells.forEach((sh) => {
      for (let i = 0; i < sh.n; i++) {
        const ang = (i / sh.n) * Math.PI * 2 - Math.PI / 2;
        const x = 120 + sh.r * Math.cos(ang);
        const y = 120 + sh.r * Math.sin(ang);
        html += `<circle class="bohr-e ${sh.cls}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4.5" />`;
      }
    });
    g.innerHTML = html;

    // radar orbital view switch
    const density = $("#orbitalDensity");
    $$("[data-orbital-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $$("[data-orbital-view]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        density.dataset.orbital = btn.dataset.orbitalView;
      });
    });
  }

  /* ============================================================
     QUIZ
     ============================================================ */
  const quizData = [
    { q: "Which principle states that electrons occupy lower-energy orbitals first?",
      opts: ["Hund's Rule", "Aufbau Principle", "Pauli Exclusion Principle", "Heisenberg Principle"], a: 1 },
    { q: "What is the maximum number of electrons in one orbital?",
      opts: ["1", "2", "4", "8"], a: 1 },
    { q: "Why do the three 2p orbitals each receive one electron before pairing?",
      opts: ["Aufbau Principle", "Pauli Exclusion Principle", "Hund's Rule", "Charge repulsion only"], a: 2 },
    { q: "What happens when an electron absorbs sufficient energy?",
      opts: ["It is destroyed", "It can move to a higher energy state", "It changes charge", "It leaves the atom permanently"], a: 1 },
    { q: "What may be released when an excited electron returns to a lower energy state?",
      opts: ["A proton", "A neutron", "A photon", "An orbital"], a: 2 },
    { q: "Which model describes electron locations using probability distributions?",
      opts: ["Bohr model", "Schrödinger / quantum mechanical model", "Dalton model", "Plum-pudding model"], a: 1 }
  ];

  function initQuiz() {
    renderQuestion();
  }

  function renderQuestion() {
    const body = $("#quizBody");
    const i = state.quizProgress;
    $("#quizCounter").textContent = "QUESTION " + String(i + 1).padStart(2, "0") + " / 06";
    $("#quizBar").style.width = (i / quizData.length * 100) + "%";

    if (i >= quizData.length) return renderResult();

    const item = quizData[i];
    body.innerHTML = `
      <p class="quiz-question">${item.q}</p>
      <div class="quiz-options">${item.opts.map((o, idx) =>
        `<button class="quiz-opt" data-idx="${idx}">${o}</button>`).join("")}</div>
      <div class="quiz-feedback" id="quizFeedback"></div>`;

    const feedback = $("#quizFeedback");
    $$("#quizBody .quiz-opt").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.idx, 10);
        $$("#quizBody .quiz-opt").forEach((b) => (b.disabled = true));
        if (idx === item.a) {
          btn.classList.add("correct");
          feedback.textContent = "Correct.";
          feedback.className = "quiz-feedback right";
          state.quizScore++;
        } else {
          btn.classList.add("wrong");
          $$("#quizBody .quiz-opt")[item.a].classList.add("correct");
          feedback.textContent = "Correct answer: " + item.opts[item.a] + ".";
          feedback.className = "quiz-feedback wrongf";
        }
        const next = document.createElement("button");
        next.className = "btn btn-primary";
        next.textContent = i === quizData.length - 1 ? "COMPLETE REVIEW" : "NEXT QUESTION";
        next.addEventListener("click", () => { state.quizProgress++; renderQuestion(); });
        body.appendChild(next);
      });
    });
  }

  function renderResult() {
    $("#quizBar").style.width = "100%";
    $("#quizCounter").textContent = "REVIEW COMPLETE";
    $("#quizBody").innerHTML = `
      <div class="quiz-result">
        <div class="qr-title">OPERATIONS REVIEW COMPLETE</div>
        <div class="qr-score">${state.quizScore} / ${quizData.length}</div>
        <div class="qr-sub">${state.quizScore} of ${quizData.length} concepts verified</div>
        <div style="margin-top:20px"><button class="btn btn-ghost" id="quizRetry">RESTART CERTIFICATION</button></div>
      </div>`;
    $("#quizRetry").addEventListener("click", () => {
      state.quizProgress = 0; state.quizScore = 0; renderQuestion();
    });
  }

  /* ============================================================
     GLOSSARY + REQUIREMENT STATUS
     ============================================================ */
  const glossary = [
    { term: "Electron", chem: "A negatively charged subatomic particle occupying the region around the nucleus.", analogy: "A passenger.", limit: "Electrons are not people and do not move independently by choice." },
    { term: "Electron configuration", chem: "The distribution of electrons across shells, subshells and orbitals, e.g. 1s² 2s² 2p⁴.", analogy: "The full gate-assignment manifest.", limit: "It describes probabilities and energies, not seat positions." },
    { term: "Bohr's Atomic Model", chem: "Model in which electrons occupy fixed, quantized circular energy levels around the nucleus.", analogy: "Legacy navigation with fixed flight rings.", limit: "It does not fully describe multi-electron atoms." },
    { term: "Schrödinger's Atomic Model", chem: "Quantum model describing electrons by wavefunctions and probability distributions.", analogy: "Quantum radar showing probability density.", limit: "The density is a probability map, not a physical cloud." },
    { term: "Electron shell / Energy level", chem: "A principal energy region described by the quantum number n.", analogy: "A terminal.", limit: "A shell is an energy level, not a building." },
    { term: "Subshell", chem: "A division within an energy level labelled s, p, d or f.", analogy: "A concourse.", limit: "A concourse is an organisational grouping, not a corridor." },
    { term: "Orbital", chem: "A region described by a wavefunction where an electron has a high probability of being found.", analogy: "A gate.", limit: "A real orbital is not a physical gate or fixed location." },
    { term: "Hund's Rule", chem: "Electrons occupy equal-energy orbitals singly with parallel spins before pairing.", analogy: "Board empty gates before doubling up.", limit: "It is a consequence of energy and spin, not a queue policy." },
    { term: "Pauli Exclusion Principle", chem: "No orbital holds more than two electrons, and paired electrons must have opposite spins.", analogy: "Security limit of two per gate, opposite spins only.", limit: "It is a fundamental quantum rule, not a staffing decision." },
    { term: "Aufbau Principle", chem: "Electrons occupy lower-energy orbitals before higher-energy orbitals.", analogy: "Automatic boarding fills nearer terminals first.", limit: "Some real atoms show exceptions due to subtle energy effects." },
    { term: "Ground state", chem: "The lowest-energy arrangement of an atom's electrons.", analogy: "All passengers at their standard assigned gates.", limit: "It is an energy configuration, not a physical resting place." },
    { term: "Excited state", chem: "A higher-energy arrangement after an electron absorbs energy.", analogy: "A passenger temporarily moved to a higher terminal.", limit: "The electron does not physically travel a visible route." },
    { term: "Absorption", chem: "When an electron absorbs energy and moves to a higher-energy state.", analogy: "Energy input clears an electron to a higher terminal.", limit: "Energy is quantized; only specific amounts are absorbed." },
    { term: "Emission", chem: "When an electron drops to a lower-energy state, releasing energy as a photon.", analogy: "A photon departs as the electron returns.", limit: "The photon is light, not an aircraft." },
    { term: "Wavelength", chem: "The distance between successive wave peaks; inversely related to frequency and energy.", analogy: "Photon flight band on the spectrum strip.", limit: "Longer wavelength means lower energy — E = hc/λ." },
    { term: "Energy", chem: "The capacity associated with an electron's state or a photon, E = hν = hc/λ.", analogy: "The vertical scale of the terminal map.", limit: "Energy levels are quantized, not continuous heights." }
  ];

  function initGlossary() {
    const grid = $("#glossaryGrid");
    grid.innerHTML = glossary.map((g) => `
      <div class="glossary-item" data-term="${g.term.toLowerCase()}">
        <div class="gi-term">${g.term.toUpperCase()}</div>
        <div class="gi-block"><span class="gi-label">CHEMISTRY</span><span class="gi-text">${g.chem}</span></div>
        <div class="gi-block gi-analogy"><span class="gi-label">AIRPORT ANALOGY</span><span class="gi-text">${g.analogy}</span></div>
        <div class="gi-block gi-limit"><span class="gi-label">LIMITATION</span><span class="gi-text">${g.limit}</span></div>
      </div>`).join("");

    const search = $("#glossarySearch");
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      $$(".glossary-item").forEach((item) => {
        const match = item.dataset.term.includes(q) || item.textContent.toLowerCase().includes(q);
        item.classList.toggle("hidden", !!q && !match);
      });
    });
  }

  function initReqStatus() {
    const items = [
      "Electron", "Electron configuration", "Bohr Model", "Schrödinger Model",
      "Energy levels", "Subshells", "Orbitals", "Hund's Rule", "Pauli Principle",
      "Aufbau Principle", "Ground state", "Excited state", "Absorption",
      "Emission", "Wavelength", "Energy"
    ];
    $("#reqList").innerHTML = items.map((i) =>
      `<li><span>${i}</span><span class="req-check">\u2713</span></li>`).join("");
  }

  /* ============================================================
     ATOMIC SIMULATOR ENGINE (H–Ca)
     Central data model + configuration engine. Everything in the
     new simulator sections derives from `atom`. Scientific values
     are computed algorithmically, not hardcoded per element.
     ============================================================ */

  // superscript / subscript digit maps for formatting
  const SUP_DIGITS = { "0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3", "4": "\u2074",
    "5": "\u2075", "6": "\u2076", "7": "\u2077", "8": "\u2078", "9": "\u2079" };
  function toSuper(n) { return String(n).split("").map((d) => SUP_DIGITS[d] || d).join(""); }
  function toSubP(n) { return SUP[n] || String(n); } // subshell orbital index subscript (₁₂₃)

  // Aufbau filling order with quantum data. capacity = orbitals * 2.
  // l: s=0 p=1 d=2 f=3
  const ORBITAL_ORDER = [
    { name: "1s", n: 1, type: "s", l: 0, orbitals: 1, capacity: 2 },
    { name: "2s", n: 2, type: "s", l: 0, orbitals: 1, capacity: 2 },
    { name: "2p", n: 2, type: "p", l: 1, orbitals: 3, capacity: 6 },
    { name: "3s", n: 3, type: "s", l: 0, orbitals: 1, capacity: 2 },
    { name: "3p", n: 3, type: "p", l: 1, orbitals: 3, capacity: 6 },
    { name: "4s", n: 4, type: "s", l: 0, orbitals: 1, capacity: 2 },
    { name: "3d", n: 3, type: "d", l: 2, orbitals: 5, capacity: 10 },
    { name: "4p", n: 4, type: "p", l: 1, orbitals: 3, capacity: 6 },
    { name: "5s", n: 5, type: "s", l: 0, orbitals: 1, capacity: 2 },
    { name: "4d", n: 4, type: "d", l: 2, orbitals: 5, capacity: 10 },
    { name: "5p", n: 5, type: "p", l: 1, orbitals: 3, capacity: 6 },
    { name: "6s", n: 6, type: "s", l: 0, orbitals: 1, capacity: 2 },
    { name: "4f", n: 4, type: "f", l: 3, orbitals: 7, capacity: 14 },
    { name: "5d", n: 5, type: "d", l: 2, orbitals: 5, capacity: 10 },
    { name: "6p", n: 6, type: "p", l: 1, orbitals: 3, capacity: 6 },
    { name: "7s", n: 7, type: "s", l: 0, orbitals: 1, capacity: 2 }
  ];

  // Element table (H–Ca). period/group for main-group + transition context.
  const ELEMENTS = [
    { z: 1,  sym: "H",  name: "Hydrogen",  period: 1, group: 1 },
    { z: 2,  sym: "He", name: "Helium",    period: 1, group: 18 },
    { z: 3,  sym: "Li", name: "Lithium",   period: 2, group: 1 },
    { z: 4,  sym: "Be", name: "Beryllium", period: 2, group: 2 },
    { z: 5,  sym: "B",  name: "Boron",     period: 2, group: 13 },
    { z: 6,  sym: "C",  name: "Carbon",    period: 2, group: 14 },
    { z: 7,  sym: "N",  name: "Nitrogen",  period: 2, group: 15 },
    { z: 8,  sym: "O",  name: "Oxygen",    period: 2, group: 16 },
    { z: 9,  sym: "F",  name: "Fluorine",  period: 2, group: 17 },
    { z: 10, sym: "Ne", name: "Neon",      period: 2, group: 18 },
    { z: 11, sym: "Na", name: "Sodium",    period: 3, group: 1 },
    { z: 12, sym: "Mg", name: "Magnesium", period: 3, group: 2 },
    { z: 13, sym: "Al", name: "Aluminium", period: 3, group: 13 },
    { z: 14, sym: "Si", name: "Silicon",   period: 3, group: 14 },
    { z: 15, sym: "P",  name: "Phosphorus",period: 3, group: 15 },
    { z: 16, sym: "S",  name: "Sulfur",    period: 3, group: 16 },
    { z: 17, sym: "Cl", name: "Chlorine",  period: 3, group: 17 },
    { z: 18, sym: "Ar", name: "Argon",     period: 3, group: 18 },
    { z: 19, sym: "K",  name: "Potassium", period: 4, group: 1 },
    { z: 20, sym: "Ca", name: "Calcium",   period: 4, group: 2 }
  ];
  const ELEMENT_BY_SYM = {};
  ELEMENTS.forEach((e) => (ELEMENT_BY_SYM[e.sym] = e));

  /* --- configuration engine --- */
  // Fill `electronCount` electrons into ORBITAL_ORDER; return array of
  // { name, n, type, l, count } for occupied subshells (in fill order).
  function generateElectronConfiguration(electronCount) {
    let remaining = electronCount;
    const config = [];
    for (const o of ORBITAL_ORDER) {
      if (remaining <= 0) break;
      const count = Math.min(o.capacity, remaining);
      config.push({ name: o.name, n: o.n, type: o.type, l: o.l, orbitals: o.orbitals, capacity: o.capacity, count });
      remaining -= count;
    }
    return config;
  }

  // For display, configuration is conventionally ordered by n then l.
  function displayOrderedConfig(config) {
    return config.slice().sort((a, b) => (a.n - b.n) || (a.l - b.l));
  }

  function formatConfiguration(config, opts) {
    const superscript = !opts || opts.superscript !== false;
    return displayOrderedConfig(config).map((c) =>
      c.name + (superscript ? toSuper(c.count) : String(c.count))).join(" ");
  }

  // Shell distribution: electrons per principal level n (ordered by n).
  function calculateShellDistribution(config) {
    const byN = {};
    config.forEach((c) => { byN[c.n] = (byN[c.n] || 0) + c.count; });
    return Object.keys(byN).map(Number).sort((a, b) => a - b).map((n) => ({ n, count: byN[n] }));
  }

  // Valence: electrons in the highest occupied principal energy level.
  // (For main-group atoms through Ca this matches chemical valence count.)
  function calculateValenceElectrons(config) {
    const shells = calculateShellDistribution(config);
    if (!shells.length) return { n: 0, count: 0 };
    const highestN = Math.max.apply(null, shells.map((s) => s.n));
    const count = shells.filter((s) => s.n === highestN).reduce((a, s) => a + s.count, 0);
    return { n: highestN, count };
  }

  function calculateOccupiedShells(config) {
    return calculateShellDistribution(config).length;
  }

  function calculateOccupiedOrbitals(config) {
    // An orbital is "occupied" if it holds 1 or 2 electrons. With Hund's
    // rule, count<=orbitals means `count` singly-occupied orbitals; beyond
    // that every orbital is occupied (some now paired).
    let occ = 0;
    config.forEach((c) => { occ += c.count <= c.orbitals ? c.count : c.orbitals; });
    return occ;
  }

  // Per-electron records with quantum numbers, consistent with Hund's rule.
  // Returns [{ id, n, l, ml, ms, subshell, orbitalIndex, spin, type }]
  function buildElectronRecords(config) {
    const records = [];
    let idx = 0;
    displayOrderedConfig(config).forEach((c) => {
      // ml values for this subshell: -l .. +l  (orbital slots, left to right)
      const mlValues = [];
      for (let m = -c.l; m <= c.l; m++) mlValues.push(m);
      // Hund: fill each orbital singly (spin up) first, then pair (spin down).
      const filled = []; // { ml, ms, orbitalIndex }
      let count = c.count;
      for (let i = 0; i < mlValues.length && count > 0; i++) { filled.push({ ml: mlValues[i], ms: +0.5, orbitalIndex: i }); count--; }
      for (let i = 0; i < mlValues.length && count > 0; i++) { filled.push({ ml: mlValues[i], ms: -0.5, orbitalIndex: i }); count--; }
      filled.forEach((f) => {
        idx++;
        records.push({
          id: "E-" + String(idx).padStart(2, "0"),
          n: c.n, l: c.l, ml: f.ml, ms: f.ms,
          subshell: c.name, type: c.type,
          orbitalIndex: f.orbitalIndex,
          spin: f.ms > 0 ? UP : DOWN
        });
      });
    });
    return records;
  }

  /* --- central atom state (source of truth for simulator sections) --- */
  const atom = {
    symbol: "O",
    z: 8,
    protons: 8,
    electronCount: 8,
    get charge() { return this.protons - this.electronCount; },
    config: [],
    records: []
  };

  // Map a quantum record (from atom.records) into the legacy electron shape
  // that the original airport modules consume:
  //   { id, terminal, section, gate, orbital, spin, energy }
  function recordToLegacy(r) {
    const secLetter = r.type; // s | p | d | f
    // orbital display: s -> "2s"; p/d/f -> "2p₁" style using orbitalIndex+1
    let orbital = r.subshell;
    if (secLetter !== "s") orbital = r.subshell + (SUP[r.orbitalIndex + 1] || (r.orbitalIndex + 1));
    // gate label: s -> "S01"; others -> "P01"/"D01"... by orbital index
    const gate = secLetter.toUpperCase() + "0" + (r.orbitalIndex + 1);
    // energy tier from principal level
    const energy = r.n === 1 ? "LOW" : (r.n === 2 ? "MED" : "HIGH");
    return {
      id: r.id,
      terminal: r.n,
      section: secLetter,
      gate: gate,
      orbital: orbital,
      spin: r.spin,
      energy: energy
    };
  }

  function rebuildLegacyElectrons() {
    electrons = atom.records.map(recordToLegacy);
  }

  function recomputeAtom() {
    atom.config = generateElectronConfiguration(atom.electronCount);
    atom.records = buildElectronRecords(atom.config);
    rebuildLegacyElectrons();
  }

  function ionNotation(sym, charge) {
    if (charge === 0) return sym;
    const mag = Math.abs(charge);
    const sign = charge > 0 ? "\u207a" : "\u207b"; // ⁺ / ⁻
    const magText = mag === 1 ? "" : toSuper(mag);
    return sym + magText + sign;
  }

  function chargeStateLabel(charge) {
    if (charge === 0) return "NEUTRAL ATOM";
    return charge > 0 ? "CATION" : "ANION";
  }

  // Simulator-wide sync registry (separate from legacy syncHandlers,
  // but we also fire legacy syncAll so existing modules stay updated).
  const atomSyncHandlers = [];
  function onAtomSync(fn) { atomSyncHandlers.push(fn); }
  function atomSyncAll() {
    atomSyncHandlers.forEach((fn) => { try { fn(); } catch (err) { console.error("[atomSync]", err); } });
  }

  // Load an element by symbol: reset electrons to neutral, recompute, sync.
  function loadElement(sym) {
    const el = ELEMENT_BY_SYM[sym];
    if (!el) return;
    const prevSym = atom.symbol;
    atom.symbol = el.sym;
    atom.z = el.z;
    atom.protons = el.z;
    atom.electronCount = el.z; // neutral on load
    // reset the (Oxygen-specific) excitation demo whenever the atom changes
    state.energyState = "ground";
    recomputeAtom();
    ensureValidSelection();
    if (prevSym !== el.sym) {
      announce("Atomic destination changed: " + (ELEMENT_BY_SYM[prevSym] ? ELEMENT_BY_SYM[prevSym].name.toUpperCase() : prevSym) + " → " + el.name.toUpperCase() + ".");
    }
    logEvent("SYSTEM", el.name + " loaded.");
    syncEntireEIA();
  }

  /* ------------------------------------------------------------
     ONE canonical synchronization sequence. Called after any change
     to the atom, ion charge, or excitation demo. Rebuilds every
     dynamic module from the central atom state. Guarded so a missing
     section never throws.
     ------------------------------------------------------------ */
  // Keep the current passenger selection valid for the current atom/ion.
  function ensureValidSelection() {
    if (!electrons.some((e) => e.id === state.selectedElectron)) {
      state.selectedElectron = electrons.length ? electrons[0].id : null;
    }
  }

  function syncEntireEIA() {
    // legacy airport modules (now derived from atom.records)
    safe(renderElectronTable);
    safe(renderManifest);
    safe(renderEntrance);
    safe(renderFooter);
    safe(updateMapOccupancy);
    // re-render the selected passenger's record + pass, or clear if none
    if (state.selectedElectron && electrons.some((e) => e.id === state.selectedElectron)) {
      const e = electrons.find((x) => x.id === state.selectedElectron);
      safe(() => renderRecord(e));
      safe(() => renderBoardingPass(e));
      safe(() => {
        $$("#electronTable tbody tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.id === state.selectedElectron));
        $$(".manifest-row").forEach((r) => r.classList.toggle("selected", r.dataset.id === state.selectedElectron));
      });
    }
    // fire both registries so every subscribed module updates
    syncAll();
    atomSyncAll();
  }
  function safe(fn) { try { fn(); } catch (err) { console.error("[sync]", err); } }

  function currentElement() { return ELEMENT_BY_SYM[atom.symbol]; }

  function facilityCode() { return "EIA-ATOM-" + String(atom.protons).padStart(3, "0"); }
  function stateWord() {
    return (state.energyState === "excited" && atom.symbol === "O") ? "EXCITED STATE" : "GROUND STATE";
  }

  /* Dynamic entrance stats — reflect the currently loaded atom + ion. */
  function renderEntrance() {
    const e = currentElement();
    const atomStr = e.name.toUpperCase() + " / " + e.sym;
    const set = (id, v) => { const el = $("#" + id); if (el) el.textContent = v; };
    const atomEl = $("#entranceAtom");
    if (atomEl) { atomEl.textContent = atomStr; atomEl.dataset.flip = atomStr; }
    set("entranceZ", String(atom.protons).padStart(2, "0"));
    set("entrancePassengers", String(atom.electronCount).padStart(2, "0") + " ELECTRONS");
    set("entranceConfig", formatConfiguration(atom.config));
    set("entranceFacility", facilityCode());
    const stEl = $("#entranceState");
    const word = stateWord();
    if (stEl) { stEl.textContent = word; stEl.dataset.state = word === "EXCITED STATE" ? "excited" : "ground"; }
    const navState = $("#navState");
    if (navState) { navState.textContent = word; navState.dataset.state = word === "EXCITED STATE" ? "excited" : "ground"; }
  }

  /* Dynamic footer — reflect current atom + state. */
  function renderFooter() {
    const e = currentElement();
    const fac = $("#footerFacility");
    if (fac) fac.textContent = facilityCode();
    const st = $("#footerState");
    if (st) st.textContent = e.name.toUpperCase() + " / " + e.sym + " · " + stateWord();
  }

  /* ============================================================
     ANNOUNCEMENT BAR + EVENT LOG (control tower infrastructure)
     ============================================================ */
  const announceQueue = [];
  let announceBusy = false;
  function announce(msg, kind) {
    announceQueue.push({ msg, kind: kind || "ANNOUNCEMENT" });
    if (!announceBusy) drainAnnounce();
  }
  function drainAnnounce() {
    const bar = $("#announceBar");
    if (!bar) { announceQueue.length = 0; announceBusy = false; return; }
    if (!announceQueue.length) { announceBusy = false; return; }
    announceBusy = true;
    const { msg, kind } = announceQueue.shift();
    const label = kind === "SECURITY" ? "EIA SECURITY" : "EIA ANNOUNCEMENT";
    bar.dataset.kind = kind.toLowerCase();
    bar.innerHTML = `<span class="ann-label">${label}</span><span class="ann-msg">${msg}</span>`;
    bar.classList.remove("show"); void bar.offsetWidth; bar.classList.add("show");
    const hold = reduceMotion ? 1600 : 3600;
    setTimeout(() => {
      bar.classList.remove("show");
      setTimeout(drainAnnounce, reduceMotion ? 30 : 350);
    }, hold);
  }

  const eventLog = []; // newest last; cap ~50
  function logEvent(cat, msg) {
    const d = new Date();
    const time = [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
    eventLog.push({ time, cat, msg });
    while (eventLog.length > 50) eventLog.shift();
    renderEventLog();
  }
  function renderEventLog() {
    const list = $("#towerLog");
    if (!list) return;
    if (!eventLog.length) { list.innerHTML = `<li class="muted">No events logged.</li>`; return; }
    list.innerHTML = eventLog.slice().reverse().map((e, i) =>
      `<li class="${i === 0 ? "new" : ""}"><span class="tl-time mono">${e.time}</span><span class="tl-cat">${e.cat}</span><span class="tl-msg">${e.msg}</span></li>`).join("");
  }

  /* ============================================================
     SHARED HELPERS FOR IMMERSIVE-AIRPORT MODULES
     ============================================================ */
  // Fill a <select> with the 8 passengers.
  function fillPassengerSelect(sel, selected) {
    if (!sel) return;
    sel.innerHTML = electrons.map((e) =>
      `<option value="${e.id}"${e.id === selected ? " selected" : ""}>${e.id}</option>`).join("");
  }
  // Human subshell label from display state (e.g. 2p, 3s).
  function subshellLabel(d) { return d.subshell; }
  // Section letter for the boarding-group ("S" / "P").
  function sectionLetter(d) { return d.section.toUpperCase(); }

  /* ============================================================
     initBooking() — flight booking + itinerary + energy upgrade
     ============================================================ */
  function initBooking() {
    const passSel = $("#bookPassenger");
    fillPassengerSelect(passSel, "E-07");
    const readout = $("#bookingReadout");
    const itin = $("#itinerary");
    const upgradePanel = $("#upgradePanel");
    const upgradeBtn = $("#bookUpgrade");

    function selected() { return passSel.value || "E-07"; }

    function refillSelect() {
      const keep = passSel.value;
      fillPassengerSelect(passSel, electrons.some((e) => e.id === keep) ? keep : (electrons[0] && electrons[0].id));
    }

    function renderReadout() {
      const d = getElectronDisplayState(selected());
      if (!d) { readout.innerHTML = ""; upgradeBtn.hidden = true; return; }
      readout.innerHTML = `
        <div class="br-row"><span class="br-k">PASSENGER</span><span class="br-v mono">${d.id}</span></div>
        <div class="br-row"><span class="br-k">CURRENT TERMINAL</span><span class="br-v mono">T${d.terminal}</span></div>
        <div class="br-row"><span class="br-k">CONCOURSE</span><span class="br-v mono">${sectionLetter(d)}</span></div>
        <div class="br-row"><span class="br-k">GATE</span><span class="br-v mono">${d.gate}</span></div>
        <div class="br-row"><span class="br-k">ORBITAL</span><span class="br-v mono">${d.orbital}</span></div>
        <div class="br-row"><span class="br-k">STATUS</span><span class="br-v" style="color:${d.stateKey === 'excited' ? 'var(--amber)' : 'var(--green)'}">${d.stateKey === 'excited' ? 'UPGRADED' : 'CONFIRMED'}</span></div>`;
      // energy upgrade is an Oxygen-only demonstration for E-08
      upgradeBtn.hidden = !(atom.symbol === "O" && d.id === "E-08");
    }

    function renderItinerary() {
      const d = getElectronDisplayState(selected());
      if (!d) return;
      const el = currentElement();
      itin.hidden = false;
      itin.innerHTML = `
        <div class="itinerary-head">EIA ELECTRON AIRWAYS · ITINERARY</div>
        <div class="itinerary-grid">
          <div class="iti-cell"><span class="iti-k">PASSENGER</span><span class="iti-v">${d.id}</span></div>
          <div class="iti-cell"><span class="iti-k">ATOM</span><span class="iti-v">${el.sym} / ${String(atom.protons).padStart(2, "0")}</span></div>
          <div class="iti-cell"><span class="iti-k">FROM</span><span class="iti-v" style="font-size:11px">NUCLEUS SYSTEM</span></div>
          <div class="iti-cell"><span class="iti-k">TERMINAL</span><span class="iti-v">T${d.terminal}</span></div>
          <div class="iti-cell"><span class="iti-k">CONCOURSE</span><span class="iti-v">${sectionLetter(d)}</span></div>
          <div class="iti-cell"><span class="iti-k">GATE</span><span class="iti-v">${d.gate}</span></div>
          <div class="iti-cell"><span class="iti-k">ORBITAL</span><span class="iti-v">${d.orbital}</span></div>
          <div class="iti-cell"><span class="iti-k">SPIN</span><span class="iti-v">${d.spin}</span></div>
          <div class="iti-cell"><span class="iti-k">CONFIG STATE</span><span class="iti-v" style="color:${d.stateKey === 'excited' ? 'var(--amber)' : 'var(--green)'};font-size:12px">${d.stateText.toUpperCase()}</span></div>
        </div>
        <p class="itinerary-note">This itinerary describes where the electron belongs in the configuration. It is not literal physical movement.</p>`;
    }

    function renderUpgrade() {
      upgradePanel.hidden = false;
      const excited = state.energyState === "excited";
      if (excited) {
        upgradePanel.innerHTML = `
          <div class="up-title">ENERGY TRANSFER · ACTIVE</div>
          <div class="up-row"><span class="up-k">PASSENGER</span><span class="up-v">E-08</span></div>
          <div class="up-row"><span class="up-k">CURRENT</span><span class="up-v">3s</span></div>
          <div class="up-row"><span class="up-k">STATE</span><span class="up-v" style="color:var(--amber)">EXCITED</span></div>
          <p class="itinerary-note" style="padding-left:0">E-08 is in an excited state. Return it to emit a photon and restore the ground state.</p>
          <button class="btn btn-amber" id="upgReturn">RETURN TO LOWER ENERGY</button>`;
        $("#upgReturn").addEventListener("click", () => { $("#emitBtn").click(); });
      } else {
        upgradePanel.innerHTML = `
          <div class="up-title">ENERGY TRANSFER REQUEST</div>
          <div class="up-row"><span class="up-k">PASSENGER</span><span class="up-v">E-08</span></div>
          <div class="up-row"><span class="up-k">CURRENT</span><span class="up-v">2p${SUP[1]}</span></div>
          <div class="up-row"><span class="up-k">REQUESTED</span><span class="up-v">3s</span></div>
          <div class="up-row"><span class="up-k">ENERGY REQUIRED</span><span class="up-v" style="font-size:11px">HIGHER THAN CURRENT</span></div>
          <p class="itinerary-note" style="padding-left:0">Absorption is quantized — the electron moves to a higher allowed energy state. It does not physically fly.</p>
          <button class="btn btn-primary" id="upgConfirm">CONFIRM ENERGY ABSORPTION</button>`;
        $("#upgConfirm").addEventListener("click", () => {
          pushTimeline("E-08 selected");
          $("#absorbBtn").click();
        });
      }
    }

    passSel.addEventListener("change", () => { renderReadout(); itin.hidden = true; upgradePanel.hidden = true; });
    $("#bookItinerary").addEventListener("click", renderItinerary);
    upgradeBtn.addEventListener("click", renderUpgrade);

    // keep booking in sync when atom / ion / excitation changes
    function syncBooking() {
      refillSelect();
      renderReadout();
      if (!itin.hidden) renderItinerary();
      if (!upgradePanel.hidden && atom.symbol === "O" && selected() === "E-08") renderUpgrade();
      else upgradePanel.hidden = true;
    }
    onSync(syncBooking);
    onAtomSync(syncBooking);
    renderReadout();
  }

  /* ============================================================
     initAirportMap() — schematic overview + clickable wayfinding
     ============================================================ */
  const orbitalToGateNode = {
    "1s": "1s", "2s": "2s", "3s": "3s",
    ["2p" + SUP[1]]: "2p1", ["2p" + SUP[2]]: "2p2", ["2p" + SUP[3]]: "2p3"
  };

  function normalizeOrbital(orb) {
    // "2p₁" -> "2p1", "3p₃" -> "3p3"
    return orb.replace(SUP[1], "1").replace(SUP[2], "2").replace(SUP[3], "3");
  }
  function gateOccupantsByNode(nodeGate) {
    // nodeGate like 1s, 2s, 2p1, 3s, 3p1, 4s...
    return electrons.filter((e) => {
      const d = getElectronDisplayState(e);
      const node = orbitalToGateNode[d.orbital] || normalizeOrbital(d.orbital);
      return node === nodeGate;
    });
  }

  function initAirportMap() {
    const svg = $("#wayfindingSvg");
    const panel = $("#airportMapBody");
    const tree = $("#nodeTree");

    function setPanel(title, code, body, limit) {
      panel.innerHTML = `<h4>${title}</h4>
        <div class="r-code">${code}</div>
        <p>${body}</p>
        ${limit ? `<div class="r-limit">${limit}</div>` : ""}`;
    }

    function clearMapHighlights() {
      $$(".wf-terminal", svg).forEach((t) => t.classList.remove("focus"));
      $$(".wf-gate", svg).forEach((g) => g.classList.remove("focus"));
      $("#routeLayer", svg).innerHTML = "";
    }

    // terminal boxes
    $$(".wf-terminal", svg).forEach((t) => {
      const box = t.querySelector(".wf-term-box");
      box.addEventListener("click", () => {
        clearMapHighlights();
        t.classList.add("focus");
        const n = t.dataset.nodeTerminal;
        setPanel("TERMINAL " + n, "SHELL n = " + n,
          "Principal energy level n = " + n + ". Higher n means higher average energy and greater average distance from the nucleus.",
          "A shell is a quantized energy level, not a physical building.");
      });
    });

    // gates
    $$(".wf-gate", svg).forEach((g) => {
      g.addEventListener("click", () => {
        clearMapHighlights();
        g.classList.add("focus");
        const node = g.dataset.nodeGate;
        const occ = gateOccupantsByNode(node);
        const orbLabel = node.replace(/2p(\d)/, (m, d) => "2p" + SUP[d]).replace(/3p(\d)/, (m, d) => "3p" + SUP[d]);
        setPanel("GATE " + orbLabel, "ORBITAL " + orbLabel,
          "An orbital holds up to two electrons with opposite spins. " +
          (occ.length ? "Assigned: " + occ.map((o) => o.id + " (" + getElectronDisplayState(o).spin + ")").join(", ") + "."
                      : "Unoccupied in oxygen's ground state."),
          "An orbital is a probability distribution, not a physical room.");
        openGateScreen(node, orbLabel, occ);
      });
    });

    // gate screen close
    $("#gateScreenClose").addEventListener("click", closeGateScreen);

    // core node
    const core = $(".wf-core", svg);
    if (core) core.addEventListener("click", () => {
      clearMapHighlights();
      setPanel("NUCLEUS / CORE", "+8 PROTONS",
        "The central core holds the protons and neutrons. Its positive charge binds the electrons across the terminals.",
        "The nucleus is the atom's centre — it is not an airport building.");
    });

    // schematic overview nodes -> focus terminals + build tree
    $$("[data-focus]", $("#schematic")).forEach((node) => {
      node.addEventListener("click", () => {
        const f = node.dataset.focus;
        clearMapHighlights();
        if (f === "core") {
          if (core) core.dispatchEvent(new Event("click"));
          tree.innerHTML = "";
          return;
        }
        const term = $(`.wf-terminal[data-node-terminal="${f}"]`, svg);
        if (term) { term.classList.add("focus"); term.querySelector(".wf-term-box").dispatchEvent(new Event("click")); }
        // build ascii-ish branch tree
        const trees = {
          "1": "T1\n└── S\n    └── S01 · 1s",
          "2": "T2\n├── S\n│   └── S01 · 2s\n└── P\n    ├── P01 · 2p" + SUP[1] + "\n    ├── P02 · 2p" + SUP[2] + "\n    └── P03 · 2p" + SUP[3],
          "3": "T3\n├── S\n│   └── S01 · 3s\n└── P\n    ├── P01\n    ├── P02\n    └── P03"
        };
        tree.textContent = trees[f] || "";
      });
    });

    onSync(() => { /* map occupancy is read live on click; nothing persistent to repaint */ });
  }

  /* boarding gate screen (airport monitor) */
  function openGateScreen(node, orbLabel, occ) {
    const drawer = $("#gateScreen");
    const body = $("#gateScreenBody");
    const cap = occ.length;
    const excited = occ.some((o) => getElectronDisplayState(o).stateKey === "excited");
    const status = cap >= 2 ? "BOARDING COMPLETE" : (cap === 1 ? "BOARDING" : "AWAITING PASSENGERS");
    const rule = cap >= 2 ? "PAULI VERIFIED · opposite spins" : (cap === 1 ? "SINGLE OCCUPANT" : "—");
    body.innerHTML = `
      <div class="gs-monitor">
        <div class="gsm-head"><span class="gsm-eia">EIA</span><span class="gsm-gate">GATE ${orbLabel}</span></div>
        <div class="gsm-row"><span class="gsm-k">DESTINATION</span><span class="gsm-v">${orbLabel} ORBITAL</span></div>
        <div class="gsm-row"><span class="gsm-k">BOARDING GROUP</span><span class="gsm-v">OXYGEN</span></div>
        <div class="gsm-row"><span class="gsm-k">PASSENGERS</span><span class="gsm-v">${occ.length ? occ.map((o) => o.id + " " + getElectronDisplayState(o).spin).join(" · ") : "—"}</span></div>
        <div class="gsm-row"><span class="gsm-k">CAPACITY</span><span class="gsm-v">${cap} / 2</span></div>
        <div class="gsm-row"><span class="gsm-k">STATUS</span><span class="gsm-v ${excited ? "amber" : "ok"}">${status}</span></div>
        <div class="gsm-row"><span class="gsm-k">RULE CHECK</span><span class="gsm-v ok">${rule}</span></div>
      </div>`;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $("#drawerScrim").hidden = false;
  }
  function closeGateScreen() {
    $("#gateScreen").classList.remove("open");
    $("#gateScreen").setAttribute("aria-hidden", "true");
    if (!$("#opsDrawer").classList.contains("open")) $("#drawerScrim").hidden = true;
  }

  /* animated wayfinding route from core to a target gate node */
  function drawRoute(nodeGate) {
    const svg = $("#wayfindingSvg");
    const layer = $("#routeLayer", svg);
    const gate = $(`.wf-gate[data-node-gate="${nodeGate}"]`, svg);
    if (!gate || !layer) return;
    $$(".wf-terminal", svg).forEach((t) => t.classList.remove("focus"));
    $$(".wf-gate", svg).forEach((g) => g.classList.remove("focus"));
    gate.classList.add("focus");
    gate.closest(".wf-terminal").classList.add("focus");
    const gx = parseFloat(gate.getAttribute("x")) + parseFloat(gate.getAttribute("width")) / 2;
    const gy = parseFloat(gate.getAttribute("y")) + parseFloat(gate.getAttribute("height")) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const midX = 110;
    path.setAttribute("d", `M 86 210 L ${midX} 210 L ${midX} ${gy} L ${gx} ${gy}`);
    path.setAttribute("class", "wf-route");
    layer.appendChild(path);
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("r", "5"); dot.setAttribute("class", "wf-route-dot");
    layer.appendChild(dot);
    if (!reduceMotion) {
      const len = path.getTotalLength();
      path.style.strokeDasharray = len; path.style.strokeDashoffset = len;
      path.getBoundingClientRect();
      path.style.transition = "stroke-dashoffset 0.9s ease";
      path.style.strokeDashoffset = "0";
      let t0 = null;
      function move(ts) {
        if (!t0) t0 = ts;
        const p = Math.min(1, (ts - t0) / 900);
        const pt = path.getPointAtLength(len * p);
        dot.setAttribute("cx", pt.x); dot.setAttribute("cy", pt.y);
        if (p < 1) requestAnimationFrame(move);
      }
      requestAnimationFrame(move);
    } else {
      dot.setAttribute("cx", gx); dot.setAttribute("cy", gy);
    }
  }

  /* ============================================================
     initGateFinder() — locate a passenger, draw route
     ============================================================ */
  function initGateFinder() {
    const sel = $("#finderSelect");
    fillPassengerSelect(sel, electrons[0] ? electrons[0].id : "E-01");
    const result = $("#finderResult");

    function refill() {
      const keep = sel.value;
      fillPassengerSelect(sel, electrons.some((e) => e.id === keep) ? keep : (electrons[0] && electrons[0].id));
    }

    function locate() {
      const d = getElectronDisplayState(sel.value || (electrons[0] && electrons[0].id));
      if (!d) return;
      // normalise orbital (2p₁ -> 2p1) for the wayfinding node id
      const norm = d.orbital.replace(SUP[1], "1").replace(SUP[2], "2").replace(SUP[3], "3");
      const node = orbitalToGateNode[d.orbital] || norm;
      result.innerHTML = `
        <div class="finder-head">PASSENGER ${d.id}</div>
        <div class="finder-grid">
          <div><span class="fd-k">TERMINAL</span> <span class="fd-v mono">T${d.terminal}</span></div>
          <div><span class="fd-k">CONCOURSE</span> <span class="fd-v mono">${sectionLetter(d)}</span></div>
          <div><span class="fd-k">GATE</span> <span class="fd-v mono">${d.gate}</span></div>
          <div><span class="fd-k">ORBITAL</span> <span class="fd-v mono">${d.orbital}</span></div>
          <div><span class="fd-k">SPIN</span> <span class="fd-v mono">${d.spin}</span></div>
        </div>
        <div class="fr-route">YOU ARE HERE · CENTRAL CORE → TERMINAL ${d.terminal} → ${sectionLetter(d)} CONCOURSE → GATE ${d.gate}</div>`;
      drawRoute(node);
      $("#airportmap").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "nearest" });
    }
    $("#finderGo").addEventListener("click", locate);
    onAtomSync(refill);
  }

  /* ============================================================
     initGateStatus() — live occupancy for every orbital gate
     ============================================================ */
  function initGateStatus() {
    const grid = $("#gateStatusGrid");
    if (!grid) return;

    function render() {
      // node id must match gateOccupantsByNode: s -> "2s", p -> "2p1"
      const gates = [];
      displayOrderedConfig(atom.config).forEach((c) => {
        for (let i = 0; i < c.orbitals; i++) {
          const node = c.type === "s" ? c.name : c.name + (i + 1);
          const label = c.type === "s" ? c.name : c.name + (SUP[i + 1] || (i + 1));
          gates.push({ node, label });
        }
      });
      grid.innerHTML = gates.map((g) => {
        const occ = gateOccupantsByNode(g.node);
        const count = occ.length;
        let statusText, statusCls;
        if (count >= 2) { statusText = "FULL"; statusCls = "full"; }
        else if (count === 1) { statusText = "AVAILABLE"; statusCls = "available"; }
        else { statusText = "EMPTY"; statusCls = "empty"; }
        const pax = occ.length
          ? occ.map((o) => {
              const spin = getElectronDisplayState(o).spin;
              const sc = spin === UP ? "spin-up" : "spin-down";
              return `<div class="gs-pass">${o.id} <span class="${sc}">${spin}</span></div>`;
            }).join("")
          : `<div class="gs-pass muted">—</div>`;
        return `<div class="gs-cell">
          <div class="gs-name">${g.label}</div>
          <div class="gs-occ">OCCUPANCY ${count} / 2</div>
          ${pax}
          <div class="gs-status ${statusCls}">${statusText}</div>
        </div>`;
      }).join("");
    }
    onSync(render);
    onAtomSync(render);
    render();
  }

  /* ============================================================
     initAirportDirectory() — clickable directory -> scroll/focus
     ============================================================ */
  function initAirportDirectory() {
    const grid = $("#directoryGrid");
    const items = [
      { name: "CENTRAL CORE", desc: "Nucleus", target: "airportmap" },
      { name: "TERMINAL 1", desc: "Low-energy shell", target: "terminals" },
      { name: "TERMINAL 2", desc: "Second energy level", target: "terminals" },
      { name: "TERMINAL 3", desc: "Higher-energy shell", target: "terminals" },
      { name: "P CONCOURSE", desc: "Three orbitals", target: "airportmap" },
      { name: "S CONCOURSE", desc: "One orbital", target: "airportmap" },
      { name: "ENERGY TRANSFER LAB", desc: "Excitation / Emission", target: "energy" },
      { name: "QUANTUM RADAR", desc: "Schrödinger model", target: "radar" },
      { name: "BOARDING CONTROL", desc: "Aufbau / Hund / Pauli", target: "boarding" }
    ];
    grid.innerHTML = items.map((i) =>
      `<button class="dir-item" data-target="${i.target}">
        <span class="dir-name">${i.name}</span>
        <span class="dir-desc">${i.desc}</span>
       </button>`).join("");
    $$(".dir-item", grid).forEach((b) => b.addEventListener("click", () => {
      const t = document.getElementById(b.dataset.target);
      if (t) t.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
    }));
  }

  /* ============================================================
     initAircraftCabin() + initSeatMap()
     ============================================================ */
  function renderSeatMap() {
    const map = $("#seatMap");
    if (!map) return;
    // 2p concourse rows P01/P02/P03, each 2 seats (spin up / down)
    const rows = [
      { label: "P01", node: "2p1" },
      { label: "P02", node: "2p2" },
      { label: "P03", node: "2p3" }
    ];
    map.innerHTML = rows.map((r) => {
      const occ = gateOccupantsByNode(r.node);
      const up = occ.find((o) => getElectronDisplayState(o).spin === UP);
      const dn = occ.find((o) => getElectronDisplayState(o).spin === DOWN);
      const seat = (e, spin) => e
        ? `<span class="seat occupied${spin === DOWN ? " down" : ""}">${e.id} ${spin}</span>`
        : `<span class="seat">EMPTY</span>`;
      return `<div class="seat-row">
        <div class="seat-row-label">ROW ${r.label}</div>
        <div class="seat-pair">${seat(up, UP)}${seat(dn, DOWN)}</div>
      </div>`;
    }).join("");
  }

  function initSeatMap() {
    renderSeatMap();
    onSync(renderSeatMap);
  }

  function initAircraftCabin() {
    const view = $("#cabinView");
    const level = $("#cabinFlightLevel");
    const status = $("#cabinStatus");
    const cabinLevel = $("#cabinLevel");

    function render() {
      const d = getElectronDisplayState("E-08");
      const excited = d.stateKey === "excited";
      view.classList.toggle("excited", excited);
      level.textContent = excited ? "FLIGHT LEVEL 3s · HIGHER ENERGY" : "FLIGHT LEVEL 2p · LOWER ENERGY";
      status.textContent = excited ? "EXCITED-STATE TRANSFER" : "GROUND-STATE SERVICE";
      status.style.color = excited ? "var(--amber)" : "var(--green)";
      cabinLevel.textContent = excited ? "3s" : "2p";
    }
    onSync(render);
    render();
  }

  /* ============================================================
     initCheckIn() — guided 6-step, rule-validated
     ============================================================ */
  function initCheckIn() {
    const steps = $$(".checkin-steps li");
    const panel = $("#checkinPanel");
    let step = 1;
    let passenger = "E-07";

    function setStep(n) {
      step = n;
      steps.forEach((li) => {
        const s = parseInt(li.dataset.step, 10);
        li.classList.toggle("active", s === step);
        li.classList.toggle("done", s < step);
      });
      render();
    }

    function render() {
      const d = getElectronDisplayState(passenger);
      if (step === 1) {
        panel.innerHTML = `
          <h4>STEP 1 · SELECT PASSENGER</h4>
          <div class="checkin-choices"><select id="ciPassenger">${electrons.map((e) => `<option value="${e.id}"${e.id === passenger ? " selected" : ""}>${e.id}</option>`).join("")}</select></div>
          <p>Check-in validates the assignment against the Aufbau, Hund and Pauli rules.</p>
          <button class="btn btn-primary" id="ciNext">CONTINUE</button>`;
        $("#ciPassenger").addEventListener("change", (e) => { passenger = e.target.value; });
        $("#ciNext").addEventListener("click", () => setStep(2));
      } else if (step === 2) {
        panel.innerHTML = `
          <h4>STEP 2 · VERIFY ENERGY LEVEL</h4>
          <p>Passenger <b>${d.id}</b> is assigned to <b>Terminal T${d.terminal}</b>, corresponding to principal energy level n = ${d.terminal}.</p>
          <button class="btn btn-primary" id="ciNext">VERIFIED · CONTINUE</button>`;
        $("#ciNext").addEventListener("click", () => setStep(3));
      } else if (step === 3) {
        panel.innerHTML = `
          <h4>STEP 3 · ASSIGN SUBSHELL</h4>
          <p>Concourse <b>${sectionLetter(d)}</b> · subshell <b>${d.subshell}</b>. The ${d.subshell} subshell belongs to the ${sectionLetter(d)} concourse.</p>
          <button class="btn btn-primary" id="ciNext">CONTINUE</button>`;
        $("#ciNext").addEventListener("click", () => setStep(4));
      } else if (step === 4) {
        panel.innerHTML = `
          <h4>STEP 4 · ASSIGN ORBITAL</h4>
          <p>Gate <b>${d.gate}</b> · orbital <b>${d.orbital}</b>. Aufbau: lower-energy orbitals fill first. Hund: equal-energy orbitals fill singly before pairing.</p>
          <button class="btn btn-primary" id="ciNext">CONTINUE</button>`;
        $("#ciNext").addEventListener("click", () => setStep(5));
      } else if (step === 5) {
        panel.innerHTML = `
          <h4>STEP 5 · VERIFY SPIN</h4>
          <p>Spin <b>${d.spin}</b>. Pauli: a paired orbital must contain two electrons of opposite spin.</p>
          <button class="btn btn-primary" id="ciNext">CONTINUE</button>`;
        $("#ciNext").addEventListener("click", () => setStep(6));
      } else {
        panel.innerHTML = `
          <h4>STEP 6 · BOARD</h4>
          <div class="checkin-ok">CHECK-IN COMPLETE · ${d.id} boarded at gate ${d.gate}, orbital ${d.orbital}, spin ${d.spin}. Assignment satisfies Aufbau, Hund and Pauli.</div>
          <div class="checkin-fail">
            <div class="cf-title">CHECK-IN FAILED · HUND'S RULE</div>
            <div class="cf-reason">RULE REFERENCE</div>
            <p>An empty equal-energy orbital is available. The electron must occupy it before pairing.</p>
          </div>
          <div class="checkin-fail">
            <div class="cf-title">CHECK-IN FAILED · PAULI EXCLUSION PRINCIPLE</div>
            <div class="cf-reason">RULE REFERENCE</div>
            <p>This orbital already contains an electron with the same spin.</p>
          </div>
          <div class="checkin-fail">
            <div class="cf-title">CHECK-IN FAILED · AUFBAU PRINCIPLE</div>
            <div class="cf-reason">RULE REFERENCE</div>
            <p>Lower-energy orbitals must be filled first.</p>
          </div>
          <button class="btn btn-ghost" id="ciRestart" style="margin-top:14px">RESTART CHECK-IN</button>`;
        $("#ciRestart").addEventListener("click", () => { passenger = "E-07"; setStep(1); });
      }
    }
    setStep(1);
  }

  /* ============================================================
     initSecurityScreening() — Pauli verification demo
     ============================================================ */
  function initSecurityScreening() {
    const wrap = $("#screening");
    // scenario: gate 2p1 currently holds E-05 (↑). Incoming candidate spin toggles.
    let incoming = DOWN;

    function render() {
      const cleared = incoming !== UP; // opposite of current occupant (↑)
      wrap.innerHTML = `
        <div class="sc-cell"><span class="sc-k">PASSENGER</span><span class="sc-v">E-08</span></div>
        <div class="sc-cell"><span class="sc-k">GATE</span><span class="sc-v">2p${SUP[1]}</span></div>
        <div class="sc-cell"><span class="sc-k">CURRENT OCCUPANT</span><span class="sc-v">E-05 ${UP}</span></div>
        <div class="sc-cell"><span class="sc-k">INCOMING SPIN</span><span class="sc-v">${incoming}</span></div>
        <div class="sc-cell"><span class="sc-k">RESULT</span><span class="sc-v ${cleared ? "cleared" : "denied"}">${cleared ? "CLEARED" : "DENIED"}</span></div>
        <div class="sc-cell"><span class="sc-k">REASON</span><span class="sc-v" style="font-size:11px">${cleared ? "Opposite spin — Pauli satisfied" : "PAULI EXCLUSION — same spin"}</span></div>`;
      // action buttons live outside the grid
      let actions = wrap.parentElement.querySelector(".screening-actions");
      if (!actions) {
        actions = document.createElement("div");
        actions.className = "screening-actions";
        wrap.insertAdjacentElement("afterend", actions);
      }
      actions.innerHTML = `
        <button class="btn btn-mini" id="scrUp">SET INCOMING ${UP}</button>
        <button class="btn btn-mini" id="scrDown">SET INCOMING ${DOWN}</button>`;
      $("#scrUp").addEventListener("click", () => { incoming = UP; render(); });
      $("#scrDown").addEventListener("click", () => { incoming = DOWN; render(); });
    }
    render();
  }

  /* ============================================================
     initDepartures() + initArrivals()
     ============================================================ */
  function initDepartures() {
    const body = $("#departuresTable tbody");
    function render() {
      const excited = state.energyState === "excited";
      const rows = [
        { f: "EN-201", p: "E-08", o: "2p" + SUP[1], d: "3s", status: excited ? "DEPARTED" : "STANDBY", cls: excited ? "st-departed" : "st-standby" },
        { f: "EN-202", p: "E-08", o: "3s", d: "2p" + SUP[1], status: excited ? "BOARDING" : "SCHEDULED", cls: excited ? "st-departed" : "st-standby" },
        { f: "PH-001", p: "PHOTON", o: "TRANSITION", d: "SPACE", status: photonEmitted ? "DEPARTED" : "STANDBY", cls: photonEmitted ? "st-departed" : "st-standby" }
      ];
      body.innerHTML = rows.map((r) =>
        `<tr><td>${r.f}</td><td>${r.p}</td><td>${r.o}</td><td>${r.d}</td><td class="${r.cls}">${r.status}</td></tr>`).join("");
    }
    onSync(render);
    render();
  }

  function initArrivals() {
    const body = $("#arrivalsTable tbody");
    function render() {
      const ground = state.energyState === "ground";
      const arrived = ground && photonEmitted;
      const rows = [
        { f: "EN-202", p: "E-08", from: "3s", state: arrived ? "GROUND" : (state.energyState === "excited" ? "EXCITED" : "GROUND"),
          status: arrived ? "ARRIVED" : (state.energyState === "excited" ? "EN ROUTE" : "SCHEDULED"),
          cls: arrived ? "st-boarded" : "st-standby" }
      ];
      body.innerHTML = rows.map((r) =>
        `<tr><td>${r.f}</td><td>${r.p}</td><td>${r.from}</td><td>${r.state}</td><td class="${r.cls}">${r.status}${arrived ? " · PHOTON EMITTED" : ""}</td></tr>`).join("");
    }
    onSync(render);
    render();
  }

  /* ============================================================
     initFlightStatus()
     ============================================================ */
  function initFlightStatus() {
    const sel = $("#flightSelect");
    const result = $("#flightStatusResult");
    function render() {
      const excited = state.energyState === "excited";
      const f = sel.value || "EN-201";
      let rows = [];
      if (f === "EN-201") {
        rows = [["PASSENGER", "E-08"], ["TYPE", "ENERGY TRANSITION"], ["FROM", "2p"], ["TO", "3s"],
          ["STATUS", excited ? "EXCITED STATE" : "STANDBY"], ["ENERGY", excited ? "ABSORBED" : "AWAITING INPUT"]];
      } else if (f === "EN-202") {
        rows = [["PASSENGER", "E-08"], ["TYPE", "RETURN TRANSITION"], ["FROM", "3s"], ["TO", "2p"],
          ["STATUS", (!excited && photonEmitted) ? "COMPLETED" : (excited ? "READY" : "SCHEDULED")], ["ENERGY", "RELEASED AS PHOTON"]];
      } else {
        rows = [["FLIGHT", "PH-001"], ["TYPE", "PHOTON"], ["ORIGIN", "ELECTRON TRANSITION"],
          ["STATUS", photonEmitted ? "DEPARTED" : "STANDBY"], ["RELATION", "E = hc / λ"]];
      }
      result.innerHTML = `<div class="fs-head mono">FLIGHT ${f}</div>` + rows.map(([k, v]) =>
        `<div class="fs-row"><span class="fs-k">${k}</span><span class="fs-v">${v}</span></div>`).join("");
    }
    sel.addEventListener("change", render);
    $("#flightGo").addEventListener("click", render);
    onSync(render);
    render();
  }

  /* ============================================================
     initTimeline()
     ============================================================ */
  function initTimeline() {
    renderTimeline();
  }

  /* ============================================================
     initJourneyMode() — guided 10-step walkthrough
     ============================================================ */
  function initJourneyMode() {
    const steps = [
      { t: "CHECK-IN", c: "E-08 · OXYGEN", p: "Select passenger E-08 and begin check-in. The passenger is an electron in the oxygen atom." },
      { t: "TERMINAL ASSIGNMENT", c: "TERMINAL 2 · n=2", p: "E-08 is assigned to Terminal 2, the second principal energy level." },
      { t: "CONCOURSE", c: "P CONCOURSE · 2p", p: "Within Terminal 2, E-08 belongs to the P concourse (the 2p subshell)." },
      { t: "GATE", c: "GATE P01 · 2p" + SUP[1], p: "E-08 is assigned to gate P01, orbital 2p" + SUP[1] + ", paired with E-05." },
      { t: "BOARDING", c: "SPIN " + DOWN, p: "E-08 boards with spin " + DOWN + ", opposite to E-05 (" + UP + "). Pauli exclusion is satisfied." },
      { t: "ENERGY TRANSFER", c: "ABSORPTION", p: "Energy input is applied. E-08 absorbs a quantum of energy." },
      { t: "EXCITED STATE", c: "2p → 3s", p: "E-08 transitions to the higher-energy 3s state. The atom is now excited." },
      { t: "RETURN FLIGHT", c: "3s → 2p", p: "E-08 returns to the lower-energy 2p state." },
      { t: "PHOTON EMISSION", c: "PH-001", p: "The energy difference is released as a photon (PH-001)." },
      { t: "GROUND STATE RESTORED", c: "1s² 2s² 2p⁴", p: "The atom returns to its ground-state configuration. Operations are normal." }
    ];
    let i = 0;
    const body = $("#journeyBody");
    const counter = $("#journeyCounter");
    const bar = $("#journeyBar");

    function render() {
      const s = steps[i];
      counter.textContent = "STEP " + String(i + 1).padStart(2, "0") + " / 10";
      bar.style.width = ((i + 1) / steps.length * 100) + "%";
      body.innerHTML = `<h4>${s.t}</h4><div class="jb-code mono">${s.c}</div><p>${s.p}</p>
        <p class="muted" style="font-size:12px">Electrons do not physically travel like airplanes. This is a visual analogy for changes between allowed energy states.</p>`;
      $("#journeyBack").disabled = i === 0;
      $("#journeyNext").textContent = i === steps.length - 1 ? "RESTART" : "NEXT";
    }
    $("#journeyNext").addEventListener("click", () => {
      if (i === steps.length - 1) { i = 0; } else { i++; }
      render();
    });
    $("#journeyBack").addEventListener("click", () => { if (i > 0) { i--; render(); } });
    render();
  }

  /* ============================================================
     initGlobalSearch()
     ============================================================ */
  function initGlobalSearch() {
    const input = $("#globalSearch");
    const results = $("#searchResults");
    const index = [
      { q: ["e-08", "e08", "electron 8"], label: "E-08 · Passenger record", target: "passengers", act: () => selectElectron("E-08", false) },
      { q: ["2p", "p concourse"], label: "2p subshell · Terminal map", target: "terminals" },
      { q: ["p01", "2p1"], label: "Gate P01 · 2p₁", target: "airportmap" },
      { q: ["hund"], label: "Hund's Rule · Boarding control", target: "boarding" },
      { q: ["pauli"], label: "Pauli exclusion · Security", target: "security" },
      { q: ["aufbau"], label: "Aufbau principle · Boarding control", target: "boarding" },
      { q: ["photon", "ph-001", "ph001", "emission"], label: "Photon / emission · Flights", target: "flights" },
      { q: ["excite", "excited", "absorption", "energy"], label: "Energy Lab · excitation & emission", target: "energy" },
      { q: ["terminal 2", "t2"], label: "Terminal 2 · Airport map", target: "airportmap" },
      { q: ["schrödinger", "schrodinger", "quantum radar", "radar"], label: "Schrödinger model · Radar", target: "radar" },
      { q: ["quantum number", "quantum numbers", "ml", "ms", "quantum"], label: "Quantum flight records", target: "quantum" },
      { q: ["ion", "ionization", "ionisation", "cation", "anion", "charge"], label: "Ion control · immigration", target: "ions" },
      { q: ["spectroscopy", "spectrum", "wavelength", "balmer", "hydrogen"], label: "Spectroscopy observation window", target: "spectroscopy" },
      { q: ["valence", "octet"], label: "Valence / international terminal", target: "atoms" },
      { q: ["shell", "subshell", "orbital"], label: "Control tower · configuration view", target: "tower" },
      { q: ["configuration", "electron configuration", "config"], label: "Gate assignment lab", target: "gatelab" },
      { q: ["challenge", "controller", "game"], label: "Air traffic controller challenge", target: "challenge" },
      { q: ["certificate", "cert"], label: "Operations certificate", target: "cert" },
      { q: ["control tower", "tower", "dashboard"], label: "EIA control tower", target: "tower" },
      { q: ["periodic", "element", "atom", "passport"], label: "Atomic passport control", target: "atoms" },
      { q: ["book", "booking", "itinerary"], label: "Flight booking", target: "booking" },
      { q: ["cabin", "seat"], label: "Flight cabin · seat map", target: "cabin" }
    ];
    // element results (dynamic load)
    ELEMENTS.forEach((e) => index.push({
      q: [e.sym.toLowerCase(), e.name.toLowerCase(), "z" + e.z, String(e.z)],
      label: "LOAD ELEMENT · " + e.name + " / " + e.sym + " (Z=" + e.z + ")",
      target: "atoms",
      act: () => { loadElement(e.sym); }
    }));

    function search(term) {
      const t = term.trim().toLowerCase();
      if (!t) return [];
      return index.filter((r) => r.q.some((k) => k === t || k.startsWith(t) || t.startsWith(k)) || r.label.toLowerCase().includes(t)).slice(0, 12);
    }
    function highlightSection(el) {
      el.classList.remove("section-flash"); void el.offsetWidth; el.classList.add("section-flash");
    }
    function render(list) {
      if (!list.length) { results.hidden = true; results.innerHTML = ""; return; }
      results.hidden = false;
      results.innerHTML = list.map((r, idx) =>
        `<button class="sr-item" data-idx="${idx}">${r.label}</button>`).join("");
      $$(".sr-item", results).forEach((b) => b.addEventListener("click", () => {
        const r = list[parseInt(b.dataset.idx, 10)];
        if (r.act) r.act();
        const t = document.getElementById(r.target);
        if (t) { t.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" }); highlightSection(t); }
        results.hidden = true; input.value = "";
      }));
    }
    input.addEventListener("input", () => render(search(input.value)));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { const list = search(input.value); if (list.length) { render(list); $(".sr-item", results)?.click(); } }
      if (e.key === "Escape") { results.hidden = true; }
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".global-search")) results.hidden = true;
    });
  }

  /* ============================================================
     ATOMIC PASSPORT CONTROL — periodic directory + passport panel
     ============================================================ */
  function initAtomicPassport() {
    const grid = $("#periodicGrid");
    const panel = $("#passportPanel");
    if (!grid) return;

    grid.innerHTML = ELEMENTS.map((e) =>
      `<button class="pt-tile" data-sym="${e.sym}" style="grid-column:${e.group}; grid-row:${e.period};" aria-label="${e.name}, atomic number ${e.z}">
        <span class="pt-z mono">${String(e.z).padStart(3, "0")}</span>
        <span class="pt-sym">${e.sym}</span>
        <span class="pt-name">${e.name.toUpperCase()}</span>
      </button>`).join("");

    function renderPassport(sym) {
      const e = ELEMENT_BY_SYM[sym];
      const cfg = generateElectronConfiguration(e.z);
      const val = calculateValenceElectrons(cfg);
      const shells = calculateShellDistribution(cfg);
      panel.innerHTML = `
        <div class="passport-head">ATOMIC PASSPORT</div>
        <div class="passport-grid">
          <div class="pp-cell"><span class="pp-k">ELEMENT</span><span class="pp-v">${e.name.toUpperCase()}</span></div>
          <div class="pp-cell"><span class="pp-k">SYMBOL</span><span class="pp-v mono">${e.sym}</span></div>
          <div class="pp-cell"><span class="pp-k">PASSPORT NO.</span><span class="pp-v mono">ATOM-${String(e.z).padStart(3, "0")}</span></div>
          <div class="pp-cell"><span class="pp-k">PERIOD</span><span class="pp-v mono">${String(e.period).padStart(2, "0")}</span></div>
          <div class="pp-cell"><span class="pp-k">GROUP</span><span class="pp-v mono">${String(e.group).padStart(2, "0")}</span></div>
          <div class="pp-cell"><span class="pp-k">ELECTRONS</span><span class="pp-v mono">${String(e.z).padStart(2, "0")}</span></div>
          <div class="pp-cell"><span class="pp-k">VALENCE PASSENGERS</span><span class="pp-v mono">${String(val.count).padStart(2, "0")}</span></div>
          <div class="pp-cell pp-wide"><span class="pp-k">CONFIGURATION</span><span class="pp-v mono">${formatConfiguration(cfg)}</span></div>
          <div class="pp-cell pp-wide"><span class="pp-k">SHELL DISTRIBUTION</span><span class="pp-v mono">${shells.map((s) => s.count).join(" | ")}</span></div>
        </div>
        <button class="btn btn-primary" id="loadElementBtn" data-sym="${e.sym}">LOAD INTO EIA</button>`;
      $("#loadElementBtn").addEventListener("click", () => {
        loadElement(e.sym);
        highlightLoaded();
        $("#operations")?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
      });
    }

    function highlightLoaded() {
      $$(".pt-tile", grid).forEach((t) => t.classList.toggle("loaded", t.dataset.sym === atom.symbol));
    }

    $$(".pt-tile", grid).forEach((tile) => {
      tile.addEventListener("click", () => {
        $$(".pt-tile", grid).forEach((t) => t.classList.remove("selected"));
        tile.classList.add("selected");
        renderPassport(tile.dataset.sym);
      });
    });

    renderPassport(atom.symbol);
    highlightLoaded();
    onAtomSync(highlightLoaded);
  }

  /* ============================================================
     CONTROL TOWER — live central dashboard
     ============================================================ */
  function initControlTower() {
    const grid = $("#towerGrid");
    if (!grid) return;

    function render() {
      const e = currentElement();
      const cfg = atom.config;
      const val = calculateValenceElectrons(cfg);
      const shells = calculateShellDistribution(cfg);
      const excited = state.energyState === "excited";
      const charge = atom.charge;
      let systemStatus = "CONFIGURATION VALID", statusCls = "ok";
      if (excited) { systemStatus = "EXCITED STATE"; statusCls = "amber"; }
      const cells = [
        ["CURRENT ATOM", e.name.toUpperCase() + " / " + e.sym, ""],
        ["ATOMIC NUMBER", String(e.z).padStart(2, "0"), "mono"],
        ["PROTONS", String(atom.protons).padStart(2, "0"), "mono"],
        ["ELECTRONS", String(atom.electronCount).padStart(2, "0"), "mono"],
        ["NET CHARGE", (charge > 0 ? "+" : "") + charge + " · " + ionNotation(e.sym, charge), charge === 0 ? "mono" : "mono amber"],
        ["STATE", excited ? "EXCITED" : "GROUND", excited ? "amber" : "ok"],
        ["CONFIGURATION", formatConfiguration(cfg), "mono wide"],
        ["VALENCE PASSENGERS", String(val.count).padStart(2, "0") + " · n=" + val.n, "mono"],
        ["ACTIVE TERMINALS", String(calculateOccupiedShells(cfg)).padStart(2, "0"), "mono"],
        ["OCCUPIED GATES", String(calculateOccupiedOrbitals(cfg)).padStart(2, "0"), "mono"],
        ["PHOTON STATUS", photonEmitted ? "PH-001 EMITTED" : "STANDBY", photonEmitted ? "cyan" : "mono"],
        ["SYSTEM STATUS", systemStatus, statusCls]
      ];
      grid.innerHTML = cells.map(([k, v, cls]) =>
        `<div class="tw-cell"><span class="tw-k">${k}</span><span class="tw-v ${cls || ""}">${v}</span></div>`).join("");
    }
    function syncNavBadge() {
      const e = currentElement();
      const navAtom = $("#navAtom");
      if (navAtom) navAtom.textContent = e.name.toUpperCase() + " / " + e.sym;
      const navCount = $("#navElectronCount");
      if (navCount) navCount.textContent = String(atom.electronCount).padStart(2, "0") + " ELECTRONS";
    }

    onAtomSync(render);
    onAtomSync(syncNavBadge);
    render();
    syncNavBadge();

    const clearBtn = $("#towerClearLog");
    if (clearBtn) clearBtn.addEventListener("click", () => { eventLog.length = 0; renderEventLog(); });
    const resetBtn = $("#resetSystem");
    if (resetBtn) resetBtn.addEventListener("click", openResetModal);
    renderEventLog();
  }

  /* ============================================================
     VALENCE TERMINAL (International Terminal)
     ============================================================ */
  function initValenceTerminal() {
    const wrap = $("#valenceTerminal");
    if (!wrap) return;
    function render() {
      const cfg = atom.config;
      const val = calculateValenceElectrons(cfg);
      // capacity of the highest occupied shell n: 2n^2
      const shellCap = 2 * val.n * val.n;
      const vacancies = Math.max(0, shellCap - val.count);
      // valence subshells for display
      const valSubs = displayOrderedConfig(cfg).filter((c) => c.n === val.n);
      const gates = valSubs.map((c) => {
        const dots = [];
        for (let i = 0; i < c.orbitals; i++) {
          const inOrb = (i < c.count ? 1 : 0) + (c.count - c.orbitals > i ? 1 : 0);
          dots.push(`<span class="vt-orb vt-${Math.min(2, inOrb)}"></span>`);
        }
        return `<div class="vt-sub"><span class="vt-sub-name mono">${c.name}</span><div class="vt-orbs">${dots.join("")}</div></div>`;
      }).join("");
      wrap.innerHTML = `
        <div class="vt-grid">
          <div class="vt-cell"><span class="vt-k">VALENCE TERMINAL</span><span class="vt-v mono">TERMINAL ${val.n}</span></div>
          <div class="vt-cell"><span class="vt-k">CURRENT OCCUPANCY</span><span class="vt-v mono">${val.count} / ${shellCap}</span></div>
          <div class="vt-cell"><span class="vt-k">VACANT CAPACITY</span><span class="vt-v mono">${vacancies}</span></div>
        </div>
        <div class="vt-gates">${gates}</div>
        <p class="vt-note">Valence electrons are electrons in the highest occupied principal energy level. They are especially important in chemical bonding and reactivity. <span class="muted">Note: the eight-electron (octet) pattern applies most directly to many main-group atoms and ions, and is not a universal rule.</span></p>`;
    }
    onAtomSync(render);
    render();
  }

  /* ============================================================
     ION CONTROL (Electron Immigration Control)
     ============================================================ */
  function initIonControl() {
    const panel = $("#ionPanel");
    if (!panel) return;
    const notice = $("#ionNotice");

    function render() {
      const e = currentElement();
      const charge = atom.charge;
      panel.innerHTML = `
        <div class="ion-grid">
          <div class="ion-cell"><span class="ion-k">ELEMENT</span><span class="ion-v mono">${e.sym}</span></div>
          <div class="ion-cell"><span class="ion-k">PROTONS</span><span class="ion-v mono">${atom.protons}</span></div>
          <div class="ion-cell"><span class="ion-k">ELECTRONS</span><span class="ion-v mono">${atom.electronCount}</span></div>
          <div class="ion-cell"><span class="ion-k">NET CHARGE</span><span class="ion-v mono ${charge === 0 ? "" : "amber"}">${charge > 0 ? "+" : ""}${charge}</span></div>
          <div class="ion-cell"><span class="ion-k">SPECIES</span><span class="ion-v mono ${charge === 0 ? "ok" : "amber"}">${ionNotation(e.sym, charge)}</span></div>
          <div class="ion-cell"><span class="ion-k">CLASS</span><span class="ion-v">${chargeStateLabel(charge)}</span></div>
        </div>
        <div class="ion-cfg mono">${formatConfiguration(atom.config)}</div>
        <div class="ctrl-actions">
          <button class="btn btn-primary" id="ionAdd">ADD ELECTRON</button>
          <button class="btn btn-amber" id="ionRemove">REMOVE ELECTRON</button>
          <button class="btn btn-ghost" id="ionNeutral">RESET TO NEUTRAL</button>
        </div>`;
      $("#ionAdd").addEventListener("click", () => changeElectrons(1));
      $("#ionRemove").addEventListener("click", () => changeElectrons(-1));
      $("#ionNeutral").addEventListener("click", () => {
        atom.electronCount = atom.protons; recomputeAtom();
        ensureValidSelection();
        showIonNotice("RESET", "Neutral atom restored.", null);
        logEvent("IMMIGRATION", "Electron count reset to neutral " + currentElement().sym + ".");
        syncEntireEIA();
      });
    }

    function changeElectrons(delta) {
      const prev = formatConfiguration(atom.config);
      const next = atom.electronCount + delta;
      if (next < 1 || next > 20) return; // keep within supported bounds (through Ca isoelectronic)
      atom.electronCount = next;
      recomputeAtom();
      ensureValidSelection();
      const e = currentElement();
      const charge = atom.charge;
      const nowCfg = formatConfiguration(atom.config);
      if (delta > 0) {
        showIonNotice("IMMIGRATION CLEARANCE COMPLETE", "Electron added. Current species: " + ionNotation(e.sym, charge) + ".", { prev, next: nowCfg, charge });
        logEvent("IMMIGRATION", "Electron added. Ion changed to " + ionNotation(e.sym, charge) + ".");
        announce("Electron immigration registered. Current ion: " + ionNotation(e.sym, charge) + ".");
      } else {
        showIonNotice("PASSENGER DEPARTURE REGISTERED", "Electron removed (highest energy level first). Current species: " + ionNotation(e.sym, charge) + ".", { prev, next: nowCfg, charge });
        logEvent("IMMIGRATION", "Electron removed. Ion changed to " + ionNotation(e.sym, charge) + ".");
        announce("Passenger departure registered. Current ion: " + ionNotation(e.sym, charge) + ".");
      }
      syncEntireEIA();
    }

    function showIonNotice(title, msg, detail) {
      notice.hidden = false;
      notice.innerHTML = `
        <div class="ion-notice-title">${title}</div>
        <p>${msg}</p>
        ${detail ? `<div class="ion-notice-detail mono">PREV ${detail.prev}<br>NEW  ${detail.next}<br>CHARGE ${detail.charge > 0 ? "+" : ""}${detail.charge}</div>` : ""}`;
      if (!reduceMotion) { notice.classList.remove("flash"); void notice.offsetWidth; notice.classList.add("flash"); }
    }

    onAtomSync(render);
    render();
  }

  /* ============================================================
     QUANTUM RECORDS — per-electron quantum numbers
     ============================================================ */
  function initQuantumRecords() {
    const body = $("#quantumBody");
    if (!body) return;
    function render() {
      const rows = atom.records.map((r) =>
        `<tr>
          <td>${r.id}</td>
          <td class="mono">${r.n}</td>
          <td class="mono">${r.l} (${r.type})</td>
          <td class="mono">${r.ml > 0 ? "+" + r.ml : r.ml}</td>
          <td class="mono">${r.ms > 0 ? "+½" : "−½"}</td>
          <td class="mono">${r.subshell}</td>
          <td class="${r.spin === UP ? "spin-up" : "spin-down"}">${r.spin}</td>
        </tr>`).join("");
      body.innerHTML = rows;
    }
    onAtomSync(render);
    render();
  }

  /* ============================================================
     ORBITAL DIAGRAM + SHELL SUMMARY + VIEW TOGGLE
     ============================================================ */
  function renderOrbitalDiagram() {
    const wrap = $("#orbitalDiagram");
    if (!wrap) return;
    const rows = displayOrderedConfig(atom.config).map((c) => {
      // build boxes with arrows per Hund
      const boxes = [];
      for (let i = 0; i < c.orbitals; i++) {
        let up = i < c.count;
        let down = (c.count - c.orbitals) > i;
        let arrows = "";
        if (up) arrows += `<span class="od-arr">${UP}</span>`;
        if (down) arrows += `<span class="od-arr down">${DOWN}</span>`;
        boxes.push(`<span class="od-box">${arrows || "&nbsp;"}</span>`);
      }
      return `<div class="od-row"><span class="od-label mono">${c.name}</span><span class="od-boxes">${boxes.join("")}</span></div>`;
    }).join("");
    wrap.innerHTML = rows;
  }

  function renderShellSummary() {
    const wrap = $("#shellSummary");
    if (!wrap) return;
    const shells = calculateShellDistribution(atom.config);
    const total = shells.reduce((a, s) => a + s.count, 0);
    wrap.innerHTML = `
      <div class="ss-rows">
        ${shells.map((s) => `<div class="ss-row"><span class="ss-k mono">n = ${s.n}</span><span class="ss-v mono">${s.count} electrons</span></div>`).join("")}
        <div class="ss-row ss-total"><span class="ss-k mono">TOTAL</span><span class="ss-v mono">${total} electrons</span></div>
      </div>
      <div class="ss-dist mono">${shells.map((s) => s.count).join(" | ")}</div>`;
  }

  function initViewToggle() {
    const seg = $("#viewToggle");
    if (!seg) return;
    const stage = $("#configStage");
    const saved = safeGet("eia_viewmode") || "split";
    function apply(mode) {
      stage.dataset.view = mode;
      $$(".vt-seg", seg).forEach((b) => b.classList.toggle("active", b.dataset.view === mode));
      safeSet("eia_viewmode", mode);
    }
    $$(".vt-seg", seg).forEach((b) => b.addEventListener("click", () => apply(b.dataset.view)));
    apply(saved);
    onAtomSync(() => { renderOrbitalDiagram(); renderShellSummary(); });
    renderOrbitalDiagram(); renderShellSummary();
  }

  /* ============================================================
     localStorage helpers (safe)
     ============================================================ */
  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  /* ============================================================
     RESET SYSTEM — custom confirm modal
     ============================================================ */
  let resetModalWired = false;
  function openResetModal() {
    const modal = $("#resetModal");
    if (!modal) return;
    const confirm = $("#resetConfirm");
    const cancel = $("#resetCancel");

    function close() { modal.classList.remove("open"); modal.hidden = true; }
    function doReset() {
      photonEmitted = false;
      state.selectedElectron = null;
      // return E-08 / energy display to ground first, then reload oxygen
      if (typeof setEnergyState === "function") setEnergyState("ground");
      loadElement("O");                 // resets atom to neutral oxygen + fires atomSyncAll
      eventLog.length = 0;
      logEvent("SYSTEM", "EIA system reset. Oxygen restored, ground state.");
      announce("System reset. Default atom OXYGEN restored.");
      selectElectron("E-07", false);
      syncAll();
      close();
    }

    // wire listeners only once to avoid duplicates on repeated opens
    if (!resetModalWired) {
      confirm.addEventListener("click", doReset);
      cancel.addEventListener("click", close);
      modal.addEventListener("click", (ev) => { if (ev.target === modal) close(); });
      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && modal.classList.contains("open")) close();
      });
      resetModalWired = true;
    }

    modal.hidden = false;
    modal.classList.add("open");
  }

  /* ============================================================
     CONFIGURATION VALIDATION ENGINE (Aufbau / Hund / Pauli)
     Validates a manual assignment: map of subshellName -> array of
     orbital slots, each slot = array of spins ["up","down"].
     ============================================================ */
  function validateAssignment(assignment, electronCount) {
    // assignment: { "1s": [[spin,...], ...perOrbital], "2p": [...], ... }
    const violations = [];
    // total electrons
    let total = 0;
    Object.keys(assignment).forEach((sub) => assignment[sub].forEach((orb) => (total += orb.length)));
    // Pauli: max 2 per orbital, opposite spins
    Object.keys(assignment).forEach((sub) => {
      assignment[sub].forEach((orb, i) => {
        if (orb.length > 2) violations.push({ rule: "PAULI", msg: `Gate ${sub} orbital ${i + 1} exceeds capacity of two electrons.` });
        if (orb.length === 2 && orb[0] === orb[1]) violations.push({ rule: "PAULI", msg: `Two electrons in ${sub} orbital ${i + 1} must have opposite spins.` });
      });
    });
    // Reference (correct) configuration for this electron count
    const refCfg = generateElectronConfiguration(electronCount);
    const refCounts = {}; refCfg.forEach((c) => (refCounts[c.name] = c.count));
    // Aufbau: no higher-energy subshell occupied before a lower one is full
    for (let i = 0; i < ORBITAL_ORDER.length; i++) {
      const o = ORBITAL_ORDER[i];
      const cnt = (assignment[o.name] || []).reduce((a, orb) => a + orb.length, 0);
      if (cnt > 0) {
        // all earlier subshells in fill order must be at full capacity
        for (let j = 0; j < i; j++) {
          const prev = ORBITAL_ORDER[j];
          const prevCnt = (assignment[prev.name] || []).reduce((a, orb) => a + orb.length, 0);
          if (prevCnt < prev.capacity) {
            violations.push({ rule: "AUFBAU", msg: `${o.name} was occupied before ${prev.name} was filled.` });
            break;
          }
        }
      }
    }
    // Hund: within a subshell, no pairing until every orbital singly occupied
    Object.keys(assignment).forEach((sub) => {
      const orbs = assignment[sub];
      const anyPaired = orbs.some((orb) => orb.length === 2);
      const anyEmpty = orbs.some((orb) => orb.length === 0);
      if (anyPaired && anyEmpty) {
        violations.push({ rule: "HUND", msg: `Equal-energy ${sub} gates must be singly occupied before pairing.` });
      }
      // also: singly-occupied orbitals should share the same spin direction
      const singles = orbs.filter((orb) => orb.length === 1).map((orb) => orb[0]);
      if (singles.length > 1 && new Set(singles).size > 1 && !anyPaired) {
        // parallel-spin expectation for ground state (soft rule)
        violations.push({ rule: "HUND", msg: `Singly-occupied ${sub} gates should have parallel spins in the ground state.` });
      }
    });
    // count match
    const countOk = total === electronCount;
    return { violations, countOk, total, refConfig: refCfg };
  }

  /* ============================================================
     GATE ASSIGNMENT LAB — manual build with keyboard + auto assign
     ============================================================ */
  function initGateAssignmentLab() {
    const board = $("#gateLabBoard");
    if (!board) return;
    const result = $("#gateLabResult");
    // subshells available in the lab (through 4s, 3d shown/locked)
    const labSubs = ["1s", "2s", "2p", "3s", "3p", "4s", "3d"];
    // assignment model
    let assignment = {};
    let pool = 0;          // electrons remaining to place
    let selectedSpin = "up";
    let selectedGate = null; // {sub, orb}

    function labMeta(sub) { return ORBITAL_ORDER.find((o) => o.name === sub); }

    function resetGates() {
      assignment = {};
      labSubs.forEach((s) => {
        const meta = labMeta(s);
        assignment[s] = Array.from({ length: meta.orbitals }, () => []);
      });
      pool = atom.electronCount;
      selectedGate = null;
      render();
    }

    function autoAssign() {
      resetGates();
      const cfg = generateElectronConfiguration(atom.electronCount);
      cfg.forEach((c) => {
        if (!assignment[c.name]) return;
        // Hund fill: singly up first, then pair down
        let count = c.count;
        for (let i = 0; i < c.orbitals && count > 0; i++) { assignment[c.name][i].push("up"); count--; }
        for (let i = 0; i < c.orbitals && count > 0; i++) { assignment[c.name][i].push("down"); count--; }
      });
      pool = 0;
      render();
    }

    function placed() {
      let n = 0; Object.keys(assignment).forEach((s) => assignment[s].forEach((o) => (n += o.length))); return n;
    }

    function place(sub, orbIdx) {
      const locked = sub === "3d" && atom.electronCount <= 20 && false; // 3d unused for H–Ca ground states
      if (pool <= 0) { flashResult("No passengers remaining. Reset gates to reassign.", "warn"); return; }
      const orb = assignment[sub][orbIdx];
      if (orb.length >= 2) { flashResult("Gate at capacity (2).", "warn"); return; }
      if (orb.length === 1 && orb[0] === selectedSpin) { flashResult("Same-spin pairing is not allowed (Pauli).", "warn"); return; }
      orb.push(selectedSpin);
      pool--;
      render();
    }

    function removeFrom(sub, orbIdx) {
      const orb = assignment[sub][orbIdx];
      if (orb.length) { orb.pop(); pool++; render(); }
    }

    function render() {
      board.innerHTML = labSubs.map((s) => {
        const meta = labMeta(s);
        const orbs = assignment[s].map((orb, i) => {
          const arrows = orb.map((sp) => `<span class="gl-arr ${sp}">${sp === "up" ? UP : DOWN}</span>`).join("");
          const sel = selectedGate && selectedGate.sub === s && selectedGate.orb === i ? " selected" : "";
          return `<button class="gl-orb${sel}" data-sub="${s}" data-orb="${i}" aria-label="${s} orbital ${i + 1}, ${orb.length} of 2">${arrows || "&nbsp;"}</button>`;
        }).join("");
        return `<div class="gl-sub"><span class="gl-sub-name mono">${s}</span><div class="gl-orbs">${orbs}</div></div>`;
      }).join("");
      const poolWrap = $("#gateLabPool");
      if (poolWrap) poolWrap.innerHTML = `PASSENGERS TO ASSIGN <span class="mono">${pool}</span> · PLACED <span class="mono">${placed()}</span> / ${atom.electronCount}`;
      // wire orbital buttons
      $$(".gl-orb", board).forEach((b) => {
        b.addEventListener("click", () => {
          selectedGate = { sub: b.dataset.sub, orb: parseInt(b.dataset.orb, 10) };
          place(b.dataset.sub, parseInt(b.dataset.orb, 10));
        });
        b.addEventListener("contextmenu", (e) => { e.preventDefault(); removeFrom(b.dataset.sub, parseInt(b.dataset.orb, 10)); });
      });
    }

    function flashResult(msg, cls) {
      result.className = "gatelab-result " + (cls || "");
      result.innerHTML = `<div class="glr-msg">${msg}</div>`;
    }

    function check() {
      const v = validateAssignment(assignment, atom.electronCount);
      if (!v.countOk) {
        flashResult(`Assign all ${atom.electronCount} passengers before verification. Currently placed: ${v.total}.`, "warn");
        return;
      }
      if (v.violations.length) {
        const first = v.violations[0];
        const titles = { HUND: "HUND RULE VIOLATION", PAULI: "PAULI EXCLUSION VIOLATION", AUFBAU: "AUFBAU VIOLATION" };
        result.className = "gatelab-result denied";
        result.innerHTML = `<div class="glr-title">SECURITY ALERT · ${titles[first.rule] || first.rule}</div>
          <ul class="glr-list">${v.violations.map((x) => `<li>${x.msg}</li>`).join("")}</ul>`;
        announce("Configuration verification failed: " + (titles[first.rule] || first.rule) + ".", "SECURITY");
        logEvent("OPS", "Gate lab verification failed (" + first.rule + ").");
      } else {
        result.className = "gatelab-result cleared";
        result.innerHTML = `<div class="glr-title">CONFIGURATION VERIFIED</div>
          <p>All passengers cleared for boarding.</p>
          <div class="glr-cfg mono">${formatConfiguration(v.refConfig)}</div>`;
        announce("Configuration verified. All passengers cleared for boarding.");
        logEvent("OPS", "Gate lab configuration verified for " + currentElement().sym + ".");
      }
    }

    // spin selector + controls
    const spinSel = $("#gateLabSpin");
    if (spinSel) {
      $$(".gl-spin", spinSel).forEach((b) => b.addEventListener("click", () => {
        selectedSpin = b.dataset.spin;
        $$(".gl-spin", spinSel).forEach((x) => x.classList.toggle("active", x === b));
      }));
    }
    $("#gateLabCheck")?.addEventListener("click", check);
    $("#gateLabAuto")?.addEventListener("click", autoAssign);
    $("#gateLabReset")?.addEventListener("click", resetGates);

    onAtomSync(resetGates);
    resetGates();
  }

  /* ============================================================
     SPECTROSCOPY OBSERVATION WINDOW
     ============================================================ */
  const PLANCK = 6.62607015e-34; // J·s
  const CLIGHT = 299792458;      // m/s
  const RYDBERG = 1.097e7;       // m^-1

  function wavelengthToColor(nm) {
    // returns css color for visible range, or null if outside
    if (nm < 380 || nm > 750) return null;
    let r = 0, g = 0, b = 0;
    if (nm < 440) { r = -(nm - 440) / (440 - 380); g = 0; b = 1; }
    else if (nm < 490) { r = 0; g = (nm - 440) / (490 - 440); b = 1; }
    else if (nm < 510) { r = 0; g = 1; b = -(nm - 510) / (510 - 490); }
    else if (nm < 580) { r = (nm - 510) / (580 - 510); g = 1; b = 0; }
    else if (nm < 645) { r = 1; g = -(nm - 645) / (645 - 580); b = 0; }
    else { r = 1; g = 0; b = 0; }
    const f = nm < 420 ? 0.3 + 0.7 * (nm - 380) / (420 - 380) : nm > 700 ? 0.3 + 0.7 * (750 - nm) / (750 - 700) : 1;
    const ch = (x) => Math.round(255 * Math.pow(Math.max(0, x) * f, 0.8));
    return `rgb(${ch(r)},${ch(g)},${ch(b)})`;
  }

  function bandName(nm) {
    if (nm < 380) return "ULTRAVIOLET";
    if (nm > 750) return "INFRARED";
    if (nm < 450) return "VIOLET";
    if (nm < 495) return "BLUE";
    if (nm < 570) return "GREEN";
    if (nm < 590) return "YELLOW";
    if (nm < 620) return "ORANGE";
    return "RED";
  }

  function initSpectroscopy() {
    const scope = $("#spectroScope");
    if (!scope) return;
    const readout = $("#spectroReadout");
    const slider = $("#spectroSlider");

    function computeFromWavelength(nm) {
      const lambda = nm * 1e-9;
      const freq = CLIGHT / lambda;
      const energy = (PLANCK * CLIGHT) / lambda;
      return { freq, energy };
    }

    function render(nm) {
      const { freq, energy } = computeFromWavelength(nm);
      const color = wavelengthToColor(nm);
      const band = bandName(nm);
      // emission line
      const pct = ((nm - 380) / (750 - 380)) * 100;
      $("#spectroLine").style.left = Math.max(0, Math.min(100, pct)) + "%";
      $("#spectroLine").style.background = color || "transparent";
      $("#spectroLine").style.opacity = color ? "1" : "0.15";
      readout.innerHTML = `
        <div class="sp-row"><span class="sp-k">PHOTON ID</span><span class="sp-v mono">PH-001</span></div>
        <div class="sp-row"><span class="sp-k">TRANSITION</span><span class="sp-v mono">3s → 2p</span></div>
        <div class="sp-row"><span class="sp-k">BAND</span><span class="sp-v">${band}</span></div>
        <div class="sp-row"><span class="sp-k">WAVELENGTH</span><span class="sp-v mono">${nm} nm</span></div>
        <div class="sp-row"><span class="sp-k">FREQUENCY</span><span class="sp-v mono">${(freq / 1e14).toFixed(2)} × 10¹⁴ Hz</span></div>
        <div class="sp-row"><span class="sp-k">ENERGY</span><span class="sp-v mono">${(energy / 1e-19).toFixed(2)} × 10⁻¹⁹ J</span></div>`;
    }
    if (slider) { slider.addEventListener("input", () => render(parseInt(slider.value, 10))); render(parseInt(slider.value, 10)); }
  }

  /* ============================================================
     HYDROGEN EMISSION BOARD (Balmer series via Rydberg)
     ============================================================ */
  function initHydrogenBoard() {
    const board = $("#hydrogenBoard");
    if (!board) return;
    const nf = 2;
    const transitions = [3, 4, 5, 6].map((ni) => {
      const invLambda = RYDBERG * (1 / (nf * nf) - 1 / (ni * ni));
      const lambda = 1 / invLambda;          // metres
      const nm = lambda * 1e9;
      return { ni, nf, nm };
    });
    board.innerHTML = `
      <div class="table-scroll">
        <table class="fids-table">
          <thead><tr><th>FLIGHT</th><th>TRANSITION</th><th>WAVELENGTH</th><th>BAND</th><th>STATUS</th></tr></thead>
          <tbody>
            ${transitions.map((t, i) => {
              const color = wavelengthToColor(t.nm);
              return `<tr>
                <td>H-${String(i + 1).padStart(3, "0")}</td>
                <td class="mono">n=${t.ni} → n=${t.nf}</td>
                <td class="mono">${t.nm.toFixed(1)} nm</td>
                <td><span class="band-dot" style="background:${color || "#444"}"></span>${bandName(t.nm)}</td>
                <td class="ok">ARRIVED</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
      <div class="hy-strip">${transitions.map((t) => {
        const c = wavelengthToColor(t.nm);
        const pct = ((t.nm - 380) / (750 - 380)) * 100;
        return c ? `<span class="hy-line" style="left:${pct}%;background:${c}" title="${t.nm.toFixed(1)} nm"></span>` : "";
      }).join("")}</div>
      <p class="muted" style="font-size:12px">Balmer-series wavelengths computed from the Rydberg equation 1/λ = R(1/n_f² − 1/n_i²), R ≈ 1.097 × 10⁷ m⁻¹. This hydrogen demonstration is specific to hydrogen and does not describe the currently loaded atom.</p>`;
  }

  /* ============================================================
     AIR TRAFFIC CONTROLLER CHALLENGE
     ============================================================ */
  function initControllerChallenge() {
    const panel = $("#challengePanel");
    if (!panel) return;
    let challengeEl = null;
    let startedAt = 0;
    let timerId = null;
    let assignment = {};
    const labSubs = ["1s", "2s", "2p", "3s", "3p", "4s"];
    let selectedSpin = "up";

    function meta(sub) { return ORBITAL_ORDER.find((o) => o.name === sub); }
    function resetGates() {
      assignment = {};
      labSubs.forEach((s) => { assignment[s] = Array.from({ length: meta(s).orbitals }, () => []); });
    }
    function placed() { let n = 0; Object.keys(assignment).forEach((s) => assignment[s].forEach((o) => (n += o.length))); return n; }

    function startChallenge() {
      challengeEl = ELEMENTS[Math.floor(Math.random() * ELEMENTS.length)];
      resetGates();
      startedAt = performance.now();
      clearInterval(timerId);
      let remaining = 45;
      const timerEl = () => $("#challengeTimer");
      timerId = setInterval(() => {
        remaining--;
        if (timerEl()) timerEl().textContent = remaining + "s";
        if (remaining <= 0) { clearInterval(timerId); }
      }, 1000);
      render();
    }

    function place(sub, i) {
      const orb = assignment[sub][i];
      if (placed() >= challengeEl.z) return;
      if (orb.length >= 2) return;
      if (orb.length === 1 && orb[0] === selectedSpin) return;
      orb.push(selectedSpin); render();
    }
    function removeFrom(sub, i) { const orb = assignment[sub][i]; if (orb.length) { orb.pop(); render(); } }

    function submit() {
      if (!challengeEl) return;
      const v = validateAssignment(assignment, challengeEl.z);
      const timeSec = ((performance.now() - startedAt) / 1000);
      clearInterval(timerId);
      const pass = v.countOk && v.violations.length === 0;
      const accuracy = v.countOk ? Math.max(0, Math.round(100 - v.violations.length * 20)) : 0;
      // best score
      const best = parseInt(safeGet("eia_challenge_best") || "0", 10);
      if (accuracy > best) safeSet("eia_challenge_best", String(accuracy));
      $("#challengeReport").hidden = false;
      $("#challengeReport").innerHTML = `
        <div class="cr-title">CONTROLLER REPORT</div>
        <div class="cr-grid">
          <div><span class="cr-k">ATOM</span><span class="cr-v">${challengeEl.name.toUpperCase()}</span></div>
          <div><span class="cr-k">CONFIGURATION</span><span class="cr-v mono">${formatConfiguration(v.refConfig)}</span></div>
          <div><span class="cr-k">RESULT</span><span class="cr-v ${pass ? "ok" : "amber"}">${pass ? "PASSED" : "REVIEW"}</span></div>
          <div><span class="cr-k">ACCURACY</span><span class="cr-v mono">${accuracy}%</span></div>
          <div><span class="cr-k">RULE VIOLATIONS</span><span class="cr-v mono">${v.violations.length}</span></div>
          <div><span class="cr-k">TIME</span><span class="cr-v mono">${timeSec.toFixed(1)} SEC</span></div>
          <div><span class="cr-k">BEST</span><span class="cr-v mono">${Math.max(best, accuracy)}%</span></div>
        </div>
        ${v.violations.length ? `<ul class="glr-list">${v.violations.map((x) => `<li>${x.msg}</li>`).join("")}</ul>` : ""}`;
      if (pass) {
        const done = parseInt(safeGet("eia_challenge_wins") || "0", 10) + 1;
        safeSet("eia_challenge_wins", String(done));
        logEvent("TRAINING", "Challenge passed: " + challengeEl.name + " (" + accuracy + "%).");
      }
    }

    function render() {
      if (!challengeEl) { panel.innerHTML = `<p class="muted">Press START CHALLENGE to receive incoming atomic traffic.</p>`; return; }
      const orbs = labSubs.map((s) => {
        const cells = assignment[s].map((orb, i) => {
          const arrows = orb.map((sp) => `<span class="gl-arr ${sp}">${sp === "up" ? UP : DOWN}</span>`).join("");
          return `<button class="gl-orb" data-sub="${s}" data-orb="${i}" aria-label="${s} orbital ${i + 1}">${arrows || "&nbsp;"}</button>`;
        }).join("");
        return `<div class="gl-sub"><span class="gl-sub-name mono">${s}</span><div class="gl-orbs">${cells}</div></div>`;
      }).join("");
      panel.innerHTML = `
        <div class="challenge-head">
          <div><span class="ch-k">CALLSIGN</span><span class="ch-v mono">${challengeEl.sym}-${String(challengeEl.z).padStart(3, "0")}</span></div>
          <div><span class="ch-k">ELEMENT</span><span class="ch-v">${challengeEl.name.toUpperCase()}</span></div>
          <div><span class="ch-k">PASSENGERS</span><span class="ch-v mono">${String(challengeEl.z).padStart(2, "0")}</span></div>
          <div><span class="ch-k">TIME</span><span class="ch-v mono" id="challengeTimer">45s</span></div>
        </div>
        <div class="challenge-spin" id="challengeSpin">
          <button class="btn btn-mini gl-spin active" data-spin="up">SPIN ${UP}</button>
          <button class="btn btn-mini gl-spin" data-spin="down">SPIN ${DOWN}</button>
        </div>
        <div class="gatelab-board">${orbs}</div>
        <div class="ch-pool mono">PLACED ${placed()} / ${challengeEl.z}</div>`;
      $$(".gl-orb", panel).forEach((b) => {
        b.addEventListener("click", () => place(b.dataset.sub, parseInt(b.dataset.orb, 10)));
        b.addEventListener("contextmenu", (e) => { e.preventDefault(); removeFrom(b.dataset.sub, parseInt(b.dataset.orb, 10)); });
      });
      $$(".gl-spin", panel).forEach((b) => b.addEventListener("click", () => {
        selectedSpin = b.dataset.spin;
        $$(".gl-spin", panel).forEach((x) => x.classList.toggle("active", x === b));
      }));
    }

    $("#challengeStart")?.addEventListener("click", startChallenge);
    $("#challengeSubmit")?.addEventListener("click", submit);
    $("#challengeNew")?.addEventListener("click", startChallenge);
    const bestLabel = $("#challengeBest");
    if (bestLabel) bestLabel.textContent = (safeGet("eia_challenge_best") || "0") + "%";
    render();
  }

  /* ============================================================
     CERTIFICATE
     ============================================================ */
  function initCertificate() {
    const gen = $("#certGenerate");
    if (!gen) return;
    const view = $("#certificate");
    gen.addEventListener("click", () => {
      const name = ($("#certName")?.value || "").trim() || "EIA CONTROLLER";
      const best = parseInt(safeGet("eia_challenge_best") || "0", 10);
      const quiz = state.quizScore || 0;
      const score = Math.max(best, Math.round((quiz / 6) * 100));
      const id = "EIA-OPS-" + Math.floor(1000 + Math.random() * 9000);
      const date = new Date().toLocaleDateString();
      view.hidden = false;
      view.innerHTML = `
        <div class="cert-inner">
          <div class="cert-head"><span class="cert-mark">EIA</span><span class="cert-title">ATOMIC OPERATIONS CERTIFICATE</span></div>
          <div class="cert-row"><span class="cert-k">CONTROLLER</span><span class="cert-v">${name}</span></div>
          <div class="cert-row"><span class="cert-k">CERTIFICATION</span><span class="cert-v">Atomic Electron Configuration Operations</span></div>
          <div class="cert-row"><span class="cert-k">SCORE</span><span class="cert-v mono">${score}%</span></div>
          <div class="cert-quals">
            <div>✓ Aufbau Boarding Sequence</div>
            <div>✓ Hund Gate Allocation</div>
            <div>✓ Pauli Security Compliance</div>
            <div>✓ Electron Configuration</div>
            <div>✓ Atomic Structure</div>
          </div>
          <div class="cert-foot"><span class="mono">CERTIFICATE ID ${id}</span><span class="mono">${date}</span></div>
          <button class="btn btn-primary" id="certPrint">PRINT CERTIFICATE</button>
        </div>`;
      $("#certPrint").addEventListener("click", () => window.print());
      logEvent("TRAINING", "Certificate generated (" + score + "%).");
    });
  }

  /* ============================================================
     BOOT
     ============================================================ */
  function boot() {
    // compute the central atom state FIRST so every module (legacy + new)
    // renders from the same canonical electron records.
    recomputeAtom();
    ensureValidSelection();

    initStartup();
    initNavigation();
    initFlip();
    initElectronTable();
    initManifest();
    initDrawer();
    initTerminalMap();
    initAufbauSimulation();
    initHundSimulation();
    initPauliSimulator();
    initEnergyLab();
    initSpectrum();
    initModelComparison();
    initQuiz();
    initGlossary();
    initReqStatus();

    // upgraded immersive-airport modules
    initBooking();
    initAirportMap();
    initGateFinder();
    initGateStatus();
    initAirportDirectory();
    initAircraftCabin();
    initSeatMap();
    initCheckIn();
    initSecurityScreening();
    initDepartures();
    initArrivals();
    initFlightStatus();
    initTimeline();
    initJourneyMode();
    initGlobalSearch();

    // atomic simulator layer
    initAtomicPassport();
    initControlTower();
    initValenceTerminal();
    initIonControl();
    initQuantumRecords();
    initViewToggle();
    initGateAssignmentLab();
    initSpectroscopy();
    initHydrogenBoard();
    initControllerChallenge();
    initCertificate();

    // default selection (E-01 exists for every atom) so panels aren't empty
    selectElectron(state.selectedElectron || "E-01", false);
    // one canonical paint of every dynamic module
    syncEntireEIA();
    logEvent("SYSTEM", currentElement().name + " loaded.");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
