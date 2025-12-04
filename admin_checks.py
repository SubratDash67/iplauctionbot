from discord import app_commands
import discord
import logging
from typing import Callable
from config import BOT_ADMINS

logger = logging.getLogger("AuctionBot.AdminChecks")

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
                logger.info(f"admin_or_owner_check: allowed by app owner (user={user.id})")
                return True
        except Exception as e:
            logger.debug(f"app_info lookup failed: {e}")

        if user.id in BOT_ADMINS:
            logger.info(f"admin_or_owner_check: allowed by BOT_ADMINS (user={user.id})")
            return True

        try:
            if interaction.guild and user.guild_permissions.administrator:
                logger.info(f"admin_or_owner_check: allowed by guild admin (user={user.id})")
                return True
        except Exception as e:
            logger.debug(f"guild permission check error: {e}")

        logger.info(f"admin_or_owner_check: DENIED for user {user.id}")
        return False

    return app_commands.check(predicate)