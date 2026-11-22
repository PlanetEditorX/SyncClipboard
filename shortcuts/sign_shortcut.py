# -*- coding: utf-8 -*-
"""给生成的快捷指令签名。

iOS 15+ 不再提供「允许不受信任的快捷指令」开关，未签名的 .shortcut 无法导入；
Windows 上也没有本地签名手段（需 macOS 钥匙串里的 Apple ID 私钥）。
这里走 RoutineHub 社区的 HubSign 服务：把工作流 XML POST 过去，拿回 AEA1 文件。

    注意：工作流全文（含配置词典里的内网地址与密钥）会发送到
    hubs.routinehub.services 这个第三方。

    python sign_shortcut.py
"""

import json
import os
import plistlib
import struct
import sys
from urllib.request import Request, urlopen

import liblzfse

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '获取同步v3-unsigned.plist')
DST = os.path.join(HERE, '获取同步v3.shortcut')
NAME = '获取同步v3'

HUBSIGN_URL = 'https://hubsign.routinehub.services/sign'
HEADERS = {
    'Content-Type': 'application/json',
    'User-Agent': 'cherri/1.0',
    'Origin': 'https://routinehub.co',
    'Referer': 'https://routinehub.co/',
}


def main():
    doc = plistlib.load(open(SRC, 'rb'))
    xml = plistlib.dumps(doc, fmt=plistlib.FMT_XML).decode()
    payload = json.dumps({'shortcutName': NAME, 'shortcut': xml}).encode()

    req = Request(HUBSIGN_URL, data=payload, headers=HEADERS, method='POST')
    with urlopen(req, timeout=180) as resp:
        data = resp.read()
        print('HTTP', resp.status, 'bytes', len(data), 'magic', data[:4])

    if data[:4] != b'AEA1':
        print('FAIL: 返回不是 AEA1')
        print(data[:300])
        return 1

    open(DST, 'wb').write(data)
    print('saved', DST, os.path.getsize(DST), 'B')

    # --- 解包回验：签名后的工作流必须与源文件逐动作一致 ---
    i = data.find(b'bvx2')
    _, n_payload = struct.unpack('<II', data[i + 4:i + 12])
    out = liblzfse.decompress(data[i:i + 12 + n_payload])
    j = out.find(b'bplist00')
    signed = plistlib.loads(out[j:])['WFWorkflowActions']
    orig = doc['WFWorkflowActions']

    ids_a = [a.get('WFWorkflowActionIdentifier') for a in orig]
    ids_b = [a.get('WFWorkflowActionIdentifier') for a in signed]
    print('动作数一致:', len(orig) == len(signed), len(signed))
    print('动作序列一致:', ids_a == ids_b)
    print('AEA1 容器 + bvx2 段正常:', data[:4] == b'AEA1' and i > 0)
    if ids_a != ids_b:
        for k, (x, y) in enumerate(zip(ids_a, ids_b)):
            if x != y:
                print('  首个差异', k, x, y)
                break
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
