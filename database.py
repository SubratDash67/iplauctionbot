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
        """Get a database connection with row factory"""
        conn = sqlite3.connect(self.db_path)
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
                    bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (team_code) REFERENCES teams(team_code)
                )
            """
            )

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

            # Migration: Add enabled column if it doesn't exist
            try:
                cursor.execute(
                    "ALTER TABLE list_order ADD COLUMN enabled INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

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
                    stats_channel_id INTEGER DEFAULT 0,
                    stats_message_id INTEGER DEFAULT 0
                )
            """
            )

            # Initialize auction state if not exists
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

    # ==================== TEAM OPERATIONS ====================

    def init_teams(self, teams: Dict[str, int]):
        """Initialize teams with purses"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM teams")
            for team_code, purse in teams.items():
                cursor.execute(
                    "INSERT INTO teams (team_code, purse, original_purse) VALUES (?, ?, ?)",
                    (team_code, purse, purse),
                )

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

    # ==================== TEAM SQUAD OPERATIONS ====================

    def add_to_squad(self, team_code: str, player_name: str, price: int):
        """Add a player to team's squad"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO team_squads (team_code, player_name, price) VALUES (?, ?, ?)",
                (team_code, player_name, price),
            )

    def get_team_squad(self, team_code: str) -> List[Tuple[str, int]]:
        """Get all players in a team's squad"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT player_name, price FROM team_squads WHERE team_code = ? ORDER BY bought_at",
                (team_code,),
            )
            return [(row["player_name"], row["price"]) for row in cursor.fetchall()]

    def get_all_squads(self) -> Dict[str, List[Tuple[str, int]]]:
        """Get all team squads"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT team_code FROM teams")
            teams = [row["team_code"] for row in cursor.fetchall()]

            squads = {}
            for team in teams:
                squads[team] = self.get_team_squad(team)
            return squads

    def clear_squads(self):
        """Clear all squad data"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM team_squads")

    # ==================== PLAYER LIST OPERATIONS ====================

    def create_list(self, list_name: str) -> bool:
        """Create a new player list (preserve original list_name case)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM player_lists WHERE LOWER(list_name) = LOWER(?)",
                (list_name,),
            )
            if cursor.fetchone()["cnt"] > 0:
                return False

            # Add to list order preserving the provided name
            cursor.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 as next_pos FROM list_order"
            )
            next_pos = cursor.fetchone()["next_pos"]
            cursor.execute(
                "INSERT OR IGNORE INTO list_order (position, list_name) VALUES (?, ?)",
                (next_pos, list_name),
            )
            return True

    def add_player_to_list(
        self, list_name: str, player_name: str, base_price: Optional[int] = None
    ) -> bool:
        """Add a player to a list"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO player_lists (list_name, player_name, base_price) VALUES (?, ?, ?)",
                (list_name, player_name, base_price),
            )
            return True

    def add_players_to_list(
        self, list_name: str, players: List[Tuple[str, Optional[int]]]
    ):
        """Add multiple players to a list"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            for player_name, base_price in players:
                cursor.execute(
                    "INSERT INTO player_lists (list_name, player_name, base_price) VALUES (?, ?, ?)",
                    (list_name, player_name, base_price),
                )

    def get_player_lists(self) -> Dict[str, List[Tuple[str, Optional[int]]]]:
        """Get all player lists with unauctioned players"""
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

    def get_list_order(self) -> List[str]:
        """Get the order of lists (returns names as stored - preserves case)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT list_name FROM list_order ORDER BY position")
            return [row["list_name"] for row in cursor.fetchall()]

    def set_list_order(self, order: List[str]) -> bool:
        """Set the order of lists (all disabled by default)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM list_order")
            for i, list_name in enumerate(order):
                cursor.execute(
                    "INSERT INTO list_order (position, list_name, enabled) VALUES (?, ?, 0)",
                    (i, list_name),
                )
            return True

    def mark_player_auctioned(self, list_name: str, player_name: str):
        """Mark a player as auctioned"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET auctioned = 1 WHERE list_name = ? AND player_name = ? AND auctioned = 0",
                (list_name, player_name),
            )

    def get_random_player_from_list(
        self, list_name: str
    ) -> Optional[Tuple[int, str, Optional[int]]]:
        """Get a random unauctioned player from a list"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, player_name, base_price FROM player_lists WHERE list_name = ? AND auctioned = 0 ORDER BY RANDOM() LIMIT 1",
                (list_name,),
            )
            row = cursor.fetchone()
            if row:
                return (row["id"], row["player_name"], row["base_price"])
            return None

    def mark_player_auctioned_by_id(self, player_id: int):
        """Mark a specific player as auctioned by ID"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE player_lists SET auctioned = 1 WHERE id = ?", (player_id,)
            )

    def clear_player_lists(self):
        """Clear all player lists"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM player_lists")
            cursor.execute("DELETE FROM list_order")

    # ==================== SET ENABLE/DISABLE OPERATIONS ====================

    def enable_sets(self, set_names: List[str]) -> int:
        """Enable specific sets for auction. Returns count of sets enabled."""
        with self._transaction() as conn:
            cursor = conn.cursor()
            count = 0
            for set_name in set_names:
                cursor.execute(
                    "UPDATE list_order SET enabled = 1 WHERE LOWER(list_name) = LOWER(?)",
                    (set_name,),
                )
                count += cursor.rowcount
            return count

    def disable_all_sets(self):
        """Disable all sets"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE list_order SET enabled = 0")

    def get_enabled_sets(self) -> List[str]:
        """Get list of enabled set names in order"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT list_name FROM list_order WHERE enabled = 1 ORDER BY position"
            )
            return [row["list_name"] for row in cursor.fetchall()]

    def get_all_sets_with_status(self) -> List[Tuple[str, bool, int]]:
        """Get all sets with their enabled status and unauctioned player count.
        Returns list of (set_name, enabled, remaining_players)"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT lo.list_name, lo.enabled,
                       (SELECT COUNT(*) FROM player_lists pl 
                        WHERE pl.list_name = lo.list_name AND pl.auctioned = 0) as remaining
                FROM list_order lo
                ORDER BY lo.position
                """
            )
            return [
                (row["list_name"], bool(row["enabled"]), row["remaining"])
                for row in cursor.fetchall()
            ]

    def has_unauctioned_players_in_enabled_sets(self) -> bool:
        """Check if any enabled set has unauctioned players"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) as cnt FROM player_lists pl
                JOIN list_order lo ON pl.list_name = lo.list_name
                WHERE lo.enabled = 1 AND pl.auctioned = 0
                """
            )
            row = cursor.fetchone()
            return row["cnt"] > 0 if row else False

    # ==================== ADMIN LIST OPERATIONS ====================

    def delete_list(self, list_name: str) -> int:
        """
        Delete a list (all players in it) and remove from list_order.
        Returns number of player rows deleted.
        """
        with self._transaction() as conn:
            cursor = conn.cursor()
            # Delete player rows (case-insensitive)
            cursor.execute(
                "DELETE FROM player_lists WHERE LOWER(list_name) = LOWER(?)", (list_name,)
            )
            deleted = cursor.rowcount
            # Remove from list order
            cursor.execute(
                "DELETE FROM list_order WHERE LOWER(list_name) = LOWER(?)", (list_name,)
            )
            return deleted

    # ==================== AUCTION STATE OPERATIONS ====================

    def get_auction_state(self) -> dict:
        """Get current auction state"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM auction_state WHERE id = 1")
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {}

    def update_auction_state(self, **kwargs):
        """Update auction state fields"""
        if not kwargs:
            return
        with self._transaction() as conn:
            cursor = conn.cursor()
            fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
            values = list(kwargs.values())
            cursor.execute(f"UPDATE auction_state SET {fields} WHERE id = 1", values)

    def reset_auction_state(self):
        """Reset auction state to defaults"""
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
                    last_bid_time = 0,
                    stats_channel_id = 0,
                    stats_message_id = 0
                WHERE id = 1
            """
            )

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
        """Record a bid in history"""
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
        """Get all bids for a specific player"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM bid_history WHERE player_name = ? ORDER BY timestamp",
                (player_name,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_bids(self, limit: int = 10) -> List[dict]:
        """Get most recent bids"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM bid_history ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_bids_for_player(self, player_name: str) -> int:
        """Count total bids for a player"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM bid_history WHERE player_name = ?",
                (player_name,),
            )
            return cursor.fetchone()["cnt"]

    def clear_bid_history(self):
        """Clear all bid history"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bid_history")

    # ==================== AUTO-BID OPERATIONS ====================

    def set_auto_bid(self, team_code: str, max_amount: int, user_id: int):
        """Set or update auto-bid for a team"""
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
        """Get active auto-bid max for a team"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT max_amount FROM auto_bids WHERE team_code = ? AND active = 1",
                (team_code,),
            )
            row = cursor.fetchone()
            return row["max_amount"] if row else None

    def get_all_auto_bids(self) -> Dict[str, int]:
        """Get all active auto-bids"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT team_code, max_amount FROM auto_bids WHERE active = 1"
            )
            return {row["team_code"]: row["max_amount"] for row in cursor.fetchall()}

    def clear_auto_bid(self, team_code: str):
        """Deactivate auto-bid for a team"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE auto_bids SET active = 0 WHERE team_code = ?", (team_code,)
            )

    def clear_all_auto_bids(self):
        """Clear all auto-bids"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM auto_bids")

    # ==================== USER-TEAM MAPPING ====================

    def set_user_team(self, user_id: int, team_code: str, user_name: str = None):
        """Assign user to team"""
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
        """Get team for a user"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT team_code FROM user_teams WHERE user_id = ?", (user_id,)
            )
            row = cursor.fetchone()
            return row["team_code"] if row else None

    def get_all_user_teams(self) -> Dict[int, str]:
        """Get all user-team mappings"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, team_code FROM user_teams")
            return {row["user_id"]: row["team_code"] for row in cursor.fetchall()}

    def remove_user_team(self, user_id: int) -> bool:
        """Remove user's team assignment"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_teams WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    def clear_user_teams(self):
        """Clear all user-team mappings"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_teams")

    # ==================== SALES OPERATIONS ====================

    def record_sale(
        self, player_name: str, team_code: str, final_price: int, total_bids: int = 0
    ):
        """Record a finalized sale"""
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
        """Get all sales"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sales ORDER BY sold_at")
            return [dict(row) for row in cursor.fetchall()]

    def get_last_sale(self) -> Optional[dict]:
        """Get the most recent sale"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sales ORDER BY sold_at DESC LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    def rollback_last_sale(self) -> Optional[dict]:
        """Rollback the last sale - returns the sale data"""
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
                "DELETE FROM team_squads WHERE team_code = ? AND player_name = ? ORDER BY bought_at DESC LIMIT 1",
                (sale["team_code"], sale["player_name"]),
            )

            # Delete sale record
            cursor.execute("DELETE FROM sales WHERE id = ?", (sale["id"],))

            return sale

    def clear_sales(self):
        """Clear all sales"""
        with self._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sales")

    # ==================== FULL RESET ====================

    def full_reset(self):
        """Complete reset of all auction data"""
        self.reset_auction_state()
        self.reset_teams()
        self.clear_squads()
        self.clear_player_lists()
        self.clear_bid_history()
        self.clear_all_auto_bids()
        self.clear_user_teams()
        self.clear_sales()