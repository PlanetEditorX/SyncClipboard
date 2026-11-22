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
5. 运行日志：**不受任何开关控制**，每个节点把累积后的完整日志 POST 到
   `/ios_log`，服务端覆盖写 `<项目>/log/ios_shortcut.log`。不走 iOS 的
   「存储到文件」—— 那条路保存纯文本会静默失败（v7 真机：一条日志都没有）。
   结尾再尽力存一份 `SyncClipboard-调试.log` 到「下载」给自己看，存不上
   不影响排查。中间不弹任何通知，只留结尾一条汇总通知。
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
    python build_shortcut.py            # 生成 unsigned plist
"""

import json
import os
import plistlib
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, '获取同步-unsigned.plist')          # 仅借用顶层元数据
OUT = os.path.join(HERE, '获取同步v9-unsigned.plist')

OBJ = '\ufffc'          # 文本里变量的占位符


def U():
    return str(uuid.uuid4()).upper()


# ---------------------------------------------------------------- 序列化助手

def tok(parts):
    """拼一个 WFTextTokenString。parts 里 str 原样拼接，dict 作为变量占位。"""
    out, atts = '', {}
    for p in parts:
        if isinstance(p, str):
            out += p
        else:
            atts['{%d, 1}' % len(out)] = p
            out += OBJ
    for ch in out:               # attachmentsByRange 用 UTF-16 代码单元偏移
        assert ord(ch) <= 0xFFFF, '文本含非 BMP 字符（如 emoji），占位符偏移会错'
    value = {'string': out}
    if atts:
        value['attachmentsByRange'] = atts
    return {'Value': value, 'WFSerializationType': 'WFTextTokenString'}


def att(x):
    return {'Value': x, 'WFSerializationType': 'WFTextTokenAttachment'}


def act_out(uid, name=None, agg=None):
    """引用某个动作的输出。uid 传字符串则新建引用；传已有引用则追加取值链。"""
    if isinstance(uid, dict):
        d = dict(uid)
        if agg:
            d['Aggrandizements'] = list(d.get('Aggrandizements', [])) + list(agg)
        return d
    d = {'OutputName': name, 'OutputUUID': uid, 'Type': 'ActionOutput'}
    if agg:
        d['Aggrandizements'] = agg
    return d


def pick(subject, *keys):
    """subject.<keys[0]>.<keys[1]>… 逐层取词典值。"""
    agg = []
    for k in keys:
        agg.extend(dkey(k))
    return act_out(subject, agg=agg)


def var(name, agg=None):
    d = {'Type': 'Variable', 'VariableName': name}
    if agg:
        d['Aggrandizements'] = agg
    return d


def dkey(key):
    """取词典的某个键：先强制成词典，再按键取值。"""
    return [
        {'CoercionItemClass': 'WFDictionaryContentItem',
         'Type': 'WFCoercionVariableAggrandizement'},
        {'DictionaryKey': key, 'Type': 'WFDictionaryValueVariableAggrandizement'},
    ]


def as_text():
    return [{'CoercionItemClass': 'WFStringContentItem',
             'Type': 'WFCoercionVariableAggrandizement'}]


def cond_input(subj, coerce=True):
    """「如果 <变量> 是/不是 X」的被判断对象。

    文本比较必须带 WFStringContentItem 强制转换（放在 Aggrandizements 最后），
    否则 iOS 判断不出参数类型，动作会标红报「请为此操作中的每个参数选取值」。
    数值比较（大于/小于）不能加，加了就变字符串比大小。
    """
    if coerce:
        subj = act_out(subj, agg=as_text())
    return {'Type': 'Variable', 'Variable': att(subj)}


def dict_field(pairs):
    items = []
    for k, v in pairs:
        items.append({
            'WFItemType': 0,
            'WFKey': tok([k]),
            'WFValue': tok([v]),
        })
    return {'Value': {'WFDictionaryFieldValueItems': items},
            'WFSerializationType': 'WFDictionaryFieldValue'}


# iOS 上「下载」目录的定位信息，换设备需要重新在 App 里选一次目录
FOLDER_DOWNLOAD = {
    'displayName': '下载',
    'filename': '下载',
    'fileLocation': {
        'WFFileLocationType': 'LocalStorage',
        'crossDeviceItemID': ('deviceSpecific:2AECE2CD-33B9-4F1D-B7F3-7C2387398365'
                              ':fp:/9OxbzAI+aMJNbh1YMODYORPHihwLBA7Hpj1XpkYvd_0='
                              '/com.apple.FileProvider.LocalStorage//fid=1130423'),
        'fileProviderDomainID': 'com.apple.FileProvider.LocalStorage',
        'relativeSubpath': '下载',
    },
}

# 条件码
IS, IS_NOT, GT = 4, 5, 2


class SC:
    def __init__(self):
        self.a = []

    # -- 底层 ------------------------------------------------------------
    def _add(self, ident, params):
        self.a.append({'WFWorkflowActionIdentifier': ident,
                       'WFWorkflowActionParameters': params})
        return self.a[-1]

    def comment(self, text):
        self._add('is.workflow.actions.comment', {'WFCommentActionText': text})

    # -- 数据 ------------------------------------------------------------
    def dictionary(self, pairs, out_name=None):
        uid = U()
        p = {'UUID': uid, 'WFItems': dict_field(pairs)}
        if out_name:
            p['CustomOutputName'] = out_name
        self._add('is.workflow.actions.dictionary', p)
        return uid

    def text(self, parts):
        uid = U()
        self._add('is.workflow.actions.gettext',
                  {'UUID': uid, 'WFTextActionText': tok(parts)})
        return uid

    def setvar(self, name, subject):
        self._add('is.workflow.actions.setvariable',
                  {'WFInput': att(subject), 'WFVariableName': name})

    def count(self, subject):
        uid = U()
        self._add('is.workflow.actions.count', {'UUID': uid, 'Input': att(subject)})
        return uid

    def urlencode(self, subject):
        uid = U()
        self._add('is.workflow.actions.urlencode',
                  {'UUID': uid, 'WFInput': tok([subject])})
        return uid

    def device_name(self):
        uid = U()
        self._add('is.workflow.actions.getdevicedetails', {'UUID': uid})
        return act_out(uid, '设备名称')

    # -- 网络 ------------------------------------------------------------
    def get(self, url_subject):
        uid = U()
        self._add('is.workflow.actions.downloadurl',
                  {'UUID': uid, 'ShowHeaders': True, 'WFURL': tok([url_subject])})
        return act_out(uid, 'URL的内容')

    def post_json(self, url_subject, headers, body):
        uid = U()
        self._add('is.workflow.actions.downloadurl', {
            'UUID': uid,
            'ShowHeaders': True,
            'WFHTTPMethod': 'POST',
            'WFURL': tok([url_subject]),
            'WFHTTPHeaders': dict_field(headers),
            'WFJSONValues': dict_field(body),
        })
        return act_out(uid, 'URL的内容')

    def notify(self, parts, sound=False):
        """先造一条文本（供通知标题用），再弹通知。"""
        t = self.text(parts)
        self._add('is.workflow.actions.notification', {
            'UUID': U(),
            'WFInput': att(act_out(t, '文本')),
            'WFNotificationActionBody': tok(parts),
            'WFNotificationActionSound': sound,
        })

    def clipboard(self, subject):
        self._add('is.workflow.actions.setclipboard',
                  {'UUID': U(), 'WFInput': att(subject)})

    def exit(self):
        self._add('is.workflow.actions.exit', {})

    # -- 文件 ------------------------------------------------------------
    def save_to_folder(self, subject, dest=None):
        """存储到文件。

        dest 是「完整目标路径」的 parts 列表，**含文件名**，例如
        `[根子路径, 目录名, '/', 子目录, '/', 文件名]`。

        这个参数（`WFFileDestinationPath`，UI 上叫 Subpath）的语义是**路径
        本身**，不是「目录前缀」：末尾带 "/" 时最后一段才被当成目录，否则整
        串直接被拿去当文件名。而且 iOS 会先对路径做一遍规范化 —— 变量值末尾
        的 "/" 靠不住，实测会丢，于是 `目录名/` 变成 `目录名` 并成为文件名
        （v6 真机：每个文件都用目录名当名字、互相覆盖，最后只剩一个）。
        所以斜杠一律用**字面文本**给出，绝不依赖变量内容的末尾字符。

        `WFDestinationPath` 是旧版键名，一并写上去；多写的键 iOS 会忽略。
        """
        uid = U()
        p = {
            'UUID': uid,
            'WFAskWhereToSave': False,
            'WFFolder': FOLDER_DOWNLOAD,
            'WFInput': att(subject),
        }
        if dest is not None:
            p['WFFileDestinationPath'] = tok(dest)
            p['WFDestinationPath'] = tok(dest)
            # 同一个文件重复同步时覆盖，否则 iOS 会存成「a 1.txt」
            p['WFSaveFileOverwrite'] = True
            p['WFOverwriteFile'] = True
        self._add('is.workflow.actions.documentpicker.save', p)
        return act_out(uid, 'Saved Files')

    # -- 控制流 ----------------------------------------------------------
    def if_(self, subject, cond, string=None, number=None):
        g = U()
        p = {'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 0,
             'WFCondition': cond,
             'WFInput': cond_input(subject, coerce=string is not None)}
        if string is not None:
            p['WFConditionalActionString'] = string
        if number is not None:
            p['WFNumberValue'] = str(number)
        self._add('is.workflow.actions.conditional', p)
        return g

    def if_any(self, subject, strings):
        """「如果 … 是 A 或 B」。subject 需已是 var()/act_out() 描述。"""
        g = U()
        templates = []
        for s in strings:
            templates.append({
                'WFCondition': IS,
                'WFConditionalActionString': s,
                'WFInput': cond_input(subject, coerce=True),
            })
        self._add('is.workflow.actions.conditional', {
            'GroupingIdentifier': g,
            'UUID': U(),
            'WFControlFlowMode': 0,
            'WFConditions': {
                'Value': {
                    'WFActionParameterFilterPrefix': 0,
                    'WFActionParameterFilterTemplates': templates,
                    'WFContentPredicateBoundedDate': False,
                },
                'WFSerializationType': 'WFContentPredicateTableTemplate',
            },
        })
        return g

    def else_(self, g):
        self._add('is.workflow.actions.conditional',
                  {'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 1})

    def endif(self, g):
        self._add('is.workflow.actions.conditional',
                  {'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 2})

    def repeat_each(self, subject):
        g = U()
        self._add('is.workflow.actions.repeat.each', {
            'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 0,
            'WFInput': att(subject),
        })
        return g

    def end_repeat(self, g, each=False):
        ident = ('is.workflow.actions.repeat.each' if each
                 else 'is.workflow.actions.repeat.count')
        self._add(ident, {'GroupingIdentifier': g, 'UUID': U(),
                          'WFControlFlowMode': 2})


# ------------------------------------------------------------------ 构建流程

b = SC()
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
ROOT_SUB = '根子路径'    # 落盘子路径的前缀（见 cfg 里的说明）
REPEAT_ITEM = 'Repeat Item'

b.comment('SyncClipboard 拉取 v8（逐文件保目录结构）\n'
          '改配置：编辑下面「配置」词典里的协议/主机/端口/密钥\n'
          '目录按清单逐个文件取，目录结构与电脑上完全一致（不再打包/解压）\n'
          '过程写进「下载/SyncClipboard-调试.log」，跑完把它发回来即可\n'
          '文件统一存入「下载」，需要换目录请在「存储到文件」动作里重选')

cfg = b.dictionary([
    (PROTO, 'http'),
    (HOST, '192.168.1.24'),
    (PORT, '8000'),
    (KEY, '123456'),
    # 落盘路径的前缀。留空即「相对所选的『下载』文件夹」；万一你的 iOS 把
    # 它当成存储根相对路径，文件会落到「下载/下载/项目名/…」，那时把它填成
    # 「下载/」即可，不必重新生成快捷指令。
    (ROOT_SUB, ''),
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

# ---- 运行日志 -------------------------------------------------------------
#
# 日志**走 HTTP 推给电脑端**（POST /ios_log），由服务端写成
# `<项目>/log/ios_shortcut.log`。
#
# 为什么不用「存储到文件」写进手机「下载」：v7 真机证明那条路会**静默失败**
# —— 既没有文件，也没有任何提示。日志是排查的唯一抓手，它自己静默丢一次，
# 人就只能干瞪眼。而 HTTP 通道在 /request_file 上已经跑通无数次，日志直接落
# 到电脑上，排查时也不用再让用户回传文件。
#
# 每次推的都是**累积后的完整日志**，服务端覆盖写同一个文件：崩在哪一步，电脑
# 上那份就停在哪一步。循环里每处理完一个文件推一次，所以第一条出问题的记录
# 一定留得住。
LOG_FILE = 'SyncClipboard-调试.log'      # 同时留一份到手机「下载」，方便自己看

b.setvar('日志', act_out(b.text(['SyncClipboard 同步日志\n']), '文本'))
b.setvar('日志地址', act_out(b.text([var('服务器'), '/ios_log']), '文本'))


def log(parts):
    """往「日志」变量追加一行。"""
    b.setvar('日志', b.text([var('日志'), *parts, '\n']))


def log_push():
    """把当前完整日志推给电脑端（服务端覆盖写 log/ios_shortcut.log）。

    用和 /request_file 同一套「获取 URL 内容 + JSON body」，参数已验证过。
    电脑端是旧版（没有 /ios_log）时只会拿到 404 的内容，不会中断流程。
    """
    b.post_json(
        var('日志地址'),
        headers=[('key', act_out(cfg, '配置', dkey(KEY)))],
        body=[('log', var('日志'))],
    )


def dbg(parts):
    """记一行日志并立刻推给电脑端（崩在下一步也能看到已发生的事）。"""
    log(parts)
    log_push()


def save_confirmed(subject, label_parts, dest=None):
    """存盘并核对真的落了盘，结果只进日志。

    「存储到文件」拿到空输入时**不报错也不存盘**，直接在后面接一句「已保存」
    就是自欺欺人。这里拿存盘动作自己的输出做判据（它有输出就说明真写了），
    没有就在日志里写「保存失败」。
    """
    saved = b.save_to_folder(subject, dest=dest)
    g_ok = b.if_(saved, 100)                # 有任何值 = 确实写入了
    log(['   OK 写入 ', *label_parts])
    b.else_(g_ok)
    log(['   !! 保存失败（动作输出为空，文件没落地）', *label_parts])
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
b.notify(['服务端没有新文本'])
b.endif(g_has_text)

b.else_(g_text)

# ---- 5 分支：文件 ---------------------------------------------------------
b.comment('5. 文件分支：先逐个下载普通文件（一层循环），再按合并清单'
          '逐文件下载目录内容（另一层循环）。两段并列，不做嵌套')

g_kind = b.if_any(var('类型'), ['file_list', 'folder'])

b.setvar('文件列表', pick(resp, 'files'))
b.setvar('文件数量', act_out(b.count(var('文件列表')), '计数'))
dbg(['③ 待下载 ', var('文件数量'), ' 项'])

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
# 普通文件没有子目录，dest 就是「根子路径 + 文件名」；根子路径留空时只剩
# 文件名，所以这里不额外塞字面 "/"，避免拼出前导斜杠。
save_confirmed(b.get(f_url), [f_name],
               dest=[act_out(cfg, '配置', dkey(ROOT_SUB)), f_name])
b.endif(g_is_file)
b.end_repeat(g_each, each=True)

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

# 结尾：把整份日志再存一份到手机「下载」，方便用户自己翻。
# 这一步**不校验、不提示**：能存就存，存不上也不影响排查 —— 电脑端的
# log/ios_shortcut.log 才是权威副本（每个节点都已推过）。
b.save_to_folder(var('日志'), dest=[LOG_FILE])

b.comment('结束。若某个文件没出现，把「下载/SyncClipboard-调试.log」发回去看：'
          '里面逐条记了清单给到的 dir/prefix 和每次存盘的成功/失败。'
          '日志始终写，不受任何开关控制')

# ------------------------------------------------------------------ 输出

if os.path.exists(REF):
    meta = plistlib.load(open(REF, 'rb'))
    meta = {k: v for k, v in meta.items() if k != 'WFWorkflowActions'}
else:
    meta = {'WFWorkflowClientVersion': '3612.0.1.3',
            'WFWorkflowMinimumClientVersion': 3010,
            'WFWorkflowMinimumClientVersionString': '3010',
            'WFWorkflowTypes': ['WFWorkflowTypeShowInSearch'],
            'WFWorkflowImportQuestions': [],
            'WFQuickActionSurfaces': [],
            'WFWorkflowInputContentItemClasses': ['WFGenericFileContentItem'],
            'WFWorkflowOutputContentItemClasses': [],
            'WFWorkflowHasOutputFallback': False,
            'WFWorkflowHasShortcutInputVariables': False,
            'WFWorkflowIcon': {'WFWorkflowIconGlyphNumber': 59711,
                               'WFWorkflowIconStartColor': 255}}
meta['WFWorkflowActions'] = b.a

with open(OUT, 'wb') as f:
    plistlib.dump(meta, f, fmt=plistlib.FMT_XML, sort_keys=False)

print('动作数:', len(b.a))
print('输出:', OUT, os.path.getsize(OUT), 'B')
