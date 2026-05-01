import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";

export type ValidationError = "INVALID_PDF" | "PASSWORD_PROTECTED";

export async function validatePdf(filePath: string): Promise<ValidationError | null> {
  const handle = await fs.open(filePath, "r");
  try {
    const buf = Buffer.alloc(5);
    await handle.read(buf, 0, 5, 0);
    if (buf.toString("ascii") !== "%PDF-") return "INVALID_PDF";
  } finally {
    await handle.close();
  }

  const requiresPassword = await qpdfRequiresPassword(filePath);
  if (requiresPassword) return "PASSWORD_PROTECTED";

  return null;
}

function qpdfRequiresPassword(filePath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const child = spawn("qpdf", ["--requires-password", filePath]);
    child.on("error", () => resolve(false));
    child.on("close", (code) => {
      // qpdf exit codes:
      //   0 — file requires a password
      //   2 — file is not encrypted
      //   3 — encrypted but empty password works (owner-locked) — we treat as OK
      resolve(code === 0);
    });
  });
}
