import { createWriteStream } from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { ReadableStream as NodeReadableStream } from "node:stream/web";
import pLimit from "p-limit";
import { compress, SubprocessError, SubprocessTimeoutError } from "@/lib/compress";
import { createJob, deleteJob, holdJob, redactJobPaths, tmpBytes } from "@/lib/job-fs";
import { validatePdf } from "@/lib/validate-pdf";
import type { ErrorCode } from "@/lib/errors";
import {
  LIMITS,
  availableMemory,
  DISK_BUDGET_BYTES,
  JOB_DISK_RESERVE_BYTES,
  MAX_INFLIGHT_JOBS,
  MEMORY_PRESSURE_FLOOR,
} from "@/lib/runtime-limits";
import { BASE_PATH } from "@/lib/config";

export const runtime = "nodejs";
export const maxDuration = 900;

const compressionLimit = pLimit(LIMITS.concurrency);

class PayloadTooLargeError extends Error {}

function err(code: ErrorCode, status: number, init?: ResponseInit): Response {
  return Response.json({ error: code }, { status, ...init });
}

// Admission counters, held across the whole request rather than just the
// compression. See MAX_INFLIGHT_JOBS for why gating on `compressionLimit`
// alone let unbounded uploads through.
//
// The disk reserve is charged here, synchronously, before the request awaits
// anything: a budget check on its own is a snapshot, and every slot admitted
// against the same snapshot could then write its worst case on top of it,
// putting real usage a job or two past the supposed ceiling. Charging first
// means a concurrent request sees this one's claim rather than the disk it
// has not filled yet.
let inflightJobs = 0;
let reservedBytes = 0;

function acquireJobSlot(): (() => void) | null {
  if (inflightJobs >= MAX_INFLIGHT_JOBS) return null;
  inflightJobs += 1;
  reservedBytes += JOB_DISK_RESERVE_BYTES;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    inflightJobs -= 1;
    reservedBytes -= JOB_DISK_RESERVE_BYTES;
  };
}

export async function POST(req: Request) {
  const contentLength = Number(req.headers.get("content-length") ?? 0);
  if (contentLength > LIMITS.maxBytes) {
    return err("FILE_TOO_LARGE", 413);
  }

  const slot = acquireJobSlot();
  if (!slot) {
    return err("BUSY", 503, { headers: { "retry-after": "30" } });
  }

  try {
    // Refuse new work if the host is already memory-tight. Prevents an
    // already-OOM-adjacent host from getting tipped over by a fresh job.
    // availableMemory() returns null on platforms without /proc/meminfo
    // (macOS dev), in which case we skip the check.
    const avail = availableMemory();
    if (avail !== null && avail < MEMORY_PRESSURE_FLOOR) {
      return err("BUSY", 503, { headers: { "retry-after": "30" } });
    }

    // Same idea one resource over: finished jobs awaiting download hold disk
    // that no in-flight counter sees. Bytes already written by a live job are
    // counted twice — once here, once in its reservation — which errs toward
    // refusing work rather than overrunning the budget.
    if ((await tmpBytes()) + reservedBytes > DISK_BUDGET_BYTES) {
      return err("BUSY", 503, { headers: { "retry-after": "60" } });
    }

    if (!req.body) {
      return err("MISSING_FILE", 400);
    }

    return await runJob(req.body);
  } finally {
    slot();
  }
}

async function runJob(body: ReadableStream<Uint8Array>): Promise<Response> {
  const job = await createJob();
  const inputPath = path.join(job.dir, "input.pdf");
  const release = holdJob(job.dir);
  let receivedBytes = 0;

  try {
    // The client PUTs the PDF as the raw body rather than multipart/form-data,
    // because `req.formData()` buffers the whole part in memory before handing
    // back a File — measured at ~4.3x the upload size in RSS, which alone would
    // blow past MemoryMax on a 2 GB host well before compression starts. Piping
    // the body keeps peak memory flat regardless of file size.
    // mode: 0o600 keeps the uploaded PDF unreadable by other local users
    // (defense-in-depth alongside UMask=077 in the systemd unit).
    await pipeline(
      Readable.fromWeb(body as unknown as NodeReadableStream<Uint8Array>),
      // content-length is a claim, not a guarantee; count what actually
      // arrives and abort the moment it exceeds the cap.
      async function* (source) {
        for await (const chunk of source) {
          receivedBytes += (chunk as Uint8Array).byteLength;
          if (receivedBytes > LIMITS.maxBytes) throw new PayloadTooLargeError();
          yield chunk;
        }
      },
      createWriteStream(inputPath, { mode: 0o600 }),
    );

    if (receivedBytes === 0) {
      await deleteJob(job.dir);
      return err("MISSING_FILE", 400);
    }

    const validationError = await validatePdf(inputPath);
    if (validationError) {
      await deleteJob(job.dir);
      return err(validationError, 400);
    }

    const result = await compressionLimit(() =>
      compress({ inputPath, inputBytes: receivedBytes, jobDir: job.dir }),
    );

    return Response.json({
      jobId: job.id,
      originalBytes: result.originalBytes,
      compressedBytes: result.compressedBytes,
      ratio: result.ratio,
      noBenefit: result.noBenefit,
      durationMs: result.durationMs,
      downloadUrl: `${BASE_PATH}/api/download/${job.id}`,
    });
  } catch (e) {
    await deleteJob(job.dir).catch(() => undefined);
    if (e instanceof PayloadTooLargeError) {
      return err("FILE_TOO_LARGE", 413);
    }
    if (e instanceof SubprocessTimeoutError) {
      return err("COMPRESS_TIMEOUT", 504);
    }
    if (e instanceof SubprocessError) {
      // The tail is a pikepdf/Pillow traceback about someone's document, and
      // the landing page promises no content logging. Exit code always; the
      // tail only when an operator opts in for a debugging session.
      const detail =
        process.env.PDF_COMP_LOG_STDERR === "1"
          ? redactJobPaths(e.stderrTail)
          : "(stderr withheld — set PDF_COMP_LOG_STDERR=1 to log it)";
      console.error("[compress] subprocess failed", e.exitCode, detail);
      return err("COMPRESS_FAILED", 500);
    }
    console.error(
      "[compress] internal",
      redactJobPaths(e instanceof Error ? (e.stack ?? `${e.name}: ${e.message}`) : String(e)),
    );
    return err("INTERNAL", 500);
  } finally {
    // From here the job is either deleted or finished, and its mtime is the
    // time final.pdf appeared — so the 10-minute download window starts now.
    release();
  }
}
