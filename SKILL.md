---
name: geo
description: GeoLook technical audit and model sampling toolkit. For recurring owned-site GEO work, use the owning mini-geo skill for scheduling, evidence, approval, and effect review. Toolkit scores and generated assets are hypotheses, not ranking guarantees.
---

# GeoLook local runtime

The installed code is `~/.local/share/geolook`. Project state is `work/<slug>`.
Recurring owned-site work follows `~/.agents/skills/mini-geo/SKILL.md` first.

All model calls use `scripts/replicate_gateway.py` through `sample.ask`.
`replicate.json` pins model IDs and the shared budget. The launcher supplies
`REPLICATE_API_TOKEN` from `~/.env.d/00-base.zsh` and `public.zsh`; no project `.env` is read and the
UI's credential/settings write endpoint is disabled. Model changes need an explicit
reviewed config and a new measurement baseline.

`crawl`, `audit`, and `report` produce technical evidence. `sample` consumes query
budget; only an approved manual run or activated owning workflow may invoke it.
`bootstrap`, `expand`, and `generate` share the same gateway and budget, but their
use is not implied by a monitoring run. A content or schema score alone cannot
justify deploying generated assets. Confirm actual page purpose and source facts.

API outputs are model API evidence, distinct from ChatGPT, Google, or Perplexity
consumer web answers. Sample identity includes question-set, method, model, mode,
cycle, and round. Never merge mixed-model history into a platform-level result.
Interrupted requests require reconciliation before more calls. Terminal unbilled predictions retain conservative budget reservations.

Local UI stays loopback-only behind the existing Cloudflare Access setup. Starting
or restarting live services, changing account settings, publishing or deploying
requires the appropriate owner approval. Files prepared in a staged release are
not live until the operator activates them.
