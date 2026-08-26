# 🐷 Better-money · 会记账、会攒钱、还会写总结的本地账本

<p align="center">
  <img src="docs/logo.png" width="96" alt="Better-money">
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-Capacitor-3178C6?style=flat-square&logo=typescript&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white">
  <img alt="ECharts" src="https://img.shields.io/badge/ECharts-5.5-AA344D?style=flat-square">
  <img alt="AI" src="https://img.shields.io/badge/AI-OpenAI%2FDeepSeek%2FQwen%E5%8F%AF%E5%88%87%E6%8D%A2-412991?style=flat-square">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-10%2F11-0078D6?style=flat-square&logo=windows&logoColor=white">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white">
  <img alt="Android" src="https://img.shields.io/badge/Android-8.0%2B-3DDC84?style=flat-square&logo=android&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-4caf50?style=flat-square">
  <img alt="Stars" src="https://img.shields.io/github/stars/xianyunqingzhu/Better-money?style=flat-square">
</p>

<p align="center"><b>为「想攒钱但攒不住钱」的你设计：每晚花一分钟记几笔，随时知道还剩多少钱、目标攒到哪了，每周还能收到一篇像朋友写的总结小作文。</b></p>

<p align="center">数据 100% 留在本地 · 没有账号 · 没有云端 · 没有广告</p>

---

## ✨ 为什么选 Better-money

| 💡 优点 | 说明 |
|---|---|
| 🤖 **一句话记多笔** | 输入「午饭食堂 15、奶茶 12、聚餐 200 4人AA、昨天兼职 300」——自动拆成多笔、自动算 AA 分摊、自动补记日期，还能识别收入和退款 |
| 📷 **小票拍照即记** | 拍购物小票或支付截图，自动识别商品、金额、商家；识别结果逐条确认后才入账，不怕 AI 看走眼 |
| 🐷 **专门治「冲动消费」** | 想买的东西先进「目标清单」冷静 7 天；每笔收入自动存一部分到目标；「先不买」还会把省下的钱记进荣誉榜 |
| 📊 **钱花哪了一目了然** | 分类占比、近 30 天趋势、近 8 周对比、目标进度，全部本地图表，切月查看历史 |
| ✍️ **会写总结的账本** | 每周/每月自动生成一篇总结小作文——语气可选：朋友、毒舌、温柔、老师 |
| 📱 **电脑 + 手机双端** | 电脑端功能最全；手机端随身记；两端通过一个 ZIP 文件交换账本，微信传一下就行 |
| 🔒 **数据完全归你** | 无服务器、无账号、无实时同步；API Key 只存在你自己的设备上，共享文件里永远没有它 |
| 💾 **备份做得较真** | 每次启动自动备份；完整备份 ZIP 带校验、可恢复；升级自动迁移，绝不悄悄丢数据 |
| 🆓 **不要钱** | 免费开源；AI 功能自备 API Key（OpenAI / DeepSeek / Qwen 都支持），用多少花多少 |

## 📱 双端覆盖

| | 💻 电脑端（Windows / macOS） | 📱 手机端（Android 8.0+） |
|---|---|---|
| 记账 | 智能文字解析、手动、CSV/Excel 账单导入 | 智能文字解析、手动 |
| 识图 | 上传小票/截图 | 拍照 / 相册多选（可追加至 10 张） |
| 退货 | — | **退款自动配对原支出**，全额/部分退自动修正，也可从历史手动选择 |
| 看板 | 完整图表看板 | 两页横滑：五项数据 + 图表 |
| 目标 | 完整目标清单 | 完整目标清单（含冷静期/自动存） |
| 总结 | 周/月总结（可配图） | 周/月总结 |
| 历史 | 明细表格 | 卡片列表 + 筛选 |
| 对账 | 对账校准 + 撤销 | 同左 |
| 数据 | 本地 SQLite + 备份 | 本地 SQLite + 备份 |
| 交换 | **共享包导出/导入** | **共享包导出/导入** |

## 🔄 两端怎么交换数据

不需要云端、不需要两台设备同时在线——手动传一个 ZIP 就行：

```
电脑端 ──导出──▶ better-money-share-*.zip ──微信/USB/网盘──▶ 手机端
   ▲                                                          │
   └─────────────── 手机再导出，电脑导入（可来回） ◀─────────────┘
```

- 导入前先**预览**：来自哪台设备、新增/修改/删除多少笔，确认后才写入
- 同一天两边都改过才弹冲突面板：保留本机 / 使用包内 / 合并逐条处理
- 删除会同步传播，已删除的不会「复活」；重复导入同一包不会重复
- 共享包**不含** API Key、总结正文和图片原件；失败自动回滚

## 🚀 快速开始

### 电脑端

**最简单的方式**：到 [Releases](https://github.com/xianyunqingzhu/Better-money/releases) 下载 `BetterMoney-Setup-<版本>.exe` 双击安装，桌面图标打开即用（内置 Python，无需安装环境）。

**源码运行**（开发者）：
- Windows：双击 `启动.bat` → 浏览器自动打开 `http://127.0.0.1:8642`
- macOS：双击 `启动.command`；或双击 `Better-money.app` 后台运行

首次使用有四步引导：全新开始 / 迁移旧数据 / 从备份恢复 → 初始余额 → 月预算与自动存比例 → AI 配置（可跳过）。

### 手机端

1. 到 [Releases](https://github.com/xianyunqingzhu/Better-money/releases) 下载 `better-money-app-<版本>.apk`
2. 传到手机，允许「安装未知来源应用」后安装（覆盖升级数据保留）
3. 想把电脑账本搬过来：电脑「设置 → 数据与共享」导出共享包 → 手机「设置 → 数据与共享」导入
4. 手机「设置 → AI」填自己的 API Key（Key 属于哪家服务商，Base 和模型就填哪家）

> 📖 详细教程：**[使用说明.md](使用说明.md)** ｜ 手机端构建说明：**[mobile/README.md](mobile/README.md)** ｜ 共享包格式：[docs/共享同步包格式.md](docs/共享同步包格式.md)

## 🧭 开发里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | FastAPI + SQLite + 网页骨架（手动记账、看板、设置） | ✅ 已完成 |
| M2 | 文字批量记账（LLM 解析多笔/收入/AA/补记） | ✅ 已完成 |
| M3 | 截图/小票识别（确认面板）+ CSV 账单导入 | ✅ 已完成 |
| M4 | ECharts 图表看板（占比/趋势/对比/目标进度） | ✅ 已完成 |
| M5 | 周/月总结小作文（非模板化） | ✅ 已完成 |
| M6 | 攒钱增强：预算预警/冷静期/储蓄率/目标清单/对账 | ✅ 已完成 |
| M7 | 打磨：自动备份/数据导出/历史明细/使用说明 | ✅ 已完成 |
| M8 | **手机端 Android 应用 + 两端共享同步（schema v3）** | ✅ 已完成 |

## 🏗 技术栈

```
├── 电脑端  Python 3.13 · FastAPI · SQLite · ECharts（本地 Web 应用，单机 127.0.0.1）
├── 手机端  TypeScript · Capacitor(WebView) · sql.js · ECharts（纯本地，无后端服务）
└── 测试    电脑端 380+ pytest（含 E2E 六脚本）；手机端 vitest 行为基线；两端共享包互通验证
```

## 🔒 数据与隐私

- 所有账目、图片、配置、备份**只存在你自己的设备**；服务只监听 `127.0.0.1`，请勿把端口开放到公网
- API Key 只保存在本机配置文件，**不会**进入共享包、完整备份或日志
- 共享包不含 API Key、总结正文与图片原件；完整备份 ZIP 同样剔除 API Key 并带清单校验
- 记账文字/图片会发送给你配置的大模型服务用于解析（用哪家由你决定），请知悉

## 📂 目录结构

```
Better-money/
├── app/                # 电脑端后端（FastAPI 入口、AI 层、备份、共享同步）
├── static/             # 电脑端网页前端
├── mobile/             # 手机端（Capacitor + TypeScript，详见 mobile/README.md）
│   ├── src/domain/     # 业务逻辑（金额整数分、统计、共享合并，全部可单测）
│   ├── src/db/         # sql.js 本地库与 schema v3 迁移
│   └── android/        # Android 工程（Gradle 构建，无需 Android Studio）
├── tests/              # 电脑端测试（单元 + 契约 + 六套 E2E + 安装版冒烟）
├── docs/               # 文档与设计说明
├── tools/              # 图标生成、基准备份、跨端互通验证等工具
├── 设计文档.md / 使用说明.md
└── 启动.bat / 启动.command   # 源码启动脚本
```

---

<p align="center">🐷 记账不难，攒钱也不难——从今晚记下第一笔开始。</p>
