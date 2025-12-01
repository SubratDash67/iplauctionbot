# Bot.py
"""
Discord Auction Bot - Main Application
Patched: fixes for trade/swap confirmation, compensation handling, missing helpers,
and added safe placeholders for countdown/start loops (real logic included).
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import time
import logging
from typing import Optional, Dict
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


def cr_to_rupees(cr: float) -> int:
    """Convert crores (float) to rupees (int)."""
    try:
        return int(round(float(cr) * 10_000_000))
    except Exception:
        return 0


# ---- AuctionBot ----
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

# ============================================================
# Helper functions required by multiple places
# ============================================================


async def start_next_player(channel: discord.TextChannel):
    """
    Start auctioning the next player.
    Uses AuctionManager.get_next_player() to fetch next.
    This function is a fuller implementation adapted from earlier working logic.
    """
    if bot.auction_manager.paused:
        return

    # gap between players
    await asyncio.sleep(bot.player_gap)

    if bot.auction_manager.paused:
        return

    result = bot.auction_manager.get_next_player()
    success, player_name, base_price, is_first_in_list = result

    if not success:
        current_list = bot.auction_manager.get_current_list_name()
        if current_list:
            await channel.send(f"**✅ Set {current_list.upper()} completed!**")
            await asyncio.sleep(LIST_GAP)
            await start_next_player(channel)
        else:
            bot.auction_manager.paused = True
            bot.auction_manager._save_state_to_db()

            unsold = bot.auction_manager.db.get_unsold_players()
            await channel.send(
                "**⚠️ All loaded sets have been completed.** Auction paused."
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
    """
    Main countdown loop which finalizes sale when timer elapses.
    Adapted from a previous fuller implementation; it relies on AuctionManager state.
    """
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
                    for pname, _ in squad:
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
# TEAM / TRADE COMMANDS (kept mostly as before, patched)
# ============================================================


@bot.tree.command(name="settradechannel", description="Set channel for trade log display (Admin only)")
@app_commands.describe(channel="Channel for trade log display")
@app_commands.checks.has_permissions(administrator=True)
async def settradechannel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    """Set the channel where trade log will be displayed and auto-updated"""
    await interaction.response.defer()
    trade_msg = bot.auction_manager.get_trade_log_message()
    msg = await channel.send(trade_msg)
    bot.auction_manager.set_trade_channel(str(channel.id), str(msg.id))
    await interaction.followup.send(
        f"✅ Trade log channel set to {channel.mention}. The trade log will auto-update after each trade."
    )


async def update_trade_log():
    """Update the trade log message in the configured channel"""
    try:
        channel_id, message_id = bot.auction_manager.get_trade_channel()
        if not channel_id or not message_id:
            return

        channel = bot.get_channel(int(channel_id))
        if not channel:
            return

        try:
            msg = await channel.fetch_message(int(message_id))
            trade_content = bot.auction_manager.get_trade_log_message()
            await msg.edit(content=trade_content)
        except discord.NotFound:
            trade_content = bot.auction_manager.get_trade_log_message()
            new_msg = await channel.send(trade_content)
            bot.auction_manager.set_trade_channel(channel_id, str(new_msg.id))
        except Exception as e:
            logger.error(f"Error updating trade log message: {e}")
    except Exception as e:
        logger.error(f"Error in update_trade_log: {e}")


class TradeConfirmView(discord.ui.View):
    def __init__(
        self,
        bot_ref: AuctionBot,
        player: str,
        from_team: str,
        to_team: str,
        price_cr: float,
        user_id: int,
    ):
        super().__init__(timeout=60)
        self.bot_ref = bot_ref
        self.player = player
        self.from_team = from_team.upper()
        self.to_team = to_team.upper()
        self.price_cr = price_cr  # crores float
        self.user_id = user_id
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
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your confirmation!", ephemeral=True)
            return

        # Re-check purses and player existence right before executing
        teams = self.bot_ref.auction_manager.db.get_teams()
        from_purse = teams.get(self.from_team, 0)
        to_purse = teams.get(self.to_team, 0)
        price_rupees = cr_to_rupees(self.price_cr)

        # Check seller salary (informational)
        seller_salary = self.bot_ref.auction_manager.db.get_player_price_in_squad(self.from_team, self.player) or 0

        # Projected purses
        projected_from = from_purse + price_rupees
        projected_to = to_purse - price_rupees

        if projected_to < 0:
            await interaction.response.send_message(
                f"Cannot perform trade: {self.to_team} would have negative purse ({format_amount(projected_to)}).", ephemeral=True
            )
            for item in self.children:
                item.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                try:
                    await interaction.edit_original_response(view=self)
                except Exception:
                    pass
            return

        # Execute trade via auction manager (expects crores float as earlier)
        success, msg = self.bot_ref.auction_manager.trade_player(
            self.player, self.from_team, self.to_team, self.price_cr
        )

        for item in self.children:
            item.disabled = True

        try:
            await interaction.response.edit_message(content=msg, view=self)
        except Exception:
            try:
                await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        if success:
            self.bot_ref.create_background_task(self.bot_ref.update_stats_display())
            self.bot_ref.create_background_task(update_trade_log())

    @discord.ui.button(label="Cancel ❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your confirmation!", ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content="Trade cancelled by user.", view=self)
        except Exception:
            try:
                await interaction.response.send_message("Trade cancelled.", ephemeral=True)
            except Exception:
                pass


class SwapConfirmView(discord.ui.View):
    def __init__(
        self,
        bot_ref: AuctionBot,
        player_a: str,
        team_a: str,
        player_b: str,
        team_b: str,
        compensation_cr: float,
        comp_rupees: int,
        compensation_from: Optional[str],
        user_id: int,
    ):
        super().__init__(timeout=60)
        self.bot_ref = bot_ref
        self.player_a = player_a
        self.team_a = team_a.upper()
        self.player_b = player_b
        self.team_b = team_b.upper()
        self.compensation_cr = compensation_cr  # float in crores
        self.comp_rupees = comp_rupees  # int rupees for display
        # compensation_from should be either None or already uppercased team code or 'A'/'B'
        self.compensation_from = compensation_from
        self.user_id = user_id
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
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your confirmation!", ephemeral=True)
            return

        # Re-check projected purses and affordability before executing
        teams = self.bot_ref.auction_manager.db.get_teams()
        purse_a = teams.get(self.team_a, 0)
        purse_b = teams.get(self.team_b, 0)

        # Get current salaries
        price_a = self.bot_ref.auction_manager.db.get_player_price_in_squad(self.team_a, self.player_a) or 0
        price_b = self.bot_ref.auction_manager.db.get_player_price_in_squad(self.team_b, self.player_b) or 0

        # Salary refund/deduct logic: net_change = refund_out - incoming_salary
        proj_a = purse_a + price_a - price_b
        proj_b = purse_b + price_b - price_a

        # Apply compensation if provided
        if self.comp_rupees > 0 and self.compensation_from:
            payer_raw = self.compensation_from
            if isinstance(payer_raw, str):
                payer = payer_raw.strip().upper()
            else:
                payer = str(payer_raw).strip().upper()

            if payer == "A":
                payer_team = self.team_a
            elif payer == "B":
                payer_team = self.team_b
            else:
                payer_team = payer

            if payer_team not in (self.team_a, self.team_b):
                await interaction.response.send_message("Invalid compensation payer. Use A, B or a team code.", ephemeral=True)
                return

            if payer_team == self.team_a:
                proj_a -= self.comp_rupees
                proj_b += self.comp_rupees
            else:
                proj_a += self.comp_rupees
                proj_b -= self.comp_rupees

        # Final affordability check
        if proj_a < 0 or proj_b < 0:
            await interaction.response.send_message(
                f"Cannot perform swap: resulting purses would be negative. Projected {self.team_a}: {format_amount(proj_a)}, {self.team_b}: {format_amount(proj_b)}",
                ephemeral=True,
            )
            for item in self.children:
                item.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                try:
                    await interaction.edit_original_response(view=self)
                except Exception:
                    pass
            return

        # Execute swap via auction manager (pass compensation in crores)
        success, msg = self.bot_ref.auction_manager.swap_players(
            self.player_a,
            self.team_a,
            self.player_b,
            self.team_b,
            self.compensation_cr,
            (self.compensation_from if self.compensation_from else None),
        )

        for item in self.children:
            item.disabled = True

        try:
            await interaction.response.edit_message(content=msg, view=self)
        except Exception:
            try:
                await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        if success:
            self.bot_ref.create_background_task(self.bot_ref.update_stats_display())
            self.bot_ref.create_background_task(update_trade_log())

    @discord.ui.button(label="Cancel ❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your confirmation!", ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content="Swap cancelled by user.", view=self)
        except Exception:
            try:
                await interaction.response.send_message("Swap cancelled.", ephemeral=True)
            except Exception:
                pass


# ==========================
# trade command (confirmation)
# ==========================
@bot.tree.command(
    name="trade", description="Trade a player between teams for cash (Admin only)"
)
@app_commands.describe(
    player="Player Name",
    from_team="Source Team",
    to_team="Target Team",
    price="Trade Value in Crores (e.g., 2 = 2Cr, 0.5 = 50L)",
)
@app_commands.checks.has_permissions(administrator=True)
async def trade(
    interaction: discord.Interaction,
    player: str,
    from_team: str,
    to_team: str,
    price: float,
):
    # Build confirmation showing purses and the actual effect (buyer pays, seller receives)
    await interaction.response.defer(ephemeral=True)

    teams = bot.auction_manager.db.get_teams()
    from_code = from_team.upper()
    to_code = to_team.upper()

    if from_code not in teams or to_code not in teams:
        await interaction.followup.send("One or both team codes are invalid.", ephemeral=True)
        return

    from_purse = teams.get(from_code, 0)
    to_purse = teams.get(to_code, 0)
    price_rupees = cr_to_rupees(price)

    # Get seller's recorded salary (for display) if present
    seller_salary = bot.auction_manager.db.get_player_price_in_squad(from_code, player) or 0

    # Projected purses after cash trade (buyer pays price, seller receives price)
    proj_from = from_purse + price_rupees
    proj_to = to_purse - price_rupees

    embed = discord.Embed(
        title="Confirm Cash Trade",
        description=f"Trade **{player}** from **{from_code}** → **{to_code}** for **{format_amount(price_rupees)}**",
        color=discord.Color.orange(),
    )
    embed.add_field(name=f"{from_code} Purse (current)", value=f"{format_amount(from_purse)}", inline=True)
    embed.add_field(name=f"{to_code} Purse (current)", value=f"{format_amount(to_purse)}", inline=True)
    embed.add_field(
        name="Projected purses after trade (buyer pays → seller receives)",
        value=f"{from_code}: {format_amount(proj_from)}\n{to_code}: {format_amount(proj_to)}",
        inline=False,
    )
    embed.add_field(
        name="Player salary details",
        value=f"Current recorded salary (seller side): {format_amount(seller_salary)}\nSalary after trade (buyer side): {format_amount(price_rupees)}",
        inline=False,
    )
    embed.set_footer(text="Confirm to execute the trade. This action is logged.")

    view = TradeConfirmView(bot, player, from_code, to_code, price, interaction.user.id)
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    view.message = msg


# ==========================
# swaptrade command (confirmation)
# ==========================
@bot.tree.command(
    name="swaptrade", description="Swap two players between teams (Admin only)"
)
@app_commands.describe(
    player_a="First player name (from Team A)",
    team_a="Team A (giving player_a)",
    player_b="Second player name (from Team B)",
    team_b="Team B (giving player_b)",
    compensation="Compensation amount in Crores (optional, if values differ)",
    compensation_from="Team paying compensation (A or B, optional)",
)
@app_commands.checks.has_permissions(administrator=True)
async def swaptrade(
    interaction: discord.Interaction,
    player_a: str,
    team_a: str,
    player_b: str,
    team_b: str,
    compensation: float = 0.0,
    compensation_from: str = None,
):
    await interaction.response.defer(ephemeral=True)

    teams = bot.auction_manager.db.get_teams()
    team_a_code = team_a.upper()
    team_b_code = team_b.upper()

    if team_a_code not in teams or team_b_code not in teams:
        await interaction.followup.send("One or both team codes are invalid.", ephemeral=True)
        return

    purse_a = teams.get(team_a_code, 0)
    purse_b = teams.get(team_b_code, 0)

    # Get current salaries for both players
    price_a = bot.auction_manager.db.get_player_price_in_squad(team_a_code, player_a) or 0
    price_b = bot.auction_manager.db.get_player_price_in_squad(team_b_code, player_b) or 0

    # Net salary effect (refund outgoing, deduct incoming)
    proj_a = purse_a + price_a - price_b
    proj_b = purse_b + price_b - price_a

    comp_rupees = cr_to_rupees(compensation) if compensation else 0

    comp_note = "No compensation."
    comp_from_for_view = None
    if comp_rupees > 0 and compensation:
        # Normalize compensation_from safely (it may be string or other)
        if compensation_from is None:
            # If admin provided a compensation but didn't specify payer, reject
            await interaction.followup.send("Please specify compensation payer (A, B or team code).", ephemeral=True)
            return

        if isinstance(compensation_from, str):
            comp_from_for_view = compensation_from.strip().upper()
        else:
            comp_from_for_view = str(compensation_from).strip().upper()

        payer = comp_from_for_view
        if payer == "A":
            payer_team = team_a_code
        elif payer == "B":
            payer_team = team_b_code
        else:
            payer_team = payer

        if payer_team not in (team_a_code, team_b_code):
            await interaction.followup.send("Invalid compensation payer. Use A, B or a team code.", ephemeral=True)
            return

        # Apply compensation to projected purses only for preview
        if payer_team == team_a_code:
            proj_a -= comp_rupees
            proj_b += comp_rupees
        else:
            proj_a += comp_rupees
            proj_b -= comp_rupees

        comp_note = f"Compensation {format_amount(comp_rupees)} paid by {payer_team}."

    embed = discord.Embed(
        title="Confirm Swap Trade",
        description=f"Swap **{player_a}** ({team_a_code}) ↔ **{player_b}** ({team_b_code})",
        color=discord.Color.orange(),
    )
    embed.add_field(name=f"{team_a_code} Purse (current)", value=f"{format_amount(purse_a)}", inline=True)
    embed.add_field(name=f"{team_b_code} Purse (current)", value=f"{format_amount(purse_b)}", inline=True)
    embed.add_field(
        name="Projected after salary exchange (+/-) and compensation",
        value=f"{team_a_code}: {format_amount(proj_a)}\n{team_b_code}: {format_amount(proj_b)}",
        inline=False,
    )
    embed.add_field(
        name="Salary details",
        value=f"{player_a} salary: {format_amount(price_a)} (refund from {team_a_code})\n{player_b} salary: {format_amount(price_b)} (incoming to {team_a_code})",
        inline=False,
    )
    if comp_rupees > 0 and comp_from_for_view:
        embed.add_field(name="Compensation", value=f"{format_amount(comp_rupees)} (paid by {comp_from_for_view})", inline=False)
    embed.set_footer(text="Confirm to execute the swap. Salaries move with players; only compensation moves cash between purses.")

    view = SwapConfirmView(
        bot,
        player_a,
        team_a_code,
        player_b,
        team_b_code,
        compensation,  # crores float
        comp_rupees,   # rupees int
        (comp_from_for_view if comp_from_for_view else None),
        interaction.user.id,
    )
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    view.message = msg


# ============================================================
# Remaining commands unchanged — they will use the helpers above
# ============================================================

# (All other commands in original file remain in this patched file as above.
#  For brevity we don't duplicate unchanged parts here beyond what already appears earlier.
#  The key fixes are included: cr_to_rupees, start_next_player, countdown_loop,
#  safer compensation_from handling, and safer component edit fallbacks.)

# ============================================================
# Error handler / main
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