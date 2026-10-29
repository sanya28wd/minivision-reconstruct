import type { FrontendRendererArgs } from "@streamlit/component-v2-lib";
import { DEFAULT_OPTIONS, DEFAULT_VIEW, indexBundle, renderCamera, renderMap, toScreen, toWorld, trackColor } from "./renderer";
import type { Bundle, BundleIndex, ComponentData, DrawOptions, Hit, MapResult, Marker, MarkerDocument, Point, SaveState, Scene, SceneShape, ShapeKind, View } from "./types";
import "./workspace.css";

interface WorkspaceState {
  readonly frame: number; readonly selected: number | null; readonly view: View; readonly options: DrawOptions;
  readonly scene: Scene; readonly markers: MarkerDocument; readonly undo: readonly Scene[]; readonly redo: readonly Scene[];
  readonly drawing: ShapeKind | null; readonly draft: readonly Point[]; readonly shapeId: string | null; readonly vertex: number | null;
  readonly markerLocation: Point | null; readonly locatingMarker: boolean;
}
const TEMPLATE: string = `
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
function element<T extends Element>(root: HTMLElement | ShadowRoot, selector: string): T {
  const result = root.querySelector<T>(selector); if (result === null) throw new Error(`Workspace element '${selector}' is missing.`); return result;
}
function context(canvas: HTMLCanvasElement): CanvasRenderingContext2D { const result = canvas.getContext("2d"); if (result === null) throw new Error("Canvas 2D is unavailable."); return result; }
async function fetchChecked(url: string): Promise<Response> {
  let lastError: Error = new Error(`Request not started: ${url}`);
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try { const response = await fetch(url); if (!response.ok) throw new Error(`HTTP ${response.status}, URL=${url}, response=${await response.text()}`); return response; }
    catch (error: unknown) { lastError = error instanceof Error ? error : new Error(String(error)); if (attempt < 3) { console.warn("Replay request retry", { url, attempt, error: lastError.message }); await new Promise<void>((resolve): void => { window.setTimeout(resolve, attempt * 200); }); } }
  }
  throw lastError;
}
function pointOn(event: PointerEvent, canvas: HTMLCanvasElement): Point { const rect = canvas.getBoundingClientRect(); return [(event.clientX - rect.left) * canvas.width / rect.width, (event.clientY - rect.top) * canvas.height / rect.height]; }
function closestHit(hits: readonly Hit[], point: Point, tolerance: number): number | null {
  const ordered = hits.map((hit: Hit): readonly [number, number] => [hit.trackId, Math.hypot(hit.point[0] - point[0], hit.point[1] - point[1])]).sort((a, b): number => a[1] - b[1]);
  return ordered.length > 0 && ordered[0][1] <= tolerance ? ordered[0][0] : null;
}
function cameraSelection(hits: readonly Hit[], point: Point): number | null {
  const contained = hits.filter((hit: Hit): boolean => hit.bounds !== undefined && point[0] >= hit.bounds[0] && point[0] <= hit.bounds[2] && point[1] >= hit.bounds[1] && point[1] <= hit.bounds[3]);
  const ordered = [...contained].sort((a: Hit, b: Hit): number => (a.bounds![2] - a.bounds![0]) * (a.bounds![3] - a.bounds![1]) - (b.bounds![2] - b.bounds![0]) * (b.bounds![3] - b.bounds![1]));
  return ordered.length === 0 ? null : ordered[0].trackId;
}
function changedScene(state: WorkspaceState, scene: Scene): WorkspaceState { return { ...state, scene, undo: [...state.undo, state.scene], redo: [] }; }

export default function mount(args: FrontendRendererArgs<SaveState, ComponentData>): () => void {
  const root = element<HTMLElement>(args.parentElement, ".replay-root");
  const restored: WorkspaceState | null = root.dataset.workspace === undefined ? null : JSON.parse(root.dataset.workspace) as WorkspaceState;
  root.dataset.reviewer = args.data.reviewer; root.innerHTML = TEMPLATE;
  const status = element<HTMLElement>(root, ".status"); const video = element<HTMLVideoElement>(root, "video");
  const camera = element<HTMLCanvasElement>(root, ".camera"); const road = element<HTMLCanvasElement>(root, ".road");
  const cameraCtx = context(camera); const roadCtx = context(road); const scrub = element<HTMLInputElement>(root, ".scrub");
  let disposed: boolean = false; let animation: number | null = null; let snapshot: ImageBitmap | null = null; let frameRequest: number = 0;
  let state: WorkspaceState | null = null; let bundle: Bundle | null = null; let index: BundleIndex | null = null;
  let mapResult: MapResult | null = null; let cameraHits: readonly Hit[] = []; let drag: { readonly start: Point; readonly view: View; readonly original: Scene; readonly vertex: number | null; readonly shapeId: string | null } | null = null;
  const removers: (() => void)[] = [];
  function listen<T extends keyof HTMLElementEventMap>(target: HTMLElement, event: T, handler: (event: HTMLElementEventMap[T]) => void): void { target.addEventListener(event, handler); removers.push((): void => target.removeEventListener(event, handler)); }
  function report(error: unknown): void { video.pause(); status.className = "status error"; status.textContent = error instanceof Error ? error.message : String(error); }
  function run(task: Promise<void>): void { void task.catch(report); }
  function info(text: string): void { status.className = "status"; status.textContent = text; }
  function resize(): void { for (const canvas of [camera, road]) { const rect = canvas.getBoundingClientRect(); canvas.width = Math.max(320, Math.round(rect.width)); canvas.height = Math.max(240, Math.round(rect.height)); } draw(); }
  function draw(): void {
    if (state === null || bundle === null || index === null) return;
    const live: Bundle = { ...bundle, scene: state.scene, markers: state.markers };
    if (snapshot !== null) cameraHits = renderCamera(cameraCtx, snapshot, live, index, state.frame, state.selected);
    else if (video.readyState >= 2) cameraHits = renderCamera(cameraCtx, video, live, index, state.frame, state.selected);
    mapResult = renderMap(roadCtx, live, index, state.frame, state.scene, state.view, state.options, state.selected);
    if (state.draft.length > 0) { roadCtx.strokeStyle = "#B66542"; roadCtx.setLineDash([4, 4]); roadCtx.beginPath(); state.draft.forEach((p: Point, i: number): void => { const s = toScreen(p, mapResult!.transform); if (i === 0) roadCtx.moveTo(...s); else roadCtx.lineTo(...s); }); roadCtx.stroke(); roadCtx.setLineDash([]); }
    const selectedShape = state.scene.shapes.find((shape: SceneShape): boolean => shape.shape_id === state!.shapeId);
    if (selectedShape !== undefined) for (let offset = 0; offset < selectedShape.points.length; offset += 1) { const p = toScreen(selectedShape.points[offset], mapResult.transform); roadCtx.fillStyle = offset === state.vertex ? "#B66542" : "#FFF"; roadCtx.strokeStyle = "#B66542"; roadCtx.beginPath(); roadCtx.arc(...p, 5, 0, Math.PI * 2); roadCtx.fill(); roadCtx.stroke(); }
    element<HTMLElement>(root, ".eyebrow").textContent = `MINIVISION / ${bundle.manifest.case_id.replaceAll("-", " ").toUpperCase()}`;
    const frame = bundle.source_frames[state.frame];
    root.dataset.sourceFrame = String(state.frame); root.dataset.selectedTrack = state.selected === null ? "" : String(state.selected);
    element<HTMLElement>(root, ".camera-frame").textContent = `Frame ${state.frame}`;
    element<HTMLElement>(root, ".timestamp").textContent = `${frame.source_time.toFixed(3)} s · ${state.frame}`;
    element<HTMLButtonElement>(root, ".play").textContent = video.paused ? "▶ Play" : "Ⅱ Pause";
    element<HTMLButtonElement>(root, ".previous").disabled = state.frame === 0; element<HTMLButtonElement>(root, ".next").disabled = state.frame >= bundle.source_frames.length - 1;
    element<HTMLSelectElement>(root, ".track-select").value = state.selected === null ? "" : String(state.selected);
    const inspector = element<HTMLElement>(root, ".inspector"); inspector.replaceChildren();
    const title = document.createElement("strong"); title.textContent = state.selected === null ? `${index.trackIds.length} road users` : `Road user #${state.selected}`; title.style.color = state.selected === null ? "#203941" : trackColor(state.selected); inspector.append(title);
    const detail = document.createElement("span"); const item = index.byFrame.get(state.frame)?.find((value): boolean => value.track_id === state!.selected);
    detail.className = "muted"; detail.textContent = state.selected === null ? "Select a road user in either view to inspect its evidence." : item === undefined || item.position === null ? "Unavailable in this source frame · no position inferred" : `${item.class_name} · ${item.state} · ${item.provenance} · detection confidence ${item.detection_confidence === null ? "unavailable" : item.detection_confidence.toFixed(2)}`; inspector.append(detail);
  }
  async function exactFrame(frame: number): Promise<void> {
    if (bundle === null || state === null) return;
    video.pause(); if (animation !== null) { video.cancelVideoFrameCallback(animation); animation = null; } const request = ++frameRequest;
    const response = await fetchChecked(`${args.data.frame_url}${frame}`); const image = await createImageBitmap(await response.blob());
    if (disposed || request !== frameRequest) { image.close(); return; }
    snapshot?.close(); snapshot = image; state = { ...state, frame }; scrub.value = String(frame); draw();
  }
  function hoverText(id: number | null): string {
    if (state === null || index === null || id === null) return "Select a road user · drag empty map space to pan";
    const item = index.byFrame.get(state.frame)?.find((value): boolean => value.track_id === id);
    return item === undefined ? `Road user #${id} unavailable` : `#${id} · ${item.class_name} · frame ${item.source_frame} · ${item.source_time.toFixed(3)} s · ${item.state} · ${item.provenance} · detection confidence ${item.detection_confidence ?? "unavailable"}`;
  }
  function selectTrack(id: number | null): void { if (state === null) return; state = { ...state, selected: id }; draw(); }
  function tick(_now: number, metadata: VideoFrameCallbackMetadata): void {
    if (disposed || bundle === null || state === null) return;
    const replay = bundle.replay_frames[Math.min(bundle.replay_frames.length - 1, Math.floor(metadata.mediaTime * bundle.manifest.replay_fps + 0.001))];
    state = { ...state, frame: replay.source_frame }; scrub.value = String(state.frame); draw();
    if (!video.paused) animation = video.requestVideoFrameCallback(tick);
  }
  async function play(): Promise<void> {
    if (state === null || bundle === null) return;
    if (!video.paused) { video.pause(); run(exactFrame(state.frame)); return; }
    const target = bundle.source_frames[state.frame].source_time - bundle.source_frames[0].source_time;
    if (Math.abs(video.currentTime - target) > 0.001) await new Promise<void>((resolve, reject): void => {
      const ready = (): void => { video.removeEventListener("error", failure); resolve(); };
      const failure = (): void => { video.removeEventListener("seeked", ready); reject(new Error(`Video seek failed at source frame ${state?.frame}.`)); };
      video.addEventListener("seeked", ready, { once: true }); video.addEventListener("error", failure, { once: true }); video.currentTime = Math.min(target, Math.max(0, video.duration - 1 / 30));
    });
    snapshot?.close(); snapshot = null; await video.play(); animation = video.requestVideoFrameCallback(tick); draw();
  }
  function updateScene(scene: Scene): void { if (state === null) return; state = changedScene(state, scene); updateShapeList(); draw(); }
  function updateShapeList(): void {
    if (state === null) return; const list = element<HTMLSelectElement>(root, ".shape-select"); list.replaceChildren(new Option("Select visual shape", ""));
    for (const shape of state.scene.shapes) list.add(new Option(`${shape.kind} · ${shape.label || shape.shape_id}`, shape.shape_id)); list.value = state.shapeId ?? "";
    element<HTMLInputElement>(root, ".rotation").value = String(state.scene.rotation_degrees);
  }
  function updateMarkers(): void {
    if (state === null) return; const list = element<HTMLSelectElement>(root, ".marker-select"); const chips = element<HTMLElement>(root, ".markers"); const rail = element<HTMLElement>(root, ".marker-rail"); rail.replaceChildren(); list.replaceChildren(new Option("Select reviewer marker", "")); chips.replaceChildren();
    for (const marker of state.markers.markers) {
      list.add(new Option(`${marker.source_time.toFixed(2)} s · ${marker.label}`, marker.marker_id));
      const button = document.createElement("button"); button.className = "marker-chip"; button.textContent = `◇ ${marker.source_time.toFixed(2)} s · ${marker.label}`; button.title = `${marker.reviewer}: ${marker.note}`;
      button.onclick = (): void => { run(exactFrame(marker.source_frame)); }; chips.append(button);
      const pin = document.createElement("button"); pin.className = "timeline-pin"; pin.textContent = "◆"; pin.title = `${marker.source_time.toFixed(3)} s · ${marker.label} · reviewer: ${marker.reviewer}`;
      pin.style.left = `${100 * marker.source_frame / Math.max(1, (bundle?.source_frames.length ?? 1) - 1)}%`; pin.onclick = (): void => { run(exactFrame(marker.source_frame)); }; rail.append(pin);
    }
  }
  listen(element(root, ".play"), "click", (): void => run(play()));
  listen(element(root, ".snapshot"), "click", (): void => { if (state !== null) args.setTriggerValue("snapshot_request", { source_frame: state.frame, action_id: crypto.randomUUID() }); });
  listen(element(root, ".previous"), "click", (): void => { if (state !== null) run(exactFrame(Math.max(0, state.frame - 1))); });
  listen(element(root, ".next"), "click", (): void => { if (state !== null && bundle !== null) run(exactFrame(Math.min(bundle.source_frames.length - 1, state.frame + 1))); });
  listen(scrub, "input", (): void => run(exactFrame(Number(scrub.value))));
  listen(element(root, ".rate"), "change", (): void => { video.playbackRate = Number(element<HTMLSelectElement>(root, ".rate").value); });
  listen(element(root, ".track-select"), "change", (): void => { const value = element<HTMLSelectElement>(root, ".track-select").value; selectTrack(value === "" ? null : Number(value)); });
  for (const name of ["paths", "labels", "features", "markers"] as const) listen(element(root, `.${name}-toggle`), "change", (): void => { if (state !== null) { state = { ...state, options: { ...state.options, [name]: element<HTMLInputElement>(root, `.${name}-toggle`).checked } }; draw(); } });
  listen(element(root, ".trail"), "change", (): void => { if (state !== null) { const value = element<HTMLSelectElement>(root, ".trail").value; state = { ...state, options: { ...state.options, trailSeconds: value === "all" ? null : Number(value) } }; draw(); } });
  listen(element(root, ".reset"), "click", (): void => { if (state !== null) { state = { ...state, view: DEFAULT_VIEW }; draw(); } });
  listen(element(root, ".fit"), "click", (): void => {
    if (state === null) return; const points = [...state.scene.references.flatMap((feature): readonly Point[] => feature.points), ...state.scene.shapes.flatMap((shape): readonly Point[] => shape.points)];
    if (points.length > 0) {
      const xs = points.map((point: Point): number => point[0]); const ys = points.map((point: Point): number => point[1]);
      const x1 = Math.min(...xs); const x2 = Math.max(...xs); const y1 = Math.min(...ys); const y2 = Math.max(...ys);
      if (x2 > x1 && y2 > y1) updateScene({ ...state.scene, bounds: [x1 - (x2 - x1) * 0.1, y1 - (y2 - y1) * 0.1, x2 + (x2 - x1) * 0.1, y2 + (y2 - y1) * 0.1] });
    }
    state = { ...state, view: DEFAULT_VIEW }; draw();
  });
  function zoom(factor: number): void { if (state !== null) { state = { ...state, view: { ...state.view, zoom: Math.max(0.4, Math.min(5, state.view.zoom * factor)) } }; draw(); } }
  listen(element(root, ".zoom-in"), "click", (): void => zoom(1.2)); listen(element(root, ".zoom-out"), "click", (): void => zoom(1 / 1.2));
  listen(camera, "click", (event: MouseEvent): void => { const rect = camera.getBoundingClientRect(); const p: Point = [(event.clientX - rect.left) * camera.width / rect.width, (event.clientY - rect.top) * camera.height / rect.height]; selectTrack(cameraSelection(cameraHits, p)); });
  listen(camera, "pointermove", (event: PointerEvent): void => { camera.title = hoverText(cameraSelection(cameraHits, pointOn(event, camera))); });
  listen(road, "pointerdown", (event: PointerEvent): void => {
    if (state === null || mapResult === null) return; const p = pointOn(event, road); const world = toWorld(p, mapResult.transform);
    if (state.locatingMarker) { state = { ...state, markerLocation: world, locatingMarker: false }; info("Marker location selected."); return; }
    if (state.drawing !== null) { state = { ...state, draft: [...state.draft, world] }; draw(); return; }
    const shape = state.scene.shapes.find((value): boolean => value.shape_id === state!.shapeId);
    const vertex = shape?.points.findIndex((point: Point): boolean => { const s = toScreen(point, mapResult!.transform); return Math.hypot(s[0] - p[0], s[1] - p[1]) < 12; }) ?? -1;
    if (vertex >= 0) { state = { ...state, vertex }; drag = { start: p, view: state.view, original: state.scene, vertex, shapeId: state.shapeId }; }
    else { const id = closestHit(mapResult.hits, p, 24); if (id !== null) { selectTrack(id); return; } drag = { start: p, view: state.view, original: state.scene, vertex: null, shapeId: null }; }
    road.setPointerCapture(event.pointerId);
  });
  listen(road, "pointermove", (event: PointerEvent): void => {
    if (state === null || mapResult === null) return; const p = pointOn(event, road); if (drag === null) { road.title = hoverText(closestHit(mapResult.hits, p, 24)); return; }
    if (drag.vertex !== null && drag.shapeId !== null) { const moved = toWorld(p, mapResult.transform); state = { ...state, scene: { ...state.scene, shapes: state.scene.shapes.map((shape): SceneShape => shape.shape_id === drag!.shapeId ? { ...shape, points: shape.points.map((point: Point, offset: number): Point => offset === drag!.vertex ? moved : point) } : shape) } }; }
    else state = { ...state, view: { ...drag.view, pan: [drag.view.pan[0] + p[0] - drag.start[0], drag.view.pan[1] + p[1] - drag.start[1]] } }; draw();
  });
  listen(road, "pointerup", (): void => { if (state !== null && drag !== null && drag.vertex !== null) state = { ...state, undo: [...state.undo, drag.original], redo: [] }; drag = null; });
  for (const button of root.querySelectorAll<HTMLButtonElement>("[data-draw]")) listen(button, "click", (): void => { if (state !== null) { video.pause(); state = { ...state, drawing: button.dataset.draw as ShapeKind, draft: [], shapeId: null }; info("Click the map to place vertices, then finish the shape."); draw(); } });
  listen(element(root, ".finish-shape"), "click", (): void => {
    if (state === null || state.drawing === null) return; const minimum = { surface: 3, marking: 2, label: 1 }[state.drawing]; const text = element<HTMLInputElement>(root, ".shape-label").value.trim();
    if (state.draft.length < minimum || (state.drawing === "label" && (state.draft.length !== 1 || !text))) { report(new Error(`A ${state.drawing} requires ${minimum} points${state.drawing === "label" ? " and text" : ""}.`)); return; }
    const shape: SceneShape = { shape_id: crypto.randomUUID(), kind: state.drawing, points: state.draft, label: text };
    updateScene({ ...state.scene, shapes: [...state.scene.shapes, shape] }); state = { ...state, drawing: null, draft: [], shapeId: shape.shape_id }; updateShapeList(); info("Visual shape added; save a revision to retain it."); draw();
  });
  listen(element(root, ".cancel-shape"), "click", (): void => { if (state !== null) { state = { ...state, drawing: null, draft: [] }; draw(); } });
  listen(element(root, ".shape-select"), "change", (): void => { if (state !== null) { state = { ...state, shapeId: element<HTMLSelectElement>(root, ".shape-select").value || null, vertex: null }; const shape = state.scene.shapes.find((value): boolean => value.shape_id === state!.shapeId); element<HTMLInputElement>(root, ".shape-label").value = shape?.label ?? ""; draw(); } });
  listen(element(root, ".shape-label"), "change", (): void => { if (state === null || state.shapeId === null) return; const text = element<HTMLInputElement>(root, ".shape-label").value.trim(); const shape = state.scene.shapes.find((value): boolean => value.shape_id === state!.shapeId); if (shape?.kind === "label" && !text) { report(new Error("A visual label requires text.")); return; } updateScene({ ...state.scene, shapes: state.scene.shapes.map((value): SceneShape => value.shape_id === state!.shapeId ? { ...value, label: text } : value) }); });
  listen(element(root, ".delete-shape"), "click", (): void => { if (state !== null && state.shapeId !== null) { updateScene({ ...state.scene, shapes: state.scene.shapes.filter((shape): boolean => shape.shape_id !== state!.shapeId) }); state = { ...state, shapeId: null, vertex: null }; draw(); } });
  listen(element(root, ".add-vertex"), "click", (): void => { if (state === null) return; const shape = state.scene.shapes.find((value): boolean => value.shape_id === state!.shapeId); if (shape === undefined || shape.kind === "label") return; const offset = state.vertex ?? 0; const a = shape.points[offset]; const b = shape.points[(offset + 1) % shape.points.length]; const points = [...shape.points.slice(0, offset + 1), [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] as Point, ...shape.points.slice(offset + 1)]; updateScene({ ...state.scene, shapes: state.scene.shapes.map((value): SceneShape => value.shape_id === shape.shape_id ? { ...value, points } : value) }); });
  listen(element(root, ".delete-vertex"), "click", (): void => { if (state === null || state.vertex === null) return; const shape = state.scene.shapes.find((value): boolean => value.shape_id === state!.shapeId); if (shape === undefined || shape.points.length <= (shape.kind === "surface" ? 3 : shape.kind === "marking" ? 2 : 1)) { info("Minimum vertex count reached; delete the whole shape instead."); return; } updateScene({ ...state.scene, shapes: state.scene.shapes.map((value): SceneShape => value.shape_id === shape.shape_id ? { ...value, points: value.points.filter((_point: Point, offset: number): boolean => offset !== state!.vertex) } : value) }); state = { ...state, vertex: null }; draw(); });
  listen(element(root, ".rotation"), "change", (): void => { if (state !== null) { const rotation = Number(element<HTMLInputElement>(root, ".rotation").value); if (!Number.isFinite(rotation)) { report(new Error("Rotation must be finite.")); return; } updateScene({ ...state.scene, rotation_degrees: rotation }); } });
  listen(element(root, ".undo"), "click", (): void => { if (state === null || state.undo.length === 0) return; state = { ...state, scene: state.undo[state.undo.length - 1], undo: state.undo.slice(0, -1), redo: [...state.redo, state.scene] }; updateShapeList(); draw(); });
  listen(element(root, ".redo"), "click", (): void => { if (state === null || state.redo.length === 0) return; state = { ...state, scene: state.redo[state.redo.length - 1], redo: state.redo.slice(0, -1), undo: [...state.undo, state.scene] }; updateShapeList(); draw(); });
  listen(element(root, ".save-scene"), "click", (): void => { if (state === null) return; if (!args.data.reviewer.trim()) { report(new Error("Enter a reviewer name in the application before saving.")); return; } args.setTriggerValue("scene_save", { document: state.scene, reviewer: args.data.reviewer, action_id: crypto.randomUUID() }); info("Saving scene revision…"); });
  listen(element(root, ".locate-marker"), "click", (): void => { if (state !== null) { video.pause(); state = { ...state, locatingMarker: true }; info("Click the map to choose the marker location."); } });
  listen(element(root, ".add-marker"), "click", (): void => {
    if (state === null || bundle === null || index === null) return; if (!video.paused) { report(new Error("Pause before annotating a source frame.")); return; }
    const text = element<HTMLInputElement>(root, ".marker-label").value.trim(); const reviewer = args.data.reviewer.trim(); const linked = element<HTMLInputElement>(root, ".marker-tracks").value.trim();
    const ids: readonly number[] = linked ? linked.split(",").map((value: string): number => Number(value.trim())) : state.selected === null ? [] : [state.selected];
    if (!text || !reviewer || ids.some((id: number): boolean => !Number.isInteger(id) || !index!.trackIds.includes(id))) { report(new Error("Enter a label, reviewer, and valid track IDs.")); return; }
    const marker: Marker = { marker_id: crypto.randomUUID(), source_frame: state.frame, source_time: bundle.source_frames[state.frame].source_time, position: state.markerLocation, track_ids: ids, label: text, note: element<HTMLTextAreaElement>(root, ".marker-note").value, reviewer, created_at: new Date().toISOString() };
    state = { ...state, markers: { ...state.markers, markers: [...state.markers.markers, marker] }, markerLocation: null }; updateMarkers(); draw(); info("Reviewer marker added; save a revision to retain it.");
  });
  listen(element(root, ".jump-marker"), "click", (): void => { const id = element<HTMLSelectElement>(root, ".marker-select").value; const marker = state?.markers.markers.find((value): boolean => value.marker_id === id); if (marker !== undefined) run(exactFrame(marker.source_frame)); });
  listen(element(root, ".delete-marker"), "click", (): void => { if (state !== null) { const id = element<HTMLSelectElement>(root, ".marker-select").value; state = { ...state, markers: { ...state.markers, markers: state.markers.markers.filter((value): boolean => value.marker_id !== id) } }; updateMarkers(); draw(); } });
  listen(element(root, ".save-markers"), "click", (): void => { if (state === null || !args.data.reviewer.trim()) { report(new Error("Enter a reviewer name before saving.")); return; } args.setTriggerValue("marker_save", { document: state.markers, reviewer: args.data.reviewer, action_id: crypto.randomUUID() }); info("Saving marker revision…"); });
  listen(video, "error", (): void => report(new Error(`Replay video failed: URL=${args.data.video_url}, media error=${video.error?.code}`)));
  listen(video, "ended", (): void => { if (state !== null) run(exactFrame(state.frame)); });
  const observer = new ResizeObserver(resize); observer.observe(road); observer.observe(camera);
  run((async (): Promise<void> => {
    if (!("requestVideoFrameCallback" in video)) throw new Error("This browser requires requestVideoFrameCallback; use a current Chrome, Edge, or Safari.");
    bundle = await (await fetchChecked(args.data.bundle_url)).json() as Bundle; if (disposed) return;
    root.dataset.caseId = bundle.manifest.case_id;
    index = indexBundle(bundle); state = restored ?? { frame: 0, selected: null, view: DEFAULT_VIEW, options: DEFAULT_OPTIONS, scene: bundle.scene, markers: bundle.markers, undo: [], redo: [], drawing: null, draft: [], shapeId: null, vertex: null, markerLocation: null, locatingMarker: false };
    const seek = args.data.seek;
    if (seek !== null && seek !== undefined && root.dataset.seekAction !== seek.action_id) {
      root.dataset.seekAction = seek.action_id;
      state = { ...state, frame: Math.min(Math.max(0, seek.source_frame), bundle.source_frames.length - 1), selected: seek.track_id ?? state.selected };
    }
    element<HTMLElement>(root, ".replay-heading h2").textContent = bundle.manifest.case_id.replace(/^demo-/, "").replaceAll("-", " ").replace(/^./, (letter: string): string => letter.toUpperCase());
    element<HTMLElement>(root, ".badge").textContent = bundle.scene.mode === "metric" ? "Validated metric calibration" : "Validated relative calibration";
    scrub.max = String(bundle.source_frames.length - 1); const trackList = element<HTMLSelectElement>(root, ".track-select"); for (const id of index.trackIds) trackList.add(new Option(`Road user #${id}`, String(id)));
    video.src = args.data.video_url; updateShapeList(); updateMarkers(); resize(); await exactFrame(state.frame); info(`Tracks: ${bundle.manifest.track_revision} · calibration: ${bundle.manifest.calibration_revision} · reviewer annotations`);
  })());
  return (): void => { if (state !== null) root.dataset.workspace = JSON.stringify(state); disposed = true; frameRequest += 1; if (animation !== null) video.cancelVideoFrameCallback(animation); video.pause(); video.removeAttribute("src"); video.load(); snapshot?.close(); observer.disconnect(); for (const remove of removers) remove(); };
}
