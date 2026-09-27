// POST {code} or {refresh_token} → Notion's token endpoint with the client secret. Response passed through.
import type { VercelRequest, VercelResponse } from "@vercel/node";
import { NOTION_TOKEN_URL, brokerEnv, sendError } from "../../lib/notion.js";

export default async function handler(req: VercelRequest, res: VercelResponse): Promise<void> {
  if (req.method !== "POST") return sendError(res, 405, "method not allowed");
  const body: unknown = req.body;
  if (typeof body !== "object" || body === null) return sendError(res, 400, "expected a JSON body");
  const { code, refresh_token: refreshToken } = body as Record<string, unknown>;

  const env = brokerEnv();
  if (!env) return sendError(res, 500, "broker not configured");

  let grant: Record<string, string>;
  if (typeof code === "string" && code) {
    grant = { grant_type: "authorization_code", code, redirect_uri: env.redirectUri };
  } else if (typeof refreshToken === "string" && refreshToken) {
    grant = { grant_type: "refresh_token", refresh_token: refreshToken };
  } else {
    return sendError(res, 400, "expected code or refresh_token");
  }

  const basic = Buffer.from(`${env.clientId}:${env.clientSecret}`).toString("base64");
  let upstream: Response;
  try {
    upstream = await fetch(NOTION_TOKEN_URL, {
      method: "POST",
      headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/json" },
      body: JSON.stringify(grant),
    });
  } catch {
    console.log(`token ${grant.grant_type}: upstream unreachable`);
    return sendError(res, 502, "could not reach Notion");
  }
  // Status only — the body carries the access token.
  console.log(`token ${grant.grant_type}: notion ${upstream.status}`);
  res.status(upstream.status)
    .setHeader("Cache-Control", "no-store")
    .setHeader("Content-Type", upstream.headers.get("content-type") ?? "application/json")
    .send(await upstream.text());
}
