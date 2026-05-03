import { spawn, type ChildProcess } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import type { Preset } from "./presets";
import { LIMITS } from "./runtime-limits";

const DEFAULT_TIMEOUT_MS = 600_000;
const STDERR_CAP = 1024 * 1024;
const NO_BENEFIT_THRESHOLD = 0.95;

export class SubprocessError extends Error {
  constructor(public exitCode: number | null, public stderrTail: string) {
    super(`subprocess exited with code ${exitCode}`);
    this.name = "SubprocessError";
  }
}

export class SubprocessTimeoutError extends Error {
  constructor() {
    super("subprocess exceeded timeout");
    this.name = "SubprocessTimeoutError";
  }
}

type CompressOptions = {
  inputPath: string;
  inputBytes: number;
  jobDir: string;
  preset: Preset;
  timeoutMs?: number;
};

type CompressResult = {
  outputPath: string;
  originalBytes: number;
  compressedBytes: number;
  ratio: number;
  noBenefit: boolean;
  durationMs: number;
};

export async function compress(opts: CompressOptions): Promise<CompressResult> {
  const startedAt = Date.now();
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const finalPath = path.join(opts.jobDir, "final.pdf");
  const args = opts.preset.pikepdf;

  // turbopackIgnore: tells the Next.js build not to traverse .venv during
  // module-graph analysis. The directory contains symlinks pointing outside
  // the project root (system Python install), which Turbopack rejects.
  const pythonBin = path.join(/* turbopackIgnore: true */ process.cwd(), ".venv", "bin", "python");
  const scriptPath = path.join(process.cwd(), "scripts", "recompress.py");

  // Share CPU between concurrent jobs: each Python invocation gets at most
  // its fair slice. Single-job systems use everything.
  const workers = Math.max(1, Math.floor(LIMITS.cores / LIMITS.concurrency));

  await runProcess(
    pythonBin,
    [
      scriptPath,
      opts.inputPath,
      finalPath,
      String(args.colorQuality),
      String(args.grayQuality),
      String(args.maxLongEdge),
      String(workers),
    ],
    timeoutMs,
  );

  const finalStat = await fs.stat(finalPath);
  const originalBytes = opts.inputBytes;
  const compressedBytes = finalStat.size;
  const noBenefit = compressedBytes >= originalBytes * NO_BENEFIT_THRESHOLD;

  if (noBenefit) {
    await fs.rm(finalPath, { force: true });
  }

  return {
    outputPath: noBenefit ? opts.inputPath : finalPath,
    originalBytes,
    compressedBytes: noBenefit ? originalBytes : compressedBytes,
    ratio: noBenefit ? 0 : Math.max(0, 1 - compressedBytes / originalBytes),
    noBenefit,
    durationMs: Date.now() - startedAt,
  };
}

function runProcess(
  cmd: string,
  args: string[],
  timeoutMs: number,
  okExitCodes: readonly number[] = [0],
): Promise<void> {
  return new Promise((resolve, reject) => {
    const child: ChildProcess = spawn(cmd, args, { stdio: ["ignore", "ignore", "pipe"] });
    const stderrChunks: Buffer[] = [];
    let stderrLength = 0;
    let timedOut = false;

    const killTimer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);

    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderrLength >= STDERR_CAP) return;
      const remaining = STDERR_CAP - stderrLength;
      const slice = chunk.length <= remaining ? chunk : chunk.subarray(0, remaining);
      stderrChunks.push(slice);
      stderrLength += slice.length;
    });

    child.on("error", (err) => {
      clearTimeout(killTimer);
      reject(err);
    });

    child.on("close", (code) => {
      clearTimeout(killTimer);
      if (timedOut) {
        reject(new SubprocessTimeoutError());
        return;
      }
      if (code !== null && okExitCodes.includes(code)) {
        resolve();
        return;
      }
      const stderrTail = Buffer.concat(stderrChunks).toString("utf8").slice(-2000);
      reject(new SubprocessError(code, stderrTail));
    });
  });
}
