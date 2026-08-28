# Functional Regression Suites

Test sets are grouped by the behavior they protect rather than by the date or
the implementation that happened to create them.

- `routing/a1_a2_a3`: full-image routing into A1, A2, or A3.
- `orientation/a3_text_rotation`: A3 four-direction correction using OCR text
  regions only.

When a rotation defect is found, add the smallest original source image to the
orientation suite, update its SHA-256 manifest entry, and run the entire suite
before promoting the change. A rotation change must not ship unless every saved
orientation case passes.
