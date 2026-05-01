import { LIMITS } from "@/lib/runtime-limits";

export const runtime = "nodejs";

export function GET() {
  return Response.json(
    { maxBytes: LIMITS.maxBytes },
    { headers: { "cache-control": "public, max-age=60" } },
  );
}
