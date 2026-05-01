import { LIMITS } from "@/lib/runtime-limits";
import { Home } from "./home";

// Server Component shell — passes the host-aware upload limit into the
// client tree so the first paint already shows the correct "до X МБ" instead
// of the client first rendering a fallback then snapping to the real value.
export default function Page() {
  return <Home maxBytes={LIMITS.maxBytes} />;
}
