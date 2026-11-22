# -*- coding: utf-8 -*-
"""生成「获取同步」快捷指令的 plist。

流程
----
    配置词典 → 拼服务器地址 → POST /request_file
      ├─ type = text  → 内容进剪贴板
      └─ 其它         → 两段并列的单层循环
                         ① 逐项 kind = file，下载普通文件
                         ② 取 manifest_batch_url 的合并清单，逐条下载目录内容

    「存储到文件」写进「下载」，路径由根子路径 + 清单给的目录 + 文件名拼成。

配置词典（导入后可在快捷指令里直接改）
-------------------------------------
    协议 / 主机 / 端口   拼成 http://主机:端口
    密钥                请求头 key，与电脑端一致
    根子路径            落盘路径前缀。留空 = 直接存进所选目录
    调试                开 = 把过程 POST 到 /ios_log；关 = 只弹进度通知

服务端字段（只读，序号由服务端算好下发给手机）
-------------------------------------------
    /request_file
        type                text / file_list / folder
        files[]             待处理条目
            kind            file 或 folder
            download_url    普通文件的下载地址
            name / size     文件名与字节数
            index, total    普通文件序号（没有则通知不带序号）
        file_count          普通文件个数
        folder_count        目录个数
        manifest_batch_url  多个目录合并成的一张清单地址

    /manifest/batch
        root                清单根名，通知里显示
        entries[]           摊平后的文件条目
            dir             相对目录，含根目录名，末尾带 "/"
            prefix          同 dir 但去掉尾斜杠，拼落盘路径用
            name / url      文件名与下载地址
            index, total    跨目录连续序号
        empty_dirs[]        没有文件的空目录（无法只靠写文件重建）
        notice              服务端附带的提示语

构建
----
    python 获取同步/build.py
    python lib/verify.py 获取同步/获取同步-unsigned.plist
    python lib/sign.py   获取同步/获取同步-unsigned.plist
"""

import os
import sys
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))
SHORTCUTS_ROOT = os.path.dirname(HERE)      # shortcuts/
sys.path.insert(0, SHORTCUTS_ROOT)

from lib.sc import *  # noqa: E402,F401  (SC/tok/att/act_out/pick/var/dkey/OBJ/U)
from lib.sc import DEFAULT_META  # noqa: E402

OUT = os.path.join(HERE, '获取同步-unsigned.plist')

# ------------------------------------------------------------------ 构建流程

b = SC()
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
ROOT_SUB = '根子路径'    # 落盘路径前缀，留空 = 直接存进所选目录
DEBUG = '调试'           # 开 = 过程写入日志；关 = 只弹进度通知
REPEAT_ITEM = 'Repeat Item'

b.comment('SyncClipboard 拉取（逐文件保目录结构）\n'
          '配置：协议 / 主机 / 端口 / 密钥 / 根子路径 / 调试\n'
          '目录按清单逐文件下载，子目录按清单给的 dir 重建\n'
          '平时只弹进度通知；调试 = 开 时过程写入电脑端 log/ios_shortcut.log\n'
          '文件存入「下载」，要换目录请在「存储到文件」动作里重选')

cfg = b.dictionary([
    (PROTO, 'http'),
    (HOST, '192.168.1.23'),
    (PORT, '8000'),
    (KEY, '123456'),
    # 落盘路径前缀。留空即相对所选的「下载」；若 iOS 把它当成存储根相对路径，
    # 文件会落到「下载/下载/…」，那时填「下载/」，不必重新生成快捷指令。
    (ROOT_SUB, ''),
    # 开 = 每个节点把累积的日志 POST 到 /ios_log；关 = 不发请求、不写文件。
    (DEBUG, '关'),
], out_name='配置')

# ---- 1 地址 ---------------------------------------------------------------
b.comment('1. 服务器地址：协议 + 主机 + 端口')
server = b.text([act_out(cfg, '配置', dkey(PROTO)), '://',
                 act_out(cfg, '配置', dkey(HOST)), ':',
                 act_out(cfg, '配置', dkey(PORT))])
b.setvar('服务器', act_out(server, '文本'))
b.setvar('请求地址', act_out(b.text([var('服务器'), '/request_file']), '文本'))

# ---- 2 设备来源 -----------------------------------------------------------
b.comment('2. 设备名称：随请求放进 source 字段，电脑端据此区分设备')
b.setvar('来源', b.device_name())    # 只进 JSON body，不做 URL 编码

# ---- 运行日志（「调试」= 开 时才发出去） -----------------------------------
#
# 日志变量始终在拼（纯内存拼接，不发请求、不建文件），真正推给电脑端的是
# log_push，它被「如果 配置.调试 是 '开'」包住 —— 关着时一个字节都不产生。
#
# 走 HTTP（POST /ios_log）而不是「存储到文件」：后者拿纯文本时可能既不落盘
# 也不报错，日志就静默丢了；HTTP 和 /request_file 同一套动作，参数确定。
#
# 每次推的都是累积后的完整日志，服务端覆盖写同一个文件：中断在哪一步，电脑
# 上那份就停在哪一步。循环里每处理完一个文件推一次。
LOG_FILE = 'SyncClipboard-调试.log'      # 调试=开 时手机「下载」也留一份副本

b.setvar('日志', act_out(b.text(['SyncClipboard 同步日志\n']), '文本'))
b.setvar('日志地址', act_out(b.text([var('服务器'), '/ios_log']), '文本'))


def log(parts):
    """往「日志」变量追加一行（只拼字符串，不发请求）。

    累加源必须是「日志」变量本身，不能用「上一条文本动作的输出」：存盘结果
    那句日志写在「如果 / 否则」里，两条文本动作只会执行一条，若引用的是其中
    某一条，另一条执行时引用就是空的，日志会变成空串。

    `b.text()` 返回的是 UUID 字符串而不是动作输出引用，所以要包一层
    `act_out(...)` 再交给「设置变量」；漏包的话变量会被赋成空值。
    """
    b.setvar('日志', act_out(b.text([var('日志'), *parts, '\n']), '文本'))


def _debug_on():
    """「如果 配置.调试 是 开」—— 日志相关动作都放在这个条件里。"""
    return b.if_(act_out(cfg, '配置', dkey(DEBUG)), IS, '开')


def log_push():
    """把当前完整日志推给电脑端（POST /ios_log），仅调试=开 时执行。

    body 用 `{"log": 日志}`，请求头带 key 与 /request_file 相同。电脑端没有
    这个路由时只会返回错误内容，不会中断流程。
    """
    g = _debug_on()
    b.post_json(
        var('日志地址'),
        headers=[('key', act_out(cfg, '配置', dkey(KEY)))],
        body=[('log', var('日志'))],
    )
    b.endif(g)


def dbg(parts):
    """记一行日志并立刻推一次（中断在下一步也能看到已发生的事）。"""
    log(parts)
    log_push()


def notice(parts):
    """进度通知：无条件弹，与「调试」开关无关。

    内容为这次共几项、正在下载哪个、哪个已保存/保存失败。通知静音，堆在
    通知中心里，扫一眼就知道进度。
    """
    b.notify(parts)


def save_confirmed(subject, label_parts, dest=None):
    """存盘并核对真的落了盘：成功写「已保存」，失败写「保存失败」。

    「存储到文件」拿到空输入时不报错也不落盘，因此用它的输出作判据 ——
    有任何值就说明真的写了文件，没有就明确提示，避免对着空目录猜。
    """
    saved = b.save_to_folder(subject, dest=dest)
    g_ok = b.if_(saved, 100)                # 100 = 有任何值；101 = 无任何值
    log(['   OK 写入 ', *label_parts])
    notice(['已保存 ', *label_parts])
    b.else_(g_ok)
    log(['   !! 保存失败（动作输出为空，文件没落地）', *label_parts])
    notice(['保存失败：', *label_parts, '（文件没写进「下载」）'])
    b.endif(g_ok)

dbg(['=== SyncClipboard 同步开始 ==='])
dbg(['① 服务器 ', var('服务器'), ' ｜ 设备 ', var('来源')])

# ---- 3 拉取 ---------------------------------------------------------------
b.comment('3. 拉取（/request_file 一次消费：电脑端返回前已清空记录，'
          '所以只请求一次，之后全部读「响应」变量）')
resp = b.post_json(
    var('请求地址'),
    headers=[('key', act_out(cfg, '配置', dkey(KEY)))],
    body=[('type', 'file'), ('source', var('来源'))],
)
b.setvar('响应', resp)
b.setvar('类型', pick(resp, 'type'))
dbg(['② 响应 type=', var('类型')])
# 原始返回体也记一行：返回体里有没有 manifest_batch_url 等字段一目了然。
dbg(['② 原文 ', var('响应')])

# ---- 4 分支：纯文本 -------------------------------------------------------
g_text = b.if_(var('类型'), IS, 'text')
b.comment('4. 文本分支：latest_global.content 进剪贴板')
b.setvar('文本内容', pick(resp, 'latest_global', 'content'))
g_has_text = b.if_(var('文本内容'), 100)     # 100 = 有任何值；101 = 无任何值
b.clipboard(var('文本内容'))
b.notify(['已复制到剪贴板：', var('文本内容')])
b.else_(g_has_text)
# 文案说「内容」而不是「文本」：本分支也接收「这次没东西」，说「文本」会
# 让人以为文件丢了。
b.notify(['服务端没有新内容'])
b.endif(g_has_text)

b.else_(g_text)

# ---- 5 分支：文件 ---------------------------------------------------------
b.comment('5. 文件分支：先逐项下载普通文件（一层循环），再按合并清单'
          '逐条下载目录内容（另一层循环）。两段并列，不做嵌套')

g_kind = b.if_any(var('类型'), ['file_list', 'folder'])

b.setvar('文件列表', pick(resp, 'files'))
b.setvar('文件数量', act_out(b.count(var('文件列表')), '计数'))
dbg(['③ 待下载 ', var('文件数量'), ' 项'])
notice(['开始同步：共 ', var('文件数量'), ' 项'])

# --- 普通文件：一层循环，只挑 kind == file ---
#
# 循环体里不嵌套任何循环：iOS 的「重复每一项」嵌套时，内层取到的 Repeat Item
# 会落到外层条目上，字段取空。目录部分因此用下面的独立循环，两段并列，任何
# 时刻循环深度都只有 1。
#
# 判断「有没有内容」用「有任何值」（条件 100），不用数字比较 —— 数字比较遇到
# 取不到的字段（空值）时，iOS 会报「请为此操作中的每个参数选取值」并中断整条
# 指令。
g_has = b.if_(var('文件列表'), 100)

g_each = b.repeat_each(var('文件列表'))
item_kind = var(REPEAT_ITEM, dkey('kind'))
g_is_file = b.if_(item_kind, IS, 'file')
f_url = var(REPEAT_ITEM, dkey('download_url'))
f_name = var(REPEAT_ITEM, dkey('name'))
log(['· 文件 ', f_name])
# 下载前先弹一条：大文件耗时较长，光看「开始同步」不知道卡在哪个文件上。
# index / total 由服务端给，拼成「第 3/10 个」；字段缺省时退回不带序号的写法。
g_f_idx = b.if_(var(REPEAT_ITEM, dkey('total')), 100)
notice(['正在下载 第 ', var(REPEAT_ITEM, dkey('index')), '/',
        var(REPEAT_ITEM, dkey('total')), ' 个：', f_name])
b.else_(g_f_idx)
notice(['正在下载：', f_name])
b.endif(g_f_idx)
# 普通文件没有子目录，dest = 根子路径 + 文件名；根子路径留空时只剩文件名，
# 所以这里不额外加字面 "/"，免得拼出前导斜杠。
save_confirmed(b.get(f_url), [f_name],
               dest=[act_out(cfg, '配置', dkey(ROOT_SUB)), f_name])
b.endif(g_is_file)
b.end_repeat(g_each, each=True)

# 收尾标记：只有「OK 写入」的话，看不出是正常跑完还是半路中断。
log(['③ 文件同步结束：', var('文件数量'), ' 个'])
log_push()
b.endif(g_has)

# --- 目录：服务端已把所有目录合并成一张清单，这里同样只有一层循环 ---
b.comment('目录：合并清单 → 逐条下载 → 按清单的 dir 写进「下载/目录名/子目录」\n'
          '清单已摊平（dir 含目录名），所以这里只有一层循环')
b.setvar('清单地址', pick(resp, 'manifest_batch_url'))

g_has_list = b.if_(var('清单地址'), 100)        # 有任何值 = 这次带了目录

listing = b.get(var('清单地址'))
b.setvar('清单', listing)

g_listing_ok = b.if_(pick(listing, 'status'), IS, 'ok')

b.setvar('清单根', pick(listing, 'root'))
b.setvar('清单文件数', act_out(b.count(pick(listing, 'entries')), '计数'))
b.setvar('空目录数', act_out(b.count(pick(listing, 'empty_dirs')), '计数'))
dbg(['④ 清单 ', var('清单根'), ' ｜ ', var('清单文件数'), ' 个文件 ｜ 空目录 ',
     var('空目录数')])
# 目录总览：光看「开始同步 共 1 项」不知道里面装着几个文件。
notice(['目录 ', var('清单根'), '：共 ', var('清单文件数'), ' 个文件'])

g_notice = b.if_(pick(listing, 'notice'), 100)  # 有任何值 = 服务端有提示语
b.notify([pick(listing, 'notice')])
b.endif(g_notice)

g_has_files = b.if_(pick(listing, 'entries'), 100)

g_each_file = b.repeat_each(pick(listing, 'entries'))

entry_dir = var(REPEAT_ITEM, dkey('dir'))
entry_prefix = var(REPEAT_ITEM, dkey('prefix'))
entry_name = var(REPEAT_ITEM, dkey('name'))
entry_url = var(REPEAT_ITEM, dkey('url'))
log(['· ', entry_name, ' ｜ dir=', entry_dir, ' ｜ prefix=', entry_prefix])
# 带序号的进度：目录里文件重名时也能分清是哪一个；分母是这次要传的文件总数。
g_e_idx = b.if_(var(REPEAT_ITEM, dkey('total')), 100)
notice(['正在下载 第 ', var(REPEAT_ITEM, dkey('index')), '/',
        var(REPEAT_ITEM, dkey('total')), ' 个：', entry_name])
b.else_(g_e_idx)
notice(['正在下载：', entry_name])
b.endif(g_e_idx)

g_has_url = b.if_(entry_url, 100)

# ---- 核心：dest = 根子路径 + prefix + "/" + 文件名 ----
#
# prefix 是去掉末尾斜杠的相对目录（含根目录名），斜杠由这里的字面 "/" 提供。
# 不能依赖 prefix 自带的尾斜杠：iOS 会先规范化路径，变量值末尾的 "/" 会被
# 吃掉，`目录名/` 于是整串变成文件名 `目录名`，多个文件互相覆盖后「下载」里
# 只剩下一个以目录名命名的文件。
g_has_prefix = b.if_(entry_prefix, 100)
save_confirmed(
    b.get(entry_url), [entry_prefix, '/', entry_name],
    dest=[act_out(cfg, '配置', dkey(ROOT_SUB)),
          entry_prefix, '/', entry_name])

b.else_(g_has_prefix)
# 清单里没有 prefix 时退回用 dir 拼：dir 末尾自带 "/"，多出来那一个 iOS 会
# 合并，层级仍然正确。
save_confirmed(
    b.get(entry_url), [entry_dir, entry_name],
    dest=[act_out(cfg, '配置', dkey(ROOT_SUB)),
          entry_dir, '/', entry_name])
b.endif(g_has_prefix)

b.else_(g_has_url)
log(['   跳过（清单条目没有下载地址）：', entry_name])
b.endif(g_has_url)

log_push()           # 每处理完一个文件推一次，中断在下一个也不丢记录
b.end_repeat(g_each_file, each=True)

dbg(['⑤ 同步结束：', var('清单文件数'), ' 个文件'])
b.notify(['同步完成：', var('清单文件数'), ' 个文件已存入「下载」'])
g_has_empty = b.if_(pick(listing, 'empty_dirs'), 100)
b.notify([var('空目录数'), ' 个空目录没有文件可写，未能重建（其余结构已保留）'])
b.endif(g_has_empty)

b.else_(g_has_files)
b.notify(['目录是空的：', var('清单根')])
b.endif(g_has_files)

b.else_(g_listing_ok)
b.notify(['取不到目录清单：', pick(listing, 'message'),
          '\n（共享可能已过期，请回电脑重新复制）'])
b.endif(g_listing_ok)

b.else_(g_has_list)
# 没拿到合并清单地址。判断「这次带没带目录」不用数字比较：folder_count 缺省时
# 取出来是空值，空值做「大于 0」会让 iOS 中断整条指令；空值配「有任何值」安全。
#   有 manifest_batch_error → 电脑端明确说合并不了，原样转达；
#   没有 + 类型 = folder    → 带目录却没有合并清单 = 电脑端未提供该字段；
#   没有 + 类型 = file_list → 复制的是普通文件，本来就没有清单，静默。
g_batch_err = b.if_(pick(resp, 'manifest_batch_error'), 100)
b.notify([pick(resp, 'manifest_batch_error')])
b.else_(g_batch_err)
g_old_server = b.if_(var('类型'), IS, 'folder')
b.notify(['这次带了目录，但电脑端没给出合并清单 —— 请把电脑端升级到最新版'
          '（git pull 后重启程序）'])
b.endif(g_old_server)
b.endif(g_batch_err)

b.endif(g_has_list)

b.else_(g_kind)
b.notify(['请求失败：', var('响应', dkey('message'))])
b.endif(g_kind)

b.endif(g_text)

# 结尾：调试=开 时把整份日志再存一份到「下载」，方便自己翻。不校验、不提示：
# 能存就存，存不上也不影响排查，电脑端 log/ios_shortcut.log 才是权威副本。
g_dbg_file = _debug_on()
b.save_to_folder(var('日志'), dest=[LOG_FILE])
b.endif(g_dbg_file)

b.comment('结束。平时看进度通知即可；排查时把「配置」里的「调试」改成 开，'
          '过程会写进电脑端 log/ios_shortcut.log')

# ------------------------------------------------------------------ 输出

meta = dict(DEFAULT_META)
meta['WFWorkflowActions'] = b.a

with open(OUT, 'wb') as f:
    plistlib.dump(meta, f, fmt=plistlib.FMT_XML, sort_keys=False)

print('动作数:', len(b.a))
print('输出:', OUT, os.path.getsize(OUT), 'B')
