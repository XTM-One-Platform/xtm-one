/**
 * Every setting field of the "Connect a vault" dialog carries an info bubble
 * (UX r2 B3 of #10406, design standards s.2): what the field is, a real
 * example, what empty does. A field added to a provider without one fails here.
 */
import { describe, expect, it } from "vitest";

import { VAULT_TYPES, visibleFields, VAULT_AUTH_METHODS } from "@/lib/secret-vaults";
import { VAULT_KV_VERSION_DESCRIPTIONS, vaultFieldHelp, vaultFieldInfo } from "./secret-vault-labels";

describe("secret vault field copy", () => {
  it("has an info bubble for every setting field of every provider and sign-in method", () => {
    const missing: string[] = [];
    for (const type of VAULT_TYPES) {
      for (const method of VAULT_AUTH_METHODS[type]) {
        for (const field of visibleFields(type, method)) {
          if (!field.credential && !vaultFieldInfo(field.key)) missing.push(`${type}.${method}.${field.key}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("has a help line under every setting field of every provider and sign-in method (UX r3 B2 of #10406)", () => {
    const missing: string[] = [];
    for (const type of VAULT_TYPES) {
      for (const method of VAULT_AUTH_METHODS[type]) {
        for (const field of visibleFields(type, method)) {
          if (!field.credential && !vaultFieldHelp(type, field.key)) missing.push(`${type}.${method}.${field.key}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("says what each KV version means", () => {
    const kv = visibleFields("hashicorp", "token").find((field) => field.key === "kv_version");
    expect(Object.keys(VAULT_KV_VERSION_DESCRIPTIONS).sort()).toEqual([...(kv?.options ?? [])].sort());
  });
});
