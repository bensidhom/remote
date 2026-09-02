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

function copyParseResult(result: ParseResult): ParseResult {
  return {
    positions: new Float32Array(result.positions),
    colors: new Float32Array(result.colors),
    stats: { ...result.stats },
  };
}

function combineCloudResults(raw: ParseResult, processed: ParseResult): ParseResult {
  const rawCount = raw.positions.length / 3;
  const processedCount = processed.positions.length / 3;
  const processedLimit = Math.min(processedCount, Math.floor(MAX_UI_POINTS * 0.65));
  const rawLimit = Math.min(rawCount, MAX_UI_POINTS - processedLimit);
  const outputCount = rawLimit + processedLimit;
  const positions = new Float32Array(outputCount * 3);
  const colors = new Float32Array(outputCount * 3);
  const rawStep = Math.max(1, rawCount / Math.max(1, rawLimit));
  const processedStep = Math.max(1, processedCount / Math.max(1, processedLimit));
  let outputIndex = 0;
  const append = (source: ParseResult, count: number, step: number, rawLayer: boolean) => {
    for (let sample = 0; sample < count; sample++, outputIndex++) {
      const sourceIndex = Math.min(source.positions.length / 3 - 1, Math.floor(sample * step));
      const sourceOffset = sourceIndex * 3;
      const outputOffset = outputIndex * 3;
      positions[outputOffset] = source.positions[sourceOffset] + source.stats.originX - raw.stats.originX;
      positions[outputOffset + 1] = source.positions[sourceOffset + 1] + source.stats.originY - raw.stats.originY;
      positions[outputOffset + 2] = source.positions[sourceOffset + 2] + source.stats.originZ - raw.stats.originZ;
      if (rawLayer) {
        colors[outputOffset] = 0.72; colors[outputOffset + 1] = 0.75; colors[outputOffset + 2] = 0.82;
      } else {
        colors[outputOffset] = source.colors[sourceOffset];
        colors[outputOffset + 1] = source.colors[sourceOffset + 1];
        colors[outputOffset + 2] = source.colors[sourceOffset + 2];
      }
    }
  };
  append(raw, rawLimit, rawStep, true);
  append(processed, processedLimit, processedStep, false);
  return {
    positions,
    colors,
    stats: {
      count: outputCount,
      originalCount: (raw.stats.originalCount ?? rawCount) + (processed.stats.originalCount ?? processedCount),
      minX: Math.min(raw.stats.minX, processed.stats.minX), maxX: Math.max(raw.stats.maxX, processed.stats.maxX),
      minY: Math.min(raw.stats.minY, processed.stats.minY), maxY: Math.max(raw.stats.maxY, processed.stats.maxY),
      minZ: Math.min(raw.stats.minZ, processed.stats.minZ), maxZ: Math.max(raw.stats.maxZ, processed.stats.maxZ),
      spanX: Math.max(raw.stats.maxX, processed.stats.maxX) - Math.min(raw.stats.minX, processed.stats.minX),
      spanY: Math.max(raw.stats.maxY, processed.stats.maxY) - Math.min(raw.stats.minY, processed.stats.minY),
      spanZ: Math.max(raw.stats.maxZ, processed.stats.maxZ) - Math.min(raw.stats.minZ, processed.stats.minZ),
      originX: raw.stats.originX, originY: raw.stats.originY, originZ: raw.stats.originZ,
    },
  };
}

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

async function responseError(response: Response, fallback: string): Promise<Error> {
  const payload = await response.json().catch(() => ({}));
  const detail = payload?.detail;
  const message = typeof detail === 'string'
    ? detail
    : detail?.message || detail?.code || payload?.message || payload?.code || `${fallback} (${response.status})`;
  return new Error(message);
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
  dataYear?: string | number;
}

interface PointCloudSourceReview {
  horizontalUnit: 'unknown' | 'metre' | 'international_foot' | 'us_survey_foot';
  verticalUnit: 'unknown' | 'metre' | 'international_foot' | 'us_survey_foot';
  frameKind: 'unknown' | 'projected_crs' | 'engineering_local' | 'scanner_local' | 'reconstruction_relative';
  crs: string;
  upAxis: '+x' | '-x' | '+y' | '-y' | '+z' | '-z';
  evidence: string;
  reviewed: boolean;
  scanCount: number | null;
  selectedScans: string;
  registrationReference: string;
  registrationRms: string;
  registrationMaximumResidual: string;
  registrationMethod: string;
  registrationReviewed: boolean;
}

interface DurablePointCloudRun {
  job_id: string;
  run_id: string;
  data_year?: number;
  status: 'queued' | 'running' | 'cancellation_requested' | 'succeeded' | 'failed' | 'cancelled' | 'published';
  phase: string;
  progress: Record<string, any>;
  error_code?: string | null;
  source_sha256: string;
  analysis_profile_sha256: string;
  knowledge_pack_sha256: string;
  snapshot_revision_id: string;
  canonical_sha256: string;
  counts: { sources: number; vlm_calls: number; components: number; measurements: number; reviews: number };
  review_required: boolean;
  published: boolean;
  etag: string;
}

interface DurablePointCloudEvidence {
  context: Record<string, any>;
  sources: any[];
  vlm_calls: any[];
  components: any[];
  measurements: any[];
  reviews: any[];
  knowledge_context?: {
    trusted_snbi?: { field_values?: any[]; element_rows?: any[] };
    cited_report_evidence?: { chunks?: any[]; sources?: any[] };
    omissions?: Record<string, number>;
  } | null;
  truncated: boolean;
}

interface ProcessedCloudBundle {
  preview: ParseResult;
  analysis: any;
  run: DurablePointCloudRun | null;
  evidence: DurablePointCloudEvidence | null;
  renderedPointCount: number;
}

const EMPTY_SOURCE_REVIEW: PointCloudSourceReview = {
  horizontalUnit: 'unknown',
  verticalUnit: 'unknown',
  frameKind: 'unknown',
  crs: '',
  upAxis: '+z',
  evidence: '',
  reviewed: false,
  scanCount: null,
  selectedScans: '',
  registrationReference: '0',
  registrationRms: '',
  registrationMaximumResidual: '',
  registrationMethod: '',
  registrationReviewed: false,
};

// Preserve raw road-to-soffit geometry for display when an exact automatic
// feature-bound B.H.12/B.H.13 result is unavailable.
const geometryClearanceCandidate = (info: any | null | undefined): any | null => {
  if (info?.status === 'success') return info;
  if (info?.raw_geometry_candidate) {
    const raw = geometryClearanceCandidate(info.raw_geometry_candidate);
    if (raw) return raw;
  }
  const candidates = Array.isArray(info?.roadway_candidates)
    ? info.roadway_candidates
    : [];
  if (!candidates.length) return null;
  const primary = candidates[0];
  return {
    ...info,
    status: 'geometry_candidate',
    roadways: candidates,
    roadway_count: candidates.length,
    clearance_m: primary.center_m,
    min_clearance_m: primary.controlling_min_m,
    typical_clearance_m: primary.median_m,
  };
};

const compactComponentLabel = (value: unknown): string => String(value ?? 'Unknown component')
  .replace(/\s+#\d+\s*$/i, '')
  .replace(/\s+/g, ' ')
  .trim();

const PointCloud: React.FC<PointCloudProps> = ({ onClose, bridgeStateCode, bridgeNumber, bridgeName, dataYear }) => {
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
  const rawCloudDataRef = useRef<ParseResult | null>(null);
  const processedCloudBundleRef = useRef<ProcessedCloudBundle | null>(null);
  const loadedBridgeSourceShaRef = useRef<string | null>(null);

  const [stats,       setStats]       = useState<PointStats | null>(null);
  const [status,      setStatus]      = useState('Open an E57 source, or locally visualize .xyz, .ply, .las, .npz, or .csv.');
  const [loading,     setLoading]     = useState(false);
  const [dragging,    setDragging]    = useState(false);
  const [measurement, setMeasurement] = useState<Measurement>({ groundZ: null, soffitZ: null });
  const [measureStep, setMeasureStep] = useState<'idle'|'ground'|'soffit'>('idle');
  const [pointSize,   setPointSize]   = useState(0.06);
  const [fileName,    setFileName]    = useState('');
  const [cloudDisplayMode, setCloudDisplayMode] = useState<'raw' | 'processed' | 'together'>('raw');
  const [availableCloudLayers, setAvailableCloudLayers] = useState({ raw: false, processed: false });
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
  const [selectedClearancePlaneId, setSelectedClearancePlaneId] = useState('');
  const [clearanceBusy, setClearanceBusy] = useState(false);
  const [analysisInfo, setAnalysisInfo] = useState<any | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [durableRun, setDurableRun] = useState<DurablePointCloudRun | null>(null);
  const [, setDurableEvidence] = useState<DurablePointCloudEvidence | null>(null);
  const durablePollAbortRef = useRef<AbortController | null>(null);
  // Ref mirror for the render loop (its closure is created once at mount):
  // while the server analyze runs, the viewer throttles its redraws so the
  // WebGL pass doesn't compete with SAM/qwen for the same laptop GPU.
  const analyzingRef = useRef(false);
  useEffect(() => { analyzingRef.current = analyzing; }, [analyzing]);
  useEffect(() => () => durablePollAbortRef.current?.abort(), []);
  // Layer toggles for the one-pass result: fitted component volumes + Poisson mesh
  const analysisMeshRef = useRef<THREE.Mesh | null>(null);
  const [showVolumes, setShowVolumes] = useState(false);
  const [showMesh, setShowMesh] = useState(false);
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
  const [cloudInfo,   setCloudInfo]   = useState<{ exists: boolean; raw_exists?: boolean; filename?: string; size_bytes?: number; uploaded_at?: string; source_sha256?: string; accepted_result_exists?: boolean; accepted_result_data_year?: number; legacy_processed_exists?: boolean; processed_point_count?: number; source_profile?: any } | null>(null);
  const [sourceReview, setSourceReview] = useState<PointCloudSourceReview>(EMPTY_SOURCE_REVIEW);
  const [saving,      setSaving]      = useState(false);
  const [deleting,    setDeleting]    = useState(false);

  useEffect(() => {
    durablePollAbortRef.current?.abort();
    rawCloudDataRef.current = null;
    processedCloudBundleRef.current = null;
    loadedBridgeSourceShaRef.current = null;
    setAvailableCloudLayers({ raw: false, processed: false });
    setCloudDisplayMode('raw');
    setDurableRun(null);
    setDurableEvidence(null);
  }, [bridgeStateCode, bridgeNumber, dataYear]);

  useEffect(() => {
    const originalIsE57 = !!originalFileRef.current?.name.toLowerCase().endsWith('.e57');
    cloudScaleKnownRef.current = originalIsE57
      && sourceReview.reviewed
      && sourceReview.horizontalUnit === 'metre'
      && sourceReview.verticalUnit === 'metre'
      && !['unknown', 'reconstruction_relative'].includes(sourceReview.frameKind)
      && sourceReview.evidence.trim().length >= 3;
  }, [sourceReview]);

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
        if (d.filename) setFileName(d.filename);
        const declaration = d.source_profile?.coordinate_declaration;
        const frame = declaration?.frame;
        if (frame) {
          setSourceReview({
            horizontalUnit: frame.horizontal_unit ?? 'unknown',
            verticalUnit: frame.vertical_unit ?? 'unknown',
            frameKind: frame.kind ?? 'unknown',
            crs: frame.crs ?? '',
            upAxis: (() => {
              const up = Array.isArray(frame.up_vector) ? frame.up_vector.join(',') : '';
              const axes: Record<string, PointCloudSourceReview['upAxis']> = {
                '1,0,0': '+x', '-1,0,0': '-x', '0,1,0': '+y',
                '0,-1,0': '-y', '0,0,1': '+z', '0,0,-1': '-z',
              };
              return axes[up] ?? '+z';
            })(),
            evidence: declaration.evidence_summary ?? '',
            reviewed: declaration.reviewed === true,
            scanCount: d.source_profile?.source_inspection?.total_scan_count ?? null,
            selectedScans: Array.isArray(d.source_profile?.selected_scan_indices)
              ? d.source_profile.selected_scan_indices.join(',') : '',
            registrationReference: String(d.source_profile?.registration?.reference_scan_index ?? 0),
            registrationRms: d.source_profile?.registration?.rms_metres != null
              ? String(d.source_profile.registration.rms_metres) : '',
            registrationMaximumResidual: d.source_profile?.registration?.maximum_residual_metres != null
              ? String(d.source_profile.registration.maximum_residual_metres) : '',
            registrationMethod: d.source_profile?.registration?.method ?? '',
            registrationReviewed: d.source_profile?.registration?.reviewed === true,
          });
        }
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
    const original = originalFileRef.current;
    if (!original || !original.name.toLowerCase().endsWith('.e57')) {
      setStatus('Only an original E57 file can be attached as source evidence. Other formats remain local visualization only.');
      return;
    }
    const blob: Blob = original;
    const fname = original.name;
    const sc = String(bridgeStateCode);
    const bn = String(bridgeNumber);
    setSaving(true);
    setStatus('Saving point cloud to bridgeâ€¦');
    try {
      const fd = new FormData();
      fd.append('file', blob, fname);
      fd.append('horizontal_unit', sourceReview.horizontalUnit);
      fd.append('vertical_unit', sourceReview.verticalUnit);
      fd.append('frame_kind', sourceReview.frameKind);
      fd.append('crs', sourceReview.crs.trim());
      fd.append('up_axis', sourceReview.upAxis);
      fd.append('declaration_evidence', sourceReview.evidence.trim());
      fd.append('declaration_reviewed', String(sourceReview.reviewed));
      fd.append('selected_scan_indices', sourceReview.selectedScans.trim());
      if (sourceReview.registrationReference.trim()) fd.append('registration_reference_scan', sourceReview.registrationReference.trim());
      if (sourceReview.registrationRms.trim()) fd.append('registration_rms_metres', sourceReview.registrationRms.trim());
      if (sourceReview.registrationMaximumResidual.trim()) fd.append('registration_maximum_residual_metres', sourceReview.registrationMaximumResidual.trim());
      fd.append('registration_method', sourceReview.registrationMethod.trim());
      fd.append('registration_reviewed', String(sourceReview.registrationReviewed));
      const res = await fetch(`/api/bridges/${sc}/${bn}/point-cloud`, { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const detail = (err as any).detail;
        throw new Error(
          typeof detail === 'string'
            ? detail
            : detail?.message || detail?.code || `Upload failed (${res.status})`,
        );
      }
      const d = await res.json();
      setCloudInfo(previous => ({ ...(previous ?? {}), exists: true, raw_exists: true, filename: d.filename, size_bytes: d.size_bytes, source_profile: d.source_profile }));
      setStatus(d.source_profile?.metric_processing_authorized
        ? `Saved and reviewed "${d.filename}" for metric processing.`
        : `Saved "${d.filename}" for visualization; units/gravity or the reviewed server parser remain unverified.`);
    } catch (err) {
      setStatus(`Save error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }, [hasBridge, bridgeStateCode, bridgeNumber, sourceReview]);

  const deleteFromBridge = useCallback(async () => {
    if (!hasBridge || !cloudInfo?.exists || deleting) return;
    const reason = window.prompt('Why should this saved, unprocessed point-cloud source be deleted?')?.trim();
    if (!reason) {
      setStatus('Deletion cancelled. An audit reason is required.');
      return;
    }
    const confirmed = window.confirm(
      `Delete the saved point cloud for bridge ${bridgeNumber}? Sources referenced by durable run evidence cannot be deleted here.`,
    );
    if (!confirmed) return;
    setDeleting(true);
    setStatus('Requesting audited point-cloud deletion…');
    try {
      const url = `/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud?reason=${encodeURIComponent(reason)}`;
      const response = await fetch(url, { method: 'DELETE' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload?.detail;
        throw new Error(typeof detail === 'string' ? detail : detail?.message || detail?.code || `Delete failed (${response.status})`);
      }
      setCloudInfo(previous => ({ ...(previous ?? {}), exists: !!previous?.accepted_result_exists, raw_exists: false }));
      setStatus(payload?.status === 'deleted'
        ? 'The unprocessed saved point cloud was deleted with an audit receipt.'
        : 'Deletion was accepted and will be retried by the durable cleanup worker.');
    } catch (error) {
      setStatus(`Delete error: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setDeleting(false);
    }
  }, [hasBridge, cloudInfo?.exists, deleting, bridgeStateCode, bridgeNumber]);

  // loadFromBridge is defined after processFile (below) because it depends on it.
  const loadFromBridgeRef = useRef<((mode?: 'auto' | 'raw' | 'processed') => Promise<void>) | null>(null);
  const loadFromBridge = useCallback(() => {
    return loadFromBridgeRef.current?.('auto') ?? Promise.resolve();
  }, []);
  const loadRawFromBridge = useCallback(() => {
    return loadFromBridgeRef.current?.('raw') ?? Promise.resolve();
  }, []);
  const loadProcessedFromBridge = useCallback(() => {
    return loadFromBridgeRef.current?.('processed') ?? Promise.resolve();
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

  // Several transverse sections are evaluated, but only sections contributing
  // to the minimum envelope are persisted. Draw one selected retained plane at
  // a time so the geometry and its distances remain readable.
  const drawClearanceLine = useCallback((info: any, s: PointStats | null, selectedPlaneId = '') => {
    if (!sceneRef.current) return;
    if (clearLineRef.current) { sceneRef.current.remove(clearLineRef.current); clearLineRef.current = null; }
    if (!info || !s) return;
    const group = new THREE.Group();
    const featureMeasurement = info?.feature_measurement?.primary_measurement;
    const selectedBh12 = Array.isArray(featureMeasurement?.bh12_candidates)
      ? featureMeasurement.bh12_candidates.find((candidate: any) => candidate.candidate_id === featureMeasurement.selected_bh12_candidate_id)
      : null;
    const bh12ControlId = selectedBh12?.controlling_sample_id;
    const bh13ControlId = featureMeasurement?.bh13_controlling_sample_id;
    const V = (x: number, y: number, z: number) => new THREE.Vector3(x - s.originX, y - s.originY, z - s.originZ);
    const dotR = Math.max(0.15, s.spanZ * 0.02);
    const dot = (v: THREE.Vector3, color: number) => {
      const m = new THREE.Mesh(new THREE.SphereGeometry(dotR, 12, 12), new THREE.MeshBasicMaterial({ color }));
      m.position.copy(v); return m;
    };
    const vline = (p: number[], color: number, w = 2, markers = true) => {
      // p = [x, y, road_z, soffit_z]
      const a = V(p[0], p[1], p[2]), b = V(p[0], p[1], p[3]);
      group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]),
        new THREE.LineBasicMaterial({ color, linewidth: w })));
      if (markers) { group.add(dot(a, 0x22c55e)); group.add(dot(b, 0xf97316)); }
    };
    const plane = (corners: number[][], color: number, opacity: number) => {
      if (!corners || corners.length < 3) return;
      const geo = new THREE.BufferGeometry();
      const planePoints = corners.map((c) => V(c[0], c[1], c[2]));
      const verts = new Float32Array(planePoints.flatMap((v) => [v.x, v.y, v.z]));
      geo.setAttribute('position', new THREE.BufferAttribute(verts, 3));
      const idx: number[] = [];                            // fan over the hull polygon
      for (let i = 1; i < corners.length - 1; i++) idx.push(0, i, i + 1);
      geo.setIndex(idx);
      group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, transparent: true, opacity, side: THREE.DoubleSide, depthWrite: false })));
      const outline = [...planePoints, planePoints[0]];
      group.add(new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(outline),
        new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9 }),
      ));
    };
    const clearanceRectangles = (samples: any[], defaultColor: number) => {
      if (!samples.length) return;
      const points = samples.map(sample => new THREE.Vector2(Number(sample.x), Number(sample.y)));
      const fallbackDirection = new THREE.Vector2(1, 0);
      for (let index = 0; index < samples.length; index++) {
        const current = points[index];
        const previous = points[index - 1];
        const next = points[index + 1];
        const left = previous
          ? previous.clone().add(current).multiplyScalar(0.5)
          : current.clone().sub((next ? next.clone().sub(current) : fallbackDirection).multiplyScalar(0.5));
        const right = next
          ? current.clone().add(next).multiplyScalar(0.5)
          : current.clone().add((previous ? current.clone().sub(previous) : fallbackDirection).multiplyScalar(0.5));
        const sample = samples[index];
        const sampleId = sample.measurement_sample_id;
        const controlsBh13 = sampleId === bh13ControlId;
        const controlsBh12 = sampleId === bh12ControlId;
        const color = controlsBh13 ? 0xdc2626 : controlsBh12 ? 0x2563eb : sample.used_by_result === false ? 0x94a3b8 : defaultColor;
        plane([
          [left.x, left.y, Number(sample.road_z)],
          [right.x, right.y, Number(sample.road_z)],
          [right.x, right.y, Number(sample.soffit_z)],
          [left.x, left.y, Number(sample.soffit_z)],
        ], color, controlsBh13 || controlsBh12 || sample.controlling ? 0.26 : 0.15);
      }
    };

    const roadways = info.roadways as any[] | undefined;
    if (roadways && roadways.length) {
      const pal = [0x6366f1, 0x2563eb, 0x16a34a, 0xa855f7];
      roadways.slice(0, 1).forEach((rw, i) => {
        const col = pal[i % pal.length];
        const retainedPlanes = Array.isArray(rw.measurement_planes) ? rw.measurement_planes : [];
        const selectedPlane = retainedPlanes.find((candidate: any) => candidate.plane_id === selectedPlaneId)
          ?? retainedPlanes.find((candidate: any) => candidate.controls_global_minimum)
          ?? retainedPlanes[0]
          ?? null;
        const samples = Array.isArray(selectedPlane?.samples)
          ? selectedPlane.samples
          : (Array.isArray(rw.profile_samples) ? rw.profile_samples : []);
        clearanceRectangles(samples, col);
        samples.forEach((sample: any) => {
          const p = [sample.x, sample.y, sample.road_z, sample.soffit_z];
          const sampleId = sample.measurement_sample_id;
          const controlsBh13 = sampleId === bh13ControlId;
          const controlsBh12 = sampleId === bh12ControlId;
          const isMin = controlsBh13 || controlsBh12 || !!sample.controlling;
          const contributes = sample.used_by_result !== false;
          vline(p, controlsBh13 ? 0xdc2626 : controlsBh12 ? 0x2563eb : isMin ? 0xef4444 : contributes ? col : 0x94a3b8, isMin ? 3 : 1, false);
        });
        if (!samples.length && rw.controlling_point) vline(rw.controlling_point, 0xef4444, 3);  // legacy result
      });
    } else if (info.clearance_line) {
      // Single line (unvalidated water-surface gap proxy, or fallback).
      const l = info.clearance_line;
      vline([l.ground[0], l.ground[1], l.ground[2], l.soffit[2]], info.waterway?.is_waterway ? 0x3b82f6 : 0xfacc15, 2);
    }
    sceneRef.current.add(group);
    clearLineRef.current = group;
    renderPendingRef.current = 6;
  }, []);

  useEffect(() => {
    const candidate = geometryClearanceCandidate(clearanceInfo);
    const roadway = Array.isArray(candidate?.roadways) ? candidate.roadways[0] : null;
    const retainedPlanes = Array.isArray(roadway?.measurement_planes) ? roadway.measurement_planes : [];
    setSelectedClearancePlaneId(current => retainedPlanes.some((plane: any) => plane.plane_id === current)
      ? current
      : (retainedPlanes.find((plane: any) => plane.controls_global_minimum)?.plane_id ?? retainedPlanes[0]?.plane_id ?? ''));
  }, [clearanceInfo]);

  useEffect(() => {
    drawClearanceLine(geometryClearanceCandidate(clearanceInfo), stats, selectedClearancePlaneId);
  }, [clearanceInfo, stats, selectedClearancePlaneId, drawClearanceLine]);

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

  // Apply the one-pass result to the cloud. Label 0 means outside the analysed
  // bridge footprint or not confidently assigned; keep those source points
  // visible in grey so semantic review never appears to delete scan evidence.
  const applyAnalysisResult = useCallback((d: any) => {
    if (!pointsRef.current) return;
    // Start every result in the least-obstructed QA view. Fitted geometry is an
    // optional overlay and should never cover the source cloud by default.
    setShowVolumes(false);
    setShowMesh(false);
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
    const counts = new Map<number, number>();
    for (let i = 0; i < cnt; i++) {
      const lid = labels[i] || 0;
      counts.set(lid, (counts.get(lid) || 0) + 1);
      pos[i * 3] = op[i * 3]; pos[i * 3 + 1] = op[i * 3 + 1]; pos[i * 3 + 2] = op[i * 3 + 2];
      const rgb = lid === 0 ? [0.58, 0.62, 0.70] : (lut.get(lid) || [0.6, 0.6, 0.6]);
      colors[i * 3] = rgb[0]; colors[i * 3 + 1] = rgb[1]; colors[i * 3 + 2] = rgb[2];
    }
    posAttr.needsUpdate = true; colAttr.needsUpdate = true;
    pointsRef.current.frustumCulled = false;
    setSegLabels(labels);
    const returnedClasses: SegClass[] = (d.classes || []).map((c: any) => ({
      id: c.id,
      key: c.key,
      name: c.name,
      color_rgb: c.color_rgb,
      color_hex: c.color_hex,
      count: counts.get(c.id) || 0,
      material: c.material,
      is_clearance: c.is_clearance,
    }));
    if ((counts.get(0) || 0) > 0 && !returnedClasses.some(c => c.id === 0)) {
      returnedClasses.push({
        id: 0,
        key: 'retained_unclassified',
        name: 'Outside analysis / retained',
        color_rgb: [0.58, 0.62, 0.70],
        color_hex: '#949eB3',
        count: counts.get(0) || 0,
        is_clearance: false,
      });
    }
    setSegClasses(returnedClasses);
    setSegVisible(Array(returnedClasses.length).fill(true));
    setSegDone(true);
    renderPendingRef.current = 8;
  }, []);

  const activateCloudDisplay = useCallback((mode: 'raw' | 'processed' | 'together') => {
    const raw = rawCloudDataRef.current;
    const bundle = processedCloudBundleRef.current;
    if (mode === 'raw') {
      if (!raw) return;
      origColorsRef.current = null;
      origPosRef.current = null;
      loadIntoScene(copyParseResult(raw));
      setAnalysisInfo(null);
      setClearanceInfo(null);
      setDurableRun(null);
      setDurableEvidence(null);
      setMeasurement({ groundZ: null, soffitZ: null });
      setSegLabels(null); setSegDone(false); setSegClasses(DEFAULT_SEG_CLASSES);
      setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
      setCloudDisplayMode('raw');
      setStatus('Raw point cloud loaded.');
      return;
    }
    if (!bundle || (mode === 'together' && !raw)) return;
    origColorsRef.current = null;
    origPosRef.current = null;
    const visible = mode === 'together'
      ? combineCloudResults(raw!, bundle.preview)
      : copyParseResult(bundle.preview);
    loadIntoScene(visible);
    setAnalysisInfo(bundle.analysis);
    setClearanceInfo(bundle.analysis.clearance ?? null);
    setDurableRun(bundle.run);
    setDurableEvidence(bundle.evidence);
    const geometryClearance = geometryClearanceCandidate(bundle.analysis.clearance);
    if (geometryClearance) setMeasurement({
      groundZ: geometryClearance.ground_z ?? null,
      soffitZ: geometryClearance.soffit_z ?? null,
    });
    if (mode === 'processed') {
      applyAnalysisResult(bundle.analysis);
      setStatus(`Processed point cloud loaded with ${bundle.analysis.component_count ?? 0} component groups and all saved results.`);
    } else {
      setSegLabels(null); setSegDone(false); setSegClasses(DEFAULT_SEG_CLASSES);
      setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
      setStatus('Raw and processed point clouds shown together. Raw points are grey.');
    }
    setCloudDisplayMode(mode);
  }, [applyAnalysisResult, loadIntoScene]);

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
    rawCloudDataRef.current = null;
    processedCloudBundleRef.current = null;
    loadedBridgeSourceShaRef.current = null;
    setAvailableCloudLayers({ raw: false, processed: false });
    setCloudDisplayMode('raw');
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
      // Only E57 has a reviewed server source parser in this release. Other
      // formats can be viewed locally, but their units must not be guessed.
      cloudScaleKnownRef.current = false;
      setSourceReview(ext === 'e57'
        ? { ...EMPTY_SOURCE_REVIEW, horizontalUnit: 'metre', verticalUnit: 'metre', frameKind: 'scanner_local' }
        : EMPTY_SOURCE_REVIEW);
      // E57: parse on the backend (pye57) â€” reads all scans reliably, up to ~2M points.
      if (ext === 'e57') {
        setStatus('Parsing E57 on the serverâ€¦');
        const fd = new FormData();
        fd.append('file', file);
        fd.append('max_points', String(E57_MAX_POINTS));
        const res = await fetch('/api/point-cloud/preview?binary=1', { method: 'POST', body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `E57 parse failed (${res.status})`);
        const { result: pr, meta } = await readCloudResponse(res);
        rawCloudDataRef.current = copyParseResult(pr);
        setAvailableCloudLayers({ raw: true, processed: false });
        loadIntoScene(copyParseResult(pr));
        const scanCount = Number(meta.source_profile?.total_scan_count ?? meta.scan_count ?? 1);
        setSourceReview(v => ({
          ...v,
          scanCount,
          selectedScans: scanCount === 1 ? '0' : '',
          registrationReviewed: false,
        }));
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
        rawCloudDataRef.current = copyParseResult(pr);
        setAvailableCloudLayers({ raw: true, processed: false });
        loadIntoScene(copyParseResult(pr));
        setStatus(`Loaded ${(meta.rendered_point_count ?? 0).toLocaleString()} of ${(meta.original_point_count ?? 0).toLocaleString()} NPZ points${meta.downsampled ? ' (downsampled)' : ''} from "${file.name}"`);
        return;
      }
      const buf = await file.arrayBuffer();
      let result: ParseResult;
      if      (ext === 'ply') result = parsePLY(buf);
      else if (ext === 'las') result = parseLAS(buf);
      else if (ext === 'laz') throw new Error('.LAZ (compressed LAS) is not supported. Convert to .LAS or .PLY first using CloudCompare or PDAL.');
      else                    result = parseXYZ(buf);
      rawCloudDataRef.current = copyParseResult(result);
      setAvailableCloudLayers({ raw: true, processed: false });
      loadIntoScene(copyParseResult(result));
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
      setSourceReview(EMPTY_SOURCE_REVIEW);
      setSegLabels(null); setSegDone(false); setSegClasses(DEFAULT_SEG_CLASSES);
      setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
      setClearanceInfo(null); setMeasurement({ groundZ: null, soffitZ: null });
      setFileName(isVideo ? arr[0].name : `${arr.length} photos`);
      cloudScaleKnownRef.current = false;
      const reconstructed = previewToResult(d);
      rawCloudDataRef.current = copyParseResult(reconstructed);
      processedCloudBundleRef.current = null;
      setAvailableCloudLayers({ raw: true, processed: false });
      setCloudDisplayMode('raw');
      loadIntoScene(copyParseResult(reconstructed));
      setStatus(`Reconstructed ${(d.rendered_point_count ?? 0).toLocaleString()} points Â· ${d.engine ?? ''}. ${d.scale_note ?? ''}`);
    } catch (err) {
      setStatus(`Reconstruction error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setReconstructing(false); setLoading(false);
    }
  }, [loadIntoScene]);

  // Assign the real loadFromBridge implementation now that processFile is declared
  useEffect(() => {
    loadFromBridgeRef.current = async (mode = 'auto') => {
      if (!hasBridge) return;
      const sc = String(bridgeStateCode);
      const bn = String(bridgeNumber);
      setLoading(true);
      setStatus(mode === 'processed' ? 'Loading processed point cloud and analysis…' : mode === 'raw' ? 'Loading raw point cloud…' : 'Loading saved point cloud…');
      try {
        if (mode !== 'raw') {
          const savedResultResponse = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/saved-result`);
          if (savedResultResponse.ok) {
            const savedPreviewResponse = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/saved-result/preview?binary=1`);
            if (!savedPreviewResponse.ok) throw await responseError(savedPreviewResponse, 'Saved processed preview failed');
            const analysis = await savedResultResponse.json();
            const { result: preview, meta } = await readCloudResponse(savedPreviewResponse);
            processedCloudBundleRef.current = {
              preview: copyParseResult(preview),
              analysis,
              run: null,
              evidence: null,
              renderedPointCount: Number(meta.rendered_point_count ?? preview.stats.count),
            };
            cloudScaleKnownRef.current = analysis.scale_known !== false;
            try {
              const rawResponse = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/preview?max_points=700000&binary=1`);
              if (rawResponse.ok) {
                const { result: rawPreview } = await readCloudResponse(rawResponse);
                rawCloudDataRef.current = copyParseResult(rawPreview);
                loadedBridgeSourceShaRef.current = cloudInfo?.source_sha256 ?? null;
              }
            } catch { /* The saved processed result remains independently loadable. */ }
            setAvailableCloudLayers({ raw: !!rawCloudDataRef.current, processed: true });
            activateCloudDisplay('processed');
            setCloudInfo(previous => ({
              ...(previous ?? { exists: true }),
              exists: true,
              legacy_processed_exists: true,
              processed_point_count: meta.rendered_point_count,
            }));
            setLoading(false);
            return;
          }
          if (savedResultResponse.status !== 404) throw await responseError(savedResultResponse, 'Saved processed result lookup failed');
          if (mode === 'processed') throw new Error('No saved processed result exists for this bridge and year.');
        }

        // Loading raw intentionally clears any currently displayed processed
        // labels, evidence, fitted geometry, and clearance result.
        setAnalysisInfo(null);
        setClearanceInfo(null);
        setDurableRun(null);
        setDurableEvidence(null);
        setMeasurement({ groundZ: null, soffitZ: null });
        setSegLabels(null);
        setSegDone(false);
        setSegClasses(DEFAULT_SEG_CLASSES);
        setSegVisible(Array(DEFAULT_SEG_CLASSES.length).fill(true));
        // E57 is parsed server-side (pye57) â€” no download/re-upload of the large file.
        const pv = await fetch(`/api/bridges/${sc}/${bn}/point-cloud/preview?max_points=${E57_MAX_POINTS}&binary=1`);
        if (pv.ok) {
          const { result: pr, meta } = await readCloudResponse(pv);
          originalFileRef.current = null;
          cloudScaleKnownRef.current = true;   // E57 scans carry real units
          rawCloudDataRef.current = copyParseResult(pr);
          loadedBridgeSourceShaRef.current = cloudInfo?.source_sha256 ?? null;
          setAvailableCloudLayers({ raw: true, processed: !!processedCloudBundleRef.current });
          activateCloudDisplay('raw');
          setStatus(`Loaded raw point cloud: ${(meta.rendered_point_count ?? 0).toLocaleString()} of ${(meta.original_point_count ?? 0).toLocaleString()} points${meta.downsampled ? ' (downsampled)' : ''}.`);
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
        loadedBridgeSourceShaRef.current = cloudInfo?.source_sha256 ?? null;
      } catch (err) {
        setStatus(`Load error: ${err instanceof Error ? err.message : String(err)}`);
        setLoading(false);
      }
    };
  }, [hasBridge, bridgeStateCode, bridgeNumber, dataYear, cloudInfo?.source_sha256, processFile, activateCloudDisplay]);

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
      const url = '/api/point-cloud/segment' + binQS({ use_qwen: useHostedVlm, use_local_sam: false });
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
  }, [stats, useHostedVlm, applySegColors, applyInstanceColors, gatherPointsBinary]);

  // â”€â”€ Backend clearance â€” RANSAC ground/soffit + DB cross-check â”€â”€
  const runBackendClearance = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    setClearanceBusy(true);
    setStatus('Computing clearance…');
    try {
      const url = '/api/point-cloud/clearance' + binQS({
          origin: `${stats.originX},${stats.originY},${stats.originZ}`,
          scale_known: cloudScaleKnownRef.current });
      const res = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: gatherPointsBinary().buf });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Clearance failed (${res.status})`);
      const d = await res.json();
      if (d.status !== 'success') { setStatus(`Clearance: ${d.status}`); setClearanceBusy(false); return; }
      setMeasurement({ groundZ: d.ground_z, soffitZ: d.soffit_z });
      setClearanceInfo(d);
      setStatus(d.scale_known === false
        ? 'Metric scale required before clearance can be reported.'
        : `Clearance ${Number(d.clearance_m).toFixed(3)} m · ${Number(d.clearance_ft).toFixed(2)} ft`);
    } catch (err) {
      setStatus(`Clearance error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setClearanceBusy(false);
    }
  }, [stats, gatherPointsBinary]);

  const loadDurableResult = useCallback(async (run: DurablePointCloudRun, signal: AbortSignal) => {
    const [resultResponse, previewResponse, evidenceResponse] = await Promise.all([
      fetch(`/api/point-cloud/runs/${run.run_id}/result`, { signal }),
      fetch(`/api/point-cloud/runs/${run.run_id}/preview?binary=1`, { signal }),
      fetch(`/api/point-cloud/runs/${run.run_id}/evidence`, { signal }),
    ]);
    if (!resultResponse.ok) throw await responseError(resultResponse, 'Candidate result failed');
    if (!previewResponse.ok) throw await responseError(previewResponse, 'Candidate preview failed');
    if (!evidenceResponse.ok) throw await responseError(evidenceResponse, 'Candidate evidence failed');
    const analysis = await resultResponse.json();
    const { result: preview, meta } = await readCloudResponse(previewResponse);
    const evidence = await evidenceResponse.json() as DurablePointCloudEvidence;
    loadIntoScene(preview);
    origPosRef.current = new Float32Array(preview.positions);
    origColorsRef.current = new Float32Array(preview.colors);
    setAnalysisInfo(analysis);
    applyAnalysisResult(analysis);
    setClearanceInfo(analysis.clearance ?? null);
    const geometryClearance = geometryClearanceCandidate(analysis.clearance);
    if (geometryClearance) {
      setMeasurement({
        groundZ: geometryClearance.ground_z ?? null,
        soffitZ: geometryClearance.soffit_z ?? null,
      });
    }
    setDurableEvidence(evidence);
    setCloudInfo(prev => ({
      ...(prev ?? { exists: true }),
      exists: true,
      accepted_result_exists: run.published,
      legacy_processed_exists: true,
      processed_point_count: meta.rendered_point_count,
    }));
    setStatus(`Analysis complete · ${analysis.component_count ?? 0} components · ${(evidence.measurements || []).length} measurements`);
  }, [applyAnalysisResult, loadIntoScene]);

  const pollDurableRun = useCallback(async (initial: DurablePointCloudRun, signal: AbortSignal) => {
    let run = initial;
    for (let attempt = 0; attempt < 1200; attempt++) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      if (attempt > 0) {
        const response = await fetch(`/api/point-cloud/runs/${run.run_id}`, { signal });
        if (!response.ok) throw await responseError(response, 'Run status failed');
        run = await response.json() as DurablePointCloudRun;
        setDurableRun(run);
      }
      const completed = Number(run.progress?.completed ?? 0);
      const total = Number(run.progress?.total ?? 8);
      setStatus(`Point-cloud run ${run.phase.replace(/_/g, ' ')} · ${completed}/${total}`);
      if (run.status === 'succeeded') {
        await loadDurableResult(run, signal);
        return run;
      }
      if (run.status === 'failed' || run.status === 'cancelled') {
        throw new Error(run.error_code || `Point-cloud run ${run.status}`);
      }
      await new Promise<void>((resolve, reject) => {
        const timer = window.setTimeout(resolve, 1500);
        signal.addEventListener('abort', () => {
          window.clearTimeout(timer);
          reject(new DOMException('Aborted', 'AbortError'));
        }, { once: true });
      });
    }
    throw new Error('Point-cloud run exceeded the bounded polling window. It remains durable and can be resumed.');
  }, [loadDurableResult]);

  // Metric-ready sources use the durable worker and save their automatic result.
  // Older saved clouds still receive an immediate session analysis.
  const runAnalyze = useCallback(async () => {
    if (!pointsRef.current || !stats) return;
    const useDurableWorkflow = Boolean(
      hasBridge
      && cloudInfo?.exists
      && cloudInfo.source_profile?.metric_processing_authorized === true,
    );
    setAnalyzing(true);
    setDurableEvidence(null);
    setStatus(useDurableWorkflow
      ? 'Enqueuing an exact-revision point-cloud run…'
      : 'Analyzing the loaded point cloud…');
    try {
      if (useDurableWorkflow) {
        durablePollAbortRef.current?.abort();
        const controller = new AbortController();
        durablePollAbortRef.current = controller;
        const idempotencyKey = crypto.randomUUID();
        const response = await fetch(
          `/api/bridges/${bridgeStateCode}/${bridgeNumber}/point-cloud/runs`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
            body: JSON.stringify({
              data_year: dataYear == null || dataYear === '' ? null : Number(dataYear),
              model_mode: 'clearance',
              use_vlm: useHostedVlm,
            }),
            signal: controller.signal,
          },
        );
        if (!response.ok) throw await responseError(response, 'Analysis enqueue failed');
        const run = await response.json() as DurablePointCloudRun;
        setDurableRun(run);
        await pollDurableRun(run, controller.signal);
        return;
      }
      const url = '/api/point-cloud/analyze' + binQS({
        origin: `${stats.originX},${stats.originY},${stats.originZ}`,
        scale_known: cloudScaleKnownRef.current,
        bridge_context_state_code: hasBridge ? bridgeStateCode : undefined,
        bridge_context_bridge_number: hasBridge ? bridgeNumber : undefined,
        save_result: hasBridge
          && cloudDisplayMode === 'raw'
          && !!cloudInfo?.source_sha256
          && loadedBridgeSourceShaRef.current === cloudInfo.source_sha256,
        source_sha256: hasBridge ? loadedBridgeSourceShaRef.current ?? undefined : undefined,
        data_year: hasBridge && dataYear !== undefined && dataYear !== '' ? Number(dataYear) : undefined,
        use_vlm: useHostedVlm,
        use_ai_cleaning: useAiCleaning,
        use_local_sam: false,
        model_mode: 'clearance',
      });
      const response = await fetch(url, { method: 'POST', headers: BIN_HDRS, body: gatherPointsBinary().buf });
      if (!response.ok) throw await responseError(response, 'Ad-hoc analysis failed');
      const analysis = await response.json();
      if (analysis.status !== 'success') throw new Error(`Analysis: ${analysis.status}`);
      setAnalysisInfo(analysis);
      applyAnalysisResult(analysis);
      setClearanceInfo(analysis.clearance);
      const geometryClearance = geometryClearanceCandidate(analysis.clearance);
      if (geometryClearance) setMeasurement({ groundZ: geometryClearance.ground_z ?? null, soffitZ: geometryClearance.soffit_z ?? null });
      if (analysis.persistence?.saved === true) {
        setCloudInfo(previous => ({
          ...(previous ?? { exists: true }),
          exists: true,
          legacy_processed_exists: true,
          processed_point_count: analysis.persistence.processed_point_count,
        }));
        await loadProcessedFromBridge();
      } else {
        setStatus(hasBridge
          ? `Analysis complete: ${analysis.component_count ?? analysis.components?.length ?? 0} components found.`
          : 'Point-cloud preview complete.');
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        setStatus('Point-cloud status polling stopped. The durable job state remains on the server.');
      } else {
        setStatus(`Analysis error: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally {
      setAnalyzing(false);
    }
  }, [stats, hasBridge, cloudInfo, cloudDisplayMode, bridgeStateCode, bridgeNumber, dataYear, useHostedVlm, useAiCleaning, gatherPointsBinary, applyAnalysisResult, pollDurableRun, loadProcessedFromBridge]);

  const cancelDurableRun = useCallback(async () => {
    if (!durableRun || !['queued', 'running', 'cancellation_requested'].includes(durableRun.status)) return;
    try {
      const response = await fetch(`/api/point-cloud/runs/${durableRun.run_id}/cancel`, { method: 'POST' });
      if (!response.ok) throw await responseError(response, 'Cancel failed');
      const cancelled = await response.json() as DurablePointCloudRun;
      setDurableRun(cancelled);
      durablePollAbortRef.current?.abort();
      setStatus('Cancellation was recorded durably. A blocking provider call may stop at its next application boundary.');
    } catch (error) {
      setStatus(`Cancel error: ${error instanceof Error ? error.message : String(error)}`);
    }
  }, [durableRun]);

  const toggleSegVisibility = useCallback((indices: number[]) => {
    if (!pointsRef.current || !segLabels) return;
    const validIndices = indices.filter(index => !!segClasses[index]);
    if (!validIndices.length) return;
    const geo = pointsRef.current.geometry as THREE.BufferGeometry;
    const cnt = geo.getAttribute('position').array.length / 3;
    const visible = validIndices.some(index => !(segVisible[index] ?? true));
    const newVis = segVisible.map((value, index) => validIndices.includes(index) ? visible : value);
    setSegVisible(newVis);
    const colAttr = geo.getAttribute('color') as THREE.BufferAttribute;
    const colors  = colAttr.array as Float32Array;
    for (const index of validIndices) {
      const cls = segClasses[index];
      const [r, g, b] = visible ? cls.color_rgb : [0.957, 0.965, 0.98];
      for (let k = 0; k < cnt; k++) {
        if (segLabels[k] === cls.id) {
          colors[k * 3] = r;
          colors[k * 3 + 1] = g;
          colors[k * 3 + 2] = b;
        }
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
  const geometryClearance = geometryClearanceCandidate(clearanceInfo);
  const primaryFeatureMeasurement = geometryClearance?.feature_measurement?.primary_measurement ?? null;
  const measuredBh12 = Number(primaryFeatureMeasurement?.bh12_raw_m);
  const measuredBh13 = Number(primaryFeatureMeasurement?.bh13_raw_m);
  const verticalGap = Number.isFinite(measuredBh13)
    ? measuredBh13
    : geometryClearance?.min_clearance_m ?? clearance;
  const verticalGapScaleKnown = geometryClearance
    ? geometryClearance.scale_known !== false
    : cloudScaleKnownRef.current;
  const clearanceRoadways: any[] = Array.isArray(geometryClearance?.roadways)
    ? geometryClearance.roadways
    : [];
  const primaryClearanceRoadway = clearanceRoadways[0] ?? null;
  const retainedClearancePlanes: any[] = Array.isArray(primaryClearanceRoadway?.measurement_planes)
    ? primaryClearanceRoadway.measurement_planes
    : [];
  const selectedClearancePlane = retainedClearancePlanes.find((plane: any) => plane.plane_id === selectedClearancePlaneId)
    ?? retainedClearancePlanes.find((plane: any) => plane.controls_global_minimum)
    ?? retainedClearancePlanes[0]
    ?? null;
  const clearanceRegions: any[] = Array.isArray(primaryFeatureMeasurement?.regions)
    ? primaryFeatureMeasurement.regions
    : [];
  const selectedBh12Candidate = Array.isArray(primaryFeatureMeasurement?.bh12_candidates)
    ? primaryFeatureMeasurement.bh12_candidates.find((candidate: any) => candidate.candidate_id === primaryFeatureMeasurement.selected_bh12_candidate_id)
    : null;
  const snbiBh13Rows: any[] = Array.isArray(analysisInfo?.snbi_bh13)
    ? analysisInfo.snbi_bh13
    : [];
  const comparisonBh13 = snbiBh13Rows.find((row: any) => row.comparison_target)
    ?? snbiBh13Rows[0]
    ?? null;
  const dualClearance = (metres: number, metreDigits = 3) =>
    `${metres.toFixed(metreDigits)} m · ${(metres * 3.28084).toFixed(2)} ft`;
  const recordedClearance = (feet: unknown) => {
    const value = Number(feet);
    if (!Number.isFinite(value)) return '—';
    if (value >= 99.9) return `≥30.480 m · ${value.toFixed(1)} ft`;
    return dualClearance(value * 0.3048);
  };
  const sourceUploadEligible = !!originalFileRef.current?.name.toLowerCase().endsWith('.e57');
  const sourceInputStyle: React.CSSProperties = {
    width: '100%', border: '1px solid #cbd5e1', borderRadius: 6, padding: '5px 6px',
    fontSize: 11, color: SX.text, background: '#fff',
  };

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
          {hasBridge && (cloudInfo?.raw_exists ?? (!!cloudInfo?.exists && !!cloudInfo?.filename)) && (
            <button
              onClick={loadRawFromBridge}
              disabled={loading}
              className="pcbtn pcbtn-outline"
              title={`Load the exact unprocessed source: ${cloudInfo.filename ?? 'saved point cloud'}`}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 15v4c0 1.1.9 2 2 2h14a2 2 0 0 0 2-2v-4M17 9l-5 5-5-5M12 12.8V2.5"/>
              </svg>
              Load Raw
            </button>
          )}
          {hasBridge && cloudInfo?.legacy_processed_exists && (
            <button
              onClick={loadProcessedFromBridge}
              disabled={loading}
              className="pcbtn pcbtn-outline"
              title="Load the processed cloud with its saved labels, measurements, and clearance planes"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 19h16M4 5h16M7 9h10v6H7z"/>
              </svg>
              Load Processed
            </button>
          )}
          {hasBridge && (cloudInfo?.raw_exists ?? (!!cloudInfo?.exists && !!cloudInfo?.filename)) && (
            <button
              onClick={deleteFromBridge}
              disabled={deleting}
              className="pcbtn pcbtn-outline"
              title="Delete an unprocessed saved source with an audit reason; durable evidence is protected"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6m3 0V4h8v2M10 11v5M14 11v5"/>
              </svg>
              {deleting ? 'Deleting…' : 'Delete Saved Cloud'}
            </button>
          )}
          {hasBridge && stats && !(cloudInfo?.raw_exists ?? (!!cloudInfo?.exists && !!cloudInfo?.filename)) && (
            <button
              onClick={saveToBridge}
              disabled={saving || !sourceUploadEligible}
              className="pcbtn pcbtn-success"
              title={sourceUploadEligible
                ? 'Attach the exact E57 bytes and source-coordinate review receipt'
                : 'This format is available for local visualization only; only original E57 source files can be attached.'}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                <polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>
              </svg>
              {saving ? 'Savingâ€¦' : sourceUploadEligible ? 'Save E57 Source' : 'View-only Format'}
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
              title="E57 can be attached as source evidence. Other listed formats are local visualization only."
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
              </div>
            </div>
          )}

          {measureStep !== 'idle' && (
            <div style={{ position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)', background: '#fbbf24', color: '#78350f', padding: '5px 16px', borderRadius: 999, fontWeight: 700, fontSize: 13, zIndex: 5, pointerEvents: 'none' }}>
              Click {measureStep === 'ground' ? 'ground surface' : 'bridge soffit (underside)'}
            </div>
          )}

          <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, background: 'rgba(255,255,255,0.85)', padding: '4px 14px', fontSize: 12, color: '#64748b', backdropFilter: 'blur(4px)', borderTop: '1px solid #e2e8f0' }}>
            {measureStep !== 'idle' && <span style={{ color: '#d97706', marginRight: 6 }}>â—</span>}
            {status}
          </div>
        </div>

        {/* Right Panel */}
        <div style={{ width: 286, display: 'flex', flexDirection: 'column', overflow: 'auto', background: '#fbfcfe', borderLeft: '1px solid ' + SX.border }}>

          <PanelSection title="Cloud Statistics">
            {stats ? (
              <div style={{ fontSize: 12, display: 'flex', flexDirection: 'column', gap: 3 }}>
                <StatRow label="Points" value={stats.count.toLocaleString()} />
                <StatRow label="File"   value={fileName} mono />
                {cloudScaleKnownRef.current && (
                  <>
                    <StatRow label="Extent" value={`${stats.spanX.toFixed(1)} × ${stats.spanY.toFixed(1)} × ${stats.spanZ.toFixed(1)} m`} />
                    <StatRow label="" value={`${(stats.spanX * 3.28084).toFixed(1)} × ${(stats.spanY * 3.28084).toFixed(1)} × ${(stats.spanZ * 3.28084).toFixed(1)} ft`} />
                  </>
                )}
                <details style={{ marginTop: 3, borderTop: '1px solid ' + SX.border, paddingTop: 4 }}>
                  <summary style={{ cursor: 'pointer', color: SX.label, fontSize: 10 }}>Coordinate bounds</summary>
                  <div style={{ marginTop: 4 }}>
                    <StatRow label="X (m)" value={`${stats.minX.toFixed(2)} … ${stats.maxX.toFixed(2)}`} />
                    <StatRow label="Y (m)" value={`${stats.minY.toFixed(2)} … ${stats.maxY.toFixed(2)}`} />
                    <StatRow label="Z (m)" value={`${stats.minZ.toFixed(2)} … ${stats.maxZ.toFixed(2)}`} />
                  </div>
                </details>
              </div>
            ) : <div style={{ fontSize: 12, color: SX.label }}>No data loaded</div>}
          </PanelSection>

          {(availableCloudLayers.raw || availableCloudLayers.processed) && (
            <PanelSection title="Cloud View">
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 4 }}>
                {(['raw', 'processed', 'together'] as const).map(mode => {
                  const enabled = mode === 'raw'
                    ? availableCloudLayers.raw
                    : mode === 'processed'
                      ? availableCloudLayers.processed
                      : availableCloudLayers.raw && availableCloudLayers.processed;
                  return (
                    <button key={mode} disabled={!enabled} onClick={() => activateCloudDisplay(mode)}
                      className={`pcbtn ${cloudDisplayMode === mode ? 'pcbtn-primary' : 'pcbtn-ghost'}`}
                      style={{ padding: '6px 4px', fontSize: 10.5, textTransform: 'capitalize' }}>
                      {mode}
                    </button>
                  );
                })}
              </div>
            </PanelSection>
          )}

          {sourceUploadEligible && (
          <PanelSection title="Source Coordinates">
            <details>
              <summary style={{ cursor: 'pointer', fontSize: 11, fontWeight: 600, color: sourceReview.reviewed ? '#047857' : SX.text }}>
                {sourceReview.reviewed ? 'Reviewed metric source' : 'Advanced source setup'}
              </summary>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 10.5 }}>
                <label>Horizontal units
                  <select value={sourceReview.horizontalUnit} style={sourceInputStyle}
                    onChange={e => setSourceReview(v => ({ ...v, horizontalUnit: e.target.value as PointCloudSourceReview['horizontalUnit'], reviewed: false }))}>
                    <option value="unknown">Unknown</option><option value="metre">Metres</option>
                    <option value="international_foot">International feet</option><option value="us_survey_foot">US survey feet</option>
                  </select>
                </label>
                <label>Vertical units
                  <select value={sourceReview.verticalUnit} style={sourceInputStyle}
                    onChange={e => setSourceReview(v => ({ ...v, verticalUnit: e.target.value as PointCloudSourceReview['verticalUnit'], reviewed: false }))}>
                    <option value="unknown">Unknown</option><option value="metre">Metres</option>
                    <option value="international_foot">International feet</option><option value="us_survey_foot">US survey feet</option>
                  </select>
                </label>
                <label>Coordinate frame
                  <select value={sourceReview.frameKind} style={sourceInputStyle}
                    onChange={e => setSourceReview(v => ({ ...v, frameKind: e.target.value as PointCloudSourceReview['frameKind'], reviewed: false }))}>
                    <option value="unknown">Unknown</option><option value="scanner_local">Scanner local</option>
                    <option value="engineering_local">Engineering local</option><option value="projected_crs">Projected CRS</option>
                    <option value="reconstruction_relative">Relative reconstruction</option>
                  </select>
                </label>
                {sourceReview.frameKind === 'projected_crs' && (
                  <label>CRS identifier
                    <input value={sourceReview.crs} placeholder="e.g. EPSG:3424" style={sourceInputStyle}
                      onChange={e => setSourceReview(v => ({ ...v, crs: e.target.value, reviewed: false }))} />
                  </label>
                )}
                <label>Verified up axis
                  <select value={sourceReview.upAxis} style={sourceInputStyle}
                    onChange={e => setSourceReview(v => ({ ...v, upAxis: e.target.value as PointCloudSourceReview['upAxis'], reviewed: false }))}>
                    {['+z', '-z', '+y', '-y', '+x', '-x'].map(axis => <option key={axis} value={axis}>{axis.toUpperCase()}</option>)}
                  </select>
                </label>
                <label>Evidence reference
                  <input value={sourceReview.evidence} placeholder="scanner export / control note" style={sourceInputStyle}
                    onChange={e => setSourceReview(v => ({ ...v, evidence: e.target.value, reviewed: false }))} />
                </label>
                {(sourceReview.scanCount ?? 0) > 1 && (
                  <div style={{ padding: 6, border: '1px solid #fde68a', borderRadius: 6, background: '#fffbeb', display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <div style={{ color: '#92400e', fontWeight: 700 }}>
                      {sourceReview.scanCount} scans detected — choose the reviewed merge set.
                    </div>
                    <label>Selected scan indices
                      <input value={sourceReview.selectedScans} placeholder="0,1,2" style={sourceInputStyle}
                        onChange={e => setSourceReview(v => ({ ...v, selectedScans: e.target.value, registrationReviewed: false }))} />
                    </label>
                    <label>Reference scan
                      <input type="number" min="0" value={sourceReview.registrationReference} style={sourceInputStyle}
                        onChange={e => setSourceReview(v => ({ ...v, registrationReference: e.target.value, registrationReviewed: false }))} />
                    </label>
                    <label>Registration RMS (metres)
                      <input type="number" min="0" step="0.001" value={sourceReview.registrationRms} style={sourceInputStyle}
                        onChange={e => setSourceReview(v => ({ ...v, registrationRms: e.target.value, registrationReviewed: false }))} />
                    </label>
                    <label>Maximum residual (metres)
                      <input type="number" min="0" step="0.001" value={sourceReview.registrationMaximumResidual} style={sourceInputStyle}
                        onChange={e => setSourceReview(v => ({ ...v, registrationMaximumResidual: e.target.value, registrationReviewed: false }))} />
                    </label>
                    <label>Registration method/evidence
                      <input value={sourceReview.registrationMethod} placeholder="survey targets / registered scan receipt" style={sourceInputStyle}
                        onChange={e => setSourceReview(v => ({ ...v, registrationMethod: e.target.value, registrationReviewed: false }))} />
                    </label>
                    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
                      <input type="checkbox" checked={sourceReview.registrationReviewed}
                        onChange={e => setSourceReview(v => ({ ...v, registrationReviewed: e.target.checked }))} />
                      I reviewed the selected merge set and registration residuals.
                    </label>
                  </div>
                )}
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: 6, color: SX.text }}>
                  <input type="checkbox" checked={sourceReview.reviewed}
                    onChange={e => setSourceReview(v => ({ ...v, reviewed: e.target.checked }))} />
                  I reviewed the units, frame, up direction, and cited evidence.
                </label>
                <div style={{ padding: '5px 6px', borderRadius: 6, background: sourceReview.reviewed ? '#ecfdf5' : '#fffbeb', color: sourceReview.reviewed ? '#047857' : '#92400e' }}>
                  {sourceReview.reviewed ? 'Review will be stored as a hashed source receipt.' : 'Visualization only until reviewed; no metric output is authorized.'}
                </div>
              </div>
            {cloudInfo?.source_profile && (
              <div style={{ marginTop: 7, paddingTop: 6, borderTop: '1px solid ' + SX.border, fontSize: 10.5, color: cloudInfo.source_profile.metric_processing_authorized ? '#047857' : '#92400e' }}>
                Saved source: {cloudInfo.source_profile.metric_processing_authorized ? 'metric-ready receipt' : 'visualization-only receipt'}
              </div>
            )}
            </details>
          </PanelSection>
          )}

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
                <button onClick={runAnalyze} disabled={analyzing}
                  className="pcbtn pcbtn-primary pcbtn-block">
                  {analyzing
                    ? <>{sxSpinner('#fff')}Analyzing...</>
                    : 'Analyze Point Cloud'}
                </button>
                {verticalGap != null && Number.isFinite(verticalGap) && verticalGapScaleKnown && (
                  <div style={{ padding: 8, borderRadius: 8, background: '#f8fffb', border: '1px solid #86efac', display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <div style={{ display: 'grid', gridTemplateColumns: Number.isFinite(measuredBh12) ? '1fr 1fr' : '1fr', gap: 6 }}>
                      {Number.isFinite(measuredBh12) && (
                        <MetricCell label="B.H.12" value={dualClearance(measuredBh12)} />
                      )}
                      <MetricCell label={Number.isFinite(measuredBh13) ? 'B.H.13' : 'Clearance'} value={dualClearance(verticalGap)} />
                    </div>
                    {primaryFeatureMeasurement?.recorded_comparison && (
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, padding: '5px 7px', borderRadius: 6, background: '#eff6ff', border: '1px solid #bfdbfe' }}>
                        <MetricCell label="SNBI B.H.12" value={recordedClearance(primaryFeatureMeasurement.recorded_comparison.bh12_recorded_ft)} />
                        <MetricCell label="SNBI B.H.13" value={recordedClearance(primaryFeatureMeasurement.recorded_comparison.bh13_recorded_ft)} />
                      </div>
                    )}
                    {!primaryFeatureMeasurement && comparisonBh13 && !comparisonBh13.sentinel_99_9 && (
                      <MetricCell label="SNBI B.H.13" value={recordedClearance(comparisonBh13.bh13_ft)} />
                    )}
                    {clearanceRoadways.slice(0, 1).map((roadway: any, roadwayIndex: number) => {
                      const samples = Array.isArray(selectedClearancePlane?.samples)
                        ? selectedClearancePlane.samples
                        : (Array.isArray(roadway.profile_samples) ? roadway.profile_samples : []);
                      const profileClearances = samples.map((sample: any) => Number(sample.clearance)).filter(Number.isFinite);
                      const profileMax = Math.max(1, ...profileClearances) * 1.04;
                      const chartLeft = 8;
                      const chartRight = 316;
                      const chartTop = 15;
                      const chartBottom = 108;
                      const chartY = (value: number) => chartBottom - (value / profileMax) * (chartBottom - chartTop);
                      const sampleOffsets = samples.map((sample: any) => Number(sample.offset)).filter(Number.isFinite);
                      const regionOffsets = clearanceRegions.flatMap((region: any) => [Number(region.start_offset_m), Number(region.end_offset_m)]).filter(Number.isFinite);
                      const offsetMin = Math.min(...(regionOffsets.length ? regionOffsets : sampleOffsets));
                      const offsetMax = Math.max(...(regionOffsets.length ? regionOffsets : sampleOffsets));
                      const offsetSpan = Math.max(1e-9, offsetMax - offsetMin);
                      const chartX = (offset: number) => chartLeft + ((offset - offsetMin) / offsetSpan) * (chartRight - chartLeft);
                      const bh12ControlId = selectedBh12Candidate?.controlling_sample_id;
                      const bh13ControlId = primaryFeatureMeasurement?.bh13_controlling_sample_id;
                      return (
                        <div key={`roadway-${roadwayIndex}`} style={{ borderTop: '1px solid #bbf7d0', paddingTop: 6 }}>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 8px' }}>
                            <MetricCell label="Minimum" value={dualClearance(Number(roadway.controlling_min_m))} />
                            <MetricCell label="Centre" value={dualClearance(Number(roadway.center_m))} />
                            <MetricCell label="Median" value={dualClearance(Number(roadway.median_m))} />
                            <MetricCell label="Maximum" value={dualClearance(Number(roadway.max_m))} />
                          </div>
                          {retainedClearancePlanes.length > 1 && (
                            <select aria-label="Visible clearance plane" value={selectedClearancePlane?.plane_id ?? ''}
                              onChange={event => setSelectedClearancePlaneId(event.target.value)}
                              style={{ ...sourceInputStyle, marginTop: 6, padding: '4px 5px' }}>
                              {retainedClearancePlanes.map((plane: any, index: number) => {
                                const roles = Array.isArray(plane.roles) ? plane.roles : [];
                                const label = roles.length
                                  ? roles.map((role: string) => role.toUpperCase()).join(' · ')
                                  : `${String(plane.station_edge ?? 'section').replace(/^./, (value: string) => value.toUpperCase())} ${index + 1}`;
                                return <option key={plane.plane_id} value={plane.plane_id}>{label}</option>;
                              })}
                            </select>
                          )}
                          {samples.length > 0 && (
                            <div style={{ marginTop: 6, padding: 4, border: '1px solid #dbeafe', borderRadius: 6, background: '#fff' }}>
                              <svg viewBox="0 0 324 132" role="img" aria-label="Clearance rectangles" style={{ display: 'block', width: '100%', height: 'auto' }}>
                                {selectedBh12Candidate && (
                                  <rect x={chartX(Number(selectedBh12Candidate.start_offset_m))} y={4}
                                    width={Math.max(1, chartX(Number(selectedBh12Candidate.end_offset_m)) - chartX(Number(selectedBh12Candidate.start_offset_m)))}
                                    height={chartBottom - 4} fill="#dbeafe" opacity="0.45" stroke="#2563eb" strokeWidth="1.5" />
                                )}
                                {samples.map((sample: any, sampleIndex: number) => {
                                  const offset = Number(sample.offset);
                                  const previousOffset = sampleIndex > 0 ? Number(samples[sampleIndex - 1].offset) : offset - (Number(samples[sampleIndex + 1]?.offset) - offset || offsetSpan / Math.max(1, samples.length));
                                  const nextOffset = sampleIndex < samples.length - 1 ? Number(samples[sampleIndex + 1].offset) : offset + (offset - Number(samples[sampleIndex - 1]?.offset) || offsetSpan / Math.max(1, samples.length));
                                  const x = chartX(Math.max(offsetMin, (previousOffset + offset) / 2));
                                  const right = chartX(Math.min(offsetMax, (offset + nextOffset) / 2));
                                  const barWidth = Math.max(1, right - x);
                                  const y = chartY(Number(sample.clearance));
                                  const sampleId = sample.measurement_sample_id;
                                  const bh13Control = sampleId === bh13ControlId;
                                  const bh12Control = sampleId === bh12ControlId;
                                  const controlling = bh13Control || bh12Control || !!sample.controlling;
                                  const contributes = sample.used_by_result !== false;
                                  const stroke = bh13Control ? '#dc2626' : bh12Control ? '#2563eb' : contributes ? '#4f46e5' : '#94a3b8';
                                  return (
                                    <g key={sample.sample_id ?? sampleIndex}>
                                      <rect x={x} y={y} width={barWidth} height={chartBottom - y} fill={controlling ? '#fee2e2' : contributes ? '#eef2ff' : '#f1f5f9'} stroke={stroke} strokeWidth={controlling ? 2 : 1.2} />
                                      <text x={x + barWidth / 2} y={Math.max(10, y - 3)} textAnchor="middle" fontSize="8" fontWeight={controlling ? 800 : 600} fill={controlling ? '#b91c1c' : '#334155'}>{Number(sample.clearance).toFixed(2)}</text>
                                    </g>
                                  );
                                })}
                                <line x1={chartLeft} y1={chartBottom} x2={chartRight} y2={chartBottom} stroke="#475569" strokeWidth="1.5" />
                                {clearanceRegions.map((region: any, index: number) => {
                                  const left = chartX(Number(region.start_offset_m));
                                  const right = chartX(Number(region.end_offset_m));
                                  return (
                                    <g key={region.region_id ?? index}>
                                      <rect x={left} y={112} width={Math.max(1, right - left)} height={15} fill={index % 2 ? '#e0e7ff' : '#eef2ff'} stroke="#a5b4fc" />
                                      <text x={(left + right) / 2} y={122.5} textAnchor="middle" fontSize="7.5" fill="#3730a3">{String(region.region_id ?? `Lane ${index + 1}`).replace(/-/g, ' ')}</text>
                                    </g>
                                  );
                                })}
                              </svg>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
                {durableRun && ['queued', 'running', 'cancellation_requested'].includes(durableRun.status) && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                    <div style={{ flex: 1, height: 5, borderRadius: 5, background: '#e2e8f0', overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${Math.min(100, 100 * Number(durableRun.progress?.completed ?? 0) / Math.max(1, Number(durableRun.progress?.total ?? 8)))}%`, background: SX.indigo }} />
                    </div>
                    <button onClick={cancelDurableRun} className="pcbtn pcbtn-ghost">Cancel</button>
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
            {analysisInfo && (
              <div style={{ display: 'flex', gap: 12, marginBottom: 6 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10.5, color: SX.text, cursor: 'pointer' }}>
                  <input type="checkbox" checked={showVolumes} onChange={e => setShowVolumes(e.target.checked)} style={{ accentColor: SX.indigo }} />
                  Volumes
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10.5, color: SX.text, cursor: 'pointer' }}>
                  <input type="checkbox" checked={showMesh} onChange={e => setShowMesh(e.target.checked)} style={{ accentColor: SX.indigo }} />
                  Mesh
                </label>
              </div>
            )}
            {!segDone
              ? <span style={{ color: SX.label }}>—</span>
              : <SegmentLegend classes={segClasses} segVisible={segVisible} onToggle={toggleSegVisibility} onReset={restoreOriginalCloud}/>}
          </PanelSection>
        </div>
      </div>
    </div>
  );
};

const PanelSection: React.FC<{ title: string; children: React.ReactNode; last?: boolean }> = ({ title, children, last }) => (
  <section style={{ padding: '8px 10px', borderBottom: last ? 'none' : '1px solid ' + SX.border }}>
    <div style={{ fontWeight: 700, fontSize: 9.5, textTransform: 'uppercase', letterSpacing: 0.7, marginBottom: 5, color: SX.title }}>{title}</div>
    {children}
  </section>
);

interface SegmentLegendProps {
  classes: SegClass[];
  segVisible: boolean[];
  onToggle: (indices: number[]) => void;
  onReset: () => void;
}
const SegmentLegend: React.FC<SegmentLegendProps> = ({ classes, segVisible, onToggle, onReset }) => {
  const grouped = new Map<string, { first: SegClass; indices: number[]; count: number; isClearance: boolean }>();
  classes.forEach((component, index) => {
    if (!component.count) return;
    const name = compactComponentLabel(component.name);
    const key = name.toLowerCase();
    const current = grouped.get(key);
    if (current) {
      current.indices.push(index);
      current.count += component.count;
      current.isClearance ||= !!component.is_clearance;
    } else {
      grouped.set(key, { first: { ...component, name }, indices: [index], count: component.count, isClearance: !!component.is_clearance });
    }
  });
  return (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
    {[...grouped.values()].map(group => {
      const c = group.first;
      const vis = group.indices.every(index => segVisible[index] ?? true);
      return (
        <button key={c.name} onClick={() => onToggle(group.indices)}
          style={{ display: 'flex', alignItems: 'flex-start', gap: 7, padding: '5px 7px', borderRadius: 7, border: `1px solid ${vis ? c.color_hex + '66' : SX.border}`, background: vis ? `${c.color_hex}14` : '#f8fafc', cursor: 'pointer', textAlign: 'left', width: '100%', opacity: vis ? 1 : 0.55 }}>
          <span style={{ width: 9, height: 9, borderRadius: '50%', background: c.color_hex, flexShrink: 0, display: 'inline-block', marginTop: 3 }}/>
          <span style={{ flex: 1, minWidth: 0, textAlign: 'left', fontSize: 11, color: vis ? SX.text : SX.label, fontWeight: 500, lineHeight: 1.25 }}>{c.name}</span>
          {group.isClearance && <span style={{ fontSize: 8.5, color: '#059669', fontWeight: 700, textTransform: 'uppercase', marginTop: 2 }}>clr</span>}
          <span style={{ fontSize: 10, color: SX.label, fontFamily: 'ui-monospace, monospace', marginTop: 1 }}>{group.count.toLocaleString()}</span>
        </button>
      );
    })}
    <button onClick={onReset} className="pcbtn pcbtn-ghost pcbtn-block" style={{ marginTop: 2 }}>↩ Restore Colours</button>
  </div>
  );
};

const MetricCell: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div style={{ minWidth: 0, padding: '4px 5px', borderRadius: 5, background: '#fff', border: '1px solid #dcfce7' }}>
    <div style={{ fontSize: 9, color: SX.label }}>{label}</div>
    <div style={{ fontSize: 10.5, fontWeight: 700, color: SX.text, lineHeight: 1.25 }}>{value}</div>
  </div>
);

const StatRow: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 12, gap: 8 }}>
    <span style={{ flexShrink: 0, color: SX.label }}>{label}</span>
    <span style={{ fontFamily: mono ? 'ui-monospace, monospace' : undefined, fontWeight: 500, color: SX.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 130, textAlign: 'right' }}>{value}</span>
  </div>
);

export default PointCloud;
