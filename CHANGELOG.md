# Changelog

---
### v1.4.0
- Added public `/activity status` and `/activity leaderboard` commands.
- Added `/activity tier role-swap` to keep only a member's highest earned activity role.

### v1.3.0
- Added optional honeypot message history cleanup windows for `/honeypot set-action`.
- The bouncer verification panel now includes configured Terms of Service and Privacy Policy links, plus a moderator assistance note.
- Added optional `AMADEUS_PRIVACY_POLICY_URL` and `AMADEUS_TERMS_OF_SERVICE_URL` startup sync for the bot application's bio.

### v1.2.1
- Reusing the bouncer Start Verification button now resends the active CAPTCHA image instead of only asking for the code.
- 100% test coverage for the bouncer verification panel.

### v1.2.0
- `/honeypot enable-alerts` will no longer warn you to set a channel if one is already set.
- Honeypot moderation actions no longer apply to members with the configured Amadeus admin role.
- Honeypot bans now skip the configured `AMADEUS_OWNER_ID` user.

### v1.1.1
- Added additional unit tests for multiple modules.

### v1.1.0
- Added support for `reason` in `/honeypot set-action` when using `mute`, `kick`, or `ban`

### v1.0.1
- Resolved honeypot race condition
