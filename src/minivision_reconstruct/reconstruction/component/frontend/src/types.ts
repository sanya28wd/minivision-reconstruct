export type Point = readonly [number, number];
export type Box = readonly [number, number, number, number];
export type ObservationState = "observed" | "lost" | "recovered" | "interpolated" | "manual";
export interface Observation {
  readonly source_frame: number; readonly source_time: number; readonly track_id: number;
  readonly class_name: "person" | "bicycle" | "car" | "motorcycle" | "bus" | "truck";
  readonly position: Point | null; readonly bbox: Box | null; readonly state: ObservationState;
  readonly provenance: "automatic" | "reviewer" | "review_interpolation";
  readonly detection_confidence: number | null;
}
export interface SourceFrame { readonly source_frame: number; readonly source_time: number; readonly timestamp_source: string; }
export interface ReplayFrame { readonly replay_frame: number; readonly replay_time: number; readonly source_frame: number; readonly source_time: number; }
export interface RoadReference { readonly feature_id: string; readonly name: string; readonly kind: "lane" | "stop_line" | "crossing" | "monitored_zone"; readonly points: readonly Point[]; }
export type ShapeKind = "surface" | "marking" | "label";
export interface SceneShape { readonly shape_id: string; readonly kind: ShapeKind; readonly points: readonly Point[]; readonly label: string; }
export interface Scene {
  readonly schema_version: "1.0"; readonly calibration_id: string; readonly mode: "metric" | "non_metric";
  readonly display_unit: "m" | "relative_units"; readonly bounds: Box; readonly rotation_degrees: number;
  readonly references: readonly RoadReference[]; readonly shapes: readonly SceneShape[]; readonly theme: "warm";
}
export interface Marker {
  readonly marker_id: string; readonly source_frame: number; readonly source_time: number;
  readonly position: Point | null; readonly track_ids: readonly number[]; readonly label: string;
  readonly note: string; readonly reviewer: string; readonly created_at: string;
}
export interface MarkerDocument { readonly schema_version: "1.0"; readonly markers: readonly Marker[]; }
export interface Manifest {
  readonly case_id: string; readonly run_id: string; readonly track_revision: string; readonly calibration_revision: string;
  readonly replay_fps: number; readonly replay_frame_count: number; readonly source_frame_count: number;
  readonly source_width: number; readonly source_height: number; readonly duration_seconds: number;
}
export interface Bundle {
  readonly manifest: Manifest; readonly scene: Scene; readonly markers: MarkerDocument;
  readonly observations: readonly Observation[]; readonly source_frames: readonly SourceFrame[]; readonly replay_frames: readonly ReplayFrame[];
}
export interface BundleIndex {
  readonly byFrame: ReadonlyMap<number, readonly Observation[]>;
  readonly byTrack: ReadonlyMap<number, readonly Observation[]>;
  readonly trackIds: readonly number[];
}
export interface View { readonly zoom: number; readonly pan: Point; }
export interface DrawOptions { readonly paths: boolean; readonly labels: boolean; readonly features: boolean; readonly markers: boolean; readonly trailSeconds: number | null; }
export interface MapTransform { readonly scale: number; readonly angle: number; readonly center: Point; readonly screenCenter: Point; }
export interface Hit { readonly trackId: number; readonly point: Point; readonly bounds?: Box; }
export interface MapResult { readonly transform: MapTransform; readonly hits: readonly Hit[]; }
export interface SeekRequest { readonly source_frame: number; readonly track_id: number | null; readonly action_id: string; }
export interface ComponentData { readonly bundle_url: string; readonly video_url: string; readonly frame_url: string; readonly reviewer: string; readonly seek: SeekRequest | null; }
export interface SceneSave { readonly document: Scene; readonly reviewer: string; readonly action_id: string; }
export interface MarkerSave { readonly document: MarkerDocument; readonly reviewer: string; readonly action_id: string; }
export type SaveState = { scene_save: SceneSave | null; marker_save: MarkerSave | null; snapshot_request: { readonly source_frame: number; readonly action_id: string } | null; };
