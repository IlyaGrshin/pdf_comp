import { promises as fs } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

const ROOT = path.join(process.cwd(), "tmp");
const JOB_TTL_MS = 10 * 60 * 1000;
const SWEEP_INTERVAL_MS = 5 * 60 * 1000;
const JOB_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

type Job = {
  id: string;
  dir: string;
};

export async function createJob(): Promise<Job> {
  // 0700 on the ROOT and per-job dirs — uploaded PDFs must not be
  // readable by other local users on the host. systemd `UMask=077`
  // enforces the same at the process level; this belt-and-suspenders
  // survives dev environments and non-systemd hosts.
  await fs.mkdir(ROOT, { recursive: true, mode: 0o700 });
  const id = randomUUID();
  const dir = path.join(ROOT, id);
  await fs.mkdir(dir, { mode: 0o700 });
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

// Job dirs with a request still working on them. The sweeper ages dirs by
// mtime, and a directory's mtime is stamped when an entry is created inside
// it — i.e. when input.pdf is opened, at the *start* of the upload. A slow
// upload plus a long compression can therefore cross JOB_TTL_MS while the
// Python subprocess is still writing, and the sweeper would delete the
// directory out from under it. Holding the dir for the request's lifetime
// keeps the 10-minute promise for finished jobs without racing live ones.
const heldJobs = new Set<string>();

export function holdJob(jobDir: string): () => void {
  heldJobs.add(jobDir);
  return () => {
    heldJobs.delete(jobDir);
  };
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
          if (heldJobs.has(dir)) return;
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
