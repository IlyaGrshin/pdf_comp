const KB = 1024;
const MB = KB * 1024;
const GB = MB * 1024;

export function formatBytes(bytes: number): string {
  if (bytes < KB) return `${bytes} Б`;
  if (bytes < MB) return `${(bytes / KB).toFixed(1)} КБ`;
  if (bytes < GB) return `${(bytes / MB).toFixed(1)} МБ`;
  return `${(bytes / GB).toFixed(2)} ГБ`;
}

export function formatRatio(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} мс`;
  return `${(ms / 1000).toFixed(1)} сек`;
}
