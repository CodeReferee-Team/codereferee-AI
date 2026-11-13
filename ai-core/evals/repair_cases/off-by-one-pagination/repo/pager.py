def page(items, number, size):
    """Pages are 1-indexed."""
    start = number * size
    return items[start:start + size]
