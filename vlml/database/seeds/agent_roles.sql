-- Agent Roles Reference Table
-- Maps Valorant agents to their roles for analytics

CREATE TABLE IF NOT EXISTS agent_roles (
    agent_name VARCHAR PRIMARY KEY,
    agent_role VARCHAR NOT NULL,  -- duelist/initiator/controller/sentinel
    is_duelist BOOLEAN DEFAULT FALSE,
    is_initiator BOOLEAN DEFAULT FALSE,
    is_controller BOOLEAN DEFAULT FALSE,
    is_sentinel BOOLEAN DEFAULT FALSE
);

-- Insert all current Valorant agents (safe for re-runs)
INSERT OR REPLACE INTO agent_roles VALUES
-- Duelists
('Jett', 'duelist', TRUE, FALSE, FALSE, FALSE),
('Raze', 'duelist', TRUE, FALSE, FALSE, FALSE),
('Phoenix', 'duelist', TRUE, FALSE, FALSE, FALSE),
('Reyna', 'duelist', TRUE, FALSE, FALSE, FALSE),
('Yoru', 'duelist', TRUE, FALSE, FALSE, FALSE),
('Neon', 'duelist', TRUE, FALSE, FALSE, FALSE),
('Iso', 'duelist', TRUE, FALSE, FALSE, FALSE),
-- Initiators
('Sova', 'initiator', FALSE, TRUE, FALSE, FALSE),
('Breach', 'initiator', FALSE, TRUE, FALSE, FALSE),
('Skye', 'initiator', FALSE, TRUE, FALSE, FALSE),
('KAY/O', 'initiator', FALSE, TRUE, FALSE, FALSE),
('Fade', 'initiator', FALSE, TRUE, FALSE, FALSE),
('Gekko', 'initiator', FALSE, TRUE, FALSE, FALSE),
-- Controllers
('Omen', 'controller', FALSE, FALSE, TRUE, FALSE),
('Brimstone', 'controller', FALSE, FALSE, TRUE, FALSE),
('Viper', 'controller', FALSE, FALSE, TRUE, FALSE),
('Astra', 'controller', FALSE, FALSE, TRUE, FALSE),
('Harbor', 'controller', FALSE, FALSE, TRUE, FALSE),
('Clove', 'controller', FALSE, FALSE, TRUE, FALSE),
-- Sentinels
('Sage', 'sentinel', FALSE, FALSE, FALSE, TRUE),
('Cypher', 'sentinel', FALSE, FALSE, FALSE, TRUE),
('Killjoy', 'sentinel', FALSE, FALSE, FALSE, TRUE),
('Chamber', 'sentinel', FALSE, FALSE, FALSE, TRUE),
('Deadlock', 'sentinel', FALSE, FALSE, FALSE, TRUE),
('Vyse', 'sentinel', FALSE, FALSE, FALSE, TRUE);

-- Verify agent count
SELECT 'Agent roles loaded' AS status, COUNT(*) AS agent_count FROM agent_roles;
