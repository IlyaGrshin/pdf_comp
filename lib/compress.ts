import { spawn, type ChildProcess } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import type { Preset } from "./presets";

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

export type CompressOptions = {
  inputPath: string;
  jobDir: string;
  preset: Preset;
  timeoutMs?: number;
};

export type CompressResult = {
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

  const pythonBin = path.join(process.cwd(), ".venv", "bin", "python");
  const scriptPath = path.join(process.cwd(), "scripts", "recompress.py");

  const originalStatPromise = fs.stat(opts.inputPath);

  await runProcess(
    pythonBin,
    [
      scriptPath,
      opts.inputPath,
      finalPath,
      String(args.colorQuality),
      String(args.grayQuality),
      String(args.maxLongEdge),
    ],
    timeoutMs,
  );

  const [originalStat, finalStat] = await Promise.all([
    originalStatPromise,
    fs.stat(finalPath),
  ]);
  const originalBytes = originalStat.size;
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

export function runProcess(
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
