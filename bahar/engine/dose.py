"""Inhaled PM2.5 dose of a session, as a share of a reference resting day.

  dose_ug = PM (µg/m³) x ventilation (L/min) / 1000 (L per m³) x duration (min)
  reference day = 60 µg/m³ x 8 L/min x 1440 min / 1000 ≈ 691 µg (weights.DOSE_REF_*)
"""

from bahar.config import weights as w

REFERENCE_DAY_UG = w.DOSE_REF_PM25 * w.DOSE_REF_VENTILATION_LPM * w.DOSE_REF_MIN / 1000


def session_dose_ug(pm25: float, ventilation_lpm: float, duration_min: float) -> float:
    return pm25 * ventilation_lpm / 1000 * duration_min


def dose_share_pct(dose_ug: float) -> float:
    """Session dose as % of a full day breathing 'satisfactory' air at rest."""
    return 100 * dose_ug / REFERENCE_DAY_UG
