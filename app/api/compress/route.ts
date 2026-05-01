import { createWriteStream } from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { ReadableStream as NodeReadableStream } from "node:stream/web";
import { z } from "zod";
import { PRESETS, type PresetId, isPresetId } from "@/lib/presets";
import { compress, SubprocessError, SubprocessTimeoutError } from "@/lib/compress";
import { compressionLimit } from "@/lib/concurrency";
import { createJob, deleteJob, startSweeper } from "@/lib/job-fs";
import { validatePdf } from "@/lib/validate-pdf";
import type { ErrorCode } from "@/lib/errors";

export const runtime = "nodejs";
export const maxDuration = 900;

const MAX_BYTES = 1024 * 1024 * 1024;
const MAX_QUEUE_DEPTH = 1;

startSweeper();

const presetSchema = z.string().refine(isPresetId, { message: "INVALID_PRESET" });

function err(code: ErrorCode, status: number, init?: ResponseInit): Response {
  return Response.json({ error: code }, { status, ...init });
}

export async function POST(req: Request) {
  const contentLength = Number(req.headers.get("content-length") ?? 0);
  if (contentLength > MAX_BYTES) {
    return err("FILE_TOO_LARGE", 413);
  }

  if (compressionLimit.activeCount >= 2 && compressionLimit.pendingCount >= MAX_QUEUE_DEPTH) {
    return err("BUSY", 503, { headers: { "retry-after": "30" } });
  }

  let formData: FormData;
  try {
    formData = await req.formData();
  } catch {
    return err("INVALID_PDF", 400);
  }

  const file = formData.get("file");
  const presetRaw = formData.get("preset");

  if (!(file instanceof File) || file.size === 0) {
    return err("MISSING_FILE", 400);
  }
  if (file.size > MAX_BYTES) {
    return err("FILE_TOO_LARGE", 413);
  }

  const presetParse = presetSchema.safeParse(presetRaw);
  if (!presetParse.success) {
    return err("INVALID_PRESET", 400);
  }
  const presetId = presetParse.data as PresetId;
  const preset = PRESETS[presetId];

  const job = await createJob();
  const inputPath = path.join(job.dir, "input.pdf");

  try {
    // Stream the upload to disk; arrayBuffer() would hold a 1 GB file twice.
    await pipeline(
      Readable.fromWeb(file.stream() as unknown as NodeReadableStream<Uint8Array>),
      createWriteStream(inputPath),
    );

    const validationError = await validatePdf(inputPath);
    if (validationError) {
      await deleteJob(job.dir);
      return err(validationError, 400);
    }

    const result = await compressionLimit(() =>
      compress({ inputPath, jobDir: job.dir, preset }),
    );

    return Response.json({
      jobId: job.id,
      preset: presetId,
      originalBytes: result.originalBytes,
      compressedBytes: result.compressedBytes,
      ratio: result.ratio,
      noBenefit: result.noBenefit,
      durationMs: result.durationMs,
      downloadUrl: `/api/download/${job.id}`,
    });
  } catch (e) {
    await deleteJob(job.dir).catch(() => undefined);
    if (e instanceof SubprocessTimeoutError) {
      return err("COMPRESS_TIMEOUT", 504);
    }
    if (e instanceof SubprocessError) {
      console.error("[compress] subprocess failed", e.exitCode, e.stderrTail);
      return err("COMPRESS_FAILED", 500);
    }
    console.error("[compress] internal", e);
    return err("INTERNAL", 500);
  }
}
