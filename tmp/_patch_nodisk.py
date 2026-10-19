import io
p = "12_maintain.py"
s = io.open(p, encoding="utf-8").read()

old_nvme = '            "nvme_used": human_bytes(dir_size_bytes(cfg.path("paths.nvme_root"))),'
new_nvme = (
    '            # --no-disk 时跳过 NVMe 目录统计：那是 35 万个文件的真实 I/O，\n'
    '            # 单次要 21 秒左右。只想看「跑到哪一步了」的时候没必要付这个代价。\n'
    '            "nvme_used": ("skipped（--no-disk）" if getattr(args, "no_disk", False)\n'
    '                          else human_bytes(dir_size_bytes(cfg.path("paths.nvme_root")))),'
)
assert old_nvme in s, "nvme_used 行没找到"
s = s.replace(old_nvme, new_nvme)

old_arg = '    p.add_argument("--days", type=int, default=30, help="clean-frames / rotate-logs 的天数阈值")'
new_arg = (old_arg + "\n"
           '    p.add_argument("--no-disk", action="store_true",\n'
           '                   help="status 时跳过 NVMe 目录统计（省约 21 秒，看进度时用）")')
assert old_arg in s, "days 参数行没找到"
s = s.replace(old_arg, new_arg)

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("patched")
