import io, re
p = "README.md"
s = io.open(p, encoding="utf-8").read()
# 统一编号：0.3→0.2（硬件约束）、0.4→0.3（L2d）、0.5→0.4（实测修正）
s = s.replace("### 0.3 全集跑起来后暴露的", "### 0.2 全集跑起来后暴露的")
s = s.replace("### 0.4 L2d 重排序", "### 0.3 L2d 重排序")
s = s.replace("### 0.5 实测证据修正规格的地方", "### 0.4 实测证据修正规格的地方")
s = s.replace("见下面 §0.4①", "见下面 §0.3①")
s = s.replace("见 §0.4①", "见 §0.3①")
s = s.replace("见 §0.4③", "见 §0.3③")
s = s.replace("见 §0.4④", "见 §0.3④")
# §0.3 正文里对自身的引用（原 §0.3 那节）
s = s.replace("（2026-10-16，改变了流水线结构）", "（2026-10-16，改变了流水线结构）")
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("renumbered")
