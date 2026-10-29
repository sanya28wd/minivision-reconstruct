# ACCIDENT synthetic §11.2 results: week8_heldout (incident-week8-002-ablation-proximity-only, run incident-ablation-002)

Primary candidate = most confident collision or single-vehicle proposal (label-blind). Detected = its peak within ±0.5 s of the labelled impact. Location, type and pair are scored on detected clips only. A near miss counts at impact when its peak is within the same tolerance. The tracking columns are diagnostics from the labelled boxes (IoU >= 0.3); the single-vehicle type has one labelled actor.

| Group | Clips | Proposed | Detected | Median abs. time error (s) | Median location error (norm.) | Inside label box | Type correct | Pair correct | False alarms / clip | Near miss at impact, missed clips (right pair) | Both actors tracked at impact | Both keep their track ID through the post-window | Chain failures |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 25 | 22 | 4 | 3.43 | 0.104 | 2/4 | 0/4 | 2/4 | 7.24 | 0 (0) | 3 | 0 | 0 |
| head_on | 5 | 4 | 1 | 3.18 | 0.083 | 0/1 | 0/1 | 0/1 | 0.80 | 0 (0) | 0 | 0 | 0 |
| rear_end | 5 | 5 | 2 | 3.55 | 0.112 | 1/2 | 0/2 | 1/2 | 16.40 | 0 (0) | 2 | 0 | 0 |
| sideswipe | 5 | 5 | 0 | 5.10 | – | – | – | – | 11.40 | 0 (0) | 1 | 0 | 0 |
| single | 5 | 5 | 0 | 2.70 | – | – | – | – | 5.80 | 0 (0) | 0 | 0 | 0 |
| t_bone | 5 | 3 | 1 | 1.35 | 0.126 | 1/1 | 0/1 | 1/1 | 1.80 | 0 (0) | 0 | 0 | 0 |
