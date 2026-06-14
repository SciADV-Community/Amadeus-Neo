from cogs.bouncer_admin import build_bouncer_panel_embed


def test_build_bouncer_panel_embed_includes_terms_privacy_and_assistance_text():
    embed = build_bouncer_panel_embed(
        min_account_age_days=14,
        panel_image_url=None,
        terms_of_service_url="https://example.com/terms",
        privacy_policy_url="https://example.com/privacy",
    )

    assert "- Your Discord account must be at least **14 days old**." in embed.description
    assert "- You must complete a private CAPTCHA." in embed.description
    assert "- You must agree to our [Terms of Service](https://example.com/terms)." in embed.description
    assert "**Privacy Policy:**" in embed.description
    assert "[Click here to see our Privacy Policy](https://example.com/privacy)" in embed.description
    assert embed.footer.text == (
        "Verification is private. Your CAPTCHA is only visible to you.\n"
        "If you require special assistance please contact a moderator."
    )


def test_build_bouncer_panel_embed_omits_unconfigured_terms_and_privacy_links():
    embed = build_bouncer_panel_embed(
        min_account_age_days=1,
        panel_image_url=None,
        terms_of_service_url="",
        privacy_policy_url="",
    )

    assert "Terms of Service" not in embed.description
    assert "Privacy Policy" not in embed.description


def test_build_bouncer_panel_embed_sets_optional_panel_image():
    embed = build_bouncer_panel_embed(
        min_account_age_days=14,
        panel_image_url="https://cdn.discordapp.com/attachments/1/2/panel.png",
        terms_of_service_url="",
        privacy_policy_url="",
    )

    assert embed.image.url == "https://cdn.discordapp.com/attachments/1/2/panel.png"
