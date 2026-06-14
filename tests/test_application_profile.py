from amadeus.application_profile import build_application_description


def test_build_application_description_returns_current_description_when_urls_are_unset():
    assert (
        build_application_description(
            "Existing bio",
            privacy_policy_url="",
            terms_of_service_url="",
        )
        == "Existing bio"
    )


def test_build_application_description_appends_privacy_and_terms_links():
    description = build_application_description(
        "Amadeus Neo",
        privacy_policy_url="https://example.com/privacy",
        terms_of_service_url="https://example.com/terms",
    )

    assert description == (
        "Amadeus Neo\n"
        "\n"
        "Privacy Policy: https://example.com/privacy\n"
        "Terms of Service: https://example.com/terms"
    )


def test_build_application_description_replaces_existing_privacy_and_terms_lines():
    description = build_application_description(
        (
            "Amadeus Neo\n"
            "\n"
            "Privacy Policy: https://old.example/privacy\n"
            "Terms of Service: https://old.example/terms"
        ),
        privacy_policy_url="https://new.example/privacy",
        terms_of_service_url="https://new.example/terms",
    )

    assert description == (
        "Amadeus Neo\n"
        "\n"
        "Privacy Policy: https://new.example/privacy\n"
        "Terms of Service: https://new.example/terms"
    )


def test_build_application_description_supports_only_one_configured_link():
    assert (
        build_application_description(
            None,
            privacy_policy_url="https://example.com/privacy",
            terms_of_service_url="",
        )
        == "Privacy Policy: https://example.com/privacy"
    )
