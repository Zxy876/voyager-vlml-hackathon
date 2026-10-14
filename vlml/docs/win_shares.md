# Win Shares Methodology

Win Shares quantify individual player contributions to round wins using probability lift analysis.

## Overview

Based on **55,000+ rounds** of VCT Americas 2025 data, we calculate how much each in-game action increases the probability of winning a round. These probability lifts become weights in the Win Share formula.

## The Formula

```
Win Share =
    (First Bloods × 0.2303) +
    (Trade Kills × 0.0957) +
    (Survivals × 0.5946) +
    (Deaths Traded × 0.338) +
    (Multi-Kills × 0.3123) +
    (Plants × 0.1979) +
    (Defuses × 0.5121)

Win Share per Round = Total Win Share / Rounds Played
```

## Weight Derivation

Each weight equals the **probability lift**: win rate when event occurs minus win rate when it doesn't.

| Event | Win% With | Win% Without | Lift | Weight |
|-------|-----------|--------------|------|--------|
| Survival | 90.89% | 31.43% | 59.46% | **0.5946** |
| Defuse | 100.00% | 48.79% | 51.21% | **0.5121** |
| Death Traded | 56.99% | 23.19% | 33.80% | **0.3380** |
| Multi-Kill (2+) | 75.94% | 44.71% | 31.23% | **0.3123** |
| First Blood | 70.66% | 47.63% | 23.03% | **0.2303** |
| Plant | 68.36% | 48.57% | 19.79% | **0.1979** |
| Trade Kill | 58.18% | 48.61% | 9.57% | **0.0957** |

**Key insight**: Survival has the highest weight (0.5946) because staying alive correlates most strongly with winning rounds.

## Benchmarks

### Team Averages (VCT Americas 2025)

| Team | Avg WS/Round | Spread |
|------|-------------|--------|
| G2 Esports | 0.390 | 0.069 |
| Sentinels | 0.379 | 0.062 |
| NRG | 0.361 | 0.080 |
| Cloud9 | 0.360 | 0.048 |

A 3% Win Share gap (0.390 vs 0.360) correlates with ~7% difference in round win rate.

### Role Benchmarks

**Duelists**
- Opening Efficiency: > 0.53
- Survival Rate: > 0.28

**Initiators**
- Opening Efficiency: > 0.45
- Survival Rate: > 0.33

**Controllers**
- Opening Efficiency: > 0.50
- Survival Rate: > 0.35

**Sentinels**
- Opening Efficiency: > 0.50
- Survival Rate: > 0.32

## Supporting Analysis

### Kill Count Impact

| Kills | Win Rate | Delta |
|-------|----------|-------|
| 0 | 37.96% | — |
| 1 | 56.80% | +18.84% |
| 2 | 72.29% | +34.33% |
| 3 | 83.93% | +45.97% |
| 4 | 90.11% | +52.15% |
| 5 | 98.25% | +60.29% |

### Damage Thresholds

| Damage | Win Rate | Interpretation |
|--------|----------|----------------|
| 0-49 | 39.79% | Below contribution |
| 50-99 | 44.57% | Minimal impact |
| **100-149** | **55.38%** | Contribution threshold |
| 150-199 | 56.35% | Solid impact |
| 200-299 | 66.77% | High impact |
| 300+ | 76.35% | Carry performance |

### Team Survivors Impact

| Survivors | Win Rate |
|-----------|----------|
| 0 | 2.16% |
| 1 | 60.78% |
| 2 | 83.21% |
| 3 | 92.61% |
| 4 | 98.74% |
| 5 | 97.38% |

## Database Tables

Win Share data is stored in:
- `ref_win_probability_factors` - Weight reference table
- `agg_player_win_shares` - Pre-calculated win shares per player per game

## Example Queries

### Top Win Share Players

```sql
SELECT
    player_name,
    team_name,
    SUM(rounds_played) AS total_rounds,
    ROUND(AVG(win_share_per_round), 3) AS avg_ws_per_round
FROM agg_player_win_shares
GROUP BY player_name, team_name
HAVING SUM(rounds_played) > 200
ORDER BY avg_ws_per_round DESC
LIMIT 10;
```

### Team Efficiency Comparison

```sql
SELECT
    team_name,
    ROUND(AVG(win_share_per_round), 3) AS team_avg_ws,
    ROUND(AVG(opening_duel_efficiency), 3) AS avg_opening_eff,
    ROUND(AVG(survival_rate), 3) AS avg_survival
FROM agg_player_win_shares
GROUP BY team_name
ORDER BY team_avg_ws DESC;
```

### Win Share by Agent Role

```sql
SELECT
    agent_role,
    ROUND(AVG(fb_win_share / rounds_played), 4) AS fb_contrib,
    ROUND(AVG(survival_share / rounds_played), 4) AS survival_contrib,
    ROUND(AVG(win_share_per_round), 4) AS total_ws_per_round
FROM agg_player_win_shares
WHERE agent_role IS NOT NULL
GROUP BY agent_role
ORDER BY total_ws_per_round DESC;
```

## Key Takeaways

1. **Stay Alive** (59.5% lift) — Survival is the strongest predictor
2. **Win the First Fight** (23% lift) — But don't force bad duels
3. **Get Traded When You Die** (33.8% lift) — Team positioning matters
4. **Convert Multi-Kills** (31.2% lift) — The clutch factor

---

*Analysis based on 55,185 player-rounds from VCT Americas 2025*
