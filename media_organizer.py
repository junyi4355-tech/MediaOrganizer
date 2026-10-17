# -*- coding: utf-8 -*-
"""媒体素材整理器 —— 公共基础与旧版界面。

本文件提供:
  * 诊断基础设施: init_diag / log_s / install_excepthook / install_qt_message_handler
    (打包后是无控制台 exe, 一切崩溃证据只能靠 exe 旁的 startup.log 落盘)
  * 归档逻辑: 分类(照片/视频/其他) / 读 EXIF / 日期粒度 / 防重名 / 计划生成 / 落盘
  * 默认入口 main(): 转到 workflow_editor(多卡片工作流编辑器);
    --classic 走本文件的旧版"数据光带工作流"界面(MainWindow / FlowScene / FlowView)
命令行:
    --classic    打开旧版光带工作流界面
    --selftest   运行整理逻辑自检并退出(无界面)
    --smoke      打开窗口 4 秒后自动关闭(打包冒烟)
    --smoke-leave 闪退回归自检(Leave 事件 + 回调抛异常两条路径)
    --demo / --shot <png>  演示数据 / 截图
"""
import os
import sys
import time
import queue
import shutil
import datetime
import traceback

# 锁定 Windows 平台插件(避免 Qt 平台探测异常导致的无窗口退出)
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6.QtCore import (Qt, QRectF, QPointF, pyqtSignal, QThread,
                          QSettings, QTimer, QObject, QEvent)
from PyQt6.QtGui import (QPainter, QColor, QPen, QPainterPath)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QGraphicsView, QGraphicsScene,
                             QGraphicsItem, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QFileDialog, QComboBox, QCheckBox,
                             QRadioButton, QTextEdit, QProgressBar, QDockWidget, QFrame,
                             QMessageBox, QToolButton, QSlider)

# ---------------------------------------------------------------------------
# 诊断基础设施(打包后是无控制台的 exe, 一切证据只能靠 startup.log 落盘)
# ---------------------------------------------------------------------------
LOG_PATH = None          # 诊断日志路径(init_diag 决定)
_LOG_FP = None           # 重定向 stdout/stderr 用的句柄


def _base_dir():
    """exe 所在目录(frozen) 或脚本所在目录(源码), 日志/标记文件都放这里。"""
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                           else __file__))


def _settings():
    """便携化: 配置写在程序旁的 MediaOrganizer.ini, 不碰注册表。
    目录只读时(只读共享/直接跑压缩包里)退回用户级 ini, 程序照常运行。"""
    ini = os.path.join(_base_dir(), "MediaOrganizer.ini")
    try:
        s = QSettings(ini, QSettings.Format.IniFormat)
        s.setValue("_probe", "1")
        s.remove("_probe")
        s.sync()
        if s.status() == QSettings.Status.NoError:
            return s
    except Exception:
        pass
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "MediaOrganizer")


def log_s(msg):
    """逐行写诊断日志。★ 以后排查闪退, 先看 exe 旁的 startup.log 最后一行。"""
    if not LOG_PATH:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def init_diag():
    """把 stdout/stderr 重定向到 exe 旁的 startup.log 并启用 faulthandler。
    exe 旁不可写时退回系统临时目录。返回日志路径。"""
    global LOG_PATH, _LOG_FP
    if LOG_PATH:
        return LOG_PATH
    path = os.path.join(_base_dir(), "startup.log")
    try:
        with open(path, "a", encoding="utf-8"):
            pass
    except Exception:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "MediaOrganizer_startup.log")
    LOG_PATH = path
    try:
        _LOG_FP = open(path, "a", encoding="utf-8", buffering=1)
        os.dup2(_LOG_FP.fileno(), 1)     # 捕获 Qt C++ 层的平台/渲染报错
        os.dup2(_LOG_FP.fileno(), 2)
    except Exception:
        _LOG_FP = None
    try:
        import faulthandler
        faulthandler.enable(file=(_LOG_FP if _LOG_FP is not None
                                  else open(path, "a", encoding="utf-8")))
    except Exception:
        pass
    return path


def install_excepthook():
    """★ 关键防线, 别删。
    PyQt6 对"从 Qt 回调(槽/虚函数重写)里逃逸出来的 Python 异常"的默认处理是
    qFatal() -> UCRT abort() -> __fastfail, 也就是进程以 0xC0000409 静默消失:
    没有对话框、没有 Python 栈、faulthandler 也抓不到。2026-10-16 那次"用户机器
    一移鼠标就闪退"就是 workflow_editor.leaveEvent 里 e.pos() 的 AttributeError
    被这条机制放大成静默猝死的。
    装一个"不再抛出"的 sys.excepthook 是 PyQt 官方留的逃生门: 异常只落日志,
    进程继续跑。任何回调里的未捕获异常从此都不会再杀进程。"""
    def _hook(t, v, tb):
        log_s("!! 未捕获异常(已拦截, 进程继续): %s: %s\n%s"
              % (getattr(t, "__name__", t), v, "".join(traceback.format_exception(t, v, tb))))
    sys.excepthook = _hook
    try:
        import threading

        def _thook(args):
            log_s("!! 线程内未捕获异常(已拦截): %s: %s\n%s"
                  % (args.exc_type.__name__, args.exc_value,
                     "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                        args.exc_traceback))))
        threading.excepthook = _thook
    except Exception:
        pass


def install_qt_message_handler():
    """把 Qt 层告警/致命消息也写进 startup.log。"""
    try:
        from PyQt6.QtCore import qInstallMessageHandler

        def _h(mode, ctx, msg):
            log_s("QT[%s] %s" % (mode.name, msg))
        qInstallMessageHandler(_h)
    except Exception:
        pass


def install_smoke_leave(app, win):
    """--smoke-leave 自动验收: 把"曾经静默杀死进程"的两条路径各跑一遍, 结果写文件。
      1) 给窗口发 Leave 事件 —— 旧版 workflow_editor.leaveEvent 里 e.pos() 必崩
      2) 在 Qt 回调里故意抛异常 —— 旧版会被 PyQt6 转成 qFatal/abort(0xC0000409)
    结果逐行追加写 _smoke_result.txt: 进程若中途猝死, 文件就停在那一行。"""
    out = os.path.join(_base_dir(), "_smoke_result.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("%s  SMOKE-LEAVE 开始\n" % time.strftime("%H:%M:%S"))
    except Exception:
        out = os.path.join(os.environ.get("TEMP", "."), "_smoke_result.txt")

    def w(line):
        try:
            with open(out, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), line))
        except Exception:
            pass

    def s1():
        app.sendEvent(win, QEvent(QEvent.Type.Leave))
        w("[1/3] 已发送 Leave 事件, 进程存活")

    def s2():
        def boom():
            raise RuntimeError("smoke: 故意在 Qt 回调里抛异常(应被 excepthook 拦截)")
        w("[2/3] 即将在 Qt 回调里抛异常")
        QTimer.singleShot(0, boom)

    def s3():
        w("[3/3] 事件循环仍在运行 -> PASS")

    QTimer.singleShot(600, s1)
    QTimer.singleShot(1200, s2)
    QTimer.singleShot(1900, s3)
    QTimer.singleShot(2400, app.quit)


# ---------------------------------------------------------------------------
# 素材分类与日期逻辑
# ---------------------------------------------------------------------------
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
              "heic", "heif", "svg", "ico", "avif", "raw", "cr2", "nef",
              "arw", "dng", "psd"}
VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "flv", "wmv", "m4v",
              "mpg", "mpeg", "3gp", "ts", "mts", "m2ts", "rmvb", "rm"}

CAT_IMAGE = "照片"
CAT_VIDEO = "视频"
CAT_OTHER = "其他"
CATS = [CAT_IMAGE, CAT_VIDEO, CAT_OTHER]

GRAN_YEAR = "年"
GRAN_YEAR_MONTH = "年-月"
GRAN_YEAR_MONTH_DAY = "年-月-日"
GRANS = [GRAN_YEAR, GRAN_YEAR_MONTH, GRAN_YEAR_MONTH_DAY]


def classify_file(path):
    """按扩展名把文件归为 图片/视频/其他。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in IMAGE_EXTS:
        return CAT_IMAGE
    if ext in VIDEO_EXTS:
        return CAT_VIDEO
    return CAT_OTHER


def _exif_datetime(path):
    """尝试从图片 EXIF 读取拍摄时间, 失败返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            exif = im.getexif()
        for tag in (36867, 306):  # DateTimeOriginal / DateTime
            val = exif.get(tag)
            if val:
                try:
                    return datetime.datetime.strptime(str(val).strip(),
                                                      "%Y:%m:%d %H:%M:%S")
                except Exception:
                    continue
    except Exception:
        pass
    return None


def file_datetime(path, category, use_exif):
    """得到文件的时间(用于归档)。图片可优先取 EXIF 拍摄时间, 否则取修改时间。"""
    if use_exif and category == CAT_IMAGE:
        d = _exif_datetime(path)
        if d is not None:
            return d
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path))
    except Exception:
        return datetime.datetime.now()


def date_rel_path(dt, granularity):
    """按粒度生成归档相对目录(不含文件名), 如 2025 / 2025-03 / 2025-03-14。"""
    y = dt.year
    if granularity == GRAN_YEAR:
        return f"{y}"
    m = f"{y}-{dt.month:02d}"
    if granularity == GRAN_YEAR_MONTH:
        return m
    return f"{m}/{y}-{dt.month:02d}-{dt.day:02d}"


def unique_dest(folder, rel):
    """若目标已存在则自动追加序号, 返回不冲突的绝对路径。"""
    full = os.path.join(folder, rel)
    if not os.path.exists(full):
        return full
    base, ext = os.path.splitext(rel)
    n = 1
    while True:
        cand = os.path.join(folder, f"{base}_{n}{ext}")
        if not os.path.exists(cand):
            return cand
        n += 1


# ---------------------------------------------------------------------------
# 节点图形项
# ---------------------------------------------------------------------------
NODE_W = 230
HEADER_H = 38
ROW_H = 46

COLORS = {
    "素材源": QColor("#3b82f6"),
    "类型分类": QColor("#8b5cf6"),
    "按日期归档": QColor("#14b8a6"),
    "输出": QColor("#f59e0b"),
}
SOCK_COLOR = {
    CAT_IMAGE: QColor("#22c55e"),
    CAT_VIDEO: QColor("#ef4444"),
    CAT_OTHER: QColor("#94a3b8"),
    "default": QColor("#e2e8f0"),
}


def perform_job(folder, job, move, log=None):
    """真正把一个文件移动到/复制到目标(处理重名)。返回是否成功。"""
    dest = unique_dest(folder, job["dest_rel"])
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if move:
            shutil.move(job["path"], dest)
        else:
            shutil.copy2(job["path"], dest)
        if log:
            log("  %s → %s" % (os.path.basename(job["path"]), os.path.relpath(dest, folder)))
        return True
    except Exception as e:
        if log:
            log("  失败 %s: %s" % (os.path.basename(job["path"]), e))
        return False


# ---------------------------------------------------------------------------
# 后台执行线程
# ---------------------------------------------------------------------------
def build_plan(input_folder, gran, use_exif, log, progress):
    """扫描输入文件夹, 返回归档计划: [{path, category, date, dest_rel}, ...]"""
    items = []
    for root, dirs, files in os.walk(input_folder):
        for fn in files:
            p = os.path.join(root, fn)
            cat = classify_file(p)
            dt = file_datetime(p, cat, use_exif)
            rel = os.path.join(cat, date_rel_path(dt, gran), fn)
            items.append({"path": p, "category": cat, "date": dt, "dest_rel": rel})
    log("扫描完成: 共 %d 个文件" % len(items))
    return items


# ---------------------------------------------------------------------------
# 后台执行线程(只计算计划, 不落盘)
# ---------------------------------------------------------------------------
class Runner(QThread):
    log = pyqtSignal(str)
    plan_done = pyqtSignal(list)
    done = pyqtSignal(dict)

    def __init__(self, input_folder, gran, use_exif, parent=None):
        super().__init__(parent)
        self.input_folder = input_folder
        self.gran = gran
        self.use_exif = use_exif

    def run(self):
        try:
            plan = build_plan(self.input_folder, self.gran, self.use_exif,
                              self.log.emit, lambda a, b: None)
            self.plan_done.emit(plan)
        except Exception as e:
            self.log.emit("出错: %s" % e)
            self.log.emit(traceback.format_exc())
            self.done.emit({"error": str(e)})


class BulkRunner(QThread):
    """跳过动画时的后台批量处理: 扫描 -> 归档计划 -> 直接复制/移动。"""
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    done = pyqtSignal(dict)

    def __init__(self, input_folder, output_folder, gran, move, parent=None):
        super().__init__(parent)
        self.input_folder = input_folder
        self.output_folder = output_folder
        self.gran = gran
        self.move = move

    def run(self):
        try:
            plan = build_plan(self.input_folder, self.gran, True, self.log.emit, lambda a, b: None)
            if not plan:
                self.log.emit("没有需要处理的文件。")
                self.done.emit({"ok": True, "stats": {}})
                return
            stats = {c: 0 for c in CATS}
            total = len(plan)
            self.log.emit("直接处理 %d 个文件 → %s" % (total, self.output_folder))
            for i, job in enumerate(plan, 1):
                if perform_job(self.output_folder, job, self.move, self.log.emit):
                    stats[job["category"]] = stats.get(job["category"], 0) + 1
                if i % 50 == 0 or i == total:
                    self.progress.emit(i, total)
            self.progress.emit(total, total)
            self.done.emit({"ok": True, "stats": stats})
        except Exception as e:
            self.log.emit("出错: %s" % e)
            self.log.emit(traceback.format_exc())
            self.done.emit({"error": str(e)})


# ---------------------------------------------------------------------------
# 半圆盘可视化舞台: 左=输入, 右=半圆盘分布目标文件夹, 光线输送(海量数据被分类)
# ---------------------------------------------------------------------------
CAT_COLORS = {CAT_IMAGE: QColor("#34d399"), CAT_VIDEO: QColor("#f87171"),
              CAT_OTHER: QColor("#94a3b8")}


class FolderNodeItem(QGraphicsItem):
    """目标文件夹节点卡片(固定位置)。"""

    def __init__(self, key, category, name, pos, color):
        super().__init__()
        self.key = key
        self.category = category
        self.name = name
        self.color = color
        self.count = 0
        self.total = 0
        self.active = False
        self.setZValue(3)
        self.setPos(pos)
        self.text_w = 8.5 * len(name)
        self.w = max(140, 40 + self.text_w)
        self.h = 44

    def boundingRect(self):
        return QRectF(-8, -8, self.w + 16, self.h + 16)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.w, self.h)
        if self.active:
            c = QColor(self.color); c.setAlphaF(0.8)
            painter.setPen(QPen(c, 1.6)); painter.setBrush(QColor(32, 32, 36))
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 11, 11)
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        painter.setBrush(QColor(28, 28, 31))
        painter.drawRoundedRect(rect, 10, 10)
        # 顶边高光(用细矩形代替 drawLine, 规避 item 内 drawLine 崩溃)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawRect(QRectF(rect.left() + 10, rect.top() + 1.2, rect.width() - 20, 1.2))
        # 类别圆点
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        painter.drawEllipse(QRectF(10, 14, 12, 12))
        # 名称(类别 · 日期)
        painter.setPen(QPen(QColor("#ededed"), 1))
        f = painter.font(); f.setPointSize(9); f.setBold(True); painter.setFont(f)
        painter.drawText(QRectF(30, 4, self.w - 36, 32), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.name)
        # 计数徽标(已落盘/应落)
        if self.total:
            badge = "%d/%d" % (self.count, self.total)
            bw = 11 + 7 * len(badge)
            b = QRectF(rect.right() - bw - 8, 9, bw, 18)
            painter.setPen(QPen(QColor(self.color), 1)); painter.setBrush(self.color)
            painter.drawRoundedRect(b, 9, 9)
            painter.setPen(QPen(QColor("#0f0f10"), 1))
            f.setBold(True); f.setPointSize(8); painter.setFont(f)
            painter.drawText(b, Qt.AlignmentFlag.AlignCenter, badge)
        # 底部真实进度条(已落盘/应落)
        ratio = (self.count / self.total) if self.total else 0.0
        py = rect.height() - 7
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 16))
        painter.drawRoundedRect(QRectF(12, py, rect.width() - 24, 3), 1.5, 1.5)
        painter.setBrush(self.color)
        painter.drawRoundedRect(QRectF(12, py, max(0.0, (rect.width() - 24) * ratio), 3), 1.5, 1.5)


class FlowWire(QGraphicsItem):
    """源 → 目标文件夹的固定连线。"""

    def __init__(self, p1, p2, color):
        super().__init__()
        self.p1 = p1; self.p2 = p2; self.color = color
        dx = p2.x() - p1.x()
        path = QPainterPath(p1)
        path.cubicTo(p1.x() + dx * 0.45, p1.y(), p2.x() - dx * 0.45, p2.y(), p2.x(), p2.y())
        self.path = path
        self.act = 0.0
        self.setZValue(2)

    def boundingRect(self):
        return self.path.boundingRect().adjusted(-10, -10, 10, 10)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self.path
        total = path.length()
        if total <= 1:
            return
        steps = int(total / 4) + 2   # 密排成"实线"(规避 item 内 drawLine/drawPath 的原生崩溃)
        hot = self.act > 0.05
        # 常驻淡淡的灰色实线(管路基线, 光粒沿这条线走) — 平时清晰可见
        c = QColor(139, 139, 144, 150 if hot else 85)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(c)
        for i in range(steps):
            painter.drawEllipse(path.pointAtPercent(i / (steps - 1)), 1.4 if hot else 1.1, 1.4 if hot else 1.1)
        # 光通过时整条管路微亮发白
        if hot:
            painter.setBrush(QColor(255, 255, 255, 120))
            for i in range(steps):
                painter.drawEllipse(path.pointAtPercent(i / (steps - 1)), 1.0, 1.0)


class BeamItem(QGraphicsItem):
    """沿连线流动的发光光带。"""

    def __init__(self, wire, color):
        super().__init__()
        self.wire = wire
        self.color = color
        self.t = 0.0
        self.setZValue(4)
        wire.act = min(1.5, wire.act + 0.35)

    def boundingRect(self):
        return self.wire.path.boundingRect().adjusted(-26, -26, 26, 26)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self.wire.path
        total = path.length()
        if total <= 0:
            return
        head = path.pointAtPercent(min(1.0, self.t))
        trail = min(0.5, 160.0 / total)
        tb = max(0.0, self.t - trail)
        steps = 24
        base = QColor(self.color)
        # 一束细小的光沿管路流动(AI 工作流风格): 前端略亮, 尾部渐隐, 无独立粗粒子
        for i in range(steps, 0, -1):
            frac = tb + (self.t - tb) * (i / steps)
            pt = path.pointAtPercent(frac)
            k = i / steps
            r = 1.0 + 1.1 * k
            # 类别色淡淡光晕(彩色分类的体现)
            c = QColor(base); c.setAlphaF(0.12 + 0.20 * k)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(c)
            painter.drawEllipse(pt, r * 1.5, r * 1.5)
            # 高亮白芯(细光束本体)
            painter.setBrush(QColor(255, 255, 255, 60 + int(140 * k)))
            painter.drawEllipse(pt, r, r)
        # 前端一小点亮芯(光束自然前端, 精致不粗)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 210))
        painter.drawEllipse(head, 1.3, 1.3)


class FlowScene(QGraphicsScene):
    """大厂工作流舞台: 源居中, 目标文件夹左右两列对称排开, 自动牵线。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(-120, -500, 1600, 1000)
        self.source_name = ""
        self.source_center = QPointF(500, 0)   # 源卡片中心
        self.source_left = QPointF(376, 0)     # 左侧光束出点(向左侧列)
        self.source_right = QPointF(624, 0)    # 右侧光束出点(向右侧列)
        self.left_inner = 200    # 左列卡片内侧(右边缘)固定, 距源留间隙
        self.right_inner = 800   # 右列卡片内侧(左边缘)固定, 距源留间隙
        self.nodes = []
        self.wires = []
        self.node_by_key = {}
        self.wire_by_key = {}

    def clear_nodes(self):
        for w in self.wires:
            self.removeItem(w)
        for n in self.nodes:
            self.removeItem(n)
        self.nodes = []; self.wires = []
        self.node_by_key = {}; self.wire_by_key = {}

    def build_nodes(self, plan):
        self.clear_nodes()
        folders = {}
        for job in plan:
            d = os.path.dirname(job["dest_rel"])
            if d not in folders:
                folders[d] = [job["category"], 0]
            folders[d][1] += 1
        # 分列: 照片→左列, 视频→右列, 其他→均分左右(左右对称)
        left_items = []; right_items = []
        for cat in CATS:
            its = [(d, c) for d, (cc, c) in folders.items() if cc == cat]
            its.sort(key=lambda x: x[0])
            if cat == CAT_IMAGE:
                left_items += [(d, c, cat) for d, c in its]
            elif cat == CAT_VIDEO:
                right_items += [(d, c, cat) for d, c in its]
            else:
                half = len(its) // 2
                left_items += [(d, c, cat) for d, c in its[:half]]
                right_items += [(d, c, cat) for d, c in its[half:]]
        spacing = 78
        for srcpt, is_left, items in ((self.source_left, True, left_items),
                                      (self.source_right, False, right_items)):
            if not items:
                continue
            n = len(items)
            for i, (d, cnt, cat) in enumerate(items):
                display = d.replace("\\", "/")
                color = CAT_COLORS.get(cat, CAT_COLORS[CAT_OTHER])
                node = FolderNodeItem(d, cat, display, QPointF(0, 0), color)
                node.total = cnt
                y = (i - (n - 1) / 2) * spacing
                # 左右对称: 内侧边缘(靠近源)对齐固定位置并留间隙, 外侧自然延伸
                if is_left:
                    inner = self.left_inner
                    node.setPos(QPointF(inner - node.w, y))   # 左列: 卡片右边缘贴齐
                    end = QPointF(inner, y)
                else:
                    inner = self.right_inner
                    node.setPos(QPointF(inner, y))            # 右列: 卡片左边缘贴齐
                    end = QPointF(inner, y)
                self.nodes.append(node); self.addItem(node)
                self.node_by_key[d] = node
                wire = FlowWire(srcpt, end, color)
                wire.node_key = d
                self.wires.append(wire); self.addItem(wire)
                self.wire_by_key[d] = wire
        self._fit_rect()

    def _fit_rect(self):
        xs = [self.source_center.x() - 160, self.source_center.x() + 160]
        ys = [0.0]
        for w in self.wires:
            br = w.path.boundingRect()
            xs += [br.left(), br.right()]; ys += [br.top(), br.bottom()]
        for n in self.nodes:
            xs += [n.pos().x() - 20, n.pos().x() + n.w + 20]
            ys += [n.pos().y() - 20, n.pos().y() + n.h + 20]
        m = 60
        self.setSceneRect(min(xs) - m, min(ys) - m,
                          (max(xs) - min(xs)) + 2 * m, (max(ys) - min(ys)) + 2 * m)


class FlowView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setBackgroundBrush(QColor("#0f0f10"))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._enable_gpu()
        self._apply_update_mode()

    def _apply_update_mode(self):
        """★ GL viewport 必须整屏刷新。Qt 官方文档(viewportUpdateMode):
        "FullViewportUpdate ... is the preferred update mode for viewports that do not
        support partial updates, such as QOpenGLWidget"。局部刷新 + QOpenGLWidget
        会出现移动后的重影残影。"""
        gl = hasattr(self.viewport(), "grabFramebuffer")     # QOpenGLWidget 的特征方法
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate if gl
            else QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)

    def _enable_gpu(self):
        """用 OpenGL(QOpenGLWidget)渲染 viewport 做 GPU 加速;
        强制软件渲染的三种情况: 环境变量 MO_RASTER=1 / 程序旁放 MediaOrganizer.raster
        空文件 / --shot 出图(截图必须走软件渲染才抓得到画布)。"""
        base = _base_dir()
        if (os.environ.get("MO_RASTER") or screenshot_mode()
                or os.path.exists(os.path.join(base, "MediaOrganizer.raster"))):
            return
        try:
            from PyQt6.QtOpenGLWidgets import QOpenGLWidget
            gl = QOpenGLWidget()
            self.setViewport(gl)
        except Exception:
            pass

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QColor("#0f0f10"))
        sc = self.scene()
        # 源节点卡片(居中)
        c = sc.source_center
        card = QRectF(c.x() - 130, c.y() - 46, 260, 92)
        painter.setBrush(QColor(28, 28, 31))
        painter.setPen(QPen(QColor(255, 255, 255, 24), 1))
        painter.drawRoundedRect(card, 12, 12)
        # 顶边高光(细矩形, 规避 drawLine 崩溃)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 40))
        painter.drawRect(QRectF(card.left() + 12, card.top() + 1.2, card.width() - 24, 1.2))
        painter.setPen(QPen(QColor(139, 139, 144), 1))
        f = painter.font(); f.setPointSize(9); f.setBold(True); painter.setFont(f)
        painter.drawText(card.adjusted(12, 8, -12, -44), Qt.AlignmentFlag.AlignCenter, "素材源 · 输入文件夹")
        painter.setPen(QPen(QColor(237, 237, 237), 1))
        f.setPointSize(10); f.setBold(False); painter.setFont(f)
        painter.drawText(card.adjusted(12, 46, -12, -10), Qt.AlignmentFlag.AlignCenter,
                         self._short(sc.source_name, 20))
        # 左右两侧光束出点光晕
        for sp in (sc.source_left, sc.source_right):
            for rr, aa in ((14, 0.10), (7, 0.20)):
                c2 = QColor(255, 255, 255); c2.setAlphaF(aa)
                painter.setBrush(c2); painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(sp, rr, rr)

    @staticmethod
    def _short(s, n):
        s = s or "未选择"
        return s if len(s) <= n else s[:n - 1] + "…"


def _fmt_size(n):
    """字节 → 人类可读大小。"""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.0f%s" % (n, u)
        n /= 1024
    return "%.0fTB" % n


class _Copier(QThread):
    """后台落盘线程: 主线程只管动画, 拷贝/移动丢到这里, 完成后经信号回主线程, 避免大文件卡死 UI。"""
    job_done = pyqtSignal(str, bool)   # job["path"], ok
    job_start = pyqtSignal(str, int)   # job["path"], size(字节), 用于日志提示"正在拷贝…"

    def __init__(self, output_folder, move, parent=None):
        super().__init__(parent)
        self.output_folder = output_folder
        self.move = move
        self.q = queue.Queue()

    def submit(self, job):
        self.q.put(job)

    def stop(self):
        self.q.put(None)

    def run(self):
        while True:
            job = self.q.get()
            if job is None:
                break
            try:
                size = os.path.getsize(job["path"])
            except Exception:
                size = 0
            self.job_start.emit(job["path"], size)
            try:
                ok = perform_job(self.output_folder, job, self.move, None)
            except Exception:
                ok = False
            self.job_done.emit(job["path"], ok)


class FlowAnimator(QObject):
    """驱动光带沿固定连线流动; 每个文件发一条光带, 到达时提交后台落盘, 主线程只做动画不阻塞。"""

    def __init__(self, scene, output_folder, move, fps_provider, log, on_progress, on_done, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.output_folder = output_folder
        self.move = move
        self.fps_provider = fps_provider  # 每次 tick 实时读取当前速度, 运行中拖滑块立即生效
        self.log = log
        self.on_progress = on_progress
        self.on_done = on_done
        self.repeat = 2           # 每个文件发 repeat 条光带(增加流动密度)
        self.max_beam = 10        # 最多并发光带数
        self.records = []         # [{job, written, submitted, wire}]
        self.by_path = {}         # job["path"] -> record
        self.tasks = []           # 待发射的光带任务 {rec, wire}
        self.active = []          # 飞行中的光带 {beam, rec}
        self.written = 0
        self.submitted = 0
        self.total = 0
        self._last_report = -1
        self.copier = _Copier(output_folder, move)
        self.copier.job_done.connect(self._on_copy_done)
        self.copier.job_start.connect(self._on_job_start)
        self._cur_copy = None      # copier 当前正在落盘的文件路径(补发光带只绑定它, 保证真实不造假)
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self.tick)

    def _on_job_start(self, path, size):
        # 记录"当前真正在拷的文件", 后续光带只朝它发, 确保动画=真实传输
        self._cur_copy = path
        self.log("▶ 正在拷贝 %s (%s)…" % (os.path.basename(path), _fmt_size(size)))

    def start(self, jobs):
        self.records = []
        self.by_path = {}
        seen = set()
        for j in jobs:
            if id(j) not in seen:
                seen.add(id(j))
                rec = {"job": j, "written": False, "submitted": False, "wire": None}
                self.records.append(rec)
                self.by_path[j["path"]] = rec
        self.total = len(self.records)
        self.tasks = []
        self.active = []
        self.written = 0
        self.submitted = 0
        self._last_report = -1
        if self.total == 0:
            self.on_progress(0, 0)
            self.on_done()
            return
        self.copier.start()
        for rec in self.records:
            folder = os.path.dirname(rec["job"]["dest_rel"])
            wire = self.scene.wire_by_key.get(folder)
            rec["wire"] = wire
            if wire is None:
                # 没有对应节点, 直接交给后台落盘
                rec["submitted"] = True
                self.submitted += 1
                self.copier.submit(rec["job"])
            else:
                for _ in range(self.repeat):
                    self.tasks.append({"rec": rec, "wire": wire})
        if self.tasks:
            self.timer.start()
        self._report()
        self._maybe_done()

    def _beam_dur(self):
        # 一条光带从源到节点的时长(秒); 实时按当前速度决定, 拖滑块立即改变飞行速度
        return 1.3 if self.fps_provider() < 2.0 else 0.55

    def _report(self):
        # 真实进度 = 已落盘(written) / 总数; 绝不用"已出发"撑满 100%, 避免误以为完成
        if self.written != self._last_report:
            self._last_report = self.written
            self.on_progress(self.written, self.total)

    def tick(self):
        dt = 0.03
        fps = self.fps_provider()  # 实时速度
        # 按速率发射光带
        spawn_acc = self._spawn_rem = getattr(self, "_spawn_rem", 0.0) + dt * fps
        while spawn_acc >= 1.0 and self.tasks and len(self.active) < self.max_beam:
            spawn_acc -= 1.0
            task = self.tasks.pop(0)
            rec = task["rec"]
            color = CAT_COLORS.get(rec["job"]["category"], CAT_COLORS[CAT_OTHER])
            beam = BeamItem(task["wire"], color)
            self.scene.addItem(beam)
            self.active.append({"beam": beam, "rec": rec})
        self._spawn_rem = spawn_acc
        # 移动 + 到达(局部重绘, 不再整场景强制重绘)
        done = []
        for a in self.active:
            a["beam"].t += dt / self._beam_dur()
            a["beam"].update()
            if a["beam"].t >= 1.0:
                done.append(a)
        for a in done:
            self.scene.removeItem(a["beam"])
            self.active.remove(a)
            rec = a["rec"]
            node = self.scene.node_by_key.get(os.path.dirname(rec["job"]["dest_rel"]))
            if node is not None:
                node.active = False
                node.update()
            if not rec["submitted"]:
                rec["submitted"] = True
                self.submitted += 1
                self.copier.submit(rec["job"])
        # 光带已发完但后台仍在拷: 只给"当前真正在拷的文件"持续补发光带(它确实在传输, 重复发射是真实的)
        if (not self.tasks) and self._cur_copy and self.written < self.total and len(self.active) < 2:
            rec = self.by_path.get(self._cur_copy)
            if rec is not None and (not rec["written"]) and rec["wire"] is not None:
                color = CAT_COLORS.get(rec["job"]["category"], CAT_COLORS[CAT_OTHER])
                beam = BeamItem(rec["wire"], color)
                self.scene.addItem(beam)
                self.active.append({"beam": beam, "rec": rec})
        self._report()
        self._maybe_done()

    def _on_copy_done(self, path, ok):
        if self._cur_copy == path:
            self._cur_copy = None   # 该文件已落盘, 不再朝它补发光带
        rec = self.by_path.get(path)
        if rec is None or rec["written"]:
            return
        rec["written"] = True
        self.written += 1
        job = rec["job"]
        if ok:
            node = self.scene.node_by_key.get(os.path.dirname(job["dest_rel"]))
            if node is not None:
                node.count += 1
                node.update()
            self.log("  %s → %s" % (os.path.basename(job["path"]), job["dest_rel"]))
        else:
            self.log("  失败 %s" % os.path.basename(job["path"]))
        self._report()
        self._maybe_done()

    def _maybe_done(self):
        if self.written >= self.total:
            self.timer.stop()
            if self.copier.isRunning():
                self.copier.stop()
                self.copier.wait(3000)
            self.on_done()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """主窗口: 顶部设置 + 数据光带工作流舞台 + 日志。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("素材整理工作流 — 数据光带工作流")
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)
        self.input_folder = ""
        self.output_folder = ""
        self.runner = None
        self.animator = None
        self.scene = FlowScene(self)
        self.view = FlowView(self.scene, self)
        self._build_toolbar()
        self._build_central()
        self._build_log_dock()
        self._apply_settings()
        self._start_fx()

    # ---------------- 界面构建 ----------------
    def _build_toolbar(self):
        tb = self.addToolBar("运行")
        tb.setMovable(False)
        tb.setStyleSheet("background:#141416; border:none; padding:4px;")
        self.run_btn = QPushButton("▶  运行工作流")
        self.run_btn.setStyleSheet("QPushButton{background:#5e6ad2;color:white;font-weight:bold;"
                                   "border-radius:8px;padding:8px 18px;font-size:13px;}"
                                   "QPushButton:hover{background:#707aeb;}"
                                   "QPushButton:disabled{background:#2a2a2d;color:#55555a;}")
        self.run_btn.clicked.connect(self.start_run)
        tb.addWidget(self.run_btn)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine); sep.setStyleSheet("color:#2a2a2d;"); tb.addWidget(sep)
        lbl = QLabel("分类速度"); lbl.setStyleSheet("color:#8b8b90; font-size:12px;"); tb.addWidget(lbl)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, 100); self.speed_slider.setValue(35); self.speed_slider.setFixedWidth(180)
        self.speed_slider.setStyleSheet(
            "QSlider{background:transparent;}"
            "QSlider::groove:horizontal{height:4px;background:#26262a;border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#5e6ad2;border-radius:2px;}"
            "QSlider::add-page:horizontal{background:#26262a;border-radius:2px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-6px 0;border-radius:8px;"
            "background:#f2f2f3;border:1px solid rgba(255,255,255,0.28);}"
            "QSlider::handle:horizontal:hover{background:#ffffff;}"
            "QSlider::handle:horizontal:pressed{background:#c7c9d4;}")
        self.speed_slider.valueChanged.connect(self._speed_changed)
        tb.addWidget(self.speed_slider)
        self.speed_tag = QLabel("~2.5/秒"); self.speed_tag.setStyleSheet("color:#8b8b90; font-size:12px;"); tb.addWidget(self.speed_tag)
        self.skip_anim = QCheckBox("跳过动画直接处理"); self.skip_anim.setStyleSheet("color:#8b8b90; font-size:12px;"); tb.addWidget(self.skip_anim)
        o = QToolButton(); o.setText("打开输出文件夹"); o.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly); o.clicked.connect(self._open_output); tb.addWidget(o)
        r = QToolButton(); r.setText("重置视图"); r.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly); r.clicked.connect(self._reset_view); tb.addWidget(r)
        cl = QToolButton(); cl.setText("清空日志"); cl.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly); cl.clicked.connect(self._clear_log); tb.addWidget(cl)
        hint = QLabel("  素材源居中 → 左右目标文件夹对称(自动连线, 光纤白光带实时输送文件)"); hint.setStyleSheet("color:#55555a; font-size:11px;"); tb.addWidget(hint)

    def _build_central(self):
        self.settings = QWidget()
        self.settings.setStyleSheet("QWidget{background:#141416;border-bottom:1px solid rgba(255,255,255,0.06);}")
        sl = QHBoxLayout(self.settings); sl.setContentsMargins(12, 8, 12, 8); sl.setSpacing(12)
        self.in_btn = QPushButton("📁 输入文件夹…"); self.in_btn.setStyleSheet(self._lin_btn("#5e6ad2")); self.in_btn.clicked.connect(self._pick_input); sl.addWidget(self.in_btn)
        self.in_path = QLabel("未选择"); self.in_path.setStyleSheet("color:#c9c9cc; font-size:12px;"); sl.addWidget(self.in_path, 1)
        self.out_btn = QPushButton("📂 输出文件夹…"); self.out_btn.setStyleSheet(self._lin_btn("#5e6ad2")); self.out_btn.clicked.connect(self._pick_output); sl.addWidget(self.out_btn)
        self.out_path = QLabel("未选择"); self.out_path.setStyleSheet("color:#c9c9cc; font-size:12px;"); sl.addWidget(self.out_path, 1)
        sl.addWidget(self._sep())
        gl = QLabel("归档粒度"); gl.setStyleSheet("color:#8b8b90; font-size:12px;"); sl.addWidget(gl)
        self.gran = QComboBox(); self.gran.addItems(GRANS); self.gran.setCurrentIndex(1)
        self.gran.setStyleSheet("QComboBox{background:#1c1c1f;color:#ededed;border:1px solid rgba(255,255,255,0.10);border-radius:8px;padding:5px 10px;font-size:12px;}"
                                "QComboBox:hover{border-color:rgba(255,255,255,0.18);}"
                                "QComboBox QAbstractItemView{background:#1a1a1d;color:#ededed;selection-background-color:#333;}")
        sl.addWidget(self.gran)
        sl.addWidget(self._sep())
        self.mode_move = QRadioButton("移动"); self.mode_copy = QRadioButton("复制"); self.mode_copy.setChecked(True)
        for rb in (self.mode_move, self.mode_copy): rb.setStyleSheet("color:#c9c9cc; font-size:12px;")
        wl = QLabel("方式"); wl.setStyleSheet("color:#8b8b90; font-size:12px;")
        sl.addWidget(wl); sl.addWidget(self.mode_move); sl.addWidget(self.mode_copy)
        sl.addWidget(self._sep())
        self.chip_video = QLabel("视频 0"); self.chip_photo = QLabel("照片 0"); self.chip_other = QLabel("其他 0")
        self._style_chips()
        container = QWidget(); cv = QVBoxLayout(container); cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(0)
        cv.addWidget(self.settings); cv.addWidget(self.view, 1)
        self.setCentralWidget(container)

    def _build_log_dock(self):
        dock = QDockWidget("运行日志", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet("QTextEdit{background:#131315;color:#c9c9cc;border:none;font-family:Consolas;font-size:12px;}")
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0)
        self.progress.setFormat("已落盘 %p%")
        self.progress.setStyleSheet("QProgressBar{background:#1a1a1d;border:none;border-radius:4px;height:8px;}"
                                    "QProgressBar::chunk{background:#5e6ad2;border-radius:4px;}")
        wrap = QWidget(); wv = QVBoxLayout(wrap); wv.setContentsMargins(6, 4, 6, 6)
        wv.addWidget(self.progress); wv.addWidget(self.log_box); dock.setWidget(wrap)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.log_box.setMinimumHeight(120)

    @staticmethod
    def _lin_btn(color):
        return (f"QPushButton{{background:#1c1c1f;color:#ededed;border:1px solid rgba(255,255,255,0.10);"
                f"border-radius:8px;padding:7px 14px;font-size:12px;}}"
                f"QPushButton:hover{{background:#242427;border-color:{color};}}"
                f"QPushButton:pressed{{background:#2a2a2d;}}")

    @staticmethod
    def _sep():
        s = QFrame(); s.setFrameShape(QFrame.Shape.VLine)
        s.setStyleSheet("color:#2a2a2d; background:#2a2a2d; max-width:1px;"); return s

    def _style_chips(self):
        colors = {"视频": "#fb7185", "照片": "#4ade80", "其他": "#94a3b8"}
        for chip in (self.chip_video, self.chip_photo, self.chip_other):
            cat = chip.text().split()[0]
            chip.setStyleSheet(f"color:{colors[cat]}; font-size:12px; font-weight:bold;"
                               f"border:1px solid {colors[cat]}40; border-radius:8px; padding:3px 10px;"
                               f"background:#161618;")
        for chip in (self.chip_video, self.chip_photo, self.chip_other):
            self.settings.layout().addWidget(chip)

    # ---------------- 文件夹选择 ----------------
    def _pick_input(self):
        fd = QFileDialog.getExistingDirectory(self, "选择输入文件夹", self.input_folder or os.path.expanduser("~"))
        if fd:
            self.input_folder = fd
            self.in_path.setText(os.path.basename(fd) or fd)
            self.scene.source_name = fd
            _settings().setValue("src", fd)

    def _pick_output(self):
        fd = QFileDialog.getExistingDirectory(self, "选择输出文件夹", self.output_folder or os.path.expanduser("~"))
        if fd:
            self.output_folder = fd
            self.out_path.setText(os.path.basename(fd) or fd)
            _settings().setValue("out", fd)

    def _apply_settings(self):
        s = _settings()
        src = s.value("src", ""); out = s.value("out", "")
        if src and os.path.isdir(src):
            self.input_folder = src; self.in_path.setText(os.path.basename(src) or src); self.scene.source_name = src
        if out and os.path.isdir(out):
            self.output_folder = out; self.out_path.setText(os.path.basename(out) or out)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._frame)

    def _frame(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # ---------------- 运行 ----------------
    def _fps(self):
        return 0.6 + (self.speed_slider.value() / 100.0) * 5.5

    def _speed_changed(self, val):
        if self.speed_tag:
            self.speed_tag.setText("%.1f/秒" % self._fps())

    def start_run(self):
        if self.runner and self.runner.isRunning():
            return
        if not (self.input_folder and os.path.isdir(self.input_folder)):
            QMessageBox.warning(self, "提示", "请先选择【输入文件夹】"); return
        if not (self.output_folder and os.path.isdir(self.output_folder)):
            QMessageBox.warning(self, "提示", "请先选择【输出文件夹】"); return
        self._log("========================================")
        self.progress.setValue(0)
        self._refresh_top_chips({c: 0 for c in CATS})
        self.scene.clear_nodes()
        self.scene.source_name = self.input_folder
        if self.skip_anim.isChecked():
            self.run_btn.setEnabled(False)
            self.runner = BulkRunner(self.input_folder, self.output_folder,
                                     self.gran.currentText(), self.mode_move.isChecked(), self)
            self.runner.log.connect(self._log)
            self.runner.progress.connect(lambda a, b: self.progress.setValue(int(a / b * 100) if b else 0))
            self.runner.done.connect(self._on_done)
            self.runner.start()
            return
        self.run_btn.setEnabled(False)
        self.runner = Runner(self.input_folder, self.gran.currentText(), True, self)
        self.runner.log.connect(self._log)
        self.runner.plan_done.connect(self._on_plan_done)
        self.runner.done.connect(self._on_run_error)
        self.runner.start()

    def _on_plan_done(self, jobs):
        self.run_btn.setEnabled(True)
        if not jobs:
            self._log("没有需要处理的文件。"); QMessageBox.information(self, "完成", "没有需要处理的文件。"); return
        self.scene.build_nodes(jobs)
        QTimer.singleShot(0, self._frame)
        self._log("▶ 光带工作流开始: 共 %d 个文件, 目标文件夹 %d 个 (%.1f 个/秒)" %
                  (len(jobs), len(self.scene.nodes), self._fps()))
        self.animator = FlowAnimator(self.scene, self.output_folder, self.mode_move.isChecked(),
                                     self._fps, self._log, self._anim_progress, self._anim_done, self)
        self.animator.start(jobs)

    def _anim_progress(self, done, total):
        self.progress.setValue(int(done / total * 100) if total else 0)
        stats = {c: 0 for c in CATS}
        for n in self.scene.nodes:
            stats[n.category] = stats.get(n.category, 0) + n.count
        self._refresh_top_chips(stats)

    def _anim_done(self):
        self.run_btn.setEnabled(True)
        self.progress.setValue(100)
        stats = {c: 0 for c in CATS}
        for n in self.scene.nodes:
            stats[n.category] = stats.get(n.category, 0) + n.count
        total = sum(stats.values())
        self._log("✔ 分类完成: 共 %d 个文件" % total)
        if total:
            msg = "整理完成!\n\n" + "\n".join(f"{k}: {v} 个" for k, v in stats.items())
            QMessageBox.information(self, "完成", msg)

    def _on_done(self, result):
        self.run_btn.setEnabled(True)
        if result.get("ok"):
            stats = result.get("stats") or {}
            total = sum(stats.values())
            self.progress.setValue(100)
            self._log("✔ 全部完成: 共处理 %d 个文件" % total)
            self._refresh_top_chips(stats)
            if total:
                msg = "整理完成!\n\n" + "\n".join(f"{k}: {v} 个" for k, v in stats.items())
                QMessageBox.information(self, "完成", msg)
        else:
            self.progress.setValue(0)

    def _on_run_error(self, result):
        self.run_btn.setEnabled(True)
        if result.get("error"):
            QMessageBox.warning(self, "出错", str(result["error"]))

    def _refresh_top_chips(self, stats):
        self.chip_video.setText("视频 %d" % stats.get(CAT_VIDEO, 0))
        self.chip_photo.setText("照片 %d" % stats.get(CAT_IMAGE, 0))
        self.chip_other.setText("其他 %d" % stats.get(CAT_OTHER, 0))

    # ---------------- 工具 ----------------
    def _open_output(self):
        if self.output_folder and os.path.isdir(self.output_folder):
            os.startfile(self.output_folder)
        else:
            QMessageBox.information(self, "提示", "请先选择【输出文件夹】")

    def _reset_view(self):
        self._frame()

    def _clear_log(self):
        self.log_box.clear()

    def demo(self):
        """用合成的示例素材展示光带工作流动画(用于验证)。"""
        import tempfile
        self._in_demo = True
        tmp = tempfile.mkdtemp(prefix="demo_")
        self.input_folder = os.path.join(tmp, "input")
        self.output_folder = os.path.join(tmp, "output")
        os.makedirs(self.input_folder)

        def job(cat, y, m, name):
            return {"path": os.path.join(self.input_folder, name),
                    "category": cat, "date": None,
                    "dest_rel": os.path.join(cat, "%d-%02d" % (y, m), name)}

        jobs = [job(CAT_IMAGE, 2023, 3, "a1.jpg"), job(CAT_IMAGE, 2023, 3, "a2.jpg"),
                job(CAT_IMAGE, 2024, 8, "b1.png"),
                job(CAT_VIDEO, 2022, 5, "c1.mp4"), job(CAT_VIDEO, 2022, 5, "c2.mp4"),
                job(CAT_VIDEO, 2024, 1, "d1.mp4"),
                job(CAT_OTHER, 2021, 11, "e1.txt")]
        self.scene.build_nodes(jobs)
        self.scene.source_name = self.input_folder
        self.in_path.setText("demo"); self.out_path.setText("demo")
        self.speed_slider.setValue(30)
        QTimer.singleShot(0, self._frame)
        self.animator = FlowAnimator(self.scene, self.output_folder, False, self._fps,
                                     self._log, self._anim_progress, self._anim_done, self)
        self.animator.start(jobs)

    def _log(self, text):
        self.log_box.append(text)

    def closeEvent(self, e):
        # 处理未完成时关窗会丢文件, 弹确认; 并优雅停掉后台拷贝线程
        anim = getattr(self, "animator", None)
        if not getattr(self, "_in_demo", False) and anim is not None and 0 < anim.written < anim.total:
            left = anim.total - anim.written
            r = QMessageBox.warning(
                self, "处理未完成",
                "还有 %d / %d 个文件没有落盘, 现在关闭会丢失这部分拷贝。\n"
                "建议等进度条到 100% 再关, 或改用【跳过动画直接处理】。\n\n确定仍要退出吗?" %
                (left, anim.total),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        try:
            if anim is not None and anim.copier.isRunning():
                anim.copier.stop()
                anim.copier.wait(5000)
        except Exception:
            pass
        super().closeEvent(e)

    def _start_fx(self):
        # 光带动画已由各 item 局部 update() 驱动, 不再整场景强制重绘(消除 CPU 卡顿)
        pass

    def showEvent(self, e):
        super().showEvent(e)
        if getattr(self, "_gpu_probed", False):
            return
        self._gpu_probed = True
        # 等窗口真正显示、GL 上下文初始化后再检测, 避免误判
        QTimer.singleShot(150, self._gpu_probe)

    def _gpu_probe(self):
        """检测 GPU(OpenGL) 是否真的把画面交出来了; 黑屏/纯色/空帧则回退软件渲染。
        实测(2026-10-16): 本机 GL viewport 装上后 grabFramebuffer() 返回全黑帧, 探测到
        后会切回软件渲染 —— 这条自愈路径是有效且必要的。"""
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
                    # 整屏近黑(avg 极小) 或 整屏纯色(spread 极小) 都说明 GL 没渲染出画面
                    bad = avg < 12 or spread < 8
                log_s("GPU 探测: %s -> %s" % (info, "回退软件渲染" if bad else "GPU 正常"))
                if bad:
                    self.view.setViewport(QWidget())
                    self.view._apply_update_mode()
                    self.view.update()
        except Exception as e:
            log_s("GPU 探测异常(已忽略): %r" % (e,))
        self._pulse_on = False
        self.pulse_timer = QTimer(self); self.pulse_timer.setInterval(600)
        self.pulse_timer.timeout.connect(self._pulse_tick); self.pulse_timer.start()

    def _pulse_tick(self):
        if not self.run_btn.isEnabled():
            return
        self._pulse_on = not self._pulse_on
        c = "#34d399" if self._pulse_on else "#22c55e"
        self.run_btn.setStyleSheet(f"QPushButton{{background:{c};color:white;font-weight:bold;"
                                   f"border-radius:6px;padding:8px 18px;font-size:13px;"
                                   f"border:1px solid rgba(255,255,255,60);}}"
                                   f"QPushButton:hover{{background:#16a34a;}}"
                                   f"QPushButton:disabled{{background:#334155;}}")


# ---------------------------------------------------------------------------
# 逻辑自检(无界面)
# ---------------------------------------------------------------------------
def selftest():
    import tempfile
    from PIL import Image
    tmp = tempfile.mkdtemp(prefix="media_org_")
    src = os.path.join(tmp, "src")
    os.makedirs(src)
    img = os.path.join(src, "a.jpg")
    Image.new("RGB", (10, 10), (255, 0, 0)).save(img, quality=80)
    vid = os.path.join(src, "b.mp4")
    with open(vid, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42")
    txt = os.path.join(src, "c.txt")
    with open(txt, "w") as f:
        f.write("hello")
    results = []
    for p in (img, vid, txt):
        results.append((os.path.basename(p), classify_file(p)))
    dt = file_datetime(img, CAT_IMAGE, True)
    rel = date_rel_path(dt, GRAN_YEAR_MONTH_DAY)
    ok_cat = results[0][1] == CAT_IMAGE and results[1][1] == CAT_VIDEO and results[2][1] == CAT_OTHER
    ok_rel = rel == "%d-%02d/%d-%02d-%02d" % (dt.year, dt.month, dt.year, dt.month, dt.day)
    print("SELFTEST results:", results)
    print("SELFTEST date_rel:", rel)
    print("SELFTEST classify_ok:", ok_cat, "| date_rel_ok:", ok_rel)
    return ok_cat and ok_rel


GLOBAL_QSS = """
QMainWindow, QDockWidget { background: #0f0f10; color: #ededed; }
QMainWindow::separator { background: #2a2a2d; width: 2px; height: 2px; }
QToolBar { background: #141416; border: none; border-bottom: 1px solid rgba(255,255,255,0.06); spacing: 8px; padding: 6px 10px; }
QDockWidget { color: #ededed; font-size: 12px; }
QDockWidget::title { background: #141416; padding: 6px 10px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #8b8b90; font-weight: 600; }
QDockWidget > QWidget { background: #0f0f10; border: none; }
QLabel { color: #c9c9cc; background: transparent; }
QTextEdit { background: #131315; color: #c9c9cc; border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; selection-background-color: #333; padding: 4px; }
QProgressBar { background: #1a1a1d; border: none; border-radius: 4px; height: 6px; text-align: center; color: transparent; }
QProgressBar::chunk { background: #5e6ad2; border-radius: 4px; }
QPushButton { background: #1c1c1f; color: #ededed; border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; padding: 7px 12px; font-size: 12px; }
QPushButton:hover { background: #242427; border-color: rgba(255,255,255,0.16); }
QPushButton:pressed { background: #2a2a2d; }
QPushButton:disabled { color: #55555a; background: #17171a; border-color: rgba(255,255,255,0.05); }
QToolButton { background: transparent; color: #8b8b90; border: none; border-radius: 6px; padding: 6px 10px; font-size: 12px; }
QToolButton:hover { color: #ededed; background: rgba(255,255,255,0.06); }
QComboBox { background: #1c1c1f; color: #ededed; border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; padding: 5px 10px; }
QComboBox:hover { border-color: rgba(255,255,255,0.18); }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: #1a1a1d; color: #ededed; selection-background-color: #333; border: 1px solid rgba(255,255,255,0.08); }
QCheckBox, QRadioButton { color: #c9c9cc; font-size: 12px; spacing: 6px; }
QCheckBox::indicator, QRadioButton::indicator { width: 15px; height: 15px; border: 1px solid rgba(255,255,255,0.18); border-radius: 4px; background: #1c1c1f; }
QCheckBox::indicator:checked { background: #5e6ad2; border-color: #5e6ad2; }
QRadioButton::indicator { border-radius: 8px; }
QRadioButton::indicator:checked { background: #5e6ad2; border-color: #5e6ad2; }
QSlider::groove:horizontal { height: 4px; background: #2a2a2d; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #ededed; border: 1px solid rgba(255,255,255,0.2); }
QSlider::handle:horizontal:hover { background: #ffffff; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.14); border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.24); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: rgba(255,255,255,0.14); border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line:horizontal { width: 0; }
"""


def screenshot_mode():
    """--shot 出图时必须走软件渲染: QWidget.grab() 抓不到 QOpenGLWidget 的内容,
    否则截出来的画布永远是纯白(那张 shotg.png 就是这么骗过验收的: 画布 99.1% 白,
    实际屏幕上什么都没有)。"""
    return "--shot" in sys.argv


def run_legacy():
    """稳定版启动入口(命令行 --classic): 数据光带工作流。
    素材源居中 -> 左右目标文件夹对称, 光纤白光带实时输送, 真实反映落盘。"""
    init_diag()
    install_excepthook()
    log_s("== 启动(稳定版 · 数据光带工作流)")
    app = QApplication(sys.argv)
    install_qt_message_handler()
    app.setApplicationName("素材整理工作流")
    app.setStyleSheet(GLOBAL_QSS)
    log_s("QApplication 创建完成")
    win = MainWindow()
    log_s("主窗口已创建")
    win.show()
    log_s("主窗口已显示")
    if "--smoke" in sys.argv:
        QTimer.singleShot(4000, app.quit)
    if "--smoke-leave" in sys.argv:
        install_smoke_leave(app, win)
    if "--shot" in sys.argv:
        try:
            out = sys.argv[sys.argv.index("--shot") + 1]
        except IndexError:
            out = "shot.png"

        def grab():
            try:
                win.grab().save(out)
                log_s("已出图: %s" % out)
            finally:
                app.quit()
        QTimer.singleShot(1800, grab)
    log_s("进入事件循环")
    app.exec()
    log_s("事件循环退出, 正常结束")


def main():
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    if "--classic" in sys.argv:
        # 回退路径: 旧版稳定入口(固定三类 + 两个文件夹选择 + 光带动画)
        run_legacy()
        return
    # 默认入口: 多卡片可连线工作流编辑器(--editor 为等价别名)
    # 2026-10-16 的"用户机器一移鼠标就闪退"已定位并修复(leaveEvent 里 e.pos()),
    # 因此恢复为默认入口。
    from workflow_editor import run
    run()


if __name__ == "__main__":
    main()
