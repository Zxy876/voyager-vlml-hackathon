#!/usr/bin/env python3
"""Validate data quality for all DuckDB tables."""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def table_exists(conn: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()[0]
        > 0
    )


def get_tables(conn: duckdb.DuckDBPyConnection, names: list[str] | None) -> list[str]:
    if names:
        return names
    return [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
    ]


def get_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[dict]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [
        {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": bool(r[3]),
            "dflt_value": r[4],
            "pk": bool(r[5]),
        }
        for r in rows
    ]


def run_generic_checks(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    issues: list[str] = []
    columns = get_columns(conn, table)
    col_names = [c["name"] for c in columns]

    total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if total_rows == 0:
        issues.append("empty table")

    pk_cols = [c["name"] for c in columns if c["pk"]]
    if pk_cols:
        pk_list = ", ".join(pk_cols)
        distinct_rows = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {pk_list} FROM {table})"
        ).fetchone()[0]
        if distinct_rows != total_rows:
            issues.append(f"duplicate primary keys on {pk_list}")
        for pk in pk_cols:
            nulls = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {pk} IS NULL"
            ).fetchone()[0]
            if nulls > 0:
                issues.append(f"{pk} has {nulls} NULLs")

    # Non-negative numeric checks for totals/denoms/time
    numeric_cols = [c["name"] for c in columns if "INT" in c["type"] or "FLOAT" in c["type"]]
    check_cols = [
        c
        for c in numeric_cols
        if c.endswith("_total")
        or c.endswith("_denom")
        or c.endswith("_sum")
        or c.endswith("_count")
        or c.endswith("_s")
        or "duration" in c
    ]
    for col in check_cols:
        neg = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} < 0"
        ).fetchone()[0]
        if neg > 0:
            issues.append(f"{col} has {neg} negative values")

    # total <= denom checks
    denom_cols = [c for c in col_names if c.endswith("_denom")]
    for denom in denom_cols:
        total_col = denom.replace("_denom", "_total")
        if total_col in col_names:
            bad = conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {total_col} > {denom}"
            ).fetchone()[0]
            if bad > 0:
                issues.append(f"{total_col} > {denom} in {bad} rows")

    # Ratio bounds for percentage/rate columns
    ratio_cols = [
        c for c in col_names if c.endswith("_rate") or c.endswith("_percentage")
    ]
    for col in ratio_cols:
        bad = conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {col} IS NOT NULL AND ({col} < 0 OR {col} > 1)"
        ).fetchone()[0]
        if bad > 0:
            issues.append(f"{col} out of [0,1] in {bad} rows")

    return issues


def run_table_specific_checks(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    issues: list[str] = []
    if table == "base_events":
        for col in ["event_id", "occurred_at", "series_id", "event_type"]:
            nulls = conn.execute(
                f"SELECT COUNT(*) FROM base_events WHERE {col} IS NULL"
            ).fetchone()[0]
            if nulls > 0:
                issues.append(f"{col} has {nulls} NULLs")
        bad_side = conn.execute(
            "SELECT COUNT(*) FROM base_events "
            "WHERE actor_side IS NOT NULL AND actor_side NOT IN ('attack','defense','attacker','defender','atk','def')"
        ).fetchone()[0]
        if bad_side > 0:
            issues.append(f"actor_side has {bad_side} invalid values")
        bad_target_side = conn.execute(
            "SELECT COUNT(*) FROM base_events "
            "WHERE target_side IS NOT NULL AND target_side NOT IN ('attack','defense','attacker','defender','atk','def')"
        ).fetchone()[0]
        if bad_target_side > 0:
            issues.append(f"target_side has {bad_target_side} invalid values")
        orphan_rounds = conn.execute(
            "SELECT COUNT(*) FROM base_events e "
            "LEFT JOIN rounds r ON e.round_id = r.round_id "
            "WHERE e.round_id IS NOT NULL AND r.round_id IS NULL"
        ).fetchone()[0]
        if orphan_rounds > 0:
            issues.append(f"{orphan_rounds} events reference missing rounds")
    if table == "rounds":
        bad = conn.execute(
            "SELECT COUNT(*) FROM rounds "
            "WHERE started_at IS NOT NULL AND ended_at IS NOT NULL "
            "AND ended_at < started_at"
        ).fetchone()[0]
        if bad > 0:
            issues.append(f"{bad} rounds ended before start")
    if table == "games":
        bad = conn.execute(
            "SELECT COUNT(*) FROM games WHERE total_rounds IS NOT NULL AND total_rounds < 0"
        ).fetchone()[0]
        if bad > 0:
            issues.append(f"{bad} games with negative total_rounds")
        round_mismatch = conn.execute(
            "SELECT COUNT(*) FROM games g "
            "LEFT JOIN rounds r ON g.game_id = r.game_id "
            "GROUP BY g.game_id, g.total_rounds "
            "HAVING g.total_rounds IS NOT NULL AND COUNT(r.round_id) != g.total_rounds"
        ).fetchall()
        if round_mismatch:
            issues.append(f"{len(round_mismatch)} games with total_rounds mismatch")
    if table == "agg_player_round_stats":
        bad_kills = conn.execute(
            "SELECT COUNT(*) FROM agg_player_round_stats WHERE kills < 0 OR kills > 5"
        ).fetchone()[0]
        if bad_kills > 0:
            issues.append(f"kills out of range in {bad_kills} rows")
        bad_deaths = conn.execute(
            "SELECT COUNT(*) FROM agg_player_round_stats WHERE deaths < 0 OR deaths > 1"
        ).fetchone()[0]
        if bad_deaths > 0:
            issues.append(f"deaths out of range in {bad_deaths} rows")
        bad_first = conn.execute(
            "SELECT COUNT(*) FROM agg_player_round_stats "
            "WHERE first_bloods > 1 OR first_deaths > 1"
        ).fetchone()[0]
        if bad_first > 0:
            issues.append(f"first blood/death flags >1 in {bad_first} rows")
        bad_headshot = conn.execute(
            "SELECT COUNT(*) FROM agg_player_round_stats "
            "WHERE headshot_hits_total > hits_total"
        ).fetchone()[0]
        if bad_headshot > 0:
            issues.append(f"headshot_hits_total > hits_total in {bad_headshot} rows")
    if table == "agg_team_round_stats":
        bad_team_kills = conn.execute(
            "SELECT COUNT(*) FROM agg_team_round_stats WHERE team_kills < 0 OR team_kills > 5"
        ).fetchone()[0]
        if bad_team_kills > 0:
            issues.append(f"team_kills out of range in {bad_team_kills} rows")
        bad_team_deaths = conn.execute(
            "SELECT COUNT(*) FROM agg_team_round_stats WHERE team_deaths < 0 OR team_deaths > 5"
        ).fetchone()[0]
        if bad_team_deaths > 0:
            issues.append(f"team_deaths out of range in {bad_team_deaths} rows")
        bad_scores = conn.execute(
            "SELECT COUNT(*) FROM agg_team_round_stats "
            "WHERE team_score_before < 0 OR enemy_score_before < 0"
        ).fetchone()[0]
        if bad_scores > 0:
            issues.append(f"negative round score in {bad_scores} rows")
        bad_timing = conn.execute(
            "SELECT COUNT(*) FROM agg_team_round_stats "
            "WHERE round_duration_s IS NOT NULL AND ("
            " (time_to_first_contact_s IS NOT NULL AND time_to_first_contact_s > round_duration_s) OR"
            " (time_to_first_kill_s IS NOT NULL AND time_to_first_kill_s > round_duration_s) OR"
            " (time_to_first_death_s IS NOT NULL AND time_to_first_death_s > round_duration_s) OR"
            " (time_to_plant_s IS NOT NULL AND time_to_plant_s > round_duration_s) OR"
            " (post_plant_duration_s IS NOT NULL AND post_plant_duration_s > round_duration_s)"
            ")"
        ).fetchone()[0]
        if bad_timing > 0:
            issues.append(f"timing exceeds round duration in {bad_timing} rows")
    if table == "agg_team_game_stats":
        bad_rounds = conn.execute(
            "SELECT COUNT(*) FROM agg_team_game_stats "
            "WHERE rounds_won < 0 OR rounds_lost < 0"
        ).fetchone()[0]
        if bad_rounds > 0:
            issues.append(f"rounds_won/rounds_lost negative in {bad_rounds} rows")
        mismatch = conn.execute(
            "SELECT COUNT(*) FROM agg_team_game_stats tgs "
            "JOIN games g ON tgs.game_id = g.game_id "
            "WHERE g.total_rounds IS NOT NULL "
            "AND (tgs.rounds_won + tgs.rounds_lost) != g.total_rounds"
        ).fetchone()[0]
        if mismatch > 0:
            issues.append(f"{mismatch} team games with rounds mismatch vs games.total_rounds")
    if table == "agg_player_game_stats":
        mismatch = conn.execute(
            "SELECT COUNT(*) FROM agg_player_game_stats pgs "
            "JOIN games g ON pgs.game_id = g.game_id "
            "WHERE g.total_rounds IS NOT NULL AND pgs.rounds_played > g.total_rounds"
        ).fetchone()[0]
        if mismatch > 0:
            issues.append(f"{mismatch} player games with rounds_played > games.total_rounds")
    if table == "agg_player_daily_stats":
        bad_maps = conn.execute(
            "SELECT COUNT(*) FROM agg_player_daily_stats "
            "WHERE maps_won > maps_played OR series_won > series_played"
        ).fetchone()[0]
        if bad_maps > 0:
            issues.append(f"maps_won/series_won exceed played in {bad_maps} rows")
    if table == "agg_tournament_stats":
        bad_rates = conn.execute(
            "SELECT COUNT(*) FROM agg_tournament_stats "
            "WHERE maps_won > maps_played OR series_won > series_played"
        ).fetchone()[0]
        if bad_rates > 0:
            issues.append(f"maps_won/series_won exceed played in {bad_rates} rows")
    return issues


def print_sample_queries(conn: duckdb.DuckDBPyConnection, table: str) -> None:
    sample_queries = {
        "agg_player_round_stats": [
            ("deaths out of range", "SELECT round_id, player_id, deaths FROM agg_player_round_stats WHERE deaths < 0 OR deaths > 1 LIMIT 10"),
            ("first blood/death flags >1", "SELECT round_id, player_id, first_bloods, first_deaths FROM agg_player_round_stats WHERE first_bloods > 1 OR first_deaths > 1 LIMIT 10"),
        ],
        "agg_team_round_stats": [
            ("team kills/deaths out of range", "SELECT round_id, team_name, team_kills, team_deaths FROM agg_team_round_stats WHERE team_kills > 5 OR team_deaths > 5 LIMIT 10"),
            ("timing exceeds duration", "SELECT round_id, team_name, round_duration_s, time_to_first_contact_s, time_to_first_kill_s, time_to_first_death_s, time_to_plant_s, post_plant_duration_s FROM agg_team_round_stats WHERE round_duration_s IS NOT NULL AND ((time_to_first_contact_s IS NOT NULL AND time_to_first_contact_s > round_duration_s) OR (time_to_first_kill_s IS NOT NULL AND time_to_first_kill_s > round_duration_s) OR (time_to_first_death_s IS NOT NULL AND time_to_first_death_s > round_duration_s) OR (time_to_plant_s IS NOT NULL AND time_to_plant_s > round_duration_s) OR (post_plant_duration_s IS NOT NULL AND post_plant_duration_s > round_duration_s)) LIMIT 10"),
        ],
        "agg_team_game_stats": [
            ("rounds mismatch vs games", "SELECT tgs.game_id, tgs.team_name, tgs.rounds_won, tgs.rounds_lost, g.total_rounds FROM agg_team_game_stats tgs JOIN games g ON tgs.game_id = g.game_id WHERE g.total_rounds IS NOT NULL AND (tgs.rounds_won + tgs.rounds_lost) != g.total_rounds LIMIT 10"),
            ("fd_percentage out of bounds", "SELECT game_id, team_name, fd_percentage FROM agg_team_game_stats WHERE fd_percentage IS NOT NULL AND (fd_percentage < 0 OR fd_percentage > 1) LIMIT 10"),
        ],
        "agg_player_game_stats": [
            ("rounds_played > games.total_rounds", "SELECT pgs.game_id, pgs.player_id, pgs.rounds_played, g.total_rounds FROM agg_player_game_stats pgs JOIN games g ON pgs.game_id = g.game_id WHERE g.total_rounds IS NOT NULL AND pgs.rounds_played > g.total_rounds LIMIT 10"),
            ("fd_percentage out of bounds", "SELECT game_id, player_id, fd_percentage FROM agg_player_game_stats WHERE fd_percentage IS NOT NULL AND (fd_percentage < 0 OR fd_percentage > 1) LIMIT 10"),
        ],
        "agg_player_daily_stats": [
            ("fd_percentage out of bounds", "SELECT date, player_id, fd_percentage FROM agg_player_daily_stats WHERE fd_percentage IS NOT NULL AND (fd_percentage < 0 OR fd_percentage > 1) LIMIT 10"),
        ],
        "agg_tournament_stats": [
            ("fd_percentage out of bounds", "SELECT tournament_id, entity_type, entity_id, fd_percentage FROM agg_tournament_stats WHERE fd_percentage IS NOT NULL AND (fd_percentage < 0 OR fd_percentage > 1) LIMIT 10"),
        ],
        "base_events": [
            ("invalid actor_side", "SELECT event_id, actor_side FROM base_events WHERE actor_side IS NOT NULL AND actor_side NOT IN ('attack','defense','attacker','defender','atk','def') LIMIT 10"),
            ("orphan rounds", "SELECT e.event_id, e.round_id FROM base_events e LEFT JOIN rounds r ON e.round_id = r.round_id WHERE e.round_id IS NOT NULL AND r.round_id IS NULL LIMIT 10"),
        ],
    }

    for label, query in sample_queries.get(table, []):
        rows = conn.execute(query).fetchall()
        if rows:
            print(f"   sample: {label}")
            for row in rows:
                print(f"     {row}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate DuckDB tables for data quality.")
    parser.add_argument(
        "--db-path",
        default="data/vlml_events.duckdb",
        help="Path to DuckDB file.",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        help="Optional list of tables to validate (default: all tables).",
    )
    parser.add_argument(
        "--samples",
        action="store_true",
        help="Print sample rows for failing checks.",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        return 1

    conn = duckdb.connect(str(db_path), read_only=True)
    tables = get_tables(conn, args.tables)

    overall_issues = 0
    for table in tables:
        if not table_exists(conn, table):
            print(f"❌ {table}: missing table")
            overall_issues += 1
            continue

        issues = []
        issues.extend(run_generic_checks(conn, table))
        issues.extend(run_table_specific_checks(conn, table))

        if issues:
            overall_issues += len(issues)
            print(f"⚠️  {table}: {len(issues)} issue(s)")
            for issue in issues:
                print(f"   - {issue}")
            if args.samples:
                print_sample_queries(conn, table)
        else:
            print(f"✅ {table}: ok")

    conn.close()
    return 0 if overall_issues == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
