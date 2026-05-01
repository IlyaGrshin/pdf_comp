import { promises as fs } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

const ROOT = path.join(process.cwd(), "tmp");
const JOB_TTL_MS = 10 * 60 * 1000;
const SWEEP_INTERVAL_MS = 5 * 60 * 1000;
const JOB_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export type Job = {
  id: string;
  dir: string;
};

export async function createJob(): Promise<Job> {
  await fs.mkdir(ROOT, { recursive: true });
  const id = randomUUID();
  const dir = path.join(ROOT, id);
  await fs.mkdir(dir);
  return { id, dir };
}

export function jobDirOf(jobId: string): string | null {
  if (!JOB_ID_RE.test(jobId)) return null;
  return path.join(ROOT, jobId);
}

export async function jobIsExpired(jobDir: string): Promise<boolean> {
  try {
    const stat = await fs.stat(jobDir);
    return Date.now() - stat.mtimeMs > JOB_TTL_MS;
  } catch {
    return true;
  }
}

export async function deleteJob(jobDir: string): Promise<void> {
  await fs.rm(jobDir, { recursive: true, force: true });
}

let sweeperStarted = false;

export function startSweeper(): void {
  if (sweeperStarted) return;
  sweeperStarted = true;
  const tick = async () => {
    try {
      const entries = await fs.readdir(ROOT).catch(() => [] as string[]);
      const now = Date.now();
      await Promise.all(
        entries.map(async (entry) => {
          const dir = path.join(ROOT, entry);
          try {
            const stat = await fs.stat(dir);
            if (now - stat.mtimeMs > JOB_TTL_MS) {
              await fs.rm(dir, { recursive: true, force: true });
            }
          } catch {
            // ignore — entry already removed or unreadable
          }
        }),
      );
    } catch {
      // never let the sweeper crash the process
    }
  };
  setInterval(tick, SWEEP_INTERVAL_MS).unref();
  tick();
}
