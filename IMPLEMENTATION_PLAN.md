# MiniVision-Reconstruct

## Execution README for the Video-Based Accident Reconstruction Extension

**Working title:** MiniVision-Reconstruct: Explainable Accident Reconstruction from Video-Based Evidence for Road Operations

**Primary use case:** Convert fixed-camera traffic video into an explainable incident reconstruction for Dubai government reviewers, showing the sequence of actions and which participants may have contributed to the incident.

**Primary research question:**

> How accurately can a lightweight, explainable computer-vision pipeline reconstruct the incident sequence and identify evidence-supported contributing actions for human review?

The government already has speed metrics. Do not calculate speed from video. An authorized external speed record may be linked to a road user and event only after its source, units, timestamp, and identity alignment are verified. Keep that record distinct from observations made by this system.

This README is the implementation source of truth. An implementation agent should complete the phases in order, satisfy each acceptance gate, and avoid silently changing the scope.

---

## 1. Project boundaries

### 1.1 Semester MVP

The first complete version must support:

- One uploaded fixed-camera CCTV video.
- Two or more visible road users.
- Vehicle and pedestrian detection.
- Persistent multi-object tracking.
- Manual correction of fragmented or incorrect tracks.
- Manual road-plane calibration.
- A synchronized 2D bird's-eye incident reconstruction.
- Trajectories, lane position, movement order, turns, stops, heading changes, and separation with uncertainty.
- Candidate collision and near-miss detection.
- Evidence-linked contribution assessment for each involved road user, including multiple contributors or an undetermined result.
- Reviewer confirmation, rejection, or correction of proposed contributing actions.
- A clickable incident timeline.
- OCR for timestamps, speed limits, route numbers, and road signs.
- A temporal safety report with evidence, confidence, and limitations.
- Annotated video, trajectory CSV, event JSON, and PDF report export.
- Evidence hashing, audit history, and privacy-redacted export.

### 1.2 Stretch scope

Implement only after the MVP acceptance tests pass:

- Traffic-signal state reconstruction.
- Blocked-lane and traffic-queue analysis.
- Secondary-collision response recommendations.
- Near-miss and blackspot heatmaps across multiple cases.
- Similar-incident search.
- Counterfactual road-safety simulation.
- Multi-camera synchronization and trajectory fusion.
- Moving-dashcam ego-motion compensation.
- Photorealistic 3D visualization.

### 1.3 Non-goals

The system must not:

- Decide legal fault, criminal liability, or issue penalties.
- Calculate speed or acceleration from video, or infer speeding from a speed-limit sign alone.
- Assign responsibility to a person from a vehicle track or identify a driver, owner, or pedestrian.
- Claim a collision from bounding-box overlap alone.
- Modify or overwrite original evidence.
- Hide uncertainty, missing frames, occlusion, or manual corrections.
- Be presented as an autonomous enforcement system.
- Control real RTA signs, signals, or field equipment.

---

## 2. Relationship to the previous MiniVision project

Previous project location:

~~~text
/Users/sanyawadhawan/Projects/minivision/design proj/
~~~

Use the old project as a source and baseline, not as the working directory for the extension. Create a new repository or top-level directory so the submitted previous project remains reproducible.

### 2.1 Keep and adapt

| Previous component | Decision | New role |
|---|---|---|
| OCR extraction and cleanup | Keep | Read video timestamps, route numbers, destinations, speed limits, and warnings. |
| UAE route-sign understanding | Keep | Add local road context to evidence summaries. |
| Application workflow pattern | Adapt | Host case review and calibration in Streamlit, with a Canvas reconstruction workspace. |
| Dataset preparation scripts | Adapt | Prepare video clips, frames, tracks, events, and calibration records. |
| Evaluation/reporting scripts | Adapt | Evaluate detection, tracking, trajectory, event timing, contribution assessments, OCR, and runtime. |
| Vision API boundary | Keep concept | Replace the large multi-purpose API with small typed modules. |
| Edge benchmarking | Keep | Measure FPS, latency, memory, model size, and export time. |
| Caption rewriting | Restrict | Generate text only from structured evidence. |
| Hazard vocabulary | Keep carefully | Use as report categories, not proof of a crash. |
| Existing report assets | Historical baseline | Demonstrate continuity and image-only limitations. |

### 2.2 Replace or retire

| Previous component | Decision | Reason |
|---|---|---|
| Frame-level SAFE/RISKY model as primary logic | Retire | It cannot reconstruct temporal events. |
| Existing safety checkpoint as evidence | Do not use | Documented behavior predicts every test image as RISKY: 50% accuracy, macro-F1 0.333, SAFE recall 0. |
| Phrase-classification caption head as project core | Retire | Reconstruction needs tracking, geometry, and temporal reasoning. |
| Generic VQA and emotion outputs | Remove from MVP | They do not support the main RTA use case. |
| Image-only processing | Keep only as baseline | Compare it with the new temporal method. |
| Silent checkpoint fallbacks | Remove | Missing or invalid models must raise clear errors. |

### 2.3 Checkpoint migration warning

The observed files under the old project's models/checkpoints directory are zero-byte placeholders. Do not report them as functioning models. Recover verified weights or train/export new checkpoints.

Every model artifact must have:

- Non-zero file size.
- SHA-256 hash.
- Model name and version.
- Training-data reference.
- Evaluation report.
- Successful smoke inference.

---

## 3. Product modes

### 3.1 Incident Review

- Inspect original and annotated video.
- Review tracks and identities.
- Calibrate the road plane.
- Replay the bird's-eye reconstruction.
- Inspect the probable collision or near-miss sequence.
- Review each participant's observed actions and proposed contribution, including counterevidence and unresolved facts.
- Correct automatic results.
- Export a reconstruction package.

### 3.2 Traffic Operations

- Identify stopped vehicles and blocked lanes.
- Estimate queue direction and growth when visible.
- Highlight secondary-collision risk.
- Draft an operator-facing warning and response recommendation.
- Require operator confirmation for every recommendation.

### 3.3 Road-Safety Engineering

- Aggregate collision and near-miss positions.
- Display conflict heatmaps.
- Compare movement, time, visibility, and road-user type.
- Identify repeated conflict patterns.
- Test simplified counterfactual interventions after the core pipeline is validated.

---

## 4. Technical stack

| Layer | Technology | Responsibility |
|---|---|---|
| Language | Python 3.11 | Backend and analysis pipeline. |
| Model framework | PyTorch | Detection inference and optional fine-tuning. |
| Detection | Pretrained YOLO-family detector | Road-user boxes, classes, and scores. Review licensing before commercial deployment. |
| Tracking | ByteTrack first; BoT-SORT if justified | Persistent IDs and occlusion recovery. |
| Video | OpenCV and FFmpeg/ffprobe | Decode, metadata, frames, overlays, codecs, and export. |
| Geometry | OpenCV, NumPy, Shapely | Homography, mapping, zones, intersections, and distances. |
| Motion | NumPy and SciPy | Smoothing, movement order, direction changes, stopping, separation, and event signals. |
| OCR | Existing EasyOCR pipeline first | Timestamp and road-context extraction. |
| UI | Streamlit with a TypeScript Canvas component | Case review, calibration, synchronized reconstruction, scene editing, and exports. |
| Tables | Pandas | Tracks, events, and metrics. |
| Storage | SQLite plus case folders | Cases, review state, and artifact paths. |
| API | FastAPI after MVP | Typed programmatic interface. |
| Reports | HTML and WeasyPrint, or ReportLab | Reproducible PDF output. |
| Optimization | ONNX Runtime after baseline | Faster target-hardware inference. |
| Annotation | CVAT | Boxes, tracks, events, signals, and road geometry. |
| Contact-point refinement | SAM 2 (optional, config-selected) | Mask-based road-contact points and BEV footprint polygons (§8.4). |
| Packaging | pyproject.toml | Reproducible local environment. |

### Tooling decisions

- **Bird's-eye view stays geometric.** Learned BEV models (BEVFormer, Lift-Splat-Shoot, BEVFusion) are built for multi-camera moving vehicles and do not transfer to one fixed roadside camera. The reconstruction uses a validated homography. Models may only refine the inputs to the homography (contact points, calibration proposals), and a reviewer must confirm any calibration they propose.
- **Trajectory analysis stays rule-based and explainable.** Smoothing, event detection, and surrogate safety measures (PET, closest approach) are deterministic methods. Trajectory *prediction* models (for example Trajectron++) are out of scope because the task is reconstruction, not forecasting.
- **Roboflow Agent / hosted Workflows are not adopted.** They cover only detection, tracking, and zone counting, which are already implemented, and uploading CCTV evidence to a third-party cloud conflicts with §8.1 and §8.12. Roboflow Universe datasets or labelling may be used to fine-tune the detector, if detection quality on real footage becomes the bottleneck. Exported weights must then meet the model-artifact rules in §2.3, and inference must run locally.

### Engineering rules

- Use strict type annotations.
- Define dataclasses or Pydantic models for complex records.
- Prefer pure, single-purpose functions.
- Use classes only for model, storage, file, and external-system connectors.
- Do not mutate input records or global state.
- Do not use default parameter values; configuration must be explicit.
- Raise specific, actionable errors.
- Never fabricate a value when a model, calibration, or measurement is missing.
- Store code, model, and pipeline versions with every result.
- Preserve unrelated previous-project files.

---

## 5. Target repository structure

~~~text
minivision-reconstruct/
├── pyproject.toml
├── README.md
├── configs/
├── src/minivision_reconstruct/
│   ├── domain/
│   │   ├── models.py
│   │   ├── errors.py
│   │   └── enums.py
│   ├── evidence/
│   │   ├── integrity.py
│   │   ├── metadata.py
│   │   └── case_store.py
│   ├── video/
│   │   ├── reader.py
│   │   ├── frames.py
│   │   └── renderer.py
│   ├── perception/
│   │   ├── detector.py
│   │   ├── tracker.py
│   │   ├── track_cleanup.py
│   │   └── privacy.py
│   ├── calibration/
│   │   ├── homography.py
│   │   ├── validation.py
│   │   └── road_geometry.py
│   ├── reconstruction/
│   │   ├── trajectory.py
│   │   ├── smoothing.py
│   │   ├── motion_events.py
│   │   ├── conflicts.py
│   │   └── event_phases.py
│   ├── contribution/
│   │   ├── evidence.py
│   │   ├── assessment.py
│   │   └── review.py
│   ├── context/
│   │   ├── ocr.py
│   │   ├── route_signs.py
│   │   └── signal_state.py
│   ├── safety/
│   │   ├── temporal_assessment.py
│   │   ├── secondary_risk.py
│   │   └── recommendations.py
│   ├── reporting/
│   │   ├── timeline.py
│   │   ├── narrative.py
│   │   ├── package.py
│   │   └── pdf.py
│   ├── analytics/
│   │   ├── blackspots.py
│   │   ├── similarity.py
│   │   └── counterfactuals.py
│   ├── api/app.py
│   └── ui/gradio_app.py
├── scripts/
│   ├── process_case.py
│   ├── evaluate.py
│   └── export_report.py
├── tests/
│   ├── integration/
│   ├── smoke/
│   └── fixtures/
├── evaluation/
├── cases/
└── models/
~~~

Do not commit private video evidence. Commit only tiny licensed or synthetic fixtures when permitted.

---

## 6. Core data contracts

Define these typed records before implementing processing logic.

### 6.1 Evidence record

~~~text
case_id
original_filename
sha256
file_size_bytes
duration_seconds
frames_per_second
frame_width
frame_height
codec
creation_time_if_available
ingested_at
~~~

### 6.2 Detection record

~~~text
case_id
frame_number
timestamp_seconds
class_name
confidence
bbox_x1
bbox_y1
bbox_x2
bbox_y2
model_version
~~~

### 6.3 Track observation

~~~text
case_id
track_id
frame_number
timestamp_seconds
class_name
bbox
road_contact_pixel
detection_confidence
association_confidence
is_observed
is_interpolated
~~~

Use the bottom-center of a vehicle box as the initial road-contact approximation. Evaluate segmentation or learned contact points only if measured results show this is a bottleneck.

### 6.4 Calibration record

~~~text
case_id
image_points
world_points_metres
homography_matrix
reprojection_error
calibration_method
created_by
created_at
is_metric
validation_status
~~~

### 6.5 Movement observation

~~~text
track_id
timestamp_seconds
world_x
world_y
heading_degrees
position_uncertainty_metres
movement_state
lane_or_zone_id
source_quality
~~~

`movement_state` describes visible behavior such as moving, turning, stopping, stopped, or unknown. It is not a speed estimate. Store any authorized government speed metric as a separate external-evidence record with source system, record ID, units, measurement time, road-user match, and alignment quality.

### 6.6 Event record

~~~text
event_id
case_id
event_type
start_time
peak_time
end_time
involved_track_ids
world_location
evidence_signals
confidence
status
reviewer_note
created_by
~~~

### 6.7 Contribution assessment

~~~text
assessment_id
case_id
event_id
involved_track_ids
contributing_track_ids
outcome
observed_actions
supporting_evidence_refs
counterevidence_refs
applicable_road_context_refs
unresolved_facts
confidence
status
reviewer_note
created_by
~~~

Allowed outcomes: `ONE_CONTRIBUTOR`, `MULTIPLE_CONTRIBUTORS`, `NO_SUPPORTED_CONTRIBUTOR`, and `UNDETERMINED`. A track ID identifies a visible road user, not a driver or legal party. Every proposed contributor needs a specific action, temporal relationship to the incident, and traceable evidence. A missing signal state, obscured action, or ambiguous track match must remain unresolved; it must not be treated as evidence against a road user.

Allowed event statuses:

- PROPOSED_BY_SYSTEM
- CONFIRMED_BY_REVIEWER
- REJECTED_BY_REVIEWER
- MODIFIED_BY_REVIEWER

### 6.8 Provenance rule

Every material claim must be labelled:

- **Observed:** directly visible or read from metadata.
- **Calculated:** produced from explicit measurements and formulas.
- **Inferred:** produced by multi-signal logic or a learned model.
- **User-provided:** entered by the reviewer.
- **Unknown:** not supported by available evidence.

---

## 7. End-to-end processing flow

1. Ingest the original video and calculate its hash.
2. Extract metadata and validate that the video is decodable.
3. Decode frames with exact timestamps.
4. Detect supported road-user classes.
5. Associate detections into persistent tracks.
6. Let the reviewer merge, split, or correct tracks.
7. Collect road-plane reference points and measurements.
8. Validate calibration and calculate reprojection error.
9. Map road-contact pixels to bird's-eye coordinates.
10. Smooth trajectories without overwriting observations.
11. Derive movement order, direction, stopping, lane/zone use, and separation without calculating speed.
12. Generate candidate conflicts using multiple temporal signals.
13. Divide the sequence into pre-event, event, and post-event phases.
14. Extract OCR and road context from selected frames.
15. Link optional agency speed records only when provenance and road-user/time alignment are verified.
16. Propose contributing actions with supporting evidence, counterevidence, and unresolved facts.
17. Create the temporal safety assessment.
18. Let the reviewer confirm, reject, or modify events, geometry, and contribution assessments.
19. Recompute dependent results after accepted corrections.
20. Render the annotated video and bird's-eye replay.
21. Generate the timeline, report, and evidence package.
22. Run validation before final export.

---

## 8. Detailed feature specification

### 8.1 Evidence integrity

Implement SHA-256 hashing, read-only original handling, separate derived artifacts, processing configuration, model hashes, audit events, and a final artifact manifest.

**Acceptance:**

- Reprocessing the same original yields the same hash.
- No step writes to the original.
- The report lists every applied visual transformation.

### 8.2 Detection and tracking

Implement road-user detection, persistent IDs, lost-track states, short-gap interpolation, and reviewer controls for merge, split, class correction, and deletion.

**Acceptance:**

- A fixture produces a video with stable IDs.
- Observed and interpolated positions look different.
- Every manual edit is audit logged.

### 8.3 Camera calibration

Implement image/world point selection, known-distance entry, homography calculation, reprojection error, and rejection of invalid calibration. Support clearly labelled pixel-space mode.

**Acceptance:**

- Held-out validation points map within the declared tolerance.
- Calibration does not produce or display video-derived speed.
- The calibration UI shows the reprojection error for each control point, so a single mis-clicked point can be identified and corrected. The current least-squares fit (`cv2.findHomography(..., method=0)`) spreads one bad point's error across the whole calibration, and the held-out check shows that the fit failed but not which point caused it.

### 8.4 Bird's-eye digital twin

Implement a simplified 2D map with road users, observed and uncertain trajectories, direction arrows, lane regions, stop lines, collision markers, and synchronized playback.

**Acceptance:**

- All views remain time-synchronized.
- Selecting a vehicle filters its graphs and evidence.

**Road-contact point.** The Week 6–7 pipeline projects the bottom-centre of each box (`project_track_footpoint` in `calibration.py`). This is the largest known source of position error. On angled views and at intersections it sits on the camera-facing side of the vehicle, which biases separation, PET, and collision location. Add a config-selected contact-point method, and keep `bottom_centre` as the default so that frozen configurations such as `movement-week7-002` stay reproducible:

- `mask_bottom_centre`: the bottom-centre of a SAM 2 instance mask instead of the box. This is tighter, and less affected by partial occlusion and box padding.
- `mask_footprint`: the lowest mask contour points projected through the homography to a BEV footprint polygon, with its centroid as the position. This allows body-to-body separation and footprint contact for §8.6, instead of contact-point distances only.

Record the method and mask model version in every movement manifest. Report the change in trajectory position error from this method as an ablation (§11.11).

### 8.5 Movement and uncertainty analysis

Implement separate raw and smoothed tracks, movement order, heading, pairwise separation, stop/start and turn candidates, lane/stop-line crossings, and uncertainty. A visible abrupt stop may be described without claiming measured deceleration.

**Acceptance:**

- Ordered movements and stop/turn events match labelled staged video within declared timing tolerance.
- A stopped segment is labelled stopped without a speed value.
- Raw observations remain available.

### 8.6 Collision and near-miss candidates

Combine:

- Converging trajectories.
- Small physical separation.
- Geometric overlap or mask contact.
- Abrupt change in visible movement.
- Sudden heading change or rotation.
- Abrupt stopping.
- Post-event obstruction.
- Optional optical-flow disturbance.

Calculate closest-approach time, post-encroachment time, and minimum separation where valid. Do not calculate relative speed, required deceleration, or a speed-based time-to-collision estimate.

**Acceptance:**

- Bounding-box overlap alone cannot confirm a collision.
- Every event lists its supporting signals.
- The reviewer can confirm, reject, or modify the event.

### 8.7 OCR-grounded context

Run OCR on selected frames and regions, use temporal voting, reuse UAE sign cleanup, and extract timestamp, route, destination, direction, warning, and speed-limit fields.

**Acceptance:**

- Every accepted OCR value links to a source frame and confidence.
- Conflicting readings remain visible until resolved.

### 8.8 Traffic-signal reconstruction

Stretch feature:

- Reviewer-defined signal regions and stop lines.
- Red, amber, green, off, obscured, and unknown states.
- Temporally smoothed states with raw results retained.
- Stop-line crossing times.
- Optional comparison with supplied controller logs.

Never assert a violation when the signal or stop line is uncertain.

### 8.9 Evidence timeline

Include object entry/exit, maneuver start, visible stopping, heading change, lane/stop-line crossing, closest approach, candidate impact, rotation, lane obstruction, and reviewer corrections. Every event links to frames and provenance.

### 8.10 Contributing-action assessment

For each involved road user, compare the ordered actions with visible lane geometry, crossings, stop lines, signal states when verified, and the conflict sequence. Propose the action or actions that may have contributed to the incident. Show evidence for and against each proposal, causal timing, missing evidence, and confidence. Use `UNDETERMINED` when the video cannot support a participant-level conclusion. A road-rule or violation claim requires an approved, case-applicable agency rule source; uncertain signal state or OCR alone cannot establish one. A reviewer must be able to accept, reject, or edit every proposed contribution. Reports must clearly distinguish this analytical assessment from an official determination.

### 8.11 Operator correction

Allow the reviewer to:

- Join fragmented tracks.
- Split incorrectly joined tracks.
- Correct object class.
- Move collision point.
- Change collision time.
- Correct lane, stop-line, crossing, and signal regions.
- Add road measurements.
- Mark signal state.
- Confirm or reject an event.
- Confirm, reject, or edit a proposed contributing action and its evidence links.

Keep automatic and reviewed versions. Never erase the original proposal.

### 8.12 Privacy-by-design

Implement face blur, number-plate blur, optional region masks, authorized export, and anonymized export. State that redaction modifies derived displays, not original evidence.

### 8.13 Secondary-collision prevention

After core reconstruction, identify stopped vehicles, blocked lanes, queue direction/growth, approaching traffic, and visibility. Produce advisory actions such as upstream warning, lane closure review, patrol dispatch, or diversion review.

Recommendations must expose evidence and require operator confirmation.

### 8.14 Blackspot and near-miss analytics

Across reviewed cases, aggregate collision points, closest approaches, stopping/turning events, movement pairs, road-user types, time, visibility, verified signal phase, and reviewed contributing actions. Use reviewed events by default and link every heatmap point to its source case.

### 8.15 Similar-incident search

Start with structured filters for event type, users, movement, visibility, layout, contributing action, and time. Add vector similarity only after structured retrieval works and is evaluated. Explain why each result is similar.

### 8.16 Counterfactual simulator

Late stretch feature:

- Modify movement start, path choice, or verified signal timing.
- Replay simplified trajectories.
- Compare conflict indicators.
- Display assumptions prominently.

Label output as simplified scenario analysis, not proof of prevention.

### 8.17 Multi-camera reconstruction

Future feature:

- Synchronize cameras.
- Calibrate each to a shared road plane.
- Match tracks using time, position, direction, and appearance.
- Preserve conflicts and uncertainty between views.

Do not begin before single-camera reconstruction passes evaluation.

---

## 9. Strengthening and reusing the safety report

### 9.1 What to reuse

Reuse the previous report's presentation ideas:

- Risk level.
- Hazard factors.
- Evidence breakdown.
- Recommendation.
- Confidence.
- Human-readable explanation.

Keep the old report as an **image-only baseline**, not as ground truth.

### 9.2 What not to reuse

Do not reuse:

- The old checkpoint as a trustworthy classifier.
- Caption keywords as sufficient evidence of a crash.
- A single score without temporal or geometric justification.
- “Accident prediction” language for retrospective reconstruction.

### 9.3 New component name

Rename it:

> **Temporal Safety and Operational Impact Assessment**

### 9.4 Required report sections

1. **Evidence quality:** resolution, frame rate, missing frames, lighting, occlusion, and camera motion.
2. **Calibration quality:** metric/non-metric status, points, reprojection error, and scale.
3. **Road users:** IDs, categories, intervals, and track quality.
4. **Pre-event:** directions, maneuvers, lane use, separation, and verified signal state.
5. **Event:** candidate time, location, users, supporting signals, and confidence.
6. **Post-event:** final positions, rotation, stopped vehicles, blocked lanes, and queue.
7. **Road context:** OCR-derived time, route, warning, and speed-limit evidence.
8. **Operational impact:** obstruction, secondary risk, and advisory actions.
9. **Contribution assessment:** each involved track's proposed contributing action, supporting and contrary evidence, confidence, and unresolved facts; include multiple or undetermined outcomes.
10. **External metrics:** agency speed records only when supplied and verified, with provenance and alignment quality.
11. **Uncertainty:** position, timing, event, contribution, OCR, and signal confidence.
12. **Reviewer decisions:** accepted, rejected, or corrected proposals.
13. **Limitations:** facts that cannot be supported by the video.

### 9.5 Safety signals

Compute assessment from structured evidence:

- Minimum separation.
- Closest-approach timing.
- Post-encroachment time.
- Visible stopping or turning.
- Sudden heading change.
- Vehicle stopped in a live lane.
- Number and duration of blocked lanes.
- Approaching traffic and visibility.
- Pedestrian exposure.
- Visibility/weather.
- Verified warning and speed-limit context.

Every score must expose its contributing signals. Build a transparent calculation/rule baseline first. Add a learned temporal classifier only if a labelled dataset and proper validation exist.

### 9.6 Required comparison

| System | Input | Purpose |
|---|---|---|
| Previous safety baseline | One frame plus caption/OCR | Demonstrate image-only limitations. |
| Detection-only video | Independent frames | Show that detections alone lack stable movement. |
| Tracking baseline | Video tracks | Show motion without metric road geometry. |
| Full reconstruction | Tracking, calibration, temporal signals, OCR | Demonstrate the complete contribution. |

---

## 10. Week-by-week execution plan

The schedule assumes 16 weeks. If time is shorter, complete Weeks 0–10 and treat later work as stretch scope.

### Week 0 — Freeze scope and preserve the old project

**Tasks**

- Create a new repository.
- Record/archive the old project state.
- Copy only selected reusable modules after dependency review.
- Confirm fixed-camera CCTV as MVP.
- Write non-goals and ethics boundaries.
- Create package structure and typed domain records.
- Add one tiny synthetic or licensed fixture video.

**Deliverables**

- Repository, pyproject.toml, architecture, scope statement.

**Acceptance gate**

- Old submission unchanged.
- New package imports.
- Fixture is configuration-driven.

### Week 1 — Evidence ingestion

**Tasks**

- Create cases.
- Preserve original files.
- Compute SHA-256.
- Extract metadata with ffprobe.
- Decode frames with exact times.
- Raise clear errors for corrupt input.
- Validate first with one tiny permitted ACCIDENT clip or a deterministic synthetic fixture; do not download the full benchmark for ingestion work.

**Deliverables**

- Manifest, metadata JSON, frame reader, smoke workflow.

**Acceptance gate**

- Correct frame count/timestamps.
- Original hash remains unchanged.

### Week 2 — Detection baseline

**Tasks**

- Integrate one pretrained detector.
- Restrict supported classes.
- Export typed detection records.
- Render boxes/class/confidence.
- Measure latency and memory.
- Inspect a small ACCIDENT subset containing day, night, highway/intersection, poor-quality, and occluded cases.
- Use a small labelled UA-DETRAC subset for formal detection measurements when its annotations are available.

**Deliverables**

- Detection data, annotated video, baseline metrics.

**Acceptance gate**

- End-to-end inference without silent fallback.
- Every rendered detection maps to a record.

### Week 3 — Persistent tracking

**Tasks**

- Integrate ByteTrack.
- Export stable IDs.
- Render IDs and trajectory tails.
- Represent lost/recovered/interpolated states.
- Measure ID switches and fragmentation on a labelled UA-DETRAC subset; use ACCIDENT clips for qualitative accident-scene stress testing.

**Deliverables**

- Track data, tracking video, evaluation.

**Acceptance gate**

- Stable enough IDs for MVP clips.
- Identity failures are measured.

### Week 4 — Human track review

**Tasks**

- Add frame navigation and track selection.
- Add merge, split, correction, deletion, and short-gap repair.
- Store immutable audit events.
- Rebuild reviewed tracks from observations plus edits.

**Deliverables**

- Review UI, audit log, reviewed export.

**Acceptance gate**

- A deliberately fragmented track can be repaired.
- Automatic and reviewed versions remain available.

### Week 5 — Calibration

**Tasks**

- Build image/world point entry.
- Calculate homography and error.
- Add lanes, stop lines, crossings, and monitored zones.
- Support non-metric mode.

**Deliverables**

- Calibration UI/JSON and road overlay.

**Acceptance gate**

- Validation points meet tolerance.
- No metric claims in non-metric mode.

### Week 6 — Polished bird's-eye reconstruction

**Tasks**

- Prepare raw projected contact observations from explicitly selected track and validated calibration revisions, preserving states, provenance, and source timestamps.
- Build a mapped 30 FPS camera proxy and synchronized Canvas replay with exact original-frame inspection.
- Render symbolic road users, direction arrows, evidence-styled paths, linked selection, and an editable schematic road scene with locked calibration references.
- Save numbered scene and reviewer-marker revisions and export through the shared renderer. Detection confidence remains detection confidence; speed is not calculated.

**Deliverables**

- Interactive camera/map workspace, reproducible scene and marker revisions, projected records and integrity manifests, 1080p bird's-eye/comparison MP4s, and PNG snapshots.
- Setup, preparation, review, export, and controlled-demo commands are documented in [the application README](README.md).

**Acceptance gate**

- Both views reference the same source frame; source-frame stepping is exact and playback alignment stays within one proxy frame.
- Observed, interpolated, reviewer-created, and unavailable observations are distinguishable without connecting lost gaps.
- Scene editing cannot move calibrated geometry or projected observations; markers and scenes survive reload.
- Shared-renderer exports decode at 1920×1080 and 30 FPS.
- A 40-second controlled fixture with 20 road users has been verified at approximately 30 FPS. Completion on an unseen permitted real case remains pending. As of 2026-10-25, the five `cases/accident-*` videos are downloaded locally and have ByteTrack runs, but none has a calibration, reconstruction, or movement run yet.

### Week 7 — Movement sequence and uncertainty

**Tasks**

- Preserve raw and create smoothed trajectories.
- Derive ordered maneuvers, stop/start events, heading, lane use, and separation.
- Propagate calibration/tracking quality into uncertainty.
- Create interactive plots.
- Validate movement and event timing against annotated staged motion.

**Deliverables**

- Movement records, validation, plots.

**Acceptance gate**

- Maneuver and stop timing error reported against ground truth.
- No false precision.

**Status (2026-10-11):**

- **Implemented:** `src/minivision_reconstruct/movement/`.
  - Raw positions are preserved alongside segment-local smoothed trajectories.
  - Heading, visible stop/start, left/right turns, lane use and lane changes, stop-line and zone crossings, and contact-point separation are derived without any speed calculation.
  - Uncertainty is propagated from box size, detection confidence, interpolation or reviewer origin, the homography Jacobian, and held-out calibration RMSE.
  - The event order marks overlapping uncertainty windows as unresolved.
  - Displays are rounded to uncertainty.
  - Interactive Plotly panels in the reconstruction workspace seek the replay to an event's source frame.
- **Validation:** visibly synthetic staged-motion cases with exact scripted ground truth. Configuration `movement-week7-002` was tuned on seeds 1–2 and frozen before the held-out seeds 101–103 were generated.
  - On held-out cases, maneuver and stop peak timing error is 0.17 s mean and 0.43 s max.
  - All 93 annotated events fall within their declared tolerance, with one extra closest-approach proposal.
  - 100% of true event times and positions fall inside the reported uncertainty.
  - Lane sequences match exactly.
  - At twice the tuned pixel noise, 58 of 63 events fall within tolerance (reported in `evaluation/week7/noise-stress-movement-003/`).
- **Pending:** validation on hand-annotated staged *physical* toy-vehicle videos (§11.5). The ground-truth schema already supports them.

### Week 8 — Collision, near-miss, and contribution analysis

**Tasks**

- Combine path, separation, motion-change, and stopping signals.
- Calculate closest-approach and post-encroachment timing where valid.
- Rank candidate event time/location.
- Show evidence and confidence.
- Add confirm/reject/modify controls.
- Develop and tune on ACCIDENT synthetic clips only.
- Evaluate impact time, impact location, and collision type on a held-out synthetic subset before touching the real benchmark labels.
- Link each involved track to its visible pre-event actions, road geometry, candidate impact, and post-event position.
- Propose participant-level contributing actions with supporting evidence, counterevidence, and unresolved facts.
- Allow one, multiple, no supported, or undetermined contributor outcomes.
- Add reviewer confirm/reject/edit controls and retain the original proposal in the audit log.
- Require an approved agency rule source before labelling a road-rule breach.

**Deliverables**

- Event engine, timing evaluation, evidence-linked contribution assessment, and review history.

**Acceptance gate**

- Overlap alone cannot confirm collision.
- Event timing error is measured.
- Every proposed contributor links to a timed action and source frames.
- Occluded or ambiguous scenarios can remain undetermined.
- A review correction updates the exported assessment without erasing the proposal.

**Status (2026-10-27):**

- **Implemented:** `src/minivision_reconstruct/incident/`, with configuration `configs/incident.json` (`incident-week8-002`).
  - PET at crossings of smoothed paths, per segment so lost gaps are never bridged, with a conflict area sized from position uncertainty and an uncertainty window. Time and position only.
  - Collision, near-miss, and single-vehicle candidates. Proximity is required (contact points within `contact_distance` plus combined uncertainty, or PET ≤ 0); a collision also needs two kinds of consequence (visible stop, heading jump outside detected turns, lane obstruction). Box overlap is supporting only. Each candidate lists its signals, source frames, cited events, phases, and a time window covering the uncertain impact time.
  - Contribution assessment with rules R1–R5, counterevidence, and unresolved facts; decisive data gaps force `UNDETERMINED`; `rule_source` is always null.
  - Reviewer confirm/reject/modify in the workspace's Incident analysis panel, saved as hash-chained revisions; the proposals are never rewritten, and the exported reviewed view embeds each original proposal.
- **Validation:** ten scripted incident scenarios with exact truth. `incident-week8-002` was tuned on seeds 1–2 and frozen before held-out seeds 101–103 were generated (`evaluation/week8/`).
  - Held-out detection precision 0.95, recall 0.88, F1 0.91; 0.03 false alarms per clip.
  - Impact time error median 0.06 s, 90% within ±0.5 s; impact location 100% within 3 m; collision type 93%.
  - PET within 0.04 s of truth at the estimator's own conflict radius (9 of 9).
  - Contribution agreement 100% for detected incidents (20 of 21 overall); every contributor links to a timed action and source frames.
  - Proximity-only ablation: F1 0.88 and four times the false alarms, so consequence fusion is kept.
- **Real footage** (`evaluation/week8/real-cases.md`): `accident-day-intersection-004` was calibrated (metric, assumption-based, held-out RMSE 0.95 m) and run end to end. The rear-end was found 0.27 s before the labelled impact at the correct pair, but as a near miss, because both tracks are lost at impact; its assessment is `UNDETERMINED`. `accident-day-highway-002` (relative calibration) missed its single-vehicle accident.
- **Decision on footprints (§8.4):** deferred. On real footage, tracking continuity through impact and calibration limit results, not the contact-point approximation.
- **ACCIDENT synthetic, §11.2** (`evaluation/week8/accident-synthetic.md`): 40 CARLA clips downloaded, registered, and calibrated automatically from each clip's camera pose (90° field of view, flat road). With the frozen config, 0 of 25 held-out accidents are detected, with 0.04 false alarms per clip. The cause is perception: both colliding vehicles are tracked at impact in only 3 of 25 clips, and in none do both keep their track ID through the post-impact window. In 4 clips the right pair is proposed as a near miss at the impact time.
- **Pending:**
  - Track continuity through impact (fragment linking) and the occluded-impact consequence window, as `incident-week8-003`, evaluated on fresh fixture seeds and a fresh ACCIDENT selection.
  - Detection of small, top-down vehicles (detector input size or model), evaluated on the Week 2–3 sets first.
  - Real-footage evaluation: a larger, scene-disjoint selection of real ACCIDENT clips scored on impact time, image-space impact location and collision type (dev/test split, configuration frozen before the test run); hand-labelled boxes of the colliding road users for about 2 s either side of impact on about 30 clips, to measure track continuity through impact; staged videos with measured ground truth for movement timing; and a before/after measurement of human track review (identity errors, fragmentation, reviewer time). Real clips carry no camera parameters, so this needs either manual calibration on a subset or an image-space evaluation mode.
  - Single-vehicle detection (1 of 3 held-out) needs a better heading source.
  - One held-out occluded impact became a near miss; the fix needs a new configuration version and fresh held-out seeds.

Keep every Week 8 threshold in an explicit, versioned configuration. Tune it on synthetic data only, and freeze it before the held-out evaluation, following the Week 7 protocol.

### Week 9 — OCR and UAE road context

**Tasks**

- Port OCR cleanup and route parsing.
- Run OCR on selected windows/regions.
- Add temporal voting.
- Link readings to frames.
- Separate raw OCR from interpretation.

**Deliverables**

- OCR evidence table, context panel, evaluation.

**Acceptance gate**

- Every reported value has source/confidence.
- Conflicts are visible.

### Week 10 — Timeline, safety report, and export

**Tasks**

- Generate clickable timeline.
- Implement Temporal Safety and Operational Impact Assessment.
- Include the reviewed contributing-action assessment and unresolved facts.
- Generate annotated video, twin video, CSV, JSON, images, manifest, and PDF.
- Validate IDs/timestamps across outputs.

**Deliverables**

- Complete MVP dashboard and case package.

**Acceptance gate**

- One unseen fixed-camera case runs from upload to reviewed export.
- Report claims link to evidence.
- No automated legal fault conclusion; proposed contributions remain clearly labelled as review findings.

### Week 11 — Privacy and operational impact

**Tasks**

- Add face/plate redaction.
- Produce authorized and anonymized exports.
- Detect stopped vehicles/lane obstruction.
- Estimate queues only when supported.
- Add secondary-risk recommendations.

**Deliverables**

- Redacted package and operational panel.

**Acceptance gate**

- Original remains untouched.
- Recommendations require operator confirmation.

### Week 12 — Signal reconstruction

**Tasks**

- Add signal regions and stop lines.
- Estimate temporal states.
- Calculate crossing times.
- Support unknown/obscured states.
- Optionally compare controller logs.

**Deliverables**

- Signal timeline and crossing evidence.

**Acceptance gate**

- Uncertain evidence never becomes an asserted violation.

### Week 13 — Blackspot analytics

**Tasks**

- Aggregate reviewed events.
- Generate collision, conflict, stopping, and obstruction heatmaps.
- Add filters.
- Build structured similar-incident search.

**Deliverables**

- Safety-engineering dashboard and explained similar cases.

**Acceptance gate**

- Reviewed data used by default.
- Every point links to source case/event.

### Week 14 — Choose one advanced spike

**Option A: Counterfactual**

- Modify movement timing or path choice.
- Replay simplified trajectories.
- Compare conflict metrics.
- Document assumptions.

**Option B: Multi-camera**

- Synchronize two views.
- Calibrate them to one plane.
- Match a few tracks.
- Report fusion uncertainty.

**Acceptance gate**

- Works on one controlled example.
- Clearly labelled experimental.

### Week 15 — Evaluation and ablations

**Tasks**

- Freeze video-level evaluation splits.
- Evaluate detection, tracking, calibration, trajectory, movement events, contribution assessments, OCR, and runtime.
- Compare image-only, detection-only, tracking-only, and full reconstruction.
- Ablate calibration, smoothing, OCR, and event signals.
- Document night, occlusion, blur, and poor-calibration failures.
- Freeze all thresholds and configuration before final evaluation on ACCIDENT real CCTV clips.
- Process the real benchmark sequentially and save lightweight predictions/metrics rather than annotated video for every clip.

**Deliverables**

- Final metrics, ablations, error analysis, failure gallery.

**Acceptance gate**

- Reproducible from configuration/model hashes.
- Claims use the complete test set.

### Week 16 — Demonstration and handoff

**Tasks**

- Freeze versions.
- Test clean installation.
- Prepare primary and backup demo videos.
- Record the demo.
- Finalize report, ethics, limitations, and RTA narrative.
- Confirm private evidence is absent from Git.

**Deliverables**

- Reproducible repository, case package, presentation, report, and recording.

**Acceptance gate**

- A new user can reproduce the primary demo.
- Presentation separates observation, calculation, inference, and reviewer input.

---

## 11. Evaluation design

### 11.1 Dataset roles and priority

No single public dataset supplies real CCTV accidents, complete vehicle tracks, camera calibration, collision timing, verified contributing actions, and UAE OCR context. Use each dataset only for the task its annotations support. Collision labels alone do not establish which participant contributed to an incident.

| Dataset | Role | Priority | Do not claim |
|---|---|---:|---|
| ACCIDENT | Primary benchmark for impact time, spatial impact location, collision region/type, and difficult real CCTV conditions. | Primary | Participant responsibility from collision labels alone. |
| UA-DETRAC | Fixed-camera vehicle detection and multi-object tracking evaluation. | Primary support | Accident-reconstruction accuracy; it is mainly normal traffic. |
| Controlled staged videos | Calibration, trajectory, movement order, separation, event time/location, and known scenario actions. | Essential | Real-world crash diversity or legal fault. |
| Previous UAE/DoTaS data | OCR, route codes, bilingual signs, destinations, warnings, and UAE context. | Reuse | Video tracking or temporal accident performance. |
| CADP | Older CCTV accident comparison and literature baseline. | Optional | Complete calibration or metric ground truth. |
| DeepAccident Mini | Simulated motion, multi-view, BEV, and calibration experiments. | Optional | Real CCTV generalization. |
| inD / rounD (ika, RWTH Aachen) | Real-road trajectory accuracy: drone video with ground-truth top-down trajectories, used to measure BEV position error in metres. Requires the free non-commercial academic licence. | Recommended | Fixed-CCTV viewing angles; accidents (it contains normal traffic only). |
| CARLA-generated clips | Collision and PET ground truth with exact impact time and location, for Week 8 tuning and held-out evaluation. | Optional | Real-world appearance or crash diversity. |
| DADA-2000/dashcam data | Moving-camera accident understanding. | Future | Fixed-camera performance without ego-motion handling. |

### 11.2 ACCIDENT benchmark

Use Kaggle dataset picekl/accident and its official benchmark documentation as the main accident-event source.

Official sources:

- Dataset: https://www.kaggle.com/datasets/picekl/accident
- Project: https://accidentbench.github.io/
- Code: https://github.com/accidentbench/ACCIDENT
- Paper: https://openaccess.thecvf.com/content/CVPR2026W/AUTOPILOT/html/Picek_ACCIDENT_A_Benchmark_Dataset_for_Vehicle_Accident_Detection_from_Traffic_CVPRW_2026_paper.html

The benchmark reports:

- 2,027 real traffic-surveillance clips.
- 2,211 synthetic CCTV-style CARLA clips.
- Accident impact time/frame.
- Normalized impact point and collision region where supplied.
- High-level collision type.
- Scene metadata such as weather, layout, lighting, camera position, duration, frame count, and resolution where supplied.
- Head-on, rear-end, T-bone, sideswipe, and single-vehicle categories.

Use it as follows:

1. **Development:** synthetic clips and richer simulator annotations.
2. **Validation:** a held-out, video-disjoint synthetic subset.
3. **Final benchmark:** real CCTV clips only after thresholds, prompts, and configuration are frozen.
4. **Qualitative demo:** a small number of licensed real clips selected independently of model success.

Do not repeatedly tune against released real-test labels and then call the result a fair zero-shot or untouched-test benchmark. If real labels are used for development, declare that use and create a separate held-out protocol.

Store at least:

~~~text
video_id
dataset_split
ground_truth_impact_time
predicted_impact_time
absolute_temporal_error
ground_truth_impact_x
ground_truth_impact_y
predicted_impact_x
predicted_impact_y
normalized_spatial_error
ground_truth_collision_type
predicted_collision_type
scene_tags
pipeline_version
~~~

The real footage varies in frame rate, resolution, compression, lighting, accident-region size, and occlusion. Preserve these conditions for evaluation; do not report only cleaned showcase clips.

### 11.3 ACCIDENT licensing and privacy

The benchmark supplementary material states:

- Released annotations and metadata use CC BY 4.0.
- Released code and CARLA assets use Apache-2.0.
- Included clips were selected from sources permitting redistribution and derivative use.
- Use is restricted to research.
- Re-identification is prohibited.
- Licence plates may appear incidentally.

Required handling:

- Record dataset version, source, licence, and retrieval date.
- Cite the benchmark paper.
- Do not attempt face, plate, driver, owner, or pedestrian identification.
- Redact faces and plates in presentation exports.
- Do not commit videos to Git or redistribute them outside their terms.
- Recheck current Kaggle rules before publication or non-academic use.

### 11.4 UA-DETRAC

Use UA-DETRAC for detection and persistent tracking, which the real ACCIDENT annotations do not fully cover.

It provides fixed-camera traffic sequences with vehicle boxes, trajectories, classes, occlusion/truncation information, and varied lighting/weather.

Use it to measure:

- Detection precision, recall, and mAP where supported.
- Tracking IDF1/HOTA where supported.
- Identity switches and fragmentation.
- Performance by occlusion, scale, and weather.

Start with 5–10 representative sequences and expand only after the pipeline is stable. Do not treat normal-traffic performance as accident-event performance.

Reference: https://sites.google.com/view/daweidu/projects/ua-detrac

### 11.5 Controlled metric dataset

Create a safe miniature-road or toy-vehicle dataset because public CCTV usually lacks measurements and action labels needed to validate geometry and contributing-action reasoning.

Target:

- 10 normal interactions.
- 10 near misses.
- 10 low-speed staged toy collisions.

Include straight movement, crossing paths, rear-end movement, left-turn conflict, lane change, sudden stopping, partial occlusion, and cases where either, both, or neither visible road user has a supported contributing action.

Record:

- Road-plane dimensions.
- Camera position and video settings.
- Calibration image/world points.
- Vehicle start/end points.
- Known movement order and action timing.
- Ground-truth event frame/time and point.
- Scenario-defined contributing actions, including counterevidence and indeterminate examples.
- Scenario and lighting category.

Use it for calibration error, trajectory error, maneuver timing, separation error, event time/location error, and contribution-assessment validation. Have independent reviewers label a separate permitted incident subset before using it to claim real-world contribution performance. Never stage dangerous events with real vehicles or on public roads.

### 11.6 UAE OCR data

Use only relevant previous MiniVision/DoTaS records for:

- Arabic and English road text.
- E11, E311, and other route codes.
- Speed-limit signs.
- Destinations, directions, and lane instructions.
- OCR cleanup and temporal voting.

Keep OCR evaluation separate. A still image cannot count as a tracking or accident-reconstruction sample.

### 11.7 Mac-friendly resource policy

Do not download or expand every dataset at the beginning.

**Initial subset**

- 20–30 ACCIDENT synthetic clips.
- 20–30 ACCIDENT real clips for pipeline checks and qualitative stress testing, without tuning against their labels.
- 5–10 UA-DETRAC sequences.
- 10–30 short controlled videos.
- A small UAE OCR subset.

**Intermediate subset**

- 100–200 ACCIDENT synthetic clips.
- A declared real CCTV analysis subset used according to the frozen-evaluation policy.
- 10–20 UA-DETRAC sequences.
- All controlled videos.

**Final evaluation**

- Increase the ACCIDENT real subset only after freezing the pipeline.
- Run sequentially, one video at a time.
- Save lightweight predictions, metrics, and failure records.
- Render annotated videos only for selected demos and failure analysis.

Resource limits:

- Keep the active development data near a declared 10–20 GB budget unless more storage is approved.
- Keep datasets outside Git.
- Use an external SSD if needed.
- Do not download the approximately 313 GB full DeepAccident dataset.
- Do not download the approximately 9 GB DeepAccident Mini subset unless an approved experiment needs it.
- Avoid duplicate archives after verified extraction unless reproducibility/licensing requires them.

### 11.8 Streaming and derived-data rules

The pipeline must:

1. Open one video.
2. Decode one frame or a very small batch.
3. Resize immediately for inference while retaining source dimensions.
4. Use batch size 1 by default on memory-limited Macs.
5. Write JSONL/CSV predictions incrementally.
6. Release frame/model outputs before reading the next frame.
7. Close the video before opening another.
8. Avoid retaining all frames or detections in RAM.

Do not permanently extract every video into images. If temporary frames are required, use a case-scoped temporary directory, verify the downstream output, and safely remove only that validated temporary directory.

Use a two-pass workflow for long clips:

- **Pass 1:** sample frames to find active road users and likely event windows.
- **Pass 2:** process every frame around candidate windows for accurate tracking and localization.

For early Mac development:

- Use a small pretrained detector.
- Use batch size 1.
- Resize detection input to approximately 640 pixels unless evaluation justifies another size.
- Sample frames only during early scanning, not final impact-frame scoring without accounting for lost temporal resolution.
- Use Apple MPS only when explicitly configured and validated; record CPU/MPS in metrics.
- Do not train a detector from scratch locally.
- Use a managed GPU for optional fine-tuning, then bring back a hashed checkpoint.

### 11.9 Dataset directory and registry

Store datasets outside Git:

~~~text
data/
├── registry.csv
├── accident/
│   ├── synthetic/
│   └── real/
├── ua_detrac/
├── controlled/
└── uae_ocr/
~~~

The registry must include:

~~~text
dataset_name
dataset_version
source_url
licence
retrieved_at
local_video_id
source_video_id
split
development_allowed
labels_used_for_tuning
local_relative_path
sha256
duration_seconds
frames_per_second
width
height
annotation_path
purpose
privacy_notes
~~~

Do not place neighboring clips from the same original source in different splits. Deduplicate by source ID and, where practical, file hash or perceptual similarity.

### 11.10 Required metrics

| Component | Metrics |
|---|---|
| Detection | Precision, recall, mAP, per-class errors. |
| Tracking | IDF1, HOTA if supported, ID switches, fragments, recall. |
| Calibration | Reprojection and held-out point error. |
| Trajectory | Position RMSE or mean position error. |
| Movement sequence | Maneuver/stop timing error and lane or stop-line crossing accuracy. |
| Event localization | Collision-time and location error. |
| Event detection | Precision, recall, F1, false alarms per clip. |
| Contribution assessment | Agreement with independently reviewed contributor/action labels, per-action precision/recall, undetermined rate, and evidence-link accuracy. |
| OCR | Character/word and structured-field accuracy. |
| Timeline | Event precision, recall, factual-review score. |
| Runtime | FPS, latency, memory, model size, export time. |
| Human review | Review time and corrections per case. |

### 11.11 Required ablations

- Independent frames versus tracking.
- Pixel-space versus calibrated movement.
- Raw versus smoothed tracks.
- Box bottom-centre versus mask-based contact point versus footprint polygon.
- One collision signal versus multi-signal fusion.
- OCR disabled versus enabled.
- Automatic versus reviewer-corrected result.
- Old image safety report versus new temporal report.

### 11.12 Test discipline

- Split by source video/event, never by extracted frame.
- Do not put neighboring frames into different splits.
- Freeze thresholds before final test evaluation.
- Report the full test set, not only curated examples.
- Maintain a failure log with root causes.

---

## 12. Dashboard pages

### Case

- Metadata and evidence hash.
- Original and annotated video.
- Processing state and errors.
- Object and track tables.

### Calibration

- Reference frame.
- Image-point selector.
- World-coordinate/distance entry.
- Road-geometry editor.
- Reprojection error.

### Reconstruction

- Synchronized original, annotated, and bird's-eye views.
- Timeline slider and vehicle selector.
- Movement timeline, heading, and separation plots.
- Per-participant contributing-action review with supporting and contrary evidence.
- Candidate events.

### Review

- Track merge/split.
- Collision time/point correction.
- Stop-line, lane, crossing, and signal editing.
- Confirm/reject controls.
- Edit proposed contributing actions, supporting evidence, and unresolved facts.
- Audit history.

### Safety

- Pre-event, event, and post-event summaries.
- Evidence/calibration quality.
- Hazard and operational signals.
- Reviewed contribution assessment for each involved road user.
- Blocked lanes and secondary risk.
- Confidence and limitations.

### Export

- Authorized package.
- Privacy-redacted package.
- PDF.
- CSV trajectories.
- JSON events.
- Integrity manifest.

---

## 13. Error and uncertainty policy

Raise or visibly report:

- Missing or zero-byte model.
- Corrupt/unsupported video.
- Invalid frame rate or timestamp.
- Detector/tracker initialization failure.
- Degenerate calibration.
- Excessive reprojection error.
- Insufficient metric scale.
- Track gaps too long to interpolate.
- Missing evidence for a claim.
- Export failure.

Show uncertainty for:

- Track association.
- Road-plane position.
- Movement order and stop/turn events.
- Collision time/location.
- Proposed contributing actions and their evidence links.
- OCR.
- Signal state.
- Operational recommendation.

Use ranges where defensible and avoid unnecessary decimal precision.

---

## 14. Definition of done

The project is complete when:

- A fixed-camera incident video is ingested without modifying the original.
- Road users have persistent IDs.
- Tracking errors can be corrected and audited.
- Road-plane calibration is validated.
- A synchronized 2D bird's-eye reconstruction is produced.
- Direction, maneuver order, stopping, separation, and uncertainty are reported.
- Collision/near-miss candidates have multiple supporting signals.
- Every involved road user has a reviewable contribution assessment or an explicit undetermined outcome.
- OCR context links to source frames.
- Events can be confirmed, rejected, and modified.
- A temporal safety and operational-impact report is generated.
- Authorized and redacted packages are available.
- Held-out video-level evaluation is complete.
- The old safety system appears only as a baseline.
- No output assigns legal fault or identifies a person from a road-user track.
- Installation and the primary demo are reproducible.

---

## 15. Final demonstration sequence

1. Upload a 20–40 second fixed-camera clip.
2. Show its evidence hash and metadata.
3. Run detection and persistent tracking.
4. Repair one fragmented track.
5. Load or create road-plane calibration.
6. Play synchronized annotated and bird's-eye views.
7. Select two road users and show their movement order, lane use, stopping, and separation.
8. Jump to the candidate event from the timeline.
9. Show evidence signals and uncertainty.
10. Display OCR-derived time, route, or road-control context.
11. Review the proposed contributing action for each involved road user, including contrary evidence or an undetermined result.
12. Confirm or correct the event and contribution assessment.
13. Show obstruction and secondary-risk recommendations if supported.
14. Export reconstruction and safety report.
15. End with blackspot analytics or the chosen stretch feature.

**Final pitch:**

> MiniVision-Reconstruct converts traffic-camera footage into an explainable incident reconstruction. It shows what each road user did, when the conflict developed, and which actions may have contributed, with evidence and uncertainty for a government reviewer to assess. Legal conclusions remain with authorized investigators.

---

## 16. Agent execution checklist

Before each week:

- Read this README and active repository instructions.
- Inspect existing logic before adding new code.
- Confirm the previous acceptance gate passes.
- Work only on the current week's scope.
- Record assumptions that affect measurements.

Before declaring a week complete:

- Run repository-defined validations.
- Run a real or synthetic end-to-end workflow.
- Visually inspect videos, plots, and reports.
- Save metrics and configuration.
- Confirm failures raise actionable errors.
- Do not declare completion when the acceptance gate fails.

When blocked:

- Identify the exact stage and input.
- Preserve the original error and parameters.
- Test the smallest safe alternative.
- Never hide failure with a generic caption, random output, or fallback score.
- Ask for a scope decision only when alternatives change the project objective.

---

## 17. Priority if time becomes limited

Protect in this order:

1. Evidence integrity.
2. Detection and tracking.
3. Track correction.
4. Calibration.
5. Bird's-eye reconstruction.
6. Movement sequence and uncertainty.
7. Collision/near-miss timeline.
8. Contributing-action review.
9. Temporal safety report.
10. OCR context.
11. Privacy-redacted export.

Cut in this order:

1. Photorealistic 3D.
2. Moving dashcam support.
3. Multi-camera fusion.
4. Vector-based similar-case search.
5. Counterfactual simulation.
6. Signal reconstruction.
7. Blackspot dashboard.

A smaller system with measured accuracy, transparent uncertainty, and a convincing end-to-end demo is stronger than many unvalidated features.
