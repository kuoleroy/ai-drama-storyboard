#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_beat_density.py —— 节拍密度体检器。

密度有两个方向，本脚本两个都查：
  上限 —— 一镜塞太多，演不完（与 check_handoff.py 互补）
  下限 —— 一镜内容太薄，节奏散、观众划走（爽不起来的根因）

三条刻度（来自《GPT剧本生成指令》的手榴弹式紧凑推进）：
  ① 局势变化密度：每 45–75 秒一次有效变化；每 2–3 分钟一个节点；每 5–7 分钟一个转折
  ② 单节拍发言密度：每个 15 秒节拍 3–5 句有效发言
  ③ 单节拍画面密度：每个节拍能撑起 2–5 个有作用的运镜

用法：
    python3 check_beat_density.py <稿子.txt> [--sec 15]
"""

import argparse
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, split_shots, parse_segments, norm_time,
)

DLG_LINE = re.compile(r"^[^（(【\s][^（(]{0,14}[（(][^）)]{0,26}[）)]\s*[：:]\s*[“\"「『]")

# 局势变化信号：决策/证据/关系/阻碍/目标
CHANGE_MARKERS = [
    # 决策与行动
    "决定", "答应", "拒绝", "同意", "要走", "去追", "签了", "签下", "打开", "合上",
    "推开", "递出", "交出", "放下", "拿起", "转身", "离开", "回来", "追出去", "起身",
    # 证据与信息
    "原来", "竟然", "发现", "看清", "照片", "调出", "签名", "字迹", "编号", "日期",
    "回响", "听见", "看见", "认出", "证据", "照片上", "写着",
    # 关系与情绪转折
    "对不起", "你是", "你就是", "不是他", "是她", "认出", "愣住", "崩溃", "松了",
    "松了口气", "沉默了很久", "第一次", "从今天起", "以后", "再也不",
]

# 空转信号：只有看/想/沉默/走路，没有实质变化
IDLE_ONLY = [
    "沉默", "沉默着", "发呆", "愣住", "看着", "望着", "抬头看", "低头", "走了几步",
    "沉默片刻", "没有说话", "眼神", "静静地",
]


def get_dialogue_lines(block):
    """取出该镜的台词行（不含汇总栏）。"""
    out = []
    for l in block[1:]:
        ls = l.strip()
        if ls.startswith("【台词与声音】"):
            continue
        if DLG_LINE.match(ls):
            out.append(ls)
    return out


def count_chars(lines):
    n = 0
    for l in lines:
        for m in re.findall(r"[“\"「『]([^”\"」』]{1,})[”\"」』]", l):
            n += len(m.strip())
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--sec", type=int, default=None, help="单镜时长（秒），默认从标题读")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("文件不存在: %s" % args.src)
        return 2

    lines, _, _ = read_lines(args.src)
    shots = split_shots(lines)
    if not shots:
        print("未找到分镜标题行")
        return 2

    print("=" * 72)
    print("节拍密度体检: %s" % args.src)
    print("=" * 72)

    rows = []
    for num, sec, block in shots:
        per = sec or args.sec or 15
        dlg = get_dialogue_lines(block)
        chars = count_chars(dlg)
        segs, _ = parse_segments(block[1:])
        body = "\n".join(l for l in block[1:] if l.startswith("运镜"))
        changes = [w for w in CHANGE_MARKERS if w in body]
        idle = [w for w in IDLE_ONLY if w in body] and not changes
        rows.append({
            "num": num, "sec": per, "chars": chars, "dlg": len(dlg),
            "segs": len(segs), "changes": changes, "idle": idle,
            "density": chars / float(per),
        })

    # ---- ① 逐镜密度档位 ----
    print("\n【1】单镜台词密度（每秒字数）")
    print("  基准：3–4 字/秒为可演区间，扣除动作与反应时间")
    thin, ok, tight = [], [], []
    for r in rows:
        d = r["density"]
        if d < 2.0:
            thin.append(r)
        elif d > 4.6:
            tight.append(r)
        else:
            ok.append(r)
    print("  过薄(<2.0 字/秒): %d 镜   合适: %d 镜   偏挤(>4.6): %d 镜"
          % (len(thin), len(ok), len(tight)))
    if thin:
        print("  过薄清单（内容不足，该加信息或加动作后果）：")
        for r in thin[:12]:
            print("    分镜%03d  %d 字 / %d 秒 = %.2f 字/秒  运镜%d段"
                  % (r["num"], r["chars"], r["sec"], r["density"], r["segs"]))
        if len(thin) > 12:
            print("    ... 还有 %d 镜" % (len(thin) - 12))
    if tight:
        print("  偏挤清单（演不完，要拆句或前移）：")
        for r in tight[:8]:
            print("    分镜%03d  %d 字 / %d 秒 = %.2f 字/秒"
                  % (r["num"], r["chars"], r["sec"], r["density"]))

    # ---- ② 局势变化间隔 ----
    print("\n【2】局势变化间隔（目标：每 45–75 秒一次有效变化）")
    gaps = []
    last_change_shot = None
    last_change_sec = 0
    for r in rows:
        if r["changes"]:
            if last_change_shot is not None:
                gap = r["num"] - last_change_shot
                gaps.append((gap, last_change_shot, r["num"]))
            last_change_shot = r["num"]
    if gaps:
        avg = sum(g[0] for g in gaps) / len(gaps)
        long_gaps = [g for g in gaps if g[0] >= 4]
        print("  平均间隔 %.1f 镜 | 最长 %d 镜" % (avg, max(g[0] for g in gaps)))
        if long_gaps:
            print("  ! 间隔 ≥4 镜（>60 秒无有效变化）：")
            for g in long_gaps[:8]:
                print("    分镜%03d → %03d（空 %d 镜）" % (g[1], g[2], g[0]))
        else:
            print("  ✓ 无 ≥4 镜的长空档")
    else:
        print("  ! 未检出任一变化信号 —— 可能整稿缺变化标记（需人工判断）")

    # ---- ③ 运镜数 ----
    print("\n【3】单节拍运镜数（目标 2–5 个有作用的运镜）")
    low = [r for r in rows if r["segs"] < 2]
    high = [r for r in rows if r["segs"] > 5]
    print("  运镜数分布: %s" % dict(sorted(Counter(r["segs"] for r in rows).items())))
    if low:
        print("  ! 运镜 <2 段（画面撑不起时长）：%s"
              % "、".join("分镜%03d" % r["num"] for r in low[:10]))
    if high:
        print("  ! 运镜 >5 段（切太碎，AI 易生成雷同画面）：%s"
              % "、".join("分镜%03d" % r["num"] for r in high[:10]))
    if not low and not high:
        print("  ✓ 全部在 2–5 区间")

    # ---- ④ 空转镜 ----
    print("\n【4】空转镜（只有看/想/沉默/走路，无实质变化）")
    idle_shots = [r for r in rows if r["idle"]]
    if idle_shots:
        for r in idle_shots[:10]:
            print("    分镜%03d" % r["num"])
        print("  共 %d 镜 —— 连续出现会让节奏发散" % len(idle_shots))
    else:
        print("  ✓ 无")

    # ---- ⑤ 发言句数 ----
    print("\n【5】单镜发言句数（目标 3–5 句有效发言）")
    few = [r for r in rows if r["dlg"] < 3]
    many = [r for r in rows if r["dlg"] > 8]
    print("  句数分布: %s" % dict(sorted(Counter(r["dlg"] for r in rows).items())))
    if few:
        print("  ! 少于 3 句：%s" % "、".join("分镜%03d" % r["num"] for r in few[:10]))
    if many:
        print("  ! 多于 8 句（可能塞太满）：%s"
              % "、".join("分镜%03d" % r["num"] for r in many[:10]))
    if not few and not many:
        print("  ✓ 全部在合理区间")

    # ---- 汇总 ----
    total = len(thin) + len(tight) + len(low) + len(high) + len(idle_shots)
    print("\n" + "=" * 72)
    print("合计待关注 %d 处" % total)
    print("提示：密度不足是『爽不起来』的根因 —— 先补密度，再调爽点方向，最后改台词。")
    print("      本工具只给客观计数，是否算『有效变化』仍需人工判断。")
    print("=" * 72)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
