const KB = 1024;
const MB = KB * 1024;
const GB = MB * 1024;

export function formatBytes(bytes: number): string {
  if (bytes < KB) return `${bytes} Б`;
  if (bytes < MB) return `${trim(bytes / KB)} КБ`;
  if (bytes < GB) return `${trim(bytes / MB)} МБ`;
  return `${trim(bytes / GB, 2)} ГБ`;
}

export function formatRatio(ratio: number): string {
  return `${trim(ratio * 100)}%`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} мс`;
  return `${trim(ms / 1000)} сек`;
}

// Round to `decimals` places (default 1) and drop the trailing ".0".
function trim(n: number, decimals = 1): string {
  const rounded = Math.round(n * 10 ** decimals) / 10 ** decimals;
  return Number.isInteger(rounded) ? rounded.toString() : rounded.toFixed(decimals);
}
