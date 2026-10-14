-- Map zones lookup table
-- Grain: one row per map zone bounding box
CREATE TABLE IF NOT EXISTS map_zones (
    map_name VARCHAR NOT NULL,
    zone_name VARCHAR NOT NULL,
    zone_type VARCHAR NOT NULL,  -- default/site/mid/other
    min_x FLOAT NOT NULL,
    max_x FLOAT NOT NULL,
    min_y FLOAT NOT NULL,
    max_y FLOAT NOT NULL,
    PRIMARY KEY (map_name, zone_name)
);
