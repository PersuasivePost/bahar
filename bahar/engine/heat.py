"""Outdoor WBGT (wet-bulb globe temperature) per hour, and its heat-stress flag.

  WBGT = 0.7 Tnwb + 0.2 Tg + 0.1 Ta                     (Yaglou & Minard 1957; ISO 7243)

Tg (black globe) comes from Liljegren et al. (2008), "Modeling the wet bulb globe
temperature using standard meteorological measurements", J. Occup. Environ. Hyg. 5:645-655,
globe energy balance:

  Tg^4 = 1/2 (1 + eps_a) Ta^4 - h / (eps_g sigma) (Tg - Ta)
         + S / (2 eps_g sigma) (1 - alpha_g) [f_db (1 / (2 cos z) - 1) + 1 + alpha_sfc]

with eps_a = 0.575 e_a^(1/7) (Brutsaert-type clear-sky emissivity, e_a in hPa), solved by
bisection (the left side minus the right is monotonic in Tg). Simplifications, documented:
convection uses the ISO 7726 forced-convection coefficient for a 150 mm globe instead of
Liljegren's Reynolds-number fit, the surface is assumed at air temperature, and Tnwb is
approximated by the psychrometric wet bulb (Open-Meteo's, or Stull 2011), which reads up to
~1 °C low in strong sun and still air. ECMWF thermofeel was not adopted: it needs mean radiant
temperature inputs (direct normal / longwave fluxes) the forecast does not provide per venue.
"""

import math

from bahar.config import weights as w
from bahar.models import HeatFlag

_SIGMA = 5.670374419e-8  # Stefan-Boltzmann, W m-2 K-4
_BISECT_STEPS = 40  # 80 K bracket / 2^40: far below any meaningful precision


def stull_wet_bulb_c(temp_c: float, rh: float) -> float:
    """Psychrometric wet bulb at sea-level pressure, Stull (2011), J. Appl. Meteor. Climatol.
    50:2267; valid for RH 5-99 % and -20..50 °C, error within about ±1 °C."""
    return (
        temp_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(temp_c + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh**1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )


def vapour_pressure_hpa(dew_c: float) -> float:
    """Actual vapour pressure from the dew point (Magnus formula, Bolton 1980)."""
    return 6.112 * math.exp(17.67 * dew_c / (dew_c + 243.5))


def globe_temperature_c(
    temp_c: float, dew_c: float, wind_kmh: float, swr: float, direct_rad: float | None,
    cos_zenith: float,
) -> float:  # fmt: skip
    """Black-globe temperature (Liljegren 2008, simplified as in the module docstring)."""
    ta = temp_c - w.ABSOLUTE_ZERO_C
    u2 = max(w.MIN_WIND_MS, wind_kmh / 3.6 * w.WIND_10M_TO_2M)
    h = 6.3 * u2**0.6 / w.GLOBE_DIAMETER_M**0.4  # ISO 7726 forced convection, W m-2 K-1
    eps_a = 0.575 * vapour_pressure_hpa(dew_c) ** (1 / 7)
    # Direct-beam fraction; without a direct-radiation value treat all sunlight as diffuse.
    f_db = min(1.0, max(0.0, direct_rad / swr)) if swr > 0 and direct_rad is not None else 0.0
    cos_z = max(w.MIN_COS_ZENITH, cos_zenith)
    eps_g = w.GLOBE_EMISSIVITY
    rhs = 0.5 * (1 + eps_a) * ta**4 + swr / (2 * eps_g * _SIGMA) * (1 - w.GLOBE_ALBEDO) * (
        f_db * (1 / (2 * cos_z) - 1) + 1 + w.SURFACE_ALBEDO
    )
    conv = h / (eps_g * _SIGMA)
    lo, hi = ta - 20.0, ta + 60.0
    for _ in range(_BISECT_STEPS):
        mid = (lo + hi) / 2
        if mid**4 + conv * (mid - ta) > rhs:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2 + w.ABSOLUTE_ZERO_C


def wbgt_c(
    temp_c: float, rh: float, dew_c: float, wind_kmh: float, swr: float,
    direct_rad: float | None, cos_zenith: float, wet_bulb_c: float | None = None,
) -> float:  # fmt: skip
    """Outdoor WBGT in °C. `cos_zenith` should describe the hour the radiation was averaged
    over (Open-Meteo radiation is the mean of the preceding hour)."""
    tnwb = wet_bulb_c if wet_bulb_c is not None else stull_wet_bulb_c(temp_c, rh)
    tg = globe_temperature_c(temp_c, dew_c, wind_kmh, swr, direct_rad, cos_zenith)
    return 0.7 * tnwb + 0.2 * tg + 0.1 * temp_c


def heat_flag(wbgt: float) -> HeatFlag:
    for lower, flag in w.WBGT_FLAGS:
        if wbgt >= lower:
            return flag
    return HeatFlag.low
