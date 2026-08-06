"""
Debug command handlers for /amadeus debug.

This is a helper module, not a cog. Functions here are wired to the
amadeus_debug subgroup in amadeus_admin.py.
"""

import re

import discord

from amadeus.discord_utils import NO_MENTIONS


MESSAGE_ID_RE = re.compile(r"^\d{17,20}$")
MESSAGE_LINK_RE = re.compile(r"/channels/\d+/\d+/(\d{17,20})(?:\D|$)")
REACTOR_PAGE_BODY_LIMIT = 1800


async def cmd_ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)


def parse_message_id(post_id: str) -> int | None:
    value = post_id.strip()

    if MESSAGE_ID_RE.match(value):
        return int(value)

    match = MESSAGE_LINK_RE.search(value)
    if match:
        return int(match.group(1))

    return None


def _emoji_name(emoji: str | discord.Emoji | discord.PartialEmoji) -> str | None:
    return getattr(emoji, "name", None)


def _emoji_id(emoji: str | discord.Emoji | discord.PartialEmoji) -> int | None:
    return getattr(emoji, "id", None)


def reaction_matches_emoji(
    reaction_emoji: str | discord.Emoji | discord.PartialEmoji,
    requested_emoji: str,
) -> bool:
    requested = requested_emoji.strip()
    if not requested:
        return False

    requested_partial = discord.PartialEmoji.from_str(requested)
    requested_id = requested_partial.id

    if requested_id is not None:
        return _emoji_id(reaction_emoji) == requested_id

    if isinstance(reaction_emoji, str):
        return reaction_emoji == requested

    stripped_name = requested.strip(":")
    return (
        str(reaction_emoji) == requested
        or _emoji_name(reaction_emoji) == requested
        or _emoji_name(reaction_emoji) == stripped_name
    )


def find_reaction(
    message: discord.Message,
    requested_emoji: str,
) -> discord.Reaction | None:
    for reaction in message.reactions:
        if reaction_matches_emoji(reaction.emoji, requested_emoji):
            return reaction

    return None


async def fetch_reaction_users(reaction: discord.Reaction) -> list[discord.User | discord.Member]:
    users_by_id: dict[int, discord.User | discord.Member] = {}

    async def collect(reaction_type: discord.ReactionType) -> None:
        async for user in reaction.users(limit=None, type=reaction_type):
            users_by_id.setdefault(user.id, user)

    await collect(discord.ReactionType.normal)

    if getattr(reaction, "burst_count", 0):
        await collect(discord.ReactionType.burst)

    return list(users_by_id.values())


def username_for(user: discord.User | discord.Member) -> str:
    return user.name


def format_reactor_pages(
    *,
    post_id: int,
    emoji: str,
    usernames: list[str],
) -> list[str]:
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_length = 0

    for username in usernames:
        line_length = len(username) + 1
        if current_chunk and current_length + line_length > REACTOR_PAGE_BODY_LIMIT:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0

        current_chunk.append(username)
        current_length += line_length

    if current_chunk:
        chunks.append(current_chunk)

    total_pages = len(chunks)
    pages = []
    for index, chunk in enumerate(chunks, start=1):
        page_label = f", page {index}/{total_pages}" if total_pages > 1 else ""
        pages.append(
            f"Users who reacted with {emoji} on message `{post_id}` "
            f"({len(usernames)} total{page_label}):\n"
            "```text\n"
            + "\n".join(chunk)
            + "\n```"
        )

    return pages


async def cmd_reacts(
    interaction: discord.Interaction,
    post_id: str,
    emoji: str,
) -> None:
    message_id = parse_message_id(post_id)
    if message_id is None:
        await interaction.response.send_message(
            "Post ID must be a Discord message ID or message link.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    if channel is None or not hasattr(channel, "fetch_message"):
        await interaction.response.send_message(
            "Run this in the channel or thread that contains the message.",
            ephemeral=True,
        )
        return

    requested_emoji = emoji.strip()
    if not requested_emoji:
        await interaction.response.send_message(
            "Emoji cannot be empty.",
            ephemeral=True,
        )
        return

    if len(requested_emoji) > 128:
        await interaction.response.send_message(
            "Emoji must be 128 characters or fewer.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await interaction.followup.send(
            f"I could not find message `{message_id}` in this channel or thread.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return
    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to read that message or its reactions.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"Discord rejected the message lookup: `{e}`",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return

    reaction = find_reaction(message, requested_emoji)
    if reaction is None:
        await interaction.followup.send(
            f"No reaction matching {requested_emoji} was found on message `{message_id}`.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return

    try:
        users = await fetch_reaction_users(reaction)
    except discord.Forbidden:
        await interaction.followup.send(
            "I found that reaction, but I do not have permission to read its users.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"Discord rejected the reaction-user lookup: `{e}`",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return

    usernames = sorted((username_for(user) for user in users), key=str.casefold)
    if not usernames:
        await interaction.followup.send(
            f"No users reacted with {requested_emoji} on message `{message_id}`.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return

    for page in format_reactor_pages(
        post_id=message_id,
        emoji=requested_emoji,
        usernames=usernames,
    ):
        await interaction.followup.send(
            page,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
