# -*- coding: utf-8 -*-
"""
shotparse —— AI 短剧分镜 txt 的解析 / 写回公共库。

支持两种常见的分镜标题写法：
    {分镜001｜30秒}
    {分镜001}｜15秒
    {分镜001}

设计要点（全部来自实战踩坑）：
  * 时间码统一接受 00:00-00:04 与 00:00-00:04 两种写法；
  * 运镜行收尾括号允许 ] / ］ / 】 / 〕 混用（很多导出稿会混）；
  * 运镜正文可能被粘在上一行的台词尾部，解析时要能识别并拆出；
  * 分时段正文区与【结束状态】之间是正文，之后是编剧笔记区；
  * 写回时严格保留原文件的行尾符与 BOM。
"""

import os
import re
import shutil

# ---------------------------------------------------------------- 常量

BOM = "﻿"

# 分镜标题：允许 {分镜001｜30秒} / {分镜001}｜15秒 / {分镜001} / {分镜001｜30秒
TITLE_RE = re.compile(
    r"^\{?\s*分镜\s*(\d{1,3})\s*\}?\s*[｜|]?\s*(\d{1,3})\s*秒?\s*\}?\s*$"
)
TITLE_RE2 = re.compile(r"^(\{分镜(\d{1,3})\})\s*[｜|]?\s*(\d{1,3})?\s*秒?\s*$")

# 运镜行：运镜N[00:00-00:04|景别·动作]：正文
SEG_RE = re.compile(
    r"^运镜(\d+)\[(\d{1,2}:\d{2})-(\d{1,2}:\d{2})(?:\|([^\]］】〕]*))?[\]］】〕]\s*[：:]?\s*(.*)$"
)

# 括号混用归一：把运镜行收尾的全角括号 ] 】 〕 统一成半角 ]
BRACKET_RE = re.compile(r"^(\s*运镜\d+\[[^\n]*?)([\]］】〕])(\s*[：:]?\s*)(.*)$")


def normalize_brackets(line):
    """把运镜行收尾的全角括号统一成半角 ]。"""
    m = BRACKET_RE.match(line)
    if m and m.group(2) != "]":
        return m.group(1) + "]" + m.group(3) + m.group(4)
    return line


def is_title_line(line):
    """判断是否是合法的分镜标题行（用于避免把标题当交叉引用改掉）。"""
    return bool(TITLE_RE.match(line.strip()) or TITLE_RE2.match(line.strip()))

# 跨镜引用（对无状态 API 是噪声）
CROSS_REF_PATTERNS = [
    r"上一镜", r"前一镜", r"上一段", r"前面那镜", r"上一场", r"前一幕",
    r"上一批", r"第\d{2,3}\s*镜", r"全剧在\s*\d+\s*镜", r"承接",
]

# 纸面可读文字（AI 生成中文必错）—— 只认"物体上印着/写着字"的情形
READABLE_TEXT_PATTERNS = [
    r"印着", r"写着", r"字样", r"打印字", r"手写字", r"刻着", r"题着",
    r"签字栏", r"执行人栏", r"甲方栏", r"委托监护人栏", r"签署栏",
    r"抬头一行", r"抬头写", r"编号[:：]", r"编号为",
    r"字迹清晰", r"字迹可辨", r"清晰可辨", r"清晰显示", r"可辨认的字",
]

# 主体位移动词（用于判断"人是否钉在原地"）
MOTION_VERBS = [
    # 移动 / 位移
    "走", "跑", "退", "进", "上前", "上前一步", "往前", "往后", "折返", "退开",
    "跨", "迈", "挪", "挪步", "跟上", "追上", "走向", "走到", "推开", "拉开",
    "起身", "站起", "起身离", "坐下", "起身走", "转身", "回头", "侧身", "俯身",
    "弯腰", "前倾", "后仰", "蹲下", "起身站",
    # 取放 / 操作（也算可见动作）
    "拿起", "拎起", "拎出", "取出", "掏出", "放下", "搁下", "合上", "打开",
    "掀开", "翻开", "翻页", "递", "接过", "摊开", "展开", "展开", "按住",
    "按下", "扣上", "系上", "解下", "挂上", "插进", "拔出", "抽出", "抱起",
    "端起", "捧起", "抓住", "捏住", "攥住", "松开", "摸到", "拍开", "扔下",
    "压住", "揉", "搓", "抹", "写", "签", "撕", "折", "拆", "剪",
    # 视线 / 头部（弱位移，命中即视为有动作）
    "抬头", "低头", "抬眼", "垂眼", "闭眼", "睁眼", "看向", "望向", "盯着",
    "视线移", "目光",
]

# 刻意静止（属于有意为之，不算"人不动"的缺陷）
STILLNESS_MARKERS = [
    "一动不动", "定住", "停住", "屏住", "静止", "钉在", "没有动", "站住不动",
    "保持不动", "纹丝不动",
]

def norm_time(t):
    """'00:4' -> 4 ; '00:04' -> 4"""
    if not t:
        return 0
    try:
        m, s = t.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def read_lines(path):
    """读取并返回 (lines, had_bom, newline)。newline 为 '\\n' 或 '\\r\\n'。"""
    raw = open(path, "rb").read()
    had_bom = raw[:3] == b"\xef\xbb\xbf"
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return text.split("\n"), had_bom, newline


def write_lines(path, lines, had_bom=True, newline="\n"):
    """原子写回：先写临时文件再替换（避免编码名写错时把源文件清空）。"""
    data = newline.join(lines)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(data)
    if had_bom:
        # utf-8-sig 会在首部加 BOM
        with open(tmp, "r+", encoding="utf-8-sig", newline="") as f:
            content = f.read()
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            f.write(content)
    os.replace(tmp, path)


def backup(path, bak_dir=None, tag="bak"):
    """备份原文件，返回备份路径（已存在则不覆盖）。"""
    bak_dir = bak_dir or os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    target = os.path.join(bak_dir, "%s_%s" % (name, tag))
    i = 1
    while os.path.exists(target):
        target = os.path.join(bak_dir, "%s_%s%d" % (name, tag, i))
        i += 1
    shutil.copy2(path, target)
    return target


def find_titles(lines):
    """返回 [(index, num, seconds)]，只认行首独立的分镜标题。"""
    out = []
    for i, l in enumerate(lines):
        s = l.strip()
        m = TITLE_RE.match(s) or TITLE_RE2.match(s)
        if m:
            num = int(m.group(1))
            sec = m.group(2) or m.group(3)
            out.append((i, num, int(sec) if sec else None))
    return out


def split_shots(lines):
    """把整个文件切成 [(num, seconds, [lines...])]。"""
    titles = find_titles(lines)
    shots = []
    for k, (i, num, sec) in enumerate(titles):
        end = titles[k + 1][0] if k + 1 < len(titles) else len(lines)
        shots.append((num, sec, lines[i:end]))
    return shots


def normalize_brackets(line):
    """把运镜行收尾的全角括号统一成半角 ]。"""
    m = BRACKET_RE.match(line)
    if m and m.group(2) and m.group(2)[0] in "］】〕":
        return line.replace(m.group(2)[0], "]", 1)
    return line


def parse_segments(block):
    """从一块正文里解析出运镜段。

    返回 (segments, extra_lines)：
      segments = [{"no":1,"t1":"00:00","t2":"00:04","tag":"景别·动作","body":"..."}]
      extra_lines = 没能并入运镜段的其它正文行（通常是台词行）
    同时会把"粘在台词行尾部的运镜头"拆出来并入对应运镜段。
    """
    segments = []
    extra = []
    for line in block:
        s = line.strip()
        if not s:
            continue
        m = SEG_RE.match(s)
        if m:
            segments.append({
                "no": int(m.group(1)),
                "t1": m.group(2), "t2": m.group(3),
                "tag": (m.group(4) or "").strip(),
                "body": m.group(5).strip(),
            })
            continue
        # 台词行尾部粘着运镜头：xxx。运镜3[00:05-00:08|标签]：正文
        g = re.search(r"(运镜\d+\[[^\]]*[\]］】〕]\s*[：:].*)$", s)
        if g:
            head = s[:g.start()].strip()
            if head:
                extra.append(head)
            seg = g.group(1)
            m2 = SEG_RE.match(seg)
            if m2:
                segments.append({
                    "no": int(m2.group(1)),
                    "t1": m2.group(2), "t2": m2.group(3),
                    "tag": (m2.group(4) or "").strip(),
                    "body": m2.group(5).strip(),
                })
            else:
                extra.append(seg)
            continue
        extra.append(s)
    return segments, extra


def field(block, label):
    """取 '标签：值' 形式的字段值；无冒号时取标签之后的剩余文本。"""
    for l in block:
        t = l.strip()
        if t.startswith(label):
            return t[len(label):].lstrip("：:").strip()
    return ""


def field_re(block, pattern):
    for l in block:
        if re.match(pattern, l.strip()):
            return l.strip()
    return ""


def has_cross_ref(text):
    for p in CROSS_REF_PATTERNS:
        if re.search(p, text):
            return True
    return False


def cross_ref_hits(text):
    return [p for p in CROSS_REF_PATTERNS if re.search(p, text)]


def has_readable_text(text):
    return any(re.search(p, text) for p in READABLE_TEXT_PATTERNS)


def has_motion(text):
    return any(v in text for v in MOTION_VERBS)


def shot_duration_seconds(segments):
    """由最后一段的结束时间码推断单镜时长（秒）。"""
    if not segments:
        return None
    return norm_time(segments[-1]["t2"])


def check_timecodes(segments, expect=None):
    """检查时间码：连续、不重叠、覆盖 0→expect。返回问题列表。"""
    issues = []
    if not segments:
        return ["无运镜段"]
    nums = [s["no"] for s in segments]
    if nums != list(range(1, len(nums) + 1)):
        issues.append("运镜编号不连续: %s" % nums)
    starts = [norm_time(s["t1"]) for s in segments]
    ends = [norm_time(s["t2"]) for s in segments]
    if starts[0] != 0:
        issues.append("首段起点 %s != 00:00" % segments[0]["t1"])
    for i in range(len(ends) - 1):
        if ends[i] > starts[i + 1]:
            issues.append("第%d段与第%d段时间重叠" % (i + 1, i + 2))
    if expect is not None and ends[-1] != expect:
        issues.append("末段终点 %02d:%02d != 期望 %d 秒" % (ends[-1] // 60, ends[-1] % 60, expect))
    return issues
