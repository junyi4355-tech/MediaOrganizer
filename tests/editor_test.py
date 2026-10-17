# -*- coding: utf-8 -*-
"""编辑器端到端自检(需要桌面会话, 会短暂弹窗)。

覆盖: 接线定向、级数、同级同列、成环防护、连点取消、来源卡单接线柱、
      整链发光、文件夹卡(纯目录层级)、落盘目录=用户命名、未接入跳过。
结果写 _editor_result.txt(不靠控制台, PowerShell 会吞中文输出)。

用法:  py tests\\editor_test.py
"""
import os
import sys
import time
import tempfile
import faulthandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ["MO_RASTER"] = "1"          # 软件渲染: 像素/截图判断才可靠

from PyQt6.QtWidgets import QApplication               # noqa: E402
from PyQt6.QtCore import QPointF, QPoint, Qt, QEvent   # noqa: E402
from PyQt6.QtGui import QMouseEvent                    # noqa: E402
import workflow_editor as we                           # noqa: E402

RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "_editor_result.txt")
open(RES, "w", encoding="utf-8").close()


def w(line):
    with open(RES, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    we.init_diag()
    we.install_excepthook()      # 自检也要安全网: 否则一个异常就是进程 abort
    faulthandler.enable()
    faulthandler.dump_traceback_later(180, exit=True)

    tmp = tempfile.mkdtemp(prefix="mo_editor_test_")
    srcdir = os.path.join(tmp, "素材")
    os.makedirs(srcdir)
    for n in ("a.jpg", "b.mp4", "c.txt", "d.dng", "e.arw"):
        open(os.path.join(srcdir, n), "wb").write(b"x" * 4096)
    out = os.path.join(tmp, "输出")
    os.makedirs(out)

    app = QApplication([])
    app.setStyleSheet(we.GLOBAL_QSS)
    win = we.EditorWindow()
    sc = win.scene
    win.show()
    vp = win.view.viewport()

    def send(t, pt, b=Qt.MouseButton.NoButton, bs=Qt.MouseButton.NoButton):
        g = vp.mapToGlobal(QPoint(int(pt.x()), int(pt.y())))
        QApplication.sendEvent(vp, QMouseEvent(t, QPointF(pt), QPointF(g), b, bs,
                                               Qt.KeyboardModifier.NoModifier))

    def click_port(port):
        v = win.view.mapFromScene(port.scenePos())
        send(QEvent.Type.MouseButtonPress, QPointF(v), Qt.MouseButton.LeftButton,
             Qt.MouseButton.LeftButton)
        send(QEvent.Type.MouseButtonRelease, QPointF(v), Qt.MouseButton.LeftButton,
             Qt.MouseButton.NoButton)
        return sc.itemAt(port.scenePos(), win.view.transform()) is port

    ok = []

    # ---------- 1) 主链: 源 → 文件夹卡("我的素材") → 输出卡(视频) ----------
    src = we.SourceNode("s1", "源", srcdir, "年-月-日", out_base=out)
    sc.add_source(src)
    folder = we.FolderNode("f1", "我的素材")
    sc.add_target(folder)
    sc.arrange()
    h1 = click_port(src.out_port)
    h2 = click_port(folder.in_port)
    ok.append(("文件夹卡不是输出卡(无筛选)",
               (not isinstance(folder, we.TargetNode)) and (not hasattr(folder, "filter"))))
    ok.append(("点接线柱 源→文件夹卡", h1 and h2 and sc._upstream(folder) is src))

    vid = we.TargetNode("t1", "视频", "s1", ("cat", we.CAT_VIDEO), "年-月-日", None)
    sc.add_target(vid)
    sc.arrange()
    click_port(folder.out_port)
    click_port(vid.in_port)
    ok.append(("文件夹卡→输出卡", sc._upstream(vid) is folder))
    ok.append(("级数 源0/文件夹1/输出2",
               (sc.level(src), sc.level(folder), sc.level(vid)) == (0, 1, 2)))
    root, segs, s2 = sc.chain_for(vid)
    ok.append(("链路层级 = 用户命名/视频", segs == ("我的素材", "视频") and s2 is src))
    ok.append(("来源卡只有一个接线柱", len(src.ports()) == 1))

    # ---------- 2) 反向点也按"离源远近"定向(用独立的一对卡, 不动主链) ----------
    holder = we.TargetNode("t7", "照片", "s1", ("cat", we.CAT_IMAGE), "年-月-日", None)
    sc.add_target(holder)
    sc.arrange()
    click_port(src.out_port)
    click_port(holder.in_port)                      # holder = 第1级
    loose = we.FolderNode("f7", "散装")
    sc.add_target(loose)
    sc.arrange()
    click_port(loose.out_port)                      # 先点"未接入"卡的【出】
    click_port(holder.in_port)                      # 再点"已接入"卡的【入】
    ok.append(("反向点仍按离源定向", sc._upstream(loose) is holder and sc.level(loose) == 2))
    sc.delete_card(loose)
    sc.delete_card(holder)

    # ---------- 3) 成环防护 / 连点取消 ----------
    ok.append(("成环防护(下游不能再吃回祖先)",
               sc._creates_cycle(vid, folder) and not sc._creates_cycle(src, vid)))
    click_port(src.out_port)
    click_port(src.out_port)
    ok.append(("连点两次取消选中", sc._pending_port is None))

    # ---------- 4) 同级 → 同一列 ----------
    other = we.TargetNode("t2", "其他", "s1", ("cat", we.CAT_OTHER), "年-月-日", None)
    sc.add_target(other)
    sc.arrange()
    click_port(src.out_port)
    click_port(other.in_port)
    sc.arrange()
    ok.append(("同级排在同一列", abs(folder.x() - other.x()) < 0.5))
    sc.delete_card(other)          # 后面的"整链发光/落盘"只留主链, 避免多一张末端卡干扰
    sc.arrange()

    # ---------- 5) 整条链每段都发光(文件夹卡没有 task, 下游末端卡在传) ----------
    win.running = True
    vid.task_id = 7
    t = we.EditorTask(7, srcdir, "年-月-日", vid.filter, out, False,
                      segments=("我的素材", "视频"))
    t.total, t.written, t.paused = 10, 0, False
    win.scheduler._tasks[7] = t
    win._tick_lights()
    first = {id(c): (c.active, c.light) for c in sc.conns}
    time.sleep(0.05)
    win._tick_lights()
    second = {id(c): (c.active, c.light) for c in sc.conns}
    ok.append(("整条链每段都发光",
               len(sc.conns) == 2 and all(v[0] for v in second.values())
               and all(second[k][1] > first[k][1] for k in first)))
    win.running = False
    win.scheduler._tasks.clear()

    # ---------- 6) 真跑一轮: 目录必须是 我的素材/视频/日期 ----------
    win._start_run()
    t0 = time.time()
    while win.running and time.time() - t0 < 30:
        app.processEvents()
        time.sleep(0.05)
    rel = sorted(os.path.relpath(os.path.join(r, f), out) for r, _, fs in os.walk(out) for f in fs)
    ok.append(("落盘目录 = 用户命名/视频", len(rel) == 1
               and rel[0].split(os.sep)[0] == "我的素材"
               and rel[0].split(os.sep)[1] == "视频"
               and rel[0].endswith("b.mp4")))

    # ---------- 7) 末端文件夹卡不收文件(只给提示) ----------
    win._reset_all()
    src2 = we.SourceNode("s9", "源2", srcdir, "年-月-日",
                         out_base=os.path.join(tmp, "空输出"))
    sc.add_source(src2)
    sc.add_target(we.FolderNode("f9", "空文件夹"))
    sc._connect(src2, list(sc.targets.values())[0])
    win._start_run()
    t0 = time.time()
    while win.running and time.time() - t0 < 8:
        app.processEvents()
        time.sleep(0.05)
    ok.append(("末端文件夹卡不收文件", "还没接输出卡" in win.log_box.toPlainText()))

    for name, good in ok:
        w("%-30s %s" % (name, "PASS" if good else "FAIL"))
    w("EDITOR_RESULT: " + ("PASS" if all(g for _, g in ok) else "FAIL"))
    try:
        win.scheduler.shutdown()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
