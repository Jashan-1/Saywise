from fastapi import APIRouter

from app.api.v1 import orgs, people, settings

router = APIRouter(prefix="/v1")
router.include_router(orgs.router)
router.include_router(people.router)
router.include_router(settings.router)
