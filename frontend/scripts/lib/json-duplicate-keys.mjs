/**
 * Find duplicate keys inside the objects of a JSON document.
 *
 * `JSON.parse` keeps the last of two equal keys and says nothing, so a
 * catalog can carry a dead entry for years. This walks the raw text once,
 * token by token, and reports every key already seen in the same object,
 * with the line of both occurrences. Keys are compared decoded, so `"a"` and
 * `"\u0061"` are the same key, as they are to `JSON.parse`.
 *
 * The input is expected to be valid JSON (callers parse it afterwards and
 * report a syntax error there); on malformed input the result is best effort.
 *
 * @param {string} text
 * @returns {{ key: string, path: (string | number)[], firstLine: number, line: number }[]}
 */
export function findDuplicateKeys(text) {
  const duplicates = [];
  const stack = [];
  let line = 1;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    const top = stack[stack.length - 1];
    if (ch === "\n") {
      line += 1;
    } else if (ch === '"') {
      const startLine = line;
      let j = i + 1;
      while (j < text.length && text[j] !== '"') {
        if (text[j] === "\n") line += 1;
        j += text[j] === "\\" ? 2 : 1;
      }
      if (top && top.type === "object" && top.expectKey) {
        const key = JSON.parse(text.slice(i, j + 1));
        const firstLine = top.keys.get(key);
        if (firstLine === undefined) top.keys.set(key, startLine);
        else duplicates.push({ key, path: top.path, firstLine, line: startLine });
        top.lastKey = key;
        top.expectKey = false;
      }
      i = j;
    } else if (ch === "{" || ch === "[") {
      const path = top ? [...top.path, top.type === "object" ? top.lastKey : top.index] : [];
      stack.push(
        ch === "{"
          ? { type: "object", keys: new Map(), expectKey: true, lastKey: undefined, path }
          : { type: "array", index: 0, path },
      );
    } else if (ch === "}" || ch === "]") {
      stack.pop();
    } else if (ch === "," && top) {
      if (top.type === "object") top.expectKey = true;
      else top.index += 1;
    }
    i += 1;
  }
  return duplicates;
}
