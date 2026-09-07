import type { AppDatabase } from "./database";

export interface Todo {
  id: number;
  title: string;
  priority: number;
  created_at: string;
  updated_at: string;
  completed_at: string;
}

/** 手机本地待办，完整数据库备份自动包含此表。 */
export class TodoRepo {
  constructor(private db: AppDatabase) {}

  list(completed = false): Todo[] {
    return this.db.query<Todo>(completed
      ? "SELECT * FROM todos WHERE completed_at <> '' ORDER BY completed_at DESC, id DESC"
      : "SELECT * FROM todos WHERE completed_at = '' ORDER BY priority, id");
  }

  private title(value: string): string {
    const title = value.trim();
    if (!title) throw new Error("请输入待办内容");
    if (title.length > 500) throw new Error("待办内容不能超过 500 字");
    return title;
  }

  private nextPriority(): number {
    return (this.db.queryOne<{ priority: number }>(
      "SELECT COALESCE(MAX(priority), 0) + 1 AS priority FROM todos WHERE completed_at = ''",
    ))!.priority;
  }

  create(value: string): number {
    const now = new Date().toISOString();
    this.db.run("INSERT INTO todos (title, priority, created_at, updated_at) VALUES (?, ?, ?, ?)",
      [this.title(value), this.nextPriority(), now, now]);
    return this.db.queryOne<{ id: number }>("SELECT last_insert_rowid() AS id")!.id;
  }

  edit(id: number, value: string) {
    this.db.run("UPDATE todos SET title = ?, updated_at = ? WHERE id = ?",
      [this.title(value), new Date().toISOString(), id]);
  }

  move(id: number, direction: -1 | 1) {
    const rows = this.list();
    const index = rows.findIndex(t => t.id === id);
    const other = index + direction;
    if (index < 0 || other < 0 || other >= rows.length) return;
    [rows[index], rows[other]] = [rows[other], rows[index]];
    this.db.transaction(() => rows.forEach((row, priority) => {
      this.db.run("UPDATE todos SET priority = ?, updated_at = ? WHERE id = ?",
        [priority, new Date().toISOString(), row.id]);
    }));
  }

  complete(id: number) {
    const now = new Date().toISOString();
    this.db.run("UPDATE todos SET completed_at = ?, updated_at = ? WHERE id = ? AND completed_at = ''",
      [now, now, id]);
  }

  restore(id: number) {
    this.db.run("UPDATE todos SET completed_at = '', priority = ?, updated_at = ? WHERE id = ? AND completed_at <> ''",
      [this.nextPriority(), new Date().toISOString(), id]);
  }
}
