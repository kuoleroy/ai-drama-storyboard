#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_repetition.py —— 语义重复检测器。

机器查不出的"剧情完整性"靠通读，但**形式上的语义重复**可以查出一部分：
  1. 换词再问     同一问题在不同镜里用不同措辞再问一遍
  2. 换人解释     不同角色复述同一件事
  3. 重复震惊     同一刺激点多次产生相同反应
  4. 同句式复读   舞台说明、场景栏、运镜标签的固定句式反复
  5. 承诺/保证复读 同一承诺被反复确认

用法：
    python3 audit_repetition.py <分镜稿.txt> [--threshold 0.62] [--top 20]

说明：
  * 用字符二元组（bigram）的 Jaccard 相似度做近似判断，不是语义理解
  * 命中率会偏高（宁可多报），最终由人工确认
  * 退出码：0 = 未发现；1 = 发现疑似重复（可作为人工通读的优先清单）
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, find_titles, split_shots, parse_segments, field,
)

DLG_LINE = re.compile(r"^[^（(【\s][^（(]{0,14}[（(][^）)]{0,26}[）)]\s*[：:]\s*[“\"「『]")
QUOTE_RE = re.compile(r"[“\"「『]([^”\"」』]{2,})[”\"」』]")

# 疑问句式
QUESTION_MARKERS = ["？", "?", "吗", "呢", "什么", "为什么", "怎么", "谁",
                     "哪", "多少", "是不是", "有没有"]

# 震惊/情绪反应
SHOCK_MARKERS = ["愣住", "僵住", "瞳孔", "倒吸", "一惊", "怔", "手一抖",
                 "脸色", "失声", "说不出", "颤", "发抖", "呼吸停"]

# 承诺/保证
PROMISE_MARKERS = ["答应", "保证", "一定", "肯定", "不会", "放心", "承诺",
                   "说话算话", "我保证"]


def bigrams(s):
    """字符二元组集合（去掉标点与空白）。"""
    s = re.sub(r"[^\w\u4e00-\u9fa5]", "", s)
    if len(s) < 2:
        return {s} if s else set()
    return set(s[i:i + 2] for i in range(len(s) - 1))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b))


def overlap_coef(a, b):
    """重叠系数 = 交集 / 较小集合。

    短句用 Jaccard 会失真（"慢走，下次记得还。" vs "借你的。下次记得还。"
    只有 0.44，但共享 4 个字）—— 重叠系数能抓到"共用意图不同措辞"。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def _sim(a, b):
    """综合相似度：取 Jaccard 与重叠系数的较大值。"""
    return max(jaccard(a, b), overlap_coef(a, b) * 0.9)


# 台词里的"高频功能短语"——同一句式骨架的不同措辞，最典型的重复
PHRASE_PATTERNS = [
    ("下次记得还", ["下次记得还", "记得还"]),
    ("明天还", ["明天还", "明天给你送来", "明天还你", "明天给你送"]),
    ("路上小心/慢点", ["路上小心", "路上慢点", "慢走", "路上当心"]),
    ("拿着/给伞", ["拿着", "给伞", "给你伞", "递过"]),
    ("谢谢", ["谢谢", "多谢", "谢了"]),
    ("对不起", ["对不起", "抱歉"]),
    ("你还在吗", ["你还在吗", "你在吗", "你在么"]),
]


def phrase_repeat(dlg):
    """检测高频功能短语在多镜重复出现（同一句式骨架换措辞）。"""
    hits = []
    for label, pats in PHRASE_PATTERNS:
        matched = []
        for num, spk, txt in dlg:
            if any(p in txt for p in pats):
                matched.append((num, spk, txt))
        if len(matched) >= 2:
            hits.append((label, matched))
    return hits


def extract_dialogue_with_shot(shots):
    """返回 [(shot_no, speaker, text)]

    注意：同一句台词通常在**正文**和【台词与声音】汇总栏里各出现一次。
    这里只取正文（正文才是段落级的真实归属），汇总栏仅在没有正文时兜底。
    """
    out = []
    for num, sec, block in shots:
        body_lines = []
        summary_lines = []
        for l in block[1:]:
            ls = l.strip()
            if ls.startswith("【台词与声音】"):
                summary_lines = [p.strip() for p in
                                 ls[len("【台词与声音】"):].split(" ｜ ")]
                continue
            body_lines.append(ls)

        got = []
        for ls in body_lines:
            if not DLG_LINE.match(ls):
                continue
            m = re.match(r"^([^（(【\s][^（(]{0,14})[（(][^）)]{0,26}[）)]\s*[：:]\s*(.*)$", ls)
            if not m:
                continue
            speaker = m.group(1)
            for t in QUOTE_RE.findall(m.group(2)):
                got.append((speaker, t))
        # 正文没有 → 用汇总栏兜底
        if not got:
            for part in summary_lines:
                m = re.match(r"^([^（(]{1,14})[（(]([^）)]{0,26})[）)]\s*[：:]\s*[“\"「『](.+)[”\"」』]$", part)
                if m:
                    got.append((m.group(1), m.group(3)))
        for spk, t in got:
            out.append((num, spk, t))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--threshold", type=float, default=0.62,
                    help="Jaccard 相似度阈值（越高越严）")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("文件不存在: %s" % args.src)
        return 2

    lines, _, _ = read_lines(args.src)
    shots = split_shots(lines)
    if not shots:
        print("未找到分镜标题行")
        return 2

    dlg = extract_dialogue_with_shot(shots)
    report = {"file": args.src, "shots": len(shots), "dialogue": len(dlg)}

    print("=" * 72)
    print("语义重复检测: %s" % args.src)
    print("=" * 72)
    print("分镜 %d 个 | 台词 %d 句 | 相似度阈值 %.2f" % (len(shots), len(dlg), args.threshold))
    print("-" * 72)

    findings = []

    # ---- 1. 换词再问：同一说话人问相似问题 ----
    q_by_speaker = defaultdict(list)
    for num, spk, txt in dlg:
        # 太短的句子（"为什么。"）本身不是问题，跳过
        if len(txt.strip()) < 5:
            continue
        if any(m in txt for m in QUESTION_MARKERS):
            q_by_speaker[spk].append((num, txt))
    rep_ask = []
    seen_pairs = set()
    for spk, items in q_by_speaker.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a[0] == b[0]:
                    continue
                key = (spk, a[1], b[1])
                if key in seen_pairs:
                    continue
                sc = _sim(bigrams(a[1]), bigrams(b[1]))
                if sc >= args.threshold:
                    seen_pairs.add(key)
                    rep_ask.append((sc, spk, a, b))
    rep_ask.sort(reverse=True, key=lambda x: x[0])
    print("\n【1】换词再问（同一说话人问相似问题，跨镜）")
    if rep_ask:
        for sim, spk, a, b in rep_ask[:args.top]:
            print("  ! %.2f  分镜%03d vs %03d  [%s]" % (sim, a[0], b[0], spk))
            print("       A: %s" % a[1][:70])
            print("       B: %s" % b[1][:70])
        print("  共 %d 组疑似" % len(rep_ask))
    else:
        print("  无")
    findings += [("换词再问", len(rep_ask))]

    # ---- 2. 台词整体相似（任意两句，不限疑问句）----
    rep_pair = []
    seen_pairs = set()
    for i in range(len(dlg)):
        for j in range(i + 1, len(dlg)):
            a, b = dlg[i], dlg[j]
            if a[0] == b[0]:
                continue
            key = (a[2], b[2])
            if key in seen_pairs:
                continue
            s = _sim(bigrams(a[2]), bigrams(b[2]))
            if s >= args.threshold:
                seen_pairs.add(key)
                rep_pair.append((s, a, b))
    rep_pair.sort(reverse=True, key=lambda x: x[0])
    print("\n【2】台词整体相似（任意两句台词雷同）")
    if rep_pair:
        for s, a, b in rep_pair[:args.top]:
            print("  ! %.2f  分镜%03d vs %03d" % (s, a[0], b[0]))
            print("       A: [%s] %s" % (a[1], a[2]))
            print("       B: [%s] %s" % (b[1], b[2]))
        print("  共 %d 组疑似" % len(rep_pair))
    else:
        print("  无")

    # ---- 3. 高频功能短语（同一句式骨架换措辞 —— 最典型的重复）----
    ph = phrase_repeat(dlg)
    print("\n【3】高频功能短语（同一句式骨架在不同镜重复）")
    if ph:
        for label, matched in ph:
            locs = "、".join("分镜%03d「%s」" % (n, t) for n, _, t in matched[:6])
            print("  ! 「%s」出现 %d 次：%s" % (label, len(matched), locs))
        print("  注：出现 2 次多为伏笔收束（可接受）；≥3 次为冗余重复，须改。")
        print("  共 %d 组" % len(ph))
    else:
        print("  无")
    findings += [("高频短语", len([1 for _, m in ph if len(m) >= 3]))]

    # ---- 3b. 纸面文字主动作词（会让 AI 生成乱码，必须改）----
    print("\n【3b】纸面文字主动作词（提示词在命令 AI 生成文字）")
    WRITE_ACT = ["写下", "写下一个名字", "写一行字", "写了", "写好", "写着字",
                 "填写", "题字", "签名", "标注着", "印着字"]
    paper_hits = []
    for num, sec, block in shots:
        for l in block[1:]:
            ls = l.strip()
            if not ls.startswith("运镜"):
                continue
            for w in WRITE_ACT:
                if w in ls:
                    paper_hits.append((num, w, ls[:70]))
                    break
    if paper_hits:
        for num, w, ctx in paper_hits[:12]:
            print("  ! 分镜%03d 「%s」→ %s" % (num, w, ctx))
        print("  共 %d 处 —— 只要提示词出现『写下/写一行字』，AI 就会生成乱码文字。" % len(paper_hits))
        print("  改法：改成『笔尖划过纸面，只留下极淡墨痕，看不出任何字迹』。")
    else:
        print("  ✓ 无")
    findings += [("纸面文字动作", len(paper_hits))]

    # ---- 4. 换人解释同一事实 ----
    by_content = defaultdict(list)
    for num, spk, txt in dlg:
        for key in re.split(r"[，。！？；：…、,.!?;:]", txt):
            key = key.strip()
            if len(key) >= 4:
                by_content[key].append((num, spk, txt))
    rep_explain = []
    for key, items in by_content.items():
        # 同一镜内同一句只算一次
        uniq = []
        seen = set()
        for it in items:
            k = (it[0], it[2])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(it)
        if len(uniq) < 2:
            continue
        speakers = set(i[1] for i in uniq)
        if len(speakers) >= 2:      # 不同人复述同一事
            rep_explain.append((len(uniq), key, uniq))
    rep_explain.sort(reverse=True, key=lambda x: x[0])
    print("\n【4】换人解释同一事实（不同角色复述同一信息点）")
    if rep_explain:
        for n, key, items in rep_explain[:args.top]:
            locs = "、".join("分镜%03d/%s" % (i[0], i[1]) for i in items[:5])
            print("  ! 「%s」被 %d 处复述 → %s" % (key[:30], n, locs))
        print("  共 %d 个信息点被复述" % len(rep_explain))
    else:
        print("  无")
    findings += [("换人解释", len(rep_explain))]

    # ---- 3. 重复震惊 ----
    shock = []
    for num, spk, txt in dlg:
        hits = [m for m in SHOCK_MARKERS if m in txt]
        if hits:
            shock.append((num, spk, txt, hits))
    # 按情绪词分组，看同一刺激点是否多次产生相同反应
    shock_groups = defaultdict(list)
    for num, spk, txt, hits in shock:
        shock_groups[hits[0]].append((num, spk, txt))
    rep_shock = [(k, v) for k, v in shock_groups.items() if len(v) >= 2]
    print("\n【5】重复震惊（同一情绪词多次出现）")
    if rep_shock:
        for k, v in rep_shock:
            print("  ! 「%s」×%d：%s" % (k, len(v),
                  "、".join("分镜%03d" % i[0] for i in v[:8])))
        print("  共 %d 组" % len(rep_shock))
    else:
        print("  无")
    findings += [("重复震惊", len(rep_shock))]

    # ---- 4. 承诺/保证复读 ----
    prom = [(num, spk, txt) for num, spk, txt in dlg
            if any(m in txt for m in PROMISE_MARKERS)]
    prom_groups = defaultdict(list)
    for num, spk, txt in prom:
        for m in PROMISE_MARKERS:
            if m in txt:
                prom_groups[m].append((num, spk))
                break
    rep_prom = [(k, v) for k, v in prom_groups.items() if len(v) >= 3]
    print("\n【6】承诺/保证复读（同一承诺被反复确认）")
    if rep_prom:
        for k, v in rep_prom:
            print("  ! 「%s」出现 %d 次：%s" % (k, len(v),
                  "、".join("分镜%03d" % i[0] for i in v[:10])))
        print("  共 %d 组" % len(rep_prom))
    else:
        print("  无")
    findings += [("承诺复读", len(rep_prom))]

    # ---- 5. 同句式复读（舞台说明 / 场景栏 / 运镜标签）----
    stage = Counter()
    scene = Counter()
    tags = Counter()
    for num, sec, block in shots:
        for l in block[1:]:
            ls = l.strip()
            if ls.startswith("（") and ls.endswith("）"):
                stage[ls[:16]] += 1
            if ls.startswith("场景："):
                scene[ls[:14]] += 1
            m = re.match(r"^运镜\d+\[([^\]]*)\]", ls)
            if m and m.group(1):
                tags[m.group(1)] += 1
    rep_stage = [(k, v) for k, v in stage.most_common(5) if v >= 3]
    rep_scene = [(k, v) for k, v in scene.most_common(5) if v >= 3]
    rep_tags = [(k, v) for k, v in tags.most_common(8) if v >= 3]
    print("\n【7】同句式复读")
    for name, items, thr in (("表演说明", rep_stage, 3), ("场景栏", rep_scene, 3),
                              ("运镜标签", rep_tags, 3)):
        if items:
            print("  ! %s：" % name)
            for k, v in items:
                print("       %s ×%d" % (k[:44], v))
        else:
            print("  · %s：无高频复读" % name)
    findings += [("表演说明复读", len(rep_stage)), ("场景栏复读", len(rep_scene)),
                 ("运镜标签复读", len(rep_tags))]

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    total = sum(v for _, v in findings)
    print("合计疑似 %d 处" % total)
    print("提示：命中率高是正常的 —— 本工具只给**人工通读的优先清单**，")
    print("      是否真重复、有没有必要的新后果，仍需逐条判断。")
    print("=" * 72)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
