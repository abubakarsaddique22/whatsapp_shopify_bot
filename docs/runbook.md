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
- **Event kitna purana rakha jata hai?** Abhi purane `sync_events` ki safai nahi hoti (retention Phase 11 mein).
- **Delta ko delete nazar kyun nahi aata?** Shopify delete hua product list hi nahi karta. `products/delete` webhook usay turant hide kar deta hai, warna raat ka `--mode full`.
- **Shopify ne khali list di to?** Full pass kuch hide nahi karta (mirror wipe nahi hota); report mein `skipped_hiding: True` aata hai.
