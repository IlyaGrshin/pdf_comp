import { LIMITS } from "@/lib/runtime-limits";
import { Home } from "./home";

// force-dynamic: LIMITS is computed from os.totalmem() at module load.
// Without this the page is prerendered at build time, so a build on a
// 16 GB laptop would bake the 1 GB dev cap into HTML while the API
// route (recomputed at runtime) actually enforces the 512 MB VPS cap.
// The client would then let users spend minutes uploading files that
// production immediately rejects.
export const dynamic = "force-dynamic";

// Server Component shell — passes the host-aware upload limit into the
// client tree so the first paint already shows the correct "up to X MB"
// instead of rendering a fallback then snapping to the real value.
export default function Page() {
  return <Home maxBytes={LIMITS.maxBytes} />;
}
