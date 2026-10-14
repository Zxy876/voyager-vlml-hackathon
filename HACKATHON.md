# MVE — Nebius × NVIDIA Global AI Hackathon 提交说明

> **Track: Personal AI** —— *"Build an always-on, private assistant that works
> for you while keeping your data under your control. Give it persistent
> memory, reusable skills, access to the tools and information you choose,
> and the ability to carry out tasks across your daily workflows."*
>
> MVE (Minimum Viable Egocentric) is exactly that, for **data work**: an agent
> that answers your questions against your own database, and — the part most
> demos skip — **learns a reusable skill for every question it answers**, with
> success verified against the database itself, not against another model's
> opinion.

## 一句话

**Voyager 的学习闭环 × 真实数据库取证**：agent 自己出题、自己写 SQL、取到数才算学会；
学会的每条取数配方进技能库；下次你再问，它复用配方回答——**记忆在技能库里，
不在模型权重里**（这正是本赛道的 "persistent memory, reusable skills"）。

## 为什么是 Personal AI，而不是又一个聊天机器人

| Personal AI 赛道要求 | MVE 的对应实现 |
|---|---|
| persistent memory（持久记忆） | `mve/skill_store.json`：每条技能 = 一条验证过的 SQL 配方 + 一句话方法 + 命中/成功计数。重启不丢，跨会话复用 |
| reusable skills（可复用技能） | 检索键是**任务文本**（同构原版 Voyager `retrieve_skills(query=context)`，无 topic_id 路由层）；同名技能 Rewriting 不膨胀 |
| access to the tools you choose（自选工具） | 10 个 MCP 数据工具（`query_sql` + `get_database_info` + 8 个报告），agent 自己决定调哪个 |
| carry out tasks across workflows（跨工作流执行） | 两条线：**练习线**（自己出题→写 SQL→取数→固化技能）和**消费线**（你问一句→复用技能回答→裁判独立复核） |
| your data under your control（数据自主） | 全部数据在本地 DuckDB；LLM 只生成 SQL，**不回传数据**；换数据源 = 改一行 `db_config.json` |

## 它解决的真实问题（Potential Impact）

LLM 会"答得像"但取不到真数：空表上一条聚合 SQL 返回 `(NULL, 0)`，行数判据
`rows ≥ 1` 会把它当成"取到数了"——坏配方就此固化成"技能"，复用时答出
**0% / 样本量 0** 这种看起来完整的假答案（本项目 2026-10-14 实测踩到并修复，
见 README 第 21 章）。MVE 的 critic 不看模型态度，只看数据库：
**取到数 ⇔ 至少一次工具调用成功 且 结果里有真实值**（全 NULL、除 0 外全 NULL
都算没取到）。在这个判据下，"学会"是可验证的事件，不是自报的。

## 提交期内的重大更新（项目早于提交期，按规则说明）

项目主体开发于 2026-09-30 起（Voyager × VLML 同构迁移）。提交期内
（2026-10-14 → 10-30）完成：

1. **LLM 后端迁到 Nebius Token Factory**：全仓库 LLM 调用收敛到唯一出口
   `voyager_vlml/llm.py`（OpenAI 兼容 HTTP，无 SDK 依赖），`NEBIUS_API_KEY`
   在即走 Token Factory + NVIDIA Nemotron；未配密钥时每次调用大声警告，
   防止演示悄悄回退到非 Nebius 后端（那等于提交不符合规则的项目）。
2. **「真空成功」修复**（判据层）：`critic.is_vacuous` / `has_real_data`——
   把"取到数"从"工具 ok 且行数 ≥ 1"收紧到"至少一个真实值"；消费线五处闸门
   （降级判据/事实/裁判/方法比对/叙事/沉淀）与裁判侧重打包逻辑全部只认真数。
   单测 22 项（`test_vacuum.py`）。
3. **技能库真空审计**（`audit_skills.py`）：19 条技能逐条对库实跑，6 条真空，
   4 条换已验证 SQL、2 条修模式/口径 bug——换数据源后必跑此脚本。

## 怎么在 Nebius 上跑（Setup）

```bash
# 0) 依赖（duckdb + httpx，其余标准库；Python ≥ 3.10）
pip install -r requirements.txt

# 1) LLM 后端 = Nebius Token Factory + NVIDIA Nemotron
cp .env.example mve/.env      # 填入 NEBIUS_API_KEY（studio.nebius.ai 创建）
python scripts/check_llm.py   # 冒烟：列模型 + 实跑一次对话和 JSON 通道

# 2) 数据层（VLML 数据层为第三方目录 vlml/，本仓库不含；数据来自 rib.gg 公开赛况）
#    ingest 落盘 raw_events/ → run_pipeline 幂等入库（详见 README 第 14 章）
python mve/vlml_source/ingest.py --event 2977 --max 8 --db vlml/data/vlml_rib.duckdb
python mve/knowledge_graph.py --build    # 建图谱（可选，load() 带指纹守卫）

# 3) 跑起来
python mve/dashboard.py                  # 面板 http://127.0.0.1:8777
python -m voyager_vlml.loop --rounds 5   # 练习线：自己出题写 SQL，技能库开始增长
# 消费线：面板「人导入」问一句，例如：
#   在 'Split' 地图上，G2 Esports 队在赢得首血之后，平均每回合能赢下多少比例？
#   请同时给出样本量。
# 期望（已在 rib 库实查验证）：100%、样本量 7；裁判独立取数复核，覆盖率 1.0
```

**Demo（线上实例，Nebius 后端）**：部署后填写 —— 面板九个分区，建议走法：
数据源确认 → 启动 Voyager → 运行中 → 轨迹 → 技能·因果（看技能库从 0 增长）
→ 人导入同题复测。

## 技术栈与 Nebius/NVIDIA 用量

- **唯一 LLM 出口** `voyager_vlml/llm.py`：OpenAI 兼容 HTTP POST
  `https://api.tokenfactory.nebius.com/v1/chat/completions`，Bearer `NEBIUS_API_KEY`，
  模型 `nvidia/nemotron-3-super-120b-a12b`（默认；更省可换 nemotron-3-nano-30b-a3b）。
- **LLM 在系统里干什么**（也只干这些）：出题、写 SQL、判真空后的 critique 引导、
  叙事回答。**成败判据不经过 LLM**——由 DuckDB 取证给出（见上）。
- **Token Factory 加速工作流的点**：练习线每轮 3 次以内迭代修正 + 消费线
  裁判独立取数，都是 Nemotron 的短上下文、高频、低延迟调用；nano/super 分档
  正好覆盖"快速日常调用 vs 认真推理"两档。

## 诚实的限制（ Judges 值得知道 ）

1. 演示数据集是 Valorant 公赛数据（rib.gg）——引擎与数据源无关
   （`db_config.json` 换库即换领域），但仓库里**验证过的技能是这份数据上的**。
2. 模型能力上限如实报告：Nemotron/GLM 在 SQL 列名纠错上都会 3 轮改不对
   （Binder Error 不消退），这时系统**如实记失败**，不假装学会——
   判据保证"失败如实报失败"，不保证"模型一定写对 SQL"。
3. 面板是原型完成度，不是产品打磨度；代码注释与 README 第 20 章
   （调试档案）记录了完整踩坑史。

## 仓库结构（评委快速定位）

| 路径 | 内容 |
|---|---|
| `voyager_vlml/loop.py` | 练习线：里程碑选题 → 出题 → 写 SQL → 判据 → 固化技能 |
| `voyager_vlml/consume.py` | 消费线：问一句 → 复用技能 → 裁判复核 → 叙事回答 |
| `voyager_vlml/agents/critic.py` | 判据层：真空检测 + SQL 语言 critique（回灌迭代） |
| `voyager_vlml/llm.py` | **唯一 LLM 出口**（Nebius Token Factory 优先） |
| `mve/skill_store.py` | 技能库（同构原版 SkillManager：同名 Rewriting） |
| `mve/dashboard.py` | 面板（九个分区，线上演示入口） |
| `test_vacuum.py` | 判据层单测（22 项） |
| `audit_skills.py` | 技能库真空审计（换数据源后必跑） |
| `README.md` | 完整文档（第 21 章 = 原版 Voyager 技能↔里程碑关系取证 + 本次修复全记录） |
