# -*- coding: utf-8 -*-
"""生成「短信同步」快捷指令的 plist。

它解决的是「验证码短信到了，人还在电脑前」这件事：短信进来 → 自动化跑这条
快捷指令 → 从正文里抽出验证码 → 推给电脑端 → 电脑剪贴板里就是那 6 位数字，
直接在网页上粘贴。手机这边顺带复制到剪贴板，方便自己手动粘贴。

流程
----
    取短信正文（自动化把正文当「快捷指令输入」传进来）
      → 没有输入？记一行日志后停（多半是手动点运行了）
      → 匹配 A：关键词在前（验证码：123456）
          命中 → 用组 1
          没命中 → 匹配 B：数字在前（123456 是您的验证码）→ 用组 1
      → 还是空？这不是验证码短信，记一行日志后停
      → 探活 /status
          不可达 → 验证码暂存到「存储内容」，弹通知（可选）后停
          可达   → 先补推上次暂存的（如果有），再推这一条
      → POST /ios/sync 回包 status = ok？
          ok   → 复制到剪贴板（可选）+ 通知（可选）+ 日志
          其它 → 验证码暂存 + 通知（可选）+ 日志
      → 推一次日志（「调试」= 开 时才真的出网）

它跟另外两份的区别
------------------
* 获取同步：手动点、一次跑完、拿文件；和短信无关。
* 文字同步：驻留轮询，靠「打开 App 时」的自动化反复进；它**只能在你正用着
  手机某个 App 时**同步，而验证码短信恰恰多在你没动手机、或者刚切到电脑上
  的时候到 —— 那会儿文字同步根本不在跑。所以这份不轮询、不读剪贴板、不看
  前台 App，纯事件驱动，一条短信一次。
* 它写完剪贴板之后，文字同步下一轮可能把同一个验证码再上行一次 —— 服务端
  按「该设备手上已经是这份内容」判掉，不会多出一条历史。

配置词典（导入后在快捷指令里点「配置」就能改；全是文本，别写成数字/布尔）
------------------------------------------------------------------------
    键      默认            作用
    ------  --------------  --------------------------------------------
    协议    http            拼地址：协议://主机:端口
    主机    192.168.1.23    电脑端地址，与 config/server_config.json 一致
    端口    8000            电脑端监听端口
    密钥    123456          与电脑端一致；进请求头 key 和 body key，
                            错了两边都 403（快捷指令会弹「无法连接到服务器」）
    复制    开              开 = 推送成功后把验证码复制到手机剪贴板；
                            关 = 只推给电脑（手机上不留痕）
    通知    开              开 = 推送成功/失败各弹一条；关 = 全程静默
    调试    关              开 = 每次运行把日志 POST 到 /ios_log，
                            覆盖写电脑端 log/ios_shortcut.log；
                            关 = 日志只在内存里拼，一个字节都不出网

日志不记验证码本身，只记「有没有抽到 / 推送成不成」。验证码是凭据，落进
日志文件等于多一处泄漏面；排查「推没推过去」看推送结果那行就够。

服务端契约
----------
    POST /ios/sync  {key, content, source} → {status: 'ok', latest_global}
    GET  /status    探活，200 即在线
    POST /ios_log   {log} 覆盖写 log/ios_shortcut.log

    content = 验证码本身（6 位数字），不是整条短信：电脑端要的是能直接粘的
    那串；整条短信里的「【某某】」「有效期 5 分钟」粘进输入框全是噪音。
    source  = 设备名，跟文字同步用同一个 —— 两台手机/两条快捷指令共用一个
    设备名时，服务端才认得出「这台设备手上已经是这份内容」，不会把手机自己
    刚推上去的东西再投回来。

构建
----
    python 短信同步/build.py
    python lib/verify.py 短信同步/短信同步-unsigned.plist
    python lib/sign.py   短信同步/短信同步-unsigned.plist

正则在 `lib/sms.py`（PATTERN_A / PATTERN_B），改动后先跑
`python -m pytest tests/test_sms_code.py` 看那 20 条真实句式还过不过。
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
from lib.sms import PATTERN_A, PATTERN_B  # noqa: E402

OUT = os.path.join(HERE, '短信同步-unsigned.plist')

# ------------------------------------------------------------------ 构建流程

b = SC()
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
COPY, NOTIFY_ON, DEBUG = '复制', '通知', '调试'

# 暂存键：电脑端不可达（或推送失败）时，把验证码存在快捷指令自己的
# key-value 里，下一次运行补推。只存一条 —— 验证码本来就一分钟内过时，
# 攒一队没意义；新的一条直接盖掉旧的。
PENDING = '验证码待发'

b.comment('SyncClipboard 短信同步（收到信息 → 抽验证码 → 推给电脑）\n'
          '配置：协议 / 主机 / 端口 / 密钥 / 复制 / 通知 / 调试\n'
          '本指令只该由「收到信息时」的自动化运行，靠「快捷指令输入」拿短信正文\n'
          '抽不到验证码就安静退出；电脑端不可达时暂存，下次运行补推\n'
          '平时弹一条通知；调试 = 开 时过程写入电脑端 log/ios_shortcut.log')

# ---- 0 配置 ---------------------------------------------------------------
b.comment('0. 配置：导入后可在 App 里直接改，改完不用重新签')
cfg = b.dictionary([
    (PROTO, 'http'),
    # 与电脑端 config/server_config.json 的 host 一致。旧的手工包里写死过
    # 192.168.1.24，那是更早的地址，别照抄。
    (HOST, '192.168.1.23'),
    (PORT, '8000'),
    (KEY, '123456'),
    # 开 = 推送成功后把验证码复制到手机剪贴板；关 = 只在电脑上，手机不留痕。
    (COPY, '开'),
    # 开 = 成功/失败各弹一条通知（验证码本身就在通知里，能直接照着敲）。
    (NOTIFY_ON, '开'),
    # 开 = 每次运行把日志推给电脑端；关 = 一个字都不出网。
    (DEBUG, '关'),
], out_name='配置')


def c(key):
    """取配置里的某一项。"""
    return act_out(cfg, '配置', dkey(key))


# ---- 1 地址 ---------------------------------------------------------------
b.comment('1. 地址：服务器 + 两个端点（同步 / 探活）')
server = b.text([c(PROTO), '://', c(HOST), ':', c(PORT)])
b.setvar('服务器', act_out(server, '文本'))

ep = b.dictionary([
    ('同步', [var('服务器'), '/ios/sync']),
    ('状态', [var('服务器'), '/status']),
], out_name='接口')
b.setvar('接口', act_out(ep, '接口'))


def e(key):
    return act_out(ep, '接口', dkey(key))


# ---- 2 设备名 -------------------------------------------------------------
b.comment('2. 设备名：进 source 字段\n'
          '与文字同步保持同一个名字，服务端才认得出是同一台设备')
b.setvar('来源', b.device_name())

# ---- 运行日志（「调试」= 开 时才发出去） -----------------------------------
LOG = '日志'
# 开头带构建时间：手机上跑的到底是哪一份，日志里一眼能认。
LOG_TAG = '短信同步日志（构建 %s）\n' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
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


# ---- 3 正文 ---------------------------------------------------------------
#
# 短信正文是自动化当「快捷指令输入」传进来的。不用触发器输出的那份
# （`{"Type":"TriggerOutput"}`）是因为产物里不带自动化（见文档「触发怎么配」）：
# 触发器没定义时那个引用取不到值。
b.comment('3. 正文：取「快捷指令输入」\n'
          '手动点运行时没有正文，会直接停在这一步')
b.setvar('正文', act_out(b.text([b.shortcut_input()]), '文本'))

g_body = b.if_(var('正文'), NO_VALUE)
dbg(['=== 短信同步开始 ===\n'
     '没有输入：这份快捷指令只能由「收到信息时」的自动化运行，'
     '手动点运行拿不到短信正文'])
b.exit()
b.endif(g_body)

b.setvar('字数', act_out(b.count(var('正文')), '计数'))
dbg(['=== 短信同步开始 ===\n'
     '地址 ', var('服务器'), ' ｜ 复制 ', c(COPY), ' ｜ 通知 ', c(NOTIFY_ON),
     '\n设备 ', var('来源'), ' ｜ 正文 ', var('字数'), ' 字'])

# ---- 4 抽验证码 -----------------------------------------------------------
#
# 两条模式，A 没命中才跑 B（见 lib/sms.py 的说明）。模式里只留**一个**捕获组，
# 且它就是验证码本身：「获取匹配中的组」不写组号时默认取组 1，所以两条分支
# 都直接拿这个动作的输出，不必碰组号参数。
b.comment('4. 抽验证码：先 A（关键词在前）再 B（数字在前）\n'
          '模式在 lib/sms.py，改完重跑 build.py')
b.setvar('候选', b.match_group(b.match_text(var('正文'), PATTERN_A)))

g_hit = b.if_(var('候选'), ANY)
b.setvar('验证码', var('候选'))
b.else_(g_hit)
b.comment('A 没命中：试试「123456 是您的登录验证码」这种写法')
b.setvar('验证码', b.match_group(b.match_text(var('正文'), PATTERN_B)))
b.endif(g_hit)

g_miss = b.if_(var('验证码'), NO_VALUE)
dbg(['=== 短信同步结束 ===\n'
     '正文里没抽到验证码（正文 ', var('字数'), ' 字），跳过\n'
     '若确实是验证码短信，说明句式不在 lib/sms.py 的两条模式里'])
b.exit()
b.endif(g_miss)

b.setvar('位数', act_out(b.count(var('验证码')), '计数'))
log(['④ 抽到验证码（', var('位数'), ' 位，内容不记日志）'])

# ---- 5 探活 + 补推 --------------------------------------------------------
#
# 探活走 Actions 的 App Intent 而不是「获取 URL 内容」：后者在连不上时直接抛错
# 并中断整条指令 —— 那正是这里要避免的。我们要的是「不可达就把验证码存起来、
# 安静退出」，而不是弹一个「无法连接到服务器」。
#
# 验证码短信经常在你不在家（手机流量、电脑没开机）的时候到，所以「不可达」
# 不是异常，是常态之一：暂存一条，下次有短信时补推。
b.comment('5. 电脑端在不在？不在就暂存，下次运行补推\n'
          '可达时先把上次暂存的补上，再推这一条')
reach = b.reachable(e('状态'))

g_off = b.if_(reach, IS_NOT, '是')
b.stored_set(var('验证码'), PENDING)
g_off_note = b.if_(c(NOTIFY_ON), IS, '开')
b.notify(['电脑端不可达，验证码 ', var('验证码'), ' 已暂存，下次运行补推'])
b.endif(g_off_note)
dbg(['=== 短信同步结束 ===\n⑤ 电脑端不可达 → 暂存为「', PENDING, '」'])
b.exit()
b.endif(g_off)

# 补推。清空放在 POST 之后：POST 抛错时整条指令中断，暂存还在，下回再试。
b.setvar('待发', b.stored_get(PENDING))
g_pending = b.if_(var('待发'), ANY)
b.post_json(e('同步'),
            headers=[('key', c(KEY))],
            body=[('key', c(KEY)), ('content', var('待发')), ('source', var('来源'))])
b.stored_set('', PENDING)
log(['⑤ 补推上次暂存的验证码'])
b.endif(g_pending)

# ---- 6 推送 ---------------------------------------------------------------
#
# content 就是验证码本身：电脑端要的是能直接粘进输入框的那串数字，整条短信
# 里的「【某某】」「5 分钟内有效」粘过去全是噪音。想看是哪家发的，去短信里看。
b.comment('6. 推送：POST /ios/sync，content 就是验证码本身\n'
          '回包 status = ok 才算成功；否则暂存并说明原因')
push = b.post_json(e('同步'),
                   headers=[('key', c(KEY))],
                   body=[('key', c(KEY)), ('content', var('验证码')),
                         ('source', var('来源'))])
b.setvar('响应', push)
b.setvar('状态', pick(b.detect_dict(var('响应')), 'status'))

g_ok = b.if_(var('状态'), IS, 'ok')
g_copy = b.if_(c(COPY), IS, '开')
b.clipboard(var('验证码'))
log(['⑥ 已复制到手机剪贴板'])
b.endif(g_copy)
g_ok_note = b.if_(c(NOTIFY_ON), IS, '开')
b.notify(['验证码 ', var('验证码'), ' 已推送到电脑'])
b.endif(g_ok_note)
dbg(['=== 短信同步结束 ===\n⑥ 推送成功'])
b.else_(g_ok)
b.stored_set(var('验证码'), PENDING)
g_bad_note = b.if_(c(NOTIFY_ON), IS, '开')
b.notify(['推送失败（status = ', var('状态'), '），验证码已暂存'])
b.endif(g_bad_note)
dbg(['=== 短信同步结束 ===\n⑥ 推送失败：回包 status = ', var('状态'),
     '\n常见原因：密钥与电脑端不一致（403）、服务端版本过旧'])
b.endif(g_ok)

b.comment('结束。平时弹一条通知；排查时把「配置」里的「调试」改成 开，'
          '过程会写进电脑端 log/ios_shortcut.log')

# ------------------------------------------------------------------ 输出

meta = dict(DEFAULT_META)
meta['WFWorkflowActions'] = b.a
# 正文取自「快捷指令输入」，不置 True 的话导入后这个变量取不到值。
meta['WFWorkflowHasShortcutInputVariables'] = True

with open(OUT, 'wb') as f:
    plistlib.dump(meta, f, fmt=plistlib.FMT_XML, sort_keys=False)

print('动作数:', len(b.a))
print('输出:', OUT, os.path.getsize(OUT), 'B')
