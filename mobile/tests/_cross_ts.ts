/** 跨实现互通验证：TS 端（手机）导入/导出。 */
import { readFileSync, writeFileSync } from "node:fs";
import { LedgerRepo } from "../src/db/repository";
import { applyImport, exportSharePackage, parseSharePackage, previewImport } from "../src/domain/share";

class MemoryStorage {
  files = new Map<string, Uint8Array>();
  async read(name: string) {
    return this.files.get(name) ?? null;
  }
  async write(name: string, data: Uint8Array) {
    this.files.set(name, data);
  }
  async delete(name: string) {
    this.files.delete(name);
  }
  async exists(name: string) {
    return this.files.has(name);
  }
}

const mode = process.argv[2]; // import | export | roundtrip
const zipPath = process.argv[3];
const exportPath = process.argv[4];

async function main() {
  const repo = await LedgerRepo.open(new MemoryStorage());
  if (mode === "import") {
    const pkg = await parseSharePackage(readFileSync(zipPath!));
    const preview = previewImport(repo, pkg);
    const result = await applyImport(repo, pkg, {});
    console.log(JSON.stringify({
      preview_add_tx: preview.summary.add_transactions,
      preview_add_goals: preview.summary.add_goals,
      applied_add_tx: result.add_transactions,
      applied_add_goals: result.add_goals,
      live_tx: repo.listTransactions().length,
      live_goals: repo.listGoals().length,
      pending: repo.syncStatus().pending_changes,
    }));
  } else if (mode === "roundtrip") {
    const pkg = await parseSharePackage(readFileSync(zipPath!));
    const result = await applyImport(repo, pkg, {});
    const exported = await exportSharePackage(repo);
    writeFileSync(exportPath!, Buffer.from(exported.zip));
    console.log(JSON.stringify({
      imported_tx: result.add_transactions,
      imported_goals: result.add_goals,
      exported: exported.counts,
      pending: repo.syncStatus().pending_changes,
    }));
  } else {
    const exported = await exportSharePackage(repo);
    writeFileSync(zipPath!, Buffer.from(exported.zip));
    console.log(JSON.stringify({ exported: exported.counts }));
  }
  repo.db.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
