"""Classification helpers for raw event ingestion."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


def load_ability_type_map(project_root: Path) -> Dict[str, Dict]:
    """Load ability type mapping from JSON (optional)."""
    mapping_path = project_root / "database" / "seeds" / "ability_types.json"
    if not mapping_path.exists():
        return {"overrides": {}, "keywords": {}}
    try:
        data = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ⚠️  Could not load ability type map: {exc}")
        return {"overrides": {}, "keywords": {}}
    overrides = {
        str(k).lower(): v for k, v in (data.get("overrides") or {}).items()
    }
    keywords = data.get("keywords") or {}
    return {"overrides": overrides, "keywords": keywords}


def classify_ability(ability_id: str, ability_name: str, ability_map: Dict[str, Dict]) -> str | None:
    """Classify ability into a coarse type (flash/smoke/stun/etc.)."""
    if not ability_map:
        return None
    overrides = ability_map.get("overrides", {})
    if ability_id and ability_id.lower() in overrides:
        return overrides[ability_id.lower()]
    if ability_name and ability_name.lower() in overrides:
        return overrides[ability_name.lower()]
    keywords = ability_map.get("keywords", {})
    text = " ".join([part for part in [ability_id, ability_name] if part]).lower()
    for category, tokens in keywords.items():
        if any(token in text for token in tokens):
            return category
    return None


def infer_tournament_region(tournament_name: str) -> str | None:
    """Infer tournament region from tournament name."""
    if not tournament_name:
        return None
    name = tournament_name.lower()
    if "americas" in name:
        return "americas"
    if "emea" in name:
        return "emea"
    if "pacific" in name:
        return "pacific"
    if "china" in name:
        return "china"
    return None
