const KB = 1024;
const MB = KB * 1024;
const GB = MB * 1024;

export function formatBytes(bytes: number): string {
  if (bytes < KB) return `${bytes} B`;
  if (bytes < MB) return `${trim(bytes / KB)} KB`;
  if (bytes < GB) return `${trim(bytes / MB)} MB`;
  return `${trim(bytes / GB, 2)} GB`;
}

export function formatRatio(ratio: number): string {
  return `${trim(ratio * 100)}%`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  return `${trim(ms / 1000)} s`;
}

// Round to `decimals` places (default 1) and drop the trailing ".0".
function trim(n: number, decimals = 1): string {
  const rounded = Math.round(n * 10 ** decimals) / 10 ** decimals;
  return Number.isInteger(rounded) ? rounded.toString() : rounded.toFixed(decimals);
}
