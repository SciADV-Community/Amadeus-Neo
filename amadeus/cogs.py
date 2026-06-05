import os
import re

import discord
from discord.ext import commands

from amadeus.logging_utils import log

AMADEUS_DEBUG = os.environ.get("AMADEUS_DEBUG", "false").lower() in {"1", "true", "yes", "y", "on"}
AMADEUS_DEBUG_SERVER_ID = os.environ.get("AMADEUS_DEBUG_SERVER_ID", "")

COG_ENV_NAME = "AMADEUS_COGS"

# These cogs are always loaded by main.py for bot administration.
STATIC_ADMIN_COG = "cogs.amadeus_admin"
STATIC_OWNER_COG = "cogs.amadeus_owner"
STATIC_COG_EXTENSIONS = {STATIC_ADMIN_COG, STATIC_OWNER_COG}

# Safe default if AMADEUS_COGS is unset
DEFAULT_AMADEUS_COGS = ""


def normalize_extension_name(extension: str) -> str:
    """
    Accepts:

    - bouncer
    - cogs.bouncer
    - cogs/bouncer.py

    Returns:

    - cogs.bouncer
    """

    extension = extension.strip()

    if not extension:
        return ""

    if extension.endswith(".py"):
        extension = extension[:-3]

    extension = extension.replace("/", ".").replace("\\", ".")

    if "." not in extension:
        extension = f"cogs.{extension}"

    return extension


def parse_extension_list(raw_value: str) -> list[str]:
    """
    Parses a comma, semicolon, or whitespace-separated extension list.

    Example:
    cogs.bouncer,cogs.general
    """

    extensions: list[str] = []

    for piece in re.split(r"[,;\s]+", raw_value):
        extension = normalize_extension_name(piece)

        if not extension:
            continue

        if extension not in extensions:
            extensions.append(extension)

    return extensions


def is_static_extension(extension: str) -> bool:
    """
    Returns True if this extension is managed statically by main.py.
    """

    return normalize_extension_name(extension) in STATIC_COG_EXTENSIONS


def _paired_admin_extension(extension: str) -> str | None:
    """
    Returns the *_admin paired extension name if its file exists, else None.

    Example: cogs.bouncer -> cogs.bouncer_admin (if cogs/bouncer_admin.py exists)
    """

    admin_ext = f"{extension}_admin"
    path = admin_ext.replace(".", os.sep) + ".py"

    if os.path.isfile(path):
        log(f"COGS // PAIRED ADMIN FOUND 『 {admin_ext} 』", level="debug", logger_name="cogs")
        return admin_ext

    log(f"COGS // NO PAIRED ADMIN 『 {extension} 』", level="debug", logger_name="cogs")
    return None


def get_configured_cog_extensions(
    *,
    env_name: str = COG_ENV_NAME,
    default_value: str = DEFAULT_AMADEUS_COGS,
    skip_static: bool = True,
) -> list[str]:
    """
    Reads cog extensions from an environment variable and auto-pairs *_admin variants.

    Example:
    AMADEUS_COGS="cogs.bouncer"  ->  ["cogs.bouncer", "cogs.bouncer_admin"]

    The static cogs are skipped by default because main.py always loads them.
    """

    raw_value = os.environ.get(env_name, default_value)

    if not raw_value.strip():
        raw_value = default_value

    extensions = parse_extension_list(raw_value)

    if skip_static:
        extensions = [e for e in extensions if not is_static_extension(e)]

    result: list[str] = []

    for ext in extensions:
        if ext not in result:
            result.append(ext)

        admin_ext = _paired_admin_extension(ext)

        if admin_ext and admin_ext not in result:
            result.append(admin_ext)

    return result


def extension_to_module_name(extension: str) -> str:
    """
    Returns the short module name for an extension.

    cogs.bouncer -> bouncer
    """

    ext = normalize_extension_name(extension)

    if ext.startswith("cogs."):
        return ext[5:]

    return ext


def format_extension_error(error: Exception) -> str:
    """
    Formats discord.py extension errors for admin command responses.
    """

    if isinstance(error, commands.ExtensionFailed):
        original = error.original
        return f"{type(original).__name__}: {original}"

    return f"{type(error).__name__}: {error}"


async def sync_commands_to_guild(
    bot: commands.Bot,
    guild: discord.abc.Snowflake,
) -> int:
    """
    Syncs the bot's current slash command tree to one guild.

    Useful after loading or unloading cogs.
    """

    log(f"COGS // SYNC COMMANDS 『 GUILD {guild.id} 』", level="debug", logger_name="cogs")

    guild_object = discord.Object(id=guild.id)

    bot.tree.clear_commands(guild=guild_object)
    bot.tree.copy_global_to(guild=guild_object)

    synced = await bot.tree.sync(guild=guild_object)

    return len(synced)


async def load_extensions(bot: commands.Bot) -> None:
    """
    Loads all configured cog extensions, including auto-paired *_admin variants.

    Static cogs (amadeus_admin, amadeus_owner) are loaded first, then all
    configured extensions from AMADEUS_COGS.
    """
    await bot.load_extension(STATIC_OWNER_COG)
    await bot.load_extension(STATIC_ADMIN_COG)
    log("« AMADEUS ADMINISTRATION ONLINE »\n", logger_name="core")
    log("« LOADING COG UNITS ⁑", logger_name="core")

    for extension in get_configured_cog_extensions():
        try:
            log(f"  LOADING 『 {extension} 』", level="debug", logger_name="core")
            await bot.load_extension(extension)
            log(f"  ONLINE 『 {extension} 』", logger_name="core")
        except commands.ExtensionNotFound:
            log(f"  FAILURE 『 {extension} 』 — module not found", level="error", logger_name="core")
        except commands.ExtensionAlreadyLoaded:
            log(f"  FAILURE 『 {extension} 』 — already loaded", level="warning", logger_name="core")
        except commands.NoEntryPointError:
            log(f"  FAILURE 『 {extension} 』 — missing setup() function", level="error", logger_name="core")
        except commands.ExtensionFailed as e:
            log(f"  FAILURE 『 {extension} 』 — {type(e.original).__name__}: {e.original}", level="exception", logger_name="core")


async def sync_application_commands(bot: commands.Bot) -> None:
    """
    Syncs slash commands after all extensions are loaded.

    Debug mode (AMADEUS_DEBUG=true + AMADEUS_DEBUG_SERVER_ID set):
        Syncs to the test guild only for fast iteration.
        Clears global commands so they don't shadow guild-scoped ones.

    Production mode:
        Clears test guild commands if AMADEUS_DEBUG_SERVER_ID is set.
        Syncs globally.
    """
    log(
        f"SYNC // DEBUG {AMADEUS_DEBUG} // GUILD 『 {AMADEUS_DEBUG_SERVER_ID or '—'} 』",
        level="debug",
        logger_name="core",
    )

    if AMADEUS_DEBUG and AMADEUS_DEBUG_SERVER_ID:
        guild = discord.Object(id=int(AMADEUS_DEBUG_SERVER_ID))
        synced_count = await sync_commands_to_guild(bot, guild)

        # Clear stale global commands after the guild sync — must come after
        # sync_commands_to_guild since that function copies from the global tree.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        log(f"« SYNCED {synced_count} COMMANDS TO TEST UNIT {AMADEUS_DEBUG_SERVER_ID}", logger_name="core")

    else:
        if AMADEUS_DEBUG_SERVER_ID:
            test_guild = discord.Object(id=int(AMADEUS_DEBUG_SERVER_ID))
            bot.tree.clear_commands(guild=test_guild)
            await bot.tree.sync(guild=test_guild)

        synced = await bot.tree.sync()
        log(f"« SYNCED {len(synced)} GLOBAL COMMANDS", logger_name="core")
