#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_feed_prompts.py —— 把分镜稿编译成"每镜自足"的可投喂文本。

用法：
    python3 build_feed_prompts.py <分镜.txt> [-o 输出.txt] [--mode append|separate]

模式：
    append    （默认）在原分镜稿的每镜开头插入投喂栏位，输出一份新文件
    separate  另存为独立的投喂稿，原稿不动

核心原则（决定了无状态 API 的成败）：
  1. 每镜自足 —— 只写本镜看得见的内容，禁止出现"上一镜/第NNN镜/承接"
  2. 绝对表述 —— 地点、时段、人物、道具状态全部写绝对值，不写"同上/沿用"
  3. 外观放权、状态锁死 —— 道具外观交给 AI，锁的只是开合/持有者/位置
  4. 纸面文字模糊化 —— 关键信息由台词交付，画面只保留"有字迹/被划掉"的关系
  5. 段内自足 —— 每段带主体位移与镜头跟随，避免"只转镜头、人不动"
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, write_lines, backup, find_titles, split_shots,
    parse_segments, field, has_cross_ref, cross_ref_hits,
)

# ------------------------------------------------------------------ 常量

GLOBAL_RULES = [
    "禁止生成字幕、自动配文及额外文字叠加，禁止生成背景音乐与配乐。",
    "出现有生命的人形角色时，根据画风和镜头距离，适度呈现自然呼吸、眨眼、眼神变化、重心调整及细微动作，幅度克制，避免重复和刻意表演；特殊角色及夸张动作以分镜设定为准。",
    "镜头运动流畅、有明确目的，切镜保持空间关系、动作及视线连贯。除非分镜明确要求，避免无故抖动、突然变焦、闪烁及焦点跳动。不额外添加分镜未要求的人物、物件或剧情。画幅为竖屏9:16。",
]

PER_SHOT_RULES = [
    "本镜各段之间的构图、景别与动作必须互不相同；本镜内人物的造型、服装与陈设保持一致。",
    "每段必须出现可见的人物位移或取放动作（走动、转身、起立、前倾、后退、递交、开合）；纯静物画面除外。",
    "镜头必须跟随该位移运动：主体走，镜头跟；主体停，镜头才停；不做无动机的推拉摇移。",
    "不出现【人物】名单以外的人物与物件；手部不穿模、不多指、不形变。",
    "纸面文字一律不要求清晰可辨，只保留「有字迹／被划掉／被压住」的关系；需要逐字正确的字句一律为后期合成，画面内不生成任何文字。",
    "道具外观由 AI 自由发挥，但位置、数量、开合状态与持有者必须与【道具状态】一致。",
]

# 道具状态词典：道具名 → 状态词（只锁不变量）
STATE_RULES = [
    ("盖子合上", ["盖子合上", "把盖盖上", "合上盖", "盖上", "合盖"]),
    ("盖子敞开", ["盖子打开", "盖子敞开", "敞着", "开着"]),
    ("空盒", ["空盒", "空的"]),
    ("内有物", ["装着", "里面有"]),
    ("收在外套内袋", ["内袋"]),
    ("置于茶几", ["茶几"]),
    ("放在金属托盘", ["托盘"]),
    ("握在手里", ["手里", "拿着", "捏着", "捏住", "攥着"]),
    ("贴在右手", ["贴在右手", "贴上电极", "电极片"]),
    ("绑在右手床栏", ["束带", "绑在", "扣上"]),
    ("空", ["空的", "空着"]),
    ("完整", ["完整的", "完好"]),
    ("破旧", ["破旧", "残破", "发黄", "开裂"]),
]

PROP_KWS = [
    "红色金属盒", "红盒", "红木匣", "压制贴", "档案袋", "档案盒", "随访单",
    "磁带", "卡带", "信封", "录音机", "吊坠", "残铁", "金属圆片", "圆片",
    "饼干盒", "文件夹", "托盘", "登记表", "登记本", "纸牌", "留言卡",
]


def prop_state(text):
    """从一段文本里抽取道具的绝对状态描述。"""
    out = []
    for sent in re.split(r"[。；;]", text):
        for kw in PROP_KWS:
            if kw in sent:
                for label, kws in STATE_RULES:
                    if any(k in sent for k in kws):
                        out.append((kw, label))
                        break
    # 去重保序
    seen, res = set(), []
    for k, v in out:
        if k not in seen:
            seen.add(k)
            res.append((k, v))
    return res


def build_shot_block(num, sec, block, opts):
    """生成一镜的完整投喂文本（不含原正文，供 append 模式插入）。

    若原块里已存在同类栏位，则复用而非重复生成。
    """
    body = block[1:]
    out = []

    def already(tag):
        return any(l.strip().startswith(tag) for l in body)

    # ---- 提取各栏 ----
    style = field(body, "风格：") or field(body, "风格")
    chars_raw = field(body, "人物与声音：") or field(body, "人物：") or field(body, "人物")
    scene_raw = field(body, "场景：") or field(body, "场景")
    cam = field(body, "机位：") or field(body, "机位")
    start = field(body, "【起始状态】")
    end = field(body, "【结束状态】")
    light = field(body, "【光线】")

    # 人物名单（只取名单段，"人物声音："之后是描述不含名单）
    def names_of(text):
        cut = text.split("人物声音：")[0]
        found = []
        for m in re.finditer(r"([一-龥A-Za-z·]{2,10})[（(]([^）)]{0,40})[）)]", cut):
            nm, note = m.group(1), m.group(2)
            if any(k in note for k in ("电话", "画外", "录音", "门外", "门内", "不入镜",
                                       "仅声音", "可闻", "对讲", "声音来自", "不露面")):
                continue
            if nm not in found:
                found.append(nm)
        if not found:
            # 纯列表格式："小雨、客人" / "小雨，客人" / "小雨和客人"
            clean = re.split(r"[（(]", cut)[0]
            clean = re.sub(r"^(人物|角色|出场人物)\s*[：:]\s*", "", clean).strip()
            clean = re.sub(r"[,，、]$", "", clean)
            for part in re.split(r"[、，,]|和(?=[一-龥])|与(?=[一-龥])", clean):
                nm = part.strip().strip("。．.；;")
                if not nm or len(nm) > 8:
                    continue
                if re.match(r"^[本下上半同随该其这那每在只不无仅][一-龥]{0,4}$", nm):
                    continue
                if nm not in found:
                    found.append(nm)
        return found

    # 人物名单：优先复用已有的【人物】栏；其次取"人物与声音"的名单段
    existing = field(body, "【人物】")
    if existing:
        up_names = []
        low_names = []
        mup = re.search(r"上半[:：]\s*([^｜|]+)", existing)
        mlow = re.search(r"下半[:：]\s*([^｜|]+)", existing)
        if mup:
            up_names = [x.strip() for x in re.split(r"[、，,]", mup.group(1)) if x.strip()]
        if mlow:
            low_names = [x.strip() for x in re.split(r"[、，,]", mlow.group(1)) if x.strip()]
        if not up_names and not low_names:
            clean = re.sub(r"^(上半|下半)\s*[:：]\s*", "", existing)
            clean = re.sub(r"｜.*$", "", clean)
            clean = re.sub(r"\|.*$", "", clean)
            up_names = [x.strip() for x in re.split(r"[、，,]", clean) if x.strip()]
        half_split = bool(mup and mlow)
    else:
        half_split = "〔下半镜" in chars_raw
        if half_split:
            cu, cl = chars_raw.split("〔下半镜", 1)
            cl = re.sub(r"^[^：:]*[：:]\s*", "", cl)
        else:
            cu, cl = chars_raw, ""
        up_names, low_names = names_of(cu), names_of(cl)

    visible = up_names or low_names
    all_names = list(dict.fromkeys(up_names + low_names))

    # 场景（可能有上下半两段，用 ／ 分隔）
    scene_parts = [s.strip() for s in re.split(r"／|／", scene_raw) if s.strip()]
    scene_line = " ／ ".join(scene_parts)

    # 时段
    tm = re.findall(
        r"(?:次日)?(?:凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|入夜|晚上|夜里|深夜|天亮|当晚)"
        r"[，,]?[\u4e00-\u9fa5\d]{0,3}(?:点|时)[\u4e00-\u9fa5\d]{0,4}分?", scene_raw)
    if not tm:
        tm = re.findall(r"(凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|入夜|晚上|夜里|深夜|天亮|当晚|雨夜)", scene_raw)

    # ---- 组装各栏（已存在的栏位直接跳过，不重复生成）----
    if not already("【人物】"):
        out.append("【人物】" + ("上半：" + "、".join(up_names) + " ｜ 下半：" + "、".join(low_names)
                                  if half_split and up_names and low_names
                                  else "、".join(all_names)))
    if not already("【场景】"):
        out.append("【场景】" + scene_line + (" ｜ 时段：" + "→".join(tm[:2]) if tm else ""))
    if light and not already("【光线】"):
        out.append("【光线】" + light)

    # 道具状态：优先复用原文件已有的【道具状态】栏；否则从起始/结束状态里抽
    existing_prop = field(body, "【道具状态】")
    if existing_prop:
        if not already("【道具状态】"):
            out.append("【道具状态】" + existing_prop)
    else:
        prop_src = " ".join([start, end] + [l for l in body if l.startswith("运镜")])
        ps = prop_state(prop_src)
        out.append("【道具状态】" + ("；".join("%s（%s）" % (k, v) for k, v in ps)
                                     if ps else "无关键道具"))

    if not already("【本镜可见人物】"):
        out.append("【本镜可见人物】" + ("、".join(all_names) if all_names else "无")
                   + "（各段不再重复标注）")

    return out


def collect_dialogue(block):
    """从正文抽出台词（用于音轨输入），并分离舞台说明。"""
    dlg, stage = [], []
    in_body = False
    for l in block:
        ls = l.strip()
        if ls.startswith("分时段正文：") or ls.startswith("台词与画面描述："):
            in_body = True
            continue
        if ls.startswith("【结束状态】") or ls.startswith("【台词与声音】"):
            in_body = False
        if not ls or ls.startswith("运镜"):
            continue
        if not in_body:
            continue
        if ls.startswith("（") and ls.endswith("）"):
            stage.append(ls)
        else:
            dlg.append(ls)
    return dlg, stage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--mode", choices=["append", "separate"], default="append")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("文件不存在: %s" % args.src)
        sys.exit(2)

    lines, had_bom, newline = read_lines(args.src)
    titles = find_titles(lines)
    if not titles:
        print("未找到分镜标题行")
        sys.exit(2)

    shots = split_shots(lines)
    out = []

    # 追加模式：保留原标题与正文，在镜号后插入投喂栏
    if args.mode == "append":
        out = list(lines)
        insert_at = []
        for num, sec, block in shots:
            ins = build_shot_block(num, sec, block, {})
            # 投喂栏插到标题之后
            insert_at.append((num, ins))
        # 从后往前插，避免索引错位
        for num, ins in reversed(insert_at):
            ti = next(i for i, n, s in titles if n == num)
            out[ti + 1:ti + 1] = ins
        result = out
    else:
        # 独立模式：全局说明保留多行在前；每镜输出为单行块（分镜内不换行），分镜间空一行
        result = []
        if titles:
            first_idx = titles[0][2]
            gpart = lines[:first_idx]
            while gpart and not gpart[-1].strip():
                gpart.pop()
            result.extend(gpart)
            result.append("")
        for num, sec, block in shots:
            parts = ["{分镜%03d｜%s秒}" % (num, sec if sec else "?")]
            parts += build_shot_block(num, sec, block, {})
            parts += [l for l in block[1:] if l.strip()]
            line = " ".join(p.strip() for p in parts if p.strip())
            result.append(line)
            result.append("")  # 分镜与分镜之间空一行

    dst = args.output or (args.src if args.mode == "append" else
                           os.path.splitext(args.src)[0] + "_投喂版.txt")
    if not args.no_backup and os.path.abspath(dst) == os.path.abspath(args.src):
        backup(args.src, tag="投喂栏插入前备份")
    write_lines(dst, result, had_bom, newline)
    print("已生成: %s" % dst)
    print("分镜数: %d" % len(shots))


if __name__ == "__main__":
    main()
