# Real-footage evaluation protocol

Written on 2026-10-28, before any of these clips was processed. It fixes the clip selection and how the
clips may be used, so the final test result cannot be shaped by the test data.

## Source

ACCIDENT benchmark, real CCTV clips (`data/accident/real/manifests/metadata-real.csv`, 2,027 clips; Kaggle
`picekl/accident`, CC BY-NC-SA 4.0). Each clip is labelled with impact time and frame, a normalised impact box,
collision type, rollover, region, scene layout, weather, day/night, and video quality.

## Selection (`selection.csv`, a copy of `data/accident/real/manifests/real-eval-subset.csv`)

```bash
PYTHONPATH=src .week6-venv/bin/python scripts/prepare_accident_subset.py select-real \
  --manifest data/accident/real/manifests/metadata-real.csv \
  --output data/accident/real/manifests/real-eval-subset.csv \
  --per-type-dev 8 --per-type-test 12 --min-height 720 --seed 9 \
  --exclude=03HtRCJa8Yk_00,1BIZOZtxRpY_00,-jZ5Rlhzzi4_00,4jVVNw9AF2s_00,0isplBZQftg_00
```

- **100 clips: 40 development, 60 test**, 8 and 12 per collision type (head-on, rear-end, sideswipe, single,
  t-bone).
- **Separate cameras.** Development clips come from the benchmark's official geo-aware *train* split (regions
  World and UAE); test clips come from its *test* split (US states). The two sides share no region. Any YouTube
  source found on both sides is dropped, and each YouTube source contributes at most one clip.
- **Already inspected clips are excluded**: the five clips under `cases/accident-*` (and their sources).
- **Usable resolution**: clip height of at least 720 px.
- **Spread**: within each collision type, clips rotate across day/night × weather; selection is deterministic for
  the seed.

| Split | Clips | Day / night | Weather (normal / rain / snow) | Regions |
|---|---|---|---|---|
| `real_dev` | 40 | 23 / 17 | 17 / 8 / 15 | World 38, UAE 2 |
| `real_test` | 60 | 33 / 27 | 21 / 23 / 16 | 14 US regions |

The selection file is read-only. Every clip is registered in `data/registry.csv` with its SHA-256; test clips are
registered with `development_allowed=false`.

## Rules of use

- **Development clips** may be watched, labelled, and used to measure the baseline and to tune or fine-tune.
- **Test clips** may not be watched, labelled for tuning, or used to choose any setting. They are run once, with a
  configuration frozen beforehand, and every clip stays in the result table, including failures.
- Detector fine-tuning data must exclude every test clip.
- A change after the test run needs a new configuration version and a new, unseen test selection.

## Planned measurements

Per stage, on development clips during work and on test clips once at the end:

- Detection: are the colliding vehicles detected around the impact?
- Tracking: do both colliding vehicles keep their track IDs through the impact? (Hand-labelled boxes for 2 s either
  side of impact on 20 development clips now and 20 test clips just before the single test run; see "Impact labels".)
- Incident: accident found within ±0.5 s; impact-time error; impact location against the labelled box
  (normalised image coordinates); collision type; false alarms per clip.

## Impact labels (track continuity)

- **Who labels:** a person, in `scripts/label_impact_app.py`. The app suggests which detector boxes are the two
  colliding road users; the suggestion is only a starting point and is often wrong after the impact. Saved labels
  record the annotator's name and have status `verified`.
- **Which clips:** development labels cover the first 4 clips of each collision type in selection order (20 clips,
  fixed before any clip was viewed). Test labels cover the same number per type, made only after the configuration
  is frozen and immediately before the single test run, and are never used to choose a setting.
- **Keyframes:** every 0.5 s from 2 s before to 2 s after the benchmark's impact frame (fewer when the impact is
  near the start or end of the clip).
- **Proposals:** YOLOv8x (`models/yolov8x.pt`, SHA-256 `3df4ada6…8524c8`) at 1280 px, confidence 0.15. This is a
  labelling aid only; the pipeline under test keeps its own frozen detector.
- **Storage:** labels in `evaluation/real-footage/impact-labels/<split>/` (numbers only); contact sheets, previews,
  and proposals in the Git-ignored `data/accident/real/impact-label-work/`.
- **Measure:** for each labelled road user, whether one track ID covers the last visible keyframe before the impact
  and the first one at or after it (`incident/impact_labels.py`, `track_continuity`, IoU of at least 0.3).

## Calibration

Image-space mode with thresholds in vehicle-size units; see `CALIBRATION.md`.

## Stopping rule

Development work ends when its time box (about three weeks) ends, or earlier if changes stop producing a measurable
improvement on the development clips. The configuration is then frozen and the test clips are run once.
