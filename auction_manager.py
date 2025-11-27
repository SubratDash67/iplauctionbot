"""
Auction Manager Module - v2.1
Handles all auction-related logic with atomic operations, auto-bid, and anti-sniping
"""

import asyncio
import time
import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from config import (
    DEFAULT_BASE_PRICE,
    DEFAULT_COUNTDOWN,
    NO_BID_TIMEOUT,
    NO_START_TIMEOUT,
    PLAYER_GAP,
    LIST_GAP,
    DEFAULT_CSV_FILE,
    TEAMS,
    get_bid_increment,
)
from database import Database
from utils import FileManager, MessageFormatter, format_amount
from retained_players import RETAINED_PLAYERS, get_remaining_purse


@dataclass
class BidResult:
    """Result of a bid attempt"""

    success: bool
    message: str
    amount: int = 0
    team: str = ""
    is_auto_bid: bool = False
    auto_bids_triggered: List[dict] = field(default_factory=list)
    original_bid_amount: int = 0


class AuctionManager:
    """
    Main auction management class with atomic operations.
    """

    def __init__(
        self, teams: Dict[str, int], excel_file: str, db_path: str = "auction.db"
    ):
        self.db = Database(db_path)
        self.excel_file = excel_file
        self.file_manager = FileManager()
        self.formatter = MessageFormatter()

        # Initialize teams with retained players deducted - ONLY if no existing teams
        adjusted_teams = {}
        for team_code, initial_purse in teams.items():
            remaining = get_remaining_purse(team_code, initial_purse)
            adjusted_teams[team_code] = remaining

        # Only initialize teams if database is empty (preserves data across bot restarts)
        teams_initialized = self.db.init_teams_if_empty(adjusted_teams)

        # Add retained players to squads (only if teams were just initialized or squads are empty)
        if teams_initialized:
            self._initialize_retained_players()
        else:
            # Still check and add any missing retained players
            self._initialize_retained_players()

        self._bid_lock = asyncio.Lock()

        # In-memory state (synced with DB)
        self._load_state_from_db()

        # Initialize Excel
        try:
            self.file_manager.initialize_excel(self.excel_file)
        except Exception as e:
            logger.warning(f"Could not initialize Excel file: {e}")

        self._auto_load_csv_players()

    def _initialize_retained_players(self):
        existing_squads = self.db.get_all_squads()

        for team_code, players in RETAINED_PLAYERS.items():
            existing_players = set()
            if team_code in existing_squads:
                existing_players = {
                    name.lower() for name, _ in existing_squads[team_code]
                }

            for player_name, salary in players:
                if player_name.lower() not in existing_players:
                    self.db.add_to_squad(team_code, player_name, salary)

    def _auto_load_csv_players(self):
        pass

    def _load_ipl_csv(
        self, filepath: str, max_set: Optional[int] = None
    ) -> Tuple[bool, str]:
        """Robust loader for IPL CSVs with Set Name support. Supports incremental loading."""
        import csv

        try:
            # Get existing lists to avoid duplicates
            existing_lists = self.db.get_player_lists()
            existing_set_names = set(existing_lists.keys()) if existing_lists else set()

            players_by_set = {}
            set_number_map = {}  # Stores {list_name: set_number} for sorting
            row_count = 0
            skipped_count = 0
            header = None
            header_map = {}

            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                for raw_row in reader:
                    row_count += 1
                    row = [cell.strip() if cell is not None else "" for cell in raw_row]

                    if not any(cell for cell in row):
                        skipped_count += 1
                        continue

                    # Header Detection
                    if header is None:
                        low_cells = [c.lower() for c in row if c]
                        if (
                            any("first name" in c for c in low_cells)
                            or any("list sr" in c for c in low_cells)
                            or any("2025 set" in c for c in low_cells)
                        ):
                            header = row
                            for i, col in enumerate(header):
                                if not col:
                                    continue
                                key = col.lower().strip()
                                key = re.sub(r"[\s\._\-]+", "", key)
                                header_map[key] = i
                            continue
                        else:
                            skipped_count += 1
                            continue

                    # Skip repeated headers
                    first_cell = row[0].lower() if row and row[0] else ""
                    if any(
                        h in first_cell
                        for h in (
                            "list sr.no",
                            "list sr.no.",
                            "list sr",
                            "first name",
                            "tata ipl",
                            "auction list",
                        )
                    ):
                        skipped_count += 1
                        continue

                    # Helper to get column
                    def get_col(*names):
                        for name in names:
                            n = re.sub(r"[\s\._\-]+", "", name.lower())
                            idx = header_map.get(n)
                            if idx is not None and idx < len(row):
                                return row[idx]
                        return ""

                    # Extract fields
                    first_name = get_col("First Name", "Firstname", "Player Name")
                    surname = get_col("Surname", "Last Name", "Lastname")
                    set_no_str = get_col("Set No.", "Set No", "SetNo", "Set")
                    set_name_str = get_col(
                        "Set Name", "SetName", "Set Desc", "Definition"
                    )
                    base_price_str = get_col(
                        "Base Price", "BasePrice", "Base Price (Lakh)", "Baseprice"
                    )
                    list_sr_no = get_col(
                        "List Sr.No.",
                        "List Sr.No",
                        "List Sr No",
                        "ListSrNo",
                        "Sr.No.",
                        "Sr. No.",
                    )

                    # Validation
                    if not first_name:
                        skipped_count += 1
                        continue

                    fn_lower = first_name.lower()
                    if (
                        fn_lower in ("first name", "player name", "name")
                        or "tata ipl" in fn_lower
                        or "auction list" in fn_lower
                    ):
                        skipped_count += 1
                        continue

                    if first_name.replace(".", "").replace(" ", "").isdigit():
                        skipped_count += 1
                        continue

                    if not set_no_str:
                        skipped_count += 1
                        continue

                    # Parse Set Number (Required for ordering/filtering)
                    try:
                        set_number = int(float(set_no_str.replace(",", "")))
                    except (ValueError, TypeError):
                        skipped_count += 1
                        continue

                    if max_set is not None and set_number > max_set:
                        skipped_count += 1
                        continue

                    # Parse ID
                    if not list_sr_no:
                        skipped_count += 1
                        continue
                    try:
                        float(list_sr_no.replace(",", ""))
                    except Exception:
                        skipped_count += 1
                        continue

                    player_name = f"{first_name} {surname}".strip()

                    # Parse Price
                    base_price = DEFAULT_BASE_PRICE
                    if base_price_str:
                        try:
                            clean = re.sub(r"[^\d\.]", "", base_price_str)
                            if clean:
                                val = float(clean)
                                if val < 10000:
                                    base_price = int(val * 100000)
                                else:
                                    base_price = int(val)
                        except Exception:
                            base_price = DEFAULT_BASE_PRICE

                    # DETERMINE SET NAME (KEY)
                    # Use 2025 Set column value (like M1, M2, BA1) as set name
                    set_2025_str = get_col("2025 Set", "2025Set", "2025set")
                    if set_2025_str:
                        set_key = set_2025_str.strip()
                    elif set_name_str:
                        set_key = set_name_str.strip()
                    else:
                        set_key = f"Set {set_number}"

                    # Skip if this set already exists with players
                    if set_key.lower() in existing_set_names:
                        skipped_count += 1
                        continue

                    # Group players
                    if set_key not in players_by_set:
                        players_by_set[set_key] = []
                        set_number_map[set_key] = (
                            set_number  # Map name to number for sorting
                        )
                        try:
                            self.db.create_list(set_key)
                        except Exception:
                            pass

                    players_by_set[set_key].append((player_name, base_price))

            logger.info(
                f"CSV Parsing Summary: {row_count} rows read, {skipped_count} skipped, {len(players_by_set)} new sets"
            )
            if max_set:
                logger.info(f"Max set filter: 1 to {max_set}")
            if existing_set_names:
                logger.info(f"Existing sets skipped: {len(existing_set_names)}")

            if not players_by_set:
                return (
                    True,
                    f"No new sets to load (sets 1-{max_set} already loaded). Use a higher number or /clear to reload.",
                )

            total_players = 0
            for set_name, players in players_by_set.items():
                self.db.add_players_to_list(set_name, players)
                total_players += len(players)
                logger.debug(f"  {set_name}: {len(players)} players")

            # Update list order to include new sets (merge with existing)
            if players_by_set:
                # Get existing order
                existing_order = self.db.get_list_order()
                existing_order_set = set(existing_order)

                # Add new sets sorted by their set number
                new_sets_sorted = sorted(
                    players_by_set.keys(), key=lambda k: set_number_map.get(k, 999)
                )

                # Merge: keep existing order, append new sets
                final_order = existing_order + [
                    s for s in new_sets_sorted if s.lower() not in existing_order_set
                ]
                self.db.set_list_order(final_order)

            max_set_msg = f" (sets 1-{max_set})" if max_set else ""
            existing_msg = (
                f" ({len(existing_set_names)} existing sets kept)"
                if existing_set_names
                else ""
            )
            return (
                True,
                f"Loaded {total_players} players from {len(players_by_set)} NEW sets{max_set_msg}{existing_msg}",
            )

        except FileNotFoundError:
            return False, f"CSV file not found: {filepath}"
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            logger.error(f"Error loading CSV: {error_details}")
            return False, f"Error loading CSV: {str(e)}"

    def _load_state_from_db(self):
        state = self.db.get_auction_state()
        self.active = bool(state.get("active", 0))
        self.paused = bool(state.get("paused", 0))
        self.current_player = state.get("current_player")
        self.current_list_index = state.get("current_list_index", 0)
        self.base_price = state.get("base_price", DEFAULT_BASE_PRICE)
        self.current_bid = state.get("current_bid", 0)
        self.highest_bidder = state.get("highest_bidder")
        self.countdown_seconds = state.get("countdown_seconds", DEFAULT_COUNTDOWN)
        self.stats_channel_id = state.get("stats_channel_id", 0)
        self.stats_message_id = state.get("stats_message_id", 0)

        db_last_bid_time = state.get("last_bid_time", 0)
        if db_last_bid_time and db_last_bid_time > 0:
            self.last_bid_time = db_last_bid_time
        else:
            self.last_bid_time = time.time()

        self.countdown_remaining = self.countdown_seconds

    def _save_state_to_db(self):
        self.db.update_auction_state(
            active=1 if self.active else 0,
            paused=1 if self.paused else 0,
            current_player=self.current_player,
            current_list_index=self.current_list_index,
            base_price=self.base_price,
            current_bid=self.current_bid,
            highest_bidder=self.highest_bidder,
            countdown_seconds=self.countdown_seconds,
            last_bid_time=self.last_bid_time,
            stats_channel_id=self.stats_channel_id,
            stats_message_id=self.stats_message_id,
        )

    @property
    def teams(self) -> Dict[str, int]:
        return self.db.get_teams()

    @property
    def team_squads(self) -> Dict[str, List[Tuple[str, int]]]:
        return self.db.get_all_squads()

    @property
    def player_lists(self) -> Dict[str, List[Tuple[str, Optional[int]]]]:
        return self.db.get_player_lists()

    @property
    def list_order(self) -> List[str]:
        return self.db.get_list_order()

    # ==================== LIST MANAGEMENT ====================

    def create_list(self, list_name: str) -> bool:
        return self.db.create_list(list_name.lower())

    def add_player_to_list(
        self, list_name: str, player: Tuple[str, Optional[int]]
    ) -> bool:
        player_name, base_price = player
        return self.db.add_player_to_list(list_name.lower(), player_name, base_price)

    def load_list_from_csv(self, list_name: str, filepath: str) -> Tuple[bool, str]:
        list_name = list_name.lower()
        try:
            players = self.file_manager.load_players_from_csv(filepath)
            if not players:
                return False, "No players found in CSV file"

            self.db.create_list(list_name)
            self.db.add_players_to_list(list_name, players)
            return True, f"Loaded {len(players)} players into {list_name}"
        except Exception as e:
            return False, str(e)

    def load_players_from_sets(
        self, max_set: int, filepath: str = None
    ) -> Tuple[bool, str]:
        import os

        if filepath is None:
            filepath = os.path.join(os.path.dirname(__file__), DEFAULT_CSV_FILE)

        if not os.path.exists(filepath):
            return False, f"CSV file not found: {filepath}"

        if max_set < 1 or max_set > 79:
            return False, "max_set must be between 1 and 79"

        return self._load_ipl_csv(filepath, max_set=max_set)

    def set_list_order(self, order: List[str]) -> Tuple[bool, str]:
        order_lower = [name.lower() for name in order]
        player_lists = self.db.get_player_lists()
        for list_name in order_lower:
            if list_name not in player_lists:
                return False, f"List '{list_name}' does not exist"
        self.db.set_list_order(order_lower)
        return True, "List order updated successfully"

    def get_list_info(self) -> str:
        return self.formatter.format_list_display(self.player_lists, self.list_order)

    def delete_set(self, set_name: str) -> Tuple[bool, str]:
        """Delete a set/list and all its players from the auction"""
        set_name_lower = set_name.lower()
        return self.db.delete_set(set_name_lower)

    # ==================== AUCTION CONTROL ====================

    def start_auction(self) -> Tuple[bool, str]:
        if self.active:
            return False, "Auction is already running"

        player_lists = self.db.get_player_lists()
        if not player_lists:
            return False, "No player lists available"

        list_order = self.db.get_list_order()
        if not list_order:
            self.db.set_list_order(list(player_lists.keys()))

        self.active = True
        self.paused = False
        self.current_list_index = 0
        self._save_state_to_db()
        return True, "Auction started successfully"

    def stop_auction(self) -> bool:
        if not self.active:
            return False
        self._reset_state()
        return True

    def pause_auction(self) -> bool:
        if not self.active or self.paused:
            return False
        self.paused = True
        self._save_state_to_db()
        return True

    def resume_auction(self) -> bool:
        if not self.active or not self.paused:
            return False
        self.paused = False
        self.last_bid_time = time.time()  # Reset timestamp on resume
        self._save_state_to_db()
        return True

    def _reset_state(self):
        self.active = False
        self.paused = False
        self.current_player = None
        self.current_list_index = 0
        self.base_price = DEFAULT_BASE_PRICE
        self.current_bid = 0
        self.highest_bidder = None
        self.last_bid_time = time.time()
        self.stats_channel_id = 0
        self.stats_message_id = 0
        self.db.reset_auction_state()

    def _reset_player_state(self):
        self.current_player = None
        self.current_bid = 0
        self.highest_bidder = None
        self.last_bid_time = time.time()
        self.db.clear_all_auto_bids()

    # ==================== PLAYER MANAGEMENT ====================

    def reset_last_bid_time(self):
        """Resets the last bid time to now."""
        self.last_bid_time = time.time()
        self._save_state_to_db()

    def get_next_player(self) -> Tuple[bool, str, Optional[int], bool]:
        """
        Get the next player. Safely handles state to avoid re-auctioning active player.
        """
        # CRITICAL FIX: If resume called while player is active, return that player
        # This prevents "Double Sold" bug on resume
        if self.current_player:
            # Check if this player is already in a squad (sold)
            squads = self.db.get_all_squads()
            is_sold = False
            for squad in squads.values():
                for pname, _ in squad:
                    if pname.lower() == self.current_player.lower():
                        is_sold = True
                        break

            if not is_sold:
                # Player is active but not sold - just continue with them
                return (True, self.current_player, self.base_price, False)
            else:
                # Player was marked sold in DB but state wasn't cleared. Clear and move next.
                self._reset_player_state()

        list_order = self.db.get_list_order()

        auctioned_count = self.db.get_auctioned_count()
        is_start_of_auction = auctioned_count == 0

        old_list_index = self.current_list_index
        list_advanced = False

        while self.current_list_index < len(list_order):
            current_list = list_order[self.current_list_index]

            if self.current_list_index > old_list_index:
                list_advanced = True

            player_data = self.db.get_random_player_from_list(current_list)
            if player_data:
                player_id, player_name, base_price = player_data
                self.db.mark_player_auctioned_by_id(player_id)

                self._reset_player_state()
                self.current_player = player_name
                self.base_price = (
                    base_price if base_price is not None else DEFAULT_BASE_PRICE
                )
                self.current_bid = self.base_price
                self._save_state_to_db()

                is_first_in_list = list_advanced or is_start_of_auction
                return (True, player_name, self.base_price, is_first_in_list)
            else:
                self.current_list_index += 1
                self._save_state_to_db()
                list_advanced = True

        return (False, None, None, False)

    def get_current_list_name(self) -> Optional[str]:
        list_order = self.db.get_list_order()
        if 0 <= self.current_list_index < len(list_order):
            return list_order[self.current_list_index]
        return None

    # ==================== ATOMIC BIDDING ====================

    async def place_bid(
        self, team: str, user_id: int, user_name: str = None, interaction_id: str = None
    ) -> BidResult:
        async with self._bid_lock:
            return await self._place_bid_internal(
                team, user_id, user_name, interaction_id, is_auto=False
            )

    async def _place_bid_internal(
        self,
        team: str,
        user_id: int,
        user_name: str = None,
        interaction_id: str = None,
        is_auto: bool = False,
    ) -> BidResult:

        if not self.active:
            return BidResult(False, "Auction is not active")

        if self.paused:
            return BidResult(False, "Auction is paused - bidding not allowed")

        if not self.current_player:
            return BidResult(False, "No player is currently being auctioned")

        # CRITICAL: Check if player is already sold (prevents double-sell bug)
        squads = self.db.get_all_squads()
        for squad in squads.values():
            for pname, _ in squad:
                if pname.lower() == self.current_player.lower():
                    return BidResult(False, "This player has already been sold")

        team_upper = team.upper()
        teams = self.db.get_teams()
        if team_upper not in teams:
            return BidResult(False, "Invalid team name")

        if self.highest_bidder is None:
            min_bid = self.base_price
        else:
            min_bid = self.current_bid + get_bid_increment(self.current_bid)

        if teams[team_upper] < min_bid:
            return BidResult(
                False,
                f"Insufficient purse. Need {format_amount(min_bid)}, have {format_amount(teams[team_upper])}",
            )

        timestamp = time.time()

        self.current_bid = min_bid
        self.highest_bidder = team_upper
        self.last_bid_time = timestamp

        self.db.record_bid(
            player_name=self.current_player,
            team_code=team_upper,
            user_id=user_id,
            user_name=user_name,
            amount=min_bid,
            timestamp=timestamp,
            is_auto_bid=is_auto,
            interaction_id=interaction_id,
        )

        self._save_state_to_db()

        auto_bids_triggered = await self._process_auto_bids(team_upper, user_id)

        return BidResult(
            success=True,
            message="Bid placed successfully",
            amount=min_bid if not auto_bids_triggered else self.current_bid,
            team=self.highest_bidder,
            is_auto_bid=is_auto,
            auto_bids_triggered=auto_bids_triggered,
            original_bid_amount=min_bid,
        )

    def undo_last_bid(self) -> Tuple[bool, str]:
        """Undo the last bid on the current player"""
        if not self.active or not self.current_player:
            return False, "No active player auction."

        # Get history
        history = self.db.get_bid_history_for_player(self.current_player)
        if not history:
            return False, "No bids to undo."

        # Remove last bid
        self.db.delete_last_bid(self.current_player)

        # Get new state
        previous = self.db.get_previous_bid(self.current_player)

        if previous:
            self.current_bid = previous["amount"]
            self.highest_bidder = previous["team_code"]
            self.last_bid_time = previous["timestamp"]
        else:
            # Back to base price
            self.current_bid = self.base_price
            self.highest_bidder = None
            self.last_bid_time = time.time()

        self._save_state_to_db()
        return (
            True,
            f"Undo successful. Current bid: {format_amount(self.current_bid)} by {self.highest_bidder or 'None'}",
        )

    # ==================== TRADING & MANUAL ====================
    def trade_player(
        self, player_name: str, from_team: str, to_team: str, price: int
    ) -> Tuple[bool, str]:
        """Trade player between teams"""
        success = self.db.trade_player(
            player_name, from_team.upper(), to_team.upper(), price
        )
        if success:
            return (
                True,
                f"Traded **{player_name}** from **{from_team}** to **{to_team}** for {format_amount(price)}",
            )
        return False, "Trade failed. Check if player exists in source team."

    def manual_add_player(self, team: str, player: str, price: int) -> Tuple[bool, str]:
        """Manually add a player to a squad"""
        team_upper = team.upper()
        teams = self.db.get_teams()

        if team_upper not in teams:
            return False, "Invalid team."

        if not self.db.deduct_from_purse(team_upper, price):
            return False, "Insufficient purse."

        self.db.add_to_squad(team_upper, player, price)
        return (
            True,
            f"Added **{player}** to **{team_upper}** for {format_amount(price)}",
        )

    # ==================== AUTO-BID (PROXY BIDDING) ====================

    def set_auto_bid(
        self, team: str, max_amount: int, user_id: int
    ) -> Tuple[bool, str]:
        team_upper = team.upper()
        teams = self.db.get_teams()

        if team_upper not in teams:
            return False, "Invalid team name"

        if max_amount > teams[team_upper]:
            return (
                False,
                f"Auto-bid max ({format_amount(max_amount)}) exceeds purse ({format_amount(teams[team_upper])})",
            )

        if self.current_player and max_amount < self.current_bid:
            return (
                False,
                f"Auto-bid max must be at least current bid ({format_amount(self.current_bid)})",
            )

        self.db.set_auto_bid(team_upper, max_amount, user_id)
        return True, f"Auto-bid set for {team_upper}: max {format_amount(max_amount)}"

    def clear_auto_bid(self, team: str) -> Tuple[bool, str]:
        team_upper = team.upper()
        self.db.clear_auto_bid(team_upper)
        return True, f"Auto-bid cleared for {team_upper}"

    def get_auto_bid(self, team: str) -> Optional[int]:
        return self.db.get_auto_bid(team.upper())

    async def _process_auto_bids(
        self, excluded_team: str, excluded_user: int
    ) -> List[dict]:
        auto_bids_triggered = []
        auto_bids = self.db.get_all_auto_bids()
        teams = self.db.get_teams()

        iterations = 0
        max_iterations = 100

        while iterations < max_iterations:
            iterations += 1
            next_bid = self.current_bid + get_bid_increment(self.current_bid)

            eligible = []
            for team, max_amount in auto_bids.items():
                if team == self.highest_bidder:
                    continue
                if max_amount >= next_bid and teams.get(team, 0) >= next_bid:
                    eligible.append((team, max_amount))

            if not eligible:
                break

            eligible.sort(key=lambda x: (-x[1], x[0]))
            winning_team, _ = eligible[0]

            timestamp = time.time()
            self.current_bid = next_bid
            self.highest_bidder = winning_team
            self.last_bid_time = time.time()

            self.db.record_bid(
                player_name=self.current_player,
                team_code=winning_team,
                user_id=0,
                user_name="AUTO-BID",
                amount=next_bid,
                timestamp=timestamp,
                is_auto_bid=True,
            )

            auto_bids_triggered.append({"team": winning_team, "amount": next_bid})
            teams = self.db.get_teams()

        if auto_bids_triggered:
            self._save_state_to_db()

        return auto_bids_triggered

    # ==================== RE-AUCTION UNSOLD PLAYERS ====================

    def reauction_player(self, player_name: str) -> Tuple[bool, str]:
        player_data = self.db.find_player_by_name(player_name)

        if not player_data:
            return False, f"Player **{player_name}** not found in auction database."

        player_id, actual_name, list_name, base_price, auctioned = player_data

        if auctioned == 0:
            return (
                False,
                f"**{actual_name}** is already in the auction pool (not yet auctioned).",
            )

        squads = self.db.get_all_squads()
        for team, squad in squads.items():
            for name, _ in squad:
                if name.lower() == actual_name.lower():
                    return (
                        False,
                        f"**{actual_name}** was already sold to **{team}**. Use /rollback to undo the sale first.",
                    )

        if base_price is None:
            base_price = DEFAULT_BASE_PRICE

        self.db.reset_player_auctioned_status(player_id)

        return (
            True,
            f"**{actual_name}** has been added back to auction in **{list_name}** with base price {format_amount(base_price)}",
        )

    # ==================== RELEASE PLAYERS ====================

    def release_retained_player(self, team: str, player_name: str) -> Tuple[bool, str]:
        """Release any player from a squad back into auction (Retained or Sold)"""
        team_upper = team.upper()
        teams = self.db.get_teams()

        if team_upper not in teams:
            return False, "Invalid team name"

        # Check squad first (covers both retained and bought players)
        squads = self.db.get_all_squads()
        if team_upper not in squads:
            return False, f"{team_upper} has no players to release."

        player_found = None
        for p_name, price in squads[team_upper]:
            if p_name.lower() == player_name.lower():
                player_found = (p_name, price)
                break

        if not player_found:
            return False, f'"{player_name}" is not in {team_upper}\'s squad.'

        p_name, salary = player_found

        # Refund logic
        # For retained players, salary is their retention price.
        # For auctioned players, salary is their sold price.
        current_purse = teams[team_upper]
        new_purse = current_purse + salary

        # 1. Update Purse
        self.db.update_team_purse(team_upper, new_purse)

        # 2. Remove from Squad (Deleting from `team_squads` via DB call if I had one,
        # but I'll use a direct query via a new DB method or reuse logic)
        # Actually, let's implement a specific DB method for removal to be safe.
        # For now, we can use trade_player logic or manual deletion.
        # Let's add a direct delete query here since we are in Manager.

        with self.db._transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM team_squads WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_upper, player_name),
            )
            # Also delete from sales if present
            cursor.execute(
                "DELETE FROM sales WHERE team_code = ? AND LOWER(player_name) = LOWER(?)",
                (team_upper, player_name),
            )

        # 3. Add to auction pool
        self.db.create_list("released_players")
        self.db.add_player_to_list("released_players", p_name, salary)

        return (
            True,
            f'Released {p_name} from {team_upper}. Refunded {format_amount(salary)}. Player added to "released_players" list.',
        )

    # ==================== SALE FINALIZATION ====================

    def finalize_sale(self) -> Tuple[bool, Optional[str], int]:
        self._load_state_from_db()

        if not self.current_player:
            return False, None, 0

        player = self.current_player

        # CRITICAL FIX: Get the winning bid from bid_history table - authoritative source
        # This prevents the "wrong team sold" bug caused by stale in-memory state
        highest_bid = self.db.get_highest_bid_for_player(player)

        if highest_bid:
            team = highest_bid["team_code"]
            amount = highest_bid["amount"]
        else:
            # No bids - player goes unsold
            team = None
            amount = 0

        # Check if already sold (Double-Sold Safety)
        squads = self.db.get_all_squads()
        for squad in squads.values():
            for pname, _ in squad:
                if pname.lower() == player.lower():
                    # Player is already in a squad, probably from a previous run or race condition
                    self._reset_player_state()
                    self._save_state_to_db()  # Persist the cleared state
                    return False, None, 0

        if team:
            if not self.db.deduct_from_purse(team, amount):
                return False, None, 0

            self.db.add_to_squad(team, player, amount)

            bid_count = self.db.count_bids_for_player(player)
            self.db.record_sale(player, team, amount, bid_count)

            try:
                teams = self.db.get_teams()
                squads = self.db.get_all_squads()
                sales = self.db.get_all_sales()

                self.file_manager.regenerate_excel_from_db(
                    self.excel_file, sales, teams, squads
                )
            except Exception as e:
                logger.error(f"Error saving to Excel: {e}")

            # CRITICAL: Clear player state AFTER successful sale to prevent double-sell
            self._reset_player_state()
            self._save_state_to_db()

            return True, team, amount

        return False, None, 0

    # ==================== ADMIN OPERATIONS ====================

    def set_countdown(self, seconds: int) -> bool:
        if seconds < 5 or seconds > 300:
            return False
        self.countdown_seconds = seconds
        self._save_state_to_db()
        return True

    def set_team_purse(self, team: str, amount: int) -> bool:
        team_upper = team.upper()
        if amount < 0:
            return False
        return self.db.update_team_purse(team_upper, amount)

    def set_stats_channel(self, channel_id: int):
        self.stats_channel_id = channel_id
        self._save_state_to_db()

    def rollback_last_sale(self) -> Optional[dict]:
        sale = self.db.rollback_last_sale()

        if sale:
            try:
                teams = self.db.get_teams()
                squads = self.db.get_all_squads()
                sales = self.db.get_all_sales()

                self.file_manager.regenerate_excel_from_db(
                    self.excel_file, sales, teams, squads
                )
            except Exception as e:
                logger.error(f"Error updating Excel on rollback: {e}")

        return sale

    def clear_all_data(self):
        self._reset_state()
        self.db.full_reset()

        # Re-initialize teams with retained players deducted
        adjusted_teams = {}
        for team_code, initial_purse in TEAMS.items():
            remaining = get_remaining_purse(team_code, initial_purse)
            adjusted_teams[team_code] = remaining

        # Force initialize teams (since we just did full_reset)
        self.db.init_teams(adjusted_teams)

        # Re-add retained players to squads
        self._initialize_retained_players()

        try:
            self.file_manager.initialize_excel(self.excel_file)
        except Exception as e:
            logger.error(f"Error reinitializing Excel: {e}")

    # ==================== DISPLAY HELPERS ====================

    def get_purse_display(self) -> str:
        return self.formatter.format_purse_display(self.db.get_teams())

    def get_bid_history_display(self, player: str = None, limit: int = 5) -> str:
        if player:
            bids = self.db.get_bid_history_for_player(player)
        else:
            bids = self.db.get_recent_bids(limit)

        if not bids:
            return "No bids recorded."

        msg = "**Recent Bids:**\n```\n"
        for bid in bids[-limit:]:
            auto = "[AUTO]" if bid.get("is_auto_bid") else ""
            msg += f"{bid['team_code']:6} : {format_amount(bid['amount'])} {auto}\n"
        msg += "```"
        return msg

    def get_stats_message(self) -> str:
        data = self.db.get_stats_data()

        msg = "**📊 LIVE AUCTION STATS**\n\n"

        if data["most_expensive"]:
            msg += f"💰 **Most Expensive:** {data['most_expensive']['player_name']} - {format_amount(data['most_expensive']['final_price'])}\n"
        else:
            msg += "💰 **Most Expensive:** None\n"

        if data["most_players"]:
            msg += f"📈 **Most Players:** {data['most_players']['team_code']} ({data['most_players']['cnt']})\n"
        else:
            msg += "📈 **Most Players:** None\n"

        if data["least_players"]:
            msg += f"📉 **Least Players:** {data['least_players']['team_code']} ({data['least_players']['cnt']})\n"
        else:
            msg += "📉 **Least Players:** None\n"

        msg += "\n**💳 Team Purses:**\n```\n"
        for team, purse in data["purses"].items():
            msg += f"{team:6}: {format_amount(purse)}\n"
        msg += "```"

        return msg

    def get_status_display(self) -> str:
        status = "**Auction Status:**\n"
        status += f"Active: {'Yes' if self.active else 'No'}\n"
        status += f"Paused: {'Yes' if self.paused else 'No'}\n"

        if self.current_player:
            status += f"Current Player: **{self.current_player}**\n"
            status += f"Base Price: {format_amount(self.base_price)}\n"
            status += f"Current Bid: {format_amount(self.current_bid)}\n"
            status += f"Highest Bidder: **{self.highest_bidder or 'None'}**\n"

            import time as time_module

            elapsed = int(time_module.time() - self.last_bid_time)
            status += f"Time since last bid: {elapsed}s\n"

        current_list = self.get_current_list_name()
        if current_list:
            status += f"Current List: **{current_list}**\n"

        auto_bids = self.db.get_all_auto_bids()
        if auto_bids:
            status += f"Auto-bids Active: {', '.join(auto_bids.keys())}\n"

        return status


class AuctionState:
    pass
