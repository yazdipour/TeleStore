# LiveBlatant

Self-hosted AltStore / SideStore / LiveContainer repository that streams IPA files from Blatants Telegram channel.

No IPA files are stored in this repo. The local server reads Telegram with your own account session and streams files when the Store requests `downloadURL`.

## Setup

1. Create Telegram API credentials at https://my.telegram.org/apps.
2. Create `.env`:

```bash
cp .env.example .env
```

3. Edit `.env` and set:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_CHANNEL=blatants
TELEGRAM_LIMIT=100
BASE_URL=http://localhost:8080
```

4. Choose how many recent channel posts to scan:

```env
TELEGRAM_LIMIT=100
```

5. Start:

```bash
docker compose up --build
```

First run starts even without a Telegram session. Open this URL and log in:

```text
http://localhost:8080/login
```

The session is saved in the `telegram-session` Docker volume, so later `docker compose up` runs skip login.

## AltStore URL

On the same computer:

```text
http://localhost:8080/source.json
```

On an iPhone on the same Wi-Fi, use your computer LAN IP:

```text
http://192.168.1.50:8080/source.json
```

Also set `BASE_URL` in `.env` to the same LAN URL before starting, so `downloadURL` values are reachable from the phone.

The app reads Telegram captions and file metadata. It can parse caption lines like:

```text
Bundle ID: com.example.app
Updated to: 1.2.3
Minimum iOS: 16.0
```

If caption parsing fails, increase `TELEGRAM_LIMIT` or improve the caption parser.

## Routes

- `GET /source.json` - AltStore source JSON.
- `GET /ipa/{message_id}/{filename}` - streams Telegram file to AltStore.
- `HEAD /ipa/{message_id}/{filename}` - returns file headers.
- `GET /health` - basic health check.
