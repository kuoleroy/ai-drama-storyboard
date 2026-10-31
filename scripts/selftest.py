#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— 技能包自检：造一份"带各种毛病"的样例稿，跑修复器 → 跑体检器 → 断言。

用法：python3 selftest.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fix_storyboard.py")
AUDIT = os.path.join(HERE, "audit_storyboard.py")

SAMPLE = """{分镜001｜30秒}
【人物】程远
【场景】出租屋客厅
风格：真人写实，竖屏9:16
场景：出租屋客厅，凌晨一点
机位：缓慢推向程远
【起始状态】程远蹲在纸箱旁
分时段正文：
运镜1[00:00-00:03|中景·纸箱]：程远把夹克从纸箱里拎出来一半。
运镜2[00:03-00:06|近景·手部]：可见人物1人。他捏住袖口。
运镜3[00:06-00:09|特写·纸页】：可见人物1人。他展开纸页。
（纸页上印着"HAWK-1"）
【结束状态】程远把纸塞进内袋
【剧情作用】第一次回响
【衔接】下一镜须继承：程远站着
【卡点】无。

{分镜002｜30秒}
【人物】程远
【场景】出租屋客厅
风格：真人写实，竖屏9:16
场景：出租屋客厅，凌晨一点零四分
机位：固定
【起始状态】程远站着
分时段正文：
运镜1[00:00-00:08|中景·窗台]：窗台上手机震了一下。
程远（抬头看）：“谁。”
运镜2[00:00-00:08|近景·脸]：他的视线从纸箱移到窗台。
【结束状态】程远走向窗台
【剧情作用】与{分镜001（第一次回响）}形成闭环
【衔接】无
【卡点】无。
"""


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, r.stdout + r.stderr


def main():
    tmp = tempfile.mkdtemp(prefix="storyboard_selftest_")
    path = os.path.join(tmp, "sample.txt")
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(SAMPLE)

    print("=== 1. 修复前体检 ===")
    rc1, out1 = run([sys.executable, AUDIT, path])
    print("\n".join(out1.splitlines()[:14]))

    print("\n=== 2. 执行修复 ===")
    rc2, out2 = run([sys.executable, FIX, path])
    print(out2.strip())

    print("\n=== 3. 修复后体检 ===")
    rc3, out3 = run([sys.executable, AUDIT, path])
    print("\n".join(out3.splitlines()[:14]))

    print("\n=== 4. 断言 ===")
    with open(path, encoding="utf-8-sig") as f:
        after = f.read()
    fails = []

    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (" | " + detail if detail else ""))
        if not cond:
            fails.append(name)

    # 括号：不应再有全角收尾
    full_bracket = [l for l in after.split("\n")
                    if l.startswith("运镜") and re.search(r"[\]］】〕]", l) is None]
    check("运镜行括号均已配对为半角]", not full_bracket, str(full_bracket[:2]))

    check("空运镜段已补正文",
          all(re.match(r"^运镜\d+\[[^\]]*\]：\s*\S", l)
              for l in after.split("\n") if l.startswith("运镜")))

    check("跨镜引用『上一镜』已清除", "上一镜" not in after)
    check("跨镜引用『下一镜』已清除", "下一镜" not in after)
    # 花括号交叉引用：只查非标题行（标题行本身含 {分镜NNN} 是合法的）
    non_title = [l for l in after.split("\n")
                 if not re.match(r"^\s*\{?\s*分镜\s*\d{1,3}\s*\}?\s*[｜|]?\s*\d{1,3}\s*秒?\s*\}?\s*$", l.strip())]
    check("花括号交叉引用已去括号与镜号",
          not any("{分镜" in l or "第001镜" in l for l in non_title),
          str([l for l in non_title if "{分镜" in l or "第001镜" in l][:2]))
    check("标题仍为 2 个", len(re.findall(r"\{分镜\d{3}｜\d+秒\}", after)) == 2)
    check("台词保留", "谁。" in after)
    check("剧情作用保留信息", "第一次回响" in after)
    check("备份文件已生成", any(f.endswith("修复前备份") for f in os.listdir(tmp)),
          str(os.listdir(tmp)))

    if fails:
        print("\n自检失败: %s" % fails)
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    print("\n全部通过 ✓")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
