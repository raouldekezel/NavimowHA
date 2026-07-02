"""CHORE-01 smoke test — verifies the pytest CI harness itself.

This test exists so that the pytest job on an otherwise empty PR still
returns exit 0 (no tests collected returns exit 5 which breaks CI). It
also proves that the integration module imports cleanly under the pinned
harness — a canary for silent SDK / HA breakage at the manifest level.

Delete this file once a real behavioural test lives under tests/.
"""

from __future__ import annotations


def test_integration_module_imports_cleanly() -> None:
    """Actually exercise the import path the docstring promises.

    Importing `custom_components.navimow` triggers `__init__.py` top-level
    (imports Platform, ConfigEntry, our const module, etc.) and thus
    catches a silent HA or transitive-dependency breakage introduced by a
    future upstream sync, before any behavioural test runs.
    """
    import custom_components.navimow  # noqa: F401
    import custom_components.navimow.const  # noqa: F401


def test_manifest_domain_matches_folder() -> None:
    """The manifest domain must match the folder name — HACS relies on it."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    manifest_path = root / "custom_components" / "navimow" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["domain"] == "navimow"
    assert manifest["config_flow"] is True
