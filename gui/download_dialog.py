import tkinter as tk
from tkinter import ttk


class DownloadProgressDialog:
    """下载进度对话框"""
    def __init__(self, title="下载进度"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("450x200")
        self.root.resizable(False, False)

        # 居中显示
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TProgressbar", thickness=20)

        self.label = tk.Label(self.root, text="正在下载文件...",font=("微软雅黑", 11, "bold"))
        self.label.pack(pady=(15, 10))

        progress_frame = tk.Frame(self.root)
        progress_frame.pack(fill=tk.X, padx=30, pady=5)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100,
            length=350, mode='determinate', style="TProgressbar"
        )
        self.progress_bar.pack(fill=tk.X)

        self.detail_label = tk.Label(self.root, text="准备下载...",
                                     font=("微软雅黑", 9))
        self.detail_label.pack(pady=10)

        button_frame = tk.Frame(self.root)
        button_frame.pack(pady=(5, 15))

        self.cancel_button = tk.Button(
            button_frame, text="取消下载", command=self.cancel,
            font=("微软雅黑", 10), width=12, height=1,
            bg="#f0f0f0", relief=tk.RAISED, cursor="hand2"
        )
        self.cancel_button.pack()

        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.cancelled = False
        self._running = True

    def update_progress(self, percentage, downloaded_mb, total_mb):
        """更新进度"""
        if not self._running:
            return
        try:
            self.progress_var.set(percentage)
            if total_mb > 0:
                self.detail_label.config(
                    text=f"下载进度：{percentage}%  ({downloaded_mb:.1f} MB / {total_mb:.1f} MB)"
                )
            else:
                self.detail_label.config(text=f"已下载：{downloaded_mb:.1f} MB")
            self.root.update()
        except:
            pass

    def cancel(self):
        """取消下载"""
        self.cancelled = True
        self._running = False
        try:
            self.root.destroy()
        except:
            pass

    def close(self):
        """关闭对话框"""
        self._running = False
        try:
            self.root.destroy()
        except:
            pass

    def is_cancelled(self):
        """检查是否已取消"""
        return self.cancelled