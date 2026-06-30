"""数据生命周期相关路由：/api/data/*"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..data_lifecycle import get_lifecycle_manager
from ..state import db, require_admin_dep

router = APIRouter()


def _parse_iso_datetime(value: Optional[str], field_name: str) -> Optional[datetime]:
    """解析可选 ISO 时间参数，非法输入返回明确的 400。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} 必须是有效的 ISO 时间格式"
        ) from exc


@router.get("/api/data/stats")
async def get_data_stats():
    """获取数据统计信息"""
    manager = get_lifecycle_manager(db)
    if not manager:
        return {"error": "生命周期管理器未初始化"}
    return await manager.get_stats()


@router.get("/api/data/export")
async def export_data(
    format: str = Query(default="json", description="导出格式: json, csv"),
    start: Optional[str] = Query(default=None, description="起始时间 (ISO格式)"),
    end: Optional[str] = Query(default=None, description="结束时间 (ISO格式)"),
    limit: int = Query(default=1000, le=10000, description="最大记录数")
):
    """导出价格数据"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")

    start_dt = _parse_iso_datetime(start, "start")
    end_dt = _parse_iso_datetime(end, "end")

    if format.lower() == "csv":
        content = await manager.export_csv(start=start_dt, end=end_dt, limit=limit)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=gold_prices.csv"}
        )
    else:
        content = await manager.export_json(start=start_dt, end=end_dt, limit=limit)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=gold_prices.json"}
        )


@router.post("/api/data/cleanup")
async def cleanup_data(_admin: bool = Depends(require_admin_dep)):
    """执行数据清理（需要管理员权限）"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")

    result = await manager.cleanup()
    return result


@router.post("/api/data/backup")
async def backup_data(backup_name: Optional[str] = None, _admin: bool = Depends(require_admin_dep)):
    """创建数据库备份（需要管理员权限）"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")

    result = await manager.backup_database(backup_name)
    return result


@router.get("/api/data/backups")
async def list_backups():
    """列出所有备份文件"""
    manager = get_lifecycle_manager(db)
    if not manager:
        raise HTTPException(status_code=500, detail="生命周期管理器未初始化")

    backups = await manager.list_backups()
    return {"backups": backups, "count": len(backups)}
