"""服务操作的串行执行队列。

启停服务会派生 / 回收子进程，必须串行；退出动作还要排在已接受的操作之后。
这段逻辑与界面无关，所以独立成一个类：

- 原来的实现直接挂在 TrayManager 上，而 TrayManager 是 QObject 子类，
  测试里只能用 ``__new__`` 伪造半初始化实例，容易把 Qt 运行时搞崩；
- 拆出来之后这里只有 Python 线程与队列，测试不需要图形环境。
"""

import logging
import queue
import threading

logger = logging.getLogger("gui")


class ServiceActionQueue:
    """把启停、重启、退出按 FIFO 顺序串行执行。"""

    def __init__(self, on_change=None):
        self._busy = False
        self._quitting = False
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._on_change = on_change
        self._thread = threading.Thread(
            target=self._run, name="service-control", daemon=True
        )
        self._thread.start()

    @property
    def busy(self):
        """有操作在执行，或已进入退出流程。"""
        with self._lock:
            return self._busy or self._quitting

    @property
    def quitting(self):
        with self._lock:
            return self._quitting

    def submit(self, action):
        """提交一个普通服务操作；忙碌时直接拒绝。"""
        with self._lock:
            if self._busy or self._quitting:
                return False
            self._busy = True
            self._queue.put((action, False))
        self._notify()
        return True

    def shutdown(self, action):
        """提交退出动作；它会排在已接受的操作之后，且只执行一次。"""
        with self._lock:
            if self._quitting:
                return False
            self._quitting = True
            self._busy = True
            self._queue.put((action, True))
        self._notify()
        return True

    def join(self, timeout=None):
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _notify(self):
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            logger.exception("刷新界面回调失败")

    def _run(self):
        while True:
            action, is_shutdown = self._queue.get()
            try:
                action()
            except Exception:
                logger.exception("%s失败", "退出操作" if is_shutdown else "服务操作")
            finally:
                if not is_shutdown:
                    with self._lock:
                        if not self._quitting:
                            self._busy = False
                    self._notify()
                self._queue.task_done()
            if is_shutdown:
                return
