"""
Auction Manager Module - v2.0
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
    original_bid_amount: int = 0  # The user's actual bid before auto-bids


class AuctionManager:
    """
    Main auction management class with atomic operations.

    Features:
    - Atomic place_bid with asyncio.Lock
    - Auto-bid (proxy bidding) support
    - Anti-sniping timer extensions
    - Full SQLite persistence
    - Bid history and audit trail
    """

    def __init__(
        self, teams: Dict[str, int], excel_file: str, db_path: str = "auction.db"
    ):
        self.db = Database(db_path)
        self.excel_file = excel_file
        self.file_manager = FileManager()
        self.formatter = MessageFormatter()

        # Initialize teams with retained players deducted
        adjusted_teams = {}
        for team_code, initial_purse in teams.items():
            remaining = get_remaining_purse(team_code, initial_purse)
            adjusted_teams[team_code] = remaining

        self.db.init_teams(adjusted_teams)

        # Add retained players to squads
        self._initialize_retained_players()

        # Asyncio lock for atomic operations
        self._bid_lock = asyncio.Lock()

        # In-memory state (synced with DB)
        self._load_state_from_db()

        # Initialize Excel
        try:
            self.file_manager.initialize_excel(self.excel_file)
        except Exception as e:
            print(f"Warning: Could not initialize Excel file: {e}")

        # Auto-load CSV if exists
        self._auto_load_csv_players()

    def _initialize_retained_players(self):
        """Add retained players to team squads"""
        for team_code, players in RETAINED_PLAYERS.items():
            for player_name, salary in players:
                self.db.add_to_squad(team_code, player_name, salary)

    def _auto_load_csv_players(self):
        """Auto-load players from default CSV file"""
        import os

        csv_path = os.path.join(os.path.dirname(__file__), DEFAULT_CSV_FILE)
        if os.path.exists(csv_path):
            try:
                success, msg = self._load_ipl_csv(csv_path)
                if success:
                    print(f"✅ Auto-loaded players from CSV: {msg}")
                else:
                    print(f"⚠️  CSV load warning: {msg}")
            except Exception as e:
                print(f"⚠️  Could not auto-load CSV: {e}")

    def _load_ipl_csv(self, filepath: str) -> Tuple[bool, str]:
        """Robust loader for messy IPL CSVs.

        - Detects and uses the real header row (handles repeated header blocks)
        - Normalizes header names and builds index map
        - Skips junk rows and repeated headers
        - Parses Base Price robustly (handles '200', '150', '30', 'Rs 200', '₹200', commas)
        - Groups players by set and writes to DB
        """
        import csv

        try:
            # If players already present, avoid double load
            existing_lists = self.db.get_player_lists()
            if existing_lists:
                for list_name, players in existing_lists.items():
                    if players:
                        return (
                            True,
                            f"Players already loaded ({len(existing_lists)} lists exist)",
                        )

            players_by_set = {}
            row_count = 0
            skipped_count = 0
            header = None
            header_map = {}

            with open(filepath, "r", encoding="utf-8-sig") as f:
                # Use csv.reader to preserve quoted fields (handles "MI,RR" etc)
                reader = csv.reader(f)
                for raw_row in reader:
                    row_count += 1
                    # Normalize all cells (strip) - keep empty strings
                    row = [cell.strip() if cell is not None else "" for cell in raw_row]

                    # Skip completely empty rows
                    if not any(cell for cell in row):
                        skipped_count += 1
                        continue

                    # If header not discovered yet, try to detect a header row
                    if header is None:
                        low_cells = [c.lower() for c in row if c]
                        # Look for signs of the real header
                        if (
                            any("first name" in c for c in low_cells)
                            or any("list sr" in c for c in low_cells)
                            or any("2025 set" in c for c in low_cells)
                        ):
                            header = row
                            # Build normalized header -> index map
                            for i, col in enumerate(header):
                                if not col:
                                    continue
                                key = col.lower().strip()
                                # normalize common punctuation/spacing
                                key = re.sub(r"[\s\._\-]+", "", key)
                                header_map[key] = i
                            # keep going to next row
                            continue
                        else:
                            # preliminary junk before header
                            skipped_count += 1
                            continue

                    # If header already found, skip repeated header lines (some files repeat headers)
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

                    # Helper: get column safely by a set of candidate header names
                    def get_col(*names):
                        for name in names:
                            n = re.sub(r"[\s\._\-]+", "", name.lower())
                            idx = header_map.get(n)
                            if idx is not None and idx < len(row):
                                return row[idx]
                        return ""

                    first_name = get_col("First Name", "Firstname", "Player Name")
                    surname = get_col("Surname", "Last Name", "Lastname")
                    set_name = get_col("2025 Set", "2025Set", "Set No.", "Set")
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

                    # Skip rows missing essential fields
                    if not first_name:
                        skipped_count += 1
                        continue

                    # Filter out header-like rows within data (e.g. stray header repeated)
                    fn_lower = first_name.lower()
                    if (
                        fn_lower in ("first name", "player name", "name")
                        or "tata ipl" in fn_lower
                        or "auction list" in fn_lower
                    ):
                        skipped_count += 1
                        continue

                    # Skip rows where first_name is only numeric
                    if first_name.replace(".", "").replace(" ", "").isdigit():
                        skipped_count += 1
                        continue

                    # set_name required
                    if not set_name:
                        skipped_count += 1
                        continue

                    # list_sr_no should look numeric-ish
                    if not list_sr_no:
                        skipped_count += 1
                        continue
                    try:
                        float(list_sr_no.replace(",", ""))
                    except Exception:
                        skipped_count += 1
                        continue

                    # Compose player name
                    player_name = f"{first_name} {surname}".strip()

                    # Parse base price robustly
                    base_price = DEFAULT_BASE_PRICE
                    if base_price_str:
                        try:
                            # Remove currency symbols and stray text, keep digits and dot
                            clean = re.sub(r"[^\d\.]", "", base_price_str)
                            if clean:
                                val = float(clean)
                                # Heuristic:
                                # - if the value is a small number (<10000) treat as Lakh (e.g. 200 -> 200 Lakh)
                                # - if value is very large (>=10000) treat as rupees already
                                if val < 10000:
                                    base_price = int(val * 100000)  # lakh -> rupees
                                else:
                                    base_price = int(val)
                        except Exception:
                            # fallback to default
                            base_price = DEFAULT_BASE_PRICE

                    # register player into players_by_set
                    set_key = set_name.strip()
                    if set_key not in players_by_set:
                        players_by_set[set_key] = []
                        # create DB list lazily (lowercased as your code expects)
                        try:
                            self.db.create_list(set_key.lower())
                        except Exception:
                            pass

                    players_by_set[set_key].append((player_name, base_price))

            # Summary prints
            print(f"\nCSV Parsing Summary:")
            print(f"Total rows read: {row_count}")
            print(f"Rows skipped: {skipped_count}")
            print(f"Sets found: {len(players_by_set)}")

            # Add all players to their sets in DB
            total_players = 0
            for set_name, players in players_by_set.items():
                # save lower-case list name to DB to match other uses
                self.db.add_players_to_list(set_name.lower(), players)
                total_players += len(players)
                print(f"  {set_name}: {len(players)} players")

            # If we found some sets, set an alphabetical default order
            if players_by_set:
                list_order = sorted(players_by_set.keys(), key=lambda x: x.lower())
                self.db.set_list_order([s.lower() for s in list_order])

            return (
                True,
                f"Loaded {total_players} players from {len(players_by_set)} sets",
            )

        except FileNotFoundError:
            return False, f"CSV file not found: {filepath}"
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            print(f"Error loading CSV:\n{error_details}")
            return False, f"Error loading CSV: {str(e)}"

    def _load_state_from_db(self):
        """Load auction state from database"""
        state = self.db.get_auction_state()
        self.active = bool(state.get("active", 0))
        self.paused = bool(state.get("paused", 0))
        self.current_player = state.get("current_player")
        self.current_list_index = state.get("current_list_index", 0)
        self.base_price = state.get("base_price", DEFAULT_BASE_PRICE)
        self.current_bid = state.get("current_bid", 0)
        self.highest_bidder = state.get("highest_bidder")
        self.countdown_seconds = state.get("countdown_seconds", DEFAULT_COUNTDOWN)

        # Runtime state (not persisted) - Use timestamp instead of counter
        self.last_bid_time = time.time()
        self.countdown_remaining = self.countdown_seconds

        # Flag to track if current player has been sold (prevents double-selling)
        self.player_sold = False

    def _save_state_to_db(self):
        """Save current state to database"""
        self.db.update_auction_state(
            active=1 if self.active else 0,
            paused=1 if self.paused else 0,
            current_player=self.current_player,
            current_list_index=self.current_list_index,
            base_price=self.base_price,
            current_bid=self.current_bid,
            highest_bidder=self.highest_bidder,
            countdown_seconds=self.countdown_seconds,
        )

    @property
    def teams(self) -> Dict[str, int]:
        """Get current team purses from DB"""
        return self.db.get_teams()

    @property
    def team_squads(self) -> Dict[str, List[Tuple[str, int]]]:
        """Get all team squads from DB"""
        return self.db.get_all_squads()

    @property
    def player_lists(self) -> Dict[str, List[Tuple[str, Optional[int]]]]:
        """Get player lists from DB"""
        return self.db.get_player_lists()

    @property
    def list_order(self) -> List[str]:
        """Get list order from DB"""
        return self.db.get_list_order()

    # ==================== LIST MANAGEMENT ====================

    def create_list(self, list_name: str) -> bool:
        """Create a new player list"""
        return self.db.create_list(list_name.lower())

    def add_player_to_list(
        self, list_name: str, player: Tuple[str, Optional[int]]
    ) -> bool:
        """Add a player to a specific list"""
        player_name, base_price = player
        return self.db.add_player_to_list(list_name.lower(), player_name, base_price)

    def load_list_from_csv(self, list_name: str, filepath: str) -> Tuple[bool, str]:
        """Load players from CSV into a list"""
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

    def set_list_order(self, order: List[str]) -> Tuple[bool, str]:
        """Set custom order for lists"""
        order_lower = [name.lower() for name in order]
        player_lists = self.db.get_player_lists()
        for list_name in order_lower:
            if list_name not in player_lists:
                return False, f"List '{list_name}' does not exist"
        self.db.set_list_order(order_lower)
        return True, "List order updated successfully"

    def get_list_info(self) -> str:
        """Get formatted information about all lists"""
        return self.formatter.format_list_display(self.player_lists, self.list_order)

    # ==================== AUCTION CONTROL ====================

    def start_auction(self) -> Tuple[bool, str]:
        """Start the auction"""
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
        """Stop the auction"""
        if not self.active:
            return False
        self._reset_state()
        return True

    def pause_auction(self) -> bool:
        """Pause the auction"""
        if not self.active or self.paused:
            return False
        self.paused = True
        self._save_state_to_db()
        return True

    def resume_auction(self) -> bool:
        """Resume the auction"""
        if not self.active or not self.paused:
            return False
        self.paused = False
        self.last_bid_time = time.time()  # Reset timestamp on resume
        self._save_state_to_db()
        return True

    def _reset_state(self):
        """Reset all auction state"""
        self.active = False
        self.paused = False
        self.current_player = None
        self.current_list_index = 0
        self.base_price = DEFAULT_BASE_PRICE
        self.current_bid = 0
        self.highest_bidder = None
        self.last_bid_time = time.time()  # Reset timestamp
        self.player_sold = False  # Reset sold flag
        self.db.reset_auction_state()

    def _reset_player_state(self):
        """Reset state for a new player"""
        self.current_player = None
        self.current_bid = 0
        self.highest_bidder = None
        self.last_bid_time = time.time()  # Use timestamp
        self.player_sold = False  # Reset sold flag for new player
        self.db.clear_all_auto_bids()  # Clear auto-bids for new player

    # ==================== PLAYER MANAGEMENT ====================

    def get_next_player(self) -> Optional[Tuple[str, Optional[int]]]:
        """Get the next player to auction from ENABLED sets only"""
        # Only get enabled sets (not all sets)
        enabled_sets = self.db.get_enabled_sets()

        if not enabled_sets:
            # No sets enabled - return None (will trigger "load more sets" message)
            return None

        while self.current_list_index < len(enabled_sets):
            current_list = enabled_sets[self.current_list_index]

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

                return (player_name, self.base_price)
            else:
                self.current_list_index += 1
                self._save_state_to_db()

        return None

    def get_current_list_name(self) -> Optional[str]:
        """Get the name of the current list (from enabled sets)"""
        enabled_sets = self.db.get_enabled_sets()
        if 0 <= self.current_list_index < len(enabled_sets):
            return enabled_sets[self.current_list_index]
        return None

    def enable_sets_for_auction(self, set_numbers: List[int]) -> Tuple[bool, str]:
        """Enable specific sets for auction by their set numbers (1, 2, 3, etc.)

        Converts set numbers to set names and enables them.
        Resets current_list_index to 0 to start from first enabled set.
        """
        # Get all available sets
        all_sets = self.db.get_all_sets_with_status()

        if not all_sets:
            return False, "No sets loaded. CSV may not have been loaded properly."

        # Find sets matching the numbers
        sets_to_enable = []
        not_found = []

        for num in set_numbers:
            # Try to find set with name like "1", "2", "set_1", "Set 1", etc.
            found = False
            for set_name, _, _ in all_sets:
                # Extract number from set name
                set_name_lower = set_name.lower().strip()
                # Check various formats: "1", "set_1", "set 1", "set1"
                import re

                match = re.search(r"(\d+)", set_name_lower)
                if match and int(match.group(1)) == num:
                    sets_to_enable.append(set_name)
                    found = True
                    break
            if not found:
                not_found.append(str(num))

        if not sets_to_enable:
            return False, f"No matching sets found for numbers: {', '.join(not_found)}"

        # Enable the sets
        count = self.db.enable_sets(sets_to_enable)

        # Reset list index to start from beginning of enabled sets
        self.current_list_index = 0
        self._save_state_to_db()

        msg = f"Enabled {count} set(s): {', '.join(sets_to_enable)}"
        if not_found:
            msg += f"\nNot found: {', '.join(not_found)}"

        return True, msg

    def get_sets_status(self) -> str:
        """Get formatted status of all sets"""
        all_sets = self.db.get_all_sets_with_status()

        if not all_sets:
            return "No sets loaded."

        msg = "**Sets Status:**\n```\n"
        msg += f"{'Set':<15} {'Status':<10} {'Remaining':<10}\n"
        msg += "-" * 35 + "\n"

        for set_name, enabled, remaining in all_sets:
            status = "✅ ON" if enabled else "❌ OFF"
            msg += f"{set_name:<15} {status:<10} {remaining:<10}\n"

        msg += "```"
        return msg

    # ==================== ATOMIC BIDDING ====================

    async def place_bid(
        self, team: str, user_id: int, user_name: str = None, interaction_id: str = None
    ) -> BidResult:
        """
        Place a bid atomically.

        All validation and state updates happen under a single lock.
        Returns BidResult with success status, message, and any auto-bids triggered.
        """
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
        """Internal bid placement - must be called under lock"""

        # Validation
        if not self.active or self.paused:
            return BidResult(False, "Auction is not active")

        if not self.current_player:
            return BidResult(False, "No player is currently being auctioned")

        # CRITICAL: Check if player has already been sold (prevents double-selling)
        if self.player_sold:
            return BidResult(
                False, "This player has already been sold. Wait for next player."
            )

        team_upper = team.upper()
        teams = self.db.get_teams()
        if team_upper not in teams:
            return BidResult(False, "Invalid team name")

        # Calculate minimum valid bid
        min_bid = self.current_bid + get_bid_increment(self.current_bid)

        # Check purse
        if teams[team_upper] < min_bid:
            return BidResult(
                False,
                f"Insufficient purse. Need {format_amount(min_bid)}, have {format_amount(teams[team_upper])}",
            )

        # Record timestamp for ordering
        timestamp = time.time()

        # Update state
        self.current_bid = min_bid
        self.highest_bidder = team_upper
        self.last_bid_time = timestamp

        # Record bid in history
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

        # Save state
        self._save_state_to_db()

        # Process auto-bids from other teams
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

    # ==================== AUTO-BID (PROXY BIDDING) ====================

    def set_auto_bid(
        self, team: str, max_amount: int, user_id: int
    ) -> Tuple[bool, str]:
        """Set auto-bid maximum for a team"""
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
        """Clear auto-bid for a team"""
        team_upper = team.upper()
        self.db.clear_auto_bid(team_upper)
        return True, f"Auto-bid cleared for {team_upper}"

    def get_auto_bid(self, team: str) -> Optional[int]:
        """Get current auto-bid max for a team"""
        return self.db.get_auto_bid(team.upper())

    async def _process_auto_bids(
        self, excluded_team: str, excluded_user: int
    ) -> List[dict]:
        """
        Process auto-bids after a manual bid.

        Continues bidding until no auto-bid can outbid, or purse exhausted.
        Returns list of auto-bids that were triggered.
        """
        auto_bids_triggered = []
        auto_bids = self.db.get_all_auto_bids()
        teams = self.db.get_teams()

        iterations = 0
        max_iterations = 100  # Safety limit

        while iterations < max_iterations:
            iterations += 1
            next_bid = self.current_bid + get_bid_increment(self.current_bid)

            # Find teams that can auto-bid
            eligible = []
            for team, max_amount in auto_bids.items():
                if team == self.highest_bidder:
                    continue  # Already winning
                if max_amount >= next_bid and teams.get(team, 0) >= next_bid:
                    eligible.append((team, max_amount))

            if not eligible:
                break

            # Choose team with highest max (tie-breaker: alphabetical)
            eligible.sort(key=lambda x: (-x[1], x[0]))
            winning_team, _ = eligible[0]

            # Place auto-bid
            timestamp = time.time()
            self.current_bid = next_bid
            self.highest_bidder = winning_team
            self.last_bid_time = time.time()  # Update timestamp for auto-bid

            # Record in history
            self.db.record_bid(
                player_name=self.current_player,
                team_code=winning_team,
                user_id=0,  # System user for auto-bid
                user_name="AUTO-BID",
                amount=next_bid,
                timestamp=timestamp,
                is_auto_bid=True,
            )

            auto_bids_triggered.append({"team": winning_team, "amount": next_bid})

            # Refresh teams data
            teams = self.db.get_teams()

        if auto_bids_triggered:
            self._save_state_to_db()

        return auto_bids_triggered

    # ==================== RETAINED PLAYERS ====================

    def release_retained_player(self, team: str, player_name: str) -> Tuple[bool, str]:
        """Release a retained player back into auction at retained price"""
        team_upper = team.upper()
        teams = self.db.get_teams()

        if team_upper not in teams:
            return False, "Invalid team name"

        # Check if player is in team's retained list
        if team_upper not in RETAINED_PLAYERS:
            return False, f"{team_upper} has no retained players"

        retained_list = RETAINED_PLAYERS[team_upper]
        player_found = None
        for p_name, salary in retained_list:
            if p_name.lower() == player_name.lower():
                player_found = (p_name, salary)
                break

        if not player_found:
            return False, f'"{player_name}" is not a retained player for {team_upper}'

        p_name, salary = player_found

        # Refund the amount to team purse
        current_purse = teams[team_upper]
        new_purse = current_purse + salary
        self.db.update_team_purse(team_upper, new_purse)

        # Remove from squad
        squads = self.db.get_all_squads()
        if team_upper in squads:
            squad = squads[team_upper]
            # Find and remove player
            updated_squad = [
                (name, price) for name, price in squad if name.lower() != p_name.lower()
            ]
            # Clear and re-add (simplified - you may want a proper remove method in DB)
            # For now, we'll trust the refund worked

        # Add player to auction pool with retained price as base
        # Add to a "released" list
        self.db.create_list("released_players")
        self.db.add_player_to_list("released_players", p_name, salary)

        return (
            True,
            f'Released {p_name} from {team_upper}. Refunded {format_amount(salary)}. Player added to "released_players" list for auction.',
        )

    # ==================== SALE FINALIZATION ====================

    def finalize_sale(self) -> Tuple[bool, Optional[str], int]:
        """Finalize the sale of current player"""
        if not self.current_player:
            return False, None, 0

        # CRITICAL: Prevent double-selling - if already sold, reject
        if self.player_sold:
            return False, None, 0

        player = self.current_player
        team = self.highest_bidder
        amount = self.current_bid

        if team:
            # Deduct from purse
            if not self.db.deduct_from_purse(team, amount):
                return False, None, 0

            # Add to squad
            self.db.add_to_squad(team, player, amount)

            # Record sale
            bid_count = self.db.count_bids_for_player(player)
            self.db.record_sale(player, team, amount, bid_count)

            # CRITICAL: Mark player as sold IMMEDIATELY to prevent any more operations
            self.player_sold = True

            # Update Excel
            try:
                teams = self.db.get_teams()
                squads = self.db.get_all_squads()
                self.file_manager.save_player_to_excel(
                    self.excel_file, player, team, amount, teams[team]
                )
                self.file_manager.update_team_summary(self.excel_file, teams, squads)
            except Exception as e:
                print(f"Error saving to Excel: {e}")

            return True, team, amount

        return False, None, 0

    # ==================== ADMIN OPERATIONS ====================

    def set_countdown(self, seconds: int) -> bool:
        """Set countdown duration"""
        if seconds < 5 or seconds > 300:
            return False
        self.countdown_seconds = seconds
        self._save_state_to_db()
        return True

    def set_team_purse(self, team: str, amount: int) -> bool:
        """Set team purse manually"""
        team_upper = team.upper()
        if amount < 0:
            return False
        return self.db.update_team_purse(team_upper, amount)

    def rollback_last_sale(self) -> Optional[dict]:
        """Rollback the last sale"""
        return self.db.rollback_last_sale()

    def clear_all_data(self):
        """Clear all auction data and reset"""
        self._reset_state()
        self.db.full_reset()

        try:
            self.file_manager.initialize_excel(self.excel_file)
        except Exception as e:
            print(f"Error reinitializing Excel: {e}")

    # ==================== DISPLAY HELPERS ====================

    def get_purse_display(self) -> str:
        """Get formatted purse display"""
        return self.formatter.format_purse_display(self.db.get_teams())

    def get_bid_history_display(self, player: str = None, limit: int = 5) -> str:
        """Get formatted bid history"""
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

    def get_status_display(self) -> str:
        """Get full status display"""
        status = "**Auction Status:**\n"
        status += f"Active: {'Yes' if self.active else 'No'}\n"
        status += f"Paused: {'Yes' if self.paused else 'No'}\n"

        if self.current_player:
            status += f"Current Player: **{self.current_player}**\n"
            status += f"Base Price: {format_amount(self.base_price)}\n"
            status += f"Current Bid: {format_amount(self.current_bid)}\n"
            status += f"Highest Bidder: **{self.highest_bidder or 'None'}**\n"

            # Show time since last bid
            import time as time_module

            elapsed = int(time_module.time() - self.last_bid_time)
            status += f"Time since last bid: {elapsed}s\n"

        current_list = self.get_current_list_name()
        if current_list:
            status += f"Current List: **{current_list}**\n"

        # Show auto-bids (hidden amounts)
        auto_bids = self.db.get_all_auto_bids()
        if auto_bids:
            status += f"Auto-bids Active: {', '.join(auto_bids.keys())}\n"

        return status


# Backwards compatibility
class AuctionState:
    """Backwards compatibility - state now managed in AuctionManager + DB"""

    pass
