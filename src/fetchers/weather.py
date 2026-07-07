"""WeatherAPI.com — game-time conditions at outdoor parks.

Used for rain-delay risk flags and a mild temperature effect in the HR model.
Domed/retractable parks skip the lookup entirely.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from config import WEATHER_API_KEY
from fetchers import get_with_retry, log_error

BASE = "https://api.weatherapi.com/v1"


def configured() -> bool:
    return bool(WEATHER_API_KEY)


def game_time_forecast(lat: float, lon: float,
                       first_pitch_utc: str) -> Optional[dict]:
    """{temp_f, wind_mph, precip_chance, condition} at the game hour, or None."""
    if not WEATHER_API_KEY:
        return None
    r = get_with_retry(f"{BASE}/forecast.json", params={
        "key": WEATHER_API_KEY, "q": f"{lat},{lon}", "days": 2, "aqi": "no",
    })
    if r is None:
        return None
    try:
        data = r.json()
        target = datetime.fromisoformat(first_pitch_utc.replace("Z", "+00:00"))
        # WeatherAPI hours are park-local; time_epoch is UTC-comparable.
        target_epoch = target.timestamp()
        best, best_delta = None, float("inf")
        for day in data["forecast"]["forecastday"]:
            for hour in day["hour"]:
                delta = abs(hour["time_epoch"] - target_epoch)
                if delta < best_delta:
                    best, best_delta = hour, delta
        if best is None or best_delta > 3 * 3600:
            return None
        return {
            "temp_f": best["temp_f"],
            "wind_mph": best["wind_mph"],
            "precip_chance": max(int(best.get("chance_of_rain") or 0),
                                 int(best.get("chance_of_snow") or 0)),
            "condition": best["condition"]["text"],
        }
    except Exception as exc:
        log_error("weather", str(exc))
        return None
