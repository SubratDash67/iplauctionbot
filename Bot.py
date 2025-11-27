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


# ============================================================
# SQUAD / TEAM DISPLAY COMMANDS
# ============================================================

@bot.tree.command(name="mysquad", description="Show your assigned team's squad")
async def my_squad(interaction: discord.Interaction):
    user_id = interaction.user.id
    team = bot.user_teams.get(user_id)
    if not team:
        await interaction.response.send_message(
            "You are not assigned to any team. Ask an admin to assign you.", ephemeral=True
        )
        return

    squads = bot.auction_manager.team_squads
    teams_purse = bot.auction_manager.teams

    if team not in squads or not squads[team]:
        await interaction.response.send_message(
            f"**{team}** has no players yet.\nRemaining Purse: {format_amount(teams_purse.get(team, 0))}",
            ephemeral=True,
        )
        return

    squad = squads[team]
    total_spent = sum(price for _, price in squad)

    msg = f"**{team} Squad:**\n```\n"
    for player, price in squad:
        msg += f"{player:30} : {format_amount(price)}\n"
    msg += f"\n{'='*50}\n"
    msg += f"{'Total Spent':30} : {format_amount(total_spent)}\n"
    msg += f"{'Remaining Purse':30} : {format_amount(teams_purse.get(team, 0))}\n"
    msg += f"{'Players Bought':30} : {len(squad)}\n"
    msg += "```"

    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="teamsquad", description="View any team's squad (optional team parameter)")
@app_commands.describe(team="Team Code (e.g. MI, CSK). If omitted, shows your team.")
async def teamsquad(interaction: discord.Interaction, team: Optional[str] = None):
    """Show a team's squad. If team not provided shows invoker's team."""
    # If team provided, validate; otherwise use user's assigned team
    if team:
        team_upper = team.upper()
        if team_upper not in TEAMS:
            await interaction.response.send_message(f"Invalid team: {team_upper}", ephemeral=True)
            return
    else:
        team_upper = bot.user_teams.get(interaction.user.id)
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


@bot.tree.command(name="deleteset", description="Delete a player set/list (Admin only)")
@app_commands.describe(set_name="Name of the set/list to delete (exact match)")
@app_commands.checks.has_permissions(administrator=True)
async def delete_set(interaction: discord.Interaction, set_name: str):
    try:
        deleted = bot.auction_manager.db.delete_list(set_name)
        if deleted:
            await interaction.response.send_message(f"Deleted set '{set_name}' and {deleted} player(s).")
        else:
            await interaction.response.send_message(f"Set '{set_name}' not found or already empty.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error deleting set: {e}", ephemeral=True)


# ============================================================
# (other commands remain mostly unchanged)
# ... (the rest of Bot.py stays as before; only resume changed below)
# ============================================================


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
    
    # Use AuctionManager's resume (it also clears current_player if already sold)
    resumed = bot.auction_manager.resume_auction()
    if not resumed:
        await interaction.response.send_message("Failed to resume auction.", ephemeral=True)
        return

    # After resume, re-check whether current player exists and whether it was already sold
    if bot.auction_manager.current_player:
        # double-check DB to avoid resuming into a player that's already sold
        squads = bot.auction_manager.team_squads
        sold = False
        for squad in squads.values():
            for name, _ in squad:
                if name.lower() == bot.auction_manager.current_player.lower():
                    sold = True
                    break
            if sold:
                break

        if sold:
            # Clear and move to next player
            bot.auction_manager._reset_player_state()
            bot.auction_manager._save_state_to_db()
            await interaction.response.send_message("Auction resumed: previous player was already sold. Moving to next player.")
            await start_next_player(interaction.channel)
            return
        else:
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


# (the rest of the file below is unchanged from your original Bot.py)
# Please merge this file content with the remaining functions (start_next_player, countdown_loop, error handlers, __main__ start)