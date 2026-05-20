import tkinter as tk
from gui import settings, autorun
import threading
from server.flask_app import start_flask

def main_menu():
    # 启动 Flask 服务后台线程
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # GUI 主菜单
    root = tk.Tk()
    root.title("SyncClipboard")
    root.geometry("250x120")

    menu_bar = tk.Menu(root)
    root.config(menu=menu_bar)

    file_menu = tk.Menu(menu_bar, tearoff=0)
    file_menu.add_command(label="设置", command=settings.open_settings)
    file_menu.add_separator()
    file_menu.add_command(label="开机自启", command=lambda: autorun.set_autorun(True))
    file_menu.add_command(label="取消开机自启", command=lambda: autorun.set_autorun(False))
    file_menu.add_separator()
    file_menu.add_command(label="退出", command=root.quit)
    menu_bar.add_cascade(label="菜单", menu=file_menu)

    label = tk.Label(root, text="SyncClipboard 服务正在运行...", padx=10, pady=20)
    label.pack()

    root.mainloop()