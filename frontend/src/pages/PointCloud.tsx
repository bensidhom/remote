// frontend/src/pages/PointCloud.tsx
import React, { useRef, useEffect, useState, useCallback, DragEvent } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { PLYLoader } from 'three/examples/jsm/loaders/PLYLoader.js';

interface PointStats {
  count: number;
  originalCount?: number;   // pre-decimation size, when the parser downsampled
  minX: number; maxX: number;
  minY: number; maxY: number;
  minZ: number; maxZ: number;
  spanX: number; spanY: number; spanZ: number;
  originX: number; originY: number; originZ: number;
}
interface Measurement { groundZ: number | null; soffitZ: number | null }
interface ParseResult  { positions: Float32Array; colors: Float32Array; stats: PointStats }

function heightToColor(t: number): [number, number, number] {
  const c = Math.max(0, Math.min(1, t));
  if (c < 0.25) return [0, c * 4, 1];
  if (c < 0.5)  return [0, 1, 1 - (c - 0.25) * 4];
  if (c < 0.75) return [(c - 0.5) * 4, 1, 0];
  return [1, 1 - (c - 0.75) * 4, 0];
}

function colorizeByZ(positions: Float32Array, count: number): Float32Array {
  let minZ = Infinity, maxZ = -Infinity;
  for (let i = 0; i < count; i++) { const z = positions[i * 3 + 2]; if (z < minZ) minZ = z; if (z > maxZ) maxZ = z; }
  const zRange = maxZ - minZ || 1;
  const colors = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const [r, g, b] = heightToColor((positions[i * 3 + 2] - minZ) / zRange);
    colors[i * 3] = r; colors[i * 3 + 1] = g; colors[i * 3 + 2] = b;
  }
  return colors;
}

// Hard cap on points loaded into the viewer. Uncapped multi-million-point clouds
// (a 646MB .las is ~20M points) keep the GPU saturated AND make every analysis
// call ship a gigabyte-scale JSON body â€” the backend subsamples to ~300k for
// analysis anyway, so nothing is gained past this. Matches the backend's
// MAX_RENDER_POINTS. Decimation is a uniform stride, so coverage stays even.
const MAX_UI_POINTS = 2_000_000;

function decimate(raw: Float64Array, count: number, existingColors?: Float32Array):
    { raw: Float64Array; count: number; colors?: Float32Array } {
  if (count <= MAX_UI_POINTS) return { raw, count, colors: existingColors };
  const step = Math.ceil(count / MAX_UI_POINTS);
  const n = Math.floor((count - 1) / step) + 1;
  const outRaw = new Float64Array(n * 3);
  const outCol = existingColors ? new Float32Array(n * 3) : undefined;
  for (let i = 0, pi = 0; i < count; i += step, pi++) {
    outRaw[pi * 3] = raw[i * 3]; outRaw[pi * 3 + 1] = raw[i * 3 + 1]; outRaw[pi * 3 + 2] = raw[i * 3 + 2];
    if (outCol && existingColors) {
      outCol[pi * 3] = existingColors[i * 3]; outCol[pi * 3 + 1] = existingColors[i * 3 + 1]; outCol[pi * 3 + 2] = existingColors[i * 3 + 2];
    }
  }
  return { raw: outRaw, count: n, colors: outCol };
}

function buildResult(rawIn: Float64Array, countIn: number, existingColors?: Float32Array): ParseResult {
  // A few exporters keep hidden/deleted vertices as NaN. Never let one invalid
  // vertex poison the centroid and make an otherwise valid cloud render blank.
  let finiteCount = 0;
  for (let i = 0; i < countIn; i++) {
    const p = i * 3;
    if (Number.isFinite(rawIn[p]) && Number.isFinite(rawIn[p + 1]) && Number.isFinite(rawIn[p + 2])) finiteCount++;
  }
  if (finiteCount === 0) throw new Error('No finite point coordinates found in this cloud.');
  let source = rawIn;
  let sourceColors = existingColors;
  if (finiteCount !== countIn) {
    source = new Float64Array(finiteCount * 3);
    sourceColors = existingColors ? new Float32Array(finiteCount * 3) : undefined;
    for (let i = 0, out = 0; i < countIn; i++) {
      const p = i * 3;
      if (!Number.isFinite(rawIn[p]) || !Number.isFinite(rawIn[p + 1]) || !Number.isFinite(rawIn[p + 2])) continue;
      source[out * 3] = rawIn[p]; source[out * 3 + 1] = rawIn[p + 1]; source[out * 3 + 2] = rawIn[p + 2];
      if (sourceColors && existingColors) {
        sourceColors[out * 3] = existingColors[p]; sourceColors[out * 3 + 1] = existingColors[p + 1]; sourceColors[out * 3 + 2] = existingColors[p + 2];
      }
      out++;
    }
  }
  const { raw, count, colors: sampledColors } = decimate(source, finiteCount, sourceColors);
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
  let sx = 0, sy = 0, sz = 0;
  for (let i = 0; i < count; i++) {
    const x = raw[i * 3], y = raw[i * 3 + 1], z = raw[i * 3 + 2];
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
    if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    sx += x; sy += y; sz += z;
  }
  const originX = sx / count, originY = sy / count, originZ = sz / count;
  const positions = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    positions[i * 3]     = raw[i * 3]     - originX;
    positions[i * 3 + 1] = raw[i * 3 + 1] - originY;
    positions[i * 3 + 2] = raw[i * 3 + 2] - originZ;
  }
  const colors = sampledColors ?? colorizeByZ(positions, count);
  return { positions, colors, stats: { count, originalCount: countIn, minX, maxX, minY, maxY, minZ, maxZ, spanX: maxX-minX, spanY: maxY-minY, spanZ: maxZ-minZ, originX, originY, originZ } };
}

// E57 is parsed server-side (pye57) with uniform downsampling to the same cap the
// client-side parsers (.ply/.las/.xyz/.csv/.pcd) apply via decimate().
const E57_MAX_POINTS = MAX_UI_POINTS;

// â”€â”€ Binary transport â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Point payloads go to the backend as the viewer's Float32Array verbatim
// (Content-Type: application/octet-stream, options in the query string) instead
// of JSON â€” stringifying 2M points froze the UI for seconds and stalled the
// backend parsing the ~50MB body. Per-point labels come back base64-packed.
const BIN_HDRS = { 'Content-Type': 'application/octet-stream' };
const binQS = (o: Record<string, string | number | boolean | undefined>): string =>
  '?' + Object.entries(o)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}=${encodeURIComponent(typeof v === 'boolean' ? (v ? '1' : '0') : String(v))}`)
    .join('&');
const b64ToU8 = (s: string): Uint8Array => Uint8Array.from(atob(s), (ch) => ch.charCodeAt(0));
const b64ToI32 = (s: string): Int32Array => {
  const u = b64ToU8(s);
  return new Int32Array(u.buffer, 0, u.byteLength >> 2);
};

// Convert the backend /point-cloud/preview JSON into the render ParseResult.
function previewToResult(d: any): ParseResult {
  const pts: number[][] = d.points || [];
  const n = pts.length;
  const positions = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) { positions[i * 3] = pts[i][0]; positions[i * 3 + 1] = pts[i][1]; positions[i * 3 + 2] = pts[i][2]; }
  let colors: Float32Array;
  if (Array.isArray(d.colors) && d.colors.length === n) {
    colors = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) { colors[i * 3] = d.colors[i][0]; colors[i * 3 + 1] = d.colors[i][1]; colors[i * 3 + 2] = d.colors[i][2]; }
  } else {
    colors = colorizeByZ(positions, n);
  }
  const b = d.bounds || { min: [0, 0, 0], max: [0, 0, 0], center: [0, 0, 0] };
  const stats: PointStats = {
    count: n,
    minX: b.min[0], maxX: b.max[0], minY: b.min[1], maxY: b.max[1], minZ: b.min[2], maxZ: b.max[2],
    spanX: b.max[0] - b.min[0], spanY: b.max[1] - b.min[1], spanZ: b.max[2] - b.min[2],
    originX: b.center[0], originY: b.center[1], originZ: b.center[2],
  };
  return { positions, colors, stats };
}

// Parse the framed binary preview response (see backend _frame_cloud_response):
// [uint32 LE header_len][JSON header, 4-byte aligned][N*3 f32 points][N*3 u8 colors].
// Typed-array views on the response buffer â€” no JSON.parse over millions of numbers.
function frameToResult(buf: ArrayBuffer): { result: ParseResult; meta: any } {
  const dv = new DataView(buf);
  const hlen = dv.getUint32(0, true);
  const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hlen)));
  const n: number = meta.n ?? 0;
  const positions = new Float32Array(buf, 4 + hlen, n * 3);
  let colors: Float32Array;
  if (meta.has_colors) {
    const cu8 = new Uint8Array(buf, 4 + hlen + n * 12, n * 3);
    colors = new Float32Array(n * 3);
    for (let i = 0; i < n * 3; i++) colors[i] = cu8[i] / 255;
  } else {
    colors = colorizeByZ(positions, n);
  }
  const b = meta.bounds || { min: [0, 0, 0], max: [0, 0, 0], center: [0, 0, 0] };
  const stats: PointStats = {
    count: n,
    minX: b.min[0], maxX: b.max[0], minY: b.min[1], maxY: b.max[1], minZ: b.min[2], maxZ: b.max[2],
    spanX: b.max[0] - b.min[0], spanY: b.max[1] - b.min[1], spanZ: b.max[2] - b.min[2],
    originX: b.center[0], originY: b.center[1], originZ: b.center[2],
  };
  return { result: { positions, colors, stats }, meta };
}

// Read a preview-shaped success response either way the server sent it.
async function readCloudResponse(res: Response): Promise<{ result: ParseResult; meta: any }> {
  if ((res.headers.get('content-type') || '').includes('application/octet-stream')) {
    return frameToResult(await res.arrayBuffer());
  }
  const d = await res.json();
  return { result: previewToResult(d), meta: d };
}

function parseXYZ(buffer: ArrayBuffer): ParseResult {
  const text = new TextDecoder('utf-8', { fatal: false }).decode(buffer);
  const lines = text.split(/\r?\n/);
  const raw: number[] = [];
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || line.startsWith('//') || line.startsWith('%')) continue;
    const parts = line.split(/[\s,;]+/);
    if (parts.length < 3) continue;
    const x = parseFloat(parts[0]), y = parseFloat(parts[1]), z = parseFloat(parts[2]);
    if (!isFinite(x) || !isFinite(y) || !isFinite(z)) continue;
    raw.push(x, y, z);
  }
  if (raw.length === 0) throw new Error('No valid XYZ points found. Check that the file has at least 3 numeric columns per line.');
  const count = raw.length / 3;
  return buildResult(new Float64Array(raw), count);
}

function parsePLY(buffer: ArrayBuffer): ParseResult {
  const bytes = new Uint8Array(buffer);

  // â”€â”€ Find end_header (handles \n and \r\n) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  let headerEnd = -1;
  for (let i = 0; i < Math.min(65536, bytes.length - 12); i++) {
    if (bytes[i] === 101 && bytes[i+1] === 110 && bytes[i+2] === 100 && bytes[i+3] === 95 &&
        bytes[i+4] === 104 && bytes[i+5] === 101 && bytes[i+6] === 97 && bytes[i+7] === 100 &&
        bytes[i+8] === 101 && bytes[i+9] === 114) {
      // 'end_header' found â€” skip \r?\n
      headerEnd = (bytes[i+10] === 13) ? i + 12 : i + 11;
      break;
    }
  }
  if (headerEnd < 0) throw new Error('PLY: end_header not found');

  const headerText = new TextDecoder().decode(bytes.subarray(0, headerEnd));
  const lines = headerText.split(/\r?\n/).map(l => l.trim())
    .filter(l => l && !l.startsWith('comment') && !l.startsWith('obj_info'));

  if (lines[0] !== 'ply') throw new Error('Not a PLY file');
  const fmtLine = lines.find(l => l.startsWith('format ')) ?? '';
  const isAscii = fmtLine.includes('ascii');
  const le = !fmtLine.includes('big_endian');

  // â”€â”€ Type maps â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  type PT = 'f32'|'f64'|'i8'|'u8'|'i16'|'u16'|'i32'|'u32';
  const typeMap: Record<string,PT> = {
    float:'f32', float32:'f32', double:'f64', float64:'f64',
    char:'i8', int8:'i8', uchar:'u8', uint8:'u8',
    short:'i16', int16:'i16', ushort:'u16', uint16:'u16',
    int:'i32', int32:'i32', uint:'u32', uint32:'u32',
  };
  const typeSize: Record<PT,number> = { f32:4, f64:8, i8:1, u8:1, i16:2, u16:2, i32:4, u32:4 };

  interface Prop { name: string; type: PT; offset: number }
  interface Elem { name: string; count: number; props: Prop[]; stride: number }

  // â”€â”€ Parse header elements â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  const elements: Elem[] = [];
  let cur: Elem | null = null;
  for (const line of lines) {
    if (line.startsWith('element ')) {
      const [, n, c] = line.split(/\s+/);
      cur = { name: n, count: parseInt(c), props: [], stride: 0 };
      elements.push(cur);
    } else if (line.startsWith('property ') && cur) {
      const p = line.split(/\s+/);
      if (p[1] === 'list') continue;
      const t = typeMap[p[1].toLowerCase()] ?? 'f32';
      cur.props.push({ name: p[2], type: t, offset: cur.stride });
      cur.stride += typeSize[t];
    }
  }

  // â”€â”€ ASCII: delegate to Three.js PLYLoader (works for standard vertex files) â”€
  if (isAscii) {
    const loader = new PLYLoader();
    const geo = loader.parse(buffer);
    const pa = geo.getAttribute('position') as THREE.BufferAttribute | null;
    if (pa && pa.count > 0) {
      const n = pa.count;
      const raw = new Float64Array(n * 3);
      for (let i = 0; i < n; i++) { raw[i*3] = pa.getX(i); raw[i*3+1] = pa.getY(i); raw[i*3+2] = pa.getZ(i); }
      const ca = geo.getAttribute('color') as THREE.BufferAttribute | null;
      let vc: Float32Array | undefined;
      if (ca) { vc = new Float32Array(n*3); for (let i = 0; i < n; i++) { vc[i*3]=ca.getX(i); vc[i*3+1]=ca.getY(i); vc[i*3+2]=ca.getZ(i); } }
      geo.dispose();
      return buildResult(raw, n, vc);
    }
    geo.dispose();
    throw new Error('PLY ASCII: no vertex/position data found');
  }

  // â”€â”€ Binary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  const dv = new DataView(buffer);
  const read = (off: number, t: PT): number => {
    switch(t){
      case 'f32': return dv.getFloat32(off, le);
      case 'f64': return dv.getFloat64(off, le);
      case 'i8':  return dv.getInt8(off);
      case 'u8':  return dv.getUint8(off);
      case 'i16': return dv.getInt16(off, le);
      case 'u16': return dv.getUint16(off, le);
      case 'i32': return dv.getInt32(off, le);
      case 'u32': return dv.getUint32(off, le);
    }
  };

  // Compute byte start of each element
  let bytePos = headerEnd;
  const elBase: number[] = [];
  for (const el of elements) { elBase.push(bytePos); bytePos += el.count * el.stride; }

  // Find element with x/y/z
  const posEl = elements.find(e => e.props.some(p => p.name === 'x'));
  if (!posEl) throw new Error('PLY: no element containing x/y/z properties found');
  const xp = posEl.props.find(p => p.name === 'x')!;
  const yp = posEl.props.find(p => p.name === 'y')!;
  const zp = posEl.props.find(p => p.name === 'z')!;
  if (!yp || !zp) throw new Error('PLY: element has x but missing y or z');

  // Find color source: try separate element, then same element
  const colorNames = ['red','r','diffuse_red'];
  const colorEl = elements.find(e => e !== posEl && e.props.some(p => colorNames.includes(p.name)));
  const colorInPosEl = !colorEl && posEl.props.some(p => colorNames.includes(p.name));

  const posBase = elBase[elements.indexOf(posEl)];
  const colorBase = colorEl ? elBase[elements.indexOf(colorEl)] : posBase;
  const colorSrc  = colorEl ?? (colorInPosEl ? posEl : null);

  // Uniform stride caps the loaded count (see MAX_UI_POINTS).
  const step = Math.max(1, Math.ceil(posEl.count / MAX_UI_POINTS));
  const ptCount = Math.floor((posEl.count - 1) / step) + 1;

  // Read positions + accumulate centroid
  const positions = new Float32Array(ptCount * 3);
  let sx = 0, sy = 0, sz = 0;
  for (let i = 0, pi = 0; i < posEl.count; i += step, pi++) {
    const base = posBase + i * posEl.stride;
    const x = read(base + xp.offset, xp.type);
    const y = read(base + yp.offset, yp.type);
    const z = read(base + zp.offset, zp.type);
    positions[pi*3] = x; positions[pi*3+1] = y; positions[pi*3+2] = z;
    sx += x; sy += y; sz += z;
  }
  const ox = sx/ptCount, oy = sy/ptCount, oz = sz/ptCount;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (let i = 0; i < ptCount; i++) {
    const rx = positions[i*3]+ox, ry = positions[i*3+1]+oy, rz = positions[i*3+2]+oz;
    if (rx<minX)minX=rx; if (rx>maxX)maxX=rx;
    if (ry<minY)minY=ry; if (ry>maxY)maxY=ry;
    if (rz<minZ)minZ=rz; if (rz>maxZ)maxZ=rz;
    positions[i*3] -= ox; positions[i*3+1] -= oy; positions[i*3+2] -= oz;
  }

  // Read colors
  let colors: Float32Array;
  if (colorSrc) {
    const rp = colorSrc.props.find(p => colorNames.includes(p.name))!;
    const gp = colorSrc.props.find(p => ['green','g','diffuse_green'].includes(p.name))!;
    const bp = colorSrc.props.find(p => ['blue','b','diffuse_blue'].includes(p.name))!;
    const isFloat = rp.type === 'f32' || rp.type === 'f64';
    const scale = isFloat ? 1 : 1/255;
    const srcBase = colorEl ? colorBase : posBase;
    const srcStride = colorSrc.stride;
    colors = new Float32Array(ptCount * 3);
    for (let i = 0, pi = 0; i < Math.min(colorSrc.count, posEl.count); i += step, pi++) {
      const base = srcBase + i * srcStride;
      colors[pi*3]   = read(base + rp.offset, rp.type) * scale;
      colors[pi*3+1] = read(base + gp.offset, gp.type) * scale;
      colors[pi*3+2] = read(base + bp.offset, bp.type) * scale;
    }
  } else {
    colors = colorizeByZ(positions, ptCount);
  }

  const stats: PointStats = { count: ptCount, originalCount: posEl.count, minX, maxX, minY, maxY, minZ, maxZ, spanX: maxX-minX, spanY: maxY-minY, spanZ: maxZ-minZ, originX: ox, originY: oy, originZ: oz };
  return { positions, colors, stats };
}

function parseE57(buffer: ArrayBuffer): ParseResult {
  const bytes = new Uint8Array(buffer);
  const view  = new DataView(buffer);
  const sig   = new TextDecoder().decode(bytes.subarray(0, 8));
  if (!sig.startsWith('ASTM-E57')) throw new Error('Not a valid E57 file (invalid signature)');
  const pageSize      = Number(view.getBigUint64(40, true));
  const xmlPhysOffset = Number(view.getBigUint64(24, true));
  const xmlLogLength  = Number(view.getBigUint64(32, true));

  function depage(physOffset: number, logicalLength: number): Uint8Array {
    const logPageSize = pageSize - 4;
    const out = new Uint8Array(logicalLength);
    let outPos = 0, remaining = logicalLength;
    let pageIdx = Math.floor(physOffset / pageSize);
    const firstPageInternalOffset = physOffset % pageSize;
    let firstChunk = true;
    while (remaining > 0 && pageIdx * pageSize < bytes.length) {
      const pageStart = pageIdx * pageSize;
      const skipInPage = firstChunk ? firstPageInternalOffset : 0;
      const available = logPageSize - skipInPage;
      if (available <= 0) { pageIdx++; firstChunk = false; continue; }
      const toCopy = Math.min(available, remaining);
      const srcStart = pageStart + skipInPage;
      if (srcStart + toCopy > bytes.length) break;
      out.set(bytes.subarray(srcStart, srcStart + toCopy), outPos);
      outPos += toCopy; remaining -= toCopy; pageIdx++; firstChunk = false;
    }
    return out;
  }

  const xmlBytes = depage(xmlPhysOffset, xmlLogLength);
  const xmlText  = new TextDecoder('utf-8').decode(xmlBytes);
  const doc = new DOMParser().parseFromString(xmlText, 'text/xml');
  if (doc.querySelector('parsererror')) throw new Error('E57 XML section is malformed');

  // Find CompressedVector
  const cv = (() => {
    for (const el of Array.from(doc.querySelectorAll('*'))) {
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (t.includes('compressedvector') || el.tagName === 'points') return el;
    }
    return null;
  })();
  if (!cv) throw new Error('E57: no CompressedVector/points element found');

  const dataPhysOffset = parseInt(cv.getAttribute('fileOffset') || '0');
  const recordCount    = parseInt(cv.getAttribute('recordCount') || '0');
  if (!dataPhysOffset || !recordCount) throw new Error('E57: missing fileOffset or recordCount');

  const prototype = (() => {
    for (const el of Array.from(doc.querySelectorAll('*'))) {
      if (el.localName === 'prototype' || el.tagName.endsWith('prototype')) return el;
    }
    return null;
  })();
  if (!prototype) throw new Error('E57: no prototype element found');

  interface E57Field { name: string; type: 'float'|'scaledInt'|'other'; bytesEach: number; bitsEach: number; minimum: number; scale: number; offset: number }
  const fields: E57Field[] = [];
  for (const child of Array.from(prototype.children)) {
    const name      = child.localName || child.tagName.split(':').pop() || child.tagName;
    const typeAttr  = (child.getAttribute('type') || '').split(':').pop()?.toLowerCase() || '';
    const precision = (child.getAttribute('precision') || 'double').toLowerCase();
    const minimum   = parseFloat(child.getAttribute('minimum') || '0');
    const maximum   = parseFloat(child.getAttribute('maximum') || '0');
    const scale     = parseFloat(child.getAttribute('scale')   || '1');
    const offset    = parseFloat(child.getAttribute('offset')  || '0');
    if (typeAttr.includes('float')) {
      fields.push({ name, type: 'float', bytesEach: precision === 'single' ? 4 : 8, bitsEach: 0, minimum, scale, offset });
    } else if (typeAttr.includes('scaled') || typeAttr.includes('integer')) {
      const range = maximum - minimum;
      const bitsEach = range <= 0 ? 1 : Math.floor(Math.log2(range)) + 1;
      fields.push({ name, type: 'scaledInt', bytesEach: 0, bitsEach, minimum, scale, offset });
    } else {
      fields.push({ name, type: 'other', bytesEach: 4, bitsEach: 0, minimum, scale, offset });
    }
  }

  const xIdx = fields.findIndex(f => f.name === 'cartesianX');
  const yIdx = fields.findIndex(f => f.name === 'cartesianY');
  const zIdx = fields.findIndex(f => f.name === 'cartesianZ');
  if (xIdx < 0 || yIdx < 0 || zIdx < 0) throw new Error('E57 prototype missing cartesianX/Y/Z fields');

  const rawDataLogLen = buffer.byteLength - dataPhysOffset;
  const dataBytes = depage(dataPhysOffset, Math.max(0, rawDataLogLen));
  const dv = new DataView(dataBytes.buffer);

  const MAX = recordCount;                    // load every point (no cap)
  const rawCoords = new Float64Array(MAX * 3);
  let totalPts = 0, bytePos = 0;

  function readFloat(off: number, sz: number) { return sz === 4 ? dv.getFloat32(off, true) : dv.getFloat64(off, true); }

  function unpackInts(bufOffset: number, n: number, f: E57Field): Float64Array {
    const res = new Float64Array(n);
    let bitPos = 0;
    const bits = f.bitsEach;
    for (let i = 0; i < n; i++) {
      let rawVal = 0;
      for (let b = 0; b < bits; b++) {
        const by = bufOffset + (bitPos >> 3), bi = bitPos & 7;
        rawVal |= ((dataBytes[by] >> bi) & 1) << b;
        bitPos++;
      }
      res[i] = (rawVal + f.minimum) * f.scale + f.offset;
    }
    return res;
  }

  while (bytePos + 6 < dataBytes.length && totalPts < MAX) {
    const pType = dataBytes[bytePos];
    if (pType !== 1) {
      if (bytePos + 4 > dataBytes.length) break;
      bytePos += dv.getUint16(bytePos + 2, true) + 1; continue;
    }
    const packetLogLen    = dv.getUint16(bytePos + 2, true) + 1;
    const bytestreamCount = dv.getUint16(bytePos + 4, true);
    if (bytestreamCount < 1 || bytePos + 6 + bytestreamCount * 2 > dataBytes.length) { bytePos += packetLogLen; continue; }

    const bsLengths: number[] = [];
    for (let k = 0; k < bytestreamCount; k++) bsLengths.push(dv.getUint16(bytePos + 6 + k * 2, true));
    const dataStart = bytePos + 6 + bytestreamCount * 2;

    const refF = fields[0];
    const n = refF.type === 'float'
      ? Math.floor(bsLengths[0] / refF.bytesEach)
      : Math.floor((bsLengths[0] * 8) / Math.max(1, refF.bitsEach));
    const pointsInPkt = Math.min(n, MAX - totalPts);

    const bsOff: number[] = [];
    let acc = dataStart;
    for (let k = 0; k < bytestreamCount; k++) { bsOff.push(acc); acc += bsLengths[k]; }

    const extract = (fIdx: number, cnt: number): Float64Array => {
      const f = fields[fIdx];
      if (f.type === 'float') {
        const arr = new Float64Array(cnt);
        for (let i = 0; i < cnt; i++) arr[i] = readFloat(bsOff[fIdx] + i * f.bytesEach, f.bytesEach);
        return arr;
      }
      return unpackInts(bsOff[fIdx], cnt, f);
    };

    const xs = extract(xIdx, pointsInPkt);
    const ys = extract(yIdx, pointsInPkt);
    const zs = extract(zIdx, pointsInPkt);
    for (let i = 0; i < pointsInPkt; i++) {
      rawCoords[(totalPts+i)*3] = xs[i]; rawCoords[(totalPts+i)*3+1] = ys[i]; rawCoords[(totalPts+i)*3+2] = zs[i];
    }
    totalPts += pointsInPkt;
    bytePos  += packetLogLen;
  }

  if (totalPts === 0) throw new Error('E57: no points read. File may use unsupported compression or encoding.');
  return buildResult(rawCoords.subarray(0, totalPts * 3) as unknown as Float64Array, totalPts);
}

function parseLAS(buffer: ArrayBuffer): ParseResult {
  const view = new DataView(buffer), bytes = new Uint8Array(buffer);
  if (String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]) !== 'LASF') throw new Error('Not a valid LAS/LAZ file');
  const ptOff = view.getUint32(96, true);
  const ptLen = view.getUint16(105, true);
  let numPts  = view.getUint32(107, true);
  if (numPts === 0) { try { numPts = Number(view.getBigUint64(247, true)); } catch (_) { numPts = 0; } }
  const xS = view.getFloat64(131, true), yS = view.getFloat64(139, true), zS = view.getFloat64(147, true);
  const xO = view.getFloat64(155, true), yO = view.getFloat64(163, true), zO = view.getFloat64(171, true);
  // Uniform stride keeps at most MAX_UI_POINTS; reading directly with the stride
  // avoids ever materialising the full multi-hundred-MB coordinate array.
  const step = Math.max(1, Math.ceil(numPts / MAX_UI_POINTS));
  const raw = new Float64Array((Math.floor((numPts - 1) / step) + 1) * 3);
  let cnt = 0;
  for (let i = 0; i < numPts; i += step) {
    const off = ptOff + i * ptLen;
    if (off + 12 > buffer.byteLength) break;
    raw[cnt * 3] = view.getInt32(off, true) * xS + xO;
    raw[cnt * 3 + 1] = view.getInt32(off + 4, true) * yS + yO;
    raw[cnt * 3 + 2] = view.getInt32(off + 8, true) * zS + zO;
    cnt++;
  }
  const res = buildResult(raw.subarray(0, cnt * 3) as unknown as Float64Array, cnt);
  res.stats.originalCount = numPts;
  return res;
}

// â”€â”€ Segmentation constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
const SEG_GROUND       = 0;
const SEG_SUBSTRUCTURE = 1;
const SEG_SOFFIT       = 2;
const SEG_DECK         = 3;
const SEG_UNCLASSIFIED = 4;
const SEG_COUNT        = 5;

const SEG_LABELS: string[] = [
  'Ground / Road Surface',
  'Substructure (Piers / Abutments)',
  'Soffit / Bridge Underside',
  'Bridge Deck / Superstructure',
  'Unclassified',
];
const SEG_COLORS_RGB: [number, number, number][] = [
  [0.13, 0.82, 0.37],   // green   â€“ ground
  [0.98, 0.45, 0.12],   // orange  â€“ substructure
  [0.22, 0.72, 1.00],   // cyan    â€“ soffit
  [0.98, 0.88, 0.18],   // yellow  â€“ deck
  [0.45, 0.45, 0.55],   // gray    â€“ unclassified
];
const SEG_HEX = ['#20d15f', '#fa721f', '#38b8ff', '#fae02e', '#737380'];

// Dynamic class descriptor â€” local geometric segmentation uses the fixed 5 below,
// while the backend (AI) segmentation can return open-vocabulary names + materials.
interface SegClass {
  id: number;
  key?: string;
  name: string;
  color_rgb: [number, number, number];
  color_hex: string;
  count: number;
  material?: string;
  is_clearance?: boolean;
}
const DEFAULT_SEG_CLASSES: SegClass[] = SEG_LABELS.map((name, id) => ({
  id, name,
  color_rgb: SEG_COLORS_RGB[id],
  color_hex: SEG_HEX[id],
  count: 0,
  is_clearance: id === SEG_GROUND || id === SEG_SOFFIT,
}));

// â”€â”€ Sidebar visual language (light / indigo, professional & compact) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
const SX = {
  indigo: '#4f46e5', indigoHover: '#4338ca', indigoSoft: '#eef2ff',
  border: '#e6e8ee', text: '#334155', label: '#94a3b8', title: '#64748b',
};
// Button system â€” real CSS classes so every button gets hover/active/disabled
// states (inline styles can't express those). One visual language for the page.
const PC_CSS = `
.pcbtn{display:inline-flex;align-items:center;justify-content:center;gap:6px;font-weight:600;font-size:12px;line-height:1.2;border-radius:8px;padding:8px 13px;border:1px solid transparent;cursor:pointer;transition:background .15s,border-color .15s,color .15s;user-select:none;white-space:nowrap}
.pcbtn:active:not(:disabled){transform:translateY(1px)}
.pcbtn:disabled{cursor:not-allowed;opacity:.55}
.pcbtn-block{width:100%}
.pcbtn-primary{background:#4f46e5;color:#fff;box-shadow:0 1px 2px rgba(79,70,229,.35)}
.pcbtn-primary:hover:not(:disabled){background:#4338ca}
.pcbtn-accent{background:#2563eb;color:#fff;box-shadow:0 1px 2px rgba(37,99,235,.30)}
.pcbtn-accent:hover:not(:disabled){background:#1d4ed8}
.pcbtn-violet{background:#7c3aed;color:#fff;box-shadow:0 1px 2px rgba(124,58,237,.30)}
.pcbtn-violet:hover:not(:disabled){background:#6d28d9}
.pcbtn-success{background:#059669;color:#fff;box-shadow:0 1px 2px rgba(5,150,105,.30)}
.pcbtn-success:hover:not(:disabled){background:#047857}
.pcbtn-outline{background:#fff;color:#334155;border-color:#d7dce5}
.pcbtn-outline:hover:not(:disabled){background:#f8fafc;border-color:#c3cad6}
.pcbtn-ghost{background:#f8fafc;color:#475569;border-color:#e6e8ee}
.pcbtn-ghost:hover:not(:disabled){background:#eef2ff;border-color:#c7d2fe;color:#4338ca}
.pcbtn-danger{background:#fff5f5;color:#dc2626;border-color:#fecaca}
.pcbtn-danger:hover:not(:disabled){background:#fee2e2}
.pcbtn-close{background:#f1f5f9;color:#64748b;border:none;border-radius:50%;width:32px;height:32px;padding:0}
.pcbtn-close:hover{background:#fee2e2;color:#dc2626}
`;
const sxSpinner = (color: string) => (
  <svg style={{ width: 13, height: 13, animation: 'spin 1s linear infinite' }} viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="10" stroke="#e2e8f0" strokeWidth="3" />
    <path fill={color} d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
  </svg>
);


interface PointCloudProps {
  onClose?: () => void;
  bridgeStateCode?: string | number;
  bridgeNumber?: string;
  bridgeName?: string;
}

const PointCloud: React.FC<PointCloudProps> = ({ onClose, bridgeStateCode, bridgeNumber, bridgeName }) => {
  const mountRef      = useRef<HTMLDivElement>(null);
  const rendererRef   = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef      = useRef<THREE.Scene | null>(null);
  const cameraRef     = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef   = useRef<OrbitControls | null>(null);
  const frameRef      = useRef<number>(0);
  const pointsRef     = useRef<THREE.Points | null>(null);
  const raycasterRef  = useRef<THREE.Raycaster>(new THREE.Raycaster());
  const groundLineRef = useRef<THREE.Line | null>(null);
  const soffitLineRef = useRef<THREE.Line | null>(null);
  const clearLineRef  = useRef<THREE.Object3D | null>(null);
  const volumesRef    = useRef<THREE.Object3D | null>(null);
  const meshRef       = useRef<THREE.Mesh | null>(null);
  const ptSizeRef        = useRef<number>(0.06);
  const origColorsRef    = useRef<Float32Array | null>(null);
  const origPosRef       = useRef<Float32Array | null>(null); // backup for isolate/reset
  const renderPendingRef = useRef<number>(5); // dirty-render counter
  const originalFileRef  = useRef<File | null>(null); // kept for bridge save

  const [stats,       setStats]       = useState<PointStats | null>(null);
  const [status,      setStatus]      = useState('Drop a point cloud file here or use Open File â€” .xyz  .ply  .e57  .las  .npz  .csv');
  const [loading,     setLoading]     = useState(false);
  const [dragging,    setDragging]    = useState(false);
  const [measurement, setMeasurement] = useState<Measurement>({ groundZ: null, soffitZ: null });
  const [measureStep, setMeasureStep] = useState<'idle'|'ground'|'soffit'>('idle');
  const [pointSize,   setPointSize]   = useState(0.06);
  const [fileName,    setFileName]    = useState('');
  const [segLabels,   setSegLabels]   = useState<Uint8Array | null>(null);
  const [segVisible,  setSegVisible]  = useState<boolean[]>(Array(SEG_COUNT).fill(true));
  const [segClasses,  setSegClasses]  = useState<SegClass[]>(DEFAULT_SEG_CLASSES);
  const [segRunning,  setSegRunning]  = useState(false);
  const [segDone,     setSegDone]     = useState(false);
  const useHostedVlm = true;
  const useAiCleaning = true;
  const [instances,   setInstances]   = useState<Int32Array | null>(null);
  const [segColorMode, setSegColorMode] = useState<'components' | 'instances'>('components');
  // Backend clearance result (min/typical/cross-section + DB cross-check)
  const [clearanceInfo, setClearanceInfo] = useState<any | null>(null);
  const [clearanceBusy, setClearanceBusy] = useState(false);
  const [analysisInfo, setAnalysisInfo] = useState<any | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  // Ref mirror for the render loop (its closure is created once at mount):
  // while the server analyze runs, the viewer throttles its redraws so the
  // WebGL pass doesn't compete with SAM/qwen for the same laptop GPU.
  const analyzingRef = useRef(false);
  useEffect(() => { analyzingRef.current = analyzing; }, [analyzing]);
  // Layer toggles for the one-pass result: fitted component volumes + Poisson mesh
  const analysisMeshRef = useRef<THREE.Mesh | null>(null);
  const [showVolumes, setShowVolumes] = useState(true);
  const [showMesh, setShowMesh] = useState(true);
  const [reconstructing, setReconstructing] = useState(false);
  // False when the current cloud came from photogrammetric reconstruction
  // (SfM/VGGT): its geometry has no absolute scale, so measurements are
  // relative units, not meters/feet.
  const cloudScaleKnownRef = useRef(true);
  const [smoothBusy,  setSmoothBusy]  = useState(false);
  const [surfaceBusy, setSurfaceBusy] = useState(false);
  const [meshInfo,    setMeshInfo]    = useState<{ vertex_count: number; face_count: number } | null>(null);
  const [viewMode,    setViewMode]    = useState<'points' | 'surface'>('points');
  // Bridge-linked cloud save/load
  const hasBridge = !!(bridgeStateCode && bridgeNumber);
  const [cloudInfo,   setCloudInfo]   = useState<{ exists: boolean; filename?: string; size_bytes?: number; uploaded_at?: string; processed_exists?: boolean; processed_point_count?: number } | null>(null);
  const [saving,      setSaving]      = useState(false);

  useEffect(() => {
    if (!mountRef.current) return;
    const el = mountRef.current;
    const w = el.clientWidth || 800, h = el.clientHeight || 500;
    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    renderer.setClearColor(0xf4f6fa);   // light, matching the app shell
    el.appendChild(renderer.domElement);
    rendererRef.current = renderer;
    const scene = new THREE.Scene();
    sceneRef.current = scene;
    // Grid lies in XY (horizontal ground) for Z-up convention, matching CloudCompare
    const grid = new THREE.GridHelper(200, 40, 0xd3dbe7, 0xe4e9f2);
    grid.rotation.x = Math.PI / 2;
    scene.add(grid);
    // Lights â€” only used by the surface (mesh) view; harmless for points.
    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(1, -1, 2);
    scene.add(dir);
    const camera = new THREE.PerspectiveCamera(55, w / h, 0.01, 100000);
    camera.up.set(0, 0, 1);    // Z-up (CloudCompare convention)
    camera.position.set(0, -80, 60);
    cameraRef.current = camera;
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping    = true;
    controls.dampingFactor     = 0.08;
    controls.rotateSpeed       = 0.75;
    controls.zoomSpeed         = 1.4;
    controls.panSpeed          = 0.9;
    controls.screenSpacePanning = false;
    controls.maxPolarAngle     = Math.PI * 0.9;
    controlsRef.current = controls;

    // Dirty rendering: only draw when the camera moves or scene data changes.
    // Adaptive LOD: thin points to 50% size while orbiting for smooth interaction.
    const LOD = 0.5;
    let lodActive = false;
    let lastThrottledDraw = 0;
    const animate = () => {
      frameRef.current = requestAnimationFrame(animate);
      const moved = controls.update();
      if (moved && !lodActive) {
        lodActive = true;
        if (pointsRef.current)
          (pointsRef.current.material as THREE.PointsMaterial).size = ptSizeRef.current * LOD;
      } else if (!moved && lodActive) {
        lodActive = false;
        if (pointsRef.current)
          (pointsRef.current.material as THREE.PointsMaterial).size = ptSizeRef.current;
      }
      // While a server analyze runs, drop idle redraws to ~2fps: continuously
      // re-rendering a 2M-point cloud competes with SAM/SAM3 for the same GPU
      // on single-GPU laptops (contributed to a WDDM TDR reset). User
      // interaction still renders every frame for responsiveness.
      if (analyzingRef.current && !moved) {
        const now = performance.now();
        if (now - lastThrottledDraw < 500) return;
        lastThrottledDraw = now;
      }
      // Otherwise always render: 28kâ€“5M points are cheap, and a dirty-counter is
      // fragile with async loads + modal mount timing (was leaving the canvas blank).
      renderer.render(scene, camera);
    };
    animate();
    const resize = () => {
      const w2 = el.clientWidth, h2 = el.clientHeight;
      if (!w2 || !h2) return; // skip 0-size (avoids NaN aspect / blank canvas)
      camera.aspect = w2 / h2; camera.updateProjectionMatrix(); renderer.setSize(w2, h2);
      renderPendingRef.current = 4;
    };
    const obs = new ResizeObserver(resize);
    obs.observe(el);
    // The container can be 0-height at mount (e.g. inside a modal tab); re-measure
    // once layout settles so the canvas picks up its real size.
    requestAnimationFrame(() => { resize(); requestAnimationFrame(resize); });
    return () => {
      cancelAnimationFrame(frameRef.current);
      obs.disconnect(); controls.dispose(); renderer.dispose();
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement);
    };
  }, []);

  useEffect(() => {
    ptSizeRef.current = pointSize;
    if (pointsRef.current) {
      (pointsRef.current.material as THREE.PointsMaterial).size = pointSize;
      renderPendingRef.current = 4;
    }
  }, [pointSize]);

  // Trigger a re-render whenever scene-relevant state changes
  useEffect(() => { renderPendingRef.current = 5; }, [stats, measurement, segLabels, segVisible]);

  // Check whether a saved cloud exists for this bridge
  useEffect(() => {
    if (!hasBridge) return;
    const sc = String(bridgeStateCode);
    const bn = String(bridgeNumber);
    fetch(`/api/bridges/${sc}/${bn}/point-cloud/info`)
      .then(r => r.json())
      .then(d => {
        setCloudInfo(d);
        if (!d.exists) {
          setStatus(`No saved point cloud or processed result is mapped to bridge ${bn}. Open a file to attach and process one.`);
        }
      })
      .catch(() => {
        setCloudInfo({ exists: false });
        setStatus(`Could not check saved point-cloud results for bridge ${bn}.`);
      });
  }, [hasBridge, bridgeStateCode, bridgeNumber]);

  // Sanitised base name (no extension) for saved/exported files.
  const cloudBaseName = useCallback(() => {
    const base = (fileName || 'cloud').replace(/\.[^.]+$/, '') || 'cloud';
    return base.replace(/[^A-Za-z0-9_\-]/g, '_');
  }, [fileName]);

  // Serialise the CURRENTLY DISPLAYED cloud to a binary .ply (world coords =
  // centered + origin). Prefers the true RGB (origColorsRef) over any segmentation
  // overlay. Works for reconstructed / smoothed / opened clouds alike.
  const buildPLYBlob = useCallback((): Blob | null => {
    const geo = pointsRef.current?.geometry as THREE.BufferGeometry | undefined;
    const posAttr = geo?.getAttribute('position') as THREE.BufferAttribute | undefined;
    if (!posAttr) return null;
    const pos = posAttr.array as Float32Array;
    const col = origColorsRef.current
      ?? (geo?.getAttribute('color')?.array as Float32Array | undefined) ?? null;
  const sourceCount = pos.length / 3;
  const valid: number[] = [];
  for (let i = 0; i < sourceCount; i++) {
    const p = i * 3;
    if (Number.isFinite(pos[p]) && Number.isFinite(pos[p + 1]) && Number.isFinite(pos[p + 2])) valid.push(i);
  }
  if (valid.length === 0) return null;
  const n = valid.length;
  const hasColor = !!col && col.length >= sourceCount * 3;
    const ox = stats?.originX ?? 0, oy = stats?.originY ?? 0, oz = stats?.originZ ?? 0;
    const header =
      'ply\nformat binary_little_endian 1.0\n' +
      `element vertex ${n}\n` +
      'property float x\nproperty float y\nproperty float z\n' +
      (hasColor ? 'property uchar red\nproperty uchar green\nproperty uchar blue\n' : '') +
      'end_header\n';
    const hb = new TextEncoder().encode(header);
    const stride = hasColor ? 15 : 12;            // 3Ã—float32 (+ 3Ã—uint8)
    const buf = new ArrayBuffer(hb.length + n * stride);
    new Uint8Array(buf).set(hb, 0);
    const dv = new DataView(buf);
    let o = hb.length;
    for (let i = 0; i < n; i++) {
      const p = valid[i] * 3;
      dv.setFloat32(o, pos[p] + ox, true);
      dv.setFloat32(o + 4, pos[p + 1] + oy, true);
      dv.setFloat32(o + 8, pos[p + 2] + oz, true);
      o += 12;
      if (hasColor) {
        dv.setUint8(o,     Math.min(255, Math.max(0, Math.round(col![p]     * 255))));
        dv.setUint8(o + 1, Math.min(255, Math.max(0, Math.round(col![p + 1] * 255))));
        dv.setUint8(o + 2, Math.min(255, Math.max(0, Math.round(col![p + 2] * 255))));
        o += 3;
      }
    }
    return new Blob([buf], { type: 'application/octet-stream' });
  }, [stats]);

  // Download the current cloud as a .ply (works without a bridge â€” e.g. ad-hoc
  // reconstructions).
  const exportPLY = useCallback(() => {
    const blob = buildPLYBlob();
    if (!blob) { setStatus('Nothing to export â€” load or reconstruct a cloud first.'); return; }
    const name = `${cloudBaseName()}.ply`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    const sz = blob.size >= 1e6 ? `${(blob.size / 1e6).toFixed(1)} MB` : `${Math.round(blob.size / 1e3)} KB`;
    setStatus(`Exported ${name} (${sz}).`);
  }, [buildPLYBlob, cloudBaseName]);

  const saveToBridge = useCallback(async () => {
    if (!hasBridge) return;
    let blob: Blob, fname: string;
    if (originalFileRef.current) {                 // opened file â€” preserve it exactly
      blob = originalFileRef.current; fname = originalFileRef.current.name;
    } else {                                       // reconstructed/derived â€” serialise what's shown
      const ply = buildPLYBlob();
      if (!ply) { setStatus('Nothing to save â€” load or reconstruct a cloud first.'); return; }
      blob = ply; fname = `${cloudBaseName()}.ply`;
    }
    const sc = String(bridgeStateCode);
    const bn = String(bridgeNumber);
    setSaving(true);
    setStatus('Saving point cloud to bridgeâ€¦');
    try {
      const fd = new FormData();
      fd.append('file', blob, fname);
      const res = await fetch(`/api/bridges/${sc}/${bn}/point-cloud`, { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error((err as any).detail ?? `Upload failed (${res.status})`);
      }
      const d = await res.json();
      setCloudInfo({ exists: true, filename: d.filename, size_bytes: d.size_bytes });
      setStatus(`Saved "${d.filename}" to bridge ${bn}.`);
    } catch (err) {
      setStatus(`Save error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }, [hasBridge, bridgeStateCode, bridgeNumber, buildPLYBlob, cloudBaseName]);

  // loadFromBridge is defined after processFile (below) because it depends on it.
  const loadFromBridgeRef = useRef<(() => Promise<void>) | null>(null);
  const loadFromBridge = useCallback(() => {
    return loadFromBridgeRef.current?.() ?? Promise.resolve();
  }, []);

  const loadIntoScene = useCallback((result: ParseResult) => {
    if (!sceneRef.current) return;
    // Drop any prior surface mesh â€” a new cloud replaces it.
    if (meshRef.current) {
      sceneRef.current.remove(meshRef.current);
      meshRef.current.geometry.dispose();
      (meshRef.current.material as THREE.Material).dispose();
      meshRef.current = null;
      setMeshInfo(null);
      setViewMode('points');
    }
    setInstances(null);
    setSegColorMode('components');
    if (pointsRef.current) {
      sceneRef.current.remove(pointsRef.current);
      (pointsRef.current.geometry as THREE.BufferGeometry).dispose();
      (pointsRef.current.material as THREE.Material).dispose();
      pointsRef.current = null;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(result.positions, 3));
    geo.setAttribute('color',    new THREE.BufferAttribute(result.colors, 3));
    const mat = new THREE.PointsMaterial({ size: ptSizeRef.current, vertexColors: true, sizeAttenuation: true });
    const pts = new THREE.Points(geo, mat);
    sceneRef.current.add(pts);
    pointsRef.current = pts;
    geo.computeBoundingSphere();
    const bs = geo.boundingSphere!;
    const radius = Math.max(bs.radius, 1);
    // Z-up: orbit from -Y direction, elevated in Z
    cameraRef.current!.position.set(bs.center.x, bs.center.y - radius * 2.0, bs.center.z + radius * 0.5);
    cameraRef.current!.near = radius * 0.001;
    cameraRef.current!.far  = radius * 100;
    cameraRef.current!.updateProjectionMatrix();
    controlsRef.current!.target.copy(bs.center);
    controlsRef.current!.update();
    renderPendingRef.current = 8;
    setStats(result.stats);
  }, []);

  const drawClearancePlanes = useCallback((gZ: number|null, sZ: number|null, s: PointStats|null) => {
    if (!sceneRef.current || !s) return;
    const span = Math.max(s.spanX, s.spanY) * 0.6;
    const mkLine = (z: number, color: number) => new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(-span, -span, z), new THREE.Vector3( span, -span, z),
        new THREE.Vector3( span,  span, z), new THREE.Vector3(-span,  span, z),
        new THREE.Vector3(-span, -span, z),
      ]),
      new THREE.LineBasicMaterial({ color })
    );
    if (groundLineRef.current) { sceneRef.current.remove(groundLineRef.current); groundLineRef.current = null; }
    if (soffitLineRef.current) { sceneRef.current.remove(soffitLineRef.current); soffitLineRef.current = null; }
    if (gZ !== null) { groundLineRef.current = mkLine(gZ - s.originZ, 0x22c55e); sceneRef.current.add(groundLineRef.current); }
    if (sZ !== null) { soffitLineRef.current = mkLine(sZ - s.originZ, 0xf97316); sceneRef.current.add(soffitLineRef.current); }
  }, []);

  useEffect(() => { drawClearancePlanes(measurement.groundZ, measurement.soffitZ, stats); }, [measurement, stats, drawClearancePlanes]);

  // Draw the full clearance result: the fitted road & soffit surfaces per roadway
  // and the controlling-minimum + centre measurement lines. World coords â†’ centred
  // frame via origin. Falls back to the single clearance_line (e.g. waterway).
  const drawClearanceLine = useCallback((info: any, s: PointStats | null) => {
    if (!sceneRef.current) return;
    if (clearLineRef.current) { sceneRef.current.remove(clearLineRef.current); clearLineRef.current = null; }
    if (!info || !s) return;
    const group = new THREE.Group();
    const V = (x: number, y: number, z: number) => new THREE.Vector3(x - s.originX, y - s.originY, z - s.originZ);
    const dotR = Math.max(0.15, s.spanZ * 0.02);
    const dot = (v: THREE.Vector3, color: number) => {
      const m = new THREE.Mesh(new THREE.SphereGeometry(dotR, 12, 12), new THREE.MeshBasicMaterial({ color }));
      m.position.copy(v); return m;
    };
    const vline = (p: number[], color: number, w = 2) => {
      // p = [x, y, road_z, soffit_z]
      const a = V(p[0], p[1], p[2]), b = V(p[0], p[1], p[3]);
      group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]),
        new THREE.LineBasicMaterial({ color, linewidth: w })));
      group.add(dot(a, 0x22c55e)); group.add(dot(b, 0xf97316));
    };
    const plane = (corners: number[][], color: number, opacity: number) => {
      if (!corners || corners.length < 3) return;
      const geo = new THREE.BufferGeometry();
      const verts = new Float32Array(corners.flatMap((c) => { const v = V(c[0], c[1], c[2]); return [v.x, v.y, v.z]; }));
      geo.setAttribute('position', new THREE.BufferAttribute(verts, 3));
      const idx: number[] = [];                            // fan over the hull polygon
      for (let i = 1; i < corners.length - 1; i++) idx.push(0, i, i + 1);
      geo.setIndex(idx);
      group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, transparent: true, opacity, side: THREE.DoubleSide, depthWrite: false })));
    };

    const roadways = info.roadways as any[] | undefined;
    if (roadways && roadways.length) {
      const pal = [0x6366f1, 0x2563eb, 0x16a34a, 0xa855f7];
      roadways.forEach((rw, i) => {
        const col = pal[i % pal.length];
        plane(rw.soffit_plane_corners, col, 0.22);      // deck underside
        plane(rw.road_plane_corners, 0x94a3b8, 0.16);   // road surface
        if (rw.controlling_point) vline(rw.controlling_point, 0xef4444, 3);  // controlling min = red
        if (rw.center_point) vline(rw.center_point, 0x10b981, 2);            // centre = green
      });
    } else if (info.clearance_line) {
      // Single line (waterway navigation clearance, or fallback).
      const l = info.clearance_line;
      vline([l.ground[0], l.ground[1], l.ground[2], l.soffit[2]], info.waterway?.is_waterway ? 0x3b82f6 : 0xfacc15, 2);
    }
    sceneRef.current.add(group);
    clearLineRef.current = group;
    renderPendingRef.current = 6;
  }, []);

  useEffect(() => { drawClearanceLine(clearanceInfo, stats); }, [clearanceInfo, stats, drawClearanceLine]);

  // Draw each fitted component as its actual geometry: a translucent triangulated
  // surface (deck top+soffit heightfield, under-surface, pier prisms) that hugs the
  // cloud â€” not a bounding box. Falls back to the OBB corners only if no mesh.
  const drawComponentVolumes = useCallback((info: any, s: PointStats | null) => {
    if (!sceneRef.current) return;
    if (volumesRef.current) { sceneRef.current.remove(volumesRef.current); volumesRef.current = null; }
    if (!info?.components || !s) return;
    const group = new THREE.Group();
    for (const c of info.components) {
      const col = new THREE.Color(c.color || '#6366f1');
      const m = c.mesh;
      if (m && m.vertices?.length && m.faces?.length) {
        const verts = m.vertices as number[][];
        const faces = m.faces as number[][];
        const pos = new Float32Array(verts.length * 3);
        for (let i = 0; i < verts.length; i++) {
          pos[i * 3] = verts[i][0] - s.originX; pos[i * 3 + 1] = verts[i][1] - s.originY; pos[i * 3 + 2] = verts[i][2] - s.originZ;
        }
        const idx = new Uint32Array(faces.length * 3);
        for (let i = 0; i < faces.length; i++) { idx[i * 3] = faces[i][0]; idx[i * 3 + 1] = faces[i][1]; idx[i * 3 + 2] = faces[i][2]; }
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        geo.setIndex(new THREE.BufferAttribute(idx, 1));
        geo.computeVertexNormals();
        group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
          color: col, transparent: true, opacity: 0.30, side: THREE.DoubleSide, depthWrite: false,
        })));
      } else if (c.corners && c.corners.length >= 8) {   // fallback: OBB wireframe
        const EDGES = [[0,1],[2,3],[4,5],[6,7],[0,2],[1,3],[4,6],[5,7],[0,4],[1,5],[2,6],[3,7]];
        const V = (k: number) => new THREE.Vector3(c.corners[k][0] - s.originX, c.corners[k][1] - s.originY, c.corners[k][2] - s.originZ);
        const pts: THREE.Vector3[] = [];
        for (const [a, b] of EDGES) { pts.push(V(a), V(b)); }
        group.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: col })));
      }
    }
    sceneRef.current.add(group);
    volumesRef.current = group;
    renderPendingRef.current = 6;
  }, []);

  useEffect(() => { drawComponentVolumes(analysisInfo, stats); }, [analysisInfo, stats, drawComponentVolumes]);

  // Poisson surface of the kept bridge â€” computed inside the same Process pass and
  // layered under the fitted volumes (toggleable).
  const drawAnalysisSurface = useCallback((info: any, s: PointStats | null) => {
    if (!sceneRef.current) return;
    if (analysisMeshRef.current) {
      sceneRef.current.remove(analysisMeshRef.current);
      analysisMeshRef.current.geometry.dispose();
      (analysisMeshRef.current.material as THREE.Material).dispose();
      analysisMeshRef.current = null;
    }
    const surf = info?.surface;
    if (!surf?.vertices?.length || !surf?.faces?.length || !s) return;
    const verts: number[][] = surf.vertices, faces: number[][] = surf.faces;
    const pos = new Float32Array(verts.length * 3);
    for (let i = 0; i < verts.length; i++) {
      pos[i * 3] = verts[i][0] - s.originX; pos[i * 3 + 1] = verts[i][1] - s.originY; pos[i * 3 + 2] = verts[i][2] - s.originZ;
    }
    const idx = new Uint32Array(faces.length * 3);
    for (let i = 0; i < faces.length; i++) { idx[i * 3] = faces[i][0]; idx[i * 3 + 1] = faces[i][1]; idx[i * 3 + 2] = faces[i][2]; }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setIndex(new THREE.BufferAttribute(idx, 1));
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
      color: 0xaab4c4, metalness: 0.05, roughness: 0.9, side: THREE.DoubleSide, flatShading: false,
      transparent: true, opacity: 0.9,
    }));
    sceneRef.current.add(mesh);
    analysisMeshRef.current = mesh;
    renderPendingRef.current = 8;
  }, []);

  useEffect(() => { drawAnalysisSurface(analysisInfo, stats); }, [analysisInfo, stats, drawAnalysisSurface]);

  // Layer visibility toggles
  useEffect(() => {
    if (volumesRef.current) volumesRef.current.visible = showVolumes;
    if (analysisMeshRef.current) analysisMeshRef.current.visible = showMesh;
    renderPendingRef.current = 6;
  }, [showVolumes, showMesh, analysisInfo]);

  // Apply the one-pass result to the cloud: colour every point by its component
  // and hide the points the pipeline removed (noise / vegetation / off-structure)
  // by collapsing them to NaN so only the isolated bridge remains on screen.
  const applyAnalysisResult = useCallback((d: any) => {
    if (!pointsRef.current) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const posAttr = geo.getAttribute('position') as THREE.BufferAttribute;
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    const cnt = posAttr.array.length / 3;
    const labels: Uint8Array = d.labels_b64u8 ? b64ToU8(d.labels_b64u8) : Uint8Array.from((d.labels ?? []) as number[]);
    if (labels.length !== cnt) return;                 // safety: must be order-aligned
    if (!origColorsRef.current) origColorsRef.current = new Float32Array(colAttr.array as Float32Array);
    if (!origPosRef.current) origPosRef.current = new Float32Array(posAttr.array as Float32Array);
    const lut = new Map<number, [number, number, number]>();
    (d.classes || []).forEach((c: any) => lut.set(c.id, c.color_rgb));
    const pos = posAttr.array as Float32Array;
    const colors = colAttr.array as Float32Array;
    const op = origPosRef.current;
    for (let i = 0; i < cnt; i++) {
      const lid = labels[i] || 0;
      if (lid === 0) { pos[i * 3] = NaN; pos[i * 3 + 1] = NaN; pos[i * 3 + 2] = NaN; }
      else {
        pos[i * 3] = op[i * 3]; pos[i * 3 + 1] = op[i * 3 + 1]; pos[i * 3 + 2] = op[i * 3 + 2];
        const rgb = lut.get(lid) || [0.6, 0.6, 0.6];
        colors[i * 3] = rgb[0]; colors[i * 3 + 1] = rgb[1]; colors[i * 3 + 2] = rgb[2];
      }
    }
    posAttr.needsUpdate = true; colAttr.needsUpdate = true;
    pointsRef.current.frustumCulled = false;           // NaN-culled points break the bounding sphere
    setSegLabels(labels);
    setSegClasses((d.classes || []).map((c: any) => ({
      id: c.id,
      key: c.key,
      name: c.name,
      color_rgb: c.color_rgb,
      color_hex: c.color_hex,
      count: labels.filter((x) => x === c.id).length,
      material: c.material,
      is_clearance: c.is_clearance,
    })));
    setSegVisible(Array((d.classes || []).length).fill(true));
    setSegDone(true);
    renderPendingRef.current = 8;
  }, []);

  // Restore the full original cloud (undo isolation colouring/hiding).
  const restoreOriginalCloud = useCallback(() => {
    if (!pointsRef.current) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const posAttr = geo.getAttribute('position') as THREE.BufferAttribute;
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    if (origPosRef.current) (posAttr.array as Float32Array).set(origPosRef.current);
    if (origColorsRef.current) (colAttr.array as Float32Array).set(origColorsRef.current);
    posAttr.needsUpdate = true; colAttr.needsUpdate = true;
    setSegLabels(null);
    setSegDone(false);
    setSegClasses(DEFAULT_SEG_CLASSES);
    setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
    setInstances(null);
    setSegColorMode('components');
    setAnalysisInfo(null);
    setClearanceInfo(null);
    setMeasurement({ groundZ: null, soffitZ: null });
    setStatus('Restored the full original cloud.');
    renderPendingRef.current = 6;
  }, []);

  const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (measureStep === 'idle' || !pointsRef.current || !cameraRef.current || !mountRef.current) return;
    const rect = mountRef.current.getBoundingClientRect();
    const mouse = new THREE.Vector2(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1
    );
    raycasterRef.current.setFromCamera(mouse, cameraRef.current);
    raycasterRef.current.params.Points!.threshold = Math.max(0.05, (stats?.spanZ ?? 1) * 0.003);
    const hits = raycasterRef.current.intersectObject(pointsRef.current);
    if (hits.length > 0) {
      const realZ = parseFloat((hits[0].point.z + (stats?.originZ ?? 0)).toFixed(3));
      if (measureStep === 'ground') {
        setMeasurement(p => ({ ...p, groundZ: realZ }));
        setMeasureStep('soffit');
        setStatus('Ground set. Now click the soffit (underside of bridge).');
      } else {
        setMeasurement(p => ({ ...p, soffitZ: realZ }));
        setMeasureStep('idle');
        setStatus('Clearance computed.');
      }
    }
  }, [measureStep, stats]);

  const processFile = useCallback(async (file: File) => {
    originalFileRef.current = file;
    cloudScaleKnownRef.current = true;   // real scans carry real units
    setLoading(true); setFileName(file.name);
    setStatus(`Parsing ${file.name}â€¦`);
    setMeasurement({ groundZ: null, soffitZ: null });
    setMeasureStep('idle');
    setSegLabels(null);
    setSegDone(false);
    setSegClasses(DEFAULT_SEG_CLASSES);
    setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
    setInstances(null);
    setSegColorMode('components');
    setClearanceInfo(null);
    setAnalysisInfo(null);        // clears fitted volumes + surface mesh layers
    origColorsRef.current = null;
    origPosRef.current = null;
    try {
      const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
      // E57: parse on the backend (pye57) â€” reads all scans reliably, up to ~2M points.
      if (ext === 'e57') {
        setStatus('Parsing E57 on the serverâ€¦');
        const fd = new FormData();
        fd.append('file', file);
        fd.append('max_points', String(E57_MAX_POINTS));
        const res = await fetch('/api/point-cloud/preview?binary=1', { method: 'POST', body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `E57 parse failed (${res.status})`);
        const { result: pr, meta } = await readCloudResponse(res);
        loadIntoScene(pr);
        setStatus(`Loaded ${(meta.rendered_point_count ?? 0).toLocaleString()} of ${(meta.original_point_count ?? 0).toLocaleString()} points${meta.downsampled ? ' (downsampled)' : ''} from "${file.name}"`);
        return;
      }
      // NPZ: BridgeGuard reconstruction and processed-cloud cache format. Parse it
      // server-side with NumPy (pickle disabled), then use the same compact binary
      // response path as E57 before handing it to cleaning/segmentation.
      if (ext === 'npz') {
        setStatus('Parsing NPZ point cloud on the serverâ€¦');
        const fd = new FormData();
        fd.append('file', file);
        fd.append('max_points', String(E57_MAX_POINTS));
        const res = await fetch('/api/point-cloud/preview-npz?binary=1', { method: 'POST', body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `NPZ parse failed (${res.status})`);
        const { result: pr, meta } = await readCloudResponse(res);
        loadIntoScene(pr);
        setStatus(`Loaded ${(meta.rendered_point_count ?? 0).toLocaleString()} of ${(meta.original_point_count ?? 0).toLocaleString()} NPZ points${meta.downsampled ? ' (downsampled)' : ''} from "${file.name}"`);
        return;
      }
      const buf = await file.arrayBuffer();
      let result: ParseResult;
      if      (ext === 'ply') result = parsePLY(buf);
      else if (ext === 'las') result = parseLAS(buf);
      else if (ext === 'laz') throw new Error('.LAZ (compressed LAS) is not supported. Convert to .LAS or .PLY first using CloudCompare or PDAL.');
      else                    result = parseXYZ(buf);
      loadIntoScene(result);
      const oc = result.stats.originalCount ?? result.stats.count;
      setStatus(oc > result.stats.count
        ? `Loaded ${result.stats.count.toLocaleString()} of ${oc.toLocaleString()} points (downsampled) from "${file.name}"`
        : `Loaded ${result.stats.count.toLocaleString()} points from "${file.name}"`);
    } catch (err: unknown) {
      setStatus(`Error: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setLoading(false); }
  }, [loadIntoScene]);

  // Photogrammetry: reconstruct a cloud from overlapping photos or a video (COLMAP
  // SfM on the server), then load it into the viewer like any other scan.
  const reconstructFromMedia = useCallback(async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    const arr = Array.from(fileList);
    const isVideo = arr.length === 1 && /\.(mp4|mov|avi|mkv|m4v|webm)$/i.test(arr[0].name);
    setReconstructing(true); setLoading(true);
    setStatus(isVideo
      ? 'Extracting frames & reconstructing (COLMAP SfM)â€¦ this can take a few minutes'
      : `Reconstructing from ${arr.length} photos (COLMAP SfM)â€¦ this can take a few minutes`);
    try {
      const fd = new FormData();
      arr.forEach(f => fd.append('files', f));
      const res = await fetch('/api/point-cloud/reconstruct', { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Reconstruction failed (${res.status})`);
      const d = await res.json();
      originalFileRef.current = null;
      setSegLabels(null); setSegDone(false); setSegClasses(DEFAULT_SEG_CLASSES);
      setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
      setClearanceInfo(null); setMeasurement({ groundZ: null, soffitZ: null });
      setFileName(isVideo ? arr[0].name : `${arr.length} photos`);
      cloudScaleKnownRef.current = false;
      loadIntoScene(previewToResult(d));
      setStatus(`Reconstructed ${(d.rendered_point_count ?? 0).toLocaleString()} points Â· ${d.engine ?? ''}. ${d.scale_note ?? ''}`);
    } catch (err) {
      setStatus(`Reconstruction error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setReconstructing(false); setLoading(false);
    }
  }, [loadIntoScene]);

  // Assign the real loadFromBridge implementation now that processFile is declared
  useEffect(() => {
    loadFromBridgeRef.current = async () => {
      if (!hasBridge) return;
      const sc = String(bridgeStateCode);
      const bn = String(bridgeNumber);
      setLoading(true);
      setStatus('Loading saved point cloudâ€¦');
      try {
        // Prefer the completed saved analysis. It contains the already-cleaned
        // cloud plus classes, fitted geometry, dimensions, and clearance.
        const savedAnalysis = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/processed`);
        if (savedAnalysis.ok) {
          const analysis = await savedAnalysis.json();
          const processedPreview = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/processed/preview?binary=1`);
          if (processedPreview.ok) {
            const { result: pr, meta } = await readCloudResponse(processedPreview);
            originalFileRef.current = null;
            origColorsRef.current = null;
            origPosRef.current = null;
            cloudScaleKnownRef.current = analysis.scale_known !== false;
            loadIntoScene(pr);
            setAnalysisInfo(analysis);
            setClearanceInfo(analysis.clearance ?? null);
            if (analysis.clearance) setMeasurement({
              groundZ: analysis.clearance.ground_z ?? null,
              soffitZ: analysis.clearance.soffit_z ?? null,
            });
            const classes: SegClass[] = (analysis.classes || []).map((c: any) => ({
              id: c.id, key: c.key, name: c.name, color_rgb: c.color_rgb,
              color_hex: c.color_hex, count: c.count ?? 0, material: c.material,
              is_clearance: c.is_clearance,
            }));
            const labels = analysis.labels_b64u8 ? b64ToU8(analysis.labels_b64u8) : null;
            setSegClasses(classes);
            setSegLabels(labels);
            setSegDone(!!labels);
            setSegVisible(Array(classes.length).fill(true));
            setStatus(`Loaded saved processed result: ${(meta.rendered_point_count ?? 0).toLocaleString()} cleaned points Â· ${analysis.component_count ?? 0} fitted components`);
            setLoading(false);
            return;
          }
        }
        // E57 is parsed server-side (pye57) â€” no download/re-upload of the large file.
        const pv = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/preview?max_points=${E57_MAX_POINTS}&binary=1`);
        if (pv.ok) {
          const { result: pr, meta } = await readCloudResponse(pv);
          originalFileRef.current = null;
          cloudScaleKnownRef.current = true;   // E57 scans carry real units
          loadIntoScene(pr);
          setStatus(`Loaded ${(meta.rendered_point_count ?? 0).toLocaleString()} of ${(meta.original_point_count ?? 0).toLocaleString()} points${meta.downsampled ? ' (downsampled)' : ''}`);
          setLoading(false);
          return;
        }
        if (pv.status !== 415) {
          throw new Error((await pv.json().catch(() => ({}))).detail || `Preview failed (${pv.status})`);
        }
        // Non-E57 (ply/las/xyz): download and parse client-side.
        const res = await fetch(`/api/bridges/${sc}/${bn}/point-cloud`);
        if (!res.ok) throw new Error(`Not found (${res.status})`);
        const blob = await res.blob();
        const cd = res.headers.get('content-disposition') ?? '';
        const match = cd.match(/filename="?([^"]+)"?/);
        const name = match?.[1] ?? 'cloud.xyz';
        const file = new File([blob], name, { type: blob.type });
        originalFileRef.current = file;
        await processFile(file);
      } catch (err) {
        setStatus(`Load error: ${err instanceof Error ? err.message : String(err)}`);
        setLoading(false);
      }
    };
  }, [hasBridge, bridgeStateCode, bridgeNumber, processFile]);

  // Auto-load the saved cloud once we know one exists (so the tab isn't blank).
  const autoLoadedRef = useRef('');
  useEffect(() => {
    const bridgeKey = hasBridge ? `${bridgeStateCode}:${bridgeNumber}` : '';
    // A manually opened cloud belongs to the previously viewed bridge; never let
    // it suppress loading the saved processed result for the newly selected one.
    if (bridgeKey && autoLoadedRef.current && autoLoadedRef.current !== bridgeKey) {
      originalFileRef.current = null;
    }
    if (bridgeKey && cloudInfo?.exists && autoLoadedRef.current !== bridgeKey && !originalFileRef.current) {
      autoLoadedRef.current = bridgeKey;
      loadFromBridge();
    }
  }, [hasBridge, cloudInfo, loadFromBridge]);

  const handleDrop = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer.files?.[0]; if (f) processFile(f);
  }, [processFile]);

  // Paint every point by its segment-class colour, using the dynamic class list.
  const applySegColors = useCallback((labels: Uint8Array, classes: SegClass[]) => {
    if (!pointsRef.current) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const cnt = geo.getAttribute('position').array.length / 3;
    if (!origColorsRef.current) {
      origColorsRef.current = new Float32Array(geo.getAttribute('color').array as Float32Array);
    }
    const lut = new Map<number, [number, number, number]>();
    classes.forEach(c => lut.set(c.id, c.color_rgb));
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    const colors = colAttr.array as Float32Array;
    for (let i = 0; i < cnt; i++) {
      const [r, g, b] = lut.get(labels[i]) ?? [0.5, 0.5, 0.5];
      colors[i * 3] = r; colors[i * 3 + 1] = g; colors[i * 3 + 2] = b;
    }
    colAttr.needsUpdate = true;
  }, []);

  // Colour points by SAM instance id (distinct hue per instance; -1 = unassigned).
  const applyInstanceColors = useCallback((inst: Int32Array) => {
    if (!pointsRef.current) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const cnt = geo.getAttribute('position').array.length / 3;
    if (!origColorsRef.current) {
      origColorsRef.current = new Float32Array(geo.getAttribute('color').array as Float32Array);
    }
    const hslToRgb = (h: number, s: number, l: number): [number, number, number] => {
      const k = (n: number) => (n + h * 12) % 12;
      const a = s * Math.min(l, 1 - l);
      const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
      return [f(0), f(8), f(4)];
    };
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    const colors = colAttr.array as Float32Array;
    for (let i = 0; i < cnt; i++) {
      const id = inst[i];
      let r = 0.45, g = 0.45, b = 0.5;             // unassigned â†’ grey
      if (id >= 0) { const h = (id * 0.61803398875) % 1; [r, g, b] = hslToRgb(h, 0.65, 0.55); }
      colors[i * 3] = r; colors[i * 3 + 1] = g; colors[i * 3 + 2] = b;
    }
    colAttr.needsUpdate = true;
  }, []);

  // Collect the rendered (centred) positions to POST to the backend analysers.
  // The render buffer is already Float32 â€” send its bytes directly (one memcpy,
  // no per-point JS objects, no stringify). Colors, when requested and present,
  // are appended after the positions; the backend splits on `has_colors=1`.
  const gatherPointsBinary = useCallback((withColors = false): { buf: ArrayBuffer; hasColors: boolean } => {
    const geo = pointsRef.current!.geometry as THREE.BufferGeometry;
    const visiblePos = geo.getAttribute('position').array as Float32Array;
    // One-step analysis hides removed points by writing NaNs into the visible
    // position buffer. Follow-up analysis/segmentation must still operate on the
    // original loaded cloud, not the currently hidden subset.
    let hasHiddenPoints = false;
    for (let i = 0; i < visiblePos.length; i += 3) {
      if (!Number.isFinite(visiblePos[i]) || !Number.isFinite(visiblePos[i + 1]) || !Number.isFinite(visiblePos[i + 2])) {
        hasHiddenPoints = true;
        break;
      }
    }
    const pos = hasHiddenPoints && origPosRef.current?.length === visiblePos.length
      ? origPosRef.current
      : visiblePos;
    const ca = withColors ? geo.getAttribute('color') : undefined;
    if (!ca) return { buf: pos.slice().buffer, hasColors: false };
    const visibleCol = ca.array as Float32Array;
    const col = hasHiddenPoints && origColorsRef.current?.length === visibleCol.length
      ? origColorsRef.current
      : visibleCol;
    const out = new Float32Array(pos.length + col.length);
    out.set(pos, 0);
    out.set(col, pos.length);
    return { buf: out.buffer, hasColors: true };
  }, []);

  const clearMesh = useCallback(() => {
    if (meshRef.current && sceneRef.current) {
      sceneRef.current.remove(meshRef.current);
      meshRef.current.geometry.dispose();
      (meshRef.current.material as THREE.Material).dispose();
      meshRef.current = null;
    }
    setMeshInfo(null);
    setViewMode('points');
    if (pointsRef.current) pointsRef.current.visible = true;
  }, []);

  // Render a returned Poisson mesh (centred vertices + triangle faces).
  const loadMesh = useCallback((d: any) => {
    if (!sceneRef.current) return;
    if (meshRef.current) {
      sceneRef.current.remove(meshRef.current);
      meshRef.current.geometry.dispose();
      (meshRef.current.material as THREE.Material).dispose();
      meshRef.current = null;
    }
    const verts: number[][] = d.vertices || [];
    const faces: number[][] = d.faces || [];
    const pos = new Float32Array(verts.length * 3);
    for (let i = 0; i < verts.length; i++) { pos[i * 3] = verts[i][0]; pos[i * 3 + 1] = verts[i][1]; pos[i * 3 + 2] = verts[i][2]; }
    const idx = new Uint32Array(faces.length * 3);
    for (let i = 0; i < faces.length; i++) { idx[i * 3] = faces[i][0]; idx[i * 3 + 1] = faces[i][1]; idx[i * 3 + 2] = faces[i][2]; }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setIndex(new THREE.BufferAttribute(idx, 1));
    geo.computeVertexNormals();
    const mat = new THREE.MeshStandardMaterial({ color: 0x9aa6b8, metalness: 0.1, roughness: 0.85, side: THREE.DoubleSide, flatShading: false });
    const mesh = new THREE.Mesh(geo, mat);
    sceneRef.current.add(mesh);
    meshRef.current = mesh;
    if (pointsRef.current) pointsRef.current.visible = false;  // show surface, hide points
    setMeshInfo({ vertex_count: d.vertex_count ?? verts.length, face_count: d.face_count ?? faces.length });
    setViewMode('surface');
    renderPendingRef.current = 8;
  }, []);

  const toggleView = useCallback((mode: 'points' | 'surface') => {
    setViewMode(mode);
    if (pointsRef.current) pointsRef.current.visible = mode === 'points';
    if (meshRef.current) meshRef.current.visible = mode === 'surface';
    renderPendingRef.current = 6;
  }, []);

  // â”€â”€ MLS smooth + outlier removal â€” reloads the cleaned cloud â”€â”€
  const smoothCloud = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    setSmoothBusy(true);
    setStatus('Cleaning + smoothing the surface on the serverâ€¦');
    try {
      const { buf, hasColors } = gatherPointsBinary(true);
      const url = '/api/point-cloud/smooth' + binQS({
        origin: `${stats.originX},${stats.originY},${stats.originZ}`, has_colors: hasColors, binary: true });
      const res = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: buf });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Smoothing failed (${res.status})`);
      const { result: pr, meta } = await readCloudResponse(res);
      if (meta.status !== 'success') { setStatus(`Smoothing: ${meta.status}`); return; }
      clearMesh();
      setSegLabels(null); setSegDone(false); setSegClasses(DEFAULT_SEG_CLASSES); setClearanceInfo(null);
      setMeasurement({ groundZ: null, soffitZ: null });
      originalFileRef.current = null;   // derived cloud â€” save/export should serialise THIS, not the original file
      loadIntoScene(pr);
      setStatus(`Smoothed Â· ${(meta.rendered_point_count ?? 0).toLocaleString()} pts Â· removed ${(meta.noise_removed ?? 0).toLocaleString()} outliers/vegetation`);
    } catch (err) {
      setStatus(`Smoothing error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSmoothBusy(false);
    }
  }, [stats, gatherPointsBinary, loadIntoScene, clearMesh]);

  // â”€â”€ Poisson surface mesh (COLMAP) â€” renders a watertight surface â”€â”€
  const buildSurface = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    setSurfaceBusy(true);
    setStatus('Reconstructing surface mesh (Poisson)â€¦ this can take a minute');
    try {
      const url = '/api/point-cloud/mesh' + binQS({ origin: `${stats.originX},${stats.originY},${stats.originZ}` });
      const res = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: gatherPointsBinary().buf });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Surface failed (${res.status})`);
      const d = await res.json();
      if (d.status !== 'success') { setStatus(`Surface: ${d.status}`); return; }
      loadMesh(d);
      setStatus(`Surface built Â· ${(d.vertex_count ?? 0).toLocaleString()} vertices Â· ${(d.face_count ?? 0).toLocaleString()} faces`);
    } catch (err) {
      setStatus(`Surface error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSurfaceBusy(false);
    }
  }, [stats, gatherPointsBinary, loadMesh]);

  // â”€â”€ Backend (AI) segmentation â€” robust geometry + Qwen naming (+ optional SAM) â”€â”€
  const runBackendSegmentation = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    setSegRunning(true);
    setStatus('Hosted AI segmentation + naming on the server...');
    try {
      const url = (hasBridge
        ? `/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/segment`
        : '/api/point-cloud/segment') + binQS({ use_qwen: useHostedVlm, use_local_sam: false });
      const res = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: gatherPointsBinary().buf });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Segmentation failed (${res.status})`);
      const d = await res.json();
      if (d.status !== 'success') { setStatus(`Segmentation: ${d.status}`); setSegRunning(false); return; }
      const classes: SegClass[] = (d.classes || []).map((c: any) => ({
        id: c.id, key: c.key, name: c.name,
        color_rgb: c.color_rgb, color_hex: c.color_hex,
        count: c.count ?? 0, material: c.material, is_clearance: c.is_clearance,
      }));
      const labels = d.labels_b64u8 ? b64ToU8(d.labels_b64u8) : Uint8Array.from((d.labels ?? []) as number[]);
      setSegClasses(classes);
      setSegLabels(labels);
      setSegDone(true);
      setSegVisible(Array(classes.length).fill(true));
      // If SAM ran, store instances and SHOW them (distinct colour per instance) so
      // the effect is visible; otherwise colour by component class.
      if (d.sam_used && (d.instances_b64i32 || Array.isArray(d.instances))) {
        const inst = d.instances_b64i32 ? b64ToI32(d.instances_b64i32) : Int32Array.from(d.instances as number[]);
        setInstances(inst);
        setSegColorMode('instances');
        applyInstanceColors(inst);
      } else {
        setInstances(null);
        setSegColorMode('components');
        applySegColors(labels, classes);
      }
      const named = d.named_by_qwen ? ' Â· named by AI' : '';
      const regions = d.sam_used ? ` Â· ${d.instance_count} AI regions (coloured)` : '';
      setStatus(`Segmented into ${classes.filter(c => c.count > 0).length} components${named}${regions}`);
    } catch (err) {
      setStatus(`Segmentation error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSegRunning(false);
    }
  }, [stats, hasBridge, bridgeStateCode, bridgeNumber, useHostedVlm, applySegColors, applyInstanceColors, gatherPointsBinary]);

  // â”€â”€ Backend clearance â€” RANSAC ground/soffit + DB cross-check â”€â”€
  const runBackendClearance = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    setClearanceBusy(true);
    setStatus('Computing vertical clearance on the serverâ€¦');
    try {
      const url = (hasBridge
        ? `/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/clearance`
        : '/api/point-cloud/clearance') + binQS({
          origin: `${stats.originX},${stats.originY},${stats.originZ}`,
          scale_known: cloudScaleKnownRef.current });
      const res = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: gatherPointsBinary().buf });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Clearance failed (${res.status})`);
      const d = await res.json();
      if (d.status !== 'success') { setStatus(`Clearance: ${d.status}`); setClearanceBusy(false); return; }
      setMeasurement({ groundZ: d.ground_z, soffitZ: d.soffit_z });
      setClearanceInfo(d);
      setStatus(d.scale_known === false
        ? `Clearance ${d.clearance_m} (relative units â€” uncalibrated reconstruction) Â· ${d.method} Â· ${d.confidence} confidence`
        : `Clearance ${d.clearance_m} m (${d.clearance_ft} ft) Â· ${d.method} Â· ${d.confidence} confidence`);
    } catch (err) {
      setStatus(`Clearance error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setClearanceBusy(false);
    }
  }, [stats, hasBridge, bridgeStateCode, bridgeNumber, gatherPointsBinary]);

  // One-step processing: clean â†’ isolate bridge â†’ segment â†’ fit structural volumes â†’ measure.
  const runAnalyze = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    setAnalyzing(true);
    setStatus('Processing bridge (clean, isolate, segment, fit, measure)...');
    try {
      const url = (hasBridge
        ? `/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/analyze`
        : '/api/point-cloud/analyze') + binQS({
          origin: `${stats.originX},${stats.originY},${stats.originZ}`,
          scale_known: cloudScaleKnownRef.current,
          use_vlm: useHostedVlm,
          use_ai_cleaning: useAiCleaning,
          use_local_sam: false,
          model_mode: 'clearance' });
      const res = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: gatherPointsBinary().buf });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Analysis failed (${res.status})`);
      const d = await res.json();
      if (d.status !== 'success') { setStatus(`Analysis: ${d.status}`); setAnalyzing(false); return; }
      setAnalysisInfo(d);
      applyAnalysisResult(d);                   // colour kept points + hide removed (one pass)
      setClearanceInfo(d.clearance);            // reuse the clearance drawing + panel
      if (d.clearance) setMeasurement({ groundZ: d.clearance.ground_z ?? null, soffitZ: d.clearance.soffit_z ?? null });
      if (hasBridge && d.persistence?.saved) {
        setCloudInfo(prev => ({ ...(prev ?? { exists: true }), exists: true, processed_exists: true, processed_point_count: d.persistence.processed_point_count }));
      }
      const cl = d.clearance || {};
      setStatus(`Modeled: ${d.component_count} components Â· clearance ${cl.scale_known === false ? `${cl.min_clearance_m} (relative)` : `${cl.min_clearance_ft}â€² min`} Â· kept ${d.cleaning?.kept_points?.toLocaleString()} pts`);
    } catch (err) {
      setStatus(`Analysis error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAnalyzing(false);
    }
  }, [stats, hasBridge, bridgeStateCode, bridgeNumber, useHostedVlm, useAiCleaning, gatherPointsBinary, applyAnalysisResult]);

  const toggleSegVisibility = useCallback((i: number) => {
    if (!pointsRef.current || !segLabels) return;
    const cls = segClasses[i];
    if (!cls) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const cnt = geo.getAttribute('position').array.length / 3;
    const newVis = segVisible.map((v, j) => j === i ? !v : v);
    setSegVisible(newVis);
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    const colors  = colAttr.array as Float32Array;
    const visible = newVis[i];
    // hidden â†’ paint with the (light) viewer background so the class disappears
    const [r, g, b] = visible ? cls.color_rgb : [0.957, 0.965, 0.98];
    for (let k = 0; k < cnt; k++) {
      if (segLabels[k] === cls.id) {
        colors[k * 3] = r;
        colors[k * 3 + 1] = g;
        colors[k * 3 + 2] = b;
      }
    }
    colAttr.needsUpdate = true;
  }, [segLabels, segVisible, segClasses]);

  const resetSegColors = useCallback(() => {
    if (!pointsRef.current || !origColorsRef.current) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    colAttr.array.set(origColorsRef.current);
    colAttr.needsUpdate = true;
    setSegLabels(null);
    setSegDone(false);
    setSegClasses(DEFAULT_SEG_CLASSES);
    setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
    setInstances(null);
    setSegColorMode('components');
    setStatus('Reverted to original point colors.');
  }, []);

  const clearance = measurement.groundZ !== null && measurement.soffitZ !== null
    ? parseFloat(Math.abs(measurement.soffitZ - measurement.groundZ).toFixed(3)) : null;

  return (
    <div className="bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
      style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 600 }}>
      <style>{PC_CSS}</style>

      {/* Header */}
      <div className="border-b border-gray-200 dark:border-gray-800"
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '14px 20px', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: 16 }}>Point Cloud Viewer</div>
            {hasBridge ? (
              <div style={{ fontSize: 12, color: '#0ea5e9', fontWeight: 600 }}>
                {bridgeName ? `${bridgeName} Â· ` : ''}{bridgeNumber}
              </div>
            ) : (
              <div style={{ fontSize: 12 }} className="text-gray-500">3D Inspection Â· Clearance Â· AI Segmentation</div>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          {/* Bridge save/load buttons */}
          {hasBridge && cloudInfo?.exists && (
            <button
              onClick={loadFromBridge}
              disabled={loading}
              className="pcbtn pcbtn-outline"
              title={cloudInfo.processed_exists ? 'Load the complete saved processed result' : `Load saved: ${cloudInfo.filename}`}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 15v4c0 1.1.9 2 2 2h14a2 2 0 0 0 2-2v-4M17 9l-5 5-5-5M12 12.8V2.5"/>
              </svg>
              {cloudInfo.processed_exists ? 'Load Saved Result' : 'Load Saved Cloud'}
            </button>
          )}
          {hasBridge && cloudInfo?.processed_exists && (
            <a
              href={`/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/processed/download?format=ply`}
              className="pcbtn pcbtn-outline"
              title="Download the bridge's saved cleaned and classified point cloud"
            >
              Download Result .ply
            </a>
          )}
          {hasBridge && stats && (
            <button
              onClick={saveToBridge}
              disabled={saving}
              className="pcbtn pcbtn-success"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                <polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>
              </svg>
              {saving ? 'Savingâ€¦' : (cloudInfo?.exists ? 'Re-save' : 'Save to Bridge')}
            </button>
          )}
          {stats && (
            <button
              onClick={exportPLY}
              disabled={loading}
              title="Download the current cloud as a .ply file"
              className="pcbtn pcbtn-outline"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
              </svg>
              Export .ply
            </button>
          )}
          <label className="pcbtn pcbtn-accent" style={{ fontSize: 13 }}>
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            Open File
            <input type="file" accept=".xyz,.ply,.e57,.las,.npz,.csv,.txt,.pcd"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) processFile(f); e.target.value = ''; }}
              style={{ display: 'none' }} />
          </label>
          {onClose && (
            <button onClick={onClose} className="pcbtn pcbtn-close" title="Close viewer">
              <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>

        {/* 3D Viewport */}
        <div ref={mountRef}
          style={{ flex: 1, position: 'relative', background: '#f4f6fa', cursor: measureStep !== 'idle' ? 'crosshair' : 'grab' }}
          onDrop={handleDrop}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onClick={handleCanvasClick}>

          {dragging && (
            <div style={{ position: 'absolute', inset: 0, zIndex: 10, background: 'rgba(37,99,235,0.35)', border: '3px dashed #60a5fa', borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <span style={{ color: '#93c5fd', fontWeight: 700, fontSize: 18 }}>Drop point cloud file here</span>
            </div>
          )}

          {loading && (
            <div style={{ position: 'absolute', inset: 0, zIndex: 20, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(244,246,250,0.85)' }}>
              <div style={{ textAlign: 'center' }}>
                <svg style={{ width: 44, height: 44, margin: '0 auto', display: 'block', animation: 'spin 1s linear infinite' }} xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="10" stroke="#e2e8f0" strokeWidth="4"/>
                  <path fill="#6366f1" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                </svg>
                <div style={{ marginTop: 12, color: '#64748b', fontSize: 14 }}>Parsing point cloudâ€¦</div>
              </div>
            </div>
          )}

          {!stats && !loading && (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <div style={{ textAlign: 'center' }}>

                <div style={{ fontWeight: 600, fontSize: 15, color: '#64748b' }}>No point cloud loaded</div>
                <div style={{ fontSize: 13, color: '#94a3b8', marginTop: 4 }}>Drag &amp; drop or click "Open File"</div>
                <div style={{ fontSize: 12, color: '#cbd5e1', marginTop: 6 }}>.xyz Â· .ply Â· .e57 Â· .las Â· .npz Â· .csv</div>
              </div>
            </div>
          )}

          {measureStep !== 'idle' && (
            <div style={{ position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)', background: '#fbbf24', color: '#78350f', padding: '5px 16px', borderRadius: 999, fontWeight: 700, fontSize: 13, zIndex: 5, pointerEvents: 'none' }}>
              Click {measureStep === 'ground' ? 'ground surface' : 'bridge soffit (underside)'}
            </div>
          )}

          {clearance !== null && (
            <div style={{ position: 'absolute', top: 12, right: 12, background: 'rgba(255,255,255,0.94)', border: '1px solid #a7f3d0', borderRadius: 10, padding: '10px 16px', zIndex: 5, backdropFilter: 'blur(6px)', boxShadow: '0 2px 10px rgba(15,23,42,0.08)' }}>
              <div style={{ fontSize: 11, color: '#059669', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 1 }}>Clearance</div>
              <div style={{ fontSize: 26, fontWeight: 800, color: '#047857', lineHeight: 1.1 }}>{clearance.toFixed(3)} m</div>
              <div style={{ fontSize: 11, color: '#64748b' }}>{(clearance * 3.28084).toFixed(2)} ft</div>
            </div>
          )}

          <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, background: 'rgba(255,255,255,0.85)', padding: '4px 14px', fontSize: 12, color: '#64748b', backdropFilter: 'blur(4px)', borderTop: '1px solid #e2e8f0' }}>
            {measureStep !== 'idle' && <span style={{ color: '#d97706', marginRight: 6 }}>â—</span>}
            {status}
          </div>
        </div>

        {/* Right Panel */}
        <div style={{ width: 240, display: 'flex', flexDirection: 'column', overflow: 'auto', background: '#fbfcfe', borderLeft: '1px solid ' + SX.border }}>

          <PanelSection title="Cloud Statistics">
            {stats ? (
              <div style={{ fontSize: 12, display: 'flex', flexDirection: 'column', gap: 3 }}>
                <StatRow label="Points" value={stats.count.toLocaleString()} />
                <StatRow label="File"   value={fileName} mono />
                <div style={{ marginTop: 5, paddingTop: 6, borderTop: '1px solid ' + SX.border }}>
                  <div style={{ fontWeight: 600, fontSize: 10, marginBottom: 5, color: SX.label, letterSpacing: 0.4 }}>BOUNDING BOX</div>
                  <StatRow label="X span" value={`${stats.spanX.toFixed(2)} m`} />
                  <StatRow label="Y span" value={`${stats.spanY.toFixed(2)} m`} />
                  <StatRow label="Z span" value={`${stats.spanZ.toFixed(2)} m`} />
                  <StatRow label="Z min"  value={`${stats.minZ.toFixed(3)} m`} />
                  <StatRow label="Z max"  value={`${stats.maxZ.toFixed(3)} m`} />
                </div>
              </div>
            ) : <div style={{ fontSize: 12, color: SX.label }}>No data loaded</div>}
          </PanelSection>

          <PanelSection title="Render">
            <div style={{ fontSize: 12, marginBottom: 4, display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: SX.text }}>Point Size</span>
              <span style={{ color: SX.label, fontFamily: 'ui-monospace, monospace' }}>{pointSize.toFixed(2)}</span>
            </div>
            <input type="range" min={0.005} max={0.8} step={0.005} value={pointSize}
              onChange={(e) => setPointSize(parseFloat(e.target.value))}
              style={{ width: '100%', accentColor: SX.indigo }} />
          </PanelSection>

          <PanelSection title="Bridge Analysis">
            {!stats ? <div style={{ fontSize: 12, color: SX.label }}>Load a point cloud first</div> : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
                {/* One-step pipeline: clean â†’ isolate â†’ segment â†’ fit volumes â†’ measure. */}
                <button onClick={runAnalyze} disabled={analyzing}
                  className="pcbtn pcbtn-primary pcbtn-block"
                  title="Cleans the scan, isolates the bridge and what's under it, segments it into component volumes, fits their surfaces and measures clearance â€” one step.">
                  {analyzing ? <>{sxSpinner('#fff')}Processingâ€¦</> : 'Process Bridge'}
                </button>
                {analysisInfo && (
                  <div style={{ padding: '8px 10px', borderRadius: 8, background: '#fff', border: '1px solid ' + SX.border, display: 'flex', flexDirection: 'column', gap: 5, fontSize: 11 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 9.5, color: SX.indigo, fontWeight: 700, letterSpacing: 0.5 }}>
                        {analysisInfo.component_count} COMPONENT VOLUMES
                      </span>
                      <span style={{ display: 'flex', gap: 4 }}>
                        {analysisInfo.hosted_cleaning_used && (
                          <span title="The hosted vision model reviewed candidate clutter during cleaning."
                            style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: 0.4, color: '#0369a1', background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 4, padding: '1px 5px' }}>
                            HOSTED AI-CLEANED
                          </span>
                        )}
                        {false && analysisInfo.sam_used && (
                          <span title="GPU-assisted instance segmentation removed clutter that geometry alone cannot separate."
                            style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: 0.4, color: '#0369a1', background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 4, padding: '1px 5px' }}>
                            â—ˆ GPU-CLEANED
                          </span>
                        )}
                        {analysisInfo.vlm_used && (
                          <span title="Each component was reviewed by the hosted vision model, which confirmed bridge structure and removed clutter."
                            style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: 0.4, color: '#7c3aed', background: '#f5f3ff', border: '1px solid #ddd6fe', borderRadius: 4, padding: '1px 5px' }}>
                            âœ¦ AI-REVIEWED
                          </span>
                        )}
                      </span>
                    </div>
                    {Array.isArray(analysisInfo.warnings) && analysisInfo.warnings.length > 0 && (
                      <div style={{ background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 6, padding: '5px 7px', display: 'flex', flexDirection: 'column', gap: 3 }}>
                        {analysisInfo.warnings.map((w: string, i: number) => (
                          <span key={i} style={{ fontSize: 9.5, color: '#92400e' }}>âš  {w}</span>
                        ))}
                      </div>
                    )}
                    {analysisInfo.components.map((c: any, i: number) => (
                      <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 6 }}>
                        <span style={{ display: 'flex', alignItems: 'flex-start', gap: 5, minWidth: 0 }}>
                          <span style={{ width: 9, height: 9, borderRadius: 2, background: c.color, flexShrink: 0, marginTop: 2 }} />
                          <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.label}</span>
                            {c.vlm_label && (
                              <span title="Vision-model identification" style={{ fontSize: 9, color: '#7c3aed', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                âœ¦ {c.vlm_label}
                              </span>
                            )}
                          </span>
                        </span>
                        <span style={{ color: SX.label, whiteSpace: 'nowrap' }}>
                          {c.dims_m[0]}Ã—{c.dims_m[1]}Ã—{c.dims_m[2]}m Â· <span style={{ color: c.coverage_pct >= 50 ? '#059669' : '#d97706', fontWeight: 700 }}>{c.coverage_pct}%</span>
                        </span>
                      </div>
                    ))}
                    <div style={{ display: 'flex', gap: 12, borderTop: '1px solid ' + SX.border, paddingTop: 5 }}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: SX.text, cursor: 'pointer' }}>
                        <input type="checkbox" checked={showVolumes} onChange={e => setShowVolumes(e.target.checked)} style={{ accentColor: SX.indigo }} />
                        Fitted volumes
                      </label>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, borderTop: '1px solid ' + SX.border, paddingTop: 5 }}
                      title="Removed points are noise, vegetation and off-structure clutter â€” they are hidden in the view.">
                      <ClrRow label="Kept" value={`${analysisInfo.cleaning.kept_points?.toLocaleString()} pts`} />
                      <ClrRow label="Removed" value={`${analysisInfo.cleaning.removed_noise?.toLocaleString()} pts`} muted />
                      {analysisInfo.cleaning.fragment_removed != null && analysisInfo.cleaning.fragment_removed > 0 && (
                        <ClrRow label="Fragments" value={`${analysisInfo.cleaning.fragment_removed.toLocaleString()} pts`} muted />
                      )}
                    </div>
                    <button onClick={restoreOriginalCloud}
                      className="pcbtn pcbtn-ghost pcbtn-block" style={{ fontSize: 11, padding: '5px 8px' }}>
                      Show full original cloud
                    </button>
                  </div>
                )}
                {clearance !== null && (
                  <div style={{ padding: '9px 11px', borderRadius: 8, background: '#ecfdf5', border: '1px solid #a7f3d0' }}>
                    <div style={{ fontSize: 9.5, color: '#059669', fontWeight: 700, letterSpacing: 0.6 }}>VERTICAL CLEARANCE</div>
                    {cloudScaleKnownRef.current ? (
                      <>
                        <div style={{ fontSize: 22, fontWeight: 800, color: '#047857', lineHeight: 1.15 }}>{clearance.toFixed(3)} m</div>
                        <div style={{ fontSize: 11, color: '#10b981', fontWeight: 600 }}>{(clearance * 3.28084).toFixed(2)} ft</div>
                      </>
                    ) : (
                      <>
                        <div style={{ fontSize: 22, fontWeight: 800, color: '#047857', lineHeight: 1.15 }}>{clearance.toFixed(3)}</div>
                        <div style={{ fontSize: 11, color: '#b45309', fontWeight: 600 }}>relative units (uncalibrated reconstruction)</div>
                      </>
                    )}
                  </div>
                )}
                {clearanceInfo && clearanceInfo.status === 'success' && (
                  <div style={{ padding: '8px 10px', borderRadius: 8, background: '#fff', border: '1px solid ' + SX.border, display: 'flex', flexDirection: 'column', gap: 4, fontSize: 11 }}>
                    {clearanceInfo.scale_known === false && (
                      <div title="Photogrammetric reconstructions carry no absolute scale â€” values are relative units, not meters/feet."
                        style={{ padding: '4px 8px', borderRadius: 6, background: '#fffbeb', border: '1px solid #fde68a', color: '#92400e', fontSize: 10.5, fontWeight: 600 }}>
                        âš  Relative units â€” uncalibrated
                      </div>
                    )}
                    {clearanceInfo.waterway?.is_waterway && (
                      <div title="Waterway under the bridge â€” navigation vertical clearance from the soffit to an inferred reference water level. Confirm against the controlling gage datum."
                        style={{ padding: '4px 8px', borderRadius: 6, background: '#eff6ff', border: '1px solid #bfdbfe', color: '#1e40af', fontSize: 10.5, fontWeight: 600 }}>
                        ðŸŒŠ Waterway â€” navigation clearance
                      </div>
                    )}
                    <ClrRow label={clearanceInfo.waterway?.is_waterway ? 'Navigation clr.' : 'Controlling min.'} value={clearanceInfo.scale_known === false
                      ? `${clearanceInfo.min_clearance_m?.toFixed(3)} (relative)`
                      : `${clearanceInfo.min_clearance_m?.toFixed(3)} m / ${(clearanceInfo.min_clearance_m * 3.28084).toFixed(2)} ft`} />
                    {!clearanceInfo.waterway?.is_waterway && (
                      <ClrRow label="Typical (centre)" value={clearanceInfo.scale_known === false
                        ? `${clearanceInfo.typical_clearance_m?.toFixed(3)} (relative)`
                        : `${clearanceInfo.typical_clearance_m?.toFixed(3)} m`} />
                    )}
                    <ClrRow label="Method" value={`${({ roadway_sections: 'per-roadway', waterway_navigation: 'navigation', global_fallback: 'global est.', road_center: 'road centre' }[clearanceInfo.method as string]) || clearanceInfo.method} Â· ${clearanceInfo.confidence}`} muted />
                    {clearanceInfo.roadway_count > 1 && (
                      <div style={{ marginTop: 3, paddingTop: 5, borderTop: '1px solid ' + SX.border }}>
                        <div style={{ color: SX.label, marginBottom: 3 }}>{clearanceInfo.roadway_count} roadways under the deck</div>
                        {clearanceInfo.roadways.map((rw: any, i: number) => (
                          <ClrRow key={i} label={`  roadway ${i + 1}`} muted
                            value={clearanceInfo.scale_known === false
                              ? `min ${rw.controlling_min_m?.toFixed(2)} Â· ctr ${rw.center_m?.toFixed(2)} (rel)`
                              : `min ${(rw.controlling_min_m * 3.28084).toFixed(1)}â€² Â· ctr ${(rw.center_m * 3.28084).toFixed(1)}â€²`} />
                        ))}
                      </div>
                    )}
                    {clearanceInfo.noise_removed > 0 && (
                      <ClrRow label="Cleaned" value={`${clearanceInfo.noise_removed.toLocaleString()} noise pts`} muted />
                    )}
                    {clearanceInfo.db_clearance_ft != null && (
                      <div style={{ marginTop: 3, paddingTop: 5, borderTop: '1px solid ' + SX.border, display: 'flex', flexDirection: 'column', gap: 4 }}>
                        <ClrRow label="DB recorded (BH13)" value={`${clearanceInfo.db_clearance_ft.toFixed(2)} ft`} />
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span style={{ color: SX.label }}>Î” measuredâˆ’DB</span>
                          <span style={{ fontWeight: 700, color: clearanceInfo.db_match ? '#059669' : '#d97706' }}>
                            {clearanceInfo.db_delta_ft > 0 ? '+' : ''}{clearanceInfo.db_delta_ft?.toFixed(2)} ft {clearanceInfo.db_match ? 'âœ“' : 'âš '}
                          </span>
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {(measurement.groundZ !== null || measurement.soffitZ !== null) && (
                  <button onClick={() => { setMeasurement({ groundZ: null, soffitZ: null }); setMeasureStep('idle'); setStatus('Measurement cleared.'); }}
                    className="pcbtn pcbtn-danger pcbtn-block">
                    Clear Measurement
                  </button>
                )}
              </div>
            )}
          </PanelSection>

          <PanelSection title="Components" last>
            {!segDone
              ? <div style={{ fontSize: 12, color: SX.label }}>No components yet</div>
              : <SegmentLegend classes={segClasses} segVisible={segVisible} onToggle={toggleSegVisibility} onReset={restoreOriginalCloud}/>}
          </PanelSection>
        </div>
      </div>
    </div>
  );
};

const PanelSection: React.FC<{ title: string; children: React.ReactNode; last?: boolean }> = ({ title, children, last }) => (
  <section style={{ padding: '10px 12px', borderBottom: last ? 'none' : '1px solid ' + SX.border }}>
    <div style={{ fontWeight: 700, fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.7, marginBottom: 7, color: SX.title }}>{title}</div>
    {children}
  </section>
);

interface SegmentLegendProps {
  classes: SegClass[];
  segVisible: boolean[];
  onToggle: (idx: number) => void;
  onReset: () => void;
}
const SegmentLegend: React.FC<SegmentLegendProps> = ({ classes, segVisible, onToggle, onReset }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
    {classes.map((c, idx) => {
      if (!c.count) return null;
      const vis = segVisible[idx] ?? true;
      return (
        <button key={c.id} onClick={() => onToggle(idx)}
          style={{ display: 'flex', alignItems: 'flex-start', gap: 7, padding: '5px 7px', borderRadius: 7, border: `1px solid ${vis ? c.color_hex + '66' : SX.border}`, background: vis ? `${c.color_hex}14` : '#f8fafc', cursor: 'pointer', textAlign: 'left', width: '100%', opacity: vis ? 1 : 0.55 }}>
          <span style={{ width: 9, height: 9, borderRadius: '50%', background: c.color_hex, flexShrink: 0, display: 'inline-block', marginTop: 3 }}/>
          <span style={{ flex: 1, minWidth: 0, textAlign: 'left' }}>
            <span style={{ fontSize: 11, color: vis ? SX.text : SX.label, fontWeight: 500, display: 'block', lineHeight: 1.25 }}>{c.name}</span>
            {c.material && <span style={{ fontSize: 9, color: SX.label }}>material: {c.material}</span>}
          </span>
          {c.is_clearance && <span style={{ fontSize: 8.5, color: '#059669', fontWeight: 700, textTransform: 'uppercase', marginTop: 2 }}>clr</span>}
          <span style={{ fontSize: 10, color: SX.label, fontFamily: 'ui-monospace, monospace', marginTop: 1 }}>{c.count.toLocaleString()}</span>
        </button>
      );
    })}
    <button onClick={onReset} className="pcbtn pcbtn-ghost pcbtn-block" style={{ marginTop: 2 }}>â†© Restore Colours</button>
  </div>
);

const ClrRow: React.FC<{ label: string; value: string; muted?: boolean }> = ({ label, value, muted }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
    <span style={{ color: SX.label }}>{label}</span>
    <span style={{ color: muted ? '#94a3b8' : SX.text, fontFamily: muted ? undefined : 'ui-monospace, monospace', textAlign: 'right' }}>{value}</span>
  </div>
);

const StatRow: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 12, gap: 8 }}>
    <span style={{ flexShrink: 0, color: SX.label }}>{label}</span>
    <span style={{ fontFamily: mono ? 'ui-monospace, monospace' : undefined, fontWeight: 500, color: SX.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 130, textAlign: 'right' }}>{value}</span>
  </div>
);

export default PointCloud;
