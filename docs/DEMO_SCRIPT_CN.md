# 中文口播逐字稿（总分总 · 交互点击版）

> 配合 `DEMO_SCRIPT.md`（流程+英文版+应急分支）使用。
> 括号内是**动作提示，不念出来**；语速按 220～240 字/分钟，全文约 740 字口播 + 等待，全程约 4 分钟。
> ⚠️ 官方规则：视频须英文或附英译。念中文版的话，用文末英文字幕烧录即可合规。

---

## 【总】开场 · 0:00–0:30

［动作：Chrome 打开 https://43.161.203.50:8443/mve-hackathon/ ，点「高级」→「继续前往」过证书警告，停在首页］

> 大家好，今天演示的项目叫 MVE。一句话介绍：它是一个能回答**你自己数据库里问题**的智能体，而且每答对一题，就学会一条可复用的技能——这是大多数演示都会跳过的地方。它的成败由数据库本身判定，不是由另一个模型来打分。
>
> ［停一秒］开头这个证书警告，是我们自己服务器的自签名证书，点「高级」「继续」就能进，不需要登录。

## 【分】第一部分 · 数据源 · 0:30–0:50

［动作：鼠标指概览页底部数据源卡片］

> 总览页底部这张卡片是数据源状态：绿色徽章，环境就绪——9 张表、6 场真实比赛，数据来自 rib.gg，是 Valorant 真实的职业比赛数据，13 兆，直接放在仓库里。这里没有任何假数据，你把仓库克隆下来，每一个数字都可以自己复现。

## 【分】第二部分 · 启动学习循环 · 0:50–1:50

［动作：点右上角 **▶ 启动 Voyager**，然后手离开鼠标，等一轮跑完（约 30–60 秒）］

> 现在点「启动 Voyager」，看它自己动。它自己给自己出题——比如这一轮：1057 号系列赛里，有几种不同的事件类型，每种发生了多少次。然后它自己写 SQL，在 DuckDB 上执行。
>
> 最关键的在后面：成败不看来不来得及吹，只看一件事——有没有真的从数据库里取到数。这一轮取到 6 行，真数，算成功。这条取数配方，当场作为一个技能存进了技能库。而一分钟前，这个技能库还是空的。

## 【分】第三部分 · 轨迹与技能 · 1:50–2:15

［动作：点「轨迹」tab 停 8 秒 → 点「技能·因果」tab 停 8 秒］

> 轨迹页是学习曲线。技能页是它真正学到的东西——每条技能是三件套：当初的问题、那段 SQL、还有一个向量，用来以后遇到相似问题的时候检索。
>
> 这就是整个项目的核心主张：**记忆存在技能库里，不存在模型权重里。**

## 【分】第四部分 · 人导入（重头戏）· 2:15–3:15

［动作：点「人导入」tab → 粘贴问题 → 点 **交给 Voyager 回答**］

问题文本：

```
On the 'Split' map, after G2 Esports wins the first blood,
what fraction of rounds do they win on average? Include the sample size.
```

> 接下来是重头戏。在人导入页，我问它一句话——「在 Split 地图上，G2 Esports 拿到首血之后，平均每回合的胜率是多少？请给出样本量。」——然后点「交给 Voyager 回答」。
>
> 现在技能库几乎是空的，所以它跟原版 Voyager 第零轮一样：现场写答案。整个过程大约一分钟，我边等边讲。
>
> 它跑的时候，有一个**独立的裁判**在把同一个问题重新查一遍——是独立查，不是抄它的。然后两边的答案比对：对得上，它就说是对得上；对不上，它也如实说对不上，用的是同一套单位。
>
> 顺便把实话说了：在我们十连跑的基准测试里，十次全部是真实取数、数值全部正确——但这十次全是现场写出来的。那么，积累多少条技能之后，它才能靠检索来回答？这个问题我们没有数字，实验设计写在文档里了，但我们不编。

［等待超 90 秒时的填充句，可选］：

> 补一句：这些题目来自一张知识图谱——314 个节点、771 条边——是从 VLML 自己的 SQL 目录里建出来的。

## 【分】第五部分 · 结果落地 · 3:15–3:45

［动作：切回「人导入」tab，指**最新一条**导入历史，看着屏幕实际显示的分支念］

> 结果到了：**百分之百，样本量 7**——正是文档里承诺的数字，你克隆仓库可以自己复现。
>
> 再看它自己怎么说：裁判是用不同的口径算的这个量，所以它没有宣布胜利，而是标记了自己的方法有硬伤，提醒小样本要谨慎解读。它报自己不确定的时候，用的是跟报答案一模一样的单位。

［如果实际落的是「同意」分支，把上面这句换成：］

> 结果到了：百分之百，样本量 7。而且裁判是独立复算的——拿到了同一个数字，两边互相印证。

## 【总】收尾 · 3:45–4:00

［动作：鼠标停在面板或切到仓库页］

> 总结一下：MVE 不是一个把名字写进系统提示词的聊天机器人。它做的是另一回事——在你的数据上，积累经过验证的经验，并且用报答案同样的口径，诚实地告诉你它哪里还不知道。仓库和在线演示的链接都在下面，谢谢观看。

---

## 附：英文字幕（中文版烧录用，按段落对齐）

| 段落 | 英文字幕 |
|---|---|
| 开场 | This is MVE — an agent that answers questions against your own database, and learns a reusable skill for every question it answers. Success is verified by the database itself, not by another model's opinion. *(The certificate warning is our own self-signed cert — click through, no login needed.)* |
| 数据源 | The overview shows the data source: environment ready — 9 tables, 6 real match series from rib.gg, 13 MB of real Valorant data vendored into the repo. Nothing is mocked; every number is reproducible from a fresh clone. |
| 启动循环 | I start Voyager. It proposes its own question, writes the SQL, and runs it on DuckDB. The critic decides success by ground truth: did we actually get real data back? Six rows — success. The recipe is now stored as a skill. One minute ago, this library was empty. |
| 轨迹与技能 | The trajectory tab is the learning curve. Each skill is three parts: the question, the SQL, and an embedding for retrieval. The core claim: the memory lives in the skill library, not in the model weights. |
| 人导入 | Now the part most demos skip. I ask one sentence and click "let Voyager answer". The library is nearly empty, so it writes from scratch — about a minute. Meanwhile an independent referee re-queries the same question, and the two answers are compared: corroborated or not, in the same units. Honest caveat: in our ten-run benchmark, all ten answers were real and numerically correct — but all were written on the spot. How many skills until it answers by retrieval? We left that open rather than invent a number. |
| 结果 | There it is: 100%, sample size 7 — the documented number, reproducible from a fresh clone. And notice: the referee computed the quantity differently, so instead of declaring victory, it flags its own method and says interpret with caution. *(If agreed: the referee independently got the same number — mutually corroborated.)* |
| 收尾 | MVE is not a chatbot with your name in the system prompt. It accumulates verified experience against your data — and reports what it doesn't know in the same units it uses to report answers. Links below. Thanks for watching. |
