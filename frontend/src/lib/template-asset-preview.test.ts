/**
 * Unit tests for where a template asset tile reads its picture from - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { hasTemplateThumbnail, templateAssetPreviewSources } from "./template-asset-preview.ts";

const THUMB = "/api/v1/template-containers/c/assets/a/thumbnail?size=128&v=1";
const ORIGINAL = "/api/v1/template-containers/c/assets/a/download?v=1";

test("a type the route decodes tries the thumbnail, then the original it showed before", () => {
  for (const type of ["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp", "image/avif"]) {
    assert.deepEqual(templateAssetPreviewSources(type, THUMB, ORIGINAL), [THUMB, ORIGINAL], type);
  }
  assert.ok(hasTemplateThumbnail("IMAGE/JPEG; charset=binary"));
});

test("every other type is drawn from its original, exactly as before the route", () => {
  for (const type of ["image/svg+xml", "image/tiff", "image/x-icon", "image/vnd.microsoft.icon", "image/heic", "image/heif", "image/x-photoshop", "", null, undefined]) {
    assert.deepEqual(templateAssetPreviewSources(type, THUMB, ORIGINAL), [ORIGINAL], String(type));
    assert.equal(hasTemplateThumbnail(type), false, String(type));
  }
});

test("a tile that showed nothing before still shows only what the route can give", () => {
  // The picker previews an unrendered file of more than 2 MB by its name only, as before.
  assert.deepEqual(templateAssetPreviewSources("image/tiff", THUMB, null), []);
  assert.deepEqual(templateAssetPreviewSources("image/png", THUMB, null), [THUMB]);
});
