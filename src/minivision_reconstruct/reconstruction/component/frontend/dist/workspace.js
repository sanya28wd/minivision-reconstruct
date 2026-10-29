import { indexBundle as ne, DEFAULT_OPTIONS as se, DEFAULT_VIEW as q, renderCamera as Z, renderMap as le, toScreen as B, trackColor as ie, toWorld as K } from "./renderer.js";
const oe = `
<div class="workspace">
 <header class="replay-heading"><div><p class="eyebrow">MINIVISION / EVIDENCE REPLAY</p><h2>Incident reconstruction</h2></div><span class="badge">Evidence-linked replay</span></header>
 <div class="panes"><section class="panel"><div class="panel-header"><span>CAMERA EVIDENCE</span><span class="camera-frame"></span></div><canvas class="camera" aria-label="Camera evidence with selectable road users"></canvas></section><section class="panel map"><div class="panel-header"><span>BIRD’S-EYE RECONSTRUCTION</span><span>2D road plane · drag to explore</span></div><canvas class="road" aria-label="Bird's-eye scene with selectable road users"></canvas></section></div>
 <div class="marker-rail" aria-label="Reviewer markers on source timeline"></div><div class="controls"><button class="play primary">▶ Play</button><button class="previous" aria-label="Previous source frame">←</button><button class="next" aria-label="Next source frame">→</button><button class="snapshot" title="Export saved scene at the current source frame">PNG</button><select class="rate" aria-label="Playback rate"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select><div class="timeline"><input class="scrub" type="range" min="0" step="1" value="0" aria-label="Replay timeline"><span class="timestamp"></span></div></div>
 <div class="tools"><button class="fit">Fit scene</button><button class="reset">Reset view</button><button class="zoom-out" aria-label="Zoom out">−</button><button class="zoom-in" aria-label="Zoom in">+</button><label><input class="paths-toggle" type="checkbox" checked>Paths</label><label><input class="labels-toggle" type="checkbox" checked>Labels</label><label><input class="features-toggle" type="checkbox">Road references</label><label><input class="markers-toggle" type="checkbox" checked>Markers</label><select class="trail" aria-label="Trajectory history"><option value="1">1 second</option><option value="3" selected>3 seconds</option><option value="all">Complete past path</option></select><select class="track-select" aria-label="Selected road user"><option value="">All road users</option></select></div>
 <div class="legend"><span><i></i>Observed / recovered</span><span><i class="dashed"></i>Interpolated</span><span>R · Reviewer observation</span><span>Gaps · Unavailable</span><span>Vehicle icons are symbolic</span></div>
 <div class="inspector"></div><div class="markers"></div>
 <details class="scene-editor"><summary>Edit visual road scene</summary><div class="editor"><div class="editor-section"><p>Calibrated lanes, crossings, and stop lines are locked. Styling does not change road-user positions. Drag empty space to pan; select a shape to edit its vertices.</p><div class="editor-row"><button data-draw="surface">Add road surface</button><button data-draw="marking">Add marking</button><button data-draw="label">Add label</button><button class="finish-shape">Finish shape</button><button class="cancel-shape">Cancel</button></div><label>Label text<input class="shape-label" placeholder="Road name or visual label" maxlength="120"></label><select class="shape-select" aria-label="Editable shape"></select><div class="editor-row"><button class="delete-shape">Delete shape</button><button class="add-vertex">Insert vertex</button><button class="delete-vertex">Delete selected vertex</button></div></div><div class="editor-section"><label>Display rotation (degrees)<input class="rotation" type="number" min="-360" max="360" step="1" value="0"></label><div class="editor-row"><button class="undo">Undo</button><button class="redo">Redo</button></div><button class="save-scene primary">Save scene revision</button><p>Scene edits are stored separately from the Week 5 calibration.</p></div></div></details>
 <details><summary>Reviewer event markers</summary><div class="editor"><div class="editor-section"><p>Pause at the exact source frame before adding a marker. These are reviewer annotations.</p><label>Marker label<input class="marker-label" placeholder="Important moment" maxlength="120"></label><label>Reviewer note<textarea class="marker-note" rows="2" maxlength="2000"></textarea></label><label>Linked road users<input class="marker-tracks" placeholder="Track IDs, comma separated"></label></div><div class="editor-section"><button class="locate-marker">Choose optional map location</button><button class="add-marker">Add marker at current frame</button><select class="marker-select" aria-label="Reviewer marker"></select><div class="editor-row"><button class="jump-marker">Jump to marker</button><button class="delete-marker">Delete marker</button></div><button class="save-markers primary">Save marker revision</button></div></div></details>
 <div class="status" role="status">Loading verified replay…</div><video muted playsinline preload="auto" crossorigin="anonymous"></video>
</div>`;
function n(c, a) {
  const h = c.querySelector(a);
  if (h === null) throw new Error(`Workspace element '${a}' is missing.`);
  return h;
}
function Q(c) {
  const a = c.getContext("2d");
  if (a === null) throw new Error("Canvas 2D is unavailable.");
  return a;
}
async function ee(c) {
  let a = new Error(`Request not started: ${c}`);
  for (let h = 1; h <= 3; h += 1)
    try {
      const f = await fetch(c);
      if (!f.ok) throw new Error(`HTTP ${f.status}, URL=${c}, response=${await f.text()}`);
      return f;
    } catch (f) {
      a = f instanceof Error ? f : new Error(String(f)), h < 3 && (console.warn("Replay request retry", { url: c, attempt: h, error: a.message }), await new Promise((s) => {
        window.setTimeout(s, h * 200);
      }));
    }
  throw a;
}
function z(c, a) {
  const h = a.getBoundingClientRect();
  return [(c.clientX - h.left) * a.width / h.width, (c.clientY - h.top) * a.height / h.height];
}
function te(c, a, h) {
  const f = c.map((s) => [s.trackId, Math.hypot(s.point[0] - a[0], s.point[1] - a[1])]).sort((s, v) => s[1] - v[1]);
  return f.length > 0 && f[0][1] <= h ? f[0][0] : null;
}
function ae(c, a) {
  const f = [...c.filter((s) => s.bounds !== void 0 && a[0] >= s.bounds[0] && a[0] <= s.bounds[2] && a[1] >= s.bounds[1] && a[1] <= s.bounds[3])].sort((s, v) => (s.bounds[2] - s.bounds[0]) * (s.bounds[3] - s.bounds[1]) - (v.bounds[2] - v.bounds[0]) * (v.bounds[3] - v.bounds[1]));
  return f.length === 0 ? null : f[0].trackId;
}
function ce(c, a) {
  return { ...c, scene: a, undo: [...c.undo, c.scene], redo: [] };
}
function ue(c) {
  const a = n(c.parentElement, ".replay-root"), h = a.dataset.workspace === void 0 ? null : JSON.parse(a.dataset.workspace);
  a.dataset.reviewer = c.data.reviewer, a.innerHTML = oe;
  const f = n(a, ".status"), s = n(a, "video"), v = n(a, ".camera"), _ = n(a, ".road"), j = Q(v), w = Q(_), N = n(a, ".scrub");
  let L = !1, M = null, g = null, U = 0, e = null, u = null, x = null, y = null, D = [], b = null;
  const H = [];
  function o(t, r, l) {
    t.addEventListener(r, l), H.push(() => t.removeEventListener(r, l));
  }
  function C(t) {
    s.pause(), f.className = "status error", f.textContent = t instanceof Error ? t.message : String(t);
  }
  function I(t) {
    t.catch(C);
  }
  function $(t) {
    f.className = "status", f.textContent = t;
  }
  function W() {
    for (const t of [v, _]) {
      const r = t.getBoundingClientRect();
      t.width = Math.max(320, Math.round(r.width)), t.height = Math.max(240, Math.round(r.height));
    }
    m();
  }
  function m() {
    var T;
    if (e === null || u === null || x === null) return;
    const t = { ...u, scene: e.scene, markers: e.markers };
    g !== null ? D = Z(j, g, t, x, e.frame, e.selected) : s.readyState >= 2 && (D = Z(j, s, t, x, e.frame, e.selected)), y = le(w, t, x, e.frame, e.scene, e.view, e.options, e.selected), e.draft.length > 0 && (w.strokeStyle = "#B66542", w.setLineDash([4, 4]), w.beginPath(), e.draft.forEach((E, O) => {
      const X = B(E, y.transform);
      O === 0 ? w.moveTo(...X) : w.lineTo(...X);
    }), w.stroke(), w.setLineDash([]));
    const r = e.scene.shapes.find((E) => E.shape_id === e.shapeId);
    if (r !== void 0) for (let E = 0; E < r.points.length; E += 1) {
      const O = B(r.points[E], y.transform);
      w.fillStyle = E === e.vertex ? "#B66542" : "#FFF", w.strokeStyle = "#B66542", w.beginPath(), w.arc(...O, 5, 0, Math.PI * 2), w.fill(), w.stroke();
    }
    n(a, ".eyebrow").textContent = `MINIVISION / ${u.manifest.case_id.replaceAll("-", " ").toUpperCase()}`;
    const l = u.source_frames[e.frame];
    a.dataset.sourceFrame = String(e.frame), a.dataset.selectedTrack = e.selected === null ? "" : String(e.selected), n(a, ".camera-frame").textContent = `Frame ${e.frame}`, n(a, ".timestamp").textContent = `${l.source_time.toFixed(3)} s · ${e.frame}`, n(a, ".play").textContent = s.paused ? "▶ Play" : "Ⅱ Pause", n(a, ".previous").disabled = e.frame === 0, n(a, ".next").disabled = e.frame >= u.source_frames.length - 1, n(a, ".track-select").value = e.selected === null ? "" : String(e.selected);
    const i = n(a, ".inspector");
    i.replaceChildren();
    const p = document.createElement("strong");
    p.textContent = e.selected === null ? `${x.trackIds.length} road users` : `Road user #${e.selected}`, p.style.color = e.selected === null ? "#203941" : ie(e.selected), i.append(p);
    const d = document.createElement("span"), k = (T = x.byFrame.get(e.frame)) == null ? void 0 : T.find((E) => E.track_id === e.selected);
    d.className = "muted", d.textContent = e.selected === null ? "Select a road user in either view to inspect its evidence." : k === void 0 || k.position === null ? "Unavailable in this source frame · no position inferred" : `${k.class_name} · ${k.state} · ${k.provenance} · detection confidence ${k.detection_confidence === null ? "unavailable" : k.detection_confidence.toFixed(2)}`, i.append(d);
  }
  async function S(t) {
    if (u === null || e === null) return;
    s.pause(), M !== null && (s.cancelVideoFrameCallback(M), M = null);
    const r = ++U, l = await ee(`${c.data.frame_url}${t}`), i = await createImageBitmap(await l.blob());
    if (L || r !== U) {
      i.close();
      return;
    }
    g == null || g.close(), g = i, e = { ...e, frame: t }, N.value = String(t), m();
  }
  function Y(t) {
    var l;
    if (e === null || x === null || t === null) return "Select a road user · drag empty map space to pan";
    const r = (l = x.byFrame.get(e.frame)) == null ? void 0 : l.find((i) => i.track_id === t);
    return r === void 0 ? `Road user #${t} unavailable` : `#${t} · ${r.class_name} · frame ${r.source_frame} · ${r.source_time.toFixed(3)} s · ${r.state} · ${r.provenance} · detection confidence ${r.detection_confidence ?? "unavailable"}`;
  }
  function A(t) {
    e !== null && (e = { ...e, selected: t }, m());
  }
  function J(t, r) {
    if (L || u === null || e === null) return;
    const l = u.replay_frames[Math.min(u.replay_frames.length - 1, Math.floor(r.mediaTime * u.manifest.replay_fps + 1e-3))];
    e = { ...e, frame: l.source_frame }, N.value = String(e.frame), m(), s.paused || (M = s.requestVideoFrameCallback(J));
  }
  async function re() {
    if (e === null || u === null) return;
    if (!s.paused) {
      s.pause(), I(S(e.frame));
      return;
    }
    const t = u.source_frames[e.frame].source_time - u.source_frames[0].source_time;
    Math.abs(s.currentTime - t) > 1e-3 && await new Promise((r, l) => {
      const i = () => {
        s.removeEventListener("error", p), r();
      }, p = () => {
        s.removeEventListener("seeked", i), l(new Error(`Video seek failed at source frame ${e == null ? void 0 : e.frame}.`));
      };
      s.addEventListener("seeked", i, { once: !0 }), s.addEventListener("error", p, { once: !0 }), s.currentTime = Math.min(t, Math.max(0, s.duration - 1 / 30));
    }), g == null || g.close(), g = null, await s.play(), M = s.requestVideoFrameCallback(J), m();
  }
  function R(t) {
    e !== null && (e = ce(e, t), F(), m());
  }
  function F() {
    if (e === null) return;
    const t = n(a, ".shape-select");
    t.replaceChildren(new Option("Select visual shape", ""));
    for (const r of e.scene.shapes) t.add(new Option(`${r.kind} · ${r.label || r.shape_id}`, r.shape_id));
    t.value = e.shapeId ?? "", n(a, ".rotation").value = String(e.scene.rotation_degrees);
  }
  function P() {
    if (e === null) return;
    const t = n(a, ".marker-select"), r = n(a, ".markers"), l = n(a, ".marker-rail");
    l.replaceChildren(), t.replaceChildren(new Option("Select reviewer marker", "")), r.replaceChildren();
    for (const i of e.markers.markers) {
      t.add(new Option(`${i.source_time.toFixed(2)} s · ${i.label}`, i.marker_id));
      const p = document.createElement("button");
      p.className = "marker-chip", p.textContent = `◇ ${i.source_time.toFixed(2)} s · ${i.label}`, p.title = `${i.reviewer}: ${i.note}`, p.onclick = () => {
        I(S(i.source_frame));
      }, r.append(p);
      const d = document.createElement("button");
      d.className = "timeline-pin", d.textContent = "◆", d.title = `${i.source_time.toFixed(3)} s · ${i.label} · reviewer: ${i.reviewer}`, d.style.left = `${100 * i.source_frame / Math.max(1, ((u == null ? void 0 : u.source_frames.length) ?? 1) - 1)}%`, d.onclick = () => {
        I(S(i.source_frame));
      }, l.append(d);
    }
  }
  o(n(a, ".play"), "click", () => I(re())), o(n(a, ".snapshot"), "click", () => {
    e !== null && c.setTriggerValue("snapshot_request", { source_frame: e.frame, action_id: crypto.randomUUID() });
  }), o(n(a, ".previous"), "click", () => {
    e !== null && I(S(Math.max(0, e.frame - 1)));
  }), o(n(a, ".next"), "click", () => {
    e !== null && u !== null && I(S(Math.min(u.source_frames.length - 1, e.frame + 1)));
  }), o(N, "input", () => I(S(Number(N.value)))), o(n(a, ".rate"), "change", () => {
    s.playbackRate = Number(n(a, ".rate").value);
  }), o(n(a, ".track-select"), "change", () => {
    const t = n(a, ".track-select").value;
    A(t === "" ? null : Number(t));
  });
  for (const t of ["paths", "labels", "features", "markers"]) o(n(a, `.${t}-toggle`), "change", () => {
    e !== null && (e = { ...e, options: { ...e.options, [t]: n(a, `.${t}-toggle`).checked } }, m());
  });
  o(n(a, ".trail"), "change", () => {
    if (e !== null) {
      const t = n(a, ".trail").value;
      e = { ...e, options: { ...e.options, trailSeconds: t === "all" ? null : Number(t) } }, m();
    }
  }), o(n(a, ".reset"), "click", () => {
    e !== null && (e = { ...e, view: q }, m());
  }), o(n(a, ".fit"), "click", () => {
    if (e === null) return;
    const t = [...e.scene.references.flatMap((r) => r.points), ...e.scene.shapes.flatMap((r) => r.points)];
    if (t.length > 0) {
      const r = t.map((T) => T[0]), l = t.map((T) => T[1]), i = Math.min(...r), p = Math.max(...r), d = Math.min(...l), k = Math.max(...l);
      p > i && k > d && R({ ...e.scene, bounds: [i - (p - i) * 0.1, d - (k - d) * 0.1, p + (p - i) * 0.1, k + (k - d) * 0.1] });
    }
    e = { ...e, view: q }, m();
  });
  function G(t) {
    e !== null && (e = { ...e, view: { ...e.view, zoom: Math.max(0.4, Math.min(5, e.view.zoom * t)) } }, m());
  }
  o(n(a, ".zoom-in"), "click", () => G(1.2)), o(n(a, ".zoom-out"), "click", () => G(1 / 1.2)), o(v, "click", (t) => {
    const r = v.getBoundingClientRect(), l = [(t.clientX - r.left) * v.width / r.width, (t.clientY - r.top) * v.height / r.height];
    A(ae(D, l));
  }), o(v, "pointermove", (t) => {
    v.title = Y(ae(D, z(t, v)));
  }), o(_, "pointerdown", (t) => {
    if (e === null || y === null) return;
    const r = z(t, _), l = K(r, y.transform);
    if (e.locatingMarker) {
      e = { ...e, markerLocation: l, locatingMarker: !1 }, $("Marker location selected.");
      return;
    }
    if (e.drawing !== null) {
      e = { ...e, draft: [...e.draft, l] }, m();
      return;
    }
    const i = e.scene.shapes.find((d) => d.shape_id === e.shapeId), p = (i == null ? void 0 : i.points.findIndex((d) => {
      const k = B(d, y.transform);
      return Math.hypot(k[0] - r[0], k[1] - r[1]) < 12;
    })) ?? -1;
    if (p >= 0)
      e = { ...e, vertex: p }, b = { start: r, view: e.view, original: e.scene, vertex: p, shapeId: e.shapeId };
    else {
      const d = te(y.hits, r, 24);
      if (d !== null) {
        A(d);
        return;
      }
      b = { start: r, view: e.view, original: e.scene, vertex: null, shapeId: null };
    }
    _.setPointerCapture(t.pointerId);
  }), o(_, "pointermove", (t) => {
    if (e === null || y === null) return;
    const r = z(t, _);
    if (b === null) {
      _.title = Y(te(y.hits, r, 24));
      return;
    }
    if (b.vertex !== null && b.shapeId !== null) {
      const l = K(r, y.transform);
      e = { ...e, scene: { ...e.scene, shapes: e.scene.shapes.map((i) => i.shape_id === b.shapeId ? { ...i, points: i.points.map((p, d) => d === b.vertex ? l : p) } : i) } };
    } else e = { ...e, view: { ...b.view, pan: [b.view.pan[0] + r[0] - b.start[0], b.view.pan[1] + r[1] - b.start[1]] } };
    m();
  }), o(_, "pointerup", () => {
    e !== null && b !== null && b.vertex !== null && (e = { ...e, undo: [...e.undo, b.original], redo: [] }), b = null;
  });
  for (const t of a.querySelectorAll("[data-draw]")) o(t, "click", () => {
    e !== null && (s.pause(), e = { ...e, drawing: t.dataset.draw, draft: [], shapeId: null }, $("Click the map to place vertices, then finish the shape."), m());
  });
  o(n(a, ".finish-shape"), "click", () => {
    if (e === null || e.drawing === null) return;
    const t = { surface: 3, marking: 2, label: 1 }[e.drawing], r = n(a, ".shape-label").value.trim();
    if (e.draft.length < t || e.drawing === "label" && (e.draft.length !== 1 || !r)) {
      C(new Error(`A ${e.drawing} requires ${t} points${e.drawing === "label" ? " and text" : ""}.`));
      return;
    }
    const l = { shape_id: crypto.randomUUID(), kind: e.drawing, points: e.draft, label: r };
    R({ ...e.scene, shapes: [...e.scene.shapes, l] }), e = { ...e, drawing: null, draft: [], shapeId: l.shape_id }, F(), $("Visual shape added; save a revision to retain it."), m();
  }), o(n(a, ".cancel-shape"), "click", () => {
    e !== null && (e = { ...e, drawing: null, draft: [] }, m());
  }), o(n(a, ".shape-select"), "change", () => {
    if (e !== null) {
      e = { ...e, shapeId: n(a, ".shape-select").value || null, vertex: null };
      const t = e.scene.shapes.find((r) => r.shape_id === e.shapeId);
      n(a, ".shape-label").value = (t == null ? void 0 : t.label) ?? "", m();
    }
  }), o(n(a, ".shape-label"), "change", () => {
    if (e === null || e.shapeId === null) return;
    const t = n(a, ".shape-label").value.trim(), r = e.scene.shapes.find((l) => l.shape_id === e.shapeId);
    if ((r == null ? void 0 : r.kind) === "label" && !t) {
      C(new Error("A visual label requires text."));
      return;
    }
    R({ ...e.scene, shapes: e.scene.shapes.map((l) => l.shape_id === e.shapeId ? { ...l, label: t } : l) });
  }), o(n(a, ".delete-shape"), "click", () => {
    e !== null && e.shapeId !== null && (R({ ...e.scene, shapes: e.scene.shapes.filter((t) => t.shape_id !== e.shapeId) }), e = { ...e, shapeId: null, vertex: null }, m());
  }), o(n(a, ".add-vertex"), "click", () => {
    if (e === null) return;
    const t = e.scene.shapes.find((d) => d.shape_id === e.shapeId);
    if (t === void 0 || t.kind === "label") return;
    const r = e.vertex ?? 0, l = t.points[r], i = t.points[(r + 1) % t.points.length], p = [...t.points.slice(0, r + 1), [(l[0] + i[0]) / 2, (l[1] + i[1]) / 2], ...t.points.slice(r + 1)];
    R({ ...e.scene, shapes: e.scene.shapes.map((d) => d.shape_id === t.shape_id ? { ...d, points: p } : d) });
  }), o(n(a, ".delete-vertex"), "click", () => {
    if (e === null || e.vertex === null) return;
    const t = e.scene.shapes.find((r) => r.shape_id === e.shapeId);
    if (t === void 0 || t.points.length <= (t.kind === "surface" ? 3 : t.kind === "marking" ? 2 : 1)) {
      $("Minimum vertex count reached; delete the whole shape instead.");
      return;
    }
    R({ ...e.scene, shapes: e.scene.shapes.map((r) => r.shape_id === t.shape_id ? { ...r, points: r.points.filter((l, i) => i !== e.vertex) } : r) }), e = { ...e, vertex: null }, m();
  }), o(n(a, ".rotation"), "change", () => {
    if (e !== null) {
      const t = Number(n(a, ".rotation").value);
      if (!Number.isFinite(t)) {
        C(new Error("Rotation must be finite."));
        return;
      }
      R({ ...e.scene, rotation_degrees: t });
    }
  }), o(n(a, ".undo"), "click", () => {
    e === null || e.undo.length === 0 || (e = { ...e, scene: e.undo[e.undo.length - 1], undo: e.undo.slice(0, -1), redo: [...e.redo, e.scene] }, F(), m());
  }), o(n(a, ".redo"), "click", () => {
    e === null || e.redo.length === 0 || (e = { ...e, scene: e.redo[e.redo.length - 1], redo: e.redo.slice(0, -1), undo: [...e.undo, e.scene] }, F(), m());
  }), o(n(a, ".save-scene"), "click", () => {
    if (e !== null) {
      if (!c.data.reviewer.trim()) {
        C(new Error("Enter a reviewer name in the application before saving."));
        return;
      }
      c.setTriggerValue("scene_save", { document: e.scene, reviewer: c.data.reviewer, action_id: crypto.randomUUID() }), $("Saving scene revision…");
    }
  }), o(n(a, ".locate-marker"), "click", () => {
    e !== null && (s.pause(), e = { ...e, locatingMarker: !0 }, $("Click the map to choose the marker location."));
  }), o(n(a, ".add-marker"), "click", () => {
    if (e === null || u === null || x === null) return;
    if (!s.paused) {
      C(new Error("Pause before annotating a source frame."));
      return;
    }
    const t = n(a, ".marker-label").value.trim(), r = c.data.reviewer.trim(), l = n(a, ".marker-tracks").value.trim(), i = l ? l.split(",").map((d) => Number(d.trim())) : e.selected === null ? [] : [e.selected];
    if (!t || !r || i.some((d) => !Number.isInteger(d) || !x.trackIds.includes(d))) {
      C(new Error("Enter a label, reviewer, and valid track IDs."));
      return;
    }
    const p = { marker_id: crypto.randomUUID(), source_frame: e.frame, source_time: u.source_frames[e.frame].source_time, position: e.markerLocation, track_ids: i, label: t, note: n(a, ".marker-note").value, reviewer: r, created_at: (/* @__PURE__ */ new Date()).toISOString() };
    e = { ...e, markers: { ...e.markers, markers: [...e.markers.markers, p] }, markerLocation: null }, P(), m(), $("Reviewer marker added; save a revision to retain it.");
  }), o(n(a, ".jump-marker"), "click", () => {
    const t = n(a, ".marker-select").value, r = e == null ? void 0 : e.markers.markers.find((l) => l.marker_id === t);
    r !== void 0 && I(S(r.source_frame));
  }), o(n(a, ".delete-marker"), "click", () => {
    if (e !== null) {
      const t = n(a, ".marker-select").value;
      e = { ...e, markers: { ...e.markers, markers: e.markers.markers.filter((r) => r.marker_id !== t) } }, P(), m();
    }
  }), o(n(a, ".save-markers"), "click", () => {
    if (e === null || !c.data.reviewer.trim()) {
      C(new Error("Enter a reviewer name before saving."));
      return;
    }
    c.setTriggerValue("marker_save", { document: e.markers, reviewer: c.data.reviewer, action_id: crypto.randomUUID() }), $("Saving marker revision…");
  }), o(s, "error", () => {
    var t;
    return C(new Error(`Replay video failed: URL=${c.data.video_url}, media error=${(t = s.error) == null ? void 0 : t.code}`));
  }), o(s, "ended", () => {
    e !== null && I(S(e.frame));
  });
  const V = new ResizeObserver(W);
  return V.observe(_), V.observe(v), I((async () => {
    if (!("requestVideoFrameCallback" in s)) throw new Error("This browser requires requestVideoFrameCallback; use a current Chrome, Edge, or Safari.");
    if (u = await (await ee(c.data.bundle_url)).json(), L) return;
    a.dataset.caseId = u.manifest.case_id, x = ne(u), e = h ?? { frame: 0, selected: null, view: q, options: se, scene: u.scene, markers: u.markers, undo: [], redo: [], drawing: null, draft: [], shapeId: null, vertex: null, markerLocation: null, locatingMarker: !1 };
    const t = c.data.seek;
    t != null && a.dataset.seekAction !== t.action_id && (a.dataset.seekAction = t.action_id, e = { ...e, frame: Math.min(Math.max(0, t.source_frame), u.source_frames.length - 1), selected: t.track_id ?? e.selected }), n(a, ".replay-heading h2").textContent = u.manifest.case_id.replace(/^demo-/, "").replaceAll("-", " ").replace(/^./, (l) => l.toUpperCase()), n(a, ".badge").textContent = u.scene.mode === "metric" ? "Validated metric calibration" : "Validated relative calibration", N.max = String(u.source_frames.length - 1);
    const r = n(a, ".track-select");
    for (const l of x.trackIds) r.add(new Option(`Road user #${l}`, String(l)));
    s.src = c.data.video_url, F(), P(), W(), await S(e.frame), $(`Tracks: ${u.manifest.track_revision} · calibration: ${u.manifest.calibration_revision} · reviewer annotations`);
  })()), () => {
    e !== null && (a.dataset.workspace = JSON.stringify(e)), L = !0, U += 1, M !== null && s.cancelVideoFrameCallback(M), s.pause(), s.removeAttribute("src"), s.load(), g == null || g.close(), V.disconnect();
    for (const t of H) t();
  };
}
export {
  ue as default
};
