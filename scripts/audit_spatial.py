#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_spatial.py —— 空间方向锚定检测器。

AI 生成视频时最常见的穿帮之一是**内外/进出方向搞反**：
剧本写"客人接过伞撑开走进雨里"，AI 生成成"客人打着伞走进店内"。

根因不是缺少一个参照物，而是三层锚定都没给全：
  ① 空间参照物成对出现 —— 门内要有可见地标（收银台/货架），门外也要有（伞桶/街灯）
  ② 动作动词带方向     —— "推门而出" ✓ / "走出来" ✗（中性动词方向不明）
  ③ 门的朝向声明       —— 门在画面哪一侧、朝哪开、内外谁在镜头前

用法：
    python3 audit_spatial.py <分镜稿.txt> [--scene 便利店] [--fix]

检测项：
  1. 方向敏感镜头清单（含出入、内外、进退、上下、左右）
  2. 中性动词（走出来/走进去/进去/出来）—— 方向不明，需改有向动词
  3. 缺成对参照物 —— 只写"门内"没写门内有什么
  4. 门的朝向未声明 —— 门在哪一侧、朝哪开
  5. 交接方向缺失 —— "递出/递给"没写从哪边到哪边
"""

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, find_titles, split_shots, parse_segments, field,
)

# ---- 方向敏感信号 ----
INOUT_WORDS = ["门内", "门外", "店内", "店外", "屋里", "屋外", "室内", "室外",
               "进店", "离店", "进门", "出门", "推门", "拉门", "玻璃门",
               "跨进", "跨出", "走进", "走出", "迈进", "探身", "门框", "门槛"]
MOVE_WORDS = ["进入", "退出", "进去", "出来", "穿过", "经过", "越过", "走向",
              "走向", "来到", "抵达", "离开", "返回", "折回", "退回"]
# 中性动词：方向不明，AI 会随机选
NEUTRAL_VERBS = ["走出来", "走进去", "进去", "出来", "走出来去", "走出来来",
                 "走出来", "走出门", "走进门"]
# 有向动词：明确
DIRECTED_VERBS = ["推门而出", "推门出去", "拉门而出", "跨出", "迈出", "走出店门",
                  "从店内走出", "从门内走出", "走向街面", "走向店内", "推门进入",
                  "拉门进入", "跨进", "迈进", "退回店内", "转身离开"]

# 空间参照物：内 / 外 各一组
IN_ANCHORS = ["收银台", "货架", "柜台", "地垫", "冰柜", "饮料柜", "店内灯",
              "天花板", "吊牌", "价签", "商品", "门内", "室内"]
OUT_ANCHORS = ["伞桶", "街灯", "台阶", "马路", "路面", "人行道", "雨棚",
               "檐下", "电线杆", "自行车", "树", "花坛", "门外", "街面",
               "路面反光", "积水"]

# 门朝向声明的关键词
ORIENT_WORDS = ["画面左", "画面右", "左侧", "右侧", "左边", "右边", "朝外开",
                "朝内开", "向外拉", "向内推", "推", "拉", "门把手在", "门轴在"]


def shots_of(lines):
    return split_shots(lines)


def analyze(path, only_scene=None):
    lines, _, _ = read_lines(path)
    shots = shots_of(lines)
    findings = {
        "file": path,
        "sensitive": [],      # 方向敏感镜头
        "neutral": [],       # 中性动词
        "no_anchor": [],     # 缺成对参照
        "no_orient": [],     # 门朝向未声明
        "hand_off": [],      # 交接方向缺失
    }
    for num, sec, block in shots:
        body = block[1:]
        segs, _ = parse_segments(body)
        scene = field(body, "【场景】") or field(body, "场景：")
        if only_scene and only_scene not in scene:
            continue
        all_text = "\n".join(body)

        # ---- 1. 方向敏感 ----
        inout_hits = [w for w in INOUT_WORDS if w in all_text]
        if inout_hits:
            findings["sensitive"].append((num, scene, inout_hits))

        # ---- 2. 中性动词 ----
        for s in segs:
            b = s.get("body") or ""
            for v in NEUTRAL_VERBS:
                if v not in b:
                    continue
                # 排除误报："哭出来""说出来""看出来"等不是空间移动
                idx = b.find(v)
                prev = b[max(0, idx - 1)]
                if prev not in "走进出上下" and v in ("出来", "进去"):
                    continue
                if not any(d in b for d in DIRECTED_VERBS):
                    findings["neutral"].append((num, s["no"], v, b[:60]))

        # ---- 3. 成对参照物 ----
        # 只在有"内/外"表述时检查
        has_in = any(w in all_text for w in ["门内", "店内", "室内", "屋里", "内侧"])
        has_out = any(w in all_text for w in ["门外", "店外", "室外", "屋外", "外侧", "街面"])
        if has_in or has_out:
            in_a = [w for w in IN_ANCHORS if w in all_text]
            out_a = [w for w in OUT_ANCHORS if w in all_text]
            if has_in and not in_a:
                findings["no_anchor"].append((num, scene, "有『内』但无内部门标"))
            if has_out and not out_a:
                findings["no_anchor"].append((num, scene, "有『外』但无外部地标"))

        # ---- 4. 门朝向 ----
        if any(w in all_text for w in ["门内", "门外", "店内", "店外", "推门", "拉门", "玻璃门"]):
            if not any(w in all_text for w in ORIENT_WORDS):
                findings["no_orient"].append((num, scene))

        # ---- 5. 交接方向 ----
        for s in segs:
            b = s.get("body") or ""
            for v in ["递出", "递给", "递向", "递过"]:
                if v in b:
                    # 需要出现方向词
                    if not any(w in b for w in ["从", "向", "越过", "穿过", "跨过",
                                                "门内", "门外", "店内", "店外",
                                                "手里", "手中", "面前"]):
                        findings["hand_off"].append((num, s["no"], v, b[:60]))
                    break
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--scene", default=None, help="只看含该关键词的场景")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("文件不存在: %s" % args.src)
        return 2

    f = analyze(args.src, args.scene)
    print("=" * 72)
    print("空间方向锚定检测: %s" % args.src)
    if args.scene:
        print("（只检查含『%s』的场景）" % args.scene)
    print("=" * 72)

    print("\n【1】方向敏感镜头（进出/内外/上下/左右）")
    if f["sensitive"]:
        for num, scene, hits in f["sensitive"]:
            print("  分镜%03d  %-24s %s" % (num, (scene or "")[:24], ",".join(hits)))
        print("  共 %d 镜" % len(f["sensitive"]))
    else:
        print("  无")

    print("\n【2】中性动词（方向不明，AI 会随机选方向）")
    if f["neutral"]:
        for num, seg, v, ctx in f["neutral"]:
            print("  ! 分镜%03d 运镜%d  「%s」" % (num, seg, v))
            print("       %s" % ctx)
        print("  共 %d 处 → 改成有向动词（推门而出 / 跨出 / 迈出 / 走向街面）" % len(f["neutral"]))
    else:
        print("  无")

    print("\n【3】缺成对参照物（只写『门内』没写门内有什么）")
    if f["no_anchor"]:
        for num, scene, why in f["no_anchor"]:
            print("  ! 分镜%03d  %s  [%s]" % (num, (scene or "")[:24], why))
        print("  共 %d 处" % len(f["no_anchor"]))
    else:
        print("  无")

    print("\n【4】门的朝向未声明（AI 不知道门在哪一侧、朝哪开）")
    if f["no_orient"]:
        for num, scene in f["no_orient"]:
            print("  ! 分镜%03d  %s" % (num, (scene or "")[:30]))
        print("  共 %d 处 → 加一句『玻璃门在画面右侧，门框贴『推』字』" % len(f["no_orient"]))
    else:
        print("  无")

    print("\n【5】交接方向缺失（『递出』没写从哪边到哪边）")
    if f["hand_off"]:
        for num, seg, v, ctx in f["hand_off"]:
            print("  ! 分镜%03d 运镜%d  「%s」" % (num, seg, v))
            print("       %s" % ctx)
        print("  共 %d 处" % len(f["hand_off"]))
    else:
        print("  无")

    print("\n" + "=" * 72)
    total = (len(f["neutral"]) + len(f["no_anchor"]) + len(f["no_orient"])
             + len(f["hand_off"]))
    print("合计待修 %d 处 ｜ 方向敏感镜头 %d 个（这些最需要锚定）"
          % (total, len(f["sensitive"])))
    print("=" * 72)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
