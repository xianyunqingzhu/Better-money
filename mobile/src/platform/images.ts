/** 图片：拍照/相册/文件读取 + 压缩 + 保存到应用图片目录（原件仅存本机）。 */
import { Camera, CameraResultType, CameraSource } from "@capacitor/camera";
import { Directory, Filesystem } from "@capacitor/filesystem";
import { FilePicker } from "@capawesome/capacitor-file-picker";
import { uuidHex } from "../db/migrations";

export const MAX_IMAGES = 10;
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const ALLOWED_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".webp", ".gif"]);

export interface PickedImage {
  path: string; // 应用内相对路径（data/images/...）
  name: string;
  bytes: Uint8Array;
}

/** 压缩图片：最长边 1600px，JPEG q0.85；PNG/GIF 保留原格式但限制尺寸。 */
async function compressImage(bytes: Uint8Array, ext: string): Promise<Uint8Array> {
  const blob = new Blob([bytes as unknown as BlobPart]);
  const bitmap = await createImageBitmap(blob).catch(() => null);
  if (!bitmap) return bytes;
  const maxSide = 1600;
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  if (scale >= 1 && bytes.length <= MAX_IMAGE_BYTES) {
    bitmap.close();
    return bytes;
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    bitmap.close();
    return bytes;
  }
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const type = ext === ".png" ? "image/png" : ext === ".webp" ? "image/webp" : "image/jpeg";
  const blobOut = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, type, 0.85),
  );
  if (!blobOut) return bytes;
  return new Uint8Array(await blobOut.arrayBuffer());
}

async function saveImage(bytes: Uint8Array, dateDir: string, ext: string): Promise<string> {
  const relative = `images/${dateDir}/${uuidHex()}${ext}`;
  if (await isNativeFs()) {
    await Filesystem.writeFile({
      path: relative,
      directory: Directory.Data,
      data: bytesToB64(bytes),
      recursive: true,
    });
  } else {
    localStorage.setItem("img:" + relative, bytesToB64(bytes));
  }
  return relative;
}

async function isNativeFs(): Promise<boolean> {
  try {
    const { Capacitor } = await import("@capacitor/core");
    return Capacitor.isNativePlatform();
  } catch {
    return false;
  }
}

export function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

export function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : ".jpg";
}

function normalizeExt(name: string): string {
  const ext = extOf(name);
  return ALLOWED_EXTENSIONS.has(ext) ? ext : ".jpg";
}

/** 拍照（单张）→ 保存并返回应用内路径。 */
export async function takePhoto(dateDir: string): Promise<PickedImage | null> {
  const photo = await Camera.getPhoto({
    quality: 90,
    allowEditing: false,
    resultType: CameraResultType.Base64,
    source: CameraSource.Camera,
  });
  if (!photo.base64String) return null;
  const original = b64ToBytes(photo.base64String);
  const ext = ".jpg";
  const bytes = await compressImage(original, ext);
  const path = await saveImage(bytes, dateDir, ext);
  return { path, name: `photo-${Date.now()}${ext}`, bytes };
}

/** 相册多选（最多 limit 张）。 */
export async function pickImages(dateDir: string, limit: number = MAX_IMAGES): Promise<PickedImage[]> {
  const photos = await Camera.pickImages({
    quality: 90,
    limit,
  });
  const result: PickedImage[] = [];
  for (const photo of photos.photos) {
    const webPath = photo.webPath || photo.path;
    if (!webPath) continue;
    const response = await fetch(webPath);
    const original = new Uint8Array(await response.arrayBuffer());
    const ext = normalizeExt(photo.format ? "." + photo.format : extOf(webPath));
    const bytes = await compressImage(original, ext);
    const path = await saveImage(bytes, dateDir, ext);
    result.push({ path, name: `gallery-${Date.now()}${ext}`, bytes });
  }
  return result;
}

/** 从文件系统选择共享包 ZIP（导入用）。 */
export async function pickZipFile(): Promise<{ name: string; bytes: Uint8Array } | null> {
  const result = await FilePicker.pickFiles({
    types: ["application/zip"],
    readData: true,
  });
  const file = result.files[0];
  if (!file || !file.data) return null;
  const bytes =
    typeof file.data === "string" ? b64ToBytes(file.data) : file.data;
  return { name: file.name, bytes };
}

/** 读取应用内保存的图片（供 AI 识别与详情查看）。 */
export async function readImageBytes(relative: string): Promise<Uint8Array | null> {
  try {
    if (await isNativeFs()) {
      const result = await Filesystem.readFile({
        path: relative,
        directory: Directory.Data,
      });
      return b64ToBytes(result.data as string);
    }
    const value = localStorage.getItem("img:" + relative);
    return value ? b64ToBytes(value) : null;
  } catch {
    return null;
  }
}

export function imageUrl(relative: string): string {
  return `local-image://${relative}`;
}
