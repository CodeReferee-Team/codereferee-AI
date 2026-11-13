def discounted(price, threshold=100):
    """Orders of at least the threshold get 10% off."""
    if price > threshold:
        return round(price * 0.9, 2)
    return price
