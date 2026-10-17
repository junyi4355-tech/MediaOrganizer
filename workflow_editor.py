# -*- coding: utf-8 -*-
"""素材整理工作流 — 多卡片可连线工作流编辑器(默认入口)。

架构:
  来源卡(Source)   = 链路的根: 一个输入文件夹 + 一个输出根目录(无入接线柱)
  下游卡(Target)   = 左【入】右【出】: 既可继续接下一级, 也可作为末端卡落盘
  链路 = 目录层级  = 离来源越远的卡, 就是输出目录里越深的一层子文件夹;
                     只有末端卡落盘, 中间卡只贡献一层文件夹名
  级数(level)      = 离来源几级(来源=0); 接线方向按级数自动定(近者作上一级),
                     同级才按点击先后; 一键整理按级数分列
  独占             = 末端自定义文件夹卡声明的后缀, 类别卡不再重复收(避免拷两份)
  调度             = 多任务并发, 全局最多 3 路传输; 右键输出卡可加急(其他暂让路);
                     视频大文件同时只占 1 路, 小文件并行
  交互             = 连线悬停中点 ✕ 删除 / 拖动实时跟随 / 滚轮缩放 / 一键整理 /
                     一键重置 / 每卡可删除; 运行中按钮变红可暂停/中断
"""
import os
import sys
import time
import shutil
import threading

from PyQt6.QtCore import (Qt, QTimer, QPointF, QRectF, pyqtSignal, QThread, QObject)
from PyQt6.QtGui import (QPainter, QPainterPath, QPainterPathStroker, QColor, QPen,
                         QFont, QCursor)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton,
                             QToolButton, QDockWidget, QTextEdit, QProgressBar,
                             QGraphicsScene, QGraphicsView, QGraphicsItem,
                             QGraphicsObject, QMessageBox, QComboBox, QVBoxLayout,
                             QHBoxLayout, QFrame, QFileDialog, QMenu, QInputDialog,
                             QDialog, QLineEdit, QDialogButtonBox, QFormLayout,
                             QRadioButton, QCheckBox)

from media_organizer import (build_plan, unique_dest, CATS, CAT_IMAGE, CAT_VIDEO,
                             CAT_OTHER, GRANS, _fmt_size, GLOBAL_QSS, _base_dir,
                             init_diag, log_s, install_excepthook,
                             install_qt_message_handler, install_smoke_leave,
                             screenshot_mode)

GRAN_DAY = GRANS[2]          # 全局默认归档精度: 到日
CONCURRENCY = 3              # 最多同时 3 路传输
MAX_VIDEO_IN_FLIGHT = 1      # 视频大文件同时只占 1 路
CHUNK = 1 << 20              # 流式拷贝块 1MB


# ---------------------------------------------------------------------------
# 流式拷贝 + 落盘(带 0-1 百分比回调)
# ---------------------------------------------------------------------------
def stream_copy(src, dst, on_prog=None):
    """分块复制, 实时回调进度 0.0~1.0(用于日志/卡片百分比)。"""
    size = os.path.getsize(src)
    done = 0
    on_prog = on_prog or (lambda x: None)
    if size <= 0:
        on_prog(1.0)
        return
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            done += len(chunk)
            on_prog(done / size)
    on_prog(1.0)


def perform_job_stream(out_base, job, move, on_prog=None, log=None, segments=None):
    """把一个文件归档到 out_base(处理重名)。move 无进度, copy 有百分比。返回成功与否。
    segments: 这条链贡献的目录层级, 如 ("无人机", "视频") —— 越靠链路末端越深的一层。
              为 None 时沿用 dest_rel 自带的第一层(类别名), 即旧的"每卡独立"行为。"""
    rel = job["dest_rel"]
    if segments:
        parts = rel.replace("\\", "/").split("/")
        rel = "/".join(list(segments) + parts[1:])   # 用链路层级替换原来的类别层
    dest = unique_dest(out_base, rel)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if move:
            if on_prog:
                on_prog(1.0)
            shutil.move(job["path"], dest)
        else:
            stream_copy(job["path"], dest, on_prog)
        if log:
            log("  %s → %s" % (os.path.basename(job["path"]), os.path.relpath(dest, out_base)))
        return True
    except Exception as e:
        if log:
            log("  失败 %s: %s" % (os.path.basename(job["path"]), e))
        return False


# ---------------------------------------------------------------------------
# 任务模型
# ---------------------------------------------------------------------------
# filter: (kind, value)  kind ∈ {"cat","ext","exts","all"}
#   "cat"  -> 照片/视频/其他 ; "ext"  -> ".xxx" 单个自定义后缀
#   "exts" -> (".dng",".arw") 扩展名列表(自定义文件夹卡用) ; "all" -> 全部
class EditorTask:
    def __init__(self, tid, src_folder, gran, filter_spec, out_base, move, segments=None,
                 exclusive_exts=()):
        self.tid = tid
        self.src_folder = src_folder
        self.gran = gran                 # 输出卡自己的精度(默认继承全局, 可个别微调)
        self.filter = filter_spec
        self.out_base = out_base
        self.move = move
        # 链路上各级卡贡献的文件夹层级(根目录下面第一级 -> 末端卡), 如 ("无人机","视频")
        self.segments = tuple(segments) if segments else None
        # 本次运行里被"自定义文件夹卡"声明的扩展名 —— 类别卡不再重复收这些文件,
        # 否则同一个 .dng 会既进 照片/ 又进 无人机/, 白拷一份。
        self.exclusive_exts = tuple(exclusive_exts)
        self.jobs = []                   # 待落盘 job 列表(顺序)
        self.total = 0
        self.written = 0                 # 已落盘数(真实)
        self.done = set()                # 已落盘的 idx 集合(乱序安全)
        self.paused = False              # 被暂停(加急/手动)
        self.urgent = False              # 被加急
        self.started = False

    def build_jobs(self, log):
        plan = build_plan(self.src_folder, self.gran, True, log or (lambda x: None),
                          lambda a, b: None)
        self.jobs = [j for j in plan if self._match(j)]
        self.total = len(self.jobs)

    def _match(self, job):
        kind, val = self.filter
        if kind == "cat":
            # 具体优先: 已被自定义文件夹卡声明的后缀, 类别卡让路(不重复拷贝)
            if self.exclusive_exts and job["path"].lower().endswith(self.exclusive_exts):
                return False
            return job["category"] == val
        if kind == "ext":
            return job["path"].lower().endswith(val)
        if kind == "exts":
            return job["path"].lower().endswith(tuple(val))
        return True

    def done_pct(self):
        return int(self.written * 100 / self.total) if self.total else 0


# ---------------------------------------------------------------------------
# 调度器: 多 worker 并发, 限 CONCURRENCY 路; 加急优先; 视频限 1 路; 暂停跳过
# ---------------------------------------------------------------------------
class TaskScheduler(QObject):
    job_start = pyqtSignal(int, str, int)    # task_id, path, size
    job_prog = pyqtSignal(int, str, float)   # task_id, path, 0-1
    job_done = pyqtSignal(int, str, bool)    # task_id, path, ok
    all_done = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._tasks = {}          # tid -> EditorTask
        self._inflight = {}       # tid -> set(indices 正在传)
        self._video_busy = 0
        self._active = 0          # 正在执行的 job 数
        self._finished = False
        self._workers = None
        self._workers_started = False

    def _ensure_workers(self):
        # 后台工作线程延后到"真正开始整理"时才启动, 降低程序常驻资源占用
        with self._lock:
            if not self._workers_started:
                self._workers = [_Worker(self) for _ in range(CONCURRENCY)]
                for w in self._workers:
                    w.start()
                self._workers_started = True

    def register(self, task):
        self._ensure_workers()
        with self._lock:
            self._tasks[task.tid] = task

    def _is_done(self):
        for t in self._tasks.values():
            if len(t.done) < t.total:
                return False
        return True

    def pick(self):
        """线程安全: 选一个应传 job(加急优先, 跳过暂停, 视频限1路, 已落盘/已在传的跳过)。
        从 0 起扫第一个未完成且未在传的 idx(乱序完成安全)。返回 (task, idx, job) 或 None。"""
        with self._lock:
            cands = [t for t in self._tasks.values()
                     if t.jobs and len(t.done) < t.total and not t.paused]
            if not cands:
                return None
            pool = [t for t in cands if t.urgent] or cands
            infl = self._inflight
            for t in pool:
                infl_t = infl.get(t.tid, set())
                done_t = t.done
                for idx in range(t.total):
                    if idx in infl_t or idx in done_t:
                        continue
                    j = t.jobs[idx]
                    if j["category"] == CAT_VIDEO and self._video_busy >= MAX_VIDEO_IN_FLIGHT:
                        continue
                    infl_t.add(idx)
                    infl.setdefault(t.tid, infl_t)
                    if j["category"] == CAT_VIDEO:
                        self._video_busy += 1
                    self._active += 1
                    return (t, idx, j)
            return None

    def finish(self, task, idx, job):
        with self._lock:
            infl = self._inflight.get(task.tid)
            if infl is not None:
                infl.discard(idx)
                if not infl:
                    self._inflight.pop(task.tid, None)
            task.done.add(idx)
            task.written = len(task.done)   # 真实落盘数
            if job["category"] == CAT_VIDEO:
                self._video_busy -= 1
            self._active -= 1
            if self._active <= 0 and self._is_done() and not self._finished:
                self._finished = True
                self.all_done.emit()

    def shutdown(self):
        if self._workers:
            for w in self._workers:
                w.stop_flag = True


class _Worker(QThread):
    def __init__(self, sched, parent=None):
        super().__init__(parent)
        self.sched = sched
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            got = self.sched.pick()
            if got is None:
                time.sleep(0.03)
                continue
            task, idx, job = got
            try:
                size = os.path.getsize(job["path"]) or 0
                self.sched.job_start.emit(task.tid, job["path"], size)
                ok = perform_job_stream(task.out_base, job, task.move,
                                        on_prog=lambda p: self.sched.job_prog.emit(task.tid, job["path"], p),
                                        log=None, segments=task.segments)
                self.sched.job_done.emit(task.tid, job["path"], ok)
            except Exception:
                self.sched.job_done.emit(task.tid, job["path"], False)
            finally:
                self.sched.finish(task, idx, job)


# ---------------------------------------------------------------------------
# 卡片图元
# ---------------------------------------------------------------------------
_CARD_W = 218
_CARD_H = 66
_FONT_HDR = None


def _mkfont(size, bold=False):
    f = QFont(); f.setPointSize(size); f.setBold(bold); return f


class PortItem(QGraphicsObject):
    """接线柱: 左【入】右【出】。点两张卡的接线柱即可连线(任意互接, 越远层级越深)。"""
    W = 14
    clicked = pyqtSignal(object)   # self

    def __init__(self, owner, side):
        super().__init__()
        self.owner = owner          # 卡片
        self.side = side            # "in" 或 "out"
        self.hover = False
        self.pending = False        # 已被点选, 等待接另一头
        self.setZValue(5)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        # ★ 打开几何变更通知: 否则 itemChange() 根本不会被调用, 连线就不会跟着接线柱走
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def boundingRect(self):
        return QRectF(-8, -8, self.W + 16, self.W + 16)

    def paint(self, painter, option, widget=None):
        if self.pending:
            c = QColor("#facc15")           # 已点选待连线: 亮黄
        elif self.hover:
            c = QColor(94, 106, 210)
        else:
            c = QColor(139, 139, 144, 150)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(c, 2))
        painter.setBrush(QColor(20, 20, 23))
        painter.drawEllipse(QRectF(-self.W / 2, -self.W / 2, self.W, self.W))
        if self.pending:
            painter.setBrush(QColor("#facc15"))
            painter.drawEllipse(QRectF(-2.5, -2.5, 5, 5))

    def mousePressEvent(self, e):
        self.clicked.emit(self)
        e.accept()

    def itemChange(self, change, value):
        # 接线柱位置变了(卡片被拖动/自动排布) → 立即重算相关连线, 线才会跟着走
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            sc = self.scene()
            if sc is not None and hasattr(sc, "refresh_links"):
                sc.refresh_links(self.owner)
        return super().itemChange(change, value)

    def hoverEnterEvent(self, e):
        self.hover = True; self.update(); super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self.hover = False; self.update(); super().hoverLeaveEvent(e)


class CardBase(QGraphicsObject):
    """卡片基类: 卡体 + 右上角删除按钮 + 移动 + 左【入】右【出】两个接线柱。
    链路上离来源越远的卡, 其名字就是越深的一层子文件夹。"""
    del_requested = pyqtSignal(object)

    def __init__(self, cid, title, color, has_in=True, has_out=True):
        super().__init__()
        self.cid = cid
        self.title = title
        self.color = color
        self.w = _CARD_W
        self.h = _CARD_H
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        # ★ 打开几何变更通知: Qt 默认不发 ItemPositionHasChanged, 不开这个标志
        #   itemChange() 永远不触发 —— 拖动卡片时连线就不会跟着动。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(3)
        self._del_rect = QRectF(self.w - 24, -2, 18, 18)
        self.in_port = None
        self.out_port = None
        if has_in:
            self.in_port = PortItem(self, "in")
            self.in_port.setParentItem(self)
            self.in_port.setPos(0, self.h / 2)
        if has_out:
            self.out_port = PortItem(self, "out")
            self.out_port.setParentItem(self)
            self.out_port.setPos(self.w, self.h / 2)

    def ports(self):
        return [p for p in (self.in_port, self.out_port) if p is not None]

    def segment(self):
        """本卡在链路里贡献的文件夹层级名(来源卡不贡献层级, 它本身就是输出根)。"""
        return ""

    def has_downstream(self):
        """是否还有下一级 —— 有下一级的卡只作文件夹层级, 不落盘。"""
        sc = self.scene()
        if sc is None or not hasattr(sc, "conns"):
            return False
        return any(c.src is self for c in sc.conns)

    def boundingRect(self):
        return QRectF(-4, -4, self.w + 8, self.h + 8)

    def _bg(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.w, self.h)
        painter.setPen(QPen(QColor(self.color), 1.6) if self.isSelected() else QPen(QColor(255, 255, 255, 26), 1))
        painter.setBrush(QColor(28, 28, 31))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawRect(QRectF(rect.left() + 10, rect.top() + 1.2, rect.width() - 20, 1.2))
        # 类别色竖条
        painter.setBrush(self.color)
        painter.drawRoundedRect(QRectF(6, 10, 3, rect.height() - 20), 1.5, 1.5)
        # 删除按钮(× 用圆点拼, 规避 item 内 drawLine 原生崩溃)
        painter.setBrush(QColor(255, 255, 255, 34))
        painter.drawEllipse(QRectF(self.w - 22, 6, 16, 16))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(199, 201, 204))
        cx0, cy0 = self.w - 14, 14
        painter.drawEllipse(QPointF(cx0 - 3, cy0 - 3), 1.1, 1.1)
        painter.drawEllipse(QPointF(cx0 + 3, cy0 - 3), 1.1, 1.1)
        painter.drawEllipse(QPointF(cx0 - 3, cy0 + 3), 1.1, 1.1)
        painter.drawEllipse(QPointF(cx0 + 3, cy0 + 3), 1.1, 1.1)

    def mousePressEvent(self, e):
        if self._del_rect.adjusted(0, 6, 0, 6).contains(e.pos()):
            self.del_requested.emit(self)
            e.accept()
            return
        super().mousePressEvent(e)

    def itemChange(self, change, value):
        # 卡片被拖动 → 通知场景重算挂在它身上的连线(否则线留在原地不动)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            sc = self.scene()
            if sc is not None and hasattr(sc, "refresh_links"):
                sc.refresh_links(self)
        return super().itemChange(change, value)


class SourceNode(CardBase):
    """素材来源卡 = 链路的根: 一个输入文件夹 + 一个输出根目录。
    它没有入接线柱(前面不该再有东西), 只有右侧出接线柱。"""
    edit_requested = pyqtSignal(object)   # 双击: 改来源文件夹 / 改输出根目录

    def __init__(self, cid, name, folder, gran, out_base=""):
        super().__init__(cid, "素材来源", QColor("#3b82f6"), has_in=False, has_out=True)
        self.name = name
        self.folder = folder
        self.gran = gran
        self.out_base = out_base      # 这条链的输出根目录(下游各卡的名字逐级拼在它下面)
        self.port = self.out_port

    def paint(self, painter, option, widget=None):
        self._bg(painter)
        painter.setPen(QPen(QColor("#ededed"), 1)); painter.setFont(_mkfont(10, True))
        painter.drawText(QRectF(16, 8, self.w - 40, 20), Qt.AlignmentFlag.AlignLeft, self.title + " · " + self.name)
        painter.setPen(QPen(QColor(139, 139, 144), 1)); painter.setFont(_mkfont(8))
        painter.drawText(QRectF(16, 30, self.w - 24, 16), Qt.AlignmentFlag.AlignLeft,
                         "📁 " + (os.path.basename(self.folder) or self.folder)[:22])
        root = os.path.basename(self.out_base.rstrip("\\/")) if self.out_base else "未设输出根目录"
        painter.drawText(QRectF(16, 46, self.w - 24, 14), Qt.AlignmentFlag.AlignLeft,
                         "精度:%s  → %s" % (self.gran, root[:14]))

    def mouseDoubleClickEvent(self, e):
        self.edit_requested.emit(self)
        e.accept()

    def set_geom(self):
        if self.out_port is not None:
            self.out_port.setPos(self.w, self.h / 2)


class TargetNode(CardBase):
    """输出卡: 左【入】接上一级, 右【出】可继续接下一级。
    有下一级时它只作一层文件夹; 作为链路末端时按自己的筛选落盘。
    out_base 留空 = 跟随链路根(来源卡)的输出根目录。"""
    edit_requested = pyqtSignal(object)   # 双击设置
    menu_requested = pyqtSignal(object, QPointF)   # 右键

    def __init__(self, cid, name, src_tid, filter_spec, gran, out_base=None):
        cat_color = {"cat": {CAT_IMAGE: QColor("#34d399"), CAT_VIDEO: QColor("#f87171"),
                             CAT_OTHER: QColor("#94a3b8")},
                     }.get("cat", {})
        self._cat_color = cat_color.get(filter_spec[1], QColor("#94a3b8")) if filter_spec[0] == "cat" else QColor("#8b8b90")
        super().__init__(cid, "输出", self._cat_color)
        self.name = name
        self.src_tid = src_tid
        self.filter = filter_spec
        self.gran = gran
        self.out_base = out_base     # None/"" = 跟随来源卡的输出根目录
        self.task_id = None          # 运行后由连接的任务 id
        self.count = 0
        self.total = 0
        self.urgent = False
        self.paused = False
        self.port = self.in_port

    def segment(self):
        """本卡贡献的文件夹层级名: 类别卡用类别名, 自定义后缀卡用卡片名。"""
        k, v = self.filter
        if k == "cat":
            return v
        return self.name or "输出"

    def filter_label(self):
        k, v = self.filter
        return {"cat": v, "ext": "后缀" + v, "exts": "后缀 " + " ".join(v), "all": "全部"}.get(k, v)

    def paint(self, painter, option, widget=None):
        self._bg(painter)
        painter.setPen(QPen(QColor("#ededed"), 1)); painter.setFont(_mkfont(10, True))
        tag = "  [文件夹]" if self.has_downstream() else ""
        fm = painter.fontMetrics()
        head = fm.elidedText(self.title + " · " + self.name + tag,
                             Qt.TextElideMode.ElideRight, int(self.w - 86))
        painter.drawText(QRectF(16, 5, self.w - 86, 18), Qt.AlignmentFlag.AlignLeft, head)
        # 右上角显示"第几级"(离来源几级) —— 同级就是同一层文件夹
        sc = self.scene()
        lv = sc.level(self) if (sc is not None and hasattr(sc, "level")) else None
        painter.setFont(_mkfont(8))
        painter.setPen(QPen(QColor("#f87171") if lv is None else QColor("#8bb4f8"), 1))
        painter.drawText(QRectF(self.w - 66, 5, 42, 18), Qt.AlignmentFlag.AlignRight,
                         "未接入" if lv is None else "第%d级" % lv)
        painter.setPen(QPen(QColor(139, 139, 144), 1)); painter.setFont(_mkfont(8))
        fm = painter.fontMetrics()
        painter.drawText(QRectF(16, 24, self.w - 24, 14), Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText("筛选:" + self.filter_label() + "  精度:" + self.gran,
                                       Qt.TextElideMode.ElideRight, int(self.w - 30)))
        # 第三行: 左"输出位置" + 右"计数", 互不重叠
        painter.drawText(QRectF(16, 39, self.w - 96, 14), Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText("→ " + (os.path.basename(self.out_base) if self.out_base
                                               else "跟随来源卡输出根"),
                                       Qt.TextElideMode.ElideRight, int(self.w - 102)))
        painter.setPen(QPen(QColor("#c9c9cc"), 1))
        painter.drawText(QRectF(self.w - 88, 39, 74, 14), Qt.AlignmentFlag.AlignRight,
                         "%d/%d  %d%%" % (self.count, self.total, (self.count * 100 // self.total) if self.total else 0))
        # 底部进度条
        ratio = (self.count / self.total) if self.total else 0.0
        py = self.h - 5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 16))
        painter.drawRoundedRect(QRectF(14, py, self.w - 28, 3), 1.5, 1.5)
        painter.setBrush(self._cat_color)
        painter.drawRoundedRect(QRectF(14, py, max(0.0, (self.w - 28) * ratio), 3), 1.5, 1.5)

    def mouseDoubleClickEvent(self, e):
        self.edit_requested.emit(self); e.accept()

    def contextMenuEvent(self, e):
        self.menu_requested.emit(self, e.scenePos()); e.accept()


class FolderNode(CardBase):
    """文件夹卡: **纯目录层级** —— 只有名字, 不筛选、不落盘。
    相当于在"来源卡"和"输出卡"之间夹了一层文件夹: 名字就是输出目录里的一层子文件夹,
    要收文件必须在它后面再接一张输出卡。"""
    edit_requested = pyqtSignal(object)          # 双击: 改文件夹名
    menu_requested = pyqtSignal(object, QPointF)  # 右键

    def __init__(self, cid, dir_name):
        super().__init__(cid, "文件夹", QColor("#c084fc"))
        self.dir_name = dir_name
        self.name = dir_name        # 卡片显示名 = 文件夹名, 只有一个名字
        self._cat_color = QColor("#c084fc")   # 连线发光取色用(与卡片同色)
        self.task_id = None         # 文件夹卡永远没有任务(不落盘)
        self.count = 0
        self.total = 0
        self.urgent = False
        self.paused = False
        self.port = self.in_port

    def segment(self):
        """本卡贡献的文件夹层级名 = 用户命名的文件夹名。"""
        return self.dir_name

    def paint(self, painter, option, widget=None):
        self._bg(painter)
        painter.setPen(QPen(QColor("#ededed"), 1)); painter.setFont(_mkfont(10, True))
        fm = painter.fontMetrics()
        head = fm.elidedText("文件夹 · " + self.dir_name, Qt.TextElideMode.ElideRight, int(self.w - 86))
        painter.drawText(QRectF(16, 5, self.w - 86, 18), Qt.AlignmentFlag.AlignLeft, head)
        sc = self.scene()
        lv = sc.level(self) if (sc is not None and hasattr(sc, "level")) else None
        painter.setFont(_mkfont(8))
        painter.setPen(QPen(QColor("#f87171") if lv is None else QColor("#8bb4f8"), 1))
        painter.drawText(QRectF(self.w - 66, 5, 42, 18), Qt.AlignmentFlag.AlignRight,
                         "未接入" if lv is None else "第%d级" % lv)
        painter.setPen(QPen(QColor(139, 139, 144), 1))
        painter.drawText(QRectF(16, 26, self.w - 24, 14), Qt.AlignmentFlag.AlignLeft,
                         "📁 只作目录层级, 后面接输出卡才落盘")
        painter.drawText(QRectF(16, 42, self.w - 24, 14), Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText("→ 输出目录 / " + self.dir_name,
                                       Qt.TextElideMode.ElideRight, int(self.w - 30)))

    def mouseDoubleClickEvent(self, e):
        self.edit_requested.emit(self); e.accept()

    def contextMenuEvent(self, e):
        self.menu_requested.emit(self, e.scenePos()); e.accept()


class ConnLine(QGraphicsObject):
    """来源 out 接线柱 → 输出 in 接线柱 的连线 + 传输光带。
    鼠标靠近时线中点浮出 ✕ 删除按钮(也可右键弹出菜单删除)。"""
    del_requested = pyqtSignal(object)
    menu_requested = pyqtSignal(object, QPointF)

    HIT_W = 20        # 命中/悬停宽度: 细线不好点中, 加粗描边做命中区
    BADGE_R = 9       # ✕ 按钮半径

    def __init__(self, src_node, tgt_node):
        super().__init__()
        self.src = src_node
        self.tgt = tgt_node
        self.light = 0.0             # 0..1 光点位置
        self.active = False          # 该连接正在传输(真实)
        self.hover = False
        self.path = QPainterPath()
        self._mid = QPointF()
        self._del_hit = QRectF()
        self.setZValue(2)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        self._recalc()

    def _recalc(self):
        """按两张卡片接线柱的当前位置重算曲线端点。
        ★ 必须先 prepareGeometryChange(): path 变了 boundingRect 就变了, 不通知 Qt
        就会残留旧像素 —— 拖动卡片时满屏"重影"的根因之一就在这里。"""
        p1 = self.src.mapToScene(self.src.out_port.pos())
        p2 = self.tgt.mapToScene(self.tgt.in_port.pos())
        dx = p2.x() - p1.x()
        path = QPainterPath(p1)
        path.cubicTo(p1.x() + dx * 0.45, p1.y(), p2.x() - dx * 0.45, p2.y(), p2.x(), p2.y())
        self.prepareGeometryChange()
        self.path = path
        self._p1, self._p2 = p1, p2
        self._mid = path.pointAtPercent(0.5)
        self._del_hit = QRectF(self._mid.x() - self.BADGE_R, self._mid.y() - self.BADGE_R,
                               self.BADGE_R * 2, self.BADGE_R * 2)
        self.update()

    def boundingRect(self):
        return self.path.boundingRect().adjusted(-16, -16, 16, 16).united(self._del_hit)

    def shape(self):
        st = QPainterPathStroker()
        st.setWidth(self.HIT_W)
        s = st.createStroke(self.path)
        if not self._del_hit.isNull():
            s.addEllipse(self._del_hit)
        return s

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.path.length() <= 1:
            return
        hot = self.active
        if hot:
            glow = QColor(getattr(self.tgt, "_cat_color", QColor("#8b8b90"))); glow.setAlphaF(0.30)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(glow, 3.2))
            painter.drawPath(self.path)
        base = QColor(139, 139, 144, 200 if hot else 95)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(base, 1.5 if hot else 1.2))
        painter.drawPath(self.path)
        if self.hover:      # 悬停: 中点浮出 ✕ 删除按钮
            painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
            painter.setBrush(QColor(28, 28, 31, 240))
            painter.drawEllipse(self._del_hit)
            painter.setPen(QPen(QColor("#f87171"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            m = self._mid
            painter.drawLine(QPointF(m.x() - 3.4, m.y() - 3.4), QPointF(m.x() + 3.4, m.y() + 3.4))
            painter.drawLine(QPointF(m.x() - 3.4, m.y() + 3.4), QPointF(m.x() + 3.4, m.y() - 3.4))
        if hot:
            # 传输光点(白色光纤), 沿连接流动
            pt = self.path.pointAtPercent(min(1.0, self.light))
            for rr, aa in ((7, 0.10), (4, 0.30)):
                c2 = QColor(getattr(self.tgt, "_cat_color", QColor("#8b8b90"))); c2.setAlphaF(aa)
                painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(c2)
                painter.drawEllipse(pt, rr, rr)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(255, 255, 255, 230))
            painter.drawEllipse(pt, 1.5, 1.5)

    def hoverEnterEvent(self, e):
        self.hover = True; self.update(); super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self.hover = False; self.update(); super().hoverLeaveEvent(e)

    def mousePressEvent(self, e):
        if self._del_hit.adjusted(-2, -2, 2, 2).contains(e.pos()):
            self.del_requested.emit(self)
            e.accept()
            return
        e.ignore()

    def contextMenuEvent(self, e):
        self.menu_requested.emit(self, e.scenePos())
        e.accept()


# ---------------------------------------------------------------------------
# 场景 + 视图
# ---------------------------------------------------------------------------
class EditorScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(-260, -500, 1600, 1000)
        self.sources = {}      # cid -> SourceNode
        self.targets = {}      # cid -> TargetNode
        self.conns = []        # list[ConnLine]
        self._pending_port = None   # 待连的第一根接线柱

    def add_source(self, node):
        self.sources[node.cid] = node
        self.addItem(node)
        for p in node.ports():
            p.clicked.connect(self._on_port_clicked)
        node.edit_requested.connect(self.edit_source)
        node.del_requested.connect(self.delete_card)

    def add_target(self, node):
        self.targets[node.cid] = node
        self.addItem(node)
        for p in node.ports():
            p.clicked.connect(self._on_port_clicked)
        node.edit_requested.connect(self.edit_target)
        node.menu_requested.connect(self._on_target_menu)
        node.del_requested.connect(self.delete_card)

    # ---- 连线: 任意卡片的【出】+ 另一张卡片的【入】, 点两下自动吸附 ----
    def _on_port_clicked(self, port):
        if self._pending_port is port:          # 再点自己一下 = 取消
            self._set_pending(None)
            return
        if self._pending_port is None:
            self._set_pending(port)
            return
        a, b = self._pending_port, port
        self._set_pending(None)
        if a.side == "out" and b.side == "in":
            parent, child = a.owner, b.owner
        elif a.side == "in" and b.side == "out":
            parent, child = b.owner, a.owner
        else:
            self._hint("连线要一【出】配一【入】: 左边的圆点是入, 右边的是出")
            return
        if parent is child:
            self._hint("不能把卡片接到它自己身上")
            return
        # ★ 方向按"离来源的级数"自动定: 越靠近来源的卡是上一级(文件夹在外层)。
        #   两张同级时保留你点的先后(先点的【出】那张作上一级)。
        lp, lc = self.level(parent), self.level(child)
        if lp is None and lc is not None:
            parent, child = child, parent
            lp, lc = lc, lp
        elif lp is not None and lc is not None and lc < lp:
            parent, child = child, parent
            lp, lc = lc, lp
        if self._creates_cycle(parent, child):
            self._hint("不能成环: %s 已经在 %s 的下游了" % (parent.name, child.name))
            return
        self._connect(parent, child)
        lv = lambda x: ("未接入" if x is None else "第%d级" % x)
        self._hint("已连线 %s(%s) → %s(%s)%s" % (
            parent.name, lv(lp), child.name, lv(lc),
            "  同级, 按你点的先后定方向" if (lp is not None and lp == lc) else ""))
        if not self.is_leaf(parent):
            self._hint("%s 现在只作一层文件夹, 不再落盘(它下面的末端卡才落盘)" % parent.name)

    def _set_pending(self, port):
        for c in list(self.sources.values()) + list(self.targets.values()):
            for p in c.ports():
                p.pending = (p is port)
                p.update()
        self._pending_port = port
        if port is not None:
            self._hint("已选中「%s」的【%s】接线柱 —— 再点另一张卡的【%s】即可连线" % (
                getattr(port.owner, "name", "?"), "出" if port.side == "out" else "入",
                "入" if port.side == "out" else "出"))

    def _hint(self, text):
        self.on_wire_hint(text) if hasattr(self, "on_wire_hint") else None

    def _upstream(self, card):
        for c in self.conns:
            if c.tgt is card:
                return c.src
        return None

    def downstream(self, card):
        return [c.tgt for c in self.conns if c.src is card]

    def is_leaf(self, card):
        """末端卡(没有下一级) —— 只有末端卡会真的落盘。"""
        return not self.downstream(card)

    def level(self, card):
        """离来源的级数: 来源=0, 直接接在来源上=1, 再下一级=2 …; 没接入任何来源 = None。
        ★ 级数只由"离源多远"决定 —— 同一级的卡就是同一层文件夹。"""
        d, n, seen = 0, card, set()
        while n is not None and id(n) not in seen:
            if isinstance(n, SourceNode):
                return d
            seen.add(id(n))
            n = self._upstream(n)
            if n is None:
                return None
            d += 1
        return None

    def leaves_below(self, card):
        """该卡片子树里的所有末端卡(含它自己)。整条链的连线靠这个一起发光。"""
        if self.is_leaf(card):
            return [card]
        out = []
        for ch in self.downstream(card):
            out.extend(self.leaves_below(ch))
        return out

    def _creates_cycle(self, parent, child):
        n = parent
        while n is not None:
            if n is child:
                return True
            n = self._upstream(n)
        return False

    def chain_for(self, leaf):
        """沿链向上解析: 返回 (输出根目录, 层级名元组, 链根来源卡)。
        层级名 = 从输出根下面第一级一直到 leaf 的名字, 例如 ("无人机", "视频")。
        某张卡自己指定了输出位置时, 就以它为新的根(它上面的层级不再参与)。"""
        segs, n, root, src = [], leaf, None, None
        while n is not None:
            s = n.segment() if hasattr(n, "segment") else ""
            if s:
                segs.append(s)
            if isinstance(n, SourceNode):
                src = n
            if getattr(n, "out_base", None):
                root = n.out_base
                break
            n = self._upstream(n)
        segs.reverse()
        return root, tuple(segs), src

    def _connect(self, parent, child):
        if any(c.src is parent and c.tgt is child for c in self.conns):
            return
        self.disconnect_target(child)      # 入只能有一个上一级: 重新接 = 换上一级
        c = ConnLine(parent, child)
        c.del_requested.connect(self._remove_conn)
        c.menu_requested.connect(self._on_line_menu)
        self.conns.append(c)
        self.addItem(c)
        child.src_tid = parent.cid
        parent.update()
        child.update()

    def _remove_conn(self, conn):
        """删掉一条连线(卡片保留)。"""
        if conn in self.conns:
            self.conns.remove(conn)
            self.removeItem(conn)
            conn.tgt.update()

    def _on_line_menu(self, conn, pos):
        self.on_line_menu(conn, pos) if hasattr(self, "on_line_menu") else None

    def refresh_links(self, node=None):
        """卡片移动后重算受影响的连线。不重算 = 线不跟着走 + 旧线像素残留(重影)。"""
        for c in self.conns:
            if node is None or c.src is node or c.tgt is node:
                c._recalc()

    def clear_all(self):
        """清空工作区(卡片 + 连线)并把字典一起清干净。
        ★ 不要直接用 QGraphicsScene.clear(): 它只删 C++ 对象, sources/targets/conns
        里会留下悬空引用, 之后一碰就是 "wrapped C/C++ object has been deleted"。"""
        for c in list(self.conns):
            self.removeItem(c)
        for n in list(self.sources.values()) + list(self.targets.values()):
            self.removeItem(n)
        self.conns.clear()
        self.sources.clear()
        self.targets.clear()
        self._pending_port = None

    def disconnect_target(self, tgt):
        for c in [c for c in self.conns if c.tgt is tgt]:
            self.removeItem(c)
            self.conns.remove(c)

    def delete_card(self, card):
        """删除卡片(来源卡/输出卡/文件夹卡通用): 连带删掉挂在它身上的连线。"""
        if self._pending_port is not None and self._pending_port.owner is card:
            self._set_pending(None)
        for c in [c for c in self.conns if c.src is card or c.tgt is card]:
            self.removeItem(c)
            self.conns.remove(c)
        self.removeItem(card)
        self.sources.pop(getattr(card, "cid", None), None)
        self.targets.pop(getattr(card, "cid", None), None)

    # ---- 卡片编辑回调(由窗口提供对话框) ----
    def edit_source(self, node):
        self.on_edit_source(node) if hasattr(self, "on_edit_source") else None

    def edit_target(self, node):
        self.on_edit_target(node) if hasattr(self, "on_edit_target") else None

    def _on_target_menu(self, node, pos):
        self.on_target_menu(node, pos) if hasattr(self, "on_target_menu") else None

    # ---- 一键整理: 来源在左, 同一级(离源一样近)的卡排在同一列 ----
    def arrange(self):
        srcs = list(self.sources.values())
        cx, cy = 0, 0
        for i, s in enumerate(srcs):
            s.setPos(cx, cy + (i - (len(srcs) - 1) / 2) * 120)
            s.set_geom()
        col_x0 = cx + _CARD_W + 170
        col_step = _CARD_W + 90
        by_level, orphans = {}, []
        for t in self.targets.values():
            lv = self.level(t)
            if lv is None:
                orphans.append(t)          # 未接入来源卡: 单独放到来源左边
            else:
                by_level.setdefault(lv, []).append(t)
        for lv in sorted(by_level):
            col = by_level[lv]
            x = col_x0 + (lv - 1) * col_step
            for i, t in enumerate(col):
                t.setPos(x, cy + (i - (len(col) - 1) / 2) * 92)
        for i, t in enumerate(orphans):
            t.setPos(cx - _CARD_W - 170, cy + (i - (len(orphans) - 1) / 2) * 92)
        for c in self.conns:
            c._recalc(); c.update()
        self._fit()

    def _fit(self):
        r = self.itemsBoundingRect().adjusted(-120, -120, 120, 120)
        if r.isNull() or r.width() < 80:
            r = QRectF(-300, -300, 1200, 600)
        self.setSceneRect(r)


class EditorView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#0f0f10"))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._enable_gpu()
        self._apply_update_mode()

    def _apply_update_mode(self):
        """★ GL viewport 必须整屏刷新。Qt 官方文档(viewportUpdateMode):
        "FullViewportUpdate ... is the preferred update mode for viewports that do not
        support partial updates, such as QOpenGLWidget"。用局部刷新 +
        QOpenGLWidget 就会出现"卡片移动后留重影"。"""
        gl = hasattr(self.viewport(), "grabFramebuffer")     # QOpenGLWidget 的特征方法
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate if gl
            else QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)

    def _enable_gpu(self):
        # 默认启用 GPU(OpenGL)渲染。若显卡/驱动交不出画面(黑屏), 由 _gpu_probe 自动回退;
        # 也可手工兜底: exe 旁放空文件 MediaOrganizer.raster 或设 MO_RASTER=1。
        # --shot 出图强制走软件渲染: QWidget.grab() 抓不到 QOpenGLWidget 的内容。
        if os.environ.get("MO_RASTER") or screenshot_mode() \
                or os.path.exists(os.path.join(_base_dir(), "MediaOrganizer.raster")):
            return
        try:
            from PyQt6.QtOpenGLWidgets import QOpenGLWidget
            self.setViewport(QOpenGLWidget())
        except Exception:
            pass

    def wheelEvent(self, e):
        self._user_zoomed = True        # 用户自己缩放过, 之后 resize 不再自动重排
        factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        e.accept()

    def resizeEvent(self, e):
        """侧栏展开/收起会改变 view 宽度: 没手动缩放过就重新适配一次, 免得内容被裁掉。"""
        super().resizeEvent(e)
        if not getattr(self, "_user_zoomed", False):
            sc = self.scene()
            if sc is not None:
                self.fitInView(sc.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, e):
        # 点空白处 = 取消正在等待配对的接线柱
        if e.button() == Qt.MouseButton.LeftButton and self.itemAt(e.pos()) is None:
            sc = self.scene()
            if sc is not None and hasattr(sc, "_set_pending"):
                sc._set_pending(None)
        super().mousePressEvent(e)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QColor("#0f0f10"))
        # 淡网格(真实 drawLine: 比"密排小圆点"便宜几十倍图元)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 10), 1))
        step = 48
        x0 = int(rect.left() // step) * step
        y0 = int(rect.top() // step) * step
        y = y0
        while y < rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step
        x = x0
        while x < rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        # 空舞台引导(默认入口是编辑器, 新用户需要知道怎么开始)
        try:
            sc = self.scene()
            if not (sc.sources or sc.targets):
                painter.setPen(QPen(QColor(126, 128, 138), 1))
                f = painter.font(); f.setPointSize(11); painter.setFont(f)
                painter.drawText(QRectF(rect.left(), rect.top() + rect.height() * 0.42,
                                        rect.width(), 40),
                                 Qt.AlignmentFlag.AlignCenter,
                                 "① 添加来源卡  ② 添加输出卡  ③ 点两个接线柱自动连线  ④ 点【运行工作流】")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 构建任务线程(扫描来源目录生成各输出卡的待归档计划)
# ---------------------------------------------------------------------------
class TaskBuilder(QThread):
    ready = pyqtSignal(list)   # [(task_id, total)]

    def __init__(self, tasks, parent=None):
        super().__init__(parent)
        self.tasks = tasks

    def run(self):
        for t in self.tasks:
            try:
                t.build_jobs(None)
            except Exception:
                t.jobs = []; t.total = 0
        self.ready.emit([(t.tid, t.total) for t in self.tasks])


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class EditorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("素材整理工作流 — 多任务工作流编辑器")
        self.resize(1320, 860)
        self.setMinimumSize(1020, 680)
        self.scene = EditorScene(self)
        self.view = EditorView(self.scene, self)
        self.scheduler = TaskScheduler(self)
        self.scheduler.job_start.connect(self._on_job_start)
        self.scheduler.job_prog.connect(self._on_job_prog)
        self.scheduler.job_done.connect(self._on_job_done)
        self.scheduler.all_done.connect(self._on_all_done)
        self.builder = None
        self.running = False
        self.paused = False
        self._seq = 0
        self._prog_buf = {}
        self._last_prog_log = {}
        self._frame_done = False
        self._build_toolbar()
        self._build_sidebar()
        self._build_central()
        self._build_log()
        self._light_timer = QTimer(self); self._light_timer.setInterval(30)
        self._light_timer.timeout.connect(self._tick_lights)
        # 动画定时器仅在运行整理时启动, 空闲/结束后不空转重绘(降低 CPU/内存)
        self._light_on = False
        self._add_demo_sources = False

    # ---------------- 界面 ----------------
    def _build_toolbar(self):
        tb = self.addToolBar("运行")
        tb.setMovable(False)
        tb.setStyleSheet("background:#141416; border:none; padding:4px;")
        self.run_btn = QPushButton("▶  运行工作流")
        self.run_btn.setStyleSheet(self._btn("#5e6ad2"))
        self.run_btn.clicked.connect(self._on_run_click)
        tb.addWidget(self.run_btn)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine); sep.setStyleSheet("color:#2a2a2d;"); tb.addWidget(sep)
        gl = QLabel("全局精度"); gl.setStyleSheet("color:#8b8b90; font-size:12px;"); tb.addWidget(gl)
        self.gran = QComboBox(); self.gran.addItems(GRANS); self.gran.setCurrentText(GRAN_DAY)
        self.gran.setStyleSheet("QComboBox{background:#1c1c1f;color:#ededed;border:1px solid rgba(255,255,255,0.10);"
                                "border-radius:8px;padding:5px 10px;font-size:12px;}"
                                "QComboBox QAbstractItemView{background:#1a1a1d;color:#ededed;selection-background-color:#333;}")
        tb.addWidget(self.gran)
        self.gran_label = QLabel("(默认到日, 输出卡可个别微调)"); self.gran_label.setStyleSheet("color:#55555a; font-size:11px;"); tb.addWidget(self.gran_label)
        tb.addWidget(sep)
        wl = QLabel("方式"); wl.setStyleSheet("color:#8b8b90; font-size:12px;"); tb.addWidget(wl)
        self.mode_move = QRadioButton("移动"); self.mode_copy = QRadioButton("复制"); self.mode_copy.setChecked(True)
        for rb in (self.mode_move, self.mode_copy): rb.setStyleSheet("color:#c9c9cc; font-size:12px;")
        tb.addWidget(self.mode_move); tb.addWidget(self.mode_copy)
        tb.addSeparator()
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0)
        self.progress.setFormat("总进度 %p%"); self.progress.setFixedWidth(200)
        self.progress.setStyleSheet("QProgressBar{background:#1a1a1d;border:none;border-radius:4px;height:8px;}"
                                    "QProgressBar::chunk{background:#5e6ad2;border-radius:4px;}")
        tb.addWidget(self.progress)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

    @staticmethod
    def _btn(color):
        return (f"QPushButton{{background:{color};color:white;font-weight:bold;border-radius:8px;"
                f"padding:8px 18px;font-size:13px;}}QPushButton:hover{{background:#707aeb;}}")

    def _build_central(self):
        container = QWidget(); cv = QHBoxLayout(container); cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(0)
        cv.addWidget(self.view, 1)
        cv.addWidget(self.sidebar)
        self.setCentralWidget(container)

    def _build_sidebar(self):
        # 侧边栏: 收起只显图标, 鼠标移入展开显示完整内容
        self.sidebar = QWidget(); self.sidebar.setFixedWidth(52)
        self.sidebar.setStyleSheet("QWidget{background:#141416;border-left:1px solid rgba(255,255,255,0.06);}")
        self._sb_items = [
            ("➕", "添加来源卡", self._add_source_flow),
            ("📦", "添加输出卡", self._add_target_flow),
            ("📁", "添加文件夹卡", self._add_custom_card),
            ("🧹", "一键整理", self.scene.arrange),
            ("🔄", "一键重置", self._reset_all),
        ]
        lay = QVBoxLayout(self.sidebar); lay.setContentsMargins(6, 10, 6, 10); lay.setSpacing(8)
        self._sb_btns = []
        for icon, text, fn in self._sb_items:
            b = QToolButton(); b.setText(icon); b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setFixedHeight(34); b.setStyleSheet("QToolButton{background:#1c1c1f;color:#ededed;border-radius:8px;"
                                                  "font-size:13px;}QToolButton:hover{background:#242427;color:#5e6ad2;}")
            b.clicked.connect(fn)
            b._text = text
            lay.addWidget(b); self._sb_btns.append(b)
        lay.addStretch(1)
        self.sidebar._expanded = False

    def _set_sidebar(self, expand):
        self.sidebar.setFixedWidth(220 if expand else 52)
        self.sidebar._expanded = expand
        for b, (icon, text, _) in zip(self._sb_btns, self._sb_items):
            b.setText(text if expand else icon)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly if expand else Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setToolTip(text if not expand else "")

    def enterEvent(self, e):
        self._set_sidebar(True); super().enterEvent(e)

    def leaveEvent(self, e):
        # ★ 曾经的闪退根因就在这一行: Qt6 的 leaveEvent 收到的是裸 QEvent, 没有 pos()。
        # 旧写法 `e.pos()` -> AttributeError -> 逃出 Qt 回调 -> PyQt6 qFatal -> abort,
        # 进程以 0xC0000409 静默消失(用户机器一移鼠标出窗口就"爆炸", 开发机因不动鼠标故无法复现)。
        # 现在用 mapFromGlobal(QCursor.pos()) 取真实光标位置, 并且整段兜底不让异常外泄。
        try:
            r = self.sidebar.rect().translated(self.sidebar.mapTo(self, self.sidebar.pos()))
            if not r.contains(self.mapFromGlobal(QCursor.pos())):
                self._set_sidebar(False)
        except Exception as ex:
            log_s("leaveEvent 内部异常(已忽略): %r" % (ex,))
        super().leaveEvent(e)

    def _build_log(self):
        dock = QDockWidget("运行日志", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet("QTextEdit{background:#131315;color:#c9c9cc;border:none;font-family:Consolas;font-size:12px;}")
        self.log_box.setMinimumHeight(120)
        dock.setWidget(self.log_box)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    # ---------------- 添加卡片 ----------------
    def _add_source_flow(self):
        """选来源文件夹 + 总输出文件夹, 然后自动建好并连好 照片/视频/其他 三张输出卡。"""
        fd = QFileDialog.getExistingDirectory(self, "① 选择素材来源文件夹", os.path.expanduser("~"))
        if not fd:
            return
        out_base = QFileDialog.getExistingDirectory(self, "② 选择输出文件夹(会自动建好 照片/视频/其他 三类)",
                                                    fd)
        if not out_base:
            return
        self._seq += 1
        cid = "s%d" % self._seq
        # 不再弹"自定义名称"对话框: 名称直接用文件夹名, 想改名双击卡片即可
        name = os.path.basename(fd.rstrip("\\/")) or fd
        src = SourceNode(cid, name, fd, self.gran.currentText(), out_base=out_base)
        self.scene.add_source(src)
        self._log("➕ 已添加素材来源: %s → %s  (输出根目录: %s)" % (name, fd, out_base))
        # 自动补全三类输出卡并连线(输出位置跟随来源卡根目录; 之后可逐张改/删, 也可再接自定义卡)
        for cat in CATS:
            self._seq += 1
            t = TargetNode("t%d" % self._seq, cat, cid, ("cat", cat),
                           self.gran.currentText(), None)
            self.scene.add_target(t)
            self.scene._connect(src, t)
        self._log("📦 已自动生成并连线 3 张末端输出卡: %s ⇒ %s/<类别>/<日期>/" %
                  (" / ".join(CATS), os.path.basename(out_base.rstrip("\\/")) or out_base))
        self.scene.arrange()
        self._frame_once()

    def _add_custom_card(self):
        """文件夹卡: 纯目录层级 —— 只有名字, 不筛选、不落盘。
        相当于在链路两层之间夹一层文件夹; 要收文件必须在它后面再接一张输出卡。"""
        if not self.scene.sources:
            QMessageBox.information(self, "提示", "请先添加素材来源卡, 再添加文件夹卡")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("添加文件夹卡(只作一层目录)")
        form = QFormLayout(dlg)
        name = QLineEdit("我的素材")
        name.selectAll(); name.setFocus()
        form.addRow("文件夹名", name)
        src_cb = QComboBox()
        for cid, s in self.scene.sources.items():
            src_cb.addItem(s.name, cid)
        if len(self.scene.sources) > 1:
            form.addRow("接在哪个来源后面", src_cb)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(btns)
        btns.accepted.connect(dlg.accept); btns.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        nm = name.text().strip()
        if not nm:
            QMessageBox.warning(self, "提示", "文件夹名不能为空")
            return
        src_id = src_cb.currentData() or next(iter(self.scene.sources))
        parent = self.scene.sources[src_id]
        self._seq += 1
        node = FolderNode("f%d" % self._seq, nm)
        self.scene.add_target(node)
        self.scene._connect(parent, node)
        self._log("📁 已添加文件夹卡「%s」(接在「%s」后面, 第%s级) —— 它只作目录层级, "
                  "在它后面再接一张输出卡才会收文件" % (nm, parent.name, self.scene.level(node)))
        self.scene.arrange()
        self._frame_once()

    def _add_target_flow(self):
        """输出卡: 负责筛选 + 落盘。作为链路末端时按自己的筛选把文件收进当前链路目录。"""
        if not self.scene.sources:
            QMessageBox.information(self, "提示", "请先添加素材来源卡, 再添加输出卡")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("添加输出卡")
        form = QFormLayout(dlg)
        name = QLineEdit("输出"); form.addRow("名称", name)
        src_cb = QComboBox()
        for cid, s in self.scene.sources.items():
            src_cb.addItem(s.name, cid)
        form.addRow("素材来源", src_cb)
        filt = QComboBox()
        for c in CATS:
            filt.addItem(c, ("cat", c))
        filt.addItem("自定义后缀", ("exts", None))
        form.addRow("筛选", filt)
        ext = QLineEdit(".dng, .arw"); form.addRow("自定义后缀(可多个, 逗号分隔)", ext)
        gran_cb = QComboBox(); gran_cb.addItems(GRANS); gran_cb.setCurrentText(self.gran.currentText())
        form.addRow("归档精度", gran_cb)
        con = QCheckBox("单独指定输出位置(默认跟随来源卡的输出根目录)")
        form.addRow("输出位置", con)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(btns)
        btns.accepted.connect(dlg.accept); btns.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        src_id = src_cb.currentData()
        kind, val = filt.currentData()
        if kind == "exts":
            raw = [x.strip().lower() for x in ext.text().replace(";", ",").replace(" ", ",").split(",")
                   if x.strip()]
            raw = [x if x.startswith(".") else "." + x for x in raw]
            if not raw:
                QMessageBox.warning(self, "提示", "请填写至少一个后缀, 例如 .dng, .arw")
                return
            filter_spec = ("exts", tuple(raw))
        else:
            filter_spec = ("cat", val)
        out_base = None
        if con.isChecked():
            default_out = next((getattr(s, "out_base", None) for s in self.scene.sources.values()
                                if getattr(s, "out_base", None)), os.path.expanduser("~"))
            out_base = QFileDialog.getExistingDirectory(self, "选择这张卡单独的输出位置", default_out)
            if not out_base:
                return
        self._seq += 1
        node = TargetNode("t%d" % self._seq, name.text().strip() or "输出",
                          src_id, filter_spec, gran_cb.currentText(), out_base)
        self.scene.add_target(node)
        self.scene._connect(self.scene.sources[src_id], node)     # 自动连线(可悬停 ✕ 删除)
        self._log("📦 已添加输出卡: %s (来源:%s, 筛选:%s, 精度:%s)%s" %
                  (node.name, self.scene.sources[src_id].name, node.filter_label(),
                   node.gran, "  [单独输出位置]" if out_base else ""))
        self.scene.arrange()
        self._frame_once()

    def _frame_once(self):
        QTimer.singleShot(0, lambda: self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio))

    # 卡片编辑(由场景回调)
    def on_edit_source(self, node):
        """双击来源卡: 改素材文件夹, 顺便可以改这条链的输出根目录。"""
        fd = QFileDialog.getExistingDirectory(self, "选择素材来源文件夹", node.folder)
        if fd:
            node.folder = fd
            node.update()
            self._log("✏️ 来源已改: %s" % fd)
        cur = node.out_base or os.path.expanduser("~")
        out = QFileDialog.getExistingDirectory(self, "选择输出根目录(取消则保持不变)——下游各卡的名字逐级拼在它下面",
                                               cur)
        if out:
            node.out_base = out
            node.update()
            self._log("✏️ 输出根目录已改: %s" % out)

    def on_edit_target(self, node):
        if isinstance(node, FolderNode):
            self._rename_dir(node)
            return
        out = QFileDialog.getExistingDirectory(self, "选择这张卡单独的输出位置(取消则保持不变)",
                                               node.out_base or os.path.expanduser("~"))
        if out:
            node.out_base = out
            node.update()
            self._log("✏️ %s 改为单独输出位置: %s (它上面的层级不再参与)" % (node.name, out))

    def on_wire_hint(self, text):
        """接线柱/连线操作的即时反馈 —— 之前点第一下毫无反应, 所以感觉"接不上"。"""
        self._log("🔌 " + text)

    def on_target_menu(self, node, pos):
        m = QMenu(self)
        if isinstance(node, FolderNode):
            m.addAction("改文件夹名…", lambda: self._rename_dir(node))
            m.addAction("删除文件夹卡", lambda: self.scene.delete_card(node))
        else:
            m.addAction("加急" if not node.urgent else "取消加急", lambda: self._toggle_urgent(node))
            m.addAction("删除输出卡", lambda: self.scene.delete_card(node))
        m.exec(self.view.mapToGlobal(self.view.mapFromScene(pos)))

    def _rename_dir(self, node):
        t, ok = QInputDialog.getText(self, "文件夹名", "输出目录里的这一层叫什么:",
                                     text=node.dir_name)
        if ok and t.strip():
            node.dir_name = t.strip()
            node.name = node.dir_name
            node.update()
            self._log("✏️ 文件夹卡已改名: %s (第%s级)" % (node.dir_name, self.scene.level(node)))
            self.scene.arrange()

    def on_line_menu(self, conn, pos):
        """连线右键菜单(悬停中点也有 ✕ 可以直接删)。"""
        m = QMenu(self)
        m.addAction("删除这条连线", lambda: self.scene._remove_conn(conn))
        m.addAction("删除下游卡片", lambda: self.scene.delete_card(conn.tgt))
        m.exec(self.view.mapToGlobal(self.view.mapFromScene(pos)))

    def _toggle_urgent(self, node):
        node.urgent = not node.urgent
        node.update()
        tid = node.task_id
        if tid is not None and tid in self.scheduler._tasks:
            self.scheduler._tasks[tid].urgent = node.urgent
        self._log(("⚡ 加急输出卡: %s (其他任务让路)" % node.name) if node.urgent else ("↩ 取消加急: %s" % node.name))

    def _reset_all(self):
        self.scene.clear_all()
        self._seq = 0
        self.progress.setValue(0)
        self.log_box.clear()
        self.run_btn.setText("▶  运行工作流")
        self.run_btn.setStyleSheet(self._btn("#5e6ad2"))
        self._log("🔄 已重置工作区")

    # ---------------- 运行 / 暂停 / 中断 ----------------
    def _on_run_click(self):
        if self.running:
            self._confirm_pause(); return
        self._start_run()

    def _start_run(self):
        # 只有"输出卡"能落盘; 文件夹卡即使接在最末端也不收文件
        leaves = [t for t in self.scene.targets.values()
                  if self.scene.is_leaf(t) and isinstance(t, TargetNode)]
        folder_leaves = [t for t in self.scene.targets.values()
                         if self.scene.is_leaf(t) and isinstance(t, FolderNode)]
        if folder_leaves:
            self._log("📁 文件夹卡 %s 后面还没接输出卡, 它们只作目录层级、不会收文件" %
                      ", ".join("「%s」" % x.dir_name for x in folder_leaves))
        if not leaves:
            QMessageBox.warning(self, "提示",
                                "没有可落盘的输出卡:\n从来源卡(或文件夹卡)的【出】接线柱接出一张输出卡")
            return
        plan = []
        skipped = []
        for tgt in leaves:
            root, segs, src = self.scene.chain_for(tgt)
            if src is None or not root:
                skipped.append(tgt.name)      # 未接入来源卡 / 没输出根: 跳过, 不阻塞整轮
                continue
            plan.append((tgt, src, root, segs))
        if skipped:
            self._log("⚠ 已跳过没接入来源卡的卡片: %s (把连线接上去, 或删掉它)" % ", ".join(skipped))
        if not plan:
            QMessageBox.warning(self, "提示",
                                "没有可落盘的输出卡:\n从来源卡(或文件夹卡)的【出】接线柱接出一张输出卡")
            return
        move = self.mode_move.isChecked()
        # 自定义后缀的输出卡独占这些后缀: 类别卡不再重复收(避免同一文件拷两份)
        exclusive = set()
        for tgt, _, _, _ in plan:
            k, v = tgt.filter
            if k == "exts":
                exclusive.update(x.lower() for x in v)
            elif k == "ext":
                exclusive.add(str(v).lower())
        if exclusive:
            self._log("🗂 自定义后缀独占: %s (照片/视频/其他 不再重复收它们)" % " ".join(sorted(exclusive)))
        tasks = []
        seen_filter = {}
        for i, (tgt, src, root, segs) in enumerate(plan, 1):
            if tgt.filter in seen_filter:
                self._log("⚠ 输出卡「%s」与「%s」筛选完全相同 —— 同一批文件会各拷一份, 磁盘会翻倍"
                          % (tgt.name, seen_filter[tgt.filter]))
            else:
                seen_filter[tgt.filter] = tgt.name
            tid = tgt.task_id = i
            own_ext = tgt.filter[0] in ("ext", "exts")
            t = EditorTask(tid, src.folder, tgt.gran or self.gran.currentText(),
                           tgt.filter, root, move, segments=segs,
                           exclusive_exts=(() if own_ext else tuple(sorted(exclusive))))
            t.urgent = tgt.urgent
            tasks.append(t)
            self.scheduler.register(t)
            tgt.total = 0; tgt.count = 0; tgt.update()
            self._log("🔗 链路: %s → %s  ⇒  %s" % (
                src.name, " → ".join(list(segs)), "/".join([os.path.basename(root.rstrip("\\/"))] + list(segs))))
        self._log("▶ 启动工作流: %d 张末端输出卡, 最多 %d 路并发传输" % (len(tasks), CONCURRENCY))
        self.running = True
        if not self._light_timer.isActive():
            self._light_timer.start()
        self._light_on = True
        self.run_btn.setText("⏸  暂停 / 中断")
        self.run_btn.setStyleSheet(self._btn("#ef4444"))
        self.builder = TaskBuilder(tasks, self)
        self.builder.ready.connect(self._on_build_ready)
        self.builder.start()

    def _on_build_ready(self, info):
        for tid, total in info:
            node = next((t for t in self.scene.targets.values() if t.task_id == tid), None)
            if node is not None:
                node.total = total; node.update()
        total_all = sum(t for _, t in info)
        self._log("✔ 扫描完成: 共 %d 个文件待归档" % total_all)

    def _confirm_pause(self):
        mb = QMessageBox(self)
        mb.setWindowTitle("确认操作")
        mb.setText("运行中, 请选择下一步:")
        b_pause = mb.addButton("暂停任务", QMessageBox.ButtonRole.ActionRole)
        b_stop = mb.addButton("中断任务", QMessageBox.ButtonRole.ActionRole)
        b_resume = mb.addButton("点错了, 继续运行", QMessageBox.ButtonRole.RejectRole)
        mb.exec()
        clicked = mb.clickedButton()
        if clicked is b_resume:
            return
        if clicked is b_stop:
            self._stop_run(); return
        self._pause_run()

    def _pause_run(self):
        self.paused = True
        with self.scheduler._lock:
            for t in self.scheduler._tasks.values():
                t.paused = True
        self.run_btn.setText("▶  继续")
        self.run_btn.setStyleSheet(self._btn("#f59e0b"))
        self._log("⏸ 已暂停 (所有任务暂停, 当前传输的文件会完成)")

    def _resume_run(self):
        self.paused = False
        with self.scheduler._lock:
            for t in self.scheduler._tasks.values():
                t.paused = False
        self.run_btn.setText("⏸  暂停 / 中断")
        self.run_btn.setStyleSheet(self._btn("#ef4444"))
        self._log("▶ 已恢复")

    def _stop_run(self):
        self.scheduler.shutdown()
        if self.scheduler._workers:
            for w in self.scheduler._workers:
                w.wait(2000)
        self.running = False
        self._light_timer.stop(); self._light_on = False
        self.run_btn.setText("▶  运行工作流")
        self.run_btn.setStyleSheet(self._btn("#5e6ad2"))
        self.progress.setValue(0)
        self._log("⛔ 已中断")

    # ---------------- 调度信号 -> UI ----------------
    def _on_job_start(self, tid, path, size):
        self._prog_buf[path] = 0.0
        self._last_prog_log[path] = 0.0
        self._log("▶ 正在拷贝 %s (%s)…" % (os.path.basename(path), _fmt_size(size)))

    def _on_job_prog(self, tid, path, frac):
        self._prog_buf[path] = frac
        # 节流: 每 20% 打一次日志百分比
        cur = int(frac * 100 // 20) * 20
        if self._last_prog_log.get(path, -1) != cur:
            self._last_prog_log[path] = cur
            self._log("   %s ... %d%%" % (os.path.basename(path), int(frac * 100)))

    def _on_job_done(self, tid, path, ok):
        node = next((t for t in self.scene.targets.values() if t.task_id == tid), None)
        if node is not None:
            t = self.scheduler._tasks.get(tid)
            if t is not None:
                node.count = t.written; node.update()
        if ok:
            self._log("  ✔ %s → 完成" % os.path.basename(path))
        else:
            self._log("  ✖ 失败 %s" % os.path.basename(path))

    def _on_all_done(self):
        self.running = False
        self._light_timer.stop(); self._light_on = False
        self.progress.setValue(100)
        self.run_btn.setText("▶  运行工作流")
        self.run_btn.setStyleSheet(self._btn("#5e6ad2"))
        self._log("🎉 全部任务完成!")

    def _tick_lights(self):
        # 驱动连线光点 + 汇总进度 + 卡片计数
        if not self.running:
            for c in self.scene.conns:
                if c.active:
                    c.active = False; c.update()
            return
        # 进度只按末端卡汇总一次
        total_w = total_t = 0
        for lf in self.scene.targets.values():
            if not self.scene.is_leaf(lf):
                continue
            t = self.scheduler._tasks.get(lf.task_id)
            if t is None:
                continue
            total_w += t.written; total_t += t.total
            if lf.count != t.written:
                lf.count = t.written; lf.update()
        if total_t:
            self.progress.setValue(int(total_w * 100 / total_t))
        # ★ 连线是否发光, 取决于"它下游有没有正在传输的末端卡"。
        #   旧写法只看这条线直接指向的卡有没有 task —— 链路上的中间卡没有 task,
        #   于是 源→中间卡 那一段永远不亮, 只有最后一段有动画。
        for c in self.scene.conns:
            act = False
            for lf in self.scene.leaves_below(c.tgt):
                t = self.scheduler._tasks.get(lf.task_id)
                if t is not None and t.written < t.total and not t.paused and not self.paused:
                    act = True
                    break
            c.active = act
            if act:
                c.light = (c.light + 0.02) % 1.0
            c.update()

    def _log(self, text):
        self.log_box.append(text)

    def _frame(self):
        if self._frame_done:
            return
        self._frame_done = True
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._frame)
        if not getattr(self, "_gpu_probed", False):
            self._gpu_probed = True
            # 等窗口真正显示、GL 上下文建立后再检测, 避免误判
            QTimer.singleShot(150, self._gpu_probe)

    def _gpu_probe(self):
        """GPU(OpenGL) 自检, 与稳定版同一套: 交不出画面就自动回退软件渲染。
        编辑器此前缺这段 —— 实测本机 GL 下 grabFramebuffer() 返回全黑帧, 也就是屏幕上
        画布其实是黑的, 而截图因 grab() 抓不到 GL 又显示纯白, 两头都会骗人。"""
        try:
            vp = self.view.viewport()
            if hasattr(vp, "grabFramebuffer"):
                img = vp.grabFramebuffer()
                bad, info = img.isNull(), "grabFramebuffer 返回空帧"
                if not bad:
                    small = img.scaled(48, 30)
                    ls = [small.pixelColor(x, y).lightness()
                          for y in range(small.height()) for x in range(small.width())]
                    avg = sum(ls) / max(1, len(ls))
                    spread = max(ls) - min(ls)
                    info = "avg=%.1f spread=%d" % (avg, spread)
                    bad = avg < 12 or spread < 8
                log_s("编辑器 GPU 探测: %s -> %s" % (info, "回退软件渲染" if bad else "GPU 正常"))
                if bad:
                    self.view.setViewport(QWidget())
                    self.view._apply_update_mode()
                    self.view.update()
        except Exception as ex:
            log_s("编辑器 GPU 探测异常(已忽略): %r" % (ex,))

    # ---------------- demo(验证用) ----------------
    def demo(self):
        import tempfile
        self._in_demo = True
        tmp = tempfile.mkdtemp(prefix="wf_demo_")
        src = os.path.join(tmp, "src"); os.makedirs(src)
        try:
            from PIL import Image
        except Exception:
            Image = None
        for i in range(3):
            if Image:
                Image.new("RGB", (8, 8), (30, 60, 90)).save(os.path.join(src, "p%d.jpg" % i))
            else:
                open(os.path.join(src, "p%d.jpg" % i), "wb").write(b"\xff" * 8)
        for i in range(2):
            open(os.path.join(src, "v%d.mp4" % i), "wb").write(b"\x00" * 16)
        open(os.path.join(src, "d.txt"), "w").write("x")
        s = SourceNode("s1", "demo来源", src, self.gran.currentText())
        self.scene.add_source(s)
        for cid, name, filt in (("t1", "照片", ("cat", CAT_IMAGE)),
                                ("t2", "视频", ("cat", CAT_VIDEO)),
                                ("t3", "其他", ("cat", CAT_OTHER))):
            out = os.path.join(tmp, "out_" + cid); os.makedirs(out, exist_ok=True)
            t = TargetNode(cid, name, "s1", filt, self.gran.currentText(), out)
            self.scene.add_target(t)
            self.scene._connect(s, t)
        self.scene.arrange()
        QTimer.singleShot(0, self._frame)
        QTimer.singleShot(120, self._start_run)

    def closeEvent(self, e):
        try:
            self.scheduler.shutdown()
            for w in self.scheduler._workers:
                w.wait(2000)
        except Exception:
            pass
        super().closeEvent(e)


def run():
    """默认入口: 多卡片可连线工作流编辑器。
    诊断(日志/异常拦截/Qt 消息)统一走 media_organizer 里的公共实现, 两个入口行为一致。"""
    init_diag()
    install_excepthook()
    log_s("== 启动(多卡片工作流编辑器)")
    app = QApplication(sys.argv)
    install_qt_message_handler()
    log_s("QApplication 创建完成")
    app.setApplicationName("素材整理工作流")
    app.setStyleSheet(GLOBAL_QSS)
    log_s("样式已设置")
    try:
        win = EditorWindow()
        log_s("主窗口已创建")
        win.show()
        log_s("主窗口已显示")
        if "--demo" in sys.argv:
            win.demo()
        if "--smoke" in sys.argv:
            QTimer.singleShot(4000, app.quit)
        if "--smoke-leave" in sys.argv:
            install_smoke_leave(app, win)
        if "--shot" in sys.argv:
            try:
                out = sys.argv[sys.argv.index("--shot") + 1]
            except IndexError:
                out = "shot.png"
            delay = 2000 if "--demo" in sys.argv else 1600

            def grab():
                try:
                    win.grab().save(out)
                    log_s("已出图: %s" % out)
                finally:
                    app.quit()
            QTimer.singleShot(delay, grab)
        log_s("进入事件循环")
        app.exec()
        log_s("事件循环退出, 正常结束")
    except Exception as e:
        import traceback
        log_s("异常: %s\n%s" % (e, traceback.format_exc()))
        raise


