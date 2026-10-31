#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
e2e_test.py —— 用真实剧本跑完整链路，验证"内容零丢失"。

流程：
    备份 → fix_storyboard → build_feed_prompts → audit
    逐项比对：台词内容、运镜正文、标题数、编码

用法：python3 e2e_test.py <分镜稿.txt>
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fix_storyboard.py")
FEED = os.path.join(HERE, "build_feed_prompts.py")
AUDIT = os.path.join(HERE, "audit_storyboard.py")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, r.stdout + r.stderr


def dialog_quotes(text):
    """提取所有"角色（提示）：台词"格式行里的台词内容。

    不统计描述性引号（如 "比上一镜更暗"、"可见人物N人"）——
    那些是画面描述或占位说明，不是台词。
    """
    out = set()
    for line in text.split("\n"):
        ls = line.strip()
        if ls.startswith("【台词与声音】"):
            ls = ls[len("【台词与声音】"):]
        if not re.match(r"^[^（(]{1,10}[（(][^）)]{0,20}[）)]\s*[：:]", ls):
            continue
        for m in re.findall(r"[“\"「『]([^”\"」』]{2,})[”\"」』]", ls):
            out.add(m.strip())
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    src = sys.argv[1]
    tmp = tempfile.mkdtemp(prefix="e2e_")
    work = os.path.join(tmp, "story.txt")
    shutil.copy2(src, work)

    with open(src, encoding="utf-8-sig") as f:
        before = f.read()

    print("=== 1. 修复 ===")
    rc, out = run([sys.executable, FIX, work])
    print("\n".join(out.strip().splitlines()[:12]))

    print("\n=== 2. 生成投喂版 ===")
    feed = os.path.join(tmp, "feed.txt")
    rc, out = run([sys.executable, FEED, work, "-o", feed, "--mode", "separate"])
    print(out.strip())

    print("\n=== 3. 体检（修复后）===")
    rc, out = run([sys.executable, AUDIT, work])
    print("\n".join(out.splitlines()[:12]))

    print("\n=== 4. 内容比对 ===")
    with open(work, encoding="utf-8-sig") as f:
        after = f.read()
    with open(feed, encoding="utf-8-sig") as f:
        feedtext = f.read()

    fails = []

    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (" | " + detail if detail else ""))
        if not cond:
            fails.append(name)

    # 台词内容零丢失
    qb, qa = dialog_quotes(before), dialog_quotes(after)
    lost = qb - qa
    check("台词内容零丢失", not lost, ("丢失: %s" % list(lost)[:3]) if lost else "")

    # 投喂版也保留全部台词
    qf = dialog_quotes(feedtext)
    lost2 = qb - qf
    check("投喂版台词零丢失", not lost2, ("丢失: %s" % list(lost2)[:3]) if lost2 else "")

    # 标题数一致
    tb = len(re.findall(r"\{?\s*分镜\s*\d{1,3}", before))
    ta = len(re.findall(r"^\{?\s*分镜\s*\d{1,3}\s*\}?\s*[｜|]?\s*\d{1,3}\s*秒?\s*\}?\s*$",
                         after, re.M))
    check("分镜标题数保持", tb == ta, "原 %d → 现 %d" % (tb, ta))

    # 运镜正文不丢
    seg_b = re.findall(r"^运镜\d+\[[^\]]*\][：:]?(.*)$", before, re.M)
    seg_a = re.findall(r"^运镜\d+\[[^\]]*\][：:]?(.*)$", after, re.M)
    nonempty_b = [s.strip() for s in seg_b if s.strip()]
    nonempty_a = [s.strip() for s in seg_a if s.strip()]
    check("运镜正文不减少", len(nonempty_a) >= len(nonempty_b),
          "原 %d → 现 %d" % (len(nonempty_b), len(nonempty_a)))

    # 编码保持
    raw = open(work, "rb").read()
    check("UTF-8 BOM 保持", raw[:3] == b"\xef\xbb\xbf")

    # 投喂栏齐全（separate 模式会同时输出原正文里的同名栏，故按 ≥ 判定，
    # 并额外确认每镜都有一份新增栏）
    for tag in ("【人物】", "【场景】", "【道具状态】", "【本镜可见人物】"):
        n = feedtext.count(tag)
        check("投喂版含 %s" % tag, n >= ta, "%d 条（至少 %d）" % (n, ta))
    # 原正文未被投喂栏替换（若原稿已有同类栏，生成器会跳过新增，故只断言不小于原有数）
    orig_names = before.count("【人物】")
    check("原正文未被投喂栏替换", feedtext.count("【人物】") >= orig_names,
          "原有 %d → 投喂版 %d" % (orig_names, feedtext.count("【人物】")))

    if fails:
        print("\n端到端测试失败: %s" % fails)
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    print("\n端到端测试全部通过 ✓")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
