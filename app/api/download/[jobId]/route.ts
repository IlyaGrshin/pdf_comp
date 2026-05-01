import { createReadStream, promises as fs } from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { deleteJob, jobDirOf, jobIsExpired } from "@/lib/job-fs";

export const runtime = "nodejs";

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await ctx.params;
  const dir = jobDirOf(jobId);
  if (!dir) {
    return Response.json({ error: "EXPIRED" }, { status: 404 });
  }
  if (await jobIsExpired(dir)) {
    await deleteJob(dir).catch(() => undefined);
    return Response.json({ error: "EXPIRED" }, { status: 404 });
  }

  // The compress pipeline writes final.pdf on success; on no-benefit it's
  // removed and we serve the original input.pdf instead.
  const stat = await statFirstExisting([
    path.join(dir, "final.pdf"),
    path.join(dir, "input.pdf"),
  ]);
  if (!stat) {
    return Response.json({ error: "EXPIRED" }, { status: 404 });
  }

  const nodeStream = createReadStream(stat.path);
  nodeStream.once("close", () => {
    deleteJob(dir).catch(() => undefined);
  });

  return new Response(Readable.toWeb(nodeStream) as ReadableStream<Uint8Array>, {
    status: 200,
    headers: {
      "content-type": "application/pdf",
      "content-length": String(stat.size),
      "content-disposition": 'attachment; filename="compressed.pdf"',
      "cache-control": "no-store",
    },
  });
}

async function statFirstExisting(
  paths: string[],
): Promise<{ path: string; size: number } | null> {
  for (const p of paths) {
    try {
      const s = await fs.stat(p);
      return { path: p, size: s.size };
    } catch {
      // try next
    }
  }
  return null;
}
