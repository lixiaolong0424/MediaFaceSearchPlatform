import sqlite3

c = sqlite3.connect("data/library.db")
n = c.execute("DELETE FROM app_settings WHERE key='nvme_pending_at'").rowcount
c.commit()
print("cleared markers:", n)
print("nvme_usage:", c.execute(
    "SELECT value, updated_at FROM app_settings WHERE key='nvme_usage'").fetchone())
c.close()
