# GPX Route Deduper V0.1

一个可执行、有限范围的 GPX 轨迹几何去重原型。输入 GPX，产出 `cleaned.gpx`、`cleaned.geojson`、`comparison.png`、`report.json`。

## 算法

1. `xml.etree.ElementTree` 读取 GPX (当前限定恰好一段 `trkseg`)；采用 UTM 本地投影以米计算。
2. 每 10 米重新采样，`cKDTree` 搜索首尾两段的空间重合点。
3. 结合点间距离、反向航向、沿程单调匹配识别共同往返尾巴，并拒绝单个交叉点；要求最短重合 150m。
4. 若尾巴有效，将两个拐点之间的主要闭环保留下来，剪除首尾两段同路往返部分。两个端点相距不超过 20m 才允许作中点闭合；不猜测新道路。
5. 若未发现往返尾巴，针对沿着同一圈重复跑 2–6 圈的情况做整体周期几何匹配，然后保留一圈。
6. 未匹配的路线保持原样，不会随意删去中间的未知分叉。

### 本次 GPX 的结果

原始 46.204km / 1044 点，首尾返回路段各约 1km。切除后约 44.166km / 1008 点。起终点约 16m 横向偏差以两条线的中点拼接（**不是道路导航级地图匹配**）。

### 已知限制

- 不支持把任意多分叉、多次交叉或重叠支路拆成无重复道路图；该问题需要独立的路段级匹配、交点切分与图重构模块。
- 不能跨越单个 GPX 的多个轨迹段：直接合并可能跨越轨迹中断、产生虚构道路。
- 缺少道路平行线和道路等级语义，约 10–20m 的 GPS 近邻可能是不同物理道路。导航前需对接地图匹配/路网校验。
- 一次标准闭环不应该被删除。重复整圈判定只在全线近似周期重复时启动。
- 处理后的 GPX 不保留原始时间戳（裁剪、重定位后原始时间语义不再成立）；可保留原有海拔。
- 压缩长度指实际运动轨迹长度减少，不代表删去了 2km 独有的道路几何。

## 本地网页（两个页面）

- 轨迹对比：http://127.0.0.1:8765/ — 输入本机 GPX 文件夹，搜索、选择单个文件并执行去重；支持并排 / 叠加、图层显隐、同步缩放和平移、坐标表、结果下载。
- 项目说明：http://127.0.0.1:8765/about — 当前功能、各类路线支持矩阵、算法技术路线、限制与后续规划。纯往返 A→B→A、任意内部支路和唯一道路图不是当前已实现的能力。

Windows 可双击 `start_web.cmd`（首次需要联网下载依赖，优先使用 uv）。或者：

```bash
python -m pip install -r requirements.txt
python web_app.py
```

当前机器已经用 uv 准备过依赖，也可以直接运行：

```bash
uv run --offline --no-project --python 3.14 --index-url https://pypi.tuna.tsinghua.edu.cn/simple --with numpy --with scipy --with pyproj --with matplotlib python web_app.py
```

浏览器打开上述地址。停止服务：在启动窗口按 Ctrl+C。网页默认填入以下目录，也可以更换任意本机 GPX 文件夹：

```text
D:\7281\workspace\route\1\komoot\Algeria\Algeria\02_明细\gpx
```

点击“使用示例”可切换到项目的 `examples/` 文件夹。默认不递归，可勾选包含子文件夹；列表上限 2000 个文件，单文件上限 50 MB。原文件只读，网页结果存入 `outputs/<运行ID>/`，不覆盖之前的运行。

网页使用离线几何视图，无在线底图、CDN 或外部地图服务依赖。处理发生在本机 Python 进程中。服务仅允许回环地址，不应用于公网或多用户部署。目录路径由本机用户显式提供；不要打开不信任的 GPX。特别密集、超长轨迹可能消耗较多内存和时间，文件大小限制不是计算量保证。

## 命令行安装与运行

```bash
python -m pip install -r requirements.txt
python route_dedupe.py examples/34044834.gpx --out output
python route_dedupe.py examples/34044834.gpx --out output --radius 18
python tests/test_route_dedupe.py
python -m unittest discover -s tests -p "test_web_app.py"
```

没有依赖在线地图服务。可以用 `report.json` 查看 `retraced_tail` 匹配的两个沿程坐标和端点偏差。

## V0.2 待完成

- 任意位置道路区段自匹配：空间索引 + 线段方向 + 连续区间匹配（可选择 DTW / 局部序列对齐）。
- 实体道路边聚类：同道路的正反方向收敛为同一个 edge_id，记录多次通过次数。
- `Shapely` 路段交点切分、`NetworkX MultiGraph` 拓扑求解；主路径、完整唯一路网分别输出。
- 与 GraphHopper/OSM 对齐，处理平行步道、立交、桥梁。
- 误差标注和不确定匹配的人工确认，确保不会制造虚构的连接。
