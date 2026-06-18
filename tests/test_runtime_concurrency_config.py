"""Issue #361 — config knobs for runtime concurrency + the controller factory.

``runtime.max_parallel_tasks`` is the global agent cap (it already existed and
was validated, but nothing consumed it). ``runtime.max_parallel_per_role`` is a
new OPTIONAL per-role override (a config without it stays valid).
``build_concurrency_controller`` turns the config into the live controller.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from agentic_os.autonomy.concurrency import ConcurrencyController, build_concurrency_controller
from agentic_os.config.validators import _validate

_BASE = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "config" / "agentic-os.yml").read_text(encoding="utf-8")
)


def _cfg(**runtime_overrides):
    raw = copy.deepcopy(_BASE)
    raw["runtime"].update(runtime_overrides)
    return raw


# ---- validation ----------------------------------------------------------

def test_base_config_is_valid() -> None:
    assert _validate(copy.deepcopy(_BASE)) == []


def test_config_without_per_role_knob_still_valid() -> None:
    raw = copy.deepcopy(_BASE)
    raw["runtime"].pop("max_parallel_per_role", None)
    assert _validate(raw) == []


def test_per_role_caps_accepted() -> None:
    assert _validate(_cfg(max_parallel_per_role={"planner": 1, "implementer": 4})) == []


def test_per_role_cap_must_be_positive() -> None:
    errors = _validate(_cfg(max_parallel_per_role={"planner": 0}))
    assert any("max_parallel_per_role.planner" in e for e in errors)


def test_per_role_cap_rejects_out_of_range() -> None:
    errors = _validate(_cfg(max_parallel_per_role={"implementer": 99}))
    assert any("max_parallel_per_role.implementer" in e for e in errors)


def test_per_role_caps_must_be_mapping() -> None:
    errors = _validate(_cfg(max_parallel_per_role=[1, 2, 3]))
    assert any("max_parallel_per_role" in e for e in errors)


# ---- factory -------------------------------------------------------------

def test_build_controller_uses_global_and_per_role() -> None:
    raw = _cfg(max_parallel_tasks=6, max_parallel_per_role={"planner": 1})
    ctrl = build_concurrency_controller(raw)
    assert isinstance(ctrl, ConcurrencyController)
    assert ctrl.role_limit("planner") == 1  # explicit per-role cap
    assert ctrl.role_limit("triage") == 6  # unconfigured role inherits global


def test_build_controller_defaults_per_role_to_global() -> None:
    raw = copy.deepcopy(_BASE)
    raw["runtime"]["max_parallel_tasks"] = 4
    raw["runtime"].pop("max_parallel_per_role", None)
    ctrl = build_concurrency_controller(raw)
    assert ctrl.role_limit("implementer") == 4


def test_build_controller_threads_backpressure_check() -> None:
    cold = {"planner"}
    ctrl = build_concurrency_controller(
        _cfg(max_parallel_tasks=4),
        backpressure_check=lambda role: role in cold,
    )
    assert ctrl.is_backpressured("planner") is True
    assert ctrl.is_backpressured("implementer") is False
