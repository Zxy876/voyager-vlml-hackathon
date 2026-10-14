-- Weapon Types Reference Table
-- Maps Valorant weapons to their categories for analytics

CREATE TABLE IF NOT EXISTS weapon_types (
    weapon_name VARCHAR PRIMARY KEY,
    weapon_type VARCHAR NOT NULL,  -- rifle/smg/pistol/sniper/shotgun/heavy/melee
    weapon_class VARCHAR,  -- primary/secondary/melee
    cost INTEGER,
    is_rifle BOOLEAN DEFAULT FALSE,
    is_smg BOOLEAN DEFAULT FALSE,
    is_pistol BOOLEAN DEFAULT FALSE,
    is_sniper BOOLEAN DEFAULT FALSE,
    is_shotgun BOOLEAN DEFAULT FALSE,
    is_heavy BOOLEAN DEFAULT FALSE,
    is_melee BOOLEAN DEFAULT FALSE
);

-- Insert all Valorant weapons (safe for re-runs)
INSERT OR REPLACE INTO weapon_types VALUES
-- Rifles
('Vandal', 'rifle', 'primary', 2900, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE),
('Phantom', 'rifle', 'primary', 2900, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE),
('Bulldog', 'rifle', 'primary', 2050, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE),
('Guardian', 'rifle', 'primary', 2250, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE),
-- SMGs
('Spectre', 'smg', 'primary', 1600, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE),
('Stinger', 'smg', 'primary', 950, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE, FALSE),
-- Snipers
('Operator', 'sniper', 'primary', 4700, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE),
('Marshal', 'sniper', 'primary', 950, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE),
('Outlaw', 'sniper', 'primary', 2400, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE),
-- Shotguns
('Judge', 'shotgun', 'primary', 1850, FALSE, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE),
('Bucky', 'shotgun', 'primary', 850, FALSE, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE),
-- Pistols
('Classic', 'pistol', 'secondary', 0, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE),
('Shorty', 'pistol', 'secondary', 150, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE),
('Frenzy', 'pistol', 'secondary', 450, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE),
('Ghost', 'pistol', 'secondary', 500, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE),
('Sheriff', 'pistol', 'secondary', 800, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE),
-- Heavy
('Ares', 'heavy', 'primary', 1600, FALSE, FALSE, FALSE, FALSE, FALSE, TRUE, FALSE),
('Odin', 'heavy', 'primary', 3200, FALSE, FALSE, FALSE, FALSE, FALSE, TRUE, FALSE),
-- Melee
('Knife', 'melee', 'melee', 0, FALSE, FALSE, FALSE, FALSE, FALSE, FALSE, TRUE);

-- Verify weapon count
SELECT 'Weapon types loaded' AS status, COUNT(*) AS weapon_count FROM weapon_types;
