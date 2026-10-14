# Valorant Coaching Report Standard v1.0

## Purpose

This document defines the standard framework for producing consistent, high-quality coaching reports using MCP esports data. All reports should follow this structure to ensure:

1. **Consistency** - Every report follows the same format
2. **Actionability** - Insights lead to concrete coaching actions
3. **Data Integrity** - All claims are backed by verifiable metrics
4. **Clarity** - Information is organized for quick consumption

---

## Report Types

### Type 1: Match Analysis Report
**Use Case:** Post-match breakdown for coaching review
**Scope:** Single series (1-5 maps)

### Type 2: Player Profile Report  
**Use Case:** Individual player assessment across multiple matches
**Scope:** 5+ series minimum for statistical significance

### Type 3: Scouting Report
**Use Case:** Pre-match opponent preparation
**Scope:** Opponent's last 5-10 series

### Type 4: Pattern Detection Report
**Use Case:** Identifying recurring issues or strengths
**Scope:** Full tournament or stage data

---

## Standard Sections (All Report Types)

### Section 1: Executive Summary
**Length:** 3-5 sentences maximum
**Content:**
- Match/analysis result
- Single most important finding
- Primary recommendation

**Example:**
> Cloud9 defeated NRG 2-1 in the Stage 2 Playoffs semifinal. The series was decided by opening duel differential (+3 FB for C9) and clutch conversion (C9 3/4 vs NRG 0/0). Priority VOD review: NRG's entry timing on Haven rounds 2, 4, 7, 8 where mada died first.

### Section 2: Key Metrics Dashboard
**Format:** Table with comparative data
**Required Metrics:**

| Metric Category | Metrics Required |
|-----------------|------------------|
| Opening Duels | FB, FD, FB%, FD%, Net FB Diff |
| Conversion | FB Conversion Rate, FD Salvage Rate |
| Impact | Multi-kills (2k, 3k, 4k, Ace), Clutches (attempts, wins, rate) |
| Consistency | KAST%, ADR, K/D |
| Economy | Pistol Win%, Eco Win%, Thrifty Count |

### Section 3: Round-by-Round Analysis
**Format:** Condensed timeline with key events
**Required Data Points per Round:**
- Round number
- Opening duel winner/loser
- Round winner
- High-impact events (3k+, clutch, ace)
- Coaching flag (if applicable)

**Coaching Flags:**
- 🔴 **REVIEW** - Something went wrong that needs VOD review
- 🟡 **PATTERN** - This round fits a recurring pattern
- 🟢 **REPLICATE** - Successful play to study and repeat
- ⚪ **STANDARD** - Normal round, no special attention needed

### Section 4: Issue Identification
**Format:** Numbered list with evidence
**Structure per Issue:**
```
Issue #[N]: [Short Title]
- Description: [1-2 sentences]
- Evidence: [Specific rounds/stats]
- Frequency: [X of Y rounds, or X%]
- Impact: [Rounds lost, damage to economy, etc.]
- Root Cause Hypothesis: [Team structure, individual, timing, etc.]
```

### Section 5: VOD Review Priority Queue
**Format:** Ranked list of rounds to review
**Include:**
- Round number and map
- Reason for review
- Specific player(s) to focus on
- Question to answer from VOD

**Example:**
```
1. Haven R8 - C9 won FB but lost round. Watch: Trade timing after v1c's opener
2. Haven R14 - skuba 4K but NRG lost. Watch: Post-plant positioning
3. Haven R22 - mada ACE. Watch: Entry path for replication
```

### Section 6: Action Plan
**Format:** Categorized recommendations
**Categories:**
- **Immediate** (this week's practice)
- **Short-term** (next 2-3 weeks)
- **Long-term** (strategic adjustments)

---

## Data Quality Standards

### Minimum Sample Sizes

| Analysis Type | Minimum Rounds | Minimum Series |
|---------------|----------------|----------------|
| Single Match Report | All rounds in match | 1 |
| Player Profile | 100+ rounds | 5+ |
| Team Patterns | 200+ rounds | 10+ |
| Agent-Specific | 50+ rounds on agent | 3+ |
| Map-Specific | 75+ rounds on map | 5+ |

### Statistical Confidence Labels

- **Strong Signal** (n ≥ 100): "Pattern is..."
- **Moderate Signal** (50 ≤ n < 100): "Trend suggests..."
- **Weak Signal** (20 ≤ n < 50): "Early indication that..."
- **Insufficient Data** (n < 20): "Limited data shows... (not statistically significant)"

### Required Context for All Percentages

Always provide numerator and denominator:
- ✅ "75% clutch win rate (3/4)"
- ❌ "75% clutch win rate"

---

## Metric Calculation Standards

### Opening Duel Metrics

```sql
-- First Blood Rate (player)
FB% = first_bloods / rounds_played

-- First Death Rate (player)  
FD% = first_deaths / rounds_played

-- Opening Duel Rating (player)
OD_Rating = (first_bloods - first_deaths) / rounds_played

-- FB Conversion Rate (team)
FB_Conv% = rounds_won_with_fb / rounds_with_fb

-- FD Salvage Rate (team)
FD_Salv% = rounds_won_despite_fd / rounds_with_fd
```

### Impact Metrics

```sql
-- Multi-kill Round Rate
MK_Rate = rounds_with_2k_or_more / rounds_played

-- Clutch Efficiency
Clutch_Eff = clutches_won / clutch_attempts

-- Clutch Difficulty Score
Clutch_Diff = AVG(opponents_faced_in_clutch)

-- True Impact Score (custom)
Impact = (kills + 0.5*assists + 2*clutches_won + 1.5*first_bloods) / rounds_played
```

### Consistency Metrics

```sql
-- KAST Rate
KAST% = rounds_with_kill_assist_survive_or_trade / total_rounds

-- Average Damage per Round
ADR = total_damage / rounds_played

-- Kill Participation
KP% = (kills + assists) / team_total_kills
```

---

## Visualization Standards

### Tables

**Column Order (left to right):**
1. Identifier (Player, Round, Map)
2. Primary metrics
3. Secondary metrics
4. Comparative/benchmark data
5. Flags/notes

**Formatting:**
- Bold headers
- Right-align numbers
- Include totals/averages where applicable
- Use consistent decimal places (1 for rates, 0 for counts)

### Round Timeline Format

```
R1  | C9 FB (neT→skuba) | C9 Win | OXY 4K 🟢
R2  | C9 FB (Xeppaa→mada) | C9 Win | Standard ⚪
R3  | C9 FB (v1c→brawk) | C9 Win | Standard ⚪
...
R8  | C9 FB (v1c→mada) | NRG Win | Lost 5v4 🔴
```

---

## Language Standards

### Prohibited Phrases
- "It seems like..." (be definitive or state uncertainty explicitly)
- "They should probably..." (be specific about recommendations)
- "The player was bad..." (focus on actions, not judgments)
- "Obviously..." (nothing is obvious; state the evidence)

### Required Phrasing

| Instead of... | Use... |
|---------------|--------|
| "bad performance" | "below-average output (X ADR vs Y team avg)" |
| "they kept dying" | "first death in X of Y rounds (Z%)" |
| "they're good at clutching" | "converted X of Y clutches (Z%)" |
| "their economy was bad" | "force-bought in X rounds following eco" |

### Framing Issues as Systems Problems

Always frame individual issues in team context:
- ❌ "mada keeps dying first"
- ✅ "mada's entry died first in 5/12 rounds - check if util support is arriving on time or if timing is predictable"

---

## Report Templates

### Match Analysis Report Template

```markdown
# [Team A] vs [Team B] | [Map(s)] | [Tournament]
## [Stage/Round] | [Date]

---

## Executive Summary
[3-5 sentences]

---

## Key Metrics

| Metric | [Team A] | [Team B] | Advantage |
|--------|----------|----------|-----------|
| First Bloods | X | Y | [Team] |
| First Deaths | X | Y | [Team] |
| FB Conversion | X% | Y% | [Team] |
| FD Salvage | X% | Y% | [Team] |
| Clutches | X/Y (Z%) | X/Y (Z%) | [Team] |
| Multi-kills (3k+) | X | Y | [Team] |

---

## Player Performance

| Player | Agent | K | D | FB | FD | ADR | KAST% | Impact |
|--------|-------|---|---|----|----|-----|-------|--------|
| ... | ... | ... | ... | ... | ... | ... | ... | ... |

---

## Round-by-Round Timeline

[Round timeline with flags]

---

## Issues Identified

### Issue #1: [Title]
[Structured issue format]

### Issue #2: [Title]
[Structured issue format]

---

## VOD Review Priority

1. [Round] - [Reason] - Watch: [Focus]
2. [Round] - [Reason] - Watch: [Focus]
3. [Round] - [Reason] - Watch: [Focus]

---

## Action Plan

### Immediate (This Week)
- [ ] [Action item]

### Short-Term (2-3 Weeks)
- [ ] [Action item]

### Long-Term (Strategic)
- [ ] [Action item]
```

---

## Quality Checklist

Before finalizing any report, verify:

### Data Integrity
- [ ] All percentages include numerator/denominator
- [ ] Sample sizes are disclosed
- [ ] Metrics match data dictionary definitions
- [ ] No invented or estimated numbers

### Structure
- [ ] Executive summary is ≤5 sentences
- [ ] All required sections are present
- [ ] Tables are consistently formatted
- [ ] Round timeline includes all rounds

### Actionability
- [ ] Each issue has a root cause hypothesis
- [ ] VOD review queue has specific questions
- [ ] Action items are concrete and timebound
- [ ] Recommendations tie back to evidence

### Language
- [ ] No prohibited phrases
- [ ] Issues framed as systems, not individuals
- [ ] Uncertainty is explicitly labeled
- [ ] All claims have supporting data

---

## Appendix A: SQL Query Specifications

These are the **exact queries** an MCP tool must run to generate each report section. All queries use the GRID API schema (see DATA_DICTIONARY.md).

---

### A1. Match Report Queries

#### A1.1 Match Metadata
```sql
-- Get series info
SELECT 
    s.series_id,
    s.tournament_name,
    s.tournament_year,
    s.team1_name,
    s.team2_name,
    s.winning_team_name,
    s.start_time
FROM series s
WHERE s.series_id = '{series_id}'
```

```sql
-- Get game info (for multi-map series)
SELECT 
    g.game_id,
    g.game_number,
    g.map_name,
    g.winning_team_name,
    g.total_rounds
FROM games g
WHERE g.series_id = '{series_id}'
ORDER BY g.game_number
```

#### A1.2 Team Key Metrics
```sql
-- Team-level metrics for Key Metrics Dashboard
SELECT 
    t.team_name,
    COUNT(DISTINCT t.round_id) as rounds_played,
    SUM(t.first_bloods) as total_fb,
    SUM(t.first_deaths) as total_fd,
    SUM(CASE WHEN t.entry_kill THEN 1 ELSE 0 END) as rounds_with_fb,
    SUM(CASE WHEN t.entry_kill AND t.round_won THEN 1 ELSE 0 END) as fb_converted,
    SUM(CASE WHEN t.entry_death THEN 1 ELSE 0 END) as rounds_with_fd,
    SUM(CASE WHEN t.entry_death AND t.round_won THEN 1 ELSE 0 END) as fd_salvaged,
    SUM(t.team_kills) as total_kills,
    SUM(t.team_deaths) as total_deaths,
    ROUND(SUM(t.team_damage_dealt) / COUNT(DISTINCT t.round_id), 1) as team_adr
FROM agg_team_round_stats t
WHERE t.round_id LIKE '{series_id}%'
GROUP BY t.team_name
```

#### A1.3 Player Performance Table
```sql
-- Player stats for Player Performance section
SELECT 
    p.player_name,
    p.team_name,
    p.agent_name,
    COUNT(*) as rounds_played,
    SUM(p.kills) as kills,
    SUM(p.deaths) as deaths,
    SUM(p.first_bloods) as fb,
    SUM(p.first_deaths) as fd,
    ROUND(SUM(p.damage_dealt) / COUNT(*), 1) as adr,
    ROUND(100.0 * SUM(CASE WHEN p.kast THEN 1 ELSE 0 END) / COUNT(*), 1) as kast_pct,
    SUM(CASE WHEN p.is_clutch THEN 1 ELSE 0 END) as clutch_attempts,
    SUM(CASE WHEN p.clutch_won THEN 1 ELSE 0 END) as clutches_won,
    SUM(CASE WHEN p.is_double_kill THEN 1 ELSE 0 END) as double_kills,
    SUM(CASE WHEN p.is_triple_kill THEN 1 ELSE 0 END) as triple_kills,
    SUM(CASE WHEN p.is_quad_kill THEN 1 ELSE 0 END) as quad_kills,
    SUM(CASE WHEN p.is_ace THEN 1 ELSE 0 END) as aces
FROM agg_player_round_stats p
WHERE p.round_id LIKE '{series_id}_game_{game_num}%'
GROUP BY p.player_name, p.team_name, p.agent_name
ORDER BY p.team_name, kills DESC
```

#### A1.4 Round-by-Round Timeline
```sql
-- Round timeline with opening duels and highlights
SELECT 
    p.round_number,
    MAX(CASE WHEN p.is_opening_kill THEN p.player_name END) as fb_player,
    MAX(CASE WHEN p.is_opening_kill THEN p.team_name END) as fb_team,
    MAX(CASE WHEN p.is_opening_death THEN p.player_name END) as fd_player,
    MAX(CASE WHEN p.round_won THEN p.team_name END) as round_winner,
    MAX(CASE WHEN p.is_ace THEN p.player_name || ' ACE' 
             WHEN p.is_quad_kill THEN p.player_name || ' 4K'
             WHEN p.is_triple_kill THEN p.player_name || ' 3K' 
             END) as highlight,
    MAX(CASE WHEN p.clutch_won THEN p.player_name || ' clutch' END) as clutch
FROM agg_player_round_stats p
WHERE p.round_id LIKE '{series_id}_game_{game_num}%'
GROUP BY p.round_number
ORDER BY p.round_number
```

#### A1.5 Clutch Performance
```sql
-- Clutch details for both teams
SELECT 
    p.player_name,
    p.team_name,
    p.round_number,
    p.clutch_opponents,
    p.clutch_won,
    p.clutch_difficulty_score
FROM agg_player_round_stats p
WHERE p.round_id LIKE '{series_id}%'
  AND p.is_clutch = true
ORDER BY p.round_number
```

---

### A2. Issue Detection Queries

#### A2.1 Opening Death Rate by Player
```sql
-- Detect players with high opening death rate
SELECT 
    p.player_name,
    p.team_name,
    p.agent_name,
    COUNT(*) as rounds_played,
    SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) as opening_deaths,
    ROUND(100.0 * SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) / COUNT(*), 1) as od_rate,
    -- List specific rounds for evidence
    STRING_AGG(CASE WHEN p.is_opening_death THEN 'R' || p.round_number END, ', ') as od_rounds
FROM agg_player_round_stats p
WHERE p.round_id LIKE '{series_id}%'
GROUP BY p.player_name, p.team_name, p.agent_name
HAVING SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) >= 3
ORDER BY od_rate DESC
```

**Issue Threshold:** Flag if `od_rate > 15%` for non-duelists, `od_rate > 20%` for duelists.

#### A2.2 FB Conversion Failures
```sql
-- Rounds where team won FB but lost round
SELECT 
    t.round_number,
    t.team_name,
    MAX(CASE WHEN p.is_opening_kill THEN p.player_name END) as fb_player,
    MAX(CASE WHEN p.is_opening_death THEN p.player_name END) as fd_player
FROM agg_team_round_stats t
JOIN agg_player_round_stats p ON t.round_id = p.round_id AND t.team_name = p.team_name
WHERE t.round_id LIKE '{series_id}%'
  AND t.entry_kill = true
  AND t.round_won = false
GROUP BY t.round_number, t.team_name
ORDER BY t.round_number
```

**Issue Threshold:** Flag if team loses 3+ rounds despite winning FB.

#### A2.3 Low KAST Players
```sql
-- Players with below-average KAST
SELECT 
    p.player_name,
    p.team_name,
    COUNT(*) as rounds,
    ROUND(100.0 * SUM(CASE WHEN p.kast THEN 1 ELSE 0 END) / COUNT(*), 1) as kast_pct,
    ROUND(SUM(p.damage_dealt) / COUNT(*), 1) as adr
FROM agg_player_round_stats p
WHERE p.round_id LIKE '{series_id}%'
GROUP BY p.player_name, p.team_name
HAVING ROUND(100.0 * SUM(CASE WHEN p.kast THEN 1 ELSE 0 END) / COUNT(*), 1) < 50
```

**Issue Threshold:** Flag if `kast_pct < 50%`.

#### A2.4 Untraded Deaths
```sql
-- Players dying without trade support
SELECT 
    p.player_name,
    p.team_name,
    SUM(p.deaths) as total_deaths,
    SUM(CASE WHEN p.is_untraded_death THEN 1 ELSE 0 END) as untraded_deaths,
    ROUND(100.0 * SUM(CASE WHEN p.is_untraded_death THEN 1 ELSE 0 END) / 
          NULLIF(SUM(p.deaths), 0), 1) as untraded_pct
FROM agg_player_round_stats p
WHERE p.round_id LIKE '{series_id}%'
GROUP BY p.player_name, p.team_name
HAVING SUM(p.deaths) >= 5
ORDER BY untraded_pct DESC
```

**Issue Threshold:** Flag if `untraded_pct > 40%`.

---

### A3. VOD Priority Queue Queries

#### A3.1 High-Impact Rounds to Review
```sql
-- Rounds worth VOD review, ranked by importance
WITH round_events AS (
    SELECT 
        p.round_number,
        MAX(p.team_name) FILTER (WHERE p.round_won) as winner,
        MAX(p.team_name) FILTER (WHERE p.is_opening_kill) as fb_team,
        MAX(p.team_name) FILTER (WHERE p.is_opening_death) as fd_team,
        MAX(p.player_name) FILTER (WHERE p.is_ace) as ace_player,
        MAX(p.player_name) FILTER (WHERE p.is_quad_kill) as quad_player,
        MAX(p.player_name) FILTER (WHERE p.clutch_won) as clutch_winner,
        SUM(CASE WHEN p.is_triple_kill OR p.is_quad_kill OR p.is_ace THEN 1 ELSE 0 END) as big_plays
    FROM agg_player_round_stats p
    WHERE p.round_id LIKE '{series_id}_game_{game_num}%'
    GROUP BY p.round_number
)
SELECT 
    round_number,
    winner,
    fb_team,
    CASE 
        WHEN fb_team IS NOT NULL AND fb_team != winner THEN 'FB_CONVERSION_FAIL'
        WHEN fd_team IS NOT NULL AND fd_team = winner THEN 'FD_SALVAGE'
        WHEN ace_player IS NOT NULL THEN 'ACE'
        WHEN clutch_winner IS NOT NULL THEN 'CLUTCH'
        WHEN quad_player IS NOT NULL THEN '4K'
        WHEN big_plays >= 2 THEN 'MULTI_HIGHLIGHT'
        ELSE 'STANDARD'
    END as review_reason,
    CASE 
        WHEN fb_team IS NOT NULL AND fb_team != winner THEN 1  -- Highest priority
        WHEN clutch_winner IS NOT NULL THEN 2
        WHEN ace_player IS NOT NULL THEN 3
        WHEN fd_team IS NOT NULL AND fd_team = winner THEN 4
        ELSE 5
    END as priority_rank
FROM round_events
WHERE fb_team != winner 
   OR (fd_team = winner)
   OR ace_player IS NOT NULL 
   OR clutch_winner IS NOT NULL
   OR quad_player IS NOT NULL
ORDER BY priority_rank, round_number
```

---

### A4. Player Profile Queries

#### A4.1 Career Stats Across Multiple Series
```sql
-- Player aggregate stats across N series
SELECT 
    p.player_name,
    p.team_name,
    COUNT(DISTINCT p.round_id) as total_rounds,
    COUNT(DISTINCT LEFT(p.round_id, POSITION('_game' IN p.round_id) - 1)) as series_played,
    SUM(p.kills) as total_kills,
    SUM(p.deaths) as total_deaths,
    ROUND(1.0 * SUM(p.kills) / NULLIF(SUM(p.deaths), 0), 2) as kd_ratio,
    ROUND(SUM(p.damage_dealt) / COUNT(*), 1) as adr,
    SUM(p.first_bloods) as total_fb,
    SUM(p.first_deaths) as total_fd,
    ROUND(100.0 * SUM(p.first_bloods) / COUNT(*), 1) as fb_pct,
    ROUND(100.0 * SUM(p.first_deaths) / COUNT(*), 1) as fd_pct,
    ROUND(100.0 * SUM(CASE WHEN p.kast THEN 1 ELSE 0 END) / COUNT(*), 1) as kast_pct
FROM agg_player_round_stats p
WHERE p.player_name = '{player_name}'
GROUP BY p.player_name, p.team_name
```

#### A4.2 Agent-Specific Performance
```sql
-- Performance breakdown by agent
SELECT 
    p.agent_name,
    COUNT(*) as rounds,
    ROUND(1.0 * SUM(p.kills) / NULLIF(SUM(p.deaths), 0), 2) as kd,
    ROUND(SUM(p.damage_dealt) / COUNT(*), 1) as adr,
    ROUND(100.0 * SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) / COUNT(*), 1) as od_rate,
    ROUND(100.0 * SUM(CASE WHEN p.kast THEN 1 ELSE 0 END) / COUNT(*), 1) as kast_pct
FROM agg_player_round_stats p
WHERE p.player_name = '{player_name}'
GROUP BY p.agent_name
HAVING COUNT(*) >= 20
ORDER BY rounds DESC
```

#### A4.3 Map-Specific Performance
```sql
-- Performance breakdown by map
SELECT 
    p.map_name,
    COUNT(*) as rounds,
    ROUND(1.0 * SUM(p.kills) / NULLIF(SUM(p.deaths), 0), 2) as kd,
    ROUND(SUM(p.damage_dealt) / COUNT(*), 1) as adr,
    ROUND(100.0 * SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) / COUNT(*), 1) as od_rate
FROM agg_player_round_stats p
WHERE p.player_name = '{player_name}'
GROUP BY p.map_name
HAVING COUNT(*) >= 20
ORDER BY rounds DESC
```

---

### A5. Scouting Report Queries

#### A5.1 Team Recent Form
```sql
-- Team's last N series results
SELECT 
    s.series_id,
    s.start_time,
    s.team1_name,
    s.team2_name,
    s.winning_team_name,
    CASE WHEN s.winning_team_name = '{team_name}' THEN 'W' ELSE 'L' END as result
FROM series s
WHERE s.team1_name = '{team_name}' OR s.team2_name = '{team_name}'
ORDER BY s.start_time DESC
LIMIT {num_series}
```

#### A5.2 Team Map Win Rates
```sql
-- Win rate by map
SELECT 
    g.map_name,
    COUNT(*) as games_played,
    SUM(CASE WHEN g.winning_team_name = '{team_name}' THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * SUM(CASE WHEN g.winning_team_name = '{team_name}' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate
FROM games g
JOIN series s ON g.series_id = s.series_id
WHERE s.team1_name = '{team_name}' OR s.team2_name = '{team_name}'
GROUP BY g.map_name
HAVING COUNT(*) >= 3
ORDER BY win_rate DESC
```

#### A5.3 Star Player Identification
```sql
-- Top performers on team
SELECT 
    p.player_name,
    COUNT(DISTINCT p.round_id) as rounds,
    ROUND(1.0 * SUM(p.kills) / NULLIF(SUM(p.deaths), 0), 2) as kd,
    ROUND(SUM(p.damage_dealt) / COUNT(*), 1) as adr,
    SUM(p.first_bloods) as fb,
    SUM(CASE WHEN p.clutch_won THEN 1 ELSE 0 END) as clutches
FROM agg_player_round_stats p
WHERE p.team_name = '{team_name}'
GROUP BY p.player_name
ORDER BY adr DESC
```

#### A5.4 Team Tendencies
```sql
-- Attack vs Defense performance
SELECT 
    t.side,
    COUNT(*) as rounds,
    SUM(CASE WHEN t.round_won THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * SUM(CASE WHEN t.round_won THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
    ROUND(100.0 * SUM(CASE WHEN t.entry_kill THEN 1 ELSE 0 END) / COUNT(*), 1) as fb_rate
FROM agg_team_round_stats t
WHERE t.team_name = '{team_name}'
GROUP BY t.side
```

---

### A6. Pattern Detection Queries

#### A6.1 Recurring Opening Death Pattern
```sql
-- Player opening deaths across all series
SELECT 
    p.player_name,
    p.agent_name,
    p.map_name,
    COUNT(*) as rounds,
    SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) as opening_deaths,
    ROUND(100.0 * SUM(CASE WHEN p.is_opening_death THEN 1 ELSE 0 END) / COUNT(*), 1) as od_rate
FROM agg_player_round_stats p
WHERE p.team_name = '{team_name}'
GROUP BY p.player_name, p.agent_name, p.map_name
HAVING COUNT(*) >= 30
ORDER BY od_rate DESC
```

#### A6.2 Pistol Round Performance
```sql
-- Pistol round analysis
SELECT 
    t.team_name,
    t.map_name,
    SUM(CASE WHEN t.round_number IN (1, 13) THEN 1 ELSE 0 END) as pistol_rounds,
    SUM(CASE WHEN t.round_number IN (1, 13) AND t.round_won THEN 1 ELSE 0 END) as pistol_wins,
    ROUND(100.0 * SUM(CASE WHEN t.round_number IN (1, 13) AND t.round_won THEN 1 ELSE 0 END) / 
          NULLIF(SUM(CASE WHEN t.round_number IN (1, 13) THEN 1 ELSE 0 END), 0), 1) as pistol_win_rate
FROM agg_team_round_stats t
WHERE t.team_name = '{team_name}'
GROUP BY t.team_name, t.map_name
HAVING SUM(CASE WHEN t.round_number IN (1, 13) THEN 1 ELSE 0 END) >= 4
ORDER BY pistol_win_rate DESC
```

#### A6.3 Clutch Patterns
```sql
-- Team clutch performance over time
SELECT 
    p.player_name,
    COUNT(*) as clutch_situations,
    SUM(CASE WHEN p.clutch_won THEN 1 ELSE 0 END) as clutches_won,
    ROUND(100.0 * SUM(CASE WHEN p.clutch_won THEN 1 ELSE 0 END) / COUNT(*), 1) as clutch_rate,
    ROUND(AVG(p.clutch_opponents), 2) as avg_difficulty
FROM agg_player_round_stats p
WHERE p.team_name = '{team_name}'
  AND p.is_clutch = true
GROUP BY p.player_name
HAVING COUNT(*) >= 5
ORDER BY clutch_rate DESC
```

---

## Appendix B: Issue Detection Thresholds

These thresholds determine when a metric becomes a **flagged issue** requiring coaching attention.

### B1. Individual Player Thresholds

| Metric | Role | Warning | Critical | Notes |
|--------|------|---------|----------|-------|
| Opening Death Rate | Duelist | >18% | >22% | Duelists expected to take fights |
| Opening Death Rate | Non-Duelist | >12% | >15% | Support players dying first is structural |
| KAST | All | <55% | <50% | Below 50% = not contributing |
| ADR | Duelist | <100 | <85 | Duelists need damage output |
| ADR | Non-Duelist | <80 | <65 | Support still needs presence |
| Untraded Death % | All | >35% | >45% | Team not trading properly |
| Clutch Win Rate | All (n≥5) | <40% | <30% | Sample size matters |

### B2. Team-Level Thresholds

| Metric | Warning | Critical | Notes |
|--------|---------|----------|-------|
| FB Conversion Rate | <70% | <60% | Not capitalizing on advantages |
| FD Salvage Rate | <25% | <20% | Collapsing after disadvantage |
| Pistol Win Rate | <45% | <35% | Economy snowball issues |
| Trade Success Rate | <60% | <50% | Spacing/timing problems |
| Post-Plant Win Rate (ATK) | <55% | <45% | Not holding post-plant |

### B3. Pattern Detection Thresholds

| Pattern | Trigger Condition |
|---------|-------------------|
| Entry Predictability | Same player dies first 3+ times in a half |
| FB Conversion Fail Streak | Lose 3+ rounds despite winning FB |
| Clutch Dependency | >25% of round wins come from clutches |
| Map Weakness | Win rate <35% on a map (n≥5 games) |
| Agent Discomfort | >5% worse KD on specific agent vs average |

### B4. VOD Review Priority Scoring

Rounds are auto-prioritized for VOD review based on:

| Event | Priority Score | Reason |
|-------|----------------|--------|
| Won FB, Lost Round | +10 | Should have converted |
| Lost FB, Won Round | +7 | How did we salvage? |
| Clutch Won | +6 | Replicable play |
| Clutch Lost | +5 | What went wrong? |
| Ace | +5 | Study the setup |
| 4K+ | +4 | High impact play |
| Player died first 3+ times | +8 | Pattern issue |
| Overtime round | +3 | High pressure decision |

---

## Appendix C: Report Output Format

### C1. File Naming Convention
```
{report_type}_{team_a}_vs_{team_b}_{map}_{date}.md

Examples:
- match_Cloud9_vs_NRG_Haven_2025-08-29.md
- player_profile_mada_2025-08.md
- scouting_G2_Esports_2025-08.md
```

### C2. Required Sections Checklist

#### Match Report
- [ ] Executive Summary (≤5 sentences)
- [ ] Key Metrics Table (both teams)
- [ ] Player Performance Table (all 10 players)
- [ ] Round-by-Round Timeline (with flags)
- [ ] Issues Identified (≥1 per team)
- [ ] VOD Review Priority Queue (≥3 rounds)
- [ ] Action Plan (Immediate/Short/Long term)

#### Player Profile
- [ ] Executive Summary
- [ ] Career Stats Summary
- [ ] Agent Performance Breakdown
- [ ] Map Performance Breakdown
- [ ] Trend Analysis (improving/declining)
- [ ] Identified Patterns
- [ ] Development Recommendations

#### Scouting Report
- [ ] Executive Summary
- [ ] Recent Form (last N series)
- [ ] Map Pool Analysis
- [ ] Star Players to Watch
- [ ] Tendencies (ATK/DEF splits)
- [ ] Exploitable Patterns
- [ ] Recommended Counter-Strats

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-01-03 | Initial standard created |
| 1.1 | 2026-01-03 | Added SQL specifications (Appendix A) |
| 1.2 | 2026-01-03 | Added thresholds & format specs (Appendix B, C) |
| 1.3 | 2025-01 | Added v3.0 coaching context sections (economy, situations, patterns, benchmarks) |

---

## Report v3.0: Enhanced Coaching Context

The `match_analysis_report` now includes additional sections designed for LLM reasoning:

| Section | Purpose | See Also |
|---------|---------|----------|
| `economy_context` | Round-by-round economy progression | [Prompting Guide](docs/prompting_guide.md) |
| `round_situations` | Rich per-round situation data | [Architecture](docs/architecture.md) |
| `attack_patterns` | Execute timing and site selection | |
| `benchmarks` | Historical baseline rates | |

These sections enable the LLM to:
- Detect economy cascade patterns
- Perform "what if" scenario analysis
- Identify attack predictability
- Contextualize performance against baselines

For detailed prompting techniques, see `docs/prompting_guide.md`.

---

*This standard should be reviewed and updated after each major tournament cycle or when new metrics become available in the data pipeline.*