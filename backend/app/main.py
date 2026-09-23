from fastapi import FastAPI

app = FastAPI(title="DigitalOcean Prototype Service")

@app.get("/health")
def health_check():
    return {"status": "ok"}
