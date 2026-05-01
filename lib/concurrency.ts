import pLimit from "p-limit";
import { LIMITS } from "./runtime-limits";

export const compressionLimit = pLimit(LIMITS.concurrency);
