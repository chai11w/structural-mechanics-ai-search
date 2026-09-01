# Qwen + RapidOrientation direction trial (2026-09-01)

## Decision

`keep_old`

Keep four-way OCR disabled. Do not add Qwen-directed rotation, do not lower the
RapidOrientation confidence threshold, and do not deploy either orientation
path from this experiment.

The requested goal was a fast gate that lowers the RapidOrientation threshold
without increasing wrong rotations. The joint gate did not achieve that goal.

## Scope

- Fixed routing suite: 15 source images, with A1/A2/A3 labels locked by the
  existing manifest.
- One source is a uniform white image and has no orientation truth. The other
  14 sources were verified upright.
- Synthetic inputs: 0/90/180/270 clockwise rotations, producing 60 cases.
- Qwen A/B: 120 DashScope calls to `qwen3.7-plus`, zero retries, two workers.
- RapidOrientation: local `rapid_orientation==0.0.11`, fixed ONNX model on
  `CPUExecutionProvider`, with no network calls.
- Cross-environment pre-transport inputs were joined by normalized RGB pixel
  SHA-256. The original Qwen environment reproduced all 120 encoded hashes
  before pixel hashes were backfilled; the Rapid environment matched all 60
  pre-transport pixel hashes.
- Qwen's outbound payload kept 56 cases as PNG, but resized and JPEG-encoded the
  four rotations of `A3/3.png`. Rapid was evaluated on the pre-transport PNGs,
  so the joint threshold scan is an approximate content-aligned comparison,
  not proof that both models received identical pixels.

## Qwen result

| Metric | Baseline | Orientation candidate |
| --- | ---: | ---: |
| Final route correct | 56/60 | 57/60 |
| Average latency | 14.267 s | 14.130 s |
| P50 latency | 13.519 s | 13.630 s |
| P95 latency | 19.537 s | 22.294 s |
| Estimated cost | CNY 0.373864 | CNY 0.412776 |

- There were no final-route regressions and one single-pass improvement. Ten
  cases changed at least one legacy observation field, so this run is not
  evidence that the added fields are behaviorally neutral.
- Paired candidate-minus-baseline latency was -0.136 s on average, -0.397 s at
  P50, and +5.487 s at P95. This single run does not establish either a speed
  gain or stable tail latency.
- Candidate incremental estimated cost was CNY 0.038912 for 60 calls, or about
  CNY 0.000649 per production page. Combined A/B estimated cost was CNY
  0.786640; the provider bill remains authoritative.
- Exact correction angle was 41/56 (73.21%). Every observable clockwise-90
  input was reported as a 90-degree correction instead of 270 degrees. Qwen
  therefore cannot control the correction direction.
- As a binary `rotation needed` signal, Qwen was 55/56: zero false positives on
  14 upright cases and 41/42 rotated cases detected. The missed 180-degree case
  is a sparse, nearly symmetric continuous-beam image with weak text-direction
  evidence.
- On the uniform white image, Qwen correctly abstained once but confidently
  returned zero on three equivalent rotations. Uniform images must be forced
  to preserve their pixels outside the model.

## RapidOrientation and joint gate

- Raw RapidOrientation angle accuracy: 49/56 (87.50%).
- Model inference latency: 15.465 ms average, 17.851 ms P95.
- Total local preprocessing plus inference: 30.512 ms average, 56.962 ms P95.

| Policy and threshold | Corrected rotated cases | Unsafe actions |
| --- | ---: | ---: |
| Rapid only, 0.00 | 37/42 | 5 |
| Qwen binary gate, 0.00 | 37/42 | 3 |
| Rapid only, 0.90 | 32/42 | 1 |
| Qwen binary gate, 0.90 | 32/42 | 1 |
| Rapid only, 0.91 | 32/42 | 0 |
| Qwen binary gate, 0.91 | 32/42 | 0 |
| Qwen angle agreement, 0.91 | 22/42 | 0 |

The binary Qwen gate blocked two RapidOrientation false rotations on upright
images. It could not block three cases where the image really was rotated but
RapidOrientation selected the wrong correction angle. As a result, both Rapid
alone and the Qwen binary gate still require threshold 0.91 for zero unsafe
actions on this suite, and both correct only 32/42 rotated cases (76.19%).

Requiring exact Qwen/Rapid angle agreement is worse because Qwen does not
reliably distinguish 90 from 270 degrees.

## Limits and artifacts

This is a deliberately rotation-heavy synthetic suite. Real uploads are mostly
upright, so overall accuracy from this balanced experiment must not be treated
as a production error rate. Fourteen observable source images are also too few
to establish a production threshold.

Raw results remain ignored and local:

- `.tmp_qwen_orientation_routing_eval/ab_results.json`
- `.tmp_qwen_orientation_routing_eval/rapid_joint_results.json`

The evaluators and tests are retained on the experiment branch so the evidence
can be reproduced without changing production code.
