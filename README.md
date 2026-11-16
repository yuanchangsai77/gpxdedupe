# GPX 路线

本地运行的 GPX 几何分析与候选裁切工具，包含轨迹对比页面和项目说明页面。

## P0 安全流程

网页默认仅分析：点击「分析路线」只生成候选预览和 JSON 分析报告，不发布转换 GPX/GeoJSON。需要先选择与候选一致的处理目标、确认仅几何证据的限制；主体环还必须明确同意删除两侧接入行程，才能确认转换。已被安全检查阻止的候选不能绕过确认执行。源文件、计划版本或参数变化后需重新分析。

`POST /api/analyze` 生成候选，`POST /api/apply` 严格核验分析、输入 SHA-256、目标与确认后导出，并附 `manifest.json`。旧 `/api/run` 变为仅分析别名，不再直接生成转换文件。分析有效期 30 分钟，内存最多保留 8 份待确认分析；服务重启后需重做。准入锁在读取源文件前获取，繁忙返回 429，不无限排队。

这是完整方案的 P0 增量，不是图重构、异步作业或导航规划。新增保护采用保守拒绝，不保证识别所有往返/平行道路；真实成功率仍未知。原 `route_dedupe.py` 命令行及 `run()` 仍是 **legacy 直接裁切入口**，供回归与兼容使用，不具备网页确认安全流程，勿将其视为安全接口。

## 功能

- 输入本机 GPX 文件夹，按文件名搜索并选择单条轨迹处理。
- 有条件提取闭环主体：检测首尾连续反向重合后裁掉两侧接入行程，可能包含未重合道路；页面显示匹配证据与未匹配裁剪估计。
- 检测 2–6 圈完整、近似等长的同向重复圈；默认单圈至少 450 米，不适用于标准 400 米跑道重复圈。
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
# 以下从仓库根目录运行，覆盖新增 P0 计划与危险负例测试：
# python -m unittest discover -s tests -p "test_*.py" -v
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

说明覆盖 12 类常见结构及组合：开放单程、简单闭环、纯往返、尾巴＋环、不等长接入、完整重复圈、残圈/混向圈、内部支路、8 字/相接双环、共享走廊/多分叉、平行路/立交、断点/漂移。每类说明识别依据、难点、当前行为、规划方案和前后序列对比。

新增 `tests/benchmark_topologies.py`：352 个确定性合成样本、10 个样本族，分开统计正类识别、正确裁切和安全保留；包括噪声下的漏检与误裁。运行 `python tests/benchmark_topologies.py` 生成 `outputs/topology-benchmark.json`。这些是指定合成样本的结果，**不是真实路线成功概率**，也不代表已实现 12 类拓扑分类器。真实标注基准尚未建立。

## 数据与隐私

`.gitignore` 排除了 GPX、运行结果、截图、缓存、虚拟环境和本地凭据文件。仓库仅包含代码与说明，不包含本机路线数据。添加文件前仍应检查 `git status`，不要强制提交私有轨迹或令牌。
