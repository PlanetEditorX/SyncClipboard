# -*- coding: utf-8 -*-
"""生成「推送同步」快捷指令的 plist。

它解决的是「手上的东西要立刻出现在电脑上」这件事 —— 不用先复制、不用等文字
同步那一轮、也不用电脑端来拉：

    分享面板里选这条指令     把分享过来的文件（可以多个）推到选中的设备
    桌面上直接点这条指令     推剪贴板里的东西
                             文本 / URL  → 走文字通道，电脑剪贴板里直接能粘
                             文件 / 图像 → 走上传通道，目标设备的保存目录多一份

流程
----
    内容 = 快捷指令输入（有分享输入）或 剪贴板（从图标点开）
      → 内容是空的？记一行日志后停
      → 探活 /status
          不可达 → 记一行日志后停（内容还在你手机里，不用暂存）
      → 类型是 文本 / URL / 多信息文本？
          是 → POST /ios/sync {key, content, source}
                 status = ok → 通知「已推送」
                 其它        → 通知失败原因（多半是密钥不对）
          否 → GET /clients/online?source=…
                 没有客户端在线 → 模态提示后停（静默退出会像没点动）
                 选一个目标 → 逐个文件 PUT {上传地址}?filename=…
      → 「调试」= 开 时把整份日志 POST 到 /ios_log

它跟另外三份的区别
------------------
* 获取同步：电脑 → 手机，拉。这条是反向。
* 文字同步：驻留轮询，靠「打开 App 时」的自动化反复进；它推的是**剪贴板的
  变化**，而且只在白名单前台 App 里才跑。想「现在就把这个文件推过去」，它帮不了。
* 短信同步：验证码专用，事件驱动，一条短信一次。
* 这条是**手动一次**：不轮询、不看前台 App、不读存储内容。

配置词典（导入后在快捷指令里点「配置」就能改；全是文本，别写成数字/布尔）
------------------------------------------------------------------------
    键      默认            作用
    ------  --------------  --------------------------------------------
    协议    http            拼地址：协议://主机:端口
    主机    192.168.1.23    中心服务地址，与 config/server_config.json 一致
    端口    8000            中心服务监听端口
    密钥    123456          与电脑端一致；进请求头 key 和 body key，
                            错了两边都 403
    通知    开              开 = 结果各弹一条通知；关 = 只留日志
    调试    关              开 = 每次运行把日志 POST 到 /ios_log，
                            覆盖写电脑端 log/ios_shortcut.log；
                            关 = 日志只在内存里拼，一个字节都不出网

服务端契约
----------
    GET  /status          探活，200 即在线
    POST /ios/sync        {key, content, source} → {status: 'ok', latest_global}
    GET  /clients/online  ?source=设备名
                          → {online_clients: [显示名…], upload_url: {显示名: 地址}}
    PUT  <upload_url>     ?filename=<URL 编码后的文件名>
                          请求头 key / source / target / Content-Type
                          请求体 = 文件二进制（WFHTTPBodyType = File）

    · 目标设备由中心服务挑：iOS 自己不能被推送（只能轮询），Android 走
      /upload_file_to_download 中转，电脑走 /upload_file 直传。手机这边不用
      关心区别 —— `upload_url` 拿来就用，文件名一律拼在自己这一层。
    · `source` 用 URL 编码（中文设备名直接进请求头会乱码）；`target` 是显示名，
      只给电脑端日志看。
    · 文件名 = `URL编码(文件)` + 可选的 `.扩展名`。iOS 给的是 Name 属性
      （**不含后缀**），所以要单独取 File Extension 补在后面；扩展名是纯
      ASCII，不参与编码。**别把文件变量先塞进「文本」动作再编码** —— 那一步
      会把 UTF-8 中文按 GBK 读成乱码（真机「证书文件」→「璇佷功鏂囦欢」），
      服务端另有 `fix_mojibake_filename()` 兜底。剪贴板图像没名字也没后缀，
      会退化成 `uploaded_file`。

构建
----
    python 推送同步/build.py
    python lib/verify.py 推送同步/推送同步-unsigned.plist
    python lib/sign.py   推送同步/推送同步-unsigned.plist
"""

import os
import sys
import datetime
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))
SHORTCUTS_ROOT = os.path.dirname(HERE)      # shortcuts/
sys.path.insert(0, SHORTCUTS_ROOT)

from lib.sc import *  # noqa: E402,F401  (SC/tok/att/act_out/pick/var/dkey/条件码)
from lib.sc import DEFAULT_META  # noqa: E402

OUT = os.path.join(HERE, '推送同步-unsigned.plist')

# 剪贴板里这些类型算「文字」，走 /ios/sync 进电脑剪贴板；其余都当文件上传。
# 名字是 iOS 的本地化类型名（「获取项目类型」的输出），不是 MIME。
TEXT_TYPES = ['文本', 'URL', '多信息文本']

# ------------------------------------------------------------------ 构建流程

b = SC()
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
NOTIFY_ON, DEBUG = '通知', '调试'

b.comment('SyncClipboard 推送（手机 → 选中的那台设备）\n'
          '\n'
          '两种用法：\n'
          '· 分享：在「照片 / 文件 / 浏览器」里点分享 → 选这条指令 → 选目标设备，'
          '分享过来的文件直接落到那台设备的保存目录\n'
          '· 直接点图标：推的是剪贴板。文字 / 网址进电脑剪贴板（可以直接粘），'
          '文件 / 图片传到保存目录\n'
          '\n'
          '配置：协议 / 主机 / 端口 / 密钥 / 通知 / 调试\n'
          '平时只弹结果通知；「调试」= 开 时过程写进电脑端 log/ios_shortcut.log')

# ---- 0 配置 ---------------------------------------------------------------
b.comment('0. 配置：导入后可在 App 里直接改，改完不用重新签')
cfg = b.dictionary([
    (PROTO, 'http'),
    # 与电脑端 config/server_config.json 的 host 一致。老的手工包里写死过
    # 192.168.1.24，那是更早的地址。
    (HOST, '192.168.1.23'),
    (PORT, '8000'),
    (KEY, '123456'),
    # 开 = 结果各弹一条通知；关 = 全程静默。关掉也拦不住「没有客户端在线」那句
    # 模态提示 —— 那是 alert，不是通知，见第 6 段。
    (NOTIFY_ON, '开'),
    # 开 = 每次运行把日志推给电脑端；关 = 一个字都不出网。
    (DEBUG, '关'),
], out_name='配置')


def c(key):
    """取配置里的某一项。"""
    return act_out(cfg, '配置', dkey(key))


# ---- 1 地址 ---------------------------------------------------------------
b.comment('1. 地址：服务器 + 三个端点（状态 / 客户端 / 文字）')
server = b.text([c(PROTO), '://', c(HOST), ':', c(PORT)])
b.setvar('服务器', act_out(server, '文本'))

ep = b.dictionary([
    ('状态', [var('服务器'), '/status']),
    ('客户端', [var('服务器'), '/clients/online']),
    ('文字', [var('服务器'), '/ios/sync']),
], out_name='接口')
b.setvar('接口', act_out(ep, '接口'))


def e(key):
    return act_out(ep, '接口', dkey(key))


def txt(s):
    return act_out(b.text([s]), '文本')


# ---- 2 设备名 -------------------------------------------------------------
b.comment('2. 设备名：进 source 字段\n'
          '与文字同步 / 短信同步用同一个名字（都是这台手机），服务端才认得出是\n'
          '同一台设备，不会把手机自己推上去的东西再投回来')
b.setvar('来源', b.device_name())
# 中文设备名直接塞进请求头会乱码：客户端读 source 时不按 UTF-8 解。
b.setvar('来源编码', act_out(b.urlencode(var('来源')), 'URL编码后的文本'))

# ---- 运行日志（「调试」= 开 时才发出去） -----------------------------------
LOG = '日志'
# 开头带构建时间：手机上跑的到底是哪一份，日志里一眼能认。
LOG_TAG = '推送同步日志（构建 %s）\n' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
b.setvar(LOG, act_out(b.text([LOG_TAG]), '文本'))
b.setvar('日志地址', act_out(b.text([var('服务器'), '/ios_log']), '文本'))


def log(parts):
    """往「日志」变量追加一行。累加源必须是变量本身，不能用上一条动作的输出。"""
    b.setvar(LOG, act_out(b.text([var(LOG), *parts, '\n']), '文本'))


def log_push():
    """把当前完整日志推给电脑端（POST /ios_log），仅「调试」= 开 时执行。"""
    g = b.if_(c(DEBUG), IS, '开')
    b.post_json(var('日志地址'),
                headers=[('key', c(KEY))],
                body=[('log', var(LOG))])
    b.endif(g)


def dbg(parts):
    """记一行日志并立刻推一次：中途被掐断也能看到已经走到哪一步。"""
    log(parts)
    log_push()


def note(parts):
    """「通知」= 开 时弹一条通知；关掉时连这个分支都不进。"""
    g = b.if_(c(NOTIFY_ON), IS, '开')
    b.notify(parts)
    b.endif(g)


# ---- 3 内容 ---------------------------------------------------------------
#
# 两种入口共用后面所有逻辑，只在「内容从哪来」这一步分叉：
#   分享输入有值 → 用它（可能是一叠文件）
#   从图标点开   → 用剪贴板
# 不用「获取当前App」判场景：分享面板是独立进程，前台未必还是原 App，判不准；
# 「输入有没有值」才是这件事的真正判据。
b.comment('3. 内容：有分享输入就用输入，否则用剪贴板\n'
          '分享面板进来推的是分享的文件；直接点图标推的是剪贴板')
b.setvar('内容', b.shortcut_input())
g_src = b.if_(var('内容'), ANY)
b.setvar('模式', txt('分享'))
b.else_(g_src)
b.setvar('内容', act_out(b.get_clipboard(), '剪贴板'))
b.setvar('模式', txt('剪贴板'))
b.endif(g_src)

# 剪贴板空着（或分享了个空内容）时不发请求：POST 一个空 content 服务端会回
# 400，在快捷指令里就表现为一句「无法连接到服务器」，很容易被当成网络问题。
g_none = b.if_(var('内容'), NO_VALUE)
dbg(['=== 推送同步结束 ===\n'
     '没有内容可推（来源：', var('模式'), '）\n'
     '从图标点开时要先复制点东西；分享进来时确认分享的不是空项目'])
b.exit()
b.endif(g_none)

b.setvar('类型', b.item_type(var('内容')))
# 文本时是字数、文件时是项数 —— 同一个「计数」，不在两个分支里各算一遍。
b.setvar('数量', act_out(b.count(var('内容')), '计数'))
dbg(['=== 推送同步开始 ===\n'
     '地址 ', var('服务器'), ' ｜ 来源 ', var('模式'), ' ｜ 类型 ', var('类型'),
     '\n设备 ', var('来源'), ' ｜ 数量 ', var('数量')])

# ---- 4 探活 ---------------------------------------------------------------
#
# 探活走 Actions 的 App Intent 而不是「获取 URL 内容」：后者连不上时直接抛错并
# 中断整条指令，弹的是「无法连接到服务器」，看不出是服务没开还是密钥错。
#
# 不做暂存：文字同步那种暂存是为了「人不在也没关系，回头补」；这条是手动触发
# 的，人和内容都在场，推不上去就当场说清楚，回头再点一次更省事。
b.comment('4. 服务端在不在？不在就说一声停下，接着跑只会得到一堆报错')
reach = b.reachable(e('状态'))
g_off = b.if_(reach, IS_NOT, '是')
dbg(['=== 推送同步结束 ===\n'
     '服务端不可达：', var('服务器'), '\n'
     '检查电脑端是否启动、手机是否和它同一局域网'])
note(['服务端不可达：', var('服务器')])
b.exit()
b.endif(g_off)

# ---- 5 文字分支 -----------------------------------------------------------
#
# 走 /ios/sync 而不是 /text_sync：跟文字同步、短信同步同一条通道，服务端才把
# 它记成「这台设备手上是这份内容」，下一轮轮询不会再把同一份内容顶回来。
b.comment('5. 文字：POST /ios/sync，电脑剪贴板里直接能粘\n'
          '内容是网址也一样走这里 —— 电脑上粘出来就是那条链接')
g_text = b.if_any(var('类型'), TEXT_TYPES)
# 「计数」缺省数的是项目数，一段文字永远是 1 项。要显示真实字数必须显式指定
# Characters —— 真机上「已推送到电脑（1 字）」就是漏了这个参数。
b.setvar('字数', act_out(b.count(var('内容'), 'Characters'), '计数'))
push = b.post_json(e('文字'),
                   headers=[('key', c(KEY))],
                   body=[('key', c(KEY)), ('content', var('内容')),
                         ('source', var('来源'))])
b.setvar('响应', push)
b.setvar('状态', pick(b.detect_dict(var('响应')), 'status'))

g_ok = b.if_(var('状态'), IS, 'ok')
note(['已推送到电脑（', var('字数'), ' 字）'])
dbg(['⑤ 文字推送成功（', var('字数'), ' 字，内容不记日志）'])
b.else_(g_ok)
note(['推送失败：', var('状态')])
dbg(['⑤ 文字推送失败：回包 status = ', var('状态'),
     '\n常见原因：密钥与电脑端不一致（403）、服务端版本过旧'])
b.endif(g_ok)

# ---- 6 文件分支 -----------------------------------------------------------
#
# 先问服务端「谁在线」再由用户选。不自己维护设备列表：谁在线只有服务端知道
# （Android 要中转、iOS 不参与、电脑要 ping 得通），手机这边照抄容易过期。
b.comment('6. 文件：先问服务端有哪些设备在线，再逐个上传')
b.else_(g_text)
# /clients/online 和 /ios/sync 一样要校验 key（服务端读的是请求头，不是 body）。
# 少了这一行，文件分支会稳稳拿到 403 {status: error, message: 密钥错误}，
# 然后弹「服务端拒绝了这次请求」—— 看起来像密钥配错，其实是请求没带钥匙。
# source 头原版也带：服务端这版只从查询串取，带着是为了跟它对齐、也防以后改读头。
list_resp = b.get([e('客户端'), '?source=', var('来源编码')],
                  headers=[('key', c(KEY)), ('source', var('来源编码'))])
b.setvar('在线响应', act_out(b.detect_dict(list_resp), '词典'))
b.setvar('列表状态', pick(var('在线响应'), 'status'))
b.setvar('在线客户端', pick(var('在线响应'), 'online_clients'))
b.setvar('地址表', pick(var('在线响应'), 'upload_url'))

# 密钥不对时服务端回 {status: error, message: 密钥错误} —— 没有 online_clients
# 字段。不先分辨这一步的话，用户看到的是「没有客户端在线」，去开电脑端白费劲。
g_list_bad = b.if_(var('列表状态'), IS_NOT, 'ok')
b.setvar('列表提示', pick(var('在线响应'), 'message'))
dbg(['=== 推送同步结束 ===\n'
     '客户端列表请求失败：', var('列表状态'), ' / ', var('列表提示')])
b.alert(['服务端拒绝了这次请求（', var('列表状态'), '）：', var('列表提示'),
         '\n最常见的两种：密钥与电脑端不一致（403），或电脑端版本过旧'])
b.exit()
b.endif(g_list_bad)

g_no_client = b.if_(var('在线客户端'), NO_VALUE)
dbg(['=== 推送同步结束 ===\n'
     '服务端没有在线客户端\n'
     'iOS 不参与文件直传（只能轮询）；电脑端要开着且和手机同一局域网'])
# 这条用模态提示而不是通知：用户刚在分享面板点完，静默退出会像没点动。
b.alert('没有客户端在线！\n先在那台电脑上打开 SyncClipboard，'
        '再确认手机和它在同一个局域网')
b.exit()
b.endif(g_no_client)

b.setvar('目标', b.choose_from_list(var('在线客户端'), '发送到：'))
b.setvar('上传地址', b.get_value(var('地址表'), tok([var('目标')])))
log(['⑥ 目标 ', var('目标'), ' ｜ 待传 ', var('数量'), ' 项'])

g_loop = b.repeat_each(var('内容'))
b.setvar('序号', var('Repeat Index'))
b.setvar('文件', var('Repeat Item'))
# 文件名 = 直接 URL 编码文件变量。**别用「文本」动作把文件变量中转一次再编码**：
# 真机上那一步会把 UTF-8 字节按 GBK 读，「证书文件」变成「璇佷功鏂囦欢」。
# 直接编码文件变量是上版验证过中文正常的路径，中文、空格、# 都能正确百分号化。
b.setvar('名称', act_out(b.urlencode(var('文件')), 'URL编码后的文本'))
# iOS 给的是 Name 属性（不含后缀），扩展名要单独取：取到才拼「.后缀」。
# 后缀是纯 ASCII，直接接在已编码的名称后面，不参与编码、也不会引入中文。
# 用「如果」只切这个后缀变量，PUT 与循环始终各只有一处。
b.setvar('扩展名', b.file_extension(var('文件')))
g_ext = b.if_(var('扩展名'), ANY)
b.setvar('点后缀', act_out(b.text(['.', var('扩展名')]), '文本'))
b.else_(g_ext)
b.setvar('点后缀', txt(''))
b.endif(g_ext)
b.setvar('结果', pick(b.detect_dict(b.put_file(
    [var('上传地址'), '?filename=', var('名称'), var('点后缀')],
    body_subject=var('文件'),
    headers=[('key', c(KEY)),
             # 中文设备名进请求头会乱码，这里用编码后的那份
             ('source', var('来源编码')),
             # 显示名，只给电脑端日志用
             ('target', var('目标')),
             ('Content-Type', 'application/octet-stream')],
)), 'message'))
log(['  · 第 ', var('序号'), ' 项 [', var('名称'), var('点后缀'),
         ']：', var('结果')])
b.end_repeat(g_loop, each=True)

note(['已推送 ', var('数量'), ' 项到 ', var('目标')])
dbg(['=== 推送同步结束 ===\n⑥ 全部上传完成'])
b.endif(g_text)

b.comment('结束。平时看结果通知即可；排查时把「配置」里的「调试」改成 开，'
          '过程会写进电脑端 log/ios_shortcut.log')

# ------------------------------------------------------------------ 输出

meta = dict(DEFAULT_META)
meta['WFWorkflowActions'] = b.a
# 内容取自「快捷指令输入」，不置 True 的话导入后这个变量取不到值。
meta['WFWorkflowHasShortcutInputVariables'] = True
# 分享面板入口：少了 ActionExtension，这条指令不会出现在「分享」里，
# 只能在桌面点 —— 而点开时读的是剪贴板，等于「分享文件」这个用法整个没了。
meta['WFWorkflowTypes'] = ['ActionExtension', 'WFWorkflowTypeShowInSearch']

with open(OUT, 'wb') as f:
    plistlib.dump(meta, f, fmt=plistlib.FMT_XML, sort_keys=False)

print('动作数:', len(b.a))
print('输出:', OUT, os.path.getsize(OUT), 'B')
