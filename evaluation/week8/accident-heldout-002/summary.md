# ACCIDENT synthetic §11.2 results: week8_heldout (incident-week8-002, run incident-002)

Primary candidate = most confident collision or single-vehicle proposal (label-blind). Detected = its peak within ±0.5 s of the labelled impact. Location, type and pair are scored on detected clips only. A near miss counts at impact when its peak is within the same tolerance. The tracking columns are diagnostics from the labelled boxes (IoU >= 0.3); the single-vehicle type has one labelled actor.

| Group | Clips | Proposed | Detected | Median abs. time error (s) | Median location error (norm.) | Inside label box | Type correct | Pair correct | False alarms / clip | Near miss at impact, missed clips (right pair) | Both actors tracked at impact | Both keep their track ID through the post-window | Chain failures |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 25 | 1 | 0 | 9.25 | – | – | – | – | 0.04 | 5 (4) | 3 | 0 | 0 |
| head_on | 5 | 0 | 0 | – | – | – | – | – | 0.00 | 0 (0) | 0 | 0 | 0 |
| rear_end | 5 | 1 | 0 | 9.25 | – | – | – | – | 0.20 | 2 (1) | 2 | 0 | 0 |
| sideswipe | 5 | 0 | 0 | – | – | – | – | – | 0.00 | 2 (2) | 1 | 0 | 0 |
| single | 5 | 0 | 0 | – | – | – | – | – | 0.00 | 0 (0) | 0 | 0 | 0 |
| t_bone | 5 | 0 | 0 | – | – | – | – | – | 0.00 | 1 (1) | 0 | 0 | 0 |
