/**
 * Pure helpers of the LM Studio console (`/integrations/:id/models`): how a
 * model's size and a download's progress read, which models a filter keeps,
 * and what a download may name. Pure, so `node --test` runs them
 * (`lmstudio-console.test.ts`).
 */
import type { LMStudioDownloadJob, LMStudioModel, LMStudioModelType } from "@/types";

export interface ServerModelFilter {
  search: string;
  /** `null`: every type. */
  type: LMStudioModelType | null;
  loadedOnly: boolean;
  offeredOnly: boolean;
}

export const DEFAULT_SERVER_MODEL_FILTER: ServerModelFilter = {
  search: "",
  type: null,
  loadedOnly: false,
  offeredOnly: false,
};

/** Bytes as people read them (`12.1 GB`), `—` when unknown. */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1000 && unit < units.length - 1) {
    size /= 1000;
    unit += 1;
  }
  const digits = unit === 0 || size >= 100 ? 0 : 1;
  return `${size.toFixed(digits)} ${units[unit]}`;
}

/** A download's progress as a 0-100 share, `null` when it cannot be told. */
export function downloadShare(job: Pick<LMStudioDownloadJob, "status" | "downloaded_bytes" | "total_size_bytes">): number | null {
  if (job.status === "completed" || job.status === "already_downloaded") return 100;
  const { downloaded_bytes: done, total_size_bytes: total } = job;
  if (done === null || total === null || total <= 0) return null;
  return Math.min(100, Math.max(0, (done / total) * 100));
}

/** Whether a download job has stopped moving. */
export function downloadFinished(job: Pick<LMStudioDownloadJob, "status">): boolean {
  return job.status !== "downloading";
}

/**
 * The models a filter keeps, in the server's order (chat models first). A
 * model being offered on the platform (`selected`) counts as offered.
 */
export function filterServerModels(
  models: readonly LMStudioModel[],
  filter: ServerModelFilter,
  selected: ReadonlySet<string>,
): LMStudioModel[] {
  const needle = filter.search.trim().toLowerCase();
  return models.filter((model) => {
    if (filter.type && model.type !== filter.type) return false;
    if (filter.loadedOnly && model.loaded_instances.length === 0) return false;
    if (filter.offeredOnly && !selected.has(model.key)) return false;
    if (!needle) return true;
    return [model.key, model.display_name, model.publisher ?? "", model.architecture ?? ""].some((text) =>
      text.toLowerCase().includes(needle),
    );
  });
}

const HUGGING_FACE_HOSTS = new Set(["huggingface.co", "www.huggingface.co", "hf.co"]);
const MODEL_KEY = /^[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,299}$/;

/** What a download may name: an LM Studio catalog id (`owner/model`) or a Hugging Face https link. Mirrors the server's check. */
export function isDownloadSource(value: string): boolean {
  const text = value.trim();
  if (!text || text.length > 500) return false;
  if (text.startsWith("https://")) {
    let url: URL;
    try {
      url = new URL(text);
    } catch {
      return false;
    }
    return HUGGING_FACE_HOSTS.has(url.hostname.toLowerCase()) && !url.username && !url.port && !url.hash && !/\s/.test(text);
  }
  return MODEL_KEY.test(text) && !text.includes("..") && !text.includes("://");
}

/** A context length typed in the load dialog: a whole number of tokens within the model's maximum, or `null`. */
export function parseContextLength(value: string, max: number | null): number | null {
  const text = value.trim();
  if (!/^\d+$/.test(text)) return null;
  const tokens = Number(text);
  if (!Number.isSafeInteger(tokens) || tokens < 256) return null;
  if (max !== null && tokens > max) return null;
  return tokens;
}
