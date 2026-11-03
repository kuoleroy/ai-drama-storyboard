#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_for_api.py —— 把分镜稿编译成各平台可直接调用的投喂包。

用法：
    python3 export_for_api.py <分镜稿.txt> --platform <平台> [-o 输出目录]
    python3 export_for_api.py <分镜稿.txt> --platform all -o 输出目录
    python3 export_for_api.py <分镜稿.txt> --platform all --continuity keyframes -o 输出目录
    python3 export_for_api.py <分镜稿.txt> --check          # 只做通用性体检

连续性模式（--continuity）：
    first      默认。同场戏内每段用上一段末帧作首帧，跨场断开；最快，零额外出图
    keyframes  关键帧先行：先批量出每镜 n+1 张关键帧静帧，再逐段用相邻两张作首/末帧；
               中间运动交给模型；仅支持末帧的平台（Seedance 2.0 两张图模式）可用
    off        不做首帧/末帧控制

平台 key：
    kling      可灵 2.6 / o1        5/10s，9:16，参考图≤7
    seedance   Seedance 2.0/2.5     4-15s / 4-30s，9:16，图≤9/≤30
    veo        Veo 3                4/8s，9:16
    runway     Runway Gen-4         5-10s，9:16
    sora       Sora 2               12-20s，9:16
    wan        Wan 2.6              2-10s，9:16，图+视频≤5
    hailuo     海螺                  10s，9:16
    luma       Luma Ray 2           10s，9:16
    generic    通用基线（5/10s 档位，字段名用多数派写法）

产出（每个平台一个目录）：
    manifest.json      该平台的完整调用清单（可直接喂脚本）
    prompts.txt        人读的逐段提示词清单
    segments/          每段一个 txt，方便逐个复制粘贴

设计要点：
  * 一份底稿 → N 个平台的调用包，**不改剧情、不改镜头内容**
  * 段长按目标平台的时长档位重新切分（不够 4 秒的段与相邻段合并）
  * 参数差异（字段名、分辨率写法、参考图字段）由 PLATFORMS 表抹平
  * negative_prompt 与 prompt 内嵌负向约束同时给，覆盖不支持独立字段的平台
"""

import argparse
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shotparse import (  # noqa: E402
    read_lines, write_lines, find_titles, split_shots, parse_segments,
    field, norm_time,
)

# --------------------------------------------------------------- 平台定义

def _px(w, h):
    return "%d*%d" % (w, h)


PLATFORMS = {
    "kling": {
        "name": "可灵 2.6 / o1",
        "durations": [5, 10],
        "default_duration": 10,
        "max_duration": 10,
        "min_duration": 5,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": True,
        "notes": "文生视频/首帧图生视频仅支持 5 或 10 秒；参考图与主体数之和 ≤7",
    },
    "seedance": {
        "name": "Seedance 2.0 / 2.5",
        "durations": list(range(4, 31)),
        "default_duration": 10,
        "max_duration": 30,
        "min_duration": 4,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": True,
        "supports_last_frame": True,
        "notes": "2.0 支持 4-15s，2.5 支持 4-30s；支持 return_last_frame 末帧续写",
    },
    "veo": {
        "name": "Veo 3",
        "durations": [4, 8],
        "default_duration": 8,
        "max_duration": 8,
        "min_duration": 4,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": True,
        "notes": "Vertex AI，仅 4 或 8 秒；原生同步音频",
    },
    "runway": {
        "name": "Runway Gen-4",
        "durations": [5, 10],
        "default_duration": 10,
        "max_duration": 10,
        "min_duration": 5,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": False,
        "notes": "无原生音频，台词需外挂音轨；运镜控制最强",
    },
    "sora": {
        "name": "Sora 2",
        "durations": [10, 15, 20],
        "default_duration": 10,
        "max_duration": 20,
        "min_duration": 10,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": True,
        "notes": "角色记忆与镜头运动 token 表现好",
    },
    "wan": {
        "name": "Wan 2.6",
        "durations": list(range(2, 11)),
        "default_duration": 5,
        "max_duration": 10,
        "min_duration": 2,
        "aspect": "9:16",
        "resolution": "1080*1920",
        "max_refs": 5,
        "audio_native": False,
        "notes": "size 必须写具体像素（如 1080*1920），不能写 9:16；图+视频合计 ≤5",
    },
    "hailuo": {
        "name": "海螺 Hailuo",
        "durations": [5, 10],
        "default_duration": 10,
        "max_duration": 10,
        "min_duration": 5,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": False,
        "notes": "跨镜角色一致性较好",
    },
    "luma": {
        "name": "Luma Ray 2",
        "durations": [5, 10],
        "default_duration": 10,
        "max_duration": 10,
        "min_duration": 5,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": False,
        "notes": "",
    },
    "generic": {
        "name": "通用基线（5/10s 档位）",
        "durations": [5, 10],
        "default_duration": 10,
        "max_duration": 10,
        "min_duration": 5,
        "aspect": "9:16",
        "resolution": "1080p",
        "max_refs": 5,
        "audio_native": True,
        "notes": "字段名用多数派写法，适用于大多数 OpenAI 兼容端点",
    },
}

# 负向约束（给独立字段；同时内嵌到 prompt 末尾）
NEGATIVE = (
    "字幕、文字、水印、LOGO；多余人物；多余道具；手部畸形、多指、穿模；"
    "画面变形、闪烁、鬼影；突然变焦、焦点跳动、无动机抖动；"
    "脸部特征漂移、服装变化、场景陈设突变"
)

# 行业铁律：单条 prompt 别超过 80–100 词，超过会让模型困惑、输出变糊。
# 中文按字符计，约 100–120 字是安全上限（≈80 英文词的语义量）。
CORE_LIMIT = 120


def pick_duration(target, platform):
    """为一段挑选最接近的合法时长档位。"""
    durs = platform["durations"]
    # 先找精确匹配
    if target in durs:
        return target
    # 找最接近且 >= min 的
    cands = [d for d in durs if d >= platform["min_duration"]]
    if not cands:
        cands = durs
    return min(cands, key=lambda d: abs(d - target))


def decompose_duration(total, platform):
    """把一镜的总时长分解成若干个"平台合法时长"的调用。

    目标：在保证每个值都是平台合法档位的前提下，
      1) 总和尽量接近 total（少浪费画面时长）
      2) 调用次数尽量少（少生成、少拼接）

    例：total=15
      可灵 [5,10]      → [10, 5]        精确 15 秒，2 次
      Veo  [4,8]       → [8, 8]         16 秒（多 1 秒），2 次
      Wan  [2..10]     → [10, 5]        精确 15 秒，2 次
      Seedance [4..30] → [15]           精确 15 秒，1 次
    """
    durs = sorted(set(platform["durations"]))
    max_legal = durs[-1]
    if total <= 0:
        return []
    if total in durs:
        return [total]

    best = None  # (overshoot, parts, list)
    max_parts = 8
    for k in range(1, max_parts + 1):
        # 用 DP 求恰好 k 个合法值能达到的、>= total 的最小总和
        reach = {0: []}
        for _ in range(k):
            nxt = {}
            for s, lst in reach.items():
                for d in durs:
                    ns = s + d
                    if ns > total + max_legal:
                        continue
                    if ns not in nxt:
                        nxt[ns] = lst + [d]
            reach = nxt
        cands = [(s, lst) for s, lst in reach.items() if s >= total]
        if cands:
            s, lst = min(cands, key=lambda x: (x[0], len(x[1])))
            over = s - total
            if best is None or (over, len(lst)) < (best[0], best[1]):
                best = (over, len(lst), lst)
            if over == 0:
                break  # 精确命中，不可能有更优
    if best is None:
        return [max_legal]
    return best[2]


def regroup_segments(segs, platform):
    """把原始运镜段按目标平台的时长档位重新切分。

    步骤：
      1. decompose_duration 把本镜总时长分解成若干个合法档位
      2. 按累计时长把原段分配到各组（每组尽量填满自己的档位）

    返回 [{t1, t2, dur, parts:[原段...]}]
    """
    if not segs:
        return []
    durs = [norm_time(s["t2"]) - norm_time(s["t1"]) for s in segs]
    total = sum(durs)
    targets = decompose_duration(total, platform)
    if not targets:
        return []

    # 按累计时长切分原段
    groups = []
    gi = 0
    acc = 0            # 当前组已累计的源时长
    limit = targets[0]  # 当前组的目标时长
    cur = []
    for i, d in enumerate(durs):
        cur.append(i)
        acc += d
        # 本段已越过当前组的目标边界 → 收口，进入下一组
        if gi < len(targets) - 1 and acc >= limit:
            groups.append(cur)
            cur = []
            gi += 1
            limit = targets[gi]
            acc = 0
            # 剩余不足一整段的部分归入新组
            if d > limit:
                # 该段跨越边界：把它放到新组，重新累计
                cur = [i]
                acc = d
    if cur:
        groups.append(cur)
    # 若组数多于目标数（段太碎），合并尾部
    while len(groups) > len(targets):
        groups[-2].extend(groups[-1])
        groups.pop()

    out = []
    t = 0
    for k, g in enumerate(groups):
        legal = targets[k] if k < len(targets) else targets[-1]
        out.append({
            "t1": t,
            "t2": t + legal,
            "dur": legal,
            "parts": [segs[i] for i in g],
        })
        t += legal
    return out


def strip_meta(body):
    """把运镜正文拆成「纯画面描述」与「镜头说明」两部分。

    行业通行做法：prompt 只写画面，镜头术语可留但要精简。
    这里把"镜头…"引导的句子摘出来单独放，避免核心层被镜头术语挤占。
    """
    body = (body or "").strip()
    if not body:
        return "", ""
    # 去掉"可见人物N人（…）"这类元标注
    body = re.sub(r"可见人物\d+人[（(][^）)]*[）)]?[，,]?", "", body)
    sents = [s.strip() for s in re.split(r"[。；;]", body) if s.strip()]
    visual, camera = [], []
    for s in sents:
        # 含镜头运动语词的句子归入镜头层
        if re.search(r"(镜头|运镜|画面|构图|景别|推近|拉远|横移|摇|跟|固定机位|俯拍|特写|近景|中景|全景)", s):
            camera.append(s)
        else:
            visual.append(s)
    return "。".join(visual), "。".join(camera)


def compress(text, limit):
    """在 limit 字以内截断，优先在句号处断，不丢句子主干。"""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", "，"):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[:i + 1]
    return cut


def build_prompt(shot_meta, group, platform):
    """生成该平台的分层 prompt。

    行业通行铁律：单条 prompt 别超过 80–100 词，超过会让模型困惑、输出变糊。
    但逐镜流水线要保连续性，光靠核心层不够 —— 所以分两层：

      prompt_core    画面本体（主体+动作+镜头+光线+台词），控制在 ~100 字以内
      prompt_detail  连续性与约束（空间锚定/道具状态/负面/防重复），按需追加

    prompt_minimal = 只用 core（预算紧、模型对长提示词敏感时用）
    prompt_full    = core + detail（默认，保证连续性）
    """
    # ---------- 核心层 ----------
    core = []
    # 风格：取短版（完整版放 detail）
    style = shot_meta.get("style_line") or ""
    style_short = compress(style.replace("风格：", "").split("。")[0], 34)
    if style_short:
        core.append(style_short + "。")
    # 场景、人物与道具清单
    scene = shot_meta.get("scene_line") or ""
    people = shot_meta.get("people_line") or ""
    props_list = shot_meta.get("props_list_line") or ""
    if people:
        core.append(people)
    if props_list:
        core.append(props_list)
    # 逐段画面描述（画面 + 镜头，精简）
    for s in group["parts"]:
        visual, camera = strip_meta(s.get("body"))
        tag = s.get("tag") or ""
        piece = "；".join(x for x in [visual, camera] if x)
        if piece:
            core.append(("%s：%s" % (tag, piece)) if tag else piece)
    # 光线
    light = shot_meta.get("light_line") or ""
    if light:
        core.append(light)
    # 台词（引号包住 → 触发原生口型同步；只有支持原生音频的平台才带）
    dlg = shot_meta.get("dialogue") or []
    if dlg and platform.get("audio_native"):
        core.append("台词（人物开口，唇形同步，括号内不念）：" +
                    " ".join(dlg))
    prompt_core = "\n".join(x for x in core if x).strip()

    # 核心层超长时：固定项优先保，运镜画面段逐段压缩（地板 35 字），绝不整段丢弃
    if len(prompt_core) > CORE_LIMIT:
        fixed, cam_lines = [], []
        for ln in prompt_core.split("\n"):
            if ln.startswith("风格：") or ln.startswith("人物：") \
               or ln.startswith("道具：") or ln.startswith("光线：") or ln.startswith("台词"):
                fixed.append(ln)
            else:
                cam_lines.append(ln)
        fixed_len = sum(len(x) for x in fixed)
        # 固定项已吃满预算时，把台词移出核心层（唇形仍由平台原生音频保障）
        if fixed_len > CORE_LIMIT - 40:
            fixed = [x for x in fixed if not x.startswith("台词")]
            fixed_len = sum(len(x) for x in fixed)
        budget = max(CORE_LIMIT - fixed_len, 40)
        total_cam = sum(len(x) for x in cam_lines) or 1
        new_cam = []
        for ln in cam_lines:
            if total_cam <= budget:
                new_cam.append(ln)
            else:
                share = max(int(budget * len(ln) / total_cam), 35)
                new_cam.append(compress(ln, share))
        prompt_core = "\n".join(fixed + new_cam).strip()

    # ---------- 详情层 ----------
    detail = []
    anchor = shot_meta.get("anchor_line") or ""
    if anchor:
        detail.append("空间锚定：" + anchor)
    props = shot_meta.get("prop_line") or ""
    if props:
        detail.append("道具状态：" + props)
    if style:
        detail.append("画风细则：" + style)
    detail.append("要求：镜头始终跟随主体运动，主体停下时镜头才停；"
                  "每段构图与景别互不相同；"
                  "不出现上述人物以外的人物与物件；画面内不生成任何文字。")
    # 分镜专属生成约束（如"运镜2客人手中只能有购物袋、严禁出现伞"等分段否定）必须带进每段
    gen_rule = shot_meta.get("gen_rule_line") or ""
    if gen_rule:
        detail.append("本镜生成约束：" + gen_rule)
    detail.append("避免：" + NEGATIVE)
    prompt_detail = "\n".join(detail)

    return prompt_core, prompt_detail


def shot_meta(block, global_style=None):
    """抽取一镜的元信息（用于拼 prompt）。"""
    body = block[1:]
    people = field(body, "【人物】")
    scene = field(body, "【场景】")
    props_list = field(body, "【道具】")
    style = field(body, "风格：") or field(body, "风格")
    light = field(body, "【光线】")

    # 场景行：优先用带描述的"场景："正文栏；【场景】栏作为地点名补充
    scene_desc = field(body, "场景：")
    if scene_desc:
        scene_line = "场景：" + scene_desc
    elif scene:
        scene_line = "场景：" + scene
    else:
        scene_line = ""

    # 风格行：把"见【全局风格】"内联成真正的风格描述
    if style and ("见【全局风格】" in style or "见全局风格" in style):
        style = global_style or ""
    style_line = ("风格：" + style) if style else ""
    light_line = ("光线：" + light) if light else ""
    # 空间锚定与道具状态（连续性层的关键信息）
    anchor_line = field(body, "【空间锚定】")
    prop_line = field(body, "【道具状态】")
    # 分镜专属生成约束（含"严禁出现伞"等分段否定），必须带到每段 detail 层
    gen_rule_line = field(body, "【生成约束】")

    # 台词（正文里的"角色（提示）：台词"格式）
    dlg = []
    for l in body:
        ls = l.strip()
        if ls.startswith("【台词与声音】"):
            dlg += [x.strip() for x in ls[len("【台词与声音】"):].split(" ｜ ")]
        elif re.match(r"^[^（(]{1,10}[（(][^）)]{0,20}[）)]\s*[：:]", ls):
            dlg.append(ls)
    # 去重保序
    seen, dd = set(), []
    for x in dlg:
        if x not in seen:
            seen.add(x)
            dd.append(x)

    # 参考图：角色三视图 + 关键道具图 + 场景图
    # 超平台上限时按此优先级截断：角色 > 关键道具 > 场景
    refs = field(body, "【参考图】") + " " + field(body, "【参考图·角色】") + " " \
        + field(body, "【参考图·场景】") + " " + field(body, "【参考图·道具】")
    char_refs, prop_refs, scene_refs = [], [], []
    for m in re.findall(r"[一-龥A-Za-z0-9_\-·]{2,24}\.(?:png|jpg|jpeg|webp|bmp|gif)", refs):
        if "场景" in m or "场景图" in m:
            if m not in scene_refs:
                scene_refs.append(m)
        elif "道具" in m or "道具图" in m:
            if m not in prop_refs:
                prop_refs.append(m)
        elif m not in char_refs:
            char_refs.append(m)
    if not char_refs:
        for m in re.findall(r"[一-龥A-Za-z0-9_\-·]{1,20}_三视图", refs):
            if m not in char_refs:
                char_refs.append(m)
    if not prop_refs:
        for m in re.findall(r"[一-龥A-Za-z0-9_\-·]{1,20}_道具图", refs):
            if m not in prop_refs:
                prop_refs.append(m)
    if not scene_refs:
        for m in re.findall(r"[一-龥A-Za-z0-9_\-·]{1,20}_场景图", refs):
            if m not in scene_refs:
                scene_refs.append(m)
    ref_list = char_refs + prop_refs + scene_refs

    return {
        "people_line": ("人物：" + people) if people else "",
        "props_list_line": ("道具：" + props_list) if props_list else "",
        "scene_line": scene_line,
        "style_line": style_line,
        "light_line": light_line,
        "anchor_line": anchor_line,
        "prop_line": prop_line,
        "gen_rule_line": gen_rule_line,
        "dialogue": dd,
        "refs": ref_list,
        "char_refs": char_refs,
        "prop_refs": prop_refs,
        "scene_refs": scene_refs,
    }


def platform_params(platform, prompt, negative, duration, ref_list, seed,
                    first_frame=None, last_frame=None):
    """生成该平台的参数字典（抹平字段名差异）。

    first_frame / last_frame 为约定路径（见 --frames-dir）。
    首帧字段名按平台抹平：可灵 image / Seedance metadata.image / 其余 first_frame_image。
    末帧仅对声明 supports_last_frame 的平台输出（目前是 Seedance 2.0 两张图模式）。
    """
    key = platform["_key"]
    p = {
        "prompt": prompt,
        "negative_prompt": negative,
        "aspect_ratio": platform["aspect"],
        "seed": seed,
    }
    if key == "wan":
        p["size"] = platform["resolution"]
        p["duration"] = duration
        p["parameters"] = {"size": platform["resolution"], "duration": duration,
                           "shot_type": "single", "watermark": False}
        if ref_list:
            p["reference_urls"] = ref_list
    elif key == "seedance":
        p["seconds"] = str(duration)
        p["metadata"] = {"resolution": platform["resolution"], "ratio": platform["aspect"]}
        if ref_list:
            p["images"] = ref_list
            p["metadata"]["image_role"] = "reference_image"
    elif key == "kling":
        p["duration"] = duration
        p["resolution"] = platform["resolution"]
        p["mode"] = "std"
        p["cfg_scale"] = 0.5
        if ref_list:
            p["refer_image"] = ref_list[0]
    elif key == "veo":
        p["duration"] = duration
        p["resolution"] = platform["resolution"]
        p["generate_audio"] = platform["audio_native"]
    elif key == "runway":
        p["duration"] = duration
        p["ratio"] = platform["aspect"]
        p["resolution"] = platform["resolution"]
    elif key == "sora":
        p["seconds"] = str(duration)
        p["size"] = platform["resolution"]
    else:  # generic / hailuo / luma
        p["duration"] = duration
        p["resolution"] = platform["resolution"]
    if ref_list and "refer_image" not in p:
        p["reference_images"] = ref_list

    # 首帧 / 末帧：按平台字段名写入
    if first_frame:
        if key == "kling":
            p["image"] = first_frame
        elif key == "seedance":
            p.setdefault("metadata", {})["image"] = first_frame
        else:
            p["first_frame_image"] = first_frame
    if last_frame and platform.get("supports_last_frame"):
        p["last_frame_image"] = last_frame
    return p


def find_global_style(lines):
    """从文件头找【全局风格】内容（供"见【全局风格】"内联用）。"""
    for l in lines[:40]:
        s = l.strip()
        for tag in ("【全局风格】", "【风格】", "全局风格："):
            if s.startswith(tag):
                return s[len(tag):].strip("：: ")
    return ""


def export(src, platform_keys, outdir, continuity="first", frames_dir="frames"):
    lines, had_bom, newline = read_lines(src)
    shots = split_shots(lines)
    if not shots:
        print("未找到分镜标题行")
        return 1
    global_style = find_global_style(lines)

    for pk in platform_keys:
        plat = dict(PLATFORMS[pk])
        plat["_key"] = pk
        pdir = os.path.join(outdir, pk)
        segdir = os.path.join(pdir, "segments")
        if os.path.exists(pdir):
            shutil.rmtree(pdir)
        os.makedirs(segdir)

        manifest = {
            "platform": pk,
            "platform_name": plat["name"],
            "notes": plat["notes"],
            "aspect_ratio": plat["aspect"],
            "resolution": plat["resolution"],
            "audio_native": plat["audio_native"],
            "shot_count": len(shots),
            "segments": [],
        }
        prompt_lines = []
        total_seg = 0
        total_dur = 0
        prev_seg_id = None      # 首帧续写链：上一段的末帧 → 下一段的首帧
        prev_scene = None       # 跨场判断：换场时断开续写链，避免继承上一场
        last_frame_of = {}     # shot_no -> 该段末帧应保存的文件名

        for num, sec, block in shots:
            meta = shot_meta(block, global_style)
            cur_scene = meta.get("scene_line") or ""
            same_scene = (prev_scene is not None and cur_scene == prev_scene)
            segs, _ = parse_segments(block[1:])
            groups = regroup_segments(segs, plat)
            for gi, g in enumerate(groups):
                total_seg += 1
                total_dur += g["dur"]
                seg_id = "%03d-%02d" % (num, gi + 1)
                core, detail = build_prompt(meta, g, plat)
                if continuity == "keyframes" and plat.get("supports_last_frame"):
                    detail += ("\n首尾帧：起止画面已由首帧/末帧给定，"
                               "只补中间运动，不重新构图、不改起止内容。")
                prompt_full = core + "\n" + detail
                refs = meta["refs"][:plat["max_refs"]]

                # 三种连续性模式：
                #   first（默认，快）   同场戏取上一段末帧作首帧；跨场断开
                #   keyframes（首尾帧） 先批量出关键帧静帧，每段用相邻两张
                #   off               不做帧控制
                first_frame = None
                last_frame = None
                chain_src = None
                if continuity == "keyframes":
                    first_frame = "%s/shot%03d_k%02d.png" % (frames_dir, num, gi)
                    if plat.get("supports_last_frame"):
                        last_frame = "%s/shot%03d_k%02d.png" % (frames_dir, num, gi + 1)
                elif continuity == "first":
                    chain_src = prev_seg_id if (gi > 0 or same_scene) else None
                    if chain_src:
                        first_frame = "%s/%s_last.png" % (frames_dir, chain_src)

                params = platform_params(plat, prompt_full, NEGATIVE, g["dur"], refs,
                                         seed=1000 + num * 10 + gi,
                                         first_frame=first_frame, last_frame=last_frame)
                # 另给一份"只含核心层"的参数（模型对长提示词敏感时用）
                params_min = platform_params(plat, core, NEGATIVE, g["dur"], refs,
                                             seed=1000 + num * 10 + gi,
                                             first_frame=first_frame, last_frame=last_frame)
                item = {
                    "id": seg_id,
                    "shot": num,
                    "shot_seconds": sec,
                    "seg_index": gi + 1,
                    "seg_count": len(groups),
                    "timecode": "%02d:%02d-%02d:%02d" % (
                        g["t1"] // 60, g["t1"] % 60, g["t2"] // 60, g["t2"] % 60),
                    "duration": g["dur"],
                    "source_segments": [
                        {"no": s["no"], "tag": s.get("tag", ""), "body": s.get("body", "")}
                        for s in g["parts"]],
                    "dialogue": meta["dialogue"],
                    "reference_images": refs,
                    "first_frame_from": chain_src,
                    "first_frame_image": first_frame,
                    "last_frame_image": last_frame,
                    "params": params,
                    "params_minimal": params_min,
                    "prompt_core": core,
                    "prompt_detail": detail,
                    "prompt": prompt_full,
                    "prompt_chars": {"core": len(core), "detail": len(detail),
                                     "full": len(prompt_full)},
                }
                manifest["segments"].append(item)
                # 首帧续写链
                prev_seg_id = item["id"]
                last_frame_of[num] = item["id"]
                # 单段文件：分层给出
                fn = "shot%03d_seg%02d_%d-%ds.txt" % (num, gi + 1, g["t1"], g["dur"])
                with open(os.path.join(segdir, fn), "w", encoding="utf-8") as f:
                    f.write("【核心层 prompt_core｜%d 字｜直接喂模型】\n%s\n\n"
                            "【详情层 prompt_detail｜%d 字｜连续性/约束，按需追加】\n%s\n"
                            % (len(core), core, len(detail), detail))
                prompt_lines.append(
                    "─" * 66 + "\n【%s】分镜%03d 第%d/%d段 ｜ %s ｜ %s ｜ %d秒"
                    " ｜ 核心%d字/全量%d字\n%s"
                    % (item["id"], num, gi + 1, len(groups), item["timecode"],
                       plat["name"], g["dur"], len(core), len(prompt_full), core))
            prev_scene = cur_scene

        manifest["total_segments"] = total_seg
        manifest["total_seconds"] = total_dur
        manifest["original_seconds"] = sum(sec or 0 for _, sec, _ in shots)

        # 分层投喂说明
        core_lens = [s["prompt_chars"]["core"] for s in manifest["segments"]]
        manifest["layered_prompt"] = {
            "why": ("行业铁律：单条 prompt 别超过 80–100 词，超过会让模型困惑、输出变糊。"
                    "但逐镜流水线要保连续性，光靠核心层不够 —— 所以分两层。"),
            "layers": {
                "prompt_core": ("画面本体（风格+人物+动作+镜头+光线+台词）。"
                                "直接喂模型。控制在 %d 字以内。" % CORE_LIMIT),
                "prompt_detail": ("连续性与约束（空间锚定/道具状态/画风细则/负面/防重复）。"
                                  "预算紧或模型对长提示词敏感时可省略。"),
            },
            "usage": {
                "默认": "用 params（= core + detail），连续性最好",
                "保守": "用 params_minimal（只用 core），模型对长提示词敏感时用",
                "逐段文件": "segments/*.txt 已按 core / detail 分节给出",
            },
            "core_chars": {
                "min": min(core_lens) if core_lens else 0,
                "max": max(core_lens) if core_lens else 0,
                "avg": round(sum(core_lens) / len(core_lens)) if core_lens else 0,
                "limit": CORE_LIMIT,
                "over_limit": sum(1 for x in core_lens if x > CORE_LIMIT),
            },
        }
        # 时长守恒说明
        over = manifest["total_seconds"] - manifest["original_seconds"]
        if over > 1:
            manifest["duration_note"] = (
                "总时长比原稿多 %d 秒 —— 因为该平台只有 %s 秒档位，无法整除每镜时长。"
                "多出的画面在剪辑时裁掉即可（不影响剧情，拼接点仍按 timecode）。"
                % (over, "/".join(str(d) for d in plat["durations"][:6])))
        else:
            manifest["duration_note"] = "总时长与原稿一致，档位精确匹配。"
        how = {
            "first": ("按 segments 顺序生成；同场戏内每段用上一段末帧作首帧，跨场断开。"
                      "生成后把末帧保存为 frames/<段id>_last.png，下一段已按此约定填好。"),
            "keyframes": ("先批量生成每镜的关键帧静帧（frames/shotNNN_kNN.png，"
                          "数量 = 该平台切分后的段数 + 1），"
                          "再逐段用相邻两张作首/末帧生成视频；中间运动交给模型，不重新构图。"
                          "仅声明 supports_last_frame 的平台（Seedance 2.0 两张图模式）可用末帧。"),
            "off": "不做首帧/末帧控制。",
        }.get(continuity, "")
        manifest["continuity_chain"] = {
            "mode": continuity,
            "how": how,
            "first_frame_of": {s["id"]: s.get("first_frame_from") for s in manifest["segments"]},
            "frame_paths": {s["id"]: {"first": s.get("first_frame_image"),
                                      "last": s.get("last_frame_image")}
                            for s in manifest["segments"]},
            "save_last_frame_of": last_frame_of,
        }
        if continuity == "keyframes":
            kf = {}
            for s in manifest["segments"]:
                if s["seg_index"] == 1:
                    n = s["seg_count"]
                    kf["shot%03d" % s["shot"]] = [
                        "%s/shot%03d_k%02d.png" % (frames_dir, s["shot"], i)
                        for i in range(n + 1)]
            manifest["continuity_chain"]["keyframes_per_shot"] = kf
            if not plat.get("supports_last_frame"):
                manifest["continuity_chain"]["last_frame_unsupported"] = (
                    "该平台不支持末帧，已退化为只用首帧关键帧。")
        manifest["usage"] = {
            "1": "按 segments 顺序逐段生成",
            "2": "每段用 item.params 发起调用，prompt 字段即提示词",
            "3": ("先用 continuity_chain.keyframes_per_shot 批量生成关键帧静帧，再逐段生成。"
                  if continuity == "keyframes" else
                  "生成后把末帧保存为 frames/<段id>_last.png。" if continuity == "first" else
                  "无。"),
            "4": "参考图按 item.reference_images 传，文件名需与素材目录一致",
            "5": "同 seed 重跑可复现；换 seed 可换一版试试",
        }
        with open(os.path.join(pdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        with open(os.path.join(pdir, "prompts.txt"), "w", encoding="utf-8") as f:
            f.write("%s ｜ 竖屏 %s ｜ %s\n%s\n\n%s"
                    % (plat["name"], plat["aspect"], plat["resolution"],
                       plat["notes"], "\n".join(prompt_lines)))
        print("[%s] %d 镜 → %d 段 / 共 %d 秒 → %s"
              % (pk, len(shots), total_seg, total_dur, pdir))
    return 0


def check(src):
    """通用性体检：检查当前分镜稿的段长是否落在各平台安全区。"""
    lines, _, _ = read_lines(src)
    shots = split_shots(lines)
    print("=" * 70)
    print("通用性体检: %s" % src)
    print("=" * 70)
    print("平台时长档位：")
    for pk, p in PLATFORMS.items():
        if pk == "generic":
            continue
        print("  %-10s %-22s %d–%d 秒" % (pk, p["name"], p["min_duration"], p["max_duration"]))
    print()

    all_lens = []
    too_short = []
    for num, sec, block in shots:
        segs, _ = parse_segments(block[1:])
        for s in segs:
            d = norm_time(s["t2"]) - norm_time(s["t1"])
            all_lens.append(d)
            if d < 4:
                too_short.append("分镜%03d 运镜%d: %d 秒" % (num, s["no"], d))
    if not all_lens:
        print("未解析到运镜段")
        return 1
    print("运镜段总数: %d" % len(all_lens))
    print("段长分布: 最短 %ds / 最长 %ds / 平均 %.1fs"
          % (min(all_lens), max(all_lens), sum(all_lens) / len(all_lens)))
    print("段长 <4 秒（低于 Veo3/Seedance2.0/Wan 下限）: %d 段" % len(too_short))
    for t in too_short[:12]:
        print("  ! " + t)
    if len(too_short) > 12:
        print("  ... 还有 %d 段" % (len(too_short) - 12))
    print()
    if too_short:
        print("→ 建议：运行 export_for_api.py --platform <平台>，")
        print("  脚本会按该平台的时长档位自动把短段合并成可投喂的段。")
    else:
        print("→ 全部段长 ≥4 秒，各平台均可直接投喂。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--platform", default="generic",
                    help="平台 key，或 all（导出全部）")
    ap.add_argument("-o", "--output", default="./api_export")
    ap.add_argument("--continuity", choices=["off", "first", "keyframes"], default="first",
                    help="连续性模式：first 首帧续写（默认，快）/ "
                         "keyframes 关键帧先行（首尾帧，先出静帧）/ off 不用")
    ap.add_argument("--frames-dir", default="frames",
                    help="首帧/关键帧的约定目录（相对各平台输出目录），默认 frames")
    ap.add_argument("--check", action="store_true", help="只做通用性体检")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print("文件不存在: %s" % args.src)
        return 2

    if args.check:
        return check(args.src)

    if args.platform == "all":
        keys = list(PLATFORMS.keys())
    else:
        keys = [k.strip() for k in args.platform.split(",") if k.strip()]
        for k in keys:
            if k not in PLATFORMS:
                print("未知平台: %s（可用: %s）" % (k, ", ".join(PLATFORMS.keys())))
                return 2
    os.makedirs(args.output, exist_ok=True)
    return export(args.src, keys, args.output, args.continuity, args.frames_dir)


if __name__ == "__main__":
    sys.exit(main() or 0)
