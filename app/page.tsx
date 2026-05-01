"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Uploader } from "@/components/uploader";
import { ResultCard, type CompressResponse } from "@/components/result-card";
import { ErrorBanner } from "@/components/error-banner";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { DEFAULT_PRESET } from "@/lib/presets";
import { ERROR_CODES, type ErrorCode } from "@/lib/errors";

type State =
  | { kind: "idle" }
  | { kind: "uploading"; progress: number }
  | { kind: "processing"; startedAt: number }
  | { kind: "done"; result: CompressResponse }
  | { kind: "error"; code: ErrorCode };

// Conservative pre-fetch default. Real value comes from /api/config and reflects
// the actual host's RAM (server-side computation in lib/runtime-limits.ts).
const FALLBACK_MAX_BYTES = 100 * 1024 * 1024;

export default function Page() {
  const [file, setFile] = useState<File | null>(null);
  const [state, setState] = useState<State>({ kind: "idle" });
  const [maxBytes, setMaxBytes] = useState(FALLBACK_MAX_BYTES);
  const xhrRef = useRef<XMLHttpRequest | null>(null);

  useEffect(() => {
    fetch("/api/config")
      .then((r) => r.json())
      .then((d: { maxBytes: number }) => {
        if (typeof d.maxBytes === "number" && d.maxBytes > 0) setMaxBytes(d.maxBytes);
      })
      .catch(() => undefined);
    return () => {
      xhrRef.current?.abort();
    };
  }, []);

  const isBusy = state.kind === "uploading" || state.kind === "processing";

  const startUpload = useCallback((picked: File) => {
    const fd = new FormData();
    fd.append("file", picked);
    fd.append("preset", DEFAULT_PRESET);

    const xhr = new XMLHttpRequest();
    xhrRef.current = xhr;
    xhr.open("POST", "/api/compress");
    xhr.responseType = "json";

    setState({ kind: "uploading", progress: 0 });

    xhr.upload.onprogress = (evt) => {
      if (evt.lengthComputable) {
        setState({ kind: "uploading", progress: evt.loaded / evt.total });
      }
    };
    xhr.upload.onload = () => {
      setState({ kind: "processing", startedAt: Date.now() });
    };
    xhr.onerror = () => {
      setState({ kind: "error", code: "NETWORK" });
    };
    xhr.onload = () => {
      const body = xhr.response as { error?: string } & Partial<CompressResponse>;
      if (xhr.status >= 200 && xhr.status < 300 && body && body.jobId) {
        setState({ kind: "done", result: body as CompressResponse });
      } else {
        const code = body?.error;
        const known = code && ERROR_CODES.has(code as ErrorCode) ? (code as ErrorCode) : "INTERNAL";
        setState({ kind: "error", code: known });
      }
    };
    xhr.send(fd);
  }, []);

  const handleFileChange = (next: File | null) => {
    setFile(next);
    if (next) startUpload(next);
  };

  const reset = () => {
    xhrRef.current?.abort();
    xhrRef.current = null;
    setFile(null);
    setState({ kind: "idle" });
  };

  return (
    <div className="relative isolate flex min-h-screen flex-col">
      <Backdrop />
      <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-5 py-12">
        <header className="mb-6 space-y-2">
          <h1 className="font-heading text-2xl font-semibold tracking-tight">
            Сжатие PDF
          </h1>
          <p className="text-sm text-muted-foreground">
            Для дизайнерских PDF из&nbsp;Figma. Сохраняем векторы, прозрачности
            и&nbsp;эффекты, обычно сжимаем в&nbsp;10–30&nbsp;раз.
          </p>
        </header>

        <section className="rounded-2xl border bg-card p-4 shadow-sm ring-1 ring-foreground/5 sm:p-5">
          {state.kind === "done" ? (
            <ResultCard
              result={state.result}
              fileName={file?.name ?? "document.pdf"}
              onReset={reset}
            />
          ) : state.kind === "error" ? (
            <ErrorBanner code={state.code} onRetry={reset} />
          ) : (
            <ActiveForm
              file={file}
              maxBytes={maxBytes}
              state={state}
              onFileChange={handleFileChange}
              onError={(code) => setState({ kind: "error", code })}
            />
          )}
        </section>

        <footer className="mt-6 text-xs text-muted-foreground/80">
          <p>
            Файлы хранятся на&nbsp;сервере не&nbsp;дольше 10&nbsp;минут и&nbsp;удаляются после скачивания.
          </p>
        </footer>
      </main>
    </div>
  );
}

type ActiveFormProps = {
  file: File | null;
  maxBytes: number;
  state: Extract<State, { kind: "idle" } | { kind: "uploading" } | { kind: "processing" }>;
  onFileChange: (f: File | null) => void;
  onError: (code: ErrorCode) => void;
};

function ActiveForm({
  file,
  maxBytes,
  state,
  onFileChange,
  onError,
}: ActiveFormProps) {
  const isBusy = state.kind !== "idle";

  return (
    <div className="flex flex-col gap-5">
      <Uploader
        file={file}
        maxBytes={maxBytes}
        disabled={isBusy}
        onFileChange={onFileChange}
        onTooLarge={() => onError("FILE_TOO_LARGE")}
        onWrongType={() => onError("INVALID_PDF")}
      />
      {state.kind !== "idle" && <ProgressBlock state={state} />}
    </div>
  );
}

function ProgressBlock({
  state,
}: {
  state: { kind: "uploading"; progress: number } | { kind: "processing"; startedAt: number };
}) {
  if (state.kind === "uploading") {
    const pct = Math.round(state.progress * 100);
    return (
      <Progress value={pct}>
        <ProgressLabel>Загрузка</ProgressLabel>
        <ProgressValue>{(_, value) => `${value ?? 0}%`}</ProgressValue>
      </Progress>
    );
  }
  return <ProcessingBlock startedAt={state.startedAt} />;
}

function ProcessingBlock({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(t);
  }, [startedAt]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between text-sm">
        <span className="flex items-center gap-2 font-medium">
          <Loader2 className="size-4 animate-spin text-primary" />
          Обрабатываем PDF
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {elapsed >= 5 ? `${elapsed} сек` : "обычно 5–20 сек"}
        </span>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
        <div className="h-full w-1/3 rounded-full bg-primary animate-[indeterminate_1.4s_ease-in-out_infinite]" />
      </div>
    </div>
  );
}

function Backdrop() {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 -z-10"
      style={{
        background:
          "radial-gradient(60% 50% at 50% 0%, color-mix(in oklch, var(--primary) 7%, transparent) 0%, transparent 80%)",
      }}
    />
  );
}
