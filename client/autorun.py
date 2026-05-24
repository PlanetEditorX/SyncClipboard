import os
import sys
import winreg
from tkinter import messagebox

def set_autorun(enable=True):
    exe_path = os.path.abspath(sys.argv[0])
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "SyncClipboard"

    try:
        reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            winreg.SetValueEx(reg_key, app_name, 0, winreg.REG_SZ, exe_path)
            messagebox.showinfo("开机自启", "开机自启已开启")
        else:
            winreg.DeleteValue(reg_key, app_name)
            messagebox.showinfo("开机自启", "开机自启已关闭")
        winreg.CloseKey(reg_key)
    except Exception as e:
        messagebox.showerror("错误", f"设置开机自启失败:\n{str(e)}")