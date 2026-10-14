-- Games table (individual maps within series)
CREATE TABLE IF NOT EXISTS games (
    game_id VARCHAR PRIMARY KEY,
    series_id VARCHAR NOT NULL,
    game_number INTEGER,
    map_name VARCHAR,
    team1_name VARCHAR,
    team2_name VARCHAR,
    winning_team_name VARCHAR,
    game_duration_seconds INTEGER,
    total_rounds INTEGER
);

