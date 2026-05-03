"use client";

import { AlertCircle, RotateCcw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ErrorCode } from "@/lib/errors";

const MESSAGES: Record<ErrorCode, { title: string; description: string }> = {
  FILE_TOO_LARGE: {
    title: "File too large",
    description: "The file is over the server limit. Try splitting the document into parts.",
  },
  INVALID_PDF: {
    title: "Not a PDF",
    description: "The file looks damaged or the format isn’t supported.",
  },
  PASSWORD_PROTECTED: {
    title: "Password-protected PDF",
    description: "Remove the password and try again.",
  },
  INVALID_PRESET: {
    title: "Unknown compression mode",
    description: "Refresh the page and try again.",
  },
  MISSING_FILE: {
    title: "No file selected",
    description: "Drop a PDF into the upload area and try again.",
  },
  BUSY: {
    title: "Service busy",
    description: "Too many concurrent requests. Try again in a minute.",
  },
  COMPRESS_FAILED: {
    title: "Couldn’t process the PDF",
    description: "The file may be non-standard or damaged. Try a different PDF.",
  },
  COMPRESS_TIMEOUT: {
    title: "Took too long",
    description: "This file is more complex than usual. Try a smaller PDF.",
  },
  EXPIRED: {
    title: "Link expired",
    description: "Upload the PDF again — files are kept for at most 10 minutes.",
  },
  INTERNAL: {
    title: "Something went wrong",
    description: "Internal server error. Try again in a minute.",
  },
  NETWORK: {
    title: "No connection to server",
    description: "Check your internet connection and try again.",
  },
};

type Props = {
  code: ErrorCode;
  onRetry: () => void;
};

export function ErrorBanner({ code, onRetry }: Props) {
  const { title, description } = MESSAGES[code];
  return (
    <div className="flex flex-col gap-3">
      <Alert variant="destructive">
        <AlertCircle />
        <AlertTitle>{title}</AlertTitle>
        <AlertDescription>{description}</AlertDescription>
      </Alert>
      <Button variant="outline" size="lg" className="h-11 self-start" onClick={onRetry}>
        <RotateCcw />
        Try again
      </Button>
    </div>
  );
}
