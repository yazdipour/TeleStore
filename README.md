# TeleStore

<img src="./imgs/ShaFace.png" alt="TeleStore icon" height="270" />

Self-hosted AltStore / SideStore / LiveContainer repository that streams IPA files from Telegram channels.

No IPA files are stored in this repo. The local server reads Telegram with your own account session and streams files.

> [!NOTE]
> If you are looking to have an IPA repository from Github repos, check out [GithubStore](https://github.com/yazdipour/GithubStore).

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

source:
  name: TeleStore
  subtitle: Telegram-backed IPA source
  description: Self-hosted AltStore source that streams IPA files from Telegram.
  tint_color: "#1D9BF0"
  cache_seconds: 600

channels:
  - channel: blatants
    name: Blatants
    slug: blatants
    tint_color: "#1D9BF0"
    icon: imgs/ICON-120-blue.png

  - channel: dvntms
    name: DVNTMS
    slug: dvntms
    tint_color: "#8B5CF6"
    icon: imgs/ICON-120-green.png
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
      - ./config.yml:/app/config.yml:ro
    restart: unless-stopped
volumes:
  telegram-session:
```


5. Start:

```bash
docker compose up -d
```

First run starts even without a Telegram session. Open this URL and log in:

```text
http://localhost:8080/login
```

The session is saved in the `telegram-session` Docker volume, so later `docker compose up` runs skip login.

### IPA Repo URLs

Each configured channel gets its own source JSON, named from the source slug:

```text
http://localhost:8080/blatants.json
http://localhost:8080/dvntms.json
```

The legacy first-source URL still works: `http://localhost:8080/source.json`

On an iPhone on the same Wi-Fi, set `server.base_url` to the reachable URL. For example, if your computer LAN IP is `192.168.1.50` and Docker maps host port `8080`:
 `http://192.168.1.50:8080/blatants.json`.

### Multiple Channels

Use the `channels` array in `config.yml` to configure multiple channels:

```yaml
channels:
  - channel: blatants
    name: Blatants
    slug: blatants
    tint_color: "#1D9BF0"
    icon: imgs/ICON-120-blue.png

  - channel: dvntms
    name: DVNTMS
    slug: dvntms
    tint_color: "#8B5CF6"
    icon: imgs/ICON-120-green.png
```

The app creates one repository per channel. By default, each repo is served at `/{source-name-slug}.json`, for example `/blatants.json`.

Optional per-source display overrides live on the channel entry:

```yaml
channels:
  - channel: blatants
    name: Blatants
    slug: blatants
    subtitle: Telegram-backed IPA source
    description: Self-hosted source for the Blatants channel.
    tint_color: "#1D9BF0"
    icon: imgs/ICON-120-blue.png

  - channel: dvntms
    name: DVNTMS
    slug: dvntms
    tint_color: "#8B5CF6"
    icon: imgs/ICON-120-green.png
```

The `icon` path is optional. If omitted, the source uses the default `/source-icon.png`.

To change the host port, edit `docker-compose.yml`, for example `9090:8080`, and set `server.base_url` to `http://localhost:9090` or your LAN URL.

## Developer Setup

Use local build when changing source:

```bash
cp config.example.yml config.yml
docker compose -f docker-compose.local.yml up --build
```

## AI Acknowledgment

This project was built with the assistance of AI tools for code generation and refactoring.

## License MIT License. 

See [LICENSE](./LICENSE) for details.
