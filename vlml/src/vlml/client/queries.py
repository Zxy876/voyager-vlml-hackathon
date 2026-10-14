"""Updated GraphQL queries matching actual GRID API schema.

These queries are based on the actual GRID API schema discovered through testing.

Central Data API: Organizational data (teams, players, tournaments, series)
Series State API: Detailed match statistics (kills, deaths, player performance)
"""

# ===== Central Data API Queries =====

# Player Queries
GET_PLAYER_BY_NICKNAME = """
query GetPlayerByNickname($nickname: String!, $titleId: ID) {
    players(first: 10, filter: { nickname: { contains: $nickname }, titleId: $titleId }) {
        edges {
            node {
                id
                nickname
                team {
                    id
                    name
                    nameShortened
                }
            }
        }
    }
}
"""

GET_PLAYER_BY_ID = """
query GetPlayerById($id: ID!) {
    player(id: $id) {
        id
        nickname
        fullName
        nationality
        imageUrl
        age
        team {
            id
            name
            nameShortened
            logoUrl
        }
        roles {
            name
        }
    }
}
"""

# Team Queries
GET_TEAM_BY_NAME = """
query GetTeamByName($name: String!) {
    teams(first: 10, filter: { name: { contains: $name } }) {
        edges {
            node {
                id
                name
                nameShortened
                logoUrl
                colorPrimary
                colorSecondary
                organization {
                    id
                    name
                }
            }
        }
    }
}
"""

GET_TEAM_BY_ID = """
query GetTeamById($id: ID!) {
    team(id: $id) {
        id
        name
        nameShortened
        logoUrl
        colorPrimary
        colorSecondary
        organization {
            id
            name
        }
    }
}
"""

# Series/Match Queries
GET_RECENT_SERIES = """
query GetRecentSeries($titleId: ID, $first: Int!) {
    allSeries(first: $first, filter: { titleId: $titleId }) {
        edges {
            node {
                id
                title {
                    name
                }
                tournament {
                    id
                    name
                }
                teams {
                    baseInfo {
                        id
                        name
                        nameShortened
                    }
                }
                startTimeScheduled
            }
        }
    }
}
"""

# Get series from 2025 tournaments (for recent data)
GET_RECENT_SERIES_2025 = """
query GetRecentSeries2025($tournamentIds: [ID!]!, $first: Int!) {
    allSeries(first: $first, filter: { tournamentId_in: $tournamentIds }) {
        edges {
            node {
                id
                title {
                    name
                }
                tournament {
                    id
                    name
                }
                teams {
                    baseInfo {
                        id
                        name
                        nameShortened
                    }
                }
                startTimeScheduled
            }
        }
    }
}
"""

GET_SERIES_BY_ID = """
query GetSeriesById($id: ID!) {
    series(id: $id) {
        id
        title {
            id
            name
        }
        tournament {
            id
            name
        }
        teams {
            baseInfo {
                id
                name
                nameShortened
                logoUrl
            }
        }
        startTimeScheduled
    }
}
"""

# Tournament Queries
GET_TOURNAMENTS = """
query GetTournaments($titleId: ID, $first: Int!) {
    tournaments(first: $first, filter: { titleId: $titleId }) {
        edges {
            node {
                id
                name
            }
        }
    }
}
"""

GET_TOURNAMENT_BY_ID = """
query GetTournamentById($id: ID!) {
    tournament(id: $id) {
        id
        name
    }
}
"""

GET_SERIES_BY_TOURNAMENT = """
query GetSeriesByTournament($tournamentId: ID!, $first: Int!) {
    allSeries(first: $first, filter: { tournamentId: $tournamentId }) {
        edges {
            node {
                id
                title {
                    name
                }
                tournament {
                    id
                    name
                }
                teams {
                    baseInfo {
                        id
                        name
                        nameShortened
                    }
                }
                startTimeScheduled
            }
        }
    }
}
"""

# ===== Series State API Queries =====

GET_SERIES_STATE = """
query GetSeriesState($id: ID!) {
    seriesState(id: $id) {
        id
        started
        finished
        teams {
            id
            name
            won
        }
        games {
            teams {
                id
                players {
                    id
                    name
                    kills
                    deaths
                }
            }
        }
    }
}
"""

GET_SERIES_STATE_FULL = """
query GetSeriesStateFull($id: ID!) {
    seriesState(id: $id) {
        id
        started
        finished
        teams {
            id
            name
            won
        }
        games {
            id
            teams {
                id
                won
                players {
                    id
                    name
                    kills
                    deaths
                }
            }
        }
    }
}
"""

# Get player stats by player ID (from Series State API)
GET_PLAYER_SERIES_STATS = """
query GetPlayerSeriesStats($playerId: ID!) {
    latestSeriesStateByPlayerId(id: $playerId) {
        id
        started
        finished
        games {
            teams {
                players {
                    id
                    name
                    kills
                    deaths
                }
            }
        }
    }
}
"""
