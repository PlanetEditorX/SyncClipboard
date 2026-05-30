import tkinter as tk
from tkinter import ttk
import logging
from common.utils import post_to_main_thread

_GLOBAL_TK_ROOT = None
logger = logging.getLogger("gui")

def _get_global_root():
    global _GLOBAL_TK_ROOT
    if _GLOBAL_TK_ROOT is None:
        _GLOBAL_TK_ROOT = tk.Tk()
        _GLOBAL_TK_ROOT.withdraw()
    return _GLOBAL_TK_ROOT


class DownloadProgressDialog:
    """下载进度对话框（支持复用，线程安全）"""
    def __init__(self, title="下载进度", master=None):
        if master is None:
            master = _get_global_root()
        self.master = master
        self.window = tk.Toplevel(master)
        self.window.title(title)
        self.window.geometry("450x200")
        self.window.resizable(False, False)

        # 居中显示
        self.window.update_idletasks()
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() // 2) - (width // 2)
        y = (self.window.winfo_screenheight() // 2) - (height // 2)
        self.window.geometry(f'{width}x{height}+{x}+{y}')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TProgressbar", thickness=20)

        self.label = tk.Label(self.window, text="正在下载文件...", font=("微软雅黑", 11, "bold"))
        self.label.pack(pady=(15, 10))

        progress_frame = tk.Frame(self.window)
        progress_frame.pack(fill=tk.X, padx=30, pady=5)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100,
            length=350, mode='determinate', style="TProgressbar"
        )
        self.progress_bar.pack(fill=tk.X)

        self.detail_label = tk.Label(self.window, text="准备下载...", font=("微软雅黑", 9))
        self.detail_label.pack(pady=10)

        button_frame = tk.Frame(self.window)
        button_frame.pack(pady=(5, 15))

        self.cancel_button = tk.Button(
            button_frame, text="取消下载", command=self.cancel,
            font=("微软雅黑", 10), width=12, height=1,
            bg="#f0f0f0", relief=tk.RAISED, cursor="hand2"
        )
        self.cancel_button.pack()

        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.cancelled = False
        self._running = True   # 控件初始化完毕即标记为运行中

    def reset(self, filename, total_size=0):
        def _update():
            if not self.window.winfo_exists():
                return
            self.label.config(text=f"正在下载：{filename}")
            self.progress_var.set(0)
            self.detail_label.config(text="准备下载...")
            self.window.update_idletasks()

        from common.utils import post_to_main_thread_no_wait
        post_to_main_thread_no_wait(_update)

    def update_progress(self, percentage, downloaded_mb, total_mb):
        """更新进度（线程安全，异步，不阻塞下载线程）"""
        def _update():
            if not self.window.winfo_exists():
                return
            self.progress_bar["value"] = percentage
            if total_mb > 0:
                self.detail_label.config(
                    text=f"下载进度：{percentage}% ({downloaded_mb:.1f} MB / {total_mb:.1f} MB)"
                )
            else:
                self.detail_label.config(
                    text=f"已下载：{downloaded_mb:.1f} MB"
                )
            # 强制刷新
            self.window.update_idletasks()

        # 异步投递，不等待
        from common.utils import post_to_main_thread_no_wait
        post_to_main_thread_no_wait(_update)

    def cancel(self):
        """取消下载"""
        self.cancel_event = threading.Event()
        self._running = False
        def _destroy():
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
            except:
                pass
        post_to_main_thread(_destroy)

    def close(self):
        """关闭对话框"""
        self._running = False
        def _destroy():
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
            except:
                pass
        post_to_main_thread(_destroy)

    def is_cancelled(self):
        return self.cancelled