// Shared config and checks for the Notion OAuth broker. Stateless: nothing is stored.
import type { VercelResponse } from "@vercel/node";

export const NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize";
export const NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token";

// "<daemon port>.<nonce>" — the daemon makes it; the port is where the callback goes.
const STATE_RE = /^\d{2,5}\.[A-Za-z0-9_-]{16,}$/;

export function parseState(state: unknown): { port: number } | null {
  if (typeof state !== "string" || !STATE_RE.test(state)) return null;
  const port = Number(state.split(".", 1)[0]);
  return port >= 1 && port <= 65535 ? { port } : null;
}

export interface BrokerEnv {
  clientId: string;
  clientSecret: string;
  redirectUri: string;
}

export function brokerEnv(): BrokerEnv | null {
  const clientId = process.env.NOTION_CLIENT_ID ?? "";
  const clientSecret = process.env.NOTION_CLIENT_SECRET ?? "";
  const redirectUri = process.env.NOTION_REDIRECT_URI ?? "";
  if (!clientId || !clientSecret || !redirectUri) return null;
  return { clientId, clientSecret, redirectUri };
}

export function sendError(res: VercelResponse, status: number, message: string): void {
  res.status(status).setHeader("Cache-Control", "no-store").json({ error: message });
}

export function firstParam(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}
