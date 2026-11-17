import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import {
  builtinAllowedPayload,
  builtinFunctionRows,
  functionsOfferedToAssignment,
  sameBuiltinAllowed,
  scopedFunctions,
  withGroupScope,
  BUILTIN_FUNCTION_LABELS,
} from "./builtin-allowed-tools";

type Catalog = Record<string, string>;

function translator(catalog: Catalog) {
  return (id: string) => {
    const text = catalog[id];
    if (text === undefined) throw new Error(`missing i18n key: ${id}`);
    return text;
  };
}

describe("builtinFunctionRows", () => {
  it("renders the translated label in fr and keeps the id as the row name", () => {
    expect(builtinFunctionRows(["inspect_my_traces", "get_trace_payload"], translator(fr as Catalog))).toEqual([
      { name: "inspect_my_traces", label: "Inspecter mes traces", description: "" },
      { name: "get_trace_payload", label: "Obtenir le contenu d'une trace", description: "" },
    ]);
  });

  it("renders the translated label in ja", () => {
    expect(builtinFunctionRows(["inspect_agent_traces"], translator(ja as Catalog))[0].label).toBe(
      "エージェントのトレースを調査",
    );
  });

  it("gives no label to a function with no catalog label, so the row keeps its id", () => {
    const [row] = builtinFunctionRows(["create_work_item"], translator(ja as Catalog));
    expect(row).toEqual({ name: "create_work_item", description: "" });
    expect("label" in row).toBe(false);
  });

  it("has every function label in en, fr, es and ja", () => {
    for (const catalog of [en, fr, es, ja] as Catalog[]) {
      for (const key of Object.values(BUILTIN_FUNCTION_LABELS)) expect(catalog[key], key).toBeTruthy();
    }
  });
});

describe("builtinAllowedPayload", () => {
  it("sends null when nothing is scoped", () => {
    expect(builtinAllowedPayload(null, ["knowledge"])).toBeNull();
    expect(builtinAllowedPayload({}, ["knowledge"])).toBeNull();
    expect(builtinAllowedPayload({ knowledge: null }, ["knowledge"])).toBeNull();
  });

  it("drops the groups the agent no longer carries", () => {
    expect(
      builtinAllowedPayload({ knowledge: ["search_knowledge"], memory: ["search_memory"] }, ["knowledge"]),
    ).toEqual({ knowledge: ["search_knowledge"] });
  });

  it("keeps an empty list: the group is enabled with no function", () => {
    expect(builtinAllowedPayload({ knowledge: [] }, ["knowledge"])).toEqual({ knowledge: [] });
  });

  it("dedupes and sorts the names", () => {
    expect(builtinAllowedPayload({ memory: ["store_memory", "search_memory", "store_memory"] }, ["memory"])).toEqual({
      memory: ["search_memory", "store_memory"],
    });
  });
});

describe("scopedFunctions", () => {
  it("reads a missing group and a null entry as all", () => {
    expect(scopedFunctions(null, "knowledge")).toBeNull();
    expect(scopedFunctions({ knowledge: null }, "knowledge")).toBeNull();
    expect(scopedFunctions({ memory: ["search_memory"] }, "knowledge")).toBeNull();
  });

  it("returns the explicit list", () => {
    expect(scopedFunctions({ knowledge: ["search_knowledge"] }, "knowledge")).toEqual(["search_knowledge"]);
  });
});

describe("functionsOfferedToAssignment", () => {
  const groupFunctions = ["get_knowledge_document", "list_knowledge_documents", "search_knowledge"];

  it("offers every function when the agent keeps them all", () => {
    expect(functionsOfferedToAssignment(groupFunctions, null, "knowledge")).toEqual(groupFunctions);
  });

  it("offers only what the agent keeps, in the group's order", () => {
    expect(
      functionsOfferedToAssignment(groupFunctions, { knowledge: ["search_knowledge", "get_knowledge_document"] }, "knowledge"),
    ).toEqual(["get_knowledge_document", "search_knowledge"]);
  });

  it("never offers a name the group does not grant", () => {
    expect(functionsOfferedToAssignment(groupFunctions, { knowledge: ["removed_function"] }, "knowledge")).toEqual([]);
  });
});

describe("withGroupScope", () => {
  it("removes the entry on all and sets it on a list", () => {
    const scoped = withGroupScope(null, "knowledge", ["search_knowledge"]);
    expect(scoped).toEqual({ knowledge: ["search_knowledge"] });
    expect(withGroupScope(scoped, "knowledge", null)).toEqual({});
  });

  it("leaves the other groups alone", () => {
    expect(withGroupScope({ memory: ["search_memory"] }, "knowledge", [])).toEqual({
      memory: ["search_memory"],
      knowledge: [],
    });
  });
});

describe("sameBuiltinAllowed", () => {
  it("treats null, {} and null entries as equal", () => {
    expect(sameBuiltinAllowed(null, {})).toBe(true);
    expect(sameBuiltinAllowed({ knowledge: null }, null)).toBe(true);
  });

  it("ignores order", () => {
    expect(sameBuiltinAllowed({ memory: ["b", "a"] }, { memory: ["a", "b"] })).toBe(true);
    expect(sameBuiltinAllowed({ memory: ["a"] }, { memory: ["a", "b"] })).toBe(false);
  });
});
