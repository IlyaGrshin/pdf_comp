export type PresetId = "maximum";

export type PikepdfArgs = {
  colorQuality: number;       // JPEG quality for color images (1-100)
  grayQuality: number;        // JPEG quality for grayscale (smasks). Set higher.
  maxLongEdge: number;        // Downsample images larger than this on long edge.
};

export type Preset = {
  id: PresetId;
  label: string;
  description: string;
  detail: string;
  pikepdf: PikepdfArgs;
};

export const PRESETS: Record<PresetId, Preset> = {
  maximum: {
    id: "maximum",
    label: "Максимум",
    description: "Сохраняет векторы, прозрачности и эффекты Figma.",
    detail: "обычно сжимаем в 10–30 раз",
    pikepdf: {
      colorQuality: 80,
      grayQuality: 92,
      // 2400 px on long edge ≈ 144 DPI on a 1920×1080 slide — matches what
      // iLovePDF medium uses, sharp at 200% zoom. Lower = smaller file but
      // visible upsampling at zoom on big-image slides.
      maxLongEdge: 2400,
    },
  },
};

export const PRESET_IDS: PresetId[] = ["maximum"];
export const DEFAULT_PRESET: PresetId = "maximum";

export function isPresetId(value: unknown): value is PresetId {
  return typeof value === "string" && (PRESET_IDS as string[]).includes(value);
}
