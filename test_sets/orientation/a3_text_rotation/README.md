# A3 Text Orientation Suite

This suite protects A3 page correction for 0°, 90°, 180°, and 270° inputs. It
uses nine saved A3 source pages from the routing suite. The runner generates all
four directions in memory, so the manifest represents 36 deterministic cases
without storing four duplicate image files.

Only OCR text, recognition confidence, and OCR text-box reading direction may
contribute to the decision. Page width, structural members, supports, loads,
and arrows must not contribute.

Run:

```powershell
python scripts/evaluate_a3_text_orientation.py
```

The command exits non-zero if an asset hash changes or any of the 36 cases
fails. The complete suite is a mandatory release gate after any orientation
code, OCR dependency, scoring threshold, or related image preprocessing change.
