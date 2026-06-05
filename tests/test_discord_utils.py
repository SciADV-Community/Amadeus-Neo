from types import SimpleNamespace

import pytest

from amadeus.discord_utils import (
    EMOJI_IMAGE_EXTENSIONS,
    PANEL_IMAGE_EXTENSIONS,
    ROLE_ICON_EXTENSIONS,
    escape_untrusted_text,
    image_extension_from_url,
    is_discord_media_url,
    validate_discord_attachment_image,
    validate_discord_image_url,
    validate_safe_role_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "@everyone",
        "@here",
        "<@1234567890>",
        "<@!1234567890>",
        "<@&1234567890>",
        "<#1234567890>",
        "Admin" + chr(0x202E),
        "Name" + chr(0),
    ],
)
def test_validate_safe_role_name_rejects_mentions_and_control_chars(name):
    assert validate_safe_role_name(name) is not None


@pytest.mark.parametrize(
    "name",
    [
        "Favorite Character",
        "Science Adventure",
        "role_name-123",
    ],
)
def test_validate_safe_role_name_accepts_normal_names(name):
    assert validate_safe_role_name(name) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.discordapp.com/attachments/1/2/image.png",
        "https://media.discordapp.net/attachments/1/2/image.webp?ex=123",
        "https://images-ext-1.discordapp.net/external/hash/image.png",
    ],
)
def test_is_discord_media_url_accepts_discord_media_hosts(url):
    assert is_discord_media_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/1/2/image.png",
        "https://discord.com/channels/1/2/3",
        "https://example.com/image.png",
        "https://cdn.discordapp.com.evil.example/image.png",
    ],
)
def test_is_discord_media_url_rejects_non_media_urls(url):
    assert not is_discord_media_url(url)


def test_image_extension_from_url_ignores_query_string():
    assert image_extension_from_url(
        "https://cdn.discordapp.com/attachments/1/2/image.PNG?ex=123"
    ) == ".png"


def test_validate_discord_image_url_enforces_host_and_extension():
    valid = "https://cdn.discordapp.com/attachments/1/2/role.webp"
    external = "https://example.com/role.webp"
    wrong_type = "https://cdn.discordapp.com/attachments/1/2/role.gif"

    assert validate_discord_image_url(valid, ROLE_ICON_EXTENSIONS) is None
    assert validate_discord_image_url(external, ROLE_ICON_EXTENSIONS) is not None
    assert validate_discord_image_url(wrong_type, ROLE_ICON_EXTENSIONS) is not None


def test_validate_discord_attachment_image_requires_image_content_type():
    attachment = SimpleNamespace(
        content_type="text/plain",
        url="https://cdn.discordapp.com/attachments/1/2/panel.png",
    )

    assert validate_discord_attachment_image(attachment, PANEL_IMAGE_EXTENSIONS) is not None


def test_validate_discord_attachment_image_accepts_discord_uploaded_image():
    attachment = SimpleNamespace(
        content_type="image/gif",
        url="https://cdn.discordapp.com/attachments/1/2/emoji.gif",
    )

    assert validate_discord_attachment_image(attachment, EMOJI_IMAGE_EXTENSIONS) is None


def test_escape_untrusted_text_escapes_mentions_and_markdown():
    escaped = escape_untrusted_text("**@everyone**", max_length=20)

    assert "@everyone" not in escaped
    assert "\\*" in escaped
