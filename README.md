# GPX 路线实验室

本地运行的 GPX 轨迹几何去重工具，包含轨迹对比页面和项目说明页面。

## 功能

- 输入本机 GPX 文件夹，按文件名搜索并选择单条轨迹处理。
- 识别闭环首尾往返尾巴、2–6 圈完整同向重复圈。
- 原始轨迹与处理后轨迹并排 / 叠加查看，支持缩放、平移和坐标检查。
- 下载 GPX、GeoJSON 和 JSON 处理报告；源文件只读。
- 本机处理，离线几何视图，不依赖在线地图或 CDN。

**能力边界：** 尚不支持纯往返自动保留单程、任意内部回头支路去重、唯一道路图或道路级地图匹配。输出不保留时间戳，导航前需人工核对。

## 快速开始

需要 Python 3.12 或更高版本。

```bash
cd gpx_dedupe_project
python -m pip install -r requirements.txt
python web_app.py
```

打开：

- 轨迹对比：<http://127.0.0.1:8765/>
- 项目说明：<http://127.0.0.1:8765/about>

Windows 用户也可双击 `gpx_dedupe_project/start_web.cmd`，优先使用 uv 准备依赖。

在网页中填写自己的 GPX 文件夹即可。当前源码中的默认目录是开发机器的示例配置，其他机器请替换为自己的路径。不要将此服务部署到公网。

## 命令行

```bash
cd gpx_dedupe_project
python route_dedupe.py /path/to/route.gpx --out output --radius 18
```

## 测试

```bash
cd gpx_dedupe_project
python tests/test_route_dedupe.py
python -m unittest discover -s tests -p "test_web_app.py" -v
```

真实 GPX 示例是本机数据，默认不上传；没有该文件时，真实文件测试会跳过，其余合成轨迹与 HTTP 测试仍可运行。浏览器验收脚本位于 `tests/browser_smoke.cjs`，需要 Playwright、浏览器和本机测试目录，不属于默认自动测试。

## 目录

```text
gpx_dedupe_project/
  route_dedupe.py       几何去重算法
  web_app.py            本地 HTTP 服务
  web/                 两个网页及静态资源
  tests/               算法、HTTP 与浏览器测试
  examples/            本地示例位置（真实 GPX 不上传）
  outputs/             每次运行结果（不上传）
```

更多算法细节见 [项目说明文档](gpx_dedupe_project/README.md)，也可启动后访问 `/about`。

## 数据与隐私

`.gitignore` 排除了 GPX、运行结果、截图、缓存、虚拟环境和本地凭据文件。仓库仅包含代码与说明，不包含本机路线数据。添加文件前仍应检查 `git status`，不要强制提交私有轨迹或令牌。
