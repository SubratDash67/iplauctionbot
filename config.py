"""
Configuration for Discord Auction Bot
"""

import os

# Bot token is loaded from environment variable DISCORD_TOKEN (put it in .env)
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Auction Settings
DEFAULT_COUNTDOWN = 120  # seconds - initial timer when bidding starts (2 mins)
NO_BID_TIMEOUT = 120  # seconds - if no bid in this time, player sold/unsold (2 mins)
NO_START_TIMEOUT = 60  # seconds - if bidding doesn't start, player unsold (1 min)
PLAYER_GAP = 20  # seconds gap between players
LIST_GAP = 20  # seconds gap between lists
MANUAL_SELL_COOLDOWN = 15  # seconds - minimum time before admin can /soldto
DEFAULT_BASE_PRICE = 2000000  # 20 Lakh base price
DEFAULT_PURSE = 1200000000  # 120 Crore per team (120 * 10^7)

# CSV File Path
DEFAULT_CSV_FILE = "1731674068078_TATA IPL 2025- Auction List -15.11.24.csv"

# Team Configuration
TEAMS = {
    "MI": DEFAULT_PURSE,
    "CSK": DEFAULT_PURSE,
    "RCB": DEFAULT_PURSE,
    "KKR": DEFAULT_PURSE,
    "SRH": DEFAULT_PURSE,
    "RR": DEFAULT_PURSE,
    "DC": DEFAULT_PURSE,
    "PBKS": DEFAULT_PURSE,
    "GT": DEFAULT_PURSE,
    "LSG": DEFAULT_PURSE,
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
