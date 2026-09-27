// GET ?code=&state= (or ?error=) from Notion → 302 to the daemon on this machine's loopback.
import type { VercelRequest, VercelResponse } from "@vercel/node";
import { firstParam, parseState, sendError } from "../../lib/notion.js";

export default function handler(req: VercelRequest, res: VercelResponse): void {
  if (req.method !== "GET") return sendError(res, 405, "method not allowed");
  const state = firstParam(req.query.state);
  const parsed = parseState(state);
  if (!parsed) return sendError(res, 400, "bad state");

  // Host is fixed to loopback; only the port comes from the (validated) state.
  const url = new URL(`http://127.0.0.1:${parsed.port}/v1/integrations/notion/callback`);
  for (const key of ["code", "error"] as const) {
    const value = firstParam(req.query[key]);
    if (value) url.searchParams.set(key, value);
  }
  url.searchParams.set("state", state!);
  res.setHeader("Cache-Control", "no-store");
  res.redirect(302, url.toString());
}
