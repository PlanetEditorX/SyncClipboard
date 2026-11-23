# -*- coding: utf-8 -*-
"""生成「文字同步」快捷指令的 plist。

流程
----
    前台 App 在名单里？（不在就整段跳过，一个请求都不发）
      → 守卫（电量 / Wi-Fi / 服务可达）
      → 读本地缓存（上次对齐到的内容，一段文本）
      → 重复 N 次：
            电脑端还在？不在就停（「前台检查」= 每轮 时再判一次还在不在前台 App）
            → 读剪贴板 → 文本类？
                和缓存不同 → POST /ios/sync（上行，服务端据此广播给电脑）
                和缓存相同 → GET  /ios/latest（纯拉取）
                非文本     → GET  /ios/latest
            → 服务端给了 content 才更新缓存
            → 和剪贴板不同 → 通知（可选）+ 写入剪贴板
            → 剩余 ≤ 10 轮且「续跑」= 开 → 调用自己另起一份接着跑，本份结束
            → 等待（前 1 分钟 时间1 / 1–3 分钟 时间2 / 3 分钟后 时间3）
      → 结尾推一次日志

它跟「获取同步」的区别：获取同步是一次性拉取、拿到就结束；文字同步是**驻留
轮询**，靠「App 打开时运行」的自动化反复进入，一轮一轮把两边剪贴板对齐。

配置词典（导入后在快捷指令里点「配置」就能改，改完不用重新签）
-------------------------------------------------------------
全部是**文本**，不是数字也不是布尔：「如果」里要拿它们跟字符串比，
写成数字会在参数里显示成空白，判据恒不成立。带 ⚠ 的改之前先看下文。

    键      默认            单位/取值        作用
    ------  --------------  --------------  ----------------------------
    协议    http            仅 http/https   拼地址：协议://主机:端口
    主机    192.168.1.23    局域网 IP        电脑端地址。必须与电脑端
                                             config/server_config.json 的
                                             host 一致（旧手工包写死
                                             192.168.1.24，是更早的地址）
    端口    8000            1–65535         电脑端监听端口
    密钥    123456          字符串           每个请求带的 key，与电脑端一致
                                            错 → 服务端一律 401/忽略
    电量    40              百分比，不带 %   守卫：电量 ≤ 此值 → 本次不跑。
                                            手机揣兜里跑一整天时，这是唯一
                                            能把轮询叫停的开关
 ⚠  重复    40              轮（整数）       跑几轮。「一轮」= 一次请求 +
                                            一次等待，不是一次请求。
                                            40 轮 ≈ 3s×20 + 5s×20 = 160 秒
                                            它同时是续跑的基准：
                                            剩余 = 重复 - 轮次
    时间1   3               秒              累计等待 0–60 秒 时每轮等这么久
                                            默认 40 轮里大约前 20 轮用这档
    时间2   5               秒              累计等待 60–180 秒 时用
                                            默认 40 轮里后 20 轮用这档
 ⚠  时间3   10              秒              累计 ≥ 180 秒 之后用。
                                            默认 40 轮到不了 180 秒，
                                            这档是给「重复 ≥ 45」准备的；
                                            别为了用它去硬调大「重复」——
                                            被系统掐断时既没有收尾日志，
                                            也没有续跑
    通知    关              开 / 关         开 = 从电脑拿到新内容、真的要
                                            写剪贴板时弹一条；关 = 静默
                                            只认「开」这个字，别的都算关
    续跑    关              开 / 关         见下面「续跑到底是什么」
                                            开的时候「重复」必须 ≥ 12
    调试    关              开 / 关         开 = 每轮把日志 POST 到
                                            /ios_log，覆盖写电脑端
                                            log/ios_shortcut.log；
                                            关 = 日志只在内存里拼，一个
                                            字节都不出网
    前台检查 仅入口          每轮 / 仅入口    仅入口 = 循环里不读「当前 App」，
                                            一次运行只在开头被 iOS 问一次
                                            「允许共享吗」；每轮 = 恢复
                                            「切走就停」，但**每轮都会弹一次**
                                            隐私确认（见 docs「iOS 权限弹窗」）

续跑到底是什么
--------------
一句话：**这一份快用完了，让它用「运行快捷指令（自己）」再起一份，把寿命接上。**

    触发判据（每轮末尾、等待之前）：
        剩余 = 重复 - 当前轮次
        剩余 ≤ 10（RESTART_MARGIN）  →  调用自己  →  本份 exit
    默认 40 轮 → 第 30 轮触发（剩余 10）。默认关。

    关（默认）  跑满 40 轮就结束。下一次靠自动化「打开 App 时」再进来 ——
                够用，因为同步需求本来就发生在你切回微信/浏览器的那一刻。

    开          只要「前台是名单里的 App + 电量够 + 连着 Wi-Fi + 电脑可达」
                这四条还成立，每 160 秒左右续一次，一直续下去 —— 等于在
                这个 App 前台常驻。四条里任一不成立，链子自己断，不会
                在后台空转（每次续跑都重过白名单和守卫）。

    ⚠ 开续跑时「重复」必须 ≥ 12。第 1 轮的轮次是 1，剩余 = 重复 - 1；
      重复 ≤ 11 时第 1 轮就命中「剩余 ≤ 10」，于是每轮都重启一次 ——
      看着还在跑，实际每轮只发一个请求，而且不断叠新实例。

    日志影响：续跑起的是新的一份运行，开头又打一遍「=== 文字同步开始 ===」；
    电脑端按「旧内容是新内容前缀」判是不是同一次运行，所以会判成新运行、
    把上一份存成 ios_shortcut.log.1 —— 正好留下续跑前那段的痕迹。

服务端契约（电脑端 flask_app.py，拉取/上行都只认这几个字段）
----------------------------------------------------------
    POST /ios/sync      {key, content, source} → {status, latest_global}
    GET  /ios/latest    ?source=设备名，请求头 key  → {status, latest_global}
    GET  /status        探活用，200 即在线
    POST /ios_log       {log} 覆盖写 log/ios_shortcut.log

    latest_global
        content         只在「这份该投递给本机」时才带；没东西时整个空包
        pasted          带 content 时恒为 false；空包是 {pasted: true}
        source          谁复制的

    判据只需要「响应里有没有 content」：
      · GET  /ios/latest → _take_ios_latest 的两个投递分支，带 content 且
        pasted=false，并就地 mark_pasted；
      · POST /ios/sync  → 内容是手机自己新发的，回包里带的是它自己那份
        （与本地相同，会被「和剪贴板不同」挡掉）；其余情况走同一个
        _take_ios_latest。
    所以不要拿 content 去覆盖缓存，除非它确实有值；也不要比 pasted ——
    服务端在任何情况下都不会给出「带 content 但已投递」的组合。

构建
----
    python 文字同步/build.py
    python lib/verify.py 文字同步/文字同步-unsigned.plist
    python lib/sign.py   文字同步/文字同步-unsigned.plist
"""

import os
import sys
import datetime
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))
SHORTCUTS_ROOT = os.path.dirname(HERE)      # shortcuts/
sys.path.insert(0, SHORTCUTS_ROOT)

from lib.sc import *  # noqa: E402,F401  (SC/tok/att/act_out/pick/var/dkey/条件码)
from lib.sc import DEFAULT_META, set_icon  # noqa: E402

OUT = os.path.join(HERE, '文字同步-unsigned.plist')

# ------------------------------------------------------------------ 构建流程

b = SC()
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
BATTERY, LOOP = '电量', '重复'
GAP1, GAP2, GAP3 = '时间1', '时间2', '时间3'
NOTIFY_ON, RESUME, DEBUG = '通知', '续跑', '调试'
CHECK_APP = '前台检查'      # 每轮 / 仅入口。见 5.0 那段注释与 docs「iOS 权限弹窗」

# 三档间隔的分界（秒）：打开 App 的头一分钟高频对上，之后逐步降频省电。
# 原版是按「轮次」判的（如果 重复索引 < 60 / < 180），60 轮时索引最大 60：
# 第 1–59 轮走时间1、第 60 轮走时间2、时间3 永远走不到 —— 第三档是死配置。
# 这里改成按累计等待时长判，三档才都活得到；「重复」因此要按时间预算来给。
FAST_UNTIL, SLOW_UNTIL = 60, 180

CACHE = '内容Cache'         # 「设置存储内容」的键名，跨运行保留；跟原版一致，别改，
#                             改了旧缓存就读不到，升级后首轮会把旧内容再上行一次。
#                             存的是**一段文本**（原版存整份 latest_global，这里不再
#                             存 pasted：判断用不到，多一份状态就多一处漂移）。
RESTART_MARGIN = 10         # 续跑阈值：剩余轮次 ≤ 它就把寿命接给下一份自己跑。
#                             别等最后一轮 —— 那里常被系统掐断，既没收尾日志也没续跑。
#                             注意它跟「重复」的耦合：第 1 轮剩余 = 重复 - 1，所以
#                             「重复」≤ RESTART_MARGIN + 1（默认 11）时，第 1 轮就
#                             会触发续跑，变成每轮重启一次。开续跑时「重复」≥ 12。
TEXT_TYPES = ['文本', 'URL', '多信息文本']   # 「获取项目类型」的本地化名字

# 只在这些 App 前台时才同步。省电的主要手段：桌面、或者随手点开的别的 App，
# 进去就安静退出，一个请求都不发。名单照原版（备忘录 / 豆包 / 微信 / 文件 /
# 信息 / Chrome / Filebar / Google / Safari浏览器 / QQ），要加减就改这里再签一次。
# 注意名字是本地化的：系统语言一换，「文件」「信息」这些就对不上了。
#
# 「快捷指令」必须留在名单里：手动点运行的时候，前台就是快捷指令 App，不在名单
# 里会被白名单当场挡掉 —— 表现是「点了没反应、日志一个字都没有」，很容易误判成
# 快捷指令坏了。
APP_LIST = ['快捷指令', '备忘录', '豆包', '微信', '文件', '信息',
            'Chrome', 'Filebar', 'Google', 'Safari浏览器', 'QQ']

b.comment('SyncClipboard 文字同步（轮询驻留）\n'
          '配置：协议 / 主机 / 端口 / 密钥 / 电量 / 重复 / 时间1~3 / 通知 / 续跑 / 调试 / 前台检查\n'
          '手机剪贴板变了就上行到电脑；电脑有新的就写回手机剪贴板\n'
          '守卫：不在名单里的 App、电量过低、没连 Wi-Fi、电脑端不可达 —— 任一命中直接停\n'
          '平时静默；调试 = 开 时过程写入电脑端 log/ios_shortcut.log')

# ---- 0 前台 App 白名单 -----------------------------------------------------
#
# 省电的第一道闸：只有名单里的 App 在前台时才往下走。桌面、或者随手点开的别的
# App，整段主体被跳过，一个请求都不发。名单在 build.py 顶部的 APP_LIST 里。
# 「当前 App」取的是触发这次运行的那个 App —— 自动化本身就是「打开某个 App 时」。
#
# text=True：入口这里就把 App 内容项转成文本。iOS 的隐私面板会把这次运行读到的
# 「app」类内容项算进「允许发送给 <主机> 吗」的清单里，越早转文本，清单里越不会
# 出现「1 个 app」这种会让它每次重新确认的条目（详见 docs 的「iOS 权限弹窗」）。
b.comment('0. 白名单：只有这些 App 在前台才同步，其余安静退出\n'
          '   ' + '、'.join(APP_LIST))
g_fore = b.if_any(b.current_app(text=True), APP_LIST)

cfg = b.dictionary([
    (PROTO, 'http'),
    # 与电脑端 config/server_config.json 的 host 保持一致（旧版手工包写死的是
    # 192.168.1.24，那是更早的地址，别照抄）。
    (HOST, '192.168.1.23'),
    (PORT, '8000'),
    (KEY, '123456'),
    # 电量百分比。手机揣兜里跑一整天时，这是唯一能把轮询叫停的开关。
    (BATTERY, '40'),
    # 跑几轮。**一轮 = 一次请求 + 一次等待**，不是一次请求。
    # 40 轮 ≈ 3s×20 + 5s×20 = 160 秒，够覆盖「打开 App 办完一件事」的时长。
    # 它还是续跑的基准（剩余 = 重复 - 轮次）：开「续跑」时别给到 11 以下，
    # 否则第 1 轮就触发续跑，变成每轮重启一次。
    (LOOP, '40'),
    # 三档等待（秒）：累计 0–60 用时间1，60–180 用时间2，≥180 用时间3。
    # 默认 40 轮只累计到 160 秒，第三档用不到；要它生效得「重复」≥ 45，
    # 那会明显超过 3 分钟后台时限，只有插着电驻留才值得。
    (GAP1, '3'),
    (GAP2, '5'),
    (GAP3, '10'),
    # 开 = 从电脑拿到新内容、真要写剪贴板时弹一条通知；关 = 静默写。
    (NOTIFY_ON, '关'),
    # 开 = 剩余 ≤ RESTART_MARGIN 轮时调用自己续命（近似常驻，需「重复」≥ 12）；
    # 关 = 跑完「重复」轮就结束，靠「打开 App 时」的自动化下次再进。
    (RESUME, '关'),
    # 开 = 每轮把日志推给电脑端 log/ios_shortcut.log；关 = 全在内存里，不出网。
    (DEBUG, '关'),
    # 每轮 / 仅入口。仅入口 = 循环里不读「当前 App」，一次运行只会在开头被 iOS
    # 问一次「允许共享吗」；每轮 = 恢复「切走就停」，但每轮都会弹一次隐私确认。
    (CHECK_APP, '仅入口'),
], out_name='配置')


def c(key):
    """取配置里的某一项。"""
    return act_out(cfg, '配置', dkey(key))


# ---- 1 地址 ---------------------------------------------------------------
b.comment('1. 地址：服务器 + 三个端点')
server = b.text([c(PROTO), '://', c(HOST), ':', c(PORT)])
b.setvar('服务器', act_out(server, '文本'))

ep = b.dictionary([
    ('同步', [var('服务器'), '/ios/sync']),
    ('最新', [var('服务器'), '/ios/latest']),
    ('状态', [var('服务器'), '/status']),
], out_name='接口')
b.setvar('接口', act_out(ep, '接口'))


def e(key):
    return act_out(ep, '接口', dkey(key))


# ---- 2 设备名 -------------------------------------------------------------
b.comment('2. 设备名：进 source 字段，电脑端据此判重与回投\n'
          '查询串里用编码后的那份，JSON body 里用原名')
b.setvar('来源', b.device_name())
b.setvar('来源编码', act_out(b.urlencode(var('来源')), 'URL编码后的文本'))

# ---- 运行日志（「调试」= 开 时才发出去） -----------------------------------
#
# 与「获取同步」同一套：日志变量始终在拼（纯内存），真正推给电脑端的 log_push
# 被「如果 配置.调试 是 开」包住 —— 关着时一个字节都不产生。
LOG = '日志'
# 开头带构建时间：日志里出现的那份是不是手机上正在跑的这份，靠它一眼判断。
# 手机上装着旧版时，日志连「地址 / 白名单」这几行都不会有。
LOG_TAG = '文字同步日志（构建 %s）\n' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
b.setvar(LOG, act_out(b.text([LOG_TAG]), '文本'))
b.setvar('日志地址', act_out(b.text([var('服务器'), '/ios_log']), '文本'))


def log(parts):
    """往「日志」变量追加一行。累加源必须是变量本身，不能用上一条动作的输出。"""
    b.setvar(LOG, act_out(b.text([var(LOG), *parts, '\n']), '文本'))


def _debug_on():
    return b.if_(c(DEBUG), IS, '开')


def log_push():
    """把当前完整日志推给电脑端（POST /ios_log），仅调试=开 时执行。"""
    g = _debug_on()
    b.post_json(var('日志地址'),
                headers=[('key', c(KEY))],
                body=[('log', var(LOG))])
    b.endif(g)


def dbg(parts):
    """记一行日志并立刻推一次（中断在下一步也能看到已发生的事）。"""
    log(parts)
    log_push()


# 开头把「跑的是哪套配置」写进去。电脑端连不上、内容不写回时，第一件要排除的
# 就是「手机上填的地址/端口是不是这套」，没有这行就只能靠猜。
dbg(['=== 文字同步开始 ===\n'
     '地址 ', var('服务器'), ' ｜ 重复 ', c(LOOP),
     ' ｜ 间隔 ', c(GAP1), '/', c(GAP2), '/', c(GAP3),
     ' ｜ 通知 ', c(NOTIFY_ON), ' ｜ 续跑 ', c(RESUME),
     '\n设备 ', var('来源'), ' ｜ 白名单 ', str(len(APP_LIST)), ' 个 App'])

# ---- 3 守卫 ---------------------------------------------------------------
#
# 三个条件任中一个就不跑：电量太低、没连 Wi-Fi、电脑端不可达。都用「任一」的
# 多条件动作写在一个「如果」里，省掉三层嵌套。
#
# 探活走 Actions 的 App Intent 而不是「获取 URL 内容」：后者在连不上时直接抛错
# 并中断整条指令，那正好是守卫想避免的局面。
b.comment('3. 守卫：电量 ≤ 配置.电量 / 没连 Wi-Fi / 电脑端不可达 —— 任一命中就停')
battery = b.battery_level()
wifi = b.wifi_info()
reach = b.reachable(e('状态'))

g_guard = b.if_conds([
    {'subject': battery, 'cond': LTE, 'number': c(BATTERY)},
    {'subject': wifi, 'cond': NO_VALUE},
    {'subject': reach, 'cond': IS_NOT, 'string': '是'},
], ANY_OF)
# 日志里不拼「网络的详细信息」这个对象：它是词典，可能带 SSID 之类的网络标识，
# 一拼进日志就等于把网络信息也发到电脑端 —— 平白多一类要授权的隐私内容。
dbg(['① 守卫命中，本次不跑：电量 ', battery, '（阈值 ', c(BATTERY),
     '）｜ 可达 ', reach, ' ｜ 无 Wi-Fi'])
b.exit()
b.endif(g_guard)

# ---- 4 缓存 ---------------------------------------------------------------
#
# 「获取存储内容」是快捷指令自己的 key-value 存储，跨运行、跨重启都在。这里只存
# **一段文本**（上次和服务端对齐到的内容），用来判断「剪贴板变了没」。
#
# 不存整份 latest_global：手机端的判断只需要「内容」，而服务端本来就按设备名
# 记着 clients[source]，多存一份 pasted 只会让两边状态各自漂移。首次运行拿到
# 空值，比较时按空串处理 —— 于是首轮会把剪贴板内容上行一次，这是对的。
b.comment('4. 读缓存：上次与服务端对齐到的内容（只存一段文本）\n'
          '首次运行为空 —— 首轮会把剪贴板内容上行一次')
b.setvar('缓存', b.stored_get(CACHE))

# ---- 5 轮询 ---------------------------------------------------------------
b.comment('5. 轮询：一轮 = 读剪贴板 → 与服务端对齐 → 有新内容就写回 → 等待\n'
          '轮数由「重复」给，等待间隔按累计时长分三档\n'
          '每轮都判「还在前台 App 吗 / 电脑端还在吗」，不成立就停')
# 累计等待秒数。用来选等待档位：轮次不等于时长，靠它才判得准。
b.setvar('已过', b.calculate(['0']))
g_loop = b.repeat_count(c(LOOP))

# --- 5.0 每轮两道闸：还在前台 App 里 / 电脑端还在 ---
#
# 用户切走（回桌面、或那个 App 被挂起）时「当前 App」就空了 —— 立刻停，别在后台
# 空转耗电。这是原版有意为之的省电设计，不是判错：同步需求只发生在刷微信、
# 逛浏览器的时候。
#
# 探活放第二条：断网时「获取 URL 内容」会直接抛错并中断整条指令，那一轮连日志
# 都留不下来。代价是每轮一次 /status（原版同样如此）。
b.comment('每轮先确认电脑端还在；「前台检查」= 每轮 时再确认还在前台 App 里')
# ⚠ 这里曾经每轮都读一次「当前 App」，代价是**每轮都弹一次** iOS 隐私确认
# （「允许把 1 个 app 和 1 个文本项目发送给 <主机> 吗」，点「始终允许」也只对当次
# 有效）。原因见 docs「iOS 权限弹窗」：app 类内容项跟网络发送同处一次运行，系统就
# 认为「可能把它共享出去」，而且按内容项实例逐次评估，同一次运行里也照样重问。
#
# 现在默认「仅入口」：白名单只在开头读一次（那一次仍会发生，是省电主闸，不能省），
# 循环里不再碰「当前 App」，一次运行最多只在开头被问一次。
# 想恢复原版「切走就停」的行为，把配置里的「前台检查」改成 每轮 —— 代价是每轮弹窗。
g_check = b.if_(c(CHECK_APP), IS, '每轮')
# 「获取当前 App」给的是 App 类型的内容项，而 iOS 的条件菜单对 App 类型没有
# 「没有值」这一项 —— 直接判空会标红报「请为此操作中的每个参数选取值」。先把
# 输出转成文本再判空（真机上验过：手动把类型改成文本才能选「没有值」）。
g_left = b.if_(b.current_app(text=True), NO_VALUE)
dbg(['[', var('Repeat Index'), '] 已离开前台 App，停'])
b.exit()
b.endif(g_left)
b.endif(g_check)

b.setvar('可达', b.reachable(e('状态')))
g_link = b.if_(var('可达'), IS_NOT, '是')
dbg(['[', var('Repeat Index'), '] 电脑端不可达，停'])
b.exit()
b.endif(g_link)

# --- 5.1 读剪贴板 ---
#
# 取两份：剪贴板本身用来判类型，「文本」那份用来比较和上行 —— 后面全都走文本，
# 免得 URL / 多信息文本 / 图像这些类型各自按自己的规则比，比出假差异。
clip = b.get_clipboard()
b.setvar('本地', act_out(b.text([clip]), '文本'))
b.setvar('类型', b.item_type(clip))

# --- 5.2 上行 / 拉取 ---
g_text = b.if_any(var('类型'), TEXT_TYPES)
b.comment('剪贴板是文本类：和缓存比一下\n'
          '不同 = 手机刚复制了新东西 → 上行；相同 = 纯拉取')
g_changed = b.if_(var('本地'), IS_NOT, string=tok([var('缓存')]))
up = b.post_json(e('同步'),
                 headers=[('key', c(KEY))],
                 body=[('key', c(KEY)), ('content', var('本地')),
                       ('source', var('来源'))])
b.setvar('响应', up)
# 上行成功就等于「服务端已经知道这段内容」：缓存立刻记成本地。少了这一步，
# 服务端回 {pasted:true} 空包时缓存不动，同一段文字会每轮再上行一次。
b.setvar('缓存', var('本地'))
log(['[', var('Repeat Index'), '] 文本变了 → 上行'])
b.else_(g_changed)
pull_a = b.get([e('最新'), '?source=', var('来源编码')], headers=[('key', c(KEY))])
b.setvar('响应', pull_a)
log(['[', var('Repeat Index'), '] 文本未变 → 拉取'])
b.endif(g_changed)
b.else_(g_text)
b.comment('剪贴板不是文本（图片 / 文件 / 空）：直接拉\n'
          '别把图像或文件名的文本形态当成新内容传上去')
pull_b = b.get([e('最新'), '?source=', var('来源编码')], headers=[('key', c(KEY))])
b.setvar('响应', pull_b)
log(['[', var('Repeat Index'), '] 非文本（', var('类型'), '）→ 拉取'])
b.endif(g_text)

# --- 5.3 对齐缓存 ---
#
# 服务端只在「这份该投递给本机」时才带 content，其余一律是 {pasted: true} 空包。
# 所以判据就是「有没有 content」：原版把空回包也盖进缓存，下一轮拿空串跟剪贴板
# 比，必然判成「变了」，同一段文字每轮再上行一次（靠服务端回音/去重拦下来，
# 纯白费往返）。
b.comment('服务端给了 content 才更新缓存；{pasted:true} 这种空包一个字段都不动')
latest = b.detect_dict(var('响应'))
b.setvar('最新', pick(latest, 'latest_global'))
b.setvar('待投放', pick(var('最新'), 'content'))
g_has = b.if_(var('待投放'), ANY)
b.setvar('缓存', var('待投放'))
log(['[', var('Repeat Index'), '] ← 有 content'])
b.else_(g_has)
log(['[', var('Repeat Index'), '] ← 无新内容'])
b.endif(g_has)

# --- 5.4 写回剪贴板 ---
#
# 两个条件：有内容、和手机剪贴板里的不一样。少了第二个，用户刚复制的东西会被
# 上一份内容盖回去。
#
# 不再比 pasted（原版第三条件是「已粘贴 是 否」）：服务端带 content 时 pasted
# 恒为 false，而「已投递过」的情况根本不会带 content —— 这条已经由第一个条件
# 覆盖了；何况 pasted 是 JSON 布尔，拿它跟中文「否」比，是整套流程里最脆的一处。
b.comment('写回：服务端给了 content，且和剪贴板里的不一样\n'
          '（带 content 就等于「还没投递过」，不用再看 pasted）')
g_write = b.if_conds([
    {'subject': var('待投放'), 'cond': ANY},
    {'subject': var('待投放'), 'cond': IS_NOT, 'string': tok([var('本地')])},
], ALL_OF)
g_notify = b.if_(c(NOTIFY_ON), IS, '开')
b.notify([var('待投放')])
b.endif(g_notify)
b.clipboard(var('待投放'))
log(['[', var('Repeat Index'), '] 已写回剪贴板'])
b.else_(g_write)
log(['[', var('Repeat Index'), '] 内容相同，不写回'])
b.endif(g_write)

# 每轮落一次缓存（上行分支也改过它），下次运行接着用。
b.stored_set(var('缓存'), CACHE)

# 每轮推一次日志：一次运行 40 轮就是 40 次 POST，只有「调试」= 开 时才发生。
# 不每行都推（一次运行几百行 × 全量重发会把轮次拖慢一倍），但也不能只在结尾推
# —— 那样中途被打断就什么都看不到。
log_push()

# --- 5.5 续跑 ---
#
# 后台运行有总时长限制，轮次快用完时用「运行快捷指令（自己）」再起一份接着跑，
# 比在原地被系统掐断干净：本份主动 exit，边界清楚，也不会卡在最后那几次请求上。
#
# 判据：剩余 = 重复 - 当前轮次，剩余 ≤ RESTART_MARGIN 就续。
#   · 触发点放在「等待」之前 —— 续跑那一轮不必先空等 3 秒；
#   · 新一份会重过白名单和守卫，所以电量低 / 断网 / 切到桌面时链子自己断；
#   · 「重复」必须 ≥ 12，否则第 1 轮就命中，变成每轮重启（见文件头「续跑」）。
#
# 原版没有开关、每轮无条件判同一个条件：60 轮里第 50 轮就重启，于是永远跑不完
# —— 那就是它的「常驻」实现。这里给成开关、默认关：关掉才是「跑完就结束」，
# 靠自动化「打开 App 时」重新进入。
b.comment('续跑：剩余 ≤ 10 轮时调用自己再跑一份，然后本份结束\n'
          '配置.续跑 = 开 才生效（默认关）；开时「重复」必须 ≥ 12')
g_resume = b.if_(c(RESUME), IS, '开')
b.setvar('剩余', b.calculate([c(LOOP), '-', var('Repeat Index')]))
g_rest = b.if_(var('剩余'), LTE, number=RESTART_MARGIN)
dbg(['[', var('Repeat Index'), '] 剩余 ', var('剩余'), ' 轮 ≤ ', str(RESTART_MARGIN),
     '，续跑（本份结束，新一份重过白名单与守卫）'])
b.run_self()
b.exit()
b.endif(g_rest)
b.endif(g_resume)

# --- 5.6 等待 ---
#
# 三档按**累计等待时长**选，不按轮次。原版写的是 `重复索引 < 60` / `< 180`：
# 间隔 3 秒时 60 轮总共才 180 秒，第二档只在最后一轮生效、第三档永远进不去。
# 默认 40 轮 ≈ 20×3 + 20×5 = 160 秒，只用得到前两档；想要第三档就把「重复」
# 调到 45 轮以上（累计过 180 秒后每轮等「时间3」），别原地把「重复」调大。
b.comment('等待：累计 0–60 秒 时间1，60–180 秒 时间2，之后 时间3\n'
          '已过 = 累计等待秒数，每轮把自己那一档加进去')
g_fast = b.if_(var('已过'), LT, number=FAST_UNTIL)
b.delay(c(GAP1))
b.setvar('已过', b.calculate([var('已过'), '+', c(GAP1)]))
b.else_(g_fast)
g_mid = b.if_(var('已过'), LT, number=SLOW_UNTIL)
b.delay(c(GAP2))
b.setvar('已过', b.calculate([var('已过'), '+', c(GAP2)]))
b.else_(g_mid)
b.delay(c(GAP3))
b.setvar('已过', b.calculate([var('已过'), '+', c(GAP3)]))
b.endif(g_mid)
b.endif(g_fast)

b.end_repeat(g_loop, each=False)
dbg(['=== 文字同步结束（', c(LOOP), ' 轮已跑完）==='])

b.comment('结束。平时静默；排查时把「配置」里的「调试」改成 开，'
          '过程会写进电脑端 log/ios_shortcut.log')

b.endif(g_fore)      # 0. 前台 App 白名单

# ------------------------------------------------------------------ 输出

meta = dict(DEFAULT_META)
set_icon(meta, 59403)          # 双气泡 —— 真机导出样本里的图标
meta['WFWorkflowActions'] = b.a

with open(OUT, 'wb') as f:
    plistlib.dump(meta, f, fmt=plistlib.FMT_XML, sort_keys=False)

print('动作数:', len(b.a))
print('输出:', OUT, os.path.getsize(OUT), 'B')
