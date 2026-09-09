"""FastAPI dependencies resolve resources from the current application."""

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

if TYPE_CHECKING:
    from .runtime import ApplicationRuntime


def get_runtime(connection: HTTPConnection) -> "ApplicationRuntime":
    runtime = connection.app.state.runtime
    if not runtime.started:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    return runtime


async def require_admin_dep(request: Request):
    return await request.app.state.runtime.auth.require_admin(request)
