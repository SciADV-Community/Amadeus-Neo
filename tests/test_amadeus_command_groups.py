import asyncio

import discord

from cogs.activity_admin import setup as setup_activity_admin
from cogs.amadeus_admin import AmadeusAdmin
from cogs.boost_admin import setup as setup_boost_admin
from cogs.bouncer_admin import setup as setup_bouncer_admin
from cogs.honeypot_admin import setup as setup_honeypot_admin
from cogs.play import setup as setup_play
from cogs.play_admin import setup as setup_play_admin


class FakeTree:
    def __init__(self):
        self.commands = []
        self.removed = []

    def add_command(self, command, **kwargs):
        self.commands.append(command)

    def remove_command(self, command, **kwargs):
        self.removed.append((command, kwargs))
        for existing in list(self.commands):
            if existing.name == command:
                self.commands.remove(existing)
                return existing
        return None


class FakeBot:
    def __init__(self):
        self._cogs = {}
        self.tree = FakeTree()
        self.views = []

    def get_cog(self, name):
        return self._cogs.get(name)

    async def add_cog(self, cog):
        self._cogs[cog.__cog_name__] = cog

    def add_view(self, view):
        self.views.append(view)


def make_bot():
    bot = FakeBot()
    admin = AmadeusAdmin(bot)
    bot._cogs[admin.__cog_name__] = admin
    return bot


def unload_all(bot):
    for cog in reversed(list(bot._cogs.values())):
        cog.cog_unload()


def command_names(group):
    return set(group._children)


def test_module_admin_commands_attach_under_amadeus(temp_db_path):
    bot = make_bot()

    try:
        asyncio.run(setup_activity_admin(bot))
        asyncio.run(setup_boost_admin(bot))
        asyncio.run(setup_bouncer_admin(bot))
        asyncio.run(setup_honeypot_admin(bot))
        asyncio.run(setup_play_admin(bot))

        amadeus = bot.get_cog("AmadeusAdmin").amadeus

        assert {"activity", "boost", "bouncer", "debug", "honeypot", "play"}.issubset(
            command_names(amadeus)
        )

        assert command_names(amadeus._children["debug"]) == {
            "ping",
            "reacts",
        }
        assert command_names(amadeus._children["activity"]) == {
            "status",
            "tier-add",
            "tier-remove",
            "tier-list",
            "role-swap",
            "channel-include",
            "channel-exclude",
            "channel-remove",
            "channel-list",
            "cooldown",
        }
        assert command_names(amadeus._children["boost"]) == {
            "start",
            "remove",
            "status",
        }
        assert command_names(amadeus._children["bouncer"]) == {
            "set-role",
            "set-channel",
            "post-panel",
            "min-account-age-days",
            "max-failed-attempts",
            "captcha-expiration-minutes",
            "panel-image",
            "verification-role-delay-seconds",
            "verify",
            "unverify",
            "verify-all",
            "backfill-status",
            "cancel-backfill",
        }
        assert command_names(amadeus._children["honeypot"]) == {
            "set-channel",
            "set-action",
            "enable-alerts",
            "message",
            "post",
        }
        assert command_names(amadeus._children["play"]) == {
            "set-forum",
            "remove-forum",
            "add-game",
            "set-order",
            "remove-game",
            "list-games",
            "archive-duration",
            "auto-archive",
            "config",
        }
    finally:
        unload_all(bot)


def test_public_roots_remain_member_facing_only(temp_db_path):
    bot = make_bot()

    try:
        asyncio.run(setup_activity_admin(bot))
        asyncio.run(setup_boost_admin(bot))
        asyncio.run(setup_bouncer_admin(bot))
        asyncio.run(setup_honeypot_admin(bot))
        asyncio.run(setup_play(bot))
        asyncio.run(setup_play_admin(bot))

        activity = bot.get_cog("ActivityAdmin")
        boost = bot.get_cog("BoostAdmin")
        bouncer = bot.get_cog("BounceAdmin")
        honeypot = bot.get_cog("HoneypotAdmin")
        play = bot.get_cog("Play")
        play_admin = bot.get_cog("PlayAdmin")

        assert [command.name for command in activity.__cog_app_commands__] == [
            "activity"
        ]
        assert command_names(activity.activity) == {"status", "leaderboard"}

        assert [command.name for command in boost.__cog_app_commands__] == ["boost"]
        assert command_names(boost.boost) == {"status"}

        assert bouncer.__cog_app_commands__ == []
        assert honeypot.__cog_app_commands__ == []

        assert [command.name for command in play.__cog_app_commands__] == ["play"]
        assert command_names(play.play) == {"new", "delete", "end", "unlock"}
        assert {
            command.name
            for command in bot.tree.commands
            if command.type is discord.AppCommandType.message
        } == {
            "Delete Message",
            "Pin Message",
            "Unpin",
        }
        assert play_admin.__cog_app_commands__ == []
    finally:
        unload_all(bot)
