# Module: boost

Automates server boost perks. When a member boosts, the bot DMs them to collect their preferences (custom role, role icon, emoji suggestions). The completed request is sent to the admin channel for review and approval before anything is applied.

**Tier 1 (1 boost):** custom role with icon + 1 suggested emoji  
**Tier 2 (2 boosts):** custom role with icon and hex color + 2 suggested emojis

## Enable / Disable

```
/amadeus module enable boost
/amadeus module disable boost
```

## Prerequisites

- `/amadeus set-admin-channel` must be configured — approval requests are posted there.
- `/amadeus set-admin-role` must be configured — only this role can approve or deny requests.
- The bot needs **Manage Roles** and **Manage Emojis and Stickers** permissions.
- Role icons require the server to be at **Guild Boost Level 2**. The image is saved and applied automatically once the server reaches that level.
- The bot role must sit above any custom boost roles it creates or assigns.

## How It Works

1. Member boosts → bot DMs them and walks through role/emoji preferences step by step.
2. Member reviews a summary and clicks **Submit for Review**.
3. Approval embed appears in the admin channel with image previews and **Approve** / **Deny** buttons.
4. Admin clicks a button → optional comment modal → decision is processed.
   - **Approved:** role created, icon and color applied, emojis uploaded, role assigned.
   - **Denied:** member is DM'd and the request is parked until they type `boost` in DM or use `/boost status` in the server to restart later.
5. If the member stops boosting, the custom role and emojis are removed automatically.

DM sessions expire after **48 hours** of inactivity. During an active DM session, members can type `restart` to return to step 1 or `cancel` to stop the setup. Members can use `/boost status` to resume, restart, or check their current state at any time.

If a member sends `boost` to the bot in DM, the bot checks for an eligible existing boost request and restarts it. If there is no eligible request but the member is currently boosting, the bot starts a new self-service request. Admin-forced requests are excluded from this DM self-service path.

## Collection Rules

| Item | Rules |
|---|---|
| Role name | 32 characters max; cannot contain `@everyone`, `@here`, Discord mention syntax, control characters, or bidirectional formatting characters |
| Role icon | Discord-uploaded image only; PNG, JPEG, or WebP; square 64×64 px images are recommended |
| Role color | Tier 2 only; hex color such as `#FF5733` or `A3C2FF` |
| Emoji name | 2–32 characters; letters, numbers, and underscores only |
| Emoji image | Discord-uploaded image only; PNG, JPEG, GIF, or WebP; max 256 KB; 128×128 px images are recommended |

Discord-uploaded image URLs are limited to Discord media hosts. External image URLs are rejected.

## Emoji Slot Handling

Before the emoji collection step, the bot checks available slots. If there aren't enough free slots, the emoji step is skipped and the member is notified. They can use `/boost status` later to restart the flow and add emojis once slots are available.

## Member Commands

| Command | Description |
|---|---|
| `/boost status` | Check perk status, resume a mid-flow request, or restart an eligible denied request |

## Admin Commands

| Command | Description |
|---|---|
| `/boost admin start <member> [force] [count]` | Manually start the perks flow; `force: True` skips the active-booster check, and `count` can be `1` or `2` to select the perk tier |
| `/boost admin remove <member>` | Remove a member's perks (role + emojis) and clear their flow |
| `/boost admin status <member>` | Inspect any member's flow state and active grant |

## Database

| Table | Stores |
|---|---|
| `boost_grant` | Approved grants per member — tier, custom role ID, emoji IDs |
| `boost_meta` | Per-guild subscription count cache used to infer boost tier on member update |
| `dm_flow_state` | Active DM conversation state (shared with other flow-based modules) — step, JSON data |

## Troubleshooting

**No DM was sent when a member boosted**
The bot may have been offline at the time. Use `/boost admin start <member>` to trigger the flow manually.

**Approval buttons don't respond after a restart**
The module may not be loaded. Confirm `cogs.boost` is in `AMADEUS_COGS` and re-enable the module. The `setup()` function re-registers the persistent button handlers on load.

**Role was created but icon wasn't applied**
The server is below Guild Boost Level 2. The icon is saved in the flow data and will be applied on next approval once the server reaches Level 2.

**"Insufficient emoji slots" at approval time**
Free up emoji slots in Server Settings, then use `/boost admin remove <member>` followed by `/boost admin start <member>` to redo the flow.

**Tier was inferred incorrectly**
The tier is estimated from the guild subscription count delta at the moment of the boost event. Simultaneous boosts from multiple members can cause the inference to be off. Restart the flow with `/boost admin start <member> count: 1` or `/boost admin start <member> count: 2` to select the correct tier.

**Member says they were denied but wants to try again**
They can type `boost` in DM with the bot or run `/boost status` in the server. The flow restarts from step 1.

**Image upload is rejected**
Upload the image directly to Discord rather than pasting an external URL. Role icons accept PNG, JPEG, or WebP. Emoji images accept PNG, JPEG, GIF, or WebP and must be no larger than 256 KB.
