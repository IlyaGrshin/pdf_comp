"use client";

import { useDropzone, type FileRejection } from "react-dropzone";
import { useCallback } from "react";
import { Upload, FileText, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

type Props = {
  file: File | null;
  maxBytes: number;
  disabled?: boolean;
  onFileChange: (file: File | null) => void;
  onTooLarge?: () => void;
  onWrongType?: () => void;
};

export function Uploader({ file, maxBytes, disabled, onFileChange, onTooLarge, onWrongType }: Props) {
  const onDrop = useCallback(
    (accepted: File[], rejections: FileRejection[]) => {
      if (rejections.length > 0) {
        const codes = rejections[0]?.errors.map((e) => e.code) ?? [];
        if (codes.includes("file-too-large")) onTooLarge?.();
        else onWrongType?.();
        return;
      }
      const next = accepted[0];
      if (next) onFileChange(next);
    },
    [onFileChange, onTooLarge, onWrongType],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: false,
    accept: { "application/pdf": [".pdf"] },
    maxSize: maxBytes,
    disabled: disabled || file !== null,
  });

  if (file) {
    return (
      <div className="flex items-center gap-3 rounded-xl border bg-muted/40 px-3 py-3">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <FileText className="size-5" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{file.name}</p>
          <p className="text-xs text-muted-foreground">{formatBytes(file.size)}</p>
        </div>
        {!disabled && (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Remove file"
            onClick={() => onFileChange(null)}
          >
            <X />
          </Button>
        )}
      </div>
    );
  }

  return (
    <div
      {...getRootProps()}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors",
        "border-border/70 bg-muted/30 hover:bg-muted/50",
        isDragActive && "border-primary bg-primary/5",
        disabled && "pointer-events-none opacity-50",
      )}
    >
      <input {...getInputProps()} />
      <div className="flex size-12 items-center justify-center rounded-full bg-background ring-1 ring-border">
        <Upload className="size-5 text-muted-foreground" />
      </div>
      <p className="text-sm font-medium">
        {isDragActive ? "Drop the PDF" : "Drop a PDF or click to choose"}
      </p>
      <p className="text-xs text-muted-foreground">up to {formatBytes(maxBytes)}</p>
    </div>
  );
}
