"""HARD-16 — FR strings rename ``passage`` → ``tonte`` for the six
``run`` / ``current_run`` / ``last_run`` sensors.

Pure translation-data diff: no code / ``unique_id`` / ``key`` change,
no ``en.json`` / ``strings.json`` change. Consumers keep working; only
the display strings shift to the operator's vocabulary.

Each test reads ``translations/fr.json`` directly — that's a *data*
file, not source. Per CONTRIBUTING: source-level greps are forbidden,
data-file assertions are the right way to lock a translation contract.
"""

from __future__ import annotations

import json
from pathlib import Path

FR_JSON = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "navimow"
    / "translations"
    / "fr.json"
)
EN_JSON = FR_JSON.with_name("en.json")


def _fr_sensor(key: str) -> dict:
    data = json.loads(FR_JSON.read_text(encoding="utf-8"))
    return data["entity"]["sensor"][key]


def _en_sensor(key: str) -> dict:
    data = json.loads(EN_JSON.read_text(encoding="utf-8"))
    return data["entity"]["sensor"][key]


# --------------------------------------------------------------------- #
# 1. The six renamed keys                                               #
# --------------------------------------------------------------------- #


def test_run_progress_uses_tonte() -> None:
    # FEAT-08 (#88 comment): keys renamed to `current_*` — the FR
    # vocabulary reads "en cours" for parallelism with the noun.
    assert (
        _fr_sensor("current_run_progress")["name"] == "Progression de la tonte en cours"
    )


def test_run_state_uses_tonte() -> None:
    assert _fr_sensor("current_run_state")["name"] == "État de la tonte en cours"


def test_current_run_started_uses_tonte_en_cours() -> None:
    # `courant` → `en cours` on top of the passage→tonte swap: `tonte
    # courante` reads oddly, so the rename doubles as a rephrasing.
    assert _fr_sensor("current_run_started")["name"] == "Début de la tonte en cours"


def test_last_run_started_uses_tonte() -> None:
    assert _fr_sensor("last_run_started")["name"] == "Début de la dernière tonte"


def test_last_run_duration_uses_tonte() -> None:
    assert _fr_sensor("last_run_duration")["name"] == "Durée de la dernière tonte"


def test_last_run_result_uses_tonte() -> None:
    assert _fr_sensor("last_run_result")["name"] == "Résultat de la dernière tonte"


def test_no_passage_left_on_any_renamed_key() -> None:
    """Belt-and-braces: none of the renamed name strings still carries
    the word ``passage`` (case-insensitive). Post-FEAT-08 the run
    family expanded to include `current_run_state` /
    `current_run_progress` (renamed from `run_state` / `run_progress`)."""
    for key in (
        "current_run_progress",
        "current_run_state",
        "current_run_started",
        "last_run_started",
        "last_run_duration",
        "last_run_result",
        "last_run_area",  # FEAT-08 promotion
    ):
        name = _fr_sensor(key)["name"]
        assert "passage" not in name.lower(), f"{key} → {name!r}"


# --------------------------------------------------------------------- #
# 2. Locked regression — enums and zone strings untouched               #
# --------------------------------------------------------------------- #


def test_run_state_enum_unchanged() -> None:
    """The nested ``state.*`` sub-dict qualifies the outcome, not the
    noun. The HARD-16 nouns stay identical (post-FEAT-08 rename, the key
    is now `current_run_state`); HARD-18 (#117) adds the `starting`
    provisional-window state, which is the only permitted addition."""
    state = _fr_sensor("current_run_state")["state"]
    assert state == {
        "idle": "Au repos",
        "starting": "Démarrage",
        "running": "En cours",
        "paused": "En pause",
        "returning": "Retour",
    }


def test_last_run_result_enum_unchanged() -> None:
    state = _fr_sensor("last_run_result")["state"]
    assert state == {
        "completed": "Terminé",
        "interrupted": "Interrompu",
    }


def test_zone_progress_stays_on_zone() -> None:
    """The zone family carries its own noun (``zone``, not
    ``tonte``). Post-FEAT-08 the key is `current_zone_progress`."""
    assert (
        _fr_sensor("current_zone_progress")["name"] == "Progression de la zone en cours"
    )


def test_current_zone_stays_on_zone() -> None:
    assert _fr_sensor("current_zone")["name"] == "Zone courante"


def test_battery_and_position_untouched() -> None:
    """Non-run sensors unchanged — pin the diff scope."""
    assert _fr_sensor("battery")["name"] == "Batterie"
    assert _fr_sensor("position")["name"] == "Position"
    assert _fr_sensor("weekly_area")["name"] == "Surface hebdomadaire"
    assert _fr_sensor("zones")["name"] == "Zones"


# --------------------------------------------------------------------- #
# 3. Scope discipline — EN untouched                                    #
# --------------------------------------------------------------------- #


def test_english_names_untouched() -> None:
    """The EN strings track the FEAT-08 key rename mechanically (the
    display carries the key's `current`/`last` prefix). Locking them
    here guards against an accidental cross-locale edit."""
    assert _en_sensor("current_run_progress")["name"] == "Current run progress"
    assert _en_sensor("current_run_state")["name"] == "Current run state"
    assert _en_sensor("current_run_started")["name"] == "Current run started"
    assert _en_sensor("last_run_started")["name"] == "Last run started"
    assert _en_sensor("last_run_duration")["name"] == "Last run duration"
    assert _en_sensor("last_run_result")["name"] == "Last run result"
    assert _en_sensor("last_run_area")["name"] == "Last run area"
