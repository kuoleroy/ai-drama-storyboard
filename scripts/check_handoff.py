#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_handoff.py —— 剧本 → 分镜 交接前的锁稿核验器。

用法：
    python3 check_handoff.py <分镜稿或剧本稿.txt> [--shots 130] [--sec 15]
    python3 check_handoff.py <稿子.txt> --shots 65 --sec 30

检查项（对应 handoff_spec.md 的锁稿核验清单）：
  1. 台词密度：能不能以正常表演支撑指定时长（4.5 字/秒基准）
  2. 人物锁定：每个角色是否有身份/关系说明，出场是否可追溯
  3. 场景清单：场景是否有绝对地点名，能否归一
  4. 道具链：关键道具是否有持有人与状态声明
  5. 伏笔标记：是否有埋点/回收的可检索标记
  6. 跨镜引用：交接后是否仍残留"上一镜/第NNN镜"式指代
  7. 空镜占比：是否用无效空镜/慢说来凑时长

退出码：0 = 通过；1 = 存在阻塞项（时长撑不住 / 大量跨镜引用）
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, find_titles, split_shots, parse_segments, field,
    has_cross_ref, cross_ref_hits, norm_time,
)

# ---- 参数 ----
SPEECH_RATE = 4.5          # 中文正常语速（字/秒）
BREATH = 1.5               # 每镜留给呼吸/反应的秒数
DENSE_WARN = 1.2           # 超过基准 1.2 倍算偏挤
DENSE_BLOCK = 2.0          # 超过基准 2.0 倍算撑不住
SPARSE_WARN = 0.4          # 低于基准 0.4 倍算空镜过多

IDENT_PATTERNS = [
    r"（[^）]{0,30}(?:男|女|岁|身份|医生|师|长|总|经理|警察|学生|记者|律师|工|员|师|娘|爹|父|母|子|女|叔|姨|舅|哥|姐|弟|妹)[^）]{0,20}）",
    r"\d+\s*岁",
]

FOCUS_WORDS = ["埋伏", "伏笔", "回收", "呼应", "闭环", "暗线", "铺垫", "回收点"]
PROP_WORDS = ["红色金属盒", "红木匣", "压制贴", "档案袋", "档案盒", "随访单",
              "磁带", "卡带", "信封", "录音机", "吊坠", "残铁", "金属圆片",
              "饼干盒", "文件夹", "托盘", "登记本", "留言卡", "纸牌", "伞桶",
              "登记表", "病历", "手机", "钥匙"]


DLG_LINE_RE = re.compile(r"^[^（(【\s][^（(]{0,14}[（(][^）)]{0,26}[）)]\s*[：:]\s*[“\"「『]")


def extract_dialogue(lines):
    """提取真正的角色台词（排除汇总栏、舞台说明、运镜正文里的引号内容）。

    只认"角色（提示）：台词"这种独立成行的格式 —— 运镜正文里引号包住的
    往往是画面描述或文件内容，不是角色说的话。
    """
    out = []
    for l in lines:
        ls = l.strip()
        if ls.startswith(("【台词与声音】", "【表演提示", "【生成约束】")):
            continue
        if ls.startswith("（") and ls.endswith("）"):
            continue
        if not DLG_LINE_RE.match(ls):
            continue
        for m in re.findall(r"[“\"「『]([^”\"」』]{1,})[”\"」』]", ls):
            out.append(m.strip())
    return out


def dialogue_segments(shots):
    """找出含角色台词的运镜段，返回 (有台词段数, 纯画面段数, 台词字数, 有台词段总时长)。

    密度必须按"有台词的段"算 —— 用整镜时长做分母会把大量纯画面段算成空白，
    从而误判成"空镜过多"。

    台词在稿子里是**独立成行**、排在运镜行之间的（如
        运镜2[00:03-00:06|袖口特写]：……
        画外·母亲（气声，几乎听不清）：“别……别开那只红色的……”
    ），所以按行序扫描，遇到台词行就归属到"最近一次出现的运镜段"。
    """
    DLG_LINE = re.compile(r"^[^（(【\s][^（(]{0,14}[（(][^）)]{0,26}[）)]\s*[：:]\s*[“\"「『]")
    with_dlg = pure = 0
    chars = 0
    dur = 0
    for num, sec, block in shots:
        segs, _ = parse_segments(block[1:])
        # 行序扫描：记录每条台词归属哪个运镜段
        seg_dlg = defaultdict(list)
        cur = -1
        for l in block[1:]:
            ls = l.strip()
            m = re.match(r"^运镜(\d+)\[", ls)
            if m:
                cur = int(m.group(1)) - 1
                continue
            if not ls or cur < 0:
                continue
            if ls.startswith(("【", "风格", "人物", "场景", "机位",
                              "分时段正文", "台词与画面描述", "〔下半镜")):
                continue
            if DLG_LINE.match(ls):
                seg_dlg[cur] += re.findall(r"[“\"「『]([^”\"」』]{1,})[”\"」』]", ls)
        for si, s in enumerate(segs):
            d = norm_time(s["t2"]) - norm_time(s["t1"])
            found = seg_dlg.get(si, [])
            if found:
                with_dlg += 1
                chars += sum(len(x.strip()) for x in found)
                dur += d
            else:
                pure += 1
    return with_dlg, pure, chars, dur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--shots", type=int, default=None, help="目标总镜数")
    ap.add_argument("--sec", type=int, default=None, help="单镜时长（秒）")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("文件不存在: %s" % args.src)
        return 2

    lines, _, _ = read_lines(args.src)
    titles = find_titles(lines)
    shots = split_shots(lines)
    total_lines = [l for l in lines if l.strip()]

    # ---- 判定分镜结构 ----
    if shots:
        shot_count = len(shots)
        per_sec = args.sec or (shots[0][1] or 0)
        mode = "分镜稿"
    else:
        shot_count = args.shots or 0
        per_sec = args.sec or 0
        mode = "剧本稿（无分镜标题）"

    total_sec = shot_count * per_sec if (shot_count and per_sec) else 0

    report = {
        "file": args.src,
        "mode": mode,
        "shot_count": shot_count,
        "shot_seconds": per_sec,
        "total_seconds": total_sec,
        "checks": [],
        "blockers": [],
        "warnings": [],
    }

    def add(name, ok, detail, level="warn"):
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            if level == "block":
                report["blockers"].append("%s：%s" % (name, detail))
            else:
                report["warnings"].append("%s：%s" % (name, detail))

    # ========== 1. 台词密度 ==========
    dlg = extract_dialogue(total_lines)
    dlg_chars = sum(len(d.strip()) for d in dlg)
    long_lines = [(d, len(d.strip())) for d in dlg if len(d.strip()) > 40]
    if shots:
        with_dlg, pure, seg_chars, seg_dur = dialogue_segments(shots)
    else:
        with_dlg, pure, seg_chars, seg_dur = 0, 0, dlg_chars, 0

    if seg_dur:
        density = seg_chars / seg_dur
        ratio = density / SPEECH_RATE
        detail = ("台词 %d 字 / 有台词段 %d 段共 %d 秒 = %.2f 字/秒（基准 %.1f，比值 %.2f×）"
                  % (seg_chars, with_dlg, seg_dur, density, SPEECH_RATE, ratio))
        detail += "｜纯画面段 %d 个" % pure
        if ratio > DENSE_BLOCK:
            add("台词密度可支撑性", False,
                detail + " ｜ ❌ 超过基准 %.1f 倍，正常表演撑不住，必须增镜或删剧情"
                % DENSE_BLOCK, "block")
        elif ratio > DENSE_WARN:
            add("台词密度可支撑性", False,
                detail + " ｜ ⚠️ 偏挤，建议删冗余对白或加长镜数", "warn")
        else:
            add("台词密度可支撑性", True, detail + " ｜ ✅ 撑得住")

        # 单句过长：一口气说不完（比总密度更致命）
        if long_lines:
            worst = sorted(long_lines, key=lambda x: -x[1])[:3]
            add("单句台词长度", False,
                "%d 句超过 40 字（正常语速约 9 秒才能念完）：%s ｜ 建议拆成多句或分到多段"
                % (len(long_lines),
                   "；".join("「%s…」( %d字)" % (t[:18], n) for t, n in worst)),
                "block" if max(n for _, n in long_lines) > 60 else "warn")
        else:
            add("单句台词长度", True, "无超过 40 字的单句台词")

        # 整镜口径：信息量是否够填满
        if total_sec and with_dlg:
            fill = seg_dur / total_sec
            if fill >= 0.25:
                add("画面信息密度", True,
                    "有台词画面占总时长 %.0f%%（%d/%d 秒）｜信息量足够撑满时长"
                    % (fill * 100, seg_dur, total_sec))
            else:
                add("画面信息密度", False,
                    "有台词画面仅占总时长 %.0f%%（%d/%d 秒）｜台词段占比过低，"
                    "大量空转，该加内容或压缩镜数" % (fill * 100, seg_dur, total_sec), "warn")
    else:
        add("台词密度可支撑性", False,
            "无法判定：未解析到含台词的运镜段（若这是纯画面剧本可忽略）", "warn")

    # ========== 2. 人物锁定 ==========
    if shots:
        people_counter = Counter()
        no_ident = []
        for num, sec, block in shots:
            body = block[1:]
            names = set()
            # 人物名单来源：优先【人物】栏，其次"人物与声音："栏
            pl = field(body, "【人物】")
            if not pl:
                pl = field(body, "人物与声音：") or field(body, "人物：")
            if pl:
                # 去掉括注里的说明
                clean = re.sub(r"[（(][^）)]{0,40}[）)]", " ", pl)
                clean = re.sub(r"^(上半|下半)\s*[:：]\s*", "", clean)
                clean = re.sub(r"[｜|].*$", "", clean)
                for nm in re.split(r"[、，,]", clean):
                    nm = nm.strip()
                    if nm and 1 < len(nm) <= 8 and not nm.startswith("其他"):
                        names.add(nm)
            full = "\n".join(body)
            for nm in names:
                people_counter[nm] += 1
                # 该角色首次出场时，是否有身份/年龄/关系说明
                has_ident = (
                    any(re.search(p, full) for p in IDENT_PATTERNS)
                    or re.search(re.escape(nm) + r"[（(][^）)]{0,30}[）)]", full)
                )
                if not has_ident:
                    no_ident.append(nm)
        detail = ("出场角色 %d 个：%s" % (len(people_counter),
                                        "、".join(list(people_counter)[:12])))
        if people_counter:
            add("人物锁定", True, detail)
        else:
            add("人物锁定", False, "未解析到任何出场角色名单", "warn")
        if no_ident:
            add("角色身份说明", False,
                "以下角色缺身份/年龄标注，分镜阶段无法固定形象：%s"
                % "、".join(sorted(set(no_ident))[:8]), "warn")
        else:
            add("角色身份说明", True, "所有出场角色均有身份或年龄标注")

        # 配角与画外角色混检
        offscreen = []
        for num, sec, block in shots:
            body = block[1:]
            pl = field(body, "【人物】")
            if "画外" in pl or "仅声音" in pl:
                offscreen.append(num)
        add("画外角色标注", True,
            ("%d 镜标注了画外/仅声音角色（不生成三视图）" % len(offscreen))
            if offscreen else "未发现画外角色标注")

    # ========== 3. 场景清单 ==========
    if shots:
        scenes = []
        for num, sec, block in shots:
            body = block[1:]
            sc = field(body, "【场景】") or field(body, "场景：")
            if not sc:
                continue
            # 去掉"｜ 时段：…"后缀
            sc = re.split(r"[｜|]\s*时段", sc)[0]
            # 拆上半/下半、／分隔
            for half in re.split(r"[｜|]", sc):
                for part in re.split(r"／|／", half):
                    part = re.sub(r"^(上半|下半)\s*[:：]\s*", "", part.strip())
                    place = re.split(r"[，,；;。]", part)[0].strip()
                    if place:
                        scenes.append(place)
        sc_counter = Counter(scenes)
        add("场景清单", bool(scenes),
            "共 %d 个地点条目，去重 %d 个：%s"
            % (len(scenes), len(sc_counter),
               "、".join(list(sc_counter)[:8]) + ("…" if len(sc_counter) > 8 else "")))
        # 是否有绝对地点（不含跨镜指代）
        bad = [s for s in scenes if has_cross_ref(s)]
        add("场景绝对化", not bad,
            "全部为绝对地点名" if not bad
            else "以下场景含跨镜指代：%s" % "、".join(bad[:3]), "block" if bad else "warn")

    # ========== 4. 道具链 ==========
    if shots:
        prop_states = 0
        props_seen = Counter()
        no_state = []
        for num, sec, block in shots:
            body = block[1:]
            full = "\n".join(body)
            has_prop = any(w in full for w in PROP_WORDS)
            st = field(body, "【道具状态】")
            if has_prop:
                for w in PROP_WORDS:
                    if w in full:
                        props_seen[w] += 1
                if st:
                    prop_states += 1
                else:
                    no_state.append(num)
        detail = "涉及 %d 类关键道具" % len(props_seen)
        if props_seen:
            detail += "：%s" % "、".join(list(props_seen)[:8])
        add("关键道具清单", bool(props_seen), detail)
        if no_state:
            add("道具状态声明", False,
                "%d 镜出现关键道具但缺【道具状态】（持有人/开合/位置会跳变）：%s"
                % (len(no_state), "、".join("分镜%03d" % n for n in no_state[:6])), "warn")
        else:
            add("道具状态声明", True, "每镜关键道具均有状态声明")

    # ========== 5. 伏笔标记 ==========
    hits = []
    for l in total_lines:
        for w in FOCUS_WORDS:
            if w in l:
                hits.append((w, l.strip()[:70]))
    if hits:
        add("伏笔标记", True,
            "检出 %d 处伏笔相关标记（埋点/回收/呼应）：%s"
            % (len(hits), "；".join(h[1][:40] for h in hits[:3])))
    else:
        add("伏笔标记", False,
            "未检出「埋伏/回收/呼应/闭环/铺垫」等可检索标记，伏笔回收无法核验", "warn")

    # ========== 6. 跨镜引用 ==========
    # 排除"教人怎么写"的说明行（如"不写'比上一镜更暗'这类相对表述"）
    NOTE_PREFIXES = ("【文件说明】", "【去重规则】", "【光线】绝对描述", "【生成约束】")
    cross = []
    for l in total_lines:
        ls = l.strip()
        if ls.startswith(NOTE_PREFIXES):
            continue
        # 说明性文字里出现"上一镜"但本身是规则陈述 → 跳过
        if re.search(r"(不写|不要写|禁止|避免|不得)[^。；]{0,20}上一镜", ls):
            continue
        hh = cross_ref_hits(l)
        if hh:
            cross.append((hh, ls[:80]))
    add("跨镜引用清零", not cross,
        "全文无跨镜指代" if not cross
        else "仍有 %d 处：%s" % (len(cross), cross[0][1][:70]), "block" if cross else "warn")

    # ========== 7. 空镜/凑时长 ==========
    if shots:
        emptyish = 0
        seg_total = 0
        for num, sec, block in shots:
            body = block[1:]
            segs, _ = parse_segments(body)
            for s in segs:
                seg_total += 1
                b = s.get("body") or ""
                if not b.strip() or len(b.strip()) < 12:
                    emptyish += 1
        ratio = emptyish / seg_total if seg_total else 0
        add("空镜/凑时长检查", ratio < 0.15,
            "%d/%d 段正文过短或为空（%.0f%%）｜阈值 15%%，超了说明在用空镜凑时长"
            % (emptyish, seg_total, ratio * 100))

    # ========== 输出 ==========
    print("=" * 72)
    print("锁稿核验报告: %s" % args.src)
    print("=" * 72)
    print("类型: %s | 镜数: %s | 单镜: %s 秒 | 总时长: %s 秒"
          % (mode, shot_count or "-", per_sec or "-", total_sec or "-"))
    print("-" * 72)
    for c in report["checks"]:
        print("  %s %-16s %s" % ("PASS" if c["ok"] else "WARN", c["name"], c["detail"]))
    print("-" * 72)
    if report["blockers"]:
        print("\n【阻塞项 · 必须处理后才能进入分镜】")
        for b in report["blockers"]:
            print("  X " + b)
    if report["warnings"]:
        print("\n【建议处理】")
        for w in report["warnings"]:
            print("  ! " + w)
    if not report["blockers"] and not report["warnings"]:
        print("\n全部通过 ✓")
    print("=" * 72)
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
