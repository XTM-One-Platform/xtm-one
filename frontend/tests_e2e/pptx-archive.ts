import { inflateRawSync } from 'node:zlib';

// ---------------------------------------------------------------------------
// Reading an exported PPTX in a spec: the archive's entries and where a slide
// places its pictures. Enough of Open XML for an assertion, nothing more.
// ---------------------------------------------------------------------------

/** The entries of a zip archive (a .pptx is one), stored or deflated. */
export function unzip(archive: Buffer): Map<string, Buffer> {
  let end = archive.length - 22;
  while (end >= 0 && archive.readUInt32LE(end) !== 0x06054b50) end -= 1;
  if (end < 0) throw new Error('Not a zip archive');
  const entries = new Map<string, Buffer>();
  let offset = archive.readUInt32LE(end + 16);
  for (let index = archive.readUInt16LE(end + 10); index > 0; index -= 1) {
    if (archive.readUInt32LE(offset) !== 0x02014b50) throw new Error('Corrupt zip central directory');
    const method = archive.readUInt16LE(offset + 10);
    const size = archive.readUInt32LE(offset + 20);
    const nameLength = archive.readUInt16LE(offset + 28);
    const extraLength = archive.readUInt16LE(offset + 30);
    const commentLength = archive.readUInt16LE(offset + 32);
    const local = archive.readUInt32LE(offset + 42);
    const name = archive.toString('utf8', offset + 46, offset + 46 + nameLength);
    const start = local + 30 + archive.readUInt16LE(local + 26) + archive.readUInt16LE(local + 28);
    const data = archive.subarray(start, start + size);
    entries.set(name, method === 8 ? inflateRawSync(data) : Buffer.from(data));
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

/** A shape's box on its slide, as fractions of the slide's width and height. */
export type PictureBox = { left: number; top: number; width: number; height: number };

/**
 * The boxes of the `element` shapes slide `number` (1-based) places at the top
 * level of its shape tree - a shape inside a group shape is positioned in the
 * group's own coordinates and is left out.
 */
function topLevelBoxes(entries: Map<string, Buffer>, number: number, element: 'p:pic' | 'p:graphicFrame'): [string, PictureBox][] {
  const presentation = entries.get('ppt/presentation.xml')?.toString('utf8') ?? '';
  const size = /<p:sldSz[^>]*\bcx="(\d+)"[^>]*\bcy="(\d+)"/.exec(presentation);
  if (!size) throw new Error('No slide size in ppt/presentation.xml');
  const [slideWidth, slideHeight] = [Number(size[1]), Number(size[2])];
  const slide = entries.get(`ppt/slides/slide${number}.xml`)?.toString('utf8') ?? '';
  const topLevel = slide.replace(/<p:grpSp>[\s\S]*?<\/p:grpSp>/g, '');
  return [...topLevel.matchAll(new RegExp(`<${element}>[\\s\\S]*?</${element}>`, 'g'))].map(([shape]) => {
    const off = /<a:off x="(-?\d+)" y="(-?\d+)"\/>/.exec(shape);
    const ext = /<a:ext cx="(\d+)" cy="(\d+)"\/>/.exec(shape);
    if (!off || !ext) throw new Error(`A ${element} without a position`);
    const box = {
      left: Number(off[1]) / slideWidth,
      top: Number(off[2]) / slideHeight,
      width: Number(ext[1]) / slideWidth,
      height: Number(ext[2]) / slideHeight,
    };
    return [shape, box];
  });
}

/** The boxes of the pictures slide `number` (1-based) places at the top level of its shape tree. */
export function slidePictureBoxes(entries: Map<string, Buffer>, number: number): PictureBox[] {
  return topLevelBoxes(entries, number, 'p:pic').map(([, box]) => box);
}

/** The boxes of the native charts slide `number` (1-based) places at the top level of its shape tree. */
export function slideChartBoxes(entries: Map<string, Buffer>, number: number): PictureBox[] {
  return topLevelBoxes(entries, number, 'p:graphicFrame')
    .filter(([frame]) => frame.includes('uri="http://schemas.openxmlformats.org/drawingml/2006/chart"'))
    .map(([, box]) => box);
}
