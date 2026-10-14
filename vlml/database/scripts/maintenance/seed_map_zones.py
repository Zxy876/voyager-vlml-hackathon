#!/usr/bin/env python3
"""Seed map_zones using observed plant and early-kill positions."""
from __future__ import annotations

import math
import random
from typing import Iterable, List, Sequence, Tuple

import duckdb

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    np = None


MAPS_WITH_THREE_SITES = {"haven", "lotus"}
DEFAULT_K = 2


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("percentile requires non-empty values")
    if np is not None:
        return float(np.quantile(np.array(values, dtype=float), q))
    vals = sorted(values)
    idx = int(round((len(vals) - 1) * q))
    return float(vals[max(0, min(idx, len(vals) - 1))])


def _kmeans(points: Sequence[Tuple[float, float]], k: int, max_iter: int = 20) -> List[List[Tuple[float, float]]]:
    if len(points) < k:
        return []
    centroids = random.sample(points, k)
    for _ in range(max_iter):
        clusters = [[] for _ in range(k)]
        for px, py in points:
            best_i = 0
            best_dist = float("inf")
            for i, (cx, cy) in enumerate(centroids):
                dist = (px - cx) ** 2 + (py - cy) ** 2
                if dist < best_dist:
                    best_dist = dist
                    best_i = i
            clusters[best_i].append((px, py))
        new_centroids = []
        for cluster in clusters:
            if not cluster:
                new_centroids.append(random.choice(points))
                continue
            xs = [p[0] for p in cluster]
            ys = [p[1] for p in cluster]
            new_centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))
        if all(math.isclose(c[0], n[0], rel_tol=1e-6) and math.isclose(c[1], n[1], rel_tol=1e-6)
               for c, n in zip(centroids, new_centroids)):
            return clusters
        centroids = new_centroids
    return clusters


def _fetch_positions(conn: duckdb.DuckDBPyConnection, sql: str) -> List[Tuple[float, float]]:
    rows = conn.execute(sql).fetchall()
    return [(float(x), float(y)) for x, y in rows if x is not None and y is not None]


def _insert_zone(conn: duckdb.DuckDBPyConnection, map_name: str, zone_name: str, zone_type: str,
                 min_x: float, max_x: float, min_y: float, max_y: float) -> None:
    conn.execute(
        """
        INSERT INTO map_zones (map_name, zone_name, zone_type, min_x, max_x, min_y, max_y)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [map_name, zone_name, zone_type, float(min_x), float(max_x), float(min_y), float(max_y)],
    )


def seed_map_zones(db_path: str) -> None:
    conn = duckdb.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS map_zones (
            map_name VARCHAR NOT NULL,
            zone_name VARCHAR NOT NULL,
            zone_type VARCHAR NOT NULL,
            min_x FLOAT NOT NULL,
            max_x FLOAT NOT NULL,
            min_y FLOAT NOT NULL,
            max_y FLOAT NOT NULL,
            PRIMARY KEY (map_name, zone_name)
        );
        """
    )

    maps = [row[0] for row in conn.execute(
        "SELECT DISTINCT map_name FROM base_events WHERE map_name IS NOT NULL"
    ).fetchall()]

    for map_name in maps:
        conn.execute("DELETE FROM map_zones WHERE map_name = ?", [map_name])

        default_points = _fetch_positions(
            conn,
            f"""
            SELECT e.actor_pos_x, e.actor_pos_y
            FROM base_events e
            JOIN rounds r ON r.round_id = e.round_id
            WHERE e.is_kill = TRUE
              AND e.map_name = '{map_name}'
              AND e.actor_pos_x IS NOT NULL
              AND e.actor_pos_y IS NOT NULL
              AND r.started_at IS NOT NULL
              AND EXTRACT(EPOCH FROM (e.occurred_at - r.started_at)) BETWEEN 0 AND 20
            """,
        )
        if len(default_points) < 50:
            default_points = _fetch_positions(
                conn,
                f"""
                SELECT actor_pos_x, actor_pos_y
                FROM base_events
                WHERE is_kill = TRUE
                  AND map_name = '{map_name}'
                  AND actor_pos_x IS NOT NULL
                  AND actor_pos_y IS NOT NULL
                """,
            )
        if default_points:
            xs = [p[0] for p in default_points]
            ys = [p[1] for p in default_points]
            min_x = _percentile(xs, 0.10)
            max_x = _percentile(xs, 0.90)
            min_y = _percentile(ys, 0.10)
            max_y = _percentile(ys, 0.90)
            _insert_zone(conn, map_name, "Default", "default", min_x, max_x, min_y, max_y)

        plant_points = _fetch_positions(
            conn,
            f"""
            SELECT actor_pos_x, actor_pos_y
            FROM base_events
            WHERE is_plant = TRUE
              AND map_name = '{map_name}'
              AND actor_pos_x IS NOT NULL
              AND actor_pos_y IS NOT NULL
            """,
        )
        if plant_points:
            map_key = str(map_name).lower()
            k = 3 if map_key in MAPS_WITH_THREE_SITES else DEFAULT_K
            if len(plant_points) >= k:
                clusters = _kmeans(plant_points, k=k)
                centroids = []
                for cluster in clusters:
                    xs = [p[0] for p in cluster]
                    ys = [p[1] for p in cluster]
                    centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))
                order = sorted(range(len(centroids)), key=lambda i: centroids[i][0])
                letters = ["A", "B", "C"]
                for idx, cluster_index in enumerate(order):
                    cluster = clusters[cluster_index]
                    if not cluster:
                        continue
                    xs = [p[0] for p in cluster]
                    ys = [p[1] for p in cluster]
                    _insert_zone(
                        conn,
                        map_name,
                        f"{letters[idx]} Site",
                        "site",
                        min(xs),
                        max(xs),
                        min(ys),
                        max(ys),
                    )

    conn.close()


if __name__ == "__main__":
    seed_map_zones("data/vlml_events.duckdb")
