/**
 * Unit tests for the LM Studio console helpers - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_SERVER_MODEL_FILTER,
  downloadFinished,
  downloadShare,
  filterServerModels,
  formatBytes,
  isDownloadSource,
  parseContextLength,
} from "./lmstudio-console.ts";
import type { LMStudioModel } from "@/types";

function model(key: string, overrides: Partial<LMStudioModel> = {}): LMStudioModel {
  return {
    key,
    type: "llm",
    display_name: key,
    publisher: key.split("/")[0] ?? null,
    architecture: null,
    format: "gguf",
    quantization: "Q4_K_M",
    bits_per_weight: 4,
    size_bytes: 1,
    params: null,
    max_context_length: 32768,
    vision: false,
    tool_use: false,
    reasoning_options: [],
    description: null,
    loaded_instances: [],
    offered: false,
    ...overrides,
  };
}

test("formatBytes reads like a file size", () => {
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(-1), "—");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(84_106_624), "84.1 MB");
  assert.equal(formatBytes(12_109_566_560), "12.1 GB");
  assert.equal(formatBytes(250_000_000_000), "250 GB");
});

test("downloadShare says how far a download is, when it can", () => {
  assert.equal(downloadShare({ status: "downloading", downloaded_bytes: 50, total_size_bytes: 200 }), 25);
  assert.equal(downloadShare({ status: "downloading", downloaded_bytes: null, total_size_bytes: 200 }), null);
  assert.equal(downloadShare({ status: "downloading", downloaded_bytes: 10, total_size_bytes: 0 }), null);
  assert.equal(downloadShare({ status: "downloading", downloaded_bytes: 300, total_size_bytes: 200 }), 100);
  assert.equal(downloadShare({ status: "already_downloaded", downloaded_bytes: null, total_size_bytes: null }), 100);
  assert.equal(downloadShare({ status: "completed", downloaded_bytes: null, total_size_bytes: null }), 100);
});

test("downloadFinished is every status but downloading", () => {
  assert.equal(downloadFinished({ status: "downloading" }), false);
  for (const status of ["paused", "completed", "failed", "already_downloaded"] as const) {
    assert.equal(downloadFinished({ status }), true);
  }
});

test("filterServerModels keeps the server's order and combines its conditions", () => {
  const models = [
    model("openai/gpt-oss-20b", { loaded_instances: [{ id: "openai/gpt-oss-20b", context_length: 8192, flash_attention: null, parallel: null }] }),
    model("qwen/qwen3-4b", { architecture: "qwen3" }),
    model("text-embedding-nomic", { type: "embedding", display_name: "Nomic Embed" }),
  ];
  const keys = (filter = DEFAULT_SERVER_MODEL_FILTER, selected = new Set<string>()) =>
    filterServerModels(models, filter, selected).map((m) => m.key);

  assert.deepEqual(keys(), ["openai/gpt-oss-20b", "qwen/qwen3-4b", "text-embedding-nomic"]);
  assert.deepEqual(keys({ ...DEFAULT_SERVER_MODEL_FILTER, type: "embedding" }), ["text-embedding-nomic"]);
  assert.deepEqual(keys({ ...DEFAULT_SERVER_MODEL_FILTER, loadedOnly: true }), ["openai/gpt-oss-20b"]);
  assert.deepEqual(keys({ ...DEFAULT_SERVER_MODEL_FILTER, offeredOnly: true }, new Set(["qwen/qwen3-4b"])), ["qwen/qwen3-4b"]);
  assert.deepEqual(keys({ ...DEFAULT_SERVER_MODEL_FILTER, search: " QWEN3 " }), ["qwen/qwen3-4b"]);
  assert.deepEqual(keys({ ...DEFAULT_SERVER_MODEL_FILTER, search: "nomic embed" }), ["text-embedding-nomic"]);
  assert.deepEqual(keys({ ...DEFAULT_SERVER_MODEL_FILTER, search: "qwen", type: "embedding" }), []);
});

test("isDownloadSource takes a catalog id or a Hugging Face link, like the server", () => {
  for (const ok of [
    "openai/gpt-oss-20b",
    "qwen3-4b",
    "https://huggingface.co/lmstudio-community/Qwen3-4B-GGUF",
    "https://hf.co/lmstudio-community/Qwen3-4B-GGUF",
  ]) {
    assert.equal(isDownloadSource(ok), true, ok);
  }
  for (const refused of [
    "",
    "   ",
    "../models",
    "http://huggingface.co/a/b",
    "https://evil.example/a/b",
    "https://huggingface.co.evil.example/a",
    "https://user@huggingface.co/a/b",
    "https://huggingface.co:8443/a/b",
    "https://huggingface.co/a#b",
    "model with space",
    "x".repeat(501),
  ]) {
    assert.equal(isDownloadSource(refused), false, refused);
  }
});

test("parseContextLength takes whole tokens within the model's maximum", () => {
  assert.equal(parseContextLength("8192", 32768), 8192);
  assert.equal(parseContextLength(" 4096 ", null), 4096);
  assert.equal(parseContextLength("65536", 32768), null);
  assert.equal(parseContextLength("100", 32768), null);
  assert.equal(parseContextLength("8k", 32768), null);
  assert.equal(parseContextLength("", 32768), null);
  assert.equal(parseContextLength("4096.5", 32768), null);
});
