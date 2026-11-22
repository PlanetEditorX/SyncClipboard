# -*- coding: utf-8 -*-
"""校验生成的快捷指令 plist：控制流配对、变量引用、可疑参数、动作树。

跨快捷指令通用，只吃一个 plist 路径：

    python lib/verify.py 获取同步/获取同步-unsigned.plist
"""

import collections
import os
import sys
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))

CN = {
    'is.workflow.actions.dictionary': '词典',
    'is.workflow.actions.gettext': '文本',
    'is.workflow.actions.setvariable': '设定变量',
    'is.workflow.actions.conditional': '如果',
    'is.workflow.actions.repeat.count': '重复',
    'is.workflow.actions.repeat.each': '重复逐个',
    'is.workflow.actions.delay': '等待',
    'is.workflow.actions.downloadurl': '获取URL内容',
    'is.workflow.actions.showresult': '显示结果',
    'is.workflow.actions.notification': '显示通知',
    'is.workflow.actions.exit': '停止此快捷指令',
    'is.workflow.actions.count': '计数',
    'is.workflow.actions.documentpicker.save': '存储到文件',
    'is.workflow.actions.unzip': '解压缩',
    'is.workflow.actions.setclipboard': '拷贝到剪贴板',
    'is.workflow.actions.getdevicedetails': '获取设备详细信息',
    'is.workflow.actions.urlencode': 'URL编码',
    'is.workflow.actions.comment': '注释',
}
COND = {4: '是', 5: '不是', 2: '大于', 3: '小于', 8: '包含', 9: '不包含'}


def param_keys(act):
    p = act.get('WFWorkflowActionParameters') or {}
    return set(p.keys()), p.get('WFControlFlowMode')


# 动作（identifier, WFControlFlowMode）→ 该动作允许出现的参数键。
# 用途：生成的动作里冒出了这里没有的键时提示一声；只作参考，不作正确性判据。
_REF_PARAMS = {
    ('com.sindresorhus.Actions.IsHostReachable', None): {'AppIntentDescriptor', 'UUID', 'host', 'port'},
    ('is.workflow.actions.comment', None): {'WFCommentActionText'},
    ('is.workflow.actions.conditional', 0): {'GroupingIdentifier', 'UUID', 'WFCondition', 'WFConditionalActionString', 'WFConditions', 'WFControlFlowMode', 'WFInput', 'WFNumberValue'},
    ('is.workflow.actions.conditional', 1): {'GroupingIdentifier', 'UUID', 'WFControlFlowMode'},
    ('is.workflow.actions.conditional', 2): {'GroupingIdentifier', 'UUID', 'WFControlFlowMode'},
    ('is.workflow.actions.count', None): {'Input', 'UUID'},
    ('is.workflow.actions.delay', None): {'WFDelayTime'},
    ('is.workflow.actions.dictionary', None): {'CustomOutputName', 'UUID', 'WFItems'},
    ('is.workflow.actions.documentpicker.save', None): {'UUID', 'WFAskWhereToSave', 'WFFolder', 'WFInput'},
    ('is.workflow.actions.downloadurl', None): {'ShowHeaders', 'UUID', 'WFHTTPHeaders', 'WFHTTPMethod', 'WFJSONValues', 'WFURL'},
    ('is.workflow.actions.exit', None): {},
    ('is.workflow.actions.getcurrentapp', None): {'UUID'},
    ('is.workflow.actions.getdevicedetails', None): {'UUID'},
    ('is.workflow.actions.getitemtype', None): {'UUID', 'WFInput'},
    ('is.workflow.actions.gettext', None): {'UUID', 'WFTextActionText'},
    ('is.workflow.actions.notification', None): {'UUID', 'WFInput', 'WFNotificationActionBody', 'WFNotificationActionSound'},
    ('is.workflow.actions.previewdocument', None): {'WFInput'},
    ('is.workflow.actions.repeat.count', 0): {'GroupingIdentifier', 'WFControlFlowMode', 'WFRepeatCount'},
    ('is.workflow.actions.repeat.count', 2): {'GroupingIdentifier', 'UUID', 'WFControlFlowMode'},
    ('is.workflow.actions.repeat.each', 0): {'GroupingIdentifier', 'UUID', 'WFControlFlowMode', 'WFInput'},
    ('is.workflow.actions.repeat.each', 2): {'GroupingIdentifier', 'UUID', 'WFControlFlowMode'},
    ('is.workflow.actions.setclipboard', None): {'UUID', 'WFInput'},
    ('is.workflow.actions.setvariable', None): {'WFInput', 'WFVariableName'},
    ('is.workflow.actions.showresult', None): {'Text'},
    ('is.workflow.actions.unzip', None): {'UUID'},
    ('is.workflow.actions.urlencode', None): {'UUID', 'WFInput'},
}

# 参考文件里没有、但已核实过的参数键。
#
# 参考文件（v1）是用户手工搭的，其中「解压缩」动作本身是我加的、当时没带
# 归档参数 —— 也就是说参考文件在这一点上**本身就是错的**，不能拿它当基准
# 反过来否掉正确写法。`WFArchive` 已由 Apple 动作文档核实：Extract Archive
# 只有一个具名参数 Archive，键名就是 WFArchive。
KNOWN_EXTRA_PARAMS = {
    ('is.workflow.actions.unzip', 'WFArchive'),
    # 「存储到文件」的子路径与覆盖开关。参考文件（v1）里这两组都没用到，
    # 但键名有两个来源可核：ScPL 的动作文档给出 `WFFileDestinationPath` /
    # `WFSaveFileOverwrite`，早期逆向资料给的是 `WFDestinationPath` /
    # `WFOverwriteFile`。未知键 iOS 会忽略，所以两个键名都写 —— 少写的那一
    # 个才会真的丢功能（丢子路径 = 层级又不保）。
    ('is.workflow.actions.documentpicker.save', 'WFFileDestinationPath'),
    ('is.workflow.actions.documentpicker.save', 'WFDestinationPath'),
    ('is.workflow.actions.documentpicker.save', 'WFSaveFileOverwrite'),
    ('is.workflow.actions.documentpicker.save', 'WFOverwriteFile'),
    # 通用参数，iOS 自己导出时也常带，无害
    ('is.workflow.actions.repeat.count', 'UUID'),
}


def check_params(acts):
    """参数名对照：参考集里没有的参数键提示出来，多半是键名写错或拼错。

    参考集是 `_REF_PARAMS`，取自一份可正常导入的导出。只作提示，不作正确性
    判据 —— iOS 容忍多余键，真正致命的是必填键缺失。
    """
    suspect = []
    for i, a in enumerate(acts):
        keys, mode = param_keys(a)
        base = _REF_PARAMS.get((a['WFWorkflowActionIdentifier'], mode))
        if not base:                     # 参考集里没有这种动作/模式
            continue
        extra = {k for k in keys - base
                 if (a['WFWorkflowActionIdentifier'], k) not in KNOWN_EXTRA_PARAMS}
        if extra:
            suspect.append((i, a['WFWorkflowActionIdentifier'], sorted(extra)))
    print('可疑参数（参考集同动作未出现过）:', suspect or 'none')


def walk_refs(node, sink):
    if isinstance(node, dict):
        if node.get('Type') in ('ActionOutput', 'Variable'):
            sink.append(node)
        for v in node.values():
            walk_refs(v, sink)
    elif isinstance(node, list):
        for v in node:
            walk_refs(v, sink)


def main(path):
    doc = plistlib.load(open(path, 'rb'))
    acts = doc['WFWorkflowActions']

    uuids = {}
    for i, a in enumerate(acts):
        u = (a.get('WFWorkflowActionParameters') or {}).get('UUID')
        if u:
            uuids[u] = i

    # --- 引用完整性 ---
    broken = []
    for i, a in enumerate(acts):
        refs = []
        walk_refs(a, refs)
        for r in refs:
            u = r.get('OutputUUID')
            if u and u not in uuids:
                broken.append((i, r.get('OutputName'), u))

    # --- 控制流配对 ---
    groups = collections.defaultdict(list)
    for i, a in enumerate(acts):
        p = a.get('WFWorkflowActionParameters') or {}
        if 'WFControlFlowMode' in p:
            groups[p.get('GroupingIdentifier')].append((i, p['WFControlFlowMode']))

    bad_groups = []
    for gid, seq in groups.items():
        modes = [m for _, m in seq]
        ok = modes[0] == 0 and modes[-1] == 2 and modes.count(0) == 1 \
            and modes.count(1) <= 1 and all(m in (0, 1, 2) for m in modes)
        if not ok:
            bad_groups.append((gid, seq))

    # --- 变量定义/使用 ---
    defined = set()
    used = set()
    for a in acts:
        p = a.get('WFWorkflowActionParameters') or {}
        if p.get('WFVariableName'):
            defined.add(p['WFVariableName'])
        refs = []
        walk_refs(a, refs)
        for r in refs:
            if r.get('Type') == 'Variable' and r.get('VariableName'):
                used.add(r['VariableName'])

    magic = {'Repeat Item', 'Repeat Index', 'Shortcut Input', 'Clipboard'}
    undefined = sorted(used - defined - magic)

    print('动作数:', len(acts))
    print('断裂引用:', broken or 'none')
    print('控制流异常:', bad_groups or 'none')
    print('定义变量:', sorted(defined))
    print('使用但未定义:', undefined or 'none')
    check_params(acts)
    print()
    print('================ 动作树 ================')
    indent = 0
    for i, a in enumerate(acts):
        p = a.get('WFWorkflowActionParameters') or {}
        ident = a['WFWorkflowActionIdentifier']
        name = CN.get(ident, ident)
        mode = p.get('WFControlFlowMode')
        if mode == 2:
            indent = max(0, indent - 1)
            print('  ' * indent + f'[{i:3}] ── 结束{name}')
            continue
        if mode == 1:
            print('  ' * max(0, indent - 1) + f'[{i:3}] ── 否则')
            continue

        extra = ''
        if ident == 'is.workflow.actions.conditional':
            if 'WFConditions' in p:
                tpl = p['WFConditions']['Value']['WFActionParameterFilterTemplates']
                subj = tpl[0]['WFInput']['Variable']['Value'].get('VariableName') \
                    or tpl[0]['WFInput']['Variable']['Value'].get('OutputName')
                extra = ' ' + subj + ' 是 ' + ' 或 '.join(
                    repr(t['WFConditionalActionString']) for t in tpl)
            else:
                subj = p['WFInput']['Variable']['Value']
                s = subj.get('VariableName') or subj.get('OutputName')
                extra = f' {s} {COND.get(p.get("WFCondition"), p.get("WFCondition"))}'
                if 'WFConditionalActionString' in p:
                    extra += ' ' + repr(p['WFConditionalActionString'])
                if 'WFNumberValue' in p:
                    extra += ' ' + p['WFNumberValue']
        if ident == 'is.workflow.actions.repeat.count':
            extra = f' {int(p.get("WFRepeatCount", 0))} 次'
        if ident == 'is.workflow.actions.setvariable':
            extra = ' → ' + p['WFVariableName']
        if ident == 'is.workflow.actions.comment':
            extra = ' ' + p['WFCommentActionText'].replace('\n', ' / ')[:60]
        print('  ' * indent + f'[{i:3}] {name}{extra}')
        if mode == 0:
            indent += 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('用法: python lib/verify.py <unsigned.plist>')
    main(sys.argv[1])
