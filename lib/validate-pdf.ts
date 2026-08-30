import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";

type ValidationError = "INVALID_PDF" | "PASSWORD_PROTECTED";

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

// Spawn failures are answered with "no password" so a host missing qpdf still
// serves ordinary PDFs, but silence would make the difference between "checked,
// not encrypted" and "could not check" invisible — an encrypted file then fails
// later as COMPRESS_FAILED with nothing pointing at the real cause. Warn once.
let warnedQpdfUnavailable = false;

function qpdfRequiresPassword(filePath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const child = spawn("qpdf", ["--requires-password", filePath]);
    child.on("error", () => {
      if (!warnedQpdfUnavailable) {
        warnedQpdfUnavailable = true;
        console.error(
          "[validate] qpdf could not be run — encrypted-PDF detection is disabled on this host",
        );
      }
      resolve(false);
    });
    child.on("close", (code) => {
      // qpdf exit codes:
      //   0 — file requires a password
      //   2 — file is not encrypted
      //   3 — encrypted but empty password works (owner-locked) — we treat as OK
      // Accepting 3 means the output comes back decrypted: pikepdf opens such a
      // file with the empty user password and saves it unencrypted, so whatever
      // the owner password was restricting (printing, extraction) is gone from
      // the result. Kept deliberately — the uploader already holds the file and
      // rejecting it would refuse a document its owner can legitimately open —
      // but it is a real property of the service, not an oversight.
      resolve(code === 0);
    });
  });
}
