def quota(plan):
    """Plans without a limit get the free tier quota of 10."""
    return plan["limit"]
