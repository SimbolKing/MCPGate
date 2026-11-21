"""Load, validate, and filter suite configuration, cases, and calibration set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from harness.config import EVALS_DIR
from harness.schema import (
    ALL_CATEGORIES,
    LAYER_VALUES,
    CalibrationSample,
    CalibrationSet,
    CaseSpec,
    SuiteConfig,
)


def _read_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是 YAML 映射对象")
    return data


def load_suite(evals_dir: Path = EVALS_DIR) -> SuiteConfig:
    path = evals_dir / "suite.yaml"
    return SuiteConfig.model_validate(_read_yaml(path))


def discover_case_files(evals_dir: Path = EVALS_DIR) -> list[Path]:
    cases_root = evals_dir / "cases"
    if not cases_root.exists():
        return []
    ordered: list[Path] = []
    # Keep canonical layer order: tool -> guard -> dialogue, then any unexpected subdirs for lint
    seen: set[Path] = set()
    for layer in LAYER_VALUES:
        layer_dir = cases_root / layer
        if layer_dir.is_dir():
            for path in sorted(layer_dir.glob("*.yaml")):
                ordered.append(path)
                seen.add(path)
    for path in sorted(cases_root.rglob("*.yaml")):
        if path not in seen:
            ordered.append(path)
    return ordered


def load_case(path: Path) -> CaseSpec:
    return CaseSpec.model_validate(_read_yaml(path))


def load_cases(evals_dir: Path = EVALS_DIR) -> list[tuple[Path, CaseSpec]]:
    return [(path, load_case(path)) for path in discover_case_files(evals_dir)]


def filter_cases(
    cases: list[CaseSpec],
    *,
    layers: list[str] | None = None,
    categories: list[str] | None = None,
    case_ids: list[str] | None = None,
) -> list[CaseSpec]:
    if layers is not None:
        unknown_layers = [layer for layer in layers if layer not in LAYER_VALUES]
        if unknown_layers:
            raise ValueError(f"未知的 layer: {', '.join(unknown_layers)}")
    if categories is not None:
        unknown_cats = [cat for cat in categories if cat not in ALL_CATEGORIES]
        if unknown_cats:
            raise ValueError(f"未知的 category: {', '.join(unknown_cats)}")
    if case_ids is not None:
        known_ids = {case.id for case in cases}
        unknown_ids = [cid for cid in case_ids if cid not in known_ids]
        if unknown_ids:
            raise ValueError(f"未知的用例 ID: {', '.join(unknown_ids)}")

    selected: list[CaseSpec] = []
    for case in cases:
        if layers is not None and case.layer not in layers:
            continue
        if categories is not None and case.category not in categories:
            continue
        if case_ids is not None and case.id not in case_ids:
            continue
        selected.append(case)
    return selected


def load_calibration(evals_dir: Path = EVALS_DIR) -> list[CalibrationSample]:
    path = evals_dir / "judge" / "calibration.yaml"
    return CalibrationSet.model_validate(_read_yaml(path)).samples


def load_tools_manifest(repo_root: Path) -> dict[str, dict[str, Any]]:
    path = repo_root / "gateway" / "generated" / "tools.manifest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} 必须是工具列表")
    return {str(item["name"]): item for item in raw if isinstance(item, dict) and "name" in item}
