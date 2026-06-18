"""COT Client — fetches CFTC data and returns trading verdicts."""

COT_AVAILABLE = False
_fetcher = None


def init():
    """Initialize COT engine. Safe to call multiple times."""
    global COT_AVAILABLE, _fetcher
    try:
        from cot_fetcher import COTDataFetcher
        _fetcher = COTDataFetcher()
        COT_AVAILABLE = True
        print("[OK] COT engine loaded")
    except ImportError:
        COT_AVAILABLE = False
        print("[WARN] COT not available")


def get_verdict(pair_name):
    """Get COT trading verdict for a pair. Includes JPY inversion."""
    if not COT_AVAILABLE or _fetcher is None:
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "COT недоступен"}
    mapping = {"XAU/USD": "XAU (Золото)", "USD/JPY": "USD/JPY"}
    cot_key = mapping.get(pair_name)
    if not cot_key:
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Нет данных"}
    try:
        data = _fetcher.fetch_latest_data(cot_key, limit=2)
        if not data or len(data) < 2:
            return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Нет данных"}
        analysis = _fetcher.advanced_analysis(cot_key, data)
        if not analysis:
            return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Ошибка"}
        v = analysis.get("verdict", {})

        signal = v.get("signal", "neutral")
        direction = analysis.get("sentiment", {}).get("direction", "neutral")

        # JPY inversion now handled by cot_fetcher.advanced_analysis()

        return {
            "signal": signal,
            "score": v.get("score", 0),
            "direction": direction,
            "text": v.get("text", "N/A"),
        }
    except Exception as e:
        print(f"COT error {pair_name}: {e}")
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Ошибка"}
