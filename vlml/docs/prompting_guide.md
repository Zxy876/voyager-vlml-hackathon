# Coaching Insights Prompting Guide

This guide explains how to prompt LLMs (Claude, Gemini) effectively using VLML's coaching context data to generate actionable coaching insights.

## Philosophy

**Old approach:** Pre-compute lookup tables ("3v5 retake = 15% win rate")
**VLML approach:** Extract rich situation context → Let LLM reason → Generate insights

The LLM is the reasoning layer. VLML provides:
1. Rich, structured situation data
2. Historical benchmarks as reference
3. Enough context for the LLM to explain its reasoning

---

## Modular Reports (Recommended)

VLML provides **5 match analysis tools** to minimize context usage:

| Tool | Weight | Sections Included | Use Case |
|------|--------|-------------------|----------|
| `match_summary_report` | Light | metadata, games, scope, team_comparison, key_metrics, benchmarks | Overview, deciding what to drill into |
| `match_players_report` | Medium | player_performance, kast_impact_analysis, opening_death_impact, highlight_rounds | Player analysis, VOD priority |
| `match_rounds_report` | Heavy | round_timeline, round_situations, half_breakdown | Deep-dive into specific rounds |
| `match_economy_report` | Heavy | economy_context, attack_patterns | Economy cascade, attack predictability |
| `match_analysis_report` | Full | All 18 sections | Legacy - use when you need everything |

### Recommended Workflow

1. **Start with `match_summary_report`** - get the overview
2. **Drill down** with targeted reports based on what you find:
   - Player issues? → `match_players_report`
   - Round-specific questions? → `match_rounds_report` (with pagination)
   - Economy or tactical patterns? → `match_economy_report`

### Example: Efficient Analysis

```
# Step 1: Get overview
Use match_summary_report for series "2843069" with team "Cloud9"

# Step 2: Based on findings, drill down
Use match_economy_report for series "2843069" with team "Cloud9" to analyze the economy cascade pattern I noticed

# Step 3: Analyze specific rounds
Use match_rounds_report for series "2843069" with team "Cloud9", round_start=10, round_end=15
```

This approach uses ~30% of the tokens compared to calling `match_analysis_report` for the full dataset.

---

## How It Works

VLML exposes data through MCP (Model Context Protocol) tools. The LLM calls the tool, receives the data, and analyzes it - all automatically.

```
You (prompt) → LLM calls match_summary_report → Database returns data → LLM analyzes
```

**You don't need to pass data manually.** The MCP tool fetches everything from the database and returns it to the LLM.

### Simple Example

Just ask for what you want:

```
Analyze Cloud9's match against NRG (series 2843069).

Focus on:
1. Economy cascade patterns - did losing pistol cause a snowball?
2. Attack predictability - are they too one-dimensional?
3. Compare their clutch rate to historical benchmarks

Provide top 3 coaching issues with specific round evidence.
```

The LLM will:
1. Call `match_summary_report` first for overview
2. Call `match_economy_report` for economy cascade analysis
3. Analyze and generate coaching insights

---

## Report Sections Reference

Each modular report returns specific sections. Here's what each section provides:

### Summary Report Sections (`match_summary_report`)
| Section | Data Provided | Coaching Use |
|---------|---------------|--------------|
| `metadata` | Tournament, date, teams | Context |
| `games` | Per-map results and scores | Series outcome |
| `team_comparison` | Side-by-side team metrics | Quick comparison |
| `key_metrics` | Opening duels, conversions, impact | Performance overview |
| `benchmarks` | Historical baseline rates | Contextualize performance |

### Players Report Sections (`match_players_report`)
| Section | Data Provided | Coaching Use |
|---------|---------------|--------------|
| `player_performance` | K/D, ADR, clutches, multikills per player | Individual analysis |
| `kast_impact_analysis` | KAST correlation with round outcomes | Consistency impact |
| `opening_death_impact` | First death correlation with losses | Entry impact |
| `highlight_rounds` | Aces, clutches, multikill rounds | VOD priority |

### Rounds Report Sections (`match_rounds_report`)
| Section | Data Provided | Coaching Use |
|---------|---------------|--------------|
| `round_timeline` | Chronological round breakdown | Round flow |
| `round_situations` | Full per-round state (49 fields) | "What if" reasoning |
| `half_breakdown` | First vs second half performance | Side preference |

### Economy Report Sections (`match_economy_report`)
| Section | Data Provided | Coaching Use |
|---------|---------------|--------------|
| `economy_context` | Round-by-round economy chain | Economy cascade analysis |
| `attack_patterns` | Execute timing, site selection | Predictability detection |

---

## Prompting Patterns

### Pattern 1: Economy Cascade Analysis

**Goal:** Identify economy management issues and their ripple effects.

**Prompt:**
```
Get the match report for LOUD vs Evil Geniuses (series 2843069).

Analyze the economy_context section for:
1. Economy cascades (pistol loss → eco → force → eco chain)
2. Unnecessary force buys (could have saved for full buy)
3. Momentum shifts tied to economy decisions

For each pattern found:
- Describe the chain of events
- Calculate the round cost (how many rounds were affected)
- Suggest what the team should have done differently
```

**What the LLM receives from the tool:**
```json
{
  "economy_context": [
    {"round_number": 1, "buy_type": "pistol", "loadout_value": 3300, "round_won": false, "streak": -1},
    {"round_number": 2, "buy_type": "force", "loadout_value": 16100, "round_won": false, "streak": -2, "prev_round": {"buy_type": "pistol", "round_won": false}},
    {"round_number": 3, "buy_type": "eco", "loadout_value": 4200, "round_won": false, "streak": -3},
    ...
  ]
}
```

**Example LLM Output:**
> **Economy Cascade (Rounds 1-5):**
> Lost pistol (R1) → Forced R2 with 16k loadout → Lost → Eco'd R3 → Lost → Forced R4 → Lost
>
> **Impact:** 0-4 start, lost all economic advantage
>
> **Analysis:** The R2 force was borderline (16k vs opponent's 21k). Given they lost pistol, a full eco R2 would have given them a clean full buy R3. Instead, the failed force created a 3-round deficit.
>
> **Recommendation:** After pistol loss, establish a "clean reset" protocol: eco R2, full buy R3.

---

### Pattern 2: Situation-Based Decision Making

**Goal:** Analyze specific round decisions with full context.

**Prompt:**
```
Get the match report for Cloud9 vs NRG (series 2843069).

From round_situations, find round 22 and analyze:
1. Was this a winnable round given the situation?
2. What was the key decision point?
3. If the team saved instead, what would their next round buying power be?
4. What's the coaching takeaway?

Use the benchmarks to reference typical win rates for similar situations.
```

**What the LLM receives (round_situations entry):**
```json
{
  "round_number": 22,
  "score": {"at_start": "10-11", "team": 10, "opponent": 11},
  "is_match_point": true,
  "situation": {
    "team_alive_at_end": 0,
    "opp_alive_at_end": 2,
    "team_loadout": 18500,
    "opp_loadout": 24000,
    "team_buy_type": "full_buy",
    "opp_buy_type": "full_buy"
  },
  "utility": {"team_used": 4, "opp_used": 7},
  "post_plant": {"retake_attempted": 1, "retake_kills": 1},
  "next_round": {"projected_loadout": 8500, "projected_buy_type": "force"}
}
```

**Example LLM Output:**
> **Round 22 Analysis:**
>
> **Situation:** 3v5 post-plant, team had rifles but opponent had full utility (7 vs team's 4). Score 10-11 (match point against).
>
> **Key Decision:** Attempted retake vs save
>
> **If Saved:** Next round loadout would be ~22k (full buy with util) instead of actual 8.5k (force)
>
> **Verdict:** Given the 3v5 disadvantage AND utility disparity, saving was mathematically better. Historical 3v5 retake win rate is ~15% (see benchmarks), but this specific situation was worse due to util gap.
>
> **Coaching Takeaway:** Establish a "call the save" threshold: if down 2+ players AND opponent has util advantage, prioritize next round economy.

---

### Pattern 3: Attack Predictability Detection

**Goal:** Identify repetitive attack patterns opponents can exploit.

**Prompt:**
```
Get the match report for LOUD (series 2843069).

Analyze attack_patterns for predictability:
1. Execute timing - are they too slow or too fast?
2. Site selection - do they favor one site?
3. First contact patterns - predictable angles?

For each pattern, explain how opponents could exploit it.
```

**What the LLM receives (attack_patterns entries):**
```json
{
  "attack_patterns": [
    {"round_number": 1, "first_contact": {"timing": "mid", "time_s": 45.2}, "site_hit": "A", "late_execute": false},
    {"round_number": 2, "first_contact": {"timing": "late", "time_s": 72.1}, "site_hit": "A", "late_execute": true},
    {"round_number": 3, "first_contact": {"timing": "late", "time_s": 65.8}, "site_hit": "A", "late_execute": true},
    ...
  ]
}
```

**Example LLM Output:**
> **Predictability Issues Found:**
>
> **1. Late Execute Dependency (7/12 attack rounds)**
> First contact consistently after 60s into round. Opponents can stack sites late and use aggressive util early knowing no fast execute is coming.
>
> **2. A-Site Bias (9/12 rounds hit A)**
> Only 2 B hits and 1 no-plant in 12 attack rounds. Opponents can play 3 A / 2 B or even 4-1 split.
>
> **3. Consistent First Blood Location**
> 6/12 first contacts at A Main. Opponent AWPer can hold this angle with confidence.
>
> **Recommendations:**
> - Add 2-3 fast A executes per half (before 30s)
> - Increase B hits to at least 4/12 rounds
> - Vary first contact location: use A Short or Mid more

---

### Pattern 4: Benchmark-Contextualized Analysis

**Goal:** Compare team performance against historical baselines.

**Prompt:**
```
Get the match report for Cloud9 (series 2843069).

Compare their clutch and economy performance to the benchmarks section.
For each metric:
1. Is the team above or below baseline?
2. By how much?
3. What does this suggest about their strengths/weaknesses?
```

**What the LLM receives (benchmarks section):**
```json
{
  "benchmarks": {
    "clutch_rates": {
      "1v1": {"attempts": 135, "wins": 69, "rate": 51.1},
      "1v2": {"attempts": 98, "wins": 11, "rate": 11.2},
      "1v3": {"attempts": 145, "wins": 0, "rate": 0.0}
    },
    "economy_matchups": {
      "eco_vs_full_buy": {"rounds": 342, "wins": 41, "rate": 12.0},
      "force_vs_full_buy": {"rounds": 289, "wins": 81, "rate": 28.0}
    },
    "sample_size": {"total_rounds": 5530, "total_games": 267}
  }
}
```

**Example LLM Output:**
> **Performance vs Benchmarks:**
>
> | Metric | Team | Baseline | Diff | Assessment |
> |--------|------|----------|------|------------|
> | 1v1 Clutch | 4/6 (67%) | 51.1% | +16% | **Strong** - team excels in 1v1s |
> | 1v2 Clutch | 0/3 (0%) | 11.2% | -11% | **Weak** - small sample, but 0/3 is concerning |
> | Eco Wins | 3/8 (38%) | ~12% | +26% | **Strong** - thrifty round specialists |
> | FB Conv | 15/22 (68%) | ~72% | -4% | **Average** - room for improvement |
>
> **Key Insight:** This team is exceptionally good at eco rounds (3x baseline win rate). Consider using this as a strategic weapon - intentional eco-to-thrifty rounds could be a valid tactic. However, they struggle to close 1v2 clutches - prioritize 2v1 setups over leaving players isolated.

---

## Advanced Prompting Techniques

### Combining Multiple Sections

For comprehensive analysis, reference multiple sections:

```
Get the match report for Cloud9 vs NRG (series 2843069).

Provide a full coaching debrief using:
1. economy_context - Identify any economy mismanagement
2. round_situations - Analyze the 3 most impactful rounds
3. attack_patterns - Check for exploitable patterns
4. benchmarks - Compare to historical baselines

Structure your response as:
- Executive Summary (3 sentences)
- Top 3 Issues (with round evidence)
- VOD Review Priority (specific rounds)
- Practice Recommendations (immediate, short-term)
```

### Asking "What If" Questions

The `round_situations` data includes `next_round` projections for counterfactual analysis:

```
Get the match report for Cloud9 (series 2843069).

In round_situations, find rounds where the team lost with a force buy.
For each:
- What was their next_round.projected_loadout?
- If they had saved instead, would they have had a full buy?
- Use benchmarks to compare force_vs_full_buy win rate to full_buy_vs_full_buy

Was forcing the right call?
```

### Role-Specific Analysis

Focus on specific player roles:

```
Get the match report for Cloud9 (series 2843069).

From round_situations, analyze the duelist's opening duels:
- Which rounds show entry_kill vs entry_death?
- What's their FB conversion rate (rounds won after getting FB)?
- Are there rounds with entry_death but no trade (deaths_traded = 0)?

Is the duelist creating enough value for the team?
```

---

## Best Practices

### DO

1. **Reference sections by name** - Say "analyze economy_context" not "analyze the economy data I'm providing"
2. **Ask specific questions** - "Was the R14 save correct?" not "Analyze saves"
3. **Request evidence** - "Cite the specific rounds supporting this"
4. **Reference benchmarks** - "Compare to the benchmarks section"
5. **Structure requests** - Use numbered questions or bullet points
6. **Include series ID** - The LLM needs this to call the tool

### DON'T

1. **Paste data manually** - The tool fetches it automatically from the database
2. **Ask for predictions** - VLML provides context, not crystal balls
3. **Ignore sample sizes** - 2/3 clutches ≠ 67% clutch rate
4. **Skip the "why"** - Always ask for reasoning, not just conclusions

---

## Example Full Prompts

### Option A: Quick Overview (Recommended Start)

Get the summary first, then drill down:

```
Use match_summary_report for series "2843069" with team "Cloud9".

Give me a quick overview:
1. How did the match go (score, maps)?
2. Which team had better opening duels?
3. Any concerning metrics I should dig into?
```

### Option B: Targeted Economy Analysis

For economy cascade investigation:

```
Use match_economy_report for series "2843069" with team "Cloud9".

Analyze the economy_context for:
1. Economy cascades (pistol loss leading to 3+ round deficits)
2. Questionable force/save decisions
3. Compare our eco round win rate to typical baselines

For each pattern found, cite the specific round numbers.
```

### Option C: Player Deep-Dive

For player performance analysis:

```
Use match_players_report for series "2843069" with team "Cloud9".

Identify:
1. Who was the most impactful player and why?
2. Any players underperforming their typical output?
3. Top 3 highlight rounds to review in VOD
```

### Option D: Round-Specific Analysis

For analyzing specific rounds:

```
Use match_rounds_report for series "2843069" with team "Cloud9", round_start=10, round_end=15.

From the round_situations data, analyze rounds 10-15:
- What was happening in each round (score, economy, outcome)?
- Were there any critical mistakes?
- What would different decisions have led to?
```

### Option E: Full Coaching Debrief (Multi-Tool)

For comprehensive analysis, use multiple tools:

```
I need a full coaching debrief for Cloud9 vs NRG (series 2843069).

1. Start with match_summary_report to get the overview
2. Use match_economy_report to analyze economy patterns
3. Use match_players_report to identify player-specific issues
4. Use match_rounds_report for rounds 1-12 to analyze the first half

Prepare a coaching debrief with:
- Executive Summary (3 sentences)
- Top 3 Issues (with round evidence)
- VOD Priority Queue (top 5 rounds)
- Practice Plan (immediate, short-term)
```

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 2.0 | 2025-01 | Added modular reports (summary, players, rounds, economy) |
| 1.0 | 2025-01 | Initial prompting guide for v3.0 coaching context |
