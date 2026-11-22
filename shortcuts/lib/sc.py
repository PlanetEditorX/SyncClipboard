# -*- coding: utf-8 -*-
"""快捷指令的通用构建层：动作工厂 + 序列化助手。

这里只放与「具体哪个快捷指令」无关的东西：
  · `SC`：往工作流里追加动作的薄封装（文本/词典/如果/循环/通知/存盘…）
  · `tok` / `att` / `act_out` / `pick` / `var` / `dkey`：把动作输出、变量、
    词典取值拼成 iOS 认的 token 结构
  · `DEFAULT_META`：工作流顶层元数据，所有快捷指令共用一份

具体的流程写在各自子目录的 `build.py` 里（如 `获取同步/build.py`）。
分层的理由：以后还要有文字同步、短信同步、推送同步，这些动作工厂每个都要用，
留在某个具体快捷指令的脚本里就只能靠 import 兄弟文件，越往后越别扭。
"""

import json
import os
import plistlib
import uuid

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
    """把一个附件引用包成 WFTextTokenAttachment。

    只接受 **dict**（`var(...)` / `act_out(...)` 的返回值）。传 UUID 字符串是
    常见笔误：`b.text(...)` 返回的是 UUID 字符串，不是引用，包进去就成了
    `{"Value": "7A3F…", "WFSerializationType": "WFTextTokenAttachment"}` ——
    iOS 解析不出这个动作的输出，变量直接被赋成空值。v9 真机就栽在这上面：
    「设置变量 日志」拿到的输入是坏的，手机每次 POST /ios_log 都是
    `{"log":""}`，电脑上一个字节都收不到。这里直接拦下来，编译期就报错。
    """
    if isinstance(x, str):
        raise AssertionError(
            'att() 收到的是 UUID 字符串（%s…），要传 act_out(uuid, 名字)'
            % x[:8])
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
        # 同上：uuid 字符串不是引用，传进来一定是个笔误，当场拦掉
        if isinstance(subject, str):
            raise AssertionError(
                'setvar(%r) 收到 UUID 字符串，要传 act_out(uuid, 名字)' % name)
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


DEFAULT_META = {
    'WFQuickActionSurfaces': [],
    'WFWorkflowClientVersion': '3612.0.1.3',
    'WFWorkflowHasOutputFallback': False,
    'WFWorkflowHasShortcutInputVariables': False,
    'WFWorkflowIcon': {'WFWorkflowIconGlyphNumber': 59711,
                       'WFWorkflowIconStartColor': 255},
    'WFWorkflowImportQuestions': [],
    'WFWorkflowInputContentItemClasses': [
        'WFAppContentItem',
        'WFAppStoreAppContentItem',
        'WFArticleContentItem',
        'WFContactContentItem',
        'WFDateContentItem',
        'WFEmailAddressContentItem',
        'WFFolderContentItem',
        'WFGenericFileContentItem',
        'WFImageContentItem',
        'WFiTunesProductContentItem',
        'WFLocationContentItem',
        'WFDCMapsLinkContentItem',
        'WFAVAssetContentItem',
        'WFPDFContentItem',
        'WFPhoneNumberContentItem',
        'WFRichTextContentItem',
        'WFSafariWebPageContentItem',
        'WFStringContentItem',
        'WFURLContentItem',
    ],
    'WFWorkflowMinimumClientVersion': 3010,
    'WFWorkflowMinimumClientVersionString': '3010',
    'WFWorkflowOutputContentItemClasses': [],
    'WFWorkflowTypes': ['WFWorkflowTypeShowInSearch'],
}
