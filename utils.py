"""
Utility functions for Discord Auction Bot
Contains helper functions for formatting, file operations, etc.
"""

import csv
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import os


# -----------------------------------------------------------
#  AMOUNT FORMATTER  → Converts Rs amounts to cr / lakh (readable)
# -----------------------------------------------------------
def format_amount(num: Optional[int]) -> str:
    """Convert numeric rupee amount to readable cr/lakh format.

    Rules:
      - >= 1,00,00,000 -> show in crores with up to 2 decimals, suffixed "cr"
      - >= 1,00,000 -> show in lakhs with up to 2 decimals, suffixed "L"
      - None or small -> formatted number
    """
    if num is None:
        return "0"

    try:
        n = int(num)
    except Exception:
        return str(num)

    if n >= 10_000_000:
        val = n / 10_000_000.0
        s = f"{val:.2f}".rstrip("0").rstrip(".")
        return f"{s}cr"
    if n >= 100_000:
        val = n / 100_000.0
        s = f"{val:.2f}".rstrip("0").rstrip(".")
        return f"{s}L"
    return f"{n:,}"


class FileManager:
    """Handles file operations for the auction bot"""

    @staticmethod
    def load_players_from_csv(filepath: str) -> List[Tuple[str, Optional[int]]]:
        """
        Load players from a CSV file.
        Returns list of (player_name, base_price_or_None).
        Supports:
          - simple single-column list (Name)
          - two-column Name,BasePrice
          - IPL-like CSV where base price is present in a numeric column
        """
        players: List[Tuple[str, Optional[int]]] = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = [r for r in reader if any(cell.strip() for cell in r)]
                if not rows:
                    return players

                # Try detect header with BasePrice column
                first_row = rows[0]
                header = [c.strip().lower() for c in first_row]

                # If header contains 'player' or 'name', and 'price' or 'base' try parse with header
                has_name = any("name" in c for c in header)
                has_price = any("price" in c or "base" in c for c in header)

                start_index = 1 if has_name else 0

                if has_name and has_price:
                    # Use headers to find columns
                    name_idx = next((i for i, c in enumerate(header) if "name" in c), 0)
                    price_idx = next(
                        (
                            i
                            for i, c in enumerate(header)
                            if ("price" in c or "base" in c)
                        ),
                        None,
                    )
                    for row in rows[start_index:]:
                        try:
                            name = row[name_idx].strip()
                        except IndexError:
                            continue
                        price = None
                        if price_idx is not None and price_idx < len(row):
                            raw = (
                                row[price_idx]
                                .replace(",", "")
                                .replace("Rs", "")
                                .replace("rs", "")
                                .strip()
                            )
                            if raw and (raw.replace(".", "", 1).isdigit()):
                                try:
                                    price = int(float(raw))
                                except:
                                    price = None
                        if name and not name.isdigit():
                            players.append((name, price))
                else:
                    # No clear header: guess formats per row
                    for row in rows:
                        # prefer rows with at least one non-empty cell
                        if not row:
                            continue
                        # name is first non-empty
                        name = None
                        price = None
                        for i, cell in enumerate(row):
                            if cell and cell.strip():
                                name = cell.strip()
                                # look ahead for price in next few cols
                                for next_cell in row[i + 1 : i + 4]:
                                    if next_cell and next_cell.strip():
                                        raw = (
                                            next_cell.replace(",", "")
                                            .replace("Rs", "")
                                            .replace("rs", "")
                                            .strip()
                                        )
                                        if raw and (raw.replace(".", "", 1).isdigit()):
                                            try:
                                                price = int(float(raw))
                                            except:
                                                price = None
                                            break
                                break
                        if name and not name.isdigit():
                            players.append((name, price))
            return players
        except FileNotFoundError:
            raise FileNotFoundError(f"CSV file not found: {filepath}")
        except Exception as e:
            raise Exception(f"Error reading CSV file: {str(e)}")

    @staticmethod
    def initialize_excel(filepath: str) -> None:
        """Initialize Excel file with headers - creates all required sheets"""
        wb = openpyxl.Workbook()

        header_fill = PatternFill(
            start_color="366092", end_color="366092", fill_type="solid"
        )
        header_font = Font(bold=True, color="FFFFFF")

        # Sheet 1: Auction Results
        sheet = wb.active
        sheet.title = "Auction Results"
        headers = ["Player Name", "Team", "Final Price", "Timestamp"]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Sheet 2: Team Summary
        team_sheet = wb.create_sheet("Team Summary")
        team_sheet.append(["Team", "Players Bought", "Total Spent", "Remaining Purse"])
        for cell in team_sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Sheet 3: Unsold Players
        unsold_sheet = wb.create_sheet("Unsold Players")
        unsold_sheet.append(["Player", "Set Name", "Base Price"])
        for cell in unsold_sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Sheet 4+: Individual Team Sheets
        from config import TEAMS

        for team_code in sorted(TEAMS.keys()):
            team_individual_sheet = wb.create_sheet(team_code)
            team_individual_sheet.append(["Player Name", "Price", "Type"])
            for cell in team_individual_sheet[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

        wb.save(filepath)

    @staticmethod
    def initialize_excel_with_retained_players(
        filepath: str,
        teams: Dict[str, int],
        team_squads: Dict[str, List[Tuple[str, int]]],
    ) -> None:
        """Initialize Excel file with retained players already in team sheets.

        This should be called at bot startup to populate team sheets with retained players.
        """
        try:
            from retained_players import RETAINED_PLAYERS
            from config import TEAMS as TEAM_CONFIG

            # Initialize fresh Excel
            FileManager.initialize_excel(filepath)

            wb = openpyxl.load_workbook(filepath)

            header_fill = PatternFill(
                start_color="366092", end_color="366092", fill_type="solid"
            )
            header_font = Font(bold=True, color="FFFFFF")
            summary_fill = PatternFill(
                start_color="FFC000", end_color="FFC000", fill_type="solid"
            )
            summary_font = Font(bold=True)

            # Update each team sheet with their retained players
            for team_code in sorted(TEAM_CONFIG.keys()):
                if team_code in wb.sheetnames:
                    del wb[team_code]

                ts = wb.create_sheet(team_code)
                ts.append(["Player Name", "Price", "Type"])
                for cell in ts[1]:
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center")

                # Get squad from database (includes retained players)
                squad = team_squads.get(team_code, [])
                retained = RETAINED_PLAYERS.get(team_code, [])
                retained_names = {p[0].lower() for p in retained}

                total_spent = 0

                for player_name, price in squad:
                    player_type = (
                        "Retained"
                        if player_name.lower() in retained_names
                        else "Bought"
                    )
                    ts.append([player_name, format_amount(price), player_type])
                    total_spent += price

                # Summary rows
                ts.append([])
                purse_left = teams.get(team_code, 0)

                summary_row = ts.max_row + 1
                ts.append(["Total Players", len(squad), ""])
                ts.append(["Total Spent", format_amount(total_spent), ""])
                ts.append(["Purse Remaining", format_amount(purse_left), ""])

                for row_num in range(summary_row, ts.max_row + 1):
                    for cell in ts[row_num]:
                        cell.fill = summary_fill
                        cell.font = summary_font

                ts.column_dimensions["A"].width = 30
                ts.column_dimensions["B"].width = 15
                ts.column_dimensions["C"].width = 12

            # Also update Team Summary
            if "Team Summary" in wb.sheetnames:
                del wb["Team Summary"]
            summary_sheet = wb.create_sheet("Team Summary", 1)
            summary_sheet.append(
                ["Team", "Players Bought", "Total Spent", "Remaining Purse"]
            )
            for cell in summary_sheet[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

            from config import TEAMS as TEAM_PURSES

            for team_code in sorted(TEAM_PURSES.keys()):
                squad = team_squads.get(team_code, [])
                total_spent = sum(price for _, price in squad)
                remaining = teams.get(team_code, 0)
                summary_sheet.append(
                    [
                        team_code,
                        len(squad),
                        format_amount(total_spent),
                        format_amount(remaining),
                    ]
                )

            wb.save(filepath)
        except Exception as e:
            raise Exception(f"Error initializing Excel with retained players: {str(e)}")

    @staticmethod
    def save_player_to_excel(
        filepath: str, player: str, team: str, price: int, remaining_purse: int
    ) -> None:
        """Save a sold player to the Excel file"""
        try:
            if not os.path.exists(filepath):
                FileManager.initialize_excel(filepath)
            wb = openpyxl.load_workbook(filepath)
            sheet = wb["Auction Results"]
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Format price as cr/L for readability
            formatted_price = format_amount(price)
            sheet.append([player, team, formatted_price, timestamp])
            wb.save(filepath)
        except Exception as e:
            raise Exception(f"Error saving to Excel: {str(e)}")

    @staticmethod
    def update_team_summary(
        filepath: str,
        teams: Dict[str, int],
        team_squads: Dict[str, List[Tuple[str, int]]],
    ) -> None:
        """Update the team summary sheet with current data"""
        try:
            if not os.path.exists(filepath):
                FileManager.initialize_excel(filepath)
            wb = openpyxl.load_workbook(filepath)
            if "Team Summary" in wb.sheetnames:
                del wb["Team Summary"]
            ts = wb.create_sheet("Team Summary", 1)  # Position after Auction Results
            ts.append(["Team", "Players Bought", "Total Spent", "Remaining Purse"])
            header_fill = PatternFill(
                start_color="366092", end_color="366092", fill_type="solid"
            )
            header_font = Font(bold=True, color="FFFFFF")
            for cell in ts[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")
            from config import TEAMS

            for team in sorted(teams.keys()):
                purse_left = teams.get(team, 0)
                squad = team_squads.get(team, [])
                spent = sum(price for _, price in squad)
                # Format amounts as cr/L for readability
                ts.append(
                    [
                        team,
                        len(squad),
                        format_amount(spent),
                        format_amount(purse_left),
                    ]
                )
            wb.save(filepath)
        except Exception as e:
            raise Exception(f"Error updating team summary: {str(e)}")

    @staticmethod
    def update_unsold_players_sheet(
        filepath: str,
        unsold_players: List[Tuple[str, str, Optional[int]]],
    ) -> None:
        """Update the unsold players sheet

        Args:
            filepath: Path to Excel file
            unsold_players: List of (player_name, set_name, base_price)
        """
        try:
            if not os.path.exists(filepath):
                FileManager.initialize_excel(filepath)
            wb = openpyxl.load_workbook(filepath)

            # Remove existing sheet if present
            if "Unsold Players" in wb.sheetnames:
                del wb["Unsold Players"]

            # Create at position 2 (after Team Summary)
            us = wb.create_sheet("Unsold Players", 2)
            us.append(["Player", "Set Name", "Base Price"])

            header_fill = PatternFill(
                start_color="366092", end_color="366092", fill_type="solid"
            )
            header_font = Font(bold=True, color="FFFFFF")
            for cell in us[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

            for player_name, set_name, base_price in unsold_players:
                us.append(
                    [
                        player_name,
                        set_name.upper() if set_name else "N/A",
                        format_amount(base_price) if base_price else "N/A",
                    ]
                )

            wb.save(filepath)
        except Exception as e:
            raise Exception(f"Error updating unsold players sheet: {str(e)}")

    @staticmethod
    def update_individual_team_sheets(
        filepath: str,
        teams: Dict[str, int],
        team_squads: Dict[str, List[Tuple[str, int]]],
        retained_players: Dict[str, List[Tuple[str, int]]] = None,
    ) -> None:
        """Update individual team sheets with squad details

        Args:
            filepath: Path to Excel file
            teams: Team purses
            team_squads: All squad data from database
            retained_players: Retained players dict (optional, will import if not provided)
        """
        try:
            if not os.path.exists(filepath):
                FileManager.initialize_excel(filepath)

            if retained_players is None:
                from retained_players import RETAINED_PLAYERS

                retained_players = RETAINED_PLAYERS

            wb = openpyxl.load_workbook(filepath)

            header_fill = PatternFill(
                start_color="366092", end_color="366092", fill_type="solid"
            )
            header_font = Font(bold=True, color="FFFFFF")
            summary_fill = PatternFill(
                start_color="FFC000", end_color="FFC000", fill_type="solid"
            )
            summary_font = Font(bold=True)

            from config import TEAMS

            sheet_position = (
                3  # Start after Auction Results, Team Summary, Unsold Players
            )

            for team_code in sorted(TEAMS.keys()):
                # Remove existing sheet if present
                if team_code in wb.sheetnames:
                    del wb[team_code]

                # Create new sheet
                ts = wb.create_sheet(team_code, sheet_position)
                sheet_position += 1

                # Headers
                ts.append(["Player Name", "Price", "Type"])
                for cell in ts[1]:
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center")

                squad = team_squads.get(team_code, [])
                retained = retained_players.get(team_code, [])
                retained_names = {p[0].lower() for p in retained}

                total_spent = 0

                # Add players
                for player_name, price in squad:
                    player_type = (
                        "Retained"
                        if player_name.lower() in retained_names
                        else "Bought"
                    )
                    ts.append([player_name, format_amount(price), player_type])
                    total_spent += price

                # Add summary rows
                ts.append([])  # Empty row
                purse_left = teams.get(team_code, 0)

                summary_row = ts.max_row + 1
                ts.append(["Total Players", len(squad), ""])
                ts.append(["Total Spent", format_amount(total_spent), ""])
                ts.append(["Purse Remaining", format_amount(purse_left), ""])

                # Style summary rows
                for row_num in range(summary_row, ts.max_row + 1):
                    for cell in ts[row_num]:
                        cell.fill = summary_fill
                        cell.font = summary_font

                # Auto-fit column widths
                ts.column_dimensions["A"].width = 30
                ts.column_dimensions["B"].width = 15
                ts.column_dimensions["C"].width = 12

            wb.save(filepath)
        except Exception as e:
            raise Exception(f"Error updating individual team sheets: {str(e)}")

    @staticmethod
    def regenerate_excel_from_db(
        filepath: str,
        sales: List[dict],
        teams: Dict[str, int],
        team_squads: Dict[str, List[Tuple[str, int]]],
        unsold_players: List[Tuple[str, str, Optional[int]]] = None,
    ) -> None:
        """Regenerate the entire Excel file from database records.

        Use this after any sale/rollback to ensure Excel matches DB state.

        Args:
            filepath: Path to Excel file
            sales: List of sale records
            teams: Team purses
            team_squads: All squad data
            unsold_players: List of (player_name, set_name, base_price) for unsold players
        """
        try:
            # Initialize fresh Excel file with all sheets
            FileManager.initialize_excel(filepath)

            wb = openpyxl.load_workbook(filepath)
            sheet = wb["Auction Results"]

            # Add all sales from DB (including UNSOLD)
            if sales:
                for sale in sales:
                    team_code = sale.get("team_code", "Unknown")
                    # Skip UNSOLD entries - they go in separate sheet
                    if team_code == "UNSOLD":
                        continue
                    formatted_price = format_amount(sale.get("final_price", 0))
                    timestamp = sale.get(
                        "sold_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    )
                    sheet.append(
                        [
                            sale.get("player_name", "Unknown"),
                            team_code,
                            formatted_price,
                            timestamp,
                        ]
                    )

            wb.save(filepath)

            # Update Team Summary
            FileManager.update_team_summary(filepath, teams, team_squads)

            # Update Unsold Players sheet
            if unsold_players:
                FileManager.update_unsold_players_sheet(filepath, unsold_players)

            # Update Individual Team Sheets
            FileManager.update_individual_team_sheets(filepath, teams, team_squads)

        except Exception as e:
            raise Exception(f"Error regenerating Excel: {str(e)}")


# -----------------------------------------------------------
# MESSAGE FORMATTER
# -----------------------------------------------------------
class MessageFormatter:

    @staticmethod
    def format_purse_display(teams: Dict[str, int]) -> str:
        msg = "**Current Team Purses:**\n```\n"
        for t, p in sorted(teams.items()):
            msg += f"{t:6} : {format_amount(p)}\n"
        msg += "```"
        return msg

    @staticmethod
    def format_list_display(
        player_lists: Dict[str, List], list_order: List[str]
    ) -> str:
        """Format all lists for display"""
        if not player_lists:
            return "No player lists created yet."

        msg = "**Player Lists:**\n"
        for list_name in list_order:
            if list_name in player_lists:
                players = player_lists[list_name]
                msg += f"\n**{list_name}** ({len(players)} players):\n"
                if players:
                    # Show first 10 players
                    for i, (name, price) in enumerate(players[:10]):
                        price_str = f" - {format_amount(price)}" if price else ""
                        msg += f"  {i+1}. {name}{price_str}\n"
                    if len(players) > 10:
                        msg += f"  ... and {len(players) - 10} more\n"
                else:
                    msg += "  (empty)\n"

        # Show any lists not in order
        for list_name, players in player_lists.items():
            if list_name not in list_order:
                msg += f"\n**{list_name}** ({len(players)} players) [not in order]:\n"
                if players:
                    for i, (name, price) in enumerate(players[:10]):
                        price_str = f" - {format_amount(price)}" if price else ""
                        msg += f"  {i+1}. {name}{price_str}\n"
                    if len(players) > 10:
                        msg += f"  ... and {len(players) - 10} more\n"
                else:
                    msg += "  (empty)\n"

        return msg

    @staticmethod
    def format_player_announcement(player: str, base_price: Optional[int]) -> str:
        return (
            f"**Next Player: {player}**\n"
            f"Base Price: {format_amount(base_price)}\n"
            f"Teams can now bid using `/bid`"
        )

    @staticmethod
    def format_bid_message(team: str, amount: int, player: str) -> str:
        return f"**{team}** bids **{format_amount(amount)}** for **{player}**"

    @staticmethod
    def format_sold_message(player: str, team: str, amount: int) -> str:
        if team == "UNSOLD":
            return (
                f"**UNSOLD**\n"
                f"Player: **{player}**\n"
                f"Base Price: **{format_amount(amount)}**"
            )
        return (
            f"**SOLD!**\n"
            f"Player: **{player}**\n"
            f"Team: **{team}**\n"
            f"Final Price: **{format_amount(amount)}**"
        )

    @staticmethod
    def format_countdown(seconds: int) -> str:
        return f"**{seconds}** seconds remaining..."


def validate_team_name(team: str, teams: Dict[str, int]) -> Optional[str]:
    team_upper = team.upper()
    return team_upper if team_upper in teams else None


def calculate_next_bid(current_bid: int) -> int:
    from config import get_bid_increment

    return current_bid + get_bid_increment(current_bid)
