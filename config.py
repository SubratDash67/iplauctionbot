"""
Configuration for Discord Auction Bot
"""

import os

# Bot token is loaded from environment variable DISCORD_TOKEN (put it in .env)
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Auction Settings - Timing Configuration
# =========================================
DEFAULT_COUNTDOWN = 120  # Initial timer when bidding starts (2 minutes)
NO_BID_TIMEOUT = 15  # After first bid, sell after this many seconds of no new bids

# GAP SETTINGS
# -----------------
# Delay between the last bid and when the countdown logic (Going Once/Twice) actually starts.
# 0 = No gap (Timer starts immediately after bid)
# 5 = Wait 5 seconds after bid, THEN start the 15s countdown
COUNTDOWN_GAP = 0

# Time to wait between the sale of one player and the start of the next
PLAYER_GAP = 5

NO_START_TIMEOUT = 60  # If NO bids at all, player goes unsold after this time
LIST_GAP = 5  # Seconds between sets/lists
RESUME_DELAY = 5  # Delay after auction started/resumed message

# Countdown Warning Thresholds (for "Going Once/Twice/Thrice")
MANUAL_SELL_COOLDOWN = 15  # Min time before admin can use /soldto
GOING_ONCE_TIME = 15  # Seconds remaining when "Going Once" shows
GOING_TWICE_TIME = 10  # Seconds remaining when "Going Twice" shows
GOING_THRICE_TIME = 5  # Seconds remaining when "Going Thrice" shows
BIDDING_OPEN_WARNING_TIME = 15  # Show "bidding open" after this if no bids

# Default Values
DEFAULT_BASE_PRICE = 2000000  # 20 Lakh base price
DEFAULT_PURSE = 1250000000  # 125 Crore per team (for retained player calculations)

# Auction Data File Path (Excel)
DEFAULT_AUCTION_FILE = "Auction_list.xlsx"

# Team Configuration - Using official IPL 2026 remaining purse values
# Source: Official IPL 2026 Salary Cap data
TEAMS = {
    "MI": 27500000,  # 2.75 Cr remaining
    "CSK": 434000000,  # 43.4 Cr remaining
    "RCB": 164000000,  # 16.4 Cr remaining
    "KKR": 643000000,  # 64.3 Cr remaining
    "SRH": 255000000,  # 25.5 Cr remaining
    "RR": 160500000,  # 16.05 Cr remaining
    "DC": 218000000,  # 21.8 Cr remaining
    "PBKS": 115000000,  # 11.5 Cr remaining
    "GT": 129000000,  # 12.9 Cr remaining
    "LSG": 229500000,  # 22.95 Cr remaining
}

# Maximum squad size per IPL rules
MAX_SQUAD_SIZE = 25

# Team Slot Configuration - AVAILABLE overseas and total slots to fill
# Source: Official IPL 2026 data
# Note: "overseas" = remaining overseas slots available to buy
#       "total" = remaining total slots available to buy
TEAM_SLOTS = {
    "CSK": {"overseas": 4, "total": 9},
    "DC": {"overseas": 5, "total": 8},
    "GT": {"overseas": 4, "total": 5},
    "KKR": {"overseas": 6, "total": 13},
    "LSG": {"overseas": 4, "total": 6},
    "MI": {"overseas": 1, "total": 5},
    "PBKS": {"overseas": 2, "total": 4},
    "RCB": {"overseas": 2, "total": 8},
    "RR": {"overseas": 1, "total": 9},
    "SRH": {"overseas": 2, "total": 10},
}


# Bid Increment Rules
def get_bid_increment(current_bid: int) -> int:
    """Calculate the next bid increment based on current bid amount"""
    # IPL-like increments:
    # < 1 Cr -> +5 Lakh
    # 1 - 2 Cr -> +10 Lakh
    # 2 - 5 Cr -> +20 Lakh
    # > 5 Cr -> +25 Lakh
    if current_bid < 10_000_000:
        return 500_000
    elif current_bid < 20_000_000:
        return 1_000_000
    elif current_bid < 50_000_000:
        return 2_000_000
    else:
        return 2_500_000


# File Paths (you can change)
AUCTION_DATA_FILE = "auction_data.xlsx"
BACKUP_DATA_FILE = "auction_backup.xlsx"

# Messages
MESSAGES = {
    "auction_start": "Auction has started!",
    "auction_stop": "Auction has been stopped.",
    "auction_pause": "Auction has been paused.",
    "auction_resume": "Auction has been resumed.",
    "auction_complete": "Auction completed! All players have been processed.",
    "no_funds": "Insufficient purse balance for this bid.",
    "invalid_team": "Invalid team name. Please check and try again.",
    "player_sold": "Player SOLD to {team} for {amount}",
    "player_unsold": "Player {player} went UNSOLD.",
    "countdown_started": "Starting countdown... {seconds} seconds remaining",
    "list_complete": "List {list_name} has been completed.",
}
