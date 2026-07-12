"""
Base router: top-level endpoints (liveness probe).
"""

import fastapi


router = fastapi.APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
