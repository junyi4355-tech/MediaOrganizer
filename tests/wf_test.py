# -*- coding: utf-8 -*-
"""调度器端到端测试: 3 任务并发, 校验每个文件只落盘一次、进度到 100%。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 允许从项目根导入
from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtWidgets import QApplication
from workflow_editor import EditorTask, TaskScheduler, CAT_IMAGE, CAT_VIDEO, CAT_OTHER

app = QApplication(sys.argv)
tmp = tempfile.mkdtemp(prefix="wf_")
src = os.path.join(tmp, "src"); os.makedirs(src)
for i in range(4):
    open(os.path.join(src, "p%d.jpg" % i), "wb").write(b"\xff" * 8)
for i in range(3):
    open(os.path.join(src, "v%d.mp4" % i), "wb").write(b"\x00" * 16)
open(os.path.join(src, "d.txt"), "w").write("x")

tasks = [
    EditorTask(1, src, "年-月-日", ("cat", CAT_IMAGE), os.path.join(tmp, "o1"), False),
    EditorTask(2, src, "年-月-日", ("cat", CAT_VIDEO), os.path.join(tmp, "o2"), False),
    EditorTask(3, src, "年-月-日", ("cat", CAT_OTHER), os.path.join(tmp, "o3"), False),
]
sched = TaskScheduler()
for t in tasks:
    sched.register(t)
    t.build_jobs(None)

result = {"done": False}
copied = []
sched.all_done.connect(lambda: result.__setitem__("done", True))
sched.job_done.connect(lambda tid, path, ok: copied.append((tid, path, ok)))
QTimer.singleShot(8000, app.quit)

def poll():
    if not result["done"]:
        QTimer.singleShot(30, poll)
    else:
        app.quit()

QTimer.singleShot(50, poll)
app.exec()

from collections import Counter
cnt = Counter((tid, path) for tid, path, _ in copied)
dups = [k for k, v in cnt.items() if v > 1]
ok = result["done"] and all(t.written == t.total for t in tasks)
lines = ["WF_TEST done=%s" % result["done"],
         "tasks: %s" % [(t.tid, t.total, t.written) for t in tasks],
         "copied: %s" % copied,
         "duplicates: %s" % dups]
if os.path.isdir(os.path.join(tmp, "o1")):
    o1 = sum(len(fs) for _, _, fs in os.walk(os.path.join(tmp, "o1")))
    o2 = sum(len(fs) for _, _, fs in os.walk(os.path.join(tmp, "o2"))) if os.path.isdir(os.path.join(tmp, "o2")) else 0
    o3 = sum(len(fs) for _, _, fs in os.walk(os.path.join(tmp, "o3"))) if os.path.isdir(os.path.join(tmp, "o3")) else 0
    lines += ["files: 照片=%d 视频=%d 其他=%d" % (o1, o2, o3)]
    ok = ok and o1 == 4 and o2 == 3 and o3 == 1
lines += ["WF_RESULT: " + ("PASS" if ok else "FAIL")]
with open("_wf_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
sched.shutdown()
for w in sched._workers:
    w.wait(2000)
