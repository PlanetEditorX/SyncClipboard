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
5. 调试开关：配置词典里的「调试」= 开/关。开时每个步骤弹一条静音通知
   （进入通知中心可回看），关时全程静默，只留结束提示。
6. 目录传输不需要等待：清单请求即取，没有打包轮询。

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
OUT = os.path.join(HERE, '获取同步v6-unsigned.plist')

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
    def save_to_folder(self, subject, subpath=None):
        """存储到文件。

        subpath 是「子路径」（parts 列表），用来在目标文件夹里重建目录层级：
        iOS 的实现是 `WFFileDestinationPath`，**必须以 "/" 结尾**才被当成
        目录，否则它会拿这个字符串去改文件名 —— 这也是包内层级丢失后一个
        常见的坑。`WFDestinationPath` 是旧版键名，一并写上去，多写的键
        iOS 会忽略，少写则整段子路径都失效。
        """
        uid = U()
        p = {
            'UUID': uid,
            'WFAskWhereToSave': False,
            'WFFolder': FOLDER_DOWNLOAD,
            'WFInput': att(subject),
        }
        if subpath is not None:
            p['WFFileDestinationPath'] = tok(subpath)
            p['WFDestinationPath'] = tok(subpath)
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
DEBUG = '调试'          # 配置词典里的开关键
PROTO, HOST, PORT, KEY = '协议', '主机', '端口', '密钥'
ROOT_SUB = '根子路径'    # 落盘子路径的前缀（见 cfg 里的说明）
REPEAT_ITEM = 'Repeat Item'

b.comment('SyncClipboard 拉取 v6（逐文件保目录结构）\n'
          '改配置：编辑下面「配置」词典里的协议/主机/端口/密钥/调试\n'
          '目录按清单逐个文件取，目录结构与电脑上完全一致（不再打包/解压）\n'
          '调试 = 开 时每个步骤弹一条静音通知，跑完可在通知中心回看\n'
          '文件统一存入「下载」，需要换目录请在「存储到文件」动作里重选')

cfg = b.dictionary([
    (PROTO, 'http'),
    (HOST, '192.168.1.24'),
    (PORT, '8000'),
    (KEY, '123456'),
    (DEBUG, '开'),
    # 落盘子路径的前缀。留空即「子路径相对所选文件夹（下载）」；万一你的
    # iOS 把它当成存储根相对路径，文件会落到「下载/下载/项目名/…」，那时
    # 把它填成「下载/」即可，不必重新生成快捷指令。
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

def dbg(parts):
    """调试输出：只有「配置.调试」= 开 时才弹通知。"""
    g = b.if_(act_out(cfg, '配置', dkey(DEBUG)), IS, '开')
    b.notify(parts)
    b.endif(g)


def save_confirmed(subject, label, subpath=None):
    """存盘并核对真的落了盘。

    「存储到文件」拿到空输入时**不报错也不存盘**，直接在后面接一句「已保存」
    就是自欺欺人 —— 之前那版正是这样：解压缩没拿到归档、输出为空，于是什么都
    没写进「下载」，通知却显示成功。这里拿存盘动作自己的输出做判据（它有输出
    就说明真写了文件），没有就明确报错，把问题暴露在用户面前而不是日志里。
    """
    saved = b.save_to_folder(subject, subpath=subpath)
    g_ok = b.if_(saved, 100)                # 有任何值 = 确实写入了
    dbg(['   已保存 ', label])
    b.else_(g_ok)
    b.notify(['保存失败：', label, ' 没写进「下载」'])
    b.endif(g_ok)

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
g_has = b.if_(var('文件数量'), GT, number=0)

g_each = b.repeat_each(var('文件列表'))
item_kind = var(REPEAT_ITEM, dkey('kind'))
g_is_file = b.if_(item_kind, IS, 'file')
save_confirmed(b.get(var(REPEAT_ITEM, dkey('download_url'))),
               var(REPEAT_ITEM, dkey('name')))
b.endif(g_is_file)
b.end_repeat(g_each, each=True)

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

g_has_files = b.if_(var('清单文件数'), GT, number=0)

g_each_file = b.repeat_each(pick(listing, 'entries'))

entry_dir = var(REPEAT_ITEM, dkey('dir'))
entry_name = var(REPEAT_ITEM, dkey('name'))
entry_url = var(REPEAT_ITEM, dkey('url'))
dbg(['   · ', entry_dir, entry_name])

g_has_url = b.if_(entry_url, 100)
# 子路径直接用清单给的 dir：它已含「目录名/」前缀，且末尾带 "/" —— iOS 见到
# 斜杠才当目录，否则会拿这串去改文件名。前面再挂配置里的「根子路径」。
save_confirmed(b.get(entry_url), [entry_dir, entry_name],
               subpath=[act_out(cfg, '配置', dkey(ROOT_SUB)), entry_dir])
b.else_(g_has_url)
b.notify(['跳过（清单条目没有下载地址）：', entry_name])
b.endif(g_has_url)

b.end_repeat(g_each_file, each=True)

b.notify(['同步完成：', var('清单文件数'), ' 个文件已存入「下载」'])
g_has_empty = b.if_(var('空目录数'), GT, number=0)
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
# 没拿到合并清单地址，分两种：服务端明确指出合并不了（多台电脑各登记了
# 目录），或者干脆没给 —— 后者基本就是电脑端版本旧，还只会给单目录清单。
g_batch_err = b.if_(pick(resp, 'manifest_batch_error'), 100)
b.notify([pick(resp, 'manifest_batch_error')])
b.else_(g_batch_err)
b.notify(['没有目录内容可同步。若刚复制的是目录，多半是电脑端版本过旧'
          '（需要 1.12.5 及以上，它会给出目录合并清单）'])
b.endif(g_batch_err)
b.endif(g_has_list)

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
