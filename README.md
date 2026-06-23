# Delta Chat WebPreview Bot

Delta Chat bot designed to save web pages as complete, single self-contained HTML files (including images, CSS, fonts, and assets) using `monolith` and send them back to the chat as attachments.

## Features

- 📄 **Compressed Reader Mode (`/preview <url>`):** Compile webpages into highly compressed, clutter-free reader views using Mozilla's Readability. All images are automatically downloaded, optimized, resized, and inlined as Base64 (recommended default).
- ⚡ **Full Page Archiving (`/archive <url>`):** Save complete pages as full interactive archives with JavaScript enabled using `monolith`. Proactively compresses and optimizes heavy base64-encoded image payloads post-generation to keep files tiny.
- 💬 **Quote Reply Parsing:** Reply with `/preview` or `/archive` (without a URL) to any message containing links, and the bot will automatically extract and capture the first link in the quoted text.
- ⏱️ **Rate Limiting:** Protects against abuse by rate-limiting regular users (15-second debounce) while allowing admins unlimited generations.
- 🔄 **Automatic Transport Failover:** Supports multiple mail servers. The bot automatically detects message delivery failures via raw core events, switches `configured_addr` to a backup transport in round-robin fashion, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread (up to a maximum of 10 attempts per message) to prevent loop propagation and CPU spikes.
- 🛡️ **Secure Administration:** Claim ownership with `/initadmin`. Admins bypass rate limits and have exclusive control over relays and statistics.
- 🧹 **Automatic Cache Rotation:** Keeps disk usage low by automatically purging compiled HTML cache previews older than 24 hours.
- 💾 **Direct File Downloads:** Automatically detects URLs pointing to document files (PDF, EPUB, DjVu, MS Office, LibreOffice, plain text/data files). Instead of attempting an HTML preview, the bot offers a `/download` command in groups or directly downloads/attaches the file in private chats (up to 50 MB limits, with chunked streaming).
- 🤖 **Jina.ai Fallback Support:** Integrated Jina AI Reader (`r.jina.ai`) to resolve webpage titles, preview banners, and markdown-to-HTML text contents if the target site blocks standard user agents or readability parser fails to extract meaningful data (e.g. on anti-bot challenge pages). It automatically strips tracking pixels/broken images to preserve privacy and presentation.
- 📺 **Invidious & YT Bot Redirection:** Detects Invidious instances (alternative YouTube front-ends) by checking page description metadata. If `YT Bot` is present in the chat, the bot extracts the video ID and redirects it to a standard `youtu.be` link to be processed by `YT Bot`, completely bypassing WebPreview generation. It automatically learns detected instance domains and supports manual domain registration/management via admin commands.
- 🛡️ **Local Network Protection:** Uses standard Python `ipaddress` validation to identify and skip local hosts/IPs (`localhost`, private IP subnets, `.local`/`.lan` domains, etc.), blocking resource-wasting internal network requests and spam.
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

- `/preview <url>` — Save page in highly compressed reader-mode format (using Mozilla's Readability).
- `/archive <url>` — Save page as a full monolith-based dynamic archive (with JS enabled, optimized images). *(Note: `/previewjs` is also supported as an alias to `/archive`)*
- `/download <url>` — Download file directly and send as attachment (supported for PDF, office documents, text files).
- `/stats` — Show generation counters, total traffic size, and disk space (disk space is admin-only).
- `/source` — Show primary and backup source code links 🔌.
- `/donate` — Support project development ❤️.
- `/help` — Show available commands and greeting info.
- `/initadmin` — Claim administrative ownership (private chat only).
- `/transports` — Show configured mail relays & stats (Admin only).
- `/addtransport` — Add a backup mail relay (Admin only).
- `/rmtransport <addr>` — Remove a mail relay (Admin only).
- `/setprimary <addr>` — Switch the primary mail relay (Admin only).
- `/resilient` — Toggle resilient sending mode across all relays (Admin only).
- `/invidious_add <domain/url>` — Register a custom Invidious instance domain (Admin only).
- `/invidious_rm <domain/url>` — Deregister an Invidious instance domain (Admin only). *(Note: `/invidious_remove` is also supported as an alias)*
- `/invidious_list` — List registered Invidious instance domains (Admin only).


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
