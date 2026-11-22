# -*- coding: utf-8 -*-
"""从头重构「获取同步」iOS 快捷指令。

设计要点
--------
1. 单一入口：一次 /request_file，返回体固化成「响应」变量，后续全部读它。
2. 结构收敛：type == text 走文本分支，否则走文件分支；文件分支内按
   Repeat Item.kind 判断目录/文件，因此「单目录」「多文件」「目录+文件混合」
   三种情况共用同一段下载循环。
3. 目录只走「逐文件」：先取 /manifest/batch 的**合并清单**，再按清单逐项
   下载、按清单给的 dir 写进对应子目录。**没有任何打包/解压环节** —— iOS 的
   「解压缩」会把包内目录摊平，走归档必然丢层级（v3 的真机现象：所有文件都
   躺在「下载」根下）。
4. **循环永远只有一层**。iOS 的「重复每一项」嵌两层时，内层拿到的 Repeat
   Item 是空的（v5 真机现象：内层取 url 得空，报「未指定 URL」）。所以合并
   清单在服务端就把「目录 → 文件」摊平了，快捷指令里只有并列的两段单层循环：
   一段处理普通文件，一段处理目录清单。
5. **两套输出，按需切换**：
   - 进度通知（默认，无条件弹）：开始时报一共几项、目录里几个文件；每个文件
     下载前弹「正在下载 X」，落盘后弹「已保存 X」或「保存失败 X」。静音通知，
     堆在通知中心里，随时能看进度。
   - 运行日志（配置里「调试」= 开 才写）：每个节点把累积后的完整日志 POST 到
     `/ios_log`，服务端覆盖写 `<项目>/log/ios_shortcut.log`。不走 iOS 的
     「存储到文件」—— 那条路保存纯文本会静默失败（v7 真机：一条日志都没有）。
     关着的时候一个字节都不产生，连请求都不发。
6. 目录传输不需要等待：清单请求即取，没有打包轮询。
7. **全程不做数字比较**。`如果 X 大于 0` 在 X 是空值（字段不存在）时，
   iOS 会弹「请为此操作中的每个参数选取值」并**中断整条指令** —— v8 真机就
   卡在「如果 目录数 大于 0」这一步，后面的结束提示、日志落盘全没跑。判断
   「有没有内容」一律用「有任何值」（条件 100），空值走假分支，安全。
8. 子路径必须「字面斜杠 + 不带斜杠的 prefix」拼接。iOS 的「存储到文件」
   会对子路径做规范化，变量值结尾的 "/" 会被吃掉：`目录名/` 退化成一个叫
   `目录名` 的文件名，配合覆盖开关就变成「只剩一个叫目录名的文件」（v6
   真机现象）。所以服务端多给一个去掉尾斜杠的 `prefix`，斜杠由字面文本
   提供，不依赖变量值的末尾字符。

用法
----
    python 获取同步/build.py             # 生成 unsigned plist（同目录）
    python lib/verify.py 获取同步/获取同步-unsigned.plist
    python lib/sign.py 获取同步/获取同步-unsigned.plist
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
ROOT_SUB = '根子路径'    # 落盘子路径的前缀（见 cfg 里的说明）
DEBUG = '调试'           # 开着才写日志文件；默认关，平时只弹进度通知
REPEAT_ITEM = 'Repeat Item'

b.comment('SyncClipboard 拉取（逐文件保目录结构）\n'
          '改配置：编辑下面「配置」词典里的协议/主机/端口/密钥\n'
          '目录按清单逐个文件取，目录结构与电脑上完全一致（不再打包/解压）\n'
          '平时只弹进度通知（几个文件、正在下载谁、已保存谁）\n'
          '出问题排查时把「调试」改成 开，才会写日志文件\n'
          '文件统一存入「下载」，需要换目录请在「存储到文件」动作里重选')

cfg = b.dictionary([
    (PROTO, 'http'),
    (HOST, '192.168.1.23'),
    (PORT, '8000'),
    (KEY, '123456'),
    # 落盘路径的前缀。留空即「相对所选的『下载』文件夹」；万一你的 iOS 把
    # 它当成存储根相对路径，文件会落到「下载/下载/项目名/…」，那时把它填成
    # 「下载/」即可，不必重新生成快捷指令。
    (ROOT_SUB, ''),
    # 日常关：一个日志字节都不产生，只留进度通知。要发日志给我排查时才改「开」。
    (DEBUG, '关'),
], out_name='配置')

# ---- 1 地址 ---------------------------------------------------------------
b.comment('1. 服务器地址')
server = b.text([act_out(cfg, '配置', dkey(PROTO)), '://',
                 act_out(cfg, '配置', dkey(HOST)), ':',
                 act_out(cfg, '配置', dkey(PORT))])
b.setvar('服务器', act_out(server, '文本'))
b.setvar('请求地址', act_out(b.text([var('服务器'), '/request_file']), '文本'))

# ---- 2 设备来源 -----------------------------------------------------------
b.comment('2. 设备来源（随请求带上，便于服务端区分设备）')
b.setvar('来源', b.device_name())    # 只进 JSON body，不做 URL 编码

# ---- 运行日志（只在「调试」= 开 时才写） -----------------------------------
#
# 配置里的「调试」默认 **关**：一个日志字节都不产生，界面上只留进度通知。
# 要排查问题时改成「开」，才会把过程推给电脑端写成
# `<项目>/log/ios_shortcut.log`，并在手机「下载」留一份副本。
#
# 为什么走 HTTP 而不是「存储到文件」写进手机「下载」：v7 真机证明那条路会
# **静默失败** —— 既没有文件，也没有任何提示。日志是排查的唯一抓手，它自己
# 静默丢一次，人就只能干瞪眼。HTTP 通道和 /request_file 同一套，参数早就跑通。
#
# 每次推的都是**累积后的完整日志**，服务端覆盖写同一个文件：崩在哪一步，电脑
# 上那份就停在哪一步。循环里每处理完一个文件推一次，所以第一条出问题的记录
# 一定留得住。
LOG_FILE = 'SyncClipboard-调试.log'      # 调试=开 时同时留一份到手机「下载」

b.setvar('日志', act_out(b.text(['SyncClipboard 同步日志\n']), '文本'))
b.setvar('日志地址', act_out(b.text([var('服务器'), '/ios_log']), '文本'))


def log(parts):
    """往「日志」变量追加一行（纯内存操作，不落盘、不影响流程）。

    写完只在调试开关打开时才由 log_push 真正发出去 —— 关着的时候这几步
    只是把字符串拼在变量里，不发请求、不建文件。

    这里踩过两个坑，都写下来免得再犯：

    1. `b.text()` 返回的是 **UUID 字符串**，不是动作输出引用。直接丢给
       「设置变量」，写进去的输入就是坏的，变量被赋成空值 —— v9 真机上每次
       `POST /ios_log` 的正文都是 `{"log":""}`，日志一个字节没上来，界面上
       还毫无提示（v9 的 `att()` 现在会直接报错拦住这种写法）。
    2. **别用「上一条文本动作的输出」当累加源**。看着更稳，其实有个致命缺陷：
       存盘结果那条日志写在「如果 / 否则」里，两条文本动作只有一条会执行，
       而生成顺序上记住的是**最后造的那条**（失败分支）。存盘成功时它没跑，
       引用就是空的 —— v10 真机：前面的日志都上来了，一到循环里存完盘就推
       了一个空 body，服务端 400，整条指令跟着中断。

    累加只认「日志」这个变量：分支里怎么走，它都是最后被赋的那份完整内容。
    """
    b.setvar('日志', act_out(b.text([var('日志'), *parts, '\n']), '文本'))


def _debug_on():
    """「调试」= 开 时进入下面这段，否则整段跳过（什么都不发生）。"""
    return b.if_(act_out(cfg, '配置', dkey(DEBUG)), IS, '开')


def log_push():
    """把当前完整日志推给电脑端 —— 只有「调试」= 开 时才推。

    关着的时候连请求都不发：不占网络、不建文件，也就不会在电脑上留一堆
    用户根本不需要的日志。
    """
    g = _debug_on()
    b.post_json(
        var('日志地址'),
        headers=[('key', act_out(cfg, '配置', dkey(KEY)))],
        body=[('log', var('日志'))],
    )
    b.endif(g)


def dbg(parts):
    """记一行日志并立刻推给电脑端（崩在下一步也能看到已发生的事）。"""
    log(parts)
    log_push()


def notice(parts):
    """进度通知：**无条件弹**，和调试开关无关。

    用户要的是随时知道「这次一共几个文件、现在在下哪个、哪个已经存好了」，
    这些不是调试信息，是正常使用时的基本反馈。通知是静音的，会堆在通知
    中心里，扫一眼就知道进度。
    """
    b.notify(parts)


def save_confirmed(subject, label_parts, dest=None):
    """存盘并核对真的落了盘，结果写日志 + 弹通知。

    「存储到文件」拿到空输入时**不报错也不存盘**，直接在后面接一句「已保存」
    就是自欺欺人。这里拿存盘动作自己的输出做判据（它有输出就说明真写了），
    没有就明确告诉用户「保存失败」，而不是让人对着空目录猜。
    """
    saved = b.save_to_folder(subject, dest=dest)
    g_ok = b.if_(saved, 100)                # 有任何值 = 确实写入了
    log(['   OK 写入 ', *label_parts])
    notice(['已保存 ', *label_parts])
    b.else_(g_ok)
    log(['   !! 保存失败（动作输出为空，文件没落地）', *label_parts])
    notice(['保存失败：', *label_parts, '（文件没写进「下载」）'])
    b.endif(g_ok)

dbg(['=== SyncClipboard 同步开始 ==='])
dbg(['① 服务器 ', var('服务器'), ' ｜ 设备 ', var('来源')])

# ---- 3 拉取 ---------------------------------------------------------------
b.comment('3. 拉取最新内容（/request_file 一次性消费：服务端返回前已清空记录，'
          '所以只请求一次，之后全部读「响应」变量）')
resp = b.post_json(
    var('请求地址'),
    headers=[('key', act_out(cfg, '配置', dkey(KEY)))],
    body=[('type', 'file'), ('source', var('来源'))],
)
b.setvar('响应', resp)
b.setvar('类型', pick(resp, 'type'))
dbg(['② 响应 type=', var('类型')])
# 把原始返回体也记一行：出问题时一眼能看出服务端到底给了哪些字段（有没有
# manifest_batch_url / manifest_batch_error），不用再猜版本。
dbg(['② 原文 ', var('响应')])

# ---- 4 分支：纯文本 -------------------------------------------------------
g_text = b.if_(var('类型'), IS, 'text')
b.comment('4. 纯文本分支：直接进剪贴板')
b.setvar('文本内容', pick(resp, 'latest_global', 'content'))
g_has_text = b.if_(var('文本内容'), 100)     # 100 = 有任何值；101 = 无任何值
b.clipboard(var('文本内容'))
b.notify(['已复制到剪贴板：', var('文本内容')])
b.else_(g_has_text)
# 措辞别写「新文本」：文本分支只是「这次没东西」的一种，复制了文件却来这边
# 走一遭时说「没有新文本」会让人以为文件丢了。统一说「内容」。
b.notify(['服务端没有新内容'])
b.endif(g_has_text)

b.else_(g_text)

# ---- 5 分支：文件 ---------------------------------------------------------
b.comment('5. 文件分支：先逐个下载普通文件（一层循环），再按合并清单'
          '逐文件下载目录内容（另一层循环）。两段并列，不做嵌套')

g_kind = b.if_any(var('类型'), ['file_list', 'folder'])

b.setvar('文件列表', pick(resp, 'files'))
b.setvar('文件数量', act_out(b.count(var('文件列表')), '计数'))
dbg(['③ 待下载 ', var('文件数量'), ' 项'])
# 进度通知从这里开始，一直弹到结束 —— 用户随时能知道这次有几项、正在下谁。
notice(['开始同步：共 ', var('文件数量'), ' 项'])

# --- 普通文件：一层循环，只挑 kind == file ---
#
# 循环体里**不嵌套任何循环**。iOS 的「重复每一项」嵌两层时，内层拿到的
# Repeat Item 是空的 —— v5 的真机现象就是内层取 `url` 得到空值，「获取 URL
# 内容」直接报「未指定 URL」。所以目录那部分挪到下面的独立循环里：两段并列，
# 任何时刻的循环深度都只有 1。
# 判断「有没有文件」用「有任何值」，**不做数字比较** —— 见下面目录分支的
# 长注释：数字比较一旦碰上取不到的字段，iOS 会直接中断整条指令。
g_has = b.if_(var('文件列表'), 100)

g_each = b.repeat_each(var('文件列表'))
item_kind = var(REPEAT_ITEM, dkey('kind'))
g_is_file = b.if_(item_kind, IS, 'file')
f_url = var(REPEAT_ITEM, dkey('download_url'))
f_name = var(REPEAT_ITEM, dkey('name'))
log(['· 文件 ', f_name])
# 下载前先弹一条：大文件要几十秒，光看「开始同步」不知道卡在哪个文件上。
# 服务端会带 index/total（1.12.10 起），拼成「第 3/10 个」就是进度条；
# 电脑端是旧版没这两个字段时，退回不带序号的写法，不显示「第 /个」。
g_f_idx = b.if_(var(REPEAT_ITEM, dkey('total')), 100)
notice(['正在下载 第 ', var(REPEAT_ITEM, dkey('index')), '/',
        var(REPEAT_ITEM, dkey('total')), ' 个：', f_name])
b.else_(g_f_idx)
notice(['正在下载：', f_name])
b.endif(g_f_idx)
# 普通文件没有子目录，dest 就是「根子路径 + 文件名」；根子路径留空时只剩
# 文件名，所以这里不额外塞字面 "/"，避免拼出前导斜杠。
save_confirmed(b.get(f_url), [f_name],
               dest=[act_out(cfg, '配置', dkey(ROOT_SUB)), f_name])
b.endif(g_is_file)
b.end_repeat(g_each, each=True)

# 结束标记必须写：v11 真机上单纯复制几个文件时，日志停在最后一条「OK 写入」
# 就没了，看不出是**正常跑完**还是**半路断掉** —— 目录分支有这行，文件分支
# 漏了，排查时得靠数行数猜。补上以后两条路径的收尾形态一致。
log(['③ 文件同步结束：', var('文件数量'), ' 个'])
log_push()
b.endif(g_has)

# --- 目录：服务端已把所有目录合并成一张清单，这里同样只有一层循环 ---
b.comment('目录：合并清单 → 逐个下载 → 按清单的 dir 写进「下载/目录名/子目录」\n'
          '清单一层摊平（dir 已含目录名），所以这里只有一层循环，不嵌第二层')
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
# 目录这边也要一句总览：光看「开始同步 共 1 项」根本不知道里面装着几个文件。
notice(['目录 ', var('清单根'), '：共 ', var('清单文件数'), ' 个文件'])

g_notice = b.if_(pick(listing, 'notice'), 100)  # 有任何值 = 服务端有话说
b.notify([pick(listing, 'notice')])
b.endif(g_notice)

g_has_files = b.if_(pick(listing, 'entries'), 100)

g_each_file = b.repeat_each(pick(listing, 'entries'))

entry_dir = var(REPEAT_ITEM, dkey('dir'))
entry_prefix = var(REPEAT_ITEM, dkey('prefix'))
entry_name = var(REPEAT_ITEM, dkey('name'))
entry_url = var(REPEAT_ITEM, dkey('url'))
log(['· ', entry_name, ' ｜ dir=', entry_dir, ' ｜ prefix=', entry_prefix])
# 下载前先弹一条带进度：目录里文件重名时看序号也能分清是哪一个。
# 清单的每条都有 index/total，分母是这次要传的文件总数。
g_e_idx = b.if_(var(REPEAT_ITEM, dkey('total')), 100)
notice(['正在下载 第 ', var(REPEAT_ITEM, dkey('index')), '/',
        var(REPEAT_ITEM, dkey('total')), ' 个：', entry_name])
b.else_(g_e_idx)
notice(['正在下载：', entry_name])
b.endif(g_e_idx)

g_has_url = b.if_(entry_url, 100)

# ---- 核心：dest = 根子路径 + prefix + "/" + 文件名 ----
#
# prefix 是服务端给的**去掉末尾斜杠**的相对目录（含根目录名），斜杠由这里的
# 字面 "/" 提供。不能反过来依赖 prefix 自带的尾斜杠 —— iOS 会对子路径做规范
# 化，变量结尾的 "/" 会被吃掉，`目录名/` 于是变成文件名 `目录名`，所有文件
# 互相覆盖，最后「下载」里只剩一个叫目录名的文件（v6 真机现象）。
g_has_prefix = b.if_(entry_prefix, 100)
save_confirmed(
    b.get(entry_url), [entry_prefix, '/', entry_name],
    dest=[act_out(cfg, '配置', dkey(ROOT_SUB)),
          entry_prefix, '/', entry_name])

b.else_(g_has_prefix)
# 电脑端还是 1.12.5 及更早（清单里没有 prefix）：退回用 dir 拼。dir 末尾
# 自带 "/"，多出来的那一个斜杠 iOS 会自己合并，层级仍然对。
save_confirmed(
    b.get(entry_url), [entry_dir, entry_name],
    dest=[act_out(cfg, '配置', dkey(ROOT_SUB)),
          entry_dir, '/', entry_name])
b.endif(g_has_prefix)

b.else_(g_has_url)
log(['   跳过（清单条目没有下载地址）：', entry_name])
b.endif(g_has_url)

log_push()           # 每处理完一个文件推一次，崩在下一个也不会丢记录
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
# 没拿到合并清单地址。判断「这次到底带没带目录」时**绝不用数字比较**：
# folder_count 是 1.12.7 才加的字段，电脑端是旧版时它压根不存在，取出来是
# **空值**；拿空值去「大于 0」，iOS 会当场弹「请为此操作中的每个参数选取值」
# 并中断整条指令 —— v8 真机就卡死在这儿，后面的结束提示、日志落盘全都没跑。
# 空变量配「有任何值」是安全的（同一份指令里「如果 清单地址」就是这么用的）。
#   有 manifest_batch_error → 服务端明确说合并不了，原样转达；
#   没有 + 类型 = folder    → 单目录却没有合并清单 = 电脑端版本旧，提示升级；
#   没有 + 类型 = file_list → 复制的是普通文件，本来就没清单，静默（v7 噪音）。
g_batch_err = b.if_(pick(resp, 'manifest_batch_error'), 100)
b.notify([pick(resp, 'manifest_batch_error')])
b.else_(g_batch_err)
g_old_server = b.if_(var('类型'), IS, 'folder')
b.notify(['这次带了目录，但电脑端没给出合并清单 —— 请把电脑端升级到 '
          '1.12.7 及以上（git pull 后重启程序）'])
b.endif(g_old_server)
b.endif(g_batch_err)

b.endif(g_has_list)

b.else_(g_kind)
b.notify(['请求失败：', var('响应', dkey('message'))])
b.endif(g_kind)

b.endif(g_text)

# 结尾：**调试=开 时**才把整份日志再存一份到手机「下载」，方便自己翻。
# 这一步不校验、不提示：能存就存，存不上也不影响排查 —— 电脑端的
# log/ios_shortcut.log 才是权威副本（每个节点都已推过）。
g_dbg_file = _debug_on()
b.save_to_folder(var('日志'), dest=[LOG_FILE])
b.endif(g_dbg_file)

b.comment('结束。平时只看进度通知即可；要排查时把「配置」里的「调试」改成 开，'
          '过程会写进电脑端 log/ios_shortcut.log，手机「下载」也留一份副本')

# ------------------------------------------------------------------ 输出

meta = dict(DEFAULT_META)
meta['WFWorkflowActions'] = b.a

with open(OUT, 'wb') as f:
    plistlib.dump(meta, f, fmt=plistlib.FMT_XML, sort_keys=False)

print('动作数:', len(b.a))
print('输出:', OUT, os.path.getsize(OUT), 'B')
