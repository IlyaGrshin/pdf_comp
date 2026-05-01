import os from "node:os";

// Empirical per-job RAM peak — pikepdf decodes images via Pillow into RAM,
// LANCZOS resize temporarily holds source + destination. Worst case observed
// on 4650×6370 RGB images is ~700 MB; round to 800 for safety margin.
const PER_JOB_BUDGET = 800 * 1024 * 1024;

// OS + Node + Python subprocess baseline RSS reserve.
const BASELINE_RESERVE = 500 * 1024 * 1024;

// Hard ceilings regardless of host size — bigger files / more concurrency
// stop being useful for our use case.
const HARD_FILE_CAP = 1024 * 1024 * 1024; // 1 GB
const HARD_CONCURRENCY_CAP = 4;

// Operator escape hatch: `MAX_RAM_BYTES=1500000000` to lie to Node about
// how much memory it has (useful when Node's container memory detection
// doesn't kick in).
function effectiveTotalMem(): number {
  const override = process.env.MAX_RAM_BYTES;
  if (override) {
    const n = parseInt(override, 10);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return os.totalmem();
}

function cpuCount(): number {
  return typeof os.availableParallelism === "function"
    ? os.availableParallelism()
    : os.cpus().length;
}

function compute() {
  const totalMem = effectiveTotalMem();
  const cores = cpuCount();

  const memBudget = Math.max(0, totalMem - BASELINE_RESERVE);
  const memSlots = Math.max(1, Math.floor(memBudget / PER_JOB_BUDGET));
  const concurrency = Math.min(memSlots, cores, HARD_CONCURRENCY_CAP);

  // Single-file ceiling: leave ~4× headroom for Pillow decode + resize peak.
  const maxBytes = Math.min(HARD_FILE_CAP, Math.floor(totalMem / 4));

  return { concurrency, maxBytes, totalMem, cores };
}

export const LIMITS = compute();
