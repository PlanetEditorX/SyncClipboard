"""目录清单：逐文件传输时，把目录树摊平成「相对路径 + 体积」列表。

为什么需要它
------------
iOS 快捷指令的「解压缩」把压缩包还原成一串**平铺**的文件，包内目录层级
在解压那一步就丢了 —— 接收端看到的是一堆散装文件，全落进同一个目录。
只要还经过压缩包这一层，无论包内多规整都没用。

逐文件传输干脆不打包：服务端先给一份清单（相对路径 + 每项的下载地址），
接收端按清单逐项下载，并用「存储到文件」的子路径参数写回对应子目录。
代价是请求数变多，换来的是目录结构、空目录位置、单文件失败可续传。

设计约定
--------
`dir` 一律以 "/" 结尾（根目录下的文件是空串），这样接收端拿到就能直接当
子路径用，不必在快捷指令里拼字符串 —— 快捷指令里拼路径既难写又难调。
"""

import os

# 清单条数上限。逐文件传输的耗时随文件数线性增长，上万个文件在手机上
# 逐个下载已经不现实，超出部分截断并置 truncated，让接收端提示改用压缩包。
MANIFEST_MAX_ENTRIES = 20000


def _posix(path):
    return path.replace(os.sep, "/")


def _rel_dir(rel_root):
    """os.walk 给出的相对目录 → 清单里的 dir 字段（以 "/" 结尾）。"""
    if rel_root in ("", "."):
        return ""
    return _posix(rel_root).rstrip("/") + "/"


def build_manifest(folder_path, max_entries=MANIFEST_MAX_ENTRIES):
    """扫描目录，返回清单 dict；目录不存在返回 None。

    返回：::

        {"root": "项目名", "count": 3, "total_size": 12345,
         "dirs": ["sub/", "sub/deep/"], "truncated": False,
         "entries": [{"path": "sub/a.txt", "dir": "sub/",
                      "name": "a.txt", "size": 10}, ...]}

    - 符号链接不进清单：它可能指向目录外，逐文件下载时会撞上安全校验，
      与其让接收端下到一半报错，不如一开始就不列。
    - 读不到体积的文件跳过（被独占/权限不足），不计入 count。
    """
    folder_path = os.path.normpath(folder_path)
    if not os.path.isdir(folder_path):
        return None

    root_name = os.path.basename(folder_path) or os.path.splitdrive(folder_path)[0] \
        .rstrip(":\\/") or "folder"

    entries = []
    dirs = []
    total = 0
    truncated = False

    for root, subdirs, names in os.walk(folder_path, followlinks=False):
        rel_root = os.path.relpath(root, folder_path)
        rel_dir = _rel_dir(rel_root)
        if rel_dir:
            dirs.append(rel_dir)
        # 剪掉指向别处的符号链接目录，后面的子目录也不会被走进去
        subdirs[:] = [d for d in subdirs
                      if not os.path.islink(os.path.join(root, d))]
        for name in names:
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append({
                "path": rel_dir + name,
                "dir": rel_dir,
                "name": name,
                "size": size,
            })
            total += size
        if truncated:
            break

    dirs.sort()
    empty = [d for d in dirs
             if not any(e["dir"].startswith(d) and e["dir"] != d
                        or e["dir"] == d for e in entries)]
    return {
        "status": "ok",
        "root": root_name,
        "count": len(entries),
        "total_size": total,
        "dirs": dirs,
        # 空目录没有文件可写，接收端无法凭空建出来（「存储到文件」只建
        # 写入时经过的那几层），这里单列出来，便于提示/后续补建。
        "empty_dirs": empty,
        "truncated": truncated,
        # 需要用户知道的问题，直接给成一句空/非空文本：接收端（快捷指令）
        # 判断「有任何值」比判断 JSON 布尔值可靠得多。
        "notice": (f"目录文件超过 {max_entries} 个，清单已截断，"
                   f"请改用压缩包方式整包传输") if truncated else "",
        "entries": entries,
    }
