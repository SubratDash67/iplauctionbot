# database.py
"""
Database Module - SQLite persistence layer
Handles all data storage for auction state, bids, teams, and history
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager


class Database:
    """SQLite database manager for auction persistence"""

    def __init__(self, db_path: str = "auction.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory and timeout"""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self):
        """Context manager for database transactions"""
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database tables"""
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Enable WAL mode for better concurrency
            cursor.execute("PRAGMA journal_mode=WAL")

            # Teams table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    team_code TEXT PRIMARY KEY,
                    purse INTEGER NOT NULL,
                    original_purse INTEGER NOT NULL
                )
            """
            )

            # Team squads (players bought)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS team_squads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_code TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    acquisition_type TEXT DEFAULT 'bought',
                    source_team TEXT,
                    bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (team_code) REFERENCES teams(team_code),
                    UNIQUE(team_code, player_name COLLATE NOCASE)
                )
            """
            )

            # Migration: Add acquisition_type and source_team columns if they don't exist
            try:
                cursor.execute(
                    "ALTER TABLE team_squads ADD COLUMN acquisition_type TEXT DEFAULT 'bought'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

            try:
                cursor.execute("ALTER TABLE team_squads ADD COLUMN source_team TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Global unique constraint: player can only be in ONE team across all teams
            try:
                cursor.execute(
                    """
                    DELETE FROM team_squads
                    WHERE id NOT IN (
                        SELECT MAX(id) FROM team_squads GROUP BY LOWER(player_name)
                    )
                    """
                )
                deleted = cursor.rowcount
            except Exception:
                pass

            try:
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_global_unique_player ON team_squads(player_name COLLATE NOCASE)"
                )
            except sqlite3.IntegrityError:
                # Force cleanup if index creation fails
                try:
                    cursor.execute(
                        """
                        DELETE FROM team_squads
                        WHERE id NOT IN (
                            SELECT MAX(id) FROM team_squads GROUP BY LOWER(player_name)
                        )
                        """
                    )
                    cursor.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_global_unique_player ON team_squads(player_name COLLATE NOCASE)"
                    )
                except Exception:
                    pass

            # Player lists
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS player_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_name TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    base_price INTEGER,
                    auctioned INTEGER DEFAULT 0
                )
            """
            )

            # List order (with enabled flag for incremental loading)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS list_order (
                    position INTEGER PRIMARY KEY,
                    list_name TEXT NOT NULL UNIQUE,
                    enabled INTEGER DEFAULT 0
                )
            """
            )

            try:
                cursor.execute(
                    "ALTER TABLE list_order ADD COLUMN enabled INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass

            # Auction state (single row)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    active INTEGER DEFAULT 0,
                    paused INTEGER DEFAULT 0,
                    current_player TEXT,
                    current_list_index INTEGER DEFAULT 0,
                    base_price INTEGER DEFAULT 0,
                    current_bid INTEGER DEFAULT 0,
                    highest_bidder TEXT,
                    countdown_seconds INTEGER DEFAULT 15,
                    extensions_used INTEGER DEFAULT 0,
                    last_bid_time REAL DEFAULT 0,
                    stats_channel_id TEXT,
                    stats_message_id TEXT
                )
            """
            )

            try:
                cursor.execute(
                    "ALTER TABLE auction_state ADD COLUMN last_bid_time REAL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE auction_state ADD COLUMN stats_channel_id TEXT"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE auction_state ADD COLUMN stats_message_id TEXT"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE auction_state ADD COLUMN max_loaded_set INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE auction_state ADD COLUMN trade_channel_id TEXT"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE auction_state ADD COLUMN trade_message_id TEXT"
                )
            except sqlite3.OperationalError:
                pass

            cursor.execute(
                """
                INSERT OR IGNORE INTO auction_state (id) VALUES (1)
            """
            )

            # Bid history (audit log)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bid_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_name TEXT NOT NULL,
                    team_code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT,
                    amount INTEGER NOT NULL,
                    is_auto_bid INTEGER DEFAULT 0,
                    interaction_id TEXT,
                    timestamp REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Auto-bid settings
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auto_bids (
                    team_code TEXT PRIMARY KEY,
                    max_amount INTEGER NOT NULL,
                    set_by_user_id INTEGER NOT NULL,
                    active INTEGER DEFAULT 1
                )
            """
            )

            # User-team mapping
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_teams (
                    user_id INTEGER PRIMARY KEY,
                    team_code TEXT NOT NULL,
                    user_name TEXT,
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Sale history (finalized sales)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_name TEXT NOT NULL,
                    team_code TEXT NOT NULL,
                    final_price INTEGER NOT NULL,
                    total_bids INTEGER DEFAULT 0,
                    sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Trade history (separate from sales)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_name TEXT NOT NULL,
                    from_team TEXT NOT NULL,
                    to_team TEXT NOT NULL,
                    trade_price INTEGER NOT NULL,
                    original_price INTEGER NOT NULL,
                    trade_type TEXT DEFAULT 'cash',
                    swap_player TEXT,
                    swap_player_price INTEGER,
                    compensation_amount INTEGER DEFAULT 0,
                    compensation_direction TEXT,
                    traded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Migration: Add new columns for swap trades
            try:
                cursor.execute(
                    "ALTER TABLE trade_history ADD COLUMN trade_type TEXT DEFAULT 'cash'"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE trade_history ADD COLUMN swap_player TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE trade_history ADD COLUMN swap_player_price INTEGER"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE trade_history ADD COLUMN compensation_amount INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE trade_history ADD COLUMN compensation_direction TEXT"
                )
            except sqlite3.OperationalError:
                pass

    # ==================== TEAM OPERATIONS ====================

    def init_teams(self, teams: Dict[str, int]):
        """Initialize teams with purses - ONLY if no teams exist"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM teams")
            for team_code, purse in teams.items():
                cursor.execute(
                    "INSERT INTO teams (team_code, purse, original_purse) VALUES (?, ?, ?)",
                    (team_code, purse, purse),
                )

    def init_teams_if_empty(self, teams: Dict[str, int]) -> bool:
        """Initialize teams ONLY if teams table is empty."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM teams")
            if cursor.fetchone()["cnt"] > 0:
                return False

            for team_code, purse in teams.items():
                cursor.execute(
                    "INSERT INTO teams (team_code, purse, original_purse) VALUES (?, ?, ?)",
                    (team_code, purse, purse),
                )
            return True

    def get_teams(self) -> Dict[str, int]:
        """Get all teams with current purses"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT team_code, purse FROM teams")
            return {row["team_code"]: row["purse"] for row in cursor.fetchall()}

    def get_team_purse(self, team_code: str) -> Optional[int]:
        """Get a specific team's purse"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT purse FROM teams WHERE team_code = ?", (team_code,))
            row = cursor.fetchone()
            return row["purse"] if row else None

    def update_team_purse(self, team_code: str, new_purse: int) -> bool:
        """Update a team's purse"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE teams SET purse = ? WHERE team_code = ?", (new_purse, team_code)
            )
            return cursor.rowcount > 0

    def deduct_from_purse(self, team_code: str, amount: int) -> bool:
        """Atomically deduct from team purse"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE teams SET purse = purse - ? WHERE team_code = ? AND purse >= ?",
                (amount, team_code, amount),
            )
            return cursor.rowcount > 0

    def reset_teams(self):
        """Reset all teams to original purses"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE teams SET purse = original_purse")

    def set_original_purse(self, team_code: str, purse: int) -> bool:
        """Update a team's original_purse value"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE teams SET original_purse = ? WHERE team_code = ?",
                (purse, team_code),
            )
            return cursor.rowcount > 0

    # ==================== TEAM SQUAD OPERATIONS ====================

    def add_to_squad(
        self,
        team_code: str,
        player_name: str,
        price: int,
        acquisition_type: str = "bought",
        source_team: str = None,
    ) -> bool:
        """Add a player to team's squad."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT team_code FROM team_squads WHERE LOWER(player_name) = LOWER(?)",
                    (player_name,),
                )
                existing = cursor.fetchone()
                if existing:
                    return False

                cursor.execute(
                    """INSERT INTO team_squads (team_code, player_name, price, acquisition_type, source_team) 
                       VALUES (?, ?, ?, ?, ?)""",
                    (team_code, player_name, price, acquisition_type, source_team),
                )
                return True
        except sqlite3.IntegrityError:
            return False

    def remove_from_squad(self, team_code: str, player_name: str) -> bool:
        """Remove a player from a team's squad"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_code, player_name),
            )
            return cursor.rowcount > 0

    def get_team_squad(self, team_code: str) -> List[Tuple[str, int, str, str]]:
        """Get all players in a team's squad with acquisition info."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT player_name, price, 
                          COALESCE(acquisition_type, 'bought') as acquisition_type,
                          source_team 
                   FROM team_squads WHERE team_code = ? ORDER BY bought_at""",
                (team_code,),
            )
            return [
                (
                    row["player_name"],
                    row["price"],
                    row["acquisition_type"],
                    row["source_team"],
                )
                for row in cursor.fetchall()
            ]

    def get_all_squads(self) -> Dict[str, List[Tuple[str, int]]]:
        """Get all team squads (simple format)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT team_code FROM teams")
            teams = [row["team_code"] for row in cursor.fetchall()]

            squads = {}
            for team in teams:
                cursor.execute(
                    "SELECT player_name, price FROM team_squads WHERE team_code = ? ORDER BY bought_at",
                    (team,),
                )
                squads[team] = [
                    (row["player_name"], row["price"]) for row in cursor.fetchall()
                ]
            return squads

    def get_all_squads_detailed(self) -> Dict[str, List[Tuple[str, int, str, str]]]:
        """Get all team squads with acquisition details"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT team_code FROM teams")
            teams = [row["team_code"] for row in cursor.fetchall()]

            squads = {}
            for team in teams:
                squads[team] = self.get_team_squad(team)
            return squads

    def clear_squads(self):
        """Clear all squad data (Wipes everything)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM team_squads")

    def clear_auction_buys(self):
        """Clear ONLY bought/traded players, keep retained players.
        Also clears Sales history since that tracks buys."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            # Delete players who were NOT retained
            cursor.execute(
                "DELETE FROM team_squads WHERE acquisition_type != 'retained'"
            )

    # ==================== PLAYER LIST OPERATIONS ====================

    def create_list(self, list_name: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM player_lists WHERE list_name = ?",
                (list_name.lower(),),
            )
            if cursor.fetchone()["cnt"] > 0:
                return False

            cursor.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 as next_pos FROM list_order"
            )
            next_pos = cursor.fetchone()["next_pos"]
            cursor.execute(
                "INSERT OR IGNORE INTO list_order (position, list_name, enabled) VALUES (?, ?, 0)",
                (next_pos, list_name.lower()),
            )
            return True

    def add_player_to_list(
        self, list_name: str, player_name: str, base_price: Optional[int] = None
    ) -> bool:
        """Add a single player to a list with duplicate checking.

        Returns False if player already exists in:
        - player_lists (not yet auctioned)
        - team_squads (already sold/retained)
        """
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Check if player already exists in player_lists (not yet auctioned)
            cursor.execute(
                "SELECT 1 FROM player_lists WHERE LOWER(player_name) = LOWER(?) AND auctioned = 0",
                (player_name,),
            )
            if cursor.fetchone():
                return False

            # Check if player already exists in team_squads
            cursor.execute(
                "SELECT 1 FROM team_squads WHERE LOWER(player_name) = LOWER(?)",
                (player_name,),
            )
            if cursor.fetchone():
                return False

            cursor.execute(
                "INSERT INTO player_lists (list_name, player_name, base_price) VALUES (?, ?, ?)",
                (list_name.lower(), player_name, base_price),
            )
            return True

    def remove_player_from_list(self, list_name: str, player_name: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM player_lists WHERE LOWER(list_name) = LOWER(?) AND LOWER(player_name) = LOWER(?) AND auctioned = 0",
                (list_name, player_name),
            )
            return cursor.rowcount > 0

    def mark_set_as_auctioned(self, set_name: str) -> int:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET auctioned = 1 WHERE LOWER(list_name) = LOWER(?) AND auctioned = 0",
                (set_name,),
            )
            return cursor.rowcount

    def add_players_to_list(
        self, list_name: str, players: List[Tuple[str, Optional[int]]]
    ):
        """Add players to a list with duplicate checking.

        Skips players who are:
        1. Already in any player_list (not yet auctioned)
        2. Already in any team's squad (retained, bought, or traded)
        """
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Get all existing players in player_lists (case-insensitive) - only non-auctioned
            cursor.execute(
                "SELECT LOWER(player_name) FROM player_lists WHERE auctioned = 0"
            )
            existing_in_lists = {row[0] for row in cursor.fetchall()}

            # Get all existing players in team_squads (case-insensitive)
            cursor.execute("SELECT LOWER(player_name) FROM team_squads")
            existing_in_squads = {row[0] for row in cursor.fetchall()}

            added_count = 0
            skipped_count = 0
            for player_name, base_price in players:
                player_lower = player_name.lower()
                # Skip if player already exists anywhere
                if (
                    player_lower in existing_in_lists
                    or player_lower in existing_in_squads
                ):
                    skipped_count += 1
                    continue

                cursor.execute(
                    "INSERT INTO player_lists (list_name, player_name, base_price) VALUES (?, ?, ?)",
                    (list_name.lower(), player_name, base_price),
                )
                existing_in_lists.add(player_lower)  # Track newly added players
                added_count += 1

            return added_count, skipped_count

    def get_player_lists(self) -> Dict[str, List[Tuple[str, Optional[int]]]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT list_name, player_name, base_price FROM player_lists WHERE auctioned = 0"
            )
            lists = {}
            for row in cursor.fetchall():
                list_name = row["list_name"]
                if list_name not in lists:
                    lists[list_name] = []
                lists[list_name].append((row["player_name"], row["base_price"]))
            return lists

    def get_all_lists(self) -> Dict[str, List[dict]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT list_name, player_name, base_price, auctioned FROM player_lists"
            )
            lists = {}
            for row in cursor.fetchall():
                list_name = row["list_name"]
                if list_name not in lists:
                    lists[list_name] = []
                lists[list_name].append(
                    {
                        "player_name": row["player_name"],
                        "base_price": row["base_price"],
                        "auctioned": bool(row["auctioned"]),
                    }
                )
            return lists

    def get_list_order(self) -> List[str]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_name FROM list_order ORDER BY position")
            return [row["list_name"] for row in cursor.fetchall()]

    def set_list_order(self, order: List[str]) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_order")
            for i, list_name in enumerate(order):
                cursor.execute(
                    "INSERT INTO list_order (position, list_name, enabled) VALUES (?, ?, 0)",
                    (i, list_name.lower()),
                )
            return True

    def mark_player_auctioned(self, list_name: str, player_name: str):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET auctioned = 1 WHERE list_name = ? AND player_name = ? AND auctioned = 0 LIMIT 1",
                (list_name.lower(), player_name),
            )

    def get_random_player_from_list(
        self, list_name: str
    ) -> Optional[Tuple[int, str, Optional[int]]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, player_name, base_price FROM player_lists WHERE list_name = ? AND auctioned = 0 ORDER BY RANDOM() LIMIT 1",
                (list_name.lower(),),
            )
            row = cursor.fetchone()
            if row:
                return (row["id"], row["player_name"], row["base_price"])
            return None

    def mark_player_auctioned_by_id(self, player_id: int):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET auctioned = 1 WHERE id = ?", (player_id,)
            )

    def clear_player_lists(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM player_lists")
            cursor.execute("DELETE FROM list_order")

    def reset_all_player_auction_status(self):
        """Reset auctioned status for ALL players (allows re-bidding)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE player_lists SET auctioned = 0")

    def clear_released_players(self):
        """Remove the 'released' list"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM player_lists WHERE list_name = 'released'")
            cursor.execute("DELETE FROM list_order WHERE list_name = 'released'")
            # Also clear old name for backwards compatibility
            cursor.execute(
                "DELETE FROM player_lists WHERE list_name = 'released players'"
            )
            cursor.execute(
                "DELETE FROM list_order WHERE list_name = 'released players'"
            )

    def clear_unsold_players(self):
        """Remove the 'accelerated' list (formerly 'unsold players')"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM player_lists WHERE list_name = 'accelerated'")
            cursor.execute("DELETE FROM list_order WHERE list_name = 'accelerated'")
            # Also clear old name for backwards compatibility
            cursor.execute(
                "DELETE FROM player_lists WHERE list_name = 'unsold players'"
            )
            cursor.execute("DELETE FROM list_order WHERE list_name = 'unsold players'")

    def delete_set(self, set_name: str) -> tuple:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM list_order WHERE LOWER(list_name) = LOWER(?)",
                (set_name,),
            )
            if cursor.fetchone()["cnt"] == 0:
                return (False, f"Set '{set_name}' does not exist")

            cursor.execute(
                "SELECT COUNT(*) as cnt FROM player_lists WHERE LOWER(list_name) = LOWER(?)",
                (set_name,),
            )
            player_count = cursor.fetchone()["cnt"]
            cursor.execute(
                "DELETE FROM player_lists WHERE LOWER(list_name) = LOWER(?)",
                (set_name,),
            )
            cursor.execute(
                "DELETE FROM list_order WHERE LOWER(list_name) = LOWER(?)", (set_name,)
            )

            return (True, f"Deleted set '{set_name}' with {player_count} players")

    # ==================== AUCTION STATE OPERATIONS ====================

    def get_auction_state(self) -> dict:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM auction_state WHERE id = 1")
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {}

    def update_auction_state(self, **kwargs):
        if not kwargs:
            return
        with self._transaction() as conn:
            cursor = conn.cursor()
            fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
            values = list(kwargs.values())
            cursor.execute(f"UPDATE auction_state SET {fields} WHERE id = 1", values)

    def reset_auction_state(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE auction_state SET
                    active = 0,
                    paused = 0,
                    current_player = NULL,
                    current_list_index = 0,
                    base_price = 0,
                    current_bid = 0,
                    highest_bidder = NULL,
                    extensions_used = 0,
                    last_bid_time = 0
                WHERE id = 1
            """
            )

    def get_max_loaded_set(self) -> int:
        """Get the maximum set number that has been loaded"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT max_loaded_set FROM auction_state WHERE id = 1")
            row = cursor.fetchone()
            return row["max_loaded_set"] if row and row["max_loaded_set"] else 0

    def set_max_loaded_set(self, max_set: int):
        """Set the maximum set number that has been loaded"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE auction_state SET max_loaded_set = ? WHERE id = 1", (max_set,)
            )

    def set_trade_channel(self, channel_id: str, message_id: str = None):
        """Set the trade log channel and optionally the message ID"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            if message_id:
                cursor.execute(
                    "UPDATE auction_state SET trade_channel_id = ?, trade_message_id = ? WHERE id = 1",
                    (channel_id, message_id),
                )
            else:
                cursor.execute(
                    "UPDATE auction_state SET trade_channel_id = ? WHERE id = 1",
                    (channel_id,),
                )

    def get_trade_channel(self) -> Tuple[Optional[str], Optional[str]]:
        """Get the trade log channel and message IDs"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT trade_channel_id, trade_message_id FROM auction_state WHERE id = 1"
            )
            row = cursor.fetchone()
            if row:
                return row["trade_channel_id"], row["trade_message_id"]
            return None, None

    def get_all_trades(self) -> List[dict]:
        """Get all trades for display in trade log"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT player_name, from_team, to_team, trade_price, original_price, 
                          trade_type, swap_player, swap_player_price, 
                          compensation_amount, compensation_direction, traded_at
                   FROM trade_history ORDER BY traded_at DESC"""
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== BID HISTORY OPERATIONS ====================

    def record_bid(
        self,
        player_name: str,
        team_code: str,
        user_id: int,
        amount: int,
        timestamp: float,
        user_name: str = None,
        is_auto_bid: bool = False,
        interaction_id: str = None,
    ):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO bid_history 
                (player_name, team_code, user_id, user_name, amount, is_auto_bid, interaction_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    player_name,
                    team_code,
                    user_id,
                    user_name,
                    amount,
                    1 if is_auto_bid else 0,
                    interaction_id,
                    timestamp,
                ),
            )

    def get_bid_history_for_player(self, player_name: str) -> List[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM bid_history WHERE player_name = ? ORDER BY timestamp",
                (player_name,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_bids(self, limit: int = 10) -> List[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM bid_history ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_highest_bid_for_player(self, player_name: str) -> Optional[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM bid_history WHERE player_name = ? ORDER BY amount DESC, timestamp DESC LIMIT 1",
                (player_name,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def count_bids_for_player(self, player_name: str) -> int:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM bid_history WHERE player_name = ?",
                (player_name,),
            )
            return cursor.fetchone()["cnt"]

    def clear_bid_history(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bid_history")

    # ==================== AUTO-BID OPERATIONS ====================

    def set_auto_bid(self, team_code: str, max_amount: int, user_id: int):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO auto_bids (team_code, max_amount, set_by_user_id, active)
                VALUES (?, ?, ?, 1)
            """,
                (team_code, max_amount, user_id),
            )

    def get_auto_bid(self, team_code: str) -> Optional[int]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT max_amount FROM auto_bids WHERE team_code = ? AND active = 1",
                (team_code,),
            )
            row = cursor.fetchone()
            return row["max_amount"] if row else None

    def get_all_auto_bids(self) -> Dict[str, int]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT team_code, max_amount FROM auto_bids WHERE active = 1"
            )
            return {row["team_code"]: row["max_amount"] for row in cursor.fetchall()}

    def clear_auto_bid(self, team_code: str):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE auto_bids SET active = 0 WHERE team_code = ?", (team_code,)
            )

    def clear_all_auto_bids(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM auto_bids")

    # ==================== USER-TEAM MAPPING ====================

    def set_user_team(self, user_id: int, team_code: str, user_name: str = None):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO user_teams (user_id, team_code, user_name)
                VALUES (?, ?, ?)
            """,
                (user_id, team_code, user_name),
            )

    def get_user_team(self, user_id: int) -> Optional[str]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT team_code FROM user_teams WHERE user_id = ?", (user_id,)
            )
            row = cursor.fetchone()
            return row["team_code"] if row else None

    def get_all_user_teams(self) -> Dict[int, str]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, team_code FROM user_teams")
            return {row["user_id"]: row["team_code"] for row in cursor.fetchall()}

    def remove_user_team(self, user_id: int) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_teams WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    def clear_user_teams(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_teams")

    # ==================== SALES OPERATIONS ====================

    def record_sale(
        self, player_name: str, team_code: str, final_price: int, total_bids: int = 0
    ):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sales (player_name, team_code, final_price, total_bids)
                VALUES (?, ?, ?, ?)
            """,
                (player_name, team_code, final_price, total_bids),
            )

    def get_all_sales(self) -> List[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sales ORDER BY sold_at")
            return [dict(row) for row in cursor.fetchall()]

    def get_last_sale(self) -> Optional[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sales ORDER BY sold_at DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_sale(self, player_name: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM sales WHERE LOWER(player_name) = LOWER(?)", (player_name,)
            )
            return cursor.rowcount > 0

    def rollback_last_sale(self) -> Optional[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sales ORDER BY sold_at DESC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None

            sale = dict(row)

            # Restore purse
            cursor.execute(
                "UPDATE teams SET purse = purse + ? WHERE team_code = ?",
                (sale["final_price"], sale["team_code"]),
            )

            # Remove from squad
            cursor.execute(
                """DELETE FROM team_squads WHERE id = (
                    SELECT id FROM team_squads 
                    WHERE team_code = ? AND LOWER(player_name) = LOWER(?)
                    ORDER BY bought_at DESC LIMIT 1
                )""",
                (sale["team_code"], sale["player_name"]),
            )

            # Delete sale record
            cursor.execute("DELETE FROM sales WHERE id = ?", (sale["id"],))

            return sale

    def clear_sales(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sales")

    # ==================== ADDITIONAL OPERATIONS ====================

    def get_auctioned_count(self) -> int:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM player_lists WHERE auctioned = 1"
            )
            return cursor.fetchone()["cnt"]

    def find_player_by_name(
        self, player_name: str
    ) -> Optional[Tuple[int, str, str, Optional[int], int]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, player_name, list_name, base_price, auctioned FROM player_lists WHERE LOWER(player_name) = LOWER(?)",
                (player_name,),
            )
            row = cursor.fetchone()
            if row:
                return (
                    row["id"],
                    row["player_name"],
                    row["list_name"],
                    row["base_price"],
                    row["auctioned"],
                )

            cursor.execute(
                "SELECT id, player_name, list_name, base_price, auctioned FROM player_lists WHERE LOWER(player_name) LIKE LOWER(?)",
                (f"%{player_name}%",),
            )
            row = cursor.fetchone()
            if row:
                return (
                    row["id"],
                    row["player_name"],
                    row["list_name"],
                    row["base_price"],
                    row["auctioned"],
                )
            return None

    def reset_player_auctioned_status(self, player_id: int):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET auctioned = 0 WHERE id = ?", (player_id,)
            )

    def move_player_to_list_by_id(self, player_id: int, new_list_name: str) -> bool:
        """Move a player to a different list and reset their auctioned status.

        This is used for re-auction operations where we want to move a player
        from one list to another without triggering duplicate checks.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET list_name = ?, auctioned = 0 WHERE id = ?",
                (new_list_name.lower(), player_id),
            )
            return cursor.rowcount > 0

    def delete_last_bid(self, player_name: str):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM bid_history WHERE id = (SELECT id FROM bid_history WHERE player_name = ? ORDER BY timestamp DESC LIMIT 1)",
                (player_name,),
            )

    def get_previous_bid(self, player_name: str) -> Optional[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM bid_history WHERE player_name = ? ORDER BY timestamp DESC LIMIT 1",
                (player_name,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def trade_player(
        self, player_name: str, from_team: str, to_team: str, price: int
    ) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id, price, player_name FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (from_team, player_name),
            )
            row = cursor.fetchone()
            if not row:
                return False

            original_price = row["price"]
            actual_player_name = row["player_name"]

            cursor.execute(
                "SELECT purse FROM teams WHERE team_code = ?",
                (to_team,),
            )
            target_purse = cursor.fetchone()
            if not target_purse or target_purse["purse"] < price:
                return False

            cursor.execute(
                "DELETE FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (from_team, player_name),
            )

            cursor.execute(
                "UPDATE teams SET purse = purse + ? WHERE team_code = ?",
                (price, from_team),
            )

            cursor.execute(
                "UPDATE teams SET purse = purse - ? WHERE team_code = ?",
                (price, to_team),
            )

            cursor.execute(
                """INSERT INTO team_squads (team_code, player_name, price, acquisition_type, source_team) 
                   VALUES (?, ?, ?, 'traded', ?)""",
                (to_team, actual_player_name, price, from_team),
            )

            cursor.execute(
                """INSERT INTO trade_history (player_name, from_team, to_team, trade_price, original_price, trade_type)
                   VALUES (?, ?, ?, ?, ?, 'cash')""",
                (actual_player_name, from_team, to_team, price, original_price),
            )

            return True

    def swap_players(
        self,
        player_a: str,
        team_a: str,
        player_b: str,
        team_b: str,
        compensation_amount: int = 0,
        compensation_from: str = None,
    ) -> Tuple[bool, str]:
        """
        Swap two players between teams.
        Per IPL rules:
        - Players exchange teams
        - Each player's salary counts against their NEW team's cap
        - If values differ, the team getting the higher-valued player pays compensation
        - Compensation is NOT part of salary cap calculation

        Args:
            player_a: Player moving from team_a to team_b
            team_a: Team giving player_a
            player_b: Player moving from team_b to team_a
            team_b: Team giving player_b
            compensation_amount: Amount to be paid as compensation (if values differ)
            compensation_from: Team paying the compensation ('A' or 'B')

        Returns:
            Tuple of (success, message)
        """
        with self._transaction() as conn:
            cursor = conn.cursor()

            # Get player A details from team A
            cursor.execute(
                "SELECT id, price, player_name FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_a, player_a),
            )
            row_a = cursor.fetchone()
            if not row_a:
                return False, f"Player '{player_a}' not found in {team_a}'s squad"

            price_a = row_a["price"]
            actual_name_a = row_a["player_name"]

            # Get player B details from team B
            cursor.execute(
                "SELECT id, price, player_name FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_b, player_b),
            )
            row_b = cursor.fetchone()
            if not row_b:
                return False, f"Player '{player_b}' not found in {team_b}'s squad"

            price_b = row_b["price"]
            actual_name_b = row_b["player_name"]

            # Per IPL swap rules:
            # - The price DIFFERENCE is transferred from team getting higher-valued player
            # - NO refund of original prices
            # - Players keep their original prices in new teams
            #
            # Example: Bumrah (18cr MI) ↔ Khaleel (4.8cr CSK)
            # Difference = 18 - 4.8 = 13.2cr
            # MI gets +13.2cr, CSK pays -13.2cr

            # Calculate price difference
            price_difference = abs(price_a - price_b)

            # Determine which team pays the difference
            # Team getting higher-valued player pays the difference
            if price_a > price_b:
                # Team B gets higher-valued player A, so B pays difference to A
                team_paying_diff = team_b
                team_receiving_diff = team_a
            else:
                # Team A gets higher-valued player B, so A pays difference to B
                team_paying_diff = team_a
                team_receiving_diff = team_b

            # Check purses
            cursor.execute("SELECT purse FROM teams WHERE team_code = ?", (team_a,))
            purse_a = cursor.fetchone()["purse"]

            cursor.execute("SELECT purse FROM teams WHERE team_code = ?", (team_b,))
            purse_b = cursor.fetchone()["purse"]

            # Calculate total changes for each team
            # Only price difference + compensation affects purses
            change_a = 0
            change_b = 0

            # Add price difference changes
            if price_a > price_b:
                # B pays difference to A
                change_a = price_difference
                change_b = -price_difference
            elif price_b > price_a:
                # A pays difference to B
                change_a = -price_difference
                change_b = price_difference
            # If equal, no price difference transfer

            # Add compensation changes
            if compensation_amount > 0 and compensation_from:
                if compensation_from.upper() == team_a:
                    change_a -= compensation_amount  # A pays
                    change_b += compensation_amount  # B receives
                else:
                    change_a += compensation_amount  # A receives
                    change_b -= compensation_amount  # B pays

            # Check if team A can afford
            if purse_a + change_a < 0:
                return (
                    False,
                    f"{team_a} cannot afford this swap. Would need ₹{abs(purse_a + change_a):,} more.",
                )

            # Check if team B can afford
            if purse_b + change_b < 0:
                return (
                    False,
                    f"{team_b} cannot afford this swap. Would need ₹{abs(purse_b + change_b):,} more.",
                )

            # Remove players from original teams
            cursor.execute(
                "DELETE FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_a, player_a),
            )
            cursor.execute(
                "DELETE FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_b, player_b),
            )

            # Update purses - ONLY transfer the price difference (no original price refunds!)
            if price_difference > 0:
                # Team paying difference loses money
                cursor.execute(
                    "UPDATE teams SET purse = purse - ? WHERE team_code = ?",
                    (price_difference, team_paying_diff),
                )
                # Team receiving difference gains money
                cursor.execute(
                    "UPDATE teams SET purse = purse + ? WHERE team_code = ?",
                    (price_difference, team_receiving_diff),
                )

            # Add players to new teams (keeping their original salaries)
            cursor.execute(
                """INSERT INTO team_squads (team_code, player_name, price, acquisition_type, source_team) 
                   VALUES (?, ?, ?, 'traded', ?)""",
                (
                    team_b,
                    actual_name_a,
                    price_a,
                    team_a,
                ),  # Player A goes to Team B at their salary
            )
            cursor.execute(
                """INSERT INTO team_squads (team_code, player_name, price, acquisition_type, source_team) 
                   VALUES (?, ?, ?, 'traded', ?)""",
                (
                    team_a,
                    actual_name_b,
                    price_b,
                    team_b,
                ),  # Player B goes to Team A at their salary
            )

            # Handle compensation transfer (NOT part of salary cap)
            # Compensation is separate from player salaries - it's cash between teams
            compensation_direction = None
            if compensation_amount > 0 and compensation_from:
                compensation_direction = f"{compensation_from}_pays"

                # Determine which team pays and which receives
                if compensation_from.upper() == team_a:
                    # Team A pays compensation to Team B
                    paying_team = team_a
                    receiving_team = team_b
                else:
                    # Team B pays compensation to Team A
                    paying_team = team_b
                    receiving_team = team_a

                # Transfer compensation (deduct from payer, add to receiver)
                cursor.execute(
                    "UPDATE teams SET purse = purse - ? WHERE team_code = ?",
                    (compensation_amount, paying_team),
                )
                cursor.execute(
                    "UPDATE teams SET purse = purse + ? WHERE team_code = ?",
                    (compensation_amount, receiving_team),
                )

            # Record trade history for player A (going to team B)
            cursor.execute(
                """INSERT INTO trade_history 
                   (player_name, from_team, to_team, trade_price, original_price, trade_type, 
                    swap_player, swap_player_price, compensation_amount, compensation_direction)
                   VALUES (?, ?, ?, ?, ?, 'swap', ?, ?, ?, ?)""",
                (
                    actual_name_a,
                    team_a,
                    team_b,
                    price_a,
                    price_a,
                    actual_name_b,
                    price_b,
                    compensation_amount,
                    compensation_direction,
                ),
            )

            # Record trade history for player B (going to team A)
            cursor.execute(
                """INSERT INTO trade_history 
                   (player_name, from_team, to_team, trade_price, original_price, trade_type, 
                    swap_player, swap_player_price, compensation_amount, compensation_direction)
                   VALUES (?, ?, ?, ?, ?, 'swap', ?, ?, ?, ?)""",
                (
                    actual_name_b,
                    team_b,
                    team_a,
                    price_b,
                    price_b,
                    actual_name_a,
                    price_a,
                    compensation_amount,
                    compensation_direction,
                ),
            )

            return True, "Swap completed successfully"

    def get_player_price_in_squad(self, team: str, player_name: str) -> Optional[int]:
        """Get a player's current price/salary in a team's squad"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT price FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team, player_name),
            )
            row = cursor.fetchone()
            return row["price"] if row else None

    def get_stats_data(self) -> dict:
        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """SELECT player_name, final_price, team_code FROM sales 
                   WHERE player_name NOT LIKE '%(TRADE%' 
                   AND player_name NOT LIKE '%(RELEASED%'
                   AND team_code != 'UNSOLD'
                   ORDER BY final_price DESC LIMIT 1"""
            )
            most_expensive = cursor.fetchone()

            cursor.execute(
                "SELECT team_code, COUNT(*) as cnt FROM team_squads GROUP BY team_code ORDER BY cnt DESC LIMIT 1"
            )
            most_players = cursor.fetchone()

            cursor.execute(
                "SELECT team_code, COUNT(*) as cnt FROM team_squads GROUP BY team_code ORDER BY cnt ASC LIMIT 1"
            )
            least_players = cursor.fetchone()

            cursor.execute("SELECT team_code, purse FROM teams ORDER BY team_code")
            purses = {row["team_code"]: row["purse"] for row in cursor.fetchall()}

            return {
                "most_expensive": dict(most_expensive) if most_expensive else None,
                "most_players": dict(most_players) if most_players else None,
                "least_players": dict(least_players) if least_players else None,
                "purses": purses,
            }

    def get_all_unauctioned_players(self) -> List[Tuple[str, str, Optional[int]]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT player_name, list_name, base_price FROM player_lists WHERE auctioned = 0 ORDER BY list_name, player_name"
            )
            return [
                (row["player_name"], row["list_name"], row["base_price"])
                for row in cursor.fetchall()
            ]

    def get_unsold_players(self) -> List[Tuple[int, str, str, Optional[int]]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT pl.id, pl.player_name, pl.list_name, pl.base_price 
                FROM player_lists pl
                WHERE pl.auctioned = 1 
                AND NOT EXISTS (
                    SELECT 1 FROM team_squads ts 
                    WHERE LOWER(ts.player_name) = LOWER(pl.player_name)
                )
                ORDER BY pl.list_name, pl.player_name
                """
            )
            return [
                (row["id"], row["player_name"], row["list_name"], row["base_price"])
                for row in cursor.fetchall()
            ]

    def get_unsold_players_for_excel(self) -> List[Tuple[str, str, Optional[int]]]:
        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT pl.player_name, pl.list_name, pl.base_price 
                FROM player_lists pl
                WHERE pl.auctioned = 1 
                AND NOT EXISTS (
                    SELECT 1 FROM team_squads ts 
                    WHERE LOWER(ts.player_name) = LOWER(pl.player_name)
                )
                ORDER BY pl.list_name, player_name
                """
            )
            from_player_lists = [
                (row["player_name"], row["list_name"], row["base_price"])
                for row in cursor.fetchall()
            ]

            cursor.execute(
                """
                SELECT player_name, final_price
                FROM sales 
                WHERE team_code = 'UNSOLD'
                ORDER BY sold_at
                """
            )
            from_sales = [
                (row["player_name"], "N/A", row["final_price"])
                for row in cursor.fetchall()
            ]

            seen = set()
            result = []
            for player_name, set_name, base_price in from_player_lists:
                if player_name.lower() not in seen:
                    seen.add(player_name.lower())
                    result.append((player_name, set_name, base_price))

            for player_name, set_name, base_price in from_sales:
                if player_name.lower() not in seen:
                    seen.add(player_name.lower())
                    result.append((player_name, set_name, base_price))

            return result

    def get_released_players_for_excel(self) -> List[Tuple[str, str, Optional[int]]]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT player_name, final_price
                FROM sales 
                WHERE team_code = 'RELEASED'
                ORDER BY sold_at
                """
            )
            return [
                (row["player_name"], "N/A", row["final_price"])
                for row in cursor.fetchall()
            ]

    def get_team_bid_history(self, team_code: str, limit: int = 50) -> List[dict]:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT player_name, amount, timestamp, is_auto_bid, user_name
                FROM bid_history 
                WHERE team_code = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (team_code, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def reauction_multiple_players(self, player_ids: List[int]) -> int:
        with self._transaction() as conn:
            cursor = conn.cursor()
            count = 0
            for pid in player_ids:
                cursor.execute(
                    "UPDATE player_lists SET auctioned = 0 WHERE id = ?", (pid,)
                )
                count += cursor.rowcount
            return count

    # ==================== FULL RESET ====================

    def full_reset(self):
        """Complete reset of all auction data (including retained)"""
        self.reset_auction_state()
        self.reset_teams()
        self.clear_squads()
        self.clear_player_lists()
        self.clear_bid_history()
        self.clear_all_auto_bids()
        self.clear_user_teams()
        self.clear_sales()
        self.clear_trade_history()

    def clear_trade_history(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trade_history")

    def remove_duplicate_players(self):
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM team_squads 
                WHERE id NOT IN (
                    SELECT MIN(id) FROM team_squads 
                    GROUP BY LOWER(player_name)
                )
            """
            )
            removed = cursor.rowcount
            return removed

    def move_player_to_set(self, player_name: str, target_set: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM player_lists WHERE LOWER(player_name) = LOWER(?)",
                (player_name,),
            )
            if not cursor.fetchone():
                return False

            cursor.execute(
                "INSERT OR IGNORE INTO list_order (position, list_name) VALUES ((SELECT COALESCE(MAX(position), 0) + 1 FROM list_order), ?)",
                (target_set.lower(),),
            )

            cursor.execute(
                "UPDATE player_lists SET list_name = ? WHERE LOWER(player_name) = LOWER(?)",
                (target_set.lower(), player_name),
            )
            return cursor.rowcount > 0

    # ==================== ATOMIC SALE OPERATIONS ====================

    def finalize_sale_atomic(
        self, player_name: str, team_code: str, amount: int, bid_count: int
    ) -> Tuple[bool, str]:
        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT team_code FROM team_squads WHERE LOWER(player_name) = LOWER(?)",
                (player_name,),
            )
            existing = cursor.fetchone()
            if existing:
                return False, f"Player already sold to {existing['team_code']}"

            cursor.execute(
                "UPDATE teams SET purse = purse - ? WHERE team_code = ? AND purse >= ?",
                (amount, team_code, amount),
            )
            if cursor.rowcount == 0:
                return False, "Insufficient purse"

            try:
                cursor.execute(
                    """INSERT INTO team_squads (team_code, player_name, price, acquisition_type) 
                       VALUES (?, ?, ?, 'bought')""",
                    (team_code, player_name, amount),
                )
            except sqlite3.IntegrityError:
                return False, "Player already exists in a squad"

            cursor.execute(
                """INSERT INTO sales (player_name, team_code, final_price, total_bids)
                   VALUES (?, ?, ?, ?)""",
                (player_name, team_code, amount, bid_count),
            )

            return True, "Sale completed successfully"

    def record_unsold_atomic(self, player_name: str, base_price: int) -> bool:
        with self._transaction() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT team_code FROM team_squads WHERE LOWER(player_name) = LOWER(?)",
                (player_name,),
            )
            if cursor.fetchone():
                return False

            cursor.execute(
                """INSERT INTO sales (player_name, team_code, final_price, total_bids)
                   VALUES (?, 'UNSOLD', ?, 0)""",
                (player_name, base_price),
            )

            return True

    # ==================== NEW: BASE PRICE MANAGEMENT ====================
    def change_base_price_for_players(
        self, player_names: List[str], new_price: int
    ) -> Tuple[int, List[str]]:
        """
        Change base_price for a list of player names (case-insensitive exact match).
        Returns (updated_count, not_found_list)
        """
        updated = 0
        not_found = []
        with self._transaction() as conn:
            cursor = conn.cursor()
            for name in player_names:
                cursor.execute(
                    "UPDATE player_lists SET base_price = ? WHERE LOWER(player_name) = LOWER(?)",
                    (new_price, name),
                )
                if cursor.rowcount == 0:
                    # Could be that player is in sales (RELEASED/UNSOLD) and not in player_lists
                    not_found.append(name)
                else:
                    updated += cursor.rowcount
        return updated, not_found

    def change_base_price_for_list(self, list_name: str, new_price: int) -> int:
        """
        Change base_price for all players in a given list_name (case-insensitive).
        Returns number of rows updated.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET base_price = ? WHERE LOWER(list_name) = LOWER(?)",
                (new_price, list_name),
            )
            return cursor.rowcount
