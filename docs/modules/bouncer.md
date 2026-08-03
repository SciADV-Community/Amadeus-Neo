# Module: bouncer

CAPTCHA-based member verification. New members must solve a text CAPTCHA before receiving the verified role. Members who fail too many times are kicked.

## Enable / Disable

```
/amadeus module enable bouncer
/amadeus module disable bouncer
```

## Setup

1. `/amadeus bouncer set-role <role>` — the role granted on successful verification.
2. `/amadeus bouncer set-channel <channel>` — the channel members verify in. The bot applies a 1-hour slow-mode automatically if it has **Manage Channel**.
3. `/amadeus bouncer post-panel` — posts the public "Start Verification" button embed.

> The bot needs **Manage Roles** and its role must sit **above** the verified role in the server's role hierarchy.

> If `AMADEUS_TERMS_OF_SERVICE_URL` or `AMADEUS_PRIVACY_POLICY_URL` are configured, `/amadeus bouncer post-panel` includes those links in the public verification panel.

## Configurable Settings

All settings are per-server and persist across restarts.

| Command | Range | Default | Description |
|---|---|---|---|
| `/amadeus bouncer min-account-age-days` | 0–30 | 14 | Minimum Discord account age to begin verification |
| `/amadeus bouncer max-failed-attempts` | 1–5 | 3 | Failed attempts before the user is kicked |
| `/amadeus bouncer captcha-expiration-minutes` | 1–30 | 10 | Minutes before an unsolved CAPTCHA expires |
| `/amadeus bouncer verification-role-delay-seconds` | 0–10 | 5 | Delay between passing and receiving the role |
| `/amadeus bouncer panel-image [image]` | Discord image upload | None | Banner image on the verification panel embed; omit to clear. Accepted: PNG, JPEG, GIF, or WebP |

Panel images must be uploaded to Discord. External image URLs are rejected.

## Member Commands

| Command | Description |
|---|---|
| `/verify` | Start verification in the configured verification channel |
| `/code <code>` | Submit the CAPTCHA code shown in the user's private CAPTCHA image |

If a member dismisses the private CAPTCHA message before submitting the code, pressing **Start Verification** again or running `/verify` will resend the active CAPTCHA image instead of creating a new challenge.

## Admin Commands

| Command | Description |
|---|---|
| `/amadeus bouncer verify <member>` | Manually grant the verified role |
| `/amadeus bouncer unverify <member>` | Manually remove the verified role |
| `/amadeus bouncer verify-all [include_bots]` | Bulk-assign the verified role to all members |
| `/amadeus bouncer backfill-status` | Check bulk operation progress |
| `/amadeus bouncer cancel-backfill` | Stop a running bulk operation |

## Database

| Table | Stores |
|---|---|
| `bouncer_config` | Per-guild verified role, verification channel, and all configurable settings |

## Troubleshooting

**Panel button does nothing**
The bot or the bouncer module may have been reloaded. Re-post the panel with `/amadeus bouncer post-panel`.

**"I cannot assign that role"**
The verified role is above or equal to the bot's highest role. Move the bot role above it in Server Settings → Roles.

**Messages in the verification channel aren't being deleted**
Slow-mode is not set on the channel. Re-run `/amadeus bouncer set-channel` to restore it, or set it manually. The bot sends an alert to the admin channel when this happens.

**`post-panel` refuses to post**
Slow-mode is not active on the configured channel. The bot won't post the panel without it — see the slow-mode note above.
