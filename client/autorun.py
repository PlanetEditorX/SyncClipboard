import os
import sys
import winreg                   # 导入Windows注册表操作模块
from tkinter import messagebox  # 从tkinter中导入消息弹窗组件

def set_autorun(enable=True):
    """设置开机自启"""
    # 获取当前脚本/程序的绝对路径，sys.argv[0]是脚本文件名
    exe_path = os.path.abspath(sys.argv[0])
    # 定义注册表中“当前用户”的开机启动项路径
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    # 在注册表中显示的项名称（用于标识该自启项）
    app_name = "SyncClipboard"

    try:
        # 打开HKEY_CURRENT_USER下的Run注册表项，权限为完全控制
        reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            # 设置注册表项：键名app_name，类型REG_SZ（字符串），值为exe_path
            winreg.SetValueEx(reg_key, app_name, 0, winreg.REG_SZ, exe_path)
            messagebox.showinfo("开机自启", "开机自启已开启")
        else:
            # 删除注册表中对应app_name的键值
            winreg.DeleteValue(reg_key, app_name)
            messagebox.showinfo("开机自启", "开机自启已关闭")
        # 关闭注册表句柄（释放资源）
        winreg.CloseKey(reg_key)
    except Exception as e:
        messagebox.showerror("错误", f"设置开机自启失败:\n{str(e)}")