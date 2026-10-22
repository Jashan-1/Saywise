from fastapi import APIRouter

from app.api.v1 import decisions, orgs, people, policies, settings

router = APIRouter(prefix="/v1")
router.include_router(orgs.router)
router.include_router(people.router)
router.include_router(settings.router)
router.include_router(policies.router)
router.include_router(decisions.router)
