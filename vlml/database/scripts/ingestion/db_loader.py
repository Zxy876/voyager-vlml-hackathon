#!/usr/bin/env python3
"""Database loader helpers for raw event ingestion."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Dict, List, Tuple

import duckdb

from ingestion.parsers import (
    extract_metadata_from_path,
    parse_iso_datetime,
    parse_iso_duration,
    parse_jsonl_file,
)
from ingestion.classifiers import classify_ability, infer_tournament_region, load_ability_type_map


def process_series_bulk(conn: duckdb.DuckDBPyConnection, file_path: Path) -> Tuple[bool, int]:
    """Process a single series JSONL file using bulk operations.

    Args:
        conn: DuckDB connection
        file_path: Path to JSONL file

    Returns:
        Tuple of (success, event_count)
    """
    # Extract metadata from path
    metadata = extract_metadata_from_path(file_path)
    series_id = metadata['series_id']
    tournament_year = metadata['year']
    tournament_name = metadata['tournament']

    # Check if already loaded
    result = conn.execute(
        "SELECT COUNT(*) FROM series WHERE series_id = ?",
        [series_id]
    ).fetchone()

    if result[0] > 0:
        print(f"  ⏭️  Already loaded")
        return True, 0

    # Parse events
    try:
        events = parse_jsonl_file(file_path)
        if not events:
            print(f"  ⚠️  No events found")
            return False, 0

        print(f"  📖 Parsed {len(events):,} events")
    except Exception as e:
        print(f"  ❌ Error parsing: {e}")
        return False, 0

    return process_series_bulk_events(conn, file_path, events)


def process_series_bulk_events(
    conn: duckdb.DuckDBPyConnection, file_path: Path, events: List[Dict]
) -> Tuple[bool, int]:
    """Process a single series using pre-parsed events."""
    # Extract metadata from path
    metadata = extract_metadata_from_path(file_path)
    series_id = metadata['series_id']
    tournament_year = metadata['year']
    tournament_name = metadata['tournament']

    # Check if already loaded
    result = conn.execute(
        "SELECT COUNT(*) FROM series WHERE series_id = ?",
        [series_id]
    ).fetchone()

    if result[0] > 0:
        print(f"  ⏭️  Already loaded")
        return True, 0

    # Build batch insert lists
    series_data = []
    games_data = []
    rounds_data = []
    events_data = []

    # Track state
    current_game_id = None
    current_round_id = None
    game_number = 0
    round_number = 0
    teams = set()
    games_seen = set()
    rounds_seen = set()

    # Build team ID -> team name mapping from seriesState
    team_id_to_name = {}
    player_id_to_team_id = {}
    player_id_to_name = {}
    player_id_to_agent_name = {}
    player_id_to_agent_name_by_game = {}
    platform_game_id_to_internal = {}

    series_info = {
        "series_id": series_id,
        "tournament_id": None,
        "tournament_name": tournament_name,
        "tournament_year": tournament_year,
        "tournament_region": infer_tournament_region(tournament_name),
        "team1_name": None,
        "team2_name": None,
        "winning_team_name": None,
        "start_time": None,
    }
    games_info = {}
    rounds_info = {}

    for event in events:
        if 'seriesState' in event and 'teams' in event['seriesState']:
            for team in event['seriesState']['teams']:
                team_id = str(team.get('id'))
                team_name = team.get('name')
                if team_id and team_name:
                    team_id_to_name[team_id] = team_name

                # Map players to teams
                for player in team.get('players', []):
                    player_id = str(player.get('id'))
                    if player_id:
                        player_id_to_team_id[player_id] = team_id
                    player_name = player.get('name')
                    if player_id and player_name:
                        player_id_to_name[player_id] = player_name
                    character = player.get('character', {})
                    agent_name = character.get('name') or character.get('id')
                    if player_id and agent_name:
                        player_id_to_agent_name[player_id] = agent_name
        if 'seriesState' in event and 'games' in event['seriesState']:
            for game in event['seriesState']['games']:
                sequence_number = game.get("sequenceNumber")
                internal_game_id = f"{series_id}_game_{sequence_number}" if sequence_number else None
                for team in game.get('teams', []):
                    team_id = str(team.get('id'))
                    team_name = team.get('name')
                    if team_id and team_name:
                        team_id_to_name[team_id] = team_name

                    for player in team.get('players', []):
                        player_id = str(player.get('id'))
                        if player_id:
                            player_id_to_team_id[player_id] = team_id
                        player_name = player.get('name')
                        if player_id and player_name:
                            player_id_to_name[player_id] = player_name
                        character = player.get('character', {})
                        agent_name = character.get('name') or character.get('id')
                        if player_id and agent_name:
                            if internal_game_id:
                                player_id_to_agent_name_by_game.setdefault(
                                    internal_game_id, {}
                                )[player_id] = agent_name
                            player_id_to_agent_name[player_id] = agent_name

    try:
        # Start transaction for atomic commit
        conn.execute("BEGIN TRANSACTION")

        # Build weapon type lookup
        weapon_type_lookup = {
            row[0].lower(): row[1]
            for row in conn.execute("SELECT weapon_name, weapon_type FROM weapon_types").fetchall()
            if row[0]
        }
        project_root = Path(__file__).resolve().parents[3]
        ability_type_map = load_ability_type_map(project_root)

        # Process events and build batch data
        def extract_damage_info(series_state_delta: Dict, player_id: str, team_id: str) -> Dict:
            """Extract damage and weapon info for a player from seriesStateDelta."""
            if not series_state_delta or not player_id:
                return {}

            def _match_player(team_node: Dict) -> Dict:
                for player in team_node.get("players", []):
                    if str(player.get("id")) == str(player_id):
                        return player
                return {}

            def _extract_from_team(team_node: Dict) -> Dict:
                player_node = _match_player(team_node)
                if not player_node:
                    return {}
                sources = player_node.get("damageDealtSources") or team_node.get("damageDealtSources") or []
                targets = player_node.get("damageDealtTargets") or team_node.get("damageDealtTargets") or []
                weapon_name = None
                hit_location = None
                is_headshot = False

                if len(sources) == 1:
                    weapon_name = sources[0].get("source", {}).get("name")
                if targets:
                    for target in targets:
                        target_name = target.get("target", {}).get("name")
                        if target_name:
                            if hit_location is None:
                                hit_location = target_name
                            if target_name == "head":
                                is_headshot = True

                return {
                    "damage_dealt": player_node.get("damageDealt"),
                    "weapon_name": weapon_name,
                    "hit_location": hit_location,
                    "is_headshot": is_headshot,
                }

            for game in series_state_delta.get("games", []):
                for team in game.get("teams", []):
                    if team_id and str(team.get("id")) != str(team_id):
                        continue
                    extracted = _extract_from_team(team)
                    if extracted:
                        return extracted

                for segment in game.get("segments", []):
                    for team in segment.get("teams", []):
                        if team_id and str(team.get("id")) != str(team_id):
                            continue
                        extracted = _extract_from_team(team)
                        if extracted:
                            return extracted

            return {}

        def infer_end_reason(target_state: Dict) -> str:
            win_type = target_state.get("winType")
            if win_type:
                return win_type
            objectives = []
            for team in target_state.get("teams", []):
                for obj in team.get("objectives", []):
                    obj_type = obj.get("type") or obj.get("id") or ""
                    if obj_type:
                        objectives.append(obj_type)
            for obj_type in objectives:
                lowered = obj_type.lower()
                if "explodebomb" in lowered:
                    return "detonated"
                if "defusebomb" in lowered and "begin" not in lowered and "stop" not in lowered:
                    return "defused"
                if "time" in lowered:
                    return "time"
            if objectives or target_state.get("teams"):
                return "eliminated"
            return None

        for event in events:
            event_type = event.get("type", "")
            occurred_at = event.get("occurredAt")
            event_base_id = event.get("id")
            if not event_base_id:
                event_base_id = (
                    f"{series_id}_{current_game_id}_{current_round_id}_"
                    f"{event_type}_{occurred_at}_{event.get('action')}"
                )

            # Track teams from actors/targets
            if event.get("actor") and event["actor"].get("type") == "team":
                teams.add(event["actor"].get("name"))
            if event.get("target") and event["target"].get("type") == "team":
                teams.add(event["target"].get("name"))

            # Handle game start
            if event_type == "series-started-game":
                game_number += 1
                current_game_id = f"{series_id}_game_{game_number}"
                round_number = 0

                series_state = event.get("seriesState", {})
                if series_state.get("startedAt") and not series_info["start_time"]:
                    series_info["start_time"] = series_state.get("startedAt")
                series_team_names = [t.get("name") for t in series_state.get("teams", []) if t.get("name")]
                if len(series_team_names) >= 2:
                    series_info["team1_name"] = series_info["team1_name"] or series_team_names[0]
                    series_info["team2_name"] = series_info["team2_name"] or series_team_names[1]

                game_state = None
                for game in series_state.get("games", []):
                    if game.get("sequenceNumber") == game_number:
                        game_state = game
                        break

                if game_state:
                    platform_game_id = game_state.get("id")
                    if platform_game_id:
                        platform_game_id_to_internal[platform_game_id] = current_game_id

                # Preload map names for any games listed in the snapshot
                for game in series_state.get("games", []):
                    sequence_number = game.get("sequenceNumber")
                    if not sequence_number:
                        continue
                    internal_game_id = f"{series_id}_game_{sequence_number}"
                    if internal_game_id not in games_info:
                        games_info[internal_game_id] = {
                            "game_id": internal_game_id,
                            "series_id": series_id,
                            "game_number": sequence_number,
                            "map_name": game.get("map", {}).get("name"),
                            "team1_name": None,
                            "team2_name": None,
                            "winning_team_name": None,
                            "game_duration_seconds": None,
                            "total_rounds": None,
                        }
                    else:
                        games_info[internal_game_id]["map_name"] = (
                            games_info[internal_game_id].get("map_name")
                            or game.get("map", {}).get("name")
                        )

                if current_game_id not in games_seen:
                    games_info[current_game_id] = {
                        "game_id": current_game_id,
                        "series_id": series_id,
                        "game_number": game_number,
                        "map_name": (game_state or {}).get("map", {}).get("name") or event.get("platformGameId"),
                        "team1_name": None,
                        "team2_name": None,
                        "winning_team_name": None,
                        "game_duration_seconds": None,
                        "total_rounds": None,
                    }
                    if game_state:
                        team_names = [t.get("name") for t in game_state.get("teams", []) if t.get("name")]
                        if len(team_names) >= 2:
                            games_info[current_game_id]["team1_name"] = team_names[0]
                            games_info[current_game_id]["team2_name"] = team_names[1]
                    games_seen.add(current_game_id)

            if event_type == "tournament-started-series":
                actor_state = event.get("actor", {}).get("state", {})
                target_state = event.get("target", {}).get("state", {})
                series_info["tournament_id"] = actor_state.get("id") or series_info["tournament_id"]
                series_info["tournament_name"] = actor_state.get("name") or series_info["tournament_name"]
                series_info["tournament_region"] = (
                    infer_tournament_region(series_info["tournament_name"])
                    or series_info["tournament_region"]
                )
                series_info["start_time"] = target_state.get("startedAt") or series_info["start_time"]
                team_names = [t.get("name") for t in target_state.get("teams", []) if t.get("name")]
                if len(team_names) >= 2:
                    series_info["team1_name"] = team_names[0]
                    series_info["team2_name"] = team_names[1]

            # Handle round start
            elif event_type == "game-started-round":
                round_number += 1
                current_round_id = f"{current_game_id}_round_{round_number}"

                if current_round_id not in rounds_seen:
                    round_map_name = None
                    if current_game_id and current_game_id in games_info:
                        round_map_name = games_info[current_game_id].get("map_name")
                    round_map_name = round_map_name or event.get("platformGameId")
                    rounds_info[current_round_id] = {
                        "round_id": current_round_id,
                        "series_id": series_id,
                        "game_id": current_game_id,
                        "round_number": round_number,
                        "map_name": round_map_name,
                        "started_at": occurred_at,
                        "ended_at": None,
                        "duration_seconds": None,
                        "winning_team_name": None,
                        "losing_team_name": None,
                        "end_reason": None,
                        "tournament_name": tournament_name,
                        "tournament_year": tournament_year,
                    }
                    rounds_seen.add(current_round_id)
            elif event_type == "game-ended-round":
                game_state = event.get("actor", {}).get("state", {})
                target_state = event.get("target", {}).get("state", {})
                platform_game_id = game_state.get("id")
                internal_game_id = platform_game_id_to_internal.get(platform_game_id, current_game_id)
                round_seq = target_state.get("sequenceNumber")
                if internal_game_id and round_seq:
                    if internal_game_id in games_info:
                        game_duration = parse_iso_duration(game_state.get("duration"))
                        if game_duration:
                            games_info[internal_game_id]["game_duration_seconds"] = game_duration
                    round_id = f"{internal_game_id}_round_{round_seq}"
                    round_map_name = None
                    if internal_game_id in games_info:
                        round_map_name = games_info[internal_game_id].get("map_name")
                    round_map_name = round_map_name or event.get("platformGameId")
                    round_entry = rounds_info.get(round_id, {
                        "round_id": round_id,
                        "series_id": series_id,
                        "game_id": internal_game_id,
                        "round_number": round_seq,
                        "map_name": round_map_name,
                        "started_at": target_state.get("startedAt"),
                        "ended_at": None,
                        "duration_seconds": None,
                        "winning_team_name": None,
                        "losing_team_name": None,
                        "end_reason": None,
                        "tournament_name": tournament_name,
                        "tournament_year": tournament_year,
                    })
                    round_entry["started_at"] = round_entry["started_at"] or target_state.get("startedAt")
                    duration_seconds = parse_iso_duration(target_state.get("duration"))
                    round_entry["duration_seconds"] = duration_seconds
                    if round_entry["started_at"] and duration_seconds:
                        started_dt = parse_iso_datetime(round_entry["started_at"])
                        if started_dt:
                            round_entry["ended_at"] = (started_dt + timedelta(seconds=duration_seconds)).isoformat()
                    round_teams = target_state.get("teams", [])
                    winners = [t.get("name") for t in round_teams if t.get("won")]
                    if winners:
                        round_entry["winning_team_name"] = winners[0]
                        losers = [t.get("name") for t in round_teams if t.get("name") and t.get("name") != winners[0]]
                        round_entry["losing_team_name"] = losers[0] if losers else None
                    round_entry["end_reason"] = infer_end_reason(target_state) or round_entry["end_reason"]
                    rounds_info[round_id] = round_entry
            elif event_type == "team-won-round":
                target_state = event.get("target", {}).get("state", {})
                round_seq = target_state.get("sequenceNumber")
                winner = event.get("actor", {}).get("state", {}).get("name")
                if current_game_id and round_seq and winner:
                    round_id = f"{current_game_id}_round_{round_seq}"
                    round_entry = rounds_info.get(round_id)
                    if round_entry and not round_entry.get("winning_team_name"):
                        round_entry["winning_team_name"] = winner
            elif event_type == "team-won-game":
                platform_game_id = event.get("target", {}).get("state", {}).get("id")
                internal_game_id = platform_game_id_to_internal.get(platform_game_id, current_game_id)
                winner = event.get("actor", {}).get("state", {}).get("name")
                if internal_game_id and winner and internal_game_id in games_info:
                    games_info[internal_game_id]["winning_team_name"] = winner
            elif event_type == "team-won-series":
                winner = event.get("actor", {}).get("state", {}).get("name")
                if winner:
                    series_info["winning_team_name"] = winner

            # Extract actor info
            actor_player_id = None
            actor_player_name = None
            actor_team_name = None
            actor_agent_name = None
            actor_side = None
            actor_pos_x = None
            actor_pos_y = None
            actor_loadout_value = None
            actor_net_worth = None

            if event.get("actor"):
                actor = event["actor"]
                if actor.get("type") == "player":
                    actor_player_id = str(actor.get("id"))
                    actor_player_name = actor.get("state", {}).get("name") or player_id_to_name.get(actor_player_id)

                    # Get team from mapping
                    team_id = player_id_to_team_id.get(actor_player_id)
                    if team_id:
                        actor_team_name = team_id_to_name.get(team_id)
                    elif actor.get("state", {}).get("teamId"):
                        actor_team_name = team_id_to_name.get(str(actor["state"]["teamId"]))
                    actor_agent_name = (
                        player_id_to_agent_name_by_game
                        .get(current_game_id, {})
                        .get(actor_player_id)
                    ) or player_id_to_agent_name.get(actor_player_id)
                    actor_side = actor.get("state", {}).get("side")
                    actor_game_state = actor.get("state", {}).get("game", {})
                    actor_pos = actor_game_state.get("position") or {}
                    actor_pos_x = actor_pos.get("x")
                    actor_pos_y = actor_pos.get("y")
                    actor_loadout_value = actor_game_state.get("loadoutValue")
                    actor_net_worth = actor_game_state.get("netWorth")

                elif actor.get("type") == "team":
                    actor_team_name = actor.get("name")

            # Extract target info
            target_player_id = None
            target_player_name = None
            target_team_name = None
            target_agent_name = None
            target_side = None
            target_pos_x = None
            target_pos_y = None
            target_loadout_value = None
            target_net_worth = None

            if event.get("target"):
                target = event["target"]
                if target.get("type") == "player":
                    target_player_id = str(target.get("id"))
                    target_player_name = target.get("state", {}).get("name") or player_id_to_name.get(target_player_id)

                    # Get team from mapping
                    team_id = player_id_to_team_id.get(target_player_id)
                    if team_id:
                        target_team_name = team_id_to_name.get(team_id)
                    elif target.get("state", {}).get("teamId"):
                        target_team_name = team_id_to_name.get(str(target["state"]["teamId"]))
                    target_agent_name = (
                        player_id_to_agent_name_by_game
                        .get(current_game_id, {})
                        .get(target_player_id)
                    ) or player_id_to_agent_name.get(target_player_id)
                    target_side = target.get("state", {}).get("side")
                    target_game_state = target.get("state", {}).get("game", {})
                    target_pos = target_game_state.get("position") or {}
                    target_pos_x = target_pos.get("x")
                    target_pos_y = target_pos.get("y")
                    target_loadout_value = target_game_state.get("loadoutValue")
                    target_net_worth = target_game_state.get("netWorth")

                elif target.get("type") == "team":
                    target_team_name = target.get("name")

            # Determine event flags
            is_kill = event_type == "player-killed-player"
            is_death = False  # Will be set for target of kill events
            is_plant = event_type in ["spike-planted", "player-completed-plantBomb"]
            is_defuse = event_type in ["spike-defused", "player-completed-defuseBomb"]
            is_begin_defuse = event_type == "player-completed-beginDefuseBomb"
            is_stop_defuse = event_type == "player-completed-stopDefuseBomb"
            is_half_defuse = event_type == "player-completed-reachDefuseBombCheckpoint"
            is_defuse_complete = event_type == "player-completed-defuseBomb"
            is_bomb_exploded = event_type == "player-completed-explodeBomb"
            is_ability_use = "ability" in event_type

            # Extract weapon info (if available)
            weapon_name = None
            weapon_type = None
            is_headshot = False
            hit_location = None
            damage_dealt = 0
            ability_id = None
            ability_name = None
            ability_type = None
            team_loadout_value = None
            team_net_worth = None

            if event_type == "player-used-ability":
                ability_target = event.get("target", {})
                ability_id = ability_target.get("id")
                ability_name = (
                    ability_target.get("state", {}).get("name")
                    or ability_target.get("stateDelta", {}).get("name")
                    or ability_target.get("name")
                )
                ability_type = classify_ability(ability_id, ability_name, ability_type_map)

            if event_type == "round-started-freezetime":
                round_state = event.get("actor", {}).get("state", {})
                teams_state = round_state.get("teams") or []
                team_side_by_id = {
                    str(team.get("id")): team.get("side") for team in teams_state if team.get("id")
                }
                team_name_by_id = {
                    str(team.get("id")): team.get("name") for team in teams_state if team.get("id")
                }
                teams_delta = (
                    event.get("seriesStateDelta", {})
                    .get("games", [{}])[0]
                    .get("teams", [])
                )
                for team in teams_delta:
                    team_id = str(team.get("id")) if team.get("id") else None
                    if not team_id:
                        continue
                    team_loadout_value = team.get("loadoutValue")
                    team_net_worth = team.get("netWorth")
                    events_data.append((
                        f"{event_base_id}_team_{team_id}",
                        occurred_at,
                        series_id,
                        current_game_id,
                        current_round_id,
                        event_type,
                        None,
                        None,
                        team_name_by_id.get(team_id) or team_id_to_name.get(team_id),
                        None,
                        team_side_by_id.get(team_id),
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        event.get("action"),
                        None,
                        None,
                        None,
                        tournament_name,
                        tournament_year,
                        event.get("platformGameId"),
                        weapon_name,
                        weapon_type,
                        is_headshot,
                        False,  # is_wallbang
                        hit_location,
                        False,  # is_kill
                        False,  # is_death
                        False,  # is_assist
                        False,  # is_first_blood
                        is_plant,
                        is_defuse,
                        is_begin_defuse,
                        is_stop_defuse,
                        is_half_defuse,
                        is_defuse_complete,
                        is_bomb_exploded,
                        is_ability_use,
                        damage_dealt,
                        None,  # actor_loadout_value
                        None,  # actor_net_worth
                        team_loadout_value,
                        team_net_worth,
                        json.dumps(event.get("metadata", {})),
                    ))
                continue

            # For kill events, create TWO records: one for killer, one for victim
            if is_kill and actor_player_id and target_player_id:
                event_map_name = event.get("platformGameId")
                if not event_map_name and current_game_id and current_game_id in games_info:
                    event_map_name = games_info[current_game_id].get("map_name")
                # Attempt to infer weapon from round/game/series deltas
                weapon_kills = (
                    event.get("actor", {}).get("stateDelta", {}).get("round", {}).get("weaponKills")
                    or event.get("actor", {}).get("stateDelta", {}).get("game", {}).get("weaponKills")
                    or event.get("actor", {}).get("stateDelta", {}).get("series", {}).get("weaponKills")
                )
                if isinstance(weapon_kills, dict) and len(weapon_kills) == 1:
                    weapon_name = next(iter(weapon_kills.keys()))
                    weapon_type = weapon_type_lookup.get(weapon_name.lower() if weapon_name else None)

                is_first_blood = bool(
                    event.get("actor", {}).get("stateDelta", {}).get("round", {}).get("firstKill")
                )

                assist_entries = (
                    event.get("actor", {}).get("stateDelta", {}).get("round", {}).get("killAssistsReceivedFromPlayer")
                    or event.get("actor", {}).get("stateDelta", {}).get("game", {}).get("killAssistsReceivedFromPlayer")
                    or event.get("actor", {}).get("stateDelta", {}).get("series", {}).get("killAssistsReceivedFromPlayer")
                    or []
                )

                # Killer's event (actor)
                events_data.append((
                    event_base_id + "_killer",
                    occurred_at,
                    series_id,
                    current_game_id,
                    current_round_id,
                    event_type,
                    actor_player_id,
                    actor_player_name,
                    actor_team_name,
                    actor_agent_name,
                    actor_side,
                    target_player_id,
                    target_player_name,
                    target_team_name,
                    target_agent_name,
                    target_side,
                    actor_pos_x,
                    actor_pos_y,
                    target_pos_x,
                    target_pos_y,
                    event.get("action"),
                    ability_id,
                    ability_name,
                    ability_type,
                    tournament_name,
                    tournament_year,
                    event_map_name,
                    weapon_name,
                    weapon_type,
                    is_headshot,
                    False,  # is_wallbang
                    hit_location,
                    True,  # is_kill
                    False,  # is_death
                    False,  # is_assist
                    is_first_blood,
                    False,  # is_plant
                    False,  # is_defuse
                    False,  # is_begin_defuse
                    False,  # is_stop_defuse
                    False,  # is_half_defuse
                    False,  # is_defuse_complete
                    False,  # is_bomb_exploded
                    False,  # is_ability_use
                    damage_dealt,
                    actor_loadout_value,
                    actor_net_worth,
                    None,  # team_loadout_value
                    None,  # team_net_worth
                    json.dumps(event.get("metadata", {})),
                ))

                # Victim's event (target as actor)
                events_data.append((
                    event_base_id + "_victim",
                    occurred_at,
                    series_id,
                    current_game_id,
                    current_round_id,
                    event_type,
                    target_player_id,
                    target_player_name,
                    target_team_name,
                    target_agent_name,
                    target_side,
                    actor_player_id,
                    actor_player_name,
                    actor_team_name,
                    actor_agent_name,
                    actor_side,
                    target_pos_x,
                    target_pos_y,
                    actor_pos_x,
                    actor_pos_y,
                    "died",
                    ability_id,
                    ability_name,
                    ability_type,
                    tournament_name,
                    tournament_year,
                    event_map_name,
                    weapon_name,
                    weapon_type,
                    is_headshot,
                    False,  # is_wallbang
                    hit_location,
                    False,  # is_kill
                    True,  # is_death
                    False,  # is_assist
                    False,  # is_first_blood
                    False,  # is_plant
                    False,  # is_defuse
                    False,  # is_begin_defuse
                    False,  # is_stop_defuse
                    False,  # is_half_defuse
                    False,  # is_defuse_complete
                    False,  # is_bomb_exploded
                    False,  # is_ability_use
                    0,  # damage_dealt
                    target_loadout_value,
                    target_net_worth,
                    None,  # team_loadout_value
                    None,  # team_net_worth
                    json.dumps(event.get("metadata", {})),
                ))

                # Assist events (one per assisting player)
                for assist_entry in assist_entries:
                    assist_player_id = str(assist_entry.get("playerId")) if assist_entry else None
                    if not assist_player_id:
                        continue
                    assist_player_name = player_id_to_name.get(assist_player_id)
                    assist_team_id = player_id_to_team_id.get(assist_player_id)
                    assist_team_name = team_id_to_name.get(assist_team_id) if assist_team_id else None
                    assist_agent_name = (
                        player_id_to_agent_name_by_game
                        .get(current_game_id, {})
                        .get(assist_player_id)
                    ) or player_id_to_agent_name.get(assist_player_id)
                    events_data.append((
                        event_base_id + f"_assist_{assist_player_id}",
                        occurred_at,
                        series_id,
                        current_game_id,
                        current_round_id,
                        event_type,
                        assist_player_id,
                        assist_player_name,
                        assist_team_name,
                        assist_agent_name,
                        None,
                        target_player_id,
                        target_player_name,
                        target_team_name,
                        target_agent_name,
                        target_side,
                        None,
                        None,
                        target_pos_x,
                        target_pos_y,
                        "assisted",
                        ability_id,
                        ability_name,
                        ability_type,
                        tournament_name,
                        tournament_year,
                        event_map_name,
                        weapon_name,
                        weapon_type,
                        False,
                        False,  # is_wallbang
                        None,  # hit_location
                        False,  # is_kill
                        False,  # is_death
                        True,  # is_assist
                        False,  # is_first_blood
                        False,  # is_plant
                        False,  # is_defuse
                        False,  # is_begin_defuse
                        False,  # is_stop_defuse
                        False,  # is_half_defuse
                        False,  # is_defuse_complete
                        False,  # is_bomb_exploded
                        False,  # is_ability_use
                        0,  # damage_dealt
                        None,  # actor_loadout_value
                        None,  # actor_net_worth
                        None,  # team_loadout_value
                        None,  # team_net_worth
                        json.dumps(event.get("metadata", {})),
                    ))
                continue  # Skip the default event insertion below

            if event_type == "player-damaged-player" and actor_player_id:
                team_id = player_id_to_team_id.get(actor_player_id)
                damage_info = extract_damage_info(
                    event.get("seriesStateDelta"),
                    actor_player_id,
                    team_id,
                )
                damage_dealt = damage_info.get("damage_dealt") or 0
                weapon_name = damage_info.get("weapon_name")
                hit_location = damage_info.get("hit_location")
                is_headshot = bool(damage_info.get("is_headshot"))
                weapon_type = weapon_type_lookup.get(weapon_name.lower() if weapon_name else None)

            # Add to events batch (for non-kill events or events without player info)
            if weapon_name and weapon_type is None:
                weapon_type = weapon_type_lookup.get(weapon_name.lower())
            event_map_name = event.get("platformGameId")
            if not event_map_name and current_game_id and current_game_id in games_info:
                event_map_name = games_info[current_game_id].get("map_name")
            events_data.append((
                event_base_id,
                occurred_at,
                series_id,
                current_game_id,
                current_round_id,
                event_type,
                actor_player_id,
                actor_player_name,
                actor_team_name,
                actor_agent_name,
                actor_side,
                target_player_id,
                target_player_name,
                target_team_name,
                target_agent_name,
                target_side,
                actor_pos_x,
                actor_pos_y,
                target_pos_x,
                target_pos_y,
                event.get("action"),
                ability_id,
                ability_name,
                ability_type,
                tournament_name,
                tournament_year,
                event_map_name,  # map_name
                weapon_name,
                weapon_type,
                is_headshot,
                False,  # is_wallbang
                hit_location,
                is_kill,
                False,  # is_death (calculated in transformations)
                False,  # is_assist
                False,  # is_first_blood
                is_plant,
                is_defuse,
                is_begin_defuse,
                is_stop_defuse,
                is_half_defuse,
                is_defuse_complete,
                is_bomb_exploded,
                is_ability_use,
                damage_dealt,
                actor_loadout_value,
                actor_net_worth,
                team_loadout_value,
                team_net_worth,
                json.dumps(event.get("metadata", {})),
            ))

        # Finalize series and game metadata
        if not series_info["team1_name"] or not series_info["team2_name"]:
            team_names = list(team_id_to_name.values())
            if len(team_names) >= 2:
                series_info["team1_name"] = series_info["team1_name"] or team_names[0]
                series_info["team2_name"] = series_info["team2_name"] or team_names[1]

        rounds_per_game = {}
        for rnd in rounds_info.values():
            rounds_per_game[rnd["game_id"]] = rounds_per_game.get(rnd["game_id"], 0) + 1
        for game_id, total in rounds_per_game.items():
            if game_id in games_info and not games_info[game_id].get("total_rounds"):
                games_info[game_id]["total_rounds"] = total

        # Build insert lists
        series_data.append((
            series_info["series_id"],
            series_info["tournament_id"],
            series_info["tournament_name"],
            series_info["tournament_year"],
            series_info["tournament_region"],
            series_info["team1_name"],
            series_info["team2_name"],
            series_info["winning_team_name"],
            series_info["start_time"],
        ))

        for game in games_info.values():
            games_data.append((
                game["game_id"],
                game["series_id"],
                game["game_number"],
                game["map_name"],
                game["team1_name"],
                game["team2_name"],
                game["winning_team_name"],
                game["game_duration_seconds"],
                game["total_rounds"],
            ))

        for rnd in rounds_info.values():
            rounds_data.append((
                rnd["round_id"],
                rnd["series_id"],
                rnd["game_id"],
                rnd["round_number"],
                rnd["map_name"],
                rnd["started_at"],
                rnd["ended_at"],
                rnd["duration_seconds"],
                rnd["winning_team_name"],
                rnd["losing_team_name"],
                rnd["end_reason"],
                rnd["tournament_name"],
                rnd["tournament_year"],
            ))

        # Bulk insert all data
        if series_data:
            conn.executemany(
                """INSERT INTO series (
                    series_id, tournament_id, tournament_name, tournament_year,
                    tournament_region, team1_name, team2_name,
                    winning_team_name, start_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                series_data
            )

        if games_data:
            conn.executemany(
                """INSERT INTO games (
                    game_id, series_id, game_number, map_name,
                    team1_name, team2_name, winning_team_name,
                    game_duration_seconds, total_rounds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                games_data
            )

        if rounds_data:
            conn.executemany(
                """INSERT INTO rounds (
                    round_id, series_id, game_id, round_number,
                    map_name, started_at, ended_at, duration_seconds,
                    winning_team_name, losing_team_name, end_reason,
                    tournament_name, tournament_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rounds_data
            )

        if events_data:
            conn.executemany(
                """INSERT INTO base_events (
                    event_id, occurred_at, series_id, game_id, round_id,
                    event_type, actor_player_id, actor_player_name,
                    actor_team_name, actor_agent_name, actor_side,
                    target_player_id, target_player_name, target_team_name,
                    target_agent_name, target_side,
                    actor_pos_x, actor_pos_y, target_pos_x, target_pos_y,
                    action, ability_id, ability_name, ability_type,
                    tournament_name, tournament_year, map_name,
                    weapon_name, weapon_type, is_headshot, is_wallbang, hit_location,
                    is_kill, is_death, is_assist, is_first_blood,
                    is_plant, is_defuse, is_begin_defuse, is_stop_defuse,
                    is_half_defuse, is_defuse_complete, is_bomb_exploded,
                    is_ability_use, damage_dealt,
                    actor_loadout_value, actor_net_worth, team_loadout_value, team_net_worth,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                events_data
            )

        # Commit transaction
        conn.execute("COMMIT")

        print(f"  ✅ Loaded {len(events):,} events ({len(games_data)} games, {len(rounds_data)} rounds)")
        return True, len(events)

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"  ❌ Error loading: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
