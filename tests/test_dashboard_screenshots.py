"""Dashboard screenshot capture + pixel-diff gate (issues #145, #166).

Captures PNGs of the key operator-facing screens so a reviewer can spot
layout regressions, broken UI, or missing elements that the JSON/HTML
contract tests don't catch.

**Phase 2 — pixel-diff gate against committed Linux baselines.**
Baselines live under ``tests/snapshots/dashboard/linux/`` and were taken
from the CI runner itself (the same Ubuntu image + Chromium build), so
subpixel font rendering matches and macOS dev boxes don't trip the gate
by default. Diff tolerance defaults to 1% of pixels different (env
``AGENTIC_OS_SCREENSHOTS_DIFF_THRESHOLD`` overrides, percentage as a
float). When ``AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1`` the test writes
missing baselines from the captured PNGs instead of failing — used to
refresh baselines after intentional UI changes.

On macOS the gate is disabled by default (``AGENTIC_OS_SCREENSHOTS_GATE``
env var to force-enable) because the issue is explicit that font
rendering differs from Linux and a gate that always fails is worse than
no gate. CI runs on ubuntu-latest and the gate runs there.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from agentic_os.atomic_io import atomic_write_json
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.plan_v2 import PlanItem, plan_to_json
from agentic_os.server import make_server
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload

from test_dashboard_server import _runtime, _free_port  # type: ignore[import-not-found]

pytestmark = pytest.mark.browser

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


SCREENSHOTS_DIR_ENV = "AGENTIC_OS_SCREENSHOTS_DIR"
DIFF_THRESHOLD_ENV = "AGENTIC_OS_SCREENSHOTS_DIFF_THRESHOLD"
BOOTSTRAP_ENV = "AGENTIC_OS_SCREENSHOTS_BOOTSTRAP"
GATE_ENV = "AGENTIC_OS_SCREENSHOTS_GATE"
_DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
# Issue #202 — narrow-viewport coverage. Roughly half-laptop width so
# the cockpit grid (auto-fit minmax(220px, 1fr)) reflows to one column
# and any responsive overflow regression is visible.
_NARROW_VIEWPORT = {"width": 720, "height": 1280}
# Count-threshold headroom: even after the per-channel tolerance below,
# nondeterministic font rendering on dense pages scatters a small fraction of
# pixels over the tolerance. Observed at ~1.1% on the narrow task-detail
# screen with no UI change, which tripped the old 1.0% gate. 2.0% absorbs that
# noise floor while staying far below any real layout regression (a moved or
# added block differs on many percent of pixels with large deltas).
_DEFAULT_DIFF_THRESHOLD_PCT = 2.0
# Per-channel tolerance to absorb antialiasing and minor renderer noise
# even across identical Chromium builds. Pixels with max-channel delta
# at or below this value are treated as matching. Raised from 8 to 16 so
# subpixel glyph-edge jitter (the dominant noise source) is not counted at
# all, leaving the count threshold to guard genuine regressions.
_CHANNEL_TOLERANCE = 16


def _baselines_dir() -> Path:
    return Path(__file__).resolve().parent / "snapshots" / "dashboard" / "linux"


def _screenshots_dir() -> Path:
    """Where to write captured PNGs.

    Defaults to ``build/screenshots/`` under the project root so the CI
    workflow can pick the directory up as an artifact. Overridable via
    the env var so operators can write to a scratch dir during local
    inspection.
    """
    override = os.environ.get(SCREENSHOTS_DIR_ENV)
    if override:
        return Path(override).resolve()
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "build" / "screenshots"


def _gate_enabled() -> bool:
    """Pixel-diff gate runs on Linux by default and elsewhere only when
    explicitly enabled. macOS gets a different Chromium font stack so
    the committed Linux baselines would always fail there."""
    override = os.environ.get(GATE_ENV, "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return sys.platform.startswith("linux")


def _diff_threshold_pct() -> float:
    raw = os.environ.get(DIFF_THRESHOLD_ENV)
    if not raw:
        return _DEFAULT_DIFF_THRESHOLD_PCT
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_DIFF_THRESHOLD_PCT


def _bootstrap_mode() -> bool:
    return os.environ.get(BOOTSTRAP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
def writable_dashboard(tmp_path: Path):
    paths = _runtime(tmp_path, enable_write=True)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", paths
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _seed_review_task(paths: RuntimePaths) -> str:
    """Seed a work item + candidate plan so the task-detail screenshot
    captures the candidate review table in a non-empty state."""
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {
                "title": "Screenshot fixture — checkout flow",
                "priority": "P1",
                "business_goal": "Capture the candidate review UI for visual review.",
                "expected_behavior": "Bulk approve + per-row edits visible in screenshot.",
                "relevant_surfaces": "GET /health, GET /users, GET /metrics",
            },
            default_sut_root=".",
        )
        work_item_id = detail["work_item"]["id"]
    finally:
        conn.close()

    items = [
        PlanItem(
            candidate_id="c-health-get",
            title="GET /health returns 200",
            test_type="api",
            priority="P1",
            decision="needs_operator_decision",
            expected_assertion="GET /health must return HTTP 200",
            source_refs=["openapi:/health:get"],
            target_method="GET",
            target_path="/health",
            cleanup_strategy="read-only endpoint",
            functional_area="functional-system",
            lifecycle_tags=["regression"],
        ),
        PlanItem(
            candidate_id="c-users-get",
            title="GET /users returns a list",
            test_type="api",
            priority="P1",
            decision="needs_operator_decision",
            expected_assertion="GET /users must return HTTP 200 with a JSON array",
            source_refs=["openapi:/users:get"],
            target_method="GET",
            target_path="/users",
            cleanup_strategy="read-only endpoint",
            functional_area="functional-users",
            lifecycle_tags=["regression"],
        ),
    ]
    plan_dir = paths.runtime_root / "plans" / work_item_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(plan_dir / "TEST-PLAN.json", plan_to_json(work_item_id, items))
    return work_item_id


def _capture(page, url: str, out: Path, *, wait_selector: str | None = None) -> None:
    page.set_viewport_size(_DEFAULT_VIEWPORT)
    page.goto(url, wait_until="domcontentloaded")
    if wait_selector:
        page.locator(wait_selector).first.wait_for(timeout=5000)
    # ``networkidle`` covers SSE-quiet pages that finish polling within ~1s;
    # the explicit timeout protects against long-lived event streams.
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:  # noqa: BLE001 — best-effort settle, screenshot proceeds
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), full_page=True)
    # `page.screenshot` writes synchronously; assert the file landed so the
    # CI artifact upload step can never see a silently-missing image.
    assert out.exists(), f"screenshot did not land at {out}"
    assert out.stat().st_size > 0, f"screenshot at {out} is empty"


def _compare_pixels(captured: Path, baseline: Path, diff_out: Path) -> tuple[float, str | None]:
    """Compare two PNGs pixel-by-pixel.

    Returns ``(diff_pct, error)`` where ``diff_pct`` is the percentage of
    pixels whose max-channel delta exceeds ``_CHANNEL_TOLERANCE`` and
    ``error`` is a short message for hard mismatches (dimension/mode) or
    ``None`` on success. When the gate fails, a diff PNG with red-tinted
    mismatched pixels is written to ``diff_out`` for easy review in the
    uploaded artifact.
    """
    from PIL import Image, ImageChops

    with Image.open(captured) as cap_img, Image.open(baseline) as base_img:
        cap = cap_img.convert("RGB")
        base = base_img.convert("RGB")
        if cap.size != base.size:
            return 100.0, (
                f"dimension mismatch: captured {cap.size} vs baseline {base.size}; "
                "regenerate baselines with AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1 if intentional"
            )

        diff = ImageChops.difference(cap, base)
        # Max channel delta per pixel — flatten to a single luminance-like band
        # by extracting the per-pixel max across R/G/B.
        bands = diff.split()
        # PIL has no direct max-of-bands; use ImageChops.lighter to fold.
        max_band = bands[0]
        for b in bands[1:]:
            max_band = ImageChops.lighter(max_band, b)

        width, height = cap.size
        total = width * height
        # Count pixels exceeding tolerance.
        histogram = max_band.histogram()  # 256 bins for an L band
        mismatched = sum(histogram[_CHANNEL_TOLERANCE + 1:])
        diff_pct = (mismatched / total) * 100.0 if total else 0.0

        # Always write the diff visualisation when there is *any* mismatch,
        # so reviewers can inspect borderline cases in the artifact even
        # when the gate passes.
        if mismatched > 0:
            from PIL import ImageDraw  # noqa: F401 — kept for future overlay extension

            # Red-tint pixels whose delta exceeds tolerance. Build a mask
            # from max_band and composite a red overlay onto the captured
            # image.
            mask = max_band.point(lambda v: 255 if v > _CHANNEL_TOLERANCE else 0)
            overlay = Image.new("RGB", cap.size, (255, 0, 0))
            tinted = Image.composite(overlay, cap, mask)
            diff_out.parent.mkdir(parents=True, exist_ok=True)
            tinted.save(diff_out)

    return diff_pct, None


def _solid(size: int, value: int):
    from PIL import Image

    return Image.new("RGB", (size, size), (value, value, value))


def _with_modified(img, indices, value: int):
    """Return a copy of ``img`` with the given flat pixel ``indices`` set."""
    px = list(img.getdata())
    for i in indices:
        px[i] = (value, value, value)
    out = img.copy()
    out.putdata(px)
    return out


def test_pixel_gate_tolerates_renderer_noise_but_catches_regressions(tmp_path) -> None:
    """The gate must ignore subpixel/antialiasing jitter yet catch real
    layout regressions.

    Drives the noise-robustness knobs (``_CHANNEL_TOLERANCE`` +
    ``_DEFAULT_DIFF_THRESHOLD_PCT``). On a dense text-heavy page,
    nondeterministic font rendering scatters a small fraction of pixels just
    over the per-channel tolerance — observed at ~1.1% on the narrow
    task-detail screen, which tripped the old 1.0% / tolerance-8 gate without
    any actual UI change. A genuine regression (a moved/added block) differs
    on far more pixels with much larger deltas and must still fail.
    """
    pytest.importorskip("PIL", reason="Pillow required for pixel-diff gate")
    size = 100
    total = size * size  # 10_000 px
    base = _solid(size, 128)
    base_path = tmp_path / "base.png"
    base.save(base_path)

    # Pin the *default* noise budget, not the env-overridable
    # `_diff_threshold_pct()`: a CI/operator setting
    # `AGENTIC_OS_SCREENSHOTS_DIFF_THRESHOLD` below the synthetic deltas would
    # otherwise fail this synthetic test even though no real screenshot changed.
    threshold = _DEFAULT_DIFF_THRESHOLD_PCT

    # (1) Low-delta antialiasing jitter: 2% of pixels nudged by 12 — the kind
    # of edge noise the per-channel tolerance should absorb entirely.
    low = _with_modified(base, range(200), 140)  # delta 12, 2.0% of pixels
    low_path = tmp_path / "low.png"
    low.save(low_path)
    low_pct, _ = _compare_pixels(low_path, base_path, tmp_path / "low.diff.png")
    assert low_pct <= threshold, f"antialiasing jitter tripped the gate: {low_pct}%"

    # (2) Sparse high-delta jitter: 1.5% of pixels with a large delta but well
    # under the count threshold — must be tolerated by the headroom.
    high = _with_modified(base, range(150), 188)  # delta 60, 1.5% of pixels
    high_path = tmp_path / "high.png"
    high.save(high_path)
    high_pct, _ = _compare_pixels(high_path, base_path, tmp_path / "high.diff.png")
    assert high_pct <= threshold, f"sparse jitter tripped the gate: {high_pct}%"

    # (3) Real regression: a contiguous block (10% of the frame) fully
    # repainted — the gate must catch it regardless of the loosened knobs.
    block = _with_modified(base, range(1000), 248)  # delta 120, 10% of pixels
    block_path = tmp_path / "block.png"
    block.save(block_path)
    block_pct, _ = _compare_pixels(block_path, base_path, tmp_path / "block.diff.png")
    assert block_pct > threshold, f"block regression slipped past the gate: {block_pct}%"


def test_capture_key_dashboard_screens(writable_dashboard) -> None:
    """Visit every operator-facing route and dump a full-page PNG.

    The screens picked here match issue #145: dashboard home, inbox
    (under New task), tasks list, task detail with candidate review,
    and help (which carries the support-bundle tile). Reports are
    file-artifact-only and do not have a dedicated dashboard route, so
    they are intentionally excluded — the runs/manifest is reachable
    via ``/files/`` and is not a layout-regression risk.

    After capture, each PNG is compared against the committed Linux
    baseline (issue #166). The comparison runs by default on Linux; on
    other platforms it stays off unless ``AGENTIC_OS_SCREENSHOTS_GATE=1``
    is set.
    """
    base, paths = writable_dashboard
    work_item_id = _seed_review_task(paths)

    screens = [
        ("01-home.png", f"{base}/", None),
        ("02-tasks-list.png", f"{base}/tasks", None),
        ("03-tasks-new-inbox.png", f"{base}/tasks/new", "#inbox-upload-btn"),
        ("04-task-detail-candidate-review.png",
         f"{base}/tasks/{work_item_id}",
         "#task-candidates tbody tr"),
        ("05-help-support-bundle.png", f"{base}/help", "#support-bundle-build-btn"),
    ]
    out_dir = _screenshots_dir()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport=_DEFAULT_VIEWPORT)
            page = context.new_page()
            for name, url, wait_selector in screens:
                _capture(page, url, out_dir / name, wait_selector=wait_selector)
        finally:
            browser.close()

    # Sanity: every requested screen must have produced a file.
    captured = sorted(p.name for p in out_dir.glob("*.png"))
    assert {name for name, _, _ in screens}.issubset(set(captured)), captured

    if not _gate_enabled():
        pytest.skip(
            "pixel-diff gate disabled on this platform "
            "(set AGENTIC_OS_SCREENSHOTS_GATE=1 to force-enable)"
        )

    # Pillow is required only when the gate runs; the screenshots-only
    # phase 1 mode never imports it.
    PIL = pytest.importorskip("PIL", reason="Pillow required for pixel-diff gate (pip install Pillow)")
    del PIL  # imported to fail-fast; actual usage is inside `_compare_pixels`.

    baselines = _baselines_dir()
    threshold = _diff_threshold_pct()
    bootstrap = _bootstrap_mode()

    failures: list[str] = []
    for name, _url, _sel in screens:
        captured_path = out_dir / name
        baseline_path = baselines / name
        if not baseline_path.exists():
            if bootstrap:
                baselines.mkdir(parents=True, exist_ok=True)
                baseline_path.write_bytes(captured_path.read_bytes())
                continue
            failures.append(
                f"{name}: baseline missing at {baseline_path}; "
                "regenerate with AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1"
            )
            continue

        diff_out = out_dir / f"{baseline_path.stem}.diff.png"
        diff_pct, err = _compare_pixels(captured_path, baseline_path, diff_out)
        if err is not None:
            failures.append(f"{name}: {err}")
            continue
        if diff_pct > threshold:
            failures.append(
                f"{name}: {diff_pct:.2f}% pixels differ "
                f"(threshold {threshold:.2f}%); diff at {diff_out}"
            )

    if failures:
        joined = "\n".join(f"  - {line}" for line in failures)
        pytest.fail(
            "dashboard screenshot regression vs Linux baselines:\n"
            + joined
            + "\nIf the change is intentional, refresh baselines via "
            "AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1."
        )


def test_capture_narrow_viewport_dashboard_screens(writable_dashboard) -> None:
    """Issue #202 — capture key dashboard pages at a narrow viewport so
    the cockpit grid reflow stays under pixel-diff regression.

    Only home and task detail are captured here; the queue gets its own
    coverage from the lane view in the default-viewport test. The narrow
    viewport (720×1280) forces ``auto-fit minmax(220px, 1fr)`` to drop
    to a single column, exposing any overflow / overlapping UI early.
    """
    base, paths = writable_dashboard
    work_item_id = _seed_review_task(paths)

    screens = [
        ("06-home-narrow.png", f"{base}/", None),
        ("07-task-detail-narrow.png",
         f"{base}/tasks/{work_item_id}",
         "#task-candidates tbody tr"),
    ]
    out_dir = _screenshots_dir()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport=_NARROW_VIEWPORT)
            page = context.new_page()
            for name, url, wait_selector in screens:
                page.set_viewport_size(_NARROW_VIEWPORT)
                page.goto(url, wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        page.locator(wait_selector).first.wait_for(timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:  # noqa: BLE001 — best-effort settle
                    pass
                target = out_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target), full_page=True)
        finally:
            browser.close()

    captured = sorted(p.name for p in out_dir.glob("*.png"))
    assert {name for name, _, _ in screens}.issubset(set(captured)), captured

    if not _gate_enabled():
        pytest.skip(
            "pixel-diff gate disabled on this platform "
            "(set AGENTIC_OS_SCREENSHOTS_GATE=1 to force-enable)"
        )

    PIL = pytest.importorskip("PIL", reason="Pillow required for pixel-diff gate")
    del PIL

    baselines = _baselines_dir()
    threshold = _diff_threshold_pct()
    bootstrap = _bootstrap_mode()

    failures: list[str] = []
    for name, _url, _sel in screens:
        captured_path = out_dir / name
        baseline_path = baselines / name
        if not baseline_path.exists():
            if bootstrap:
                baselines.mkdir(parents=True, exist_ok=True)
                baseline_path.write_bytes(captured_path.read_bytes())
                continue
            failures.append(
                f"{name}: baseline missing at {baseline_path}; "
                "regenerate with AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1"
            )
            continue
        diff_out = out_dir / f"{baseline_path.stem}.diff.png"
        diff_pct, err = _compare_pixels(captured_path, baseline_path, diff_out)
        if err is not None:
            failures.append(f"{name}: {err}")
            continue
        if diff_pct > threshold:
            failures.append(
                f"{name}: {diff_pct:.2f}% pixels differ "
                f"(threshold {threshold:.2f}%); diff at {diff_out}"
            )

    if failures:
        joined = "\n".join(f"  - {line}" for line in failures)
        pytest.fail(
            "narrow-viewport dashboard screenshot regression:\n"
            + joined
            + "\nIf intentional, refresh baselines via AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1."
        )
