# 前端第三方库（自托管）

页面不再从公共 CDN 加载脚本，避免 CDN 不可用或版本漂移。文件来自 npm 发布包，
下载后与 jsDelivr 提供的 SHA-256 核对一致。

| 文件 | 包 | 版本 | 许可 |
| --- | --- | --- | --- |
| echarts.min.js | echarts (`dist/echarts.min.js`) | 5.4.3 | Apache-2.0 |
| marked.umd.js | marked (`lib/marked.umd.js`) | 18.0.12 | MIT |
| purify.min.js | dompurify (`dist/purify.min.js`) | 3.2.6 | Apache-2.0 / MPL-2.0 |

升级步骤：下载对应版本文件替换，核对 `https://data.jsdelivr.com/v1/package/npm/<包>@<版本>/flat`
中的 hash，更新本表，然后运行 `pytest tests/test_static_frontend.py`。
