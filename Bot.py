# Bot.py
"""
Discord Auction Bot - Main Application
Modern bot with Slash Commands and User-Team Mapping
Users are assigned to teams and can simply use /bid
Single persistent paginated trade-log message (Prev/Next)
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import time
import logging
from typing import Optional, Dict, List
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("auction_bot.log", encoding="utf-8"),
        logging.StreamHandler(),  # Also print to console
    ],
)
logger = logging.getLogger("AuctionBot")

from config import (
    BOT_TOKEN,
    TEAMS,
    AUCTION_DATA_FILE,
    PLAYER_GAP,
    LIST_GAP,
    DEFAULT_COUNTDOWN,
    RESUME_DELAY,
)
from auction_manager import AuctionManager
from utils import MessageFormatter, validate_team_name, format_amount

TOKEN = BOT_TOKEN or os.getenv("DISCORD_TOKEN", "")

# 5s delay for new sets (after the first one)
INITIAL_SET_DELAY = 5


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

        # Background tasks set - prevents 'Task destroyed but pending' warnings
        self._background_tasks: set = set()

        # Dynamic player gap (can be changed at runtime) - instance variable
        self.player_gap = PLAYER_GAP

        self.user_teams: Dict[int, str] = {}

    def create_background_task(self, coro):
        """Create a background task that cleans up after itself."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def cancel_countdown_task(self):
        """Safely cancel the countdown task with proper cleanup"""
        if self.countdown_task and not self.countdown_task.done():
            self.countdown_task.cancel()
            try:
                await self.countdown_task
            except asyncio.CancelledError:
                pass
            self.countdown_task = None

    async def setup_hook(self):
        logger.info("Syncing slash commands globally...")
        await self.tree.sync()
        logger.info("Slash commands synced!")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name} (ID: {self.user.id})")
        logger.info("Bot is ready! Use /help to see all commands.")

        self.user_teams = self.auction_manager.db.get_all_user_teams()
        logger.info(
            f"Loaded {len(self.user_teams)} user-team assignments from database."
        )

        if self.auction_manager.active and not self.auction_manager.paused:
            logger.warning(
                "Auction was active before restart. Marking as paused for safety."
            )
            logger.warning("Admin must use /resume to continue.")
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
                    pass  # Message deleted, send new one

            # Send new message if edit failed
            msg = await channel.send(msg_content)
            self.auction_manager.stats_message_id = msg.id
            self.auction_manager._save_state_to_db()
        except Exception as e:
            logger.error(f"Error updating stats display: {e}")


bot = AuctionBot()

# Defensive: attempt to remove old registrations to avoid CommandAlreadyRegistered
# (helps when reloading during development)
for cmdname in ("settradechannel", "tradelog"):
    try:
        bot.tree.remove_command(cmdname)
    except Exception:
        pass


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
    name="assignteams",
    description="Assign multiple users to teams at once (Admin only)",
)
@app_commands.describe(
    assignments="Format: @user1:TEAM1, @user2:TEAM2 (e.g., @John:MI, @Jane:CSK)"
)
@app_commands.checks.has_permissions(administrator=True)
async def assign_teams_bulk(interaction: discord.Interaction, assignments: str):
    """Assign multiple users to teams at once"""
    await interaction.response.defer()

    # Parse assignments - we need to extract user mentions and teams
    # Format: @user:TEAM, @user2:TEAM2
    entries = [e.strip() for e in assignments.split(",") if e.strip()]

    assigned = []
    failed = []

    for entry in entries:
        if ":" not in entry:
            failed.append(f"{entry} (invalid format)")
            continue

        parts = entry.rsplit(":", 1)
        user_part = parts[0].strip()
        team_part = parts[1].strip().upper()

        if team_part not in TEAMS:
            failed.append(f"{entry} (invalid team {team_part})")
            continue

        import re

        match = re.search(r"<@!?(\d+)>", user_part)
        if match:
            user_id = int(match.group(1))
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if user:
                bot.auction_manager.db.set_user_team(
                    user.id, team_part, user.display_name
                )
                bot.user_teams[user.id] = team_part
                assigned.append(f"{user.display_name} → {team_part}")
            else:
                failed.append(f"{entry} (user not found)")
        else:
            failed.append(f"{entry} (invalid user mention)")

    msg = f"**Assigned {len(assigned)} users to teams:**\n"
    if assigned:
        msg += "\n".join(f"✅ {a}" for a in assigned)
    if failed:
        msg += f"\n\n**Failed ({len(failed)}):**\n" + "\n".join(
            f"❌ {f}" for f in failed
        )

    await interaction.followup.send(msg)


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
@app_commands.checks.cooldown(1, 2.0, key=lambda i: i.user.id)
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
        error_embed = discord.Embed(
            title="❌ Bid Failed", description=result.message, color=discord.Color.red()
        )
        await interaction.response.send_message(embed=error_embed, ephemeral=True)
        return

    player = bot.auction_manager.current_player
    current_set = bot.auction_manager.get_current_list_name()

    if result.auto_bids_triggered:
        await interaction.response.send_message(
            f"✅ You bid **{format_amount(result.original_bid_amount)}** but were immediately outbid!",
            ephemeral=True,
        )

        embed = discord.Embed(
            title="⚡ AUTO-BID TRIGGERED!", color=discord.Color.orange()
        )
        embed.add_field(
            name="Initial Bid",
            value=f"**{team}** bid {format_amount(result.original_bid_amount)}",
            inline=False,
        )
        auto_bid_text = "\n".join(
            f"• **{ab['team']}**: {format_amount(ab['amount'])}"
            for ab in result.auto_bids_triggered
        )
        embed.add_field(name="Auto-Bids", value=auto_bid_text, inline=False)
        embed.add_field(
            name="🏆 Current Leader",
            value=f"**{result.team}** at **{format_amount(result.amount)}**",
            inline=False,
        )
        await interaction.channel.send(embed=embed)
    else:
        await interaction.response.send_message(
            f"✅ Bid placed: **{format_amount(result.amount)}**", ephemeral=True
        )

        bid_embed = discord.Embed(title=f"💰 New Bid!", color=discord.Color.green())
        bid_embed.add_field(name="Team", value=f"**{result.team}**", inline=True)
        bid_embed.add_field(
            name="Amount", value=f"**{format_amount(result.amount)}**", inline=True
        )
        bid_embed.add_field(name="Player", value=f"**{player}**", inline=True)
        if current_set:
            bid_embed.set_footer(text=f"Set: {current_set.upper()}")
        await interaction.channel.send(embed=bid_embed)

    bot.create_background_task(bot.update_stats_display())

    if not bot.countdown_task or bot.countdown_task.done():
        bot.countdown_task = asyncio.create_task(countdown_loop(interaction.channel))


@bot.tree.command(name="undobid", description="Undo the last bid (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def undo_bid(interaction: discord.Interaction):
    success, msg = await bot.auction_manager.undo_last_bid()
    await interaction.response.send_message(msg)
    if success:
        bot.create_background_task(bot.update_stats_display())


@bot.tree.command(name="bidhistory", description="Show recent bid history")
@app_commands.describe(limit="Number of recent bids to show (default: 10)")
async def bid_history(interaction: discord.Interaction, limit: int = 10):
    history = bot.auction_manager.get_bid_history_display(limit=min(limit, 50))
    await interaction.response.send_message(history)


@bot.tree.command(name="teambids", description="Show bid history for a specific team")
@app_commands.describe(
    team="Team code (MI, CSK, etc.)",
    limit="Number of recent bids to show (default: 20)",
)
async def team_bid_history(
    interaction: discord.Interaction, team: str, limit: int = 20
):
    """Show bid history for a specific team with player names"""
    team_upper = team.upper()
    if team_upper not in TEAMS:
        await interaction.response.send_message(
            f"Invalid team: {team}. Valid teams: {', '.join(TEAMS.keys())}",
            ephemeral=True,
        )
        return

    history = bot.auction_manager.get_team_bid_history_display(
        team_upper, min(limit, 50)
    )
    await interaction.response.send_message(history)


@bot.tree.command(name="teamsquad", description="Show players in your team")
async def team_squad(interaction: discord.Interaction):
    """Shows the squad for the user's assigned team"""
    from config import TEAM_SLOTS

    user_id = interaction.user.id
    team_upper = bot.user_teams.get(user_id)

    if not team_upper:
        await interaction.response.send_message(
            "You are not assigned to any team. Ask an admin to assign you.",
            ephemeral=True,
        )
        return

    detailed_squads = bot.auction_manager.db.get_all_squads_detailed()
    teams_purse = bot.auction_manager.teams

    squad = detailed_squads.get(team_upper, [])
    purse = teams_purse.get(team_upper, 0)

    slots = TEAM_SLOTS.get(team_upper, {"available": 0, "overseas": 0})
    available_slots = slots["available"]
    overseas_slots = slots["overseas"]

    msg = bot.formatter.format_squad_display(
        team_upper, squad, purse, available_slots, overseas_slots
    )
    await interaction.response.send_message(msg)


@bot.tree.command(name="squad", description="View any team's squad and purse")
@app_commands.describe(team="Team Code (e.g. MI, CSK)")
async def view_squad(interaction: discord.Interaction, team: str):
    """View any team's squad - available to all users"""
    from config import TEAMS, TEAM_SLOTS

    team_upper = team.upper()

    if team_upper not in TEAMS:
        await interaction.response.send_message(f"Invalid team: {team}", ephemeral=True)
        return

    detailed_squads = bot.auction_manager.db.get_all_squads_detailed()
    teams_purse = bot.auction_manager.teams

    squad = detailed_squads.get(team_upper, [])
    purse = teams_purse.get(team_upper, 0)

    slots = TEAM_SLOTS.get(team_upper, {"available": 0, "overseas": 0})
    available_slots = slots["available"]
    overseas_slots = slots["overseas"]

    msg = bot.formatter.format_squad_display(
        team_upper, squad, purse, available_slots, overseas_slots
    )
    await interaction.response.send_message(msg)


@bot.tree.command(name="rollback", description="Undo the last sale (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def rollback_sale(interaction: discord.Interaction):
    result = bot.auction_manager.rollback_last_sale()

    if result:
        if result["team_code"] == "UNSOLD":
            # Player was unsold - just removed from sales record
            msg = f"**Rollback successful!**\n"
            msg += f"Player: **{result['player_name']}**\n"
            msg += f"Status: Was marked UNSOLD - record removed.\n"
            msg += "Use `/reauction {player_name}` to re-auction this player."
        else:
            # Player was sold - refund purse and remove from squad
            msg = f"**Rollback successful!**\n"
            msg += f"Player: **{result['player_name']}**\n"
            msg += f"Team: **{result['team_code']}**\n"
            msg += f"Amount refunded: {format_amount(result['final_price'])}\n"
            msg += "Use `/reauction {player_name}` to re-auction this player."

        bot.create_background_task(bot.update_stats_display())
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
        bot.create_background_task(bot.update_stats_display())
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(
    name="releasemultiple",
    description="Release multiple players from teams (Admin only)",
)
@app_commands.describe(
    releases="Format: Team1:Player1, Team2:Player2 (e.g., MI:Rohit, CSK:Dhoni)"
)
@app_commands.checks.has_permissions(administrator=True)
async def release_multiple(interaction: discord.Interaction, releases: str):
    """Release multiple players from teams at once"""
    await interaction.response.defer(ephemeral=True)

    entries = [e.strip() for e in releases.split(",") if e.strip()]

    released = []
    failed = []

    for entry in entries:
        if ":" not in entry:
            failed.append(f"{entry} (invalid format, use TEAM:Player)")
            continue

        parts = entry.split(":", 1)
        team = parts[0].strip().upper()
        player = parts[1].strip()

        success, message = bot.auction_manager.release_retained_player(team, player)
        if success:
            released.append(f"{team}: {player}")
        else:
            failed.append(f"{team}:{player} ({message})")

    msg = f"**Released {len(released)} players:**\n"
    if released:
        msg += "\n".join(f"✅ {r}" for r in released)
    if failed:
        msg += f"\n\n**Failed ({len(failed)}):**\n" + "\n".join(
            f"❌ {f}" for f in failed[:10]
        )

    if released:
        bot.create_background_task(bot.update_stats_display())

    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(
    name="addtosquad", description="Manually add a player to a squad (Admin only)"
)
@app_commands.describe(
    team="Team Code",
    player="Player Name",
    price="Price in Crores (e.g., 2 = 2Cr, 0.5 = 50L)",
)
@app_commands.checks.has_permissions(administrator=True)
async def add_to_squad(
    interaction: discord.Interaction, team: str, player: str, price: float
):
    success, msg = bot.auction_manager.manual_add_player(team, player, price)
    if success:
        bot.create_background_task(bot.update_stats_display())
    await interaction.response.send_message(msg)


# ============================================================
# LIST MANAGEMENT COMMANDS
# ============================================================


@bot.tree.command(name="addplayer", description="Add a player to a list (Admin only)")
@app_commands.describe(
    list_name="Name of the list",
    player_name="Player name to add",
    base_price="Base price in Crores (e.g., 2 = 2Cr, 0.5 = 50L). Default: 0.2Cr (20L)",
)
@app_commands.checks.has_permissions(administrator=True)
async def add_player(
    interaction: discord.Interaction,
    list_name: str,
    player_name: str,
    base_price: float = 0.2,
):
    price_rupees = int(base_price * 10_000_000)
    bot.auction_manager.create_list(list_name)

    if bot.auction_manager.add_player_to_list(list_name, (player_name, price_rupees)):
        await interaction.response.send_message(
            f"Added **{player_name}** to list **{list_name}** with base price {format_amount(price_rupees)}"
        )
    else:
        await interaction.response.send_message(
            f"Failed to add **{player_name}** - player may already exist in lists or squads.",
            ephemeral=True,
        )


@bot.tree.command(
    name="addplayers", description="Add multiple players to a list (Admin only)"
)
@app_commands.describe(
    list_name="Name of the list (will be created if doesn't exist)",
    players="Players in format: Name1:Price1, Name2:Price2 (Price in Cr, e.g., Virat:2, Rohit:1.5)",
)
@app_commands.checks.has_permissions(administrator=True)
async def add_players_bulk(
    interaction: discord.Interaction, list_name: str, players: str
):
    """Add multiple players at once. Format: Name1:Price1, Name2:Price2"""
    bot.auction_manager.create_list(list_name)

    added = []
    failed = []

    player_entries = [p.strip() for p in players.split(",")]

    for entry in player_entries:
        entry = entry.strip()
        if not entry:
            continue

        if ":" in entry:
            parts = entry.split(":", 1)
            player_name = parts[0].strip()
            try:
                price_cr = float(parts[1].strip())
            except ValueError:
                price_cr = 0.2  # Default 20L
        else:
            player_name = entry
            price_cr = 0.2  # Default 20L

        price_rupees = int(price_cr * 10_000_000)

        if bot.auction_manager.add_player_to_list(
            list_name, (player_name, price_rupees)
        ):
            added.append(f"{player_name} ({format_amount(price_rupees)})")
        else:
            failed.append(player_name)

    msg = f"**Added {len(added)} players to list '{list_name}':**\n"
    if added:
        msg += "\n".join(f"✅ {p}" for p in added[:20])
        if len(added) > 20:
            msg += f"\n... and {len(added) - 20} more"
    if failed:
        msg += f"\n\n**Failed ({len(failed)}):** {', '.join(failed)}"

    await interaction.response.send_message(msg)


@bot.tree.command(
    name="removeplayers", description="Remove multiple players from a list (Admin only)"
)
@app_commands.describe(
    list_name="Name of the list to remove players from",
    players="Player names separated by commas (e.g., Player1, Player2, Player3)",
)
@app_commands.checks.has_permissions(administrator=True)
async def remove_players_bulk(
    interaction: discord.Interaction, list_name: str, players: str
):
    """Remove multiple players from a list at once"""
    if bot.auction_manager.active:
        await interaction.response.send_message(
            "Cannot remove players while auction is active.", ephemeral=True
        )
        return

    player_names = [p.strip() for p in players.split(",") if p.strip()]
    if not player_names:
        await interaction.response.send_message(
            "Please provide player names separated by commas.", ephemeral=True
        )
        return

    removed, not_found = bot.auction_manager.remove_players_from_list(
        list_name, player_names
    )

    msg = f"**Removed {len(removed)} players from '{list_name}':**\n"
    if removed:
        msg += "\n".join(f"✅ {p}" for p in removed[:20])
        if len(removed) > 20:
            msg += f"\n... and {len(removed) - 20} more"
    if not_found:
        msg += f"\n\n**Not found ({len(not_found)}):** {', '.join(not_found[:10])}"
        if len(not_found) > 10:
            msg += f" ... and {len(not_found) - 10} more"

    await interaction.response.send_message(msg)


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
    name="loadsets",
    description="Load the NEXT N sets from IPL Excel (Admin only)",
)
@app_commands.describe(num_sets="Number of NEW sets to load (1-67)")
@app_commands.checks.has_permissions(administrator=True)
async def load_sets(interaction: discord.Interaction, num_sets: int):
    await interaction.response.defer()

    if num_sets < 1 or num_sets > 67:
        await interaction.followup.send(
            "Number of sets must be between 1 and 67", ephemeral=True
        )
        return

    success, message = bot.auction_manager.load_players_from_sets(num_sets)
    await interaction.followup.send(message)


@bot.tree.command(
    name="loadretained",
    description="Load retained players into DB and Excel (Admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def load_retained(interaction: discord.Interaction):
    """Loads retained data and initializes Excel."""
    await interaction.response.defer()
    success, msg = bot.auction_manager.load_retained_data()
    await interaction.followup.send(msg)


# ============================================================
# PAGINATED SHOWLISTS VIEW
# ============================================================


class ShowListsView(discord.ui.View):
    """Paginated view for /showlists with next/previous buttons"""

    def __init__(self, pages: list, user_id: int, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current_page = 0
        self.user_id = user_id
        self.message: Optional[discord.Message] = None
        self.update_buttons()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass
        self.pages = None
        self.message = None

    def update_buttons(self):
        try:
            self.prev_button.disabled = self.current_page == 0
            self.next_button.disabled = self.current_page >= len(self.pages) - 1
        except Exception:
            pass

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your menu!", ephemeral=True
            )
            return

        self.current_page = max(0, self.current_page - 1)
        self.update_buttons()
        await interaction.response.edit_message(
            content=self.pages[self.current_page], view=self
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your menu!", ephemeral=True
            )
            return

        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self.update_buttons()
        await interaction.response.edit_message(
            content=self.pages[self.current_page], view=self
        )


def paginate_lists_by_set(
    player_lists: dict, list_order: list, max_chars: int = 1800
) -> list:
    """
    Paginate the player lists by set while showing ALL players per set (no truncation).

    Keeps the same formatted layout:
    **SETNAME** (N players):
    ```
      Player Name                   | Price
      ...
    ```
    Splits into pages when the content would exceed max_chars.
    """
    pages = []
    current_page = "**📋 Player Lists:**\n"

    processed = set()
    for list_name in list_order:
        if list_name in player_lists:
            players = player_lists[list_name]
            set_content = f"\n**{list_name.upper()}** ({len(players)} players):\n```\n"
            # Show entire list (no 15-player truncation)
            for player_name, base_price in players:
                price_str = format_amount(base_price) if base_price else "20L"
                set_content += f"  {player_name:30} | {price_str}\n"
            set_content += "```"

            # If this set's content would exceed current page limit, flush and start new page
            if (
                len(current_page) + len(set_content) > max_chars
                and current_page != "**📋 Player Lists:**\n"
            ):
                pages.append(current_page)
                current_page = "**📋 Player Lists (cont.):**\n"

            current_page += set_content
            processed.add(list_name)

    # Include any lists that are not in the explicit list_order (leftover lists)
    for list_name, players in player_lists.items():
        if list_name not in processed:
            set_content = f"\n**{list_name.upper()}** ({len(players)} players):\n```\n"
            for player_name, base_price in players:
                price_str = format_amount(base_price) if base_price else "20L"
                set_content += f"  {player_name:30} | {price_str}\n"
            set_content += "```"

            if (
                len(current_page) + len(set_content) > max_chars
                and current_page != "**📋 Player Lists:**\n"
            ):
                pages.append(current_page)
                current_page = "**📋 Player Lists (cont.):**\n"

            current_page += set_content

    if current_page.strip() and current_page != "**📋 Player Lists:**\n":
        pages.append(current_page)

    if not pages:
        pages.append("No player lists created yet.")

    for i, page in enumerate(pages):
        pages[i] = page + f"\n\n*Page {i+1}/{len(pages)}*"

    return pages


@bot.tree.command(name="showlists", description="Display all lists and their contents")
async def show_lists(interaction: discord.Interaction):
    player_lists = bot.auction_manager.player_lists
    list_order = bot.auction_manager.list_order

    if not player_lists:
        await interaction.response.send_message("No player lists created yet.")
        return

    pages = paginate_lists_by_set(player_lists, list_order)

    if len(pages) == 1:
        await interaction.response.send_message(pages[0])
    else:
        view = ShowListsView(pages, interaction.user.id)
        await interaction.response.send_message(pages[0], view=view)
        view.message = await interaction.original_response()


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
    await start_next_player(interaction.channel)


@bot.tree.command(name="stop", description="Stop the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def stop_auction(interaction: discord.Interaction):
    if bot.auction_manager.stop_auction():
        await bot.cancel_countdown_task()
        await interaction.response.send_message("**AUCTION STOPPED**")
    else:
        await interaction.response.send_message(
            "No auction is currently running.", ephemeral=True
        )


@bot.tree.command(name="pause", description="Pause the auction (Bulk Pause)")
@app_commands.checks.has_permissions(administrator=True)
async def pause_auction(interaction: discord.Interaction):
    if bot.auction_manager.pause_auction():
        await interaction.response.send_message("**Auction Paused**")
    else:
        await interaction.response.send_message("Cannot pause auction.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the auction (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def resume_auction(interaction: discord.Interaction):
    if not bot.auction_manager.active:
        await interaction.response.send_message(
            "Auction is not active to resume.", ephemeral=True
        )
        return

    if not bot.auction_manager.paused:
        await interaction.response.send_message(
            "Auction is not paused.", ephemeral=True
        )
        return

    bot.auction_manager._load_state_from_db()
    current_player = bot.auction_manager.current_player

    if current_player:
        squads = bot.auction_manager.db.get_all_squads()
        player_already_sold = False
        for squad in squads.values():
            for item in squad:
                pname = item[0]
                if pname.lower() == current_player.lower():
                    player_already_sold = True
                    break
            if player_already_sold:
                break

        if player_already_sold:
            bot.auction_manager._reset_player_state()
            bot.auction_manager.paused = False
            bot.auction_manager._save_state_to_db()
            await interaction.response.send_message(
                f"**{current_player}** was already sold. Resuming auction with next player..."
            )
            await start_next_player(interaction.channel)
            return

    player_lists = bot.auction_manager.player_lists
    has_players = any(len(players) > 0 for players in player_lists.values())

    if not has_players and not current_player:
        unsold = bot.auction_manager.db.get_unsold_players()
        msg = "**⚠️ ALL LOADED SETS EXHAUSTED!**\n\n"
        msg += "**Admin Options:**\n"
        msg += "• `/loadsets X` - Load more sets (1-67)\n"
        msg += "• `/showunsold` - View unsold players\n"
        msg += "• `/reauctionall` - Bring all unsold players back\n"
        msg += "• `/stop` - End the auction completely"
        if unsold:
            msg += f"\n📋 **{len(unsold)} unsold players** available for re-auction."
        await interaction.response.send_message(msg, ephemeral=True)
        return

    bot.auction_manager.paused = False
    bot.auction_manager.last_bid_time = time.time()
    bot.auction_manager._save_state_to_db()

    current_set = bot.auction_manager.get_current_list_name()
    set_info = f" (Set: **{current_set.upper()}**)" if current_set else ""

    if bot.auction_manager.current_player:
        await interaction.response.send_message(
            f"Auction resumed!{set_info} Bidding continues for **{bot.auction_manager.current_player}**"
        )
        if not bot.countdown_task or bot.countdown_task.done():
            bot.countdown_task = asyncio.create_task(
                countdown_loop(interaction.channel)
            )
    else:
        await interaction.response.send_message(
            f"Auction resumed!{set_info} Finding next player..."
        )
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
    await bot.cancel_countdown_task()

    success, team, amount = await bot.auction_manager.finalize_sale()

    if success:
        await interaction.response.send_message(f"Player **{player}** SKIPPED (unsold)")
    else:
        bot.auction_manager._reset_player_state()
        bot.auction_manager._save_state_to_db()
        await interaction.response.send_message(f"Player **{player}** SKIPPED (unsold)")

    await asyncio.sleep(1)
    await start_next_player(interaction.channel)


@bot.tree.command(
    name="skipset", description="Skip current set and move to next set (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def skip_set(interaction: discord.Interaction):
    if not bot.auction_manager.active:
        await interaction.response.send_message("No active auction.", ephemeral=True)
        return

    current_set = bot.auction_manager.get_current_list_name()
    if not current_set:
        await interaction.response.send_message(
            "No current set to skip.", ephemeral=True
        )
        return

    skipped_count, skipped_names = bot.auction_manager.skip_current_set()
    await bot.cancel_countdown_task()

    await interaction.response.send_message(
        f"⏭️ **Skipped set {current_set.upper()}!**\n"
        f"{skipped_count} players moved to **Skipped** list.\n"
        f"Use `/showskipped` to view them. Moving to next set..."
    )

    await asyncio.sleep(2)
    await start_next_player(interaction.channel)


@bot.tree.command(
    name="showskipped", description="Show all skipped players (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def show_skipped(interaction: discord.Interaction):
    """Show all players that were skipped"""
    skipped = bot.auction_manager.get_skipped_players()

    if not skipped:
        await interaction.response.send_message(
            "No skipped players found.", ephemeral=True
        )
        return

    msg = f"**⏭️ Skipped Players ({len(skipped)} total):**\n```\n"

    for pname, base_price in skipped:
        price_str = format_amount(base_price) if base_price else "N/A"
        msg += f"  {pname:30} | Base: {price_str}\n"

    msg += "```\n"
    msg += "Skipped players will be auctioned at the end of all regular sets.\n"
    msg += "Use `/reauction player_name` to bring back a specific player."

    if len(msg) > 2000:
        await interaction.response.send_message(msg[:2000])
        for chunk in [msg[i : i + 2000] for i in range(2000, len(msg), 2000)]:
            await interaction.followup.send(chunk)
    else:
        await interaction.response.send_message(msg)


@bot.tree.command(
    name="announce", description="Send a custom announcement message (Admin only)"
)
@app_commands.describe(
    title="The title/heading of the announcement (default: ANNOUNCEMENT)",
    message="The announcement message to display",
    mention_everyone="Whether to @everyone (default: False)",
)
@app_commands.checks.has_permissions(administrator=True)
async def announce(
    interaction: discord.Interaction,
    message: str,
    title: str = "ANNOUNCEMENT",
    mention_everyone: bool = False,
):
    embed = discord.Embed(
        title=f"📢 {title.upper()}",
        description=message,
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"Announced by {interaction.user.display_name}")

    content = "@everyone" if mention_everyone else None
    await interaction.response.send_message(content=content, embed=embed)


# ============================================================
# ADMIN SETTINGS COMMANDS
# ============================================================


@bot.tree.command(
    name="setstatschannel",
    description="Set channel for live stats updates (Admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def set_stats_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
):
    bot.auction_manager.set_stats_channel(channel.id)
    await interaction.response.send_message(
        f"Stats channel set to {channel.mention}. I will start updating stats there."
    )
    await bot.update_stats_display()


@bot.tree.command(
    name="setcountdowngap",
    description="Set gap between last bid and start of countdown (Admin only)",
)
@app_commands.describe(seconds="Gap duration in seconds")
@app_commands.checks.has_permissions(administrator=True)
async def set_countdown_gap(interaction: discord.Interaction, seconds: int):
    """Sets the delay between the last bid and when the countdown/timer logic starts."""
    if seconds < 0:
        await interaction.response.send_message(
            "Gap cannot be negative.", ephemeral=True
        )
        return
    bot.auction_manager.set_countdown_gap(seconds)
    await interaction.response.send_message(
        f"✅ Countdown gap set to **{seconds} seconds**. Timer will pause for {seconds}s after each bid."
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
    name="resetpurses",
    description="Reset all team purses to configured values (Admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def reset_purses(interaction: discord.Interaction):
    from config import TEAMS

    reset_count = 0
    for team_code, purse in TEAMS.items():
        if bot.auction_manager.set_team_purse(team_code, purse):
            reset_count += 1

    msg = f"**Reset {reset_count} team purses to configured values:**\n```\n"
    for team, purse in sorted(TEAMS.items()):
        msg += f"{team:6} : {format_amount(purse)}\n"
    msg += "```"
    await interaction.response.send_message(msg)


# ============================================================
# CLEAR CONFIRMATION VIEW
# ============================================================


class ClearConfirmView(discord.ui.View):
    """Confirmation view for /clear command with backup option"""

    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.create_backup = False
        self.confirmed = False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(
        label="Clear with Backup", style=discord.ButtonStyle.primary, emoji="💾"
    )
    async def backup_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return
        self.create_backup = True
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(
        label="Clear without Backup", style=discord.ButtonStyle.danger, emoji="🗑️"
    )
    async def no_backup_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return
        self.create_backup = False
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


@bot.tree.command(
    name="clear", description="Clear auction data/buys/released but keep retained data"
)
@app_commands.checks.has_permissions(administrator=True)
async def clear_auction(interaction: discord.Interaction):
    """Clears trade, released, and auction data but keeps retained players."""
    # Show confirmation with backup option
    view = ClearConfirmView(interaction.user.id)
    await interaction.response.send_message(
        "**⚠️ Clear Auction Data**\n\n"
        "This will:\n"
        "• Remove all auction buys, trades, and released players\n"
        "• Reset team purses to config values\n"
        "• Keep retained players\n"
        "• Clear all player lists (use `/loadsets` to reload)\n\n"
        "**Do you want to create a backup before clearing?**",
        view=view,
    )

    await view.wait()

    # Disable buttons after interaction
    for item in view.children:
        item.disabled = True

    try:
        await interaction.edit_original_response(view=view)
    except:
        pass

    if not view.confirmed:
        await interaction.followup.send("❌ Clear operation cancelled.", ephemeral=True)
        return

    # Remove duplicates first
    duplicates_removed = bot.auction_manager.db.remove_duplicate_players()

    # Call clear_all_data with backup option
    backup_path = bot.auction_manager.clear_all_data(create_backup=view.create_backup)

    await bot.cancel_countdown_task()

    msg = "**✅ Auction data cleared!**\n"
    msg += "• Auction buys, trades, and released players removed.\n"
    msg += "• **Retained players preserved.**\n"
    msg += "• Team purses reset to config values.\n"
    msg += "• Player lists cleared - use `/loadsets` to reload sets."

    if backup_path:
        msg += f"\n\n💾 Backup created: `{backup_path}`"
    elif view.create_backup:
        msg += "\n\n⚠️ Backup creation failed."
    else:
        msg += "\n\n📝 No backup created (as requested)."

    if duplicates_removed > 0:
        msg += f"\n🔧 Removed {duplicates_removed} duplicate player entries."

    await interaction.followup.send(msg)


@bot.tree.command(
    name="setplayergap", description="Set gap between players in seconds (Admin only)"
)
@app_commands.describe(seconds="Gap in seconds between players (1-60)")
@app_commands.checks.has_permissions(administrator=True)
async def set_player_gap(interaction: discord.Interaction, seconds: int):
    if seconds < 1 or seconds > 60:
        await interaction.response.send_message(
            "Gap must be between 1 and 60 seconds.", ephemeral=True
        )
        return
    bot.player_gap = seconds
    bot.auction_manager.set_player_gap(seconds)
    await interaction.response.send_message(
        f"✅ Player gap set to **{seconds} seconds**"
    )


@bot.tree.command(
    name="moveplayer", description="Move a player from one set to another (Admin only)"
)
@app_commands.describe(
    player="Player name to move", target_set="Target set name (e.g., BA1, M1)"
)
@app_commands.checks.has_permissions(administrator=True)
async def move_player(interaction: discord.Interaction, player: str, target_set: str):
    success = bot.auction_manager.db.move_player_to_set(player, target_set)
    if success:
        await interaction.response.send_message(
            f"✅ Moved **{player}** to set **{target_set.upper()}**"
        )
    else:
        await interaction.response.send_message(
            f"❌ Could not find player **{player}** in any set.", ephemeral=True
        )


@bot.tree.command(
    name="moveplayers",
    description="Move multiple players to a target set (Admin only)",
)
@app_commands.describe(
    players="Comma-separated player names (e.g., Player1, Player2, Player3)",
    target_set="Target set name (e.g., BA1, M1, accelerated)",
)
@app_commands.checks.has_permissions(administrator=True)
async def move_players(interaction: discord.Interaction, players: str, target_set: str):
    """Move multiple players to a target set at once"""
    await interaction.response.defer(ephemeral=True)

    player_names = [p.strip() for p in players.split(",") if p.strip()]

    if not player_names:
        await interaction.followup.send("❌ No player names provided.", ephemeral=True)
        return

    moved = []
    failed = []

    for player_name in player_names:
        success = bot.auction_manager.db.move_player_to_set(player_name, target_set)
        if success:
            moved.append(player_name)
        else:
            failed.append(player_name)

    msg = f"**Moving players to {target_set.upper()}:**\n"
    if moved:
        msg += f"✅ **Moved ({len(moved)}):** {', '.join(moved)}\n"
    if failed:
        msg += f"❌ **Not found ({len(failed)}):** {', '.join(failed)}"

    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(
    name="unsoldtime", description="Set the timeout for unsold players (Admin only)"
)
@app_commands.describe(
    seconds="Seconds before a player with no bids goes unsold (default: 60)",
)
@app_commands.checks.has_permissions(administrator=True)
async def set_unsold_time(interaction: discord.Interaction, seconds: int):
    """Adjust the NO_START_TIMEOUT - time before a player goes unsold if no bids"""
    import config

    if seconds < 10 or seconds > 300:
        await interaction.response.send_message(
            "❌ Unsold timeout must be between 10 and 300 seconds.", ephemeral=True
        )
        return

    config.NO_START_TIMEOUT = seconds
    await interaction.response.send_message(
        f"✅ Unsold timeout set to **{seconds} seconds**.\n"
        f"Players with no bids will now go unsold after {seconds}s.",
        ephemeral=True,
    )


@bot.tree.command(
    name="fixduplicates",
    description="Remove duplicate players from squads (Admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def fix_duplicates(interaction: discord.Interaction):
    removed = bot.auction_manager.db.remove_duplicate_players()
    if removed > 0:
        await interaction.response.send_message(
            f"✅ Removed **{removed}** duplicate player entries from squads."
        )
    else:
        await interaction.response.send_message(
            "✅ No duplicate players found.", ephemeral=True
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


@bot.tree.command(name="allsquads", description="View summary of all teams' squads")
async def all_squads(interaction: discord.Interaction):
    """View a summary of all teams - available to everyone"""
    squads = bot.auction_manager.team_squads
    teams_purse = bot.auction_manager.teams

    msg = "**📋 All Teams Summary:**\n```\n"
    msg += f"{'Team':<6} {'Players':>8} {'Overseas':>9} {'Spent':>12} {'Purse':>12}\n"
    msg += "=" * 51 + "\n"

    for team_code in sorted(teams_purse.keys()):
        squad = squads.get(team_code, [])
        player_count = len(squad)
        # Handle both old (player, price) and new (player, price, is_overseas) formats
        total_spent = 0
        overseas_count = 0
        for item in squad:
            if len(item) >= 3:
                total_spent += item[1]
                if item[2]:
                    overseas_count += 1
            else:
                total_spent += item[1]
        purse = teams_purse.get(team_code, 0)

        msg += f"{team_code:<6} {player_count:>8} {overseas_count:>9} {format_amount(total_spent):>12} {format_amount(purse):>12}\n"

    msg += "```\n*Use `/squad <team>` to view detailed squad*"
    await interaction.response.send_message(msg)


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
`/teambids <team>` - View bid history for a specific team.

**View Teams:**
`/squad <team>` - View any team's detailed squad.
`/allsquads` - View summary of all teams.
`/showpurse` - See remaining purse for all teams.

**Info:**
`/status` - Check the current player and auction status.
`/showlists` - View the upcoming player lists.
"""
    await interaction.response.send_message(help_text, ephemeral=True)


@bot.tree.command(name="adminhelp", description="Show commands for Admins")
@app_commands.checks.has_permissions(administrator=True)
async def admin_help_command(interaction: discord.Interaction):
    embed1 = discord.Embed(
        title="🔧 Admin Commands - Auction Control", color=discord.Color.blue()
    )
    embed1.add_field(
        name="Auction Control",
        value=(
            "`/start` - Start auction\n"
            "`/stop` - Stop auction\n"
            "`/pause` - Pause auction\n"
            "`/resume` - Resume auction\n"
            "`/soldto TEAM` - Sell to team\n"
            "`/unsold` - Mark unsold\n"
            "`/skip` - Skip player\n"
            "`/skipset` - Skip entire set\n"
            "`/undobid` - Undo last bid\n"
            "`/rollback` - Undo last sale\n"
            "`/clear` - Reset (Keep Retained)"
        ),
        inline=False,
    )

    embed2 = discord.Embed(
        title="👥 Admin Commands - Team Management", color=discord.Color.green()
    )
    embed2.add_field(
        name="Team & Player Management",
        value=(
            "`/assignteam @user TEAM` - Assign user\n"
            "`/assignteams` - Bulk assign users\n"
            "`/unassignteam @user` - Remove user\n"
            "`/setpurse TEAM amount` - Set purse\n"
            "`/addtosquad TEAM player price` - Add player\n"
            "`/release TEAM player` - Release player\n"
            "`/releasemultiple` - Bulk release\n"
            "`/trade player from to price` - Trade player"
        ),
        inline=False,
    )

    embed3 = discord.Embed(
        title="📋 Admin Commands - Lists & Settings", color=discord.Color.orange()
    )
    embed3.add_field(
        name="Re-Auction & Data",
        value=(
            "`/loadretained` - Init/Reset Retained\n"
            "`/showunsold` - View unsold/accelerated\n"
            "`/showskipped` - View skipped players\n"
            "`/reauction player` - Re-auction one\n"
            "`/reauctionall` - Re-auction all\n"
            "`/loadsets N` - Load next N sets\n"
            "`/addplayer` - Add player"
        ),
        inline=True,
    )
    embed3.add_field(
        name="Settings & Communication",
        value=(
            "`/setcountdowngap secs` - Bid-to-timer gap\n"
            "`/setplayergap secs` - Player gap\n"
            "`/setstatschannel #ch` - Stats channel\n"
            "`/announce title msg` - Announcement"
        ),
        inline=False,
    )

    await interaction.response.send_message(
        embeds=[embed1, embed2, embed3], ephemeral=True
    )


# ============================================================
# HELPER FUNCTIONS (start_next_player, countdown_loop)
# ============================================================


async def start_next_player(channel: discord.TextChannel):
    """Start auctioning the next player"""

    if bot.auction_manager.paused:
        return

    await asyncio.sleep(bot.player_gap)

    if bot.auction_manager.paused:
        return

    result = bot.auction_manager.get_next_player()
    success, player_name, base_price, is_first_in_list = result

    if not success:
        # All loaded sets are complete - pause auction and notify admin
        bot.auction_manager.paused = True
        bot.auction_manager._save_state_to_db()

        await channel.send(
            "**⚠️ All loaded sets have been completed!**\n\n"
            "📋 **Admin Actions:**\n"
            "• Use `/loadsets N` to load more sets from the Excel file\n"
            "• Use `/resume` to continue after loading new sets\n"
            "• Use `/reauction playername` to re-auction unsold players\n"
            "• Use `/stop` to end the auction completely"
        )
        await channel.send(bot.auction_manager.get_purse_display())
        return

    current_set_name = bot.auction_manager.get_current_list_name()

    if is_first_in_list:
        set_embed = discord.Embed(
            title=f"🎯 SET: {current_set_name.upper()}",
            description=f"Starting players from **{current_set_name.upper()}**",
            color=discord.Color.gold(),
        )
        await channel.send(embed=set_embed)
        await asyncio.sleep(2)

        if bot.auction_manager.current_list_index == 0:
            delay = 5
            await channel.send(
                f"🚨 Auction starting! First bid window opens in **{delay} seconds**."
            )
        else:
            delay = INITIAL_SET_DELAY
            await channel.send(f"🚨 Bidding opens in **{delay} seconds**.")

        await asyncio.sleep(delay)

        msg = bot.formatter.format_player_announcement(player_name, base_price)
        await channel.send(msg)
        bot.auction_manager.reset_last_bid_time()
    else:
        announcement = bot.formatter.format_player_announcement(player_name, base_price)
        await channel.send(announcement)
        bot.auction_manager.reset_last_bid_time()

    if not bot.countdown_task or bot.countdown_task.done():
        bot.countdown_task = asyncio.create_task(countdown_loop(channel))


async def countdown_loop(channel: discord.TextChannel):
    """Manual bidding timer with gap support"""
    import time as time_module
    from config import NO_BID_TIMEOUT, NO_START_TIMEOUT, BIDDING_OPEN_WARNING_TIME

    player_start_time = time_module.time()

    if bot.auction_manager.last_bid_time <= 0:
        bot.auction_manager.last_bid_time = player_start_time

    last_msg = None
    first_bid_placed = False
    bidding_open_msg_sent = False
    going_once_sent = False
    going_twice_sent = False
    going_thrice_sent = False
    last_known_bid_time = bot.auction_manager.last_bid_time

    while bot.auction_manager.active and not bot.auction_manager.paused:
        await asyncio.sleep(1)

        now = time_module.time()
        bot.auction_manager._load_state_from_db()
        current_player_name = bot.auction_manager.current_player

        # Dynamic Gap
        countdown_gap = getattr(bot.auction_manager, "countdown_gap", 0)

        current_bid_time = bot.auction_manager.last_bid_time
        if bot.auction_manager.highest_bidder is not None:
            if not first_bid_placed:
                first_bid_placed = True
                last_known_bid_time = current_bid_time
                going_once_sent = False
                going_twice_sent = False
                going_thrice_sent = False
            elif current_bid_time > last_known_bid_time:
                last_known_bid_time = current_bid_time
                going_once_sent = False
                going_twice_sent = False
                going_thrice_sent = False

        if not first_bid_placed:
            elapsed_since_start = now - player_start_time
            remaining = NO_START_TIMEOUT - int(elapsed_since_start)

            if (
                elapsed_since_start >= BIDDING_OPEN_WARNING_TIME
                and not bidding_open_msg_sent
            ):
                bidding_open_msg_sent = True
                await channel.send(
                    f"📣 **BIDDING OPEN!** Waiting for first bid on **{current_player_name}**..."
                )

            if remaining <= 30 and remaining > 20 and not going_once_sent:
                going_once_sent = True
                await channel.send(
                    f"⏳ **{current_player_name}** going **UNSOLD** in **30 seconds**... Place your bids!"
                )
            if remaining <= 20 and remaining > 10 and not going_twice_sent:
                going_twice_sent = True
                await channel.send(
                    f"⚠️ **{current_player_name}** going **UNSOLD** in **20 seconds**!"
                )
            if remaining <= 10 and remaining > 0 and not going_thrice_sent:
                going_thrice_sent = True
                await channel.send(
                    f"🚨 **LAST CHANCE!** **{current_player_name}** going **UNSOLD** in **10 seconds**!"
                )

            if remaining <= 0:
                if last_msg:
                    try:
                        await last_msg.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

                if not current_player_name:
                    await asyncio.sleep(2)
                    await start_next_player(channel)
                    return

                success, team, amount = await bot.auction_manager.finalize_sale()

                if success and team == "UNSOLD":
                    sold_msg = bot.formatter.format_sold_message(
                        current_player_name, team, amount
                    )
                    await channel.send(sold_msg)
                else:
                    await channel.send(
                        f"⏰ No bids received - Player **{current_player_name}** goes **UNSOLD**"
                    )

                await asyncio.sleep(2)
                await start_next_player(channel)
                return

        else:
            # Bid placed - apply gap
            elapsed_since_last_bid = now - bot.auction_manager.last_bid_time

            # Subtract GAP from elapsed time.
            # E.g. Gap=5, Elapsed=3 -> Effective=-2 (Waiting)
            # Gap=5, Elapsed=6 -> Effective=1 (Countdown active)
            effective_elapsed = elapsed_since_last_bid - countdown_gap

            if effective_elapsed < 0:
                # Still in gap period, silent wait
                continue

            remaining = NO_BID_TIMEOUT - int(effective_elapsed)

            current_bid = bot.auction_manager.current_bid
            current_team = bot.auction_manager.highest_bidder

            if remaining <= 12 and remaining > 8 and not going_once_sent:
                going_once_sent = True
                await channel.send(
                    f"🔔 **GOING ONCE!** {format_amount(current_bid)} to **{current_team}**!"
                )

            if remaining <= 8 and remaining > 4 and not going_twice_sent:
                going_twice_sent = True
                await channel.send(
                    f"🔔🔔 **GOING TWICE!** {format_amount(current_bid)} to **{current_team}**!"
                )

            if remaining <= 4 and remaining > 0 and not going_thrice_sent:
                going_thrice_sent = True
                await channel.send(
                    f"🔔🔔🔔 **GOING THRICE!** Last chance! {format_amount(current_bid)} to **{current_team}**!"
                )

            if remaining <= 0:
                if last_msg:
                    try:
                        await last_msg.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

                player_name = bot.auction_manager.current_player

                if not player_name:
                    await asyncio.sleep(2)
                    await start_next_player(channel)
                    return

                squads = bot.auction_manager.db.get_all_squads()
                player_already_sold = False
                for squad in squads.values():
                    for item in squad:
                        pname = item[0]
                        if pname.lower() == player_name.lower():
                            player_already_sold = True
                            break
                    if player_already_sold:
                        break

                if player_already_sold:
                    bot.auction_manager._reset_player_state()
                    bot.auction_manager._save_state_to_db()
                    await asyncio.sleep(2)
                    await start_next_player(channel)
                    return

                success, team, amount = await bot.auction_manager.finalize_sale()

                if success and team and team != "UNSOLD":
                    sold_msg = bot.formatter.format_sold_message(
                        player_name, team, amount
                    )
                    await channel.send(sold_msg)
                    await channel.send(bot.auction_manager.get_purse_display())
                    bot.create_background_task(bot.update_stats_display())
                elif success and team == "UNSOLD":
                    sold_msg = bot.formatter.format_sold_message(
                        player_name, team, amount
                    )
                    await channel.send(sold_msg)
                else:
                    await channel.send(
                        f"⚠️ Error finalizing sale for **{player_name}**. Moving to next player."
                    )

                await asyncio.sleep(2)
                await start_next_player(channel)
                return

        if bot.auction_manager.paused:
            if last_msg:
                try:
                    await last_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
            break


# ============================================================
# TRADE / SWAP CONFIRMATION VIEWS & COMMANDS
# ============================================================


@bot.tree.command(
    name="trade",
    description="Trade a player from one team to another for cash (Admin only)",
)
@app_commands.describe(
    player="Player name to trade",
    from_team="Team giving the player (e.g., MI, CSK)",
    to_team="Team receiving the player",
    price="Trade price in Crores (e.g., 5 = 5Cr, 0.5 = 50L)",
)
@app_commands.checks.has_permissions(administrator=True)
async def trade_command(
    interaction: discord.Interaction,
    player: str,
    from_team: str,
    to_team: str,
    price: float,
):
    """Trade a player between teams with confirmation"""
    from_team_upper = from_team.upper()
    to_team_upper = to_team.upper()

    # Validate teams
    if from_team_upper not in TEAMS:
        await interaction.response.send_message(
            f"Invalid source team: {from_team}. Valid teams: {', '.join(TEAMS.keys())}",
            ephemeral=True,
        )
        return
    if to_team_upper not in TEAMS:
        await interaction.response.send_message(
            f"Invalid target team: {to_team}. Valid teams: {', '.join(TEAMS.keys())}",
            ephemeral=True,
        )
        return

    if from_team_upper == to_team_upper:
        await interaction.response.send_message(
            "Cannot trade a player to the same team!", ephemeral=True
        )
        return

    # Check overseas limit before showing confirmation
    is_overseas = bot.auction_manager.db.get_player_overseas_status(player)
    if is_overseas:
        from config import MAX_OVERSEAS_LIMIT

        to_team_overseas = bot.auction_manager.db.get_overseas_count(to_team_upper)
        if to_team_overseas >= MAX_OVERSEAS_LIMIT:
            await interaction.response.send_message(
                f"❌ **{to_team_upper}** already has {MAX_OVERSEAS_LIMIT} overseas players. Cannot trade overseas player.",
                ephemeral=True,
            )
            return

    # Get current purses for display
    teams_purse = bot.auction_manager.teams
    from_purse = teams_purse.get(from_team_upper, 0)
    to_purse = teams_purse.get(to_team_upper, 0)
    trade_amount = int(price * 10_000_000)

    # Create confirmation view
    view = TradeConfirmView(
        bot,
        player,
        from_team_upper,
        to_team_upper,
        price,
        interaction.user.id,
        interaction.channel,
    )

    price_display = format_amount(trade_amount)
    overseas_tag = " ✈️(Overseas)" if is_overseas else ""

    confirm_msg = (
        f"**Confirm Trade:**\n"
        f"• Player: **{player}**{overseas_tag}\n"
        f"• From: **{from_team_upper}** → To: **{to_team_upper}**\n"
        f"• Price: **{price_display}**\n\n"
        f"**Current Purses:**\n"
        f"• {from_team_upper}: {format_amount(from_purse)} → {format_amount(from_purse + trade_amount)} (+{price_display})\n"
        f"• {to_team_upper}: {format_amount(to_purse)} → {format_amount(to_purse - trade_amount)} (-{price_display})\n\n"
        f"Click Confirm to execute the trade."
    )

    await interaction.response.send_message(confirm_msg, view=view, ephemeral=True)
    view.message = await interaction.original_response()


@bot.tree.command(
    name="swap", description="Swap two players between teams (Admin only)"
)
@app_commands.describe(
    player_a="First player (moving from team_a to team_b)",
    team_a="Team giving player_a",
    player_b="Second player (moving from team_b to team_a)",
    team_b="Team giving player_b",
    compensation="Compensation amount in Crores (optional, default: 0)",
    compensation_from="Team paying compensation: team_a or team_b (optional)",
)
@app_commands.checks.has_permissions(administrator=True)
async def swap_command(
    interaction: discord.Interaction,
    player_a: str,
    team_a: str,
    player_b: str,
    team_b: str,
    compensation: float = 0.0,
    compensation_from: str = None,
):
    """Swap two players between teams with confirmation"""
    team_a_upper = team_a.upper()
    team_b_upper = team_b.upper()

    # Validate teams
    if team_a_upper not in TEAMS:
        await interaction.response.send_message(
            f"Invalid team: {team_a}. Valid teams: {', '.join(TEAMS.keys())}",
            ephemeral=True,
        )
        return
    if team_b_upper not in TEAMS:
        await interaction.response.send_message(
            f"Invalid team: {team_b}. Valid teams: {', '.join(TEAMS.keys())}",
            ephemeral=True,
        )
        return

    if team_a_upper == team_b_upper:
        await interaction.response.send_message(
            "Cannot swap players within the same team!", ephemeral=True
        )
        return

    # Check overseas limits before showing confirmation
    from config import MAX_OVERSEAS_LIMIT

    is_overseas_a = bot.auction_manager.db.get_player_overseas_status(player_a)
    is_overseas_b = bot.auction_manager.db.get_player_overseas_status(player_b)

    overseas_a = bot.auction_manager.db.get_overseas_count(team_a_upper)
    overseas_b = bot.auction_manager.db.get_overseas_count(team_b_upper)

    # Calculate post-swap overseas counts
    new_overseas_a = (
        overseas_a - (1 if is_overseas_a else 0) + (1 if is_overseas_b else 0)
    )
    new_overseas_b = (
        overseas_b - (1 if is_overseas_b else 0) + (1 if is_overseas_a else 0)
    )

    if new_overseas_a > MAX_OVERSEAS_LIMIT:
        await interaction.response.send_message(
            f"❌ **{team_a_upper}** would exceed overseas limit ({new_overseas_a}/{MAX_OVERSEAS_LIMIT}) after swap",
            ephemeral=True,
        )
        return
    if new_overseas_b > MAX_OVERSEAS_LIMIT:
        await interaction.response.send_message(
            f"❌ **{team_b_upper}** would exceed overseas limit ({new_overseas_b}/{MAX_OVERSEAS_LIMIT}) after swap",
            ephemeral=True,
        )
        return

    # Validate compensation_from if compensation is provided
    comp_from = None
    if compensation > 0:
        if not compensation_from:
            await interaction.response.send_message(
                "Please specify which team pays compensation (compensation_from)",
                ephemeral=True,
            )
            return
        comp_from = compensation_from.upper()
        if comp_from not in [team_a_upper, team_b_upper]:
            await interaction.response.send_message(
                f"compensation_from must be either {team_a_upper} or {team_b_upper}",
                ephemeral=True,
            )
            return

    # Get current purses and player prices for display
    teams_purse = bot.auction_manager.teams
    purse_a = teams_purse.get(team_a_upper, 0)
    purse_b = teams_purse.get(team_b_upper, 0)

    price_a = (
        bot.auction_manager.db.get_player_price_in_squad(team_a_upper, player_a) or 0
    )
    price_b = (
        bot.auction_manager.db.get_player_price_in_squad(team_b_upper, player_b) or 0
    )

    # Calculate expected purse changes
    price_diff = abs(price_a - price_b)
    comp_amount = int(compensation * 10_000_000) if compensation > 0 else 0

    # Determine who pays/receives
    if price_a > price_b:
        # Team A had more expensive player, gets the difference
        change_a = price_diff
        change_b = -price_diff
    else:
        # Team B had more expensive player, gets the difference
        change_a = -price_diff
        change_b = price_diff

    # Apply compensation
    if comp_amount > 0 and comp_from:
        if comp_from == team_a_upper:
            change_a -= comp_amount
            change_b += comp_amount
        else:
            change_a += comp_amount
            change_b -= comp_amount

    # Create confirmation view
    view = SwapConfirmView(
        bot,
        player_a,
        team_a_upper,
        player_b,
        team_b_upper,
        compensation,
        comp_from,
        interaction.user.id,
        interaction.channel,
    )

    overseas_tag_a = " ✈️" if is_overseas_a else ""
    overseas_tag_b = " ✈️" if is_overseas_b else ""

    confirm_msg = (
        f"**Confirm Swap:**\n"
        f"• **{player_a}**{overseas_tag_a} ({format_amount(price_a)}) → {team_b_upper}\n"
        f"• **{player_b}**{overseas_tag_b} ({format_amount(price_b)}) → {team_a_upper}\n"
    )
    if compensation > 0 and comp_from:
        confirm_msg += (
            f"• Compensation: **{comp_from}** pays **{format_amount(comp_amount)}**\n"
        )

    change_a_str = (
        f"+{format_amount(change_a)}"
        if change_a >= 0
        else f"-{format_amount(abs(change_a))}"
    )
    change_b_str = (
        f"+{format_amount(change_b)}"
        if change_b >= 0
        else f"-{format_amount(abs(change_b))}"
    )

    confirm_msg += (
        f"\n**Expected Purse Changes:**\n"
        f"• {team_a_upper}: {format_amount(purse_a)} → {format_amount(purse_a + change_a)} ({change_a_str})\n"
        f"• {team_b_upper}: {format_amount(purse_b)} → {format_amount(purse_b + change_b)} ({change_b_str})\n"
        f"\nClick Confirm to execute the swap."
    )

    await interaction.response.send_message(confirm_msg, view=view, ephemeral=True)
    view.message = await interaction.original_response()


class TradeConfirmView(discord.ui.View):
    def __init__(
        self,
        bot_ref: AuctionBot,
        player: str,
        from_team: str,
        to_team: str,
        price: float,
        user_id: int,
        channel: discord.TextChannel = None,
    ):
        super().__init__(timeout=60)
        self.bot_ref = bot_ref
        self.player = player
        self.from_team = from_team.upper()
        self.to_team = to_team.upper()
        self.price = price
        self.user_id = user_id
        self.channel = channel
        self.message: Optional[discord.Message] = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Confirm Trade ✅", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return

        # Perform the trade
        success, msg = self.bot_ref.auction_manager.trade_player(
            self.player, self.from_team, self.to_team, self.price
        )
        # Disable buttons and update admin message
        for item in self.children:
            item.disabled = True

        try:
            await interaction.response.edit_message(
                content="✅ Trade executed! See announcement below.", view=self
            )
        except Exception:
            pass

        # Send PUBLIC announcement of the trade (visible to everyone)
        if success and self.channel:
            public_msg = f"📢 **TRADE COMPLETED**\n{msg}"
            await self.channel.send(public_msg)
        elif success:
            await interaction.followup.send(msg)

        # Update stats and trade log if success
        if success:
            self.bot_ref.create_background_task(self.bot_ref.update_stats_display())
            self.bot_ref.create_background_task(update_trade_log())

    @discord.ui.button(label="Cancel ❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(
                content="Trade cancelled by user.", view=self
            )
        except Exception:
            await interaction.response.send_message("Trade cancelled.", ephemeral=True)


class SwapConfirmView(discord.ui.View):
    def __init__(
        self,
        bot_ref: AuctionBot,
        player_a: str,
        team_a: str,
        player_b: str,
        team_b: str,
        compensation: float,
        compensation_from: str,
        user_id: int,
        channel: discord.TextChannel = None,
    ):
        super().__init__(timeout=60)
        self.bot_ref = bot_ref
        self.player_a = player_a
        self.team_a = team_a.upper()
        self.player_b = player_b
        self.team_b = team_b.upper()
        self.compensation = compensation
        self.compensation_from = compensation_from
        self.user_id = user_id
        self.channel = channel
        self.message: Optional[discord.Message] = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Confirm Swap ✅", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return

        success, msg = self.bot_ref.auction_manager.swap_players(
            self.player_a,
            self.team_a,
            self.player_b,
            self.team_b,
            self.compensation,
            self.compensation_from,
        )

        for item in self.children:
            item.disabled = True

        try:
            await interaction.response.edit_message(
                content="✅ Swap executed! See announcement below.", view=self
            )
        except Exception:
            pass

        # Send PUBLIC announcement of the swap (visible to everyone)
        if success and self.channel:
            public_msg = f"📢 **SWAP COMPLETED**\n{msg}"
            await self.channel.send(public_msg)
        elif success:
            await interaction.followup.send(msg)

        if success:
            self.bot_ref.create_background_task(self.bot_ref.update_stats_display())
            self.bot_ref.create_background_task(update_trade_log())

    @discord.ui.button(label="Cancel ❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This is not your confirmation!", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(
                content="Swap cancelled by user.", view=self
            )
        except Exception:
            await interaction.response.send_message("Swap cancelled.", ephemeral=True)


# --------------------------
# Trade Log single-message pagination helpers (exactly one message)
# --------------------------


def paginate_long_message(content: str, max_size: int = 1900) -> List[str]:
    """Split content into safe pages, attempting to cut on blank lines or newlines."""
    if not content:
        return ["(No trades yet)"]
    pages: List[str] = []
    blocks = content.split("\n\n")
    current = ""
    for blk in blocks:
        cand_block = blk.rstrip()
        candidate = (current + "\n\n" + cand_block) if current else cand_block
        if len(candidate) <= max_size:
            current = candidate
        else:
            if current:
                pages.append(current)
            # If a single block is too large, split by lines
            if len(cand_block) > max_size:
                lines = cand_block.split("\n")
                chunk = ""
                for line in lines:
                    cand = (chunk + "\n" + line) if chunk else line
                    if len(cand) <= max_size:
                        chunk = cand
                    else:
                        if chunk:
                            pages.append(chunk)
                        chunk = line
                if chunk:
                    pages.append(chunk)
                current = ""
            else:
                current = cand_block
    if current:
        pages.append(current)
    # Add footer with page numbers
    for i in range(len(pages)):
        pages[i] = pages[i] + f"\n\n*Page {i+1}/{len(pages)}*"
    return pages


class TradeLogView(discord.ui.View):
    """A View that edits a single message's content to show multiple pages."""

    def __init__(self, pages: List[str], owner_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.pages = pages
        self.index = 0
        self.message: Optional[discord.Message] = None
        self.owner_id = owner_id
        self._refresh_buttons()

    def _refresh_buttons(self):
        # Guard: the buttons will be attached by discord, but we may not have them yet.
        try:
            self.prev_button.disabled = self.index == 0
            self.next_button.disabled = self.index >= len(self.pages) - 1
        except Exception:
            pass

    async def set_message(self, msg: discord.Message):
        """Attach the view to an existing message and ensure buttons reflect state."""
        self.message = msg
        self._refresh_buttons()
        try:
            await msg.edit(content=self.pages[self.index], view=self)
        except Exception:
            pass

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "You can't use this navigation.", ephemeral=True
            )
            return
        self.index = max(0, self.index - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            content=self.pages[self.index], view=self
        )

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "You can't use this navigation.", ephemeral=True
            )
            return
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            content=self.pages[self.index], view=self
        )


@bot.tree.command(
    name="settradechannel", description="Set channel for trade log display (Admin only)"
)
@app_commands.describe(channel="Channel for trade log display")
@app_commands.checks.has_permissions(administrator=True)
async def settradechannel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    """Set the channel where the single-message trade log will live."""
    await interaction.response.defer(ephemeral=True)

    trade_msg = bot.auction_manager.get_trade_log_message()
    pages = paginate_long_message(trade_msg)

    try:
        if len(pages) == 1:
            sent = await channel.send(pages[0])
            bot.auction_manager.set_trade_channel(str(channel.id), str(sent.id))
            await interaction.followup.send(
                f"✅ Trade log channel set to {channel.mention}. The trade log will auto-update after each trade.",
                ephemeral=True,
            )
        else:
            view = TradeLogView(pages)
            sent = await channel.send(pages[0], view=view)
            view.message = sent
            bot.auction_manager.set_trade_channel(str(channel.id), str(sent.id))
            await interaction.followup.send(
                f"✅ Trade log channel set to {channel.mention}. A single paginated message was created.",
                ephemeral=True,
            )
    except Exception as e:
        logger.error(f"Error setting trade channel: {e}")
        await interaction.followup.send(
            f"Failed to set trade channel: {e}", ephemeral=True
        )


async def update_trade_log():
    """Update the single trade-log message in the configured channel (keeps exactly one message)."""
    try:
        channel_id, message_id = bot.auction_manager.get_trade_channel()
        if not channel_id or not message_id:
            return

        channel = bot.get_channel(int(channel_id))
        if not channel:
            return

        trade_content = bot.auction_manager.get_trade_log_message()
        pages = paginate_long_message(trade_content)

        # Try fetch existing message
        msg = None
        try:
            msg = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            msg = None
        except Exception as e:
            logger.error(f"Error fetching trade log message: {e}")
            return

        # If message exists, edit it (attach view if multiple pages)
        if msg:
            if len(pages) == 1:
                try:
                    # remove any view and edit the single-page content
                    await msg.edit(content=pages[0], view=None)
                except discord.HTTPException as err:
                    logger.warning(
                        f"Failed to edit trade log message: {err}. Recreating message."
                    )
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    new_msg = await channel.send(pages[0])
                    bot.auction_manager.set_trade_channel(channel_id, str(new_msg.id))
                return
            else:
                # multiple pages -> ensure one message with TradeLogView
                view = TradeLogView(pages)
                try:
                    await msg.edit(content=pages[0], view=view)
                    view.message = msg
                except discord.HTTPException as err:
                    logger.warning(
                        f"Failed to edit trade log message to paginated view: {err}. Recreating message."
                    )
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    new_msg = await channel.send(pages[0], view=view)
                    view.message = new_msg
                    bot.auction_manager.set_trade_channel(channel_id, str(new_msg.id))
                return

        # If message doesn't exist, create a new single message (with view for multi-page)
        if len(pages) == 1:
            new_msg = await channel.send(pages[0])
            bot.auction_manager.set_trade_channel(channel_id, str(new_msg.id))
        else:
            view = TradeLogView(pages)
            new_msg = await channel.send(pages[0], view=view)
            view.message = new_msg
            bot.auction_manager.set_trade_channel(channel_id, str(new_msg.id))

    except Exception as e:
        logger.error(f"Error updating trade log message: {e}")


@bot.tree.command(name="tradelog", description="Show all trades")
async def tradelog(interaction: discord.Interaction):
    """Display the trade log"""
    msg = bot.auction_manager.get_trade_log_message()
    # If it's longer than Discord limit, paginate locally using the helper
    pages = paginate_long_message(msg)
    if len(pages) == 1:
        await interaction.response.send_message(pages[0])
    else:
        # Send first page with navigation view for the invoking user
        view = ShowListsView(pages, interaction.user.id)
        await interaction.response.send_message(pages[0], view=view)
        view.message = await interaction.original_response()


# ============================================================
# SOLD / UNSOLD / RE-AUCTION COMMANDS
# ============================================================


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

    await bot.cancel_countdown_task()

    import time

    bid_amount = (
        bot.auction_manager.current_bid
        if bot.auction_manager.current_bid > 0
        else (bot.auction_manager.base_price or 0)
    )
    bot.auction_manager.db.record_bid(
        player_name=player_name,
        team_code=team_upper,
        user_id=interaction.user.id,
        user_name=f"ADMIN:{interaction.user.name}",
        amount=bid_amount,
        timestamp=time.time(),
        is_auto_bid=False,
    )

    bot.auction_manager.highest_bidder = team_upper
    if bot.auction_manager.current_bid == 0:
        bot.auction_manager.current_bid = bid_amount
    bot.auction_manager._save_state_to_db()

    success, winning_team, amount = await bot.auction_manager.finalize_sale()

    if success:
        sold_msg = bot.formatter.format_sold_message(player_name, winning_team, amount)
        await interaction.response.send_message(sold_msg)
        if winning_team != "UNSOLD":
            await interaction.channel.send(bot.auction_manager.get_purse_display())

        bot.create_background_task(bot.update_stats_display())

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

    await bot.cancel_countdown_task()

    success, team, amount = await bot.auction_manager.finalize_sale()

    if success:
        sold_msg = bot.formatter.format_sold_message(player, "UNSOLD", amount)
        await interaction.response.send_message(sold_msg)
    else:
        bot.auction_manager._reset_player_state()
        bot.auction_manager._save_state_to_db()
        await interaction.response.send_message(
            f"❌ Player **{player}** marked **UNSOLD**. Use `/reauction {player}` to bring back."
        )

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


@bot.tree.command(name="showunsold", description="Show all unsold players (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def show_unsold(interaction: discord.Interaction):
    """Show all players that went unsold (in Accelerated list)"""
    unsold = bot.auction_manager.db.get_unsold_players()

    if not unsold:
        await interaction.response.send_message(
            "No unsold players found.", ephemeral=True
        )
        return

    msg = f"**📋 Unsold/Accelerated Players ({len(unsold)} total):**\n```\n"

    current_list = ""
    for pid, pname, list_name, base_price in unsold:
        if list_name != current_list:
            if current_list:
                msg += "\n"
            msg += f"--- {list_name.upper()} ---\n"
            current_list = list_name

        price_str = format_amount(base_price) if base_price else "N/A"
        msg += f"  {pname:30} | Base: {price_str}\n"

    msg += "```\n"
    msg += "Use `/reauction player_name` to bring back a single player to Accelerated\n"
    msg += "Use `/reauctionall` to bring back ALL unsold players"

    if len(msg) > 2000:
        await interaction.response.send_message(msg[:2000])
        for chunk in [msg[i : i + 2000] for i in range(2000, len(msg), 2000)]:
            await interaction.followup.send(chunk)
    else:
        await interaction.response.send_message(msg)


@bot.tree.command(
    name="reauctionall",
    description="Bring ALL unsold players back to auction (Admin only)",
)
@app_commands.checks.has_permissions(administrator=True)
async def reauction_all(interaction: discord.Interaction):
    """Re-auction all unsold players"""
    unsold = bot.auction_manager.db.get_unsold_players()

    if not unsold:
        await interaction.response.send_message(
            "No unsold players to re-auction.", ephemeral=True
        )
        return

    player_ids = [pid for pid, _, _, _ in unsold]
    count = bot.auction_manager.db.reauction_multiple_players(player_ids)

    await interaction.response.send_message(
        f"✅ **{count} unsold players** have been added back to auction at their original base prices!\n"
        f"Use `/resume` to continue the auction."
    )


@bot.tree.command(
    name="reauctionlist",
    description="Bring back unsold players from a specific set (Admin only)",
)
@app_commands.describe(
    set_name="Set name to re-auction unsold players from (e.g., M1, BA1)"
)
@app_commands.checks.has_permissions(administrator=True)
async def reauction_from_list(interaction: discord.Interaction, set_name: str):
    """Re-auction unsold players from a specific set"""
    unsold = bot.auction_manager.db.get_unsold_players()

    set_name_lower = set_name.lower()
    filtered = [
        (pid, pname, lname, bp)
        for pid, pname, lname, bp in unsold
        if lname.lower() == set_name_lower
    ]

    if not filtered:
        await interaction.response.send_message(
            f"No unsold players found in set **{set_name}**.", ephemeral=True
        )
        return

    player_ids = [pid for pid, _, _, _ in filtered]
    count = bot.auction_manager.db.reauction_multiple_players(player_ids)

    await interaction.response.send_message(
        f"✅ **{count} unsold players** from **{set_name}** have been added back to auction!"
    )


@bot.tree.command(
    name="reauctionmultiple",
    description="Re-auction multiple specific players by name (Admin only)",
)
@app_commands.describe(
    player_names="Comma-separated player names (e.g., Player1, Player2, Player3)"
)
@app_commands.checks.has_permissions(administrator=True)
async def reauction_multiple(interaction: discord.Interaction, player_names: str):
    """Re-auction multiple specific players"""
    await interaction.response.defer()

    names = [n.strip() for n in player_names.split(",") if n.strip()]

    if not names:
        await interaction.followup.send("No player names provided.", ephemeral=True)
        return

    success_list = []
    failed_list = []

    for name in names:
        success, msg = bot.auction_manager.reauction_player(name)
        if success:
            success_list.append(name)
        else:
            failed_list.append(f"{name}: {msg}")

    result_msg = ""
    if success_list:
        result_msg += (
            f"✅ **Re-auctioned ({len(success_list)}):** {', '.join(success_list)}\n"
        )
    if failed_list:
        result_msg += f"❌ **Failed ({len(failed_list)}):**\n" + "\n".join(failed_list)

    if not result_msg:
        result_msg = "No players processed."

    await interaction.followup.send(result_msg)


# ============================================================
# SEARCH & BASE PRICE COMMANDS
# ============================================================


@bot.tree.command(
    name="findplayer", description="Find a player across lists, squads, and sales"
)
@app_commands.describe(name="Player name (partial matches allowed)")
async def find_player_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    msg = bot.auction_manager.find_player(name)
    # Ensure we don't exceed Discord message length
    if len(msg) <= 2000:
        await interaction.followup.send(msg)
    else:
        # Chunk and send
        await interaction.followup.send(msg[:2000])
        for chunk in [msg[i : i + 2000] for i in range(2000, len(msg), 2000)]:
            await interaction.followup.send(chunk)


@bot.tree.command(
    name="changebaseprice",
    description="Change base price for players (use 'released' to change all released players)",
)
@app_commands.describe(
    players="Comma-separated player names OR the keyword 'released'",
    price="New base price in Crores (e.g., 2 = 2Cr, 0.5 = 50L)",
)
@app_commands.checks.has_permissions(administrator=True)
async def change_base_price_cmd(
    interaction: discord.Interaction, players: str, price: float
):
    await interaction.response.defer(ephemeral=True)
    success, msg = bot.auction_manager.change_base_price(players, price)
    if success:
        # update excel/stat message in background
        bot.create_background_task(bot.update_stats_display())
    await interaction.followup.send(msg, ephemeral=True)


# ============================================================
# MISC (errors / main)
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
        logger.error(f"Command error: {error}", exc_info=True)
        error_msg = f"An error occurred: {str(error)}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(error_msg, ephemeral=True)
            else:
                await interaction.followup.send(error_msg, ephemeral=True)
        except discord.HTTPException:
            logger.error(f"Could not send error message to user: {error_msg}")


if __name__ == "__main__":
    token = BOT_TOKEN or os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical(
            "Please set your bot token in DISCORD_TOKEN environment variable or config.py"
        )
    else:
        logger.info("Starting Discord Auction Bot...")
        bot.run(token)
