# 成都关键点库与路线重建

保留一条主线：**流式入库 → 稀疏关键点 → 步行规划与质量检查 → 版本化定义及几何 → 查看或按需重绘**。原始轨迹不覆盖，失败不使用原始 GPS 或人工直线补接，没有执行自动去重或发布导航路线。

## 两个展示页

- 成都原始/重建全量对照：<http://localhost:8767/>，源码 `city_view.html`。支持全量密度、同步缩放、点击选线、定义与关键点详情。
- 20 条样本对照与动线：<http://localhost:8767/reconstruction.html>，文件 `../web/reconstruction.html`。保留已审查的样本、指纹和播放效果，也可直接打开文件。

两个页面由同一个只读服务提供。原网站的 `/reconstruction.html` 仍可使用。保留现有道路底图，已经移除高德付费路径规划、Valhalla 对照及旧试验入口。

## 数据与最终结果

大文件集中在 `../outputs/chengdu-city/`，已由仓库忽略规则排除：

| 文件 | 用途 |
|---|---|
| `city.sqlite3` | 全量源证据、路线定义、有序关键点引用、重建几何、版本历史与状态；实际点库及结果库 |
| `delivery-report.json` | 最终数量、性能、失败分布与原始文件 SHA256 核验 |
| `sample-gallery.json` | 当前 20 条样本页的唯一生成数据，包含指纹；不依赖临时目录或旧结果 |
| `chengdu-city.osm`、`osm-source.json` | 实际路网输入、来源请求、校验及许可 |
| `poi.json`、`poi-source.json` | 地理实体输入及来源 |
| `graph-version.txt` | 实际路网版本 |
| `engine/` | GraphHopper JAR、实际配置、已构建的 `graph-cache/` 及缓存来源说明 |
| `map-cache/` | 可重新生成的 PNG 图层缓存 |
| `run.log`、`view.log`、`view-service.json` | 本轮执行记录及展示服务信息 |
| `validation/` | 可丢弃的检查截图，不进入源码结果目录 |

原始输入为 `C:\Users\7281\Downloads\route\chengdu_toproutes.ndjson`，3,291,147,278 字节，没有删除或修改。源码 `results/` 仅保留最终报告 `chengdu-city-delivery.json` 和地图加速测量 `chengdu-city-map-timing.json`。

全量 42,130 条、44,233,784 个 GPS 点已完成处理尝试；4,912 条道路候选、1,153 条待复核、34,916 条未通过、1,149 条预算内未完成。42,130 条定义已归档，84,260 份结果修订保留；逐行 SHA256 全部与导入一致。

最近连续处理的 25,738 条耗时 8,113.55 秒，约 3.17 条/秒，包含失败尝试和入库，不含路网构建与最后归档。全量基线使用 `city-1/sparse-road-4`；保留的新版修复了超长绕行候选的采样异常，尚未重新运行全量。

有定义不等于语义可靠：部分失败记录仅保留未解决的观测起终点。共享实体关联是邻近候选，匿名点保持路线私有。道路证据主要在窗口级，未提供完整逐区间硬边约束；没有校准准确率或独立来源计数。

## 代码职责

| 模块 | 职责 |
|---|---|
| `city_pipeline.py`、`city_limited.py` | 有界导入、分窗批处理、独占锁、状态和断点续跑；受限入口为一个 CPU、1 GiB 地址空间 |
| `city_store.py`、`city_finalize.py`、`city-route.schema.json` | 证据与定义分开、空间索引、有序重复到访、历史修订和核验 |
| `sparse_routing.py`、`geometry.py`、`fast_geometry.py`、`fast_frechet.c` | 稳健选点、顺序/走廊/长度/端点检查和等价数值加速 |
| `adapters.py` | 本地 GraphHopper 接入，不能落实的硬边约束明确拒绝 |
| `endpoint_recovery.py` | 可复用的受限端点回退；最多后续三个点、累计 60 m；150 m 道路探测后仍须 foot 规划及全程质检，未默认改写全量基线 |
| `city_road_catalog.py` | 道路交叉口、入口和 POI 候选导入 |
| `city_rebuild.py` | 复用已保存控制点的按需重绘、同版本缓存和 GeoJSON 输出 |
| `city_server.py`、`city_view.html`、`route_motion.js` | 只读地图、右侧优先、投影缓存、选线与播放；投影缓存位于 Linux 临时目录，可重建 |
| `build_gallery.py`、`comparison.template.html` | 从唯一的样本快照重建样本页；模板是源文件，不是第三个展示入口 |

## 本地运行

以下命令在 `gpx_dedupe_project` 下运行。服务适用于 Linux/WSL，需 NumPy、Pillow；加速内核需 C 编译器。只浏览结果不需要启动路由引擎。

```bash
python3 -m pip install -r route_semantics/requirements.txt
python3 -m route_semantics.fast_geometry
MALLOC_ARENA_MAX=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 -m route_semantics.city_server --database outputs/chengdu-city/city.sqlite3 --port 8767
```

从保留快照重新生成样本页，不调用规划引擎：

```bash
python3 -m route_semantics.build_gallery outputs/chengdu-city/sample-gallery.json web/reconstruction.html
```

入库、状态和受限批处理：

```bash
python3 -m route_semantics.city_pipeline ingest --source /mnt/c/Users/7281/Downloads/route/chengdu_toproutes.ndjson --database outputs/chengdu-city/city.sqlite3
python3 -m route_semantics.city_pipeline status --database outputs/chengdu-city/city.sqlite3
python3 -m route_semantics.city_limited run --database outputs/chengdu-city/city.sqlite3 --graph-version '<graph-version.txt 中的版本>' --workers 2
```

更换算法/路网版本会重新排队，查看结果不应启动全量任务；昂贵计算仍须遵守用户批准的资源与时限。按需重绘需本地引擎，省略 `--force` 可读取同版本几何：

```bash
python3 -m route_semantics.city_rebuild --database outputs/chengdu-city/city.sqlite3 --route-id 227 --graph-version '<graph-version.txt 中的版本>' --force --output outputs/chengdu-city/rebuilt-227.geojson
```

引擎配置模板为 `graphhopper-city.yml`。实际配置使用 `/experiment/graph-cache` 和 `/experiment/chengdu.osm`；启动容器时将 `engine/` 挂到 `/experiment`，将保留的 `chengdu-city.osm` 单独只读挂到 `/experiment/chengdu.osm`。只绑定本机端口，保留 foot、CH、资源限制和 timeout，不改生产服务。

## 验证

保留流水线、地图、稀疏选点、端点回退、失败诊断和动线测试。浏览器入口为 `tests/browser_city_pipeline.cjs`、`tests/browser_sample_gallery.cjs`，截图写入已忽略的 `outputs/chengdu-city/validation/`，不调用付费路径规划。

提交前审查还覆盖了非对象数据行继续导入、密集观测的按需重绘、三维坐标的二维数值校验、归档与重建共用独占锁，以及路网/算法/任务变化后的图层缓存失效。未为这些修复重新运行全量，最终报告仍对应保留的基线实验。
