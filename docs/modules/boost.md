# Module: boost

Automates server boost perks. When a member boosts, the bot DMs them a private setup dashboard. Each reward has a button that opens a Discord modal for its text and file inputs, plus **Submit for Review** and **Clear** buttons. Every perk is optional: a member can request any subset of the perks their tier unlocks, or press **Submit for Review** whenever they are done. Unselected perks are not included in the request. Pressing **Submit for Review** sends the request to the admin channel and replies with a private confirmation. The request is reviewed and approved before anything is applied.

**Tier 1 (1 boost):** custom role with icon and/or 1 suggested emoji  
**Tier 2 (2 boosts):** custom role with icon and hex color and/or up to 2 suggested emojis

## Enable / Disable

```
/amadeus module enable boost
/amadeus module disable boost
```

## Prerequisites

- `/amadeus set-admin-channel` must be configured — approval requests are posted there. The bot needs **View Channel**, **Send Messages**, **Embed Links**, and **Attach Files** in that channel.
- `/amadeus set-admin-role` must be configured — only this role can approve or deny requests.
- The bot needs **Manage Roles** and **Manage Emojis and Stickers** permissions.
- Role icons require the server to be at **Guild Boost Level 2**. This only matters if the member requests a role.
- The bot role must sit above any custom boost roles it creates or assigns.

## How It Works

1. Member boosts → bot DMs them the rewards they unlocked, a setup button for each item, and **Submit for Review** / **Clear** buttons.
   - **1 boost:** **Custom Role**, **Emoji 1**, then **Submit for Review** and **Clear**.
   - **2 boosts:** **Custom Role**, **Emoji 1**, **Emoji 2**, then **Submit for Review** and **Clear**.
2. Each perk button opens a modal:
   - The role modal collects the name and icon, plus a six-digit hex color for two-boost members.
   - Each emoji modal collects its name and image in one form.
3. Saving a modal silently changes its button to a green check mark; no extra success message is sent. **Clear** wipes every choice so the member can start over. Perks are optional, so pressing **Submit for Review** with anything left blank just skips those perks.
4. Pressing **Submit for Review** sends the request to the admin channel and replies with a private confirmation. There's no second review screen.
5. Only the requested perks are shown to admins. The approval embed appears in the admin channel with a **Requested Perks** field and full-width, vertically stacked image previews for the requested items, plus **Approve** / **Deny** buttons. Perks the member skipped are not displayed.
6. Admin clicks a button → optional comment modal → decision is processed. The image previews, attachments, and controls are removed only after the decision succeeds, leaving the request summary and decision record. Denied records also show the supplied reason, or **No reason provided** when the comment was left blank. The same reason is included in the member's denial DM.
   - **Approved:** only the requested resources are created, such as the role, icon, color, emojis, and role assignment.
   - **Denied:** member is DM'd and the request is parked until they type `boost` in DM or use `/boost status` in the server to restart later.
7. If the member stops boosting, any custom role and emojis they received are removed automatically. Failed cleanup remains recorded and is retried instead of being forgotten.

DM sessions expire after **48 hours** of inactivity. During an active DM session, members can type `restart` to clear the forms or `cancel` to stop the setup. Members can use `/boost status` to receive a fresh setup dashboard, restart, or check their current state at any time. Setup buttons remain usable after a bot restart as long as the underlying session is still active.

If a member sends `boost` to the bot in DM, the bot checks for an eligible existing boost request and restarts it. If there is no eligible request but the member is currently boosting, the bot starts a new self-service request.

Admin-forced requests are managed by admins. The member can't resume or restart one with `/boost status` or the DM keywords, and the leftover flow is cleared, so an admin has to run `/amadeus boost start <member> force: True` again. A forced request that's already submitted and waiting on a decision is left alone. The member can still `cancel` a forced setup they don't want.

## Collection Rules

| Item | Rules |
|---|---|
| Role name | 32 characters max; cannot contain `@everyone`, `@here`, Discord mention syntax, control characters, or bidirectional formatting characters |
| Role icon | Modal file upload; PNG, JPEG, or WebP; maximum 256 KB; square 64×64 px images are recommended |
| Role color | Two boosts only; exactly six hexadecimal characters such as `FF5733` or `A3C2FF` |
| Emoji name | 2–32 characters; letters, numbers, and underscores only |
| Emoji image | Discord-uploaded image only; PNG, JPEG, GIF, or WebP; max 256 KB; 128×128 px images are recommended |

Discord-uploaded image URLs are limited to Discord media hosts. External image URLs are rejected.

## Capacity Handling

Perks are optional, so capacity is checked against the perks a member actually requested rather than everything their tier unlocks. When an approval is submitted, the bot checks there's room for the requested role (role limit), a free slot for each requested emoji, and Boost Level 2 if a role icon was requested. An emoji-only request is never blocked by the role limit or the role-icon requirement. If a requirement fails, the request stays pending and the configured admin channel gets an alert so staff can free capacity and retry.

## Member Commands

| Command | Description |
|---|---|
| `/boost status` | Check perk status, resume a mid-flow request, or restart an eligible denied request |

## Admin Commands

| Command | Description |
|---|---|
| `/amadeus boost start <member> [force] [count]` | Manually start the perks flow; `force: True` skips the active-booster check, and `count` can be `1` or `2` to select the perk tier |
| `/amadeus boost remove <member>` | Remove a member's perks (role + emojis) and clear their flow |
| `/amadeus boost status <member>` | Inspect any member's flow state and active grant |

## Database

| Table | Stores |
|---|---|
| `boost_grant` | Approved grants per member — tier, custom role ID, emoji IDs |
| `boost_meta` | Per-guild subscription count cache used to infer boost tier on member update |
| `boost_cleanup` | Old or partially-created Discord resources waiting for automatic cleanup retry |
| `dm_flow_state` | Active DM conversation state (shared with other flow-based modules) — step, JSON data |

## Troubleshooting

**No DM was sent when a member boosted**
The bot may have been offline at the time. Use `/amadeus boost start <member>` to trigger the flow manually.

**Approval buttons don't respond after a restart**
The module may not be loaded. Confirm `cogs.boost` is in `AMADEUS_COGS` and re-enable the module. The `setup()` function re-registers the persistent button handlers on load.

**Role was created but icon wasn't applied**
The approval is treated as failed and any newly-created resources are rolled back. Check that the server is at Guild Boost Level 2 and that the bot can manage the role, then retry the still-pending approval.

**"Insufficient emoji slots" at approval time**
Free up emoji slots in Server Settings, then retry the still-pending approval. The approval controls remain available after a failed attempt.

**Tier was inferred incorrectly**
The tier is estimated from the guild subscription count delta at the moment of the boost event. Simultaneous boosts from multiple members can cause the inference to be off. Restart the flow with `/amadeus boost start <member> count: 1` or `/amadeus boost start <member> count: 2` to select the correct tier.

**Member says they were denied but wants to try again**
They can type `boost` in DM with the bot or run `/boost status` in the server. The flow restarts from step 1.

**Image upload is rejected**
Use the file upload inside the corresponding setup modal. Role icons accept PNG, JPEG, or WebP and must be no larger than 256 KB. Emoji images accept PNG, JPEG, GIF, or WebP and must be no larger than 256 KB.
