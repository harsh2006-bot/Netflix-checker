# NETFX-TG-BOT-COOKIE-CHK

## Docker-first hosting (recommended)

This project is now configured to run the same way on any host using Docker.

### 1) Configure environment

```bash
cd /path/to/project
cp .env.example .env
```

Fill `.env`:
- `BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `NFTOKEN_KEY`
- Optional: `NFTOKEN_KEY_POOL` (comma-separated keys)
- Optional: `ADMIN_ID` (primary admin for legacy single-id flows)
- Optional: `ADMIN_IDS` (comma-separated admin IDs, e.g. `YOUR_ADMIN_ID_1,YOUR_ADMIN_ID_2`)

### 2) Run with auto-restart + persistent data

```bash
docker compose up -d --build
```

What this gives you:
- `restart: always` (auto-start after crash/reboot)
- persistent local state in `./data` mounted to `/data` in container
- health endpoint on `GET /health`

### 3) Health + logs

```bash
curl http://localhost:8080/health
docker compose logs -f
```

TV login failures are logged with `[TV]` messages for easier monitoring.
