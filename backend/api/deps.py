from typing import Annotated

from fastapi import Depends, Header, Request


async def verified_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    return await request.app.state.db.authenticate(authorization)


User = Annotated[str, Depends(verified_user)]
