# TeleStore

<img src="./imgs/ShaFace.png" alt="TeleStore icon" height="270" />

Self-hosted AltStore / SideStore / LiveContainer repository that streams IPA files from Telegram channels.

No IPA files are stored in this repo. The local server reads Telegram with your own account session and streams files.

> [!NOTE]
> If you are looking to have an IPA repository from Github repos, check out [GithubStore](https://github.com/yazdipour/GithubStore).

> [!IMPORTANT]
> Don't know where to find Telegram channels with IPAs? Check out [this collection](https://gist.github.com/ongkiii/b40620d8d4a98ab17642858dce4cb2ec).

## Screenshot of the repo in SideStore and LiveContainer:

![SideStore screenshot](./imgs/scrn2.png)
![LiveContainer screenshot](./imgs/scrn.png)

## Quick Setup

1. Create Telegram API credentials at https://my.telegram.org/apps.
2. Create `config.yml`: `cp config.example.yml config.yml`

3. Edit `config.yml` and set:

```yaml
telegram:
  api_id: 123456
  api_hash: your_api_hash
  session: /data/telegram.session
  limit: 100

server:
  base_url: http://localhost:8080
  ui_config: true
  cache_seconds: 600
  ipa_cache_dir: /data/ipa-cache
  ipa_cache_workers: 4
  ipa_cache_global_workers: 8
  ipa_cache_part_size: 8388608

channels:
  - blatants
  - dvntms
```

4. Create `docker-compose.yml`:

```yaml
services:
  app:
    image: ghcr.io/yazdipour/telestore:latest
    ports:
      - "8080:8080" # if port 8080 is in use, change to another port, for example "9090:8080", and set server.base_url to http://localhost:9090
    volumes:
      - telegram-session:/data
      - ./config.yml:/app/config.yml
    restart: unless-stopped
volumes:
  telegram-session:
```


5. Start the server:

```bash
docker compose up -d
```

First run starts even without a Telegram session. Open this URL and log in: `http://localhost:8080/login`

The session is saved in the `telegram-session` Docker volume, so later `docker compose up` runs skip login.

### IPA Repo URLs

Each configured channel gets its own source JSON, named from the source slug:

```text
http://localhost:8080/blatants.json
http://localhost:8080/dvntms.json
```

On an iPhone on the same Wi-Fi, set `server.base_url` to the reachable URL. For example, if your computer LAN IP is `192.168.1.50` and Docker maps host port `8080`:
 `http://192.168.1.50:8080/blatants.json`.

## Configuration

### Download Cache

```yaml
server:
  cache_seconds: 600
  ipa_cache_dir: /data/ipa-cache
  ipa_cache_workers: 4
  ipa_cache_global_workers: 8
  ipa_cache_part_size: 8388608
```

Source JSON is cached for `server.cache_seconds`. IPA downloads are cached under `server.ipa_cache_dir` after the first request. Range requests then serve from local disk instead of re-fetching the same bytes from Telegram. On cold range requests, `server.ipa_cache_workers` downloads cache parts concurrently using `server.ipa_cache_part_size` byte chunks. `server.ipa_cache_global_workers` caps total Telegram cache downloads across files.

### Config UI

The channel editor is enabled by default. To disable editing, set:

```yaml
server:
  ui_config: false
```

Open `http://localhost:8080` to add or remove channels and copy each channel source URL. New channels are saved as plain strings, use the Telegram handle as the display name, use a deterministic random tint color, and use the Telegram channel photo as the icon.

![Config UI screenshot](./imgs/config.jpg)

## Developer Setup

Use local build when changing source:

```bash
cp config.example.yml config.yml
docker compose -f docker-compose.local.yml up --build
```

## AI Acknowledgment

This project was built with the assistance of AI tools for code generation and refactoring.

## License

MIT License. See [LICENSE](./LICENSE) for details.
