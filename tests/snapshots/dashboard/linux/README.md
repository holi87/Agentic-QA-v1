# Dashboard screenshot baselines (Linux)

Status: active

Pixel-diff baselines for `tests/test_dashboard_screenshots.py`, captured
from the GitHub Actions `ubuntu-latest` runner (issue #166).

## Why Linux-only

Chromium's font rendering is subpixel-different between macOS, Linux and
Windows. A baseline captured on a dev macOS box would fail every Linux CI
run on antialiasing alone, so the diff gate is keyed to the same runner
that produces the comparison frame.

By default the gate runs on Linux (`sys.platform == "linux"`); on other
platforms it is a no-op. Force it on with
`AGENTIC_OS_SCREENSHOTS_GATE=1` if you need to debug locally — accept
that diffs against these baselines will not be 0%.

## Provenance

These PNGs come from a successful CI run of the `dashboard screenshots`
job on `main`. Downloaded with `gh run download <run-id> -n
dashboard-screenshots` and committed verbatim. The screen list matches
`screens` in `tests/test_dashboard_screenshots.py`.

## Refreshing after an intentional UI change

1. Land the UI change on a branch.
2. Wait for the CI screenshots job to run on the PR; download the
   `dashboard-screenshots` artifact.
3. Replace the affected PNGs under `tests/snapshots/dashboard/linux/`.
4. Push the baseline update on the same PR.

Or, regenerate in-place from a Linux runner (or a Linux dev box) with:

```bash
AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1 \
  pytest -m browser tests/test_dashboard_screenshots.py
```

The bootstrap mode writes missing baselines from the captured PNGs and
skips the diff gate so the run still passes.

## Tuning the threshold

`AGENTIC_OS_SCREENSHOTS_DIFF_THRESHOLD` (percentage, float, default
`1.0`) — pixels differing by more than the per-channel tolerance count
toward the percentage; the test fails when the percentage exceeds the
threshold. Per-pixel tolerance is fixed at `_CHANNEL_TOLERANCE = 8` in
the test module to absorb minor renderer noise without masking real
layout shifts.
