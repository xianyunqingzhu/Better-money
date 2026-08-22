# Better-money 手机端（Android）

Capacitor + TypeScript 实现的独立本地账本应用。与电脑端通过手动传递的
共享 ZIP 包交换数据，不需要云端服务器，API Key 只保存在手机本地。

## 技术栈与关键决策

| 项 | 选择 |
|---|---|
| UI | 原生 HTML/CSS/TS + Vite，两页横滑布局（360dp 基准，触控区 ≥44px） |
| 数据库 | **sql.js**（SQLite WASM）+ 原子落盘到应用私有目录（.tmp→改名，保留 .bak） |
| 图表 | ECharts（按需注册：饼/线/柱） |
| 相机/相册 | @capacitor/camera（系统 intent，无需运行时权限） |
| 文件选择 | @capawesome/capacitor-file-picker（系统文件选择器） |
| 分享导出 | @capacitor/share（系统分享面板） |
| AI 调用 | CapacitorHttp（原生 HTTP，无 CORS 问题），直连用户配置的 API Base |
| 金额 | 整数分运算，避免浮点误差 |
| 行为基线 | 与电脑端 Python 测试逐项对齐（tests/ 下的 vitest 移植了同款断言） |

选择 sql.js 而不是原生 SQLite 插件的原因：开发环境（浏览器）与设备上运行的是
**完全相同的代码路径**，能在本地把全部行为验证完再交付真机；个人账本数据量
（几千行）对内存无压力；写操作后原子落盘 + 保留上一版备份，可靠性足够。

## 开发

```bash
cd mobile
npm install
npm run dev          # 浏览器开发（数据存 localStorage，功能与真机一致）
npm test             # vitest：domain/share 行为基线
npm run build        # 类型检查 + 构建 dist/
npx cap sync android # 同步到 android/
cd android && ./gradlew assembleDebug   # 构建 debug APK
```

## 构建 release APK（本机签名）

```bash
cd mobile
npm run build && npx cap sync android
cd android
./gradlew assembleRelease
# 用 keystore 签名后输出 release APK；keystore 需单独备份（丢失无法覆盖升级）
```

APK 产物：`android/app/build/outputs/apk/debug/app-debug.apk`；
release：`android/app/build/outputs/apk/release/app-release-unsigned.apk`（需签名）。

## 目录

```
src/
├── domain/     纯业务逻辑（ledger/goals/stats/summaries/share/ai，全部可单测）
├── db/         sql.js 封装、schema v3 迁移、仓储层
├── platform/   存储/图片/文件选择适配
└── ui/         两页主界面 + 记一笔/确认/目标/总结/设置/共享面板
tests/          vitest 行为基线（与 Python 测试同款断言）
```

## 共享包格式

见仓库根目录 `docs/共享同步包格式.md`。手机端与电脑端实现互相兼容
（同一套验收用例在两端都有测试覆盖）。
