# 南京主城区核心范围合成排水管网

- 范围（WGS84）：经度 118.745–118.835，纬度 32.010–32.090；这是矩形建模框，不是行政边界。
- 配置：`nanjing_core.yml`；SWMManywhere 0.2.2，`outfall_derivation.method: withtopo`。
- SWMM 输入：`output/nanjing_core/bbox_1/model_4/model_4.inp`。
- GIS 数据：同目录下的 `nodes.geoparquet`、`edges.geoparquet`、`subcatchments.geoparquet`。
- 输入中有 18,132 条管段、301 个排口、18,132 个蓄水节点和 15,600 个子汇水区。`storm.dat` 已复制到模型目录，输入文件中的降雨文件路径已改为该副本。

这是基于公开道路、建筑和高程数据推导的**合成管网**，不是南京现状地下管线测绘成果。配置中 `run_model: false`，尚未做水力模拟或校准。运行期间的两个兼容问题已处理：Overture 发布版本缓存改为 `2026-09-23.1`，Python 环境的 `affine` 固定为 2.4.0。
