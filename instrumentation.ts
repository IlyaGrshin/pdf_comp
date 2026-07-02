// Runs once on server boot in every runtime. We use it to prime the
// filesystem sweeper — otherwise `preloadEntriesOnStart: false` would
// defer sweeper startup until the first POST /api/compress, and job
// dirs written before that first request could outlive the promised
// 10-minute deletion window. See lib/job-fs.ts.
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { startSweeper } = await import("./lib/job-fs");
    startSweeper();
  }
}
