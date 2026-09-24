# Changelog

All notable changes to the Delta Chat WebPreview Bot will be documented in this file.

## [2.15.2] - 2026-09-24

### Fixed
- **Odd OpenRouter Models**: `openrouter/free` alone could route to a moderation classifier (`nvidia/nemotron-3.5-content-safety:free` answered "User Safety: safe") or a coding model. The default `OPENROUTER_MODELS` is now an ordered list of free chat models tested with Russian prompts (Nemotron 3 Ultra/Super, Qwen 3.8, Gemma 4 31B, dots-3), with `openrouter/free` as the last resort, and answers from `*safety*`/`*guard*` models are discarded.
- **403 Stopped the Whole Fallback**: A model-specific HTTP 403 aborted OpenRouter entirely; now only 401/402 (bad key / no credits) do, and 403 moves on to the next model.
- **OpenRouter Time Cap**: The OpenRouter chain is capped at 60s in total (30s per request).

## [2.15.1] - 2026-09-24

### Fixed
- **Minutes-Long AI Replies During Gemini Outages**: Each Gemini model could wait 20s, so a chain of timing-out models took about 2 minutes before reaching the OpenRouter fallback. The whole Gemini chain now has a total time budget (`GEMINI_TIME_BUDGET`, default 30s) and stops after 2 timeouts in a row.
- **Gemma Reasoning Leaked into Answers**: Gemma 4 returns its reasoning as separate response parts flagged `thought`, and the bot showed that reasoning instead of the answer. Reasoning parts are now dropped.

## [2.15.0] - 2026-09-24

### Added
- **Model Name in AI Replies**: `/ai`, `/tldr` (links, text and audio) and preview TL;DRs now show which model answered, e.g. `🤖 **AI** *(gemini-3.8-flash)*:` or `⚡ TL;DR *(cohere/north-mini-code:free)*:` when the OpenRouter fallback was used. The model is stored in `tldr_cache` (new `model` column, auto-migrated) so cached answers keep their label; entries cached before this version show no label.

## [2.14.1] - 2026-09-24

### Fixed
- **Empty OpenRouter Answers**: `openrouter/free` can route to reasoning models that spend the whole token budget thinking and return no text. Requests now ask for low reasoning effort with 2048 tokens of headroom, and an empty answer is retried once (re-routed to another free model). Auth/credit errors (401/402/403) stop the fallback immediately.

## [2.14.0] - 2026-09-24

### Added
- **OpenRouter Fallback (`OPENROUTER_API_KEY`, `OPENROUTER_MODELS`)**: When every Gemini model is rate-limited, overloaded or timing out, `/tldr`, `/ai` and preview TL;DRs now fall back to OpenRouter (default model `openrouter/free`, which routes to any available free model). Supports text and image prompts; audio stays Gemini-only. It also works as the only AI backend when `GEMINI_API_KEY` is empty. `/stats` shows OpenRouter request counts.

### Changed
- **Retired Model Removed**: Dropped `gemini-2.5-flash-lite` (no longer served, HTTP 404) from the default `GEMINI_MODELS` chain.
- **404 Cooldown**: A Gemini model answering HTTP 404 is placed on a 24-hour cooldown instead of being retried on every request.

## [2.13.2] - 2026-09-24

### Fixed
- **`update.sh` Deployed the Wrong Branch**: Branch detection took the first remote branch in alphabetical order, so a leftover PR branch such as `origin/claude/...` sorted before `origin/master` and was deployed instead (and new `master` commits were reported as "Already up to date"). It now follows the checked-out branch, falling back to `main`/`master`.

## [2.13.1] - 2026-09-24

### Changed
- **Private `/help` in Groups**: A plain `/help` sent in a group chat is now answered in a private 1:1 chat with the sender instead of the group, so several bots don't flood it with help texts (the reply ends with a note on how to show it in the group). Addressed `/help@web` is still answered in the group. Previously a plain `/help` was answered in the group, or silently ignored when other bots were present.

## [2.13.0] - 2026-09-18

### Added & Improved
- **24-Hour Unified Cache Retention (`CACHE_MAX_AGE = 86400`)**:
  - Elevated OpenGraph preview cards (`og_cache`) and compiled reader mode HTML / WebXDC files (`url_cache`) from 1 hour to 24 hours, matching `tldr_cache` and other bots in the fleet (`TG Bridge`, `YT Bot`).
  - Greatly reduces outbound network traffic, protects against external rate limits and anti-bot captchas, and delivers instant 0ms responses for previously requested links.
- **Cache Hit / Miss Tracking & Efficiency Metrics**:
  - Added `cache_log` SQLite table with indexes on `created_at` and `cache_type`.
  - Added `log_cache_event(cache_type, hit)` and `get_cache_stats()` tracking 24-hour hits, misses, overall hit ratio percentage, and granular breakdowns across `og` preview cards, `article` reader files, and `tldr` AI summaries.
  - Enhanced `/stats` command output to display live cache efficiency metrics over the last 24 hours.
  - Added automatic 30-day retention cleanup for `cache_log` records in `cleanup_old_records`.
- **Unit Tests (`tests/test_database.py`, `tests/test_transport_commands.py`)**:
  - Added comprehensive test suites verifying cache logging, 24h stats calculation, hit ratio formatting, breakdown accuracy, retention cleanup, and `/stats` command presentation.

## [2.12.1] - 2026-09-18

### Security & Robustness
- **Anti-Loop Defense Hardening**:
  - Excluded messages starting with bot card and message prefixes (`📰`, `🌐`, `🤖`, `📷`, `💬`) from URL auto-parsing.
  - Hardened `_is_bot_blocked` with strict boolean evaluation on both message snapshots and contact RPC objects (`contact.is_bot is True`) to prevent bot-to-bot echo loops and MagicMock false positives in test environments.
  - Added unit test coverage for anti-loop prefix filtering and contact bot evaluation in `tests/test_telegram_parser.py`.

## [2.12.0] - 2026-09-18

### Added
- **Telegram Post Link Delegation to TG Bridge**:
  - Added `_is_tg_bridge_in_chat` detection checking for active `TG Bridge` / `Telegram Bridge` bot contacts in the current chat.
  - Added `_is_telegram_post_url` to accurately identify direct channel post URLs (`t.me/{channel}/{post_id}` and `t.me/s/{channel}/{post_id}`).
  - In `on_new_message`, automatically skips link auto-preview for Telegram posts if `TG Bridge` is present in the chat, yielding handling to TG Bridge's native MTProto / WebXDC pipeline to avoid duplicate previews and ensure complete delivery of rich posts, media albums, and videos.
- **Unit Tests (`tests/test_telegram_parser.py`)**:
  - Added `TestTgBridgeDelegation` suite covering Telegram post URL detection, `_is_tg_bridge_in_chat` contact lookup, and `on_new_message` skip verification.

## [2.11.0] - 2026-09-16

### Added
- **Audio & Voice Message Support for `/ai` and `/tldr`**:
  - Added support for summarizing audio and Delta Chat voice messages via `/tldr` (by replying to a voice note or attaching audio).
  - Added voice message transcription, Q&A, and analysis via `/ai` (by replying to an audio note with or without a prompt, or sending `/ai` with an audio attachment).
  - Implemented audio MIME detection (`_detect_audio_mime`) supporting OGG/Opus, MP3, WAV, AAC, M4A/MP4, FLAC, and WebM via magic header bytes, declared message MIME types, and file extensions.
  - Implemented multimodal media extraction (`_extract_media_from_msg_or_quote`, `_extract_audio_from_msg_or_quote`) supporting direct attachments, quoted messages (`quote.message_id`), and parent messages (`parent_id`).
  - Generalized `_call_gemini_api` to send audio data using Google Gemini's native `inline_data` multimodal payload format with automatic text-only model exclusion (`gemma-*`).
  - Added `_summarize_audio_with_gemini` with 24-hour SQLite caching per audio hash and target language (`/lang`).
  - Added 20 MB file size limit enforcement (`MediaTooLargeError`) with polite user rejection messages for oversized media.
  - Increased media attachment download timeout from 15s to 30s to reliably fetch voice recordings across slower mail relays.
- **Unit Tests (`tests/test_audio.py`)**:
  - Added 13 comprehensive unit tests covering audio MIME detection, 20MB limit checks, Gemini audio summarization, 24h caching, `/tldr` voice summarization, and `/ai` voice message processing.

## [2.10.1] - 2026-09-16

### Security
- **Image URL SSRF & DNS Rebinding Hardening (`_is_valid_image_url`)**:
  - Added DNS resolution checks and IPv6-mapped IPv4 checks to `_is_valid_image_url()` ensuring candidate preview image hosts do not resolve to private, loopback, link-local, cloud metadata, or reserved IP ranges.
- **Archive Command URL Protection (`/keep`)**:
  - Enforced `_is_internal_or_invalid_url` check in `_handle_keep_command` to reject attempts to archive local, internal, or private endpoints.
- **Dependency Pinning**:
  - Pinned `qrcode>=7.4.2,<8.0.0`, `readability-lxml>=0.8.1,<1.0.0`, `beautifulsoup4>=4.12.3,<5.0.0`, `Pillow>=10.4.0,<11.0.0`, and `lxml>=5.2.0,<6.0.0` in `requirements.txt`.

## [2.10.0] - 2026-09-09

### Security
- **SSRF Hardening & DNS Rebinding Protection**:
  - Enhanced `_is_internal_or_invalid_url` to resolve candidate hostnames via `socket.getaddrinfo` (with IDNA punycode handling for international and Cyrillic domains) and reject destinations that resolve to private, loopback, link-local, reserved, multicast, or unspecified (`0.0.0.0`, `::`) IP addresses.
  - Added `SafeRedirectHandler` to intercept HTTP redirects across `urllib.request` calls, ensuring that redirects to private/internal networks are rejected with HTTP 403.
- **Private Chat Enforcement for Sensitive Commands**:
  - Enforced `_is_private_chat` check on `/addtransport` to prevent administrator credentials and email passwords from being leaked into group chats.
  - Enforced `_is_private_chat` check on `/initadmin` to prevent bot ownership claim races in group chats.
- **Error Message Sanitization**:
  - Sanitized error output in `/addtransport`, `/setprimary`, `/resilient`, `/rmtransport`, `/invidious_list`, `/ai`, and file downloading to avoid leaking internal exception details, credentials, or file paths into chats while preserving full error traces in the logger.

### Fixed
- **Resilient Transport Concurrency Race**:
  - Synchronized `original_send_msg` and `configured_addr` configuration with `resilient_lock`, preventing race conditions when concurrent messages are queued while background failover workers temporarily switch mail transports.
  - Wrapped secondary transport iteration in `bg_resend_worker` with a `try ... finally` block to guarantee the primary transport is always restored.
- **Bounded Message Deduplication & Rate Limiting**:
  - Replaced unordered set slicing in `_is_duplicate_msg` with `collections.OrderedDict` FIFO eviction capped at 1000 messages, eliminating arbitrary eviction of recent messages.
  - Added thread-safe locking and periodic cleanup to `_user_rate_limits` to prevent unbounded memory growth.

### Performance & Database
- **SQLite Concurrency & WAL PRAGMAs**:
  - Standardized all database connections using a helper with `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=5000;`, and `PRAGMA cache_size=-4000;`.
  - Added performance indexes on `preview_stats`, `api_log`, `url_cache`, `og_cache`, `tldr_cache`, and `url_hashes`.
- **Buffered Transport Statistics**:
  - Implemented in-memory buffering (`_transport_stats_buffer`) for transport sent and received counters with batch flushing to disk every 30 seconds, eliminating synchronous disk writes on every message.
- **Automated Record Retention Pruning**:
  - Added `cleanup_old_records(retention_days=30)` to prune stale preview stats and API logs during hourly cache cleanup cycles.

### Tests
- Added `tests/test_database.py` adhering to `AGENTS.md` testing conventions (config, admin fingerprinting, buffered transport stats, and 30-day retention).
- Added `tests/test_transport_commands.py` testing transport commands, private chat verification, `/resilient` toggle, and error sanitization.
- Expanded `tests/test_url_validation.py` with DNS resolution, rebinding, and `SafeRedirectHandler` tests.

## [2.9.9] - 2026-09-08

### Fixed
- **Filter Non-Fetchable Image URLs (`blob:`, `data:`, localhost, private IPs):**
  - Added `_is_valid_image_url` helper to validate candidate preview images before making network requests.
  - Skips non-fetchable URL schemes (`blob:`, `data:`, `javascript:`, `file:`, `about:`) and internal/private network targets (`localhost`, `127.0.0.1`, private IP ranges, `.local`, `.lan`) without triggering 3 failed download attempts or warning logs.
  - Enhanced `_parse_jina_response` and HTML meta tag parsing to skip invalid or browser-local `blob:` images and discover the first valid HTTP/HTTPS image URL in webpage content.
  - Enhanced `_inline_soup_images` to decompose `blob:` and `javascript:` images instead of attempting network fetches.

## [2.9.8] - 2026-09-07

### Added
- **Configurable Display Name & Status Text**:
  - `on_init` now checks `DISPLAY_NAME` and `STATUS_TEXT` environment variables with `/data/options.json` fallback instead of overwriting display name with static strings.

## [2.9.7] - 2026-09-05

### Performance
- **Prebuilt Monolith Binary in Docker:**
  - Replaced multi-stage Rust compilation (`cargo install monolith`) with direct download of the official prebuilt `monolith` binary (v2.10.1) for x86_64 and aarch64.
  - Drastically speeds up Docker image builds from several minutes down to seconds and avoids heavy Rust toolchain dependencies.

## [2.9.6] - 2026-09-04

### Improved
- **Clean Instagram Preview Format & Post Captions:**
  - Placed Instagram post captions/descriptions directly beneath the image thumbnail and preceding the source author link (`🌐 [Instagram (@user)](url)`).
  - Expanded Instagram caption preservation up to 500 characters while normalizing whitespace and line breaks.
  - Removed interactive action buttons (`/tldr`, `/preview`, `/webxdc`, `/keep`) for Instagram preview cards to provide a clean, distraction-free photo card experience.
  - Retained standard interactive preview buttons for non-Instagram websites.

## [2.9.5] - 2026-09-04

### Fixed
- **Instagram Preview Image Downloads & Bot User-Agent:**
  - Fixed an issue where Instagram preview cards were sent without their banner images because embed proxies (e.g. `kkinstagram.com`) return HTML redirect pages when requested with standard desktop browser User-Agents.
  - Added `BOT_USER_AGENT` (`Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)`) to `_download_cached_image` and `_download_image_bytes`, prioritizing bot user-agents for Instagram and social media embed proxies to reliably fetch the direct JPEG image data.
  - Added `rapidcdn.app` to Instagram domains routed through `INSTAGRAM_PROXY_URL`.
  - Added automatic cache miss handling in `_do_group_link_preview` when an existing cached entry for an Instagram URL is missing its thumbnail image (`image_path is None`), allowing previously failed image downloads to automatically self-heal upon re-request.

## [2.9.4] - 2026-09-04

### Improved
- **Unified Link Previews for Private & Group Chats:**
  - Standardized bare URL handling in 1-on-1 private messages to send interactive preview cards with banner images and quick action buttons (`⚡ /tldr`, `🖥️ /preview`, `📦 /webxdc`, `🏛️ /keep`), matching the behavior in group chats.
  - Avoids automatically compiling heavy, full-page readability HTML files and executing Gemini AI calls on every link shared in direct messages, while keeping full preview generation readily accessible via `/preview` commands and preview card buttons.

## [2.9.3] - 2026-09-04

### Improved
- **Automatic Direct Fallback for Proxies:**
  - Added resilient fallback to direct connection in `_urlopen` when a configured proxy (`INSTAGRAM_PROXY_URL`, `PROXY_URL`, `ARCHIVE_TODAY_PROXY_URL`, or `JINA_PROXY_URL`) encounters an error (HTTP 403, timeouts, connection refused, or tunnel failures).
- **Instagram Gateway Fallbacks & Direct Media:**
  - Added `kkclip.com` to the Instagram gateway mirror list.
  - Improved handling for gateways returning direct media (`image/jpeg`) by extracting the author username from the path (e.g. `/@username/p/...`), formatting cleaner titles (`Instagram (@username)`).
  - Ensured failed proxy attempts on gateways fall back to direct connections before falling back to generic Jina/web readability.

## [2.9.2] - 2026-09-04

### Added
- **Dedicated Instagram Proxy (`INSTAGRAM_PROXY_URL`):**
  - Added support for routing Instagram/OGInstagram metadata requests and thumbnail image downloads through an optional dedicated proxy (e.g. residential UK router) to bypass Cloudflare/datacenter IP rate-limits on public gateways (`oginstagram.com`, `kkinstagram.com`, `vxinstagram.com`).
- **`RU_PROXY` Environment Variable Alias:**
  - Added support for `RU_PROXY` as an alias for `PROXY_URL`, unifying proxy configuration across `deltachat_yt` and `deltachat_webpreview`.
- **Automatic IDNA / Punycode Domain Support:**
  - Expanded `PROXY_DOMAINS` parsing to automatically generate and match both Unicode and Punycode variants (e.g. `.рф` and `.xn--p1ai`), allowing internationalized domain suffixes written in Cyrillic to route through the proxy seamlessly.

## [2.9.1] - 2026-09-04

### Improved
- **Support Internal Docker Hosts & HTTP Schemes for `OGINSTAGRAM_HOST`:**
  - Added support for explicit `http://` or `https://` URLs, custom ports (e.g. `http://oginstagram:3000`), and internal Docker container hostnames in `OGINSTAGRAM_HOST`.
  - Automatically adapts direct media fallback endpoints (`/d/...`) for internal and port-based setups.
  - Allows running private, non-publicly exposed OGInstagram or embed proxy containers within the internal Docker bridge network.

## [2.9.0] - 2026-09-04

### Added
- **Instagram & OGInstagram Embed Support**: Added native link preview support for Instagram posts (`/p/...`), reels (`/reel/...` and `/reels/...`), stories, and profiles using [OGInstagram](https://github.com/seirenkr/OGInstagram).
  - **Early Domain Interception**: Intercepts Instagram URLs before standard generic HTML parsing and before Jina Reader to avoid Instagram login walls, rate limits, and 403 blocks.
  - **Captions & Accessibility Alt-Text Extraction**: Extracts author/profile names, full post captions (`og:description`), and accessibility descriptions (`og:image:alt` / `twitter:image:alt`), formatting them cleanly into preview cards and Markdown caches.
  - **Direct Unblocked Image Delivery**: Bypasses Instagram CDN hotlinking protection by downloading unblocked images via OGInstagram (with automatic fallback to `d.{OGINSTAGRAM_HOST}` direct media endpoint), compressing them into WebP format for fast Delta Chat delivery.
  - **Configurable Host (`OGINSTAGRAM_HOST`)**: Supports custom or self-hosted OGInstagram instances via the `OGINSTAGRAM_HOST` environment variable (defaults to `oginstagram.com`).
- **Unit Tests**: Added test suite `TestFetchInstagramOgData`, `TestIsInstagramUrl`, and `TestGetOgPreviewDataInstagramEarlyReturn` in `tests/test_instagram_parser.py` to verify OpenGraph extraction, alt-text parsing, fallback routing, and early returns.

## [2.8.2] - 2026-09-03

### Changed
- **Primary AI Model Upgrade to Gemini 3.8 Flash**: Added `gemini-3.8-flash` as the primary default model in the `GEMINI_MODELS` fallback chain (`gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-31b-it,gemma-4-26b-a4b-it,gemini-2.5-flash-lite`) for all `/tldr`, `/ai`, and web preview summarization requests.

## [2.8.1] - 2026-08-25

### Fixed
- **Web Archive Stale Snapshot Validation (`/keep`)**: Fixed a bug where `_check_wayback_availability` returned historical Wayback Machine snapshots from years ago (e.g. from 2022) when current archiving failed, falsely treating the save as successful and aborting fallback. Now, `_check_wayback_availability` enforces snapshot timestamp freshness (within 1 hour / `max_age_seconds`). If only an old snapshot exists, the save is properly marked as failed, allowing `_do_keep` to fall back to **Archive.today** and **Ghostarchive** to capture a fresh snapshot.
- **Unit Tests**: Added test suite `TestCheckWaybackAvailability` and tests in `TestSaveToWebArchive` to verify rejection of stale historical snapshots (e.g. from 2022) and seamless fallback to alternate archive services.

## [2.8.0] - 2026-08-21

### Added
- **Multimodal Image & Vision Analysis (`/ai [text]`)**: Added full multimodal vision capabilities to the `/ai` command using Google Gemini's vision models.
  - **Direct Photo Upload**: Send a photo, screenshot, or graphic with a caption like `/ai What is this?` or `/ai Transcribe this text` (or just `/ai` for an automatic detailed description).
  - **Reply to Photo / Sticker**: Reply `/ai <question>` (or just `/ai`) to any image, sticker, photo, or image attachment in the chat to analyze its visual content.
  - **MIME & Magic Header Detection**: Automatically identifies JPEG, PNG, WEBP, GIF, HEIC, and BMP images.
  - **Smart Multimodal Routing**: Automatically filters and routes image queries to multimodal `gemini-*` models in the fallback chain.
  - **24-Hour Image Caching**: Caches visual AI responses keyed by image content hash to optimize API quota consumption.
- **Unit Tests**: Added comprehensive test suites for image MIME detection, attachment and quote extraction, base64 payload construction, and vision AI query caching in `tests/test_tldr_and_lang.py`.

## [2.7.2] - 2026-08-21

### Changed
- **Adaptive AI Answer Length**: Refined prompt in `_ask_gemini_ai` to dynamically adapt the response length based on the nature of the prompt. Direct questions (e.g. quick facts, math, yes/no) yield ultra-concise answers (from a single word or sentence), while complex concepts and open-ended topics expand up to 2-3 informative paragraphs maximum.
- **Case-Insensitive Command Matching**: Made all bot commands case-insensitive (e.g. `/AI`, `/TLDR`, `/LANG`, `/PREVIEW`, `/ARCHIVE`, `/KEEP`, `/DOWNLOAD`, `/HELP`, `/STATS`). Commands can now be entered in lowercase, uppercase, or mixed-case without issue.

## [2.7.1] - 2026-08-20

### Changed
- **Optimized Gemini & Gemma Models Fallback Chain**: Updated default `GEMINI_MODELS` list to place modern 4th-generation Gemma models ahead of older generation Lite models: `gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemma-4-31b-it,gemma-4-26b-a4b-it,gemini-2.5-flash-lite`. This provides superior reasoning quality and factual depth via Gemma 4 31B and 26B MoE before falling back to legacy 2.5-lite.

## [2.7.0] - 2026-08-20

### Added
- **AI Question & Topic Answering (`/ai <text>`)**: Added support for answering questions and providing concise 2-3 paragraph topic explanations using Google Gemini API (`_ask_gemini_ai`).
  - **Direct Query**: `/ai <question or topic>` directly generates an informative overview.
  - **Reply to Message**: Replying `/ai` to any text message answers or explains the quoted content.
  - **Contextual Query**: Replying `/ai <question>` to a message answers the question taking the quoted message as context.
  - **URL & Article Context**: Providing or quoting a URL with `/ai` automatically fetches the article text and generates an AI answer/overview with the source link attached.
  - **Language Preference Support**: Seamlessly respects the chat's `/lang` setting (`AUTO` matches query language, or explicitly configured languages such as `RU`, `EN`, `DE`).
  - **SQLite Response Caching**: Responses are cached for 24 hours to conserve API quotas.
- **Unified Gemini API Helper (`_call_gemini_api`)**: Refactored Gemini API calls to a centralized helper with automatic multi-model failover (`GEMINI_MODELS`), rate limit cooldowns (HTTP 429/500/503), and API logging.
- **Unit Tests**: Added test suite covering direct AI queries, contextual queries with quoted messages, 24-hour response caching, command handlers, and help text in `tests/test_tldr_and_lang.py`.

## [2.6.1] - 2026-08-20

### Added
- **Internet Archive SPN2 API Support (`WAYBACK_ACCESS_KEY` & `WAYBACK_SECRET_KEY`)**: Added native authenticated Save Page Now 2 (SPN2) API integration for the Wayback Machine. With free S3 keys from `archive.org/account/s3.php`, archive jobs are submitted directly via authenticated API and polled until snapshot generation, avoiding anonymous 403 Forbidden blocks and rate limits.
- **Ghostarchive Integration (`ghostarchive.org`)**: Added concurrent web archiving to Ghostarchive with strict snapshot ID validation.
- **Archive.today & Ghostarchive Proxy Routing (`ARCHIVE_TODAY_PROXY_URL`)**: Added support for routing Archive.today and Ghostarchive requests through a dedicated proxy (e.g. Tor `socks5://127.0.0.1:9050` or HTTP proxy) or `PROXY_URL` to bypass Cloudflare 429/403 blocks on server IPs.
- **Docker Compose Configuration**: Added `ARCHIVE_TODAY_MIRRORS`, `ARCHIVE_TODAY_PROXY_URL`, `WAYBACK_ACCESS_KEY`, and `WAYBACK_SECRET_KEY` to `docker-compose.yml`.
- **Unit Tests**: Added test suites `TestSaveToWebArchive`, `TestSaveToGhostarchive`, and `TestArchiveTodayProxyRouting` in `tests/test_karakeep.py`.

## [2.6.0] - 2026-08-20

### Added
- **Asynchronous Concurrent `/keep` Archiving**: Overhauled the `/keep` workflow:
  1. **KaraKeep First**: If KaraKeep is configured and the command is run by the bot administrator, the URL is saved to KaraKeep first and bookmark confirmation is sent directly to the administrator's private chat.
  2. **Concurrent Web Archive & Archive.today**: The bot triggers archiving simultaneously to both the Internet Archive (Wayback Machine, with a generous 120s timeout) and **Archive.today** (with dynamic multi-mirror selection: `archive.ph`, `archive.is`, `archive.today`, `archive.li`, `archive.vn`, `archive.md`).
  3. **Streamed & Consolidated Reporting**: If both archives finish promptly, a consolidated response with both links is posted to the chat; if one service finishes early while the other takes longer, results are streamed seamlessly so users never wait on slow responses.
- **Archive.today Configuration (`ARCHIVE_TODAY_MIRRORS`)**: Added support for configuring or customizing the Archive.today mirror list via the `ARCHIVE_TODAY_MIRRORS` environment variable.
- **Unit Tests**: Added test suite `TestSaveToArchiveToday` and expanded `TestDoKeep` in `tests/test_karakeep.py` covering redirects, Location headers, meta refresh / WIP extraction, mirror failover sequence, concurrent execution, and failure reporting.

## [2.5.9] - 2026-08-16

### Added
- **Direct & Quoted Message Summarization (`/tldr`)**: Added support for summarizing plain text and quoted messages without links. Replying `/tldr` to any text message or passing text directly via `/tldr <text>` generates a concise 1-2 paragraph AI summary using the chat's target language (or `AUTO`). Summaries are cached in SQLite for 24 hours.
- **Unit Tests**: Added test cases for quoted message summarization, direct text summarization, and short text validation in `tests/test_tldr_and_lang.py`.

## [2.5.8] - 2026-08-16

### Changed
- **Gemini Models Fallback Chain**: Updated default `GEMINI_MODELS` list to include `gemini-3.7-flash` as the primary model: `gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemma-4-31b-it`.

## [2.5.7] - 2026-08-16

### Changed
- **Default Summary Language (`AUTO`)**: Changed the default summary language for chats from `EN` to `AUTO`. The bot now automatically summarizes articles in their original language by default (e.g. Russian articles in Russian, German in German, English in English).
- **Clean TL;DR Header**: Omitted the language code suffix in `/tldr` responses when using `AUTO` (`⚡ **TL;DR**:` instead of `⚡ **TL;DR** (AUTO):`), displaying language tags only when explicitly configured via `/lang`.

## [2.5.6] - 2026-08-16

### Changed
- **Preview Card Action Buttons**: Replaced `/keep` in standard preview card action buttons with `/tldr`. Default action buttons for all users are now `⚡ /tldr`, `🖥️ /preview`, and `📦 /webxdc`.
- **Admin-Only `/keep` Visibility**: The `🏛️ /keep` button under preview cards and in `/help` is now hidden for regular users and displayed only for the bot administrator.

### Added
- **Dynamic TL;DR Trigger (`/tldr_[hash]`)**: Added dynamic command `/tldr_{urlhash}` link handling so clicking `⚡ /tldr` under any link preview instantly generates an AI summary of that article.
- **Unit Tests**: Added unit tests in `tests/test_tldr_and_lang.py` for `/tldr_[hash]` dynamic command trigger, preview button formatting (admin vs regular user), and `/help` command keep visibility.

## [2.5.5] - 2026-08-13

### Added
- **Animated WebP Preservation**: Direct GIF links, animated WebP files, and OpenGraph preview images now retain multi-frame animation when converted/compressed to WebP format. `_save_image_as_webp` iterates through frames (`PIL.ImageSequence.Iterator`), resizes each frame preserving aspect ratio, preserves alpha/transparency channels, and saves using `save_all=True` with original frame durations and loops.
- **Unit Tests**: Added `tests/test_image_compression.py` verifying animated GIF to animated WebP conversion, frame counts, loop preservation, and static image handling.

## [2.5.4] - 2026-07-27

### Added
- **Multi-Model Dynamic Fallback (`GEMINI_MODELS`)**: Added support for comma-separated Gemini fallback model chains (e.g. `GEMINI_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash,gemma-4-31b`). When a model reaches its daily quota (HTTP 429), the bot automatically fails over to the next model in the chain without interrupting operations, unlocking 15,400+ free daily requests across models.
- **Unit Tests**: Added `test_multi_model_fallback` in `tests/test_tldr_and_lang.py`.

## [2.5.3] - 2026-07-27

### Added
- **API Requests Tracking & Enhanced `/stats`**: Added SQLite `api_log` table to log Jina AI Reader and Google Gemini API requests. Updated `/stats` to display 24h & total request counts for both Jina and Gemini, Jina API token balance, and Gemini model status.
- **Reaction UX Improvement**: Changed reaction for `/lang` command from `👍` to checkmark `☑️` for consistency with all other bot commands.

## [2.5.2] - 2026-07-27

### Added
- **24-Hour Summary Caching (`tldr_cache`)**: Added SQLite caching table `tldr_cache` to cache Gemini TL;DR summaries for 24 hours per URL, language, and summary mode. Eliminates duplicate Gemini API requests, saves API quota, and speeds up repeated responses from ~2s to <10ms.
- **Model Endpoint Improvements**: Configured `gemini-flash-latest` as the default model alias (automatically referencing Google's latest free-tier Flash model), sanitized input quotes/prefixes from `.env`, added official `x-goog-api-key` header, and enabled detailed HTTP response logging for easier diagnostics.

## [2.5.1] - 2026-07-27

### Added
- **AI Article Summarization (`/tldr`)**: Added `/tldr <url>` command (and reply support) to generate concise 1-2 paragraph article summaries using Google Gemini API (`GEMINI_API_KEY`).
- **Summary Language Selection (`/lang`)**: Added `/lang <code>` command to configure preferred summary language on a per-chat basis (e.g. `/lang RU`, `/lang EN`, `/lang DE`).
- **Automatic TL;DR in Previews**: Enhanced `/preview` and `/webxdc` commands to automatically attach a 1-paragraph TL;DR summary above the link when Gemini integration is enabled.
- **Standalone Offline Readability Fallback**: Added automatic fallback to local `readability-lxml` + `BeautifulSoup` HTML text extraction if Jina Reader (`r.jina.ai`) is unavailable or unconfigured, making Gemini summarization fully functional independently of Jina.
- **Unit Tests**: Added `tests/test_tldr_and_lang.py` covering Gemini summarization, `/lang` database storage, URL extraction from replies, and caption formatting.

## [2.4.0] - 2026-07-26

### Added
- **WebXDC App Command (`/webxdc`)**: Added the `/webxdc <url>` command and dynamic `/webxdc_<urlhash>` triggers. Compiles a webpage into a standalone WebXDC application (`.xdc` ZIP file with `index.html`, `manifest.toml`, and `icon.png`) and sends it directly to the chat.
- **Unit Tests**: Added `tests/test_webxdc.py` verifying WebXDC archive creation (`index.html`, `manifest.toml`, `icon.png`), `/webxdc` command trigger, and dynamic `/webxdc_<urlhash>` handling.

## [2.3.23] - 2026-07-08

### Added
- **Chat-Specific Toggle (`/webpreview`)**: Added the `/webpreview` command to allow users to disable/enable automatic link previews on a per-chat basis (in private or group chats). By default, previews remain enabled. Supports parameters `off`/`0`/`false` to disable and `on`/`1`/`true` to enable.
- **Unit Tests**: Added `tests/test_webpreview.py` containing 3 unit tests verifying database state persistence, command parsing, and `on_new_message` behavior when disabled.

## [2.3.22] - 2026-07-08

### Added
- **Storage and Bandwidth Optimization**: Configured Delta Chat's `download_limit` option to `"1"` (1 byte) by default on startup to disable automatic downloads of incoming attachments. Added `DOWNLOAD_LIMIT` environment variable.
- **Auto-Cleanup Configuration**: Configured `delete_device_after` to `"3600"` (1 hour) by default on startup to automatically clean up old messages from the bot's local database. Added `DELETE_DEVICE_AFTER` environment variable.

## [2.3.21] - 2026-07-06

### Fixed
- **Fix Dependency Conflict/NameError:** Pinned `deltabot-cli==8.1.2` and `deltachat2[full]<1.0.0` in `requirements.txt` to resolve dependency conflicts and avoid the `ChatType` NameError/ImportError bugs introduced in newer, incompatible versions of `deltachat2`.

## [2.3.20] - 2026-07-03

### Fixed
- **Zombie Process Reaping:** Enabled `init: true` in Docker Compose to automatically reap zombie processes in the bot container, preventing PID limit exhaustion.

## [2.3.19] - 2026-07-01

### Added
- **Jina AI API Key Token Balance Command (`/jina`)**: Added a new administrative command `/jina` that retrieves token balance statistics for the bot's configured `JINA_API_KEY` or a custom key provided as an argument (e.g., `/jina <api_key>`). Returns total, trial, and regular balances formatted with thousands separators, as well as trial validity dates.

## [2.3.18] - 2026-06-29

### Added
- **Web Archive fallback for `/keep`**: Overhauled `/keep` command, `/keep_{urlhash}` dynamic command, and `/keep` quote replies so they are accessible to all users. `/keep` now always saves the target URL to the Web Archive (Wayback Machine) and posts the link back to the current chat, changing the success reaction to `☑️` (matching `/preview` and `/archive`).
- **Private KaraKeep Notification for Admin**: If KaraKeep is configured and the command is executed by the bot administrator, the bot will additionally save the URL to KaraKeep in the background and send the bookmark link directly to the administrator in a private chat.

## [2.3.17] - 2026-06-26

### Fixed
- **Image Inlining responsive bypass:** Web browsers were bypassing inlined Base64 image payloads and downloading original files (e.g. from Habr's `habrastorage.org`) because responsive attributes (`srcset` and `sizes`) and lazy loading attributes (`data-src` / `data-srcset`) were left intact on `<img>` tags. Refactored image inlining into a unified `_inline_soup_images()` helper that strips these attributes.

## [2.3.16] - 2026-06-26

### Added
- **Telegram Preview Caching Support:** Extracted Telegram post content is now returned as markdown (`jina_markdown`), allowing `/preview` to generate and compile readability HTML previews directly from local cache without making redundant network requests.

### Fixed
- **Telegram Post Parser:** Switched parsing from the non-functional `t.me/oembed` endpoint (which resolves incorrectly to an `oembed` channel page on Telegram) to the official public preview feed (`t.me/s/...`) using BeautifulSoup, robustly extracting the author name, content text (with preserved line breaks), and media thumbnails.

## [2.3.15] - 2026-06-26

### Added
- **Telegram Post Parser (`_fetch_telegram_og_data`):** When a `t.me` link is sent to the bot, it now fetches post metadata via Telegram's oEmbed API (`https://t.me/oembed?url=...&format=json`) instead of trying to scrape the page HTML. Supports standard post URLs (`t.me/channel/123`), stream-prefixed URLs (`t.me/s/channel/123`), and private channel numeric IDs (`t.me/c/123456789/123`). The preview title is formed as `"channel_name: post text excerpt (up to 200 chars)"`, with thumbnail image included if the post has media. The oEmbed path is an early-return in `_get_og_preview_data` before the standard HTML fetch and Jina fallback.

## [2.3.14] - 2026-06-26

### Fixed
- **URL trailing junk stripping:** URLs extracted from natural-language text were not properly cleaned of Unicode closing punctuation. For example, `https://www.kommersant.ru/doc/8765189)»` would propagate the trailing `)»` into fetch requests, causing ASCII encoding errors. Introduced `_strip_url_trailing_junk()` which removes standard ASCII punctuation (`.,;:!?`), Unicode closing quotation marks (`»`, `"`, `'`, `›`, etc.) and unbalanced closing parentheses/brackets, while safely preserving balanced parens (e.g. Wikipedia URLs). All 8 URL extraction call-sites now use this function.
- **Unit tests:** Added `tests/test_strip_url.py` with 18 tests covering the new helper.

## [2.3.13] - 2026-06-25

### Added
- **Unit Test Suite (`tests/`):** Introduced a structured `tests/` directory with 53 unit tests across three modules:
  - `test_url_validation.py` – URL validation (`_is_internal_or_invalid_url`): valid external domains, blocked IP ranges, local TLDs.
  - `test_invidious.py` – Invidious/YouTube helpers: video ID extraction, domain cleaning, and database add/remove/list operations.
  - `test_proxy_and_jina.py` – Proxy routing, Jina AI Reader integration, SVG skip logic, octet-stream header handling, cache saving, OG fallback, and the `_do_preview` cache optimization.
- **GitHub Actions CI (`.github/workflows/tests.yml`):** Automated test runner that executes the full test suite on every push and pull request to `main`.

## [2.3.12] - 2026-06-25

### Optimized
- **Manual Preview and Download Caching optimization:** Modified `_do_preview` and `_do_download` to check `og_cache` first and skip redundant network/HTTP header queries to the remote site if a fresh preview cache is already found in the SQLite database. This fixes the issue where clicking `/preview` or `/download` triggered another request to the target website despite having a cached copy.

## [2.3.11] - 2026-06-25

### Added
- **Jina Crawler Proxy Routing (X-Proxy-Url):** If the target URL matches a domain in `PROXY_DOMAINS` (e.g. `.ru`) and `PROXY_URL` is set, the bot now forwards the proxy URL to Jina using the `X-Proxy-Url` header. This instructs Jina's remote crawler to download the target site through our proxy server, resolving geo-restrictions or IP blocks on target Russian sites, while keeping `JINA_PROXY_URL` isolated for local bot connection to the Jina API.

## [2.3.10] - 2026-06-25

### Added
- **Jina HTML Readability Fallback:** Added support for requesting raw HTML from Jina AI Reader (`X-Return-Format: html`). When standard readability extraction fails, the bot now falls back to querying raw HTML from Jina and running `readability-lxml` directly on it. This combines Jina's dynamic rendering/WAF bypass capabilities with Python's native readability/BeautifulSoup pre-cleaning logic, yielding 100% clean layouts without menus or boilerplate, while keeping Jina Markdown as a tertiary fallback.

## [2.3.9] - 2026-06-25

### Added
- **Robot Indicator for Jina Previews:** Added a robot emoji indicator `🤖🌐` next to the title when the preview is served or fetched via Jina AI Reader, giving visual feedback that Jina was used.
- **Jina Request Exclusions:** Configured Jina Reader requests with `X-Exclude-Selector` headers to exclude common layout elements (`nav`, `footer`, `header`, etc.) directly on Jina's side, reducing payload size.
- **Jina Markdown Cleaning Heuristics:** Implemented a robust markdown-cleaning algorithm (`_clean_jina_markdown`) to isolate only the main article text (starts with title heading and ends before copyright/boilerplates), greatly enhancing readability results.

### Changed
- **Reverted Group Previews to Clean Buttons:** Reverted direct rendering of Jina markdown inside group chat messages. Group previews now always show the clean card layout with `/preview`, `/archive`, and `/keep` buttons instead of displaying full-page articles directly in the chat.

## [2.3.8] - 2026-06-25

### Added
- **Direct Jina Markdown Preview rendering:** When standard OG extraction fails and Jina AI Reader is triggered, the bot now directly outputs the parsed page content (markdown) inside the group chat message instead of presenting `/preview` and `/archive` buttons.
- **Cached Jina Compilation:** When Jina markdown is fetched during link preview, it is compiled into a readability HTML file and stored in the database cache. If manual `/preview_[hash]` or private readability previews are requested later, the bot serves the compiled HTML preview instantly from cache, avoiding redundant network queries and saving Jina Reader API calls.

## [2.3.7] - 2026-06-25

### Changed
- **WAF Bypass / Octet-Stream Handling:** Improved `_check_url_headers` to allow `application/octet-stream` content-types for URLs that do not match known binary file extensions. This prevents fast-rejection of webpages protected by anti-bot/WAF systems (like QRATOR returning empty octet-streams to bots) and lets the bot proceed to the Jina AI fallback preview generator successfully.

## [2.3.6] - 2026-06-25

### Fixed
- **SVG Image Processing:** Fixed a crash/warning when processing SVG preview images. SVG vector files are now ignored during preview generation to prevent Pillow from failing to compress them and sending them as broken image attachments.

## [2.3.5] - 2026-06-25

### Added
- **Jina AI API Key & Proxy Support:** Added support for specifying a Jina AI API key via `JINA_API_KEY` to authenticate requests and increase rate limits. Isolated Jina AI Reader requests from the default `PROXY_URL` (they now execute directly by default) and added support for a dedicated `JINA_PROXY_URL` proxy configuration.
- **Domain-Specific Proxy Routing:** Added support for routing standard URL fetches and `monolith` subprocesses targeting specific domain suffixes (like `.ru`) through an optional proxy server, configured via `PROXY_URL` and `PROXY_DOMAINS` environment variables.
- **Environment Template:** Added a `.env.example` template file for local deployment convenience.

## [2.3.4] - 2026-06-25

### Added
- **Jina AI Warning Previews:** The bot now extracts and displays warnings (such as CAPTCHA notices) in group link previews if Jina AI returns a blank title and warns about page access restrictions, falling back to a clean `URL Source: <url>` title.

## [2.3.3] - 2026-06-25

### Changed
- **Bidirectional Suffix Matching:** Suffix matching is now bidirectional (e.g. `@w` or `@webpreview` will match WebPreview bot).
- **Smart Group Chat Command Filtering:** The bot now automatically ignores unaddressed general `/help` and `/stats` commands in group chats if other bots are present in the chat.

## [2.3.2] - 2026-06-25

### Added
- **Target-Specific Command Suffixes:** Added support for addressing this bot specifically in group chats using `/command@web` or `/command@wp` suffixes.

## [2.3.1] - 2026-06-25

### Changed
- **Compact Preview Format:** Simplified the layout of web and file preview messages to be more compact, utilizing Markdown links to combine the title/filename with the URL, and aligning command options side-by-side.
- **Dynamic Keep Command:** Added a dynamic `/keep_[hash]` command link alongside the `/preview_[hash]` and `/archive_[hash]` links to easily bookmark pages.

## [2.3.0] - 2026-06-25

### Added
- **KaraKeep Bookmark Integration:** Added opt-in integration with self-hosted [KaraKeep](https://karakeep.app/) instances for saving web bookmarks via the bot. Configured through `KARAKEEP_URL`, `KARAKEEP_API_KEY`, and optional `KARAKEEP_TAGS` environment variables. When enabled, the bot administrator can use `/keep <url>` or reply `/keep` to any message containing a link to save it to KaraKeep. The bookmark is saved via the KaraKeep REST API (`POST /api/v1/bookmarks`) with optional tags and the bot reports the result with a direct link to the saved bookmark.

## [2.2.7] - 2026-06-24

### Fixed
- **URL Validation Enhancements:** Improved `_is_internal_or_invalid_url` to filter out malformed URLs with hostnames containing no dots (e.g. `https://юрл`, `http://test`), unless they represent `localhost` or valid IP addresses.
- **Log Noise Reduction:** Silenced warning log messages for bracketed invalid hostname/IP syntax issues by handling `ValueError` separately and logging it as debug messages.

## [2.2.6] - 2026-06-23


### Added
- **Manual Invidious Domain Management:** Added admin commands `/invidious_add <domain/url>`, `/invidious_rm <domain/url>`, and `/invidious_list` to allow manually registering, deregistering, and listing Invidious instances. This ensures video links can still be rewritten and forwarded to `YT Bot` even if the Invidious server blocks the WebPreview Bot from auto-detecting it (e.g. returning `418 I'm a teapot` or Cloudflare checks).

## [2.2.5] - 2026-06-23


### Added
- **Invidious Detection and YT Bot Redirection:** Automatically identifies Invidious instances (alternative YouTube front-ends) by checking description metadata. When `YT Bot` is present in the chat, it intercepts these links, extracts the video ID, rewrites them to `youtu.be` links, and forwards them to the chat, completely skipping WebPreview generation.
- **Invidious Domain Learning Cache:** Implemented a self-learning database cache that remembers detected Invidious instance domains. On subsequent requests, it immediately identifies the instance and redirects video links to `YT Bot` without making any network calls.

## [2.2.4] - 2026-06-23

### Added
- **Jina.ai Fallback Support:** Integrated Jina AI Reader (`r.jina.ai`) to resolve webpage titles, preview images, and markdown text content when standard fetching or readability extraction fails or gets blocked (e.g. by anti-bot checks).
- **Markdown-to-HTML Translator:** Implemented a placeholder-based markdown-to-HTML parser that formats Jina's markdown output into styled HTML. It protects underscores inside URLs from being parsed as italic markdown.
- **Privacy & Safety Image Filtering:** In readability previews, any images that fail to download/inline (such as analytics tracking pixels, ads, or broken links) are automatically stripped from the HTML, preventing data leakage and broken images.

## [2.2.3] - 2026-06-17

### Added
- **Image Compression Logging:** Log messages for compressed images (both OG banner images and monolith HTML files) now print the original and compressed sizes in parentheses (e.g. `(120 KB -> 45 KB)`), providing better visibility into optimization efficiency.

## [2.2.2] - 2026-06-16

### Added
- **Automatic Transport Failover:** Implemented a robust, event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically switches `configured_addr` to the next configured backup transport, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.

### Fixed
- **E2E Failover Loop & Key Fallback**:
  - Added fallback support for both `chat_id` and `chatId` keys in message snapshots to prevent `chat 'Unknown' (ID: None)` errors.
  - Downgraded permanent E2E and resend logs to `WARNING`.
  - Removed administrative failover alert messages completely, relying entirely on structured logging to prevent any potential loop risks.


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
