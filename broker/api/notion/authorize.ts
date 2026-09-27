// GET ?state=<port>.<nonce> → 302 to Notion's consent page.
import type { VercelRequest, VercelResponse } from "@vercel/node";
import { NOTION_AUTHORIZE_URL, brokerEnv, firstParam, parseState, sendError } from "../../lib/notion.js";

export default function handler(req: VercelRequest, res: VercelResponse): void {
  if (req.method !== "GET") return sendError(res, 405, "method not allowed");
  const state = firstParam(req.query.state);
  if (!parseState(state)) return sendError(res, 400, "bad state");
  const env = brokerEnv();
  if (!env) return sendError(res, 500, "broker not configured");

  const url = new URL(NOTION_AUTHORIZE_URL);
  url.searchParams.set("client_id", env.clientId);
  url.searchParams.set("redirect_uri", env.redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("owner", "user");
  url.searchParams.set("state", state!);
  res.setHeader("Cache-Control", "no-store");
  res.redirect(302, url.toString());
}
