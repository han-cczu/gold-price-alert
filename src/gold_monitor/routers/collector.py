"""采集器相关路由：/api/collector/*"""

from fastapi import APIRouter, HTTPException, Query

from ..collector import FetchStrategy, get_collector
from ..state import ws_manager

router = APIRouter()


@router.get("/api/collector/stats")
async def get_collector_stats():
    """获取采集器详细统计"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    return {
        "running": collector.is_running,
        "last_price": {
            "price": collector.last_price.price if collector.last_price else None,
            "source": collector.last_price.source if collector.last_price else None,
            "timestamp": collector.last_price.timestamp.isoformat() if collector.last_price else None
        },
        "stats": collector.stats.to_dict(),
        "config": collector.get_config()
    }


@router.get("/api/collector/config")
async def get_collector_config():
    """获取采集器当前配置"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    return collector.get_config()


@router.post("/api/collector/config")
async def update_collector_config(
    interval: int = Query(None, ge=1, le=3600, description="采集间隔（秒）"),
    strategy: str = Query(None, description="采集策略: single, fallback, parallel_first, parallel_vote")
):
    """运行时修改采集器配置"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    changes = {}

    if interval is not None:
        collector.set_interval(interval)
        changes["interval"] = interval

    if strategy is not None:
        try:
            new_strategy = FetchStrategy(strategy)
            collector.set_strategy(new_strategy)
            changes["strategy"] = strategy
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效的策略: {strategy}，可选: single, fallback, parallel_first, parallel_vote"
            )

    if not changes:
        raise HTTPException(status_code=400, detail="请提供至少一个配置项")

    # 广播配置变更
    await ws_manager.broadcast({
        "type": "config_update",
        "data": changes
    })

    return {
        "message": "配置已更新",
        "changes": changes,
        "current_config": collector.get_config()
    }


@router.post("/api/collector/fill-gaps")
async def fill_data_gaps(hours: int = Query(24, ge=1, le=168)):
    """检测数据间隙并采集当前样本以接续序列

    注意：数据源仅提供当前现货价，无法回填历史时刻的真实价格，
    因此本接口不会伪造历史数据点，只在存在间隙时采集一个当前样本。
    """
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    # fill_gaps 内部会检测间隙；这里不重复检测以免重复累加统计
    recorded = await collector.fill_gaps(hours)

    return {
        "message": (
            "现货数据源无法回填历史价格，"
            f"已采集 {recorded} 个当前样本以接续序列（如需查看间隙详情见 /api/collector/gaps）"
        ),
        "samples_recorded": recorded,
        "backfill_supported": False,
    }


@router.get("/api/collector/gaps")
async def detect_data_gaps(hours: int = Query(24, ge=1, le=168)):
    """检测数据间隙"""
    collector = get_collector()
    if not collector:
        raise HTTPException(status_code=503, detail="采集器未运行")

    gaps = await collector.detect_gaps(hours)

    return {
        "gaps": [
            {
                "start": gap[0].isoformat(),
                "end": gap[1].isoformat(),
                "duration_minutes": (gap[1] - gap[0]).total_seconds() / 60
            }
            for gap in gaps
        ],
        "count": len(gaps)
    }
