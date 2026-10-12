from fastapi import FastAPI

app = FastAPI()


def compute_quota(plan: dict) -> int:
    # BUG: assumes the "limit" key always exists. Free plans omit it,
    # so the "/" handler raises KeyError at request time -> HTTP 500.
    return plan["limit"] * 2


@app.get("/")
def root():
    plan = {"name": "free"}  # no "limit" key
    return {"quota": compute_quota(plan)}


@app.get("/health")
def health():
    return {"status": "healthy"}
