import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.play_admin as play_admin_module
from cogs.play_admin import PlayAdmin, archive_duration_label


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()


class FakeStore:
    def __init__(self):
        self.config = SimpleNamespace(forum_channel_id=50, auto_archive_duration=1440)
        self.games = {}
        self.calls = []

    def search_games(self, guild_id, current):
        return [game for game in self.games.values() if current.lower() in game.display_name.lower()]

    def get_config(self, guild_id):
        return self.config

    def set_forum_channel(self, guild_id, channel_id):
        self.calls.append(("forum", guild_id, channel_id))
        self.config.forum_channel_id = channel_id

    def save_game(self, guild_id, name, forum_id, tag_id):
        game = SimpleNamespace(
            key=name.lower(),
            display_name=name,
            forum_channel_id=forum_id,
            forum_tag_id=tag_id,
        )
        self.games[game.key] = game
        return game

    def get_game(self, guild_id, key, enabled_only=True):
        return self.games.get(key)

    def remove_game(self, guild_id, key):
        self.games.pop(key, None)

    def list_games(self, guild_id):
        return list(self.games.values())

    def set_auto_archive_duration(self, guild_id, minutes):
        self.calls.append(("archive", guild_id, minutes))
        self.config.auto_archive_duration = minutes


def install_access(monkeypatch):
    monkeypatch.setattr(
        play_admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=SimpleNamespace()),
    )


def make_cog(store=None, *, enabled=True):
    cog = PlayAdmin.__new__(PlayAdmin)
    cog.play_store = store or FakeStore()
    cog.module_store = SimpleNamespace(is_module_enabled=lambda guild_id, module: enabled)
    cog.bot = SimpleNamespace()
    return cog


def make_interaction(guild):
    return SimpleNamespace(
        guild=guild,
        guild_id=guild.id if guild else None,
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
    )


def make_forum(*, tags=None, permissions=None):
    tags = tags or []
    by_id = {tag.id: tag for tag in tags}
    return SimpleNamespace(
        id=50,
        mention="#play",
        available_tags=tags,
        get_tag=lambda tag_id: by_id.get(tag_id),
        create_tag=AsyncMock(return_value=SimpleNamespace(id=99, name="New Tag")),
        permissions_for=lambda member: permissions or SimpleNamespace(
            view_channel=True,
            send_messages=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            manage_threads=True,
            manage_channels=True,
        ),
    )


def test_archive_duration_label_and_game_autocomplete():
    assert archive_duration_label(None) == "Forum default"
    assert archive_duration_label(60) == "1 hour"
    assert archive_duration_label(15) == "15 minutes"

    store = FakeStore()
    store.save_game(1, "Steins;Gate", 50, 10)
    store.save_game(1, "Chaos;Head", 50, 11)
    cog = make_cog(store)
    choices = asyncio.run(cog.game_autocomplete(SimpleNamespace(guild_id=1), "gate"))
    assert [(choice.name, choice.value) for choice in choices] == [("Steins;Gate", "steins;gate")]
    assert asyncio.run(cog.game_autocomplete(SimpleNamespace(guild_id=None), "")) == []


def test_play_set_forum_warns_about_missing_permissions(monkeypatch):
    install_access(monkeypatch)
    store = FakeStore()
    cog = make_cog(store)
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99))
    forum = make_forum(
        permissions=SimpleNamespace(
            view_channel=True,
            send_messages=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            manage_threads=False,
            manage_channels=True,
        )
    )
    interaction = make_interaction(guild)

    asyncio.run(PlayAdmin.play_set_forum.callback(cog, interaction, forum))

    assert store.calls[-1] == ("forum", 1, 50)
    assert "Missing permissions" in interaction.response.send_message.await_args.args[0]


def test_play_add_game_validates_inputs_and_creates_missing_tag(monkeypatch):
    install_access(monkeypatch)
    store = FakeStore()
    cog = make_cog(store)
    guild = SimpleNamespace(id=1)
    forum = make_forum()

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_add_game.callback(cog, interaction, " ", forum, None))
    assert interaction.response.send_message.await_args.args[0] == "Game name cannot be empty."

    store.config = None
    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_add_game.callback(cog, interaction, "Steins;Gate", None, None))
    assert "Choose a **forum**" in interaction.response.send_message.await_args.args[0]

    store.config = SimpleNamespace(forum_channel_id=None, auto_archive_duration=None)
    cog._get_default_forum = AsyncMock(return_value=forum)
    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_add_game.callback(cog, interaction, "Steins;Gate", None, "x" * 21))
    assert "Forum tag name must be" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_add_game.callback(cog, interaction, "Steins;Gate", forum, "New Tag"))
    forum.create_tag.assert_awaited_once()
    assert store.games["steins;gate"].forum_tag_id == 99
    assert "created and linked" in interaction.response.send_message.await_args.args[0]


def test_play_remove_list_archive_and_config(monkeypatch):
    install_access(monkeypatch)
    store = FakeStore()
    game = store.save_game(1, "Steins;Gate", 50, 10)
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    forum = make_forum(tags=[tag])
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99))
    cog = make_cog(store)
    cog._get_forum_channel = AsyncMock(return_value=forum)

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_list_games.callback(cog, interaction))
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "Steins;Gate" in embed.description
    assert "Tag: **Steins;Gate**" in embed.description

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_archive_duration.callback(cog, interaction, 123))
    assert "Auto-archive duration must be" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_archive_duration.callback(cog, interaction, 0))
    assert store.calls[-1] == ("archive", 1, None)

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_config.callback(cog, interaction))
    config_embed = interaction.response.send_message.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in config_embed.fields}
    assert fields["Games"] == "1"
    assert fields["Forum permissions"] == "#play: OK"

    interaction = make_interaction(guild)
    asyncio.run(PlayAdmin.play_remove_game.callback(cog, interaction, game.key))
    assert store.games == {}
    assert "Removed **Steins;Gate**" in interaction.response.send_message.await_args.args[0]
