// @vitest-environment node
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { parseSync } from "oxc-parser";
import { describe, it, expect } from "vitest";

/**
 * The dashboard's visualization engine knows no module.
 *
 * Every module (OpenCTI, OpenAEV, OpenCRQ, the next ones) is drawn by one
 * engine: its server adapter turns the module's answers into the generic
 * shapes and presentation options, and the renderers under `charts/` and
 * `viz/` draw those. A renderer that tests which module it draws is a silo
 * the next module cannot use, so their code never names one - in an
 * identifier, a string, a template or JSX text. Comments may: they cite the
 * widget a renderer reproduces. Tests are not the engine and may use a
 * module's data.
 */
const ROOT = path.resolve(__dirname, "..");
const ENGINE = ["charts", "viz"].map((dir) => path.join(ROOT, "src/components/custom-dashboard", dir));
const MODULE_NAME = /open[\s_-]?(cti|aev|crq)/i;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const file = path.join(dir, name);
    if (statSync(file).isDirectory()) return sources(file);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [file] : [];
  });
}

interface AstNode {
  type: string;
  start: number;
  [key: string]: unknown;
}

const isNode = (value: unknown): value is AstNode =>
  typeof value === "object" && value !== null && typeof (value as AstNode).type === "string";

/** What a node writes in the code: a name, a string, a regex, template or JSX text. */
function written(node: AstNode): string | undefined {
  switch (node.type) {
    case "Identifier":
    case "PrivateIdentifier":
    case "JSXIdentifier":
      return node.name as string;
    case "Literal":
      return typeof node.value === "string" ? node.value : node.regex ? (node.raw as string) : undefined;
    case "TemplateElement":
      return (node.value as { cooked: string | null }).cooked ?? undefined;
    case "JSXText":
      return node.value as string;
    default:
      return undefined;
  }
}

/** Where a source names a module in its code (comments are not nodes): `line: text`. */
function namings(name: string, text: string): string[] {
  const found: string[] = [];
  const visit = (node: AstNode) => {
    const value = written(node);
    if (value !== undefined && MODULE_NAME.test(value)) {
      found.push(`${text.slice(0, node.start).split("\n").length}: ${value.trim().slice(0, 80)}`);
    }
    for (const child of Object.values(node)) {
      for (const item of Array.isArray(child) ? child : [child]) if (isNode(item)) visit(item);
    }
  };
  visit(parseSync(name, text, { lang: name.endsWith("x") ? "tsx" : "ts" }).program as unknown as AstNode);
  return found;
}

describe("the dashboard engine", () => {
  const files = ENGINE.flatMap(sources);

  it("is scanned", () => {
    expect(files.some((file) => file.endsWith(path.join("charts", "PlatformChart.tsx")))).toBe(true);
  });

  it("names no module in its code", () => {
    expect(files.flatMap((file) => namings(file, readFileSync(file, "utf8")).map((at) => `${path.relative(ROOT, file)}:${at}`))).toEqual([]);
  });

  it("sees a module named in a string, an identifier, a template or JSX text, and not in a comment", () => {
    const count = (code: string) => namings("probe.tsx", code).length;
    expect(count('const a = "opencti";')).toBe(1);
    expect(count("const isOpenAev = true;")).toBe(1);
    expect(count("const t = `${x} OpenCRQ ${y}`;")).toBe(1);
    expect(count("const e = <span>Open CTI</span>;")).toBe(1);
    expect(count("const r = /open_aev/;")).toBe(1);
    expect(count("// drawn as OpenCTI draws it\n/* OpenAEV's widget */\nconst a = 1; /** OpenCRQ */")).toBe(0);
  });
});
