"""
Discord Auction Bot - Main Application
Modern bot with Slash Commands and User-Team Mapping
Users are assigned to teams and can simply use /bid
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
from typing import Optional, Dict
from dotenv import load_dotenv

load_dotenv()

from config import BOT_TOKEN, TEAMS, AUCTION_DATA_FILE, PLAYER_GAP, LIST_GAP, DEFAULT_COUNTDOWN
from auction_manager import AuctionManager
from utils import MessageFormatter, validate_team_name, format_amount

TOKEN = BOT_TOKEN or os.getenv("DISCORD_TOKEN", "")

# 15s delay for new sets (after the first one)
INITIAL_SET_DELAY = 15

class AuctionBot(commands.Bot):
    """Custom bot class with auction manager and slash commands"""

    def __init__(self):
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

        self.auction_manager = AuctionManager(TEAMS, AUCTION_DATA_FILE)
        self.formatter = MessageFormatter()
        self.countdown_channel: Optional[discord.TextChannel] = None
        self.countdown_task: Optional[asyncio.Task] = None

        self.user_teams: Dict[int, str] = {}

    async def setup_hook(self):
        print("Syncing slash commands globally...")
        await self.tree.sync()
        print("Slash commands synced!")

    async def on_ready(self):
        print(f"Logged in as {self.user.name} (ID: {self.user.id})")
        print("Bot is ready! Use /help to see all commands.")

        self.user_teams = self.auction_manager.db.get_all_user_teams()
        print(f"Loaded {len(self.user_teams)} user-team assignments from database.")

        if self.auction_manager.active and not self.auction_manager.paused:
            print("⚠️  WARNING: Auction was active before restart.")
            print("⚠️  Marking as paused for safety. Admin must /resume to continue.")
            self.auction_manager.paused = True
            self.auction_manager._save_state_to_db()

    async def update_stats_display(self):
        """Updates the persistent stats message"""
        channel_id = self.auction_manager.stats_channel_id
        if not channel_id:
            return

        channel = self.get_channel(channel_id)
        if not channel:
            return

        msg_content = self.auction_manager.get_stats_message()
        message_id = self.auction_manager.stats_message_id

        try:
            if message_id:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(content=msg_content)
                    return
                except discord.NotFound:
                    pass # Message deleted, send new one

            # Send new message if edit failed
            msg = await channel.send(msg_content)
            self.auction_manager.stats_message_id = msg.id
            self.auction_manager._save_state_to_db()
        except Exception as e:
            print(f"Error updating stats display: {e}")

bot = AuctionBot()


# ============================================================
# TEAM ASSIGNMENT COMMANDS
# ============================================================


@bot.tree.command(name="assignteam", description="Assign a user to a team (Admin only)")
@app_commands.describe(
    user="The user to assign", team="Team abbreviation (MI, CSK, RCB, etc.)"
)
@app_commands.checks.has_permissions(administrator=True)
async def assign_team(interaction: discord.Interaction, user: discord.User, team: str):
    team_upper = team.upper()
    if team_upper not in TEAMS:
        teams_list = ", ".join(TEAMS.keys())
        await interaction.response.send_message(
            f"Invalid team. Available teams: {teams_list}", ephemeral=True
        )
        return

    bot.auction_manager.db.set_user_team(user.id, team_upper, user.display_name)
    bot.user_teams[user.id] = team_upper
    await interaction.response.send_message(
        f"**{user.display_name}** is now assigned to **{team_upper}**"
    )


@bot.tree.command(
    name="unassignteam", description="Remove a user's team assignment (Admin only)"
)
@app_commands.describe(user="The user to unassign")
@app_commands.checks.has_permissions(administrator=True)
async def unassign_team(interaction: discord.Interaction, user: discord.User):
    if user.id in bot.user_teams:
        team = bot.user_teams.pop(user.id)
        bot.auction_manager.db.remove_user_team(user.id)
        await interaction.response.send_message(
            f"**{user.display_name}** removed from **{team}**"
        )
    else:
        await interaction.response.send_message(
            f"**{user.display_name}** has no team assignment", ephemeral=True
        )


@bot.tree.command(name="showteams", description="Show all user-team assignments")
async def show_teams(interaction: discord.Interaction):
    if not bot.user_teams:
        await interaction.response.send_message("No users assigned to teams yet.")
        return

    msg = "**Team Assignments:**\n```\n"
    for user_id, team in bot.user_teams.items():
        user = bot.get_user(user_id)
        name = user.display_name if user else f"User {user_id}"
        msg += f"{team:6} : {name}\n"
    msg += "```"
    await interaction.response.send_message(msg)


@bot.tree.command(name="myteam", description="Check which team you are assigned to")
async def my_team(interaction: discord.Interaction):
    team = bot.user_teams.get(interaction.user.id)
    if team:
        await interaction.response.send_message(
            f"You are assigned to **{team}**", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "You are not assigned to any team. Ask an admin to assign you with /assignteam",
            ephemeral=True,
        )


# ============================================================
# BIDDING COMMAND
# ============================================================


@bot.tree.command(
    name="bid", description="Place a bid for your team (auto-calculates amount)"
)
async def bid(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in bot.user_teams:
        await interaction.response.send_message(
            "You are not assigned to any team. Ask an admin to use `/assignteam`",
            ephemeral=True,
        )
        return

    team = bot.user_teams[user_id]

    result = await bot.auction_manager.place_bid(
        team, user_id, interaction.user.display_name, str(interaction.id)
    )

    if not result.success:
        await interaction.response.send_message(result.message, ephemeral=True)
        return

    player = bot.auction_manager.current_player

    if result.auto_bids_triggered:
        await interaction.response.send_message(
            f"✅ You bid **{format_amount(result.original_bid_amount)}** but were immediately outbid!",
            ephemeral=True,
        )

        msg = f"**{team}** bid **{format_amount(result.original_bid_amount)}**...\n"
        msg += f"⚡ **BUT WAS IMMEDIATELY OUTBID!**\n\n"
        for auto_bid in result.auto_bids_triggered:
            msg += f"• **{auto_bid['team']}** auto-bid: **{format_amount(auto_bid['amount'])}**\n"
        msg += f"\n🏆 Current winner: **{result.team}** at **{format_amount(result.amount)}**"
        await interaction.channel.send(msg)
    else:
        await interaction.response.send_message(
            f"✅ Bid placed: **{format_amount(result.amount)}**", ephemeral=True
        )
        bid_msg = bot.formatter.format_bid_message(result.team, result.amount, player)
        await interaction.channel.send(bid_msg)

    # Update stats
    asyncio.create_task(bot.update_stats_display())

    if not bot.countdown_task or bot.countdown_task.done():
        bot.countdown_task = asyncio.create_task(countdown_loop(interaction.channel))

@bot.tree.command(name="undobid", description="Undo the last bid (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def undo_bid(interaction: discord.Interaction):
    success, msg = bot.auction_manager.undo_last_bid()
    await interaction.response.send_message(msg)
    if success:
        # Update live stats
        asyncio.create_task(bot.update_stats_display())


@bot.tree.command(name="bidhistory", description="Show recent bid history")
@app_commands.describe(limit="Number of recent bids to show (default: 10)")
async def bid_history(interaction: discord.Interaction, limit: int = 10):
    history = bot.auction_manager.get_bid_history_display(limit=min(limit, 50))
    await interaction.response.send_message(history)


@bot.tree.command(name="teamsquad", description="Show players in your team")
async def team_squad(interaction: discord.Interaction):
    """Shows the squad for the user's assigned team"""
    user_id = interaction.user.id
    team_upper = bot.user_teams.get(user_id)

    if not team_upper:
        await interaction.response.send_message(
            "You are not assigned to any team. Ask an admin to assign you.", ephemeral=True
        )
        return

    squads = bot.auction_manager.team_squads
    teams_purse = bot.auction_manager.teams

    if team_upper not in squads or not squads[team_upper]:
        await interaction.response.send_message(
            f"**{team_upper}** has no players yet.\nRemaining Purse: {format_amount(teams_purse.get(team_upper, 0))}",
            ephemeral=True,
        )
        return

    squad = squads[team_upper]
    total_spent = sum(price for _, price in squad)

    msg = f"**{team_upper} Squad:**\n```\n"
    for player, price in squad:
        msg += f"{player:30} : {format_amount(price)}\n"
    msg += f"\n{'='*50}\n"
    msg += f"{'Total Spent':30} : {format_amount(total_spent)}\n"
    msg += f"{'Remaining Purse':30} : {format_amount(teams_purse.get(team_upper, 0))}\n"
    msg += f"{'Players Bought':30} : {len(squad)}\n"
    msg += "```"

    await interaction.response.send_message(msg)

@bot.tree.command(name="squad", description="View any team's squad (Admin only)")
@app_commands.describe(team="Team Code (e.g. MI, CSK)")
@app_commands.checks.has_permissions(administrator=True)
async def admin_squad(interaction: discord.Interaction, team: str):
    """Admin command to view any team's squad"""
    team_upper = team.upper()
    from config import TEAMS

    if team_upper not in TEAMS:
        await interaction.response.send_message(f"Invalid team: {team}", ephemeral=True)
        return

    squads = bot.auction_manager.team_squads
    teams_purse = bot.auction_manager.teams

    if team_upper not in squads or not squads[team_upper]:
        await interaction.response.send_message(
            f"**{team_upper}** has no players yet.\nRemaining Purse: {format_amount(teams_purse.get(team_upper, 0))}",
            ephemeral=True,
        )
        return

    squad = squads[team_upper]
    total_spent = sum(price for _, price in squad)

    msg = f"**{team_upper} Squad (Admin View):**\n```\n"
    for player, price in squad:
        msg += f"{player:30} : {format_amount(price)}\n"
    msg += f"\n{'='*50}\n"
    msg += f"{'Total Spent':30} : {format_amount(total_spent)}\n"
    msg += f"{'Remaining Purse':30} : {format_amount(teams_purse.get(team_upper, 0))}\n"
    msg += f"{'Players Bought':30} : {len(squad)}\n"
    msg += "```"

    await interaction.response.send_message(msg)


@bot.tree.command(name="rollback", description="Undo the last sale (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def rollback_sale(interaction: discord.Interaction):
    result = bot.auction_manager.rollback_last_sale()

    if result:
        msg = f"**Rollback successful!**\n"
        msg += f"Player: **{result['player_name']}**\n"
        msg += f"Team: **{result['team_code']}**\n"
        msg += f"Amount refunded: {format_amount(result['amount'])}\n"
        msg += "Excel file updated."
        
        asyncio.create_task(bot.update_stats_display())
        await interaction.response.send_message(msg)
    else:
        await interaction.response.send_message(
            "No recent sale to rollback.", ephemeral=True
        )


@bot.tree.command(
    name="release", description="Release a player from a team (Admin only)"
)
@app_commands.describe(
    team="Team code (MI, CSK, etc.)", player="Player name to release"
)
@app_commands.checks.has_permissions(administrator=True)
async def release_player(interaction: discord.Interaction, team: str, player: str):
    success, message = bot.auction_manager.release_retained_player(team, player)
    if success:
        asyncio.create_task(bot.update_stats_display())
    await interaction.response.send_message(message, ephemeral=not success)

@bot.tree.command(name="addtosquad", description="Manually add a player to a squad (Admin only)")
@app_commands.describe(team="Team Code", player="Player Name", price="Price in Rupees")
@app_commands.checks.has_permissions(administrator=True)
async def add_to_squad(interaction: discord.Interaction, team: str, player: str, price: int):
    success, msg = bot.auction_manager.manual_add_player(team, player, price)
    if success:
         asyncio.create_task(bot.update_stats_display())
    await interaction.response.send_message(msg)

@bot.tree.command(name="trade", description="Trade a player between teams (Admin only)")
@app_commands.describe(player="Player Name", from_team="Source Team", to_team="Target Team", price="Trade Value")
@app_commands.checks.has_permissions(administrator=True)
async def trade(interaction: discord.Interaction, player: str, from_team: str, to_team: str, price: int):
    success, msg = bot.auction_manager.trade_player(player, from_team, to_team, price)
    if success:
         asyncio.create_task(bot.update_stats_display())
    await interaction.response.send_message(msg)

@bot.tree.command(
    name="soldto", description="Manually mark current player as sold (Admin only)"
)
@app_commands.describe(team="Team code (MI, CSK, etc.)")
@app_commands.checks.has_permissions(administrator=True)
async def sold_to(interaction: discord.Interaction, team: str):
    if not bot.auction_manager.active:
        await interaction.response.send_message("No active auction", ephemeral=True)
        return

    if not bot.auction_manager.current_player:
        await interaction.response.send_message(
            "No player currently being auctioned", ephemeral=True
        )
        return

    team_upper = team.upper()
    from config import TEAMS

    if team_upper not in TEAMS:
        await interaction.response.send_message(f"Invalid team: {team}", ephemeral=True)
        return

    player_name = bot.auction_manager.current_player

    if bot.auction_manager.current_bid > 0:
        bot.auction_manager.highest_bidder = team_upper
        bot.auction_manager._save_state_to_db()

    success, winning_team, amount = bot.auction_manager.finalize_sale()

    if success:
        sold_msg = bot.formatter.format_sold_message(player_name, winning_team, amount)
        await interaction.response.send_message(sold_msg)
        await interaction.channel.send(bot.auction_manager.get_purse_display())
        
        asyncio.create_task(bot.update_stats_display())

        await asyncio.sleep(2)
        await start_next_player(interaction.channel)
    else:
        await interaction.response.send_message(
            "Failed to finalize sale", ephemeral=True
        )


@bot.tree.command(
    name="unsold", description="Mark current player as unsold (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def mark_unsold(interaction: discord.Interaction):
    if not bot.auction_manager.active or not bot.auction_manager.current_player:
        await interaction.response.send_message(
            "No active auction or player.", ephemeral=True
        )
        return

    player = bot.auction_manager.current_player
    await interaction.response.send_message(f"Player **{player}** marked UNSOLD")

    await asyncio.sleep(1)
    await start_next_player(interaction.channel)


@bot.tree.command(
    name="reauction", description="Add an unsold player back to auction (Admin only)"
)
@app_commands.describe(
    player_name="Name of the player to re-auction (uses original base price from CSV)"
)
@app_commands.checks.has_permissions(administrator=True)
async def reauction_player(interaction: discord.Interaction, player_name: str):
    success, message = bot.auction_manager.reauction_player(player_name)
    await interaction.response.send_message(message, ephemeral=not success)


# ============================================================
# LIST MANAGEMENT COMMANDS
# ============================================================


@bot.tree.command(name="addplayer", description="Add a player to a list")
@app_commands.describe(list_name="Name of the list", player_name="Player name to add")
async def add_player(
    interaction: discord.Interaction, list_name: str, player_name: str
):
    if bot.auction_manager.add_player_to_list(list_name, (player_name, None)):
        await interaction.response.send_message(
            f"Added **{player_name}** to list **{list_name}**"
        )
    else:
        await interaction.response.send_message(
            f"List **{list_name}** does not exist.", ephemeral=True
        )


@bot.tree.command(
    name="loadcsv", description="Load players from a CSV file (Admin only)"
)
@app_commands.describe(list_name="Name of the list", filepath="Full path to CSV file")
@app_commands.checks.has_permissions(administrator=True)
async def load_csv(interaction: discord.Interaction, list_name: str, filepath: str):
    await interaction.response.defer()
    filepath = filepath.strip().strip('"').strip("'")
    success, message = bot.auction_manager.load_list_from_csv(list_name, filepath)
    await interaction.followup.send(message)


@bot.tree.command(
    name="loadsets", description="Load players from IPL CSV by set number (Admin only)"
)
@app_commands.describe(max_set="Load players from sets 1 to this number (1-79)")
@app_commands.checks.has_permissions(administrator=True)
async def load_sets(interaction: discord.Interaction, max_set: int):
    await interaction.response.defer()

    if max_set < 1 or max_set > 79:
        await interaction.followup.send(
            "max_set must be between 1 and 79", ephemeral=True
        )
        return

    success, message = bot.auction_manager.load_players_from_sets(max_set)
    await interaction.followup.send(message)


@bot.tree.command(name="showlists", description="Display all lists and their contents")
async def show_lists(interaction: discord.Interaction):
    info = bot.auction_manager.get_list_info()
    if len(info) > 2000:
        await interaction.response.send_message(info[:2000])
        for chunk in [info[i : i + 2000] for i in range(2000, len(info), 2000)]:
            await interaction.followup.send(chunk)
    else:
        await interaction.response.send_message(info)


@bot.tree.command(
    name="setorder", description="Set the order of lists for auction (Admin only)"
)
@app_commands.describe(lists="List names separated by spaces")
@app_commands.checks.has_permissions(administrator=True)
async def set_order(interaction: discord.Interaction, lists: str):
    list_names = lists.split()
    if not list_names:
        await interaction.response.send_message(
            "Please provide list names.", ephemeral=True
        )
        return
    success, message = bot.auction_manager.set_list_order(list_names)
    await interaction.response.send_message(message)


# ============================================================
# AUCTION CONTROL COMMANDS
# ============================================================


@bot.tree.command(name="start", description="Start the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def start_auction(interaction: discord.Interaction):
    success, message = bot.auction_manager.start_auction()
    if not success:
        await interaction.response.send_message(message, ephemeral=True)
        return

    await interaction.response.send_message("**AUCTION STARTED!**")
    bot.countdown_channel = interaction.channel
    await asyncio.sleep(PLAYER_GAP)
    await start_next_player(interaction.channel)


@bot.tree.command(name="stop", description="Stop the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def stop_auction(interaction: discord.Interaction):
    if bot.auction_manager.stop_auction():
        if bot.countdown_task:
            bot.countdown_task.cancel()
            bot.countdown_task = None
        await interaction.response.send_message("**AUCTION STOPPED**")
    else:
        await interaction.response.send_message(
            "No auction is currently running.", ephemeral=True
        )


@bot.tree.command(name="pause", description="Pause the auction (Bulk Pause)")
@app_commands.checks.has_permissions(administrator=True)
async def pause_auction(interaction: discord.Interaction):
    if bot.auction_manager.pause_auction():
        await interaction.response.send_message("**AUCTION PAUSED (Bulk Pause)** - Timer stopped, next player won't start.")
    else:
        await interaction.response.send_message("Cannot pause auction.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def resume_auction(interaction: discord.Interaction):
    """Resume the auction"""
    if not bot.auction_manager.active:
        await interaction.response.send_message("Auction is not active to resume.", ephemeral=True)
        return

    if not bot.auction_manager.paused:
        await interaction.response.send_message("Auction is not paused.", ephemeral=True)
        return
    
    bot.auction_manager.paused = False
    bot.auction_manager._save_state_to_db()
    
    # Logic to handle resuming correctly
    # If a player was currently active, we announce and restart timer
    if bot.auction_manager.current_player:
        await interaction.response.send_message(
            f"Auction resumed! Bidding continues for **{bot.auction_manager.current_player}**"
        )
        # Restart the timer task if it died while paused
        if not bot.countdown_task or bot.countdown_task.done():
             bot.countdown_task = asyncio.create_task(countdown_loop(interaction.channel))
    else:
        # If no player was active, start next
        await interaction.response.send_message("Auction resumed! Finding next player...")
        await start_next_player(interaction.channel)


@bot.tree.command(
    name="skip", description="Skip current player - mark as unsold (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def skip_player(interaction: discord.Interaction):
    if not bot.auction_manager.active or not bot.auction_manager.current_player:
        await interaction.response.send_message(
            "No active auction or player.", ephemeral=True
        )
        return

    player = bot.auction_manager.current_player
    await interaction.response.send_message(f"Player **{player}** SKIPPED (unsold)")

    if bot.countdown_task:
        bot.countdown_task.cancel()

    await asyncio.sleep(1)
    await start_next_player(interaction.channel)


# ============================================================
# ADMIN SETTINGS COMMANDS
# ============================================================

@bot.tree.command(name="setstatschannel", description="Set channel for live stats updates (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def set_stats_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.auction_manager.set_stats_channel(channel.id)
    await interaction.response.send_message(f"Stats channel set to {channel.mention}. I will start updating stats there.")
    # Force an update immediately
    await bot.update_stats_display()


@bot.tree.command(
    name="setcountdown", description="Set countdown duration in seconds (Admin only)"
)
@app_commands.describe(seconds="Countdown duration (5-300)")
@app_commands.checks.has_permissions(administrator=True)
async def set_countdown(interaction: discord.Interaction, seconds: int):
    if bot.auction_manager.set_countdown(seconds):
        await interaction.response.send_message(
            f"Countdown set to **{seconds}** seconds"
        )
    else:
        await interaction.response.send_message(
            "Countdown must be between 5 and 300 seconds.", ephemeral=True
        )


@bot.tree.command(
    name="setpurse", description="Set a team's purse manually (Admin only)"
)
@app_commands.describe(team="Team abbreviation", amount="Purse amount in rupees")
@app_commands.checks.has_permissions(administrator=True)
async def set_purse(interaction: discord.Interaction, team: str, amount: int):
    team_validated = validate_team_name(team, bot.auction_manager.teams)
    if not team_validated:
        await interaction.response.send_message(
            f"Invalid team name: **{team}**", ephemeral=True
        )
        return
    if bot.auction_manager.set_team_purse(team_validated, amount):
        await interaction.response.send_message(
            f"Set **{team_validated}** purse to {format_amount(amount)}"
        )
    else:
        await interaction.response.send_message("Invalid amount.", ephemeral=True)


@bot.tree.command(
    name="clear", description="Clear all auction data and reset (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def clear_auction(interaction: discord.Interaction):
    bot.auction_manager.clear_all_data()
    bot.user_teams.clear()
    if bot.countdown_task:
        bot.countdown_task.cancel()
        bot.countdown_task = None
    await interaction.response.send_message(
        "**All auction data has been cleared and reset.**"
    )


# ============================================================
# INFO COMMANDS
# ============================================================


@bot.tree.command(name="showpurse", description="Display current team purses")
async def show_purse(interaction: discord.Interaction):
    await interaction.response.send_message(bot.auction_manager.get_purse_display())


@bot.tree.command(name="status", description="Show current auction status")
async def show_status(interaction: discord.Interaction):
    status = bot.auction_manager.get_status_display()
    await interaction.response.send_message(status)


@bot.tree.command(name="userhelp", description="Show commands for players/users")
async def user_help_command(interaction: discord.Interaction):
    help_text = """
**Discord Auction Bot - User Commands**

**My Team:**
`/myteam` - Check which team you are assigned to.
`/teamsquad` - View your team's current squad and purse.

**Bidding:**
`/bid` - Place a bid for the current player on behalf of your team.
`/bidhistory` - View the most recent bids placed.

**Info:**
`/showpurse` - See remaining purse for all teams.
`/status` - Check the current player and auction status.
`/showlists` - View the upcoming player lists.
"""
    await interaction.response.send_message(help_text)

@bot.tree.command(name="adminhelp", description="Show commands for Admins")
@app_commands.checks.has_permissions(administrator=True)
async def admin_help_command(interaction: discord.Interaction):
    help_text = """
**Discord Auction Bot - Admin Commands**

**Auction Control:**
`/start` - Start the auction.
`/stop` - Stop the auction completely.
`/pause` - Pause the auction (stops timer, prevents next player).
`/resume` - Resume the auction from where it left off.
`/soldto TEAM` - Manually sell the current player to a team.
`/unsold` - Mark the current player as unsold.
`/skip` - Skip the current player (same as unsold).
`/undobid` - Remove the last placed bid.
`/rollback` - Undo the last completed sale.
`/clear` - WIPE ALL DATA and reset.

**Team & Player Management:**
`/assignteam @user TEAM` - Assign a user to a team.
`/unassignteam @user` - Remove a user from a team.
`/setpurse TEAM amount` - Manually adjust a team's purse.
`/squad TEAM` - View any team's squad.
`/addtosquad TEAM player price` - Manually add a player to a team.
`/release TEAM player` - Remove a player from a team (refunds money).
`/trade player from_team to_team price` - Move a player between teams.
`/reauction player` - Bring an unsold player back into the auction.

**List Management:**
`/loadsets max_set` - Load players from CSV (Sets 1 to X).
`/addplayer list player` - Add a single player to a list.
`/setorder list1 list2` - Set the order of player lists.

**Settings:**
`/setcountdown seconds` - Change the bid timer duration.
`/setstatschannel #channel` - Set the channel for the live leaderboard.
"""
    await interaction.response.send_message(help_text)


# ============================================================
# HELPER FUNCTIONS
# ============================================================


async def start_next_player(channel: discord.TextChannel):
    """Start auctioning the next player"""
    
    # PAUSE CHECK: Do not proceed if auction is paused
    if bot.auction_manager.paused:
        return

    await asyncio.sleep(PLAYER_GAP)
    
    # Check again after sleep
    if bot.auction_manager.paused:
        return

    # Unpack 4 values
    result = bot.auction_manager.get_next_player()
    success, player_name, base_price, is_first_in_list = result

    if not success:
        current_list = bot.auction_manager.get_current_list_name()
        if current_list:
            await channel.send(f"**List {current_list} completed!**")
            await asyncio.sleep(LIST_GAP)
            await start_next_player(channel)
        else:
            await channel.send("**AUCTION COMPLETED!**")
            await channel.send(bot.auction_manager.get_purse_display())
            bot.auction_manager.active = False
        return

    if is_first_in_list:
        msg = bot.formatter.format_player_announcement(player_name, base_price)
        await channel.send(msg)
        
        # Determine delay based on auction phase
        # If list index is 0, it's the first set (Start of Auction)
        if bot.auction_manager.current_list_index == 0:
            delay = 5
            await channel.send(f"🚨 Auction starting! Bidding will open in **{delay} seconds**.")
        else:
            delay = INITIAL_SET_DELAY
            await channel.send(f"🚨 **{player_name}** is the first player of a new list. Bidding will open in **{delay} seconds**.")
        
        await asyncio.sleep(delay)
        
        bot.auction_manager.reset_last_bid_time()
        await channel.send(f"📣 **BIDDING OPEN!** {DEFAULT_COUNTDOWN} seconds on the clock.")
    else:
        announcement = bot.formatter.format_player_announcement(player_name, base_price)
        await channel.send(announcement)

    if not bot.countdown_task or bot.countdown_task.done():
        bot.countdown_task = asyncio.create_task(countdown_loop(channel))


async def countdown_loop(channel: discord.TextChannel):
    """Manual bidding timer - waits for no bid timeout or admin action"""
    import time as time_module
    from config import NO_BID_TIMEOUT, NO_START_TIMEOUT

    player_start_time = time_module.time()

    # Sync timestamp at start (or use existing if resumed)
    if bot.auction_manager.last_bid_time <= 0:
        bot.auction_manager.last_bid_time = player_start_time

    last_msg = None
    first_bid_placed = False

    while bot.auction_manager.active and not bot.auction_manager.paused:
        await asyncio.sleep(5)

        now = time_module.time()

        bot.auction_manager._load_state_from_db()
        current_player_name = bot.auction_manager.current_player

        if bot.auction_manager.highest_bidder is not None:
            first_bid_placed = True

        if not first_bid_placed:
            elapsed_since_start = now - player_start_time
            remaining = NO_START_TIMEOUT - int(elapsed_since_start)

            if remaining <= 0:
                await channel.send(
                    f"⏰ No bids in {NO_START_TIMEOUT}s - Player **{current_player_name}** goes UNSOLD"
                )
                await asyncio.sleep(2)
                asyncio.create_task(start_next_player(channel))
                return

            if int(elapsed_since_start) % 15 == 0:
                if last_msg:
                    try:
                        await last_msg.edit(
                            content=f"⏳ Waiting for first bid... ({remaining}s remaining before unsold)"
                        )
                    except:
                        last_msg = await channel.send(
                            f"⏳ Waiting for first bid... ({remaining}s remaining before unsold)"
                        )
                else:
                    last_msg = await channel.send(
                        f"⏳ Waiting for first bid... ({remaining}s remaining before unsold)"
                    )

        else:
            elapsed_since_last_bid = now - bot.auction_manager.last_bid_time
            remaining = NO_BID_TIMEOUT - int(elapsed_since_last_bid)

            if remaining <= 0:
                if last_msg:
                    try:
                        await last_msg.delete()
                    except:
                        pass

                player_name = bot.auction_manager.current_player
                success, team, amount = bot.auction_manager.finalize_sale()

                if success:
                    sold_msg = bot.formatter.format_sold_message(
                        player_name, team, amount
                    )
                    await channel.send(sold_msg)
                    await channel.send(bot.auction_manager.get_purse_display())
                    
                    asyncio.create_task(bot.update_stats_display())
                else:
                    await channel.send(f"Player **{player_name}** went UNSOLD.")

                await asyncio.sleep(2)
                asyncio.create_task(start_next_player(channel))
                return

            if int(elapsed_since_last_bid) % 10 == 0:
                if last_msg:
                    try:
                        await last_msg.edit(
                            content=f"⏳ Time since last bid: {int(elapsed_since_last_bid)}s / {NO_BID_TIMEOUT}s"
                        )
                    except:
                        last_msg = await channel.send(
                            f"⏳ Time since last bid: {int(elapsed_since_last_bid)}s / {NO_BID_TIMEOUT}s"
                        )
                else:
                    last_msg = await channel.send(
                        f"⏳ Time since last bid: {int(elapsed_since_last_bid)}s / {NO_BID_TIMEOUT}s"
                    )

        if bot.auction_manager.paused:
            if last_msg:
                try:
                    await last_msg.delete()
                except:
                    pass
            break


# ============================================================
# ERROR HANDLERS
# ============================================================


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
    elif isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"Command on cooldown. Try again in {error.retry_after:.1f}s",
            ephemeral=True,
        )
    else:
        print(f"Command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"An error occurred: {str(error)}", ephemeral=True
            )


# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == "__main__":
    token = BOT_TOKEN or os.getenv("DISCORD_TOKEN")
    if not token:
        print(
            "Please set your bot token in DISCORD_TOKEN environment variable or config.py"
        )
    else:
        bot.run(token)