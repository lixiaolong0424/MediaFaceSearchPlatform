"""用 Streamlit 官方的 AppTest 无头执行 app.py，直接判定「能不能渲染出来」。

为什么用它：白屏有两类原因，靠看浏览器分不清 ——
  A. 脚本没跑完（卡住/很慢）→ 前端一直停在 Stop
  B. 脚本跑完但抛异常 → 前端显示红色 traceback
  C. 脚本跑完、没异常，但前端/WS 有问题
AppTest 在进程内跑一遍脚本，能直接给出 at.exception 与元素个数，一次锁定 A/B。
"""

import sys
import time
from pathlib import Path

ROOT = Path(r'D:\DATA\PROJECT\建设多媒体整理系统')
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

t0 = time.monotonic()
at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
at.run()
el = time.monotonic() - t0

print(f"\n脚本执行耗时：{el:.1f}s")
print("=" * 70)
if at.exception:
    print("❌ 抛出异常：")
    for e in at.exception:
        print(f"   {e.type}: {e.message}")
        if getattr(e, "stack_trace", None):
            print("   " + "\n   ".join(str(e.stack_trace).splitlines()[-12:]))
else:
    print("✅ 没有异常")

print(f"\n渲染元素统计：")
for name in ("markdown", "caption", "warning", "error", "info", "success",
             "metric", "tabs", "columns", "button", "slider", "selectbox",
             "checkbox", "text_input", "number_input", "radio", "header",
             "subheader", "image", "dataframe", "expander", "spinner"):
    try:
        n = len(getattr(at, name))
    except Exception:
        continue
    if n:
        print(f"   {name:<14} {n}")

print("\n前几条 markdown（看界面到底渲染了什么）：")
for m in at.markdown[:12]:
    txt = " ".join(str(m.value).split())[:110]
    print(f"   - {txt}")
print("\nwarning/error 内容：")
for kind in ("warning", "error"):
    for x in getattr(at, kind)[:6]:
        print(f"   [{kind}] {' '.join(str(x.value).split())[:140]}")
print("\n侧边栏 metric：")
for m in at.metric[:8]:
    print(f"   {m.label} = {m.value}")

# 关键断言：照片墙必须真的有内容
md = " ".join(" ".join(str(m.value).split()) for m in at.markdown)
assert_txt = "命中" in md or "照片墙" in md or "素材人脸检索系统" in md
print(f"\n标题/内容是否出现：{assert_txt}")
print("=" * 70)
print("结论：" + ("脚本可正常渲染 ✅" if (not at.exception and assert_txt)
                  else "仍有问题 ❌"))
