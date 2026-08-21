# 🐷 Better-money · 个人智能记账与储蓄助手

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white">
  <img alt="ECharts" src="https://img.shields.io/badge/ECharts-5.5-AA344D?style=flat-square">
  <img alt="AI" src="https://img.shields.io/badge/AI-OpenAI%2FDeepSeek%2FQwen%E5%8F%AF%E5%88%87%E6%8D%A2-412991?style=flat-square">
  <img alt="Platform" src="https://img.shields.io/badge/Windows%2FmacOS-%E6%9C%AC%E5%9C%B0%E8%BF%90%E8%A1%8C-0078D6?style=flat-square&logo=windows&logoColor=white">
  <img alt="Progress" src="https://img.shields.io/badge/M1--M7-%E5%85%A8%E9%83%A8%E5%AE%8C%E6%88%90-success?style=flat-square">
  <img alt="Last Commit" src="https://img.shields.io/github/last-commit/xianyunqingzhu/Better-money?style=flat-square">
  <img alt="Stars" src="https://img.shields.io/github/stars/xianyunqingzhu/Better-money?style=flat-square">
</p>

为"想攒钱但攒不住钱"的学生设计的本地记账工具：每晚花几分钟记账，
随时知道还剩多少钱、目标攒到哪了，每周/每月收到一篇像朋友写的小作文总结。

完整设计见 `设计文档.md`，**详细使用教程见 `使用说明.md`**。

## 快速开始

### Windows

1. 双击 `启动.bat`
   - 首次运行会自动创建虚拟环境并安装依赖（需要联网）
2. 浏览器自动打开 http://127.0.0.1:8642
3. 首次使用会弹出**四步引导**：起点（全新开始 / 迁移旧数据 / 从备份恢复）→ 初始余额 → 月预算与自动存比例 → AI 配置（可跳过）
4. 开始记账。关闭命令行窗口即停止服务。

### macOS

1. 双击 `启动.command`（首次运行自动创建虚拟环境并安装依赖，需要联网与 Python 3.10+）
2. 浏览器自动打开 http://127.0.0.1:8642，关闭终端窗口即停止服务
3. 或双击 `Better-money.app`（无窗口后台运行，停止用 `停止服务.command`；可右键"制作替身"把替身拖入 Dock）
4. 若提示"无法打开，因为来自身份不明的开发者"：右键文件 → 打开；或终端运行 `xattr -dr com.apple.quarantine 项目文件夹路径`

手动启动（可选）：

```bat
.venv\Scripts\activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8642
```

### 数据放在哪里

- **源码运行**：未设置额外环境变量时，默认使用项目里的 `data/`。
- **未来 Windows 安装版**：打包后的应用将把 `%LOCALAPPDATA%\BetterMoney` 作为应用数据根目录；这是安装版目标，目前不要把它当成已经发布的安装包。
- **开发测试**：必须把 `BETTER_MONEY_HOME` 指到 `%TEMP%` 下的一次性目录，测试数据写入该目录的 `data/`，不要指向项目根目录。

## 里程碑进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | FastAPI + SQLite + 网页骨架（手动记账、看板、设置） | ✅ 已完成 |
| M2 | 文字批量记账（LLM 解析多笔/收入/AA/补记） | ✅ 已完成 |
| M3 | 截图/小票识别（确认面板）+ CSV 账单导入 | ✅ 已完成 |
| M4 | ECharts 图表看板（占比/趋势/对比/目标进度） | ✅ 已完成 |
| M5 | 周/月总结小作文（非模板化） | ✅ 已完成 |
| M6 | 攒钱增强：预算预警/冷静期/储蓄率/目标清单/对账 | ✅ 已完成 |
| M7 | 打磨：自动备份/数据导出/历史明细/使用说明 | ✅ 已完成 |

## 功能一览

- **记账**：文字批量（多笔、AA 分摊、补记、收入识别、估算标记）· 手动表单 · 截图/小票识别（确认面板可改可删，PNG/JPG/WebP 校验）· 微信/支付宝账单 CSV/Excel 导入（自动去重、大小与类型限制）
- **看板**：余额 / 本月收支 / 今日可花额度 / 分类占比饼图 / 30 天趋势 / 近 8 周对比 / **多目标进度列表**（已存 / 需要 / 还差）/ 月份切换 / 预算预警（80% 黄、100% 红）
- **目标**：愿望清单多目标、优先级排序、冷静期（放弃记入"省下的钱"）、收入自动存按优先级跨目标分配、达成自动记支出、金额调拨、一键删除
- **总结**：点「生成总结」选区间（本周 / 上周 / 本月 / 上月 / 自定义任意区间），周/月类型自由组合；同区间可覆盖重写；总结可删除（不影响账目）；账目修改后自动标过期
- **账本**：起始日初始余额（只设置一次，每月自动滚动）、对账校准与**调整撤销**、已规划金额与可支配余额拆分
- **设置**：AI 服务商预设（OpenAI / DeepSeek / Qwen / 自定义）+ 一键测试连接、初始余额受保护更正（自动安全备份）、立即备份 / 恢复备份 / 导出完整备份 ZIP / 打开数据文件夹
- **保障**：启动自动备份（保留 30 份）、校验过的 ZIP 完整备份与恢复、旧数据迁移向导、AI 挂了红横幅提示 + 手动兜底、解析失败进待处理队列

## 目录结构

```
Better-money/
├── app/                # 后端
│   ├── main.py         # FastAPI 入口与全部 API
│   ├── db.py           # SQLite 建表与访问
│   ├── ai.py           # AI 层（文字解析/图片识别，可切换供应商）
│   ├── summarizer.py   # 周/月总结生成
│   ├── importers.py    # 微信/支付宝账单解析
│   ├── backup.py       # 启动自动备份
│   └── config.py       # 配置（data/config.json）
├── static/             # 网页前端（HTML/CSS/JS + ECharts 本地库）
├── tests/              # 六套端到端测试 + 回归脚本 + mock LLM
├── tools/              # 图标生成、mac .app 打包脚本
├── data/               # 运行时生成：数据库、配置、图片、备份（不提交）
├── 启动.bat / 停止服务.bat        # Windows 启动/停止
├── 启动.command / 停止服务.command # macOS 启动/停止
├── Better-money.app/   # macOS 应用包（可拖入 Dock）
├── requirements.txt
├── 设计文档.md
├── 使用说明.md
└── README.md
```

## 说明与安全

- 源码模式的数据默认保存在项目的 `data/`；未来 Windows 安装版的数据根目录为 `%LOCALAPPDATA%\BetterMoney`。
- 完整备份是 ZIP 归档：包含数据库、已去除 API Key 的配置、清单，以及选择包含图片时的图片文件；不要把单独的 `backup.db` 当成完整备份。
- 服务只监听 `127.0.0.1`，仅供本机使用，无需登录；**不要把端口开放到公网**。
- API Key 只保存在当前数据目录的 `data/config.json`，不会写进完整备份，也不会上传到任何别的地方。
- 记账数据（文字/截图）会发送给你配置的大模型服务用于解析，请知悉。
