# -*- coding: utf-8 -*-
"""从头重构「获取同步」iOS 快捷指令。

设计要点
--------
1. 单一入口：一次 /request_file，返回体固化成「响应」变量，后续全部读它。
2. 结构收敛：type == text 走文本分支，否则走文件分支；文件分支内按
   Repeat Item.kind 判断目录/文件，因此「单目录」「多文件」「目录+文件混合」
   三种情况共用同一段下载循环。
3. 调试开关：配置词典里的「调试」= 开/关。开时每个步骤弹一条静音通知
   （进入通知中心可回看），关时全程静默，只留结束提示。
4. 打包轮询改成嵌套循环：内层 15 次 × 2 秒 ≈ 30 秒，外层 40 轮 ≈ 最长 20 分钟，
   外层每轮输出一条进度通知，避免每 2 秒刷一条通知。上限定在 20 分钟是因为
   客户端的「共享存活」从电脑复制那一刻起算、默认 30 分钟，超时要能先于它触发。

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
OUT = os.path.join(HERE, '获取同步v2-unsigned.plist')

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

    def wait(self, seconds):
        self._add('is.workflow.actions.delay', {'WFDelayTime': float(seconds)})

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
    def unzip(self):
        """解压缩。系统动作没有输入参数，隐式取上一步输出——所以调用前必须
        紧跟「获取URL内容」下载动作，中间不能插其它动作。"""
        uid = U()
        self._add('is.workflow.actions.unzip', {'UUID': uid})
        return act_out(uid, 'Files')

    def save_to_folder(self, subject):
        self._add('is.workflow.actions.documentpicker.save', {
            'UUID': U(),
            'WFAskWhereToSave': False,
            'WFFolder': FOLDER_DOWNLOAD,
            'WFInput': att(subject),
        })

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

    def repeat(self, n):
        g = U()
        self._add('is.workflow.actions.repeat.count', {
            'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 0,
            'WFRepeatCount': float(n),
        })
        return g

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
DEBUG = '调试'          # 配置词典里的开关键
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
REPEAT_ITEM = 'Repeat Item'

b.comment('SyncClipboard 拉取 v2（重构版）\n'
          '改配置：编辑下面「配置」词典里的协议/主机/端口/密钥/调试\n'
          '调试 = 开 时每个步骤弹一条静音通知，跑完可在通知中心回看\n'
          '文件统一存入「下载」，需要换目录请在两个「存储到文件」动作里重选')

cfg = b.dictionary([
    (PROTO, 'http'),
    (HOST, '192.168.1.24'),
    (PORT, '8000'),
    (KEY, '123456'),
    (DEBUG, '开'),
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

def dbg(parts):
    """调试输出：只有「配置.调试」= 开 时才弹通知。"""
    g = b.if_(act_out(cfg, '配置', dkey(DEBUG)), IS, '开')
    b.notify(parts)
    b.endif(g)

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
b.comment('5. 文件分支：type 为 file_list 时逐个下载；为 folder 时同一段逻辑'
          '按 kind 判断，先等发送端打包再下载并解压')

g_kind = b.if_any(var('类型'), ['file_list', 'folder'])

b.setvar('文件列表', pick(resp, 'files'))
b.setvar('文件数量', act_out(b.count(var('文件列表')), '计数'))
dbg(['③ 待下载 ', var('文件数量'), ' 项'])

g_has = b.if_(var('文件数量'), GT, number=0)

g_each = b.repeat_each(var('文件列表'))

item = var(REPEAT_ITEM)
item_name = var(REPEAT_ITEM, dkey('name'))
item_kind = var(REPEAT_ITEM, dkey('kind'))
item_url = var(REPEAT_ITEM, dkey('download_url'))

dbg(['   · 开始下载 ', item_name, '（', item_kind, '）'])

g_is_folder = b.if_(item_kind, IS, 'folder')

# --- 目录：先等打包完成 ---
b.comment('打包轮询：内层 15 次 × 2 秒 ≈ 30 秒，外层 40 轮 ≈ 最长 20 分钟；\n'
          '外层每轮输出一条进度，避免每 2 秒刷一条通知。\n'
          '上限必须小于客户端「共享存活」（默认 30 分钟），否则超时前共享已被清掉')
b.setvar('打包地址', var(REPEAT_ITEM, dkey('prepare_url')))
b.setvar('打包状态', act_out(b.text(['idle']), '文本'))

# 外层 40 轮 × 30 秒 = 20 分钟。客户端共享存活默认 30 分钟（自电脑复制那一刻
# 起算，打包 + 下载都算在内），这里必须比它短，否则「打包超时」永远走不到 ——
# 中途就已被客户端清掉共享、/prepare 变 404。
g_outer = b.repeat(40)
g_outer_body = b.if_(var('打包状态'), IS_NOT, 'ready')

g_inner = b.repeat(15)
g_inner_body = b.if_(var('打包状态'), IS_NOT, 'ready')

pack = b.get(var('打包地址'))
b.setvar('打包进度', pack)
b.setvar('打包状态', pick(pack, 'status'))

g_bad = b.if_any(var('打包状态'), ['error', 'unknown', 'cancelled'])
b.notify(['打包失败（', var('打包状态'), '），请回电脑重新复制'])
b.exit()
b.endif(g_bad)

b.wait(2)
b.endif(g_inner_body)
b.end_repeat(g_inner, each=False)

dbg(['   打包 ', var('打包状态'), ' ', var('打包进度', dkey('progress'))])

b.endif(g_outer_body)
b.end_repeat(g_outer, each=False)

g_timeout = b.if_(var('打包状态'), IS_NOT, 'ready')
b.notify(['打包超时（已等待约 20 分钟）：', item_name])
b.exit()
b.endif(g_timeout)

b.get(item_url)                      # 解压缩隐式吃这一步的输出，不能插动作
b.save_to_folder(b.unzip())

b.else_(g_is_folder)

# --- 普通文件：直接存 ---
b.save_to_folder(b.get(item_url))

b.endif(g_is_folder)

dbg(['   已保存 ', item_name])
b.end_repeat(g_each, each=True)

b.notify(['同步完成：', var('文件数量'), ' 项已存入「下载」'])

b.else_(g_has)
b.notify(['响应里没有文件（type=', var('类型'), '）'])
b.endif(g_has)

b.else_(g_kind)
b.notify(['请求失败：', var('响应', dkey('message'))])
b.endif(g_kind)

b.endif(g_text)

b.comment('结束。若某个文件没出现，先看「调试」通知里 ② ③ 的输出，'
          '再看服务端日志是否收到 /request_file')

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
