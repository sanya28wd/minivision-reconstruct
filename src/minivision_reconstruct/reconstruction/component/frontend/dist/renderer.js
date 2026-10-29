const b = ["#F4B942", "#EF6461", "#66B9CB", "#867AC6", "#F19972", "#71B58B", "#638DCE", "#D98AB4"], B = { paths: !0, labels: !0, features: !1, markers: !0, trailSeconds: 3 }, L = { zoom: 1, pan: [0, 0] };
function F(e) {
  return b[((e - 1) % b.length + b.length) % b.length];
}
function W(e) {
  const n = /* @__PURE__ */ new Map(), o = /* @__PURE__ */ new Map();
  for (const a of e.observations)
    n.set(a.source_frame, [...n.get(a.source_frame) ?? [], a]), o.set(a.track_id, [...o.get(a.track_id) ?? [], a]);
  return { byFrame: n, byTrack: o, trackIds: [...o.keys()].sort((a, u) => a - u) };
}
function T(e, n, o, a) {
  const [u, d, r, s] = e.bounds, i = e.rotation_degrees * Math.PI / 180, t = Math.abs(Math.cos(i)) * (r - u) + Math.abs(Math.sin(i)) * (s - d), l = Math.abs(Math.sin(i)) * (r - u) + Math.abs(Math.cos(i)) * (s - d);
  return { scale: Math.min((n - 100) / t, (o - 100) / l) * a.zoom, angle: i, center: [(u + r) / 2, (d + s) / 2], screenCenter: [n / 2 + a.pan[0], o / 2 + a.pan[1]] };
}
function p(e, n) {
  const o = e[0] - n.center[0], a = e[1] - n.center[1];
  return [n.screenCenter[0] + n.scale * (o * Math.cos(n.angle) - a * Math.sin(n.angle)), n.screenCenter[1] - n.scale * (o * Math.sin(n.angle) + a * Math.cos(n.angle))];
}
function O(e, n) {
  const o = (e[0] - n.screenCenter[0]) / n.scale, a = -(e[1] - n.screenCenter[1]) / n.scale;
  return [n.center[0] + o * Math.cos(n.angle) + a * Math.sin(n.angle), n.center[1] - o * Math.sin(n.angle) + a * Math.cos(n.angle)];
}
function P(e, n, o) {
  e.beginPath(), n.forEach((a, u) => {
    const d = p(a, o);
    u === 0 ? e.moveTo(...d) : e.lineTo(...d);
  });
}
function D(e, n, o) {
  if (n.kind === "label") {
    const a = p(n.points[0], o);
    e.fillStyle = "#52605C", e.font = "600 13px system-ui", e.fillText(n.label, ...a);
    return;
  }
  P(e, n.points, o), n.kind === "surface" ? (e.closePath(), e.fillStyle = "#424B50", e.fill(), e.strokeStyle = "#C9C6BD", e.lineWidth = 5, e.stroke()) : (e.strokeStyle = "#F4EDE0", e.lineWidth = 2, e.setLineDash([12, 10]), e.stroke(), e.setLineDash([]));
}
function I(e, n, o, a) {
  var r;
  const u = n.shapes.filter((s) => s.kind === "surface"), d = /* @__PURE__ */ new Map();
  for (const s of u) for (let i = 0; i < s.points.length; i += 1) {
    const t = s.points[i], l = s.points[(i + 1) % s.points.length], h = [t.map((y) => y.toFixed(4)).join(","), l.map((y) => y.toFixed(4)).join(",")].sort().join("/");
    d.set(h, { points: [t, l], count: (((r = d.get(h)) == null ? void 0 : r.count) ?? 0) + 1 });
  }
  for (const s of d.values())
    s.count === 1 && (P(e, s.points, o), e.strokeStyle = "#D5CEBA", e.lineWidth = 16, e.lineJoin = "round", e.stroke(), e.strokeStyle = "#F5F0DD", e.lineWidth = 3, e.stroke());
  e.beginPath();
  for (const s of u)
    s.points.forEach((i, t) => {
      const l = p(i, o);
      t === 0 ? e.moveTo(...l) : e.lineTo(...l);
    }), e.closePath();
  e.fillStyle = "#44444F", e.fill();
  for (const s of n.references)
    if (!(!a && (s.kind === "lane" || s.kind === "monitored_zone")))
      if (P(e, s.points, o), e.lineWidth = 2, s.kind === "stop_line")
        e.strokeStyle = "#FFF8E8", e.lineWidth = 5, e.stroke();
      else if (s.kind === "crossing") {
        e.closePath(), e.save(), e.clip();
        const i = s.points.map((f) => p(f, o)), t = i.map((f) => f[0]), l = i.map((f) => f[1]), h = Math.min(...t), y = Math.min(...l), m = Math.max(...t) - h, k = Math.max(...l) - y;
        if (e.fillStyle = "#FCFAEE", m > k) for (let f = h; f < h + m; f += 13) e.fillRect(f, y, 7, k);
        else for (let f = y; f < y + k; f += 13) e.fillRect(h, f, m, 7);
        e.restore();
      } else s.kind === "monitored_zone" ? (e.closePath(), e.fillStyle = "#1D9A9C15", e.fill(), e.strokeStyle = "#167E9A", e.setLineDash([4, 5]), e.stroke(), e.setLineDash([])) : (e.closePath(), e.strokeStyle = "#ECE7D0A0", e.lineWidth = 1.5, e.setLineDash([9, 9]), e.stroke(), e.setLineDash([]));
  n.shapes.filter((s) => s.kind !== "surface").forEach((s) => D(e, s, o));
}
function C(e, n, o, a, u, d, r) {
  for (const [s, i] of n.byTrack) {
    const t = i.filter((l) => l.source_frame <= o && (r === null || l.source_time >= a - r));
    for (let l = 1; l < t.length; l += 1) {
      const h = t[l - 1], y = t[l];
      h.position === null || y.position === null || y.source_frame !== h.source_frame + 1 || (e.globalAlpha = d === null || d === s ? 0.75 : 0.16, e.strokeStyle = F(s), e.lineWidth = d === s ? 3.5 : 2, e.setLineDash(h.state === "interpolated" || y.state === "interpolated" ? [5, 5] : s > b.length ? [14, 3] : []), P(e, [h.position, y.position], u), e.stroke());
    }
  }
  e.setLineDash([]), e.globalAlpha = 1;
}
function R(e, n, o) {
  let a = null, u = !1;
  for (let d = 1; d < e.length; d += 1) {
    const r = e[d];
    if (r.source_frame > n) break;
    const s = e[d - 1];
    if (r.position === null || s.position === null || r.source_frame !== s.source_frame + 1) {
      a = null, u = !1;
      continue;
    }
    let i = d - 1;
    for (; i > Math.max(0, d - 3) && e[i - 1].position !== null && e[i].source_frame === e[i - 1].source_frame + 1; ) i -= 1;
    const t = e[i].position;
    if (t === null) continue;
    const l = p(t, o), h = p(r.position, o), y = Math.hypot(h[0] - l[0], h[1] - l[1]);
    u = y >= 2, (u || a === null && y > 1e-6) && (a = Math.atan2(h[1] - l[1], h[0] - l[0]));
  }
  return { angle: a, moving: u };
}
function w(e, n, o, a, u) {
  const d = a.angle;
  e.save(), e.translate(...o), e.rotate(d === null ? 0 : d + Math.PI / 2), e.shadowColor = "#18262A35", e.shadowBlur = 5, e.shadowOffsetY = 2;
  const r = F(n.track_id), s = n.state === "interpolated";
  if (e.fillStyle = s ? "#FAF7EF" : r, e.strokeStyle = r, e.lineWidth = u ? 3 : 1.5, e.setLineDash(s ? [4, 3] : []), d === null && n.class_name !== "person")
    e.beginPath(), e.arc(0, 0, 10, 0, Math.PI * 2), e.fill(), e.stroke();
  else if (n.class_name === "person")
    e.beginPath(), e.arc(0, -5, 4, 0, Math.PI * 2), e.fill(), e.stroke(), e.beginPath(), e.roundRect(-5, 1, 10, 10, 4), e.fill(), e.stroke();
  else if (n.class_name === "bicycle" || n.class_name === "motorcycle")
    e.beginPath(), e.roundRect(-3, -14, 6, 28, 3), e.fill(), e.stroke(), e.beginPath(), e.moveTo(-7, -6), e.lineTo(7, -6), e.stroke();
  else {
    const i = n.class_name === "bus" ? 48 : n.class_name === "truck" ? 42 : 36;
    e.fillStyle = "#202630";
    for (const t of [-12, 8]) for (const l of [-i / 2 + 6, i / 2 - 12])
      e.beginPath(), e.roundRect(t, l, 4, 7, 1), e.fill();
    e.fillStyle = s ? "#FAF7EF" : r, e.beginPath(), e.roundRect(-10, -i / 2, 20, i, 5), e.fill(), e.stroke(), e.setLineDash([]), e.shadowBlur = 0, e.fillStyle = "#FFFFFF35", e.beginPath(), e.roundRect(-8, -i / 2 + 2, 16, i - 4, 4), e.fill(), e.fillStyle = "#263B50", e.beginPath(), e.roundRect(-7, -i / 2 + 7, 14, 7, 2), e.fill(), e.fillStyle = r, e.beginPath(), e.roundRect(-6, -2, 12, 10, 2), e.fill(), e.fillStyle = "#33475B", e.fillRect(-7, i / 2 - 9, 14, 4), e.fillStyle = "#FFF8CD", e.fillRect(-8, -i / 2 + 2, 4, 2), e.fillRect(4, -i / 2 + 2, 4, 2), e.fillStyle = "#B94145", e.fillRect(-8, i / 2 - 3, 4, 2), e.fillRect(4, i / 2 - 3, 4, 2);
  }
  e.shadowBlur = 0, e.setLineDash([]), d !== null && a.moving && (e.strokeStyle = r, e.lineWidth = 2, e.beginPath(), e.moveTo(0, -23), e.lineTo(0, -32), e.moveTo(-4, -28), e.lineTo(0, -33), e.lineTo(4, -28), e.stroke()), e.restore(), (n.provenance === "reviewer" || n.state === "manual") && (e.fillStyle = "#FFF", e.beginPath(), e.arc(o[0] + 11, o[1] + 10, 5, 0, Math.PI * 2), e.fill(), e.fillStyle = r, e.font = "bold 8px system-ui", e.fillText("R", o[0] + 8, o[1] + 13));
}
function A(e, n, o, a, u, d) {
  e.font = "600 12px system-ui";
  const r = e.measureText(n).width + 14, i = [
    [o[0] + 17, o[1] - 14],
    [o[0] - r - 17, o[1] - 14],
    [o[0] + 17, o[1] + 14],
    ...[42, 70, 98, 126, 154].flatMap((l) => [
      [o[0] - r / 2, o[1] - l],
      [o[0] - r / 2, o[1] + l],
      [o[0] + l, o[1] - 14],
      [o[0] - r - l, o[1] - 14],
      [o[0] + l, o[1] - l],
      [o[0] - r - l, o[1] - l],
      [o[0] + l, o[1] + l],
      [o[0] - r - l, o[1] + l]
    ])
  ].map((l) => [Math.max(5, Math.min(u - r - 5, l[0])), Math.max(5, Math.min(d - 25, l[1])), r, 23]), t = i.find((l) => a.every((h) => l[0] + l[2] < h[0] || l[0] > h[0] + h[2] || l[1] + l[3] < h[1] || l[1] > h[1] + h[3])) ?? i[0];
  return Math.hypot(t[0] + r / 2 - o[0], t[1] + 11 - o[1]) > 42 && (e.strokeStyle = "#65716C", e.lineWidth = 1, e.beginPath(), e.moveTo(...o), e.lineTo(t[0] + r / 2, t[1] + 11), e.stroke()), e.fillStyle = "#FFFDF3ED", e.beginPath(), e.roundRect(...t, 6), e.fill(), e.fillStyle = "#233A43", e.fillText(n, t[0] + 7, t[1] + 16), t;
}
function v(e, n, o) {
  const a = new Path2D();
  for (const i of n.shapes.filter((t) => t.kind === "surface"))
    i.points.forEach((t, l) => {
      const h = p(t, o);
      l === 0 ? a.moveTo(...h) : a.lineTo(...h);
    }), a.closePath();
  const [u, d, r, s] = n.bounds;
  e.save(), e.lineWidth = 42;
  for (let i = u + 3; i < r; i += 5.5) for (let t = d + 3; t < s; t += 5.5) {
    const l = p([i + Math.sin(i * 2 + t) * 1.4, t + Math.cos(t * 2 + i) * 1.4], o);
    if (e.isPointInPath(a, ...l) || e.isPointInStroke(a, ...l)) continue;
    const h = Math.min(25, (2.5 + Math.sin(i + t) * 0.4) * o.scale);
    e.shadowColor = "#29372245", e.shadowBlur = 3, e.shadowOffsetX = 4, e.shadowOffsetY = 6, e.fillStyle = "#678B37", e.beginPath(), e.arc(...l, h, 0, Math.PI * 2), e.fill(), e.shadowBlur = 0, e.shadowOffsetX = 0, e.shadowOffsetY = 0;
    for (let y = 0; y < 5; y += 1) {
      const m = y * Math.PI * 2 / 5;
      e.fillStyle = y % 2 === 0 ? "#BCD655" : "#9EBE45", e.beginPath(), e.arc(l[0] + Math.cos(m) * h * 0.4, l[1] + Math.sin(m) * h * 0.4, h * 0.57, 0, Math.PI * 2), e.fill();
    }
  }
  e.restore();
}
function N(e, n, o, a, u, d, r, s) {
  const i = e.canvas.width, t = e.canvas.height, l = T(u, i, t, d);
  e.clearRect(0, 0, i, t), e.fillStyle = n.manifest.case_id.startsWith("demo-") ? "#ABC661" : "#E8E9DF", e.fillRect(0, 0, i, t), n.manifest.case_id.startsWith("demo-") && v(e, u, l), I(e, u, l, r.features);
  const h = n.source_frames[a].source_time;
  if (r.paths && C(e, o, a, h, l, s, r.trailSeconds), r.markers) for (const f of n.markers.markers) {
    if (f.position === null || f.source_frame > a) continue;
    const g = p(f.position, l);
    e.fillStyle = "#B34E3E", e.beginPath(), e.arc(...g, 7, 0, Math.PI * 2), e.fill(), e.strokeStyle = "#FFF", e.lineWidth = 2, e.stroke();
  }
  const y = [], m = [], k = [...o.byFrame.get(a) ?? []].sort((f, g) => +(g.track_id === s) - +(f.track_id === s));
  for (const f of k) {
    if (f.position === null) continue;
    const g = p(f.position, l), M = g[0] < 20 || g[0] > i - 20 || g[1] < 25 || g[1] > t - 55, S = [Math.max(20, Math.min(i - 20, g[0])), Math.max(25, Math.min(t - 55, g[1]))];
    e.globalAlpha = s === null || f.track_id === s ? 1 : 0.22, M ? (e.fillStyle = F(f.track_id), e.beginPath(), e.arc(...S, 6, 0, Math.PI * 2), e.fill()) : w(e, f, S, R(o.byTrack.get(f.track_id) ?? [], a, l), f.track_id === s), y.push({ trackId: f.track_id, point: S });
  }
  if (r.labels) for (const f of y) {
    e.globalAlpha = s === null || f.trackId === s ? 1 : 0.22;
    const g = f.point[0] === 20 || f.point[0] === i - 20 || f.point[1] === 25 || f.point[1] === t - 55;
    m.push(A(e, `#${f.trackId}${g ? " ↗" : ""}`, f.point, m, i, t));
  }
  if (e.globalAlpha = 1, e.fillStyle = "#596461", e.font = "12px system-ui", e.fillStyle = "#FCFDF3ED", e.beginPath(), e.roundRect(10, 9, 350, 26, 8), e.fill(), e.fillStyle = "#46544A", e.fillText(u.mode === "metric" ? "METRIC ROAD PLANE · SYMBOLIC VEHICLES" : "RELATIVE COORDINATES · SYMBOLIC VEHICLES", 20, 25), u.mode === "metric") {
    const f = 100 / l.scale, g = 10 ** Math.floor(Math.log10(f)), M = Math.max(g, Math.floor(f / g) * g);
    e.strokeStyle = "#596461", e.lineWidth = 2, e.beginPath(), e.moveTo(22, t - 25), e.lineTo(22 + M * l.scale, t - 25), e.stroke(), e.fillText(`${Number(M.toPrecision(2))} m`, 22, t - 34);
  }
  return { transform: l, hits: y };
}
function $(e, n, o, a, u, d) {
  const r = e.canvas.width, s = e.canvas.height, i = o.manifest.source_width, t = o.manifest.source_height, l = Math.min(r / i, s / t), h = (r - i * l) / 2, y = (s - t * l) / 2;
  e.fillStyle = "#17232D", e.fillRect(0, 0, r, s), e.drawImage(n, h, y, i * l, t * l);
  const m = [];
  for (const k of a.byFrame.get(u) ?? []) {
    if (k.bbox === null) continue;
    const [f, g, M, S] = k.bbox, E = h + f * l, _ = y + g * l;
    e.globalAlpha = d === null || d === k.track_id ? 1 : 0.25, e.strokeStyle = F(k.track_id), e.lineWidth = d === k.track_id ? 3 : 2, e.setLineDash(k.state === "interpolated" ? [5, 5] : []), e.strokeRect(E, _, (M - f) * l, (S - g) * l), e.setLineDash([]), e.fillStyle = F(k.track_id), e.fillRect(E, Math.max(y, _ - 22), 58, 22), e.fillStyle = "#FFF", e.font = "bold 12px system-ui", e.fillText(`#${k.track_id}${k.provenance === "reviewer" ? " · R" : ""}`, E + 5, Math.max(y, _ - 22) + 16), m.push({ trackId: k.track_id, point: [h + (f + M) * l / 2, y + (g + S) * l / 2], bounds: [E, _, h + M * l, y + S * l] });
  }
  return e.globalAlpha = 1, e.fillStyle = "#101A27C0", e.fillRect(10, s - 36, 250, 26), e.fillStyle = "#FFF", e.font = "12px system-ui", e.fillText(`SOURCE FRAME ${u} · ${o.source_frames[u].source_time.toFixed(3)} s`, 18, s - 18), m;
}
function V(e, n, o, a, u) {
  e.fillStyle = "#F8F6EF", e.fillRect(0, 0, 1920, 1080), e.fillStyle = "#17333E", e.font = "600 30px system-ui", e.fillText("MINIVISION / INCIDENT RECONSTRUCTION", 48, 62), e.font = "18px system-ui", e.fillStyle = "#687773", e.fillText(`${a.manifest.case_id} · frame ${u} · source ${a.source_frames[u].source_time.toFixed(3)} s`, 48, 99), e.fillText("CAMERA EVIDENCE", 48, 151), e.fillText("BIRD’S-EYE ROAD PLANE", 812, 151), e.drawImage(o, 48, 175, 720, 790), e.drawImage(n, 812, 175, 1060, 790), e.font = "18px system-ui", e.fillText("Solid: observed   •   Dashed: interpolated   •   R: reviewer   •   Gaps: unavailable   •   Icons are symbolic", 48, 1028);
}
export {
  B as DEFAULT_OPTIONS,
  L as DEFAULT_VIEW,
  W as indexBundle,
  T as mapTransform,
  $ as renderCamera,
  V as renderComparison,
  N as renderMap,
  p as toScreen,
  O as toWorld,
  F as trackColor
};
