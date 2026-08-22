/** 平台存储适配：Android 用 Capacitor Filesystem，Web 开发用 localStorage。 */
import { Capacitor } from "@capacitor/core";
import { Directory, Filesystem } from "@capacitor/filesystem";
import type { StorageAdapter } from "../db/database";

const DB_KEY = "better_money_db_v1";

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

/** Android/iOS 原生文件存储（应用私有目录）。 */
const nativeStorage: StorageAdapter = {
  async read(name: string) {
    try {
      const result = await Filesystem.readFile({
        path: name,
        directory: Directory.Data,
      });
      return b64ToBytes(result.data as string);
    } catch {
      return null;
    }
  },
  async write(name: string, data: Uint8Array) {
    await Filesystem.writeFile({
      path: name,
      directory: Directory.Data,
      data: bytesToB64(data),
      recursive: true,
    });
  },
  async delete(name: string) {
    try {
      await Filesystem.deleteFile({ path: name, directory: Directory.Data });
    } catch {
      /* 不存在则忽略 */
    }
  },
  async exists(name: string) {
    try {
      await Filesystem.stat({ path: name, directory: Directory.Data });
      return true;
    } catch {
      return false;
    }
  },
};

/** Web 开发回退（localStorage；仅用于浏览器调试）。 */
const webStorage: StorageAdapter = {
  async read(name: string) {
    const value = localStorage.getItem(DB_KEY + ":" + name);
    return value ? b64ToBytes(value) : null;
  },
  async write(name: string, data: Uint8Array) {
    localStorage.setItem(DB_KEY + ":" + name, bytesToB64(data));
  },
  async delete(name: string) {
    localStorage.removeItem(DB_KEY + ":" + name);
  },
  async exists(name: string) {
    return localStorage.getItem(DB_KEY + ":" + name) !== null;
  },
};

export function platformStorage(): StorageAdapter {
  return Capacitor.isNativePlatform() ? nativeStorage : webStorage;
}

export function isNative(): boolean {
  return Capacitor.isNativePlatform();
}
