import threading
from server.flask_app import start_flask, cache
from server.clipboard_watcher import ClipboardWatcher
import time

# 启动 Flask 服务后台线程
flask_thread = threading.Thread(target=start_flask, daemon=True)
flask_thread.start()

# 启动剪贴板监听线程
watcher = ClipboardWatcher(cache)
watch_thread = threading.Thread(target=watcher.start, daemon=True)
watch_thread.start()

# 阻塞主线程
print("服务启动完成，Ctrl+C 停止")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("服务停止")

# import tkinter as tk
# from gui import settings, autorun
# import threading
# from server.flask_app import start_flask, cache
# from server.clipboard_watcher import ClipboardWatcher

# def main_menu():
#     # 启动 Flask 服务后台线程
#     flask_thread = threading.Thread(target=start_flask, daemon=True)
#     flask_thread.start()

#     # GUI 主菜单
#     root = tk.Tk()
#     root.title("SyncClipboard")
#     root.geometry("250x120")

#     menu_bar = tk.Menu(root)
#     root.config(menu=menu_bar)

#     file_menu = tk.Menu(menu_bar, tearoff=0)
#     file_menu.add_command(label="设置", command=settings.open_settings)
#     file_menu.add_separator()
#     file_menu.add_command(label="开机自启", command=lambda: autorun.set_autorun(True))
#     file_menu.add_command(label="取消开机自启", command=lambda: autorun.set_autorun(False))
#     file_menu.add_separator()
#     file_menu.add_command(label="退出", command=root.quit)
#     menu_bar.add_cascade(label="菜单", menu=file_menu)


#     # 启动本地剪贴板监听线程
#     watcher = ClipboardWatcher(cache)
#     watch_thread = threading.Thread(target=watcher.start, daemon=True)
#     watch_thread.start()

#     label = tk.Label(root, text="SyncClipboard 服务正在运行...", padx=10, pady=20)
#     label.pack()

#     root.mainloop()