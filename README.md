# Delta Chat WebPreview Bot

Delta Chat bot designed to save web pages as complete, single self-contained HTML files (including images, CSS, fonts, and assets) using `monolith` and send them back to the chat as attachments.

## Features

- 📄 **Safe Static Previews (`/preview <url>`):** Compile complete pages into a single lightweight, secure, and fast HTML file with JavaScript execution disabled (recommended).
- ⚡ **JS-Hydrated Previews (`/previewjs <url>`):** Save complete pages with JavaScript execution enabled to preserve dynamic page hydration.
- 💬 **Quote Reply Parsing:** Reply with `/preview` or `/previewjs` (without a URL) to any message containing links, and the bot will automatically extract and capture the first link in the quoted text.
- ⏱️ **Rate Limiting:** Protects against abuse by rate-limiting regular users (15-second debounce) while allowing admins unlimited generations.
- 🔄 **Failover Transports (Relays):** Supports multiple mail servers. The bot automatically switches to backup transports if the primary server fails to send messages.
- 🛡️ **Secure Administration:** Claim ownership with `/initadmin`. Admins bypass rate limits and have exclusive control over relays and statistics.
- 🧹 **Automatic Cache Rotation:** Keeps disk usage low by automatically purging compiled HTML cache previews older than 24 hours.
- 🐳 **Docker Ready:** Built with a multi-stage Docker build compiling Rust-based `monolith` and packing it into a slim Python runtime.

## Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/mrgluek/deltachat_webpreview
   cd deltachat_webpreview
   ```

2. **Initialize Account:**
   Run the initialization command once to set up the bot's email and password:

   ```bash
   docker compose run --rm webpreview_bot python bot.py init bot-email@example.com your_password
   ```

3. **Start the Bot:**

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

   *Note: If it's a new account, a QR code will be printed to the logs for linking your Delta Chat device.*

4. **Claim Admin Ownership:**
   Send `/initadmin` to the bot in a private message to become the administrator.

## Commands

- `/preview <url>` — Save web page preview as a safe static HTML attachment (no JS).
- `/previewjs <url>` — Save web page preview with JavaScript execution enabled.
- `/stats` — Show generation counters, total traffic size, and disk space (disk space is admin-only).
- `/source` — Show primary and backup source code links 🔌.
- `/donate` — Support project development ❤️.
- `/help` — Show available commands and greeting info.
- `/initadmin` — Claim administrative ownership (private chat only).
- `/transports` — Show configured mail relays & stats (Admin only).
- `/addtransport` — Add a backup mail relay (Admin only).
- `/rmtransport <addr>` — Remove a mail relay (Admin only).
- `/setprimary <addr>` — Switch the primary mail relay (Admin only).

## Admin Management

Admin functions can be performed directly through chat commands, or managed via the server CLI:

### Set Administrator

```bash
docker compose exec webpreview_bot python set_admin.py --email your@email.com
```

### Transport (Mail Relay) CLI Initialization

Although we recommend using `/addtransport` in chat, you can also add a backup relay via the command line:

1. Stop the bot: `docker compose stop webpreview_bot`
2. Add relay: `docker compose run --rm webpreview_bot python bot.py init transport backup-email@example.com password`
3. Start the bot: `docker compose up -d`

## Support & Development

If you find this bot useful, consider supporting its development:

- **Git Repository:** [mrgluek/deltachat_webpreview](https://github.com/mrgluek/deltachat_webpreview)
- **Forgejo Mirror:** [gluek/deltachat_webpreview](https://git.gluek.info/gluek/deltachat_webpreview)
- **Donations:** Use the `/donate` command in Delta Chat.
