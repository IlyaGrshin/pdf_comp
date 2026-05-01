import pLimit from "p-limit";

export const compressionLimit = pLimit(2);
