# -*- coding: utf-8 -*-
"""校验生成的快捷指令 plist：控制流配对、变量引用、动作树。

    python verify_shortcut.py [plist 路径]
"""

import collections
import sys
import plistlib

REF = 'D:/Documents/Github/SyncClipboard/shortcuts/获取同步-unsigned.plist'

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


# 参考文件里没有、但已核实过的参数键。
#
# 参考文件（v1）是用户手工搭的，其中「解压缩」动作本身是我加的、当时没带
# 归档参数 —— 也就是说参考文件在这一点上**本身就是错的**，不能拿它当基准
# 反过来否掉正确写法。`WFArchive` 已由 Apple 动作文档核实：Extract Archive
# 只有一个具名参数 Archive，键名就是 WFArchive。
KNOWN_EXTRA_PARAMS = {
    ('is.workflow.actions.unzip', 'WFArchive'),
    # 通用参数，iOS 自己导出时也常带，无害
    ('is.workflow.actions.repeat.count', 'UUID'),
}


def check_params(acts, ref_path):
    """参数名对照：凡是参考文件里同样动作从未出现过的参数键，都值得怀疑。"""
    import os
    if not os.path.exists(ref_path):
        print('参数对照: 跳过（缺参考文件）')
        return
    known = collections.defaultdict(set)
    for a in plistlib.load(open(ref_path, 'rb'))['WFWorkflowActions']:
        keys, mode = param_keys(a)
        known[(a['WFWorkflowActionIdentifier'], mode)] |= keys

    suspect = []
    for i, a in enumerate(acts):
        keys, mode = param_keys(a)
        base = known.get((a['WFWorkflowActionIdentifier'], mode))
        if not base:                     # 参考文件里没有这种动作/模式
            continue
        extra = {k for k in keys - base
                 if (a['WFWorkflowActionIdentifier'], k) not in KNOWN_EXTRA_PARAMS}
        if extra:
            suspect.append((i, a['WFWorkflowActionIdentifier'], sorted(extra)))
    print('可疑参数（参考文件同动作未出现过）:', suspect or 'none')


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
    check_params(acts, REF)
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
    main(sys.argv[1] if len(sys.argv) > 1
         else 'D:/Documents/Github/SyncClipboard/shortcuts/获取同步v3-unsigned.plist')
