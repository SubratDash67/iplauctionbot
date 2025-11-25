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

from config import BOT_TOKEN, TEAMS, AUCTION_DATA_FILE, PLAYER_GAP, LIST_GAP
from auction_manager import AuctionManager
from utils import MessageFormatter, validate_team_name, format_amount

TOKEN = BOT_TOKEN or os.getenv("DISCORD_TOKEN", "")


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

        # User ID to Team mapping - admin assigns users to teams
        self.user_teams: Dict[int, str] = {}

    async def setup_hook(self):
        """Called when the bot is starting up - syncs slash commands"""
        print("Syncing slash commands globally...")
        await self.tree.sync()
        print("Slash commands synced!")

    async def on_ready(self):
        """Called when the bot is ready"""
        print(f"Logged in as {self.user.name} (ID: {self.user.id})")
        print("Bot is ready! Use /help to see all commands.")

        # ZOMBIE STATE FIX: Handle crash/restart during active auction
        if self.auction_manager.active and not self.auction_manager.paused:
            print("⚠️  WARNING: Auction was active before restart.")
            print("⚠️  Marking as inactive for safety. Admin must /start to resume.")
            self.auction_manager.active = False
            self.auction_manager._save_state_to_db()


bot = AuctionBot()


# ============================================================
# TEAM ASSIGNMENT COMMANDS (Admin assigns users to teams)
# ============================================================


@bot.tree.command(name="assignteam", description="Assign a user to a team (Admin only)")
@app_commands.describe(
    user="The user to assign", team="Team abbreviation (MI, CSK, RCB, etc.)"
)
@app_commands.checks.has_permissions(administrator=True)
async def assign_team(interaction: discord.Interaction, user: discord.User, team: str):
    """Assign a Discord user to an IPL team"""
    team_upper = team.upper()
    if team_upper not in TEAMS:
        teams_list = ", ".join(TEAMS.keys())
        await interaction.response.send_message(
            f"Invalid team. Available teams: {teams_list}", ephemeral=True
        )
        return

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
    """Remove a user's team assignment"""
    if user.id in bot.user_teams:
        team = bot.user_teams.pop(user.id)
        await interaction.response.send_message(
            f"**{user.display_name}** removed from **{team}**"
        )
    else:
        await interaction.response.send_message(
            f"**{user.display_name}** has no team assignment", ephemeral=True
        )


@bot.tree.command(name="showteams", description="Show all user-team assignments")
async def show_teams(interaction: discord.Interaction):
    """Display all user-team mappings"""
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
    """Check your own team assignment"""
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
# BIDDING COMMAND (Main feature - just /bid, no team needed)
# ============================================================


@bot.tree.command(
    name="bid", description="Place a bid for your team (auto-calculates amount)"
)
async def bid(interaction: discord.Interaction):
    """Place a bid - automatically uses user's assigned team"""
    user_id = interaction.user.id

    # Check if user is assigned to a team
    if user_id not in bot.user_teams:
        await interaction.response.send_message(
            "You are not assigned to any team. Ask an admin to use `/assignteam`",
            ephemeral=True,
        )
        return

    team = bot.user_teams[user_id]

    # Place the bid (async with atomic lock)
    result = await bot.auction_manager.place_bid(
        team, user_id, interaction.user.display_name, str(interaction.id)
    )

    if not result.success:
        await interaction.response.send_message(result.message, ephemeral=True)
        return

    player = bot.auction_manager.current_player

    # Clear user feedback: distinguish their bid from resulting state
    if result.auto_bids_triggered:
        # User was immediately outbid by auto-bids
        await interaction.response.send_message(
            f"✅ You bid **{format_amount(result.original_bid_amount)}** but were immediately outbid!",
            ephemeral=True,
        )

        # Public announcement showing the cascade
        msg = f"**{team}** bid **{format_amount(result.original_bid_amount)}**...\n"
        msg += f"⚡ **BUT WAS IMMEDIATELY OUTBID!**\n\n"
        for auto_bid in result.auto_bids_triggered:
            msg += f"• **{auto_bid['team']}** auto-bid: **{format_amount(auto_bid['amount'])}**\n"
        msg += f"\n🏆 Current winner: **{result.team}** at **{format_amount(result.amount)}**"
        await interaction.channel.send(msg)
    else:
        # Normal bid - user is winning
        await interaction.response.send_message(
            f"✅ Bid placed: **{format_amount(result.amount)}**", ephemeral=True
        )
        bid_msg = bot.formatter.format_bid_message(result.team, result.amount, player)
        await interaction.channel.send(bid_msg)

    # Timer resets automatically via timestamp in auction_manager

    # Start countdown task if not running
    if not bot.countdown_task or bot.countdown_task.done():
        bot.countdown_task = asyncio.create_task(countdown_loop(interaction.channel))


@bot.tree.command(name="bidhistory", description="Show recent bid history")
@app_commands.describe(limit="Number of recent bids to show (default: 10)")
async def bid_history(interaction: discord.Interaction, limit: int = 10):
    """Show recent bid history"""
    history = bot.auction_manager.get_bid_history_display(limit=min(limit, 50))
    await interaction.response.send_message(history)


@bot.tree.command(name="teamsquad", description="Show players bought by a team")
@app_commands.describe(team="Team code (MI, CSK, etc.)")
async def team_squad(interaction: discord.Interaction, team: str):
    """Show all players bought by a specific team"""
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

    msg = f"**{team_upper} Squad:**\n```\n"
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
    """Rollback the last player sale"""
    result = bot.auction_manager.rollback_last_sale()

    if result:
        msg = f"**Rollback successful!**\n"
        msg += f"Player: **{result['player_name']}**\n"
        msg += f"Team: **{result['team_code']}**\n"
        msg += f"Amount refunded: {format_amount(result['amount'])}\n"
        await interaction.response.send_message(msg)
    else:
        await interaction.response.send_message(
            "No recent sale to rollback.", ephemeral=True
        )


@bot.tree.command(
    name="release", description="Release a retained player to auction (Admin only)"
)
@app_commands.describe(
    team="Team code (MI, CSK, etc.)", player="Player name to release"
)
@app_commands.checks.has_permissions(administrator=True)
async def release_player(interaction: discord.Interaction, team: str, player: str):
    """Release a retained player back into auction pool"""
    success, message = bot.auction_manager.release_retained_player(team, player)
    await interaction.response.send_message(message, ephemeral=not success)


@bot.tree.command(
    name="soldto", description="Manually mark current player as sold (Admin only)"
)
@app_commands.describe(team="Team code (MI, CSK, etc.)")
@app_commands.checks.has_permissions(administrator=True)
async def sold_to(interaction: discord.Interaction, team: str):
    """Manually finalize sale to a specific team"""
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

    # Set highest bidder if there's a current bid
    if bot.auction_manager.current_bid > 0:
        bot.auction_manager.highest_bidder = team_upper

    success, winning_team, amount = bot.auction_manager.finalize_sale()

    if success:
        sold_msg = bot.formatter.format_sold_message(
            bot.auction_manager.current_player, winning_team, amount
        )
        await interaction.response.send_message(sold_msg)
        await interaction.channel.send(bot.auction_manager.get_purse_display())

        # Move to next player
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
    """Mark current player as unsold and move to next"""
    if not bot.auction_manager.active or not bot.auction_manager.current_player:
        await interaction.response.send_message(
            "No active auction or player.", ephemeral=True
        )
        return

    player = bot.auction_manager.current_player
    await interaction.response.send_message(f"Player **{player}** marked UNSOLD")

    await asyncio.sleep(1)
    await start_next_player(interaction.channel)


# ============================================================
# LIST MANAGEMENT COMMANDS
# ============================================================


@bot.tree.command(name="createlist", description="Create a new player list")
@app_commands.describe(name="Name of the list to create")
async def create_list(interaction: discord.Interaction, name: str):
    """Create a new player list"""
    if bot.auction_manager.create_list(name):
        await interaction.response.send_message(f"Created list: **{name}**")
    else:
        await interaction.response.send_message(
            f"List **{name}** already exists.", ephemeral=True
        )


@bot.tree.command(name="addplayer", description="Add a player to a list")
@app_commands.describe(list_name="Name of the list", player_name="Player name to add")
async def add_player(
    interaction: discord.Interaction, list_name: str, player_name: str
):
    """Add a player to a specific list"""
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
    """Load players from a CSV file into a list"""
    await interaction.response.defer()
    # Strip quotes from filepath if present
    filepath = filepath.strip().strip('"').strip("'")
    success, message = bot.auction_manager.load_list_from_csv(list_name, filepath)
    await interaction.followup.send(message)


@bot.tree.command(name="showlists", description="Display all lists and their contents")
async def show_lists(interaction: discord.Interaction):
    """Display all lists and their contents"""
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
    """Set the order in which lists will be auctioned"""
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
    """Start the auction"""
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
    """Stop the auction"""
    if bot.auction_manager.stop_auction():
        if bot.countdown_task:
            bot.countdown_task.cancel()
            bot.countdown_task = None
        await interaction.response.send_message("**AUCTION STOPPED**")
    else:
        await interaction.response.send_message(
            "No auction is currently running.", ephemeral=True
        )


@bot.tree.command(name="pause", description="Pause the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def pause_auction(interaction: discord.Interaction):
    """Pause the auction"""
    if bot.auction_manager.pause_auction():
        await interaction.response.send_message("**AUCTION PAUSED**")
    else:
        await interaction.response.send_message("Cannot pause auction.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def resume_auction(interaction: discord.Interaction):
    """Resume the auction"""
    if bot.auction_manager.resume_auction():
        await interaction.response.send_message("**AUCTION RESUMED**")
        bot.countdown_channel = interaction.channel
        if not bot.countdown_task or bot.countdown_task.done():
            bot.countdown_task = asyncio.create_task(
                countdown_loop(interaction.channel)
            )
    else:
        await interaction.response.send_message(
            "Cannot resume auction.", ephemeral=True
        )


@bot.tree.command(
    name="skip", description="Skip current player - mark as unsold (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def skip_player(interaction: discord.Interaction):
    """Skip current player and move to next"""
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


@bot.tree.command(
    name="setcountdown", description="Set countdown duration in seconds (Admin only)"
)
@app_commands.describe(seconds="Countdown duration (5-300)")
@app_commands.checks.has_permissions(administrator=True)
async def set_countdown(interaction: discord.Interaction, seconds: int):
    """Set the countdown duration"""
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
    """Set a team's purse manually"""
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
    """Clear all auction data and reset"""
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
    """Display current team purses"""
    await interaction.response.send_message(bot.auction_manager.get_purse_display())


@bot.tree.command(name="status", description="Show current auction status")
async def show_status(interaction: discord.Interaction):
    """Show current auction status"""
    status = bot.auction_manager.get_status_display()
    await interaction.response.send_message(status)


@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    """Display all available commands"""
    help_text = """
**Discord Auction Bot - Slash Commands**

**Team Setup (Admin):**
`/assignteam @user TEAM` - Assign user to a team
`/unassignteam @user` - Remove user's team
`/showteams` - Show all assignments
`/myteam` - Check your team

**List Management:**
`/createlist name` - Create player list
`/addplayer list player` - Add player
`/loadcsv list filepath` - Load from CSV
`/showlists` - Display all lists
`/setorder lists` - Set auction order

**Auction Control (Admin):**
`/start` - Start auction
`/stop` - Stop auction
`/pause` - Pause auction
`/resume` - Resume auction
`/soldto TEAM` - Manually finalize sale to team
`/unsold` - Mark player unsold
`/clear` - Clear all data

**Bidding:**
`/bid` - Place a bid (uses your assigned team automatically)
`/bidhistory [limit]` - Show recent bid history
`/teamsquad TEAM` - Show players bought by a team

**Admin Actions:**
`/rollback` - Undo the last sale
`/release TEAM PLAYER` - Release retained player to auction

**Settings (Admin):**
`/setcountdown seconds` - Set countdown
`/setpurse TEAM amount` - Set purse

**Info:**
`/showpurse` - Display purses
`/status` - Show status
`/help` - Show commands

**Teams:** MI, CSK, RCB, KKR, SRH, RR, DC, PBKS, GT, LSG
"""
    await interaction.response.send_message(help_text)


# ============================================================
# HELPER FUNCTIONS
# ============================================================


async def start_next_player(channel: discord.TextChannel):
    """Start auctioning the next player"""
    await asyncio.sleep(PLAYER_GAP)

    next_player = bot.auction_manager.get_next_player()

    if not next_player:
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

    player_name, base_price = next_player
    announcement = bot.formatter.format_player_announcement(player_name, base_price)
    await channel.send(announcement)

    if not bot.countdown_task or bot.countdown_task.done():
        bot.countdown_task = asyncio.create_task(countdown_loop(channel))


async def countdown_loop(channel: discord.TextChannel):
    """Manual bidding timer - waits for no bid timeout or admin action"""
    import time as time_module
    from config import NO_BID_TIMEOUT, NO_START_TIMEOUT

    # Track when player was announced
    player_start_time = time_module.time()

    # Sync timestamp at start of countdown (for first bid)
    bot.auction_manager.last_bid_time = player_start_time

    last_msg = None
    last_update_time = 0
    first_bid_placed = False

    while bot.auction_manager.active and not bot.auction_manager.paused:
        await asyncio.sleep(5)  # Check every 5 seconds (rate limit friendly)

        now = time_module.time()

        # Check if any bid has been placed
        if bot.auction_manager.current_bid > bot.auction_manager.base_price:
            first_bid_placed = True

        if not first_bid_placed:
            # No bid yet - check NO_START_TIMEOUT (60s)
            elapsed_since_start = now - player_start_time
            remaining = NO_START_TIMEOUT - int(elapsed_since_start)

            if remaining <= 0:
                # No bids in 60 seconds - mark unsold
                await channel.send(
                    f"⏰ No bids in {NO_START_TIMEOUT}s - Player **{bot.auction_manager.current_player}** goes UNSOLD"
                )
                await asyncio.sleep(2)
                asyncio.create_task(start_next_player(channel))
                return

            # Update message every 15 seconds
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
            # Bid placed - check NO_BID_TIMEOUT (120s) since last bid
            elapsed_since_last_bid = now - bot.auction_manager.last_bid_time
            remaining = NO_BID_TIMEOUT - int(elapsed_since_last_bid)

            if remaining <= 0:
                # No new bid in 120 seconds - finalize sale
                if last_msg:
                    try:
                        await last_msg.delete()
                    except:
                        pass

                success, team, amount = bot.auction_manager.finalize_sale()

                if success:
                    sold_msg = bot.formatter.format_sold_message(
                        bot.auction_manager.current_player, team, amount
                    )
                    await channel.send(sold_msg)
                    await channel.send(bot.auction_manager.get_purse_display())
                else:
                    await channel.send(
                        f"Player **{bot.auction_manager.current_player}** went UNSOLD."
                    )

                await asyncio.sleep(2)
                asyncio.create_task(start_next_player(channel))
                return

            # Update message showing time since last bid
            if int(elapsed_since_last_bid) % 10 == 0:  # Every 10 seconds
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
    """Global error handler for slash commands"""
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
