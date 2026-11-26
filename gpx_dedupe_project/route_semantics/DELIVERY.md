# 算法包交付与运行说明

本包交付的是离线路线处理算法及可复现的运行入口。页面用于检查算法结果，接收方不需要把页面当作App组件使用。对外输入是轨迹或已有路线库；对外输出是筛选清单、完整路线GeoJSON、统计报告和可选检查页面。

## 1. 包含与不包含

包含：原始轨迹导入、路线定义与关键点、步行规划适配和质量检查、全量密度统计、高低密度表达选择、高密度空网格补充、结果导出、说明和单元测试。默认交付入口不启用去重。

不包含：成都真实轨迹、真实SQLite库、OSM大文件、GraphHopper JAR和图缓存、Python/Node运行时。要复现成都真实效果，需要另交付经授权的数据或由接收方提供同格式数据。内置14条轨迹是明确标记的合成样例，不是成都真实业务轨迹。

## 2. 环境

- Linux或WSL；当前入口依赖Linux文件锁、CPU亲和性和资源限制，未验证原生Windows/macOS。
- 本次验证：Python 3.11.15、NumPy 2.4.6、Pillow 12.2.0、Node.js 26.1.0。其他版本未做完整兼容验证。
- C编译器用于构建数值加速内核；仅运行密度样例不要求编译，重建完整测试前应编译。
- 新重建需要接收方准备本地GraphHopper步行服务。本项目实际实验使用11.0及foot配置；OSM图必须覆盖路线经过的全部区域。

进入解压后的包根目录，以下相对路径均以此为准：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m route_semantics.fast_geometry
node --version
```

若使用现有依赖环境，可直接运行。锁文件固定本次测试的两个Python依赖；安装这些依赖仍需要可用的包源或接收方预先准备的离线wheel，本ZIP不是完整离线运行时镜像。

## 3. 最小样例：不启动规划器也能验证交付

```bash
python -m route_semantics.city_pipeline ingest --source route_semantics/examples/synthetic.ndjson --database work/demo.sqlite3
python -m route_semantics.delivery --database work/demo.sqlite3 --output-dir work/demo-result --region 合成样例
python -m http.server 8768 --bind 127.0.0.1 --directory work/demo-result
```

打开 <http://localhost:8768/>。样例预期：14条输入、3条原始入选，其中1条补充高密度空格，11条暂不默认展示。因为没有规划结果，重建数为0。这验证导入、密度、最终选择、补充和导出，不验证真实道路重建质量。

## 4. 使用已有原始/定义/重建库

若接收方拿到本项目格式的数据库，筛选阶段无需原始NDJSON文件和规划服务：

```bash
python -m route_semantics.delivery --database data/city.sqlite3 --output-dir work/city-result --region 成都 --threshold 10 --low-fraction 0.2 --minimum-meters 500
```

输入数据库只读，不会重新规划或修改原始证据。输出目录必须与输入数据库目录分开。重复运行可复用同一密度索引；更换源数据需新目录。改变阈值会更新该输出目录的决策和文件，需要保留不同结果时使用不同目录。

CLI非零退出表示本轮失败。run-state.json记录本轮running/succeeded/failed；即使旧report.json仍在，也必须以本轮状态和退出码为准。突然终止可能留下running状态，需续跑，不能视为成功。新参数建议使用新输出目录。

最终入口统一执行：全量5公里统计→初步状态→检查页面数据准备→与页面共用的最终策略及空格补充→事务保存默认决策→清单/GeoJSON/报告/页面。接收方无需手工同步中间JSON和SQLite。

| 参数 | 默认值 | 含义 |
|---|---:|---|
| threshold | 10 | 5×5公里格内不同路线数≤此值为低密度 |
| low-fraction | 0.2 | 原始优先所需低密度长度占全程比例 |
| minimum-meters | 500 | 原始优先累计低密度长度门槛，同时用于空格补充的新增长度门槛；短路线要求全长 |
| region | 路线 | 页面和报告的数据集名称，不改变算法 |
| node | node | Node可执行文件名或路径 |

当前交付固定5公里格、20米以内的长度加权积分、1000米观测间隙检查。更改格尺度或采样策略需算法版本更新，不作为CLI可调参数。

默认入口限制单CPU亲和性及每进程2GiB地址空间；Node继承限制，但这不是整个进程树的容器级总内存限制。仅离线运行，不部署线上接口。

## 5. 从真实轨迹开始，并生成新重建

现有导入入口要求每行一个JSON对象的NDJSON，不是GeoJSON，也不是通用GPX直接导入：

```json
{"activities":[96],"segments":[{"routePoints":[{"longitude":104.06,"latitude":30.67},{"longitude":104.07,"latitude":30.67}]}]}
```

坐标为WGS84，经度在前。每段至少2点，不能连接不连续分段。当前入口使用经纬度，不把时间和海拔纳入完整质量模型。原始GPX或其他格式先转换为此结构；包内未宣称已有通用转换器。导入单记录最多32MiB、25万点；异常保留失败记录或拒绝超限，不应通过拆错分段绕过限制。

```bash
python -m route_semantics.city_pipeline ingest --source data/routes.ndjson --database work/routes.sqlite3
```

接收方先准备并启动仅本机可访问的GraphHopper服务，监听示例地址 `http://127.0.0.1:18989`，必须配置foot及edge_key/osm_way_id路径详情。提供的graphhopper-city.yml是配置模板，其OSM路径、图目录和端口映射需按部署调整；包不自动下载路网、JAR或建立Docker服务。

先验证一个小批次，graph-version必须使用真实图的版本标识，不能把更换后的图冒用旧版本：

```bash
python -m route_semantics.city_pipeline run --database work/routes.sqlite3 --url http://127.0.0.1:18989 --graph-version '<实际路网版本>' --workers 1 --limit 20
python -m route_semantics.city_pipeline status --database work/routes.sqlite3
```

确认结果后按接收方计算预算运行全量，再归档定义并筛选：

```bash
python -m route_semantics.city_limited run --database work/routes.sqlite3 --url http://127.0.0.1:18989 --graph-version '<实际路网版本>' --workers 2
python -m route_semantics.delivery --database work/routes.sqlite3 --output-dir work/routes-result --region 目标区域
```

city_limited会在无limit全量结束后执行归档；GraphHopper自身CPU/内存需要接收方另行限制。算法或图版本改变可能重新排队，先在工作副本验证，不对生产库直接运行。入库续跑应保持源路径、大小和修改时间；更换文件位置可能被识别为新来源，不要无检查地重复导入。

当前成都实测是先全量重建、再按密度筛选。包没有宣称已实现“先密度分流、只规划高密度”的自动任务队列。

## 6. 输出文件

| 文件 | 用途 |
|---|---|
| report.json | 本次参数、统计、各阈值结果、完整性及本次入口耗时 |
| run-state.json | 本轮运行状态；失败时不把旧成功报告误认为本轮结果 |
| decisions.csv | 每条路线是否入选、几何类型、低密度长度/比例及理由，含未入选记录 |
| selected_routes.ndjson | 入选路线编号、几何类型和理由；是清单，不是几何文件 |
| routes.geojson | 入选路线完整几何，WGS84、MultiLineString，保留原分段 |
| coverage.sqlite3 | 可复用的密度索引和最终默认决策，不代替源证据库 |
| policy.json | 与页面共用策略生成的完整逐条决定和多阈值统计 |
| index.html | 可直接打开或用静态服务查看的检查页面，不作为App全量加载方案 |

GeoJSON属性：route_id关联输入库；geometry_kind为original或rebuilt；reason解释选择；source_sha256关联原始证据；重建额外保留road_graph_version/algorithm_version；navigation_verified始终为false。只对已选路线流式导出完整存储几何，不使用页面抽稀几何充当正式结果。

页面调节只作对比，不自动回写已导出的CSV、GeoJSON和默认报告。要交付另一组参数，用CLI重新运行对应参数。

## 7. 验收

```bash
python -m unittest discover -s tests -p 'test_*.py'
node tests/test_density_policy.cjs
node tests/test_rebuilt_dedup.cjs
node tests/test_route_motion.cjs
```

完整Python测试前先编译数值内核。核对report总数、CSV记录数、清单数和GeoJSON要素数；使用合成样例应得到3条入选。再拿接收方少量真实轨迹做道路质量和输入坐标检查，不能用合成样例证明真实准确率。

## 8. 成都数据另行交付

若需要同样的成都结果，另交付city.sqlite3（约2.1GiB，含原始/定义/重建）、coverage/full.sqlite3和对应报告；现成展示HTML可作为演示附件。要重新规划还需对应版本OSM/图缓存/引擎，不能只交关键点就假设任何图都能复原。真实数据不默认放入算法ZIP。

已取得的结果：42130条输入，默认5370条展示（4900重建、470原始，含54条空格补充）。严格去重实验仅减少1条，不作为主算法能力或本次验收核心。

尚未完成：生产App接口、服务端视窗加载接入、完整轨迹质量模型、人工准确率标注、独立来源统计和大规模语义去重。离线文件契约不等于已修改现有App/Cloud接口。
