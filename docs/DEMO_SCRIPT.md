# 录屏流程与台词：MVE 3 分钟演示（2026-10-14 实测版）

> 你负责点，这份告诉你：点什么、按什么顺序、每步说什么（英文台词）、出意外怎么接。
> 所有数字都是今天在活实例上实测的，不是估计。

## 0. 开录前检查（2 分钟，每项 10 秒）

| 检查项 | 预期 | 不对怎么办 |
|---|---|---|
| 打开 https://43.161.203.50:8443/mve-hackathon/ | 弹「连接不是私密连接」→ 点**高级** → **继续前往** | 证书警告只弹一次，之后不再弹 |
| 概览页底部数据源卡片 | 绿色徽章 **VLML 就绪**，9 表 / 6 系列赛 | 刷新页面 |
| 点「技能·因果」tab | 空库（0 条技能） | 跟我说，我一条命令清场重来 |
| 顶部驾驶舱状态 | 「从未启动过」 | 同上 |
| 剪贴板 | 预存问题文本（见 §2 第 4 步） | — |

浏览器设置：Chrome、窗口 1920×1080、缩放 125%（字太小评委看不清）、
隐藏书签栏（Cmd+Shift+B）、关掉无关标签页。录屏工具先试录 10 秒确认有麦克风。

## 1. 时间轴总览

| 时间 | 画面 | 你做 |
|---|---|---|
| 0:00–0:20 | 面板主页 | 开场白 + 证书警告一句话带过 |
| 0:20–0:45 | 概览 tab | 指数据源卡片 |
| 0:45–1:45 | 练习 tab | 点 **▶ 启动 Voyager**，看 1～2 轮跑完（每轮约 30–60 秒） |
| 1:45–2:10 | 轨迹 → 技能·因果 | 各停 10 秒指曲线/技能卡 |
| 2:10–2:20 | 人导入 tab | 粘贴问题 → 点 **交给 Voyager 回答** |
| 2:20–3:10 | 人等结果 | 讲裁判机制 + 诚实声明（结果约 1–2 分钟到，实测 40–60 秒） |
| 3:10–3:40 | 人导入 tab | 指最新一条导入：100% / 样本量 7 + 收尾 |

全程约 3.5–4 分钟。想压回 3 分钟：砍 §2 第 5 步里的知识图谱飞览，第 2 步只看一轮。

## 2. 分段脚本

### 第 1 步 · 开场（0:00–0:20）——面板保持首页，不用点

> "Hi, this is MVE — an agent that answers questions against your own
> database, and learns a reusable skill for every question it answers.
> Success is checked against the database itself, never against another
> model's opinion. You'll see a certificate warning — that's our own
> self-signed cert, just click through. No login needed."

### 第 2 步 · 数据源（0:20–0:45）——停在概览 tab，鼠标指底部数据源卡片

> "This is the overview. The green badge means the environment is ready:
> nine tables, six real match series from rib.gg — actual Valorant data,
> thirteen megabytes, vendored right into the repo. Nothing here is
> mocked; you can clone the repository and check every number yourself."

### 第 3 步 · 启动学习循环（0:45–1:45）——点 **▶ 启动 Voyager**，然后手离开鼠标

> "Now I start Voyager. Watch what happens. It proposes its own question —
> it's asking how many distinct event types occurred in series 1057. It
> writes the SQL, runs it against DuckDB, and the critic decides success
> by ground truth: did we actually get real data back? Six rows — success.
> That recipe is now stored as a skill. The skill library started at zero,
> one minute ago."

等一轮完整跑完（约 30–60 秒）再动。**不要停循环**——让它继续在后台跑，
第 5 步等待时有「期间又学会几条」的收尾素材。

### 第 4 步 · 轨迹与技能（1:45–2:10）——点「轨迹」tab 停 10 秒，再点「技能·因果」停 10 秒

> "The trajectory tab is the learning curve. The skills tab is what it
> actually learned — each skill is the question, the SQL, and an embedding.
> This is the whole point: the memory lives in the skill library, not in
> the model weights."

### 第 5 步 · 人导入（2:10–2:20）——点「人导入」tab → 粘贴问题 → 点 **交给 Voyager 回答**

问题文本（开录前复制到剪贴板）：

```
On the 'Split' map, after G2 Esports wins the first blood, what fraction
of rounds do they win on average? Include the sample size.
```

> "Now the part most demos skip. I ask one sentence — [paste] — and click
> 'let Voyager answer'. The skill library is nearly empty, so, like the
> original Voyager at iteration zero, it writes the recipe from scratch.
> This takes about a minute — I'll keep talking while it runs."

归类下拉保持默认「自动匹配」，不用动。

### 第 6 步 · 等待时填充（2:20–3:10）——可选：切「知识图谱」tab 飞览 10 秒

> "While it works: an independent referee re-queries the same question
> from scratch, and the two answers are compared. If they corroborate,
> it says so. If they don't — it says that too, in the same units.
>
> One honest caveat up front: in our ten-run benchmark, ten out of ten
> answers were real and numerically correct — but every single one was
> written on the spot. How many skills until it answers by retrieval
> instead? That's an open question, and we left it open rather than
> invent a number."

（知识图谱飞览台词，时间紧可删）："The curriculum comes from a knowledge
graph — 314 nodes, 771 edges — built from VLML's own SQL catalog."

### 第 7 步 · 结果落地（3:10–3:40）——切回「人导入」tab，指**最新一条**导入历史

先指答案行（两种裁判结果，指着屏幕念实际显示的那个）：

**A. 裁判同意时：**
> "There it is: one hundred percent, sample size seven — the exact number
> our documentation promises, reproducible from a fresh clone. And the
> independent referee got the same number. Mutually corroborated."

**B. 裁判挑出方法问题时（今天实测就落到这一支）：**
> "There it is: one hundred percent, sample size seven — correct. But
> look what it says next: the referee computed the quantity differently,
> so instead of declaring victory, the system flags its own method and
> says interpret with caution. It reports its uncertainty in the same
> units it uses to report answers."

收尾（两种情况通用）：
> "The repository and this live demo are linked below. Thanks for watching."

## 3. 应急分支

| 情况 | 台词 / 动作 |
|---|---|
| 某轮练习失败（判据说没取到数） | "This round failed — and it says so honestly. We don't paper over failures; a failure is recorded as a failure."（失败如实留空本来就是设计卖点，不用慌） |
| 一轮超过 60 秒还没完 | "This round is taking a while — Nemotron is thinking." 不要静默，静默评委以为卡了 |
| 导入 90 秒还没出结果 | 结果在后台跑、4 秒轮询自动进历史。继续讲第 6 步内容，到了再切回来看 |
| LLM 报错 / 面板无响应 | 刷新页面（状态在服务器，不丢）；刷新后直接口播 HACKATHON.md 里的 10/10 实测表收尾 |
| 中途发现技能库不是 0 开始的 | 不用管，台词改成 "it already knows a few recipes — watch it add more" |

## 4. 台词总字数与节奏

英文台词约 390 词 ≈ 2 分 45 秒（140 词/分钟），加点击和等待约 3.5–4 分钟。
超时的两个口子：第 3 步只看一轮（省 1 分钟）、第 6 步删图谱飞览（省 15 秒）。

## 5. 本脚本的实测依据（2026-10-14 晚，活实例）

- 面板干净态：9 表 / 6 系列赛 / env ready / 技能 0 / 导入 0 / 从未启动
- 学习循环实测 1 轮：自选 vlml_grain_event → SQL 取到 6 行真数 → 判据成功 → 技能入库
- 消费线实测 1 次（同题同库）：答案 **100% / 样本量 7**（与文档承诺一致）；
  裁判独立复查询，判定「两边不是同一个量」→ 叙事如实自报「方法有硬伤，谨慎解读」
- 消费线耗时：约 40–60 秒（面板提示「约 1～3 分钟」偏保守）
- 无 mem0 服务的降级路径已验证（coach.py try/except，不阻断）
