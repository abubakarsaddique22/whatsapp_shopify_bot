# Runbook

Kya karna hai jab kuch ghalat ho. Har phase apna hissa yahan jodta hai.

## Shopify sync (Phase 10)

Teen cheezein chalni chahiye: **server** (webhook leta hai), **sync worker** (`uv run python -m app.sync.worker`,
event par product dobara fetch karta hai) aur **reconcile** (`--mode delta` har kuch minute, `--mode full` raat ko,
`--mode policy` har ghante; development mein `--loop`). Webhook sirf batata hai ke kuch badla, mirror hamesha Shopify se
dobara fetch kar ke update hota hai.

Pehla qadam hamesha: `uv run python -m app.sync.monitor` (alarm ho to exit code 1, har alarm log mein `sync_alarm` bhi likhta hai).

| Alarm | Matlab | Kya karein |
|-------|--------|------------|
| `dead_letter` | Koi event 5 koshishon ke baad bhi fail hua (status `failed`) | `--list-failed` se wajah dekhein (zyada tar Shopify down ya token). Wajah theek karein, phir `--retry-failed` (ya `--retry-failed --event-id N`). Retry safe hai: handler Shopify se taaza data lata hai |
| `backlog` | Sab se purana pending event 10 minute se zyada purana hai | Sync worker band hai ya atka hua hai. `uv run python -m app.sync.worker` dobara chalayein |
| `run_failed` | Kisi job (delta / full / policy) ki aakhri run `failed` | `sync_runs` ka `error` dekhein (Shopify down? token?). Wajah theek karein, job haath se chalayein: `uv run python -m app.jobs.reconcile --mode <kind>`. Delta apna watermark sirf kamyab run se leta hai, is liye kuch skip nahi hota |
| `run_stuck` | Run 30 minute se zyada `running` hai (process mar gaya) | Job dobara chalayein; nayi run purani `running` row ko nazarandaz kar deti hai |
| `run_stale` | Job ki aakhri kamyab run apne interval se bohat purani hai | Scheduler ya `--loop` band hai. Chalayein |
| `no_good_run` | Job ab tak kabhi kamyab nahi hui | Haath se ek baar chalayein |

### Webhook nahi aa rahe
1. `uv run python scripts\register_webhooks.py --list`: 4 topics (products create / update / delete, inventory_levels/update) aur sahi URL dikhna chahiye.
2. Tunnel ka URL badla ho to `.env` mein `SHOPIFY_WEBHOOK_BASE_URL` theek karein aur `uv run python scripts\register_webhooks.py` dobara chalayein (duplicate nahi banta).
3. ngrok inspector http://127.0.0.1:4040: request nazar na aaye = Shopify ne bheji hi nahi; 401 = `SHOPIFY_CLIENT_SECRET` ghalat; 200 `ignored` = `SHOPIFY_SHOP_DOMAIN` shop ke header se nahi milta.
4. Jo webhook gum ho gaye unki bharpai `--mode delta` (edit hue products) aur `--mode full` (delete hue products, jo delta ko nazar nahi aate) karti hain.

### Product Shopify mein badla magar bot purana jawab de raha hai
- Price aur stock bot hamesha live Shopify se leta hai; mirror sirf title, options, variants, images aur "available for sale" rakhta hai.
- `uv run python -m app.jobs.reconcile --mode delta` chalayein, phir `uv run python -m app.sync.monitor`.
- Product hide hua ho (draft, archived, online store par nahi) to bot use nahi dekhta: Shopify mein usay Active aur Online Store par rakhein.

### Policy ya page badla magar bot purana jawab de raha hai
Policies aur pages ka Shopify webhook nahi hota. `uv run python -m app.jobs.reconcile --mode policy` (ya `uv run python -m app.cli rag-ingest`). Sirf badla hua content dobara embed hota hai.

### Aam sawal
- **Event kitna purana rakha jata hai?** `jobs/retention.py` (Phase 11): kamyab events `RETENTION_SYNC_DAYS` (14) ke baad, dead letter kam az kam 90 din.
- **Delta ko delete nazar kyun nahi aata?** Shopify delete hua product list hi nahi karta. `products/delete` webhook usay turant hide kar deta hai, warna raat ka `--mode full`.
- **Shopify ne khali list di to?** Full pass kuch hide nahi karta (mirror wipe nahi hota); report mein `skipped_hiding: True` aata hai.

## Security, privacy aur handoff (Phase 11)

Pehla qadam hamesha: `uv run python -m app.sync.monitor` (sync) aur admin API ka `GET /admin/status` (kill switch aur khule handoffs). Admin API sirf tab hai jab `.env` mein `ADMIN_ENDPOINTS_ENABLED=true` ho; har call mein header `X-Admin-Key`.

| Surat | Kya karein |
|-------|------------|
| Bot bahki ho gaya ya galat jawab de raha hai | **Kill switch on**: `POST /admin/kill-switch {"on": true}` (ya `.env` mein `KILL_SWITCH=true` aur worker restart). Worker har naye message aur har send se pehle dekhta hai. Is dauran aaye messages `ignored` ho jate hain aur baad mein jawab **nahi** paate: customers ko dobara likhna hoga. Wajah theek hone par `{"on": false}` (`.env` wala setting API se off nahi hota) |
| Customer ko insaan chahiye / handoff khula hai | `GET /admin/handoffs` ya Slack/email ki notification (customer ke number ke sirf aakhri 4 digits). Chat WhatsApp Business app mein dhoondein, jawab dein. Phir `POST /admin/conversations/{id}/resume`: bot dobara bolta hai aur handoff band. Resume na karein to pause `HANDOFF_PAUSE_HOURS` (12) baad khud khatam |
| Handoff ki notification nahi pohanchi | `handoffs.notify_error` dekhein (`select id, reason, notify_error from handoffs where status='open' and notified_at is null;`). Channel theek karein, phir `uv run python -m app.handoff.notifier` (dobara bhejta hai). Channel ka test: `... --test`. Handoff khud kabhi gum nahi hota |
| Ek customer ko kuch der ke liye chup karana | `POST /admin/conversations/{id}/pause {"hours": 2}` |
| Customer kehta hai "bot jawab nahi deta" | Teen wajahein: rate limit ya cool-down (`select kind, created_at from security_events where kind in ('rate_limited','cooldown_notice','injection_suspected') order by id desc limit 10;`, window guzarne par khud theek), pause (handoff khula hai), ya budget handoff-only. Pehle `GET /admin/status`, phir ye dekhein |
| `budget_alarm` / `budget_reached` ka alert | 24 ghante ka andaza `DAILY_COST_BUDGET` ke 80% / 100% par. 100% par bot sirf handoff deta hai. Asal kharcha dekhein; sahi ho to `DAILY_COST_BUDGET` ya `COST_PER_*_MESSAGE` badlein (worker restart), galat ho (spam) to rate limits kam karein. 24 ghante baad bot khud wapas aata hai |
| `document_flagged` (policy ya page mein hidayat nazar aayi) | Wo document index nahi hua, purana acha version chal raha hai. `select ref, detail from security_events where kind='document_flagged' order by id desc;` (patterns ke naam, text nahi). Shopify mein wo page / policy dekhein, jumla hatayein, phir `uv run python -m app.jobs.reconcile --mode policy` |
| "Mera data delete karo" | `uv run python scripts\delete_customer.py --wa-id <number>` (bina `--yes` sirf ginti), phir `--yes`. LangSmith trace aur customer ke phone / Meta ki copy yahan se nahi mitti |
| Purana data saaf karna | `uv run python -m app.jobs.retention --dry-run`, phir bina `--dry-run`. Roz scheduler se chalayein |
| Secret leak ho gaya (token, key, Slack URL) | Kill switch on, secret ko provider ke dashboard par badlein (WhatsApp token, Shopify client secret, LLM key, SMTP password, Slack URL), `.env` update, worker aur server restart, kill switch off. `git log -p -- .env` se dekhein ke Git mein to nahi gaya |

### Hamlon ka pata kaise chalta hai
`security_events` table (sirf sender ka hash aur chhote facts, kabhi message text ya number nahi): `injection_suspected` (har pakda hua hamla), `rate_limited`, `cooldown_notice`, `budget_alarm`, `budget_reached`, `budget_notice`, `document_flagged`, `admin_action`, `customer_erased`. Ek customer ki 3 `injection_suspected` ek ghante mein = cool-down.

### Security tests
`uv run pytest tests\adversarial` (Postgres chahiye). Fail ho to test ka naam batata hai kaun si hifazat tooti. Go-live se pehle `docs/security_checklist.md`.
