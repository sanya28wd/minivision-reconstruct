import type { Bundle, BundleIndex, DrawOptions, Hit, MapResult, MapTransform, Observation, Point, Scene, SceneShape, View } from "./types";

const PALETTE: readonly string[] = ["#F4B942", "#EF6461", "#66B9CB", "#867AC6", "#F19972", "#71B58B", "#638DCE", "#D98AB4"];
export const DEFAULT_OPTIONS: DrawOptions = { paths: true, labels: true, features: false, markers: true, trailSeconds: 3 };
export const DEFAULT_VIEW: View = { zoom: 1, pan: [0, 0] };
export function trackColor(id: number): string { return PALETTE[((id - 1) % PALETTE.length + PALETTE.length) % PALETTE.length]; }
export function indexBundle(bundle: Bundle): BundleIndex {
  const frames = new Map<number, Observation[]>(); const tracks = new Map<number, Observation[]>();
  for (const item of bundle.observations) {
    frames.set(item.source_frame, [...(frames.get(item.source_frame) ?? []), item]);
    tracks.set(item.track_id, [...(tracks.get(item.track_id) ?? []), item]);
  }
  return { byFrame: frames, byTrack: tracks, trackIds: [...tracks.keys()].sort((a: number, b: number): number => a - b) };
}
export function mapTransform(scene: Scene, width: number, height: number, view: View): MapTransform {
  const [x1, y1, x2, y2] = scene.bounds; const angle = scene.rotation_degrees * Math.PI / 180;
  const rotatedWidth = Math.abs(Math.cos(angle)) * (x2 - x1) + Math.abs(Math.sin(angle)) * (y2 - y1);
  const rotatedHeight = Math.abs(Math.sin(angle)) * (x2 - x1) + Math.abs(Math.cos(angle)) * (y2 - y1);
  return { scale: Math.min((width - 100) / rotatedWidth, (height - 100) / rotatedHeight) * view.zoom, angle, center: [(x1 + x2) / 2, (y1 + y2) / 2], screenCenter: [width / 2 + view.pan[0], height / 2 + view.pan[1]] };
}
export function toScreen(point: Point, transform: MapTransform): Point {
  const x = point[0] - transform.center[0]; const y = point[1] - transform.center[1];
  return [transform.screenCenter[0] + transform.scale * (x * Math.cos(transform.angle) - y * Math.sin(transform.angle)), transform.screenCenter[1] - transform.scale * (x * Math.sin(transform.angle) + y * Math.cos(transform.angle))];
}
export function toWorld(point: Point, transform: MapTransform): Point {
  const x = (point[0] - transform.screenCenter[0]) / transform.scale; const y = -(point[1] - transform.screenCenter[1]) / transform.scale;
  return [transform.center[0] + x * Math.cos(transform.angle) + y * Math.sin(transform.angle), transform.center[1] - x * Math.sin(transform.angle) + y * Math.cos(transform.angle)];
}
function path(ctx: CanvasRenderingContext2D, points: readonly Point[], transform: MapTransform): void {
  ctx.beginPath(); points.forEach((point: Point, index: number): void => { const p = toScreen(point, transform); if (index === 0) ctx.moveTo(...p); else ctx.lineTo(...p); });
}
function drawShape(ctx: CanvasRenderingContext2D, shape: SceneShape, transform: MapTransform): void {
  if (shape.kind === "label") { const point = toScreen(shape.points[0], transform); ctx.fillStyle = "#52605C"; ctx.font = "600 13px system-ui"; ctx.fillText(shape.label, ...point); return; }
  path(ctx, shape.points, transform);
  if (shape.kind === "surface") { ctx.closePath(); ctx.fillStyle = "#424B50"; ctx.fill(); ctx.strokeStyle = "#C9C6BD"; ctx.lineWidth = 5; ctx.stroke(); }
  else { ctx.strokeStyle = "#F4EDE0"; ctx.lineWidth = 2; ctx.setLineDash([12, 10]); ctx.stroke(); ctx.setLineDash([]); }
}
function drawRoad(ctx: CanvasRenderingContext2D, scene: Scene, transform: MapTransform, features: boolean): void {
  const surfaces = scene.shapes.filter((shape: SceneShape): boolean => shape.kind === "surface");
  // Shared polygon edges are construction seams, not physical curbs.
  const edges = new Map<string, { readonly points: readonly [Point, Point]; readonly count: number }>();
  for (const shape of surfaces) for (let index = 0; index < shape.points.length; index += 1) {
    const a = shape.points[index]; const b = shape.points[(index + 1) % shape.points.length];
    const key = [a.map((value: number): string => value.toFixed(4)).join(","), b.map((value: number): string => value.toFixed(4)).join(",")].sort().join("/");
    edges.set(key, { points: [a, b], count: (edges.get(key)?.count ?? 0) + 1 });
  }
  for (const edge of edges.values()) {
    if (edge.count !== 1) continue;
    path(ctx, edge.points, transform); ctx.strokeStyle = "#D5CEBA"; ctx.lineWidth = 16; ctx.lineJoin = "round"; ctx.stroke();
    ctx.strokeStyle = "#F5F0DD"; ctx.lineWidth = 3; ctx.stroke();
  }
  ctx.beginPath();
  for (const shape of surfaces) {
    shape.points.forEach((point: Point, index: number): void => { const p = toScreen(point, transform); if (index === 0) ctx.moveTo(...p); else ctx.lineTo(...p); });
    ctx.closePath();
  }
  ctx.fillStyle = "#44444F"; ctx.fill();
  for (const feature of scene.references) {
    if (!features && (feature.kind === "lane" || feature.kind === "monitored_zone")) continue;
    path(ctx, feature.points, transform); ctx.lineWidth = 2;
    if (feature.kind === "stop_line") { ctx.strokeStyle = "#FFF8E8"; ctx.lineWidth = 5; ctx.stroke(); }
    else if (feature.kind === "crossing") {
      ctx.closePath(); ctx.save(); ctx.clip();
      const points = feature.points.map((point: Point): Point => toScreen(point, transform));
      const xs = points.map((point: Point): number => point[0]); const ys = points.map((point: Point): number => point[1]);
      const left = Math.min(...xs); const top = Math.min(...ys); const width = Math.max(...xs) - left; const height = Math.max(...ys) - top;
      ctx.fillStyle = "#FCFAEE";
      if (width > height) for (let x = left; x < left + width; x += 13) ctx.fillRect(x, top, 7, height);
      else for (let y = top; y < top + height; y += 13) ctx.fillRect(left, y, width, 7);
      ctx.restore();
    }
    else if (feature.kind === "monitored_zone") { ctx.closePath(); ctx.fillStyle = "#1D9A9C15"; ctx.fill(); ctx.strokeStyle = "#167E9A"; ctx.setLineDash([4, 5]); ctx.stroke(); ctx.setLineDash([]); }
    else { ctx.closePath(); ctx.strokeStyle = "#ECE7D0A0"; ctx.lineWidth = 1.5; ctx.setLineDash([9, 9]); ctx.stroke(); ctx.setLineDash([]); }
  }
  scene.shapes.filter((shape: SceneShape): boolean => shape.kind !== "surface").forEach((shape: SceneShape): void => drawShape(ctx, shape, transform));
}
function drawTrails(ctx: CanvasRenderingContext2D, index: BundleIndex, frame: number, time: number, transform: MapTransform, selected: number | null, trail: number | null): void {
  for (const [id, records] of index.byTrack) {
    const visible = records.filter((record: Observation): boolean => record.source_frame <= frame && (trail === null || record.source_time >= time - trail));
    for (let offset = 1; offset < visible.length; offset += 1) {
      const before = visible[offset - 1]; const after = visible[offset];
      if (before.position === null || after.position === null || after.source_frame !== before.source_frame + 1) continue;
      ctx.globalAlpha = selected === null || selected === id ? 0.75 : 0.16; ctx.strokeStyle = trackColor(id); ctx.lineWidth = selected === id ? 3.5 : 2;
      ctx.setLineDash(before.state === "interpolated" || after.state === "interpolated" ? [5, 5] : id > PALETTE.length ? [14, 3] : []);
      path(ctx, [before.position, after.position], transform); ctx.stroke();
    }
  }
  ctx.setLineDash([]); ctx.globalAlpha = 1;
}
interface Direction { readonly angle: number | null; readonly moving: boolean; }
function heading(records: readonly Observation[], frame: number, transform: MapTransform): Direction {
  let last: number | null = null;
  let moving: boolean = false;
  for (let index = 1; index < records.length; index += 1) {
    const current = records[index]; if (current.source_frame > frame) break;
    const previous = records[index - 1];
    if (current.position === null || previous.position === null || current.source_frame !== previous.source_frame + 1) { last = null; moving = false; continue; }
    let start = index - 1;
    while (start > Math.max(0, index - 3) && records[start - 1].position !== null && records[start].source_frame === records[start - 1].source_frame + 1) start -= 1;
    const before = records[start].position; if (before === null) continue;
    const p = toScreen(before, transform); const q = toScreen(current.position, transform);
    const distance = Math.hypot(q[0] - p[0], q[1] - p[1]); moving = distance >= 2;
    if (moving || (last === null && distance > 0.000001)) last = Math.atan2(q[1] - p[1], q[0] - p[0]);
  }
  return { angle: last, moving };
}
function vehicle(ctx: CanvasRenderingContext2D, item: Observation, point: Point, direction: Direction, selected: boolean): void {
  const angle = direction.angle;
  ctx.save(); ctx.translate(...point); ctx.rotate(angle === null ? 0 : angle + Math.PI / 2);
  ctx.shadowColor = "#18262A35"; ctx.shadowBlur = 5; ctx.shadowOffsetY = 2;
  const color = trackColor(item.track_id); const estimated = item.state === "interpolated";
  ctx.fillStyle = estimated ? "#FAF7EF" : color; ctx.strokeStyle = color; ctx.lineWidth = selected ? 3 : 1.5;
  ctx.setLineDash(estimated ? [4, 3] : []);
  if (angle === null && item.class_name !== "person") { ctx.beginPath(); ctx.arc(0, 0, 10, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
  else if (item.class_name === "person") {
    ctx.beginPath(); ctx.arc(0, -5, 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); ctx.beginPath(); ctx.roundRect(-5, 1, 10, 10, 4); ctx.fill(); ctx.stroke();
  } else if (item.class_name === "bicycle" || item.class_name === "motorcycle") {
    ctx.beginPath(); ctx.roundRect(-3, -14, 6, 28, 3); ctx.fill(); ctx.stroke(); ctx.beginPath(); ctx.moveTo(-7, -6); ctx.lineTo(7, -6); ctx.stroke();
  } else {
    const length = item.class_name === "bus" ? 48 : item.class_name === "truck" ? 42 : 36;
    ctx.fillStyle = "#202630";
    for (const x of [-12, 8]) for (const y of [-length / 2 + 6, length / 2 - 12]) { ctx.beginPath(); ctx.roundRect(x, y, 4, 7, 1); ctx.fill(); }
    ctx.fillStyle = estimated ? "#FAF7EF" : color;
    ctx.beginPath(); ctx.roundRect(-10, -length / 2, 20, length, 5); ctx.fill(); ctx.stroke(); ctx.setLineDash([]); ctx.shadowBlur = 0;
    ctx.fillStyle = "#FFFFFF35"; ctx.beginPath(); ctx.roundRect(-8, -length / 2 + 2, 16, length - 4, 4); ctx.fill();
    ctx.fillStyle = "#263B50"; ctx.beginPath(); ctx.roundRect(-7, -length / 2 + 7, 14, 7, 2); ctx.fill();
    ctx.fillStyle = color; ctx.beginPath(); ctx.roundRect(-6, -2, 12, 10, 2); ctx.fill();
    ctx.fillStyle = "#33475B"; ctx.fillRect(-7, length / 2 - 9, 14, 4);
    ctx.fillStyle = "#FFF8CD"; ctx.fillRect(-8, -length / 2 + 2, 4, 2); ctx.fillRect(4, -length / 2 + 2, 4, 2);
    ctx.fillStyle = "#B94145"; ctx.fillRect(-8, length / 2 - 3, 4, 2); ctx.fillRect(4, length / 2 - 3, 4, 2);

  }
  ctx.shadowBlur = 0; ctx.setLineDash([]);
  if (angle !== null && direction.moving) { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(0, -23); ctx.lineTo(0, -32); ctx.moveTo(-4, -28); ctx.lineTo(0, -33); ctx.lineTo(4, -28); ctx.stroke(); }
  ctx.restore();
  if (item.provenance === "reviewer" || item.state === "manual") { ctx.fillStyle = "#FFF"; ctx.beginPath(); ctx.arc(point[0] + 11, point[1] + 10, 5, 0, Math.PI * 2); ctx.fill(); ctx.fillStyle = color; ctx.font = "bold 8px system-ui"; ctx.fillText("R", point[0] + 8, point[1] + 13); }
}
function label(ctx: CanvasRenderingContext2D, text: string, point: Point, used: readonly (readonly [number, number, number, number])[], width: number, height: number): readonly [number, number, number, number] {
  ctx.font = "600 12px system-ui"; const w = ctx.measureText(text).width + 14;
  const candidates: readonly Point[] = [
    [point[0] + 17, point[1] - 14], [point[0] - w - 17, point[1] - 14], [point[0] + 17, point[1] + 14],
    ...[42, 70, 98, 126, 154].flatMap((distance: number): readonly Point[] => [
      [point[0] - w / 2, point[1] - distance], [point[0] - w / 2, point[1] + distance],
      [point[0] + distance, point[1] - 14], [point[0] - w - distance, point[1] - 14],
      [point[0] + distance, point[1] - distance], [point[0] - w - distance, point[1] - distance],
      [point[0] + distance, point[1] + distance], [point[0] - w - distance, point[1] + distance],
    ]),
  ];
  const rectangles = candidates.map((p: Point): readonly [number, number, number, number] => [Math.max(5, Math.min(width - w - 5, p[0])), Math.max(5, Math.min(height - 25, p[1])), w, 23]);
  const chosen = rectangles.find((r): boolean => used.every((u): boolean => r[0] + r[2] < u[0] || r[0] > u[0] + u[2] || r[1] + r[3] < u[1] || r[1] > u[1] + u[3])) ?? rectangles[0];
  if (Math.hypot(chosen[0] + w / 2 - point[0], chosen[1] + 11 - point[1]) > 42) {
    ctx.strokeStyle = "#65716C"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(...point); ctx.lineTo(chosen[0] + w / 2, chosen[1] + 11); ctx.stroke();
  }
  ctx.fillStyle = "#FFFDF3ED"; ctx.beginPath(); ctx.roundRect(...chosen, 6); ctx.fill(); ctx.fillStyle = "#233A43"; ctx.fillText(text, chosen[0] + 7, chosen[1] + 16); return chosen;
}
function drawDemoLandscape(ctx: CanvasRenderingContext2D, scene: Scene, transform: MapTransform): void {
  const road = new Path2D();
  for (const shape of scene.shapes.filter((shape: SceneShape): boolean => shape.kind === "surface")) {
    shape.points.forEach((point: Point, index: number): void => { const p = toScreen(point, transform); if (index === 0) road.moveTo(...p); else road.lineTo(...p); }); road.closePath();
  }
  const [x1, y1, x2, y2] = scene.bounds;
  ctx.save(); ctx.lineWidth = 42;
  for (let x = x1 + 3; x < x2; x += 5.5) for (let y = y1 + 3; y < y2; y += 5.5) {
    const p = toScreen([x + Math.sin(x * 2 + y) * 1.4, y + Math.cos(y * 2 + x) * 1.4], transform);
    if (ctx.isPointInPath(road, ...p) || ctx.isPointInStroke(road, ...p)) continue;
    const radius = Math.min(25, (2.5 + Math.sin(x + y) * 0.4) * transform.scale);
    ctx.shadowColor = "#29372245"; ctx.shadowBlur = 3; ctx.shadowOffsetX = 4; ctx.shadowOffsetY = 6;
    ctx.fillStyle = "#678B37"; ctx.beginPath(); ctx.arc(...p, radius, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0; ctx.shadowOffsetX = 0; ctx.shadowOffsetY = 0;
    for (let leaf = 0; leaf < 5; leaf += 1) {
      const angle = leaf * Math.PI * 2 / 5; ctx.fillStyle = leaf % 2 === 0 ? "#BCD655" : "#9EBE45";
      ctx.beginPath(); ctx.arc(p[0] + Math.cos(angle) * radius * 0.4, p[1] + Math.sin(angle) * radius * 0.4, radius * 0.57, 0, Math.PI * 2); ctx.fill();
    }
  }
  ctx.restore();
}
export function renderMap(ctx: CanvasRenderingContext2D, bundle: Bundle, index: BundleIndex, sourceFrame: number, scene: Scene, view: View, options: DrawOptions, selected: number | null): MapResult {
  const width = ctx.canvas.width; const height = ctx.canvas.height; const transform = mapTransform(scene, width, height, view);
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = bundle.manifest.case_id.startsWith("demo-") ? "#ABC661" : "#E8E9DF"; ctx.fillRect(0, 0, width, height);
  if (bundle.manifest.case_id.startsWith("demo-")) drawDemoLandscape(ctx, scene, transform);
  drawRoad(ctx, scene, transform, options.features);
  const sourceTime = bundle.source_frames[sourceFrame].source_time;
  if (options.paths) drawTrails(ctx, index, sourceFrame, sourceTime, transform, selected, options.trailSeconds);
  if (options.markers) for (const marker of bundle.markers.markers) {
    if (marker.position === null || marker.source_frame > sourceFrame) continue;
    const p = toScreen(marker.position, transform); ctx.fillStyle = "#B34E3E"; ctx.beginPath(); ctx.arc(...p, 7, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = "#FFF"; ctx.lineWidth = 2; ctx.stroke();
  }
  const hits: Hit[] = []; const labels: (readonly [number, number, number, number])[] = [];
  const items = [...(index.byFrame.get(sourceFrame) ?? [])].sort((a: Observation, b: Observation): number => Number(b.track_id === selected) - Number(a.track_id === selected));
  for (const item of items) {
    if (item.position === null) continue;
    const p = toScreen(item.position, transform); const edge = p[0] < 20 || p[0] > width - 20 || p[1] < 25 || p[1] > height - 55;
    const displayed: Point = [Math.max(20, Math.min(width - 20, p[0])), Math.max(25, Math.min(height - 55, p[1]))];
    ctx.globalAlpha = selected === null || item.track_id === selected ? 1 : 0.22;
    if (edge) { ctx.fillStyle = trackColor(item.track_id); ctx.beginPath(); ctx.arc(...displayed, 6, 0, Math.PI * 2); ctx.fill(); }
    else vehicle(ctx, item, displayed, heading(index.byTrack.get(item.track_id) ?? [], sourceFrame, transform), item.track_id === selected);
    hits.push({ trackId: item.track_id, point: displayed });
  }
  if (options.labels) for (const hit of hits) {
    ctx.globalAlpha = selected === null || hit.trackId === selected ? 1 : 0.22;
    const edge = hit.point[0] === 20 || hit.point[0] === width - 20 || hit.point[1] === 25 || hit.point[1] === height - 55;
    labels.push(label(ctx, `#${hit.trackId}${edge ? " ↗" : ""}`, hit.point, labels, width, height));
  }
  ctx.globalAlpha = 1; ctx.fillStyle = "#596461"; ctx.font = "12px system-ui";
  ctx.fillStyle = "#FCFDF3ED"; ctx.beginPath(); ctx.roundRect(10, 9, 350, 26, 8); ctx.fill(); ctx.fillStyle = "#46544A";
  ctx.fillText(scene.mode === "metric" ? "METRIC ROAD PLANE · SYMBOLIC VEHICLES" : "RELATIVE COORDINATES · SYMBOLIC VEHICLES", 20, 25);
  if (scene.mode === "metric") {
    const length = 100 / transform.scale; const power = 10 ** Math.floor(Math.log10(length)); const nice = Math.max(power, Math.floor(length / power) * power);
    ctx.strokeStyle = "#596461"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(22, height - 25); ctx.lineTo(22 + nice * transform.scale, height - 25); ctx.stroke(); ctx.fillText(`${Number(nice.toPrecision(2))} m`, 22, height - 34);
  }
  return { transform, hits };
}
export function renderCamera(ctx: CanvasRenderingContext2D, image: CanvasImageSource, bundle: Bundle, index: BundleIndex, sourceFrame: number, selected: number | null): readonly Hit[] {
  const width = ctx.canvas.width; const height = ctx.canvas.height; const sourceWidth = bundle.manifest.source_width; const sourceHeight = bundle.manifest.source_height;
  const scale = Math.min(width / sourceWidth, height / sourceHeight); const left = (width - sourceWidth * scale) / 2; const top = (height - sourceHeight * scale) / 2;
  ctx.fillStyle = "#17232D"; ctx.fillRect(0, 0, width, height); ctx.drawImage(image, left, top, sourceWidth * scale, sourceHeight * scale);
  const hits: Hit[] = [];
  for (const item of index.byFrame.get(sourceFrame) ?? []) {
    if (item.bbox === null) continue;
    const [x1, y1, x2, y2] = item.bbox; const x = left + x1 * scale; const y = top + y1 * scale;
    ctx.globalAlpha = selected === null || selected === item.track_id ? 1 : 0.25; ctx.strokeStyle = trackColor(item.track_id); ctx.lineWidth = selected === item.track_id ? 3 : 2; ctx.setLineDash(item.state === "interpolated" ? [5, 5] : []);
    ctx.strokeRect(x, y, (x2 - x1) * scale, (y2 - y1) * scale); ctx.setLineDash([]); ctx.fillStyle = trackColor(item.track_id); ctx.fillRect(x, Math.max(top, y - 22), 58, 22); ctx.fillStyle = "#FFF"; ctx.font = "bold 12px system-ui"; ctx.fillText(`#${item.track_id}${item.provenance === "reviewer" ? " · R" : ""}`, x + 5, Math.max(top, y - 22) + 16);
    hits.push({ trackId: item.track_id, point: [left + (x1 + x2) * scale / 2, top + (y1 + y2) * scale / 2], bounds: [x, y, left + x2 * scale, top + y2 * scale] });
  }
  ctx.globalAlpha = 1; ctx.fillStyle = "#101A27C0"; ctx.fillRect(10, height - 36, 250, 26); ctx.fillStyle = "#FFF"; ctx.font = "12px system-ui"; ctx.fillText(`SOURCE FRAME ${sourceFrame} · ${bundle.source_frames[sourceFrame].source_time.toFixed(3)} s`, 18, height - 18);
  return hits;
}
export function renderComparison(ctx: CanvasRenderingContext2D, map: HTMLCanvasElement, camera: HTMLCanvasElement, bundle: Bundle, frame: number): void {
  ctx.fillStyle = "#F8F6EF"; ctx.fillRect(0, 0, 1920, 1080); ctx.fillStyle = "#17333E"; ctx.font = "600 30px system-ui"; ctx.fillText("MINIVISION / INCIDENT RECONSTRUCTION", 48, 62);
  ctx.font = "18px system-ui"; ctx.fillStyle = "#687773"; ctx.fillText(`${bundle.manifest.case_id} · frame ${frame} · source ${bundle.source_frames[frame].source_time.toFixed(3)} s`, 48, 99);
  ctx.fillText("CAMERA EVIDENCE", 48, 151); ctx.fillText("BIRD’S-EYE ROAD PLANE", 812, 151);
  ctx.drawImage(camera, 48, 175, 720, 790); ctx.drawImage(map, 812, 175, 1060, 790);
  ctx.font = "18px system-ui"; ctx.fillText("Solid: observed   •   Dashed: interpolated   •   R: reviewer   •   Gaps: unavailable   •   Icons are symbolic", 48, 1028);
}
