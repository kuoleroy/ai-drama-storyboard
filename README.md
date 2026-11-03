# AI 短剧分镜生成与投喂改造 · Skill

把剧本改造成**可以逐镜丢给各家 AI 视频生成 API** 的分镜稿。

针对的硬约束只有一个：**生成视频的 API 没有记忆**。它读到"上一镜""承接前文""沿用上一场的道具"会困惑，甚至把这些字当成画面元素直接生成出来。本技能的全部工作，就是让每一镜都自足。

---

## 这个技能解决什么

| 症状 | 根因 | 本技能的做法 |
|---|---|---|
| **某平台直接报错 400** | 段长 <4 秒（Veo3/Seedance2.0/Wan 的下限） | 体检 + 按平台档位自动重切段 |
| 导入工具多算一个分镜 | 正文里有 `{分镜052（…）}` 这种带花括号的交叉引用 | 去花括号 + 去镜号，改成"前文的…" |
| 画面描述被当成台词念出来 | 运镜头粘在了台词行尾部 | 自动拆回独立行 |
| 生成"只转镜头、人不动" | 运镜段正文为空 + 没有主体位移 | 补空段正文；每段加主体位移 + 镜头跟随 |
| 每镜构图雷同 | 491 段都以"可见人物1人。"开头 | 元标注抽成每镜一行 |
| 道具下一镜凭空消失或换手 | 只写了名字没写状态 | 【道具】列清单，【道具状态】锁开合/持有者/位置 |
| 道具看着很怪 | 锁了外观细节 | 关键道具进参考图锁形制，普通道具放权；状态另写【道具状态】 |
| 文案上的字全是乱码 | 模型生成中文文字极弱 | 模糊化，关键信息交给台词 |
| 剧情平淡、观众划走 | 爽点后置 + 憋屈段连片 | 六种信息型爽点引擎，不动人物场景 |

---

## 通用性：一稿喂遍各家 API

这是本技能最核心的约束 —— **同一份分镜稿要能喂遍可灵、Seedance、Veo、Runway、Sora、Wan、海螺、Luma**。

各家单次生成时长：

| 平台 | 时长 | 画幅 | 原生音频 |
|---|---|---|---|
| 可灵 2.6 / o1 | **5 / 10 秒** | 9:16 | ✅ |
| Seedance 2.0 / 2.5 | **4–15 / 4–30 秒** | 9:16 | ✅ |
| Veo 3 | **4 / 8 秒** | 9:16 | ✅ |
| Runway Gen-4 | **5–10 秒** | 9:16 | ❌ |
| Sora 2 | **10–20 秒** | 9:16 | ✅ |
| Wan 2.6 | **2–10 秒** | 9:16 | ❌ |
| 海螺 / Luma | **5–10 秒** | 9:16 | ❌ |

**通用交集：段长 4–10 秒，5 秒与 10 秒是全平台共同档位，9:16 是全平台共同画幅。**

> ⚠️ 段长 < 4 秒会被 Veo 3 / Seedance 2.0 / Wan 直接拒收。

所以 15 秒/镜应该切成 **2 段（10+5）**，30 秒/镜切成 **3 段（10+10+10）**，
而不是常见写法的 15 秒 4 段、30 秒 8 段。

导出器会自动按目标平台的档位重切，并抹平字段名差异：

```bash
python3 scripts/export_for_api.py 分镜稿.txt --platform all -o ./api_export
```

```
api_export/
├── kling/
│   ├── manifest.json    调用清单：params + prompt + 首帧续写链 + 使用说明
│   ├── prompts.txt       人读的逐段提示词
│   └── segments/         每段一个 txt
├── veo/
├── seedance/
└── ...
```

字段名差异（`duration` vs `seconds` vs `duration_seconds`、`size` 写 `1080*1920`、
参考图字段 `refer_image` / `images` / `reference_urls`）由适配层统一处理。

---

## 快速开始

```bash
# 锁稿核验（拿到剧本要拆分镜时先跑）：时长撑不撑得住、锁定清单齐不齐
python3 scripts/check_handoff.py 稿子.txt --shots 130 --sec 15

# 节拍密度（内容够不够厚？爽点密不密？）
python3 scripts/check_beat_density.py 稿子.txt

# 体检（结构、时间码、空段、粘连、重复、跨镜引用）
python3 scripts/audit_storyboard.py 稿子.txt

# 语义重复（换词再问、换人解释同一事实、承诺复读）
python3 scripts/audit_repetition.py 稿子.txt

# 空间方向（内外/进出防穿帮）
python3 scripts/audit_spatial.py 稿子.txt

# 通用性体检（段长是否落在各平台安全区）
python3 scripts/export_for_api.py 稿子.txt --check

# 机械修复（先自动备份）
python3 scripts/fix_storyboard.py 稿子.txt [--dry]

# 生成投喂版
python3 scripts/build_feed_prompts.py 稿子.txt -o 输出.txt

# 导出各家 API 调用包
python3 scripts/export_for_api.py 稿子.txt --platform kling,veo,seedance -o ./api_export

# 回归自检
python3 scripts/selftest.py
python3 scripts/e2e_test.py 稿子.txt
```

也可以直接把技能装进 WorkBuddy：

```
WorkBuddy → 设置 → 技能 → 添加本地技能 → 选 ai-drama-storyboard 目录
```

---

## 流水线全貌

```
剧本定稿
  ↓  check_handoff.py --shots N --sec M      ← 锁稿核验（时长撑不撑得住？清单齐不齐？）
  ↓  check_beat_density.py                   ← 节拍密度（内容够厚吗？爽点密吗？）
  ↓  交付锁稿清单 + 不可改动项清单
分镜稿
  ↓  audit_storyboard.py                      ← 体检（空段/粘连/重复/跨镜引用）
  ↓  audit_repetition.py                      ← 语义重复（换词再问/换人解释/承诺复读）
  ↓  人工通读（剧情完整性、语义重复必须靠人）
  ↓  fix_storyboard.py                        ← 机械修复（保留原格式）
  ↓  build_feed_prompts.py                    ← 每镜自足
投喂稿
  ↓  export_for_api.py --check                ← 通用性体检（段长合规）
  ↓  export_for_api.py --platform all         ← 导出各家 API 调用包
生成
```

---

## 目录结构

```
ai-drama-storyboard/
├── SKILL.md                          核心方法论（十二条铁律）
├── references/
│   ├── beat_density.md                节拍密度控制（爽不起来的根因）★
│   ├── handoff_spec.md               剧本→分镜交接规范（锁稿核验）★
│   ├── script_review.md              分镜稿审核与原格式修订规范 ★
│   ├── spatial_anchor.md             空间方向三层锚定（内外防穿帮）★
│   ├── visual_style.md               视觉风格库（52 风格 + 四视角场景图）★
│   ├── short_prompts.md              三份短指令模板（连续性/违规/后缀）
│   ├── api_compat.md                 各平台能力矩阵与通用基线 ★
│   ├── field_spec.md                 各栏位写法规范
│   ├── pitfalls.md                   踩坑详解（症状→根因→修法）
│   └── creative_review.md            爽点评估与改造方法论
├── scripts/
│   ├── shotparse.py                  解析/写回公共库
│   ├── check_handoff.py              锁稿核验器 ★
│   ├── check_beat_density.py         节拍密度体检器 ★
│   ├── audit_storyboard.py           体检器（10 项检查）
│   ├── audit_repetition.py           语义重复检测器 ★
│   ├── audit_spatial.py              空间方向检测器 ★
│   ├── fix_storyboard.py             机械修复器
│   ├── build_feed_prompts.py         投喂栏生成器
│   ├── export_for_api.py             多平台适配导出器 ★
│   ├── selftest.py / e2e_test.py     回归与端到端测试
└── assets/                            各指令原文 + 单镜样例（可直接复制使用）
```

---

## 十二条铁律（摘要）

0. **通用性优先** —— 段长 4–10 秒、9:16、参考图 ≤5、负向写两遍、台词加引号
1. **每镜自足** —— 只写本镜看得见的内容，禁止跨镜指代
2. **关键道具锁外观，状态锁死** —— 关键道具进参考图，普通道具放权；开合/持有者/位置走【道具状态】
3. **纸面文字一律模糊化** —— 关键信息由台词交付
4. **人物要动，镜头要跟** —— 每段有可见位移，镜头跟随该位移
5. **同一句式不要复读** —— 模型最容易照抄句式
6. **剧情爽 ≠ 打斗爽** —— 逐镜流水线做不了打斗
7. **静态化一切高难度动作** —— 信息压迫比动作威胁更抓人
8. **交接前先锁稿** —— 时长撑不撑得住？分镜阶段无创作权限
9. **审核只改有依据的问题，且不改原格式** —— 最小文字修改，格式全保留
10. **空间方向要三层锚定** —— 参照物成对 + 动词带方向 + 门朝向声明
11. **密度要有下限** —— 每 45–75 秒一次有效变化，先补密度再调爽点

完整解释见 `SKILL.md`。

---

## 支持的格式

分镜标题行（三种都认）：

```
{分镜001｜30秒}
{分镜001｜15秒}
{分镜001}
```

运镜行：

```
运镜1[00:00-00:04|景别·动作]：正文
```

收尾括号 `]` / `］` / `】` / `〕` 混用会自动归一。

---

## 许可

MIT
