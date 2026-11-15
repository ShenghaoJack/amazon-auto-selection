# 代码规范检查: 注释与代码同行混排 / 未使用导入(粗查)
import glob
import re
import sys

issues = []
for f in sorted(glob.glob("steps/*.py") + glob.glob("core/*.py") + ["main.py"]):
    with open(f, encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    in_str = None
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for idx, ch in enumerate(s):
            if ch in ('"', "'"):
                if in_str == ch:
                    in_str = None
                elif in_str is None:
                    in_str = ch
            elif ch == "#" and in_str is None:
                if idx > 0 and s[idx - 1] not in (" ", "\t"):
                    issues.append(f"{f}:{i}: {s[:70]}")
                break
print("inline-comment issues:", len(issues))
for x in issues[:30]:
    print(" ", x)
sys.exit(1 if issues else 0)
