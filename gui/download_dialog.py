import tkinter as tk
from tkinter import ttk
import logging

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

    # ---------- 以下方法全部改为线程安全 ----------
    def reset(self, filename, total_size=0):
        """切换到下一个文件：更新标题和标签，重置进度（线程安全）"""
        def _update():
            if not self.window.winfo_exists():
                return
            self.label.config(text=f"正在下载：{filename}")
            self.progress_var.set(0)
            self.detail_label.config(text="准备下载...")
            # 注意：不要在 after 回调里直接 update()，由主循环自动刷新
        self.window.after(0, _update)

    def update_progress(self, percentage, downloaded_mb, total_mb):
        """更新进度（线程安全）"""
        def _update():
            if not self.window.winfo_exists():
                return
            self.progress_var.set(percentage)
            if total_mb > 0:
                self.detail_label.config(
                    text=f"下载进度：{percentage}%  ({downloaded_mb:.1f} MB / {total_mb:.1f} MB)"
                )
            else:
                self.detail_label.config(text=f"已下载：{downloaded_mb:.1f} MB")
        self.window.after(0, _update)

    def cancel(self):
        """取消下载（线程安全）"""
        self.cancelled = True
        self._running = False
        def _destroy():
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
            except:
                pass
        self.window.after(0, _destroy)

    def close(self):
        """关闭对话框（线程安全）"""
        self._running = False
        def _destroy():
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
            except:
                pass
        self.window.after(0, _destroy)

    def is_cancelled(self):
        return self.cancelled