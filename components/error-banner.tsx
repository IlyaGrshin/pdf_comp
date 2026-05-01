"use client";

import { AlertCircle, RotateCcw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { ErrorCode } from "@/lib/errors";

export type { ErrorCode };

const MESSAGES: Record<ErrorCode, { title: string; description: string }> = {
  FILE_TOO_LARGE: {
    title: "Файл слишком большой",
    description: "Максимальный размер — 1 ГБ. Попробуйте разбить документ на части.",
  },
  INVALID_PDF: {
    title: "Это не PDF-файл",
    description: "Похоже, файл повреждён или формат не поддерживается.",
  },
  PASSWORD_PROTECTED: {
    title: "PDF защищён паролем",
    description: "Снимите защиту паролем и попробуйте снова.",
  },
  INVALID_PRESET: {
    title: "Неизвестный режим сжатия",
    description: "Перезагрузите страницу и попробуйте снова.",
  },
  MISSING_FILE: {
    title: "Файл не выбран",
    description: "Перетащите PDF в окно загрузки и попробуйте снова.",
  },
  BUSY: {
    title: "Сервис занят",
    description: "Слишком много одновременных запросов. Попробуйте через минуту.",
  },
  COMPRESS_FAILED: {
    title: "Не удалось обработать PDF",
    description: "Файл может быть нестандартным или повреждён. Попробуйте другой PDF.",
  },
  COMPRESS_TIMEOUT: {
    title: "Слишком долгая обработка",
    description: "Файл оказался сложнее обычного. Попробуйте PDF поменьше.",
  },
  EXPIRED: {
    title: "Ссылка устарела",
    description: "Загрузите PDF снова — файлы хранятся не дольше 10 минут.",
  },
  INTERNAL: {
    title: "Что-то пошло не так",
    description: "Внутренняя ошибка сервиса. Попробуйте ещё раз через минуту.",
  },
  NETWORK: {
    title: "Нет связи с сервером",
    description: "Проверьте интернет-соединение и попробуйте снова.",
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
        Попробовать снова
      </Button>
    </div>
  );
}
