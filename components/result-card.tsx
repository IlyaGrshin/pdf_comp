"use client";

import { Download, Check, Info, RotateCcw } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { formatBytes, formatRatio, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

export type CompressResponse = {
  jobId: string;
  preset: string;
  originalBytes: number;
  compressedBytes: number;
  ratio: number;
  noBenefit: boolean;
  durationMs: number;
  downloadUrl: string;
};

type Props = {
  result: CompressResponse;
  fileName: string;
  onReset: () => void;
};

export function ResultCard({ result, fileName, onReset }: Props) {
  const downloadName = makeDownloadName(fileName);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 text-sm text-emerald-600">
        <div className="flex size-5 items-center justify-center rounded-full bg-emerald-500/15">
          <Check className="size-3.5" />
        </div>
        <span className="font-medium">
          {result.noBenefit ? "Готово" : `Сжато на ${formatRatio(result.ratio)}`}
        </span>
      </div>

      {result.noBenefit ? (
        <Alert>
          <Info />
          <AlertTitle>Уже хорошо сжат</AlertTitle>
          <AlertDescription>
            Дополнительное сжатие не дало выигрыша — возвращаем оригинальный файл.
          </AlertDescription>
        </Alert>
      ) : (
        <ResultBars
          originalBytes={result.originalBytes}
          compressedBytes={result.compressedBytes}
        />
      )}

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>Обработано за {formatDuration(result.durationMs)}</span>
        {!result.noBenefit && (
          <span>
            экономия {formatBytes(result.originalBytes - result.compressedBytes)}
          </span>
        )}
      </div>

      <div className="flex flex-col gap-2 sm:flex-row">
        <a
          href={result.downloadUrl}
          download={downloadName}
          className={cn(buttonVariants({ size: "lg" }), "h-11 flex-1 text-sm")}
        >
          <Download />
          Скачать PDF
        </a>
        <Button variant="outline" size="lg" className="h-11" onClick={onReset}>
          <RotateCcw />
          Сжать другой
        </Button>
      </div>
    </div>
  );
}

function ResultBars({
  originalBytes,
  compressedBytes,
}: {
  originalBytes: number;
  compressedBytes: number;
}) {
  const compressedWidth = Math.max(4, (compressedBytes / originalBytes) * 100);
  return (
    <div className="grid gap-3">
      <Row label="Было" bytes={originalBytes} widthPct={100} tone="muted" />
      <Row label="Стало" bytes={compressedBytes} widthPct={compressedWidth} tone="primary" />
    </div>
  );
}

function Row({
  label,
  bytes,
  widthPct,
  tone,
}: {
  label: string;
  bytes: number;
  widthPct: number;
  tone: "muted" | "primary";
}) {
  return (
    <div className="grid gap-1">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium tabular-nums">{formatBytes(bytes)}</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={
            tone === "primary"
              ? "h-full rounded-full bg-primary transition-[width] duration-700"
              : "h-full rounded-full bg-foreground/30"
          }
          style={{ width: `${widthPct}%` }}
        />
      </div>
    </div>
  );
}

function makeDownloadName(originalName: string): string {
  const base = originalName.replace(/\.pdf$/i, "");
  return `${base}-compressed.pdf`;
}
