#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_storyboard.py —— AI 短剧分镜稿体检器。

用法：
    python3 audit_storyboard.py <分镜.txt> [--json]

检查项（每一项都对应一条实战踩过的坑）：
  1. 结构：分镜数、编号连续性、标题格式、每镜时长
  2. 时间码：连续性、重叠、覆盖 0→时长；运镜编号是否重复
  3. 空运镜段：正文为空（AI 会自由发挥 ⇒ 人钉在原地）
  4. 粘连：运镜正文被粘在台词行尾部（会被当台词念出来）
  5. 跨镜引用：上一镜/第NNN镜/承接……（对无状态 API 是噪声）
  6. 重复度：台词内容、运镜标签、场景栏、表演说明句式
  7. 元标注复读：段首"可见人物N人"等句式覆盖率
  8. 主体位移：缺可见位移的段（镜头独自运动 / 人不动）
  9. 纸面可读文字：AI 生成中文必错的位置
 10. 括号与格式：全角/半角混用、字段标签缺失

退出码：0 = 无致命问题；1 = 存在致命问题（空段/粘连/编号断裂）
"""

import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, find_titles, split_shots, parse_segments,
    check_timecodes, has_cross_ref, cross_ref_hits,
    has_readable_text, has_motion, field, norm_time, STILLNESS_MARKERS,
)

# 分镜稿常见栏目（用于字段缺失检查；缺失只作提示，不算致命）
FIELD_LABELS = [
    "风格", "人物与声音", "场景", "机位", "【起始状态】", "【结束状态】",
    "【剧情作用】", "【衔接】", "【卡点】",
]
# 不同版本可能用这些变体
FIELD_ALIASES = {
    "人物与声音": ["人物与声音：", "人物：", "人物与声音", "人物"],
    "场景": ["场景：", "场景"],
    "机位": ["机位：", "机位", "【机位】"],
    "风格": ["风格：", "风格"],
    "【起始状态】": ["【起始状态】", "【本镜起始状态】", "起始状态"],
    "【结束状态】": ["【结束状态】", "【本镜结束状态】", "结束状态"],
    "【剧情作用】": ["【剧情作用】", "【本镜剧情作用", "【剧情作用（"],
    "【衔接】": ["【衔接】", "【本镜衔接】", "衔接"],
    "【卡点】": ["【卡点】", "【付费卡点】", "卡点"],
}


def has_label(block, label):
    for l in block:
        t = l.strip()
        if t.startswith(label):
            return True
        for alias in FIELD_ALIASES.get(label, []):
            if t.startswith(alias):
                return True
    return False


def audit(path):
    lines, had_bom, newline = read_lines(path)
    rep = {
        "file": path,
        "total_lines": len(lines),
        "bom": had_bom,
        "newline": "CRLF" if newline == "\r\n" else "LF",
        "shots": 0,
        "errors": [],
        "warnings": [],
        "info": {},
    }
    titles = find_titles(lines)
    if not titles:
        rep["errors"].append("未找到任何分镜标题行（形如 {分镜001｜30秒}）")
        return rep

    nums = [n for _, n, _ in titles]
    rep["shots"] = len(titles)
    if nums != list(range(1, len(nums) + 1)):
        rep["errors"].append("分镜编号不连续: %s" % nums)
    # 标题行里可能混进了正文（导入工具按花括号计数 → 误判多镜）
    brace_lines = [l for l in lines if "{分镜" in l]
    extra = [l for l in brace_lines
             if not re.match(r"^\{?\s*分镜\s*\d{1,3}\s*\}?\s*[｜|]?\s*\d{1,3}\s*秒?\s*\}?\s*$", l.strip())]
    if extra:
        rep["warnings"].append("有 %d 行含 {分镜 但不是标题行，导入工具可能误判为额外分镜：" % len(extra))
        for e in extra[:3]:
            rep["warnings"].append("    " + e.strip()[:80])

    shots = split_shots(lines)

    seg_total = 0
    empty_segs, glued, tc_issues = [], [], []
    dup_segs, no_motion, readable, crossrefs = [], [], [], []
    tag_counter, scene_counter, stage_counter = Counter(), Counter(), Counter()
    meta_prefix = 0
    dup_tags_in_shot, fields_missing, seg_len_stats = [], [], []

    for num, sec, block in shots:
        body = block[1:]
        segs, extra_lines = parse_segments(body)
        seg_total += len(segs)

        # 2. 时间码
        for iss in check_timecodes(segs, sec if sec else None):
            tc_issues.append("分镜%03d: %s" % (num, iss))

        # 3. 空运镜段
        for s in segs:
            if not s["body"].strip():
                empty_segs.append("分镜%03d 运镜%d [%s]" % (num, s["no"], s["tag"]))

        # 4. 粘连：extra_lines 里含"运镜N["说明正文被并进了台词行
        for l in extra_lines:
            if re.search(r"运镜\d+\[", l):
                glued.append("分镜%03d: %s" % (num, l[:70]))

        # 6. 标签重复
        tags = [s["tag"] for s in segs if s["tag"]]
        for t, c in Counter(tags).items():
            if c > 1:
                dup_tags_in_shot.append("分镜%03d: %s ×%d" % (num, t, c))
        tag_counter.update(tags)

        # 7. 元标注复读
        for s in segs:
            if re.search(r"可见\s*\d+\s*人", s["body"]):
                meta_prefix += 1

        # 8. 主体位移（刻意静止镜不算缺陷）
        for s in segs:
            b = s["body"]
            if not b:
                continue
            if re.search(r"(特写|空镜|静物|无人|字幕|黑场)", b):
                continue
            if any(m in b for m in STILLNESS_MARKERS):
                continue
            if not has_motion(b):
                no_motion.append("分镜%03d 运镜%d: %s" % (num, s["no"], b[:50]))

        # 9. 纸面可读文字（只在画面描述行里找，不看汇总栏/表演提示/生成约束）
        for l in body:
            ls = l.strip()
            if ls.startswith(("【台词与声音】", "【表演提示", "【生成约束】", "【本镜", "【参考图", "【道具】", "【道具状态】")):
                continue
            if ls.startswith("（") and ls.endswith("）"):
                continue
            if has_readable_text(ls):
                readable.append("分镜%03d: %s" % (num, ls[:70]))

        # 5. 跨镜引用
        for l in body:
            hits = cross_ref_hits(l)
            if hits:
                crossrefs.append("分镜%03d [%s]: %s" % (num, ",".join(hits), l.strip()[:70]))

        # 场景栏重复（按地点名归一，忽略 "／" 分隔的前后两段与时间描写）
        sc = field(body, "场景：") or field(body, "场景")
        if sc:
            for part in re.split(r"／|／|／", sc):
                place = re.split(r"[，,；;。]", part.strip())[0].strip()
                if place:
                    scene_counter[place] += 1

        # 表演说明句式
        for l in body:
            s = l.strip()
            if s.startswith("（") and s.endswith("）"):
                stage_counter[s[:16]] += 1

        # 字段缺失（只统计"部分镜头有、部分镜头没有"的字段；整稿统一没有则不算缺陷）
        for lb in FIELD_LABELS:
            if not has_label(body, lb):
                fields_missing.append("分镜%03d 缺 %s" % (num, lb))

        # 段长统计
        for s in segs:
            seg_len_stats.append(norm_time(s["t2"]) - norm_time(s["t1"]))

    # 只保留"该字段在全稿并非一致缺失"的项
    present_in = Counter()
    for num, sec, block in shots:
        body = block[1:]
        for lb in FIELD_LABELS:
            if has_label(body, lb):
                present_in[lb] += 1
    fields_missing = [f for f in fields_missing
                      if present_in[f.split(" 缺 ")[1]] > 0]

    # 台词重复：只统计正文里的台词行（排除【台词与声音】汇总栏）
    dlg_all = []
    for num, sec, block in shots:
        for l in block:
            ls = l.strip()
            if ls.startswith("【台词与声音】"):
                continue
            if ls.startswith("（") and ls.endswith("）"):
                continue
            if not re.match(r"^[^（(]{1,10}[（(][^）)]{0,20}[）)]\s*[：:]", ls):
                continue  # 不是"角色（提示）：台词"格式
            for m in re.findall(r"[“\"「『]([^”\"」』]{2,})[”\"」』]", ls):
                dlg_all.append(m.strip())
    dlg_dup = [(k, v) for k, v in Counter(dlg_all).items() if v > 1]

    rep["info"] = {
        "segments": seg_total,
        "dialogue_lines": len(dlg_all),
        "avg_seg_seconds": round(sum(seg_len_stats) / len(seg_len_stats), 2) if seg_len_stats else 0,
        "min_seg_seconds": min(seg_len_stats) if seg_len_stats else 0,
        "max_seg_seconds": max(seg_len_stats) if seg_len_stats else 0,
        "meta_prefix_segments": meta_prefix,
        "scene_repeat_top": [(k, v) for k, v in scene_counter.most_common(5)],
        "stage_pattern_top": [(k, v) for k, v in stage_counter.most_common(5)],
        "dialogue_dup_count": len(dlg_dup),
        "dialogue_dup_top": dlg_dup[:5],
        "dup_tags_in_shot": dup_tags_in_shot[:10],
        "fields_missing": fields_missing[:20],
        "readable_text_count": len(readable),
        "cross_ref_count": len(crossrefs),
        "no_motion_count": len(no_motion),
        "empty_segment_count": len(empty_segs),
        "glued_count": len(glued),
        "timecode_issue_count": len(tc_issues),
        "readable_examples": readable[:12],
        "cross_ref_examples": crossrefs[:12],
        "no_motion_examples": no_motion[:12],
    }

    if empty_segs:
        rep["errors"].append("空运镜段 %d 处（AI 会自由发挥）：%s" % (len(empty_segs), empty_segs[:5]))
    if glued:
        rep["errors"].append("运镜正文粘在台词行尾 %d 处（会被当台词念出来）：%s" % (len(glued), glued[:5]))
    if tc_issues:
        rep["errors"].append("时间码问题 %d 处：%s" % (len(tc_issues), tc_issues[:8]))
    if dup_tags_in_shot:
        rep["warnings"].append("同镜内运镜标签重复：%s" % dup_tags_in_shot[:5])
    if dlg_dup:
        rep["warnings"].append("台词内容重复 %d 组：%s" % (len(dlg_dup), dlg_dup[:3]))
    if crossrefs:
        rep["warnings"].append("跨镜引用 %d 处（无状态 API 无法理解，建议改为绝对表述）：%s" % (len(crossrefs), crossrefs[:5]))
    if readable:
        rep["warnings"].append("纸面可读文字 %d 处（AI 生成中文必错，建议模糊化或改为后期合成）：%s" % (len(readable), readable[:5]))
    if no_motion:
        rep["warnings"].append("缺可见人物位移的段 %d 处（易生成'只转镜头、人不动'）：%s" % (len(no_motion), no_motion[:5]))
    if meta_prefix > seg_total * 0.5:
        rep["warnings"].append("段首元标注（如'可见人物N人'）覆盖 %d/%d 段 —— 同一句式反复，模型易照抄调度，建议抽出为每镜一行" % (meta_prefix, seg_total))
    return rep


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    path = args[0]
    if not os.path.exists(path):
        print("文件不存在: %s" % path)
        sys.exit(2)
    rep = audit(path)
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("=" * 68)
        print("分镜稿体检报告: %s" % rep["file"])
        print("=" * 68)
        print("分镜数: %d | 总行: %d | 编码: UTF-8%s %s" % (
            rep["shots"], rep["total_lines"], " BOM" if rep["bom"] else "", rep["newline"]))
        i = rep["info"]
        print("运镜段: %d | 平均段长: %.1fs (最短 %ss / 最长 %ss)" % (
            i["segments"], i["avg_seg_seconds"], i["min_seg_seconds"], i["max_seg_seconds"]))
        print("台词条数: %d | 重复组: %d" % (i["dialogue_lines"], i["dialogue_dup_count"]))
        print("-" * 68)
        if rep["errors"]:
            print("\n【致命问题】")
            for e in rep["errors"]:
                print("  X " + str(e))
        if rep["warnings"]:
            print("\n【建议处理】")
            for w in rep["warnings"]:
                print("  ! " + str(w))
        if i.get("fields_missing"):
            print("\n【字段缺失】")
            for f in i["fields_missing"]:
                print("  - " + f)
        if i.get("dup_tags_in_shot"):
            print("\n【同镜标签重复】")
            for t in i["dup_tags_in_shot"]:
                print("  - " + t)
        if i.get("scene_repeat_top"):
            print("\n【高频重复场景】")
            for k, v in i["scene_repeat_top"]:
                print("  - %s x%d" % (k, v))
        print("\n" + "=" * 68)
    sys.exit(1 if rep["errors"] else 0)


if __name__ == "__main__":
    main()
