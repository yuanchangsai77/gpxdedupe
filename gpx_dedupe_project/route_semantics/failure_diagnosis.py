"""Explain experimental failures without changing routing acceptance thresholds."""


def diagnose(product):
    if product["mode"] == "road_candidate":
        return {
            "category": "road_candidate",
            "review_preview": False,
            "message": "全程道路候选，仍需审核。",
        }
    if product["mode"] == "facility_unavailable":
        return {
            "category": "facility_network_missing",
            "review_preview": False,
            "message": "操场缺少可路由的跑道网络。需要独立核实设施几何与通行条件；目前只展示原始对照。",
        }
    metrics = product.get("metrics")
    if not metrics:
        if any(
            "resource_limit" in a.get("reason", "")
            or "request_budget" in a.get("reason", "")
            for a in product.get("attempts", [])
        ):
            return {
                "category": "validation_resource_limit",
                "review_preview": False,
                "message": "候选或校验触及资源上限，需要分窗或增加受控预算；这不代表实际道路无法通过。",
            }
        errors = [a.get("engine_error", "") for a in product.get("attempts", [])]
        point_missing = any(
            "PointNotFound" in e or "Cannot find point" in e for e in errors
        )
        return {
            "category": "unsnappable_control"
            if point_missing
            else "engine_connection_failed",
            "review_preview": False,
            "message": "存在无法吸附到当前步行路网的关键点，需核查具体位置、地图覆盖和通行属性。"
            if point_missing
            else "当前步行引擎未生成连接路线；需区分路网缺失、拓扑断开与通行限制，不能据此断言现实中无法通过。",
        }
    near = (
        max(metrics["forward_p95_m"], metrics["reverse_p95_m"]) <= 65
        and metrics["maximum_corridor_error_m"] <= 85
        and metrics["ordered_error_m"] <= 85
        and 0.7 <= metrics["distance_ratio"] <= 1.35
        and metrics["endpoint_snap_m"] <= 60
    )
    if near:
        return {
            "category": "near_candidate_review",
            "review_preview": True,
            "message": "已有较接近的道路候选，但偏差或端点吸附未通过检查。默认以紫色显示供复核，不算重建通过。",
        }
    if metrics["distance_ratio"] < 0.7:
        return {
            "category": "missing_journey",
            "review_preview": False,
            "message": "候选少走了部分行程或重复圈次；需要保留关键折返点与经过次序，不能用更短路线替代。",
        }
    if metrics["reverse_p95_m"] > 150 or metrics["distance_ratio"] > 1.5:
        return {
            "category": "unsupported_detour",
            "review_preview": False,
            "message": "候选有明显绕行，可能涉及错误吸附、道路断连或通行限制。异常候选仅供手动诊断，需定位异常路段后解决。",
        }
    return {
        "category": "corridor_or_order_mismatch",
        "review_preview": False,
        "message": "候选改变了主要走廊或行进顺序，尚不能作为重建路线；需进一步核查分支和路网。",
    }
