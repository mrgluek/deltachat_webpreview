# Changelog

All notable changes to the Delta Chat WebPreview Bot will be documented in this file.

## [2.2.1] - 2026-06-16

### Fixed
- **Meta Tag Parsing Bug:** Corrected regular expression patterns in Open Graph metadata extraction to use non-greedy matching confined to a single tag (`[^>]*?` instead of `.*?`), preventing false matches across multiple tag boundaries (e.g. on Yandex Translate turbopages proxy URLs).

## [2.2.0] - 2026-06-14

### Added
- **Direct File Downloads:** Added automatic detection of document file URLs (PDF, EPUB, DjVu, MS Office, LibreOffice, and plain text/data files) via extension and response Content-Type headers. Instead of generating HTML previews, the bot now shows a `[ 💾 /download_urlhash ]` button in group chat preview cards.
- **Background Downloader:** Implemented a non-blocking downloader to fetch and cache files up to 50 MB in size using stream chunking to prevent memory overload, sending them directly as Delta Chat message attachments.
- **Safety Exclusions for Local Hosts/IPs:** Integrated local host/IP filtering utilizing the standard Python `ipaddress` library to exclude local hostnames (`localhost`, `example.com`), private IPv4/IPv6 ranges (`127.0.0.1`, `10.0.0.0/8`, link-local, etc.), and local/private domains (`.local`, `.lan`), preventing unnecessary outbound connections and chat spam.
- **Manual Download Command:** Added `/download <url>` manual command trigger.

## [2.1.0] - 2026-06-05

### Added
- **DPI Bypass Hack:** Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode:** Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [2.0.0] - 2026-05-31

### Added
- **Mozilla Readability Default Mode:** `/preview` now compiles pages using Mozilla's Readability algorithm into highly compressed, clean, and beautiful Reader Mode HTML documents.
- **Pure-Python Image Optimization Engine:** Automatically downloads, resizes (maximum width 800px), compresses and base64-inlines all images inside readability pages into optimized WebP (preserving transparency) or JPEG formats using Pillow, producing completely offline-readable and featherweight outputs.
- **Premium Reader Styling:** Readability documents are styled using a custom modern responsive layout that adapts seamlessly to desktop or mobile viewport dimensions and supports prefers-color-scheme light/dark modes automatically.
- **Full Interactive Archiving Command:** Added a new `/archive` command running monolith with JavaScript execution enabled.
- **Monolith Base64 Post-Compression:** Integrates BeautifulSoup and Pillow image post-processing to parse generated monolith archives and compress heavy inlined Base64 graphics, reducing monolith file sizes from dozens of megabytes down to 1-2 MB.
- **Backwards Compatibility Support:** Kept `/previewjs` as a silent alias routed directly to the new `/archive` command.


## [1.2.2] - 2026-05-22

### Fixed
- **Profile Status Description:** Restored the missing `/help` command tip in the bot's multiline profile status (`selfstatus`) description.

## [1.2.1] - 2026-05-22

### Fixed
- **Contact Fingerprint Parsing:** Fixed a parsing bug where PGP fingerprints containing newlines or extra whitespaces in `get_contact_encryption_info` failed to be extracted for contacts. This resolves admin verification failures when writing to the bot from secondary relay accounts using the same PGP keys.

## [1.2.0] - 2026-05-22

### Added
- **Content-Length Size Pre-Check:** Integrated a lightweight response header check before starting the heavy `monolith` page compilation. If the remote resource's declared size exceeds **10 MB**, the process is aborted immediately, preventing large downloads.
- **Content-Type Binary/Media Filters:** Added proactive detection for binary and media asset headers (e.g. `application/zip`, `video/mp4`, `audio/mp3`, `application/pdf`, etc.). The bot fast-rejects requests pointing to these media or non-HTML resources.
- **Compiled Output Size Limit:** Enforced a post-compilation safety limit checking the size of the compiled HTML. If it exceeds **50 MB**, the file is discarded to ensure reliability and prevent email delivery transport failures under the server's message limit.

## [1.1.0] - 2026-05-22

### Added
- Implemented Fast-Rejection Optimization for blocked websites. When a target website blocks the bot's lightweight fetches with hard HTTP status codes (such as HTTP 403 Forbidden, 401 Unauthorized, or 404 Not Found) on both standard and fallback User-Agents, the bot caches this failure in SQLite as a `__FAILED_BLOCK__` entry for 1 hour.
- Suppressed empty button auto-preview spam in group chats when lightweight fetches fail with a hard block.
- Implemented fast-rejection check in manual monolith compilation commands (`/preview` and `/previewjs`) using the cache, or running a quick pre-check if the URL is not yet cached. Rejects requests instantly with a `❌` reaction and a clean block error message, completely bypassing the heavy 35-second `monolith` subprocess and saving massive CPU and bandwidth resources.

## [1.0.9] - 2026-05-22

### Added
- Implemented robust self-healing bypass for websites protected by the Anubis Web AI Firewall. The bot detects the cryptographic Proof-of-Work challenge and automatically retries utilizing a custom non-Mozilla User-Agent to retrieve the genuine webpage content.
- Added self-healing fallback in both direct monolith page compilation and group chat Open Graph metadata auto-previews.
- Extended the non-Mozilla fallback capability to the OG banner image downloading subsystem, securing complete preview generation for protected resources.

## [1.0.5] - 2026-05-22

### Added
- Implemented lightweight, automated Open Graph (OG) banner image and title previews inside group chats. Spawns dynamic `/preview_[hash]` and `/previewjs_[hash]` command links for on-demand high-fidelity monolith offline page compilations.
- Implemented a case-insensitive URL exclusions blacklist system (`/preview_exclude <pattern>`, `/preview_unexclude <pattern>`, `/preview_exclusions`) matching by part of the URL (e.g. `/telegram/` or `https://ya.ru`).
- Integrated exclusion checks in both auto-previews and explicit monolith compilations, blocking blacklisted URLs and sending a warning to the requester.
- Added automatic media bot detection: if the YouTube downloader bot (`YT Bot`) is present in the current group chat, WebPreview Bot will automatically skip generating auto-previews for links that `YT Bot` typically handles (such as YouTube, Yandex Music, Rutube, soundcloud, etc.) to prevent duplicate bot postings and chat spam.

## [1.0.4] - 2026-05-22

### Added
- Implemented a 1-hour SQLite-based caching system for page previews. If the exact same URL (with matching JavaScript settings) is requested within 1 hour, the bot returns the cached file directly, dramatically speeding up response time and reducing bandwidth and disk usage.
- Integrated automatic background DB pruning in the hourly cache cleaner loop to purge expired SQLite cache entries alongside deleting local files older than 1 hour.

### Changed
- Converted rate-limiting notifications from an intrusive and spammy text message to an elegant `⏱` emoji reaction attached directly to the triggering message.

## [1.0.3] - 2026-05-22

### Changed
- Standardized the welcome greeting to return the exact same detailed output as the `/help` command instead of a short introductory prefix message.

## [1.0.2] - 2026-05-22

### Fixed
- Resolved `Method not found` error (`-32601`) during private chat greeting checks by migrating contact `greeted` status tracking to the local SQLite database, completely bypassing missing JSON-RPC `get_contact_config` and `set_contact_config` core methods.
- Updated the 1-on-1 private chat welcome message to include a prompt to send `/help` for more commands.
- Restored the multiline `selfstatus` description to correctly separate the general info block and the command trigger line (`Send: /preview <url>`).


## [1.0.1] - 2026-05-22

### Added
- Added custom bot icon `icon.png` which is automatically set as the bot's avatar on startup.
- Auto-detection and auto-parsing of URLs sent directly in 1-on-1 private chats without needing the `/preview` command.
- Auto-welcoming message for new users starting a 1-on-1 private chat with the bot.
- Automatic upgrading of the bot administrator's fingerprint in the SQLite configuration when it becomes available via PGP key exchange.
- Log printing of the bot's SecureJoin QR code URL at startup for easy administration onboarding.

### Fixed
- Fixed `Method not found` JSON-RPC error (`-32601`) on hosts running older versions of `deltachat-rpc-server` by implementing a robust `_is_private_chat` checker with sequential fallbacks (`get_basic_chat_info` -> `get_full_chat_by_id` -> `get_chat_contacts`).
- Restored user custom modifications for caption formatting (double newline before the link: `\n\n🔗`) and `selfstatus` description to preserve them across updates.
- Fixed private chat detection logic to correctly parse both snake_case (`chat_type`) and camelCase (`chatType`) formats returned by different JSON-RPC server releases.
- Fixed build failures in the Docker environment by adding `make` utility package to the builder stage in the `Dockerfile` (needed for compiling Rust dependency crates for `monolith`).

### Changed
- Decoupled nested volume structures in `docker-compose.yml` to store SQLite data in `./data` and Delta Chat credentials in `./webpreview` to prevent file permission and access conflicts.
- Renamed the `/code` command to `/source` to provide a much more logical and intuitive name for getting the bot source repository URLs.
- Standardized file naming for offline HTML previews using the cleaned domain name and unix timestamp (`webpreview_[domain]_[timestamp].html`).

---

## [1.0.0] - 2026-05-21

### Added
- Initial release of Delta Chat WebPreview Bot.
- Multi-transport SMTP/IMAP relays support (same as Delta Chat Bouncer and YT bots).
- HTML single-file offline compilation using Rust-based `monolith` engine.
- Interactive user commands: `/preview`, `/previewjs`, `/help`, `/source`, `/stats`, `/initadmin`, `/remove_transport`, `/add_transport`.
- Robust SQLite persistent database for tracking logs, stats, and configurations.
- Admin commands restricted to verified admin fingerprints.
- Rate-limiting rules (15-second cooldown per user) and cache pruning routines for safe disk storage management.
