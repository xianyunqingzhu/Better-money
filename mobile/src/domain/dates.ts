/** 日期辅助。 */

export function parseDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const d = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

export function toIso(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function todayIso(): string {
  return toIso(new Date());
}

export function monthBounds(month: string): { first: string; last: string } {
  const [year, mon] = month.split("-").map(Number);
  const lastDay = new Date(year, mon, 0).getDate();
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    first: `${year}-${pad(mon)}-01`,
    last: `${year}-${pad(mon)}-${pad(lastDay)}`,
  };
}

export function monthRange(month: string, now: Date = new Date()): { first: string; last: string } {
  if (!month) return monthBounds(`${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
  return monthBounds(month);
}

export function addDays(iso: string, days: number): string {
  const d = parseDate(iso);
  if (!d) return iso;
  d.setDate(d.getDate() + days);
  return toIso(d);
}

/** 该月剩余天数（含今天）。 */
export function daysLeftInMonth(now: Date = new Date()): number {
  const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  return lastDay - now.getDate() + 1;
}

export const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export function todayDesc(recordDate: string, now: Date = new Date()): string {
  const d = parseDate(recordDate) || now;
  return `今天是 ${toIso(d)} ${WEEKDAYS[d.getDay() === 0 ? 6 : d.getDay() - 1]}`;
}

/** 与桌面 now_str() 同格式。 */
export function nowStr(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}
