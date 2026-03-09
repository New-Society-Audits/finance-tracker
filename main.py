from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from app.auth import router as auth_router
from app.database import init_db

SECRET_KEY = "change-this-before-deploying"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.include_router(auth_router)


@app.get("/")
def root():
    return {"message": "Hello World"}


if __name__ == "__mai
n__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
