# -*- coding: utf-8 -*-
"""快捷指令的通用构建层：动作工厂 + token 序列化助手。

与具体流程无关，各快捷指令共用：

    SC                 往工作流里追加动作（文本 / 词典 / 如果 / 循环 / 通知 / 存盘…）
    tok, att            文本 token 与附件引用
    act_out, pick, var  动作输出、逐层取词典值、变量引用
    dkey, as_text       取值链：按词典键取值、强制成字符串
    cond_input          「如果」动作的被判断对象（按比较类型决定是否转字符串）
    FOLDER_DOWNLOAD     「存储到文件」的默认目标目录描述
    DEFAULT_META        工作流顶层元数据
    条件码               IS/IS_NOT/GT/LT/LTE/GTE/ANY/NO_VALUE，见下方常量

拉取型（获取同步）用 post_json + repeat_each；轮询驻留型（文字同步）还会用到
get_clipboard / item_type / stored_get / stored_set / reachable / repeat_count /
calculate / delay / run_self；事件型（短信同步）用 shortcut_input / match_text /
match_group 从正文里抽验证码；推送型（推送同步）用 put_file 把文件直传到目标
设备，配合 choose_from_list 选目标、alert 提示「没有客户端在线」，再用
count(what='Characters') 数真实字数、file_extension 补回被丢掉的文件后缀。

具体流程写在各子目录的 `build.py` 里（如 `获取同步/build.py`）。
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
    """把一个引用包成 WFTextTokenAttachment。

    只接受 dict（`var(...)` / `act_out(...)` 的返回值）。传 UUID 字符串是常见
    笔误：`b.text(...)` 返回的是 UUID 字符串而不是引用，包进去就成了
    `{"Value": "7A3F…", "WFSerializationType": "WFTextTokenAttachment"}`，
    iOS 解析不出该动作的输出，变量/文件会拿到空值。这里直接抛错在生成阶段拦掉。
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

    「有没有值」同样要强转，但原因不同：iOS 的内容项类型决定了条件菜单里能选
    什么 —— **App 类型的内容项没有「没有值」这一项**，选中就报参数缺失。所以
    「获取当前App」这类输出在判空之前必须先转成文本（原版也是这么写的）。
    """
    if coerce:
        existing = subj.get('Aggrandizements') or [] if isinstance(subj, dict) else []
        already = any(a.get('CoercionItemClass') == 'WFStringContentItem'
                      for a in existing)
        if not already:
            subj = act_out(subj, agg=as_text())
    return {'Type': 'Variable', 'Variable': att(subj)}


def dict_field(pairs):
    """拼词典字面量。值可以是单个 part，也可以是 parts 列表（含变量时用）。"""
    items = []
    for k, v in pairs:
        items.append({
            'WFItemType': 0,
            'WFKey': tok([k]),
            'WFValue': tok(v if isinstance(v, list) else [v]),
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

# 条件码。前四个与 NSPredicate 的比较算子一一对应（真机验证过的只有 2 与 4/5）：
#   0 <   1 ≤   2 >   3 ≥   4 是   5 不是   100 有任何值   101 没有任何值
# 「有没有内容」一律用 100/101，不要用数字比较 —— 字段缺省时取到空值，
# 空值去比大小会让 iOS 弹「请为此操作中的每个参数选取值」并中断整条指令。
IS, IS_NOT, GT = 4, 5, 2
LT, LTE, GTE = 0, 1, 3
ANY, NO_VALUE = 100, 101
ANY_OF, ALL_OF = 0, 1      # WFActionParameterFilterPrefix：0 = 任一，1 = 全部


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
        # 同上：UUID 字符串不是引用，传进来一定是笔误，当场拦掉
        if isinstance(subject, str):
            raise AssertionError(
                'setvar(%r) 收到 UUID 字符串，要传 act_out(uuid, 名字)' % name)
        self._add('is.workflow.actions.setvariable',
                  {'WFInput': att(subject), 'WFVariableName': name})

    def count(self, subject, what=None):
        """计数。what 省略 = iOS 缺省的 `Items`（项目数）。

        坑在这儿：一段文字是 **1 个项目**。通知里写「（N 字）」却拿项目数来填，
        真机上永远显示「1 字」。要字数必须显式写 `WFCountType = 'Characters'`
        （其余可选 Words / Sentences / Lines，见官方 Count 动作定义）。
        """
        uid = U()
        p = {'UUID': uid, 'Input': att(subject)}
        if what:
            p['WFCountType'] = what
        self._add('is.workflow.actions.count', p)
        return uid

    def file_extension(self, subject):
        """「获取文件的详细信息 → 文件扩展名」，输出不带点的后缀（如 `png`）。

        文件被强制转文本时 iOS 取的是 Name 属性，**不含扩展名** —— 所以
        `urlencode(文件)` 拼出来的 filename 天生缺后缀，真机上 `a.png` 会存成
        `a`。这里显式取 File Extension，由调用方拼回 `名称.扩展名` 再整体编码。
        动作是 `is.workflow.actions.properties.files`，属性名就是显示名字符串。
        """
        uid = U()
        self._add('is.workflow.actions.properties.files', {
            'UUID': uid,
            'WFInput': att(subject),
            'WFContentItemPropertyName': 'File Extension',
        })
        return act_out(uid, '文件的详细信息')

    def urlencode(self, subject):
        uid = U()
        self._add('is.workflow.actions.urlencode',
                  {'UUID': uid, 'WFInput': tok([subject]),
                   # 显式写 Encode（动作还有 Decode 模式）。缺省虽也是 Encode，
                   # 但中文文件名经不起「万一被当 Decode」的猜测。
                   'WFEncodeMode': 'Encode'})
        return uid

    def device_name(self):
        uid = U()
        self._add('is.workflow.actions.getdevicedetails', {'UUID': uid})
        return act_out(uid, '设备名称')

    def shortcut_input(self):
        """快捷指令输入（自动化把短信正文当输入传进来的那份）。

        引用形状是 `{"Type": "ExtensionInput"}`，不是 ActionOutput；用它之前
        顶层 meta 必须把 `WFWorkflowHasShortcutInputVariables` 置 True，否则
        导入后这个变量取不到值。想当文本用就 `b.text([b.shortcut_input()])`
        再取输出 —— 直接参与比较也行，cond_input 会补强转。
        """
        return {'Type': 'ExtensionInput'}

    # -- 文本匹配 ---------------------------------------------------------
    def match_text(self, subject, pattern):
        """匹配文本（正则）。返回「匹配」列表，若没有命中则是一个空列表。

        pattern 传字符串；真机上的正则引擎是 ICU，lookbehind / lookahead /
        定长量词都支持，但不认 PCRE 的 `\\K`、也默认不区分大小写，所以英文
        关键词得自己写 `[Cc]ode` 这种。
        """
        uid = U()
        self._add('is.workflow.actions.text.match', {
            'UUID': uid, 'ShowWhenRun': False,
            'WFMatchTextPattern': pattern,
            'text': tok([subject]),
        })
        return act_out(uid, '匹配')

    def match_group(self, matches, index=None):
        """获取匹配中的组。

        index 省略时不写 `WFMatchGroupIndex` —— iOS 导出时同样不写这个键，
        而 App 里显示的是「组 1」，说明**默认就是组 1**。模式里只留一个捕获
        组、并让它是验证码本身，就不必碰这个参数（它的类型在公开资料里对不上
        账，写成字符串有可能被判成空参数，整条指令标红）。
        """
        uid = U()
        p = {'UUID': uid, 'ShowWhenRun': False, 'matches': att(matches)}
        if index is not None:
            p['WFMatchGroupIndex'] = index
        self._add('is.workflow.actions.text.match.getgroup', p)
        return act_out(uid, '文本')

    # -- 网络 ------------------------------------------------------------
    def get(self, url_subject, headers=None):
        """获取 URL 内容（GET）。

        url_subject 传单个引用时按原样用；传 parts 列表（含字面量与变量）时整串
        拼进 WFURL —— 拼查询串（`{地址}?source={设备名}`）时用这个，省掉一个
        额外的「文本」动作。headers 是 [(键, 值)] 列表，值可以是引用。
        """
        uid = U()
        parts = url_subject if isinstance(url_subject, list) else [url_subject]
        p = {'UUID': uid, 'ShowHeaders': True, 'WFURL': tok(parts)}
        if headers:
            p['WFHTTPHeaders'] = dict_field(headers)
        self._add('is.workflow.actions.downloadurl', p)
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

    def put_file(self, url_subject, body_subject, headers=None):
        """把一份文件二进制原样 PUT 上去（不带表单、不带 JSON）。

        两个参数键是 iOS 自己导出的形状，缺一个都不行：
        `WFHTTPBodyType = 'File'` 才是「请求体 = 文件」（缺省是 JSON），
        `WFRequestVariable` 才是「这份文件」—— 写成 `WFInput` 或塞进
        `WFJSONValues` 都会被当成普通字段，服务端收到的是空 body。
        服务端的文件名从查询串里取，所以 url_subject 要把 `?filename=` 拼进去。
        """
        uid = U()
        parts = url_subject if isinstance(url_subject, list) else [url_subject]
        p = {'UUID': uid,
             'ShowHeaders': True,
             'WFHTTPMethod': 'PUT',
             'WFHTTPBodyType': 'File',
             'WFURL': tok(parts),
             'WFRequestVariable': att(body_subject)}
        if headers:
            p['WFHTTPHeaders'] = dict_field(headers)
        self._add('is.workflow.actions.downloadurl', p)
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

    def choose_from_list(self, subject, prompt='请选择'):
        """从列表中选取：弹一个选择器，返回「所选项目」。

        单选（不写 `WFChooseFromListActionSelectMultiple`）就是 iOS 的默认行为，
        跟原版手工包一致；要改成多选得把该键置 True，那时输出变成列表，后面的
        取值链也跟着变，别只改这一处。
        """
        uid = U()
        self._add('is.workflow.actions.choosefromlist', {
            'UUID': uid,
            'WFInput': att(subject),
            'WFChooseFromListActionPrompt': prompt,
        })
        return act_out(uid, '所选项目')

    def alert(self, message, title=None):
        """弹一个模态提示，必须点「好」才继续。

        跟「显示通知」不是一回事：通知会自己消失、还可能被专注模式吞掉，
        用户主动点了一下快捷指令却什么都没发生时，得有一个非看不可的反馈。

        message 传字符串就原样用（原版手工包导出的就是这个形状），传 parts
        列表则拼成文本 token —— 提示里要带变量（状态码、错误信息）时用后者。
        除消息外不写别的键：原版导出后也只有 `WFAlertActionMessage` 一个。
        """
        p = {'UUID': U(),
             'WFAlertActionMessage': message if isinstance(message, str)
             else tok(message)}
        if title is not None:
            p['WFAlertActionTitle'] = title
        self._add('is.workflow.actions.alert', p)

    def get_clipboard(self):
        """读剪贴板（不是「拷贝到剪贴板」）。"""
        uid = U()
        self._add('is.workflow.actions.getclipboard', {'UUID': uid})
        return act_out(uid, '剪贴板')

    def item_type(self, subject):
        """获取项目类型：返回本地化名字（文本 / URL / 多信息文本 / 图像…）。"""
        uid = U()
        self._add('is.workflow.actions.getitemtype',
                  {'UUID': uid, 'WFInput': att(subject)})
        return act_out(uid, '类型')

    def detect_dict(self, subject):
        """转换为词典：响应体是 JSON 文本时解析成词典，已是词典则原样透传。"""
        uid = U()
        self._add('is.workflow.actions.detect.dictionary',
                  {'UUID': uid, 'WFInput': att(subject)})
        return act_out(uid, '词典')

    def get_value(self, subject, key):
        """按词典键取值（动作版）。"""
        uid = U()
        self._add('is.workflow.actions.getvalueforkey',
                  {'UUID': uid, 'WFInput': att(subject),
                   'WFDictionaryKey': key})
        return act_out(uid, key)

    def stored_get(self, key):
        """读设备本地「存储内容」（快捷指令的 key-value，跨运行保留）。"""
        uid = U()
        self._add('is.workflow.actions.getstoredcontent',
                  {'UUID': uid, 'WFStoredContentKey': key})
        return act_out(uid, key)

    def stored_set(self, subject, key):
        """写「存储内容」。值包成文本 token，跟 iOS 自己导出的形状一致。"""
        self._add('is.workflow.actions.setstoredcontent',
                  {'WFInput': tok([subject]), 'WFStoredContentKey': key})

    def date_now(self):
        uid = U()
        self._add('is.workflow.actions.date', {'UUID': uid})
        return act_out(uid, '日期')

    def battery_level(self):
        uid = U()
        self._add('is.workflow.actions.getbatterylevel', {'UUID': uid})
        return act_out(uid, '电池电量')

    def wifi_info(self):
        """当前 Wi-Fi 的详细信息；没连 Wi-Fi 时输出为空值。"""
        uid = U()
        self._add('is.workflow.actions.getwifi', {'UUID': uid})
        return act_out(uid, '网络的详细信息')

    def current_app(self, text=False):
        """当前前台 App。

        text=True 时在输出上挂 WFStringContentItem 强转。判「有没有值」必须用
        它：App 类型的内容项在 iOS 的条件菜单里没有「没有值」，选中就标红。
        做「是 某个 App 名」比较时不用管，比较那边会自己加强转。
        """
        uid = U()
        self._add('is.workflow.actions.getcurrentapp', {'UUID': uid})
        return act_out(uid, '当前App', agg=as_text() if text else None)

    def reachable(self, url_subject, timeout=3):
        """主机可达性检查（Actions 的 App Intent），返回「是 / 否」。

        不用「获取 URL 内容」探活：那条动作在连不上时直接抛错并中断整条指令，
        轮询里一旦断网就没有第二次机会。

        参数形状照 Actions 自己导出的样子：`host` 是**文本 token**（不是
        attachment），`timeout` 是**字符串**。它是 App Intent 的参数，类型不对
        时 App 里那句参数会显示成空白、跑起来恒判不可达。
        """
        uid = U()
        self._add('com.sindresorhus.Actions.IsReachableIntent', {
            'UUID': uid,
            'AppIntentDescriptor': {
                'AppIntentIdentifier': 'IsReachableIntent',
                'BundleIdentifier': 'com.sindresorhus.Actions',
                'Name': 'Actions',
                'TeamIdentifier': 'YG56YK5RN5',
            },
            'host': tok([url_subject]),
            'requireSuccessStatusCode': True,
            'timeout': str(timeout),
        })
        return act_out(uid, 'Is Web Server Reachable')

    def calculate(self, parts):
        """计算表达式，parts 是文本 parts 列表（可用变量），返回动作输出。"""
        uid = U()
        self._add('is.workflow.actions.calculateexpression',
                  {'UUID': uid, 'Input': tok(parts)})
        return act_out(uid, '计算结果')

    def delay(self, seconds):
        p = {'WFDelayTime': att(seconds) if isinstance(seconds, dict) else seconds}
        self._add('is.workflow.actions.delay', p)

    def run_self(self):
        """调用自己（续跑）。isSelf 不写名字/UUID：导入后自动指向当前快捷指令。"""
        self._add('is.workflow.actions.runworkflow', {
            'UUID': U(),
            'WFWorkflow': {'isSelf': True},
        })

    def exit(self):
        self._add('is.workflow.actions.exit', {})

    # -- 文件 ------------------------------------------------------------
    def save_to_folder(self, subject, dest=None):
        """存储到文件。

        dest 是目标路径的 parts 列表，**含文件名**，例如
        `[根子路径, 目录名, '/', 子目录, '/', 文件名]`；省略则按 iOS 默认
        命名直接存进目标文件夹。

        `WFFileDestinationPath`（界面上的 Subpath）是**完整路径**而非目录
        前缀：分隔靠字面 "/"，整串没有斜杠时会被整段当成文件名。iOS 还会先
        规范化路径，变量值结尾的 "/" 不保证保留，所以斜杠一律用字面文本给出，
        不依赖变量内容的末尾字符。

        覆盖开关（`WFSaveFileOverwrite` / `WFOverwriteFile`）打开，同名文件
        直接替换，否则 iOS 会存成「a 1.txt」。`WFDestinationPath` 是同一参数
        的旧键名，一并写入，多余的键会被忽略。
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
        """单条件「如果」。

        string 可以是普通字符串，也可以是 `tok([...])` 造出来的引用串（比较对象
        本身是个变量时就这么用）；number 同理，可以是数字字符串，也可以是
        `act_out(...)`/`var(...)` 引用（目标值取自配置词典时）。

        「有任何值 / 没有任何值」(100/101) 例外：这两种条件 iOS 只认表模板
        （WFConditions → WFActionParameterFilterTemplates）形态，单条件形态
        导入后动作会标红「请为此操作中的每个参数选取值」。原版里这两条也是
        表模板。
        """
        if cond in (ANY, NO_VALUE) and string is None and number is None:
            return self.if_conds([{'subject': subject, 'cond': cond}])
        g = U()
        p = {'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 0,
             'WFCondition': cond,
             'WFInput': cond_input(subject, coerce=string is not None)}
        if string is not None:
            p['WFConditionalActionString'] = string
        if number is not None:
            p['WFNumberValue'] = att(number) if isinstance(number, dict) else str(number)
        self._add('is.workflow.actions.conditional', p)
        return g

    def _tpl(self, c):
        """把 {subject, cond, string, number, date} 拼成一个条件模板。"""
        # 只有文本比较要强制转字符串：数值/日期比较带上强制转换会变成字符串比大小
        tpl = {'WFCondition': c['cond'],
               'WFInput': cond_input(c['subject'], coerce='string' in c)}
        if 'string' in c:
            tpl['WFConditionalActionString'] = c['string']
        if 'number' in c:
            v = c['number']
            tpl['WFNumberValue'] = att(v) if isinstance(v, dict) else str(v)
        if 'date' in c:
            tpl['WFDate'] = c['date']
        return tpl

    def if_conds(self, conds, mode=ANY_OF):
        """多条件「如果」：conds 是 [{subject, cond, string|number|date}, …]。

        mode=ANY_OF（0）= 任一满足，ALL_OF（1）= 全部满足。多个条件可以是不同
        的判断对象 —— 这正是它跟 if_any 的区别（后者只支持「同一个东西是 A 或
        B」）。守卫那种「电量低 或 没 Wi-Fi 或 服务不可达」就用它。
        """
        g = U()
        self._add('is.workflow.actions.conditional', {
            'GroupingIdentifier': g,
            'UUID': U(),
            'WFControlFlowMode': 0,
            'WFConditions': {
                'Value': {
                    'WFActionParameterFilterPrefix': mode,
                    'WFActionParameterFilterTemplates':
                        [self._tpl(c) for c in conds],
                    'WFContentPredicateBoundedDate': False,
                },
                'WFSerializationType': 'WFContentPredicateTableTemplate',
            },
        })
        return g

    def if_any(self, subject, strings):
        """「如果 <同一个东西> 是 A 或 B」。subject 需已是 var()/act_out() 描述。"""
        return self.if_conds([{'subject': subject, 'cond': IS, 'string': s}
                              for s in strings], ANY_OF)

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

    def repeat_count(self, count):
        """重复 N 次。count 可以是数字字符串，也可以是 var()/act_out() 引用。"""
        g = U()
        self._add('is.workflow.actions.repeat.count', {
            'GroupingIdentifier': g, 'UUID': U(), 'WFControlFlowMode': 0,
            'WFRepeatCount': att(count) if isinstance(count, dict) else str(count),
        })
        return g

    def end_repeat(self, g, each=False):
        ident = ('is.workflow.actions.repeat.each' if each
                 else 'is.workflow.actions.repeat.count')
        self._add(ident, {'GroupingIdentifier': g, 'UUID': U(),
                          'WFControlFlowMode': 2})


# ------------------------------------------------------------------ 图标

# 快捷指令「库」里的图标 = 调色板颜色 + 内置图形编号，两项写在 WFWorkflowIcon
# 里，随 .shortcut 文件一起导入。之前四份脚本都沿用下面这组占位值
# （255 深灰 + 59711 剪贴板），所以导入后四条长得一模一样，像默认图标。
#
# 下面四个 glyph 编号来自真机导出样本（在 iOS 里手动选好图标后导出的
# .shortcut 解包所得），不是猜的：
#   文字同步 59403 双气泡 / 获取同步 59711 剪贴板
#   短信同步 59773 信封   / 推送同步 59708 向上箭头（分享）
# 颜色沿用样本里的 255（深灰）。
#
# 注意：从相册选的照片图标属于「添加到主屏幕」的书签，不属于 .shortcut；
# WFWorkflowIconImageData 即便写进去，跨设备导入也不保证保留。库内图标
# 只能用「调色板颜色 + 内置图形」，照片图标只能导入后在手机上手动选。

ICON_COLORS = {
    '红': 4282601983, '深橙': 4251333119, '橙': 4271458815,
    '黄': 4274264319, '绿': 4292093695, '青': 431817727,
    '浅蓝': 1440408063, '蓝': 463140863, '深蓝': 946986751,
    '紫罗兰': 2071128575, '紫': 3679049983, '粉': 3980825855,
    '深灰': 255, '灰绿': 3031607807, '褐': 2846468607,
}

ICON_GLYPHS = {
    'clipboard': 59711, 'messageBubbles': 59403, 'textBubble': 59779,
    'sendMessage': 61467, 'envelope': 59773, 'picture': 59784,
    'document': 59725, 'documentOutline': 59727, 'squareStack': 61457,
    'uploadArrow': 59708, 'downloadArrow': 59693, 'paperAirplane': 59836,
    'cloud': 59714, 'syncArrows': 59846, 'shortcuts': 61440,
}


def set_icon(meta, glyph, color='深灰'):
    """设置库内图标。glyph / color 可传上面表里的名字，也可直接传整数。"""
    g = ICON_GLYPHS.get(glyph, glyph) if isinstance(glyph, str) else glyph
    c = ICON_COLORS.get(color, color) if isinstance(color, str) else color
    meta['WFWorkflowIcon'] = {
        'WFWorkflowIconGlyphNumber': g,
        'WFWorkflowIconStartColor': c,
    }
    return meta


DEFAULT_META = {
    'WFQuickActionSurfaces': [],
    'WFWorkflowClientVersion': '3612.0.1.3',
    'WFWorkflowHasOutputFallback': False,
    'WFWorkflowHasShortcutInputVariables': False,
    # 兜底默认值；各 build.py 必须调用 set_icon() 覆盖成自己的图标。
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
