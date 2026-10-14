-- Series metadata table
CREATE TABLE IF NOT EXISTS series (
    series_id VARCHAR PRIMARY KEY,
    tournament_id VARCHAR,
    tournament_name VARCHAR,
    tournament_year INTEGER,
    tournament_region VARCHAR,
    team1_name VARCHAR,
    team2_name VARCHAR,
    winning_team_name VARCHAR,
    start_time TIMESTAMP,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

