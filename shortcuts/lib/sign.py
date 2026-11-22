# -*- coding: utf-8 -*-
"""给生成的快捷指令签名。

iOS 15+ 需要签名才能导入，本机没有 Apple 私钥无法本地签，因此把工作流 XML
POST 给 RoutineHub 的 HubSign 服务，取回 AEA1 容器文件。

    python lib/sign.py <unsigned.plist> [签名输出路径]

    输入            unsigned.plist；输出默认同目录同名 .shortcut
    shortcutName    文件名去掉「-unsigned.plist」，也是 iOS 里显示的名字
    shortcut        工作流 XML 全文 —— 含配置词典里的内网地址与密钥，
                    会发送给 hubs.routinehub.services（第三方）
    请求            强制绕开 HTTP(S)_PROXY 直连；走代理时 Cloudflare 回 403
    回验            解 bvx2 段逐动作比对；缺 liblzfse 时只校 AEA1 头
"""

import json
import os
import plistlib
import struct
import sys
from urllib.request import ProxyHandler, Request, build_opener

try:
    import liblzfse               # 只用于签名后的回验（bvx2 段解压）
except ImportError:               # 退而求其次：PyPI 上的 lzfse 提供同名函数
    try:
        import lzfse as liblzfse
    except ImportError:           # 都没装也能签，只是少了逐动作比对
        liblzfse = None

HERE = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) < 2:
    sys.exit('用法: python lib/sign.py <unsigned.plist> [签名输出路径]')

SRC = os.path.abspath(sys.argv[1])
# 快捷指令名 = plist 文件名去掉「-unsigned.plist」。iOS 里按这个名字显示，
# 也按它判断「同名已在库里」—— 换名导入才能更新已存在的那条。
_stem = os.path.basename(SRC)
NAME = (_stem[:-len('-unsigned.plist')] if _stem.endswith('-unsigned.plist')
        else os.path.splitext(_stem)[0])
DST = (sys.argv[2] if len(sys.argv) > 2
       else os.path.join(os.path.dirname(SRC), NAME + '.shortcut'))

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
    # 强制直连：本机环境变量里挂着 HTTP(S)_PROXY 时，请求会从代理出口出去，
    # 而 HubSign 前置的 Cloudflare 会把这个出口判成异常流量，直接回 403
    # 「Attention Required」。绕开代理反而能正常拿到 200。
    opener = build_opener(ProxyHandler({}))
    with opener.open(req, timeout=180) as resp:
        data = resp.read()
        print('HTTP', resp.status, 'bytes', len(data), 'magic', data[:4])

    if data[:4] != b'AEA1':
        print('FAIL: 返回不是 AEA1')
        print(data[:300])
        return 1

    open(DST, 'wb').write(data)
    print('saved', DST, os.path.getsize(DST), 'B')

    # --- 解包回验：签名后的工作流必须与源文件逐动作一致 ---
    if liblzfse is None:
        print('动作数一致: 未校验（缺 liblzfse，无法解 bvx2 段）')
        print('AEA1 容器:', data[:4] == b'AEA1')
        return 0

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
