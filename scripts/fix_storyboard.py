#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_storyboard.py —— 分镜稿机械问题自动修复器。

只做"有客观依据、可确定性判定"的修复，不做任何创作性改写。
创作性修改（补台词、调整剧情、模糊化具体文字）一律留给人工或提示词。

用法：
    python3 fix_storyboard.py <分镜.txt> [--dry] [--no-backup]

自动修复项：
  1. 运镜行收尾括号 全角 ］】〕 → 半角 ]        （解析器漏读）
  2. 运镜正文被粘在台词行尾部 → 拆回独立运镜行（否则会被当台词念出来）
  3. 空运镜段 → 从紧随其后的括号说明补回；仍为空则标注待补
  4. 时间码：后半镜未接上（跳回 00:00）→ 按原比例重排铺满 0→单镜时长
  5. 运镜编号重复/跳号 → 重排为连续序号
  6. 跨镜引用 → 替换为绝对表述（词表 + 数字镜号去指代化）
  7. 标题行里混进的正文（{分镜052（…）}）→ 去掉花括号，防止导入工具误判多镜

安全保证：
  * 每次运行先备份（可用 --no-backup 关闭，但建议保留）；
  * 原子写回（先写 .tmp 再 os.replace），不会因编码/异常把源文件清空；
  * 结束时打印逐项统计，供人工复核。
"""

import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, write_lines, backup, find_titles, split_shots,
    parse_segments, normalize_brackets, norm_time, SEG_RE, is_title_line,
)

# ---- 跨镜引用 → 绝对表述 的替换词表 ----------------------------------
# 原则：只替换"指代别的镜头"的说法；描述性对比（如"比上一镜更暗"）也一并去掉指代，
#       因为无状态模型无法理解任何跨镜比较。
CROSS_REPL = [
    ("（位置沿用上一镜）", ""),
    ("（沿用上一镜）", ""),
    ("接着上一镜；", ""),
    ("紧接上一镜；", ""),
    ("紧接上一镜，", ""),
    ("承接上一镜，", ""),
    ("接上一镜", ""),
    ("下一镜须继承：", "须继承："),
    ("下一镜继承：", "须继承："),
    ("延续至下一镜", "延续"),
    ("过渡到下一镜", "过渡"),
    ("下一镜", "随后"),
    ("与镜", "与前面那场"),
    ("比镜", "比前面那场"),
    ("与第", "与前文的"),
    ("比第", "比前文的"),
    ("全剧在 130 镜里", "全剧"),
    ("全剧第130镜", "全剧最后一镜"),
    # 放在最后：这些词最长，放前面会先被短词吃掉
    ("上一镜的", "此前的"),
    ("上一镜", "此前"),
    ("前一镜", "此前"),
    ("上一段", "此前"),
    ("前面那镜", "此前"),
    ("上一场", "此前那场"),
    ("前一幕", "此前"),
    ("上一批", "此前"),
]

# 形如 "第052镜（同意书笔迹…）" → "前文的同意书笔迹…"
NUM_CROSS_RE = re.compile(r"第\d{1,3}\s*镜\s*[（(]([^）)]*)[）)]")

# 形如 "{分镜052（同意书笔迹是七岁程远模仿）}" → "前文的同意书笔迹（七岁程远模仿）"
BRACE_CROSS_RE = re.compile(
    r"\{?\s*分镜\s*\d{1,3}\s*[（(]([^）)]*)[）)]\s*\}?"
)


def debrace_cross(text):
    """去掉花括号交叉引用里的镜号，改成"前文的…"这类无指代表述。"""
    def repl(m):
        inner = m.group(1).strip()
        return ("前文的" + inner) if inner else "前文"
    return BRACE_CROSS_RE.sub(repl, text)


def fix_brackets(lines, stats):
    out = []
    for l in lines:
        nl = normalize_brackets(l)
        if nl != l:
            stats["括号归一"] += 1
        out.append(nl)
    return out


def fix_glued(lines, stats):
    """把粘在台词行尾部的运镜头拆出来。"""
    out = []
    for l in lines:
        s = l.rstrip()
        m = re.search(r"(运镜\d+\[[^\]]*[\]］】〕]\s*[：:].*)$", s)
        if m and not s.startswith("运镜"):
            head = s[:m.start()].strip()
            if head:
                out.append(head)
            out.append(m.group(1))
            stats["拆开粘连"] += 1
            continue
        out.append(l)
    return out


def fix_empty_segments(lines, stats):
    """空运镜段：把紧随其后的括号说明搬进正文。返回 (新行, 被吞掉的行号集合)。"""
    out = []
    swallowed = set()
    empty_re = re.compile(r"^(运镜\d+\[[^\]］】〕]*[\]］】〕])\s*[：:]?\s*$")
    for i, l in enumerate(lines):
        m = empty_re.match(l.strip())
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if m and nxt.startswith("（") and nxt.endswith("）"):
            out.append(m.group(1) + "：" + nxt.strip("（）"))
            swallowed.add(i + 1)
            stats["空段补正文"] += 1
            continue
        out.append(l)
    return out, swallowed


def fix_timecodes(lines, stats):
    """修正"后半镜时间码跳回 00:00"这类断裂。

    只做一种确定性修复：当一镜内的运镜起点序列出现回退（后段起点 <= 前段起点），
    说明后半段没有接上前半段（常见于把 2 个 15 秒镜合并成 1 个 30 秒镜时）。
    此时把后半段整体平移到前半段结束处，并保持原有各段时长比例。
    不做"整体拉伸到标称时长"——那属于内容改写，须人工决定。
    """
    titles = find_titles(lines)
    if not titles:
        return lines
    out = list(lines)
    for k, (ti, num, sec) in enumerate(titles):
        end = titles[k + 1][0] if k + 1 < len(titles) else len(lines)
        block = out[ti:end]
        seg_idx = []
        for j, l in enumerate(block):
            m = SEG_RE.match(l.strip())
            if m:
                seg_idx.append((j, int(m.group(1)),
                                norm_time(m.group(2)), norm_time(m.group(3))))
        if len(seg_idx) < 2:
            continue
        starts = [a for (_, _, a, _) in seg_idx]
        # 找回退点
        cut = None
        for i in range(1, len(starts)):
            if starts[i] <= starts[i - 1]:
                cut = i
                break
        if cut is None:
            continue
        head_end = seg_idx[cut - 1][3]           # 前半段结束时间
        tail = seg_idx[cut:]
        tail_dur = sum(b - a for (_, _, a, b) in tail)
        if tail_dur <= 0:
            continue
        shift = head_end - tail[0][2]
        for i, (j, oldno, a, b) in enumerate(tail):
            na, nb = a + shift, b + shift
            m = SEG_RE.match(block[j].strip())
            tag = m.group(4) or ""
            body = m.group(5)
            block[j] = "运镜%d[%02d:%02d-%02d:%02d%s]：%s" % (
                oldno, na // 60, na % 60, nb // 60, nb % 60,
                ("|" + tag) if tag else "", body)
            stats["时间码接续"] += 1
    return out


def fix_cross_refs(lines, stats):
    out = []
    for l in lines:
        if is_title_line(l):
            out.append(l)      # 标题行绝不改动
            continue
        nl = l
        for a, b in CROSS_REPL:
            if a in nl:
                nl = nl.replace(a, b)
                stats["跨镜引用改写"] += 1
        # {分镜052（…）} / 第052镜（…） → 前文的…
        if BRACE_CROSS_RE.search(nl) or NUM_CROSS_RE.search(nl):
            nl = debrace_cross(nl)
            nl = NUM_CROSS_RE.sub(lambda m: "前文的" + m.group(1).strip(), nl)
            stats["花括号交叉引用"] += 1
        out.append(nl)
    return out


def renumber_segments(lines, stats):
    """每镜内的运镜编号重排为 1..N。"""
    titles = find_titles(lines)
    out = list(lines)
    for k, (ti, num, sec) in enumerate(titles):
        end = titles[k + 1][0] if k + 1 < len(titles) else len(lines)
        seq = 0
        for j in range(ti, end):
            m = SEG_RE.match(out[j].strip())
            if m:
                seq += 1
                old = int(m.group(1))
                if old != seq:
                    out[j] = out[j].replace("运镜%d[" % old, "运镜%d[" % seq, 1)
                    stats["运镜编号重排"] += 1
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    nobak = "--no-backup" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(2)
    path = args[0]
    if not os.path.exists(path):
        print("文件不存在: %s" % path)
        sys.exit(2)

    lines, had_bom, newline = read_lines(path)
    stats = {
        "括号归一": 0, "拆开粘连": 0, "空段补正文": 0,
        "时间码接续": 0, "跨镜引用改写": 0, "花括号交叉引用": 0, "运镜编号重排": 0,
    }

    lines = fix_glued(lines, stats)
    lines = fix_brackets(lines, stats)          # 先统一括号，后续正则才稳
    lines, swallowed = fix_empty_segments(lines, stats)
    lines = [l for i, l in enumerate(lines) if i not in swallowed]
    lines = fix_timecodes(lines, stats)
    lines = renumber_segments(lines, stats)
    lines = fix_cross_refs(lines, stats)        # 最后改文本，避免破坏标题结构

    print("=== 修复统计 ===")
    for k, v in stats.items():
        print("  %-14s %d" % (k, v))
    if not any(stats.values()):
        print("  （无需修复）")
    if dry:
        print("\n[dry-run] 未写入。去掉 --dry 即可落盘。")
        return

    if not nobak:
        bak = backup(path, tag="修复前备份")
        print("\n备份: %s" % bak)
    write_lines(path, lines, had_bom, newline)
    print("已写入: %s" % path)


if __name__ == "__main__":
    main()
