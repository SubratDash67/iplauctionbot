from discord import app_commands
import discord
import logging
from typing import Callable, List, Optional
from config import BOT_ADMINS

logger = logging.getLogger("AuctionBot.AdminChecks")

# Channel type constants
CHANNEL_AUCTION_ROOM = "auction_room"
CHANNEL_AUCTION_CHAT = "auction_chat"
CHANNEL_AUCTION_TEAM = "auction_team"
CHANNEL_AUCTION_NOTIFY = "auction_notify"

# Define which commands are allowed in which channels
# Commands not in any list are allowed everywhere
# Admin commands bypass channel restrictions

# Commands allowed in #auction-room (bidding channel)
AUCTION_ROOM_COMMANDS = [
    "bid",
    "bidhistory",
    "teambids",  # User commands
    "start",
    "stop",
    "pause",
    "resume",  # Admin auction control
    "skip",
    "skipset",
    "undobid",
    "rollback",
    "soldto",
    "unsold",  # Admin player management
    "reauction",
    "reauctionall",
    "reauctionlist",
    "reauctionmultiple",  # Admin re-auction
    "addplayer",
    "addplayers",
    "removeplayers",
    "loadcsv",
    "loadsets",
    "loadretained",  # Admin list management
    "moveplayer",
    "moveplayers",  # Admin move players
    "setcountdowngap",
    "setplayergap",
    "unsoldtime",
    "changebaseprice",
    "clear",  # Admin settings
]

# Commands allowed in #auction-chat (discussion channel - NO bidding)
AUCTION_CHAT_COMMANDS = [
    "myteam",
    "squad",
    "showteams",
    "showlists",
    "showpurse",
    "status",  # User commands
    "allsquads",
    "findplayer",
    "userhelp",  # User commands
    "assignteam",
    "assignteams",
    "unassignteam",  # Admin team assignment
    "setpurse",
    "resetpurses",  # Admin purse management
    "trade",
    "swap",
    "addtosquad",
    "release",
    "releasemultiple",
    "fixduplicates",  # Admin player management
    "setlistorder",  # Admin list ordering
]

# Commands allowed in #auction-team (final team submissions)
AUCTION_TEAM_COMMANDS = [
    "teamsquad",  # User commands only
]

# Commands allowed in #auction-notify (admin announcements, stats, logs)
AUCTION_NOTIFY_COMMANDS = [
    "tradelog",
    "showunsold",
    "showskipped",  # User can view (via admin display)
    "announce",
    "settradechannel",
    "setstatschannel",
    "adminhelp",  # Admin commands
]

# Commands that set channel configurations (always allowed everywhere for admins)
CHANNEL_CONFIG_COMMANDS = [
    "setauctionroom",
    "setauctionchat",
    "setauctionteam",
    "setnotifyauction",
    "showchannelconfig",
    "clearchannelconfig",
]


def admin_or_owner_check() -> Callable:
    """
    Allows:
      - application owner
      - user IDs in config.BOT_ADMINS
      - OR guild members with Administrator permission
    Logs the reason for allow/deny to help debugging (no debug command required).
    """

    # admin_checks.py (predicate body)
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        client = interaction.client
        try:
            app_info = getattr(client, "_cached_app_info", None)
            if app_info is None:
                app_info = await client.application_info()
                setattr(client, "_cached_app_info", app_info)
            owner = getattr(app_info, "owner", None)
            owner_id = getattr(owner, "id", None)
            if owner_id and user.id == owner_id:
                logger.info(
                    f"admin_or_owner_check: allowed by app owner (user={user.id})"
                )
                return True
        except Exception as e:
            logger.debug(f"app_info lookup failed: {e}")

        if user.id in BOT_ADMINS:
            logger.info(f"admin_or_owner_check: allowed by BOT_ADMINS (user={user.id})")
            return True

        try:
            if interaction.guild and user.guild_permissions.administrator:
                logger.info(
                    f"admin_or_owner_check: allowed by guild admin (user={user.id})"
                )
                return True
        except Exception as e:
            logger.debug(f"guild permission check error: {e}")

        logger.info(f"admin_or_owner_check: DENIED for user {user.id}")
        return False

    return app_commands.check(predicate)


async def is_admin_or_owner(interaction: discord.Interaction) -> bool:
    """
    Helper function to check if a user is an admin/owner without using it as a decorator.
    Used for channel permission bypass logic.
    """
    user = interaction.user
    client = interaction.client

    try:
        app_info = getattr(client, "_cached_app_info", None)
        if app_info is None:
            app_info = await client.application_info()
            setattr(client, "_cached_app_info", app_info)
        owner = getattr(app_info, "owner", None)
        owner_id = getattr(owner, "id", None)
        if owner_id and user.id == owner_id:
            return True
    except Exception:
        pass

    if user.id in BOT_ADMINS:
        return True

    try:
        if interaction.guild and user.guild_permissions.administrator:
            return True
    except Exception:
        pass

    return False


def get_command_allowed_channels(command_name: str) -> List[str]:
    """
    Get the list of channel types where a command is allowed.
    Returns empty list if command is allowed everywhere.
    """
    allowed_channels = []

    if command_name in AUCTION_ROOM_COMMANDS:
        allowed_channels.append(CHANNEL_AUCTION_ROOM)
    if command_name in AUCTION_CHAT_COMMANDS:
        allowed_channels.append(CHANNEL_AUCTION_CHAT)
    if command_name in AUCTION_TEAM_COMMANDS:
        allowed_channels.append(CHANNEL_AUCTION_TEAM)
    if command_name in AUCTION_NOTIFY_COMMANDS:
        allowed_channels.append(CHANNEL_AUCTION_NOTIFY)

    return allowed_channels


def channel_permission_check(command_name: str) -> Callable:
    """
    Check if a command can be used in the current channel.

    Rules:
    1. Admin commands bypass channel restrictions (admins can use any command anywhere)
    2. Channel config commands (setauctionroom, etc.) are always allowed for admins
    3. User commands are restricted to their designated channels
    4. If no channels are configured, commands work everywhere (backward compatibility)
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        # Channel config commands are always allowed for admins
        if command_name in CHANNEL_CONFIG_COMMANDS:
            return True

        # Check if user is admin - admins bypass channel restrictions
        if await is_admin_or_owner(interaction):
            logger.debug(f"channel_permission_check: admin bypass for {command_name}")
            return True

        # Get the allowed channel types for this command
        allowed_channel_types = get_command_allowed_channels(command_name)

        # If command is not in any specific channel list, allow everywhere
        if not allowed_channel_types:
            logger.debug(f"channel_permission_check: {command_name} allowed everywhere")
            return True

        # Get channel configuration from database
        if not interaction.guild:
            # DM context - allow if command isn't channel-restricted
            return True

        # Access the database through the bot's auction_manager
        try:
            db = interaction.client.auction_manager.db
            guild_id = str(interaction.guild.id)
            current_channel_id = str(interaction.channel.id)

            # Get all channel configs for this guild
            channel_configs = db.get_all_channel_configs(guild_id)

            # If no channels configured at all, allow everywhere (backward compatibility)
            if not channel_configs:
                logger.debug(
                    f"channel_permission_check: no channels configured, allowing {command_name}"
                )
                return True

            # Check if current channel matches any of the allowed channel types
            for channel_type in allowed_channel_types:
                configured_channel = channel_configs.get(channel_type)
                if configured_channel and configured_channel == current_channel_id:
                    logger.debug(
                        f"channel_permission_check: {command_name} allowed in {channel_type}"
                    )
                    return True

            # Command not allowed in this channel
            # Build helpful error message
            allowed_channel_names = []
            for ch_type in allowed_channel_types:
                ch_id = channel_configs.get(ch_type)
                if ch_id:
                    channel = interaction.guild.get_channel(int(ch_id))
                    if channel:
                        allowed_channel_names.append(f"#{channel.name}")
                    else:
                        allowed_channel_names.append(f"<#{ch_id}>")

            if allowed_channel_names:
                channels_str = ", ".join(allowed_channel_names)
                error_msg = f"❌ This command (`/{command_name}`) can only be used in: {channels_str}"
            else:
                # None of the allowed channels are configured
                error_msg = f"❌ This command (`/{command_name}`) requires channel configuration. Ask an admin to set up auction channels."

            # We need to raise a CheckFailure with a custom message
            raise app_commands.CheckFailure(error_msg)

        except app_commands.CheckFailure:
            raise  # Re-raise our custom error
        except Exception as e:
            logger.error(f"channel_permission_check error: {e}")
            # On error, allow the command (fail open for safety)
            return True

    return app_commands.check(predicate)
