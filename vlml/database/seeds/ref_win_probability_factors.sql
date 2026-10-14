-- Win probability factors reference table
-- Derived from VCT Americas 2025 data (~55,000 rounds)
-- Used for Win Share calculations

CREATE TABLE IF NOT EXISTS ref_win_probability_factors (
    factor_name VARCHAR PRIMARY KEY,
    factor_type VARCHAR,
    win_rate_with FLOAT,
    win_rate_without FLOAT,
    probability_lift FLOAT,
    weight FLOAT,
    sample_size INT,
    notes VARCHAR
);

-- Clear and re-insert (idempotent)
DELETE FROM ref_win_probability_factors WHERE 1=1;

INSERT INTO ref_win_probability_factors VALUES
    ('first_blood', 'event', 0.7066, 0.4763, 0.2303, 0.2303, 5519, 'Player got first blood'),
    ('survival', 'event', 0.9089, 0.3143, 0.5946, 0.5946, 17172, 'Player survived the round'),
    ('death_traded', 'event', 0.5699, 0.2319, 0.3380, 0.3380, 9263, 'Player died but was traded'),
    ('multi_kill', 'event', 0.7594, 0.4471, 0.3123, 0.3123, 9231, 'Player got 2+ kills in round'),
    ('trade_kill', 'event', 0.5818, 0.4861, 0.0957, 0.0957, 7600, 'Player got a trade kill'),
    ('plant', 'event', 0.6836, 0.4857, 0.1979, 0.1979, 3799, 'Player completed spike plant'),
    ('defuse', 'event', 1.0000, 0.4879, 0.5121, 0.5121, 1224, 'Player completed defuse'),
    ('kast', 'state', 0.5043, 0.1538, 0.3505, NULL, 54392, 'KAST true - not used in win share'),
    ('early_util', 'state', 0.5128, 0.4952, 0.0176, NULL, 12745, 'Used utility early - minimal impact');
