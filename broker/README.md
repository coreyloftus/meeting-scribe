# meeting-scribe-broker

Stateless Notion OAuth broker for Meeting Scribe. It holds the Notion public
integration's client secret so the app never ships it. It stores nothing: it
redirects the browser and proxies the token exchange.

| Route | What it does |
|---|---|
| `GET /api/notion/authorize?state=<port>.<nonce>` | 302 to Notion's consent page. |
| `GET /api/notion/callback?code=&state=` | 302 to `http://127.0.0.1:<port>/v1/integrations/notion/callback` (the local daemon). |
| `POST /api/notion/token` `{code}` or `{refresh_token}` | Exchanges with Notion using the client secret; returns Notion's response as-is. |

Logs contain status codes only — never request or response bodies.

## Environment variables

| Name | Value |
|---|---|
| `NOTION_CLIENT_ID` | OAuth client ID of the Notion public integration |
| `NOTION_CLIENT_SECRET` | OAuth client secret |
| `NOTION_REDIRECT_URI` | `https://<broker-host>/api/notion/callback` — must match a redirect URI registered in Notion |

## Deploy

```bash
cd broker
vercel link
vercel env add NOTION_CLIENT_ID
vercel env add NOTION_CLIENT_SECRET
vercel env add NOTION_REDIRECT_URI
vercel deploy --prod
```

Register these redirect URIs in the Notion integration:

- `https://<broker-host>/api/notion/callback`
- `http://localhost:3000/api/notion/callback` (local testing)

Then set `DEFAULT_BROKER_URL` in `meeting_scribe/config.py` to `https://<broker-host>`
(or `notion.broker_url` in `config.json`).

## Local

```bash
npm install
npm run typecheck
# .env with the three variables above, then:
vercel dev --local --listen 3000
```
